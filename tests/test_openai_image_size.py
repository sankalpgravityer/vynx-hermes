"""gpt-image renders must come out the same SHAPE as Gemini's.

Gemini takes an `aspect_ratio` and returns that shape. gpt-image offers three
fixed canvases, so the fallback produced 1024x1536 (2:3) for a tenant whose
Gemini renders are 3:4 — two shapes in one gallery, and the product page crops
the taller one, cutting off the bottom-right corner where the vnyx.ai watermark
sits. The missing watermark was the symptom; the mismatched gallery was the bug.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.imaging.openai_image import conform_to_ratio, resolve_size


def png(width: int, height: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 60, 60)).save(buf, format="PNG")
    return buf.getvalue()


def size_of(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def test_the_boas_case_portrait_canvas_conformed_to_gemini_shape():
    """BOAS sets 5:7, which resolves to 3:4 for Gemini. gpt-image's nearest
    canvas is 1024x1536 (2:3) — visibly taller. It must be cropped to 3:4."""
    out = conform_to_ratio(png(1024, 1536), "3:4")
    width, height = size_of(out)
    assert (width, height) == (1024, 1365)
    assert width / height == pytest.approx(0.75, abs=0.001)


def test_a_render_already_at_the_target_is_untouched():
    original = png(1024, 1536)
    assert conform_to_ratio(original, "2:3") == original


def test_a_too_wide_render_is_trimmed_at_the_sides():
    out = conform_to_ratio(png(1536, 1024), "16:9")
    assert size_of(out) == (1536, 864)


def test_a_square_target_crops_the_long_side():
    assert size_of(conform_to_ratio(png(1024, 1536), "1:1")) == (1024, 1024)


@pytest.mark.parametrize("ratio", [None, "auto", "", "not-a-ratio", "0:5", "3:0"])
def test_no_usable_ratio_returns_the_render_untouched(ratio):
    """A wrongly-shaped render still beats no render — this runs last in an
    escalation chain, and must never be the thing that loses the image."""
    original = png(800, 600)
    assert conform_to_ratio(original, ratio) == original


def test_unreadable_bytes_are_returned_rather_than_raising():
    junk = b"not an image at all"
    assert conform_to_ratio(junk, "3:4") == junk


def test_canvas_choice_still_picks_the_nearest_supported_shape():
    assert resolve_size("3:4") == "1024x1536"
    assert resolve_size("5:7") == "1024x1536"
    assert resolve_size("16:9") == "1536x1024"
    assert resolve_size("1:1") == "1024x1024"
    assert resolve_size(None) == "1024x1536"


def test_auto_tenant_conforms_to_geminis_natural_portrait():
    """THE REPORTED BUG. The tenant's aspectRatio is 'auto', so Gemini renders
    unconstrained at its own 1728x2432 (27:38) while gpt-image lands on its
    fixed 1024x1536 (2:3) — visibly taller and narrower. The product page crops
    to fit and takes the bottom with it: the model's feet, and the vnyx.ai
    watermark in the corner.

    Policy carries the measured Gemini default so the fallback conforms to the
    primary.
    """
    from app.config import policy

    cfg = (policy().get("imagery") or {}).get("generation") or {}
    auto = cfg.get("auto_portrait_ratio")
    assert auto, "policy must carry the Gemini auto portrait ratio"

    out = conform_to_ratio(png(1024, 1536), auto)
    width, height = size_of(out)
    # Gemini's measured unconstrained 2K portrait.
    assert width / height == pytest.approx(1728 / 2432, abs=0.002)
    assert (width, height) == (1024, 1441)


def test_gemini_is_not_constrained_to_the_auto_ratio():
    """The fallback conforms to the primary, never the reverse. Constraining
    Gemini would change the shape of every future render and mismatch them
    against the existing catalog."""
    from app.imaging.nanobanana import resolve_aspect_ratio

    supported = ["1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9"]
    assert resolve_aspect_ratio("auto", supported) is None
    assert resolve_aspect_ratio(None, supported) is None
