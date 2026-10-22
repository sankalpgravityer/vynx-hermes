"""A title that names another garment than the category is held, never approved.

22 Oct 2026, the user's call: BOA-007605, a handbag filed under Bags, was
approved and published as "Vintage TAVECCHI Leather T-Shirt" — the copy step had
been withheld and TEXT.008 was only advisory.
"""
from __future__ import annotations

from app.config import policy
from app.rules.consistency import check_copy
from app.services.auto_approval import outcome
from app.vnyx_client import to_snapshot
from scripts import repair_product as rp

GARMENT = {"rule_id": "TEXT.008", "fields": ["title", "subcategory"],
           "message": "Title reads as tops but the product is filed under 'Bags' (accessories)."}
SIZE = {"rule_id": "TEXT.008", "fields": ["title", "size"],
        "message": "Title ends in size 'XL' but the size field says 'M'."}


def test_boa_007605_title_is_the_garment_form_of_text_008():
    p = to_snapshot({"id": "p1", "tenantId": "t1", "sku": "BOA-007605", "masterCategory": "Men",
                     "category": "Backpacks & Bags", "subCategory": "Bags",
                     "title": "Vintage TAVECCHI Leather T-Shirt", "description": "x"}, catalog={})
    found = [f for f in check_copy(p, policy()) if f.rule_id == "TEXT.008"]
    assert found and "subcategory" in found[0].fields


def test_the_garment_form_holds_whether_blocking_or_advisory():
    for where in ("blocking", "advisory"):
        v = rp.record_title({where: [GARMENT]})
        assert v.blocks and v.code == "TITLE_CATEGORY_MISMATCH"
        assert v.reasons[0].startswith("TEXT.008 Title reads as tops")


def test_the_size_form_and_other_rules_do_not_hold():
    assert not rp.record_title({"advisory": [SIZE]}).blocks
    assert not rp.record_title({"blocking": [{"rule_id": "TEXT.002", "fields": ["title"]}]}).blocks
    # the Brain's TEXT.008 off → it never reaches the answer
    assert not rp.record_title({"blocking": [], "advisory": []}).blocks


def test_the_hold_reads_as_a_record_refusal_and_its_code():
    assert rp.TITLE_LABEL in rp.RECORD_LABELS
    line = "title vs category: TEXT.008 Title reads as tops but the product is filed under 'Bags'"
    v = outcome.classify({"approval": {"outcome": "gate_blocked", "gate_code": "TITLE_CATEGORY_MISMATCH",
                                       "blockers": [line]}})
    assert (v.status, v.outcome, v.approved) == ("HELD_FOR_HUMAN", "TITLE_CATEGORY_MISMATCH", False)
    assert outcome._preflight_code([line]) == "TITLE_CATEGORY_MISMATCH"
