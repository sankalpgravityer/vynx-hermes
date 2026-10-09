"""One garment, one unit, before the agent sends a product to Shopify (9 Oct 2026).

The make-live sheet brought stock above 1 back to 1 for the products it put live;
the agent's own pushes — an approval, a sync of its repairs, a Shopify draft made
live — did not. The chain's `stock` step (vnyx-api settle-stock.ts) runs after
approve and before sync and the runner's go-live. Pinned here:

  * it runs only in the agent's runs, and only when this run sends the product
    to Shopify — published by its approval, or on Shopify with sync or go-live on;
  * it asks for the sheet's --close-stale-loose;
  * its own stock write does not make the sync step re-push the whole product;
  * a failure is a warning on the step, never the end of the chain;
  * the flag travels to a remote vnyx-api as a typed option.

No network, no database, no subprocess.
"""
from __future__ import annotations

from typing import Any

import pytest

from scripts import repair_product as rp
from tests.test_brain_checks import DSN, PID, _repair, step, wired  # noqa: F401

SETTLED = {
    "outcome": "settled",
    "reason": "stock LPN 2 · bin 2 · inventory 2 · product 1 → LPN 1 · bin 1 · "
              "inventory 1 · product 1: LPN SPCR-1 line 2 → 1 — Shopify stock pushed",
    "stockBefore": "LPN 2 · bin 2 · inventory 2 · product 1",
    "stockAfter": "LPN 1 · bin 1 · inventory 1 · product 1",
    "changes": ["LPN SPCR-1 line 2 → 1"],
    "refused": [],
    "shopifyStock": "pushed",
}


@pytest.fixture
def stock(wired, monkeypatch):  # noqa: F811
    answer: dict[str, Any] = {"res": dict(SETTLED), "writes": None}
    chained = rp.run_step  # the `wired` fake

    def fake(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "settle-stock.ts":
            wired["calls"]["scripts"].append((script, list(args)))
            if answer["writes"]:
                # The step's own write moves Product.updatedAt.
                wired["stamp"]["after"] = answer["writes"]
            return answer["res"]["outcome"] != "failed", "", answer["res"]
        return chained(vnyx_api, script, args, timeout_s=timeout_s, quiet=quiet,
                       results_name=results_name)

    monkeypatch.setattr(rp, "run_step", fake)
    monkeypatch.setattr(rp, "remote_has_step", lambda script: True)
    monkeypatch.setattr(rp, "remote_supports", lambda *o: True)
    return {**wired, "answer": answer}


def ran(stock_fixture) -> list[list[str]]:
    return [a for s, a in stock_fixture["calls"]["scripts"] if s == "settle-stock.ts"]


def test_a_cli_run_leaves_stock_alone(stock):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    r = _repair(sync_changes=True)
    assert step(r, "stock")["ran"] is False
    assert "only the Auto Approval agent's runs" in step(r, "stock")["why"]
    assert not ran(stock)


def test_a_live_product_is_settled_before_the_sync(stock):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    r = _repair(sync_changes=True, stock_to_one=True)
    assert step(r, "stock")["ran"] and step(r, "stock")["ok"]
    assert ran(stock) == [["--db", DSN, "--product", PID, "--apply", "--close-stale-loose"]]
    assert "LPN SPCR-1 line 2 → 1" in step(r, "stock")["note"]
    assert r["stock"]["changes"] == ["LPN SPCR-1 line 2 → 1"]
    assert r["stock"]["shopify"] == "pushed"
    names = [s["step"] for s in r["steps"]]
    assert names.index("approve") < names.index("stock") < names.index("sync")


def test_the_go_live_switch_alone_is_enough(stock):
    """The runner makes the draft live after the chain — the count must be right first."""
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    r = _repair(stock_to_one=True, activate_drafts=True)
    assert step(r, "stock")["ran"] is True


def test_nothing_is_settled_when_the_run_sends_nothing_to_shopify(stock):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    r = _repair(stock_to_one=True)
    assert step(r, "stock")["ran"] is False
    assert "sends nothing to Shopify" in step(r, "stock")["why"]


def test_a_product_not_on_shopify_and_not_published_is_left_alone(stock):
    r = _repair(stock_to_one=True, sync_changes=True, activate_drafts=True)
    assert step(r, "stock")["ran"] is False
    assert "not on Shopify" in step(r, "stock")["why"]


def test_a_product_the_approval_just_published_is_settled(stock):
    """After approve: its default-bin placement is where a second unit comes from."""
    stock["approve"].update({"outcome": "approved", "stageBefore": "REVIEW",
                             "stageAfter": "APPROVED"})
    r = _repair(stock_to_one=True, approve=True)
    assert step(r, "stock")["ran"] is True


def test_an_approval_without_the_push_does_not_settle(stock):
    stock["approve"].update({"outcome": "approved", "published": False,
                             "stageBefore": "REVIEW", "stageAfter": "APPROVED"})
    r = _repair(stock_to_one=True, approve=True, publish=False)
    assert step(r, "stock")["ran"] is False


def test_the_steps_own_write_does_not_re_push_the_product(stock):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    stock["answer"]["writes"] = "2026-10-09T13:00:00"
    r = _repair(stock_to_one=True, sync_changes=True)
    assert step(r, "stock")["ran"] is True
    assert step(r, "sync")["ran"] is False
    assert "nothing was repaired" in step(r, "sync")["why"]


def test_a_failure_is_a_warning_and_the_chain_goes_on(stock):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    stock["answer"]["res"] = {"outcome": "failed", "reason": "could not read the Shopify status: 503",
                              "changes": [], "refused": []}
    r = _repair(stock_to_one=True, sync_changes=True)
    assert step(r, "stock")["ok"] is False
    assert "503" in step(r, "stock")["note"]
    assert any(s["step"] == "sync" for s in r["steps"])


def test_an_older_vnyx_api_is_told_to_deploy(stock, monkeypatch):
    stock["shopify"]["id"] = "gid://shopify/Product/1"
    monkeypatch.setattr(rp, "remote_has_step", lambda script: script != "settle-stock.ts")
    r = _repair(stock_to_one=True, sync_changes=True)
    assert "deploy vnyx-api" in step(r, "stock")["why"]
    assert not ran(stock)


def test_run_remote_sends_close_stale_loose_as_a_typed_option(monkeypatch):
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
    rp.run_remote("settle-stock.ts",
                  ["--db", DSN, "--product", PID, "--apply", "--close-stale-loose"],
                  timeout_s=10, quiet=True)
    assert sent["step"] == "stock"
    assert sent["apply"] is True
    assert sent["options"] == {"closeStaleLoose": True}
