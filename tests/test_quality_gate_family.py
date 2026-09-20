"""The gate's category ↔ picture check (the auditor's CATEGORY_IMAGE_MISMATCH).

Fires only when the garment the model names and the category the product is
filed under map to DIFFERENT families that are not adjacent. Everything
uncertain decides nothing — that is the whole design.
"""
from __future__ import annotations

from app.config import policy
from app.imaging import quality_gate as qg

POL = policy()

GOOD = {"model_present": True, "face_ok": True, "gender": "Men", "lead_ok": True,
        "body_coherent": True, "body_issue": "", "view": "front", "confidence": 0.9}


def decide(garment, *, subcategory=None, category=None, pol=POL):
    return qg.decide({**GOOD, "garment": garment}, product_gender="men",
                     accessory=False, pol=pol, category=category, subcategory=subcategory)


def test_family_lookup_uses_word_stems_and_stays_silent_when_unsure():
    assert qg.garment_family("puffer jacket", POL) == "outerwear"
    assert qg.garment_family("Jeans", POL) == "bottoms"
    assert qg.garment_family("T-Shirts & Polos", POL) == "tops"
    assert qg.garment_family("Denim Vest", POL) is None      # denim (bottoms) + vest (tops)
    assert qg.garment_family("thing", POL) is None
    assert qg.garment_family("", POL) is None
    assert qg.garment_family("sweatshirt", POL) == "tops"     # not matched by 'shirt' alone


def test_a_gown_filed_under_denim_vest_is_a_mismatch():
    # BOA-006114: a red gown rendered on a product filed under Men > Vests.
    v = decide("evening gown", subcategory="Vests", category="Vests")
    assert v.action == "review" and v.blocks
    assert v.code == "CATEGORY_IMAGE_MISMATCH"
    assert "gown" in v.reasons[0] and "Vests" in v.reasons[0]
    assert v.unavailable is False


def test_jeans_filed_under_jeans_pass():
    assert decide("jeans", subcategory="Jeans", category="Bottoms").action == "ok"


def test_adjacent_families_are_not_a_finding():
    # The model calls a shirt-jacket a shirt; tops vs outerwear is conflated on purpose.
    assert decide("shirt", subcategory="Jackets", category="Jackets").action == "ok"
    assert decide("puffer jacket", subcategory="Sweaters", category="Tops").action == "ok"


def test_a_dress_on_a_t_shirt_listing_is_not_adjacent():
    v = decide("dress", subcategory="T-Shirts", category="Tops")
    assert v.code == "CATEGORY_IMAGE_MISMATCH"


def test_unknown_garment_or_category_decides_nothing():
    assert decide("", subcategory="Jeans").action == "ok"
    assert decide("garment", subcategory="Jeans").action == "ok"
    assert decide("jeans", subcategory="Novelty", category="Misc").action == "ok"


def test_render_defects_still_come_first():
    v = qg.decide({**GOOD, "garment": "gown", "face_ok": False}, product_gender="men",
                  accessory=False, pol=POL, subcategory="Jeans")
    assert v.code == "IMAGE_QUALITY"


def test_category_check_can_be_switched_off():
    pol = {**POL, "quality_gate": {**POL["quality_gate"],
                                    "block_on": ["no_model", "bad_face", "broken_body", "gender_mismatch"]}}
    assert decide("gown", subcategory="Jeans", pol=pol).action == "ok"


def test_schema_asks_for_the_garment():
    assert "garment" in qg.SCHEMA["properties"]
    assert "garment" in qg.SCHEMA["required"]
