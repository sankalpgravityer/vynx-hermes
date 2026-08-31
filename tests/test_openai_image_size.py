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


# ---------------------------------------------------------------------------
# The billed tier.
#
# gpt-image prices per OUTPUT TOKEN, and the token count is decided by size and
# quality alone. At 1024x1536 that is roughly 400 tokens at `low`, 1.6k at
# `medium` and 6.2k at `high` — a 15x spread for the same view. Omitting the
# parameter is not neutral: it means "auto", which resolves towards the top.
#
# So the tier has to be on the wire, and it has to come from policy. These
# tests exist because the omission was invisible — the renders looked correct
# and the bill arrived a day later.
# ---------------------------------------------------------------------------

import base64

import httpx

from app.imaging import openai_image


def _capture(monkeypatch, body: dict | None = None):
    """Stub the transport, return the form fields the request carried."""
    sent: dict[str, str] = {}
    payload = body or {
        "data": [{"b64_json": base64.b64encode(png(1024, 1536)).decode()}],
        "usage": {"output_tokens": 1584},
    }

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, files=None, data=None):
            sent.update(data or {})
            return httpx.Response(200, json=payload)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai_image.httpx, "Client", FakeClient)
    return sent


def test_the_quality_tier_reaches_the_wire(monkeypatch):
    sent = _capture(monkeypatch)
    data, err = openai_image.generate("p", [b"src"], quality="low")
    assert err is None and data
    assert sent["quality"] == "low"


def test_quality_is_never_omitted_even_when_the_caller_says_nothing(monkeypatch):
    """The default has to be OURS, not the vendor's — the vendor's is the
    expensive one, and it is applied silently."""
    sent = _capture(monkeypatch)
    monkeypatch.delenv("OPENAI_IMAGE_QUALITY", raising=False)
    openai_image.generate("p", [b"src"])
    assert sent["quality"] == "medium"


def test_an_explicit_tier_beats_the_environment(monkeypatch):
    sent = _capture(monkeypatch)
    monkeypatch.setenv("OPENAI_IMAGE_QUALITY", "high")
    openai_image.generate("p", [b"src"], quality="low")
    assert sent["quality"] == "low"


def test_the_environment_is_used_when_policy_carries_no_tier(monkeypatch):
    """`self.cfg.get("openai_quality")` is None on a policy that predates the
    setting, and that must not silently become the vendor default."""
    sent = _capture(monkeypatch)
    monkeypatch.setenv("OPENAI_IMAGE_QUALITY", "high")
    openai_image.generate("p", [b"src"], quality=None)
    assert sent["quality"] == "high"
