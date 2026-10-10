"""Shoes and bags: the `object-isnet` strategy and the object checks (app/imaging/object_cutout.py),
6 Oct 2026.

IS-Net cuts the whole product — a pair of shoes on a table, a bag on a wall hook — where the
garment parsers, which have no class for either, return fragments or nothing. Its cut-outs, and
every paid strategy's for these products, are judged by `object_cutout.checks`: the garment
checks read grey leather on a grey table as backdrop and the wall through a bag's handle as a
tear.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout  # noqa: E402
from app.imaging import hanger_cutout as hc  # noqa: E402
from app.imaging import object_cutout as oc  # noqa: E402

TABLE = (205, 205, 208)
LEATHER = (60, 58, 55)


def _png(rgb, mask):
    b = io.BytesIO()
    Image.fromarray(np.dstack([rgb, np.where(mask, 255, 0).astype(np.uint8)]), "RGBA").save(b, "PNG")
    return b.getvalue()


def _jpeg(rgb):
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, "JPEG", quality=95)
    return b.getvalue()


def pair(gap: int = 80):
    """600x800: two shoes (dark blobs) side by side on a light table, `gap` px apart."""
    rgb = np.empty((800, 600, 3), np.uint8)
    rgb[:] = TABLE
    m = np.zeros((800, 600), bool)
    left_end = 300 - gap // 2
    cv2.ellipse(m.view(np.uint8), (left_end - 110, 450), (110, 55), 0, 0, 360, 1, -1)
    cv2.ellipse(m.view(np.uint8), (600 - left_end + 110, 450), (110, 55), 0, 0, 360, 1, -1)
    rgb[m] = LEATHER
    return rgb, m


# --- the family ---------------------------------------------------------------------

@pytest.mark.parametrize("garment,family", [
    ("Shoes Running", "footwear"), ("Men Shoes", "footwear"), ("Shoes Heels", "footwear"),
    ("Shoes Sandals & Slippers", "footwear"), ("Women Boots", "footwear"),
    ("Backpacks & Bags Tote Bags", "bags"), ("Backpacks & Bags Backpacks", "bags"),
    ("Accessories Wallet", "bags"),
    # Whole words: BOOTCUT jeans are not boots and BAGGY trousers are not a bag.
    ("Bottoms Bootcut Jeans", None), ("Trousers Baggy", None),
    ("T-Shirts & Tops T-Shirts", None), ("", None), (None, None),
])
def test_the_family_is_read_from_the_category_as_whole_words(garment, family):
    assert oc.family_of(garment) == family


def test_policy_can_route_only_some_families():
    assert oc.family_of("Shoes Running", ["bags"]) is None
    assert oc.family_of("Backpacks & Bags Bags", ["bags"]) == "bags"


def test_the_route_can_be_turned_off_or_limited_by_origin():
    on = cutout.object_config({})
    assert cutout.object_route("Shoes Running", "WEB", on) == "footwear"
    assert cutout.object_route("Shoes Running", None, on) == "footwear"   # [] = every origin
    off = cutout.object_config({"imagery": {"cutout": {"object": False}}})
    assert cutout.object_route("Shoes Running", "WEB", off) is None
    web = cutout.object_config({"imagery": {"cutout": {"object": {"origins": ["WEB"]}}}})
    assert cutout.object_route("Shoes Running", "PHOTOBOOTH", web) is None
    assert cutout.object_route("Shoes Running", "web", web) == "footwear"


def test_the_shipped_policy_routes_shoes_and_bags():
    ocfg = cutout.object_config()
    assert ocfg["enabled"] and set(ocfg["families"]) == {"footwear", "bags"}
    assert cutout.OBJECT in cutout.config()["strategies"]
    assert cutout.OBJECT in cutout.config()["url_strategies"]
    # v2 and v1 are not in the object chain.
    assert not {"cloth-seg-ft", "cloth-seg-ft-backup", "cloth-seg"} & set(ocfg["then"])


# --- the checks ---------------------------------------------------------------------

OCFG = cutout.object_config({})
PCFG = cutout.config({})


def test_a_clean_pair_of_shoes_passes():
    rgb, m = pair()
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, m), OCFG, PCFG)
    assert ok, why


def test_an_empty_cut_out_is_refused():
    """6 Oct 2026: an empty PNG passed every garment check."""
    rgb, m = pair()
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, np.zeros_like(m)), OCFG, PCFG)
    assert not ok and "almost nothing" in why


def test_a_cut_out_reaching_the_frames_edge_is_refused():
    """The table, the wall or a person's arm runs off the photo; a shoe does not
    (KLE-002830: boots held up by a person, the arm to the right-hand edge)."""
    rgb, m = pair()
    m = m.copy()
    m[400:470, 400:600] = True                         # an arm from the right-hand edge
    rgb[m] = LEATHER
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, m), OCFG, PCFG)
    assert not ok and "edge" in why


def test_the_wall_through_a_bags_handle_is_an_opening_not_a_tear():
    rgb = np.empty((800, 600, 3), np.uint8)
    rgb[:] = TABLE
    m = np.zeros((800, 600), bool)
    m[350:650, 150:450] = True                         # the bag
    cv2.ellipse(m.view(np.uint8), (300, 350), (110, 170), 0, 180, 360, 1, 28)   # the handle
    rgb[m] = (50, 80, 140)
    rgb[250:330, 290:292] = (90, 90, 90)               # a pencil line on the wall inside it
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, m), OCFG, PCFG)
    assert ok, why


def test_a_hole_the_photo_shows_as_leather_is_a_tear():
    rgb, m = pair()
    cut = m.copy()
    cut[430:470, 120:200] = False                      # the mask dropped part of the left shoe
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, cut), OCFG, PCFG)
    assert not ok and "tore" in why


def test_the_space_between_two_shoes_is_not_a_tear():
    """KLE-002829: on a grey table the floor between a pair standing close reads as the
    leather's colour; it borders two separate pieces, so it is the space between them."""
    rgb, m = pair(gap=8)
    rgb[m] = (150, 150, 152)
    between = np.zeros_like(m)
    between[400:500, 280:320] = True
    rgb[between & ~m] = (140, 140, 142)                # shadow between the shoes
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, m), OCFG, PCFG)
    assert ok, why


# --- the cut-out --------------------------------------------------------------------

class FakeSeg:
    """IS-Net stand-in: dark is the object, on the full frame and on a crop alike."""

    def _mask(self, rgb):
        return (np.asarray(rgb).mean(-1) < 150).astype(np.float32)


def test_a_straight_strip_to_the_frames_edge_goes_and_the_laces_stay(monkeypatch):
    """KLE-002828: the table's lit edge, kept by IS-Net with the shoe, from the heel to the
    side of the frame. A lace is thin too, but it is not straight to the edge."""
    monkeypatch.setattr(hc, "segmenter", lambda model: FakeSeg())
    rgb, m = pair()
    cv2.line(rgb, (520, 440), (599, 300), (110, 110, 110), 7)       # the table's edge
    cv2.ellipse(rgb, (150, 402), (40, 32), 0, 180, 360, (70, 70, 70), 5)   # a loop of lace
    alpha, rep = oc.cutout(rgb, oc.Config(solid_px=9))
    hard = alpha > 0.5
    assert rep.edge_strands_removed == 1
    assert not hard[:, -3:].any()                      # nothing left at the edge
    assert hard[366:374, 140:160].any()                # the lace is kept
    assert hard[m].mean() > 0.99                       # both shoes whole
    assert rep.pieces == 2


def test_a_person_reaching_in_is_not_trimmed_away_the_check_refuses_it(monkeypatch):
    monkeypatch.setattr(hc, "segmenter", lambda model: FakeSeg())
    rgb, m = pair()
    rgb[410:470, 440:600] = (90, 70, 60)               # an arm: wide, not a strip
    alpha, rep = oc.cutout(rgb, oc.Config(solid_px=9))
    assert rep.edge_strands_removed == 0
    ok, why = oc.checks(_jpeg(rgb), _png(rgb, alpha > 0.5), OCFG, PCFG)
    assert not ok and "edge" in why


def test_nothing_found_is_said(monkeypatch):
    monkeypatch.setattr(hc, "segmenter", lambda model: FakeSeg())
    rgb = np.full((800, 600, 3), 220, np.uint8)
    alpha, rep = oc.cutout(rgb)
    assert not alpha.any() and rep.notes == ["no product found"]


# --- the chain ----------------------------------------------------------------------

@pytest.fixture
def chain(monkeypatch):
    rgb, m = pair()
    calls: list[str] = []
    cut = _png(rgb, m)

    def strat(name, result):
        return lambda *a, **k: (calls.append(name), result)[1]

    monkeypatch.setattr(cutout, "_object_isnet", strat("object", (cut, None)))
    monkeypatch.setattr(cutout, "_hanger_isnet", strat("hanger", (None, "no hanger bar found")))
    monkeypatch.setattr(cutout, "_cloth_seg_ft", strat("v2", (cut, None)))
    monkeypatch.setattr(cutout, "_cloth_seg_ft_backup", strat("v1", (cut, None)))
    prompts: list[str] = []
    monkeypatch.setattr(cutout, "_gemini",
                        lambda data, t, prompt=None: (calls.append("gemini"), prompts.append(prompt),
                                                      (None, "no image"))[2])
    monkeypatch.setattr(cutout, "_openai", strat("openai", (None, "no image")))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    # The garment checks must never judge a shoe: they refuse everything here.
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (False, "garment backdrop check ran"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (True, "garment tear check ran"))
    return {"raw": _jpeg(rgb), "cut": cut, "calls": calls, "prompts": prompts}


STRATS = [cutout.OBJECT, cutout.HANGER, "cloth-seg-ft", "cloth-seg-ft-backup", "gemini-paint", "openai-paint"]


def test_a_shoe_is_cut_by_the_object_strategy_and_judged_as_an_object(chain):
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="Shoes Running", origin="WEB")
    assert provider == cutout.OBJECT and out is not None, err
    assert chain["calls"] == ["object"]


def test_a_shoe_the_object_strategy_cannot_cut_goes_to_the_paid_ones_never_v2(chain, monkeypatch, paid_cutout_methods):
    monkeypatch.setattr(cutout, "_object_isnet",
                        lambda *a, **k: (chain["calls"].append("object"), (None, "no product found"))[1])
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="Backpacks & Bags Bags", origin="WEB")
    assert chain["calls"] == ["object", "gemini", "openai"], chain["calls"]
    assert out is None and provider == "none"
    # The paid model is told what the product is, and that the hook is not part of it.
    assert "a bag" in chain["prompts"][0] and "hook" in chain["prompts"][0]
    assert "garment" not in chain["prompts"][0]


def test_a_paid_cut_out_of_a_shoe_is_judged_by_the_object_checks(chain, monkeypatch, paid_cutout_methods):
    monkeypatch.setattr(cutout, "_object_isnet",
                        lambda *a, **k: (chain["calls"].append("object"), (None, "no product found"))[1])
    monkeypatch.setattr(cutout, "_gemini",
                        lambda data, t, prompt=None: (chain["calls"].append("gemini"), (chain["cut"], None))[1])
    monkeypatch.setattr(cutout, "_same_framing", lambda a, b: (True, "same"))
    monkeypatch.setattr(cutout, "_paint_fidelity", lambda a, b: (0.99, 0.99))
    seen: list[str] = []
    monkeypatch.setattr(oc, "checks", lambda *a, **k: (seen.append("object checks"), (True, "fine"))[1])
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="Shoes Heels", origin="WEB")
    assert provider == "gemini-paint" and seen == ["object checks"], err


def test_an_object_cut_out_the_checks_refuse_moves_on(chain, monkeypatch, paid_cutout_methods):
    monkeypatch.setattr(oc, "checks", lambda *a, **k: (False, "the cut-out reaches the frame's edge"))
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="Shoes Boots", origin="WEB")
    assert out is None and "frame's edge" in err
    assert chain["calls"] == ["object", "gemini", "openai"]


def test_a_garment_still_takes_its_own_route(chain, monkeypatch):
    """Shorts hung on the wall: the hanger strategy, then the parsers — and the object
    strategy is never asked. (No bar found here, so the parsers come before Gemini:
    7 Oct 2026, MID-000442.)"""
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="Bottoms Shorts", origin="WEB")
    assert "object" not in chain["calls"]
    assert chain["calls"][:2] == ["hanger", "v2"] and provider == "cloth-seg-ft"


def test_no_category_means_the_chain_as_before(chain, monkeypatch):
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    out, err, provider = cutout.remove_background(chain["raw"], strategies=STRATS, origin="PHOTOBOOTH")
    assert chain["calls"] == ["v2"] and provider == "cloth-seg-ft"


def test_the_object_cut_runs_for_a_garment_when_named_without_a_parser(chain, monkeypatch):
    """The escalation's ask (6 Oct 2026): MID-000053's BACK — the parsers tore the dark
    layer behind the racerback on every re-cut; IS-Net cut the product whole. Named
    WITHOUT any parser, the object cut runs for a garment too, judged by the garment
    checks (not the object checks)."""
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    judged = []
    monkeypatch.setattr(oc, "checks", lambda *a, **k: (judged.append(1), (True, "fine"))[1])
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=[cutout.OBJECT, "gemini-paint", "openai-paint"],
        garment="T-Shirts & Tops Tops", origin="DECISION")
    assert provider == cutout.OBJECT and out is not None, err
    assert chain["calls"] == ["object"]
    assert judged == []                      # the garment checks judged it, not the object checks


def test_a_list_that_still_names_a_parser_keeps_the_family_gate(chain, monkeypatch):
    """url_strategies and the policy default name the parsers: there the object cut is
    the shoes-and-bags route and never runs for a garment."""
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    out, err, provider = cutout.remove_background(
        chain["raw"], strategies=STRATS, garment="T-Shirts & Tops Tops", origin="PHOTOBOOTH")
    assert "object" not in chain["calls"] and provider == "cloth-seg-ft"


# --- stray pieces on the garment path (6 Oct 2026) -------------------------------------
#
# MID-000053 FRONT: Gemini's keyed answer left a corner of the studio screen opaque at
# the frame's foot. Dark, so the backdrop-COLOUR checks all passed it, and a fragment
# floated beside the tank top on the live listing.

def _garment_scene():
    rgb = np.full((800, 600, 3), 240, np.uint8)
    m = np.zeros((800, 600), bool)
    m[150:600, 180:420] = True
    rgb[m] = (120, 30, 60)
    return rgb, m


def test_a_small_piece_far_from_the_garment_is_dropped():
    rgb, m = _garment_scene()
    frag = np.zeros_like(m)
    frag[740:790, 20:90] = True                       # the studio screen's corner
    rgb[frag] = (40, 35, 50)
    png, n = cutout._drop_stray_pieces(_png(rgb, m | frag))
    assert n == 1
    a = np.asarray(Image.open(io.BytesIO(png)).getchannel("A"))
    assert not (a[740:790, 20:90] >= 128).any()
    assert (a[150:600, 180:420] >= 128).all()


def test_a_near_piece_and_a_sets_second_garment_both_stay():
    rgb, m = _garment_scene()
    button = np.zeros_like(m)
    button[610:625, 290:305] = True                   # detached by the parser, but close
    half = np.zeros_like(m)
    half[650:790, 180:420] = True                     # a set's second piece: large
    rgb[button | half] = (120, 30, 60)
    png, n = cutout._drop_stray_pieces(_png(rgb, m | button | half))
    assert n == 0 and png is not None


def test_the_cleaner_runs_on_the_garment_chain_not_the_object_route(chain, monkeypatch):
    seen = []
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    monkeypatch.setattr(cutout, "_drop_stray_pieces",
                        lambda png, cfg=None: (seen.append(1), (png, 0))[1])
    cutout.remove_background(chain["raw"], strategies=STRATS, garment="Bottoms Shorts",
                             origin="PHOTOBOOTH")
    assert seen == [1]
    seen.clear()
    cutout.remove_background(chain["raw"], strategies=STRATS, garment="Shoes Running",
                             origin="WEB")
    assert seen == []                                 # the object route keeps its pair rules


def test_a_garment_ask_cuts_one_piece_a_shoe_ask_keeps_the_pair(chain, monkeypatch):
    """The escalation's object-isnet ask for a garment is a SINGLE-piece cut; the
    shoes-and-bags route keeps every large piece (a pair apart is two)."""
    asked = []

    def spy(data, ocfg, single=False):
        asked.append(single)
        return chain["cut"], None

    monkeypatch.setattr(cutout, "_object_isnet", spy)
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    cutout.remove_background(chain["raw"], strategies=[cutout.OBJECT, "gemini-paint", "openai-paint"],
                             garment="T-Shirts & Tops Tops", origin="DECISION")
    cutout.remove_background(chain["raw"], strategies=STRATS, garment="Shoes Running", origin="WEB")
    assert asked == [True, False]


def test_single_piece_keeps_only_the_largest(monkeypatch):
    monkeypatch.setattr(hc, "segmenter", lambda model: FakeSeg())
    rgb, m = pair()                                   # two shoes apart
    alpha, rep = oc.cutout(rgb, oc.Config(solid_px=9, single_piece=True))
    assert rep.pieces == 1
    import cv2 as _cv2
    n, _l = _cv2.connectedComponents((alpha > 0.5).astype(np.uint8), connectivity=8)
    assert n - 1 == 1
