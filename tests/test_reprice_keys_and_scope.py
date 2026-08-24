"""Two additions: the over-retail-only filter, and the per-tenant tab.

Multi-key SerpApi rotation was also built here and then removed at the user's
request -- a second key is another account to keep topped up, and when it is
also out of searches the run pays twice to learn the same thing. Its tests went
with it.
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
# --only-over-retail
# --------------------------------------------------------------------------- #

def test_the_filter_is_applied_in_sql_not_after():
    """A 4,700-product stage should read 704 rows, and the sheet should hold
    those 704 rather than burying them among 4,000 skips."""
    assert "{over_retail}" in rr.SELECT_SQL
    assert "{over_retail}" in rr.COUNT_SQL
    clause = " ".join(rr.OVER_RETAIL_CLAUSE.split())
    assert "IS NOT NULL" in clause
    assert ">" in clause


def test_the_filter_uses_the_guarded_cast():
    """price and retailPrice are TEXT. A bare ::numeric cast meeting one
    malformed row aborts the whole query."""
    assert "~ '^[0-9]+(\\.[0-9]+)?$'" in rr.OVER_RETAIL_CLAUSE


def test_the_filter_is_off_by_default():
    assert rr.parse_args(["--dsn", "x"]).only_over_retail is False
    assert rr.parse_args(["--dsn", "x", "--only-over-retail"]).only_over_retail


@pytest.mark.parametrize("on", [True, False])
def test_the_queries_parse_with_and_without_the_filter(on):
    pglast = pytest.importorskip("pglast", reason="pip install pglast")
    clause = rr.OVER_RETAIL_CLAUSE if on else ""
    for template in (rr.SELECT_SQL, rr.COUNT_SQL):
        sql = (template.format(over_retail=clause, limit="LIMIT 10")
               if "{limit}" in template
               else template.format(over_retail=clause))
        sql = (sql.replace("%(tenants)s", "'{}'")
                  .replace("%(review_status)s", "'ACCEPTED'"))
        assert len(pglast.parse_sql(sql)) == 1


# --------------------------------------------------------------------------- #
# The per-tenant tab
# --------------------------------------------------------------------------- #

def row_for(tenant, current, retail):
    return rr.price_row({
        "id": "1a351701-7568-4414-8975-ce07d664ab95", "sku": "S", "title": "t",
        "tenantId": "x", "tenant_name": tenant, "brand": "B",
        "current_price": current, "retail_price": retail, "grade": "A",
        "variant_id": None, "variant_base_price": None,
    }, rr.Totals())


def test_the_tenant_tab_separates_database_counts_from_run_counts(tmp_path):
    """Reading one as the other is how a 4,700 total gets compared against a
    3,352 badge, so they are never collapsed into one column."""
    openpyxl = pytest.importorskip("openpyxl")
    rows = [row_for("BOAS", "180.99", "159.99"),
            row_for("BOAS", "19.99", "39.00"),
            row_for("Kilo Kilo Vintage", "30.00", "39.00")]
    breakdown = [("BOAS", "514d1b2c", 2351),
                 ("Bleckmann", "1779ea6e", 1347),
                 ("Kilo Kilo Vintage", "6045eee9", 762)]
    args = rr.parse_args(["--dsn", "x", "--stage", "approved"])
    out = tmp_path / "s.xlsx"

    rr.write_sheet(out, rows, 4460, rr.Totals(seen=3), args, False,
                   breakdown, ["514d1b2c", "6045eee9"])

    wb = openpyxl.load_workbook(out)
    assert "By tenant" in wb.sheetnames
    ws = wb["By tenant"]
    by_name = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}

    assert by_name["BOAS"][2] == 2351          # in the database
    assert by_name["BOAS"][3] == 2            # in this run
    assert by_name["BOAS"][9] == "yes"        # in scope

    # A tenant with products in the database but none in this run still appears,
    # marked out of scope -- that row is the explanation for the count gap.
    assert by_name["Bleckmann"][2] == 1347
    assert by_name["Bleckmann"][3] == 0
    assert by_name["Bleckmann"][9] == "NO"

    total = by_name["TOTAL"]
    assert total[2] == 4460                    # 2351 + 1347 + 762
    assert total[3] == 3


def test_the_tenant_tab_counts_changes_and_movement(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    rows = [row_for("BOAS", "180.99", "159.99"),   # above_retail -> changes
            row_for("BOAS", "19.99", "39.00")]     # already correct
    args = rr.parse_args(["--dsn", "x"])
    out = tmp_path / "s.xlsx"
    rr.write_sheet(out, rows, 2, rr.Totals(seen=2), args, False,
                   [("BOAS", "id", 2)], None)

    ws = openpyxl.load_workbook(out)["By tenant"]
    boas = next(r for r in ws.iter_rows(min_row=2, values_only=True)
                if r[0] == "BOAS")
    assert boas[4] == 1                         # price changes
    assert boas[5] == 1                         # already correct
    assert boas[7] == rows[0].delta             # net movement


def test_the_sheet_still_works_without_a_breakdown(tmp_path):
    """write_sheet is called from tests and from the revert path too."""
    openpyxl = pytest.importorskip("openpyxl")
    out = tmp_path / "s.xlsx"
    rr.write_sheet(out, [row_for("BOAS", "180.99", "159.99")], 1,
                   rr.Totals(seen=1), rr.parse_args(["--dsn", "x"]), False)
    assert "By tenant" not in openpyxl.load_workbook(out).sheetnames
