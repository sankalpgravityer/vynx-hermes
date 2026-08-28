"""On-model generation via OpenAI's image API.

THE LAST STEP OF THE FALLBACK CHAIN, and the one that rescues a real slice of a
second-hand catalog.

Gemini blocks a garment whose print carries an identifiable real person. Not a
photograph of one — a NAME is enough: `RODGERS 12` on a Packers jersey and
`DEROZAN 12` on a Raptors one are both refused, by every Gemini image model
tried, on the request rather than the response (`prompt_feedback.block_reason`
with zero candidates, returned in under four seconds where a real render takes
twenty-five). No prompt can move that — the classifier reads the request,
prompt included, before the generation model sees any of it.

`gpt-image-1` renders the same garment correctly. That is the whole reason this
module exists: a different vendor, a different policy, for a lawful use — the
shop owns the garment and is selling it. It is NOT an attempt to get around the
first vendor's decision, and nothing here is phrased to.

Wired last because it is the most expensive step and Gemini handles the
overwhelming majority; it is only reached once every configured Gemini model has
produced nothing.
"""

from __future__ import annotations

import base64
import io
import logging
import os
from typing import Any

import httpx

from app.net import prefer_ipv4

log = logging.getLogger("hermes.imaging.openai")

_ENDPOINT = "https://api.openai.com/v1/images/edits"

# gpt-image-1 accepts a fixed set of canvas sizes, not arbitrary ratios. The
# tenant's aspect ratio maps onto the nearest of them; BOAS's 5:7 is portrait, so
# it lands on 1024x1536.
_PORTRAIT, _LANDSCAPE, _SQUARE = "1024x1536", "1536x1024", "1024x1024"


def _target_ratio(aspect_ratio: str | None) -> float | None:
    """`w:h` as a number, or None when no shape was asked for."""
    if not aspect_ratio or aspect_ratio == "auto":
        return None
    try:
        width, height = (float(n) for n in aspect_ratio.split(":", 1))
    except (ValueError, AttributeError):
        return None
    if width <= 0 or height <= 0:
        return None
    return width / height


def conform_to_ratio(data: bytes, aspect_ratio: str | None) -> bytes:
    """Centre-crop a render to the tenant's aspect ratio.

    WHY THIS EXISTS. Gemini takes an `aspect_ratio` and returns that shape;
    gpt-image only offers three fixed canvases, so the same product came back
    1024x1536 (2:3) from the fallback where Gemini had produced 3:4. Two shapes
    in one gallery, and the taller one is cropped by the product page — which
    cuts off the bottom-right corner, where the vnyx.ai watermark is applied.
    A missing watermark was the visible symptom; a mismatched gallery was the
    actual defect.

    Cropped, not scaled: squashing a person to fit a ratio is worse than losing
    a little headroom, and the crop is centred so the garment — which the prompt
    puts in the middle of the frame — survives it.

    Best-effort. A crop that fails returns the original bytes; a wrongly-shaped
    render still beats no render, which is the whole point of this fallback.
    """
    target = _target_ratio(aspect_ratio)
    if target is None:
        return data
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
            if not width or not height:
                return data
            current = width / height
            # Already within a pixel-rounding of the target.
            if abs(current - target) < 0.01:
                return data

            if current > target:
                # Too wide — trim the sides.
                new_width = max(1, round(height * target))
                left = (width - new_width) // 2
                box = (left, 0, left + new_width, height)
            else:
                # Too tall — trim top and bottom.
                new_height = max(1, round(width / target))
                top = (height - new_height) // 2
                box = (0, top, width, top + new_height)

            out = io.BytesIO()
            img.crop(box).save(out, format="PNG")
            log.info(
                "conformed gpt-image render %dx%d -> %dx%d for aspect %s",
                width, height, box[2] - box[0], box[3] - box[1], aspect_ratio,
            )
            return out.getvalue()
    except Exception as exc:  # noqa: BLE001 — a wrong shape beats no image
        log.warning("could not conform render to %s: %s", aspect_ratio, exc)
        return data


def resolve_size(aspect_ratio: str | None) -> str:
    """Nearest supported canvas for a `w:h` ratio. Portrait is the default —
    a full-length on-model shot is taller than it is wide."""
    if not aspect_ratio or aspect_ratio == "auto":
        return _PORTRAIT
    try:
        width, height = (float(n) for n in aspect_ratio.split(":", 1))
    except (ValueError, AttributeError):
        return _PORTRAIT
    if height <= 0 or width <= 0:
        return _PORTRAIT
    ratio = width / height
    if ratio > 1.15:
        return _LANDSCAPE
    if ratio < 0.87:
        return _PORTRAIT
    return _SQUARE


def available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


def generate(
    prompt: str,
    images: list[bytes],
    aspect_ratio: str | None = None,
    timeout_s: float = 180.0,
) -> tuple[bytes | None, str | None]:
    """One render. Returns (png_bytes, error) — exactly one is set.

    Never raises: this is the last resort in an escalation chain, and a failure
    here has to read as "this view could not be produced", not as a crash that
    loses the views that already succeeded.
    """
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return None, "no OPENAI_API_KEY configured"
    if not images:
        return None, "no source image"

    model = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    size = resolve_size(aspect_ratio)

    # `image[]` for more than one reference, which is how the edits endpoint
    # takes a set; a single reference uses the scalar field.
    files: list[tuple[str, tuple[str, bytes, str]]] = (
        [("image", ("source.png", images[0], "image/png"))]
        if len(images) == 1
        else [
            (f"image[{i}]", (f"source-{i}.png", data, "image/png"))
            for i, data in enumerate(images)
        ]
    )

    transport = (
        httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
        if prefer_ipv4() else None
    )
    try:
        with httpx.Client(timeout=timeout_s, transport=transport) as client:
            resp = client.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {key}"},
                files=files,
                data={"model": model, "prompt": prompt, "size": size, "n": "1"},
            )
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"

    if resp.status_code != 200:
        # The body carries the reason, including this vendor's own content
        # decisions — surfaced rather than flattened to a status code, so a
        # refusal here is as diagnosable as a Gemini one.
        detail = resp.text[:300]
        try:
            detail = resp.json().get("error", {}).get("message", detail)
        except Exception:  # noqa: BLE001
            pass
        return None, f"openai {resp.status_code}: {detail}"

    try:
        payload: dict[str, Any] = resp.json()
        b64 = (payload.get("data") or [{}])[0].get("b64_json")
    except Exception as exc:  # noqa: BLE001
        return None, f"unreadable openai response ({type(exc).__name__}: {exc})"

    if not b64:
        return None, "openai returned no image"

    log.info("gpt-image rendered a view Gemini would not (%s, %s)", model, size)
    # Conform to the tenant's ratio before returning: the caller stores this
    # beside Gemini renders of the same product, and they have to be one shape.
    return conform_to_ratio(base64.b64decode(b64), aspect_ratio), None
