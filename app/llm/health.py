"""Vision-provider health, and the fail-fast that stops a run during an outage.

WHY THIS EXISTS. Every in-process vision read in the agent degrades politely on
failure: care_label.read() returns "nothing legible", GeminiEvidence returns an
empty audit, the quality gate returns "review". Each is the right answer for ONE
failed call. Across a queue it is the wrong answer for all of them at once: a
rate-limit or quota wall makes every label unreadable, every product then "has
no brand or size", and the post-chain default REJECTS it — a warehouse of sound
products archived because an API key ran out of credit. The auditor this was
ported from (vnyx-auto-approve, `abort_if_gemini_broken`) documents exactly that
incident, and it is the one failure mode of an unattended loop that a human
would never have let run.

So every call is counted here by outcome, and the run loop asks BETWEEN
products whether the provider is still answering. When most recent calls are
hard API errors the loop stops: products already judged keep their verdicts,
the one in hand is retried rather than judged, and nothing further is written
until the cooldown passes.

WHAT COUNTS AS AN ERROR. Only failures of the provider: 429, 5xx, a rejected
key, a timeout, a refused connection. A model that answers "I cannot read
this" is a successful call with an unhelpful answer, and counting it here would
make a batch of illegible labels look like an outage. `is_api_error` draws that
line and both readers route through it.

WINDOWED, not cumulative. The Celery worker lives for days; a lifetime ratio
would need thousands of failures before it noticed anything after a good week.
Only the last `window` calls are consulted.

PROCESS-LOCAL, deliberately. The FastAPI service and the worker each see their
own calls. The worker's are the care-label read and the quality gate, which is
where the damage would be done, and a shared store would add a dependency to a
guard whose whole value is that it cannot itself fail.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger("hermes.vision-health")

# Overridable under `llm.health` in policy.yaml.
DEFAULTS: dict[str, Any] = {
    # How many recent calls are consulted.
    "window": 20,
    # Fewer calls than this and no verdict is reached — three products' worth.
    "min_samples": 6,
    # Fraction of the window that must be API errors before the loop stops.
    "error_ratio": 0.7,
    # How long the loop stays stopped before the window is cleared and the
    # provider gets another chance.
    "cooldown_s": 600,
}


class VisionOutage(RuntimeError):
    """Most recent vision calls are hard API errors. Stop the run."""


_lock = threading.Lock()
_recent: list[bool] = []       # newest last; True = the provider answered
_last_error = ""
_last_provider = ""
_total_ok = 0
_total_err = 0
_paused_until = 0.0


def config(pol: dict[str, Any] | None = None) -> dict[str, Any]:
    """Effective thresholds: policy.yaml over the defaults above."""
    if pol is None:
        try:
            from app.config import policy

            pol = policy()
        except Exception:  # noqa: BLE001 — a missing policy file must not break a call
            pol = {}
    over = ((pol or {}).get("llm") or {}).get("health") or {}
    return {**DEFAULTS, **{k: v for k, v in over.items() if v is not None}}


def is_api_error(exc: BaseException) -> bool:
    """A failure of the PROVIDER, not of the answer.

    google-genai wraps every non-2xx in `APIError` (ClientError for 4xx,
    ServerError for 5xx) — so a bad key, a quota wall and an overloaded backend
    all land here. httpx's `HTTPError` covers the transport: timeouts, refused
    connections, and the OpenAI reader's status errors. A JSON parse failure or
    a schema mismatch is neither: the provider spoke, it just said something
    unusable.
    """
    try:
        from google.genai import errors as genai_errors

        if isinstance(exc, genai_errors.APIError):
            return True
    except ImportError:  # pragma: no cover — SDK optional
        pass
    try:
        import httpx

        if isinstance(exc, httpx.HTTPError):
            return True
    except ImportError:  # pragma: no cover
        pass
    return isinstance(exc, (ConnectionError, TimeoutError))


def _push(ok: bool, pol: dict[str, Any] | None) -> None:
    window = max(1, int(config(pol)["window"]))
    with _lock:
        _recent.append(ok)
        if len(_recent) > window:
            del _recent[: len(_recent) - window]


def record_ok(provider: str = "gemini", pol: dict[str, Any] | None = None) -> None:
    global _total_ok
    _push(True, pol)
    with _lock:
        _total_ok += 1


def record_error(exc: BaseException, provider: str = "gemini",
                 pol: dict[str, Any] | None = None) -> bool:
    """Count `exc` if it is a provider failure. Returns whether it was counted.

    The return value is what lets a caller tell "the provider was down" from
    "the provider answered badly" without re-deriving the classification.
    """
    global _total_err, _last_error, _last_provider
    if not is_api_error(exc):
        return False
    _push(False, pol)
    with _lock:
        _total_err += 1
        _last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        _last_provider = provider
    return True


def snapshot() -> dict[str, Any]:
    """The counters as they stand, for a log line or a preflight report."""
    with _lock:
        n = len(_recent)
        err = _recent.count(False)
        return {
            "window": n,
            "errors": err,
            "ok": n - err,
            "error_ratio": (err / n) if n else 0.0,
            "last_error": _last_error,
            "last_provider": _last_provider,
            "total_ok": _total_ok,
            "total_err": _total_err,
            "paused_for_s": max(0, int(_paused_until - time.monotonic())) if _paused_until else 0,
        }


def check(pol: dict[str, Any] | None = None) -> None:
    """Raise VisionOutage if the provider is down. Call BETWEEN products.

    Never inside one: a product half-way through its chain has already spent
    its renders, and the right place to stop is before the next one starts.

    On the first detection the loop is paused for `cooldown_s`; every check
    during the pause raises too. When the pause ends the window is cleared, so
    the next calls decide afresh instead of re-tripping on the old failures.
    """
    global _paused_until
    cfg = config(pol)
    with _lock:
        if _paused_until:
            now = time.monotonic()
            if now < _paused_until:
                raise VisionOutage(
                    f"vision provider paused for another "
                    f"{int(_paused_until - now)}s after an outage "
                    f"(last error, {_last_provider}: {_last_error})"
                )
            _paused_until = 0.0
            _recent.clear()
            log.info("vision-health cooldown over; window cleared")
            return

        n = len(_recent)
        if n < int(cfg["min_samples"]):
            return
        err = _recent.count(False)
        ratio = err / n
        if ratio < float(cfg["error_ratio"]):
            return
        _paused_until = time.monotonic() + float(cfg["cooldown_s"])
        last_provider, last_error = _last_provider, _last_error

    raise VisionOutage(
        f"the vision provider failed {err} of the last {n} calls ({ratio:.0%}) — "
        f"almost certainly a rate limit, a quota wall or a rejected key, not {n} "
        f"unreadable images. Last error ({last_provider}): {last_error}. "
        f"Nothing further is judged for {int(cfg['cooldown_s'])}s; products "
        f"already finished keep their verdicts and the one in hand is retried."
    )


def reset() -> None:
    """Forget everything. Tests, and a deliberate operator restart."""
    global _last_error, _last_provider, _total_ok, _total_err, _paused_until
    with _lock:
        _recent.clear()
        _last_error = ""
        _last_provider = ""
        _total_ok = 0
        _total_err = 0
        _paused_until = 0.0
