"""outcome.classify — the two outcomes the image gate adds.

repair() replaces `would_approve` with `gate_blocked` / `gate_unavailable`
when the gate spoke. These pin what the row records for each, and that a
gate that passed changes nothing about the existing verdicts.
"""
from __future__ import annotations

from app.services.auto_approval.outcome import classify


def _result(outcome: str, blockers=(), gate_code=None, **over):
    return {
        "approval": {"outcome": outcome, "blockers": list(blockers),
                     "gate_code": gate_code},
        "remaining": [],
        "verified": True,
        **over,
    }


def test_gate_blocked_is_held_under_the_gate_code():
    v = classify(_result("gate_blocked",
                         ["image gate: NO MODEL — the lead render shows no person"],
                         gate_code="IMAGE_QUALITY"))
    assert v.status == "HELD_FOR_HUMAN"
    assert v.outcome == "IMAGE_QUALITY"
    assert "NO MODEL" in v.reason
    assert v.approved is False and v.retryable is False


def test_gender_mismatch_keeps_its_own_code():
    v = classify(_result("gate_blocked", ["image gate: the model presents as women"],
                         gate_code="MODEL_GENDER_MISMATCH"))
    assert v.outcome == "MODEL_GENDER_MISMATCH"


def test_gate_blocked_without_a_code_defaults_to_image_quality():
    v = classify(_result("gate_blocked", ["image gate: BAD FACE"]))
    assert v.outcome == "IMAGE_QUALITY"


def test_gate_unavailable_is_retryable_and_names_the_provider():
    v = classify(_result("gate_unavailable",
                         ["image gate: vision provider failed (429)"]))
    assert v.status == "HELD_FOR_HUMAN"
    assert v.outcome == "VISION_UNAVAILABLE"
    assert v.retryable is True


def test_a_passed_gate_leaves_would_approve_alone():
    v = classify(_result("would_approve"))
    assert v.status == "VERIFIED" and v.outcome == "READY_NOT_MOVED"


def test_a_passed_gate_leaves_approved_alone():
    v = classify(_result("approved", stage_after="APPROVED"))
    assert v.status == "VERIFIED" and v.outcome == "APPROVED" and v.approved
