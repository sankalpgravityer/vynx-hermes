"""Are the cut-outs right? — readiness phase 3 (docs/READINESS-PLAN.md §4 step 3).

A cut-out row says the segmenter RAN (`processing = BG_REMOVED`), and
app/imaging/background.py can say whether it WORKED (is a background still
there). Neither says whether the result is the picture the catalog wants next
to the raw photograph. Two things go wrong in practice, both measured on the
local clone before this was written:

  * THE CANVAS. Every segmenter but Hermes' own crops its output to the
    garment's bounding box, and vnyx-api composited the backdrop at the crop's
    size — so the cut-out arrives ZOOMED next to the original. Of the 126
    cut-outs with stored dimensions, 115 sit on a different canvas than their
    source and 87 are more than 10% smaller. BOA-006175: 896×1195 cut-outs on
    3000×4000 originals.
  * THE BACKDROP. A tenant with no `background` setting got a transparent
    cut-out from one path and a white one from another; a tenant with #EBEBEB
    got white from the backfill. The requirement is one solid backdrop per
    tenant (decision 1: the tenant's setting, #FFFFFF when unset).

WHAT THIS DOES. For each live FRONT/BACK cut-out, paired with the original it
was cut from (`derivedFromId`, else the superseded RAW of the same view and
origin):

    canvas      the cut-out's ASPECT RATIO within `canvas_tolerance` of the
                original's, and the garment not cut to its bounding box
    background  border ring transparent when the tenant wants transparency,
                otherwise the tenant's colour (ΔE) over most of it

WHY THE RATIO AND NOT THE SIZE. The first cut of this check compared width and
height outright and flagged 50 of 100 approved products — then the pictures
were looked at. The Gemini segmenter returns a FIXED ~1 MP canvas (896×1195
for a 3:4 source, 1279×816 for a landscape one): a uniform downscale of the
whole frame, garment in the same place at the same relative size. That is
not the zoom the requirement forbids ("ratio same as raw, no zoom"), and a
canvas check on absolute pixels would have re-matted half the catalog to no
visible effect. The zoom defect is a cut-out CROPPED TO THE GARMENT: its ratio
is the garment's, not the photograph's, and the garment touches all four
edges. Both of those are what is measured now.

The original's dimensions come from the stored `width/height` when present
(1% of rows) and otherwise from the file HEADER — a ranged read of the first
64 KB (app.net.image_dims), never a full download. The cut-out is downloaded
whole because the border has to be looked at; it is the smaller file.

WHAT IT DECIDES. `judge()` names the views that need re-matting; the chain
sends them through `backfill-bg-removal.ts --replace` (Hermes segmenter, source
canvas, tenant backdrop — phase 2) and calls `judge()` again. Still wrong →
`CUTOUT_UNFIXABLE`, a hold once `readiness.cutouts.hold` is `block` and a flag
while it is `soft`. Every decision here is a pure function that the rules
(IMG.026, IMG.027) and the audit reuse, and the fetching is injected, so the
tests never touch a network.

THE RING, NOT THE WHOLE IMAGE, for the same reason background.py gives: the
garment's own colours would swamp the measurement. And COVERAGE, not a
standard deviation: a corner watermark or a sleeve touching the frame is a
few percent of the ring that a stddev would report as "varied" and a coverage
fraction reports as "84% the backdrop" — which is what BOA-006127 measures,
with its sleeves at the frame edge in the ORIGINAL too. Only a picture with
almost none of the ring in the backdrop colour has a background left in it,
so the floor is low (`backdrop_min_fraction`), and the crop question is asked
of the outermost pixel line of each edge instead.

THE GARMENT'S SHAPE IS DERIVED, NEVER ASSUMED (docs/PICTURE-CHECK-FIXES.md §0).
Every cut-out vnyx-api stores is FULLY OPAQUE, composited on rgb(235,235,235):
measured on MID-000521 and MID-000591 (3000×4000) and BOA-006151 and BOA-006153
(896×1195), all four read 0.0% alpha-clear and 0.0% border-clear with a corner
of 235,235,235. `resolveMatteBackdrop` picks the tenant's backdrop and
`applyBackground` flattens the cut-out onto it, so the PNG is RGBA with an
alpha of 255 everywhere. Anything that took `alpha > 128` for the garment was
therefore measuring THE WHOLE FRAME, and the check silently changed meaning
depending on who made the picture: a cut-out Hermes has just produced really is
transparent and really does score 0.988-1.000 on overlap, while one that has
been through vnyx-api's compositor scores whatever the wall happens to make it.

So `garment_mask()` reads the alpha only when the cut-out genuinely has one
(some of it is clear) and otherwise segments against the cut-out's OWN backdrop
colour, taken from its four corners — flat and uniform by construction, which
is a far easier problem than segmenting the raw photograph's lit wall (§1.3).
A ring median is not good enough for this: it is the garment's colour whenever
the garment crosses the border band, and a garment spanning the full width then
measures 10% of the frame when it is 90% — the mask inverted. When no usable
mask can be derived (nothing separates from the backdrop, or everything does,
so the corner colour was not a backdrop) the measurement is `unknown` and
NOTHING IS FLAGGED.
"""
from __future__ import annotations

import io
import logging
import math
import re
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from PIL import Image

log = logging.getLogger("hermes.imaging.cutouts")

CODE = "CUTOUT_UNFIXABLE"

# The `readiness.cutouts` policy block, with its defaults. app/readiness.py
# merges this into its own config so `readiness.config(pol)["cutouts"]` and
# `cutouts.config(pol)` are the same dict.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # `soft`: a cut-out still wrong after the re-matte is a flag on the run.
    # `block`: it holds the product as CUTOUT_UNFIXABLE. Soft until the
    # shadow run over approved products has reported the false-hold rate —
    # the phase 3 exit criterion.
    "hold": "soft",
    # The cut-out's aspect ratio may differ from the original's by this
    # fraction. A same-ratio downscale passes; a crop to the garment does not.
    "canvas_tolerance": 0.02,
    # A cut-out whose garment touches this many of its four edges (outermost
    # pixel line) was cut to its bounding box — the zoom defect.
    "crop_edges_touched": 4,
    # THE SAME DEFECT WHEN THE CROP WAS PADDED BACK OUT (item 3 of
    # docs/PICTURE-CHECK-FIXES.md). The fraction of the cut-out's own frame
    # that the garment's BOUNDING BOX may cover before the cut-out is a crop to
    # the garment rather than the photograph's frame. Needs no raw segmentation
    # — only the cut-out's derived mask — which is the whole point: the test it
    # replaces asked the lit wall a question it cannot answer (§1.3).
    #
    # Where 0.90 comes from. A segmenter that crops to the bounding box hands
    # back a garment covering 1.00 of the frame; `fitToCanvas` then PADS that
    # crop back out to the source ratio, and because a booth frames the garment
    # roughly proportionally the padding is small — KLE-000028's FRONT keeps the
    # photograph's 0.750 ratio (896×1195 on 3000×4000, which is why
    # `canvas_mismatch` passes it) with the trousers standing 1.4× larger, so
    # one axis is full and the other is within a few percent of full. A CORRECT
    # cut-out is the photograph's frame and keeps the photograph's margin:
    # BOA-006127's shirt reaches the left and right edges — in the original too
    # — but fills only about 0.6 of the height, so its box covers ~0.6 and it is
    # not touched. Anything over 0.90 is a picture with almost no margin left,
    # which a booth capture never is.
    "box_fill_max": 0.90,
    # Fraction of an edge's outermost line that must be garment to count as
    # touched. Small: a sleeve tip touching the side is a point, not a run.
    "edge_touch_min_fraction": 0.01,
    # A pixel on that line counts as garment only this far (RGB distance) from
    # the backdrop colour — looser than `rgb_tolerance`, because a vignette or
    # compression noise at the frame edge sits 15-25 off the backdrop and is
    # not a hem. BLM-001429: a centred cap read as touching all four edges at
    # 7-10% until this was widened.
    "edge_rgb_tolerance": 40,
    # --- the garment's own shape (garment_mask) ------------------------------
    #
    # A pixel is garment when the alpha says so — but ONLY where there is an
    # alpha to read. Every cut-out vnyx-api stores is opaque (§0: 0.0% clear on
    # MID-000521, MID-000591, BOA-006151, BOA-006153), so "some of it is clear"
    # is what separates a real alpha from a compositor's 255 everywhere. A
    # cut-out Hermes produced itself is 50-80% clear; a composited one is 0.0%.
    # 2% is far below anything genuine and far above the measured zero.
    "mask_alpha_min_clear": 0.02,
    # The alpha value at which a pixel counts as kept. 128 is the figure §0
    # names, and the one every caller used to apply to an opaque frame.
    "mask_alpha_threshold": 128,
    # The cut-out's OWN backdrop, for the composited case: the median of four
    # corner patches, each this fraction of the shorter side. The corners and
    # not the border ring, because the ring is the garment's colour whenever the
    # garment crosses it — a garment spanning the full width measured 10% of the
    # frame under a ring median when it is 90%, the mask inverted. A garment in
    # all four corners as well is a picture with no backdrop at all, and that
    # falls out as `unknown` below rather than as a wrong answer.
    "mask_corner_fraction": 0.04,
    # RGB distance from that backdrop at which a pixel is garment. TIGHT — 24
    # against the 40 the raw photograph needs — because this backdrop is flat
    # and uniform BY CONSTRUCTION (`applyBackground` painted it), not a lit
    # studio wall whose centre reads 50 off its edges (§1.3, KLE-000124).
    "mask_rgb_tolerance": 24,
    # Outside this band the derived mask is not a garment and the measurement is
    # `unknown`: below, nothing separated from the backdrop (an empty cut-out,
    # or a garment the exact colour of the backdrop); above, everything did, so
    # the corner colour was not a backdrop and the "garment" is the frame — the
    # very mistake §0 is about. Nothing may be flagged from an unknown mask.
    "mask_min_fraction": 0.005,
    "mask_max_fraction": 0.95,
    # Decision 1: the tenant's `background` setting wins; this when unset.
    "background_default": "#FFFFFF",
    # CIE76 ΔE between the border's median colour and the tenant's hex. #FFFFFF
    # against #EBEBEB is ΔE ≈ 7, so this has to sit well under that.
    "color_tolerance": 4.0,
    # Fraction of the opaque border that must be within `rgb_tolerance` of the
    # backdrop colour. LOW on purpose: in a composited cut-out every pixel that
    # is not garment IS the backdrop, so a garment touching three edges still
    # leaves half the ring exact, while a stockroom wall leaves almost none.
    "backdrop_min_fraction": 0.5,
    # Per-pixel RGB distance that still counts as "the backdrop colour".
    "rgb_tolerance": 12,
    # Fraction of the border that must be fully transparent to call a cut-out
    # transparent (matting feathers edges; a badge is opaque by design).
    "transparent_border_min": 0.85,
    # Above this much transparency on a tenant that wants a colour, the backdrop
    # was applied to part of the picture only.
    "partial_transparent_min": 0.15,
    # Border band, as a fraction of the shorter side, as in imagery.pixels.
    "border_fraction": 0.06,
    "fetch_timeout_s": 15,
    "fetch_deadline_s": 40,
    # THE GARMENT ITSELF against the photograph it was cut from (garment_hole):
    # what the photograph shows where the cut-out has a hole at the top centre
    # of the garment. Downloads the original (5-6 MB a view).
    "garment_check": {
        "enabled": True,
        "work_width": 448,
        # The neckline window: this fraction of the garment's height from its
        # top, this fraction of its width around its centre.
        "neck_rows": 0.25,
        "neck_cols": 0.40,
        # SUPERSEDED by `mask_rgb_tolerance` (item 1). This was the distance
        # from a RING median that made a pixel garment; the ring is the
        # garment's own colour whenever the garment crosses the border band, so
        # the mask now comes from `garment_mask` and the corners instead. Kept
        # so a policy file that still sets it does not fail to load.
        "cut_tolerance": 28,
        # In the photograph: this close to the garment's median colour is
        # garment, this far from the backdrop's median is not backdrop.
        "garment_tolerance": 45,
        "backdrop_tolerance": 40,
        # The cut-out and the photograph must cover the same frame: this much
        # of the cut-out's garment must be foreground in the photograph.
        "min_overlap": 0.90,
        # HOW FAR THE PHOTOGRAPH'S GARMENT MUST SIT FROM ITS OWN BACKDROP before
        # `overlap` is allowed to decide anything (`alignment_check: auto`).
        #
        # RGB distance between the two medians, recorded per cut-out by
        # garment_hole as `separation`. Every measured false positive sits at or
        # below 55 and the one sound judgement at 204, so the floor goes above
        # the highest false and far below the true one. Between 55 and 90 is
        # unmeasured ground, deliberately called unknown rather than guessed:
        # scale_check below is what covers that band, without a photograph.
        "min_separation": 90,
        # THE RAW-PHOTOGRAPH OVERLAP TEST IS GATED, NOT OFF (19 Sep 2026); see
        # frame_problem for the measurements and for what turning it off
        # actually cost. `auto` judges only above `min_separation`; `false`
        # restores the retired behaviour; `true` the pre-retirement one.
        #
        # The original note follows, because the failure it records is real and
        # is exactly what `auto` now routes around.
        # (docs/PICTURE-CHECK-FIXES.md item 2; the `residue_check` note below
        # retired its sibling for the same reason).
        #
        # It asks whether the PHOTOGRAPH shows not-backdrop where the cut-out
        # shows garment. That question is put to a lit studio wall, and the wall
        # cannot answer it: a light garment on a light wall is inside the 40
        # `backdrop_tolerance`, so the photograph reports no garment there and
        # the overlap collapses. Measured directly, garment median against the
        # photograph's border ring (§1.3):
        #
        #   BOA-006151 FRONT   60.9%   backdrop 218,216,209  garment 212,211,205   separation  15
        #   BOA-006153 FRONT   75.7%   backdrop 195,189,182  garment 197,197,195   separation  23
        #   MID-000521 FRONT   73.2%   backdrop 114,113,115  garment 132,132,133   separation  55
        #   MID-000521 FRONT   97.6%   backdrop  83, 81, 88  garment 156,151,149   separation 204
        #
        # Only the one pair with a genuinely dark backdrop cleared 90%. On the
        # dossier this held BOA-006153 as CUTOUT_UNFIXABLE with "only 19% of the
        # cut-out's garment sits where the photograph has garment" on FRONT and
        # 24% on BACK — both cut-outs correct. (19% and 24% were measured with
        # the whole frame standing in for the garment, which is §0; 60.9% and
        # 75.7% are the same test with the mask fixed, and still false.)
        #
        # THE MEASUREMENT IS STILL TAKEN and still recorded on the row —
        # `overlap` and `aligned` are in the JSON exactly as before — it simply
        # stops being able to flag. What it was FOR is now `box_fill_max`
        # (frame_mismatch, item 3), which asks the cut-out about its own frame
        # and never asks the wall anything.
        #
        # ...and `box_fill_max` turned out not to be able to do it on a portrait
        # garment, which is why this is `auto` rather than `False` now.
        "alignment_check": "auto",
        # Fractions of the garment's area. MID-000569's booth FRONT: the form's
        # neck 2.3%; MID-000253's old booth FRONT 1.6%; the flat lays 0.0-0.2%.
        "loss_min": 0.005,
        "residue_min": 0.008,
        # THE FORM'S-NECK TEST IS OFF, and this is why.
        #
        # It asks whether the hole at the neckline shows something that is
        # neither the garment nor the backdrop, and calls that a display form.
        # It cannot tell one from a BRIGHTER PATCH OF THE SAME WALL. The
        # backdrop colour is the median of a border ring, and these studios
        # light the middle of the wall more than its edges: KLE-000124's
        # dungarees hang open between their straps over wall at rgb(243,243,243)
        # while the ring reads about rgb(200,198,197) — a distance of 50, over
        # the 40 that separates "backdrop" from "object" — so the wall between
        # the straps was reported as a form's neck, on both views, at 5.6% and
        # 4.7%. KLE-000045's t-shirt back did the same at 0.9%. Every one of
        # those cut-outs is correct.
        #
        # A garment on a HANGER is the common case and always shows wall
        # through its neck, so the false-positive rate is far worse than the
        # one true case it was built for (MID-000569's form). The `loss` half —
        # garment-coloured pixels missing from the cut-out — survived the same
        # data untouched (0.000 on every false positive) and stays on.
        #
        # The measurement is still taken and still recorded on the row, so this
        # can be switched back on behind a separator that actually works: a
        # form's neck is a solid bright column CENTRED between the shoulders,
        # not a diffuse gradient.
        "residue_check": False,
    },
    # --- the cut-outs against EACH OTHER (scale_outliers) --------------------
    #
    # One garment, one canvas, one booth: two views of it should cover
    # comparable fractions of their frames. Needs no photograph, so it is the
    # one zoom test a lit studio wall cannot defeat — see scale_outliers.
    "scale_check": {
        "enabled": True,
        # Two cut-outs is the normal case (FRONT + BACK) and is enough: the
        # smallest is the reference, so no median is needed.
        "min_views": 2,
        # WHERE 2.2 COMES FROM, AND WHAT IT STILL NEEDS. One measured defect:
        # MID-000615 FRONT at 3.4x its BACK. No measured set of GOOD pairs yet,
        # so this sits roughly midway on a log scale between 1.0 (identical) and
        # that 3.4, which catches it with margin and leaves room for the honest
        # difference between a jacket photographed open and closed. Measure the
        # distribution over a shadow run before `readiness.cutouts.hold` is
        # flipped to `block` on the strength of it.
        "max_area_ratio": 2.2,
        # The reference has to be a plausible garment. Below this the smallest
        # cut-out is one the segmenter ate, and dividing by it would report
        # every healthy sibling as zoomed — `loss` owns that defect, not this.
        "min_reference_area": 0.02,
    },
}


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    """The `readiness.cutouts` block with defaults filled in."""
    out = deepcopy(DEFAULTS)
    over = ((pol or {}).get("readiness") or {}).get("cutouts") or {}
    for k, v in over.items():
        if v is not None:
            out[k] = v
    return out


def enabled(pol: dict[str, Any] | None) -> bool:
    readiness_on = bool(((pol or {}).get("readiness") or {}).get("enabled", True))
    return readiness_on and bool(config(pol).get("enabled", True))


# --------------------------------------------------------------------------- #
# What the tenant wants behind the garment
# --------------------------------------------------------------------------- #

@dataclass
class Backdrop:
    kind: str                      # color | transparent | image
    hex: str | None = None         # #RRGGBB when kind == color
    rgb: tuple[int, int, int] | None = None
    source: str = "default"        # tenant | default
    note: str = ""

    def describe(self) -> str:
        if self.kind == "transparent":
            return "transparent (tenant setting)"
        if self.kind == "image":
            return f"the tenant's backdrop image {self.note}".strip()
        return f"{self.hex} ({'tenant setting' if self.source == 'tenant' else 'default, no tenant setting'})"

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "hex": self.hex, "source": self.source, "note": self.note}


def _setting(settings: Any, name: str) -> Any:
    if settings is None:
        return None
    if isinstance(settings, dict):
        return settings.get(name)
    return getattr(settings, name, None)


def parse_hex(value: str | None) -> tuple[int, int, int] | None:
    s = (value or "").strip().lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{3}", s):
        s = "".join(ch * 2 for ch in s)
    if not re.fullmatch(r"[0-9a-fA-F]{6}", s):
        return None
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def to_hex(rgb: tuple[int, int, int] | list[int]) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02X}{g:02X}{b:02X}"


def expected_backdrop(settings: Any, cfg: dict[str, Any]) -> Backdrop:
    """Decision 1, as a value: the tenant's `background`, the default when unset.

    MIRRORS vnyx-api's `resolveMatteBackdrop`, which is what the re-matte
    applies — the check and the fix must agree or the chain would re-matte a
    cut-out into exactly the state it then refuses. Only the literal
    `transparent` means alpha; `autoApplyBackground` is NOT read, because its
    Prisma default is `false` on eight of fifteen tenants that never chose
    anything, and "never chose" is the case the default colour exists for.
    A path (`/backgrounds/x.png`) is an image backdrop: uniform, colour unknown.
    """
    raw = str(_setting(settings, "background") or "").strip()
    if not raw:
        default_hex = str(cfg.get("background_default") or "#FFFFFF")
        return Backdrop("color", default_hex.upper(), parse_hex(default_hex), "default")
    if raw.lower() == "transparent":
        return Backdrop("transparent", None, None, "tenant")
    rgb = parse_hex(raw)
    if rgb is not None:
        return Backdrop("color", to_hex(rgb), rgb, "tenant")
    return Backdrop("image", None, None, "tenant", note=raw)


# --------------------------------------------------------------------------- #
# Colour distance
# --------------------------------------------------------------------------- #

def _srgb_to_lab(rgb: tuple[int, int, int] | list[int]) -> tuple[float, float, float]:
    def lin(c: float) -> float:
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (lin(float(c)) for c in rgb)
    # sRGB (D65) → XYZ, normalised to the D65 white point.
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b) / 1.00000
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883

    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > 0.008856 else 7.787 * t + 16.0 / 116.0

    fx, fy, fz = f(x), f(y), f(z)
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def delta_e(a: tuple[int, int, int] | list[int], b: tuple[int, int, int] | list[int]) -> float:
    """CIE76 colour difference. ~2.3 is a just-noticeable difference."""
    la, lb = _srgb_to_lab(a), _srgb_to_lab(b)
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(la, lb)))


# --------------------------------------------------------------------------- #
# The garment's own shape — DERIVED, never assumed (§0)
# --------------------------------------------------------------------------- #
#
# Everything geometric in this module stands on this one function, so it says
# out loud where its answer came from (`source`) and is allowed to say it does
# not know. The three sources:
#
#   alpha      the cut-out really is a cut-out — some of it is clear. Hermes'
#              own output; 50-80% of the frame is transparent.
#   backdrop   the cut-out was composited (every one vnyx-api stores: 0.0%
#              clear on all four products measured in §0). Segment against its
#              OWN backdrop colour, from the corners.
#   unknown    neither worked. Nothing may be flagged from this.
#
# numpy: 150k-200k pixels compared three ways, and the callers already resize
# to 384-448px first. Without numpy the answer is `unknown`, which flags
# nothing — the same direction of failure as a picture that did not download.

def corner_backdrop(img: Image.Image, cfg: dict[str, Any]) -> tuple[int, int, int] | None:
    """The cut-out's own backdrop colour, from its four corner patches.

    THE CORNERS AND NOT THE BORDER RING. The ring is what `measure` samples for
    the backdrop CHECK, and it is right for that — but as the source of a
    garment mask it is wrong the moment the garment crosses it. BOA-006127's
    sleeves reach the left and right edges (in the original too), and a garment
    spanning the full width takes the ring median with it: the mask then selects
    the backdrop instead of the garment and reads 10% of the frame where the
    truth is 90%. A corner is the last place a garment reaches, and the backdrop
    under it is flat by construction (`applyBackground` painted it).
    """
    try:
        import numpy as np
    except Exception:  # noqa: BLE001 - numpy missing: the caller reports `unknown`
        return None
    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[0], arr.shape[1]
    side = max(2, int(round(min(w, h) * float(cfg.get("mask_corner_fraction") or 0.04))))
    side = min(side, max(1, w // 2), max(1, h // 2))
    patches = [arr[:side, :side], arr[:side, w - side:],
               arr[h - side:, :side], arr[h - side:, w - side:]]
    px = np.concatenate([p.reshape(-1, 3) for p in patches])
    if px.size == 0:
        return None
    med = np.median(px, axis=0)
    return (int(med[0]), int(med[1]), int(med[2]))


def garment_mask(img: Image.Image, cfg: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    """`(mask, info)` — where the garment is in a cut-out, and how that was known.

    `mask` is a boolean numpy array the size of `img`, or None when no usable
    mask could be derived; `info["source"]` is `alpha`, `backdrop` or `unknown`
    and `info["area"]` the fraction of the frame the mask covers. A caller that
    gets None must report `unknown` and flag nothing — the alternative is what
    §0 describes, a measurement of the frame wearing the garment's name.
    """
    info: dict[str, Any] = {"source": "unknown"}
    try:
        import numpy as np
    except Exception as exc:  # noqa: BLE001
        info["note"] = f"numpy is not available ({type(exc).__name__}) — no mask"
        return None, info

    arr = np.asarray(img.convert("RGBA"))
    if arr.size == 0:
        info["note"] = "the cut-out has no pixels"
        return None, info
    alpha = arr[..., 3]
    rgb = arr[..., :3].astype(np.float32)

    lo = float(cfg.get("mask_min_fraction") or 0.005)
    hi = float(cfg.get("mask_max_fraction") or 0.95)
    a_at = int(cfg.get("mask_alpha_threshold") or 128)
    clear = float((alpha < a_at).mean())
    info["alpha_clear"] = round(clear, 4)

    # 1. A GENUINE ALPHA. Only when some of the picture is actually clear:
    # vnyx-api's compositor leaves 255 everywhere (0.0% clear on MID-000521,
    # MID-000591, BOA-006151 and BOA-006153), and `alpha > 128` on that is the
    # whole frame — the bug this function exists to end.
    if clear >= float(cfg.get("mask_alpha_min_clear") or 0.02):
        mask = alpha >= a_at
        area = float(mask.mean())
        info["area"] = round(area, 4)
        if area < lo:
            info["note"] = f"the alpha keeps only {area:.2%} of the frame — nothing to measure"
            return None, info
        info["source"] = "alpha"
        return mask, info

    # 2. COMPOSITED. Segment against the cut-out's own backdrop, which is flat
    # and uniform — the easy segmentation §0 asks for, as against the raw
    # photograph's lit wall, which is the one §1.3 shows cannot be done.
    back = corner_backdrop(img, cfg)
    if back is None:
        info["note"] = "the cut-out's own backdrop colour could not be read"
        return None, info
    info["backdrop_rgb"] = list(back)
    tol = float(cfg.get("mask_rgb_tolerance") or 24)
    dist = np.sqrt(((rgb - np.asarray(back, dtype=np.float32)) ** 2).sum(axis=-1))
    mask = (dist > tol) & (alpha >= a_at)
    area = float(mask.mean())
    info["area"] = round(area, 4)
    if area < lo:
        # An empty cut-out, or a garment the exact colour of the backdrop.
        info["note"] = (f"only {area:.2%} of the cut-out differs from its backdrop "
                        f"{to_hex(back)} — no garment could be separated")
        return None, info
    if area > hi:
        # The corners were garment, so `back` is the garment's colour and the
        # "mask" is the backdrop. Saying so is the whole point of `unknown`.
        info["note"] = (f"{area:.0%} of the cut-out differs from its corner colour "
                        f"{to_hex(back)} — that colour is not a backdrop, so the garment's "
                        f"shape is unknown")
        return None, info
    info["source"] = "backdrop"
    return mask, info


def mask_box(mask: Any, info: dict[str, Any]) -> dict[str, Any]:
    """The garment's bounding box as fractions of the cut-out's own frame.

    `fill_w`/`fill_h` are the box's width and height over the frame's, `fill`
    the product (the share of the frame the box covers) — which is what item 3
    judges — and `bbox` its corners, all scale-free so the 384px sample and the
    448px one agree. An `unknown` mask carries the reason through untouched.
    """
    out = dict(info)
    if mask is None:
        return out
    import numpy as np

    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return out
    h, w = mask.shape[0], mask.shape[1]
    y0, y1 = int(rows.min()), int(rows.max())
    x0, x1 = int(cols.min()), int(cols.max())
    bw, bh = (x1 - x0 + 1) / max(1, w), (y1 - y0 + 1) / max(1, h)
    out.update({
        "fill_w": round(bw, 4), "fill_h": round(bh, 4), "fill": round(bw * bh, 4),
        "ratio": round((x1 - x0 + 1) / max(1, y1 - y0 + 1), 4),
        "bbox": [round(x0 / w, 4), round(y0 / h, 4),
                 round((x1 + 1) / w, 4), round((y1 + 1) / h, 4)],
    })
    return out


# --------------------------------------------------------------------------- #
# Measuring one cut-out
# --------------------------------------------------------------------------- #

_SAMPLE_MAX_PX = 384
_OPAQUE_ALPHA = 16


def _ring_pixels(img: Image.Image, fraction: float) -> list[tuple[int, int, int, int]]:
    """RGBA pixels of the outer band: top and bottom strips, left and right."""
    w, h = img.size
    band = max(1, int(round(min(w, h) * fraction)))
    strips = [
        img.crop((0, 0, w, band)),
        img.crop((0, max(band, h - band), w, h)),
        img.crop((0, band, band, max(band, h - band))),
        img.crop((max(band, w - band), band, w, max(band, h - band))),
    ]
    px: list[tuple[int, int, int, int]] = []
    for s in strips:
        if s.size[0] > 0 and s.size[1] > 0:
            raw = s.tobytes()  # RGBA, 4 bytes per pixel
            px.extend(zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4]))
    return px


def _median(values: list[int]) -> int:
    s = sorted(values)
    return s[len(s) // 2] if s else 0


def _edge_lines(img: Image.Image) -> list[list[tuple[int, int, int, int]]]:
    """The outermost pixel line of each edge: top, bottom, left, right."""
    w, h = img.size
    lines = []
    for box in ((0, 0, w, 1), (0, h - 1, w, h), (0, 0, 1, h), (w - 1, 0, w, h)):
        raw = img.crop(box).tobytes()
        lines.append(list(zip(raw[0::4], raw[1::4], raw[2::4], raw[3::4])))
    return lines


def measure(data: bytes, cfg: dict[str, Any],
            expected_rgb: tuple[int, int, int] | None = None) -> dict[str, Any] | None:
    """What one cut-out's file says about itself. None if it cannot be decoded.

    Returns the canvas (`width`, `height`); the border ring's `transparent`
    fraction, median `rgb` of its opaque pixels, `coverage` (the fraction of
    opaque ring pixels within `rgb_tolerance` of `expected_rgb` — or of the
    median when no colour is expected) and per-channel `stddev`; and, for the
    crop question, `edge_fill` (the fraction of each edge's outermost line
    that is garment: top, bottom, left, right), `edges_touched`, and `box` —
    the DERIVED garment mask's bounding box as fractions of this frame, with
    the `source` it came from (§0). `box["source"] == "unknown"` means the
    garment's shape could not be read and nothing may be flagged from it.
    """
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001
        log.info("cut-out could not be decoded: %s", type(exc).__name__)
        return None

    width, height = img.size
    small = img.convert("RGBA")
    small.thumbnail((_SAMPLE_MAX_PX, _SAMPLE_MAX_PX), Image.Resampling.LANCZOS)
    px = _ring_pixels(small, float(cfg.get("border_fraction") or 0.06))
    total = len(px)
    if not total:
        return None

    tol2 = float(cfg.get("rgb_tolerance") or 12) ** 2
    opaque = [p[:3] for p in px if p[3] >= _OPAQUE_ALPHA]
    transparent = 1.0 - len(opaque) / total

    med: tuple[int, int, int] | None = None
    if opaque:
        med = (_median([p[0] for p in opaque]), _median([p[1] for p in opaque]),
               _median([p[2] for p in opaque]))
    target = expected_rgb or med

    def is_backdrop(p: tuple[int, int, int, int], within2: float) -> bool:
        """Transparent, or within `within2` (squared) of the backdrop colour."""
        if p[3] < _OPAQUE_ALPHA:
            return True
        if target is None:
            return False
        return ((p[0] - target[0]) ** 2 + (p[1] - target[1]) ** 2
                + (p[2] - target[2]) ** 2) <= within2

    # Which edges the garment reaches. A cut-out cropped to its bounding box
    # has garment on the outermost line of all four edges by construction.
    # Judged with the wider `edge_rgb_tolerance`: only a pixel clearly not the
    # backdrop is garment here, so edge noise does not read as a hem.
    touch_min = float(cfg.get("edge_touch_min_fraction") or 0.01)
    edge_tol2 = float(cfg.get("edge_rgb_tolerance") or 40) ** 2
    edge_fill = []
    for line in _edge_lines(small):
        garment = sum(1 for p in line if not is_backdrop(p, edge_tol2))
        edge_fill.append(round(garment / max(1, len(line)), 4))
    edges_touched = sum(1 for f in edge_fill if f >= touch_min)

    # WHERE THE GARMENT ACTUALLY IS, from the cut-out alone (§0, item 1). The
    # bounding box this yields is what item 3 judges, and it costs no second
    # download: the raw photograph is never opened for it.
    box = mask_box(*garment_mask(small, cfg))

    if not opaque:
        return {"width": width, "height": height, "transparent": round(transparent, 4),
                "rgb": None, "coverage": 0.0, "stddev": 0.0, "sampled": total,
                "edge_fill": edge_fill, "edges_touched": edges_touched, "box": box}

    n = len(opaque)

    def near(centre: tuple[int, int, int]) -> int:
        return sum(
            1 for p in opaque
            if (p[0] - centre[0]) ** 2 + (p[1] - centre[1]) ** 2 + (p[2] - centre[2]) ** 2 <= tol2
        )

    close = near(target)
    # How much of the ring is ONE colour (its own median), whatever that colour
    # is. Separates "a flat backdrop of the wrong colour" from "a room".
    uniformity = near(med) if med is not None else 0
    means = [sum(p[i] for p in opaque) / n for i in range(3)]
    stddev = sum(
        math.sqrt(sum((p[i] - means[i]) ** 2 for p in opaque) / n) for i in range(3)
    ) / 3.0
    return {
        "width": width, "height": height,
        "transparent": round(transparent, 4),
        "rgb": list(med),
        "coverage": round(close / n, 4),
        "uniformity": round(uniformity / n, 4),
        "stddev": round(stddev, 2),
        "sampled": total,
        "edge_fill": edge_fill,
        "edges_touched": edges_touched,
        "box": box,
    }


# --------------------------------------------------------------------------- #
# The two judgements — pure, shared with the rules
# --------------------------------------------------------------------------- #

def canvas_mismatch(cutout: tuple[int, int], original: tuple[int, int],
                    tolerance: float) -> str | None:
    """A sentence when the cut-out is not the shape of its original, else None.

    THE ASPECT RATIO, not the pixel size. A cut-out that is the whole frame
    scaled down keeps the photograph's ratio and the garment's place in it; a
    cut-out cropped to the garment takes the garment's ratio instead. The
    absolute sizes are named so the reader can see the scale, but a uniform
    downscale is not a defect (see the module docstring).
    """
    cw, ch = cutout
    ow, oh = original
    if not (cw and ch and ow and oh):
        return None
    rc = cw / ch
    ro = ow / oh
    if abs(rc - ro) / ro <= tolerance:
        return None
    # THE ORIGINAL'S STORED SIZE MAY BE THE CAMERA'S, NOT THE PICTURE'S. A phone
    # held upright records a 4000×3000 sensor frame with an EXIF rotation, and
    # the upload stores those numbers; every decoder shows — and every
    # segmenter cuts — the 3000×4000 portrait. MID-000253 (17 Sep 2026): both
    # cut-outs were flagged at 0.750 against "1.333", re-cut from the archive
    # at four and a half minutes, and came back 3000×4000 — flagged again. A
    # transposed ratio is the same frame turned, not a crop; a crop to the
    # garment (the defect) is caught by frame_mismatch on the edges as well.
    if abs(rc - oh / ow) / (oh / ow) <= tolerance:
        return None
    return (f"aspect {rc:.3f} ({cw}×{ch}) against the original's {ro:.3f} ({ow}×{oh}) "
            f"— not the photograph's frame (tolerance {tolerance:.0%})")


def frame_mismatch(border: dict[str, Any], cfg: dict[str, Any], *,
                   original: tuple[int, int] | None = None) -> str | None:
    """A sentence when the cut-out is the GARMENT'S frame, not the photograph's.

    Two ways of asking it, both from the cut-out's own pixels and neither
    needing the photograph segmented (item 3 of docs/PICTURE-CHECK-FIXES.md):

      the edges   the garment reaches the outermost pixel line of all four
                  edges. What a segmenter that crops to the bounding box hands
                  back, untouched.
      the box     the garment's bounding box covers `box_fill_max` of the
                  frame. The same crop after `fitToCanvas` has PADDED it back
                  out to the source ratio — KLE-000028's FRONT, 896×1195 on a
                  3000×4000 photograph at the identical 0.750 ratio, with the
                  trousers standing 1.4× larger and touching no edge at all.
                  Ratio and edges both look right; only the content moved.

    `original` is the photograph's `(width, height)` when it is known. It is
    NOT needed to find the defect — it names the ratio the cut-out was padded
    back out to, so the reader can see why `canvas_mismatch` said nothing.

    This is what replaced the raw-photograph overlap test (`alignment_check`,
    retired in item 2): it asks the cut-out about its own frame instead of
    asking a lit studio wall where the garment is, which is the question §1.3
    shows cannot be answered. BOA-006153 and BOA-006151, held at 19% and 24%
    overlap with correct cut-outs, cover nothing like 90% of their frames and
    are silent here.
    """
    touched = int(border.get("edges_touched") or 0)
    need = int(cfg.get("crop_edges_touched") or 4)
    if touched >= need:
        fill = border.get("edge_fill") or []
        return (f"the garment touches all {touched} edges of the cut-out "
                f"(edge fill {', '.join(f'{f:.0%}' for f in fill)}) — cut to its bounding "
                f"box, not the photograph's frame")

    # THE PADDED CROP. Only from a mask that is really the garment's: an
    # `unknown` mask (no backdrop found, or the whole frame differing from the
    # corner colour) flags nothing, because that is exactly the state §0 found
    # every composited cut-out in and it read 100% of the frame.
    box = border.get("box") or {}
    if str(box.get("source") or "unknown") == "unknown":
        return None
    fw, fh = box.get("fill_w"), box.get("fill_h")
    if fw is None or fh is None:
        return None
    fw, fh = float(fw), float(fh)
    covered = float(box.get("fill") if box.get("fill") is not None else fw * fh)
    if covered < float(cfg.get("box_fill_max") or 0.90):
        return None

    # Which way the padding went: the axis that is NOT full. Both full is the
    # crop with no padding at all, which normally trips the edge test above and
    # only reaches here when the garment stops a pixel short of the line.
    if min(fw, fh) >= 0.98:
        shape = "fills its frame on both axes"
    else:
        shape = (f"fills its frame on the {'width' if fw >= fh else 'height'} and was "
                 f"padded back out on the {'height' if fw >= fh else 'width'}")
    tail = ""
    if original and original[0] and original[1]:
        ow, oh = original
        tail = (f"; the frame is {border.get('width')}×{border.get('height')} at the "
                f"photograph's {ow / oh:.3f} ratio ({ow}×{oh}), which is why the ratio "
                f"test says nothing")
    return (f"the garment's bounding box covers {covered:.0%} of the cut-out's frame "
            f"({fw:.0%} of its width, {fh:.0%} of its height, mask from the "
            f"{box.get('source')}) — it {shape}, so this is the garment's bounding box "
            f"and not the photograph's frame, which keeps the photograph's margin{tail}")


def background_mismatch(border: dict[str, Any], expected: Backdrop,
                        cfg: dict[str, Any]) -> str | None:
    """A sentence when the border is not the backdrop the tenant wants, else None."""
    transparent = float(border.get("transparent") or 0.0)
    t_min = float(cfg.get("transparent_border_min") or 0.85)
    partial_min = float(cfg.get("partial_transparent_min") or 0.15)
    floor = float(cfg.get("backdrop_min_fraction") or 0.5)
    rgb = border.get("rgb")
    seen = to_hex(rgb) if rgb else "no opaque pixels"

    if expected.kind == "transparent":
        if transparent >= t_min:
            return None
        return (f"border is opaque ({seen}) where the tenant wants a transparent cut-out "
                f"({transparent:.0%} transparent)")

    if transparent >= t_min:
        return f"border is transparent where the tenant's backdrop is {expected.describe()}"
    if transparent > partial_min:
        return (f"{transparent:.0%} of the border is transparent — the backdrop "
                f"{expected.describe()} was not applied everywhere")

    coverage = float(border.get("coverage") or 0.0)
    # Older measurements carry no `uniformity`; the coverage stands in.
    uniformity = float(border.get("uniformity", coverage) or 0.0)
    if expected.kind == "color" and expected.rgb and rgb:
        if coverage >= floor:
            return None
        # Little of the ring is the tenant's colour. Either the ring is one
        # OTHER colour — a flat backdrop of the wrong colour, named with its
        # ΔE — or it is many colours, and a background is still in the frame.
        if uniformity >= floor:
            de = delta_e(rgb, expected.rgb)
            return (f"border is {seen}, not the tenant's {expected.hex} "
                    f"(ΔE {de:.1f}, tolerance {float(cfg.get('color_tolerance') or 4.0):g})")
        return (f"only {coverage:.0%} of the border is {expected.hex} — "
                f"a background is still in the frame")

    # An image backdrop: the colour is not known here, so uniformity has to do.
    if uniformity < floor:
        return (f"border varies (only {uniformity:.0%} within tolerance of its own "
                f"median {seen}) where the tenant's backdrop image should be uniform")
    return None


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# The garment itself, against the photograph it was cut from
# --------------------------------------------------------------------------- #
#
# MID-000569 (17 September 2026): the sweater's booth cut-out showed a deep
# white "U" where the collar should be — plain on the edit screen. Not a crop,
# not a wrong backdrop: the garment had been photographed on a form, the mask
# took the form's neck out, and the inside of the collar went with it because
# the photograph never showed it. Two vision passes, one asked part by part,
# called the cut-out whole. So it is measured instead: what does the
# PHOTOGRAPH show where the cut-out has a hole at the top centre of the garment?
#
#   loss      garment colour — the mask cut the collar (or a strap) away.
#             A re-cut can restore it: a problem, re-matted.
#   residue   something else — the form's neck, a hand, a hanger. OFF BY
#             DEFAULT (`residue_check`): it cannot tell an object from a
#             brighter patch of the same wall, and flagged correct cut-outs of
#             garments hanging open on a hanger. See that setting's note.
#   opening   the backdrop — a real neckline. Nothing to say.
#
# numpy, not the pure-PIL loops above: 270k pixels compared three ways.

def garment_hole(cut_data: bytes, raw_data: bytes, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Measure the neckline hole of a cut-out against its photograph.

    Returns `aligned` (the two cover the same frame), `hole` (the fraction of
    the garment's area that is empty inside its outline in the neckline
    window), and how that hole reads in the photograph: `loss`, `residue`,
    `opening`, each a fraction of the garment's area; `residue_rgb` is the
    median colour of the residue; `mask` says where the garment's shape came
    from. None when a picture cannot be decoded, and `aligned: None` when the
    garment's shape could not be derived at all — which flags nothing.
    """
    gc = {**(DEFAULTS.get("garment_check") or {}), **(cfg.get("garment_check") or {})}
    try:
        import numpy as np
        from PIL import ImageOps

        cut = Image.open(io.BytesIO(cut_data))
        cut.load()
        cut = cut.convert("RGBA")
        raw = ImageOps.exif_transpose(Image.open(io.BytesIO(raw_data)))
        raw.load()
        raw = raw.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.info("garment hole: a picture could not be decoded: %s", type(exc).__name__)
        return None
    # The original stored at the camera's orientation (see canvas_mismatch).
    if (raw.width > raw.height) != (cut.width > cut.height):
        raw = raw.transpose(Image.Transpose.ROTATE_90)

    width = int(gc.get("work_width") or 448)
    height = max(1, int(round(cut.height * width / max(1, cut.width))))
    cut_small = cut.resize((width, height), Image.Resampling.LANCZOS)
    r = np.asarray(raw.resize((width, height), Image.Resampling.LANCZOS)).astype(np.float32)

    band = max(1, int(round(min(width, height) * float(cfg.get("border_fraction") or 0.06))))

    def ring_median(a: Any) -> Any:
        ring = np.concatenate([a[:band].reshape(-1, 3), a[-band:].reshape(-1, 3),
                               a[band:-band, :band].reshape(-1, 3), a[band:-band, -band:].reshape(-1, 3)])
        return np.median(ring, axis=0)

    def dist(a: Any, colour: Any) -> Any:
        return np.sqrt(((a - colour) ** 2).sum(axis=-1))

    # THE GARMENT, DERIVED (§0, item 1). This used to be the alpha AND a
    # distance from the RING median: the alpha half is the whole frame on every
    # composited cut-out, and the ring half is the garment's own colour whenever
    # the garment crosses the border band — a garment spanning the full width
    # measured 10% of the frame against a truth of 90%, the mask inverted. The
    # corner median cannot be taken hostage that way, and an unreadable cut-out
    # now says so instead of answering with the frame.
    garment, mask_info = garment_mask(cut_small, cfg)
    if garment is None:
        return {"aligned": None, "mask": mask_info,
                "note": mask_info.get("note") or "the garment's shape could not be derived"}
    area = int(garment.sum())
    if area < 100:
        return {"aligned": False, "mask": mask_info, "note": "no garment found in the cut-out"}

    raw_back = ring_median(r)
    garment_rgb = np.median(r[garment], axis=0)
    # HOW FAR APART THE PHOTOGRAPH'S GARMENT AND ITS BACKDROP ACTUALLY ARE.
    #
    # This is the number that decides whether `overlap` below means anything, and
    # until now it was computed here and thrown away — which is why the alignment
    # test had to be retired wholesale rather than gated. The policy block quotes
    # four measurements of it (15, 23, 55, 204) taken by hand from the pictures;
    # recording it makes the same number available to the code, to the dossier
    # and to anyone re-calibrating `min_separation` from real data.
    #
    # Euclidean RGB distance between the two medians, on the same working grid
    # every other measurement here uses.
    separation = float(np.sqrt(((garment_rgb - raw_back) ** 2).sum()))
    back_tol = float(gc.get("backdrop_tolerance") or 40)
    garm_tol = float(gc.get("garment_tolerance") or 45)
    raw_fg = dist(r, raw_back) > back_tol
    overlap = float((raw_fg & garment).sum() / area)
    aligned = overlap >= float(gc.get("min_overlap") or 0.9)

    # Inside the garment's outline, row by row: between its leftmost and its
    # rightmost pixel. The neck opening of a top sits between the shoulders,
    # so it is inside; the gaps between sleeve and body are too, and the
    # photograph shows the backdrop (or the form) there — never the garment.
    rows_any = garment.any(axis=1)
    left = garment.argmax(axis=1)
    right = width - 1 - garment[:, ::-1].argmax(axis=1)
    cols = np.arange(width)[None, :]
    inside = (cols >= left[:, None]) & (cols <= right[:, None]) & rows_any[:, None]

    gy = np.where(rows_any)[0]
    gx = np.where(garment.any(axis=0))[0]
    top, bottom, x0, x1 = int(gy.min()), int(gy.max()), int(gx.min()), int(gx.max())
    gh, gw, cx = bottom - top, x1 - x0, (x0 + x1) / 2
    window = np.zeros_like(garment)
    half = gw * float(gc.get("neck_cols") or 0.4) / 2
    window[top: top + max(1, int(gh * float(gc.get("neck_rows") or 0.25))),
           max(0, int(cx - half)): min(width, int(cx + half) + 1)] = True

    hole = inside & ~garment & window
    to_garment = dist(r, garment_rgb)
    to_back = dist(r, raw_back)
    loss = hole & (to_garment < garm_tol) & (to_back > back_tol)
    opening = hole & (to_back <= back_tol)
    residue = hole & ~loss & ~opening
    out = {
        "aligned": aligned, "overlap": round(overlap, 3),
        # The separation, and the two colours it was taken between, so a reading
        # of the JSON can see WHY the alignment test judged or abstained without
        # re-deriving anything from the pictures.
        "separation": round(separation, 1),
        "raw_backdrop_rgb": [int(v) for v in raw_back],
        "raw_garment_rgb": [int(v) for v in garment_rgb],
        "garment_px": area,
        # How the garment's shape was known, and how much of the frame it is.
        # Recorded so a reading of the JSON can tell an `alpha` mask from a
        # `backdrop` one without re-deriving it — and so §4's pass condition
        # for item 1 ("under 50% of frame on all 12") can be checked from the
        # dossier rather than from the pictures.
        "mask": mask_info,
        "hole": round(float(hole.sum()) / area, 4),
        "loss": round(float(loss.sum()) / area, 4),
        "residue": round(float(residue.sum()) / area, 4),
        "opening": round(float(opening.sum()) / area, 4),
        "residue_rgb": [int(v) for v in np.median(r[residue], axis=0)] if residue.any() else None,
    }
    return out


def frame_problem(m: dict[str, Any] | None, cfg: dict[str, Any], *,
                  derived: bool) -> tuple[str | None, bool]:
    """A sentence when the cut-out is not the photograph's FRAMING, and whether
    a re-cut can fix it. OFF (`alignment_check`) — see below.

    THE SAME-RATIO ZOOM, which every other check was blind to. KLE-000028's
    FRONT cut-out is 896×1195 against a 3000×4000 photograph — the identical
    0.750 ratio, so `canvas_mismatch` passes it — and the trousers stand 1.4×
    larger in it than in the photograph. The segmenter cropped to the garment
    and `fitToCanvas` then PADDED that crop back out to the source ratio, so the
    garment no longer touches any edge. Ratio and edges both looked right; only
    the content had moved.

    What was supposed to catch it is `overlap`: the fraction of the cut-out's
    garment that lands on garment in the photograph at the same coordinates.
    Over six pairs of Hermes' OWN transparent cut-outs a correct one scored
    0.988–1.000 and that one 0.519 — the widest margin any of these checks had.

    WHY IT IS RETIRED (item 2, `alignment_check`). That calibration only held
    because those six cut-outs were transparent. The test's second half asks the
    PHOTOGRAPH where its garment is, by distance from its own border ring, and a
    lit studio wall cannot say: BOA-006151's garment sits 15 from its backdrop
    and BOA-006153's 23, against a `backdrop_tolerance` of 40, so the photograph
    reports no garment where the garment is and the overlap collapses — 60.9%
    and 75.7% with a correct mask, 19% and 24% as the dossier printed them. Only
    MID-000521's decision pair, with a separation of 204, cleared 90%. This is
    the same failure that retired `residue_check`, and it is not fixable by
    calibration: the number depends on the wall, not on the cut-out.

    WHY IT IS BACK, AND GATED RATHER THAN ON (19 Sep 2026). Turning it off
    wholesale did not move the check to `box_fill_max` — it deleted it. That
    test needs the garment's bounding box to cover 90% of the cut-out's frame,
    and since `fill = fill_w x fill_h` with `fill_h <= 1`, reaching 0.90 needs
    the garment to be nine tenths of the frame's WIDTH. A garment on a portrait
    canvas never is: MID-000615's FRONT cut-out is zoomed, scored `overlap`
    0.805 against its photograph, and covers 30.8% of its frame — so it passed
    `box_fill_max`, passed `canvas_mismatch` (896x1195 on 3000x4000, the
    identical 0.750 ratio) and passed the edge test, and the chain reported
    "every cut-out is on its canvas and the tenant's backdrop".

    The four false positives all share one property, and it is measurable: the
    photograph's garment is nearly the colour of its own backdrop, so the
    photograph cannot say where the garment is. `garment_hole` now RECORDS that
    separation, and `auto` judges only above `min_separation`:

        BOA-006151 FRONT   60.9%   separation  15   abstain  (cut-out correct)
        BOA-006153 FRONT   75.7%   separation  23   abstain  (cut-out correct)
        MID-000521 FRONT   73.2%   separation  55   abstain  (cut-out correct)
        MID-000521 FRONT   97.6%   separation 204   judge -> passes

    Below the floor the answer is UNKNOWN, not "fine" — so nothing is flagged
    and nothing is cleared, which is the rule §0 sets for every derived mask.
    `scale_outliers` is the independent catch for exactly that band: it compares
    a product's cut-outs with each other and never asks the wall anything.

    Three settings, because two of the three answers are useful:
        `false`  off, as it was
        `auto`   judge only where the photograph can answer  (the default)
        `true`   judge always — the pre-retirement behaviour

    ONLY WHEN THE TWO ARE REALLY THE SAME PICTURE. With a `derivedFromId` edge
    they are, by construction. Without one the pairing is a guess from view and
    origin (imagery.cutout_pairs), and two different photographs of the same
    view disagree for an honest reason — so that is a note, never a defect.
    """
    gc = {**(DEFAULTS.get("garment_check") or {}), **(cfg.get("garment_check") or {})}
    mode = _alignment_mode(gc.get("alignment_check"))
    if mode == "off":
        return None, False
    # `aligned is not False` also covers `None`: the mask could not be derived,
    # so the frames were never compared and there is nothing to say.
    if not m or m.get("aligned") is not False:
        return None, False
    overlap = m.get("overlap")
    if overlap is None:
        return None, False

    separation = m.get("separation")
    floor_sep = float(gc.get("min_separation") or 90)
    if mode == "auto":
        if separation is None:
            # An older row, measured before the separation was recorded. Silent:
            # judging it would reintroduce exactly the false positives the gate
            # exists to stop.
            return None, False
        if float(separation) < floor_sep:
            return None, False

    floor = float(gc.get("min_overlap") or 0.9)
    sep_text = (f", separation {float(separation):.0f}" if separation is not None else "")
    text = (f"framing: only {overlap:.0%} of the cut-out's garment sits where the "
            f"photograph has garment (a correct cut-out scores over {floor:.0%}"
            f"{sep_text}) — the cut-out is zoomed or shifted against the "
            f"photograph it was cut from")
    if not derived:
        return (text + "; the original was matched by view, not by a derivation "
                "edge, so the two may simply be different photographs"), False
    return text + ". Re-cut it from the original on the source canvas.", True


def _alignment_mode(value: Any) -> str:
    """`off` | `auto` | `always` from whatever the policy file holds.

    A tri-state written in YAML arrives as a bool for `true`/`false` and as a
    string for `auto`, so both shapes have to resolve. Anything unrecognised is
    `off`: a policy typo must not silently switch a check on.
    """
    if isinstance(value, bool):
        return "always" if value else "off"
    text = str(value or "").strip().lower()
    if text in {"auto", "gated", "separation"}:
        return "auto"
    if text in {"true", "on", "yes", "1", "always"}:
        return "always"
    return "off"


def scale_outliers(checks: list[dict[str, Any]], cfg: dict[str, Any]) -> dict[str, str]:
    """`{view: sentence}` for cut-outs whose garment stands far larger than its
    siblings' on the same product.

    THE ZOOM TEST THAT NEEDS NO PHOTOGRAPH, and that is the whole point of it.
    Every other way of finding a zoom compares the cut-out with the picture it
    was cut from, and §1.3 shows a lit studio wall cannot answer that question.
    This compares the cut-outs with EACH OTHER: one garment, one canvas, one
    booth, so two views of it should occupy comparable areas of their frames.

    Measured on MID-000615, whose FRONT is zoomed and whose BACK is not:

        FRONT   mask area 0.3078   <- 3.4x the frame area, ~1.9x linear
        BACK    mask area 0.0894

    THE SMALLEST IS THE REFERENCE, because a zoom only ever makes the garment
    bigger: cropping to the bounding box and padding back out enlarges it, and
    nothing in the pipeline shrinks one. So the outlier is the large one, and
    taking the smallest as the truth needs no median and works with two views.

    It cannot see a product whose cut-outs are ALL zoomed by the same amount —
    the ratio is then 1.0 and this stays silent. That case is the separation-
    gated `frame_problem` above, and the two are deliberately independent.

    An `unknown` mask is skipped rather than counted as zero: §0's rule is that
    nothing may be flagged from a mask that is not really the garment's.
    """
    sc = {**(DEFAULTS.get("scale_check") or {}), **(cfg.get("scale_check") or {})}
    if not sc.get("enabled", True):
        return {}

    areas: list[tuple[str, float]] = []
    for c in checks:
        # `mask` is measured from the cut-out alone (measure -> mask_box), so
        # this runs on a product whose photographs never downloaded. `garment`
        # is the older key and holds the same dict; read it as a fallback so a
        # caller that only has that shape still works.
        mask = c.get("mask") or ((c.get("garment") or {}).get("mask")) or {}
        if str(mask.get("source") or "unknown") == "unknown":
            continue
        area = mask.get("area")
        if area is None:
            continue
        areas.append((str(c.get("view") or "?"), float(area)))

    if len(areas) < int(sc.get("min_views") or 2):
        return {}

    smallest = min(a for _, a in areas)
    # A REFERENCE THAT IS ITSELF BROKEN WOULD FLAG EVERYTHING. A mask this small
    # is a cut-out the segmenter ate, not a garment standing far away, and
    # dividing by it would report every healthy sibling as zoomed. `loss` is the
    # check that owns that defect; this one declines to speak.
    if smallest < float(sc.get("min_reference_area") or 0.02):
        return {}

    ratio_max = float(sc.get("max_area_ratio") or 2.2)
    out: dict[str, str] = {}
    for view, area in areas:
        ratio = area / smallest
        if ratio < ratio_max:
            continue
        out[view] = (
            f"scale: the garment covers {area:.1%} of this cut-out's frame against "
            f"{smallest:.1%} on the same product's other view(s) — {ratio:.1f}x the "
            f"area, about {ratio ** 0.5:.1f}x linear. Two views of one garment on one "
            f"canvas should be comparable, so this one is cropped to the garment and "
            f"scaled back up. Re-cut it from the original on the source canvas."
        )
    return out


def hole_problem(m: dict[str, Any] | None, cfg: dict[str, Any]) -> tuple[str | None, bool]:
    """A sentence about the neckline hole, and whether a re-cut can fix it.

    (None, False) when there is nothing to say — no measurement, the two
    pictures do not cover the same frame, or the hole is a real opening.
    """
    gc = {**(DEFAULTS.get("garment_check") or {}), **(cfg.get("garment_check") or {})}
    if not m or not m.get("aligned"):
        return None, False
    if float(m.get("loss") or 0) >= float(gc.get("loss_min") or 0.005):
        return (f"neckline: {m['loss']:.1%} of the garment was cut away at the collar — "
                f"garment colour in the photograph, nothing in the cut-out"), True
    if (gc.get("residue_check")
            and float(m.get("residue") or 0) >= float(gc.get("residue_min") or 0.008)):
        rgb = m.get("residue_rgb") or []
        light = bool(rgb) and sum(rgb) / len(rgb) >= 200
        if light:
            # The white form's neck. What is behind it was never photographed.
            return (f"neckline: a hole of {m['residue']:.1%} of the garment at the collar where the "
                    f"photograph shows the form's neck — the inside of the collar was never "
                    f"photographed, so a re-cut reproduces the hole; use the flat-lay cut-out as "
                    f"the {'{view}'} or reshoot on a hanger"), False
        # Dark, and neither the backdrop nor the garment's colour: the mask thinned
        # or dropped the garment's upper body (MID-000253's Hermes re-cut faded the
        # shoulders to the backdrop). Another cut can do better.
        return (f"neckline: {m['residue']:.1%} of the garment at the collar and shoulders is "
                f"missing from the cut-out while the photograph shows something dark there, not "
                f"the backdrop — the mask thinned or dropped the garment"), True
    return None, False


@dataclass
class CutoutVerdict:
    # ok       every cut-out is on its canvas and backdrop
    # bad      at least one is not — `bad_views` says which, `reasons` why
    # unknown  nothing could be measured (downloads failed) — not a judgement
    # skipped  disabled, or no cut-out to look at
    # pending  a dry run: the re-matte would run, the re-check could not
    action: str
    hold: str = "soft"                          # readiness.cutouts.hold when judged
    code: str | None = None                     # CUTOUT_UNFIXABLE when bad
    reasons: list[str] = field(default_factory=list)
    # The views a RE-CUT can fix (the matte step re-mattes these) …
    bad_views: list[str] = field(default_factory=list)
    # … and the views it cannot: the photograph itself lacks what the cut-out
    # needs (the inside of a collar behind the form's neck). Their sentences
    # are in `reasons` too and repeated here for the step note.
    unfixable_views: list[str] = field(default_factory=list)
    unfixable_reasons: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    expected: dict[str, Any] = field(default_factory=dict)
    unavailable: bool = False                   # could not run; not about the product
    fetched: int = 0
    measured: int = 0

    @property
    def blocks(self) -> bool:
        return self.action == "bad" and self.hold == "block"

    @property
    def soft(self) -> list[str]:
        return list(self.reasons) if self.action == "bad" and self.hold != "block" else []

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["blocks"] = self.blocks
        return d

    def summary(self) -> str:
        head = {
            "ok": "every cut-out is on its canvas and the tenant's backdrop",
            "bad": ("WRONG" if self.hold == "block" else "wrong (soft)"),
            "unknown": "could not be measured",
            "skipped": "skipped",
            "pending": "would re-matte",
        }.get(self.action, self.action)
        line = head
        if self.reasons:
            line += " — " + "; ".join(self.reasons)
        return line


def judge(p: Any, pol: dict[str, Any] | None, *,
          fetch: Callable[..., dict[str, bytes | None]] | None = None,
          read_dims: Callable[..., dict[str, tuple[int, int] | None]] | None = None,
          ) -> CutoutVerdict:
    """Measure every live FRONT/BACK cut-out of a ProductSnapshot.

    `fetch(urls, timeout_s, deadline_s)` and `read_dims(urls, ...)` default to
    the network readers in app.net and are injected by the tests. The measured
    `width`, `height` and `border` are written back onto the snapshot's media
    rows, so the imagery rules (IMG.026, IMG.027) can then judge the same
    evidence without a second download.
    """
    from app.net import fetch_all, image_dims_all
    from app.rules import imagery

    cfg = config(pol)
    hold = str(cfg.get("hold") or "soft")
    if not enabled(pol):
        return CutoutVerdict("skipped", hold, reasons=["disabled in policy (readiness.cutouts)"])

    pairs = imagery.cutout_pairs(p, pol or {})
    if not pairs:
        return CutoutVerdict("skipped", hold, reasons=["no garment cut-out to check"])

    expected = expected_backdrop(getattr(p, "imagery_settings", None), cfg)
    fetch = fetch or fetch_all
    read_dims = read_dims or image_dims_all
    timeout = float(cfg.get("fetch_timeout_s") or 15)
    deadline = float(cfg.get("fetch_deadline_s") or 40)

    fetched = fetch([c.url for _, c, _ in pairs], timeout_s=timeout, deadline_s=deadline)
    # The originals' dimensions: stored when present, otherwise the header.
    need_dims = [r.url for _, _, r in pairs
                 if r is not None and not (r.width and r.height)]
    dims = read_dims(need_dims, timeout_s=timeout, deadline_s=deadline) if need_dims else {}
    # The originals themselves, for the garment check (5-6 MB a view; twice
    # the budget). A photograph that does not arrive skips the check, quietly.
    gc_on = bool(((cfg.get("garment_check") or {}).get("enabled", True)))
    raw_urls = list(dict.fromkeys(r.url for _, _, r in pairs if r is not None)) if gc_on else []
    raws = fetch(raw_urls, timeout_s=timeout * 2, deadline_s=deadline * 2) if raw_urls else {}

    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    bad_views: list[str] = []
    unfixable_views: list[str] = []
    unfixable_reasons: list[str] = []
    got = measured = 0
    for view, cut, raw in pairs:
        data = fetched.get(cut.url)
        check: dict[str, Any] = {"view": view, "url": cut.url,
                                 "original": raw.url if raw else None,
                                 "problems": [], "measured": False}
        if data is None:
            check["note"] = "cut-out could not be downloaded"
            checks.append(check)
            continue
        got += 1
        border = measure(data, cfg, expected.rgb)
        if border is None:
            check["note"] = "cut-out could not be decoded"
            checks.append(check)
            continue
        measured += 1
        check["measured"] = True
        cut.width, cut.height = border["width"], border["height"]
        # `box` rides along with the rest so the rules (IMG.026) can judge the
        # padded crop from the row, with no second download — it is measured
        # from the cut-out alone and carries its own `source`.
        cut.border = {k: border[k] for k in ("width", "height", "transparent", "rgb",
                                             "coverage", "uniformity", "stddev",
                                             "edge_fill", "edges_touched", "box")}
        check["canvas"] = [border["width"], border["height"]]
        # THE CUT-OUT'S OWN MASK, on the check row. Measured by `measure` from
        # this picture alone — no photograph, no download — which is what lets
        # `scale_outliers` compare the views without asking a studio wall
        # anything. It also puts the mask's source and area in the JSON for a
        # product whose original never arrived, where `garment` is absent.
        check["mask"] = border["box"]

        original_dims: tuple[int, int] | None = None
        if raw is not None:
            if not (raw.width and raw.height):
                d = dims.get(raw.url)
                if d:
                    raw.width, raw.height = d
            if raw.width and raw.height:
                original_dims = (raw.width, raw.height)
                check["original_canvas"] = [raw.width, raw.height]
                why = canvas_mismatch((cut.width, cut.height), (raw.width, raw.height),
                                      float(cfg.get("canvas_tolerance") or 0.02))
                if why:
                    check["problems"].append(f"canvas: {why}")
            else:
                check["note"] = "original's dimensions unknown — canvas not compared"
        else:
            # "NO ORIGINAL ON FILE" IS NOT A FINDING (item 9, §2.2). The
            # sweatshirt's cut-outs are WEB origin while its photographs are
            # DECISION and PHOTOBOOTH, so `cutout_pairs` finds no pair — which
            # is the NORMAL state for a cut-out uploaded through the web rather
            # than produced from a booth capture. The dossier draws anything in
            # `note` with a red border and a red caption, as though the picture
            # were faulty; a reason a check could not run is not a defect, so it
            # is recorded on its own key, in the JSON and nowhere else.
            check["original_note"] = "no original on file — canvas not compared"

        why = frame_mismatch(cut.border, cfg, original=original_dims)
        if why:
            check["problems"].append(f"frame: {why}")
        why = background_mismatch(cut.border, expected, cfg)
        if why:
            check["problems"].append(f"backdrop: {why}")

        # THE GARMENT against its photograph (garment_hole): the collar cut
        # away is a problem a re-cut fixes; the form's neck showing through
        # is a flaw no re-cut can, and is reported as such.
        check["flaws"] = []
        rdata = raws.get(raw.url) if (raw is not None and gc_on) else None
        if rdata is not None:
            measured_garment = garment_hole(data, rdata, cfg)
            if measured_garment is not None:
                check["garment"] = measured_garment
                cut.border["garment"] = measured_garment
                # THE FRAMING FIRST. A cut-out that is not the photograph's
                # frame cannot be asked about its neckline: the neckline
                # window would be measured over the wrong part of the picture.
                derived = bool(cut.derived_from_id and raw.id
                               and cut.derived_from_id == raw.id)
                why, fixable = frame_problem(measured_garment, cfg, derived=derived)
                if why and fixable:
                    check["problems"].append(why)
                elif why:
                    check["note"] = why
                else:
                    why, fixable = hole_problem(measured_garment, cfg)
                    if why and fixable:
                        check["problems"].append(why)
                    elif why:
                        check["flaws"].append(why.replace("{view}", view))
        elif raw is not None and gc_on:
            check["garment_note"] = "photograph could not be downloaded — garment not compared"

        checks.append(check)

    # THE CROSS-VIEW SCALE TEST, after the loop because it is the one question
    # that is about the product rather than about a picture. Folded into the
    # per-check `problems` first, so a reader of the JSON finds it on the row it
    # belongs to rather than only in the run's reasons.
    for view, why in scale_outliers(checks, cfg).items():
        for c in checks:
            if str(c.get("view") or "?") == view and c.get("measured"):
                if why not in c["problems"]:
                    c["problems"].append(why)

    for check in checks:
        view = str(check.get("view") or "?")
        if check.get("problems"):
            if view not in bad_views:
                bad_views.append(view)
            for pr in check["problems"]:
                line = f"{view}: {pr}"
                if line not in reasons:      # two FRONT cut-outs, one sentence
                    reasons.append(line)
        if check.get("flaws"):
            if view not in unfixable_views:
                unfixable_views.append(view)
            for fl in check["flaws"]:
                line = f"{view}: {fl}"
                if line not in reasons:
                    reasons.append(line)
                    unfixable_reasons.append(line)

    if measured == 0:
        return CutoutVerdict("unknown", hold, reasons=["no cut-out could be downloaded and decoded"],
                             checks=checks, expected=expected.as_dict(),
                             unavailable=True, fetched=got, measured=0)
    if bad_views or unfixable_views:
        return CutoutVerdict("bad", hold, code=CODE, reasons=reasons, bad_views=bad_views,
                             unfixable_views=unfixable_views, unfixable_reasons=unfixable_reasons,
                             checks=checks, expected=expected.as_dict(),
                             fetched=got, measured=measured)
    return CutoutVerdict("ok", hold, checks=checks, expected=expected.as_dict(),
                         fetched=got, measured=measured)
