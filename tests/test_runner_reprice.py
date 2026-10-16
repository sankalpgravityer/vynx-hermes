"""The Reprice calculator run (15 Oct 2026): what a reprice run does and does not do.

No network, no database: the lookup, the product load and the writes are stubs.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.services.auto_approval import config as cfgmod
from app.services.auto_approval import reprice, runner
from scripts import repair_product as rp

PID = "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c"
ROW = {"id": "row-1", "runId": "run-1", "tenantId": "t-1", "productId": PID,
       "productSku": "KLE-000346", "productTitle": "Stone Island Hoodie",
       "attempts": 1, "maxAttempts": 3}


def snap(preview: bool = True, **extra: Any) -> cfgmod.RunSettings:
    return cfgmod.settings_from_snapshot(
        {"mode": "LIVE", "runKind": "reprice", "reprice": {"preview": preview}, **extra})


def lookup_result(**kw: Any) -> dict[str, Any]:
    base = {"vnyx_price": 223.99, "retail_price": 44.99, "new_price": 367.99, "tier": "A",
            "tier_reason": "3 verified listings, 1.6× VNYX", "new_retail": 256.28,
            "retail_new_eur": 256.28, "retail_verified": "cited", "n_comps": 3,
            "searches": 7, "comps": [], "photos_sent": []}
    return {**base, **kw}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_runs_are_verifications_unless_marked():
    rs = cfgmod.settings_from_snapshot({"mode": "LIVE"})
    assert rs.run_kind == "verify" and rs.reprice_preview is True


def test_a_reprice_snapshot_is_read_and_mode_keeps_its_meaning():
    rs = snap(preview=False)
    assert rs.run_kind == "reprice" and rs.reprice_preview is False
    assert rs.mode == "LIVE"


def test_the_brain_saying_leave_prices_alone_does_not_change_the_reprice_settings():
    rs = snap(preview=False, brain={"compiled": {"chain": {"priceMode": "off"}}})
    assert rs.price_mode == "off"          # what a verification would obey
    assert rs.run_kind == "reprice" and rs.reprice_preview is False


# --------------------------------------------------------------------------- #
# The write rules
# --------------------------------------------------------------------------- #
def test_tier_a_writes_both_prices():
    d = reprice.decide(lookup_result())
    assert (d["price"], d["retail"], d["status"], d["outcome"]) == \
        (367.99, 256.28, "VERIFIED", "REPRICED")


def test_tier_b_holds_the_price_but_writes_a_proven_retail():
    d = reprice.decide(lookup_result(tier="B", tier_reason="only 1 listing"))
    assert d["price"] is None and d["retail"] == 256.28
    assert (d["status"], d["outcome"]) == ("HELD_FOR_HUMAN", "REPRICE_TIER_B")


def test_tier_c_writes_nothing():
    d = reprice.decide(lookup_result(tier="C", tier_reason="authenticity or grade doubt"))
    assert d["price"] is None and d["retail"] is None
    assert d["outcome"] == "REPRICE_TIER_C"


def test_an_unproven_retail_is_never_written():
    d = reprice.decide(lookup_result(new_retail=None, retail_verified="none"))
    assert d["retail"] is None and d["price"] == 367.99
    assert "not proven" in d["reason"]


def test_no_listings_is_held():
    d = reprice.decide(lookup_result(new_price=None, tier="", new_retail=None))
    assert (d["status"], d["outcome"]) == ("HELD_FOR_HUMAN", "REPRICE_NO_LISTINGS")


def test_same_prices_are_unchanged_not_rewritten():
    d = reprice.decide(lookup_result(new_price=223.99, new_retail=44.99))
    assert d["price"] is None and d["retail"] is None and d["outcome"] == "REPRICE_UNCHANGED"


# --------------------------------------------------------------------------- #
# One product, through the runner
# --------------------------------------------------------------------------- #
@pytest.fixture
def wired(monkeypatch):
    seen: dict[str, Any] = {"finish": [], "writes": [], "repair": 0}

    class _Cur:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        def cursor(self, *a, **k): return _Cur()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(reprice.db, "connection", lambda **kw: _Conn())
    monkeypatch.setattr(reprice.db, "assert_tenant", lambda *a, **k: True)
    monkeypatch.setattr(reprice.db, "dsn", lambda: "postgresql://stub")
    monkeypatch.setattr(reprice.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(reprice.product_audit, "load", lambda dsn, pid: {"record": {}, "media": []})
    monkeypatch.setattr(reprice.RL, "item_from_payload", lambda payload: {"sku": "KLE-000346"})
    seen["result"] = lookup_result()
    monkeypatch.setattr(reprice.RL, "lookup", lambda item: seen["result"])
    monkeypatch.setattr(reprice, "_write", lambda pid, price, retail: (
        seen["writes"].append((price, retail)) or {"ok": True, "reason": "changed"}))
    monkeypatch.setattr(runner, "_finish", lambda row, verdict, **kw: seen["finish"].append((verdict, kw)))

    def no_repair():
        seen["repair"] += 1
        raise AssertionError("a reprice run must never load the repair chain")

    monkeypatch.setattr(runner, "_load_repair", no_repair)
    return seen


def test_a_reprice_run_never_enters_the_repair_chain(wired):
    runner.verify_one(ROW, snap(preview=False))
    assert wired["repair"] == 0
    assert wired["writes"] == [(367.99, 256.28)]
    verdict, kw = wired["finish"][0]
    assert verdict.outcome == "REPRICED" and verdict.status == "VERIFIED"
    assert kw["touch_product"] is False             # the last verification verdict stays
    assert kw["deltas"]["reprice"]["after"] == 367.99


def test_a_preview_writes_nothing_and_says_what_it_would(wired):
    runner.verify_one(ROW, snap(preview=True))
    assert wired["writes"] == []
    verdict, kw = wired["finish"][0]
    assert verdict.outcome == "REPRICE_PREVIEW" and verdict.reason.startswith("Preview:")
    assert kw["deltas"]["reprice"]["wouldWrite"] == {"price": 367.99, "retail": 256.28}


def test_a_failed_write_is_a_failure_not_a_success(wired, monkeypatch):
    monkeypatch.setattr(reprice, "_write", lambda *a: {"ok": False, "reason": "FAILED: boom"})
    runner.verify_one(ROW, snap(preview=False))
    verdict, _ = wired["finish"][0]
    assert (verdict.status, verdict.outcome) == ("FAILED", "REPRICE_WRITE_FAILED")


def test_a_rate_limited_lookup_is_retried_not_failed(wired, monkeypatch):
    wired["result"] = {"error": "search: HTTP 429 (rate limited)"}
    released: list[str] = []
    monkeypatch.setattr(reprice.claim, "release_lease", lambda rid, backoff_seconds: released.append(rid))
    runner.verify_one(ROW, snap(preview=False))
    assert released == ["row-1"] and wired["finish"] == []


def test_an_older_vnyx_api_is_told_to_deploy(monkeypatch):
    monkeypatch.setattr(rp, "remote_has_step", lambda script: script != "set-resale-price.ts")
    w = reprice._write(PID, 367.99, None)
    assert w["ok"] is False and "deploy vnyx-api" in w["reason"]


def test_run_remote_sends_the_two_prices_as_typed_options(monkeypatch):
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
    monkeypatch.setattr(rp, "_REMOTE_ASYNC", False, raising=False)
    rp.run_remote("set-resale-price.ts",
                  ["--product", PID, "--apply", "--price", "367.99", "--retail-price", "256.28"],
                  timeout_s=10, quiet=True)
    assert sent["step"] == "reprice" and sent["apply"] is True
    assert sent["options"]["price"] == "367.99" and sent["options"]["retailPrice"] == "256.28"


# --------------------------------------------------------------------------- #
# The Brain's "Reprice by brand tier" in a normal verification (16 Oct 2026)
# --------------------------------------------------------------------------- #
def verify_snap(tiers: list[str], mode: str = "LIVE") -> cfgmod.RunSettings:
    return cfgmod.settings_from_snapshot(
        {"mode": mode, "brain": {"compiled": {"chain": {"repriceTiers": tiers}}}})


@pytest.fixture
def chain(wired, monkeypatch):
    wired["tier"] = {"brand": "Gucci", "tier": "LUXURY"}
    wired["recent"] = False
    monkeypatch.setattr(reprice, "brand_tier", lambda pid: wired["tier"])
    monkeypatch.setattr(reprice, "recently_looked_up", lambda pid, days=30: wired["recent"])
    return wired


def test_older_snapshots_reprice_nothing():
    assert cfgmod.settings_from_snapshot({"mode": "LIVE"}).reprice_tiers == []
    assert verify_snap(["LUXURY", "PREMIUM"]).reprice_tiers == ["LUXURY", "PREMIUM"]


def test_a_ticked_tier_is_looked_up_and_written_in_live(chain):
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert chain["writes"] == [(367.99, 256.28)]
    assert report["preview"] is False and report["brandTier"] == "LUXURY"
    assert [s["step"] for s in steps] == ["lookup", "reprice"]


def test_a_shadow_run_previews(chain):
    _, report = reprice.in_chain(ROW, verify_snap(["LUXURY"], mode="SHADOW"))
    assert chain["writes"] == [] and report["preview"] is True
    assert report["wouldWrite"] == {"price": 367.99, "retail": 256.28}


def test_an_unticked_tier_is_not_looked_up(chain, monkeypatch):
    monkeypatch.setattr(reprice.RL, "lookup", lambda item: pytest.fail("no lookup"))
    chain["tier"] = {"brand": "Zara", "tier": "FAST_FASHION"}
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert report is None and steps[0]["ran"] is False and "Zara" in steps[0]["why"]


def test_a_recent_lookup_is_not_repeated(chain, monkeypatch):
    monkeypatch.setattr(reprice.RL, "lookup", lambda item: pytest.fail("no lookup"))
    chain["recent"] = True
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert report is None and "30 days" in steps[0]["why"]


def test_no_answer_from_vnyx_api_skips_it(chain):
    chain["tier"] = None
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert report is None and steps[0]["ran"] is False


def test_tier_b_writes_no_selling_price_and_does_not_hold(chain):
    chain["result"] = lookup_result(tier="B", retail_verified=None, new_retail=None,
                                    retail_new_eur=None)
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert chain["writes"] == []
    assert report["outcome"] == "REPRICE_TIER_B"
    assert [s["step"] for s in steps] == ["lookup", "reprice"]
    assert "selling price left as it was" in steps[-1]["why"]


def test_a_failed_lookup_is_a_note_not_a_failure(chain):
    chain["result"] = {"error": "HTTP 503 from search"}
    steps, report = reprice.in_chain(ROW, verify_snap(["LUXURY"]))
    assert steps[0]["ok"] is False and "price left as it was" in steps[0]["note"]
    assert report["error"]
