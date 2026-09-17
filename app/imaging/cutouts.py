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
    # Fraction of an edge's outermost line that must be garment to count as
    # touched. Small: a sleeve tip touching the side is a point, not a run.
    "edge_touch_min_fraction": 0.01,
    # A pixel on that line counts as garment only this far (RGB distance) from
    # the backdrop colour — looser than `rgb_tolerance`, because a vignette or
    # compression noise at the frame edge sits 15-25 off the backdrop and is
    # not a hem. BLM-001429: a centred cap read as touching all four edges at
    # 7-10% until this was widened.
    "edge_rgb_tolerance": 40,
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
        # A pixel this far (RGB) from the cut-out's backdrop is garment.
        "cut_tolerance": 28,
        # In the photograph: this close to the garment's median colour is
        # garment, this far from the backdrop's median is not backdrop.
        "garment_tolerance": 45,
        "backdrop_tolerance": 40,
        # The cut-out and the photograph must cover the same frame: this much
        # of the cut-out's garment must be foreground in the photograph.
        "min_overlap": 0.90,
        # Fractions of the garment's area. MID-000569's booth FRONT: the form's
        # neck 2.3%; MID-000253's old booth FRONT 1.6%; the flat lays 0.0-0.2%.
        "loss_min": 0.005,
        "residue_min": 0.008,
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
    that is garment: top, bottom, left, right) and `edges_touched`.
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

    if not opaque:
        return {"width": width, "height": height, "transparent": round(transparent, 4),
                "rgb": None, "coverage": 0.0, "stddev": 0.0, "sampled": total,
                "edge_fill": edge_fill, "edges_touched": edges_touched}

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


def frame_mismatch(border: dict[str, Any], cfg: dict[str, Any]) -> str | None:
    """A sentence when the garment touches every edge — cut to its bounding box."""
    touched = int(border.get("edges_touched") or 0)
    need = int(cfg.get("crop_edges_touched") or 4)
    if touched < need:
        return None
    fill = border.get("edge_fill") or []
    return (f"the garment touches all {touched} edges of the cut-out "
            f"(edge fill {', '.join(f'{f:.0%}' for f in fill)}) — cut to its bounding "
            f"box, not the photograph's frame")


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
#   residue   something else — the form's neck, a hand, a hanger. The garment
#             was on it and the photograph does not show what is behind, so a
#             re-cut reproduces the hole (MID-000253's Hermes re-cut did).
#             Reported, never re-matted: the fix is another photograph (the
#             flat lay beside it) or a ghost-mannequin fill.
#   opening   the backdrop — a real neckline. Nothing to say.
#
# numpy, not the pure-PIL loops above: 270k pixels compared three ways.

def garment_hole(cut_data: bytes, raw_data: bytes, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """Measure the neckline hole of a cut-out against its photograph.

    Returns `aligned` (the two cover the same frame), `hole` (the fraction of
    the garment's area that is empty inside its outline in the neckline
    window), and how that hole reads in the photograph: `loss`, `residue`,
    `opening`, each a fraction of the garment's area; `residue_rgb` is the
    median colour of the residue. None when a picture cannot be decoded.
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
    c = np.asarray(cut.resize((width, height), Image.Resampling.LANCZOS))
    r = np.asarray(raw.resize((width, height), Image.Resampling.LANCZOS)).astype(np.float32)
    c_rgb = c[..., :3].astype(np.float32)
    alpha = c[..., 3]

    band = max(1, int(round(min(width, height) * float(cfg.get("border_fraction") or 0.06))))

    def ring_median(a: Any) -> Any:
        ring = np.concatenate([a[:band].reshape(-1, 3), a[-band:].reshape(-1, 3),
                               a[band:-band, :band].reshape(-1, 3), a[band:-band, -band:].reshape(-1, 3)])
        return np.median(ring, axis=0)

    def dist(a: Any, colour: Any) -> Any:
        return np.sqrt(((a - colour) ** 2).sum(axis=-1))

    cut_back = ring_median(c_rgb)
    garment = (alpha > _OPAQUE_ALPHA) & (dist(c_rgb, cut_back) > float(gc.get("cut_tolerance") or 28))
    area = int(garment.sum())
    if area < 100:
        return {"aligned": False, "note": "no garment found in the cut-out"}

    raw_back = ring_median(r)
    garment_rgb = np.median(r[garment], axis=0)
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
        "garment_px": area,
        "hole": round(float(hole.sum()) / area, 4),
        "loss": round(float(loss.sum()) / area, 4),
        "residue": round(float(residue.sum()) / area, 4),
        "opening": round(float(opening.sum()) / area, 4),
        "residue_rgb": [int(v) for v in np.median(r[residue], axis=0)] if residue.any() else None,
    }
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
    if float(m.get("residue") or 0) >= float(gc.get("residue_min") or 0.008):
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
        cut.border = {k: border[k] for k in ("transparent", "rgb", "coverage", "uniformity",
                                             "stddev", "edge_fill", "edges_touched")}
        check["canvas"] = [border["width"], border["height"]]

        if raw is not None:
            if not (raw.width and raw.height):
                d = dims.get(raw.url)
                if d:
                    raw.width, raw.height = d
            if raw.width and raw.height:
                check["original_canvas"] = [raw.width, raw.height]
                why = canvas_mismatch((cut.width, cut.height), (raw.width, raw.height),
                                      float(cfg.get("canvas_tolerance") or 0.02))
                if why:
                    check["problems"].append(f"canvas: {why}")
            else:
                check["note"] = "original's dimensions unknown — canvas not compared"
        else:
            check["note"] = "no original on file — canvas not compared"

        why = frame_mismatch(cut.border, cfg)
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
            hole = garment_hole(data, rdata, cfg)
            if hole is not None:
                check["hole"] = hole
                cut.border["hole"] = hole
                why, fixable = hole_problem(hole, cfg)
                if why and fixable:
                    check["problems"].append(why)
                elif why:
                    check["flaws"].append(why.replace("{view}", view))
        elif raw is not None and gc_on:
            check["hole_note"] = "photograph could not be downloaded — garment not compared"

        if check["problems"]:
            if view not in bad_views:
                bad_views.append(view)
            for pr in check["problems"]:
                line = f"{view}: {pr}"
                if line not in reasons:      # two FRONT cut-outs, one sentence
                    reasons.append(line)
        if check["flaws"]:
            if view not in unfixable_views:
                unfixable_views.append(view)
            for fl in check["flaws"]:
                line = f"{view}: {fl}"
                if line not in reasons:
                    reasons.append(line)
                    unfixable_reasons.append(line)
        checks.append(check)

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
