"""Two planner changes from WP3.

  * CONSIGNMENT — for a tenant on `pricing.consignment_tenants` the gate never
    plans a price write and PRICE.002/003 stop blocking; PRICE.001 still does.
  * THE TENANT'S VOCABULARY — when a title names several valid subcategories,
    the one the tenant files most products under wins, and only when it is
    clearly the house spelling.
"""
from __future__ import annotations

from app import approval
from app.config import policy
from app.models import Finding, Severity
from app.vnyx_client import to_snapshot

POL = policy()
CONSIGN = {**POL, "pricing": {**POL["pricing"], "consignment_tenants": ["t-consign"]}}

TREE = {"Men": {"Sweaters & Hoodies": ["Sweatshirts", "Hoodies", "Fleece Pullover", "Sweaters"]}}


def snap(tenant="t1", usage=None, **over):
    raw = {
        "id": "p1", "tenantId": tenant, "sku": "S1", "masterCategory": "Men",
        "category": "Sweaters & Hoodies", "subCategory": None,
        "title": "Relaxed Hoodie Sweatshirt in Black size M",
        "size": "M", "internationalSize": "M", "euSize": "48",
        "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Black",
        "material": "Cotton", "condition": "As New", "gender": ["men"],
        "careLabelCount": 1, "price": 90.0, "retailPrice": 100.0, "grade": "A",
        # The tenant's own Grade.priceFactor. With it the window findings are
        # HIGH (backend_factor); without it they are MEDIUM policy-band guesses
        # and nothing here would be blocking to begin with.
        "priceExpectation": {"priceFactor": 0.7, "expectedPrice": 70.0},
        **over,
    }
    catalog = {"categories": TREE, "sizingGuides": {"Men Uppers": {"sizes": ["M"], "euSizes": ["48"]}},
               "brands": ["BOAS"], "colors": ["Black"], "materials": ["Cotton"],
               "subcategoryUsage": usage or {}}
    return to_snapshot(raw, catalog=catalog)


# ------------------------------------------------------------- consignment

def test_is_consignment_compares_ids_as_strings():
    assert approval.is_consignment("t-consign", CONSIGN)
    assert not approval.is_consignment("t1", CONSIGN)
    assert not approval.is_consignment(None, CONSIGN)
    assert not approval.is_consignment("t-consign", POL)


def test_price_window_findings_stop_blocking_for_a_consignment_tenant():
    p = snap(tenant="t-consign", price=13.0, retailPrice=100.0)   # far below the A window
    findings = approval._all_findings(p, CONSIGN)
    price = [f for f in findings if f.rule_id in ("PRICE.002", "PRICE.003")]
    assert price, "the window finding should still be reported"
    assert all(f.severity is Severity.MEDIUM for f in price)
    assert not any(f.rule_id in ("PRICE.002", "PRICE.003") for f in approval._blocking(findings))


def test_the_same_product_still_blocks_for_an_ordinary_tenant():
    p = snap(tenant="t1", price=13.0, retailPrice=100.0)
    findings = approval._all_findings(p, POL)
    assert any(f.rule_id == "PRICE.003" for f in approval._blocking(findings))


def test_above_retail_keeps_blocking_even_on_consignment():
    p = snap(tenant="t-consign", price=120.0, retailPrice=100.0)
    findings = approval._all_findings(p, CONSIGN)
    assert any(f.rule_id == "PRICE.001" for f in approval._blocking(findings))


def test_a_price_repair_becomes_an_escalation_on_consignment():
    p = snap(tenant="t-consign", price=60.0, retailPrice=100.0)   # inside auto-apply delta, outside window
    findings = approval._all_findings(p, CONSIGN)
    plan: list[dict] = []
    approval._plan_fields(p, CONSIGN, findings, None, plan)
    writes = [a for a in plan if a["kind"] in ("set_column", "set_property") and a["field"] == "price"]
    assert writes == []
    assert any(a["kind"] == "escalate" and a["field"] == "price"
               and "consignor" in a["detail"] for a in plan)


def test_an_ordinary_tenant_still_gets_the_price_written():
    p = snap(tenant="t1", price=60.0, retailPrice=100.0)
    findings = approval._all_findings(p, POL)
    plan: list[dict] = []
    approval._plan_fields(p, POL, findings, None, plan)
    assert any(a["kind"] in ("set_column", "set_property") and a["field"] == "price" for a in plan)


# ------------------------------------------------------------- tie-breaker

def absent_sub():
    return [Finding(rule_id="DATA.010", severity=Severity.HIGH, fields=["subcategory"],
                    message="Required field 'subcategory' is empty.")]


def test_two_named_options_and_a_clear_house_spelling_picks_it():
    p = snap(usage={"Hoodies": 240, "Sweatshirts": 12})
    plan: list[dict] = []
    approval._plan_subcategory(p, absent_sub(), plan)
    assert [a["value"] for a in plan if a["field"] == "subCategory"] == ["Hoodies"]
    assert "240" in plan[0]["detail"]


def test_a_near_tie_stays_a_human_decision():
    p = snap(usage={"Hoodies": 30, "Sweatshirts": 25})
    plan: list[dict] = []
    approval._plan_subcategory(p, absent_sub(), plan)
    assert plan == []


def test_no_usage_data_means_no_pick():
    p = snap(usage={})
    plan: list[dict] = []
    approval._plan_subcategory(p, absent_sub(), plan)
    assert plan == []


def test_one_named_option_never_needed_the_tie_breaker():
    p = snap(title="Relaxed Hoodie in Black size M", usage={"Sweatshirts": 999})
    plan: list[dict] = []
    approval._plan_subcategory(p, absent_sub(), plan)
    assert [a["value"] for a in plan] == ["Hoodies"]
