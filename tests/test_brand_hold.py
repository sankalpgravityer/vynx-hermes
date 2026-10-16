"""A brand that says "no brand" is held, never approved (16 Oct 2026).

DATA.011 fired on 107 of 111 verified "NO BRAND" products and they were approved
anyway — approve-products.ts only asks whether a brand is present. And "Unbranded"
(108 products), a tenant code standing alone ("KK", "MT") and a letterless "'"
were not recognised at all.
"""
from __future__ import annotations

import pytest

from app.config import policy
from app.rules import gate
from app.services.auto_approval import outcome
from app.vnyx_client import to_snapshot
from scripts import repair_product as rp


def snap(brand):
    return to_snapshot({"id": "p1", "tenantId": "t1", "sku": "S1", "masterCategory": "Men",
                        "category": "T-Shirts & Polos", "subCategory": "T-Shirt", "brand": brand},
                       catalog={})


@pytest.mark.parametrize("brand", ["Unbranded", "UNBRANDED", "KK", "MT", "'", "-",
                                   "NO BRAND", "MT NO BRAND", "MK No Brand", "No-Brand", "NOBRAND",
                                   "Unknown", "None"])
def test_placeholder_brands_are_named(brand):
    assert gate.no_brand_values(snap(brand), policy()) == [brand]


@pytest.mark.parametrize("brand", ["Levi's", "Lee", "MK", "H&M", "MNG", "MCM", "Y-3", "Brand New Day", "Mt. Hood Outfitters"])
def test_real_brands_are_not(brand):
    assert gate.no_brand_values(snap(brand), policy()) == []


def test_an_empty_brand_is_data_010s_not_this_rule():
    assert gate.no_brand_values(snap(""), policy()) == []


def test_data_011_still_blocking_holds_as_no_brand():
    gate_answer = {"blocking": [{"rule_id": "DATA.011", "message": 'Brand is missing — stored as "Unbranded"'}]}
    v = rp.record_brand(gate_answer)
    assert v.blocks and v.code == "NO_BRAND"
    assert v.reasons[0].startswith("DATA.011")


def test_nothing_to_hold_without_data_011():
    assert not rp.record_brand({"blocking": [{"rule_id": "SIZE.011", "message": "x"}], "advisory": []}).blocks
    # the Brain's brand check off → DATA.011 never reaches `blocking`
    assert not rp.record_brand({"blocking": [], "advisory": []}).blocks


def test_the_hold_reads_as_a_record_refusal_and_its_code():
    assert rp.BRAND_LABEL in rp.RECORD_LABELS
    v = outcome.classify({"approval": {"outcome": "gate_blocked", "gate_code": "NO_BRAND",
                                       "blockers": ["brand missing: DATA.011 Brand is missing"]}})
    assert (v.status, v.outcome, v.approved) == ("HELD_FOR_HUMAN", "NO_BRAND", False)
    assert outcome._preflight_code(["brand missing: DATA.011 Brand is missing"]) == "NO_BRAND"
