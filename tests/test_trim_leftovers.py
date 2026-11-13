"""scripts/trim_leftovers_from_sheet.py (13 Nov 2026, BOA-007716). No network, no database."""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import trim_leftovers_from_sheet as tl  # noqa: E402


def test_dry_run_and_live(monkeypatch):
    old = [{"id": "a", "view": "AI_CLOSEUP", "createdAt": datetime(2026, 10, 26)}]
    monkeypatch.setattr(tl, "leftovers", lambda dsn, pid: list(old))
    done = []
    monkeypatch.setattr(tl.db, "execute", lambda sql, p=None: (done.append(p), 1)[1])
    monkeypatch.setattr(tl.rc, "_order_and_push", lambda dsn, pid, api, log: "Shopify push queued")
    assert tl.trim_one("d", "p", "S", apply=False, vnyx_api=Path("."))["result"].startswith("WOULD retire 1")
    assert done == []
    r = tl.trim_one("d", "p", "S", apply=True, vnyx_api=Path("."))
    assert r["result"] == "TRIMMED 1" and done[0]["ids"] == ["a"] and "Shopify push queued" in r["log"]


def test_nothing_left_over_is_skipped(monkeypatch):
    monkeypatch.setattr(tl, "leftovers", lambda dsn, pid: [])
    assert tl.trim_one("d", "p", "S", apply=True, vnyx_api=Path("."))["result"].startswith("SKIPPED")
