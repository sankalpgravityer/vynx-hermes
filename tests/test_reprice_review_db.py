"""Tests for the direct-to-Postgres repricing script.

`price_row` is a pure function of one database row, which is what makes the
arithmetic and every skip condition testable without a database. The SQL itself
is checked for shape only -- that it targets the Review predicate, writes the two
intended columns, and issues no DDL.

The price model under test is the same one Hermes and the analyze worker use:

    expected    = retailPrice x GRADE_PCT[grade]
    min_allowed = charm99(expected x 0.80)
    max_allowed = min(charm99(expected x 1.20), 95% of retail as a .99)

Inside the window a price KEEPS its figure and only gains a .99 ending; outside
it is clamped to the nearest edge. Those two behaviours are the ones most worth
pinning, because confusing them silently reprices a whole catalog.
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
sys.modules[_SPEC.name] = rr        # @dataclass needs this before exec
_SPEC.loader.exec_module(rr)


def record(**overrides):
    base = dict(
        id="1a351701-7568-4414-8975-ce07d664ab95",
        sku="BEV-000325",
        title="Regular Levi's Jeans in Blue size 32",
        tenantId="34c354a5-3415-4513-85b8-d40c2ec3af7e",
        tenant_name="BOAS",
        current_price="38.99",
        retail_price="39.00",
        grade="C",
        variant_id="8f14e45f-ceea-467a-9c1e-1b0f4a0f9d10",
        variant_base_price="38.99",
    )
    base.update(overrides)
    return base


def price(**overrides):
    return rr.price_row(record(**overrides), rr.Totals())


# --------------------------------------------------------------------------- #
# The grade table and the window it implies
# --------------------------------------------------------------------------- #

def test_the_specified_percentages():
    assert rr.GRADE_PCT == {"A": 0.50, "B": 0.40, "C": 0.25, "D": 0.1666}


def test_the_tolerance_matches_the_other_two_verifiers():
    """If these three disagree, each flags prices the others just approved."""
    assert rr.DEFAULT_TOLERANCE == 0.20      # == policy.yaml factor_tolerance
    assert rr.HARD_MAX_RATIO == 0.95         # == policy.yaml hard_max_ratio


@pytest.mark.parametrize("grade,retail,expected,lo,hi", [
    ("A", 100.0, 50.00, 40.99, 60.99),
    ("B", 100.0, 40.00, 32.99, 48.99),
    ("C", 100.0, 25.00, 20.99, 30.99),
    # 16.66 x 1.2 is 19.992, and the cents are set to .99 -- so the top of a
    # grade D window is 19.99, not 20.99.
    ("D", 100.0, 16.66, 13.99, 19.99),
    # The example from the brief: a raw 15.60-23.40 must read 15.99-23.99.
    ("A", 39.00, 19.50, 15.99, 23.99),
])
def test_the_window_for_each_grade(grade, retail, expected, lo, hi):
    row = price(grade=grade, retail_price=f"{retail:.2f}", current_price="1.00")
    assert row.pct == rr.GRADE_PCT[grade]
    assert row.expected == expected
    assert (row.min_allowed, row.max_allowed) == (lo, hi)
    assert row.expected_range == f"{lo:.2f} - {hi:.2f}"


def test_both_bounds_always_end_99():
    for retail in (5.0, 12.99, 39.0, 66.99, 100.0, 249.5, 1200.0):
        for grade in rr.GRADE_PCT:
            row = price(grade=grade, retail_price=str(retail),
                        current_price="1.00")
            if row.min_allowed is None:
                continue
            assert round(row.min_allowed % 1, 2) == 0.99, (grade, retail)
            assert round(row.max_allowed % 1, 2) == 0.99, (grade, retail)


def test_the_ceiling_holds_the_top_of_the_window_under_retail():
    """Grade A of a 20.00 retail targets 10.00 and a raw top of 12.00, which is
    fine -- but a high-percentage grade can push the band over the 95% line, and
    the ceiling has to win."""
    row = price(grade="A", retail_price="4.00", current_price="3.90")
    assert row.max_allowed < 4.00 * rr.HARD_MAX_RATIO
    assert round(row.max_allowed % 1, 2) == 0.99


# --------------------------------------------------------------------------- #
# In the window: keep the figure, fix the cents
# --------------------------------------------------------------------------- #

def test_an_in_window_price_that_already_ends_99_is_left_alone():
    # Grade A, retail 39.00 -> window 15.99-23.99.
    row = price(grade="A", retail_price="39.00", current_price="19.99")
    assert row.verdict == "ok"
    assert row.status == "already correct"
    assert row.writable is False


def test_an_in_window_price_keeps_its_figure_and_only_gains_the_99():
    """18.40 passes the ratio check -- it is exactly the ending charm pricing
    exists to remove, and it must NOT be snapped to the 19.99 target."""
    row = price(grade="A", retail_price="39.00", current_price="18.40")
    assert row.verdict == "round_required"
    assert row.new_price == 18.99
    assert row.delta == 0.59


def test_rounding_up_cannot_leave_the_window_it_just_passed():
    # 23.50 is inside 15.99-23.99; charm99 would say 23.99, which is the cap.
    assert price(grade="A", retail_price="39.00",
                 current_price="23.50").new_price == 23.99


# --------------------------------------------------------------------------- #
# Outside the window: clamp to the nearest edge
# --------------------------------------------------------------------------- #

def test_a_price_below_the_minimum_is_raised_to_the_minimum_not_the_target():
    """The smallest correction that makes the record right. Snapping to the
    target would move the price further than the problem warrants and throw away
    more of the eBay signal than necessary."""
    row = price(grade="A", retail_price="39.00", current_price="5.00")
    assert row.verdict == "too_low"
    assert row.new_price == 15.99          # the minimum, not the 19.99 target


def test_a_price_above_the_maximum_is_lowered_to_the_maximum():
    row = price(grade="A", retail_price="39.00", current_price="30.00")
    assert row.verdict == "too_high"
    assert row.new_price == 23.99


def test_a_price_at_or_above_its_own_retail_is_pulled_under_it():
    """The bug this whole exercise started from. Checked BEFORE the window: a
    used item at its own RRP is wrong wherever the grade band happens to sit."""
    row = price(grade="A", retail_price="39.00", current_price="48.99")
    assert row.verdict == "above_retail"
    assert row.new_price < 39.00 * rr.HARD_MAX_RATIO
    assert row.new_price == 23.99


def test_the_retail_invariant_is_strict_not_inclusive():
    # Exactly 95% of retail is already too high.
    row = price(grade="A", retail_price="100.00", current_price="95.00")
    assert row.verdict == "above_retail"


def test_a_missing_price_is_supplied_from_the_target():
    for blank in (None, "", "0", "0.00", "EUR 0"):
        row = price(grade="A", retail_price="39.00", current_price=blank)
        assert row.verdict == "no_price"
        assert row.new_price == 19.99
        assert row.writable is True


def test_every_price_written_ends_99_whatever_came_in():
    for current in ("0", "1.00", "5.55", "18.40", "19.99", "23.50", "48.99",
                    "500.00"):
        for grade in rr.GRADE_PCT:
            row = price(grade=grade, retail_price="39.00", current_price=current)
            if row.new_price is None:
                continue
            assert round(row.new_price % 1, 2) == 0.99, (grade, current)


def test_a_clamp_never_lands_outside_the_window_it_enforces():
    for current in ("0.10", "1.00", "500.00", "48.99"):
        row = price(grade="B", retail_price="66.99", current_price=current)
        if row.new_price is None:
            continue
        assert row.min_allowed <= row.new_price <= row.max_allowed


# --------------------------------------------------------------------------- #
# Skips -- every "cannot price this" path
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("grade", [None, "", "   "])
def test_no_grade_is_skipped_not_guessed(grade):
    row = price(grade=grade)
    assert row.status == "skipped"
    assert row.new_price is None
    assert "No grade" in row.reason


def test_an_unknown_grade_is_skipped():
    row = price(grade="E")
    assert row.status == "skipped"
    assert "not in the fixed table" in row.reason


def test_grades_are_matched_case_insensitively():
    assert price(grade="a", retail_price="39.00",
                 current_price="19.99").verdict == "ok"


@pytest.mark.parametrize("retail", [None, "", "0", "0.00", "-5.00", "n/a"])
def test_no_usable_retail_is_skipped(retail):
    """The percentage needs something to be a percentage OF. Inventing one here
    would write a fabricated price."""
    row = price(retail_price=retail)
    assert row.status == "skipped"
    assert row.new_price is None
    assert "retailPrice" in row.reason


def test_a_target_under_one_is_skipped_not_rounded_up_to_099():
    """Grade D of a 3.00 retail targets 0.50. charm99 would make that 0.99 --
    double the intended price. Rounding up 98% is not a rounding."""
    row = price(grade="D", retail_price="3.00")
    assert row.status == "skipped"
    assert row.new_price is None
    assert "round UP past the target" in row.reason


def test_skipped_rows_are_never_writable():
    for row in (price(grade=None), price(retail_price=None), price(grade="Z")):
        assert row.writable is False


def test_skip_reasons_are_counted_for_the_summary():
    totals = rr.Totals()
    rr.price_row(record(grade=None), totals)
    rr.price_row(record(retail_price=None), totals)
    rr.price_row(record(retail_price=None), totals)
    assert totals.skipped == {"no grade": 1, "no retail price": 2}
    assert totals.to_write == 0


def test_verdicts_are_counted_for_the_summary():
    totals = rr.Totals()
    rr.price_row(record(grade="A", retail_price="39.00",
                        current_price="18.40"), totals)
    rr.price_row(record(grade="A", retail_price="39.00",
                        current_price="19.99"), totals)
    rr.price_row(record(grade="A", retail_price="39.00",
                        current_price="99.00"), totals)
    assert totals.verdicts == {"round_required": 1, "ok": 1, "above_retail": 1}
    assert totals.to_write == 2       # the "ok" row is not written


# --------------------------------------------------------------------------- #
# Tolerance
# --------------------------------------------------------------------------- #

def test_a_wider_tolerance_keeps_more_prices_as_they_are():
    # 30.00 against a 19.50 target: outside +/-20%, inside +/-60%.
    assert rr.price_row(record(grade="A", retail_price="39.00",
                               current_price="30.00"),
                        rr.Totals(), 0.20).verdict == "too_high"
    assert rr.price_row(record(grade="A", retail_price="39.00",
                               current_price="30.00"),
                        rr.Totals(), 0.60).verdict == "round_required"


def test_a_zero_tolerance_demands_the_charmed_target():
    row = rr.price_row(record(grade="A", retail_price="39.00",
                              current_price="18.40"), rr.Totals(), 0.0)
    assert (row.min_allowed, row.max_allowed) == (19.99, 19.99)
    assert row.new_price == 19.99


# --------------------------------------------------------------------------- #
# Deltas and formatting
# --------------------------------------------------------------------------- #

def test_delta_is_reported_against_the_stored_price():
    row = price(grade="A", retail_price="100.00", current_price="30.00")
    assert (row.current, row.new_price) == (30.0, 40.99)   # raised to the floor
    assert row.delta == 10.99
    assert row.delta_pct == pytest.approx(36.63, abs=0.01)


def test_a_missing_current_price_has_no_delta():
    row = price(current_price=None, retail_price="100.00", grade="A")
    assert row.current is None
    assert row.new_price == 50.99
    assert row.delta is None and row.delta_pct is None
    assert row.writable is True


@pytest.mark.parametrize("stored,expected", [
    ("18.40", "18.99"),
    ("EUR 18.40", "18.99"),
    ("$18.40", "$18.99"),      # preserved -- see currency_symbol()
    ("£18.40", "£18.99"),
])
def test_a_non_euro_symbol_is_preserved_so_currency_is_not_flipped(stored, expected):
    """vnyx-api infers a variant's baseCurrency from this symbol and defaults to
    EUR when there is none, so writing a bare figure over "$12.00" would
    re-denominate the product."""
    row = price(grade="A", retail_price="39.00", current_price=stored)
    assert row.price_string() == expected


def test_every_column_has_a_cell():
    """A mismatch writes values under the wrong headings -- the sheet still
    opens and still looks plausible, which is worse than a crash."""
    row = price(grade="A", retail_price="39.00", current_price="18.40")
    assert len(row.cells()) == len(rr.COLUMNS)
    by_name = dict(zip([n for n, _ in rr.COLUMNS], row.cells()))
    assert by_name["Grade"] == "A"
    assert by_name["Grade %"] == 0.50
    assert by_name["Retail Price"] == 39.0
    assert by_name["Expected Price"] == 19.50
    assert by_name["Expected Range"] == "15.99 - 23.99"
    assert by_name["New Price"] == 18.99
    assert by_name["Verdict"] == "round_required"


# --------------------------------------------------------------------------- #
# The SQL
# --------------------------------------------------------------------------- #

def test_the_selection_matches_the_products_tab_definition():
    """Must equal what services/review-verification.ts filters on, or the script
    reprices a different set of products than the page shows.

    The status itself is a bind parameter now (see --stage), so what is pinned
    here is the surrounding predicate and the ordering."""
    sql = " ".join(rr.SELECT_SQL.split())
    assert 'p."isArchived" = false' in sql
    assert 'p."isDeleted" = false' in sql
    assert 'p."reviewStatus"::text = %(review_status)s' in sql
    assert rr.STAGES["review"] == "PENDING"
    assert 'ORDER BY p."createdAt" DESC' in sql
    assert """"qualityGrading" ->> 'grade'""" in sql


def test_the_writes_touch_only_price_columns():
    for sql in (rr.UPDATE_PRODUCT_SQL, rr.UPDATE_VARIANT_SQL):
        assert sql.strip().startswith("UPDATE")
        assert " WHERE " in sql
    assert 'SET price = v.price' in rr.UPDATE_PRODUCT_SQL
    assert 'SET "basePrice" = v.price' in rr.UPDATE_VARIANT_SQL


def test_no_statement_can_alter_the_schema():
    """The script is restricted to data. Pinned so a later edit cannot quietly
    introduce DDL."""
    every = " ".join([rr.SELECT_SQL, rr.COUNT_SQL, rr.UPDATE_PRODUCT_SQL,
                      rr.UPDATE_VARIANT_SQL]).upper()
    for forbidden in ("ALTER ", "DROP ", "CREATE ", "TRUNCATE ", "DELETE ",
                      "GRANT ", "INSERT "):
        assert forbidden not in every, forbidden


def test_the_dsn_password_never_reaches_the_sheet():
    shown = rr.redact("postgresql://postgres:sup3rs3cret@34.7.102.18:5432/vnyx-dev-2")
    assert "sup3rs3cret" not in shown
    assert "34.7.102.18:5432/vnyx-dev-2" in shown
    assert "postgres@" in shown


def test_the_writes_are_keyed_by_primary_key_only():
    """A price update must never be able to match more than its own row."""
    assert "WHERE p.id = v.id" in " ".join(rr.UPDATE_PRODUCT_SQL.split())
    assert "WHERE pv.id = v.id" in " ".join(rr.UPDATE_VARIANT_SQL.split())
