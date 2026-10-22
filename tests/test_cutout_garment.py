"""The category is decided from the background-removed photographs, not the render.

22 Oct 2026, the user: BOA-007605 is a t-shirt filed under Bags. Its renders
were drawn from that wrong filing, so the garment that re-files a product is
read from the FRONT/BACK cut-outs (app/imaging/cutout_garment.py).
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import cutout_garment as cg  # noqa: E402


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    monkeypatch.setenv("HERMES_VISION_CACHE", "off")


POL = copy.deepcopy(policy())


def m(view, processing, origin="WEB", url=None, position=0, **kw):
    return {"view": view, "processing": processing, "origin": origin, "position": position,
            "url": url or f"https://cdn/{view}-{processing}-{origin}-{position}.png", **kw}


class FakeEvidence:
    def __init__(self, answer):
        self.answer, self.asked, self.errors = answer, None, []

    def _fetch_images(self, urls):
        self.asked = list(urls)
        return [f"part:{u}" for u in urls]

    def _generate(self, *, model, contents, schema, system):
        return self.answer


BOA_007605 = [
    m("AI_CLOSEUP", "GENERATED", "AI"), m("AI_FRONT_34", "GENERATED", "AI", position=1),
    m("FRONT", "BG_REMOVED", position=3), m("BACK", "BG_REMOVED", position=4),
    m("FRONT", "RAW", position=6), m("LABEL", "RAW", position=5),
]


def test_reads_the_cut_outs_front_then_back_never_a_render_or_a_raw():
    assert cg.photos(BOA_007605) == ["https://cdn/FRONT-BG_REMOVED-WEB-3.png",
                                     "https://cdn/BACK-BG_REMOVED-WEB-4.png"]


def test_a_deleted_or_ai_cut_out_is_not_read_and_the_gallery_pick_wins():
    media = [m("FRONT", "BG_REMOVED", position=7), m("FRONT", "BG_REMOVED", position=3),
             m("FRONT", "BG_REMOVED", position=1, deletedAt="2026-10-01"),
             m("BACK", "BG_REMOVED", "AI", position=0)]
    assert cg.photos(media) == ["https://cdn/FRONT-BG_REMOVED-WEB-3.png"]


def test_no_cut_out_is_unavailable_and_never_acted_on():
    seen = cg.read([m("FRONT", "RAW")], POL, evidence=FakeEvidence({"garment": "t-shirt"}))
    assert seen.unavailable and not seen.garment
    assert cg.mismatch(seen, category="Backpacks & Bags", subcategory="Bags", pol=POL) is None


def test_boa_007605_t_shirt_filed_under_bags_is_a_mismatch():
    ev = FakeEvidence({"garment": "T-Shirt", "confidence": 0.95})
    seen = cg.read(BOA_007605, POL, evidence=ev)
    assert ev.asked == cg.photos(BOA_007605)
    assert seen.garment == "t-shirt" and not seen.unavailable
    why = cg.mismatch(seen, category="Backpacks & Bags", subcategory="Bags", pol=POL)
    assert why and "cut-outs show t-shirt" in why and "'Bags'" in why


def test_cut_outs_that_agree_with_the_filing_are_no_mismatch():
    seen = cg.CutoutGarment(garment="t-shirt", confidence=0.9)
    assert cg.mismatch(seen, category="T-Shirts & Polos", subcategory="T-Shirt", pol=POL) is None


def test_shorts_filed_as_jeans_is_a_type_mismatch_when_sure():
    sure = cg.CutoutGarment(garment="shorts", confidence=0.9)
    unsure = cg.CutoutGarment(garment="shorts", confidence=0.3)
    assert cg.mismatch(sure, category="Bottoms", subcategory="Jeans", pol=POL)
    assert cg.mismatch(unsure, category="Bottoms", subcategory="Jeans", pol=POL) is None


def test_provider_failure_is_unavailable():
    seen = cg.read(BOA_007605, POL, evidence=FakeEvidence(None))
    assert seen.unavailable and "vision provider" in seen.error


def test_bag_words_reach_the_word_a_tenant_tree_uses():
    assert [cg.canonical(x) for x in ("Handbag", "leather tote bag", "Purse", "Rucksack")] \
        == ["bag", "bag", "bag", "backpack"]
    assert cg.canonical(" T-Shirt ") == "t-shirt"


def test_boa_007605_handbag_cut_outs_against_a_t_shirt_filing():
    # What production showed: renders drawn as a t-shirt, cut-outs a handbag.
    seen = cg.read(BOA_007605, POL, evidence=FakeEvidence({"garment": "handbag", "confidence": 0.98}))
    assert seen.garment == "bag"
    assert cg.mismatch(seen, category="T-Shirts & Polos", subcategory="T-Shirt", pol=POL)
    assert cg.mismatch(seen, category="Backpacks & Bags", subcategory="Bags", pol=POL) is None
