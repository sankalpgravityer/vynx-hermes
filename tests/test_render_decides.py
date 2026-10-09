"""The render decides, the agent repairs (8 Oct 2026 — the user's answers for held products).

  * TAX.004 / TAX.005 / SIZE.011 — gender and master disagree: the lead render's model
    decides. Master AND gender go to the render's gender, and the branch, chart and rig
    are planned against that master (MID-000107/111/501).
  * CATEGORY_IMAGE_MISMATCH — the image gate's garment wins: the product is re-filed
    under the tenant's matching subcategory (MID-000615/803, tank tops filed as dresses).
  * NO_SIZING_GUIDE — no guide attached while a tenant chart covers the product: the
    chart is attached (SIZE.015), and vnyx-api syncs its image (MID-000586).
"""
from __future__ import annotations

from app import approval, readiness
from app.config import policy
from app.rules import gate
from app.vnyx_client import to_snapshot

TREE = {
    "Men": {"T-Shirts & Polos": ["Sports T-shirt", "Tank Top", "Polo Shirt", "T-Shirt"],
            "Sweaters & Hoodies": ["Sweater", "Hoodie"]},
    "Women": {"T-Shirts & Tops": ["Sports T-shirts", "Long-Sleeved Tops", "Polo Shirts", "Tops", "T-Shirts"],
              "Dresses": ["Casual Dress", "Occasion Dress", "Summer Dress"]},
}
GUIDES = {"Men Uppers": {"sizes": ["S", "M", "L", "XL"], "euSizes": ["46", "48", "50", "52"]},
          "Women Uppers": {"sizes": ["XS", "S", "M", "L", "XL"], "euSizes": ["34", "36", "38", "40", "42"]}}
CATALOG = {"categories": TREE, "sizingGuides": GUIDES, "brands": ["Nike"],
           "colors": ["Black"], "materials": ["Cotton"]}


def raw(**over):
    return {
        "id": "p1", "tenantId": "t1", "sku": "MID-000107", "productCode": "PC1",
        "masterCategory": "Women", "category": "T-Shirts & Tops", "subCategory": "T-Shirts",
        "size": "M", "internationalSize": "M", "euSize": "38", "sizingGuide": "Women Uppers",
        "mannequinType": "Women Top", "brand": "Nike", "color": "Black", "material": "Cotton",
        "condition": "As New", "gender": ["women"], "careLabelCount": 1,
        "priceAmount": 17.39, "retailPriceAmount": 28.99, "currency": "EUR", "grade": "A",
        "gradeLabel": "As New", "priceExpectation": {"priceFactor": 0.60, "expectedPrice": 17.39},
        "title": "Vintage Nike Black T-Shirt Women M", "description": "A black tee.",
        **over,
    }


def plan_of(out, kind=("set_column", "set_property")):
    return {a["field"]: a["value"] for a in out["repair_plan"] if a["kind"] in kind}


# --------------------------------------------------------------------------- #
# The render decides a gender conflict
# --------------------------------------------------------------------------- #

def test_decide_master_follows_the_render_only_on_a_conflict():
    pol = policy()
    conflict = to_snapshot(raw(masterCategory="Men", category="T-Shirts & Polos",
                               subCategory="T-Shirt", gender=["women"]), catalog=CATALOG)
    d = readiness.decide_master(conflict, pol, render_gender="women")
    assert (d["action"], d["master"], d["basis"], d["rule"]) == ("set", "Women", "render model", "TAX.004")
    # The render agrees with the master: the master stands, gender follows it.
    assert readiness.decide_master(conflict, pol, render_gender="men")["action"] == "keep"
    # No render read: as before.
    assert readiness.decide_master(conflict, pol)["action"] == "keep"
    # No conflict: the render is not consulted.
    clean = to_snapshot(raw(), catalog=CATALOG)
    assert readiness.decide_master(clean, pol, render_gender="men")["action"] == "keep"


def test_mid_000107_shape_master_and_gender_go_to_the_render():
    """Men master over a women's record (gender, rig and chart all women's), render women."""
    out = approval.run_gate(
        raw(masterCategory="Men", category="T-Shirts & Polos", subCategory="T-Shirt",
            gender=["women"], mannequinType="Women Top", sizingGuide="Women Uppers"),
        catalog=CATALOG, hints={"render_gender": "women"})
    p = plan_of(out)
    assert p["masterCategory"] == "Women"
    assert p.get("category") == "T-Shirts & Tops"
    # Gender, rig and chart were already women's: nothing turns them to men.
    assert p.get("gender", ["women"]) == ["women"]
    assert p.get("mannequinType", "Women Top") == "Women Top"
    assert p.get("sizingGuide", "Women Uppers") == "Women Uppers"


def test_mid_000501_shape_render_men_turns_the_record_to_men():
    """Women master, gender men, men's rig and chart; the render is a man."""
    out = approval.run_gate(
        raw(masterCategory="Women", category="T-Shirts & Tops", subCategory="Sports T-shirts",
            gender=["men"], mannequinType="Men Top", sizingGuide="Men Uppers", euSize="48"),
        catalog=CATALOG, hints={"render_gender": "men"})
    p = plan_of(out)
    assert p["masterCategory"] == "Men"
    assert p.get("category") == "T-Shirts & Polos"
    assert p.get("gender", ["men"]) == ["men"]


def test_without_a_render_read_the_master_still_anchors():
    out = approval.run_gate(
        raw(masterCategory="Men", category="T-Shirts & Polos", subCategory="T-Shirt",
            gender=["women"], mannequinType="Women Top", sizingGuide="Women Uppers"),
        catalog=CATALOG)
    p = plan_of(out)
    assert "masterCategory" not in p
    assert p["gender"] == ["men"]


# --------------------------------------------------------------------------- #
# The image gate's garment re-files the product
# --------------------------------------------------------------------------- #

def test_mid_000803_tank_top_filed_as_a_dress_moves_to_tops():
    out = approval.run_gate(raw(category="Dresses", subCategory="Casual Dress"),
                            catalog=CATALOG, hints={"render_garment": "tank top"})
    p = plan_of(out)
    # Women has no "Tank Top"; "Tops" is the leaf the garment's words contain.
    assert (p["category"], p["subCategory"]) == ("T-Shirts & Tops", "Tops")
    reasons = {a["reason"] for a in out["repair_plan"] if a["field"] in ("category", "subCategory")}
    assert reasons == {"CATEGORY_IMAGE_MISMATCH"}


def test_an_exact_leaf_wins_and_a_tie_takes_the_tenants_first():
    men = approval.run_gate(raw(masterCategory="Men", category="T-Shirts & Polos",
                                subCategory="Polo Shirt", gender=["men"],
                                mannequinType="Men Top", sizingGuide="Men Uppers", euSize="48"),
                            catalog=CATALOG, hints={"render_garment": "tank top"})
    assert plan_of(men)["subCategory"] == "Tank Top"
    dress = approval.run_gate(raw(), catalog=CATALOG, hints={"render_garment": "dress"})
    assert (plan_of(dress)["category"], plan_of(dress)["subCategory"]) == ("Dresses", "Casual Dress")


def test_a_garment_the_tree_does_not_offer_plans_nothing():
    out = approval.run_gate(raw(), catalog=CATALOG, hints={"render_garment": "spacesuit"})
    assert "category" not in plan_of(out) and "subCategory" not in plan_of(out)
    # Already filed where the garment belongs: nothing to do either.
    out = approval.run_gate(raw(subCategory="Tops"), catalog=CATALOG,
                            hints={"render_garment": "tank top"})
    assert "subCategory" not in plan_of(out)


# --------------------------------------------------------------------------- #
# A guide that covers the product is attached
# --------------------------------------------------------------------------- #

def test_mid_000586_no_guide_attached_is_planned_from_the_covering_chart():
    p = to_snapshot(raw(masterCategory="Men", category="Sweaters & Hoodies", subCategory="Sweater",
                        gender=["men"], mannequinType="Men Top", sizingGuide=None,
                        size="XL", internationalSize="XL", euSize="52"), catalog=CATALOG)
    f = [x for x in gate.check_gate(p, policy()) if x.rule_id == "SIZE.015"]
    assert f and f[0].detail["suggested"] == ["Men Uppers"]
    out = approval.run_gate(raw(masterCategory="Men", category="Sweaters & Hoodies",
                                subCategory="Sweater", gender=["men"], mannequinType="Men Top",
                                sizingGuide=None, size="XL", internationalSize="XL", euSize="52"),
                            catalog=CATALOG)
    assert plan_of(out)["sizingGuide"] == "Men Uppers"


def test_a_product_with_its_guide_is_untouched():
    p = to_snapshot(raw(), catalog=CATALOG)
    assert not [x for x in gate.check_gate(p, policy()) if x.rule_id == "SIZE.015"]
