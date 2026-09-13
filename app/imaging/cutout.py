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
import time
from typing import Any

from app.config import policy, settings
from app.imaging import background as bgcheck
from app.imaging import openai_image
from app.net import genai_client_args

log = logging.getLogger("hermes.cutout")

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


def _kept_backdrop(source: bytes, result: bytes) -> tuple[bool, str]:
    """Did the cut-out keep part of the studio backdrop? (ok, why)

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
    good cut-outs; the check abstains and says so. A white garment on a white
    sweep will trip it — correctly, since that is the case where the boundary
    is genuinely ambiguous and a human should look.
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
        return True, "unchecked"

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
        return True, f"source border is not uniform (spread {spread:.0f}), not checked"

    arr = np.asarray(res)
    if arr.shape[:2] != src.shape[:2]:
        return True, "sizes differ, not checked"

    opaque = arr[:, :, 3] >= 250
    if not opaque.any():
        return True, "nothing opaque"

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


def remove_background(
    data: bytes, timeout_s: float = 180.0
) -> tuple[bytes | None, str | None, str]:
    """A cut-out, or an honest failure. Returns (png, error, provider).

    Never raises. A caller that cannot get a cut-out needs to record that the
    photograph is still un-matted, which is a finding, not a crash.
    """
    attempts: list[str] = []

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
    strategies = (
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

    for name, kind, fn in strategies:
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
            clean, backdrop_why = _kept_backdrop(data, out)
            if not clean:
                log.info("bg-removal %s kept part of the set in %.1fs: %s",
                         label, took, backdrop_why)
                attempts.append(f"{label}: {backdrop_why}")
                continue

            log.info("bg-removal %s produced a cut-out in %.1fs (%d KB, %s)",
                     label, took, len(out) // 1024, why)
            return out, None, name

    return None, "; ".join(attempts), "none"


def remove_background_b64(data_b64: str, timeout_s: float = 180.0):
    """Base64 in, base64 out — the shape the HTTP endpoint wants."""
    try:
        raw = base64.b64decode(data_b64, validate=True)
    except Exception:  # noqa: BLE001
        return None, "image_base64 is not valid base64", "none"
    out, err, provider = remove_background(raw, timeout_s=timeout_s)
    return (base64.b64encode(out).decode() if out else None), err, provider
