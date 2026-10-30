"""Shared test plumbing.

THE VISION CACHE IS OFF FOR EVERY TEST unless a test turns it on. It is on by
default in policy, and a test that calls the gate twice with the same fake URL
expecting two different fake answers would otherwise get the first one back
from `.cache/vision` in the repository — a green suite on one machine and a red
one on the next. `tests/test_vision_cache.py` opts back in per test with its
own temporary directory.
"""
from __future__ import annotations

import pytest

from app.llm import cache as vision_cache


@pytest.fixture(autouse=True)
def _no_cached_step_runner(monkeypatch):
    """THE STEP RUNNER'S PING IS NOT CARRIED FROM ONE TEST TO THE NEXT.

    repair_product caches what vnyx-api's /ping said (`_REMOTE_OPTIONS`,
    `_REMOTE_ASYNC`). On a machine where a real vnyx-api is running, an early
    test that pings it leaves `asyncSteps: true` cached, and every later
    `run_remote` test then takes the polling path it never set up. Each test
    starts with nothing cached; a test that wants a state sets it itself.
    """
    try:
        from scripts import repair_product as rp
    except Exception:  # noqa: BLE001 — a suite without the scripts package
        yield
        return
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", None, raising=False)
    monkeypatch.setattr(rp, "_REMOTE_ASYNC", None, raising=False)
    yield


@pytest.fixture
def paid_cutout_methods():
    """The paid cut-out fallbacks as they were before 10 Oct 2026.

    The shipped policy no longer asks Gemini's or gpt-image's background removal
    (the user's call — they failed every time on the Klekt runs). The machinery is
    still there and comes back by restoring these lists, so the tests that pin how
    it behaves run with them restored.
    """
    from app.config import policy_overlay

    with policy_overlay({
        "imagery": {"cutout": {
            "strategies": ["object-isnet", "hanger-isnet", "cloth-seg-ft",
                           "cloth-seg-ft-backup", "gemini-paint", "openai-paint"],
            "url_strategies": ["object-isnet", "hanger-isnet", "cloth-seg-ft",
                               "cloth-seg-ft-backup", "gemini-paint", "openai-paint"],
            "hanger": {"then": ["gemini-paint"]},
            "object": {"then": ["gemini-paint", "openai-paint"]},
        }},
        "readiness": {"cutouts": {
            "recut_strategies": ["object-isnet", "gemini-paint", "openai-paint"],
        }},
    }):
        yield


def prices_writable(pol: dict) -> dict:
    """`pol` with the gate allowed to write prices again.

    Since 10 Oct 2026 the shipped policy lists `price` and `retail_price` in
    `guardrails.escalate_only_fields` (the user's call: the agent changes no price
    beyond the .99 rounding). The pricing machinery is unchanged and comes back by
    taking them out, so the tests that pin it run that way.
    """
    g = pol["guardrails"]
    keep = [f for f in g["escalate_only_fields"] if f not in ("price", "retail_price")]
    return {**pol, "guardrails": {**g, "escalate_only_fields": keep}}


@pytest.fixture
def gate_writes_prices():
    """`prices_writable` for code that reads the policy itself (pipeline.reconcile)."""
    from app.config import policy, policy_overlay

    with policy_overlay({"guardrails": {
            "escalate_only_fields": prices_writable(policy())["guardrails"]["escalate_only_fields"]}}):
        yield


@pytest.fixture(autouse=True)
def _no_chart_image_reads(monkeypatch):
    """NO TEST READS A SIZE-CHART IMAGE unless it says what the image holds.

    The EU-size planner reads the chart picture when the chart data lacks the
    size (app/llm/size_chart.py). With a real GEMINI_API_KEY in the
    environment that would be a paid call per fixture URL; tests that cover it
    patch `read_chart` themselves.
    """
    from app.llm import size_chart

    size_chart.reset()
    monkeypatch.setattr(size_chart, "read_chart", lambda url, pol=None: None)
    yield
    size_chart.reset()


@pytest.fixture(autouse=True)
def _vision_cache_off(monkeypatch):
    monkeypatch.setenv("HERMES_VISION_CACHE", "0")
    monkeypatch.delenv("HERMES_VISION_CACHE_REDIS", raising=False)
    vision_cache.reset()
    yield
    vision_cache.reset()


@pytest.fixture(autouse=True)
def _no_live_status_read(monkeypatch):
    """repair()'s approve step reads the product's VNYX status fresh (25 Oct 2026,
    ARCHIVED_UNTOUCHED). Tests drive repair() against a fake DSN, so the read is
    answered here — "Draft", the ordinary case — and a test that needs Archived
    sets it itself."""
    try:
        from scripts import repair_product as rp
    except Exception:  # noqa: BLE001 — a test that never imports it is unaffected
        return
    monkeypatch.setattr(rp, "vnyx_status", lambda dsn, pid: "Draft")
    monkeypatch.setattr(rp, "vnyx_sellable", lambda dsn, pid: 0)


@pytest.fixture(autouse=True)
def _no_site_network(monkeypatch):
    """The readable-site search and page reads (app/pricing/site_pages.py)
    never reach the network in a test; a test that wants them stubs its own."""
    from app.pricing import site_pages as SP

    monkeypatch.setattr(SP, "fetch", lambda url, timeout_s=20.0: None)
    monkeypatch.setattr(SP, "direct_search", lambda queries, cfg: {"comps": [], "calls": 0, "notes": []})
