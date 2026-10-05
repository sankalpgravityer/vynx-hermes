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


@pytest.fixture(autouse=True)
def _vision_cache_off(monkeypatch):
    monkeypatch.setenv("HERMES_VISION_CACHE", "0")
    monkeypatch.delenv("HERMES_VISION_CACHE_REDIS", raising=False)
    vision_cache.reset()
    yield
    vision_cache.reset()
