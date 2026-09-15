"""repair() — how the gate, the canary and the label reader feed the verdict.

No network, no database, no subprocess: `needs`, `run_step`, `approve_check`,
`product_audit.audit`, `quality_gate.judge` and `care_label.read` are all
replaced. What is under test is the WIRING in repair() — the branches real data
cannot reliably reach, because a product that is ready by every column check and
badly rendered is rare by construction:

  * a refused gate on a ready product becomes `gate_blocked` and the move is
    withheld even under --apply --approve;
  * a refused gate on a not-ready product rides along in the blockers;
  * a gate that could not run is `gate_unavailable` and lands on
    `vision_unavailable`, as does a care-label read the provider failed;
  * the canary flag is set only for a flip the render step did not cause.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import repair_product as rp  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000001"


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
    """Patch every I/O edge of repair() and hand back the knobs."""
    calls: dict[str, Any] = {"approve": [], "needs": 0}
    states: list[dict[str, Any]] = [_state()]

    def fake_needs(dsn, pid):
        calls["needs"] += 1
        # First call is the pre-chain read; later calls (gate re-read, the
        # closing `after`) take the LAST state so a test can change what the
        # chain "did" to the product.
        return states[0] if calls["needs"] == 1 else states[-1]

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "verify-and-repair.ts":
            return True, "", {"ok": True, "applied": [], "failed": []}
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    approve_outcome: dict[str, Any] = {"outcome": "would_approve", "problems": []}

    def fake_approve_check(vnyx_api, dsn, pid, *, apply, skip_bin, quiet, allow_stage=None):
        calls["approve"].append({"apply": apply})
        return dict(approve_outcome)

    gate_verdict: dict[str, Any] = {"value": GateVerdict("ok")}

    def fake_judge(media, **kw):
        return gate_verdict["value"]

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "approve_check", fake_approve_check)
    monkeypatch.setattr(qg, "judge", fake_judge)
    monkeypatch.setattr(
        rp.product_audit, "audit",
        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}},
    )
    return {"calls": calls, "states": states, "approve": approve_outcome,
            "gate": gate_verdict}


def _repair(apply: bool = False, approve: bool = False) -> dict[str, Any]:
    return rp.repair(DSN, PID, apply=apply, vnyx_api=Path("."), infer=False,
                     min_confidence=70, skip_render=True, approve=approve,
                     skip_bin=True, quiet=True, silent=True)


def test_a_passed_gate_changes_nothing(wired):
    r = _repair()
    assert r["approval"]["outcome"] == "would_approve"
    assert r["approval"]["gate_code"] is None
    assert r["gate"]["action"] == "ok"
    assert r["vision_unavailable"] == []
    gate_step = next(s for s in r["steps"] if s["step"] == "gate")
    assert gate_step["ran"] and gate_step["ok"]


def test_a_refused_gate_on_a_ready_product_withholds_the_move(wired):
    wired["gate"]["value"] = GateVerdict(
        "regen", "MODEL_GENDER_MISMATCH",
        ["the model presents as women but the product is listed as men"])
    r = _repair(apply=True, approve=True)
    # The pre-flight still ran — for the record — but was not allowed to move.
    assert wired["calls"]["approve"] == [{"apply": False}]
    assert r["approval"]["outcome"] == "gate_blocked"
    assert r["approval"]["gate_code"] == "MODEL_GENDER_MISMATCH"
    assert r["approval"]["blockers"] == [
        "image gate: the model presents as women but the product is listed as men"]
    assert r["approval"]["ready"] is False


def test_a_refused_gate_on_a_not_ready_product_rides_along(wired):
    wired["gate"]["value"] = GateVerdict("regen", "IMAGE_QUALITY", ["BAD FACE — corrupted"])
    wired["approve"].update({"outcome": "skipped_preflight", "problems": ["no size"]})
    r = _repair()
    assert r["approval"]["outcome"] == "skipped_preflight"
    assert r["approval"]["blockers"] == ["no size", "image gate: BAD FACE — corrupted"]
    assert r["approval"]["gate_code"] == "IMAGE_QUALITY"


def test_an_unavailable_gate_is_recorded_as_such_and_never_approves(wired):
    wired["gate"]["value"] = GateVerdict(
        "review", "VISION_UNAVAILABLE", ["vision provider failed (429)"], unavailable=True)
    r = _repair(apply=True, approve=True)
    assert wired["calls"]["approve"] == [{"apply": False}]
    assert r["approval"]["outcome"] == "gate_unavailable"
    assert r["vision_unavailable"] == ["gate"]
    gate_step = next(s for s in r["steps"] if s["step"] == "gate")
    assert gate_step["ok"] is False and "vision unavailable" in gate_step["note"]


def test_the_gate_is_skipped_without_a_render(wired):
    wired["gate"]["value"] = GateVerdict("skipped", reasons=["no on-model render to judge"])
    r = _repair()
    assert r["gate"]["action"] == "skipped"
    assert r["approval"]["outcome"] == "would_approve"


def test_the_canary_flags_a_flip_the_render_did_not_cause(wired):
    wired["states"].append(_state(generation_status="GENERATING"))
    r = _repair(apply=True)
    gen = r["generation"]
    assert gen["before"] == "COMPLETE" and gen["after"] == "GENERATING"
    assert gen["render_ran"] is False
    assert gen["regeneration_triggered"] is True


def test_the_canary_stays_quiet_when_nothing_flipped(wired):
    r = _repair(apply=True)
    assert r["generation"]["regeneration_triggered"] is False


def test_a_label_read_the_provider_failed_lands_on_vision_unavailable(wired, monkeypatch):
    from app.llm import care_label

    wired["states"][0] = _state(
        attributes_missing=["size"],
        loaded={
            "record": {"gender": ["men"], "category": "Jeans", "subCategory": "Jeans",
                       "sizingGuide": None, "updatedAt": None},
            "media": [{"url": "https://x/label.jpg", "view": "LABEL", "mediaType": "IMAGE"},
                      {"url": "https://x/f.png", "view": "AI_FRONT", "mediaType": "IMAGE",
                       "position": 1}],
            "catalog": {},
        },
    )
    monkeypatch.setattr(care_label, "read", lambda *a, **k: {
        "tried": ["gemini", "openai"], "unavailable": ["gemini", "openai"],
        "rejected": {}, "provider": None, "api_failed": True,
        "error": "vision providers unavailable — gemini, openai returned API errors",
    })
    r = _repair(apply=True)
    assert "care label" in r["vision_unavailable"]
    label_step = next(s for s in r["steps"] if s["step"] == "care label")
    assert label_step["ok"] is False and "unavailable" in label_step["note"]


def test_a_label_the_provider_simply_could_not_read_is_not_unavailable(wired, monkeypatch):
    from app.llm import care_label

    wired["states"][0] = _state(attributes_missing=["size"])
    monkeypatch.setattr(care_label, "read", lambda *a, **k: {
        "tried": ["gemini", "openai"], "unavailable": [], "rejected": {"size": 40},
        "provider": None,
    })
    r = _repair(apply=True)
    assert r["vision_unavailable"] == []
    label_step = next(s for s in r["steps"] if s["step"] == "care label")
    assert label_step["ok"] is True and "nothing legible" in label_step["note"]
