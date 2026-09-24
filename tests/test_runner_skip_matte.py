"""`--no-matte` has to survive the trip from the command line to repair().

repair() has always taken `skip_matte`; until 21 Sep 2026 nothing passed it, so
the flag existed and did nothing. That is the failure this file guards: a
default-False pass-through that silently stops being forwarded looks exactly
like a working one from the outside — the run simply mattes every product and
takes an extra 3½ minutes an image without saying why.

Nothing here touches a database or a model. The stub repair() records the
keyword arguments and then fails; `_finish` is stubbed so the verdict write
that follows is not part of the test. Raising something exotic to escape
earlier does not work and should not: the handler catches BaseException on
purpose, because Celery's `SoftTimeLimitExceeded` is one and a timeout must not
strand a claimed row.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.auto_approval import config as cfgmod  # noqa: E402
from app.services.auto_approval import runner  # noqa: E402

ROW = {
    "id": "11111111-1111-4111-8111-111111111111",
    "runId": "22222222-2222-4222-8222-222222222222",
    "tenantId": "33333333-3333-4333-8333-333333333333",
    "productId": "44444444-4444-4444-8444-444444444444",
    "productSku": "KLE-000001",
    "productTitle": "Vintage Nike Blue Sports T-shirt Men S",
    # Below maxAttempts, so the stub failure takes the retry path and returns
    # rather than writing a terminal verdict.
    "attempts": 0,
    "maxAttempts": 3,
}


@pytest.fixture
def captured(monkeypatch):
    """verify_one, with everything but the repair call stubbed out."""
    seen: dict[str, object] = {}

    def fake_repair(dsn, product_id, **kw):
        seen.update(kw)
        raise RuntimeError("stub: stop after the call under test")

    class _Cur:
        def execute(self, *a, **k): return None
        def fetchone(self): return None
        def fetchall(self): return []
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class _Conn:
        # `row_factory=dict_row` on the real driver; accepted and ignored here.
        def cursor(self, *a, **k): return _Cur()
        def commit(self): return None
        def rollback(self): return None
        def __enter__(self): return self
        def __exit__(self, *a): return False

    # The chain is preceded by guards that read the product; with a stub
    # database every one of them reads "empty" and rejects before repair() is
    # reached. Only the care-label guard stands between the caller and the call
    # under test, so it is answered rather than the whole product faked.
    monkeypatch.setattr(runner, "_has_care_label", lambda pid: True)
    monkeypatch.setattr(runner, "_load_repair", lambda: fake_repair)
    monkeypatch.setattr(runner.db, "connection", lambda **kw: _Conn())
    monkeypatch.setattr(runner.db, "assert_tenant", lambda *a, **k: True)
    monkeypatch.setattr(runner.db, "dsn", lambda: "postgresql://stub")
    monkeypatch.setattr(runner.events, "emit", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_vnyx_api_dir", lambda: Path("."))
    # The stub failure is not one of the retryable names, so verify_one writes a
    # terminal verdict. Nothing here is about that write.
    monkeypatch.setattr(runner, "_finish", lambda *a, **k: None)
    return seen


def _run(seen, **kwargs):
    rs = cfgmod.settings_from_snapshot({"mode": "SHADOW", "shadowWritesRepairs": True})
    runner.verify_one(ROW, rs, ignore_stop=True, **kwargs)
    assert seen, "repair() was never called — a guard returned before the chain"
    return seen


def test_skip_matte_reaches_repair_when_asked(captured):
    assert _run(captured, skip_matte=True)["skip_matte"] is True


def test_the_unattended_agent_still_mattes(captured):
    """The default has to stay False: the Celery path passes nothing, and a
    catalogue-wide run that quietly stopped re-cutting would leave every
    CUTOUT_DEFECT unrepaired with no flag to explain it."""
    assert _run(captured)["skip_matte"] is False


def test_price_rounding_only_reaches_repair(captured):
    """The .99-only price mode. Same shape as skip_matte and the same failure
    if it stops being forwarded: the run checks the grade window after all and
    nothing says so. Forwarded as the Brain's price_mode since the checks."""
    assert _run(captured, price_rounding_only=True)["price_mode"] == "rounding_only"


def test_the_window_check_is_the_default(captured):
    """The full step by default, so the unattended agent keeps checking that a
    price sits inside its grade's window — the check the flag switches OFF."""
    assert _run(captured)["price_mode"] == "full"


def test_the_batch_flag_narrows_the_brain_and_never_widens_it(captured):
    """Rounding-only on a tenant whose Brain has rounding OFF is nothing at all,
    not the rounding the tenant switched off."""
    rs = cfgmod.settings_from_snapshot({
        "mode": "SHADOW", "shadowWritesRepairs": True,
        "brain": {"compiled": {"chain": {"priceMode": "window_only"}}},
    })
    runner.verify_one(ROW, rs, ignore_stop=True, price_rounding_only=True)
    assert captured["price_mode"] == "off"


def test_the_brain_chain_switches_reach_repair(captured, monkeypatch):
    """matte / gate / publish / sync from the snapshot, and the policy overlay
    in force WHILE repair() runs — the only place it may apply."""
    from app.config import policy

    seen_policy: dict[str, object] = {}

    def fake_repair(dsn, product_id, **kw):
        captured.update(kw)
        seen_policy["block_on"] = policy()["quality_gate"]["block_on"]
        raise RuntimeError("stub")

    monkeypatch.setattr(runner, "_load_repair", lambda: fake_repair)
    rs = cfgmod.settings_from_snapshot({
        "mode": "SHADOW", "shadowWritesRepairs": True,
        "brain": {"compiled": {
            "policy": {"quality_gate": {"block_on": ["no_model"]}},
            "chain": {"matte": False, "gate": False, "publishOnApprove": False,
                      "syncChanges": True},
        }},
    })
    runner.verify_one(ROW, rs, ignore_stop=True)
    assert captured["skip_matte"] is True and captured["skip_gate"] is True
    assert captured["publish"] is False and captured["sync_changes"] is True
    assert seen_policy["block_on"] == ["no_model"]
    assert policy()["quality_gate"]["block_on"] != ["no_model"]   # unwound


def test_the_other_per_invocation_flags_are_unaffected(captured):
    """skip_matte is a decision about one batch, like allow_stage — it must not
    disturb what the run's frozen snapshot decides."""
    seen = _run(captured, skip_matte=True)
    assert seen["apply"] is True            # shadowWritesRepairs
    assert seen["skip_render"] is False     # from the snapshot, not the flag
    assert seen["approve"] is False         # SHADOW never approves
