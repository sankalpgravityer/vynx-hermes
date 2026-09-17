"""Readiness phase 1 — the master category is the anchor.

docs/READINESS-PLAN.md §4 steps 1 and 2, cold: which root a product belongs to,
what follows from it (gender, branch, leaf, guide, rig), what the photo audit
now says about the garment's gender, and how the chain and the outcome name a
product no root fits. No database, no model, no subprocess.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import approval, readiness  # noqa: E402
from app.config import policy  # noqa: E402
from app.imaging import photo_audit as pa  # noqa: E402
from app.models import TenantCatalog  # noqa: E402
from app.rules.consistency import check_taxonomy  # noqa: E402
from app.services.auto_approval import outcome  # noqa: E402
from app.vnyx_client import to_snapshot  # noqa: E402

POL = policy()

TREE = {
    "Men": {
        "Jackets": ["Sports Jackets", "Denim Jackets"],
        "Bottoms": ["Jeans"],
        "T-Shirts & Polos": ["T-Shirts"],
        "Shirts": ["T-Shirts", "Dress Shirts"],
    },
    "Women": {
        "Jackets": ["Sports Jackets"],
        "Dresses": ["Midi Dresses"],
        "Bottoms": ["Jeans"],
    },
    "Unisex": {"T-Shirts & Polos": ["T-Shirts"]},
    "Kids": {"Tops": ["T-Shirts"]},
}
GUIDES = {
    "Men Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
    "Men DressShirts": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
    "Women Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["36", "38", "40"]},
    "Men Bottoms": {"sizes": ["W30", "W32"], "euSizes": ["46", "48"]},
    "Defaults": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
}


def catalog(**over: Any) -> dict[str, Any]:
    return {"categories": TREE, "sizingGuides": GUIDES, "brands": ["Zara"],
            "colors": ["Black"], "materials": ["Cotton"], **over}


def raw(**over: Any) -> dict[str, Any]:
    base = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Jackets", "subCategory": "Sports Jackets",
        "size": "M", "euSize": "48", "sizingGuide": "Men Uppers", "brand": "Zara",
        "color": "Black", "material": "Cotton", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1, "mannequinType": "Men Top",
        "title": "Vintage Zara Black Sports Jacket Men M", "summary": "A jacket.",
        "price": "€40", "retailPrice": "€120",
    }
    base.update(over)
    return base


def snap(cat: dict[str, Any] | None = None, **over: Any):
    return to_snapshot(raw(**over), catalog=cat if cat is not None else catalog())


def gate(cat: dict[str, Any] | None = None, **over: Any) -> dict[str, Any]:
    return approval.run_gate(raw(**over), catalog=cat if cat is not None else catalog())


def writes(result: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [a for a in result["repair_plan"]
            if a["kind"] in ("set_column", "set_property") and a.get("field") == field]


def escalations(result: dict[str, Any], field: str) -> list[dict[str, Any]]:
    return [a for a in result["repair_plan"]
            if a["kind"] == "escalate" and a.get("field") == field]


# --------------------------------------------------------------------------- #
# Which root the product belongs to
# --------------------------------------------------------------------------- #

def test_a_valid_root_is_kept():
    d = readiness.decide_master(snap(), POL)
    assert d["action"] == "keep" and d["master"] == "Men"


def test_the_tenants_spelling_of_the_same_root_is_a_repair_not_a_hold():
    d = readiness.decide_master(snap(masterCategory="men"), POL)
    assert d == {"action": "set", "master": "Men", "basis": "tenant spelling",
                 "detail": "'men' is the tenant's 'Men'", "rule": "TAX.001"}


def test_a_blank_master_follows_the_gender_property():
    d = readiness.decide_master(snap(masterCategory=None, gender=["women"]), POL)
    assert d["action"] == "set" and d["master"] == "Women"
    assert d["rule"] == "DATA.010" and d["basis"] == "gender property"


def test_a_blank_master_follows_a_category_that_exists_under_one_root():
    d = readiness.decide_master(
        snap(masterCategory="", gender=None, category="Dresses", subCategory="Midi Dresses"), POL)
    assert d["action"] == "set" and d["master"] == "Women" and d["basis"] == "category path"


def test_a_category_under_two_roots_is_narrowed_by_the_subcategory():
    # Jackets sits under Men and Women; only Men offers Denim Jackets.
    d = readiness.decide_master(
        snap(masterCategory=None, gender=None, category="Jackets", subCategory="Denim Jackets"), POL)
    assert d["action"] == "set" and d["master"] == "Men"


def test_nothing_fits_is_unresolved_and_never_guessed():
    d = readiness.decide_master(
        snap(masterCategory="Ladies", gender=None, category="Jackets", subCategory="Sports Jackets"), POL)
    assert d["action"] == "unresolved" and d["master"] is None and d["rule"] == "TAX.001"
    assert "Ladies" in d["detail"] and "2 roots" in d["detail"]


def test_the_mannequin_is_never_consulted():
    """A Women Top rig on a product with no other evidence does not name a root."""
    d = readiness.decide_master(
        snap(masterCategory=None, gender=None, category="Coats", subCategory=None,
             mannequinType="Women Top"), POL)
    assert d["action"] == "unresolved"


def test_no_catalog_means_no_decision():
    p = to_snapshot(raw(masterCategory="Ladies"))
    assert readiness.decide_master(p, POL)["action"] == "skip"


def test_readiness_can_be_switched_off():
    off = copy.deepcopy(POL)
    off["readiness"]["enabled"] = False
    assert readiness.decide_master(snap(masterCategory="men"), off)["action"] == "skip"
    # TAX.007 stays silent too.
    p = snap(mannequinType=None)
    assert [f.rule_id for f in check_taxonomy(p, off) if f.rule_id == "TAX.007"] == []


# --------------------------------------------------------------------------- #
# The plan, through run_gate
# --------------------------------------------------------------------------- #

def test_a_blank_master_is_planned_and_everything_downstream_is_planned_against_it():
    r = gate(masterCategory=None, gender=["women"], category="Dresses",
             subCategory="Midi Dresses", sizingGuide="Women Uppers", euSize="38",
             mannequinType="Women Top", title="Vintage Zara Black Midi Dress Women M")
    master = writes(r, "masterCategory")
    # `gender` reaches the snapshot as the joined string 'women', not the list.
    assert master == [{"kind": "set_column", "field": "masterCategory", "value": "Women",
                       "reason": "DATA.010", "basis": "gender property",
                       "detail": "gender 'women' names the 'Women' root"}]
    assert escalations(r, "masterCategory") == []
    # The blank master was the only blocker and the plan covers it.
    assert r["verified"] is False and r["plan_covers_blockers"] is True
    assert r["human_intervention_needed"] is False


def test_an_unresolved_master_is_escalated_by_name_and_needs_a_person():
    r = gate(masterCategory="Ladies", gender=None, category="Jackets")
    esc = escalations(r, "masterCategory")
    assert len(esc) == 1 and esc[0]["code"] == "MASTER_CATEGORY_UNRESOLVED"
    assert esc[0]["reason"] == "TAX.001"
    assert r["human_intervention_needed"] is True
    assert writes(r, "masterCategory") == []


def test_gender_follows_a_men_or_women_master():
    r = gate(gender=["women"])                       # master Men, gender women
    assert "TAX.004" in {f["rule_id"] for f in r["blocking"]}
    assert writes(r, "gender") == [{
        "kind": "set_property", "field": "gender", "value": ["men"], "reason": "TAX.004",
        "detail": "follows master category 'Men'; was 'women'",
    }]
    assert r["plan_covers_blockers"] is True


def test_an_absent_gender_is_filled_from_the_master_and_never_from_the_rig():
    r = gate(gender=None, mannequinType="Women Top")
    assert writes(r, "gender") == [{
        "kind": "set_property", "field": "gender", "value": ["men"], "reason": "DATA.010",
        "detail": "follows master category 'Men'",
    }]


def test_a_genderless_root_leaves_the_gender_property_alone():
    # Kids with gender men: no TAX.004 (98 approved Kids products carried one).
    p = snap(masterCategory="Kids", category="Tops", subCategory="T-Shirts",
             gender=["men"], mannequinType="Kids", sizingGuide="Defaults")
    assert [f.rule_id for f in check_taxonomy(p, POL) if f.rule_id == "TAX.004"] == []
    # Unisex with no gender at all: nothing to derive it from, so a person picks.
    r = gate(masterCategory="Unisex", category="T-Shirts & Polos", subCategory="T-Shirts",
             gender=None, mannequinType="Top", sizingGuide="Defaults")
    esc = escalations(r, "gender")
    assert esc and "holds either gender" in esc[0]["detail"]
    assert writes(r, "gender") == []


def test_a_category_spelled_differently_under_the_master_is_a_spelling_repair():
    r = gate(masterCategory="Women", gender=["women"], category="Jacket",
             sizingGuide="Women Uppers", euSize="38", mannequinType="Women Top")
    assert writes(r, "category") == [{
        "kind": "set_column", "field": "category", "value": "Jackets", "reason": "TAX.002",
        "detail": "'Jacket' is spelled 'Jackets' under 'Women'",
    }]


def test_a_category_not_under_the_master_follows_the_branch_that_offers_the_subcategory():
    # Coats is not under Men; Jeans is offered only under Men > Bottoms.
    r = gate(category="Coats", subCategory="Jeans", size="W32", euSize="48",
             sizingGuide="Men Bottoms", mannequinType="Bottom",
             title="Vintage Zara Black Jeans Men W32")
    cat = writes(r, "category")
    assert cat and cat[0]["value"] == "Bottoms" and "offered only under" in cat[0]["detail"]
    # The subcategory is valid under the branch the product is about to be on:
    # no TAX.003 repair, no escalation.
    assert writes(r, "subCategory") == [] and escalations(r, "subCategory") == []


def test_two_branches_offering_the_subcategory_are_settled_by_the_tenants_filing():
    usage = {"Men>T-Shirts & Polos>T-Shirts": 40, "Men>Shirts>T-Shirts": 3}
    r = gate(catalog(branchUsage=usage), category="Tees", subCategory="T-Shirts",
             title="Vintage Zara Black T-Shirt Men M")
    cat = writes(r, "category")
    assert cat and cat[0]["value"] == "T-Shirts & Polos"
    assert "files it (40 products)" in cat[0]["detail"]
    # A near-tie stays a human's.
    tie = {"Men>T-Shirts & Polos>T-Shirts": 4, "Men>Shirts>T-Shirts": 3}
    r2 = gate(catalog(branchUsage=tie), category="Tees", subCategory="T-Shirts",
              title="Vintage Zara Black T-Shirt Men M")
    assert writes(r2, "category") == [] and escalations(r2, "category")


def test_a_subcategory_the_new_branch_never_offered_is_named_by_the_title():
    # Blank master -> Women (gender). Women > Jackets offers only Sports Jackets;
    # the stored 'Blazers' is off-branch and the title says Sports Jacket.
    r = gate(masterCategory=None, gender=["women"], category="Jackets", subCategory="Blazers",
             sizingGuide="Women Uppers", euSize="38", mannequinType="Women Top",
             title="Vintage Zara Black Sports Jacket Women M")
    assert writes(r, "masterCategory")[0]["value"] == "Women"
    sub = writes(r, "subCategory")
    assert sub == [{
        "kind": "set_column", "field": "subCategory", "value": "Sports Jackets",
        "reason": "TAX.003",
        "detail": ("'Blazers' is not offered under 'Women > Jackets'; the title names it, "
                   "and 'Sports Jackets' is the only option on this branch that it matches"),
    }]


# --------------------------------------------------------------------------- #
# The rig: derived, never read
# --------------------------------------------------------------------------- #

def test_derive_rig_speaks_the_edit_screens_vocabulary():
    d = readiness.derive_rig
    assert d("Kids", ["men"], "Tops", "T-Shirts", POL) == "Kids"
    assert d("Men", ["men"], "Bottoms", "Jeans", POL) == "Bottom"       # no gender word
    assert d("Women", None, "Jackets", "Sports Jackets", POL) == "Women Top"
    assert d("Men", None, "Jackets", None, POL) == "Men Top"
    assert d("Unisex", ["women"], "T-Shirts & Polos", "T-Shirts", POL) == "Women Top"
    assert d("Unisex", None, "T-Shirts & Polos", "T-Shirts", POL) == "Top"
    assert d(None, None, None, None, POL) is None


def test_a_wrong_gender_rig_is_replaced_and_the_master_is_never_moved_to_it():
    r = gate(mannequinType="Women Top")
    tax005 = [f for f in r["blocking"] if f["rule_id"] == "TAX.005"]
    assert tax005 and tax005[0]["detail"]["anchor"] == "master_category"
    assert tax005[0]["detail"]["suggested"] == "Men Top"
    assert "The master category is the anchor" in tax005[0]["message"]
    assert writes(r, "mannequinType") == [{
        "kind": "set_column", "field": "mannequinType", "value": "Men Top",
        "reason": "TAX.005", "detail": "was 'Women Top'",
    }]
    # THE FLIP: nothing about the rig reaches the master or the gender.
    assert writes(r, "masterCategory") == [] and writes(r, "gender") == []
    assert r["plan_covers_blockers"] is True


def test_a_missing_rig_is_derived_and_written():
    r = gate(mannequinType=None)
    tax007 = [f for f in r["blocking"] if f["rule_id"] == "TAX.007"]
    assert tax007 and tax007[0]["detail"]["suggested"] == "Men Top"
    assert writes(r, "mannequinType") == [{
        "kind": "set_column", "field": "mannequinType", "value": "Men Top",
        "reason": "TAX.007", "detail": "no rig was selected",
    }]
    assert r["plan_covers_blockers"] is True


def test_a_bottom_rig_on_bottoms_is_accepted_by_both_forms_of_tax005():
    p = snap(category="Bottoms", subCategory="Jeans", size="W32", euSize="48",
             sizingGuide="Men Bottoms", mannequinType="Bottom")
    assert [f.rule_id for f in check_taxonomy(p, POL) if f.rule_id.startswith("TAX.00")
            and f.rule_id in ("TAX.005", "TAX.007")] == []


def test_the_rig_follows_the_gender_property_under_a_genderless_root():
    p = snap(masterCategory="Unisex", category="T-Shirts & Polos", subCategory="T-Shirts",
             gender=["women"], mannequinType="Men Top", sizingGuide="Defaults")
    found = [f for f in check_taxonomy(p, POL) if f.rule_id == "TAX.005"]
    assert found and found[0].detail["suggested"] == "Women Top"
    assert "men's rig on a women's product" in found[0].message


# --------------------------------------------------------------------------- #
# The guide: the tenant's filing breaks a tie
# --------------------------------------------------------------------------- #

def test_two_general_charts_are_settled_by_where_the_tenant_files_this_branch():
    p = snap(catalog(guideUsage={"Men>Jackets>Men Uppers": 50, "Men>Jackets>Men Knitwear": 4}))
    assert approval._pick_guide(p, ["Men Uppers", "Men Knitwear"]) == "Men Uppers"
    tie = snap(catalog(guideUsage={"Men>Jackets>Men Uppers": 5, "Men>Jackets>Men Knitwear": 4}))
    assert approval._pick_guide(tie, ["Men Uppers", "Men Knitwear"]) is None


def test_usage_helpers_slice_the_tenants_tables():
    branch = {"Men>Jackets>Sports Jackets": 10, "Men>Bottoms>Jeans": 7,
              "Women>Jackets>Sports Jackets": 3, "Men>Jackets>Denim Jackets": 2}
    assert readiness.branch_counts(branch, "Men", subcategory="Sports Jacket") == {"Jackets": 10}
    assert readiness.branch_counts(branch, "Men", category="Jackets") == {
        "Sports Jackets": 10, "Denim Jackets": 2}
    guides = {"Men>Jackets>Men Uppers": 50, "Men>Jackets>Men DressShirts": 5, "Women>Jackets>Women Uppers": 9}
    assert readiness.guide_counts(guides, "men", "jackets") == {"Men Uppers": 50, "Men DressShirts": 5}
    assert readiness.usage_pick({"A": 10, "B": 4}, ["A", "B"], POL) == "A"
    assert readiness.usage_pick({"A": 7, "B": 4}, ["A", "B"], POL) is None
    assert readiness.usage_pick({}, ["A", "B"], POL) is None


def test_the_catalog_carries_the_two_new_tables_under_their_feed_names():
    c = TenantCatalog(**{"categories": TREE, "branchUsage": {"Men>Jackets>Sports Jackets": 1},
                         "guideUsage": {"Men>Jackets>Men Uppers": 2}})
    assert c.branch_usage == {"Men>Jackets>Sports Jackets": 1}
    assert c.guide_usage == {"Men>Jackets>Men Uppers": 2}
    from app import product_audit
    assert "GROUP BY 1, 2, 3" in product_audit.BRANCH_USAGE_SQL
    assert '"sizingGuide"' in product_audit.GUIDE_USAGE_SQL


# --------------------------------------------------------------------------- #
# The photo audit: the garment's gender against the anchor
# --------------------------------------------------------------------------- #

PHOTOS = [{"url": "https://r2/front.png", "view": "FRONT", "processing": "BG_REMOVED",
           "kind": "photo", "expect": "a cut-out"},
          {"url": "https://r2/ai.jpg", "view": "AI_FRONT", "processing": "GENERATED",
           "kind": "render", "expect": "a render"}]


def _raw(**over: Any) -> dict[str, Any]:
    base = {"wear": "none", "defects": [], "wear_confidence": 0.9,
            "images": [{"index": 1, "ok": True, "issue": ""}, {"index": 2, "ok": True, "issue": ""}],
            "garment_gender": "women", "garment_type": "midi dress", "garment_confidence": 0.92}
    base.update(over)
    return base


def test_the_schema_asks_about_the_garment_not_the_render():
    for key in ("garment_gender", "garment_type", "garment_confidence"):
        assert key in pa.SCHEMA["properties"] and key in pa.SCHEMA["required"]
    assert "never from the person in a render" in pa.SYSTEM


def test_a_contradiction_is_a_soft_flag_by_default():
    v = pa.decide(_raw(), images=PHOTOS, grade_severity="none", grade_label="A",
                  pol=POL, product_gender="men")
    assert v.action == "ok" and v.code is None
    assert v.soft == ["MASTER CATEGORY — the garment photographs look like a women's midi dress "
                      "but the master category says men"]


def test_a_contradiction_holds_when_policy_says_so():
    hold = copy.deepcopy(POL)
    hold["readiness"]["master"]["photo_check"] = "hold"
    v = pa.decide(_raw(), images=PHOTOS, grade_severity="none", grade_label="A",
                  pol=hold, product_gender="men")
    assert v.action == "review" and v.code == "MASTER_CATEGORY_MISMATCH"
    assert v.reasons[0].startswith("MASTER CATEGORY — ")


@pytest.mark.parametrize("raw_over, product_gender", [
    ({"garment_gender": "unisex"}, "men"),               # could be either
    ({"garment_gender": "unknown"}, "men"),
    ({"garment_gender": "women", "garment_confidence": 0.6}, "men"),  # below the floor
    ({"garment_gender": "women"}, None),                 # Unisex / Kids master
    ({"garment_gender": "men"}, "men"),                  # agrees
])
def test_only_a_confident_plain_contradiction_counts(raw_over, product_gender):
    v = pa.decide(_raw(**raw_over), images=PHOTOS, grade_severity="none", grade_label="A",
                  pol=POL, product_gender=product_gender)
    assert not any(s.startswith("MASTER CATEGORY") for s in v.soft) and v.action == "ok"


def test_renders_alone_never_vote_on_the_master():
    renders_only = [PHOTOS[1]]
    v = pa.decide(_raw(), images=renders_only, grade_severity="none", grade_label="A",
                  pol=POL, product_gender="men")
    assert not any(s.startswith("MASTER CATEGORY") for s in v.soft)


# --------------------------------------------------------------------------- #
# The outcome and the chain
# --------------------------------------------------------------------------- #

def _result(**over: Any) -> dict[str, Any]:
    base = {"approval": {"outcome": "skipped_preflight", "problems": ["no master category"]},
            "remaining": ["DATA.010"], "verified": False,
            "master": {"action": "unresolved", "detail": "no master category; the gender property is empty"},
            "master_unresolved": True}
    base.update(over)
    return base


def test_an_unresolved_master_is_the_named_cause_of_the_hold():
    v = outcome.classify(_result())
    assert v.status == "HELD_FOR_HUMAN" and v.outcome == "MASTER_CATEGORY_UNRESOLVED"
    assert "no master category" in v.reason and v.retryable is False


def test_the_master_code_yields_to_a_move_and_to_a_resolved_master():
    assert outcome.classify(_result(approval={"outcome": "approved"})).outcome == "APPROVED"
    v = outcome.classify(_result(master_unresolved=False, remaining=["SIZE.010"]))
    assert v.outcome == "SIZE.010"


def test_the_chain_prints_the_anchor_first_and_reports_it(monkeypatch):
    from scripts import repair_product as rp
    from app.imaging import quality_gate as qg
    from app.imaging.quality_gate import GateVerdict

    loaded = {"record": raw(masterCategory="men", updatedAt=None),
              "media": [{"url": "https://x/f.png", "view": "AI_FRONT", "mediaType": "IMAGE",
                         "processing": "GENERATED", "position": 1}],
              "catalog": catalog(), "imagery_settings": None}
    state = {"loaded": loaded, "title": "T", "tenant": "T", "sku": "S1", "stage": "REVIEW",
             "review_status": "PENDING", "edit_url": None, "description_missing": False,
             "description_chars": 9, "care_label": 1, "unmatted": 0, "unmatted_views": [],
             "leftover_raw": 0, "renders": 5, "render_rows": 5, "renders_missing": 0,
             "generation_status": "COMPLETE", "is_regenerating": False, "attributes_missing": []}
    monkeypatch.setattr(rp, "needs", lambda dsn, pid: state)
    monkeypatch.setattr(rp, "run_step", lambda *a, **k: (True, "", {"ok": True, "applied": [], "failed": []}))
    monkeypatch.setattr(rp, "approve_check",
                        lambda *a, **k: {"outcome": "skipped_preflight", "problems": ["no master category"]})
    monkeypatch.setattr(qg, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    remaining = [{"rule_id": "TAX.001", "fields": ["master_category"], "severity": "high"}]
    monkeypatch.setattr(rp.product_audit, "audit",
                        lambda *a, **k: {"verified_after": False, "remaining": remaining,
                                         "counts": {"issues": 1}})

    r = rp.repair("postgresql://test", "00000000-0000-0000-0000-000000000001", apply=False,
                  vnyx_api=Path("."), infer=False, min_confidence=70, skip_render=True,
                  approve=False, skip_bin=True, quiet=True, silent=True)
    names = [s["step"] for s in r["steps"]]
    assert names[:2] == ["twin", "master"]
    master_step = r["steps"][1]
    assert master_step["ran"] and master_step["ok"]
    assert "'men'" in master_step["note"] and "'Men'" in master_step["note"]
    assert "reconcile writes it" in master_step["note"]
    assert r["master"]["action"] == "set" and r["master"]["master"] == "Men"
    # TAX.001 survived reconcile in this fake run, so the anchor is unresolved.
    assert r["master_unresolved"] is True
    assert outcome.classify(r).outcome == "MASTER_CATEGORY_UNRESOLVED"


def test_the_fixture_runner_lists_the_master_step_only_when_it_would_act(tmp_path):
    import json
    from scripts import repair_product as rp

    doc = {"record": raw(masterCategory="men"), "media": [], "catalog": catalog(),
           "grade_ladder": [], "imagery_settings": None, "chart": {}}
    p = tmp_path / "f.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    r = rp.run_fixture(p)
    assert r["would_run"][0] == "master" and r["master"]["action"] == "set"

    doc["record"]["masterCategory"] = "Men"
    p.write_text(json.dumps(doc), encoding="utf-8")
    assert "master" not in rp.run_fixture(p)["would_run"]
