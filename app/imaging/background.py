"""Does the picture actually have its background removed?

The `processing` column says whether the segmenter RAN. It cannot say whether the
segmenter WORKED, and the two come apart in practice: every provider path in
vnyx-api retries a 429 forever but returns whatever the API hands back on a 200,
so a provider that fails soft — returning the original bytes, or a barely-touched
image — produces a row marked BG_REMOVED sitting on top of an untouched
photograph. Nothing downstream can tell.

So this looks at the pixels.


THE TEST

The garment is centred by construction, so the frame edge is background unless
the shot is cropped unusually tight. Sample a border ring and ask two questions:

  1. Is there an alpha channel, and is the ring mostly transparent?
     -> a genuine cut-out.
  2. Failing that, how much does the ring VARY?
     -> a studio sweep or a flat backdrop is near-uniform; a stockroom shelf, a
        wooden floor or a doorway is not.

Threshold, not certainty. A garment photographed against a plain white wall reads
as a backdrop, which is why the middle band escalates to the vision model instead
of guessing, and why IMG.013 is advisory rather than blocking.


WHY A RING AND NOT THE WHOLE IMAGE

The garment's own colour variance would swamp the measurement — a patterned shirt
on a perfect white sweep has a very high whole-image stddev and a very low border
one. Masking to the ring is what makes the number mean "background".
"""

from __future__ import annotations

import io
import logging
from typing import Any

import httpx
from PIL import Image, ImageStat

from app.net import fetch_all
from app.models import BackgroundCheck, BackgroundVerdict, MediaAsset

log = logging.getLogger("hermes.imaging.background")

# Downscale before measuring. Two reasons: a 4K photograph costs real time to
# decode and sample, and the ring statistic is a large-scale property — a room
# still varies across the frame after downsampling, while a sweep stays flat.
# Small enough to be fast, large enough that resampling does not average a busy
# background into a smooth one.
_SAMPLE_MAX_PX = 384


def _ring_mask(size: tuple[int, int], fraction: float) -> Image.Image:
    """A mask that is white on the outer band and black everywhere inside."""
    width, height = size
    band = max(1, int(round(min(width, height) * fraction)))
    mask = Image.new("L", size, 255)
    inner = (band, band, max(band + 1, width - band), max(band + 1, height - band))
    if inner[2] > inner[0] and inner[3] > inner[1]:
        mask.paste(0, inner)
    return mask


def classify_pixels(data: bytes, cfg: dict[str, Any]) -> tuple[BackgroundVerdict, float, str]:
    """Verdict, confidence, and a sentence explaining the number behind it."""
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:  # noqa: BLE001 — any unreadable byte string lands here
        return BackgroundVerdict.UNKNOWN, 0.0, f"could not decode ({type(exc).__name__})"

    img.thumbnail((_SAMPLE_MAX_PX, _SAMPLE_MAX_PX), Image.Resampling.LANCZOS)
    mask = _ring_mask(img.size, float(cfg.get("border_fraction") or 0.06))

    # --- 1. a real cut-out ----------------------------------------------------
    #
    # `P` mode with a transparency key is how a palettised PNG carries alpha, and
    # it has no 'A' band until converted — so convert rather than test for one.
    has_alpha = img.mode in ("RGBA", "LA") or (
        img.mode == "P" and "transparency" in img.info
    )
    if has_alpha:
        alpha = img.convert("RGBA").getchannel("A")
        # histogram(mask) counts only the pixels the mask selects, in C.
        hist = alpha.histogram(mask)
        total = sum(hist)
        clear = hist[0] / total if total else 0.0
        floor = float(cfg.get("transparent_border_min") or 0.85)
        if clear >= floor:
            return (
                BackgroundVerdict.TRANSPARENT,
                min(1.0, clear),
                f"{clear:.0%} of the border is fully transparent",
            )
        # An alpha channel that is NOT transparent at the edge is the interesting
        # case: a PNG saved with alpha but never actually matted. Fall through and
        # judge it on colour like any opaque image, rather than trusting the mode.

    # --- 2. uniform backdrop vs a real scene ----------------------------------
    stat = ImageStat.Stat(img.convert("RGB"), mask)
    spread = sum(stat.stddev) / len(stat.stddev)

    uniform_max = float(cfg.get("uniform_border_max_stddev") or 12.0)
    ambiguous_max = float(cfg.get("ambiguous_border_max_stddev") or 26.0)

    if spread <= uniform_max:
        return (
            BackgroundVerdict.BACKDROP,
            1.0 - (spread / uniform_max) * 0.4,
            f"border varies by {spread:.1f}/255 — a flat backdrop or sweep",
        )
    if spread <= ambiguous_max:
        # Deliberately not decided here. A plain wall and a cheap sweep produce
        # the same number, and calling either way at this range is guessing.
        return (
            BackgroundVerdict.SCENE,
            0.35,
            f"border varies by {spread:.1f}/255 — between a backdrop and a scene, "
            f"not conclusive",
        )
    return (
        BackgroundVerdict.SCENE,
        min(1.0, spread / (ambiguous_max * 2)),
        f"border varies by {spread:.1f}/255 — a real background is still there",
    )


def is_ambiguous(check: BackgroundCheck, cfg: dict[str, Any]) -> bool:
    """Worth spending a vision call on.

    Only the middle band. A confident transparent cut-out and an obviously busy
    photograph both need no second opinion, and asking for one on every image
    would put a model call behind every button press.
    """
    return check.verdict is BackgroundVerdict.SCENE and check.confidence < 0.5


def check_media(
    assets: list[MediaAsset],
    cfg: dict[str, Any],
    client: httpx.Client | None = None,
) -> list[BackgroundCheck]:
    """Fetch and classify each asset. Never raises.

    Downloads run concurrently under one wall-clock deadline — see
    `app/net.py`. Anything that fails or does not arrive in time yields
    UNKNOWN rather than an exception: this sits behind a button on a product
    page, and one rate-limited R2 object must not cost the operator the whole
    verification.
    """
    limit = int(cfg.get("max_images_fetched") or 8)
    targets = assets[:limit]
    if not targets:
        return []

    fetched = fetch_all(
        [a.url for a in targets],
        timeout_s=float(cfg.get("fetch_timeout_s") or 15),
        deadline_s=float(cfg.get("fetch_deadline_s") or 30),
        client=client,
    )

    out: list[BackgroundCheck] = []
    for asset in targets:
        data = fetched.get(asset.url)
        if data is None:
            verdict, confidence, detail = (
                BackgroundVerdict.UNKNOWN, 0.0, "could not be downloaded in time"
            )
        else:
            verdict, confidence, detail = classify_pixels(data, cfg)
        out.append(BackgroundCheck(
            url=asset.url,
            view=asset.view,
            processing=asset.processing,
            verdict=verdict,
            basis="pixels",
            confidence=round(confidence, 3),
            detail=detail,
        ))
    return out
