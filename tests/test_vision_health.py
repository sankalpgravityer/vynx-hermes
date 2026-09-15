"""app/llm/health.py — the outage guard's arithmetic, cold.

The incident this guards against: a quota wall makes every care-label read
fail, every product then "has no brand or size", and the post-chain default
rejects the queue. These tests pin the three properties that stop that:

  * only PROVIDER failures count — a bad answer is not an outage;
  * the verdict needs a minimum of evidence and a majority of failures;
  * the window slides and the pause clears, so a long-lived worker recovers.
"""
from __future__ import annotations

import time

import httpx
import pytest

from app.llm import health

POL = {"llm": {"health": {"window": 10, "min_samples": 6,
                          "error_ratio": 0.7, "cooldown_s": 60}}}


@pytest.fixture(autouse=True)
def _clean():
    health.reset()
    yield
    health.reset()


def _provider_down() -> Exception:
    return httpx.ConnectTimeout("connect timed out")


def test_fewer_than_min_samples_never_trips():
    for _ in range(5):
        health.record_error(_provider_down(), pol=POL)
    health.check(POL)  # no raise: five failures is not yet evidence


def test_mostly_errors_trips_and_then_stays_paused():
    for _ in range(6):
        health.record_error(_provider_down(), pol=POL)
    with pytest.raises(health.VisionOutage) as first:
        health.check(POL)
    assert "6 of the last 6" in str(first.value)
    # Every check during the cooldown raises too — the pump must not resume.
    with pytest.raises(health.VisionOutage) as again:
        health.check(POL)
    assert "paused" in str(again.value)
    assert health.snapshot()["paused_for_s"] > 0


def test_bad_answers_are_not_outages():
    """A model that answers unusably is a live provider. Ten of those in a row
    must not stop the run — that is a batch of hard labels, not an outage."""
    for _ in range(10):
        assert health.record_error(ValueError("bad json"), pol=POL) is False
    health.check(POL)
    assert health.snapshot()["window"] == 0


def test_a_working_fallback_dilutes_the_ratio():
    """One Gemini error and one OpenAI success per label is 50%, under 70%."""
    for _ in range(4):
        health.record_error(_provider_down(), "gemini", pol=POL)
        health.record_ok("openai", pol=POL)
    health.check(POL)


def test_the_window_slides():
    for _ in range(10):
        health.record_error(_provider_down(), pol=POL)
    for _ in range(10):
        health.record_ok(pol=POL)
    health.check(POL)  # the window is now entirely successes
    assert health.snapshot()["errors"] == 0


def test_cooldown_over_clears_the_window(monkeypatch):
    for _ in range(6):
        health.record_error(_provider_down(), pol=POL)
    with pytest.raises(health.VisionOutage):
        health.check(POL)
    real = time.monotonic
    monkeypatch.setattr(health.time, "monotonic", lambda: real() + 61)
    health.check(POL)  # no raise: the pause is over and the failures forgotten
    assert health.snapshot()["window"] == 0
    assert health.snapshot()["paused_for_s"] == 0


def test_genai_api_errors_count():
    errors = pytest.importorskip("google.genai.errors")
    quota = errors.APIError(429, {"error": {"message": "quota exceeded"}})
    assert health.is_api_error(quota)
    assert health.record_error(quota, pol=POL) is True
    assert health.snapshot()["last_error"].startswith("APIError")


def test_transport_errors_count_and_parse_errors_do_not():
    assert health.is_api_error(httpx.ReadTimeout("slow"))
    assert health.is_api_error(httpx.ConnectError("refused"))
    assert health.is_api_error(ConnectionError())
    assert not health.is_api_error(ValueError("x"))
    assert not health.is_api_error(KeyError("candidates"))


def test_defaults_apply_when_policy_has_no_health_block():
    cfg = health.config({"llm": {}})
    assert cfg == health.DEFAULTS
    cfg = health.config({"llm": {"health": {"min_samples": 3}}})
    assert cfg["min_samples"] == 3 and cfg["window"] == health.DEFAULTS["window"]
