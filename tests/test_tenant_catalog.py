"""Rules driven by the tenant's OWN option lists, not policy.yaml's guesses.

Every list in CATALOG below is real data, lifted from a HAR capture of
dev.vnyx.ai loading a product page for tenant 34c354a5. That is the point of
these tests: the previous hardcoded tables were not slightly off, they described
a different shop. This tenant's Men tree has no `Tops`, `Outerwear` or
`Footwear` — three of the five categories policy.yaml assumes — and its Women
Bottoms chart maps W28 to EU 36 where the generic table says 40.

Running every rule group against the policy fallbacks would therefore have
reported most of a correct catalog as incorrect.
"""

from __future__ import annotations

import copy

from app.config import policy
from app.rules import run_all
from app.rules.catalog import check_catalog
from app.rules.consistency import check_sizing, check_taxonomy
from app.vnyx_client import to_snapshot

POL = policy()

# --- real data from the HAR ------------------------------------------------- #

CATALOG = {
    "categories": {
        "Men": {
            "Accessories": ["Belts", "Caps, Beanies & hats", "Gloves", "Scarves"],
            "Backpacks & Bags": ["Backpacks", "Bags"],
            "Bottoms": ["Chinos", "Dungarees", "Jeans", "Joggers", "Shorts",
                        "Trousers"],
            "Jackets": ["Bomber Jackets", "Denim Jackets", "Fleece Jackets",
                        "Leather Jackets", "Long Coats", "Outdoor Jackets",
                        "Puffer Jackets", "Sports Jackets", "Windbreakers",
                        "Winter Coats"],
            "Shirts": ["Business Shirts", "Casual Shirts"],
            "Shoes": ["Basketball", "Boots", "Football Boots", "Lifestyle",
                      "Running", "Sandals & Slippers"],
            "Sweaters & Hoodies": ["Fleece Pullover", "Hoodies", "Sweaters",
                                   "Sweatshirts"],
            "T-Shirts & Polos": ["Long Sleeves", "Polo Shirts",
                                 "Sports T-shirts", "Tank Tops", "T-Shirts"],
            "Vests": ["Denim Vests", "Fleece Vests", "Leather Vests",
                      "Puffer Vests"],
        },
        "Women": {
            "Bottoms": ["Chinos", "Dungarees", "Jeans", "Joggers",
                        "Leather Trousers", "Leggings", "Trousers"],
            "Dresses": ["Casual Dresses", "Knitted Dresses", "Occasion Dresses",
                        "Summer Dresses"],
            "T-Shirts & Tops": ["Long-Sleeved Tops", "Polo Shirts",
                                "Sports T-shirts", "Tops", "T-Shirts"],
        },
    },
    "sizingGuides": {
        "Defaults": {
            "sizes": ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "XXXXL"],
            "euSizes": ["42", "44", "46", "48", "50", "52", "54", "56", "58"],
        },
        "Men Bottoms": {
            "sizes": [f"W{n}" for n in range(21, 45)],
            "euSizes": [str(n) for n in range(37, 61)],
        },
        "Men Uppers": {
            "sizes": ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"],
            "euSizes": ["42", "44", "46", "48", "50", "52", "54", "56"],
        },
        "Women Bottoms": {
            "sizes": [f"W{n}" for n in range(23, 41)],
            "euSizes": [str(n) for n in range(31, 49)],
        },
        "Women Uppers": {
            "sizes": ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL"],
            "euSizes": ["32", "34", "36", "38", "40", "42", "44", "46"],
        },
    },
    # Trimmed to what these tests exercise; the real lists hold 63/67/57 entries.
    "brands": ["8848 ALTITUDE", "Adidas", "AGU", "Aigle", "ARC'TERYX",
               "Levi Strauss & Co.", "Nike"],
    "colors": ["Beige", "Blue", "Light Blue", "Slate Blue", "Blue/Green/Cream",
               "Black", "White"],
    "materials": ["Cotton", "Cotton Blend", "Organic Cotton", "Denim",
                  "Polyester", "Wool"],
}

BASE = {
    "id": "1a351701-7568-4414-8975-ce07d664ab95",
    "tenantId": "34c354a5-3415-4513-85b8-d40c2ec3af7e",
    "masterCategory": "Men",
    "category": "Bottoms",
    "subCategory": "Jeans",
    "sizingGuide": "Men Bottoms",
    "mannequinType": "Men Bottom",
    "title": "Regular Levi's Jeans in Blue size W32",
    "description": "Levi Strauss classic blue jeans in denim.",
    "currency": "EUR",
    "priceAmount": 38.99,
    "retailPriceAmount": 66.99,
    "grade": "C",
    "gradeLabel": "Lived In",
    "defects": ["Discoloration / fade"],
    "priceExpectation": {"priceFactor": 0.58, "expectedPrice": 38.85},
    "variantPriceDrift": {"variantBasePrice": 38.99, "drift": 0.0},
    "gender": "men",
    "brand": "Levi Strauss & Co.",
    "color": "Blue",
    "material": "Denim",
    "size": "W32",
    "euSize": "48",
    "waist": "W32",
    "fit": "Regular",
    "condition": "Lived In",
    "sku": "BEV-000325",
    "productCode": "4314131618",
    "inventoryQuantity": 1,
    "images": ["front.jpg"],
}


def snap(**overrides):
    raw = copy.deepcopy(BASE)
    raw.update(overrides)
    return to_snapshot(raw, catalog=CATALOG)


def snap_no_catalog(**overrides):
    raw = copy.deepcopy(BASE)
    raw.update(overrides)
    return to_snapshot(raw)


# --------------------------------------------------------------------------- #
# Taxonomy: the tenant's real tree
# --------------------------------------------------------------------------- #

def test_the_real_levis_record_is_clean_under_every_rule_group():
    assert run_all(snap(), POL) == []


def test_a_mens_tshirt_is_valid_against_the_tenant_tree():
    """`Men > T-Shirts & Polos > T-Shirts` is real. policy.yaml has no such
    category, so validating against it would flag every top in the catalog."""
    p = snap(category="T-Shirts & Polos", subCategory="T-Shirts",
             sizingGuide="Men Uppers", size="L", euSize="50", waist=None)
    assert [f.rule_id for f in check_taxonomy(p, POL)] == []


def test_that_same_tshirt_is_rejected_by_the_policy_fallback():
    """Proves the fallback really is the thing that was breaking, not a strawman."""
    p = snap_no_catalog(category="T-Shirts & Polos", subCategory="T-Shirts")
    ids = {f.rule_id for f in check_taxonomy(p, POL)}
    assert "TAX.002" in ids


def test_findings_record_which_tree_judged_them():
    p = snap(category="Nonexistent Category")
    hit = next(f for f in check_taxonomy(p, POL) if f.rule_id == "TAX.002")
    assert hit.detail["basis"] == "tenant"
    assert "T-Shirts & Polos" in hit.detail["allowed"]


def test_a_genuinely_wrong_category_still_fires():
    p = snap(masterCategory="Men", category="Dresses")
    assert "TAX.002" in {f.rule_id for f in check_taxonomy(p, POL)}


def test_a_leaf_category_with_no_children_cannot_invalidate_a_subcategory():
    """`x not in []` is always true — this used to flag every product under a
    category the tenant had not given children."""
    cat = copy.deepcopy(CATALOG)
    cat["categories"]["Men"]["Vests"] = []
    p = to_snapshot({**BASE, "category": "Vests", "subCategory": "Anything"},
                    catalog=cat)
    assert "TAX.003" not in {f.rule_id for f in check_taxonomy(p, POL)}


def test_no_tree_at_all_says_nothing_rather_than_inventing_one():
    cat = {**copy.deepcopy(CATALOG), "categories": {}}
    p = to_snapshot({**BASE, "category": "Whatever"}, catalog=cat)
    ids = {f.rule_id for f in check_taxonomy(p, POL)}
    assert not (ids & {"TAX.001", "TAX.002", "TAX.003"})


# --------------------------------------------------------------------------- #
# Sizing guide membership (TAX.006)
# --------------------------------------------------------------------------- #

def test_men_uppers_is_an_accepted_sizing_guide():
    """The derived name would be 'Men T-Shirts & Polos' — never a real guide."""
    p = snap(category="T-Shirts & Polos", subCategory="T-Shirts",
             sizingGuide="Men Uppers", size="L", euSize="50", waist=None)
    assert "TAX.006" not in {f.rule_id for f in check_taxonomy(p, POL)}


def test_an_unconfigured_sizing_guide_is_flagged():
    p = snap(sizingGuide="Mens Trousers EU")
    hit = next(f for f in check_taxonomy(p, POL) if f.rule_id == "TAX.006")
    assert "Men Bottoms" in hit.detail["allowed"]


# --------------------------------------------------------------------------- #
# SIZE.002 against the tenant's real chart
# --------------------------------------------------------------------------- #

def test_w32_pairs_with_eu48_on_the_mens_chart():
    assert "SIZE.002" not in {f.rule_id for f in check_sizing(snap(), POL)}


def test_a_wrong_eu_size_is_flagged_from_the_tenant_chart():
    p = snap(euSize="42")
    hit = next(f for f in check_sizing(p, POL) if f.rule_id == "SIZE.002")
    assert hit.detail["expected_eu"] == 48
    assert hit.detail["basis"] == "tenant_chart"


def test_womens_w28_is_eu36_not_the_policy_tables_40():
    """The sharpest case: the generic table is off by 4 here, so a correct
    women's product was being flagged every time."""
    p = snap(masterCategory="Women", category="Bottoms", subCategory="Jeans",
             gender="women", sizingGuide="Women Bottoms",
             size="W28", waist="W28", internationalSize="28", euSize="36",
             mannequinType="Women Bottom",
             title="Levi Strauss Jeans in Blue size W28")
    # Fully clean, not merely SIZE.002-free. Asserting only the one rule let a
    # fixture through that still tripped SIZE.001 (internationalSize left at 32
    # while size moved to W28) — the sample looked correct and was not.
    assert run_all(p, POL) == []
    # And what policy.yaml would have demanded instead:
    assert POL["sizing"]["women_bottoms_waist_in_to_eu"][28] == 40


def test_the_policy_table_flags_that_same_correct_womens_product():
    p = snap_no_catalog(masterCategory="Women", category="Bottoms",
                        gender="women", size="W28", waist="W28", euSize="36")
    hit = next(f for f in check_sizing(p, POL) if f.rule_id == "SIZE.002")
    assert hit.detail["basis"] == "policy_table"


def test_a_waist_outside_the_policy_tables_range_is_still_checked():
    """The tenant chart runs W21-W44; the policy table stops at 40 with gaps."""
    p = snap(size="W35", waist="W35", euSize="60")
    hit = next(f for f in check_sizing(p, POL) if f.rule_id == "SIZE.002")
    assert hit.detail["expected_eu"] == 51


# --------------------------------------------------------------------------- #
# Attribute membership (ATTR.001-003)
# --------------------------------------------------------------------------- #

def test_real_levis_attributes_are_all_in_the_tenant_lists():
    """'Levi Strauss & Co.', 'Blue' and 'Denim' are all really configured."""
    assert check_catalog(snap(), POL) == []


def test_a_colour_the_dropdown_cannot_offer_is_flagged():
    p = snap(color="Cerulean")
    hit = next(f for f in check_catalog(p, POL) if f.rule_id == "ATTR.002")
    assert hit.detail["value"] == "Cerulean"


def test_near_matches_are_offered_so_a_reviewer_can_judge():
    p = snap(color="Blue Denim Wash")
    hit = next(f for f in check_catalog(p, POL) if f.rule_id == "ATTR.002")
    assert any("Blue" in n for n in hit.detail["near"])


def test_punctuation_and_case_differences_are_not_findings():
    """'levi strauss co' must match the configured 'Levi Strauss & Co.'."""
    assert check_catalog(snap(brand="levi strauss co"), POL) == []


def test_a_placeholder_is_not_reported_twice():
    """DATA.001 owns placeholders; ATTR.* must not restate them."""
    ids = {f.rule_id for f in check_catalog(snap(material="Unknown"), POL)}
    assert "ATTR.003" not in ids


def test_membership_is_silent_without_a_catalog():
    assert check_catalog(snap_no_catalog(), POL) == []


def test_unknown_brand_and_material_both_fire():
    p = snap(brand="Acme Denim Co", material="Hemp")
    ids = {f.rule_id for f in check_catalog(p, POL)}
    assert ids == {"ATTR.001", "ATTR.003"}


# --------------------------------------------------------------------------- #
# Copy checks must tolerate singular/plural and punctuation
# --------------------------------------------------------------------------- #

def test_a_singular_title_satisfies_a_plural_subcategory():
    """'Nike T-Shirt' under subcategory 'T-Shirts' is consistent. A literal
    substring test misses it, and TEXT.002 then marks a perfect record wrong."""
    p = snap(category="T-Shirts & Polos", subCategory="T-Shirts", brand="Nike",
             title="Nike T-Shirt in Blue size L", sizingGuide="Men Uppers",
             size="L", euSize="50", waist=None)
    assert "TEXT.002" not in {f.rule_id for f in run_all(p, POL)}


def test_a_plural_title_satisfies_a_singular_subcategory():
    p = snap(subCategory="Jean", title="Levi Strauss Jeans in Blue size W32")
    assert "TEXT.002" not in {f.rule_id for f in run_all(p, POL)}


def test_a_title_that_really_omits_the_colour_is_still_flagged():
    p = snap(title="Levi Strauss Jeans size W32")
    hit = next(f for f in run_all(p, POL) if f.rule_id == "TEXT.002")
    assert "color" in hit.fields
