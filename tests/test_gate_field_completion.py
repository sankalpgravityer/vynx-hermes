"""Field-completion rules: gender, sizing-guide appropriateness, subcategory.

The cases that matter are the ones where a rule could plausibly do the WRONG
thing and nothing downstream would notice:

  * narrowing a both-genders product, which makes split-gender.worker skip the
    second product entirely — a silently lost listing;
  * leaving a women's garment on a generic chart, where DEFAULTS_TABLE converts
    it against the men's table and every EU size comes out ten sizes off;
  * inventing a chart the tenant already has under another name.
"""

from __future__ import annotations

from app.config import policy
from app.rules import gate
from app.vnyx_client import to_snapshot
from app import approval

# The tenant's real guide names, per the note in rules/consistency.py.
GUIDES = {
    "Men Bottoms": {"sizes": ["W30", "W32", "W34"], "euSizes": ["46", "48", "50"]},
    "Men Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
    "Women Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["36", "38", "40"]},
    "Defaults": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
}

TREE = {
    "Men": {"T-Shirts & Polos": ["Long Sleeves", "T-Shirts"], "Bottoms": ["Jeans"]},
    "Women": {"T-Shirts & Polos": ["T-Shirts"]},
}


def snap(**over):
    raw = {
        "id": "p1",
        "tenantId": "t1",
        "sku": "S1",
        "productCode": "PC1",
        "masterCategory": "Men",
        "category": "T-Shirts & Polos",
        "subCategory": "T-Shirts",
        "size": "S",
        "internationalSize": "S",
        "euSize": "46",
        "sizingGuide": "Men Uppers",
        "brand": "BOAS",
        "color": "Burgundy",
        "material": "Cotton",
        "condition": "As New",
        "gender": ["men"],
        "careLabelCount": 1,
        **over,
    }
    catalog = {
        "categories": TREE,
        "sizingGuides": GUIDES,
        "brands": ["BOAS"],
        "colors": ["Burgundy"],
        "materials": ["Cotton"],
    }
    return to_snapshot(raw, catalog=catalog), catalog


def rules(p):
    return {f.rule_id for f in gate.check_gate(p, policy())}


# --------------------------------------------------------------------------- #
# gender
# --------------------------------------------------------------------------- #

def test_resolved_gender_is_clean():
    p, _ = snap(gender=["men"])
    assert "GENDER.001" not in rules(p)
    assert "DATA.010" not in rules(p)


BOTH_GENDERS = {
    "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
    "masterCategory": "Men",
    "category": "T-Shirts & Polos", "subCategory": "T-Shirts",
    "size": "S", "internationalSize": "S", "euSize": "46",
    "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
    "material": "Cotton", "condition": "As New",
    "gender": ["men", "women"], "careLabelCount": 1,
}


def test_both_genders_is_never_narrowed_when_the_split_owns_it():
    """The load-bearing case.

    With `duplicateProductOnBothGenders` ON, split-gender.worker narrows the
    parent AND creates the copy for the other gender. Its both-gender guard makes
    a re-run a no-op once the parent is single-gender, so writing a gender here
    would mean the second product is never created — a silently lost listing.
    """
    _, cat = snap()
    out = approval.run_gate(BOTH_GENDERS, catalog=cat,
                            split_on_both_genders=True)
    writes = [a for a in out["repair_plan"] if a["kind"] != "escalate"]
    assert not any(a.get("field") == "gender" for a in writes)
    assert out["human_intervention_needed"] is True


def test_both_genders_is_derived_when_no_split_owns_it():
    """With the split workflow OFF there is nothing to protect.

    analyze.worker narrows inline in that case (`genders = ['men']`), and its
    `resolvedGender` prefers the master category — so deriving here reaches the
    same answer, just earlier.
    """
    _, cat = snap()
    out = approval.run_gate(BOTH_GENDERS, catalog=cat,
                            split_on_both_genders=False)
    writes = [
        a for a in out["repair_plan"]
        if a["kind"] == "set_property" and a["field"] == "gender"
    ]
    assert writes, "master category should settle it"
    assert writes[0]["value"] == ["men"]
    # And it must not ALSO be escalated — that would double-report a fixed field.
    assert not any(
        a["kind"] == "escalate" and a.get("field") == "gender"
        for a in out["repair_plan"]
    )


def test_gender_rule_reports_who_owns_it():
    p, _ = snap(gender=["men", "women"])
    owned = gate.check_gate(p, policy(), split_on_both_genders=True)
    free = gate.check_gate(p, policy(), split_on_both_genders=False)
    f_owned = next(f for f in owned if f.rule_id == "GENDER.001")
    f_free = next(f for f in free if f.rule_id == "GENDER.001")
    assert f_owned.detail["repairable"] is False
    assert f_owned.detail["owner"] == "split-gender-worker"
    assert f_free.detail["repairable"] is True
    assert f_free.detail["derived"] == "men"


def test_absent_gender_is_derived_from_master_category():
    p, cat = snap()
    del_raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men",
        "category": "T-Shirts & Polos", "subCategory": "T-Shirts",
        "size": "S", "internationalSize": "S", "euSize": "46",
        "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
        "material": "Cotton", "condition": "As New", "careLabelCount": 1,
    }
    out = approval.run_gate(del_raw, catalog=cat)
    gender_writes = [
        a for a in out["repair_plan"]
        if a["kind"] == "set_property" and a["field"] == "gender"
    ]
    assert gender_writes, "an absent gender should be derived"
    # A LIST, matching normalizeGenderToArray and split-gender.worker. A bare
    # string is a shape no other writer in the codebase produces.
    assert gender_writes[0]["value"] == ["men"]


# --------------------------------------------------------------------------- #
# sizing-guide appropriateness
# --------------------------------------------------------------------------- #

def test_correct_gendered_guide_is_clean():
    p, _ = snap(sizingGuide="Men Uppers")
    assert "SIZE.011" not in rules(p)
    assert "SIZE.012" not in rules(p)


def test_contradicting_guide_blocks():
    """A men's product on a women's chart. HIGH — it produces wrong EU sizes."""
    p, _ = snap(sizingGuide="Women Uppers")
    assert "SIZE.011" in rules(p)


def test_generic_guide_is_advisory_only():
    """MEDIUM, so `Defaults` does not stop a catalog being approved."""
    p, _ = snap(sizingGuide="Defaults")
    found = [f for f in gate.check_gate(p, policy()) if f.rule_id == "SIZE.012"]
    assert found, "should notice a better chart exists"
    assert found[0].severity.value == "medium"
    assert found[0].detail["suggested"] == "Men Uppers"


def test_generic_guide_kept_when_nothing_better_exists():
    """`Defaults` is a fine answer when the tenant has configured nothing else.

    The rule must not nag about a chart that does not exist — SIZE.012 exists to
    point at a BETTER option, and here there is none.
    """
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "T-Shirts & Polos",
        "subCategory": "T-Shirts", "size": "S", "internationalSize": "S",
        "euSize": "46", "sizingGuide": "Defaults", "brand": "BOAS",
        "color": "Burgundy", "material": "Cotton", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1,
    }
    only_defaults = {
        "categories": TREE,
        "sizingGuides": {"Defaults": GUIDES["Defaults"]},
        "brands": ["BOAS"], "colors": ["Burgundy"], "materials": ["Cotton"],
    }
    p = to_snapshot(raw, catalog=only_defaults)
    assert "SIZE.012" not in rules(p)
    assert "SIZE.010" not in rules(p)  # Defaults covers the size, so it is fine


def test_generic_guide_flagged_when_the_product_carries_its_own_gender():
    """Gender comes from the attribute when the master category has none.

    A `Kids` master category resolves to no gender, but `gender: ['men']` on the
    record still identifies a better chart.
    """
    p, _ = snap(sizingGuide="Defaults", masterCategory="Kids",
                category="Bottoms", gender=["men"])
    found = [f for f in gate.check_gate(p, policy()) if f.rule_id == "SIZE.012"]
    assert found and found[0].detail["suggested"] == "Men Uppers"


def test_womens_product_on_defaults_is_the_dangerous_case():
    """The reason SIZE.012 exists at all.

    DEFAULTS_TABLE in backfill-eu-sizes.ts is a module constant set to MEN, so a
    women's garment left on `Defaults` converts S to 46 instead of 36.
    """
    p, _ = snap(
        masterCategory="Women", category="T-Shirts & Polos",
        subCategory="T-Shirts", sizingGuide="Defaults", euSize="36",
        gender=["women"],
    )
    found = [f for f in gate.check_gate(p, policy()) if f.rule_id == "SIZE.012"]
    assert found
    assert found[0].detail["suggested"] == "Women Uppers"


def test_guide_lookup_is_gender_aware_not_name_derived():
    """`Men Uppers`, not the derived `Men T-Shirts & Polos`.

    consistency.py records that deriving "{master} {category}" only ever
    coincides for Bottoms — a men's t-shirt correctly uses "Men Uppers".
    """
    p, _ = snap(sizingGuide=None)
    assert gate.resolve_guide(p) == "Men Uppers"
    assert "SIZE.010" not in rules(p)


def test_ambiguity_is_resolved_by_gender_rather_than_escalated():
    """Both `Men Uppers` and `Women Uppers` list S; the master category decides."""
    p, _ = snap(sizingGuide=None, masterCategory="Women",
                category="T-Shirts & Polos", subCategory="T-Shirts",
                euSize="36", gender=["women"])
    assert gate.resolve_guide(p) == "Women Uppers"


# --------------------------------------------------------------------------- #
# subcategory
# --------------------------------------------------------------------------- #

def test_subcategory_derived_when_the_tree_offers_one_option():
    _, cat = snap()
    out = approval.run_gate(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men",
            "category": "Bottoms",  # the tree lists exactly one: Jeans
            "size": "W32", "waist": "32", "euSize": "48",
            "sizingGuide": "Men Bottoms", "brand": "BOAS", "color": "Burgundy",
            "material": "Cotton", "condition": "As New", "gender": ["men"],
            "careLabelCount": 1,
        },
        catalog=cat,
    )
    writes = [
        a for a in out["repair_plan"]
        if a["kind"] == "set_column" and a["field"] == "subCategory"
    ]
    assert writes and writes[0]["value"] == "Jeans"


def test_subcategory_not_guessed_when_the_tree_offers_several():
    _, cat = snap()
    out = approval.run_gate(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men",
            "category": "T-Shirts & Polos",  # two options in the tree
            "size": "S", "internationalSize": "S", "euSize": "46",
            "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
            "material": "Cotton", "condition": "As New", "gender": ["men"],
            "careLabelCount": 1,
        },
        catalog=cat,
    )
    assert not any(
        a["kind"] == "set_column" and a["field"] == "subCategory"
        for a in out["repair_plan"]
    )


# --------------------------------------------------------------------------- #
# the response contract the backend acts on
# --------------------------------------------------------------------------- #

def test_verified_shape():
    _, cat = snap()
    clean = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men",
        "category": "T-Shirts & Polos", "subCategory": "T-Shirts",
        "size": "S", "internationalSize": "S", "euSize": "46",
        "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
        "material": "Cotton", "condition": "As New", "gender": ["men"],
        "careLabelCount": 1, "priceAmount": 17.39,
        "retailPriceAmount": 28.99, "currency": "EUR", "grade": "A",
        "gradeLabel": "As New",
        "priceExpectation": {"priceFactor": 0.60, "expectedPrice": 17.39},
        "title": "Vintage BOAS Burgundy T-Shirt Men S",
        "description": "A deep burgundy cotton jersey tee in as-new condition.",
    }
    out = approval.run_gate(clean, catalog=cat)
    assert set(["verified", "human_intervention_needed", "reasons"]) <= set(out)
    assert isinstance(out["verified"], bool)
    # A care label present, a matching chart, an in-band price: nothing a human
    # has to supply.
    assert out["human_intervention_needed"] is False


def test_human_intervention_only_when_no_repair_exists():
    """A missing care label cannot be generated, so it needs a person."""
    _, cat = snap()
    out = approval.run_gate(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Men",
            "category": "T-Shirts & Polos", "subCategory": "T-Shirts",
            "size": "S", "internationalSize": "S", "euSize": "46",
            "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
            "material": "Cotton", "condition": "As New", "gender": ["men"],
            "careLabelCount": 0,
        },
        catalog=cat,
    )
    assert out["verified"] is False
    assert out["human_intervention_needed"] is True
    assert any("IMG.030" in r for r in out["reasons"])
