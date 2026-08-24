"""Tests. The fixtures use the real Levi's record from the VNYX screenshots."""

from __future__ import annotations

import pytest

from app.config import policy
from app.models import (
    Evidence, PriceVerdict, Provenance, ProductSnapshot, RRPEvidence,
)
from app.pipeline import reconcile
from app.resolver import resolve_pricing
from app.rules import run_all
from app.rules.pricing import assess, charm, verify_invariants, window_for

POL = policy()


def levis(**overrides) -> ProductSnapshot:
    base = dict(
        id="1a351701-7568-4414-8975-ce07d664ab95",
        sku="BEV-000325",
        product_code="4314131618",
        lpn_code="STCR-10032801",
        bin_code="ZONE A-02-12-3",
        bin_barcode="A-04-10-4",
        master_category="Men",
        category="Bottoms",
        subcategory="Jeans",
        title="Regular Levi's Jeans in Blue size 32",
        description="Levi Strauss & Co. classic blue jeans with a straight, regular fit.",
        currency="EUR",
        price=38.99,
        retail_price=66.99,
        gender="Men",
        sizing_guide="Men Bottoms",
        international_size="32",
        eu_size="42",
        size="32",
        waist="W32",
        length_size="34",
        fit="Regular",
        brand="Levi Strauss & Co.",
        color="Blue",
        material="Denim",
        condition="Lived In",
        grade="C",
        defects=["Discoloration / fade", "Snag / pull"],
        mannequin="Women Top",
        provenance={"price": Provenance.MARKET, "retail_price": Provenance.DERIVED},
    )
    base.update(overrides)
    return ProductSnapshot(**base)


# --------------------------------------------------------------------------- #

def test_real_record_flags_all_three_defects():
    findings = run_all(levis(), POL)
    ids = {f.rule_id for f in findings}
    assert "PRICE.002" in ids   # 0.58 ratio on a Grade C item
    assert "TAX.005" in ids     # Women Top mannequin on men's jeans
    assert "ID.001" in ids      # bin chip vs bin barcode
    assert "SIZE.002" in ids    # EU 42 does not convert from W32


def test_price_above_retail_is_critical():
    findings = run_all(levis(price=79.99, retail_price=66.99), POL)
    hit = next(f for f in findings if f.rule_id == "PRICE.001")
    assert hit.severity.value == "critical"


# --------------------------------------------------------------------------- #
# The price model, exactly as specified: Grade C = 40% target, +/-20% relative,
# giving a 32%-48% window. Outside -> clamp to nearest edge. Inside -> untouched.
# --------------------------------------------------------------------------- #

def _at(price, retail=100.0, grade="C"):
    return assess(ProductSnapshot(id="t", price=price, retail_price=retail,
                                  grade=grade, currency="EUR"), POL)


# `round_mode: charm99` is the shipped default: every price and both window
# bounds end .99, so a Grade C window on a 100.00 retail reads 32.99-48.99
# rather than 32.00-48.00. The raw arithmetic is unchanged — only the rounding.
# EXACT_POL below keeps the un-rounded behaviour under test too.
EXACT_POL = {**POL, "pricing": {**POL["pricing"], "round_mode": "exact"}}


def test_window_is_target_plus_minus_relative_tolerance():
    w = window_for("C", POL)
    assert w["target"] == 0.40
    assert round(w["low"], 4) == 0.32
    assert round(w["high"], 4) == 0.48
    # Ratios are untouched by rounding; the BOUNDS are what gets charmed.
    a = _at(40.0)
    assert (a.min_allowed, a.max_allowed, a.expected_price) == (32.99, 48.99, 40.0)
    assert a.discount_pct == 0.60


def test_exact_mode_still_reports_raw_bounds():
    """The rounding is a mode, not a rewrite of the arithmetic."""
    a = assess(ProductSnapshot(id="t", price=40.0, retail_price=100.0,
                               grade="C", currency="EUR"), EXACT_POL)
    assert (a.min_allowed, a.max_allowed) == (32.0, 48.0)


@pytest.mark.parametrize("price,verdict,corrected", [
    (13.0, PriceVerdict.TOO_LOW, 32.99),    # way under -> clamp up to minimum
    (32.98, PriceVerdict.TOO_LOW, 32.99),   # a cent under -> still clamped
    (32.99, PriceVerdict.OK, 32.99),        # exactly on the lower edge -> ok
    (40.0, PriceVerdict.ROUND_REQUIRED, 40.99),   # on target, but not a .99
    (44.0, PriceVerdict.ROUND_REQUIRED, 44.99),   # in range, cents rounded
    (48.99, PriceVerdict.OK, 48.99),        # exactly on the upper edge -> ok
    (48.5, PriceVerdict.ROUND_REQUIRED, 48.99),   # up to the edge, not past it
    (49.0, PriceVerdict.TOO_HIGH, 48.99),   # a cent over -> clamped
    (78.0, PriceVerdict.TOO_HIGH, 48.99),   # way over -> clamp down to maximum
])
def test_the_specified_examples(price, verdict, corrected):
    a = _at(price)
    assert a.verdict is verdict
    assert a.corrected_price == corrected


def test_charming_the_lower_bound_makes_the_window_stricter():
    """A deliberate consequence, worth a test of its own.

    32.00 sat exactly on the old lower edge and passed. With the bound at 32.99
    it is now below the minimum and gets lifted. Charm pricing narrows the floor
    by up to 99 cents — accepted, because a shop that wants .99 endings does not
    want a 32.00 shelf price either.
    """
    assert _at(32.0).verdict is PriceVerdict.TOO_LOW
    assert _at(32.0).corrected_price == 32.99
    # ...and it never quietly discounts: the correction moved the price UP.
    assert _at(32.0).corrected_price > 32.0


def test_price_inside_window_that_already_ends_99_is_never_touched():
    for price in (32.99, 35.99, 40.99, 48.99):
        a = _at(price)
        assert a.verdict is PriceVerdict.OK
        assert a.change_required is False
        assert a.corrected_price == price
    p = levis(price=26.99, retail_price=66.99)   # Grade C window 21.99-32.99
    assert not [x for x in resolve_pricing(p, run_all(p, POL), Evidence(), POL)
                if x.field == "price"]


def test_price_inside_window_is_still_rounded_to_99():
    """15.60 and 18.40 both PASS their window — they are the endings this mode
    exists to remove, so being in range is not a licence to leave the cents."""
    a = _at(45.6)
    assert a.verdict is PriceVerdict.ROUND_REQUIRED
    assert a.change_required is True
    assert a.corrected_price == 45.99

    # ...and it reaches the resolver as a patch, not just a report.
    p = levis(price=26.4, retail_price=66.99)   # Grade C window 21.99-32.99
    patch = next(x for x in resolve_pricing(p, run_all(p, POL), Evidence(), POL)
                 if x.field == "price")
    assert (patch.new_value, patch.rule_id) == (26.99, "PRICE.004")
    assert patch.action.value == "apply"


def test_a_rounding_is_reported_on_the_verdict_not_as_a_finding():
    """Two consumers make this the only workable shape.

    A finding would mark most of a correct catalog as incorrect. And vnyx-api's
    verify route lists any finding outside PRICE_WRITE_RESOLVES as still
    outstanding, so a rounding finding would be reported as unresolved right
    after the write that fixed it. Meanwhile that same route gates its write on
    `price.verdict !== 'ok'` — so the verdict is where this has to live.
    """
    p = levis(price=26.4, retail_price=66.99, bin_barcode="ZONE A-02-12-3",
              mannequin="Men Bottom", eu_size="48")
    findings = run_all(p, POL)
    assert not [f for f in findings if f.rule_id.startswith("PRICE.00")]
    assert assess(p, POL).verdict is PriceVerdict.ROUND_REQUIRED

    # No finding, yet still a repair: the record has no DEFECT, it just has a
    # price the shop would not display. Nothing is blocked and nothing needs a
    # human — the pipeline reports it as repaired because there is a patch.
    result = reconcile(p, apply=False)
    assert result.status.value == "repaired"
    assert result.publishable is True
    assert [x.field for x in result.patches] == ["price"]


def test_clamp_goes_to_nearest_edge_not_to_target():
    """The correction must be minimal — 78 becomes 48.99, not 40."""
    assert _at(78.0).corrected_price == 48.99
    assert _at(13.0).corrected_price == 32.99


def test_every_price_it_writes_ends_in_99():
    """The point of the exercise. Whatever comes in, what comes out is a .99."""
    for price in (0.5, 13.0, 32.0, 45.6, 78.0, 1234.0):
        a = _at(price)
        cents = round(a.corrected_price % 1, 2)
        assert cents == 0.99, f"{price} -> {a.corrected_price}"


@pytest.mark.parametrize("grade,expected_window", [
    ("A", (56.99, 84.99)),
    ("B", (44.99, 66.99)),
    ("C", (32.99, 48.99)),
    ("D", (9.99, 20.99)),   # tolerance_overrides gives D a wider +/-35%
])
def test_every_grade_window(grade, expected_window):
    a = _at(40.0, grade=grade)
    assert (a.min_allowed, a.max_allowed) == expected_window


def test_the_retail_ceiling_survives_rounding_up():
    """charm99 rounds up, which must not push the top of the window past
    hard_max_ratio — that is what the ABOVE_RETAIL invariant checks.

    Grade A on a 20.00 retail: the raw top is 20 x 0.9 x 1.2 = 21.60, already
    capped to 18.99 by the ratio; charming to 21.99 would sail past the 19.00
    ceiling, so it steps down instead.
    """
    a = _at(18.0, retail=20.0, grade="A")
    assert a.max_allowed < 20.0 * POL["pricing"]["hard_max_ratio"]
    assert round(a.max_allowed % 1, 2) == 0.99


def test_condition_maps_to_grade_when_grade_absent():
    a = assess(ProductSnapshot(id="t", price=40.0, retail_price=100.0,
                               condition="Lived In", currency="EUR"), POL)
    assert a.grade == "C" and a.max_allowed == 48.99


def test_min_price_floor_lifts_the_window_bottom():
    """A cheap item's lower bound is the floor, not the percentage."""
    a = _at(1.0, retail=20.0, grade="D")   # 20 * 0.0975 = 1.95, floor is 3.00
    floor = POL["pricing"]["min_price"]
    # The floor still governs — then gets its .99 like every other bound.
    assert a.min_allowed == pytest.approx(int(floor) + 0.99)
    assert a.corrected_price == a.min_allowed


def test_charm_rounding_never_escapes_the_window():
    """With round_mode charm, bounds round INWARD so the window still holds."""
    charmed = {**POL, "pricing": {**POL["pricing"], "round_mode": "charm"}}
    low = assess(ProductSnapshot(id="t", price=13.0, retail_price=100.0,
                                 grade="C", currency="EUR"), charmed)
    high = assess(ProductSnapshot(id="t", price=78.0, retail_price=100.0,
                                  grade="C", currency="EUR"), charmed)
    assert 32.0 <= low.corrected_price <= 48.0
    assert 32.0 <= high.corrected_price <= 48.0
    assert low.corrected_price == 32.95 and high.corrected_price == 47.99


def test_resolver_clamps_the_price_by_default():
    """anchor: retail means the SELLING PRICE moves, not the anchor."""
    p = levis(price=78.0, retail_price=100.0)
    patches = resolve_pricing(p, run_all(p, POL), Evidence(), POL)
    patch = next(x for x in patches if x.field == "price")
    assert patch.new_value == 48.99
    assert not [x for x in patches if x.field == "retail_price"]


def test_provenance_mode_moves_the_anchor_instead():
    pol = {**POL, "pricing": {**POL["pricing"], "anchor": "provenance"}}
    p = levis(price=78.0, retail_price=100.0,
              provenance={"price": Provenance.MARKET, "retail_price": Provenance.DERIVED})
    patches = resolve_pricing(p, run_all(p, pol), Evidence(), pol)
    patch = next(x for x in patches if x.field == "retail_price")
    assert patch.new_value > 100.0 and patch.action.value == "propose"


def test_human_locked_price_is_never_overwritten():
    p = levis(price=79.99, retail_price=66.99, locked_fields=["price"])
    patches = resolve_pricing(p, run_all(p, POL), Evidence(), POL)
    price_patches = [x for x in patches if x.field == "price"]
    assert all(x.action.value == "escalate" for x in price_patches)


def test_bin_mismatch_is_escalated_not_auto_fixed():
    result = reconcile(levis(), apply=False)
    assert result.status.value == "blocked"
    assert result.publishable is False


def test_mannequin_is_auto_corrected():
    result = reconcile(levis(bin_barcode="ZONE A-02-12-3"), apply=False)
    patch = next(p for p in result.patches if p.field == "mannequin")
    assert patch.new_value == "Men Bottom"
    assert patch.action.value == "apply"


def test_no_llm_calls_when_rules_suffice():
    p = levis(bin_barcode="ZONE A-02-12-3", price=23.99, eu_size="48",
              mannequin="Men Bottom")
    assert reconcile(p, apply=False).llm_calls == 0


@pytest.mark.parametrize("raw,expected", [(23.45, 22.99), (30.15, 29.99), (9.60, 9.95)])
def test_charm_nearest(raw, expected):
    assert charm(raw, POL["pricing"]["charm_endings"]) == expected


def test_charm_directional():
    e = POL["pricing"]["charm_endings"]
    assert charm(32.00, e, direction=+1) == 32.95   # up, stays above a lower bound
    assert charm(48.00, e, direction=-1) == 47.99   # down, stays below an upper bound