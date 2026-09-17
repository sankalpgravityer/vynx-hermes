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
def _vision_cache_off(monkeypatch):
    monkeypatch.setenv("HERMES_VISION_CACHE", "0")
    monkeypatch.delenv("HERMES_VISION_CACHE_REDIS", raising=False)
    vision_cache.reset()
    yield
    vision_cache.reset()
