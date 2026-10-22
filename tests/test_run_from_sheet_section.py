"""run_from_sheet.py tells the chain which section a product is in (11 Oct 2026).

runner.verify_one's automatic rejections are REVIEW-ONLY. A UI run scoped to the
Approved tab passes section="APPROVED", because rejecting an approved product takes
its live listing down. The script never passed a section, so a sheet run with
--include-approved ran approved products under the Review rules. These tests hold
the two paths together.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_from_sheet as rfs  # noqa: E402


@pytest.mark.parametrize("review, section", [
    ("ACCEPTED", "APPROVED"),
    ("REJECTED", "REJECTED"),
    ("PENDING", "REVIEW"),
    ("PENDING_DECISON", "REVIEW"),
    (None, "REVIEW"),
])
def test_the_section_follows_the_review_status(review, section):
    assert rfs.section_of({"reviewStatus": review}) == section


def test_an_accepted_product_at_label_is_still_in_the_approved_tab():
    """The app's tabs read reviewStatus, not the stage cache (1,941 BOAS rows)."""
    assert rfs.section_of({"reviewStatus": "ACCEPTED", "currentStage": "LABEL"}) == "APPROVED"


def _pool() -> rfs.Parallel:
    """A Parallel without its threads — `_one` is driven directly."""
    p = object.__new__(rfs.Parallel)
    p.rs, p.stages = object(), ["REVIEW", "APPROVED"]
    p.reject_attrs, p.apply = True, True
    p.lock = threading.Lock()
    p.tally = {"ok": 0, "held": 0, "failed": 0, "retry": 0}
    p.skip_matte = p.price_rounding_only = p.skip_price = False
    p.abort = None
    return p


@pytest.fixture
def drive(monkeypatch):
    """Run one product through Parallel._one; return verify_one's kwargs and
    whether the script's own missing-attribute rejection was asked."""
    seen: dict = {"rejected": False}

    def fake_verify_one(row, rs, **kw):
        seen["kwargs"] = kw
        return None

    def fake_reject(*a, **k):
        seen["rejected"] = True
        return "rejected"

    monkeypatch.setattr(rfs.db, "execute", lambda *a, **k: 1)
    monkeypatch.setattr(rfs.db, "fetch_one",
                        lambda *a, **k: {"status": "HELD_FOR_HUMAN", "steps": []})
    monkeypatch.setattr(rfs.runner, "verify_one", fake_verify_one)
    monkeypatch.setattr(rfs.runner, "stop_reason", lambda abort: None)
    monkeypatch.setattr(rfs, "reject_missing_attrs", fake_reject)

    def run(review: str) -> dict:
        row = {"sku": "KLE-1", "title": "t", "tenantId": "t1", "reviewStatus": review}
        _pool()._one({"row": row, "claimed": {"id": "q1"}, "run_id": "r1", "n": 1, "total": 1})
        return seen

    return run


def test_an_approved_product_runs_as_approved(drive):
    seen = drive("ACCEPTED")
    assert seen["kwargs"]["section"] == "APPROVED"
    assert seen["rejected"] is False, "an approved product must never be rejected by the script"


def test_a_review_product_runs_as_review(drive):
    seen = drive("PENDING")
    assert seen["kwargs"]["section"] == "REVIEW"
    assert seen["rejected"] is True
