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

    It ESCALATES rather than planning a write: picking the alphabetically-first
    entry would be a plausible-looking wrong answer nobody would catch. The
    escalation names the branch's real options so a reviewer can choose.
    """
    plan = _plan_for(snap(subCategory="Windbreaker"))
    assert [a["kind"] for a in plan] == ["escalate"]
    assert "not offered" in plan[0]["detail"]


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
    assert [a["kind"] for a in _plan_for(p)] == ["escalate"]


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


# --------------------------------------------------------------------------- #
# TAX.005 — the mannequin rig, on a tenant whose categories are not policy's
#
# `mannequin_map` in policy.yaml has seven keys: Men|Tops, Men|Outerwear,
# Men|Bottoms, Women|Tops, Women|Outerwear, Women|Bottoms, Women|Dresses. A real
# tenant's categories are Jackets, Sweaters & Hoodies, T-Shirts & Polos, Shirts,
# Vests, Accessories, Footwear — only `Bottoms` overlaps. Measured on production:
# of 1,762 products in review only 300 had a category the map could key on, and
# among the other 83% sat 255 wrong-GENDER and 21 wrong-SIDE mannequins that
# TAX.005 reported as clean while approve-products.ts refused to approve them.
# --------------------------------------------------------------------------- #

RIG_TREE = {"Men": {"Jackets": ["Sports Jackets"], "Bottoms": ["Jeans"]},
            "Women": {"Jackets": ["Sports Jackets"]}}


def rig_snap(**over):
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Jackets",
        "subCategory": "Sports Jackets", "size": "S", "euSize": "46",
        "sizingGuide": "Men Uppers", "brand": "Zara", "color": "Black",
        "material": "Leather", "condition": "As New", "gender": ["men"],
        "careLabelCount": 1, "mannequinType": "Men Top",
        **over,
    }
    return to_snapshot(raw, catalog={
        "categories": RIG_TREE, "sizingGuides": GUIDES,
        "brands": ["Zara"], "colors": ["Black"], "materials": ["Leather"]})


def tax005(p):
    from app.rules.consistency import check_taxonomy
    return [f for f in check_taxonomy(p, policy()) if f.rule_id == "TAX.005"]


def test_wrong_gender_rig_on_a_category_the_policy_map_never_heard_of():
    """`Men|Jackets` is not a policy key, so this was silent before."""
    found = tax005(rig_snap(mannequinType="Women Top"))
    assert len(found) == 1
    assert found[0].detail["basis"] == "derived"
    assert found[0].detail["suggested"] == "Men Top"


def test_wrong_side_rig():
    found = tax005(rig_snap(mannequinType="Men Bottom"))
    assert len(found) == 1
    assert found[0].detail["suggested"] == "Men Top"


def test_both_wrong_is_one_finding_naming_both():
    found = tax005(rig_snap(mannequinType="Women Bottom"))
    assert len(found) == 1
    assert "women's rig on a men's product" in found[0].message
    assert "bottom-body rig" in found[0].message
    assert found[0].detail["suggested"] == "Men Top"


def test_a_correct_rig_is_silent():
    assert tax005(rig_snap()) == []
    assert tax005(rig_snap(category="Bottoms", subCategory="Jeans",
                           mannequinType="Men Bottom")) == []


def test_an_unreadable_rig_name_is_not_guessed_at():
    """A rig whose name carries neither gender nor side says nothing."""
    assert tax005(rig_snap(mannequinType="Default")) == []


def test_the_policy_map_still_wins_where_it_applies():
    """Additive, not a replacement — the exact-name check keeps its basis."""
    p = rig_snap(category="Bottoms", subCategory="Jeans",
                 mannequinType="Women Dress")
    found = tax005(p)
    assert found and found[0].detail["basis"] == "policy_map"


def test_the_rig_repair_is_planned():
    p = rig_snap(mannequinType="Women Top")
    plan: list[dict] = []
    approval._plan_mannequin(approval._all_findings(p, policy()), plan)
    assert plan == [{
        "kind": "set_column", "field": "mannequinType", "value": "Men Top",
        "reason": "TAX.005", "detail": "was 'Women Top'",
    }]


# --------------------------------------------------------------------------- #
# A BLANK sub-category
#
# Measured on production: 28 products carry one, and their branches offer four
# to eleven options each. "Pick one from the category" would therefore be wrong
# most of the time and indistinguishable afterwards from a value somebody meant.
# The title is the only evidence on the record, and it resolves exactly one of
# the 28 — which is the honest yield, not a disappointing one.
# --------------------------------------------------------------------------- #

BLANK_TREE = {"Women": {
    "Sweaters & Hoodies": ["Sweatshirts", "Fleece Pullover", "Hoodies", "Sweaters"],
    "Skirts": ["Leather Skirt", "Pencil Skirt", "Mini Skirt", "Maxi Skirt"],
    # The tenant's real Vests branch: four TYPES, none of them a bare "Vest".
    "Vests": ["Denim Vests", "Puffer Vests", "Fleece Vests", "Leather Vests"],
    "Coats": ["Long Coat"],
}}


def blank_snap(**over):
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Women", "category": "Sweaters & Hoodies",
        "subCategory": None, "size": "M", "euSize": "38",
        "sizingGuide": "Women Uppers", "brand": "Zara", "color": "Black",
        "material": "Cotton", "condition": "As New", "gender": ["women"],
        "careLabelCount": 1, "mannequinType": "Women Top",
        "title": "Relaxed Sweatshirt in Black size M",
        **over,
    }
    return to_snapshot(raw, catalog={
        "categories": BLANK_TREE,
        "sizingGuides": {"Women Uppers": {"sizes": ["M"], "euSizes": ["38"]}},
        "brands": ["Zara"], "colors": ["Black"], "materials": ["Cotton"]})


def test_the_title_names_the_subcategory():
    """The real product this was written for."""
    plan = _plan_for(blank_snap())
    assert plan == [{
        "kind": "set_column", "field": "subCategory", "value": "Sweatshirts",
        "reason": "DATA.010", "detail": plan[0]["detail"],
    }]


def test_a_title_that_names_nothing_is_left_alone():
    """'Cream Skirt' under a branch of six skirt TYPES names none of them."""
    p = blank_snap(category="Skirts",
                   title="Vintage Karen Millen Cream Skirt Women M")
    assert _plan_for(p) == []


def test_word_matching_not_substring():
    """A bare 'Vest' in the title must not pick one of four vest TYPES.

    The real product: "Vintage Brown Faux Fur Vest Men" on a branch offering
    Denim / Puffer / Fleece / Leather. Substring matching would have latched
    onto whichever happened to contain "vest" — all four of them.
    """
    p = blank_snap(category="Vests", title="Vintage Brown Faux Fur Vest Women")
    assert [a for a in _plan_for(p) if a["kind"] == "set_column"] == []


def test_the_title_picks_the_right_type_out_of_several():
    p = blank_snap(category="Vests", title="Vintage Brown Leather Vest Women")
    plan = _plan_for(p)
    assert plan and plan[0]["value"] == "Leather Vests"


def test_one_option_still_wins_without_a_title():
    p = blank_snap(category="Coats", title="Something Unhelpful")
    plan = _plan_for(p)
    assert plan and plan[0]["value"] == "Long Coat"


def test_a_value_that_lives_on_the_other_gender_says_so():
    """The 10 women's blazers: correctly labelled, option missing from the tree.

    No per-product edit fixes this and substituting a neighbouring option would
    write a garment type nobody chose — so the escalation has to name where the
    value DOES exist, or the wrong person goes looking.
    """
    p = to_snapshot(
        {
            "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
            "masterCategory": "Women", "category": "Jackets",
            "subCategory": "Blazers", "size": "M", "euSize": "38",
            "sizingGuide": "Women Uppers", "brand": "Zara", "color": "Black",
            "material": "Wool", "condition": "As New", "gender": ["women"],
            "careLabelCount": 1, "mannequinType": "Women Top",
            "title": "Vintage Burgundy Blazer Women",
        },
        catalog={
            "categories": {"Women": {"Jackets": ["Bomber Jackets", "Long Coats"]},
                           "Men": {"Jackets": ["Blazers", "Bomber Jackets"]}},
            "sizingGuides": {"Women Uppers": {"sizes": ["M"], "euSizes": ["38"]}},
            "brands": ["Zara"], "colors": ["Black"], "materials": ["Wool"]},
    )
    plan = _plan_for(p)
    assert [a["kind"] for a in plan] == ["escalate"]
    assert "Men > Jackets" in plan[0]["detail"]
    assert "master category is wrong" in plan[0]["detail"]


# --------------------------------------------------------------------------- #
# The care-label second pass
#
# Written after a live run failed three different ways at once, each of which
# looked like a broken feature and was not.
# --------------------------------------------------------------------------- #

def test_no_images_means_no_call():
    """Asked to read a label and given none, Gemini invented one.

    It returned brand "JOE FRESH" and size "XL" at confidence 100 — a complete
    fabrication that the confidence floor waves straight through, because the
    floor grades certainty and not whether the model had anything to look at.
    """
    from app.llm import care_label
    assert care_label._read_gemini([]) is None
    assert care_label._read_openai([]) is None
    out = care_label.read([])
    assert out["error"] and "brand" not in out and "size" not in out


def test_openai_needs_the_word_json_in_the_prompt():
    """Its API returns 400 without it, and the status alone reads as an outage.

    'messages' must contain the word 'json' in some form, to use
    'response_format' of type 'json_object'.
    """
    from app.llm import care_label
    assert "json" in care_label.PROMPT.lower()


def test_the_openai_schema_is_strict_shaped():
    """json_object does not enforce a shape; json_schema does.

    Asked for json_object, OpenAI omitted both confidence fields — and a
    reading with no confidence scores 0 and is thrown away, so a correct answer
    was paid for and discarded. Strict mode needs every property required and
    additionalProperties false.
    """
    from app.llm import care_label
    schema = care_label._OPENAI_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_a_size_the_guide_cannot_express_is_refused():
    """The check a confidence score cannot do.

    On a label plainly reading "S", OpenAI returned the DIGIT "5" at 85%.
    Confident and wrong about a character is exactly what a self-reported score
    misses; the tenant's own ladder catches it.
    """
    from app.llm.care_label import _size_ok
    ladder = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"]
    assert _size_ok("S", ladder)
    assert _size_ok(" s ", ladder)      # case and spacing are not the question
    assert not _size_ok("5", ladder)
    assert not _size_ok("42", ladder)   # a waist number on a letter ladder
    # No guide attached means no opinion — judged on confidence alone, as before.
    assert _size_ok("5", None)
    assert _size_ok("anything", [])


# --------------------------------------------------------------------------- #
# EU size — derived from the product's own chart
#
# The last field blocking a real listing (31d206fa) that had everything else:
# the edit screen shows "EU Size is required" in red under a filled-in
# International Size, and nothing filled it. SIZE.002 validates an EU size that
# is PRESENT and wrong, and stays silent when there is none to validate.
# --------------------------------------------------------------------------- #

EU_GUIDES = {
    "Men Uppers": {"sizes": ["XXS", "XS", "S", "M", "L"],
                   "euSizes": ["42", "44", "46", "48", "50"]},
    # The tenant's real Women chart, which disagrees with the men's at every
    # index — the reason a generic conversion table cannot be used here.
    "Women Uppers": {"sizes": ["XS", "S", "M", "L"],
                     "euSizes": ["34", "36", "38", "40"]},
}


def eu_snap(**over):
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Jackets",
        "subCategory": "Leather Jacket", "size": "M", "euSize": None,
        "sizingGuide": "Men Uppers", "brand": "Zara", "color": "Black",
        "material": "Leather", "condition": "As New", "gender": ["men"],
        "careLabelCount": 1, "mannequinType": "Men Top",
        **over,
    }
    return to_snapshot(raw, catalog={
        "categories": TREE, "sizingGuides": EU_GUIDES,
        "brands": ["Zara"], "colors": ["Black"], "materials": ["Leather"]})


def _eu_plan(p):
    plan: list[dict] = []
    approval._plan_eu_size(p, approval._all_findings(p, policy()), plan)
    return plan


def test_eu_size_is_read_out_of_the_products_own_chart():
    plan = _eu_plan(eu_snap())
    assert plan and plan[0]["value"] == "48"
    assert plan[0]["field"] == "eu_size"


def test_the_chart_used_is_the_one_on_the_product():
    """S is EU 46 on Men Uppers and EU 36 on Women Uppers.

    Ten sizes apart, from the same letter — which is why this is a lookup in the
    attached guide and never a generic table.
    """
    assert _eu_plan(eu_snap(size="S"))[0]["value"] == "46"
    men_s = _eu_plan(eu_snap(size="S"))[0]["value"]
    women = _eu_plan(eu_snap(size="S", sizingGuide="Women Uppers",
                             masterCategory="Women", gender=["women"],
                             mannequinType="Women Top"))
    assert women[0]["value"] == "36"
    assert men_s != women[0]["value"]


def test_a_size_absent_from_the_ladder_derives_nothing():
    assert _eu_plan(eu_snap(size="XXXL")) == []


def test_no_guide_means_no_lookup():
    assert _eu_plan(eu_snap(sizingGuide=None)) == []


def test_an_eu_size_already_present_is_left_alone():
    assert _eu_plan(eu_snap(euSize="48")) == []


def test_it_pairs_with_the_size_this_run_is_about_to_write():
    """The ordering bug it would otherwise have.

    When the care-label pass has just read a new size, deriving from the STORED
    one pairs the new size with the old EU number — two fields that disagree,
    written together, by the step meant to make them agree.
    """
    p = eu_snap(size="S")
    plan = [{"kind": "set_property", "field": "international_size",
             "value": "L", "reason": "CARE_LABEL"}]
    approval._plan_eu_size(p, approval._all_findings(p, policy()), plan)
    eu = [a for a in plan if a["field"] == "eu_size"]
    assert eu and eu[0]["value"] == "50", "should pair with the PLANNED L, not the stored S"
