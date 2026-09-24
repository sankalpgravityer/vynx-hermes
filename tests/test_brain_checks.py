"""The Brain's per-tenant checks, as the worker applies them.

vnyx-api compiles a tenant's switches (services/auto-approval/checks.ts) into
rule overrides, a policy overlay and chain flags, and freezes them in the run's
configSnapshot. What is pinned here is this side of that contract:

  * app.config.policy_overlay — deep, nested, per-thread, and a no-op when empty;
  * settings_from_snapshot — an OLD snapshot runs the chain as it always did;
  * repair() — the price step's four modes, the approval without the Shopify
    push, and the re-sync of a product that is already live;
  * run_remote — the new flags travel as typed options.

No network, no database, no subprocess.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as appconfig  # noqa: E402
from app.config import deep_merge, policy, policy_overlay  # noqa: E402
from app.imaging import photo_audit as pa  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402
from app.services.auto_approval.config import settings_from_snapshot  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000001"


# --------------------------------------------------------------------------- #
# policy_overlay
# --------------------------------------------------------------------------- #

def test_deep_merge_merges_dicts_and_replaces_lists():
    base = {"a": {"b": 1, "c": [1, 2]}, "d": 1}
    out = deep_merge(base, {"a": {"c": [9]}, "e": 2})
    assert out == {"a": {"b": 1, "c": [9]}, "d": 1, "e": 2}
    assert base == {"a": {"b": 1, "c": [1, 2]}, "d": 1}   # not mutated


def test_the_overlay_applies_inside_the_block_only():
    shipped = policy()["quality_gate"]["block_on"]
    with policy_overlay({"quality_gate": {"block_on": ["no_model"]}}):
        assert policy()["quality_gate"]["block_on"] == ["no_model"]
        # Untouched keys are the shipped ones.
        assert policy()["quality_gate"]["lead_views"] == \
            appconfig._base_policy()["quality_gate"]["lead_views"]
    assert policy()["quality_gate"]["block_on"] == shipped


def test_an_empty_overlay_is_policy_yaml_unchanged():
    with policy_overlay({}):
        assert policy() is appconfig._base_policy()
    with policy_overlay(None):
        assert policy() is appconfig._base_policy()


def test_overlays_nest_and_unwind():
    with policy_overlay({"readiness": {"max_regenerations_per_run": 0}}):
        with policy_overlay({"readiness": {"kids_renders": "hold"}}):
            r = policy()["readiness"]
            assert r["max_regenerations_per_run"] == 0 and r["kids_renders"] == "hold"
        assert policy()["readiness"]["kids_renders"] == \
            appconfig._base_policy()["readiness"]["kids_renders"]


def test_one_threads_overlay_does_not_reach_another():
    seen: dict[str, Any] = {}
    inside = threading.Event()
    release = threading.Event()

    def tenant_a():
        with policy_overlay({"photo_audit": {"enabled": False}}):
            inside.set()
            release.wait(5)

    t = threading.Thread(target=tenant_a)
    t.start()
    inside.wait(5)
    seen["b"] = policy()["photo_audit"]["enabled"]
    release.set()
    t.join(5)
    assert seen["b"] is appconfig._base_policy()["photo_audit"]["enabled"]


def test_cache_clear_still_reaches_the_file_cache():
    policy.cache_clear()   # reload_policy and the tests call it by this name
    assert policy() is appconfig._base_policy()


# --------------------------------------------------------------------------- #
# settings_from_snapshot
# --------------------------------------------------------------------------- #

def test_a_snapshot_without_checks_runs_the_chain_as_before():
    rs = settings_from_snapshot({"mode": "LIVE", "brain": {"useLlm": True}})
    assert (rs.matte, rs.gate, rs.price_mode, rs.publish_on_approve, rs.sync_changes) == \
        (True, True, "full", True, False)
    assert rs.policy_overlay == {}


def test_the_compiled_checks_are_read():
    rs = settings_from_snapshot({"brain": {
        "severityOverrides": {"TAX.004": "off"},
        "compiled": {
            "policy": {"quality_gate": {"block_on": ["no_model"]}},
            "chain": {"matte": False, "gate": False, "priceMode": "rounding_only",
                      "publishOnApprove": False, "syncChanges": True},
        },
    }})
    assert rs.severity_overrides == {"TAX.004": "off"}
    assert rs.policy_overlay == {"quality_gate": {"block_on": ["no_model"]}}
    assert (rs.matte, rs.gate, rs.price_mode, rs.publish_on_approve, rs.sync_changes) == \
        (False, False, "rounding_only", False, True)


def test_an_unknown_price_mode_falls_back_to_the_full_step():
    rs = settings_from_snapshot({"brain": {"compiled": {"chain": {"priceMode": "later"}}}})
    assert rs.price_mode == "full"


# --------------------------------------------------------------------------- #
# repair()
# --------------------------------------------------------------------------- #

def _state(**over: Any) -> dict[str, Any]:
    base = {
        "loaded": {
            "record": {"gender": ["men"], "category": "Jeans", "subCategory": "Jeans",
                       "sizingGuide": None, "updatedAt": None},
            "media": [{"url": "https://x/f.png", "view": "AI_FRONT",
                       "mediaType": "IMAGE", "position": 1}],
            "catalog": {},
        },
        "title": "Test jeans", "tenant": "T", "sku": "T-1", "stage": "REVIEW",
        "review_status": "PENDING", "edit_url": None,
        "description_missing": False, "description_chars": 100,
        "care_label": 1, "unmatted": 0, "unmatted_views": [], "leftover_raw": 0,
        "renders": 5, "render_rows": 5, "renders_missing": 0,
        "generation_status": "COMPLETE", "is_regenerating": False,
        "attributes_missing": [],
    }
    base.update(over)
    return base


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, Any] = {"scripts": [], "approve": []}
    outcome: dict[str, Any] = {"outcome": "would_approve", "problems": []}
    shopify: dict[str, Any] = {"id": None}

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        calls["scripts"].append((script, list(args)))
        if script == "verify-and-repair.ts":
            return True, "", {"ok": True, "applied": ["gender"], "failed": []}
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    def fake_approve_check(vnyx_api, dsn, pid, *, apply, skip_bin, quiet,
                           allow_stage=None, publish=True):
        calls["approve"].append({"apply": apply, "publish": publish})
        return dict(outcome)

    reads = {"n": 0}
    stamp = {"after": None}

    def fake_needs(dsn, pid):
        # The first read is the pre-chain baseline; later ones carry whatever
        # updatedAt the test says the chain left behind.
        reads["n"] += 1
        s = _state()
        if reads["n"] > 1 and stamp["after"]:
            s["loaded"]["record"]["updatedAt"] = stamp["after"]
        return s

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "approve_check", fake_approve_check)
    monkeypatch.setattr(rp, "shopify_product_id", lambda dsn, pid: shopify["id"])
    monkeypatch.setattr(qg, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(
        rp.product_audit, "audit",
        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}},
    )
    return {"calls": calls, "approve": outcome, "shopify": shopify, "stamp": stamp}


def _repair(**kw: Any) -> dict[str, Any]:
    args = dict(apply=True, vnyx_api=Path("."), infer=False, min_confidence=70,
                skip_render=True, approve=False, skip_bin=True, quiet=True, silent=True)
    args.update(kw)
    return rp.repair(DSN, PID, **args)


def step(r: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in r["steps"] if s["step"] == name)


def price_args(wired) -> list[str]:
    return next(a for s, a in wired["calls"]["scripts"] if s == "fix-selling-price.ts")


@pytest.mark.parametrize("mode, flag", [
    ("full", None), ("window_only", "--skip-rounding"), ("rounding_only", "--rounding-only"),
])
def test_the_price_step_runs_in_the_brains_mode(wired, mode, flag):
    _repair(price_mode=mode)
    args = price_args(wired)
    for f in ("--skip-rounding", "--rounding-only"):
        assert (f in args) is (f == flag)


def test_price_off_skips_the_step(wired):
    r = _repair(price_mode="off")
    assert step(r, "price")["ran"] is False
    assert "window and the .99 rounding off" in step(r, "price")["why"]
    assert not any(s == "fix-selling-price.ts" for s, _ in wired["calls"]["scripts"])


def test_the_old_rounding_only_flag_still_means_rounding_only(wired):
    _repair(price_rounding_only=True)
    assert "--rounding-only" in price_args(wired)


def test_a_mode_the_server_cannot_take_is_skipped_not_widened(wired, monkeypatch):
    monkeypatch.setattr(rp, "remote_supports", lambda *o: False)
    r = _repair(price_mode="rounding_only")
    assert step(r, "price")["ran"] is False
    assert "cannot run the price step" in step(r, "price")["why"]


def test_publish_off_reaches_the_approve_step(wired):
    _repair(approve=True, publish=False)
    assert wired["calls"]["approve"][-1] == {"apply": True, "publish": False}


def test_an_approval_without_the_push_says_so(wired):
    wired["approve"].update({"outcome": "approved", "published": False,
                             "stageBefore": "REVIEW", "stageAfter": "APPROVED"})
    r = _repair(approve=True, publish=False)
    assert "NOT published" in step(r, "approve")["note"]


def test_sync_is_off_by_default(wired):
    r = _repair()
    assert step(r, "sync")["ran"] is False
    assert "off in the Brain" in step(r, "sync")["why"]


def test_sync_skips_a_run_that_wrote_nothing(wired):
    wired["shopify"]["id"] = "gid://shopify/Product/1"
    r = _repair(sync_changes=True)
    assert "nothing was repaired" in step(r, "sync")["why"]


def test_sync_never_creates_a_listing(wired):
    wired["stamp"]["after"] = "2026-09-23T10:00:00"
    r = _repair(sync_changes=True)
    assert step(r, "sync")["ran"] is False
    assert "not on Shopify" in step(r, "sync")["why"]
    assert not any(s == "resync-listings.ts" for s, _ in wired["calls"]["scripts"])


def test_sync_skips_a_product_the_approval_just_pushed(wired):
    wired["shopify"]["id"] = "gid://shopify/Product/1"
    wired["approve"].update({"outcome": "approved"})
    r = _repair(sync_changes=True, approve=True)
    assert "just approved" in step(r, "sync")["why"]


def test_sync_repushes_a_live_product_after_a_repair(wired):
    wired["shopify"]["id"] = "gid://shopify/Product/1"
    wired["stamp"]["after"] = "2026-09-23T10:00:00"
    r = _repair(sync_changes=True)
    assert step(r, "sync")["ran"] and step(r, "sync")["ok"]
    args = next(a for s, a in wired["calls"]["scripts"] if s == "resync-listings.ts")
    assert args == ["--product", PID, "--apply"]
    assert r["sync"]["ran"] is True


# --------------------------------------------------------------------------- #
# approve_check and run_remote
# --------------------------------------------------------------------------- #

def test_approve_check_passes_no_publish(monkeypatch):
    sent: list[list[str]] = []
    monkeypatch.setattr(rp, "run_step", lambda v, s, a, **k: sent.append(a) or
                        (True, "", {"results": [{"outcome": "approved"}]}))
    rp.approve_check(Path("."), DSN, PID, apply=True, skip_bin=False, quiet=True, publish=False)
    assert "--apply" in sent[-1] and "--no-publish" in sent[-1]


def test_approve_check_withholds_the_move_when_the_server_cannot_hold_the_push(monkeypatch):
    sent: list[list[str]] = []
    monkeypatch.setattr(rp, "remote_supports", lambda *o: False)
    monkeypatch.setattr(rp, "run_step", lambda v, s, a, **k: sent.append(a) or
                        (True, "", {"results": [{"outcome": "would_approve"}]}))
    rp.approve_check(Path("."), DSN, PID, apply=True, skip_bin=False, quiet=True, publish=False)
    assert "--apply" not in sent[-1]


def test_run_remote_sends_the_brain_flags_as_typed_options(monkeypatch):
    sent: dict[str, Any] = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "output": "", "results": None}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, json, headers, timeout: sent.update(json) or Resp())
    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    rp.run_remote("approve-products.ts", ["--db", DSN, "--product", PID, "--apply", "--no-publish"],
                  timeout_s=10, quiet=True)
    assert sent["options"] == {"noPublish": True, "approve": True}
    rp.run_remote("fix-selling-price.ts", ["--db", DSN, "--product", PID, "--rounding-only"],
                  timeout_s=10, quiet=True)
    assert sent["options"] == {"roundingOnly": True}
    rp.run_remote("fix-selling-price.ts", ["--db", DSN, "--product", PID, "--skip-rounding"],
                  timeout_s=10, quiet=True)
    assert sent["options"] == {"skipRounding": True}


# --------------------------------------------------------------------------- #
# /v1/approval-gate — the reconcile step plans against the tenant's Brain
# --------------------------------------------------------------------------- #

def _gate_body(**extra: Any) -> dict[str, Any]:
    from tests.test_gate_field_completion import GUIDES, TREE

    product = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Hoodies", "subCategory": "Hoodies",
        "size": "S", "internationalSize": "S", "euSize": "46",
        "sizingGuide": "Men Uppers", "brand": "BOAS", "color": "Burgundy",
        "material": "Cotton", "condition": "As New", "gender": ["men"],
        "careLabelCount": 1,
    }
    catalog = {"categories": TREE, "sizingGuides": GUIDES, "brands": ["BOAS"],
               "colors": ["Burgundy"], "materials": ["Cotton"]}
    return {"product": product, "catalog": catalog, **extra}


def _rule_ids(out: dict[str, Any]) -> set[str]:
    found = [*(out.get("blocking") or []), *(out.get("advisory") or [])]
    return {str(f.get("rule_id") or f.get("ruleId") or f.get("rule")) for f in found}


def test_the_gate_endpoint_drops_a_rule_the_brain_switched_off():
    from fastapi.testclient import TestClient

    from app.main import app

    client = TestClient(app)
    plain = client.post("/v1/approval-gate", json=_gate_body()).json()
    assert "TAX.002" in _rule_ids(plain)

    off = client.post("/v1/approval-gate", json=_gate_body(
        severity_overrides={"TAX.002": "off", "DATA.010:category": "off"},
        policy_overlay={"readiness": {"copy": {"enabled": False}}},
    )).json()
    assert "TAX.002" not in _rule_ids(off)
    # No repair planned for a check the tenant does not run.
    assert not any(a.get("field") == "category" and a.get("reason") == "TAX.002"
                   for a in off.get("repair_plan") or [])
