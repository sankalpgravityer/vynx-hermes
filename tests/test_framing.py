"""Crop and centre (app/imaging/framing.py), and the checks that must accept it.

30 Sep 2026: every cut-out is framed alike — the garment scaled by one factor
to fill 90% of its limiting axis and centred on the photograph's canvas. The
checks that compare a cut-out with its photograph register the two first, so a
framed cut-out is not called a zoom, while a re-drawn garment still is.
"""
from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import cutouts, framing  # noqa: E402
from app.models import ImagerySettings, MediaAsset, ProductSnapshot  # noqa: E402

FCFG = framing.config({})
CFG = {**cutouts.config({}), "framing": FCFG}
BACKDROP = (180, 176, 174)


def png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def texture(w: int, h: int, seed: int) -> np.ndarray:
    """A garment with something to correlate: smooth blobs, not white noise."""
    rng = np.random.default_rng(seed)
    small = rng.integers(30, 220, size=(max(2, h // 12), max(2, w // 12), 3)).astype(np.uint8)
    return np.asarray(Image.fromarray(small).resize((w, h), Image.Resampling.BICUBIC))


def photo(box=(40, 50, 160, 230), size=(300, 400), seed=1) -> tuple[Image.Image, np.ndarray]:
    """A photograph and its garment mask: a textured garment on a wall."""
    W, H = size
    arr = np.zeros((H, W, 3), np.uint8)
    arr[:] = BACKDROP
    x0, y0, x1, y1 = box
    arr[y0:y1, x0:x1] = texture(x1 - x0, y1 - y0, seed)
    mask = np.zeros((H, W), bool)
    mask[y0:y1, x0:x1] = True
    return Image.fromarray(arr), mask


def cutout_of(raw: Image.Image, mask: np.ndarray) -> bytes:
    rgba = np.dstack([np.asarray(raw), np.where(mask, 255, 0).astype(np.uint8)])
    rgba[~mask] = (255, 255, 255, 0)
    return png(Image.fromarray(rgba, "RGBA"))


def on_white(cut: bytes) -> bytes:
    im = Image.open(io.BytesIO(cut)).convert("RGBA")
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    return png(bg.convert("RGB"))


def alpha_box(data: bytes):
    a = np.asarray(Image.open(io.BytesIO(data)).convert("RGBA").getchannel("A")) >= 128
    ys, xs = np.where(a.any(axis=1))[0], np.where(a.any(axis=0))[0]
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


# --------------------------------------------------------------------------- #
# framing itself
# --------------------------------------------------------------------------- #

def test_the_garment_is_scaled_to_the_standard_and_centred_on_the_same_canvas():
    raw, mask = photo(box=(20, 30, 110, 210))          # 90x180, high and to the left
    out, info = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    im = Image.open(io.BytesIO(out))
    assert im.size == (300, 400) and info["framed"]
    x0, y0, x1, y1 = alpha_box(out)
    # The height decides: 0.90 x 400 = 360 from 180, so 2x.
    assert abs((y1 - y0) - 360) <= 2
    assert abs((x0 + x1) / 2 - 150) <= 1 and abs((y0 + y1) / 2 - 200) <= 1
    assert info["scale"] == pytest.approx(2.0, rel=0.01)


def test_nothing_is_redrawn_the_colours_are_the_photographs():
    raw, mask = photo()
    out, _ = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    arr = np.asarray(Image.open(io.BytesIO(out)).convert("RGBA"))
    kept = arr[arr[:, :, 3] == 255][:, :3].mean(axis=0)
    src = np.asarray(raw)[mask].mean(axis=0)
    assert np.abs(kept - src).max() < 3


def test_a_loose_speck_does_not_move_the_box():
    raw, mask = photo(box=(100, 100, 200, 300))
    mask = mask.copy()
    mask[5:8, 290:293] = True                          # a speck in the corner
    out, info = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    assert info["garment_box"][2] < 210 and info["garment_box"][1] >= 90


def test_a_tiny_garment_stops_at_max_scale_and_is_still_centred():
    raw, mask = photo(box=(10, 10, 40, 60))            # 30x50
    out, info = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    assert info["capped"] and info["scale"] == pytest.approx(FCFG["max_scale"])
    x0, y0, x1, y1 = alpha_box(out)
    assert abs((x0 + x1) / 2 - 150) <= 1 and abs((y0 + y1) / 2 - 200) <= 1


def test_an_opaque_photograph_with_no_flat_backdrop_is_returned_untouched():
    raw, _ = photo()
    arr = np.asarray(raw).copy()
    arr[:40, :40] = (40, 40, 40)                       # a corner that is not the wall
    data = png(Image.fromarray(arr).convert("RGBA"))
    out, info = framing.frame_cutout(data, FCFG)
    assert out == data and info["framed"] is False and "flat backdrop" in info["note"]


# --------------------------------------------------------------------------- #
# the cut-out already on file (opaque, on the tenant's backdrop)
# --------------------------------------------------------------------------- #

def stored(box=(20, 30, 110, 210), size=(300, 400), backdrop=(255, 255, 255)):
    """A stored cut-out: opaque, garment flattened onto a flat backdrop."""
    raw, mask = photo(box=box, size=size)
    arr = np.asarray(raw).copy()
    arr[~mask] = backdrop
    return png(Image.fromarray(arr)), arr[mask].mean(axis=0)


def test_an_existing_opaque_cut_out_is_cropped_and_centred_on_its_backdrop():
    data, colour = stored(backdrop=(235, 235, 235))
    out, info = framing.frame_cutout(data, FCFG)
    assert info["framed"] and info["source"] == "backdrop"
    arr = np.asarray(Image.open(io.BytesIO(out)).convert("RGB")).astype(int)
    assert tuple(arr[0, 0]) == (235, 235, 235)          # the canvas is the backdrop
    garment = np.abs(arr - 235).sum(axis=2) > 24
    ys, xs = np.where(garment.any(axis=1))[0], np.where(garment.any(axis=0))[0]
    assert abs((ys.max() - ys.min() + 1) - 360) <= 6      # 90% of 400, give or take resize overshoot
    assert abs((xs.min() + xs.max()) / 2 - 150) <= 2 and abs((ys.min() + ys.max()) / 2 - 200) <= 2
    assert np.abs(arr[garment].mean(axis=0) - colour).max() < 4   # nothing re-drawn


def test_an_existing_cut_out_on_a_smaller_canvas_is_framed_onto_the_photographs():
    """896x1195 on file for a 3000x4000 photograph: framed at the photograph's size."""
    data, _ = stored(size=(150, 200), box=(10, 15, 55, 105))
    out, info = framing.frame_cutout(data, FCFG, canvas=(300, 400))
    assert info["framed"] and Image.open(io.BytesIO(out)).size == (300, 400)


def test_a_garment_whose_edge_is_not_clear_is_not_cropped_at_all():
    """A white sleeve on white: only its seam separates from the backdrop, a
    speck too small to widen the box, just outside it. A crop by colour would
    slice the sleeve off — so the picture is left exactly as it is."""
    arr = np.full((400, 300, 3), 255, np.uint8)
    arr[100:300, 100:200] = (60, 60, 90)               # the body: 20,000 px
    arr[150:180, 203:206] = (90, 90, 90)               # the sleeve's seam: 90 px, under the 0.5% speck floor
    data = png(Image.fromarray(arr))
    out, info = framing.frame_cutout(data, FCFG)
    assert out == data and info["framed"] is False and "not clear" in info["note"]


def test_the_endpoint_frames_the_existing_cut_out_when_no_method_worked(monkeypatch):
    """All four methods failed: the one on file comes back cropped and centred,
    at the photograph's size, as a replacement for itself."""
    import base64

    from fastapi.testclient import TestClient

    from app import main
    from app.imaging import cutout

    monkeypatch.setattr(cutout, "remove_background",
                        lambda data, timeout_s=180.0, **kw: (None, "v2: torn; v1: torn", "none"))
    raw, _ = photo(size=(300, 400))
    rb = io.BytesIO(); raw.save(rb, "JPEG", quality=92)
    prev, _c = stored(size=(150, 200), box=(10, 15, 55, 105))
    resp = TestClient(main.app).post("/v1/imagery/remove-background", json={
        "image_base64": base64.b64encode(rb.getvalue()).decode(),
        "previous_base64": base64.b64encode(prev).decode()})
    p = resp.json()
    assert p["ok"] is True and p["provider"] == main.EXISTING_FRAMED and p["kept_existing"] is False
    assert p["framing"]["framed"] and "torn" in p["error"]
    assert Image.open(io.BytesIO(base64.b64decode(p["image_base64"]))).size == (300, 400)


def test_a_paid_render_is_brought_to_the_photographs_size_before_framing():
    from app import main

    raw, mask = photo(size=(300, 400))
    small = Image.open(io.BytesIO(cutout_of(raw, mask))).resize((150, 200))
    out = main._at_size(png(small), (300, 400))
    assert Image.open(io.BytesIO(out)).size == (300, 400)
    assert main._at_size(b"not an image", (300, 400)) == b"not an image"


def test_framing_twice_changes_nothing():
    raw, mask = photo()
    once, _ = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    twice, info = framing.frame_cutout(once, FCFG)
    assert twice == once and info.get("note") == "already framed"


def test_the_policy_switch_and_the_request_switch():
    assert framing.config({})["enabled"] is True
    assert framing.config({"imagery": {"cutout": {"framing": False}}})["enabled"] is False
    assert framing.config(policy())["enabled"] is True       # what ships

    from app import main

    raw, mask = photo()
    cut = cutout_of(raw, mask)
    assert main._framed(cut, False) == (cut, None)
    out, info = main._framed(cut, None)
    assert info and info["framed"] and out != cut


# --------------------------------------------------------------------------- #
# the checks against a framed cut-out
# --------------------------------------------------------------------------- #

def framed_pair(seed=1, box=(40, 50, 160, 230)):
    raw, mask = photo(box=box, seed=seed)
    out, info = framing.frame_cutout(cutout_of(raw, mask), FCFG)
    rb = io.BytesIO()
    raw.save(rb, "JPEG", quality=95)
    return on_white(out), rb.getvalue(), info


def test_a_framed_cut_out_is_registered_to_its_photograph_and_matches():
    cut, raw, info = framed_pair()
    m = cutouts.garment_hole(cut, raw, CFG)
    assert m["registered"]["scale"] == pytest.approx(info["scale"], rel=0.03)
    assert m["pixel_match"] > 0.9
    assert cutouts.frame_problem(m, CFG, derived=True) == (None, False)


def test_without_framing_the_same_cut_out_reads_as_a_zoom():
    """Registration is what framing buys, not a general excuse for a zoom."""
    cut, raw, _ = framed_pair()
    off = {**CFG, "framing": {**FCFG, "enabled": False}}
    m = cutouts.garment_hole(cut, raw, off)
    assert m["registered"] is None and m["pixel_match"] < 0.5
    why, fixable = cutouts.frame_problem(m, off, derived=True)
    assert why and "does not line up" in why and fixable


def test_a_re_drawn_garment_is_still_caught_after_registration():
    cut, _raw, _ = framed_pair(seed=1)
    _c, other, _ = framed_pair(seed=7)                  # same shape, different pixels
    m = cutouts.garment_hole(cut, other, CFG)
    why, fixable = cutouts.frame_problem(m, CFG, derived=True)
    assert why and fixable and ("re-drawn" in why or "does not line up" in why)


def test_the_standard_passes_a_framed_cut_out_and_flags_an_unframed_one():
    cut, raw, _ = framed_pair()
    m = cutouts.garment_hole(cut, raw, CFG)
    box = cutouts.measure(cut, CFG, (255, 255, 255))["box"]
    assert cutouts.standard_problem(box, m, FCFG) is None

    rawi, mask = photo()
    plain = on_white(cutout_of(rawi, mask))
    rb = io.BytesIO(); rawi.save(rb, "JPEG", quality=95)
    m2 = cutouts.garment_hole(plain, rb.getvalue(), CFG)
    box2 = cutouts.measure(plain, CFG, (255, 255, 255))["box"]
    why = cutouts.standard_problem(box2, m2, FCFG)
    assert why and why.startswith("framing: the garment") and "Re-cut it and frame it" in why


def test_a_garment_at_max_scale_is_not_re_framed_forever():
    cut, raw, info = framed_pair(box=(10, 10, 40, 60))
    assert info["capped"]
    m = cutouts.garment_hole(cut, raw, CFG)
    box = cutouts.measure(cut, CFG, (255, 255, 255))["box"]
    assert cutouts.standard_problem(box, m, FCFG) is None


def test_the_re_matte_router_reads_the_framing_sentence_as_the_default_chain():
    """No 'stand'/'hanger'/'missing' in it: it must not route to a paid mask."""
    from app.imaging import cutout

    rawi, mask = photo()
    plain = on_white(cutout_of(rawi, mask))
    box = cutouts.measure(plain, CFG, (255, 255, 255))["box"]
    why = cutouts.standard_problem(box, {"pixel_match": 0.99}, FCFG)
    assert why and cutout.rematte_strategies(why) is None


# --------------------------------------------------------------------------- #
# the verdict, end to end
# --------------------------------------------------------------------------- #

def asset(view, processing, **kw):
    return MediaAsset(
        id=kw.get("id"), url=kw.get("url", f"https://r2/{view.lower()}.png"),
        view=view, processing=processing,
        is_current=kw.get("current", True), derived_from_id=kw.get("derived"),
        width=kw.get("width"), height=kw.get("height"))


def snap(assets):
    return ProductSnapshot(id="p1", title="Vintage Zara Black Jacket Men M",
                           master_category="Men", category="Jackets",
                           media=assets, imagery_settings=ImagerySettings())


def fake_io(blobs):
    def fetch(urls, timeout_s=0, deadline_s=0):
        return {u: blobs.get(u) for u in urls}

    def read_dims(urls, timeout_s=0, deadline_s=0):
        return {}
    return fetch, read_dims


def _judge(cut: bytes, raw: bytes):
    pol = copy.deepcopy(policy())
    r = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg", width=300, height=400)
    c = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    fetch, read_dims = fake_io({"https://r2/cut.png": cut, "https://r2/raw.jpg": raw})
    return cutouts.judge(snap([c, r]), pol, fetch=fetch, read_dims=read_dims)


def test_judge_passes_a_framed_cut_out():
    cut, raw, _ = framed_pair()
    v = _judge(cut, raw)
    assert v.action == "ok", v.reasons


def test_judge_re_cuts_an_honest_cut_out_that_is_not_framed_yet():
    rawi, mask = photo()
    rb = io.BytesIO(); rawi.save(rb, "JPEG", quality=95)
    v = _judge(on_white(cutout_of(rawi, mask)), rb.getvalue())
    assert v.action == "bad" and v.bad_views == ["FRONT"]
    assert any("not cropped and centred" in r for r in v.reasons)
