"""The cut-out keeps the photograph's quality (3 Oct 2026): its colour profile, its
pixels (framing never enlarges — tests/test_framing.py), and sharp edges.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout, framing  # noqa: E402
from app.imaging import hanger_cutout as hc  # noqa: E402

P3 = ImageCms.ImageCmsProfile(ImageCms.createProfile("LAB")).tobytes()   # any real profile will do


def jpeg_with_icc(icc: bytes | None = P3) -> bytes:
    arr = np.full((400, 300, 3), 200, np.uint8)
    arr[100:300, 80:220] = (40, 90, 160)
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=95, **({"icc_profile": icc} if icc else {}))
    return b.getvalue()


def cut_png() -> bytes:
    rgba = np.zeros((400, 300, 4), np.uint8)
    rgba[100:300, 80:220] = (40, 90, 160, 255)
    b = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(b, "PNG")
    return b.getvalue()


# --- the colour profile ------------------------------------------------------------

def test_the_profile_is_added_without_touching_the_pixels():
    png = cut_png()
    out = cutout._png_with_icc(png, P3)
    im = Image.open(io.BytesIO(out))
    assert im.info.get("icc_profile") == P3
    assert np.array_equal(np.asarray(im), np.asarray(Image.open(io.BytesIO(png))))


def test_the_profile_is_added_once_and_only_to_a_png():
    once = cutout._png_with_icc(cut_png(), P3)
    assert cutout._png_with_icc(once, P3) == once
    assert cutout._png_with_icc(b"not a png", P3) == b"not a png"
    assert cutout._png_with_icc(cut_png(), None) == cut_png()


def test_the_chain_carries_the_photos_profile_on_its_own_pixels_only(monkeypatch):
    src = jpeg_with_icc()
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (cut_png(), None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))
    out, err, provider = cutout.remove_background(src, strategies=["cloth-seg-ft"])
    assert provider == "cloth-seg-ft"
    assert Image.open(io.BytesIO(out)).info.get("icc_profile") == P3

    # A painted answer is the model's picture, in the model's colours: no profile added.
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (None, "x"))
    monkeypatch.setattr(cutout, "_gemini", lambda data, t, prompt=None: (cut_png(), None))
    monkeypatch.setattr(cutout, "_same_framing", lambda s, o: (True, "same"))
    monkeypatch.setattr(cutout, "_paint_fidelity", lambda s, o: (1.0, 1.0))
    out, err, provider = cutout.remove_background(src, strategies=["cloth-seg-ft", "gemini-paint"])
    assert provider == "gemini-paint"
    assert Image.open(io.BytesIO(out)).info.get("icc_profile") is None


def test_framing_keeps_the_profile():
    out, info = framing.frame_cutout(cutout._png_with_icc(cut_png(), P3), framing.config({}))
    assert info["framed"] and Image.open(io.BytesIO(out)).info.get("icc_profile") == P3


# --- sharp edges -------------------------------------------------------------------

def test_edge_refinement_is_off_in_hermes():
    """The script's guided-filter refinement WIDENED real IS-Net edges (BLM-001006
    FRONT: 2.9 -> 4.3 soft px per outline px) and, without ximgproc, never ran where
    the reference outputs were made. Off unless policy turns it on."""
    assert cutout.hanger_config()["refine_edges"] is False


def test_the_box_filter_guided_filter_is_the_identity_on_a_flat_alpha():
    g = np.random.default_rng(0).random((64, 64)).astype(np.float32)
    ones = np.ones((64, 64), np.float32)
    assert np.allclose(hc._guided_filter(g, ones, 4, 1e-3), 1.0, atol=1e-4)
