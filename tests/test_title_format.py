"""The tenant's title format and description sections (TEXT.009 / TEXT.010).

app/rules/title_template.py ports vnyx-api's title builder; these pin the
behaviours the copy step depends on: the category override beats the default,
empty fields drop with their prefix, Brand / gender / player fields resolve the
way vnyx-api resolves them, and the two rules fire only on a real mismatch.
"""

from app.config import policy
from app.rules import title_template as tt
from app.rules.consistency import check_copy
from app.vnyx_client import to_snapshot

DEFAULT = {"enabled": True, "parts": [
    {"type": "text", "value": "Vintage"}, {"type": "field", "key": "brand"},
    {"type": "field", "key": "color"}, {"type": "field", "key": "subCategory"},
    {"type": "field", "key": "gender"}, {"type": "field", "key": "size"},
]}
JERSEY = {"parts": [
    {"type": "text", "value": "Vintage"}, {"type": "field", "key": "brand"},
    {"type": "field", "key": "playerName"},
    {"type": "field", "key": "playerNumber", "prefix": "#"},
    {"type": "field", "key": "subCategory"}, {"type": "field", "key": "size"},
]}
OVERRIDES = tt.override_candidates([(JERSEY, "Jerseys", "Tops", "Men")])
SECTIONS = [{"name": "Product Details", "isActive": True, "order": 0},
            {"name": "Size & Fit", "isActive": True, "order": 1},
            {"name": "Warranty", "isActive": False, "order": 2}]


def fmt(props, default=DEFAULT, **cols):
    cols = {"master_category": "Men", "category": "Tops", "sub_category": "T-Shirts", **cols}
    return tt.resolve(default_template=default, overrides=OVERRIDES, sections=SECTIONS,
                      props=props, **cols)


def test_default_format_with_capital_brand_and_lowercase_gender():
    out = fmt({"Brand": "Adidas", "color": "Red", "gender": ["men"],
               "international_size": "Unknown"})
    assert out["expectedTitle"] == "Vintage Adidas Red T-Shirts Men"
    assert out["titleSource"] == "default"


def test_category_override_wins_and_player_fields_resolve():
    out = fmt({"brand": "Nike", "player_name": "Jordan", "player_number": 23,
               "international_size": "L"}, sub_category="Jerseys")
    assert out["expectedTitle"] == "Vintage Nike Jordan #23 Jerseys L"
    assert (out["titleSource"], out["matchedCategory"]) == ("category", "Jerseys")


def test_empty_field_drops_with_its_prefix():
    out = fmt({"brand": "Nike", "player_name": "", "international_size": "L"},
              sub_category="Jerseys")
    assert out["expectedTitle"] == "Vintage Nike Jerseys L"


def test_override_under_other_master_does_not_apply():
    out = fmt({"brand": "Nike"}, master_category="Women", sub_category="Jerseys")
    assert out["titleSource"] == "default"


def test_format_off_means_no_expected_title_even_with_overrides():
    out = fmt({"brand": "Nike"}, default={**DEFAULT, "enabled": False},
              sub_category="Jerseys")
    assert out["expectedTitle"] is None


def test_columns_overlay_stale_taxonomy_properties():
    out = fmt({"brand": "Nike", "sub_category": "Hoodies"}, sub_category="T-Shirts")
    assert "T-Shirts" in out["expectedTitle"]


def test_active_sections_in_order():
    assert fmt({})["sections"] == ["Product Details", "Size & Fit"]


def rules(title, description, copy_format):
    snap = to_snapshot({"id": "p", "title": title, "summary": description,
                        "brand": "Nike", "color": "Red", "subCategory": "T-Shirts",
                        "copyFormat": copy_format})
    return {f.rule_id: f for f in check_copy(snap, policy())}


GOOD_DESC = "<p>Intro</p><h2>Product Details</h2><p>x</p><h2>Size &amp; Fit</h2><p>y</p>"


def test_text009_fires_only_on_a_different_title():
    cf = {"expectedTitle": "Vintage Nike Red T-Shirts Men", "titleSource": "default",
          "sections": []}
    assert "TEXT.009" not in rules("Vintage Nike  Red T-Shirts Men ", GOOD_DESC, cf)
    hit = rules("Vintage Nike Red T-Shirt Men", GOOD_DESC, cf)["TEXT.009"]
    assert hit.detail["expected"] == "Vintage Nike Red T-Shirts Men"


def test_text009_silent_without_a_format():
    assert "TEXT.009" not in rules("Anything", GOOD_DESC, {"expectedTitle": None})
    assert "TEXT.009" not in rules("Anything", GOOD_DESC, None)


def test_text010_missing_and_extra_sections():
    cf = {"expectedTitle": None, "sections": ["Product Details", "Size & Fit"]}
    assert "TEXT.010" not in rules("t", GOOD_DESC, cf)
    hit = rules("t", "<h2>Product Details</h2><h2>Shipping</h2>", cf)["TEXT.010"]
    assert hit.detail["missing"] == ["Size & Fit"]
    assert hit.detail["extra"] == ["shipping"]
