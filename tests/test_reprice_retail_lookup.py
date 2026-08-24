"""Tenant scoping and the Google Shopping retail lookup, for the DB reprice script.

Two additions with very different risk profiles, so they get their own file.

Tenant scoping is a correctness fix: the Products page is scoped to the tenants
the logged-in account can see, and an unscoped query here both inflated the
total and would have repriced products the operator cannot open.

The retail lookup spends money and rewrites an anchor other figures derive from,
so what is pinned here is mostly restraint -- which rows it refuses to look up,
and when it declines to use an answer it got.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "reprice_review_db",
    Path(__file__).resolve().parents[1] / "scripts" / "reprice_review_db.py",
)
assert _SPEC and _SPEC.loader
rr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rr
_SPEC.loader.exec_module(rr)


def record(**overrides):
    base = dict(
        id="1a351701-7568-4414-8975-ce07d664ab95",
        sku="BEV-000418",
        title="Brown Leather Biker Jacket",
        tenantId="34c354a5-3415-4513-85b8-d40c2ec3af7e",
        tenant_name="Bever",
        brand="BOAS",
        current_price="48.99",
        retail_price="39.19",
        grade="A",
        variant_id="8f14e45f-ceea-467a-9c1e-1b0f4a0f9d10",
        variant_base_price="48.99",
    )
    base.update(overrides)
    return base


def rows_for(current, retail, grade="A", title="Brown Leather Biker Jacket",
             brand="BOAS"):
    totals = rr.Totals()
    row = rr.price_row(record(current_price=current, retail_price=retail,
                              grade=grade, title=title, brand=brand), totals)
    return [row], totals


# --------------------------------------------------------------------------- #
# Tenant scoping -- the cause of a count that disagrees with the screen
# --------------------------------------------------------------------------- #

def test_the_queries_can_be_scoped_to_tenants():
    """routes/products.ts sets tenantId = getAccessibleTenantIds(req) for an
    OWNER, so the Review badge counts only visible tenants. Without the same
    clause here the script counts every tenant in the database."""
    for sql in (rr.SELECT_SQL, rr.COUNT_SQL):
        assert "%(tenants)s" in sql
        assert 'p."tenantId" = ANY' in sql


def test_a_null_tenant_list_means_no_filter():
    """One query serves both modes, so the unscoped path cannot drift from the
    scoped one."""
    assert "%(tenants)s::uuid[] IS NULL OR" in rr.TENANT_CLAUSE


def test_the_breakdown_is_deliberately_unscoped():
    """It exists to EXPLAIN a mismatch, so it has to show the tenants the scope
    excludes -- filtering it would hide the very rows being asked about."""
    assert "%(tenants)s" not in rr.TENANT_BREAKDOWN_SQL
    assert "GROUP BY" in rr.TENANT_BREAKDOWN_SQL


# --------------------------------------------------------------------------- #
# The lookup
# --------------------------------------------------------------------------- #

class FakeLookup(rr.RetailLookup):
    """A RetailLookup that answers from a fixture instead of the network."""

    def __init__(self, answer: rr.RetailQuote):
        super().__init__(serpapi_key="fake")
        self.answer = answer
        self.queries: list[str] = []

    def _fetch(self, query: str) -> rr.RetailQuote:
        self.queries.append(query)
        self.calls += 1
        return self.answer


def run_lookup(rows, totals, quote):
    lookup = FakeLookup(quote)
    rr.relookup_retail(rows, lookup, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1)
    return lookup


def test_a_price_above_its_own_retail_triggers_a_lookup():
    """The whole point: 48.99 against a 39.19 retail is 125% of RRP, so one of
    the two figures is wrong. A believable second opinion on the anchor says it
    was the retail price, and the selling price turns out to have been fine."""
    rows, totals = rows_for("48.99", "39.19")
    assert rows[0].verdict == "above_retail"

    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=100.00, samples=7, source="serpapi"))
    row = rows[0]
    assert lookup.calls == 1
    assert (row.retail_before, row.new_retail) == (39.19, 100.00)
    assert totals.retail_replaced == 1

    # Re-priced against the NEW anchor: grade A of 100.00 targets 50.00, so the
    # window becomes 40.99-60.99 and the 48.99 asking price sits inside it.
    assert row.retail == 100.00
    assert row.expected == 50.00
    assert (row.min_allowed, row.max_allowed) == (40.99, 60.99)
    assert row.verdict == "ok"
    assert row.status == "already correct"
    assert "google" in row.retail_note and "100.00" in row.retail_note


def test_by_default_a_lower_quote_is_still_taken():
    """Google is the authority on retail, in either direction. A quote below the
    asking price does not justify the price -- it says the garment is worth less
    than recorded, and the selling price falls further as a result."""
    rows, totals = rows_for("48.99", "39.19")
    run_lookup(rows, totals,
               rr.RetailQuote(retail=30.00, samples=5, source="serpapi"))
    row = rows[0]
    assert row.new_retail == 30.00
    assert row.retail == 30.00
    assert totals.retail_replaced == 1
    # 48.99 against a 30.00 anchor is even further over retail than before.
    assert row.verdict == "above_retail"
    assert row.new_price is not None and row.new_price < 30.00


def test_the_stricter_rule_rejects_a_quote_below_the_asking_price():
    """Kept available: it is the rule for "only replace the anchor when doing so
    RESOLVES the contradiction"."""
    rows, totals = rows_for("48.99", "39.19")
    lookup = FakeLookup(rr.RetailQuote(retail=30.00, samples=5, source="serpapi"))
    rr.relookup_retail(rows, lookup, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "over_retail",
                       "higher_than_price")
    row = rows[0]
    assert row.new_retail is None
    assert row.retail == 39.19                 # untouched
    assert "NOT used" in row.retail_note
    assert totals.retail_rejected == 1


def test_a_failed_lookup_leaves_the_row_exactly_as_it_was():
    rows, totals = rows_for("48.99", "39.19")
    before = rows[0].new_price
    run_lookup(rows, totals, rr.RetailQuote(None, 0, "serpapi", error="quota"))
    assert rows[0].new_price == before
    assert rows[0].new_retail is None
    assert "quota" in rows[0].retail_note
    assert totals.retail_failed == 1


def test_a_product_with_no_retail_at_all_is_also_looked_up():
    """The other state where the anchor, not the price, is the missing piece."""
    rows, totals = rows_for("45.00", None)
    assert rows[0].status == "skipped"
    run_lookup(rows, totals,
               rr.RetailQuote(retail=100.00, samples=4, source="serpapi"))
    assert rows[0].new_retail == 100.00
    assert rows[0].new_price == 45.99      # 45.00 charmed, inside 40.99-60.99
    assert rows[0].writable is True


@pytest.mark.parametrize("current,retail,verdict", [
    ("18.40", "39.00", "round_required"),
    ("5.00", "39.00", "too_low"),
])
def test_a_product_that_is_not_priced_high_is_never_looked_up(current, retail,
                                                              verdict):
    """An underpriced or merely mis-rounded row is not evidence of a bad anchor.
    For too_low in particular a HIGHER retail would push the band further up and
    the price further from it, so the call cannot help."""
    rows, totals = rows_for(current, retail)
    assert rows[0].verdict == verdict
    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=999.0, samples=9, source="serpapi"))
    assert lookup.calls == 0
    assert rows[0].new_retail is None
    assert "not looked up" in rows[0].retail_note or "not needed" in rows[0].retail_note


def test_a_price_cheaper_than_retail_is_not_looked_up():
    """The rule is a CONTRADICTION between the two stored figures, not "the price
    looks high". 30.00 against a 39.00 retail is over its grade band by a wide
    margin, but it is still cheaper than retail -- the anchor is not in question,
    so the paid call would be wasted."""
    rows, totals = rows_for("30.00", "39.00")
    assert rows[0].verdict == "too_high"
    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=100.00, samples=6, source="serpapi"))
    assert lookup.calls == 0
    assert rows[0].new_retail is None
    assert "already below the 39.00 retail price" in rows[0].retail_note
    # And it is still repriced by the grade window, as before.
    assert rows[0].new_price == 23.99


def test_a_price_just_under_retail_is_not_looked_up_either():
    """The `above_retail` VERDICT fires at 95% of retail, so it includes prices
    that are still below it. Measuring the two figures directly is what keeps
    those out."""
    rows, totals = rows_for("38.99", "39.19")
    assert rows[0].verdict == "above_retail"      # >= 95% of retail
    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=100.00, samples=6, source="serpapi"))
    assert lookup.calls == 0
    assert "already below the 39.19 retail price" in rows[0].retail_note


def test_the_broader_modes_are_still_available():
    """For when a wider sweep is wanted and the call budget allows it."""
    rows, totals = rows_for("30.00", "39.00")
    lookup = FakeLookup(rr.RetailQuote(retail=100.00, samples=6, source="serpapi"))
    rr.relookup_retail(rows, lookup, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "over_grade_max")
    assert lookup.calls == 1
    assert rows[0].new_retail == 100.00
    # Re-priced against the new anchor: grade A of 120 targets 60.00, so a price
    # that was being CUT to 23.99 is raised to 48.99 instead.
    assert (rows[0].min_allowed, rows[0].max_allowed) == (40.99, 60.99)
    assert rows[0].new_price == 40.99


def test_a_price_strictly_above_retail_is_the_case_that_qualifies():
    rows, totals = rows_for("180.99", "159.99")
    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=112.01, samples=40, source="serpapi"))
    assert lookup.calls == 1
    assert rows[0].new_retail == 112.01


def test_every_row_says_why_it_was_not_looked_up():
    """A blank Google column is ambiguous: no lookup, a lookup that found
    nothing, and a quote found-then-rejected all look the same otherwise."""
    rows = []
    totals = rr.Totals()
    for current, retail in (("19.99", "39.00"), ("5.00", "39.00"),
                            ("18.40", "39.00")):
        rows.append(rr.price_row(record(current_price=current,
                                        retail_price=retail), totals))
    rr.relookup_retail(rows, FakeLookup(rr.RetailQuote(None, 0, "serpapi")),
                       totals, rr.DEFAULT_TOLERANCE, rr.DEFAULT_MIN_PRICE, 1)
    assert all(r.retail_note for r in rows), [r.verdict for r in rows]


def test_the_modes_are_ordered_from_narrow_to_broad():
    assert rr.LOOKUP_MODES == ("over_retail", "at_or_over_95", "over_grade_max",
                               "all")
    assert rr.HIGH_VERDICTS == {"above_retail", "too_high"}


def test_the_narrowest_mode_is_the_default():
    """A paid call per product, so the default has to be the case that actually
    needs one: the two stored figures contradicting each other."""
    assert rr.parse_args(["--dsn", "x"]).lookup_when == "over_retail"


def test_identical_titles_share_one_paid_call():
    """The queue is ordered by creation time and a warehouse holds many copies of
    the same garment, so identical titles arrive in runs."""
    totals = rr.Totals()
    rows = [rr.price_row(record(title="Same Jacket"), totals) for _ in range(5)]
    lookup = run_lookup(rows, totals,
                        rr.RetailQuote(retail=100.00, samples=6, source="serpapi"))
    assert lookup.calls == 1
    assert lookup.cache_hits == 4
    assert all(r.new_retail == 100.00 for r in rows)


def test_the_query_prepends_the_brand_only_when_the_title_lacks_it():
    lookup = rr.RetailLookup(serpapi_key="k")
    rows, _ = rows_for("48.99", "39.19", title="Beige Sweater", brand="Levis")
    assert lookup.query_for(rows[0]) == "Levis Beige Sweater"
    rows, _ = rows_for("48.99", "39.19", title="Levis Beige Sweater",
                       brand="Levis")
    assert lookup.query_for(rows[0]) == "Levis Beige Sweater"


def test_the_breakdown_written_beside_a_new_retail_explains_itself():
    """review-verification.ts reads retailPriceBreakdown to report where a retail
    price came from. Leaving the old one in place would have it explaining a
    figure that is no longer stored."""
    rows, totals = rows_for("48.99", "39.19")
    run_lookup(rows, totals, rr.RetailQuote(retail=100.00, samples=7,
                                            source="serpapi",
                                            detail={"query": "q"}))
    b = rows[0].retail_breakdown
    assert b["retailPriceEur"] == 100.00
    assert b["source"] == rr.RETAIL_SOURCE == "GOOGLE_SHOPPING"
    assert b["detail"]["replaced"] == 39.19
    assert b["detail"]["samples"] == 7
    assert b["computedAt"]


def test_the_retail_write_targets_only_the_two_intended_columns():
    sql = " ".join(rr.UPDATE_RETAIL_SQL.split())
    assert sql.startswith("UPDATE")
    assert '"retailPrice" = v.retail' in sql
    assert '"retailPriceBreakdown" = v.breakdown' in sql
    assert "WHERE p.id = v.id" in sql


# --------------------------------------------------------------------------- #
# Price extraction and aggregation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,expected", [
    ("EUR 129.99", [129.99]),
    ("129,99 EUR", [129.99]),
    ("Only $49.95 today", [49.95]),
    ("no price here", []),
    ("0.50 EUR", []),          # below the 1.00 sanity floor
])
def test_prices_are_scraped_out_of_snippet_text(text, expected):
    assert rr.prices_from_text(text) == expected


def test_the_median_is_used_not_the_mean():
    """One mispriced listing must not drag the whole anchor with it."""
    assert rr._median([10.0, 20.0, 30.0]) == 20.0
    assert rr._median([10.0, 20.0, 30.0, 9999.0]) == 25.0


def test_a_lookup_needs_more_than_one_agreeing_listing():
    assert rr.MIN_SAMPLES >= 2


def test_lookup_reports_unavailable_without_keys():
    assert rr.RetailLookup().available is False
    assert rr.RetailLookup(serpapi_key="k").available is True
    assert rr.RetailLookup(google_key="k").available is False      # needs cx too
    assert rr.RetailLookup(google_key="k", google_cx="c").available is True


def test_which_backend_is_used_is_stated_not_guessed():
    assert "serpapi" in rr.RetailLookup(serpapi_key="k").describe().lower()
    assert "programmable" in rr.RetailLookup(
        google_key="k", google_cx="c").describe().lower()


# --------------------------------------------------------------------------- #
# The plausibility band
#
# Added after a production run wrote quotes like 35.99 -> 241.80 and
# 26.99 -> 6.25. Google Shopping had matched vague garment titles against
# unrelated products; the median of 40 unrelated listings is a confident-looking
# number that means nothing, and `--accept-quote always` waved every one through.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stored,quote,ratio", [
    ("35.99", 241.80, 6.7),
    ("39.99", 238.89, 6.0),
    ("26.99", 6.25, 0.2),
    ("27.99", 5.09, 0.2),
    ("82.99", 12.84, 0.2),
])
def test_an_implausible_quote_is_rejected_however_many_listings_agree(stored,
                                                                     quote, ratio):
    """Every one of these came from a real run with 18-40 listings behind it.
    Sample count is not evidence of a correct match."""
    rows, totals = rows_for("300.00", stored)
    run_lookup(rows, totals,
               rr.RetailQuote(retail=quote, samples=40, source="serpapi"))
    assert rows[0].new_retail is None, f"{stored} -> {quote} should be rejected"
    assert rows[0].retail == float(stored)
    assert "plausibility band" in rows[0].retail_note
    assert totals.retail_rejected == 1


@pytest.mark.parametrize("quote", [100.00, 39.19, 15.00])
def test_a_believable_correction_still_gets_through(quote):
    """The guard must not block the thing the lookup exists for: 2-3x is a
    plausible anchor correction, an order of magnitude is a different product."""
    rows, totals = rows_for("300.00", "39.19")
    run_lookup(rows, totals,
               rr.RetailQuote(retail=quote, samples=6, source="serpapi"))
    assert rows[0].new_retail == quote


def test_the_band_applies_even_under_accept_quote_always():
    """`always` is about DIRECTION -- taking a lower quote as readily as a higher
    one. It is not permission to take a quote for the wrong product."""
    rows, totals = rows_for("300.00", "35.99")
    lookup = FakeLookup(rr.RetailQuote(retail=241.80, samples=40,
                                       source="serpapi"))
    rr.relookup_retail(rows, lookup, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "over_retail", "always")
    assert rows[0].new_retail is None


def test_the_band_can_be_switched_off():
    rows, totals = rows_for("300.00", "35.99")
    lookup = FakeLookup(rr.RetailQuote(retail=241.80, samples=40,
                                       source="serpapi"))
    rr.relookup_retail(rows, lookup, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "over_retail", "always",
                       rr.MIN_QUOTE_GAIN_PCT, 0, 0)
    assert rows[0].new_retail == 241.80


def test_a_product_with_no_stored_retail_has_nothing_to_compare_against():
    """The band is a comparison with the stored anchor. With no anchor there is
    no comparison to make, and refusing would leave the rows that most need a
    retail price without one."""
    rows, totals = rows_for("45.00", None)
    run_lookup(rows, totals,
               rr.RetailQuote(retail=999.00, samples=5, source="serpapi"))
    assert rows[0].new_retail == 999.00
