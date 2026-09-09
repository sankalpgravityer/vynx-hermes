"""DRIFT.001 and the subcategory near-match repair.

Both were written against one real product on vnyx-dev-2
(3fddf03b-09ef-4e77-a517-a83b36e72f5f), which carried exactly these two faults
and which every other rule called clean:

  internationalSize = 'Unknown'   properties.international_size = 'S'
  subCategory       = 'Leather Jackets'   tenant tree: 'Leather Jacket'

The first is invisible to every existing rule because PROPERTY_ALIASES resolves a
PRECEDENCE between the two copies — the resolved size is 'S', so the size rules
pass, and the product export (which reads the column) publishes with no size.

The second is why a reviewer sees an empty sub-category dropdown on a product
whose sub-category is not empty: the value is stored, it is simply not one the
branch offers.

The third test here is the one that matters most. Repairing ONE side of a
doubled value converts the defect instead of clearing it — the first working
version of this did precisely that, and turned a TAX.003 into a DRIFT.001.
"""

from __future__ import annotations

from app.config import policy
from app.rules import gate
from app.vnyx_client import to_snapshot
from app import approval
from app import product_audit

TREE = {"Men": {"Jackets": ["Leather Jacket", "Denim Jacket", "Puffer Jacket"]}}
GUIDES = {"Men Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]}}

CATALOG = {
    "categories": TREE,
    "sizingGuides": GUIDES,
    "brands": ["Zara"],
    "colors": ["Black"],
    "materials": ["Leather"],
}


def snap(*, properties=None, columns=None, **over):
    raw = {
        "id": "p1",
        "tenantId": "t1",
        "sku": "S1",
        "productCode": "PC1",
        "masterCategory": "Men",
        "category": "Jackets",
        "subCategory": "Leather Jacket",
        "size": "S",
        "euSize": "46",
        "sizingGuide": "Men Uppers",
        "brand": "Zara",
        "color": "Black",
        "material": "Leather",
        "condition": "As New",
        "gender": ["men"],
        "careLabelCount": 1,
        "properties": properties if properties is not None else {},
        "columnValues": columns if columns is not None else {},
        **over,
    }
    return to_snapshot(raw, catalog=CATALOG)


def drift(p):
    return [f for f in gate.check_column_drift(p) if f.rule_id == "DRIFT.001"]


# --------------------------------------------------------------------------- #
# DRIFT.001
# --------------------------------------------------------------------------- #

def test_silent_without_column_values():
    """The same contract `catalog` and `media` keep — absent means unjudged.

    A caller that never sent the columns must not have every product reported as
    drifting; there is nothing to compare against.
    """
    p = snap(properties={"international_size": "S"})
    assert drift(p) == []


def test_placeholder_column_against_a_real_property():
    p = snap(
        columns={"internationalSize": "Unknown"},
        properties={"international_size": "S"},
    )
    found = drift(p)
    assert len(found) == 1
    assert found[0].severity.value == "high"
    assert found[0].detail["repair_to"] == "S"
    assert found[0].detail["repair_side"] == "column"


def test_placeholder_property_against_a_real_column():
    """The mirror image, and the repair has to go the other way."""
    p = snap(
        columns={"internationalSize": "M"},
        properties={"international_size": "-"},
    )
    found = drift(p)
    assert len(found) == 1
    assert found[0].detail["repair_to"] == "M"
    assert found[0].detail["repair_side"] == "properties"


def test_two_real_values_is_medium_and_unrepairable():
    """No arithmetic settles which of two real values is right."""
    p = snap(
        columns={"internationalSize": "M"},
        properties={"international_size": "S"},
    )
    found = drift(p)
    assert len(found) == 1
    assert found[0].severity.value == "medium"
    assert found[0].detail["repair_to"] is None


def test_both_empty_is_not_drift():
    """Missing is DATA.010's job. Reporting it here too would double-count."""
    p = snap(
        columns={"internationalSize": "Unknown"},
        properties={"international_size": ""},
    )
    assert drift(p) == []


def test_spacing_and_case_are_not_drift():
    p = snap(
        columns={"subCategory": "Leather Jacket"},
        properties={"sub_category": "leather  jacket"},
    )
    assert drift(p) == []


# --------------------------------------------------------------------------- #
# TAX.003 — stored but not offered
# --------------------------------------------------------------------------- #

def _plan_for(p):
    findings = approval._all_findings(p, policy())
    plan: list[dict] = []
    approval._plan_subcategory(p, findings, plan)
    return plan


def test_plural_subcategory_is_repaired_to_the_tenants_spelling():
    p = snap(subCategory="Leather Jackets")
    assert any(f.rule_id == "TAX.003" for f in approval._all_findings(p, policy()))
    plan = _plan_for(p)
    assert plan == [
        {
            "kind": "set_column",
            "field": "subCategory",
            "value": "Leather Jacket",
            "reason": "TAX.003",
            "detail": plan[0]["detail"],
        }
    ]


def test_an_unrelated_subcategory_is_never_guessed():
    """'Windbreaker' is not on this branch and matches nothing on it.

    Escalating is right; picking the alphabetically-first entry would be a
    plausible-looking wrong answer nobody would catch.
    """
    p = snap(subCategory="Windbreaker")
    assert _plan_for(p) == []


def test_ambiguity_is_left_alone():
    """Two entries normalising to the same value means a distinction we can't see."""
    p = to_snapshot(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men", "category": "Jackets",
            "subCategory": "Vests", "size": "S", "euSize": "46",
            "sizingGuide": "Men Uppers", "brand": "Zara", "color": "Black",
            "material": "Leather", "condition": "As New", "gender": ["men"],
            "careLabelCount": 1,
        },
        catalog={**CATALOG,
                 "categories": {"Men": {"Jackets": ["Vest", "Vests "]}}},
    )
    assert _plan_for(p) == []


def test_a_correct_subcategory_is_left_alone():
    p = snap(subCategory="Leather Jacket")
    assert not any(
        f.rule_id == "TAX.003" for f in approval._all_findings(p, policy())
    )
    assert _plan_for(p) == []


# --------------------------------------------------------------------------- #
# The repair writes BOTH copies
# --------------------------------------------------------------------------- #

def test_a_doubled_value_is_repaired_on_both_sides():
    """The regression that motivated the pair table.

    Writing only the column left `properties.sub_category` holding the old
    value, so re-auditing the repaired product reported a MEDIUM DRIFT.001 that
    the repair itself had created. Observed on the real product before this.
    """
    assert product_audit._PROPERTY_FOR_COLUMN["subCategory"] == "sub_category"
    assert product_audit._COLUMN_FOR_PROPERTY["international_size"] == \
        "internationalSize"

    # Every pair the drift rule judges must be writable from both directions, or
    # the repair can only ever fix half of one.
    for _field, column, prop in gate.COLUMN_PROPERTY_PAIRS:
        assert product_audit._PROPERTY_FOR_COLUMN[column] == prop
        assert product_audit._COLUMN_FOR_PROPERTY[prop] == column


# --------------------------------------------------------------------------- #
# SIZE.012 — on a generic chart while specific ones fit
#
# This tenant has TWO men's upper charts that both list "S": "Men Uppers" and
# "Men DressShirts". better_guide() returned a name only when EXACTLY one chart
# fit, so every men's top left on "Defaults" matched two, failed that test, and
# was reported as clean — a real men's hoodie (1939df54) is what surfaced it.
# --------------------------------------------------------------------------- #

TWO_UPPER_GUIDES = {
    "Defaults": {"sizes": ["XS", "S", "M", "L"], "euSizes": ["44", "46", "48", "50"]},
    "Men Uppers": {"sizes": ["XS", "S", "M", "L"], "euSizes": ["44", "46", "48", "50"]},
    "Men DressShirts": {"sizes": ["XS", "S", "M", "L"],
                        "euSizes": ["44", "46", "48", "50"]},
    "Men Bottoms": {"sizes": ["W30", "W32"], "euSizes": ["46", "48"]},
}

HOODIE_TREE = {
    "Men": {
        "Sweaters & Hoodies": ["Hoodie", "Sweater"],
        "Shirts": ["Business Shirt", "Casual Shirt"],
    }
}


def upper_snap(**over):
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Sweaters & Hoodies",
        "subCategory": "Hoodie", "size": "S", "internationalSize": "S",
        "euSize": "46", "sizingGuide": "Defaults", "brand": "Zara",
        "color": "Black", "material": "Cotton", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1,
        **over,
    }
    return to_snapshot(raw, catalog={
        "categories": HOODIE_TREE,
        "sizingGuides": TWO_UPPER_GUIDES,
        "brands": ["Zara"], "colors": ["Black"], "materials": ["Cotton"],
    })


def size012(p):
    return [f for f in gate.check_gate(p, policy()) if f.rule_id == "SIZE.012"]


def test_a_hoodie_on_defaults_names_the_general_upper_chart():
    """'Men DressShirts' is a shirt chart and a hoodie is not a shirt.

    Both charts list "S", so the ladder cannot separate them. The guide's own
    NAME can: one is named for a body side, the other for a garment.
    """
    found = size012(upper_snap())
    assert found
    assert found[0].detail["suggested"] == ["Men Uppers"]


def test_a_shirt_on_defaults_keeps_both_and_escalates():
    """Where the ambiguity is REAL, it must survive.

    A men's business shirt genuinely could take either chart, so naming one
    would be a guess. Two candidates is the honest answer and the planner turns
    it into an escalation.
    """
    p = upper_snap(category="Shirts", subCategory="Business Shirt")
    found = size012(p)
    assert found
    assert found[0].detail["suggested"] == ["Men DressShirts", "Men Uppers"]

    plan: list[dict] = []
    approval._plan_guide_switch(p, gate.check_gate(p, policy()), plan)
    assert [a["kind"] for a in plan] == ["escalate"]
    assert "pick one" in plan[0]["detail"]


def test_the_named_chart_becomes_a_repair():
    p = upper_snap()
    plan: list[dict] = []
    approval._plan_guide_switch(p, gate.check_gate(p, policy()), plan)
    assert plan == [{
        "kind": "set_column", "field": "sizingGuide", "value": "Men Uppers",
        "reason": "SIZE.012", "detail": "was 'Defaults'",
    }]


def test_a_product_already_on_a_specific_chart_is_left_alone():
    """The narrowing must not start moving products off correct charts."""
    assert size012(upper_snap(sizingGuide="Men Uppers")) == []
    assert size012(upper_snap(sizingGuide="Men DressShirts",
                              category="Shirts",
                              subCategory="Business Shirt")) == []


def test_no_specific_chart_means_defaults_is_right():
    """Silent when the tenant has nothing better — the pre-existing contract."""
    p = to_snapshot(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men", "category": "Sweaters & Hoodies",
            "subCategory": "Hoodie", "size": "S", "internationalSize": "S",
            "euSize": "46", "sizingGuide": "Defaults", "brand": "Zara",
            "color": "Black", "material": "Cotton", "condition": "As New",
            "gender": ["men"], "careLabelCount": 1,
        },
        catalog={
            "categories": HOODIE_TREE,
            "sizingGuides": {"Defaults": TWO_UPPER_GUIDES["Defaults"]},
            "brands": ["Zara"], "colors": ["Black"], "materials": ["Cotton"],
        },
    )
    assert size012(p) == []


def test_garment_words_separates_general_from_specialised():
    pol = policy()
    assert gate._garment_words("Men Uppers", pol) == set()
    assert gate._garment_words("Men Bottoms", pol) == set()
    assert "shirt" in gate._garment_words("Men DressShirts", pol)


# --------------------------------------------------------------------------- #
# The properties SIDE of a drift repair
#
# Both of these were found by dry-running the five worst products on production
# and reading what the executor said it would refuse.
# --------------------------------------------------------------------------- #

def test_every_drift_pair_is_writable_from_both_sides():
    """The properties-side repair was silently dead for every pair.

    COLUMN_PROPERTY_PAIRS names its field by the SNAPSHOT name
    (`international_size`), and _PROPERTY_KEYS was keyed by a partly different
    set — it calls that one `size`. So `set_property international_size` came
    back as "no `properties` key mapped" and was reported as needing a human,
    while the column-side half worked and made the rule look functional.
    """
    for field, column, prop in gate.COLUMN_PROPERTY_PAIRS:
        assert product_audit._PROPERTY_KEYS.get(field) == prop, (
            f"DRIFT.001 can plan `set_property {field}`, but the executor has "
            f"no properties key for it — the repair would be refused"
        )
        assert product_audit._PROPERTY_FOR_COLUMN[column] == prop
        assert product_audit._COLUMN_FOR_PROPERTY[prop] == column


def test_drift_yields_to_a_taxonomy_repair_on_the_same_field():
    """Two repairs, one value, and the drift one landed last and won.

    Real product 07159384 on production: the column held 'T-Shirt', the tree
    spells it 'T-Shirts', and `properties.sub_category` was absent. TAX.003
    planned the column to 'T-Shirts'; DRIFT.001 then planned the property to
    'T-Shirt' off the PRE-repair column. Because the executor mirrors both sides
    and applies in order, the drift entry overwrote the taxonomy fix.
    """
    p = to_snapshot(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men", "category": "Jackets",
            "subCategory": "Leather Jackets",   # tree spells it singular
            "size": "S", "euSize": "46", "sizingGuide": "Men Uppers",
            "brand": "Zara", "color": "Black", "material": "Leather",
            "condition": "As New", "gender": ["men"], "careLabelCount": 1,
            # The property copy is ABSENT, so DRIFT.001 fires HIGH and wants to
            # copy the (about to be corrected) column value across.
            "properties": {"international_size": "S"},
            "columnValues": {"subCategory": "Leather Jackets"},
        },
        catalog=CATALOG,
    )
    findings = approval._all_findings(p, policy())
    assert any(f.rule_id == "TAX.003" for f in findings)
    assert any(f.rule_id == "DRIFT.001" for f in findings)

    plan: list[dict] = []
    approval._plan_subcategory(p, findings, plan)
    approval._plan_column_drift(findings, plan)

    subcat = [a for a in plan
              if str(a.get("field", "")).lower().startswith("subcat")]
    assert len(subcat) == 1, f"one repair per value, got {subcat}"
    assert subcat[0]["value"] == "Leather Jacket"
    assert subcat[0]["reason"] == "TAX.003"


def test_drift_still_plans_a_field_nothing_else_claims():
    """The yield must not swallow the repairs it was written for."""
    p = snap(
        columns={"internationalSize": "Unknown"},
        properties={"international_size": "S"},
    )
    findings = approval._all_findings(p, policy())
    plan: list[dict] = []
    approval._plan_subcategory(p, findings, plan)
    approval._plan_column_drift(findings, plan)
    assert [(a["kind"], a["field"], a["value"]) for a in plan] == [
        ("set_column", "internationalSize", "S")
    ]
