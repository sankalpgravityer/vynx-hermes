"""app/twins.py — which SKUs are twins, and what a twin may inherit.

The two properties that matter: nothing gender-dependent is ever copied, and
nothing the twin already holds is ever overwritten.
"""
from __future__ import annotations

from app import twins
from app.config import policy

POL = policy()


def test_parent_sku_strips_the_c_for_every_configured_prefix():
    assert twins.parent_sku("CBOA-005721", POL) == "BOA-005721"
    assert twins.parent_sku("ckil-001059", POL) == "KIL-001059"
    assert twins.parent_sku("CSKU-000817", POL) == "SKU-000817"


def test_a_parent_or_unrelated_sku_is_not_a_twin():
    assert twins.parent_sku("BOA-005721", POL) is None
    assert twins.parent_sku("KIL-000964", POL) is None
    assert twins.parent_sku("", POL) is None
    assert twins.parent_sku(None, POL) is None
    assert twins.parent_sku("CBOA-", POL) is None


def _record(**over):
    base = {
        "sku": "BOA-1", "brand": None, "internationalSize": None, "waist": None,
        "lengthSize": None, "material": None, "fit": None, "condition": None,
        "color": None, "gender": ["men"], "masterCategory": "Men",
        "subCategory": "Jeans", "mannequinType": "Men Bottom",
        "sizingGuide": "Men Bottoms", "euSize": "48", "title": "Men Jeans",
    }
    base.update(over)
    return base


def test_blank_gender_neutral_fields_are_filled_from_the_parent():
    twin = _record(sku="CBOA-1", brand="Unknown", internationalSize=None,
                   gender=["women"], masterCategory="Women", euSize=None)
    parent = _record(brand="Levi's", internationalSize="32", waist="32",
                     lengthSize="34", material="Denim", fit="Straight",
                     condition="Good", color="Blue")
    plan = twins.inheritance_plan(twin, parent, POL)
    fields = {a["field"]: a["value"] for a in plan}
    assert fields == {
        "brand": "Levi's", "size": "32", "waist": "32", "length_size": "34",
        "material": "Denim", "fit": "Straight", "condition": "Good", "color": "Blue",
    }
    assert all(a["kind"] == "set_property" and a["reason"] == "TWIN" for a in plan)
    assert all("BOA-1" in a["detail"] for a in plan)


def test_nothing_gender_dependent_is_ever_in_the_plan():
    twin = _record(sku="CBOA-1", gender=["women"], masterCategory="Women",
                   subCategory=None, mannequinType=None, sizingGuide=None,
                   euSize=None, title=None)
    parent = _record(brand="Levi's", internationalSize="32")
    planned = {a["field"] for a in twins.inheritance_plan(twin, parent, POL)}
    never = set(POL["twins"]["never_inherit"]) | {
        "gender", "masterCategory", "subCategory", "mannequinType",
        "sizingGuide", "eu_size", "euSize", "title", "summary", "description",
    }
    assert not planned & never


def test_a_value_the_twin_already_holds_is_never_overwritten():
    twin = _record(sku="CBOA-1", brand="Wrangler", internationalSize="30")
    parent = _record(brand="Levi's", internationalSize="32", color="Blue")
    plan = twins.inheritance_plan(twin, parent, POL)
    assert {a["field"]: a["value"] for a in plan} == {"color": "Blue"}


def test_a_parent_blank_or_placeholder_contributes_nothing():
    twin = _record(sku="CBOA-1")
    parent = _record(brand="Unknown", internationalSize="", color="n/a")
    assert twins.inheritance_plan(twin, parent, POL) == []


def test_the_inherit_list_is_policy_not_code():
    narrow = {**POL, "twins": {**POL["twins"], "inherit_properties": ["brand"]}}
    twin = _record(sku="CBOA-1")
    parent = _record(brand="Levi's", internationalSize="32", color="Blue")
    assert [a["field"] for a in twins.inheritance_plan(twin, parent, narrow)] == ["brand"]
