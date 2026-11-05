"""The cutcheck step's Gemini fallback (5 Nov 2026, the user; KLE-000361).

When the AI check still refuses a stored cut-out after our models' re-cut, the view
is re-cut with `imagery.cutout.ai_fallback` (Gemini's outline on the photograph's own
pixels) and looked at once more. No network, no database.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.imaging import cutout_integrity as ci  # noqa: E402
from app.services.auto_approval import db as aadb  # noqa: E402
from scripts import repair_product as rp  # noqa: E402
from tests.test_approve_rejected import _repair, chain  # noqa: E402,F401

BAD = ci.IntegrityVerdict("bad", reasons=["CUTOUT DEFECT — FRONT cut-out: colour (waistband)"],
                          bad_views=["FRONT"])
OK = ci.IntegrityVerdict("ok")


@pytest.fixture
def recut(chain, monkeypatch):
    w: dict[str, Any] = {"verdicts": [BAD, BAD, OK], "calls": []}

    def fake_step(vnyx_api, script, args, **kw):
        w["calls"].append([script, *map(str, args)])
        return True, "cut-outs written: 1", {"ok": True, "applied": [], "failed": [], "results": []}

    monkeypatch.setattr(rp, "run_step", fake_step)
    monkeypatch.setattr(ci, "check", lambda *a, **k: w["verdicts"].pop(0) if w["verdicts"] else OK)
    monkeypatch.setattr(aadb, "fetch_one", lambda *a, **k: {"images": [], "category": "Jackets", "sub": "Outdoor Jacket"})
    monkeypatch.setattr(aadb, "fetch_all", lambda *a, **k: [])
    return w


def _recuts(w):
    return [c for c in w["calls"] if c[0] == "backfill-bg-removal.ts"]


def _strategies(call):
    return call[call.index("--bg-strategies") + 1] if "--bg-strategies" in call else None


def _step(r, name):
    return next(s for s in r["steps"] if s["step"] == name)


def test_still_flagged_after_our_recut_goes_to_gemini_and_passes(recut):
    r = _repair(approve_from_rejected=False, skip_matte=False)
    cuts = _recuts(recut)
    assert len(cuts) == 2
    assert _strategies(cuts[0]) != "gemini-paint"          # our models first
    assert _strategies(cuts[1]) == "gemini-paint"          # then Gemini
    assert "--views" in cuts[1] and cuts[1][cuts[1].index("--views") + 1] == "FRONT"
    note = _step(r, "cutcheck")["note"]
    assert "with gemini-paint" in note and "looked again" in note


def test_still_flagged_after_gemini_holds(recut):
    recut["verdicts"] = [BAD, BAD, BAD]
    r = _repair(approve_from_rejected=False, skip_matte=False)
    assert len(_recuts(recut)) == 2
    assert "holds as CUTOUT_DEFECT" in _step(r, "cutcheck")["note"]


def test_fixed_by_our_recut_never_asks_gemini(recut):
    recut["verdicts"] = [BAD, OK]
    _repair(approve_from_rejected=False, skip_matte=False)
    cuts = _recuts(recut)
    assert len(cuts) == 1 and _strategies(cuts[0]) != "gemini-paint"


def test_the_fallback_can_be_turned_off(recut, monkeypatch):
    from app.imaging import cutout as co

    base = co.config()
    monkeypatch.setattr(co, "config", lambda pol=None: {**base, "ai_fallback": []})
    recut["verdicts"] = [BAD, BAD]
    r = _repair(approve_from_rejected=False, skip_matte=False)
    assert len(_recuts(recut)) == 1
    assert "holds as CUTOUT_DEFECT" in _step(r, "cutcheck")["note"]
