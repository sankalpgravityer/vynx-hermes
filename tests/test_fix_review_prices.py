"""Tests for the review-price correction script.

The script writes to production prices, so the parts that decide WHICH products
get written and WHAT figure lands in the sheet are pinned here. Nothing in this
file touches the network: `to_row` is a pure function of one queue row, which is
what makes that worth testing rather than mocking a whole HTTP conversation.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Loaded by path: `scripts/` is not a package, and adding an __init__.py there
# would make it importable as one, which it is not meant to be.
_SPEC = importlib.util.spec_from_file_location(
    "fix_review_prices",
    Path(__file__).resolve().parents[1] / "scripts" / "fix_review_prices.py",
)
assert _SPEC and _SPEC.loader
frp = importlib.util.module_from_spec(_SPEC)
# Registered BEFORE exec: @dataclass resolves its annotations via
# sys.modules[cls.__module__], which is None for a module still being executed.
sys.modules[_SPEC.name] = frp
_SPEC.loader.exec_module(frp)


def queue_row(**price_overrides) -> dict:
    """One row shaped like GET /review-verification/queue returns it."""
    price = {
        "verdict": "round_required",
        "price_factor": 0.5,
        "expected_price": 19.5,
        "min_allowed": 15.99,
        "max_allowed": 23.99,
        "corrected_price": 18.99,
        "change_required": True,
        "explanation": "18.40 -> 18.99",
    }
    price.update(price_overrides)
    return {
        "id": "1a351701-7568-4414-8975-ce07d664ab95",
        "sku": "BEV-000325",
        "title": "Regular Levi's Jeans in Blue size 32",
        "grade": "C",
        "currency": "EUR",
        "price": "18.40",
        "retailPrice": "39.00",
        "editUrl": "https://dev.vnyx.ai/product/1a351701/edit?fromTab=pending",
        "verification": {"correct": True, "findings": [], "price": price},
    }


# --------------------------------------------------------------------------- #
# Price parsing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("18.40", 18.40),
    ("€18.40", 18.40),     # older rows carry the symbol in the value
    ("EUR 18.40", 18.40),
    ("18,40", 18.40),           # comma decimal
    (18.4, 18.40),
    (0, 0.0),
])
def test_money_parses_every_shape_a_price_arrives_in(raw, expected):
    assert frp.money(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "  ", "n/a", "-"])
def test_money_returns_none_rather_than_zero(raw):
    """A missing price and a free product must not look the same: 0.0 would be
    written into the sheet as a real figure and skew the movement total."""
    assert frp.money(raw) is None


def test_money_rejects_booleans():
    # bool is a subclass of int in Python, so True would otherwise parse as 1.00.
    assert frp.money(True) is None


# --------------------------------------------------------------------------- #
# What gets written
# --------------------------------------------------------------------------- #

def test_a_price_needing_a_change_is_flagged_with_its_new_figure():
    row = frp.to_row(queue_row())
    assert row.needs_change is True
    assert (row.current, row.new_price) == (18.40, 18.99)
    assert row.delta == 0.59
    assert row.price_range == "15.99 - 23.99"
    assert row.status == "preview"


def test_an_ok_price_is_left_out_of_the_write_set():
    row = frp.to_row(queue_row(
        verdict="ok", corrected_price=18.99, change_required=False,
    ))
    assert row.needs_change is False
    # Blank, not the echoed current price: the corrected figure comes back even
    # on a clean verdict, and putting it in the New Price column would read as a
    # pending change on every single run.
    assert row.new_price is None
    assert row.delta is None


def test_a_verdict_hermes_could_not_reach_is_marked_not_written():
    """A queue row with no assessment is the shape of a Hermes outage. It must
    not be silently counted as correct, and must never be written."""
    raw = queue_row()
    raw["verification"] = {"correct": None, "findings": [], "price": None}
    row = frp.to_row(raw)
    assert row.status == "unchecked"
    assert row.needs_change is False
    assert row.new_price is None
    assert "unreachable" in row.reason


def test_change_required_is_trusted_over_re_deriving_it():
    """The backend's own flag decides. A corrected_price that differs from the
    current price but is NOT flagged is still left alone -- re-deriving the
    decision here is how the two ends drift apart."""
    row = frp.to_row(queue_row(
        verdict="ok", corrected_price=99.99, change_required=False,
    ))
    assert row.new_price is None
    assert row.needs_change is False


def test_a_clamp_downwards_reports_a_negative_delta():
    raw = queue_row(verdict="too_high", corrected_price=23.99)
    raw["price"] = "35.00"
    row = frp.to_row(raw)
    assert row.delta == -11.01
    assert row.delta_pct == pytest.approx(-31.46, abs=0.01)


def test_a_missing_current_price_still_yields_a_writable_row():
    """A product with no price at all is exactly what the correction supplies,
    so it counts as needing a change even with nothing to subtract from."""
    raw = queue_row(verdict="no_price", corrected_price=19.99)
    raw["price"] = None
    row = frp.to_row(raw)
    assert row.current is None
    assert row.needs_change is True
    assert row.delta is None      # no baseline, so no delta -- not 19.99
    assert row.delta_pct is None


def test_every_column_has_a_cell_and_nothing_shifts():
    """A mismatch here silently writes values under the wrong headings, which is
    worse than a crash: the sheet still opens and still looks plausible."""
    row = frp.to_row(queue_row())
    assert len(row.cells()) == len(frp.COLUMNS)
    by_name = dict(zip([name for name, _ in frp.COLUMNS], row.cells()))
    assert by_name["Product ID"] == "1a351701-7568-4414-8975-ce07d664ab95"
    assert by_name["Current Price"] == 18.40
    assert by_name["Retail Price"] == 39.00
    assert by_name["Expected Range"] == "15.99 - 23.99"
    assert by_name["New Price"] == 18.99
    assert by_name["Needs Change"] == "YES"


def test_the_sheet_is_written_with_both_tabs(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    rows = [frp.to_row(queue_row()),
            frp.to_row(queue_row(verdict="ok", change_required=False))]
    totals = frp.Totals(seen=2, needs_change=1, movement=0.59,
                        verdicts={"round_required": 1, "ok": 1})
    args = frp.parse_args(["--limit", "2"])
    out = tmp_path / "sheet.xlsx"

    frp.write_sheet(out, rows, total=23, totals=totals, args=args, applied=False)

    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["Prices", "Summary"]
    prices = wb["Prices"]
    assert [c.value for c in prices[1]] == [name for name, _ in frp.COLUMNS]
    assert prices.max_row == 3          # header + two products
    assert prices.freeze_panes == "A2"

    summary = dict(
        (r[0], r[1]) for r in wb["Summary"].iter_rows(values_only=True)
    )
    assert summary["Total products on the review page"] == 23
    assert summary["...of those, price needs a change"] == 1
    assert "PREVIEW" in summary["Mode"]
