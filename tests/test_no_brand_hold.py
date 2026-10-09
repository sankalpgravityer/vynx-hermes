"""A brand that says "no brand" is a missing brand — held for a person (DATA.011, 8 Oct 2026).

36 approved Midtex products had been VERIFIED with brands like "MT NO BRAND", "NOBRAND"
and "MK No Brand": a value was there (DATA.010 silent), it was in the tenant's brand list
(ATTR.001 silent), and DATA.001's placeholder finding is MEDIUM, below the gate. What must
hold now:

  * every spelling of "no brand", plus "BOAS", "BRAND" and the empty placeholders, is
    caught on the brand FIELD or the brand RECORD, in any case;
  * a real brand — including one that merely contains the letters, or "MT" alone — is not;
  * the finding blocks approval and is escalated to a person, never repaired.
"""
from __future__ import annotations

import pytest

from app import approval
from app.config import policy
from app.rules import gate
from app.vnyx_client import to_snapshot

GUIDES = {"Men Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]}}
TREE = {"Men": {"T-Shirts & Polos": ["T-Shirts"]}}


def raw(**over):
    return {
        "id": "p1", "tenantId": "t1", "sku": "MID-000009", "productCode": "PC1",
        "masterCategory": "Men", "category": "T-Shirts & Polos", "subCategory": "T-Shirts",
        "size": "S", "internationalSize": "S", "euSize": "46", "sizingGuide": "Men Uppers",
        "brand": "Nike", "color": "Burgundy", "material": "Cotton", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1, "priceAmount": 17.39,
        "retailPriceAmount": 28.99, "currency": "EUR", "grade": "A", "gradeLabel": "As New",
        "priceExpectation": {"priceFactor": 0.60, "expectedPrice": 17.39},
        "title": "Vintage Burgundy T-Shirt Men S",
        "description": "A deep burgundy cotton jersey tee in as-new condition.",
        **over,
    }


CATALOG = {"categories": TREE, "sizingGuides": GUIDES,
           "brands": ["Nike", "MT NO BRAND", "NOBRAND", "MK No Brand", "BOAS"],
           "colors": ["Burgundy"], "materials": ["Cotton"]}


def findings(**over):
    p = to_snapshot(raw(**over), catalog=CATALOG)
    return [f for f in gate.check_gate(p, policy()) if f.rule_id == "DATA.011"]


@pytest.mark.parametrize("brand", [
    "MT NO BRAND", "NOBRAND", "MK No Brand", "Mt Nobrand", "MT NOBRAND", "No Brand",
    "no-brand", "BOAS", "boas", "BRAND", "Unknown", "n/a",
])
def test_every_no_brand_spelling_is_held(brand):
    (f,) = findings(brand=brand)
    assert f.severity.value == "high" and f.fields == ["brand"]
    assert f'"{brand}"' in f.message and "Brand is missing" in f.message


@pytest.mark.parametrize("brand", ["Nike", "Levi's", "MT", "Brandit", "Boast", "Ragman", "UNIQLO"])
def test_a_real_brand_is_not(brand):
    assert findings(brand=brand) == []


def test_the_brand_record_counts_even_when_the_field_names_a_brand():
    """MID-000137: field "UNIQLO", record "NOBRAND" — a person decides which is true."""
    (f,) = findings(brand="UNIQLO", brandRelation="NOBRAND")
    assert f.detail["values"] == ["NOBRAND"]
    # Field and record saying the same placeholder are named once.
    (g,) = findings(brand="NOBRAND", brandRelation="NOBRAND")
    assert g.detail["values"] == ["NOBRAND"]


def test_an_empty_brand_stays_data_010s():
    assert findings(brand="", brandRelation=None) == []


def test_the_gate_holds_it_for_a_person_and_repairs_nothing():
    out = approval.run_gate(raw(brand="MT NO BRAND"), catalog=CATALOG)
    assert out["verified"] is False
    assert out["human_intervention_needed"] is True
    blob = str(out)
    assert "DATA.011" in blob and "MT NO BRAND" in blob


def test_policy_can_change_the_list():
    pol = {**policy(), "completeness": {"no_brand": {"equals": ["generic"], "contains": []}}}
    p = to_snapshot(raw(brand="Generic"), catalog=CATALOG)
    assert [f.rule_id for f in gate.check_gate(p, pol) if f.rule_id == "DATA.011"] == ["DATA.011"]
    p = to_snapshot(raw(brand="MT NO BRAND"), catalog=CATALOG)
    assert not [f for f in gate.check_gate(p, pol) if f.rule_id == "DATA.011"]
