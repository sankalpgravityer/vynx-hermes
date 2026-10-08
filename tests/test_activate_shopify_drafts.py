"""The Brain's `activateShopifyDrafts` (8 Oct 2026): make an approved product's Shopify
DRAFT live.

Some approved products are Active in vnyx and still Drafts on Shopify (MID-000503). With
the switch on, a product the agent finds ALREADY APPROVED and passing every check goes
through vnyx-api's `golive` step, which reads Shopify's status and changes only
draft → active. What must hold here:

  * only ALREADY_APPROVED + VERIFIED products — never one just approved (that is
    publishActiveOnApprove's), never one held or failed;
  * LIVE writes; any other pass is a dry run that only asks Shopify;
  * the verdict is the same with or without it, and a failed go-live never fails the
    product;
  * an older vnyx-api without the step is not sent it.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.auto_approval import runner  # noqa: E402
from app.services.auto_approval.config import settings_from_snapshot  # noqa: E402
from app.services.auto_approval.outcome import Verdict  # noqa: E402

ROW = {"id": "r", "runId": "u", "tenantId": "t", "productId": "p",
       "productSku": "MID-000503", "productTitle": "", "attempts": 0, "maxAttempts": 3}

ALREADY = Verdict("VERIFIED", "ALREADY_APPROVED", "Already approved — nothing to do.", False, False)
JUST = Verdict("VERIFIED", "APPROVED", "Approved and moved to APPROVED.", True, False)
HELD = Verdict("HELD_FOR_HUMAN", "PREFLIGHT_BLOCKED", "price outside the window", False, False)


def _rs(mode: str = "LIVE", on: bool = True):
    return settings_from_snapshot({
        "mode": mode, "shadowWritesRepairs": True,
        "brain": {"compiled": {"chain": {"activateDrafts": on}}},
    })


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
    seen: dict = {"finished": [], "golive": [], "events": [], "verdict": ALREADY,
                  "answer": {"outcome": "activated", "reason": "Draft → Active on Shopify"}}
    monkeypatch.setattr(runner, "_finish", lambda row, verdict, **k: seen["finished"].append((verdict, k)))
    monkeypatch.setattr(runner, "_load_repair",
                        lambda: (lambda *a, **k: {"steps": [], "remaining": [], "generation": {}}))
    monkeypatch.setattr(runner, "classify", lambda result: seen["verdict"])
    monkeypatch.setattr(runner.db, "connection", lambda **kw: _NullConn())
    monkeypatch.setattr(runner.db, "assert_tenant", lambda *a, **k: True)
    monkeypatch.setattr(runner.events, "emit",
                        lambda tenant, tag, msg, **k: seen["events"].append((tag, msg)))

    def golive(row, *, apply):
        seen["golive"].append(apply)
        return seen["answer"]

    monkeypatch.setattr(runner, "_activate_shopify_draft", golive)
    return seen


def _verify(rs, section="APPROVED"):
    runner.verify_one(dict(ROW), rs, ignore_stop=True, section=section)


def test_the_switch_reaches_the_run_settings():
    assert _rs(on=True).activate_drafts is True
    # Absent in an older snapshot: off, as the agent always behaved.
    assert settings_from_snapshot({"mode": "LIVE"}).activate_drafts is False


def test_live_makes_a_passing_approved_draft_live_and_keeps_the_verdict(wired):
    _verify(_rs("LIVE"))
    assert wired["golive"] == [True]
    (verdict, kw) = wired["finished"][0]
    assert verdict is ALREADY                                   # unchanged
    assert kw["deltas"]["shopifyGoLive"]["outcome"] == "activated"
    assert ("pass", "MID-000503 — Shopify Draft → Active: Draft → Active on Shopify") in wired["events"]


def test_shadow_only_asks_shopify(wired):
    wired["answer"] = {"outcome": "would_activate", "reason": "Draft on Shopify — would be made Active"}
    _verify(_rs("SHADOW"))
    assert wired["golive"] == [False]


def test_switch_off_touches_nothing(wired):
    _verify(_rs("LIVE", on=False))
    assert wired["golive"] == []
    assert wired["finished"][0][1]["deltas"]["shopifyGoLive"] is None


@pytest.mark.parametrize("verdict", [JUST, HELD])
def test_only_already_approved_and_passing(wired, verdict):
    wired["verdict"] = verdict
    _verify(_rs("LIVE"))
    assert wired["golive"] == []


def test_a_held_approved_product_says_why_it_stayed_a_draft(wired):
    wired["verdict"] = HELD
    _verify(_rs("LIVE"))
    assert any(tag == "check" and "did not pass every check" in msg for tag, msg in wired["events"])


def test_a_failed_go_live_is_a_warning_not_a_failed_product(wired):
    wired["answer"] = {"outcome": "failed", "reason": "could not read the Shopify status: 429"}
    _verify(_rs("LIVE"))
    (verdict, _kw) = wired["finished"][0]
    assert verdict is ALREADY
    assert any(tag == "warn" and "NOT made live" in msg for tag, msg in wired["events"])


# ---- the step call itself ---------------------------------------------------

@pytest.fixture
def rp(monkeypatch):
    import importlib
    mod = importlib.import_module("scripts.repair_product")
    monkeypatch.setattr(runner, "_vnyx_api_dir", lambda: Path("."))
    return mod


def test_step_call_passes_apply_and_returns_the_scripts_verdict(rp, monkeypatch):
    calls = []
    monkeypatch.setattr(rp, "remote_has_step", lambda s: True)
    monkeypatch.setattr(rp, "run_step", lambda d, script, args, **k: calls.append((script, args, k))
                        or (True, "", {"outcome": "activated", "reason": "ok"}))
    assert runner._activate_shopify_draft(ROW, apply=True)["outcome"] == "activated"
    assert calls[0][0] == "activate-shopify-draft.ts"
    assert calls[0][1] == ["--product", "p", "--apply"]
    runner._activate_shopify_draft(ROW, apply=False)
    assert calls[1][1] == ["--product", "p"]                    # dry run: no --apply


def test_an_older_vnyx_api_is_not_sent_the_step(rp, monkeypatch):
    monkeypatch.setattr(rp, "remote_has_step", lambda s: False)
    monkeypatch.setattr(rp, "run_step", lambda *a, **k: pytest.fail("sent to a server without the step"))
    out = runner._activate_shopify_draft(ROW, apply=True)
    assert out["outcome"] == "skipped" and "deploy vnyx-api" in out["reason"]


def test_a_step_that_raises_is_reported_not_raised(rp, monkeypatch):
    monkeypatch.setattr(rp, "remote_has_step", lambda s: True)

    def boom(*a, **k):
        raise rp.StepFailed("activate-shopify-draft.ts: cannot reach vnyx-api")

    monkeypatch.setattr(rp, "run_step", boom)
    out = runner._activate_shopify_draft(ROW, apply=True)
    assert out["outcome"] == "failed" and "cannot reach" in out["reason"]


def test_remote_has_step_vetoes_only_on_a_listed_absence(rp, monkeypatch):
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", {"replace"})
    monkeypatch.setattr(rp, "_REMOTE_STEPS", {"matte", "approve"})
    assert rp.remote_has_step("activate-shopify-draft.ts") is False
    monkeypatch.setattr(rp, "_REMOTE_STEPS", {"matte", "golive"})
    assert rp.remote_has_step("activate-shopify-draft.ts") is True
    # A ping that listed no steps (an older server) is not evidence either way.
    monkeypatch.setattr(rp, "_REMOTE_STEPS", set())
    assert rp.remote_has_step("activate-shopify-draft.ts") is True
