"""shopify_cutouts.py --recheck-held: the products the cut-out check held, checked
again after the check changed (12 Oct 2026). Verification only — no push."""
from __future__ import annotations

import pytest

from scripts import shopify_cutouts as sc

HELD = {"id": "p1", "tenant": "Klekt", "tenant_id": "t1", "sku": "KLE-000398", "title": "x",
        "vs": "HELD_FOR_HUMAN", "oc": "CUTOUT_DEFECT", "category": "Women", "subcategory": "Sweatshirts",
        "prev_status": "VERIFIED", "prev_outcome": "ALREADY_APPROVED",
        "prev_reason": "Already approved — nothing to do."}
CLS = {"sent_cutouts": [], "by_id": {}}


@pytest.fixture
def writes(monkeypatch):
    calls = {"sql": [], "events": []}

    def execute(sql, params=None):
        calls["sql"].append((sql, params))
        return calls.get("rowcount", 1)

    monkeypatch.setattr(sc.db, "execute", execute)
    monkeypatch.setattr(sc.events, "emit", lambda *a, **k: calls["events"].append((a, k)))
    return calls


def _ai(monkeypatch, defects, note="", seen=()):
    monkeypatch.setattr(sc, "ai_check", lambda p, c, b: (list(defects), note, list(seen)))


def test_only_a_product_the_cutout_check_held_from_verified_is_eligible():
    assert sc.recheck_eligible(HELD) == ""
    assert "not held by the cut-out check" in sc.recheck_eligible({**HELD, "oc": "MISSING_ATTRS"})
    assert "not held by the cut-out check" in sc.recheck_eligible({**HELD, "vs": "VERIFIED"})
    assert "not VERIFIED" in sc.recheck_eligible({**HELD, "prev_status": "HELD_FOR_HUMAN"})


def test_a_pass_goes_back_to_its_last_verdict(monkeypatch, writes):
    _ai(monkeypatch, [])
    r = sc.recheck(HELD, CLS, apply=True)
    assert r["status"] == "VERIFIED"
    sql, params = writes["sql"][0]
    assert params["st"] == "VERIFIED" and params["oc"] == "ALREADY_APPROVED"
    assert params["r"].startswith(sc.RECHECK_PASSED) and "Already approved" in params["r"]
    assert "'HELD_FOR_HUMAN'" in sql and "'CUTOUT_DEFECT'" in sql      # guarded
    assert writes["events"] and writes["events"][0][0][1] == "pass"


def test_a_fail_stays_held_with_the_new_reason(monkeypatch, writes):
    _ai(monkeypatch, ["CUTOUT DEFECT — FRONT cut-out: cloth broken (collar) [clear]"])
    r = sc.recheck(HELD, CLS, apply=True)
    assert r["status"] == "HELD_FOR_HUMAN"
    _, params = writes["sql"][0]
    assert params["st"] == "HELD_FOR_HUMAN" and params["oc"] == "CUTOUT_DEFECT"
    assert "cloth broken (collar)" in params["r"]
    assert not writes["events"]


def test_no_answer_writes_nothing(monkeypatch, writes):
    _ai(monkeypatch, [], note="AI check could not run — TimeoutError")
    r = sc.recheck(HELD, CLS, apply=True)
    assert r["status"] == "HELD_FOR_HUMAN" and not writes["sql"]
    _ai(monkeypatch, [], note="no cut-out to look at")
    assert sc.recheck(HELD, CLS, apply=True)["status"] == "HELD_FOR_HUMAN" and not writes["sql"]


def test_read_only_says_what_it_would_do_and_writes_nothing(monkeypatch, writes):
    _ai(monkeypatch, [])
    r = sc.recheck(HELD, CLS, apply=False)
    assert r["status"] == "VERIFIED" and r["done"] == ["would be verified"]
    assert not writes["sql"] and not writes["events"]


def test_a_row_a_person_changed_since_is_left_alone(monkeypatch, writes):
    writes["rowcount"] = 0
    _ai(monkeypatch, [])
    r = sc.recheck(HELD, CLS, apply=True)
    assert r["status"] == "HELD_FOR_HUMAN" and "changed since" in r["done"][0]
    assert not writes["events"]
