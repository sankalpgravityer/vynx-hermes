"""A product with no care-label photograph stays in Review, held for a person (7 Oct 2026).

It used to be moved to the Rejected tab through the reject step. The user's call: keep it
in Review with verification status HELD_FOR_HUMAN, the reason on it, for someone to add
the label photograph and run it again. `readiness.no_care_label: reject` restores the
old behaviour. Decided before the chain either way: nothing it does can produce a
photograph of a physical tag, so no paid call is spent on it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import policy, policy_overlay  # noqa: E402
from app.services.auto_approval import runner  # noqa: E402
from app.services.auto_approval.config import settings_from_snapshot  # noqa: E402

ROW = {"id": "r", "runId": "u", "tenantId": "t", "productId": "p",
       "productSku": "MID-000999", "productTitle": "", "attempts": 0, "maxAttempts": 3}


class _NullConn:
    def cursor(self, *a, **k): return self
    def execute(self, *a, **k): return None
    def fetchone(self): return None
    def commit(self): return None
    def rollback(self): return None
    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def wired(monkeypatch):
    seen: dict[str, list] = {"archived": [], "finished": [], "chain": []}
    monkeypatch.setattr(runner, "_has_care_label", lambda pid: False)
    monkeypatch.setattr(runner, "_archive", lambda row, why: seen["archived"].append(why) or True)
    monkeypatch.setattr(runner, "_finish", lambda row, verdict, **k: seen["finished"].append(verdict))
    def repair(*a, **k):
        seen["chain"].append("repair")
        raise RuntimeError("the chain ran")

    monkeypatch.setattr(runner, "_load_repair", lambda: repair)
    monkeypatch.setattr(runner.db, "connection", lambda **kw: _NullConn())
    monkeypatch.setattr(runner.db, "assert_tenant", lambda *a, **k: True)
    monkeypatch.setattr(runner.events, "emit", lambda *a, **k: None)
    return seen


def _run():
    rs = settings_from_snapshot({"mode": "SHADOW", "shadowWritesRepairs": True})
    assert rs.apply
    try:
        runner.verify_one(dict(ROW), rs, ignore_stop=True, section="REVIEW")
    except RuntimeError:
        pass


def test_no_care_label_is_held_in_review_not_rejected(wired):
    _run()
    assert wired["archived"] == []                       # never sent to the Rejected tab
    assert wired["chain"] == []                          # nothing paid for
    (v,) = wired["finished"]
    assert (v.status, v.outcome, v.approved, v.retryable) == ("HELD_FOR_HUMAN", "NO_CARE_LABEL", False, False)
    assert "care label is missing" in v.reason


def test_reject_restores_the_move_to_the_rejected_tab(wired):
    with policy_overlay({"readiness": {"no_care_label": "reject"}}):
        _run()
    assert wired["archived"] == ["care label is missing"]
    (v,) = wired["finished"]
    assert (v.status, v.outcome) == ("FAILED", "NO_CARE_LABEL")


def test_the_shipped_policy_holds_and_buys_two_regen_rounds():
    """Two rounds since 10 Oct 2026 (was 10): a render wrong twice stayed wrong."""
    from app import readiness

    cfg = readiness.config(policy())
    assert cfg["no_care_label"] == "hold" and cfg["regen_rounds"] == 2
