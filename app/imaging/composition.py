"""Does the model fill the frame? — a pixel test on every render.

MID-000247 (Midtex): the gallery lead, an AI_FRONT_34, shows the model small in
the upper-left of the frame with an empty band across the bottom third; the
back three-quarter of the same product fills its frame edge to edge. The image
gate passed it — the body is coherent, the gender right, the build fine, and
the knee-up crop is what the prompt asks of a three-quarter view. Nothing
asked whether the FIGURE FILLS THE FRAME, and that is what a shopper sees
first.

THE TEST. Renders sit on a flat studio backdrop, so the figure is every pixel
that is not the backdrop colour. Its bounding box against the frame says the
rest: a well-composed render reaches (nearly) the bottom edge — the feet of a
full-body shot, the knees of a three-quarter — and stands most of the frame's
height. A render with a wide empty band below the figure, or a figure that is
a fraction of the frame's height, is a composition defect and that view is
re-rendered.

WHY PIXELS AND NOT THE MODEL. This is arithmetic the same way phase 3's
border ring is: deterministic, a few milliseconds per picture, no call to pay
for and no judgement to calibrate against a person's eye beyond two thresholds.
It also runs over ALL the renders, not the one lead the vision gate judges —
the defect here was on the view the gate never looks at.

THE WATERMARK. Every render carries the vnyx.ai badge in the bottom-right
corner, opaque and not the backdrop colour; counted, it would pull the figure's
bounding box to the bottom edge on every picture and hide exactly the empty
band this looks for. The corner is masked out before the box is taken.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from PIL import Image

log = logging.getLogger("hermes.imaging.composition")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Which renders are measured. Every on-model view: the close-up must fill
    # its frame too, just with a different subject.
    "views": ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"],
    # The empty band allowed below the figure, as a fraction of the frame's
    # height. Calibrated on 189 approved renders (39 products, 17 Sep 2026):
    # the widest band on a good render is 0.10 (p97 0.07; the knee-up
    # three-quarters end at the edge, full-body shots leave a little floor);
    # MID-000247's bad three-quarter leaves 0.22. Midway.
    "max_bottom_margin": 0.15,
    # The figure must stand at least this much of the frame's height: the
    # shortest good render measures 0.87, the bad one 0.76. Midway.
    "min_height_fill": 0.82,
    # A pixel this far (RGB distance) from the backdrop colour is figure.
    "backdrop_tolerance": 28,
    # The vnyx.ai badge: this much of the width and height in the bottom-right
    # corner is ignored.
    "badge_width": 0.24,
    "badge_height": 0.10,
    # A row or column counts as figure when at least this fraction of it is.
    "line_min_fraction": 0.02,
    "fetch_timeout_s": 15,
    "fetch_deadline_s": 40,
}

_SAMPLE_MAX_PX = 320


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for k, v in (((pol or {}).get("quality_gate") or {}).get("composition") or {}).items():
        if v is not None:
            out[k] = v
    return out


def _median(values: list[int]) -> int:
    s = sorted(values)
    return s[len(s) // 2] if s else 0


def measure(data: bytes, cfg: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The figure's bounding box against the frame. None if undecodable.

    Returns fractions of the frame: `bottom_margin`, `top_margin`,
    `left_margin`, `right_margin`, `height_fill`, `width_fill`, plus the
    backdrop colour and the sampled size. Pure arithmetic over one picture.
    """
    cfg = cfg or DEFAULTS
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        log.info("render could not be decoded: %s", type(exc).__name__)
        return None
    img.thumbnail((_SAMPLE_MAX_PX, _SAMPLE_MAX_PX), Image.Resampling.LANCZOS)
    w, h = img.size
    if w < 8 or h < 8:
        return None
    raw = img.tobytes()
    px = list(zip(raw[0::3], raw[1::3], raw[2::3]))

    # The backdrop colour: the median of the outer ring, badge corner excluded.
    band = max(1, int(round(min(w, h) * 0.06)))
    bw, bh = int(w * float(cfg.get("badge_width") or 0.24)), int(h * float(cfg.get("badge_height") or 0.10))

    def in_badge(x: int, y: int) -> bool:
        return x >= w - bw and y >= h - bh

    ring = [px[y * w + x] for y in range(h) for x in range(w)
            if (x < band or x >= w - band or y < band or y >= h - band) and not in_badge(x, y)]
    if not ring:
        return None
    backdrop = (_median([p[0] for p in ring]), _median([p[1] for p in ring]), _median([p[2] for p in ring]))
    tol2 = float(cfg.get("backdrop_tolerance") or 28) ** 2

    def is_figure(x: int, y: int) -> bool:
        if in_badge(x, y):
            return False
        p = px[y * w + x]
        return ((p[0] - backdrop[0]) ** 2 + (p[1] - backdrop[1]) ** 2 + (p[2] - backdrop[2]) ** 2) > tol2

    frac = float(cfg.get("line_min_fraction") or 0.02)
    rows = [y for y in range(h) if sum(1 for x in range(w) if is_figure(x, y)) >= frac * w]
    cols = [x for x in range(w) if sum(1 for y in range(h) if is_figure(x, y)) >= frac * h]
    if not rows or not cols:
        return {"empty": True, "backdrop": list(backdrop), "sampled": [w, h],
                "bottom_margin": 1.0, "top_margin": 1.0, "left_margin": 1.0, "right_margin": 1.0,
                "height_fill": 0.0, "width_fill": 0.0}
    top, bottom, left, right = rows[0], rows[-1], cols[0], cols[-1]
    return {
        "empty": False,
        "backdrop": list(backdrop),
        "sampled": [w, h],
        "top_margin": round(top / h, 3),
        "bottom_margin": round((h - 1 - bottom) / h, 3),
        "left_margin": round(left / w, 3),
        "right_margin": round((w - 1 - right) / w, 3),
        "height_fill": round((bottom - top + 1) / h, 3),
        "width_fill": round((right - left + 1) / w, 3),
    }


def problem(m: dict[str, Any], cfg: dict[str, Any] | None = None) -> str | None:
    """A sentence when the figure does not fill the frame, else None."""
    cfg = cfg or DEFAULTS
    if m.get("empty"):
        return "no figure found against the backdrop"
    parts = []
    if m["bottom_margin"] > float(cfg.get("max_bottom_margin") or 0.18):
        parts.append(f"an empty band of {m['bottom_margin']:.0%} of the frame below the figure")
    if m["height_fill"] < float(cfg.get("min_height_fill") or 0.62):
        parts.append(f"the figure stands only {m['height_fill']:.0%} of the frame's height")
    return "; ".join(parts) if parts else None


@dataclass
class FrameCheck:
    view: str
    url: str
    measured: dict[str, Any] | None
    problem: str | None
    fetched: bool = True


@dataclass
class CompositionVerdict:
    checks: list[FrameCheck] = field(default_factory=list)
    unavailable: bool = False           # nothing could be downloaded

    @property
    def bad_views(self) -> list[str]:
        return [c.view for c in self.checks if c.problem]

    @property
    def reasons(self) -> list[str]:
        return [f"{c.view}: {c.problem}" for c in self.checks if c.problem]

    def as_dict(self) -> dict[str, Any]:
        return {"bad_views": self.bad_views, "reasons": self.reasons, "unavailable": self.unavailable,
                "checks": [{"view": c.view, "url": c.url, "problem": c.problem,
                            "fetched": c.fetched, **(c.measured or {})} for c in self.checks]}


def check(media: list[dict[str, Any]], pol: dict[str, Any] | None,
          fetch: Callable[..., dict[str, bytes | None]] | None = None) -> CompositionVerdict:
    """Measure every live render the policy names. Never raises."""
    from app.net import fetch_all

    cfg = config(pol)
    views = set(cfg.get("views") or [])
    rows = [m for m in media
            if (m.get("mediaType") or "IMAGE") == "IMAGE" and m.get("isCurrent", True)
            and not m.get("deletedAt") and m.get("view") in views and m.get("url")]
    if not rows or not cfg.get("enabled", True):
        return CompositionVerdict()
    fetch = fetch or fetch_all
    got = fetch([str(m["url"]) for m in rows], timeout_s=float(cfg.get("fetch_timeout_s") or 15),
                deadline_s=float(cfg.get("fetch_deadline_s") or 40))
    out = CompositionVerdict()
    fetched = 0
    for m in rows:
        data = got.get(str(m["url"]))
        if data is None:
            out.checks.append(FrameCheck(str(m["view"]), str(m["url"]), None, None, fetched=False))
            continue
        fetched += 1
        measured = measure(data, cfg)
        out.checks.append(FrameCheck(str(m["view"]), str(m["url"]), measured,
                                     problem(measured, cfg) if measured else None))
    out.unavailable = fetched == 0
    return out
