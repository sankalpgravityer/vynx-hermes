"""Background removal, in Hermes, with the result checked before it is returned.

    remove_background(jpeg_or_png_bytes) -> (png, error, provider)

WHY THIS IS HERE AND NOT IN VNYX-API

vnyx-api's `removeBackground` dispatches to four providers, three of which post
to vnyxremoveapi.vnyx.ai. That host sits behind Cloudflare, whose origin timeout
is 100 seconds and cannot be raised below Enterprise, and a segmentation call
routinely takes longer — so the request comes back as a 524 error PAGE: HTML,
served with a 200-shaped body that no caller inspected. Two images on BOA-006166
took 240 seconds and produced nothing while the step reported success.

Doing it here removes the hop entirely. Hermes already holds the Gemini and
OpenAI keys, already owns the image plumbing, and — the part that was missing —
already knows how to tell a real cut-out from a fail-soft one.

THE RESULT IS VERIFIED, and that is the point rather than a nicety. The docstring
of app/imaging/background.py exists because of this exact failure: "every
provider path in vnyx-api retries a 429 forever but returns whatever the API
hands back on a 200, so a provider that fails soft -- returning the original
bytes, or a barely-touched image -- produces a row marked BG_REMOVED sitting on
top of an untouched photograph. Nothing downstream can tell."

A generative model fails soft more readily than a segmenter: asked to remove a
background it will sometimes hand back a tidied version of the same photograph.
So every candidate goes through `classify_pixels` before it is accepted, and a
provider that returns the picture unchanged is treated as having failed — which
is what sends it to the next one.

ORDER: Gemini, then OpenAI. Gemini is the cheaper call and the one the tenants
are already configured for; OpenAI's edits endpoint is the fallback, the same
escalation the render path uses.
"""
from __future__ import annotations

import base64
import io
import logging
import os
import re
import time
from copy import deepcopy
from typing import Any

from app.config import policy, settings
from app.imaging import background as bgcheck
from app.imaging import openai_image
from app.net import genai_client_args

log = logging.getLogger("hermes.cutout")

# The `imagery.cutout` policy block, with its defaults. Everything items 6, 7
# and 8 of docs/PICTURE-CHECK-FIXES.md decide is a number here, so a threshold
# can be moved from config/policy.yaml with a reload and no code change — the
# same arrangement `readiness.cutouts` has for the checks in cutouts.py.
DEFAULTS: dict[str, Any] = {
    # The long edge every pixel comparison below is made on. 448 to match
    # `readiness.cutouts.garment_check.work_width`: the two ask the same
    # question of the same garment and a different grid would give them
    # different answers about the same picture.
    "work_px": 448,
    # --- item 7: the set the segmenter left behind -------------------------
    #
    # A cut-out is judged against the SOURCE's backdrop when the source has one
    # (the original path), and against ITS OWN otherwise — which is every
    # studio sweep with uneven lighting, the case that used to abstain.
    #
    # The fraction of the KEPT pixels one contiguous backdrop-coloured region
    # may cover. 0.01 is the bar the source-backdrop path already uses and the
    # measurements either side of it are wider here than they were there: the
    # Nike booth cut-out (`fc8e53e7`, cloth-seg, podium kept) measures 1.70%
    # against its own white backdrop, and the same cut-out with the podium
    # taken off measures 0.01% — a factor of 170.
    "leftover_region_max": 0.01,
    # THE BOTTOM BAND, SEPARATELY, BECAUSE A PODIUM IS ALWAYS THERE. The
    # bottom this fraction of the KEPT region's own rows — not the frame's,
    # which on `fc8e53e7` is transparent under the podium — and the fraction of
    # the pixels kept there that are the backdrop's colour rather than garment.
    # Measured: 31.9% with the podium, 0.0% without it. 0.25 sits between them
    # and well clear of both.
    "leftover_bottom_fraction": 0.10,
    "leftover_bottom_max": 0.25,
    # WHEN THIS QUESTION MAY NOT BE PUT AT ALL. The garment has to be separable
    # from the cut-out's own backdrop before "opaque, and the backdrop's
    # colour" can mean "left over": a white shirt on a white backdrop reads as
    # backdrop everywhere and every pixel the segmenter kept would look like a
    # podium. So at least this much of what was kept must differ from the
    # backdrop, or the measurement is `unknown` and NOTHING is refused — the
    # same rule §0 sets for every derived mask.
    "leftover_garment_min": 0.50,
    # Some of the picture must actually be clear. A fully opaque result is a
    # composited cut-out (§0), where "opaque" is the whole frame and this
    # measurement would refuse everything; `garment_kept` is the check that
    # reads those.
    "leftover_min_clear": 0.02,
    # THE DEFAULT CHAIN — what runs when a caller names no strategies.
    #
    # CLOTH-SEG ALONE, AND THAT IS A COST DECISION (19 Sep 2026). The four
    # strategies are not equally priced: cloth-seg is a local ONNX graph and
    # costs nothing per image, while the other three are paid API calls tried
    # TWICE each, so a photograph cloth-seg cannot cut walks six paid calls
    # before the chain gives up. On MID-000132 that was six calls a view across
    # four views — twenty-four paid calls — and it produced no cut-out at all.
    #
    # It was affordable while it never ran: the re-matte was refused outright
    # for want of `--keep-better` in vnyx-api, so nothing reached this chain.
    # Implementing that guard is what switched it on, and the bill followed.
    #
    # So the paid strategies are now OPT-IN. What is lost is real and measured —
    # a cut-out with a podium left in stays as it is, because cloth-seg returns
    # the same podium — and the honest outcome for those is the cut-out on file
    # rather than a paid attempt at a better one. Widen this list to spend again.
    "strategies": ["cloth-seg"],
    # --- item 6: a leftover is fixed with a DIFFERENT segmenter ------------
    #
    # Re-matting a stand with cloth-seg produces the same stand — it is a
    # clothing parser, the podium is directly beneath the clothing, and its
    # mask runs at 320px (§1.1). So a re-matte asked for because something was
    # LEFT IN goes to the mask strategies instead, in this order.
    #
    # EMPTY, for the reason above: both names in it are paid. A leftover is
    # therefore left alone — see `rematte_strategies`, which now says so rather
    # than quietly falling back to a cloth-seg re-cut that reproduces the stand.
    "leftover_strategies": [],
    # …and the result is intersected with cloth-seg, so the garment parser
    # still decides what is cloth. Measured on the same Nike tee: cloth-seg
    # removed the hanger and the form's neck and kept the whole podium; a
    # matting model removed the podium and kept the hanger. The intersection is
    # the only combination that removes both.
    "intersect_cloth": True,
    # --- item 8: a replacement is never worse than what it replaces --------
    #
    # How much less garment the new cut-out may have than the one it would
    # supersede, both segmented against their own flat backdrop. KIL-001625's
    # re-matte took "a large part of shirt back" out; two segmenters honestly
    # disagree about a hem or a feathered edge by a percent or two, and that is
    # what the room below 0.10 is for.
    "replace_area_drop_max": 0.10,
    # …and the same defect when it is too small to move the total: KIL-001644
    # lost one strap. A SINGLE PIECE of the garment this big, missing from the
    # new cut-out and touching the garment's edge, is refused on its own.
    # Opened first (`replace_open_px`) so the thin rim two segmenters always
    # differ by cannot add up to one — a 1px outline around a whole garment is
    # one connected region of several percent and is not a missing strap.
    "replace_loss_region_max": 0.02,
    "replace_open_px": 5,
    # Two cut-outs of different SHAPES are not the same picture measured twice,
    # and a pixel comparison between them would be nonsense. Beyond this the
    # replacement is not judged (and therefore not refused).
    "replace_ratio_tolerance": 0.05,
    # …AND THE RATIO TEST CANNOT SEE A ZOOM, which is what these four are for.
    #
    # A cut-out cropped to the garment and padded back out keeps the source
    # RATIO, so it passes the tolerance above and then loses the pixel
    # comparison every time — its garment covers three times the canvas, so a
    # correctly framed replacement reads as "66% less garment" and is refused.
    # MID-000650's FRONT sat at 0.3285 of its frame against a known-good BACK at
    # 0.1175, and the cloth-seg replacement that matched the BACK (0.1113) was
    # turned away on exactly that arithmetic.
    #
    # So: outside this scale band the two are not framed alike, and the masks
    # are compared as SHAPES instead — each cropped to its bounding box and
    # resized to one grid. 1.25 is a quarter larger in each direction, well
    # above the few percent two segmenters differ by and well below the 1.7x
    # that pair measured.
    "replace_same_scale_max": 1.25,
    # HOW EQUALLY THE TWO AXES MUST SCALE to count as a reframing rather than a
    # loss. A zoom moves both alike; a mask that ate the shirt back shortens the
    # box on one axis only. Measured: reframings 0.973 and 0.990, losses 0.561
    # and 0.567 — nothing lands between, so 0.90 is safe in both directions.
    "replace_uniform_min": 0.90,
    # How alike the two outlines must be to be worth comparing at all. A COARSE
    # GATE — it separates "the same garment reframed" from "two different
    # pictures", and nothing more. The real judgement is the ordinary area and
    # edge-piece tests, re-run on the normalised pair, because an outline test
    # cannot do their job: KIL-001644's lost strap still matches at 95%.
    # Low on purpose, so a genuine loss reaches those rules instead of being
    # turned away here. The measured reframing scored 0.978.
    "replace_shape_iou_min": 0.80,
    "replace_shape_grid": 256,
}


def config(pol: dict[str, Any] | None = None) -> dict[str, Any]:
    """The `imagery.cutout` block with defaults filled in."""
    out = deepcopy(DEFAULTS)
    over = ((pol if pol is not None else policy()).get("imagery") or {}).get("cutout") or {}
    for k, v in over.items():
        if v is not None:
            out[k] = v
    return out

# Written for a SEGMENTER's job, not an artist's. Every clause is there because
# a generative model will otherwise take liberties: re-light the garment, crop
# it, straighten it, or paint a white card behind it and call that removed.
# CHROMA KEY, NOT "MAKE IT TRANSPARENT".
#
# Asking for transparency does not work, and the failure is instructive: the
# model understands the request perfectly and then cannot express it. Gemini's
# image output is JPEG, which has no alpha channel, so it renders the
# TRANSPARENCY CHECKERBOARD as literal grey-and-white pixels — a picture OF
# transparency. The cut-out is flawless and the file has a busy background, so
# the pixel classifier correctly reports "a real background is still there" and
# the result is thrown away.
#
# So the model is asked for something it can express — a flat, saturated colour
# — and the alpha is produced here, deterministically, by keying that colour
# out. Magenta because no garment is #FF00FF: keying white would eat a white
# shirt, and keying green would eat anything olive.
KEY_RGB = (255, 0, 255)

PROMPT = (
    "Isolate ONLY the garment in this photograph and place it on a solid "
    "background of pure magenta, RGB (255, 0, 255).\n"
    "\n"
    "EVERYTHING that is not the garment must become magenta — to the very edges "
    "of the frame, top to bottom. That includes the mannequin, the mannequin's "
    "stand and base, the podium or table it rests on, the floor, the walls, any "
    "backdrop, and anything else in the room such as a ladder, a rail or a "
    "clamp. Do not leave the lower part of the frame untouched.\n"
    "\n"
    "Return the SAME garment, unchanged: identical colour, identical texture, "
    "identical shape, identical orientation, identical framing and scale. Do not "
    "restyle, relight, retouch, straighten, crop or re-centre it.\n"
    "\n"
    "The background must be FLAT, UNIFORM magenta with no gradient, no shadow "
    "and no reflection. Any gap you can see through — between a strap and the "
    "body, inside a handle, through a buttonhole — must also be magenta.\n"
    "\n"
    "Do not draw a checkerboard. Do not add a border. Magenta must appear "
    "nowhere on the garment itself."
)

# How far from pure magenta a pixel may sit and still count as background.
# Generous, because JPEG compression bleeds colour badly at a hard edge against
# a saturated field — the ringing around the garment outline is the whole reason
# this is a distance rather than an equality test.
KEY_TOLERANCE = 90


# ASK FOR A MASK, NOT A PICTURE. This is the primary strategy; the magenta
# prompt above is now the fallback.
#
# WHY. Both failures we actually observed come from the same root cause — the
# model is asked to hand back THE PHOTOGRAPH with pixels changed, so it treats
# the photograph as its to rewrite:
#
#   gemini  painted the top two thirds magenta and left the mannequin stand,
#           the floor and a ladder leg untouched  (border varies by 74.5/255)
#   openai  returned a clean, plausible, entirely synthetic t-shirt — smoothed
#           fabric, idealised silhouette, 1024x1024 from a 3000x4000 portrait
#
# A mask removes the opportunity. The model never returns garment pixels, so it
# cannot smooth, relight, restyle or reframe them; it answers one question —
# where is the garment — and the alpha is applied HERE, to the original bytes,
# at the original resolution. Fidelity stops being something we inspect after
# the fact and becomes true by construction, which is the only version of it
# worth having on a resale listing.
#
# It also fixes the resolution loss for free: the old path could only ever
# return what the model rendered (~900-2K on the long edge), so a 3000x4000
# studio original came back as the smallest asset on the listing. A mask is
# resized up and applied to the full-size original.
MASK_PROMPT = (
    "Produce a SILHOUETTE MASK of the garment in this photograph.\n"
    "\n"
    "Output a pure black-and-white image, the same shape and framing as the "
    "input. Paint PURE WHITE, RGB (255,255,255), over every pixel that is part "
    "of the garment. Paint PURE BLACK, RGB (0,0,0), over every other pixel — "
    "the mannequin, its stand and base, the podium, the floor, the walls, the "
    "backdrop, and anything else in the room such as a ladder or a rail.\n"
    "\n"
    "Black must reach the very edges of the frame, top to bottom and left to "
    "right. Do not leave any part of the frame unpainted.\n"
    "\n"
    "Any gap you can see through — between a strap and the body, inside a "
    "handle, through a buttonhole — is NOT part of the garment and must be "
    "black.\n"
    "\n"
    "Use only black and white. No grey, no colour, no gradient, no shading, no "
    "texture, no outline. Do not draw the garment itself; draw only its shape."
)

# A mask pixel is background below this and garment above it; the band between
# becomes a soft edge. Wide, because the model's boundary is antialiased and a
# hard threshold on an antialiased edge is what produces a jagged cut-out.
MASK_LO, MASK_HI = 64, 192


def _key_out(data: bytes) -> tuple[bytes | None, str | None]:
    """Turn the keyed background into real alpha. Returns (png, error)."""
    from PIL import Image
    import numpy as np

    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not decode the model's output ({exc.__class__.__name__})"

    # int32, NOT int16. A channel difference of 255 squares to 65,025, which
    # overflows int16 (max 32,767) and wraps NEGATIVE — numpy then returns NaN
    # from the sqrt, NaN compares False against the tolerance, and those pixels
    # silently fall out of the mask. It produced a usable cut-out on the first
    # test by luck; the warning was the only sign.
    arr = np.asarray(im).astype(np.int32)
    dist = np.sqrt(
        ((arr[:, :, 0] - KEY_RGB[0]) ** 2)
        + ((arr[:, :, 1] - KEY_RGB[1]) ** 2)
        + ((arr[:, :, 2] - KEY_RGB[2]) ** 2)
    )
    mask = dist <= KEY_TOLERANCE

    covered = float(mask.mean())
    if covered < 0.02:
        # Almost no magenta: the model ignored the instruction, or returned the
        # original. Either way there is nothing to key and the caller must not
        # be handed an untouched photograph.
        return None, f"no keyable background found (only {covered:.1%} magenta)"
    if covered > 0.97:
        return None, f"the whole frame is magenta ({covered:.1%}) — no garment"

    rgba = np.dstack([np.asarray(im), np.where(mask, 0, 255).astype("uint8")])

    # De-fringe: the keyed edge keeps a magenta halo from JPEG ringing, which
    # reads as a purple outline on any background it is later composited over.
    # Pulling the red and blue channels down to the green channel where they
    # over-shoot removes it without touching the garment's own colours.
    edge = (~mask) & (dist <= KEY_TOLERANCE * 2)
    if edge.any():
        r, g, b = rgba[:, :, 0], rgba[:, :, 1], rgba[:, :, 2]
        over = edge & (r.astype(np.int16) + b.astype(np.int16) > 2 * g.astype(np.int16) + 40)
        r[over] = np.minimum(r[over], g[over])
        b[over] = np.minimum(b[over], g[over])

    # NEUTRALISE THE RGB UNDER FULL TRANSPARENCY.
    #
    # Alpha alone is correct, but the magenta is still sitting in the colour
    # channels, and plenty of things ignore alpha: a thumbnailer, a marketplace
    # that flattens onto white, an editor's "remove alpha". Any of them turns a
    # clean cut-out into a magenta rectangle. Writing white underneath costs
    # nothing and makes the flattened result the one anybody would want.
    rgba[mask] = (255, 255, 255, 0)

    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


def _apply_mask(source: bytes, mask_png: bytes) -> tuple[bytes | None, str | None]:
    """Apply a model-produced silhouette mask to the ORIGINAL photograph.

    The garment pixels that come out of here are the ones that went in — this
    function only ever writes the alpha channel. Returns (png, error).
    """
    from PIL import Image
    import numpy as np

    try:
        src = Image.open(io.BytesIO(source))
        src = src.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not decode the source ({exc.__class__.__name__})"
    try:
        m = Image.open(io.BytesIO(mask_png)).convert("L")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not decode the mask ({exc.__class__.__name__})"

    # THE MASK MUST ALIGN WITH THE SOURCE, so its aspect ratio has to match.
    # A model that reframed produced a mask for a DIFFERENT picture, and
    # stretching it to fit would smear the silhouette across the garment —
    # a worse outcome than failing, because it would look deliberate.
    src_ratio, m_ratio = src.width / src.height, m.width / m.height
    if abs(m_ratio - src_ratio) / src_ratio > 0.05:
        return None, (f"the mask is a different shape: {m.width}x{m.height} "
                      f"({m_ratio:.2f}) for a {src.width}x{src.height} "
                      f"({src_ratio:.2f}) source")

    arr = np.asarray(m).astype(np.float32)

    # IS THIS ACTUALLY A MASK? A model that ignored the instruction returns a
    # photograph, whose histogram is spread across the range; a real mask is
    # bimodal, nearly all pixels pinned at one end or the other. Checking this
    # is what stops a tidied-up photo being used as an alpha channel.
    extreme = float(((arr <= 32) | (arr >= 223)).mean())
    if extreme < 0.85:
        return None, (f"not a mask — only {extreme:.0%} of it is black or white "
                      f"(a photograph was returned instead)")

    white = float((arr >= 128).mean())
    if white < 0.02:
        return None, f"the mask is empty ({white:.1%} white — no garment found)"
    if white > 0.97:
        return None, f"the mask is all garment ({white:.1%} white — nothing removed)"

    # Stretch the soft boundary band to full range, so the edge stays
    # antialiased but everything either side is fully opaque or fully clear.
    alpha = np.clip((arr - MASK_LO) / (MASK_HI - MASK_LO), 0.0, 1.0) * 255.0
    a_img = Image.fromarray(alpha.astype("uint8"), "L")
    if a_img.size != src.size:
        # Upscaling the mask to the original, not downscaling the original to
        # the mask: the full-resolution photograph is the asset.
        a_img = a_img.resize(src.size, Image.Resampling.BILINEAR)

    out = np.dstack([np.asarray(src)[:, :, :3], np.asarray(a_img)])
    # Neutralise the RGB under full transparency, for the same reason as
    # _key_out: anything that flattens this onto white must not reveal what
    # used to be behind the garment.
    out[np.asarray(a_img) == 0] = (255, 255, 255, 0)

    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


def _is_cutout(data: bytes) -> tuple[bool, str]:
    """Did the provider actually remove anything? (ok, why)

    Reuses the review rules' own classifier so this agrees with what IMG.013
    will say about the stored row afterwards. Two opinions about "is this
    matted" is precisely the drift the rule engine exists to prevent.
    """
    try:
        cfg = ((policy().get("imagery") or {}).get("background") or {})
        verdict, score, detail = bgcheck.classify_pixels(data, cfg)
    except Exception as exc:  # noqa: BLE001 — a classifier fault must not fail the call
        log.warning("cut-out check could not run (%s); accepting the result", exc)
        return True, "unchecked"

    # TRANSPARENT is the only pass. BACKDROP deliberately is not: "opaque but
    # uniform" is a studio sweep or a white card, which is exactly what a
    # generative model paints in when it decides the background is the subject.
    # UNKNOWN means the bytes would not decode -- not a verdict, so it is not
    # treated as one; the caller gets it and the image is accepted rather than
    # discarded on a measurement that did not happen.
    if verdict is bgcheck.BackgroundVerdict.UNKNOWN:
        return True, "undecodable, accepted unchecked"
    return (verdict is bgcheck.BackgroundVerdict.TRANSPARENT,
            f"{verdict.value} ({detail})")


# The garment parser, and why it is a CLOTHING model rather than a general one.
#
# Three background removers were measured on the same two BOAS studio originals:
#
#   gemini (paint or mask)   retouches the photograph and leaves the scene
#   rembg isnet / u2net      keeps the mannequin AND the podium — 14% backdrop
#   rembg u2net_cloth_seg    0% backdrop, garment intact, full resolution
#
# The middle row is the instructive one. isnet and u2net are SALIENT OBJECT
# detectors, and in a studio shot the salient object is the whole
# mannequin-on-podium assembly — so they answer their own question correctly and
# ours wrongly. u2net_cloth_seg is a clothing parser: it is trained to find the
# garment specifically, which is why the mannequin, the stand, the podium, the
# metal disc and the red button all disappear where the other two kept them.
_CLOTH_MODEL = os.getenv("HERMES_CLOTH_SEG_MODEL", "u2net_cloth_seg")
_cloth_session: Any = None
_cloth_unavailable: str | None = None


def _cloth_seg(data: bytes) -> tuple[bytes | None, str | None]:
    """Segment the garment locally. Returns (png, error). Never raises.

    THE SESSION IS CACHED because building it loads a 168 MB ONNX graph and
    takes ~25 seconds; per-image that would dwarf the ~4s inference and make the
    local path slower than the remote ones it replaces.

    The model emits THREE stacked masks — upper body, lower body, full body — so
    a 3000x4000 source returns 3000x12000. They are unioned rather than picked
    between: a two-piece set populates upper and lower, a dress populates full,
    and taking the strongest alpha at each pixel is right for all three without
    having to know which kind of garment this is beforehand.
    """
    global _cloth_session, _cloth_unavailable
    if _cloth_unavailable:
        return None, _cloth_unavailable

    from PIL import Image
    import numpy as np

    if _cloth_session is None:
        try:
            from rembg import new_session
            started = time.perf_counter()
            _cloth_session = new_session(_CLOTH_MODEL)
            log.info("cloth-seg model %s loaded in %.1fs",
                     _CLOTH_MODEL, time.perf_counter() - started)
        except ImportError:
            _cloth_unavailable = "rembg is not installed"
            return None, _cloth_unavailable
        except Exception as exc:  # noqa: BLE001 — a missing model file, no disk, no network
            # NOT cached as unavailable: `No space left on device` while
            # fetching the weights is exactly what happened here once, and it is
            # a condition that gets fixed. Caching it would keep the segmenter
            # switched off for the life of the process afterwards.
            return None, f"could not load {_CLOTH_MODEL}: {str(exc)[:140]}"

    try:
        from rembg import remove
        src = Image.open(io.BytesIO(data)).convert("RGBA")
        stacked = Image.open(io.BytesIO(remove(data, session=_cloth_session)))
        stacked = stacked.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {str(exc)[:140]}"

    w, h = src.size
    panels = max(1, stacked.height // h)
    alpha = np.zeros((h, w), dtype=np.uint8)
    for i in range(panels):
        band = stacked.crop((0, i * h, w, (i + 1) * h))
        if band.size != (w, h):
            continue
        alpha = np.maximum(alpha, np.asarray(band.getchannel("A")))

    kept = float((alpha >= 250).mean())
    if kept < 0.02:
        return None, f"no garment found ({kept:.1%} kept)"
    if kept > 0.97:
        return None, f"the whole frame is garment ({kept:.1%}) — nothing removed"

    out = np.dstack([np.asarray(src)[:, :, :3], alpha])
    out[alpha == 0] = (255, 255, 255, 0)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


def _source_backdrop(source: bytes, result: bytes) -> tuple[bool | None, str]:
    """Did the cut-out keep part of the SOURCE's backdrop? (ok, why)

    `ok` is None when the question could not be put at all — the caller then
    asks the cut-out's own backdrop instead (item 7; see `_kept_backdrop`).

    THE HOLE THIS FILLS. `_is_cutout` asks the review rules' classifier, which
    looks at the FRAME BORDER — the right question for "has anything been
    removed at all", and blind to anything left in the middle. It passed a cut
    -out of BOA-006164 reading "100% of the border is fully transparent" while
    the image still had a white halo around both sleeves and the entire podium,
    metal disc and red button hanging off the hem. On a listing that is worse
    than no cut-out: it looks deliberate.

    WHAT IS MEASURED. The source's own outer band is the backdrop, by
    construction — it is the part of the studio shot furthest from the product.
    Sample its colour there, then count how many FULLY OPAQUE pixels of the
    result still match it. Surviving backdrop is exactly what that counts.

    WHEN IT DECLINES TO JUDGE. If the source border is not uniform there is no
    single backdrop colour to measure against, and a made-up one would reject
    good cut-outs. That abstention used to END the check, and it is why Hermes
    returned `ok: true` on the Nike booth photograph with the whole podium
    still in the cut-out: "source border is not uniform (spread 71), not
    checked" (§1.1). It now hands the question to `_own_leftovers`, which needs
    no uniform source at all.
    """
    from PIL import Image
    import numpy as np

    # EVERY ARRAY HERE IS DELIBERATELY NARROW, and the first version was not.
    #
    # `np.median` returns float64, so `rgb - backdrop` promoted a 3000x4000x3
    # image to float64 and asked for 275 MiB in one allocation — which failed
    # outright on a box with a full disk and so no pagefile headroom. A studio
    # original is 12 megapixels and this runs on every candidate from every
    # provider, so the difference is not academic.
    #
    # uint8 in, int16 for the channel differences (max 255 each, 765 summed,
    # against int16's 32767), and the three channels accumulated one at a time
    # instead of materialising an h*w*3 temporary.
    try:
        src = np.asarray(Image.open(io.BytesIO(source)).convert("RGB"))
        res = Image.open(io.BytesIO(result)).convert("RGBA")
    except Exception:  # noqa: BLE001
        return None, "the source or the result could not be decoded"

    h, w = src.shape[:2]
    band = max(2, int(min(h, w) * 0.03))
    border = np.concatenate([
        src[:band].reshape(-1, 3), src[-band:].reshape(-1, 3),
        src[:, :band].reshape(-1, 3), src[:, -band:].reshape(-1, 3),
    ]).astype(np.int16)
    backdrop = np.median(border, axis=0).astype(np.int16)
    spread = float(np.median(np.abs(border - backdrop).sum(axis=1)))
    del border
    if spread > 60:
        return None, f"source border is not uniform (spread {spread:.0f})"

    arr = np.asarray(res)
    if arr.shape[:2] != src.shape[:2]:
        return None, "the result is not the source's size"

    opaque = arr[:, :, 3] >= 250
    if not opaque.any():
        return None, "nothing opaque"

    # Same distance metric as the chroma key, for the same reason: a JPEG edge
    # never lands exactly on the backdrop colour.
    diff = np.zeros((h, w), dtype=np.int16)
    for ch in range(3):
        diff += np.abs(arr[:, :, ch].astype(np.int16) - backdrop[ch])
    suspect = opaque & (diff <= 60)
    del diff

    # IS IT ONE PIECE, or is it speckle? That is the question, not how much.
    #
    # Counting backdrop-coloured pixels does not work, and the measurement says
    # so. BOA-006202 is a black t-shirt with a pale cream print, shot against a
    # cream wall: 6.1% of its kept pixels match the backdrop and every one is
    # the ARTWORK. A cut-out keeping the mannequin and podium scores 7.2%. The
    # two ranges overlap, so no threshold on the total can separate them — the
    # 5% one rejected a flawless cut-out and cost five more providers, ~3.5
    # minutes and several billed calls to arrive at worse answers.
    #
    # Surviving backdrop is CONTIGUOUS: a podium, a halo ringing the silhouette,
    # a strip of sweep. A print is broken into fragments by the garment's own
    # colours. Measured over the six cases to hand, the largest connected
    # suspect region is:
    #
    #     cloth-seg, pale print on a cream wall   0.2%
    #     cloth-seg, tee / sweater                0.1%  0.1%
    #     openai, halo + podium                   2.6%
    #     isnet, mannequin + podium              12.3% 11.7%
    #
    # A 13x gap, so 1% sits clear of both sides. Six images is a thin basis for
    # a catalogue-wide rule, and the threshold is deliberately nearer the good
    # side: a false reject costs a retry, a false accept puts a mannequin on a
    # live listing.
    largest = 0.0
    try:
        import cv2

        n, _lbl, stats, _c = cv2.connectedComponentsWithStats(
            suspect.astype(np.uint8), connectivity=8)
        if n > 1:
            largest = float(stats[1:, cv2.CC_STAT_AREA].max()) / float(opaque.sum())
    except Exception:  # noqa: BLE001
        # Without cv2 there is no shape information, so fall back to the blunt
        # total. It over-rejects patterned garments — the failure we can afford.
        total = float(suspect.sum()) / float(opaque.sum())
        if total > 0.05:
            return False, f"{total:.0%} backdrop-coloured (no cv2 — shape not checked)"
        return True, f"{total:.0%} backdrop-coloured (no cv2)"

    scattered = float(suspect.sum()) / float(opaque.sum())
    if largest > 0.01:
        return False, (f"a single {largest:.0%} region of the cut-out is the "
                       f"backdrop colour rgb({backdrop[0]:.0f},"
                       f"{backdrop[1]:.0f},{backdrop[2]:.0f}) — the mask left "
                       f"the sweep, the podium or a halo behind")
    return True, (f"largest backdrop-coloured region {largest:.1%}"
                  + (f", {scattered:.0%} scattered (garment print)"
                     if scattered > 0.02 else ""))


# --------------------------------------------------------------------------- #
# The cut-out's OWN backdrop — item 7 of docs/PICTURE-CHECK-FIXES.md
# --------------------------------------------------------------------------- #
#
# WHY A SECOND WAY OF ASKING THE SAME QUESTION. `_source_backdrop` needs one
# backdrop colour in the PHOTOGRAPH, and a studio sweep does not have one: the
# middle of the wall is lit harder than its edges, the floor is a different
# surface from the sweep, and the border band takes in all of it. On the Nike
# booth photograph (`fc8e53e7-…-original_front.jpg`) that band measured a
# spread of 71 against a ceiling of 60, so the check said "source border is not
# uniform, not checked" and `remove_background` returned `ok: true` with the
# entire podium still under the shirt (§1.1). Every booth photograph with
# uneven lighting disabled it the same way — the check was written FOR a
# surviving podium and was off exactly where podiums are.
#
# THE CUT-OUT ITSELF HAS A BACKDROP AND IT IS FLAT BY CONSTRUCTION. Everything
# this module returns writes (255,255,255,0) under full transparency —
# `_key_out`, `_apply_mask` and `_cloth_seg` all neutralise the RGB there, for
# the separate reason that anything flattening the cut-out onto white must not
# reveal what used to be behind the garment. So the corners are white, exactly
# white, and segmenting against them is the easy problem §0 describes rather
# than the impossible one §1.3 does.
#
# WHAT IS COUNTED. The garment's real outline, from `cutouts.garment_mask` on
# the cut-out composited over its own backdrop colour (item 1 — the alpha
# cannot be used here, because the alpha is precisely what kept the podium),
# and then: pixels the segmenter KEPT which are nonetheless the backdrop's
# colour and lie OUTSIDE that outline. A podium is white, kept, and not
# garment. A garment is not.
#
# Measured on the real cut-out in the repository root, at the 448px work grid:
#
#                                      largest region   scattered   bottom band
#     fc8e53e7 as cloth-seg cut it          1.70%         2.94%        31.9%
#     the same, podium taken off            0.01%         0.02%         0.0%
#
# The podium's grey RIM sits further than `mask_rgb_tolerance` from white and
# is therefore counted as garment; only its flat white interior is counted as
# leftover, which is why 1.70% understates what the eye sees. It does not
# matter — the bar is 1%, and the clean cut-out measures 0.01%.

def _flatten(data: bytes, cfg: dict[str, Any],
             size: tuple[int, int] | None = None) -> tuple[Any, Any, Any, str | None]:
    """A cut-out over its OWN flat backdrop. `(flat RGB image, alpha, backdrop, error)`.

    THE RGB AND THE ALPHA ARE RESIZED SEPARATELY, and that is not a style
    choice: Pillow resamples RGBA with PREMULTIPLIED alpha, so every fully
    transparent pixel comes back rgb(0,0,0) and the corner colour — the whole
    basis of the segmentation below — reads black instead of the white that is
    actually written there. Measured on `fc8e53e7`: the corner is
    (255,255,255,0) at 3000×4000 and (0,0,0,0) after a thumbnail to 448.

    COMPOSITED RATHER THAN JUST DROPPING THE ALPHA, because a cut-out this
    module did not make need not carry a neutralised backdrop: everything here
    writes (255,255,255,0) under full transparency, but a foreign one may still
    have the photograph's own pixels there and dropping the alpha would hand
    `garment_mask` the whole room. Painting the corner colour in makes the
    backdrop flat by construction whatever was underneath, which is exactly the
    condition item 1 needs.
    """
    from PIL import Image
    import numpy as np

    from app.imaging import cutouts

    try:
        img = Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return None, None, None, f"could not be decoded ({exc.__class__.__name__})"
    if size is None:
        # Downsampled first, and the whole measurement is made here: a 3000×4000
        # RGBA is 12 megapixels compared four ways, and this runs on every
        # candidate from every provider. A garment's outline and a podium are
        # both hundreds of pixels across — neither needs full resolution.
        work = int(cfg.get("work_px") or 448)
        scale = min(1.0, work / float(max(img.size)))
        size = (max(8, int(round(img.width * scale))), max(8, int(round(img.height * scale))))

    rgb = np.asarray(img.convert("RGB").resize(size, Image.Resampling.LANCZOS)).astype(np.float32)
    alpha = np.asarray(img.getchannel("A").resize(size, Image.Resampling.LANCZOS))
    back = cutouts.corner_backdrop(
        Image.fromarray(rgb.astype("uint8"), "RGB"), cutouts.config(policy()))
    if back is None:
        return None, None, None, "its own backdrop colour could not be read"
    a = (alpha.astype(np.float32) / 255.0)[..., None]
    flat = rgb * a + np.asarray(back, dtype=np.float32) * (1.0 - a)
    return Image.fromarray(flat.clip(0, 255).astype("uint8"), "RGB"), alpha, back, None


def _own_leftovers(result: bytes, cfg: dict[str, Any]) -> dict[str, Any]:
    """What the cut-out says about itself: `largest`, `scattered`, `bottom`.

    All three are fractions of the pixels the segmenter KEPT. A `note` and no
    numbers means the question could not be put, and nothing may be refused on
    it — the same rule §0 sets for every derived mask.
    """
    import numpy as np

    from app.imaging import cutouts

    flat, alpha, back, err = _flatten(result, cfg)
    if err:
        return {"note": f"the cut-out {err}"}

    opaque = alpha >= 250
    kept = int(opaque.sum())
    if not kept:
        return {"note": "nothing opaque"}
    clear = float((alpha < 128).mean())
    if clear < float(cfg.get("leftover_min_clear") or 0.02):
        # A fully opaque result is a COMPOSITED cut-out (§0), where "the pixels
        # the segmenter kept" is the whole frame and every backdrop pixel would
        # read as a podium. `garment_kept` is the check that reads those.
        return {"note": f"the result is {clear:.1%} clear — not a cut-out to read this way"}

    # Segmented by COLOUR against that flat backdrop, never by the alpha:
    # handed the alpha `garment_mask` would take it, and the alpha is what kept
    # the podium — it would answer "the garment is the shirt AND the podium"
    # and find nothing left over. Flattening removes that option from it.
    garment, info = cutouts.garment_mask(flat, cutouts.config(policy()))
    if garment is None:
        return {"note": info.get("note") or "the garment's shape could not be derived"}

    share = float((garment & opaque).sum()) / float(kept)
    floor = float(cfg.get("leftover_garment_min") or 0.5)
    if share < floor:
        # The garment is the backdrop's own colour — a white shirt on white.
        # Every pixel kept then looks like a podium, so the honest answer is
        # that this cannot be told apart, not that the cut-out is bad.
        return {"note": (f"only {share:.0%} of what was kept differs from the cut-out's own "
                         f"backdrop {cutouts.to_hex(back)} — the garment is the backdrop's "
                         f"colour, so a leftover cannot be told from the garment")}

    leftover = opaque & ~garment
    out: dict[str, Any] = {
        "backdrop": cutouts.to_hex(back),
        "garment_share": round(share, 4),
        "scattered": round(float(leftover.sum()) / float(kept), 4),
    }

    # THE BOTTOM BAND, MEASURED ON THE KEPT REGION AND NOT THE FRAME. On
    # `fc8e53e7` the frame's own bottom rows are transparent — the podium ends
    # three quarters of the way down the picture — so a band taken from the
    # frame would sample nothing at all. The bottom of what the segmenter KEPT
    # is where a podium is, always, because the podium is what the garment
    # stands on.
    rows = np.where(opaque.any(axis=1))[0]
    y0, y1 = int(rows.min()), int(rows.max())
    band = max(1, int(round((y1 - y0 + 1) * float(cfg.get("leftover_bottom_fraction") or 0.10))))
    strip = slice(y1 - band + 1, y1 + 1)
    out["bottom"] = round(float(leftover[strip].sum()) / max(1.0, float(opaque[strip].sum())), 4)

    # One piece or speckle, the same question `_source_backdrop` asks and for
    # the same reason: a pale print is broken into fragments by the garment's
    # own colours, a podium is not.
    #
    # AND IT HAS TO REACH THE OUTSIDE OF THE CUT-OUT. This backdrop is PURE
    # WHITE — the colour this module writes under full transparency — where the
    # source's was whatever the studio wall happened to be, so a garment with a
    # genuinely white panel or logo has a solid region of it inside the
    # silhouette and would be refused by every strategy in turn, leaving the
    # product with no cut-out at all. What is left of the SET is never inside:
    # a podium hangs off the hem, a halo rings the outline, a strip of sweep
    # runs to the frame. Measured on `fc8e53e7`: its podium's largest piece
    # (1.70%) touches the transparent outside, and the enclosed second piece
    # (1.20%) is not needed to catch it.
    try:
        import cv2

        n, lbl, stats, _c = cv2.connectedComponentsWithStats(
            leftover.astype(np.uint8), connectivity=8)
        outside = cv2.dilate((~opaque).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool)
        biggest = 0.0
        # Largest first, stopping at the first that reaches the outside. Capped
        # because a speckled print can leave hundreds of fragments and none of
        # them past the first few can be a podium.
        for i in (np.argsort(-stats[1:, cv2.CC_STAT_AREA]) + 1)[:32] if n > 1 else []:
            if bool((outside & (lbl == i)).any()):
                biggest = float(stats[i, cv2.CC_STAT_AREA]) / float(kept)
                break
        out["largest"] = round(biggest, 4)
    except Exception:  # noqa: BLE001 — no cv2: the bottom band still answers
        out["largest"] = None
    return out


def _kept_backdrop(source: bytes, result: bytes,
                   cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Did the cut-out keep part of the set? (ok, why)

    TWO WAYS OF ASKING IT, and the second is item 7. The source's backdrop when
    the source HAS one — the original check, unchanged, and the stricter of the
    two because it knows the colour it is looking for. The CUT-OUT's own
    backdrop otherwise, and additionally in the bottom band, which is where a
    podium is whether or not the photograph's border happened to be uniform.

    The two disagree only in one direction: the source path can see a podium
    the same colour as the garment (it matches the sweep, not the shirt), and
    the own path can see one on a photograph the source path refuses to judge.
    Neither can pass what the other fails, so both are asked and either can
    refuse.
    """
    cfg = cfg if cfg is not None else config()
    src, src_why = _source_backdrop(source, result)
    if src is False:
        return False, src_why

    own = _own_leftovers(result, cfg)
    if own.get("note"):
        # Nothing may be refused on a measurement that did not happen — but the
        # reason is carried, because "not checked" hiding a podium is exactly
        # what item 7 exists to end and a silent abstention is how it hid.
        if src is True:
            return True, f"{src_why}; own backdrop not read ({own['note']})"
        return True, f"not checked: {src_why}, and the cut-out's own backdrop — {own['note']}"

    largest, bottom = own.get("largest"), float(own.get("bottom") or 0.0)
    region_max = float(cfg.get("leftover_region_max") or 0.01)
    bottom_max = float(cfg.get("leftover_bottom_max") or 0.25)
    tail = ("" if src is True else f" (the source border could not be used: {src_why})")

    if largest is not None and largest > region_max:
        return False, (f"a single {largest:.0%} region of the cut-out is its own backdrop "
                       f"colour {own['backdrop']} outside the garment's outline — the mask "
                       f"left the podium, the stand or a halo behind{tail}")
    if bottom > bottom_max:
        return False, (f"{bottom:.0%} of what the cut-out keeps in its bottom band is the "
                       f"backdrop colour {own['backdrop']}, not garment — the garment is "
                       f"standing on something the mask kept{tail}")
    if largest is None and float(own.get("scattered") or 0.0) > 0.05:
        # No cv2, so no shape information: the blunt total, over-rejecting a
        # patterned garment, which is the failure this module can afford.
        return False, (f"{own['scattered']:.0%} of the cut-out is its own backdrop colour "
                       f"outside the garment (no cv2 — shape not checked){tail}")
    return True, (f"{src_why}; own backdrop: largest region "
                  f"{'unknown (no cv2)' if largest is None else f'{largest:.1%}'}, "
                  f"bottom band {bottom:.0%}")


# --------------------------------------------------------------------------- #
# A replacement is never worse than what it replaces — item 8
# --------------------------------------------------------------------------- #
#
# WHAT WENT WRONG. Test list 3: KIL-001625's BACK cut-out came back with "large
# part of shirt back missing" and KIL-001644's with "background mask cut into
# left strap; strap missing". Neither is a false positive — both are damage the
# RE-MATTE caused. The chain re-mattes through `backfill-bg-removal.ts
# --replace`, which calls `replaceWithDerived`: the new cut-out takes the slot
# and the old one is superseded. Every check Hermes has runs AFTER that, so the
# worse picture is already live by the time it is judged, and all the finding
# can do is ask for the same thing to be tried again.
#
# So this is a PRECONDITION and not a report. A candidate that has passed every
# other check is compared against the cut-out it would supersede, and one that
# has materially less garment — or one contiguous piece missing at an edge — is
# refused like any other failed candidate: the chain moves to the next
# strategy, and if none of them beats what is on file, nothing is written and
# `remove_background` says the existing cut-out was kept.
#
# CUT-OUT AGAINST CUT-OUT, never cut-out against photograph. Both have a flat
# uniform backdrop, which is a reliable segmentation (§0, item 1); the
# photograph has a lit studio wall, which is not (§1.3 — BOA-006151's garment
# sits 15 from its own backdrop). The old cut-out is normally the COMPOSITED
# one vnyx-api stores (opaque on rgb(235,235,235)) and the new one is Hermes'
# own (transparent, white underneath), so each is segmented against its own
# backdrop and the two masks are compared on one grid. Reading the new one's
# alpha and the old one's colour would compare two different questions.

def _garment_on(data: bytes, size: tuple[int, int],
                cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """One cut-out's garment on a given grid. `(mask, info)`, mask None if unknown.

    The flattening is a no-op in effect when the cut-out is already opaque
    (every one vnyx-api stores) and paints the backdrop in when it is not, so
    the composited old cut-out and the transparent new one arrive at
    `garment_mask` as the same kind of picture and are segmented by the same
    rule. Reading one's alpha and the other's colour would compare two
    different questions and call the difference garment loss.
    """
    from app.imaging import cutouts

    flat, _alpha, back, err = _flatten(data, cfg, size)
    if err:
        return None, {"note": err}
    mask, info = cutouts.garment_mask(flat, cutouts.config(policy()))
    info["backdrop"] = cutouts.to_hex(back)
    return mask, info


def _on_own_box(mask: Any, grid: int) -> Any | None:
    """The mask cropped to its own bounding box and resized to one square grid.

    Throws away position and scale and keeps the silhouette, so two cut-outs
    that frame the same garment differently become directly comparable.
    """
    import numpy as np
    from PIL import Image

    ys, xs = np.where(mask)
    if ys.size == 0:
        return None
    crop = mask[ys.min(): ys.max() + 1, xs.min(): xs.max() + 1]
    img = Image.fromarray((crop * 255).astype(np.uint8))
    return np.asarray(img.resize((grid, grid), Image.Resampling.NEAREST)) > 127


def _rescaled(old: Any, new: Any, cfg: dict[str, Any]) -> tuple[Any, Any, float] | None:
    """`(old, new, scale)` on a common grid when the two are ONE garment framed
    at two scales, else None.

    ONLY THE SCALE IS CANCELLED, not the comparison. The caller re-runs the very
    same rules on what comes back — a materially smaller garment, and a single
    piece missing at an edge — because both are still the right questions; it is
    the CANVAS they were being asked on that was wrong. A pure "do the outlines
    match" test cannot replace them: KIL-001644's lost strap still scores 95%,
    which is a shape that matches and a garment that does not.

    None when the two are at the same scale, which is the ordinary case and
    where the pixel comparison is already valid.
    """
    import numpy as np

    if float(old.sum()) < 100 or float(new.sum()) < 100:
        return None

    # THE SCALE COMES FROM THE BOUNDING BOX, NOT THE AREA, and that is the whole
    # discriminator. Garment AREA changes for two different reasons — the
    # picture was reframed, or the mask ate part of the garment — so an
    # area-derived scale reads a lost shirt back as a zoom and cancels exactly
    # the test that would have caught it.
    #
    # A ZOOM SCALES BOTH AXES EQUALLY. A loss does not: it shortens the box on
    # the axis it ate from. Measured on the pair this exists for and on
    # synthesised damage, at 448px:
    #
    #                                 w x     h x     uniform
    #   MID-000650 zoom -> correct    0.591   0.575     0.973   accept
    #   shirt back gone, same scale   0.996   0.559     0.561   refuse
    #   shirt back gone, rescaled     0.573   0.325     0.567   refuse
    #   strap gone                    0.996   0.991     0.995   same scale, see below
    #   honest re-cut, same scale     0.996   0.991     0.995   accept
    #
    # The two groups sit at 0.97+ and 0.56 — nothing lands between them.
    def bbox(mask: Any) -> tuple[int, int] | None:
        ys, xs = np.where(mask)
        if ys.size == 0:
            return None
        return int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)

    ob, nb = bbox(old), bbox(new)
    if not ob or not nb or min(ob) < 8 or min(nb) < 8:
        return None
    wr, hr = nb[0] / ob[0], nb[1] / ob[1]
    if min(wr, hr) / max(wr, hr) < float(cfg.get("replace_uniform_min") or 0.90):
        return None      # one axis moved and the other did not: a loss, not a zoom

    scale = (wr + hr) / 2
    lo = float(cfg.get("replace_same_scale_max") or 1.25)
    if (1.0 / lo) <= scale <= lo:
        # Same framing. The pixel comparison is valid and must not be pre-empted
        # — this is where KIL-001644's strap is caught, at 0.996 scale.
        return None

    grid = int(cfg.get("replace_shape_grid") or 256)
    a, b = _on_own_box(old, grid), _on_own_box(new, grid)
    if a is None or b is None:
        return None
    union = int((a | b).sum())
    if union == 0:
        return None
    # A COARSE GATE, deliberately. It only asks "is this plausibly the same
    # garment reframed, rather than two different pictures" — the real
    # judgement is the caller's rules on the normalised pair. Set low for that
    # reason: a genuine loss is meant to reach them, not be turned away here.
    if float((a & b).sum()) / union < float(cfg.get("replace_shape_iou_min") or 0.80):
        return None
    return a, b, scale


def garment_kept(previous: bytes, candidate: bytes,
                 cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Is the candidate at least as complete as the cut-out it would replace?

    (ok, why). ok is True when it is — and also when the two cannot honestly be
    compared, because a replacement must be refused on evidence, not on the
    absence of it.
    """
    from PIL import Image
    import numpy as np

    cfg = cfg if cfg is not None else config()
    try:
        old_im = Image.open(io.BytesIO(previous))
        new_im = Image.open(io.BytesIO(candidate))
        old_w, old_h = old_im.size
        new_w, new_h = new_im.size
    except Exception as exc:  # noqa: BLE001
        return True, (f"the existing cut-out could not be read "
                      f"({exc.__class__.__name__}), not compared")
    if not (old_w and old_h and new_w and new_h):
        return True, "one of the two has no size, not compared"

    # THE SAME PICTURE, OR TWO PICTURES? A cut-out cropped to its bounding box
    # and one on the photograph's frame hold the garment at different scales,
    # and a pixel comparison between them measures the crop rather than the
    # mask. `frame_mismatch` in cutouts.py is what catches that; here it means
    # the two are not comparable and nothing is refused.
    tol = float(cfg.get("replace_ratio_tolerance") or 0.05)
    r_old, r_new = old_w / old_h, new_w / new_h
    if abs(r_new - r_old) / r_old > tol:
        return True, (f"the existing cut-out is {old_w}×{old_h} ({r_old:.3f}) and the new one "
                      f"{new_w}×{new_h} ({r_new:.3f}) — different frames, not compared")

    work = int(cfg.get("work_px") or 448)
    scale = min(1.0, work / float(max(old_w, old_h)))
    size = (max(8, int(round(old_w * scale))), max(8, int(round(old_h * scale))))

    old, old_info = _garment_on(previous, size, cfg)
    if old is None:
        # Nothing to be more complete THAN. This is also the honest answer for
        # a garment the colour of its own backdrop, which no mask can measure.
        return True, f"the existing cut-out's garment could not be derived ({old_info.get('note')})"
    new, new_info = _garment_on(candidate, size, cfg)
    if new is None:
        # The candidate cannot be shown to keep the garment, and it is the one
        # asking to replace something that works. Refused, and said plainly.
        return False, (f"the new cut-out's garment could not be derived "
                       f"({new_info.get('note')}) — the cut-out on file was KEPT")

    old_area, new_area = float(old.sum()), float(new.sum())
    if old_area < 100:
        return True, "the existing cut-out has almost no garment in it, not compared"

    # THE SAME GARMENT AT A DIFFERENT SCALE IS NOT A LOSS OF GARMENT.
    #
    # Everything below counts PIXELS, which assumes the two cut-outs frame the
    # garment the same way. When the incumbent is a ZOOM — cropped to the
    # garment and scaled back out to the source ratio — it does not: its garment
    # covers far more of the canvas, so a correctly framed replacement always
    # looks like it lost most of the garment and is always refused.
    #
    # Measured on MID-000650's FRONT, which is exactly that cut-out:
    #
    #   FRONT on file    896x1195   garment 0.3285 of frame   <- the zoom
    #   FRONT cloth-seg 3000x4000   garment 0.1113 of frame   <- the correct one
    #   BACK on file    3000x4000   garment 0.1175 of frame   <- known good
    #
    # The replacement matches the product's own BACK almost exactly and was
    # refused as "66% less garment". `replace_ratio_tolerance` above is meant to
    # catch "different frames, not compared", but a zoom keeps the source RATIO
    # (0.750 both) and sails through it — the ratio test cannot see a scale.
    #
    # So the shapes are compared with the scale normalised away: each mask
    # cropped to its own bounding box and resized to one grid. A reframing keeps
    # its outline (0.978 on that pair); a mask that ate the shirt back or a
    # strap changes it, because the lost piece is missing from the outline too.
    # That is what keeps KIL-001625's protection intact.
    reframed = _rescaled(old, new, cfg)
    note = ""
    if reframed is not None:
        old, new, scale = reframed
        old_area, new_area = float(old.sum()), float(new.sum())
        note = (f" (the two are the same garment at {scale:.1f}x — compared on a "
                f"common frame, because the one on file is cropped to the garment "
                f"and this one is on the photograph's)")

    share = new_area / old_area
    drop = 1.0 - share
    drop_max = float(cfg.get("replace_area_drop_max") or 0.10)
    if drop > drop_max:
        return False, (f"the new cut-out has {drop:.0%} less garment than the one it would "
                       f"replace ({int(new_area)} against {int(old_area)} garment pixels, "
                       f"each against its own backdrop){note} — the cut-out on file was KEPT")

    # ONE PIECE MISSING AT AN EDGE, which the total above cannot see: KIL-001644
    # lost a single strap, a couple of percent of the garment. Opened first,
    # because the thin rim two segmenters always differ by is ONE connected
    # region around the whole outline and would otherwise read as a missing
    # strap every time.
    lost = old & ~new
    if not lost.any():
        return True, (f"the new cut-out has {share:.0%} of the garment on file, "
                      f"nothing lost{note}")
    piece = None
    try:
        import cv2

        k = max(3, int(cfg.get("replace_open_px") or 5)) | 1
        kernel = np.ones((k, k), np.uint8)
        opened = cv2.morphologyEx(lost.astype(np.uint8), cv2.MORPH_OPEN, kernel)
        # The rim of the OLD garment: a loss that touches it ate in from the
        # outside, which is what a mask cutting into a strap or a shirt back
        # does. A pinhole in the middle is a different defect and the total
        # above is what would catch it.
        rim = old & ~cv2.erode(old.astype(np.uint8), kernel).astype(bool)
        n, lbl, stats, _c = cv2.connectedComponentsWithStats(opened, connectivity=8)
        floor = float(cfg.get("replace_loss_region_max") or 0.02)
        for i in range(1, n):
            blob = float(stats[i, cv2.CC_STAT_AREA]) / old_area
            if blob >= floor and bool((rim & (lbl == i)).any()):
                piece = blob
                break
    except Exception:  # noqa: BLE001 — no cv2: the total above is all there is
        return True, (f"the new cut-out has {share:.0%} of the garment on file "
                      f"(no cv2 — a single missing piece was not looked for)")

    if piece is not None:
        return False, (f"one piece of {piece:.0%} of the garment is missing from the new "
                       f"cut-out at its edge — a strap, a sleeve or part of the back that "
                       f"the mask cut into{note} — the cut-out on file was KEPT")
    return True, (f"the new cut-out has {share:.0%} of the garment on file, largest single "
                  f"loss under {float(cfg.get('replace_loss_region_max') or 0.02):.0%}{note}")


def _gemini(
    data: bytes, timeout_s: float, prompt: str = PROMPT
) -> tuple[bytes | None, str | None]:
    """One Gemini attempt. Never raises."""
    keys = settings().nano_banana_keys
    if not keys:
        return None, "no GOOGLE_NANO_BANANA_API_KEY configured"

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        return None, "google-genai is not installed"

    cfg = (policy().get("imagery") or {}).get("generation") or {}
    model = cfg.get("bg_removal_model") or cfg.get("model") or "gemini-3-pro-image-preview"

    # Every key is tried before giving up: these are per-key quotas, and a 429
    # on the first is not a statement about the second.
    last: str | None = None
    for i, key in enumerate(keys):
        try:
            client = genai.Client(
                api_key=key,
                http_options={
                    "timeout": int(timeout_s * 1000),
                    "client_args": genai_client_args(),
                },
            )
            resp = client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=data, mime_type="image/jpeg"),
                ],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE"],
                    # ASK FOR THE LARGE OUTPUT. Left unset the model returns
                    # ~900px on the long edge, which is a heavy downscale from a
                    # 3000x4000 studio original and the cut-out becomes the
                    # lowest-resolution asset on the listing. Same knob and same
                    # default the render path uses.
                    image_config={
                        "image_size": cfg.get("default_resolution") or "2K"
                    },
                ),
            )
            for cand in (resp.candidates or []):
                for part in (cand.content.parts or []):
                    blob = getattr(part, "inline_data", None)
                    if blob and blob.data:
                        return blob.data, None
            last = "the model returned no image"
        except Exception as exc:  # noqa: BLE001 — every provider error is the same fact here
            last = f"{exc.__class__.__name__}: {str(exc)[:160]}"
            log.info("gemini key %d/%d failed: %s", i + 1, len(keys), last)
    return None, last or "no image"


def _letterbox(data: bytes) -> tuple[bytes, tuple[int, int, int, int]]:
    """Fit the source onto one of gpt-image-1's three canvases, centred.

    WHY THIS EXISTS. gpt-image-1 renders at 1024x1024, 1024x1536 or 1536x1024 —
    nothing else — so a 3000x4000 portrait comes back reframed no matter what
    is asked for. That is what produced "reframed: 3000x4000 (0.75) became
    1024x1024 (1.00)" twice, and it is a property of the endpoint rather than
    anything the prompt can fix.

    Padding to a supported canvas makes the reframing OURS and therefore
    reversible: the returned mask is cropped back to the box below before it is
    applied, so the geometry matches the original again. Without this, OpenAI
    cannot participate in a mask-based flow at all.
    """
    from PIL import Image

    im = Image.open(io.BytesIO(data)).convert("RGB")
    ratio = im.width / im.height
    cw, ch = (1024, 1536) if ratio < 0.9 else (1536, 1024) if ratio > 1.1 else (1024, 1024)

    scale = min(cw / im.width, ch / im.height)
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    x, y = (cw - w) // 2, (ch - h) // 2

    # Black padding, so any padding the model leaves alone already reads as
    # background in the mask rather than as garment.
    canvas = Image.new("RGB", (cw, ch), (0, 0, 0))
    canvas.paste(im.resize((w, h), Image.Resampling.LANCZOS), (x, y))

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue(), (x, y, x + w, y + h)


def _openai(
    data: bytes, timeout_s: float, prompt: str
) -> tuple[bytes | None, str | None]:
    """One OpenAI attempt against the edits endpoint. Never raises.

    NOT app/imaging/openai_image.generate — that module is the RENDER fallback,
    which asks for a photograph of a model and has no reason to want a mask.

    The source is letterboxed first and the caller crops the result back; see
    _letterbox for why that is not optional here.
    """
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None, "no OPENAI_API_KEY configured"

    import httpx

    try:
        padded, box = _letterbox(data)
    except Exception as exc:  # noqa: BLE001
        return None, f"could not prepare the source ({exc.__class__.__name__})"

    files = {"image": ("source.png", padded, "image/png")}
    form = {
        "model": os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1"),
        "prompt": prompt,
        "output_format": "png",
        "size": "auto",
    }
    try:
        with httpx.Client(timeout=timeout_s) as client:
            resp = client.post(
                "https://api.openai.com/v1/images/edits",
                headers={"Authorization": f"Bearer {key}"},
                data=form,
                files=files,
            )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}: {resp.text[:160]}"
        payload = resp.json()
        b64 = (payload.get("data") or [{}])[0].get("b64_json")
        if not b64:
            return None, "no image in the response"
        out = base64.b64decode(b64)
    except Exception as exc:  # noqa: BLE001
        return None, f"{exc.__class__.__name__}: {str(exc)[:160]}"

    # Undo our own padding, scaling the box to whatever size came back.
    from PIL import Image

    try:
        im = Image.open(io.BytesIO(out))
        padded_im = Image.open(io.BytesIO(padded))
        sx, sy = im.width / padded_im.width, im.height / padded_im.height
        im = im.crop((round(box[0] * sx), round(box[1] * sy),
                      round(box[2] * sx), round(box[3] * sy)))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        return buf.getvalue(), None
    except Exception as exc:  # noqa: BLE001
        return None, f"could not un-pad the result ({exc.__class__.__name__})"


def _same_framing(source: bytes, result: bytes) -> tuple[bool, str]:
    """Is the result the SAME photograph, or a redrawn one?

    THE FAILURE THIS CATCHES. An image-EDIT model can answer "remove the
    background" by painting a new picture of a similar garment. gpt-image-1 did
    exactly that here: a clean, plausible, entirely synthetic t-shirt --
    smoothed fabric, idealised silhouette, the mannequin gone -- returned as a
    1024x1024 square from a 3000x4000 portrait original.

    It is a good picture and it is not the product. On a resale marketplace the
    photograph IS the description: a buyer looking at a redrawn garment is being
    shown something that does not exist, and the wear, the drape and the exact
    colour are the things they are buying on.

    ASPECT RATIO IS THE CHEAP, DETERMINISTIC SIGNAL. A genuine cut-out keeps the
    frame -- Gemini returned 896x1195 from that same 3000x4000, the same 0.75.
    A model that reframes has stopped tracing and started composing. This will
    not catch a redraw that happens to keep the ratio, so it is a floor rather
    than a proof; the pixel classifier and a human still sit after it.
    """
    from PIL import Image

    try:
        a, b = Image.open(io.BytesIO(source)), Image.open(io.BytesIO(result))
    except Exception:  # noqa: BLE001
        return True, "unchecked"

    src_ratio = a.width / a.height
    out_ratio = b.width / b.height
    drift = abs(out_ratio - src_ratio) / src_ratio
    if drift > 0.05:
        return False, (f"reframed: {a.width}x{a.height} ({src_ratio:.2f}) became "
                       f"{b.width}x{b.height} ({out_ratio:.2f})")
    return True, f"framing kept ({out_ratio:.2f})"


def _has_alpha(data: bytes) -> bool:
    """Does this image already carry a usable alpha channel?"""
    from PIL import Image
    import numpy as np

    try:
        im = Image.open(io.BytesIO(data))
    except Exception:  # noqa: BLE001
        return False
    if im.mode not in ("RGBA", "LA"):
        return False
    alpha = np.asarray(im.convert("RGBA").getchannel("A"))
    # A fully opaque alpha channel is not transparency, it is padding.
    return bool((alpha == 0).mean() > 0.02)


def _intersect_alpha(mask_png: bytes, cloth_png: bytes) -> tuple[bytes | None, str | None]:
    """Keep only what BOTH cut-outs kept. Returns (png, error).

    THE SECOND SEGMENTER, AND WHY IT IS AN INTERSECTION (item 6). Measured on
    the Nike tee (`fc8e53e7-…-original_front.jpg`): cloth-seg removed the hanger
    and the form's neck and kept the whole podium; a matting model removed the
    podium and kept the hanger. NEITHER IS RIGHT ALONE, and the two are wrong
    about different things — so a pixel is garment only where both say it is.
    The mask strategy decides that the podium is not the product; the garment
    parser still decides what is cloth, which is the half a general matting
    model has never been able to do.

    The RGB is the mask candidate's, which is the original photograph's — this
    only ever lowers the alpha.
    """
    from PIL import Image
    import numpy as np

    try:
        a = Image.open(io.BytesIO(mask_png)).convert("RGBA")
        b = Image.open(io.BytesIO(cloth_png)).convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return None, f"could not decode a cut-out to intersect ({exc.__class__.__name__})"
    if b.size != a.size:
        b = b.resize(a.size, Image.Resampling.BILINEAR)

    arr = np.asarray(a).copy()
    alpha = np.minimum(arr[:, :, 3], np.asarray(b)[:, :, 3])
    kept = float((alpha >= 250).mean())
    if kept < 0.02:
        return None, f"the two masks barely overlap ({kept:.1%} kept) — no garment in common"
    if kept > 0.97:
        return None, f"the intersection is the whole frame ({kept:.1%}) — nothing removed"
    arr[:, :, 3] = alpha
    arr[alpha == 0] = (255, 255, 255, 0)

    buf = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(buf, format="PNG", optimize=True)
    return buf.getvalue(), None


# The four strategies, in the order they are tried. Named at module level
# because a caller may now ask for a SUBSET of them by name (`strategies` /
# `skip`) and the endpoint has to be able to say which names exist without
# reaching into the chain below.
STRATEGY_NAMES = ("cloth-seg", "gemini-mask", "openai-mask", "gemini-paint")

# The provider a refusal carries when the candidate was good enough to keep but
# not good enough to REPLACE what is already on file (item 8). It is not "none"
# — nothing failed, and the caller must not re-queue the product to try the
# same thing again.
KEPT_EXISTING = "kept-existing"


# What the photo audit writes when a PART OF THE GARMENT is gone, as against
# something left in: "collar cut away by background removal" (MID-000569),
# "large part of shirt back missing" (KIL-001625), "background mask cut into
# left strap; strap missing" (KIL-001644), and the `missing_parts` list's own
# "{part} missing".
_MISSING_RE = re.compile(r"\b(missing|cut away|cut off|cut into|eaten|gone|chopped)\b")


def rematte_strategies(reason: str, cfg: dict[str, Any] | None = None) -> list[str] | None:
    """Which strategies a RE-matte should ask for, given why it was asked for.

    None means "the default chain" — the answer for a cut-out being made again
    because part of the GARMENT is missing, where cloth-seg is still the best
    first answer and the fault was in the picture or the mask's edges.

    A list means the cut-out has something LEFT IN it: a stand, a hanger, a
    hand. Re-matting that with cloth-seg produces the same stand — it is a
    clothing parser, a podium is directly beneath the clothing, and its mask
    runs at 320px — and the same verification then passes it again, which is
    why MID-000521 carried "stand visible at bottom" on all four cut-outs
    through every repair run (§1.1). So the mask strategies are asked instead,
    and the result is intersected with cloth-seg.

    The words are photo_audit's own `_LEFTOVER_RE` — IMPORTED rather than
    copied, so the list that decides what is a leftover and the list that
    decides what to do about one cannot drift apart.
    """
    from app.imaging.photo_audit import _LEFTOVER_RE

    text = str(reason or "").lower()
    if not _LEFTOVER_RE.search(text):
        return None
    # BOTH AT ONCE IS NOT THIS CASE. A defect that says a hanger is visible AND
    # that the collar was cut away is a mask wrong in both directions, and a
    # second segmenter is not the answer to the second half — it would trade
    # MID-000521's stand for MID-000569's missing neckband. The default chain,
    # and item 8 keeps whichever cut-out has more of the garment.
    if _MISSING_RE.search(text):
        return None
    # AN EMPTY LIST MEANS DO NOT RE-CUT, and it is NOT the same answer as None.
    #
    #   None  the default chain is the right tool — the missing-garment case
    #         above, where cloth-seg is still the best first answer.
    #   []    this is a leftover and nothing configured can remove it. Sending
    #         it back through the default chain would ask cloth-seg, the
    #         segmenter that left the podium in, to remove the podium — the
    #         same cut, ~20s a view, nothing changed. The caller skips.
    #
    # `or None` used to collapse the two, so emptying `leftover_strategies` to
    # stop the paid calls would have quietly bought a useless free one instead.
    return [n for n in list((cfg or config()).get("leftover_strategies") or [])
            if n in STRATEGY_NAMES]


def remove_background(
    data: bytes, timeout_s: float = 180.0, *,
    strategies: list[str] | None = None,
    skip: list[str] | None = None,
    previous: bytes | None = None,
) -> tuple[bytes | None, str | None, str]:
    """A cut-out, or an honest failure. Returns (png, error, provider).

    Never raises. A caller that cannot get a cut-out needs to record that the
    photograph is still un-matted, which is a finding, not a crash.

    `strategies` is an ALLOW-LIST of the names in STRATEGY_NAMES, not an order:
    the chain's order is a measured property of the providers (see below) and
    not the caller's to choose. Naming a subset that leaves out `cloth-seg`
    INTERSECTS each mask candidate with cloth-seg anyway, which is the point of
    item 6 — the garment parser still decides what is cloth even when it is not
    trusted to decide what is a podium. `skip` is the deny-list, and a name in
    it is not used at all, the intersection included.

    `previous` is the cut-out this one would SUPERSEDE. Given it, a candidate
    that has less garment than what is already on file is refused rather than
    returned (item 8, §2.1): `replaceWithDerived` cannot be undone, so the only
    place this can be decided is before the bytes are handed back.
    """
    attempts: list[str] = []
    cfg = config()

    # MASK FIRST, PAINT LAST.
    #
    # The two mask strategies ask the model only where the garment is; the
    # alpha is applied here to the original bytes, so the garment cannot be
    # smoothed, relit or reframed and the result keeps the source resolution.
    # The magenta strategy — the model returning a repainted picture — stays as
    # a third option because it did succeed on some products, but it goes last:
    # it is the one that produced both observed failure modes.
    #
    # Each strategy is tried twice before moving on, because what we saw was
    # variance rather than incapacity: the same model cut one product cleanly
    # and left a mannequin stand in the next. A second ask is far cheaper than
    # a product held for a human.
    chain = (
        # LOCAL AND FIRST. The only one of the four that measured 0% backdrop on
        # a real studio original, and it returns the FULL source resolution
        # rather than the model's render size.
        #
        # ~20s end to end on a 3000x4000: about 10s of inference and the rest
        # unioning the panels, PNG-encoding 12 megapixels of RGBA and running
        # both verifications over it. Comparable to one generative call, with no
        # network, no bill and no variance. One attempt, because it is
        # deterministic — a second would return the identical bytes.
        ("cloth-seg", "direct", lambda: _cloth_seg(data)),
        ("gemini-mask", "mask", lambda: _gemini(data, timeout_s, MASK_PROMPT)),
        ("openai-mask", "mask", lambda: _openai(data, timeout_s, MASK_PROMPT)),
        ("gemini-paint", "paint", lambda: _gemini(data, timeout_s, PROMPT)),
    )

    # THE POLICY'S CHAIN WHEN THE CALLER NAMES NONE. `strategies` is still the
    # caller's explicit allow-list and still wins; this only decides what "no
    # preference" means, which used to be all four and is now the free one.
    # An empty list in policy is read as "no restriction", so the setting cannot
    # accidentally disable background removal altogether.
    if strategies is None:
        strategies = [str(s) for s in (cfg.get("strategies") or [])] or None

    wanted = {str(s) for s in strategies} if strategies else None
    banned = {str(s) for s in (skip or [])}
    unknown = sorted(n for n in ((wanted or set()) | banned) if n not in STRATEGY_NAMES)
    if unknown:
        # Said rather than silently ignored: a typo in a strategy name would
        # otherwise quietly run the default chain and produce the same stand
        # the caller asked a different segmenter for.
        return None, f"no background-removal strategy is named {', '.join(unknown)}", "none"
    chain = tuple(s for s in chain if (wanted is None or s[0] in wanted) and s[0] not in banned)
    if not chain:
        return None, "every background-removal strategy was excluded by the caller", "none"

    # THE INTERSECTION (item 6). Asked for only when cloth-seg is not in the
    # chain but has not been banned: a caller that named the mask strategies
    # wants the podium gone and the garment parser's opinion about what is
    # cloth kept. Computed once, however many mask candidates are tried.
    intersect = bool(cfg.get("intersect_cloth", True)) and \
        "cloth-seg" not in {s[0] for s in chain} and "cloth-seg" not in banned
    cloth: dict[str, Any] = {}

    def cloth_cut() -> tuple[bytes | None, str | None]:
        if "png" not in cloth:
            cloth["png"], cloth["err"] = _cloth_seg(data)
        return cloth["png"], cloth["err"]

    # Whether any candidate was turned away for losing garment against
    # `previous`. It changes what the failure MEANS: nothing is wrong with the
    # picture, the cut-out on file is simply better, and the product must not
    # be queued to try the same thing again.
    kept_existing: list[str] = []

    for name, kind, fn in chain:
        for attempt in (1, 2) if kind != "direct" else (1,):
            label = f"{name}#{attempt}"
            started = time.perf_counter()
            out, err = fn()
            took = time.perf_counter() - started

            if err or not out:
                log.info("bg-removal %s FAILED in %.1fs: %s", label, took, err)
                attempts.append(f"{label}: {err}")
                continue

            if kind == "direct":
                # Already an RGBA cut-out of the original pixels.
                step_err = None
            elif kind == "mask":
                # The model returned a silhouette; the photograph is ours.
                out, step_err = _apply_mask(data, out)
            else:
                # A repainted picture: OpenAI would give real alpha, Gemini a
                # magenta field that has to be keyed.
                out, step_err = (out, None) if _has_alpha(out) else _key_out(out)

            if step_err or not out:
                log.info("bg-removal %s unusable in %.1fs: %s", label, took, step_err)
                attempts.append(f"{label}: {step_err}")
                continue

            # THE GARMENT PARSER STILL DECIDES WHAT IS CLOTH (item 6). The mask
            # strategy has said where the product is and taken the podium out;
            # cloth-seg says which of what is left is clothing, and takes the
            # hanger out. Its failure is not this candidate's failure — the
            # mask alone is still better than the cut-out being replaced — so
            # it is logged and the candidate goes on unintersected.
            if kind == "mask" and intersect:
                base, cloth_err = cloth_cut()
                if base is None:
                    log.info("bg-removal %s: cloth-seg could not be intersected (%s)",
                             label, cloth_err)
                else:
                    merged, merge_err = _intersect_alpha(out, base)
                    if merged is None:
                        log.info("bg-removal %s: intersection unusable in %.1fs: %s",
                                 label, took, merge_err)
                        attempts.append(f"{label}: intersection {merge_err}")
                        continue
                    out, label = merged, f"{label}∩cloth-seg"

            # Only the paint path can reframe; the mask path preserves the
            # source dimensions by construction, so there is nothing to check.
            if kind == "paint":
                same, frame_why = _same_framing(data, out)
                if not same:
                    log.info("bg-removal %s returned a DIFFERENT image in %.1fs: %s",
                             label, took, frame_why)
                    attempts.append(f"{label}: {frame_why}")
                    continue

            ok, why = _is_cutout(out)
            if not ok:
                log.info("bg-removal %s left a background in %.1fs: %s",
                         label, took, why)
                attempts.append(f"{label}: background still present ({why})")
                continue

            # The border says something was removed; this says whether what
            # remains is only the product. See _kept_backdrop.
            clean, backdrop_why = _kept_backdrop(data, out, cfg)
            if not clean:
                log.info("bg-removal %s kept part of the set in %.1fs: %s",
                         label, took, backdrop_why)
                attempts.append(f"{label}: {backdrop_why}")
                continue

            # LAST, AND A PRECONDITION (item 8). Everything above asks whether
            # this is a good cut-out; this asks whether it is better than the
            # one it would destroy. A candidate that is good but worse is not a
            # failure of the segmenter and must not read as one — see
            # `kept_existing` and the return below.
            if previous is not None:
                better, keep_why = garment_kept(previous, out, cfg)
                if not better:
                    log.info("bg-removal %s would lose garment in %.1fs: %s",
                             label, took, keep_why)
                    attempts.append(f"{label}: {keep_why}")
                    kept_existing.append(label)
                    continue

            log.info("bg-removal %s produced a cut-out in %.1fs (%d KB, %s)",
                     label, took, len(out) // 1024, why)
            return out, None, name

    if kept_existing:
        # NOT A FAILURE, AND IT MUST NOT BE FILED AS ONE. Every candidate that
        # got this far was a real cut-out; each had less of the garment than
        # what is already on file, so the right outcome is the one that already
        # happened — nothing was replaced. Said plainly so the chain does not
        # queue the product to try the same thing again (§2.1).
        return None, ("the existing cut-out was KEPT: no replacement was as complete as it "
                      "is (" + "; ".join(attempts) + ")"), KEPT_EXISTING
    return None, "; ".join(attempts), "none"


def remove_background_b64(data_b64: str, timeout_s: float = 180.0, **kw):
    """Base64 in, base64 out — the shape the HTTP endpoint wants."""
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:  # noqa: BLE001
        return None, "image_base64 is not valid base64", "none"
    out, err, provider = remove_background(raw, timeout_s=timeout_s, **kw)
    return (base64.b64encode(out).decode() if out else None), err, provider
