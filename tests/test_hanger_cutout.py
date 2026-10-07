"""Photos hung on the wall: the `hanger-isnet` strategy (app/imaging/hanger_cutout.py), 1 Oct 2026.

IS-Net keeps the whole garment, clips included; only the thin hanger parts are painted
out, and the merge cannot take cloth away. Photobooth and decision photos never come
here: their podium and stand are the fine-tuned cloth-seg's job.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout  # noqa: E402
from app.imaging import hanger_cutout as hc  # noqa: E402

WALL = (225, 223, 220)
DENIM = (50, 80, 140)


def hung(pencil: bool = True):
    """600x800: shorts clipped to a hanger bar that sits ON the waistband, a hook above,
    and (optionally) a pencil guide line drawn on the wall above the hanger."""
    rng = np.random.default_rng(2)
    a = np.empty((800, 600, 3), np.float32)
    a[:] = WALL
    garment = np.zeros((800, 600), bool)
    garment[200:600, 150:450] = True
    garment[480:600, 285:315] = False
    a[garment] = DENIM
    a += rng.normal(0, 2.5, a.shape)
    bar = np.zeros((800, 600), bool)
    bar[193:200, 100:500] = True                       # touching the waistband top (row 200)
    clips = np.zeros((800, 600), bool)
    for x0 in (180, 400):
        clips[165:215, x0:x0 + 24] = True              # wider than "thin": they stay
    hook = np.zeros((800, 600), bool)
    hook[120:193, 298:302] = True
    a[bar | hook] = (35, 35, 38)
    a[clips] = (150, 150, 155)
    if pencil:
        a[95:98, 40:560] = (120, 120, 125)              # BLM-001025: a guide line on the wall
    return np.clip(a, 0, 255).astype(np.uint8), garment, bar, clips, hook


class FakeSeg:
    """Stands in for IS-Net: the object is whatever is not the wall, except the faint
    pencil line, which IS-Net reads as wall."""

    def __call__(self, rgb, cfg, box=None):
        d = np.abs(rgb.astype(np.float32) - np.array(WALL, np.float32)).sum(axis=2)
        out = (d > 60).astype(np.float32)
        out[:110] = 0
        return out, box or (0, 0, rgb.shape[1], rgb.shape[0])


def run(raw):
    return hc.process(raw, FakeSeg(), hc.telea, hc.Config())


def test_the_bar_is_found_even_where_it_sits_on_the_waistband():
    raw, garment, bar, clips, hook = hung(pencil=False)
    rgba, rep, _ = run(raw)
    assert rep.bar_found
    a = rgba[..., 3] >= 128
    # the bar's ends, clear of the garment's corners (the row next to the waistband
    # may keep a pixel or two of the garment's own edge)
    assert not a[193:198, 100:140].any() and not a[193:198, 460:500].any(), "bar ends kept"
    assert not a[120:180, 298:302].any(), "hook kept"


def test_a_pencil_line_on_the_wall_is_not_taken_for_the_bar():
    """BLM-001025 FRONT: the topmost long dark line was a guide line on the wall; the real
    bar stayed in the cut-out."""
    raw, garment, bar, clips, hook = hung(pencil=True)
    rgba, rep, dbg = run(raw)
    assert rep.bar_found
    assert not dbg["mask"][95:98].any(), "the pencil line was masked as the bar"
    assert dbg["mask"][193:200, 100:150].any(), "the real bar was not masked"


def test_no_cloth_is_removed_and_the_clips_stay():
    raw, garment, bar, clips, hook = hung()
    rgba, rep, dbg = run(raw)
    a = rgba[..., 3] >= 128
    assert rep.garment_px_lost_outside_mask == 0
    assert (a & garment).sum() / garment.sum() > 0.99
    assert a[170:190, 185:200].mean() > 0.8 and a[170:190, 405:420].mean() > 0.8, "clips removed"


def test_painted_wall_inside_the_hanger_mask_is_dropped():
    """BOA-001263 BACK: next to a clip, IS-Net's 2nd pass kept freshly painted wall."""
    raw, garment, bar, clips, hook = hung(pencil=False)

    class Greedy(FakeSeg):
        def __call__(self, rgb, cfg, box=None):
            out, box = super().__call__(rgb, cfg, box)
            out[185:200, 120:450] = 1.0                # 2nd pass keeps the painted bar band
            return out, box

    rgba, rep, dbg = hc.process(raw, Greedy(), hc.telea, hc.Config())
    a = rgba[..., 3] >= 128
    assert not a[193:198, 100:140].any(), "painted wall kept at the bar's end"


# --- IS-Net keeping wall the parser left out (BOA-005343) ----------------------------

def _white_on_white():
    wall, cream = (212, 210, 206), (236, 229, 212)
    raw = np.empty((800, 600, 3), np.uint8)
    raw[:] = wall
    parsed = np.zeros((800, 600), bool)
    parsed[200:600, 150:450] = True
    parsed[460:600, 270:330] = False                   # the gap between the legs
    raw[parsed] = cream
    return raw, parsed


def test_wall_kept_counts_a_block_of_wall_between_the_legs():
    raw, parsed = _white_on_white()
    kept = parsed.copy()
    kept[470:600, 275:325] = True                      # IS-Net took the wall in the gap
    assert cutout.wall_kept(raw, kept, parsed, HCFG) > HCFG["max_wall_kept"]


def test_wall_kept_ignores_fringe_and_clips_isnet_adds():
    raw, parsed = _white_on_white()
    kept = parsed.copy()
    for x in range(160, 440, 12):
        kept[600:640, x:x + 3] = True                  # fringe threads: thin
    kept[170:200, 180:200] = True                      # a clip, not wall-coloured...
    raw[170:200, 180:200] = (150, 150, 155)
    assert cutout.wall_kept(raw, kept, parsed, HCFG) == 0.0


def test_wall_kept_ignores_garment_isnet_adds_that_is_not_wall_coloured():
    """v2 tore a piece out of a dark garment; IS-Net has it. Not wall: no refusal."""
    raw, parsed = _white_on_white()
    raw[parsed] = (40, 40, 45)
    kept = parsed.copy()
    raw[300:360, 200:260] = (40, 40, 45)
    parsed = parsed.copy()
    parsed[300:360, 200:260] = False                   # the parser's hole
    assert cutout.wall_kept(raw, kept, parsed, HCFG) == 0.0


def test_no_bar_stops_before_the_inpainting(monkeypatch):
    """A web image on white, a model wearing it: no bar. The strategy will not use the
    result, so the slow half (inpainting, the 2nd pass) never runs."""
    raw, garment, *_ = hung(pencil=False)
    raw = raw.copy()
    raw[120:200] = WALL                                # no hook, no bar, no clips
    calls = []
    monkeypatch.setattr(hc, "inpaint_regions", lambda *a, **k: calls.append(1) or a[0])
    rgba, rep, _ = hc.process(raw, FakeSeg(), hc.telea, hc.Config(stop_without_bar=True))
    assert rep.bar_found is False and calls == [] and not rgba[..., 3].any()


def test_opencv_5_line_shape_is_read():
    """HoughLinesP returns (N, 4) on OpenCV 5 and (N, 1, 4) on 4; both are read."""
    import cv2

    m = np.zeros((100, 300), np.uint8)
    cv2.line(m, (10, 50), (290, 51), 255, 3)
    lines = cv2.HoughLinesP(m, 1, np.pi / 720, 50, minLineLength=100, maxLineGap=10)
    assert lines.reshape(-1, 4).shape[1] == 4


# --- the route ---------------------------------------------------------------------

HCFG = cutout.hanger_config({})


@pytest.mark.parametrize("origin,expected", [
    ("WEB", True), ("MANUAL", True), ("web", True),
    ("PHOTOBOOTH", False), ("DECISION", False), (None, False), ("", False),
])
def test_only_wall_photos_take_the_hanger_route(origin, expected):
    assert cutout.hanger_route(origin, HCFG) is expected


def test_policy_can_turn_the_route_off():
    off = cutout.hanger_config({"imagery": {"cutout": {"hanger": False}}})
    assert cutout.hanger_route("WEB", off) is False


def test_the_shipped_policy_routes_web_and_manual():
    cfg = cutout.hanger_config()
    assert cfg["enabled"] and set(cfg["origins"]) == {"WEB", "MANUAL"}
    assert cutout.HANGER in cutout.config()["strategies"]


def _png(arr, mask):
    b = io.BytesIO()
    Image.fromarray(np.dstack([arr, np.where(mask, 255, 0).astype(np.uint8)]), "RGBA").save(b, "PNG")
    return b.getvalue()


def _jpeg(arr):
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "JPEG", quality=95)
    return b.getvalue()


@pytest.fixture
def chain(monkeypatch):
    raw, garment, *_ = hung()
    calls = []
    hanger_cut, v2_cut = _png(raw, garment), _png(raw, garment & ~np.eye(800, 600, dtype=bool))
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (calls.append("hanger"), (hanger_cut, None))[1])
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (v2_cut, None))[1])
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))
    return {"raw": _jpeg(raw), "calls": calls, "hanger": hanger_cut, "v2": v2_cut}


def test_a_wall_photo_is_cut_by_isnet_first(chain):
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft"], origin="WEB")
    assert provider == cutout.HANGER and out == chain["hanger"] and chain["calls"] == ["hanger"]


def test_a_photobooth_photo_goes_straight_to_the_fine_tuned_parser(chain):
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft"], origin="PHOTOBOOTH")
    assert provider == "cloth-seg-ft" and chain["calls"] == ["v2"]


def test_no_origin_means_the_chain_as_before(chain):
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft"])
    assert provider == "cloth-seg-ft" and chain["calls"] == ["v2"]


def test_no_hanger_bar_falls_through_to_the_parser(chain, monkeypatch):
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (chain["calls"].append("hanger"),
                                                  (None, "no hanger bar found in the photo"))[1])
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft"], origin="WEB")
    assert provider == "cloth-seg-ft" and chain["calls"] == ["hanger", "v2"]


def _paid(monkeypatch, calls, gemini=(None, "no image"), openai=(None, "no image")):
    monkeypatch.setattr(cutout, "_gemini",
                        lambda data, timeout_s, prompt=None: (calls.append("gemini"), gemini)[1])
    monkeypatch.setattr(cutout, "_openai", lambda *a, **k: (calls.append("openai"), openai)[1])


REFUSED = "needs review: hanger mask covers a lot of garment"


def test_a_hung_photo_isnet_cannot_cut_goes_to_gemini_once_before_v2(chain, monkeypatch):
    """A bar was found but IS-Net's cut was refused: Gemini, once, and only then v2,
    which keeps the bar and clips on these photos."""
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (calls.append("hanger"), (None, REFUSED))[1])
    _paid(monkeypatch, calls)
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft", "gemini-paint", "openai-paint"],
        origin="WEB")
    assert calls == ["hanger", "gemini", "v2"], calls
    assert provider == "cloth-seg-ft"


def test_gpt_image_comes_after_the_parsers_on_the_hanger_route(chain, monkeypatch):
    """It redrew every hung garment it was given (12 of 12, 1 Oct 2026)."""
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (calls.append("hanger"), (None, REFUSED))[1])
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (None, "x"))[1])
    _paid(monkeypatch, calls)
    cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft", "gemini-paint", "openai-paint"],
        origin="WEB")
    assert calls == ["hanger", "gemini", "v2", "openai"], calls


def test_no_hanger_bar_sends_the_parsers_before_any_paid_method(chain, monkeypatch):
    """MID-000442 / MID-000445 (7 Oct 2026): WEB photos taken in the booth. No bar, so
    not a hung photo: v2 is asked before Gemini, which kept the whole mannequin stand."""
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (calls.append("hanger"), (None, "no hanger bar found in the photo"))[1])
    _paid(monkeypatch, calls)
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft", "gemini-paint", "openai-paint"],
        origin="WEB")
    assert calls == ["hanger", "v2"], calls
    assert provider == "cloth-seg-ft"


def test_no_hanger_bar_keeps_the_paid_methods_in_their_order(chain, monkeypatch):
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_hanger_isnet",
                        lambda data, hcfg, **kw: (calls.append("hanger"), (None, "no hanger bar found in the photo"))[1])
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (None, "x"))[1])
    _paid(monkeypatch, calls)
    cutout.remove_background(
        chain["raw"], strategies=[cutout.HANGER, "cloth-seg-ft", "gemini-paint", "openai-paint"],
        origin="WEB")
    assert calls == ["hanger", "v2", "gemini", "openai"], calls


# --- a painted cut-out must be the photo's own garment ---------------------------------

def _textured():
    rng = np.random.default_rng(9)
    raw = np.full((800, 600, 3), 225, np.uint8)
    g = np.zeros((800, 600), bool)
    g[150:650, 150:450] = True
    tex = rng.normal(0, 25, (800, 600, 1))
    raw[g] = np.clip(np.array(DENIM, np.float32) + tex[g], 0, 255).astype(np.uint8)
    raw[300:330, 200:400] = 240                         # a printed band (lettering)
    return raw, g


def test_a_faithful_paint_passes_the_fidelity_check():
    raw, g = _textured()
    pm, em = cutout._paint_fidelity(_jpeg(raw), _png(raw, g))
    assert pm > 0.9 and em > 0.8


def test_a_redrawn_garment_fails_it():
    """Same outline, same colour — new texture, the lettering moved: gpt-image's redraw."""
    raw, g = _textured()
    redraw, _ = _textured()
    rng = np.random.default_rng(1)
    redraw[g] = np.clip(np.array(DENIM, np.float32) + rng.normal(0, 25, (int(g.sum()), 3)).astype(np.float32) * 0.3,
                        0, 255).astype(np.uint8)
    redraw[400:430, 220:380] = 240
    pm, em = cutout._paint_fidelity(_jpeg(raw), _png(redraw, g))
    pol = cutout.config()
    assert pm < pol["paint_min_pixel_match"] or em < pol["paint_min_edge_match"]


def test_the_chain_refuses_a_redraw_and_moves_on(chain, monkeypatch):
    raw, g = _textured()
    redraw = raw.copy()
    redraw[g] = np.array(DENIM, np.uint8)               # texture and lettering gone
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (None, "x"))[1])
    _paid(monkeypatch, calls, gemini=(_png(redraw, g), None))
    monkeypatch.setattr(cutout, "_same_framing", lambda s, o: (True, "same"))
    out, err, provider = cutout.remove_background(_jpeg(raw), strategies=["cloth-seg-ft", "gemini-paint"])
    assert out is None and "re-drew the garment" in err


def test_a_photobooth_photo_keeps_the_old_order(chain, monkeypatch):
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (None, "x"))[1])
    monkeypatch.setattr(cutout, "_gemini",
                        lambda data, timeout_s, prompt=None: (calls.append("gemini"), (None, "x"))[1])
    monkeypatch.setattr(cutout, "_openai", lambda *a, **k: (calls.append("openai"), (None, "x"))[1])
    cutout.remove_background(chain["raw"], origin="PHOTOBOOTH",
                             strategies=[cutout.HANGER, "cloth-seg-ft", "gemini-paint", "openai-paint"])
    assert calls == ["v2", "gemini", "openai"], calls


def test_a_paid_strategy_is_asked_once_not_twice(chain, monkeypatch):
    calls = chain["calls"]
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (None, "x"))
    monkeypatch.setattr(cutout, "_gemini",
                        lambda data, timeout_s, prompt=None: (calls.append("gemini"), (None, "x"))[1])
    cutout.remove_background(chain["raw"], strategies=["cloth-seg-ft", "gemini-paint"])
    assert calls == ["gemini"]
    assert cutout.config()["paid_attempts"] == 1


def test_the_strategy_refuses_when_no_bar_is_found(monkeypatch):
    raw, *_ = hung()

    def no_bar(raw, cfg=None, lama_path=None):
        rep = hc.Report(bar_found=False)
        return np.zeros(raw.shape[:2] + (4,), np.uint8), rep

    monkeypatch.setattr(hc, "cutout", no_bar)
    out, err = cutout._hanger_isnet(_jpeg(raw), HCFG)
    assert out is None and "no hanger bar" in err


def test_the_strategy_refuses_when_the_mask_covers_much_garment(monkeypatch):
    """BOA-003065: the hanger mask lay over the front waistband and the fill smeared it.
    The script flags that for review; Hermes takes the parser's cut-out instead."""
    raw, *_ = hung()

    def flagged(raw, cfg=None, lama_path=None):
        rep = hc.Report(bar_found=True, reasons=["hanger mask covers a lot of garment: check the waistband"],
                        needs_review=True)
        return np.zeros(raw.shape[:2] + (4,), np.uint8), rep

    monkeypatch.setattr(hc, "cutout", flagged)
    out, err = cutout._hanger_isnet(_jpeg(raw), HCFG)
    assert out is None and "covers a lot of garment" in err


def test_a_painted_answer_is_stored_at_the_photographs_own_resolution(monkeypatch):
    """7 Oct 2026, "why is my quality degraded": gemini-paint returned the model's ~1 MP
    render and it was stored as the product's picture (674x899 beside a 3000x4000 photo).
    Once a paint passes the framing and fidelity checks, its silhouette is applied to the
    ORIGINAL pixels, like a mask strategy."""
    raw, g = _textured()                                    # 800x600 photograph
    src = _jpeg(raw)
    small = Image.fromarray(np.dstack([raw, np.where(g, 255, 0).astype(np.uint8)]),
                            "RGBA").resize((300, 400), Image.Resampling.LANCZOS)
    b = io.BytesIO()
    small.save(b, "PNG")
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (b.getvalue(), None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    out, err, provider = cutout.remove_background(src, strategies=["gemini-paint"])
    assert provider == "gemini-paint" and out is not None, err
    im = Image.open(io.BytesIO(out))
    assert im.size == (600, 800)                            # the photo's size, not the model's
    arr = np.asarray(im.getchannel("A"))
    assert (arr[300:500, 250:350] == 255).all()
    rgb_out = np.asarray(im.convert("RGB"))
    rgb_src = np.asarray(Image.open(io.BytesIO(src)).convert("RGB"))
    assert (rgb_out[300:500, 250:350] == rgb_src[300:500, 250:350]).all()


def test_the_painted_pixels_stay_when_policy_says_so(monkeypatch):
    raw, g = _textured()
    src = _jpeg(raw)
    small = Image.fromarray(np.dstack([raw, np.where(g, 255, 0).astype(np.uint8)]),
                            "RGBA").resize((300, 400), Image.Resampling.LANCZOS)
    b = io.BytesIO()
    small.save(b, "PNG")
    monkeypatch.setattr(cutout, "config",
                        lambda pol=None: {**cutout.DEFAULTS, "paint_keep_resolution": False,
                                          "max_concurrent": 0})
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (b.getvalue(), None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    out, err, provider = cutout.remove_background(src, strategies=["gemini-paint"])
    assert out is not None and Image.open(io.BytesIO(out)).size == (300, 400)
