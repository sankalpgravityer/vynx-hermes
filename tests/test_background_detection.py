"""The pixel-level background check.

Synthesised images rather than fixtures on disk, so the thresholds in
`policy.yaml` are tested against inputs whose properties are known exactly: a
cut-out really has a transparent border, a sweep really is flat, and the
"photograph" really does vary. A checked-in JPEG would test whatever that
particular file happens to be.

The case worth caring about is the LAST one — an image whose row claims
BG_REMOVED while the picture still shows a room. That is the only thing this
layer can find that metadata cannot, and the reason it exists.
"""

from __future__ import annotations

import io
import random

import pytest
from PIL import Image, ImageDraw

from app.config import policy
from app.imaging.background import check_media, classify_pixels, is_ambiguous
from app.models import BackgroundCheck, BackgroundVerdict, MediaAsset


@pytest.fixture(scope="module")
def cfg() -> dict:
    return policy()["imagery"]["pixels"]


def encode(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def cut_out() -> Image.Image:
    """A real matted PNG: transparent everywhere but the garment."""
    img = Image.new("RGBA", (600, 800), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((120, 150, 480, 650), fill=(30, 30, 40, 255))
    return img


def studio_sweep() -> Image.Image:
    """Matted and composited onto a flat backdrop — what a tenant with
    `autoApplyBackground` and `background: '#ffffff'` actually stores."""
    img = Image.new("RGB", (600, 800), (255, 255, 255))
    ImageDraw.Draw(img).ellipse((120, 150, 480, 650), fill=(180, 40, 40))
    return img


def photographed(seed: int = 1) -> Image.Image:
    """A garment shot in a room: a lighting gradient plus surface noise."""
    rng = random.Random(seed)
    img = Image.new("RGB", (600, 800))
    px = img.load()
    for y in range(800):
        for x in range(600):
            base = 90 + int(70 * (x / 600)) + rng.randint(-45, 45)
            px[x, y] = (
                max(0, min(255, base)),
                max(0, min(255, base - 18)),
                max(0, min(255, base - 40)),
            )
    ImageDraw.Draw(img).ellipse((120, 150, 480, 650), fill=(230, 230, 235))
    return img


# --------------------------------------------------------------------------- #

def test_transparent_cut_out(cfg):
    verdict, confidence, _ = classify_pixels(encode(cut_out()), cfg)
    assert verdict is BackgroundVerdict.TRANSPARENT
    assert confidence > 0.9


def test_flat_backdrop_is_not_a_defect(cfg):
    """A composited cut-out is FINISHED, not unprocessed. Calling this a scene
    would flag every product on a tenant that applies its own backdrop."""
    verdict, _, _ = classify_pixels(encode(studio_sweep(), "JPEG"), cfg)
    assert verdict is BackgroundVerdict.BACKDROP


def test_real_background_is_detected(cfg):
    verdict, confidence, _ = classify_pixels(encode(photographed(), "JPEG"), cfg)
    assert verdict is BackgroundVerdict.SCENE
    assert confidence >= 0.5


def test_patterned_garment_on_a_sweep_is_still_a_backdrop(cfg):
    """The reason the ring is masked: measured whole-image, a busy garment has a
    high stddev and would read as a room."""
    img = studio_sweep()
    draw = ImageDraw.Draw(img)
    for i in range(0, 500, 12):
        draw.line((120, 150 + i, 480, 200 + i), fill=(20, 20, 20), width=5)
    verdict, _, _ = classify_pixels(encode(img, "JPEG"), cfg)
    assert verdict is BackgroundVerdict.BACKDROP


def test_alpha_present_but_never_matted_falls_through_to_colour(cfg):
    """A PNG saved with an alpha channel is not evidence of a cut-out. Trusting
    the mode instead of the pixels is how a fail-soft provider goes unnoticed."""
    img = Image.new("RGBA", (600, 800), (140, 120, 100, 255))
    ImageDraw.Draw(img).ellipse((120, 150, 480, 650), fill=(30, 30, 40, 255))
    verdict, _, _ = classify_pixels(encode(img), cfg)
    assert verdict is not BackgroundVerdict.TRANSPARENT


def test_undecodable_bytes_are_unknown_not_an_exception(cfg):
    verdict, confidence, detail = classify_pixels(b"not an image at all", cfg)
    assert verdict is BackgroundVerdict.UNKNOWN
    assert confidence == 0.0 and "decode" in detail


def test_the_case_metadata_cannot_find(cfg):
    """The whole point: the row says the segmenter ran, the picture disagrees.
    Every bg-removal provider path retries a 429 forever but accepts whatever a
    200 returns, so a soft failure leaves exactly this."""
    verdict, confidence, _ = classify_pixels(encode(photographed(), "JPEG"), cfg)
    claimed = BackgroundCheck(
        url="u", view="FRONT", processing="BG_REMOVED",
        verdict=verdict, basis="pixels", confidence=confidence,
    )
    # This combination is what the endpoint turns into IMG.013.
    assert claimed.processing != "RAW"
    assert claimed.verdict is BackgroundVerdict.SCENE
    assert claimed.confidence >= 0.5


def test_only_the_uncertain_middle_escalates_to_vision(cfg):
    """A vision call per image would put a model behind every button press."""
    imagery = policy()["imagery"]
    confident = BackgroundCheck(url="u", view="FRONT", processing="RAW",
                                verdict=BackgroundVerdict.SCENE, confidence=0.9)
    unsure = BackgroundCheck(url="u", view="FRONT", processing="RAW",
                             verdict=BackgroundVerdict.SCENE, confidence=0.35)
    clean = BackgroundCheck(url="u", view="FRONT", processing="BG_REMOVED",
                            verdict=BackgroundVerdict.TRANSPARENT, confidence=1.0)
    assert is_ambiguous(unsure, imagery)
    assert not is_ambiguous(confident, imagery)
    assert not is_ambiguous(clean, imagery)


def test_a_dead_url_yields_unknown_rather_than_raising(cfg):
    """One rate-limited R2 object must not cost the operator the whole check."""
    checks = check_media(
        [MediaAsset(url="https://127.0.0.1:9/nope.jpg", view="FRONT")],
        {**cfg, "fetch_timeout_s": 1, "fetch_deadline_s": 3},
    )
    assert len(checks) == 1
    assert checks[0].verdict is BackgroundVerdict.UNKNOWN


def test_fetches_are_capped(cfg):
    assets = [MediaAsset(url=f"https://127.0.0.1:9/{i}.jpg", view="FRONT")
              for i in range(20)]
    checks = check_media(
        assets, {**cfg, "max_images_fetched": 3, "fetch_timeout_s": 1,
                 "fetch_deadline_s": 3},
    )
    assert len(checks) == 3
