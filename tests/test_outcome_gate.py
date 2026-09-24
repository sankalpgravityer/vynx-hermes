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


# ---------------------------------------------------------------------------
# The APPROVED backlog (23 Sep 2026)
#
# `run_from_sheet --include-approved` repairs products that are already in the
# Approved tab, so EVERY one of them carries "stage is APPROVED, not REVIEW"
# from the preflight. That line is the premise of the batch, not a finding.
# ---------------------------------------------------------------------------
STAGE = "stage is APPROVED, not REVIEW"


def _classify(blockers, outcome="skipped_preflight", **over):
    from app.services.auto_approval.outcome import classify

    result = {"approval": {"outcome": outcome, "blockers": list(blockers)}}
    result.update(over)
    return classify(result)


def test_already_approved_and_nothing_else_is_verified():
    v = _classify([STAGE])
    assert (v.status, v.outcome) == ("VERIFIED", "ALREADY_APPROVED")


def test_already_approved_with_a_real_problem_is_held_not_cancelled():
    """KLE-000267 and 17 others: the stage line plus a category the image gate
    contradicts. Recorded CANCELLED / LEFT_REVIEW — "the product left the Review
    stage during verification" — which is not what happened, and the finding a
    person needed was lost behind it."""
    problem = ("image gate: the render shows t-shirt (tops) but the product is "
               "filed under 'Joggers' (bottoms)")
    v = _classify([STAGE, problem])
    assert v.status == "HELD_FOR_HUMAN"
    assert v.outcome != "LEFT_REVIEW"
    assert problem in v.reason
    assert STAGE not in v.reason      # the premise is not reported as a cause


def test_a_product_that_really_left_review_is_still_cancelled():
    """The guard this branch exists for: a human REJECTING a product mid-run
    must not have it approved afterwards."""
    v = _classify(["stage is REJECTED, not REVIEW"])
    assert (v.status, v.outcome) == ("CANCELLED", "LEFT_REVIEW")
