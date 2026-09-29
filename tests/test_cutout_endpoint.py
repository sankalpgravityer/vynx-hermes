"""POST/GET /v1/imagery/cutout — image in, transparent PNG out, at the photo's resolution."""
from __future__ import annotations

import base64
import io

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.imaging import cutout
from app.main import app


def jpeg(size=(600, 400), exif_orientation: int | None = None) -> bytes:
    img = Image.new("RGB", size, (200, 200, 200))
    img.paste((180, 40, 40), (200, 100, 400, 300))
    buf = io.BytesIO()
    if exif_orientation:
        exif = img.getexif()
        exif[0x0112] = exif_orientation
        img.save(buf, "JPEG", exif=exif)
    else:
        img.save(buf, "JPEG")
    return buf.getvalue()


def png_of(size) -> bytes:
    img = Image.new("RGBA", size, (255, 255, 255, 0))
    img.paste((180, 40, 40, 255), (size[0] // 3, size[1] // 4, 2 * size[0] // 3, 3 * size[1] // 4))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def client(monkeypatch, tmp_path):
    model = tmp_path / "cloth_seg_ft_v2.onnx"
    model.write_bytes(b"not really a model")
    monkeypatch.setattr(cutout, "_CLOTH_FT_PATH", str(model))
    monkeypatch.delenv("HERMES_CUTOUT_API_KEY", raising=False)
    return TestClient(app)


def body(data: bytes) -> dict:
    return {"image_base64": base64.b64encode(data).decode()}


def test_returns_a_png_at_the_photos_resolution(client, monkeypatch):
    seen = {}

    def fake(raw, timeout_s=180.0, strategies=None, **kw):
        seen["strategies"] = strategies
        return png_of((600, 400)), None, "cloth-seg-ft"
    monkeypatch.setattr(cutout, "remove_background", fake)
    r = client.post("/v1/imagery/cutout", json=body(jpeg()))
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert Image.open(io.BytesIO(r.content)).size == (600, 400)
    assert r.headers["X-Cutout-Provider"] == "cloth-seg-ft"
    assert seen["strategies"] == cutout.config()["url_strategies"]


def test_a_differently_sized_result_is_brought_back_to_the_photos_size(client, monkeypatch):
    """A paid paint strategy can render at its own size; the caller still gets the photo's."""
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: (png_of((1024, 683)), None, "gemini-paint"))
    r = client.post("/v1/imagery/cutout", json=body(jpeg()))
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (600, 400) and img.mode == "RGBA"
    assert img.getpixel((0, 0))[3] == 0, "the background stays transparent"


def test_the_resolution_is_the_upright_photos(client, monkeypatch):
    """A decision original: stored landscape, EXIF says rotate — the cut-out is portrait."""
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: (png_of((400, 600)), None, "cloth-seg-ft"))
    r = client.post("/v1/imagery/cutout", json=body(jpeg((600, 400), exif_orientation=6)))
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == (400, 600)


def test_no_acceptable_cut_out_is_a_422_with_the_reasons(client, monkeypatch):
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: (None, "cloth-seg-ft#1: no garment found", "none"))
    r = client.post("/v1/imagery/cutout", json=body(jpeg()))
    assert r.status_code == 422
    assert r.json()["ok"] is False and "no garment" in r.json()["error"]


def test_refuses_to_run_without_the_fine_tuned_model(client, monkeypatch):
    """Otherwise every photo would go straight to the paid strategy."""
    monkeypatch.setattr(cutout, "_CLOTH_FT_PATH", "")
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: pytest.fail("must not run the chain"))
    r = client.post("/v1/imagery/cutout", json=body(jpeg()))
    assert r.status_code == 503


def test_the_optional_api_key(client, monkeypatch):
    monkeypatch.setenv("HERMES_CUTOUT_API_KEY", "s3cret")
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: (png_of((600, 400)), None, "cloth-seg-ft"))
    assert client.post("/v1/imagery/cutout", json=body(jpeg())).status_code == 401
    ok = client.post("/v1/imagery/cutout", json=body(jpeg()), headers={"X-Api-Key": "s3cret"})
    assert ok.status_code == 200


def test_bad_input(client):
    assert client.post("/v1/imagery/cutout", json={}).status_code == 400
    assert client.post("/v1/imagery/cutout", json={"image_base64": "!!!"}).status_code == 400
    assert client.post("/v1/imagery/cutout", json=body(b"not an image")).status_code == 400


def test_get_with_a_url(client, monkeypatch):
    import app.main as main
    monkeypatch.setattr(main, "_fetch_image", lambda url: jpeg())
    monkeypatch.setattr(cutout, "remove_background",
                        lambda raw, **kw: (png_of((600, 400)), None, "cloth-seg-ft-backup"))
    r = client.get("/v1/imagery/cutout", params={"image_url": "https://example.com/a.jpg"})
    assert r.status_code == 200 and r.headers["X-Cutout-Provider"] == "cloth-seg-ft-backup"
