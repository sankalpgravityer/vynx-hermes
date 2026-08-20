"""The review-queue sweep: feed mapping, the tenant-factor price rule, and the
false positives that "run every rule group" would otherwise produce.

The fixture is the shape `GET /review-verification/feed` on vnyx-api actually
emits — camelCase keys, `properties` already flattened, the tenant's grade row
joined, and the price expectation and variant-mirror drift precomputed. Writing
the tests against that shape rather than a hand-rolled snapshot is the point:
the adapter mapping is where an integration silently reads None.
"""

from __future__ import annotations

import copy

import pytest

from app.config import policy
from app.models import PriceVerdict, Provenance
from app.rules import run_all
from app.rules.pricing import assess, window_for
from app.vnyx_client import to_snapshot

POL = policy()

# A clean product: price 38.99 against a Grade C priceFactor of 0.58 on a
# retail of 66.99 (expected 38.85), inside the +/-20% factor window.
FEED = {
    "id": "1a351701-7568-4414-8975-ce07d664ab95",
    "tenantId": "34c354a5-3415-4513-85b8-d40c2ec3af7e",
    "editUrl": (
        "https://dev.vnyx.ai/product/1a351701-7568-4414-8975-ce07d664ab95/edit"
        "?tenantId=34c354a5-3415-4513-85b8-d40c2ec3af7e&fromTab=pending"
    ),
    "sku": "BEV-000325",
    "productCode": "4314131618",
    "hanger": "AM17226",
    "lpnCode": None,
    "binCode": "A-02-12-3",
    "binNumber": "8871203344",
    "binZoneCode": "A",
    "binWarehouseCode": "WH1",
    "masterCategory": "Men",
    "category": "Bottoms",
    "subCategory": "Jeans",
    "sizingGuide": "Men Bottoms",
    "mannequinType": "Men Bottom",
    "title": "Regular Levi's Jeans in Blue size 32",
    "description": "Levi Strauss classic blue jeans, straight regular fit in denim.",
    "currency": "EUR",
    "price": "38.99",
    "priceAmount": 38.99,
    "retailPrice": "66.99",
    "retailPriceAmount": 66.99,
    "inventoryQuantity": 1,
    "retailProvenance": "market",
    "retailSource": "EBAY_NEW",
    "grade": "C",
    "gradeLabel": "Lived In",
    "gradeSeverity": "moderate",
    "defects": ["Discoloration / fade", "Snag / pull"],
    "operatorDefects": [],
    "priceExpectation": {
        "priceFactor": 0.58,
        "expectedPrice": 38.85,
        "delta": 0.14,
        "withinTolerance": False,
        "skipped": None,
    },
    "variantPriceDrift": {
        "variantBasePrice": 38.99,
        "variantCurrency": "EUR",
        "mirrorInput": 38.99,
        "drift": 0.0,
        "inSync": True,
    },
    "gender": "men",
    "brand": "Levi Strauss & Co.",
    "color": "Blue",
    "material": "Denim",
    "size": "32",
    "euSize": "48",
    "internationalSize": "32",
    "waist": "W32",
    "lengthSize": "34",
    "fit": "Regular",
    "condition": "Lived In",
    "model": "501",
    "supplier": "Acme Sourcing",
    "brandRelation": "Levi Strauss & Co.",
    "properties": {},
    "propertyConfidence": {"brand": 99, "color": 88, "material": 90},
    "reviewStatus": "PENDING",
    "currentStage": "REVIEW",
    "generationStatus": "COMPLETE",
    "source": "DECISION",
    "images": ["front.jpg"],
}


def feed(**overrides) -> dict:
    raw = copy.deepcopy(FEED)
    raw.update(overrides)
    return raw


# --------------------------------------------------------------------------- #
# Feed → snapshot mapping
# --------------------------------------------------------------------------- #

def test_feed_maps_every_field_the_rules_read():
    """A field that fails to map reads as None, which rules score as a violation."""
    p = to_snapshot(feed())
    assert p.id == FEED["id"]
    assert p.tenant_id == FEED["tenantId"]
    assert p.edit_url == FEED["editUrl"]
    # `summary` in VNYX, `description` to the rules.
    assert p.description.startswith("Levi Strauss")
    # mannequinType -> mannequin
    assert p.mannequin == "Men Bottom"
    assert p.price == 38.99 and p.retail_price == 66.99
    # The tenant's own grade config, lifted out of the nested expectation block.
    assert p.price_factor == 0.58
    assert p.expected_price == 38.85
    assert p.grade == "C" and p.grade_label == "Lived In"
    assert p.variant_base_price == 38.99 and p.variant_price_drift == 0.0
    # propertyConfidence arrives as 0-100 and must be scaled to 0-1.
    assert p.confidence["brand"] == pytest.approx(0.99)


def test_retail_provenance_comes_from_the_breakdown_not_a_default():
    """'market' (eBay comparables) must not be flattened to the DERIVED default."""
    assert to_snapshot(feed()).prov("retail_price") is Provenance.MARKET
    assert to_snapshot(feed(retailProvenance="derived")).prov("retail_price") \
        is Provenance.DERIVED


def test_price_amount_is_preferred_over_the_formatted_string():
    """VNYX stores both "38.99" and "€38.99"; the feed pre-parses to avoid drift."""
    p = to_snapshot(feed(price="€38.99", priceAmount=38.99))
    assert p.price == 38.99


def test_symbol_prefixed_price_still_parses_without_a_preparsed_amount():
    raw = feed(price="€38.99")
    del raw["priceAmount"]
    assert to_snapshot(raw).price == 38.99


# --------------------------------------------------------------------------- #
# The price rule: the tenant's own Grade.priceFactor decides
# --------------------------------------------------------------------------- #

def test_tenant_factor_beats_the_policy_band():
    """0.58 comes from the tenant's grade ladder, not grade_targets C = 0.40."""
    a = assess(to_snapshot(feed()), POL)
    assert a.rule == "backend_factor"
    assert a.price_factor == 0.58
    assert a.expected_price == 38.85
    assert a.verdict is PriceVerdict.OK
    # The policy band would have called the same product too_high (window 21-32).
    assert window_for("C", POL)["target"] == 0.40


def test_policy_band_is_the_fallback_and_says_so():
    raw = feed()
    raw["priceExpectation"] = {"priceFactor": None, "expectedPrice": None,
                               "delta": None, "withinTolerance": None,
                               "skipped": "no_price_factor_configured"}
    p = to_snapshot(raw)
    a = assess(p, POL)
    assert a.rule == "policy_band"
    # ...and the fallback is reported, never silent.
    assert "PRICE.101" in {f.rule_id for f in run_all(p, POL)}


def test_price_differing_from_the_tenant_factor_is_flagged():
    """The bug this endpoint exists for: price disagrees with the grade ladder."""
    p = to_snapshot(feed(price="58.99", priceAmount=58.99))
    a = assess(p, POL)
    assert a.verdict is PriceVerdict.TOO_HIGH
    assert "PRICE.002" in {f.rule_id for f in run_all(p, POL)}


def test_price_below_the_factor_window_is_flagged_as_underselling():
    p = to_snapshot(feed(price="12.00", priceAmount=12.0))
    a = assess(p, POL)
    assert a.verdict is PriceVerdict.TOO_LOW
    assert "PRICE.003" in {f.rule_id for f in run_all(p, POL)}


def test_price_at_or_above_retail_stays_critical_whatever_the_factor_says():
    """hard_max_ratio is independent of the ladder — arithmetic, not opinion."""
    p = to_snapshot(feed(price="66.99", priceAmount=66.99))
    findings = {f.rule_id: f for f in run_all(p, POL)}
    assert findings["PRICE.001"].severity.value == "critical"


def test_a_misconfigured_factor_is_reported_once_not_per_product():
    """A factor at/above the ceiling is a CONFIG bug, with its own rule id."""
    raw = feed()
    raw["priceExpectation"] = {**raw["priceExpectation"], "priceFactor": 0.97}
    ids = {f.rule_id for f in run_all(to_snapshot(raw), POL)}
    assert "PRICE.102" in ids


def test_exact_factor_match_distinguishes_regraded_from_analyze_priced():
    """price == retail * factor to the cent only after a manual regrade."""
    assert assess(to_snapshot(feed(priceAmount=38.85)), POL).exact_factor_match is True
    assert assess(to_snapshot(feed(priceAmount=38.99)), POL).exact_factor_match is False


# --------------------------------------------------------------------------- #
# Mirror drift — visible only to the backend
# --------------------------------------------------------------------------- #

def test_variant_mirror_drift_is_flagged():
    raw = feed()
    raw["variantPriceDrift"] = {"variantBasePrice": 38.99, "variantCurrency": "EUR",
                                "mirrorInput": 58.99, "drift": 20.0, "inSync": False}
    raw["price"], raw["priceAmount"] = "58.99", 58.99
    ids = {f.rule_id for f in run_all(to_snapshot(raw), POL)}
    assert "PRICE.110" in ids


def test_a_cent_of_mirror_drift_is_rounding_not_a_finding():
    raw = feed()
    raw["variantPriceDrift"] = {**raw["variantPriceDrift"], "drift": 0.01,
                                "inSync": True}
    assert "PRICE.110" not in {f.rule_id for f in run_all(to_snapshot(raw), POL)}


# --------------------------------------------------------------------------- #
# False positives that "all rule groups" would otherwise produce
# --------------------------------------------------------------------------- #

def test_a_clean_product_produces_no_findings_at_all():
    """The whole point: a correct record must come back correct under every group."""
    assert run_all(to_snapshot(feed()), POL) == []


def test_an_unbinned_product_is_not_an_identity_violation():
    """Putaway happens AFTER review, so no bin/LPN is the norm in this queue."""
    p = to_snapshot(feed(binCode=None, binNumber=None, binZoneCode=None,
                         binWarehouseCode=None, lpnCode=None))
    assert "ID.003" not in {f.rule_id for f in run_all(p, POL)}


def test_a_present_but_placeholder_bin_code_is_still_a_violation():
    ids = {f.rule_id for f in run_all(to_snapshot(feed(binCode="unknown")), POL)}
    assert "ID.003" in ids


def test_bin_code_outside_its_zone_is_flagged():
    ids = {f.rule_id for f in run_all(to_snapshot(feed(binZoneCode="C")), POL)}
    assert "ID.004" in ids


def test_id001_stays_dormant_without_a_duplicate_location_code():
    """VNYX has no second copy of the location code; mapping binNumber would
    have fired this CRITICAL rule on every binned product."""
    p = to_snapshot(feed())
    assert p.bin_barcode is None
    assert "ID.001" not in {f.rule_id for f in run_all(p, POL)}


def test_unisex_gender_does_not_disagree_with_its_master_category():
    """normalizeGenderToArray expands 'unisex' to both, joined to "men, women"."""
    p = to_snapshot(feed(gender="men, women"))
    assert "TAX.004" not in {f.rule_id for f in run_all(p, POL)}


def test_a_genuinely_wrong_gender_is_still_flagged():
    ids = {f.rule_id for f in run_all(to_snapshot(feed(gender="women")), POL)}
    assert "TAX.004" in ids


def test_condition_is_checked_against_the_tenants_own_grade_label():
    """Exact invariant in VNYX — condition is synced to the label on any grade write."""
    ids = {f.rule_id for f in run_all(to_snapshot(feed(condition="As New")), POL)}
    assert "GRADE.001" in ids


def test_a_custom_grade_label_is_not_a_false_positive():
    """A tenant labelling Grade C 'Well Loved' must not be flagged on every row —
    which the English condition_to_grade table alone would do."""
    p = to_snapshot(feed(grade="C", gradeLabel="Well Loved", condition="Well Loved"))
    assert "GRADE.001" not in {f.rule_id for f in run_all(p, POL)}


def test_operator_reported_defects_count_as_defects():
    """They REPLACE the AI's list in VNYX, so a C grade with only operator
    defects is not defect-free."""
    p = to_snapshot(feed(defects=[], operatorDefects=["D04"]))
    assert "GRADE.003" not in {f.rule_id for f in run_all(p, POL)}


def test_a_grade_c_with_no_defects_from_either_source_is_flagged():
    p = to_snapshot(feed(defects=[], operatorDefects=[]))
    assert "GRADE.003" in {f.rule_id for f in run_all(p, POL)}


def test_missing_price_is_not_reported_twice():
    """PRICE.010 owns it; DATA.001 used to duplicate it at a vaguer severity."""
    raw = feed(price=None, priceAmount=None)
    ids = [f.rule_id for f in run_all(to_snapshot(raw), POL)]
    assert "PRICE.010" in ids
    assert not [i for i in ids if i == "DATA.001"]


# --------------------------------------------------------------------------- #
# The grade percentage comes from the DB, not policy.yaml
# --------------------------------------------------------------------------- #

def test_the_db_factor_drives_the_expected_price():
    """Change the tenant's Regrade Factor, get a different expected price."""
    for factor, expected in ((0.50, 33.50), (0.25, 16.75), (0.80, 53.59)):
        raw = feed()
        raw["priceExpectation"] = {**raw["priceExpectation"], "priceFactor": factor}
        a = assess(to_snapshot(raw), POL)
        assert a.rule == "backend_factor"
        assert a.price_factor == factor
        assert a.expected_price == pytest.approx(expected, abs=0.01)


def test_policy_grade_targets_are_ignored_when_a_db_factor_exists():
    """The decisive check: wreck policy.yaml and the output must not move."""
    import copy
    wrecked = copy.deepcopy(POL)
    wrecked["pricing"]["grade_targets"] = {"A": 9.99, "B": 9.99, "C": 9.99, "D": 9.99}
    p = to_snapshot(feed())
    assert assess(p, wrecked).expected_price == assess(p, POL).expected_price
    assert assess(p, wrecked).rule == "backend_factor"


def test_a_tenant_grade_code_outside_the_policy_table_still_uses_its_db_factor():
    """Grades are per-tenant free text; 'E' is as valid as 'C'."""
    raw = feed(grade="E", gradeLabel="Salvage", condition="Salvage")
    raw["priceExpectation"] = {**raw["priceExpectation"], "priceFactor": 0.35}
    a = assess(to_snapshot(raw), POL)
    assert a.rule == "backend_factor"
    assert a.expected_price == pytest.approx(66.99 * 0.35, abs=0.01)


def test_an_unrecognised_grade_without_a_factor_says_the_default_is_unrelated():
    """Reporting a bare '0.4' for grade E reads as if 0.4 were E's configured
    value. It is grade C's, borrowed, and the message has to say so."""
    raw = feed(grade="E", gradeLabel="Salvage", condition="Salvage")
    raw["priceExpectation"] = {"priceFactor": None, "expectedPrice": None,
                               "delta": None, "withinTolerance": None,
                               "skipped": "no_price_factor_configured"}
    f = next(x for x in run_all(to_snapshot(raw), POL) if x.rule_id == "PRICE.101")
    assert f.detail["grade_recognised"] is False
    assert f.detail["fallback_grade"] == "C"
    assert "nothing to do with grade E" in f.message


def test_a_recognised_grade_without_a_factor_names_its_own_default():
    raw = feed(grade="A", gradeLabel="As New", condition="As New", defects=[])
    raw["priceExpectation"] = {"priceFactor": None, "expectedPrice": None,
                               "delta": None, "withinTolerance": None,
                               "skipped": "no_price_factor_configured"}
    f = next(x for x in run_all(to_snapshot(raw), POL) if x.rule_id == "PRICE.101")
    assert f.detail["grade_recognised"] is True
    assert f.detail["fallback_grade"] == "A"
    assert "policy default for grade A" in f.message
