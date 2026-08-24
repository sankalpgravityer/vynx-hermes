"""Two things the other test files cannot cover.

1. That every statement the script issues is valid PostgreSQL. Checked with
   pglast, which wraps libpg_query -- the actual Postgres parser -- so a syntax
   error is caught here instead of on a production database. It proves grammar,
   not semantics: column types and row behaviour still need a real server.

2. The rule deciding whether to believe a Google quote over the retail price
   already stored. That rule spends money and rewrites an anchor other figures
   derive from, so each branch is pinned.
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


# --------------------------------------------------------------------------- #
# SQL validity
# --------------------------------------------------------------------------- #

STATEMENTS = [
    "STATS_SQL", "COUNT_SQL", "SELECT_SQL", "TENANT_BREAKDOWN_SQL",
    "UPDATE_PRODUCT_SQL", "UPDATE_VARIANT_SQL", "UPDATE_RETAIL_SQL",
]


def parseable(sql: str) -> str:
    """psycopg placeholders are not SQL, so stand something in for them.

    Named placeholders are substituted before the positional `%s`, because
    `%(review_status)s` ends in `%s` and would otherwise be half-replaced into
    something that no longer parses.
    """
    return (sql.replace("%(tenants)s", "'{}'")
               .replace("%(review_status)s", "'PENDING'")
               .replace("%s", "'[]'")
               .replace("{over_retail}", "")
               .replace("{over_retail}", "")
           .replace("{limit}", "LIMIT 10"))


@pytest.mark.parametrize("name", STATEMENTS)
def test_every_statement_is_valid_postgresql(name):
    pglast = pytest.importorskip("pglast", reason="pip install pglast")
    pglast.parse_sql(parseable(getattr(rr, name)))


@pytest.mark.parametrize("name", STATEMENTS)
def test_no_statement_is_accidentally_two_statements(name):
    """A stray semicolon in a formatted string would let a second statement ride
    along on the same execute."""
    pglast = pytest.importorskip("pglast", reason="pip install pglast")
    assert len(pglast.parse_sql(parseable(getattr(rr, name)))) == 1


def test_the_price_columns_are_cast_defensively():
    """Product.price and retailPrice are TEXT. A bare ::numeric cast that meets
    one malformed row aborts the entire query, so anything that does not look
    like a number has to become NULL instead."""
    assert "~ '^[0-9]+(\\.[0-9]+)?$'" in rr.NUMERIC_CAST
    assert "ELSE NULL" in rr.NUMERIC_CAST
    # A comma decimal ("38,99") must not be read as 3899.
    assert "replace({col}, ',', '.')" in rr.NUMERIC_CAST


def test_the_stats_query_answers_the_question_that_prompted_it():
    sql = " ".join(rr.STATS_SQL.split())
    assert "AND sp > rp) AS over_retail" in sql          # price above retail
    assert f"sp >= rp * {rr.HARD_MAX_RATIO}" in sql      # and the 95% case
    assert "AS no_retail" in sql and "AS no_price" in sql
    # Split by stage, so "is this worth running" and "how big is the problem
    # overall" are separable.
    assert "GROUP BY in_review" in sql


def test_the_stats_query_reads_and_writes_nothing():
    upper = rr.STATS_SQL.upper()
    assert upper.strip().startswith("WITH")
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "ALTER ", "CREATE "):
        assert forbidden not in upper


# --------------------------------------------------------------------------- #
# Believing a quote
# --------------------------------------------------------------------------- #

def record(**overrides):
    base = dict(
        id="1a351701-7568-4414-8975-ce07d664ab95", sku="KIL-1",
        title="Leather Biker Jacket", tenantId="x", tenant_name="Kilo",
        brand="Kilo", current_price="70.00", retail_price="50.00", grade="A",
        variant_id=None, variant_base_price=None,
    )
    base.update(overrides)
    return base


class FakeLookup(rr.RetailLookup):
    def __init__(self, answer):
        super().__init__(serpapi_key="fake")
        self.answer = answer

    def _fetch(self, query):
        self.calls += 1
        return self.answer


def run(quote_value, rule="higher_than_retail", gain=rr.MIN_QUOTE_GAIN_PCT,
        current="70.00", retail="50.00"):
    totals = rr.Totals()
    rows = [rr.price_row(record(current_price=current, retail_price=retail),
                         totals)]
    quote = rr.RetailQuote(retail=quote_value, samples=6, source="serpapi")
    rr.relookup_retail(rows, FakeLookup(quote), totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "high", rule, gain)
    return rows[0], totals


def test_the_case_from_the_report_is_looked_up_at_all():
    """Retail 50, selling 70 -- 140% of retail, so above_retail."""
    row, _ = run(None)
    assert row.verdict in ("above_retail", "too_low", "too_high")


def test_a_materially_higher_quote_replaces_the_anchor():
    row, totals = run(120.00)
    assert row.new_retail == 120.00
    assert totals.retail_replaced == 1
    # Re-priced against it: grade A of 120 targets 60.00, window 48.99-72.99,
    # so the 70.00 asking price is now legitimate and only its cents move.
    assert (row.min_allowed, row.max_allowed) == (48.99, 72.99)
    assert row.new_price == 70.99


def test_a_quote_between_the_retail_and_the_asking_price_is_now_used():
    """The old rule required the quote to clear the SELLING price, which threw
    away better-evidenced anchors: 60.00 is a worse fit than 120.00 but a much
    better one than the stored 50.00, and it is what Google actually found."""
    row, totals = run(60.00)
    assert row.new_retail == 60.00
    assert totals.retail_replaced == 1


def test_the_stricter_rule_still_rejects_that_quote():
    row, totals = run(60.00, rule="higher_than_price")
    assert row.new_retail is None
    assert totals.retail_rejected == 1
    assert "does not clear the 70.00 asking price" in row.retail_note


def test_a_quote_barely_above_the_stored_retail_is_noise():
    """Rewriting an anchor and every price derived from it needs more than a
    rounding difference as justification."""
    row, totals = run(51.00)          # +2% against 50.00, under the 5% floor
    assert row.new_retail is None
    assert totals.retail_rejected == 1
    assert "under the 5% needed" in row.retail_note


def test_the_gain_threshold_is_configurable():
    row, _ = run(51.00, gain=1.0)
    assert row.new_retail == 51.00


def test_a_lower_quote_is_always_rejected():
    row, totals = run(30.00)
    assert row.new_retail is None
    assert totals.retail_rejected == 1


def test_always_takes_whatever_came_back():
    row, _ = run(30.00, rule="always")
    assert row.new_retail == 30.00


def test_a_product_with_no_stored_retail_accepts_any_quote():
    """There is no anchor to disagree with, so the gain test cannot apply --
    otherwise the rows that most need a retail price would never get one."""
    row, totals = run(99.00, retail=None)
    assert row.new_retail == 99.00
    assert totals.retail_replaced == 1


def test_the_rules_are_ordered_from_loose_to_strict():
    assert rr.QUOTE_RULES == ("always", "higher_than_retail", "higher_than_price")


def test_google_is_the_default_authority_on_retail():
    """The default takes the quote whenever enough listings agree, in EITHER
    direction. That is what "find the new retail price with Google and update it"
    means -- including when Google says the garment is worth less than recorded.
    """
    assert rr.parse_args(["--dsn", "x"]).accept_quote == "always"


def test_a_lower_quote_is_taken_by_default_and_cuts_the_price_further():
    """The honest consequence, pinned so it cannot surprise anyone later.

    180.99 against a stored 159.99 retail clamps to 96.99. Against Google's
    112.01 it clamps to 67.99 -- accepting the better-sampled number moves the
    price DOWN more, because 180.99 is a bigger multiple of the smaller anchor.
    """
    row, totals = run(112.01, rule="always", current="180.99", retail="159.99")
    assert row.new_retail == 112.01
    assert totals.retail_replaced == 1
    assert row.verdict == "above_retail"
    assert row.new_price == 67.99
    assert row.delta == -113.00


def test_the_stricter_rules_are_still_there_for_that_reason():
    row, totals = run(112.01, rule="higher_than_retail",
                      current="180.99", retail="159.99")
    assert row.new_retail is None
    assert totals.retail_rejected == 1
    assert row.new_price == 96.99          # clamped against the stored anchor


def test_a_rejection_always_says_why():
    for value in (30.00, 51.00):
        row, _ = run(value)
        assert "NOT used --" in row.retail_note
        assert f"{value:.2f}" in row.retail_note
