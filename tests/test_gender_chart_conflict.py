"""Size chart / gender conflicts (13 Oct 2026, VIN-000010 and VIN-000014).

Two Vinted products went live as Women products on "Men Uppers", with their
`properties.mastercategory` still "Men" and a men's EU size:

  * reconcile wrote nothing. vnyx-api's catalog carries no guide_usage, and
    SIZE.011 suggested "Women Uppers" AND "Women Bottoms" (both list M), so the
    planner escalated "2 charts fit; pick one". SIZE.011 now side-matches its
    suggestions, as SIZE.014 already did.
  * the master-category copies disagreed with both sides real, which DRIFT.001
    left to a person. The product's own gender now breaks that tie — towards the
    column only.
  * approve-products.ts checks that fields are present, not that they agree, so
    the agent approved them. Whatever of the gender/chart/rig rules still blocks
    after settle now holds the product: GENDER_CHART_CONFLICT.
"""
from __future__ import annotations

from app import approval, product_audit
from app.config import policy
from app.imaging import cutout_integrity as ci
from app.rules import gate
from app.services.auto_approval import outcome
from app.vnyx_client import to_snapshot
from scripts import repair_product as rp

TREE = {
    "Men": {"T-Shirts & Polos": ["T-Shirt", "Polo Shirt"]},
    "Women": {"T-Shirts & Tops": ["T-Shirts", "Tops"], "Bottoms": ["Jeans"]},
}
GUIDES = {
    "Men Uppers": {"sizes": ["S", "M", "L", "XL"], "euSizes": ["46", "48", "50", "52"]},
    "Women Uppers": {"sizes": ["XS", "S", "M", "L", "XL"], "euSizes": ["34", "36", "38", "40", "42"]},
    # A bottoms chart in letter sizes: by ladder alone it fits a women's M tee too.
    "Women Bottoms": {"sizes": ["XS", "S", "M", "L"], "euSizes": ["34", "36", "38", "40"]},
}
# What vnyx-api sends: the tree and the charts, and NO usage statistics.
CATALOG = {"categories": TREE, "sizingGuides": GUIDES, "brands": ["Urban Classics"],
           "colors": ["Mustard"], "materials": ["Cotton"]}


def raw(properties=None, columns=None, **over):
    """VIN-000010 as it stood after the run."""
    return {
        "id": "p1", "tenantId": "t1", "sku": "VIN-000010", "productCode": "PC1",
        "masterCategory": "Women", "category": "T-Shirts & Tops", "subCategory": "T-Shirts",
        "size": "M", "internationalSize": "M", "euSize": "48", "sizingGuide": "Men Uppers",
        "mannequinType": "Women Top", "brand": "Urban Classics", "color": "Mustard",
        "material": "Cotton", "condition": "As New", "gender": ["women"], "careLabelCount": 1,
        "title": "Urban Classics T-Shirt in Mustard size M", "description": "A tee.",
        "properties": {"mastercategory": "Men", **(properties or {})},
        "columnValues": {"masterCategory": "Women", **(columns or {})},
        **over,
    }


def run(r):
    return approval.run_gate(r, catalog=CATALOG, llm=None)


def plan_of(out):
    return {a["field"]: a["value"] for a in out["repair_plan"]
            if a["kind"] in ("set_column", "set_property")}


# --------------------------------------------------------------------------- #
# SIZE.011 — the suggestions are side-matched
# --------------------------------------------------------------------------- #

def test_size_011_suggests_only_the_tops_chart():
    size011 = next(f for f in run(raw())["blocking"] if f["rule_id"] == "SIZE.011")
    assert size011["detail"]["suggested"] == ["Women Uppers"]
    # by ladder alone the bottoms chart fits too — the side is what rules it out
    p = to_snapshot(raw(), catalog=CATALOG)
    assert "Women Bottoms" in gate.candidate_guides(p)
    assert gate.candidate_guides(p, pol=policy()) == ["Women Uppers"]


def test_reconcile_switches_the_chart_without_usage_statistics():
    """The VIN-000010 plan, on vnyx-api's catalog: a write, not an escalation."""
    out = run(raw())
    plan = plan_of(out)
    assert plan["sizingGuide"] == "Women Uppers"
    assert plan["eu_size"] == "38"          # re-read off the women's chart
    assert not [a for a in out["repair_plan"]
                if a["kind"] == "escalate" and a.get("field") == "sizingGuide"]


# --------------------------------------------------------------------------- #
# DRIFT.001 — the gender breaks a master-category tie, towards the column only
# --------------------------------------------------------------------------- #

def drift(r):
    return [f for f in gate.check_column_drift(to_snapshot(r, catalog=CATALOG))
            if f.rule_id == "DRIFT.001"]


def test_gender_agreeing_with_the_column_repairs_the_property():
    [f] = drift(raw())
    assert f.detail["repair_to"] == "Women"
    assert f.detail["repair_side"] == "properties"
    assert f.severity.value == "medium"


def test_the_repair_names_the_stored_key():
    """vnyx-api writes `properties[field]` verbatim: `master_category` would be a
    second key beside `mastercategory`, and the drift would never close."""
    assert plan_of(run(raw()))["mastercategory"] == "Women"
    assert "master_category" not in plan_of(run(raw()))
    # Hermes' own executor takes the stored key too.
    assert product_audit._PROPERTY_KEYS["mastercategory"] == "mastercategory"
    assert product_audit._PROPERTY_KEYS["sub_category"] == "sub_category"


def test_gender_agreeing_with_the_property_never_moves_the_column():
    """That is a gender/master conflict — TAX.004's, settled by the render."""
    [f] = drift(raw(gender=["men"]))
    assert f.detail["repair_to"] is None


def test_no_single_gender_leaves_it_to_a_person():
    for g in (["men", "women"], [], None):
        [f] = drift(raw(gender=g))
        assert f.detail["repair_to"] is None, g


def test_only_the_master_category_is_tie_broken():
    found = [f for f in drift(raw(subCategory="Tops", properties={"sub_category": "T-Shirts"},
                                  columns={"subCategory": "Tops"}))
             if f.fields == ["subcategory"]]
    assert found and found[0].detail["repair_to"] is None


# --------------------------------------------------------------------------- #
# the hold after settle
# --------------------------------------------------------------------------- #

def test_conflicts_name_the_chart_and_the_master_drift():
    c = rp.record_conflicts(run(raw()))
    assert c.blocks and c.code == "GENDER_CHART_CONFLICT"
    assert [r.split()[0] for r in c.reasons] == ["SIZE.011", "DRIFT.001"]


def test_a_dry_run_leaves_out_what_its_plan_would_write():
    out = run(raw())
    planned = frozenset(a.get("reason") for a in out["repair_plan"]
                        if a.get("kind") in ("set_column", "set_property", "create_size_chart"))
    assert not rp.record_conflicts(out, skip=planned).blocks


def test_a_record_that_agrees_with_itself_does_not_hold():
    fixed = raw(sizingGuide="Women Uppers", euSize="38",
                properties={"mastercategory": "Women", "eu_size": "38"})
    assert not rp.record_conflicts(run(fixed)).blocks


def test_a_rule_the_brain_switched_off_does_not_hold():
    out = approval.run_gate(raw(properties={"mastercategory": "Women"}), catalog=CATALOG,
                            llm=None, severity_overrides={"SIZE.011": "off"})
    assert not rp.record_conflicts(out).blocks


def test_the_hold_code_when_the_preflight_also_refused():
    assert outcome._preflight_code(
        ["size chart / gender conflict: SIZE.011 Sizing guide 'Men Uppers' is a men's chart"]
    ) == "GENDER_CHART_CONFLICT"


def test_the_hold_code_when_only_the_record_refused():
    v = outcome.classify({"approval": {"outcome": "gate_blocked",
                                       "gate_code": "GENDER_CHART_CONFLICT",
                                       "blockers": ["size chart / gender conflict: SIZE.011 ..."]}})
    assert (v.status, v.outcome, v.approved) == ("HELD_FOR_HUMAN", "GENDER_CHART_CONFLICT", False)


# --------------------------------------------------------------------------- #
# VIN-000009 — the form's shoulders at a sleeveless top's armholes
# --------------------------------------------------------------------------- #

def test_sleeveless_shoulders_are_not_a_cutout_defect():
    ask = ci._ASK
    assert "shoulders or upper arms showing at the armholes of a sleeveless" in ask
    not_defects = ask.split("These are NOT defects")[1]
    assert "sleeveless" in not_defects
    # still background: the stand, and the form below the hem
    background = ask.split("background   —")[1].split("colour")[0]
    assert "stand" in background and "below" in background and "beside the garment" not in background
