"""The run-close race (24 Sep 2026, BOA-005794).

vnyx-api creates a run and THEN inserts its queue rows in a second
transaction. A sweep tick between the two saw an empty run and closed it, the
product stayed QUEUED under a COMPLETED run, and — because the sweep dispatches
a pump for a manual run only while it is RUNNING — nothing ever claimed it.

Pinned here: the close waits out a grace period, the reopen exists and only
touches runs the automatic close ended, and the sweep reopens BEFORE it decides
which tenants get a pump.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.auto_approval import claim, sweep  # noqa: E402


def test_close_waits_for_a_run_that_is_still_being_filled():
    sql = inspect.getsource(claim.close_finished_runs)
    assert "interval '2 minutes'" in sql
    assert "NOT EXISTS" in sql


def test_reopen_touches_only_runs_the_automatic_close_ended():
    sql = inspect.getsource(claim.reopen_orphaned_runs)
    assert "'all products processed'" in sql
    assert "'QUEUED'" in sql and "'IN_PROGRESS'" in sql
    assert "status = 'COMPLETED'" in sql


def test_the_sweep_reopens_before_it_decides_who_gets_a_pump(monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(sweep, "preflight_result", lambda: {"ok": True, "problems": []})
    monkeypatch.setattr(claim, "reopen_orphaned_runs", lambda: order.append("reopen") or 1)
    monkeypatch.setattr(sweep.cfgmod, "enabled_configs",
                        lambda: order.append("configs") or [])
    for name in ("reap_expired_leases", "cancel_stale_queued", "close_finished_runs",
                 "roll_over_arrival_runs", "prune_old_events"):
        monkeypatch.setattr(claim, name, (lambda *a, **k: {} if "reap" in name else 0))
    monkeypatch.setattr(sweep.events, "emit", lambda *a, **k: None, raising=False)
    out = sweep.sweep()
    # Reopened right after the configs are read and before the per-tenant loop
    # (where has_open_manual_run decides the pump) — never at the end with the
    # rest of the housekeeping, which would cost a tick.
    assert order == ["configs", "reopen"]
    assert out["runsReopened"] == 1
