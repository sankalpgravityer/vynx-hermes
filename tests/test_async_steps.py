"""Background steps: start, poll, finish — so no request outlives the proxy's 60 s.

30 Sep 2026: BOA-006356/357/390/391 failed their matte with "vnyx-api returned
504" after ~62 s, the proxy's limit, while the step itself ran on. With
`asyncSteps` advertised by the ping, run_remote starts the step and polls it.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import repair_product as rp  # noqa: E402

ARGS = ["--db", "d", "--product", "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c", "--apply", "--replace"]


class Resp:
    def __init__(self, status: int, body: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status
        self._body = body or {}
        self.text = text or str(body)

    def json(self):
        return self._body


@pytest.fixture
def remote(monkeypatch):
    import httpx

    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", {"replace", "keepBetter"}, raising=False)
    monkeypatch.setattr(rp, "_REMOTE_ASYNC", True, raising=False)
    monkeypatch.setattr(rp, "STEP_POLL_S", 0)
    calls: dict[str, Any] = {"posts": [], "polls": 0}
    polls: list[Resp] = []

    def post(url, json, headers, timeout):
        calls["posts"].append(json)
        return Resp(202, {"jobId": "job-1", "state": "running"})

    def get(url, headers, timeout):
        calls["polls"] += 1
        return polls.pop(0) if polls else Resp(200, {"state": "running"})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return calls, polls


def test_a_long_step_is_started_then_polled_to_its_result(remote):
    calls, polls = remote
    polls.extend([Resp(200, {"state": "running"}), Resp(200, {"state": "running"}),
                  Resp(200, {"state": "done", "ok": True, "output": "cut-outs written : 1",
                             "results": {"x": 1}})])
    ok, out, results = rp.run_remote("backfill-bg-removal.ts", ARGS, timeout_s=600, quiet=True)
    assert ok is True and "written" in out and results == {"x": 1}
    assert calls["posts"][0]["async"] is True and calls["posts"][0]["step"] == "matte"
    assert calls["polls"] == 3


def test_a_504_on_a_poll_is_ridden_out(remote):
    """The proxy that killed the long request can still hiccup on a short one."""
    calls, polls = remote
    polls.extend([Resp(504, text="<html>gateway timeout</html>"), Resp(502, text="bad gateway"),
                  Resp(200, {"state": "done", "ok": True, "output": ""})])
    ok, _out, _r = rp.run_remote("backfill-bg-removal.ts", ARGS, timeout_s=600, quiet=True)
    assert ok is True and calls["polls"] == 3


def test_a_job_that_vanished_is_a_clear_failure(remote):
    _calls, polls = remote
    polls.append(Resp(404, {"error": "no such job"}))
    with pytest.raises(rp.StepFailed, match="gone"):
        rp.run_remote("backfill-bg-removal.ts", ARGS, timeout_s=600, quiet=True)


def test_a_step_that_threw_is_reported_with_its_error(remote):
    _calls, polls = remote
    polls.append(Resp(200, {"state": "error", "error": "spawn npx ENOENT"}))
    with pytest.raises(rp.StepFailed, match="ENOENT"):
        rp.run_remote("backfill-bg-removal.ts", ARGS, timeout_s=600, quiet=True)


def test_a_server_side_timeout_still_reads_as_a_timeout(remote):
    _calls, polls = remote
    polls.append(Resp(200, {"state": "done", "ok": False, "timedOut": True, "output": "..."}))
    with pytest.raises(rp.StepFailed, match="timed out"):
        rp.run_remote("backfill-imagery.ts", ARGS, timeout_s=1800, quiet=True)


def test_an_older_vnyx_api_gets_the_synchronous_call(monkeypatch):
    """No `asyncSteps` in the ping: exactly the old single request."""
    import httpx

    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", {"replace"}, raising=False)
    monkeypatch.setattr(rp, "_REMOTE_ASYNC", False, raising=False)
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(httpx, "post", lambda url, json, headers, timeout: (
        sent.append(json) or Resp(200, {"ok": True, "output": "", "results": None})))
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no polling on an old server"))
    ok, _o, _r = rp.run_remote("backfill-bg-removal.ts", ARGS, timeout_s=600, quiet=True)
    assert ok is True and "async" not in sent[0]


def test_the_ping_decides(monkeypatch):
    """`asyncSteps: true` switches it on; its absence or a failed ping does not."""
    import httpx

    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    for body, expected in (({"options": ["replace"], "asyncSteps": True}, True),
                           ({"options": ["replace"]}, False)):
        monkeypatch.setattr(rp, "_REMOTE_OPTIONS", None, raising=False)
        monkeypatch.setattr(rp, "_REMOTE_ASYNC", None, raising=False)
        monkeypatch.setattr(httpx, "get", lambda url, headers, timeout, b=body: Resp(200, b))
        assert rp.remote_async() is expected
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", None, raising=False)
    monkeypatch.setattr(rp, "_REMOTE_ASYNC", None, raising=False)

    def boom(*a, **k):
        raise httpx.ConnectError("down")
    monkeypatch.setattr(httpx, "get", boom)
    assert rp.remote_async() is False
