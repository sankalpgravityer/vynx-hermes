"""Two cut-out checks that held products for a false alarm (12 Oct 2026).

368 held products kept their old 896x1195 cut-out: every new cut-out was refused by a
check, and Hermes re-framed the old one. Proven on two of them, both GARMENT PARSER
(cloth-seg) cut-outs:

  * KLE-002497 — a hoodie on a hanger at the decision station, its cuff at the frame's
    foot, refused as "kept the stand". No pole, no podium.
  * KLE-001856 — light-grey joggers on a near-white wall, refused because 14% of the
    cut-out is "the backdrop colour": its own fabric.

A parser's answer now gets the benefit of the doubt exactly where a colour test cannot
judge; whole-object methods (IS-Net, the paints) are judged as before. Synthetic images
only — the shapes and colours that decide, nothing from a tenant.
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

H, W = 400, 300
WALL = (206, 206, 200)


def png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def jpg(rgb: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")   # lossless: the colours are the test
    return buf.getvalue()


def cut(colour=(40, 50, 120), top=60, left=70, right=230, to_foot=True,
        stand=None) -> np.ndarray:
    """A garment block, optionally reaching the frame's foot, optionally on a stand
    (a narrow pole and a wide podium in `stand`'s colour)."""
    a = np.zeros((H, W, 4), np.uint8)
    a[..., :3] = 255
    bottom = H if to_foot else H - 120
    a[top:bottom, left:right, :3] = colour
    a[top:bottom, left:right, 3] = 255
    if stand is not None:
        a[bottom:H - 40, 145:155, :3] = stand          # the pole
        a[bottom:H - 40, 145:155, 3] = 255
        a[H - 40:H, 90:210, :3] = stand                # the podium, at the foot
        a[H - 40:H, 90:210, 3] = 255
    return a


CFG = {**cutout.config(), "stand_parser_garment_foot": 0.6, "leftover_wall_coloured_garment": 0.10}


# ---- the stand check ------------------------------------------------------------

def test_a_parser_garment_cut_by_the_frame_at_its_hem_is_not_a_stand():
    stood, why = cutout._kept_stand(png(cut()), {**CFG, "_parser": True})
    assert stood is False and "garment's own colour" in why


def test_the_same_cut_out_from_a_whole_object_method_is_still_a_stand():
    stood, _ = cutout._kept_stand(png(cut()), {**CFG, "_parser": False})
    assert stood is True


def test_a_parser_cut_out_on_a_real_pole_and_podium_is_still_a_stand():
    a = cut(to_foot=False, stand=(150, 150, 150))
    stood, why = cutout._kept_stand(png(a), {**CFG, "_parser": True})
    assert stood is True and "pole and podium" in why


def test_zero_restores_the_old_rule_for_parsers_too():
    stood, _ = cutout._kept_stand(png(cut()), {**CFG, "_parser": True, "stand_parser_garment_foot": 0})
    assert stood is True


def test_a_top_that_ends_above_the_foot_is_never_a_stand():
    for parser in (True, False):
        assert cutout._kept_stand(png(cut(to_foot=False)), {**CFG, "_parser": parser})[0] is False


# ---- the source-backdrop check --------------------------------------------------

def scene(garment_rgba: np.ndarray) -> bytes:
    """The photograph: a uniform wall with the garment's pixels on it."""
    src = np.zeros((H, W, 3), np.uint8)
    src[:] = WALL
    m = garment_rgba[..., 3] >= 250
    src[m] = garment_rgba[..., :3][m]
    return jpg(src)


def light_garment() -> np.ndarray:
    """A light-grey garment whose lit half IS the wall's colour."""
    a = cut(colour=(150, 150, 146), to_foot=False)
    a[60:140, 70:230, :3] = (204, 204, 198)            # 31% of it within the wall's colour
    return a


def dark_garment_with_a_strip_of_wall() -> np.ndarray:
    a = cut(colour=(30, 30, 40), to_foot=False)
    a[60:280, 230:240, :3] = WALL                      # a strip of wall the mask kept
    a[60:280, 230:240, 3] = 255
    return a


def test_a_wall_coloured_garment_from_a_parser_is_not_judged_by_colour():
    g = light_garment()
    ok, why = cutout._source_backdrop(scene(g), png(g), parser=True, cfg=CFG)
    assert ok is None and "the wall's colour" in why


def test_the_same_garment_from_a_whole_object_method_is_still_refused():
    g = light_garment()
    ok, _ = cutout._source_backdrop(scene(g), png(g), parser=False, cfg=CFG)
    assert ok is False


def test_a_strip_of_wall_kept_beside_a_dark_garment_is_still_refused_for_a_parser():
    g = dark_garment_with_a_strip_of_wall()
    ok, why = cutout._source_backdrop(scene(g), png(g), parser=True, cfg=CFG)
    assert ok is False and "backdrop colour" in why


def test_zero_restores_the_old_backdrop_rule():
    g = light_garment()
    ok, _ = cutout._source_backdrop(scene(g), png(g), parser=True,
                                    cfg={**CFG, "leftover_wall_coloured_garment": 0})
    assert ok is False


def test_kept_backdrop_reads_the_parser_flag_from_the_settings():
    g = light_garment()
    ok_parser, _ = cutout._kept_backdrop(scene(g), png(g), {**CFG, "_parser": True})
    ok_object, _ = cutout._kept_backdrop(scene(g), png(g), {**CFG, "_parser": False})
    assert ok_parser is True and ok_object is False


# ---- the chain hands each check the right flag ----------------------------------

@pytest.mark.parametrize("winner, parser", [("cloth-seg-ft", True), ("object-isnet", False)])
def test_the_chain_marks_parser_answers_and_only_those(monkeypatch, winner, parser):
    seen: list[bool] = []
    good = png(cut(to_foot=False))
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (good, None))
    monkeypatch.setattr(cutout, "_object_isnet", lambda data, ocfg, single=False: (good, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda b: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_stand", lambda p, c=None: (seen.append(bool((c or {}).get("_parser"))), (False, ""))[1])
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))
    monkeypatch.setattr(cutout, "_finish_neck", lambda data, out, *a, **k: out)
    out, err, provider = cutout.remove_background(scene(cut(to_foot=False)), timeout_s=1,
                                                  strategies=[winner], garment="T-Shirts & Polos T-Shirt")
    assert provider == winner and out is not None, err
    assert seen and seen[0] is parser


def test_the_shipped_policy_turns_both_on():
    c = cutout.config()
    assert c["stand_parser_garment_foot"] == 0.6 and c["leftover_wall_coloured_garment"] == 0.10
