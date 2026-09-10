"""Translate repair()'s result into a verification status.

The single most important translation in the feature, and the reason it is its
own module with its own tests: repair() reports on TWO independent rule sets and
they can disagree.

    Hermes' 54 rules      -> result["verified"], result["remaining"]
    approve-products.ts's -> result["approval"]["outcome"] / ["blockers"]
    21-check preflight

A product can pass Hermes and still be refused by the preflight (two of its
checks — front and back background-removal — exist nowhere else), and the
reverse. Both must be reportable, which is why the row carries `blockingRules`
and `preflightProblems` separately rather than one merged list.
"""
from __future__ import annotations

from typing import Any, NamedTuple


class Verdict(NamedTuple):
    status: str            # a VerificationStatus value
    outcome: str           # machine-readable cause
    reason: str            # one human sentence
    approved: bool         # did it actually move REVIEW -> APPROVED
    retryable: bool        # should the row go back on the queue


def _worst_rule(remaining: list[str]) -> str | None:
    """The rule id to record as the cause.

    repair() returns `remaining` already ordered worst-first (product_audit
    sorts findings by severity), so the head is the right answer and re-deriving
    a severity ranking here would be a second, drifting copy of Hermes' order.
    """
    return remaining[0] if remaining else None


def _preflight_code(blockers: list[str]) -> str:
    """A stable code for the first preflight problem.

    The script emits prose ("no EU size", "front not background-removed"), which
    is right for a human and wrong for grouping. This maps the message to a code
    so the Review tab can group by cause without parsing sentences — and falls
    back to a slug rather than dropping an unrecognised one, so a new check
    added to the script still produces something usable here.
    """
    if not blockers:
        return "PREFLIGHT"
    first = blockers[0].strip().lower()
    known = {
        "no brand": "NO_BRAND",
        "no size": "NO_SIZE",
        "no sizing guide": "NO_SIZING_GUIDE",
        "no size chart on the product": "NO_SIZE_CHART",
        "no eu size": "NO_EU_SIZE",
        "no colour": "NO_COLOUR",
        "no title": "NO_TITLE",
        "no description": "NO_DESCRIPTION",
        "no master category": "NO_MASTER_CATEGORY",
        "no category": "NO_CATEGORY",
        "no gender": "NO_GENDER",
        "no selling price": "NO_SELLING_PRICE",
        "no retail price": "NO_RETAIL_PRICE",
        "no care label photograph": "NO_CARE_LABEL",
        "no front photograph": "NO_FRONT_PHOTO",
        "no back photograph": "NO_BACK_PHOTO",
        "front not background-removed": "FRONT_NOT_MATTED",
        "back not background-removed": "BACK_NOT_MATTED",
        "no mannequin selected": "NO_MANNEQUIN",
        "product is deleted": "PRODUCT_DELETED",
    }
    if first in known:
        return known[first]
    for prefix, code in (
        ("missing ai_", "MISSING_RENDER"),
        ("stage is ", "NOT_IN_REVIEW"),
        ("mannequin ", "MANNEQUIN_MISMATCH"),
    ):
        if first.startswith(prefix):
            return code
    slug = "".join(ch if ch.isalnum() else "_" for ch in first).strip("_").upper()
    return f"PREFLIGHT_{slug[:40]}" if slug else "PREFLIGHT"


def classify(result: dict[str, Any]) -> Verdict:
    """repair()'s dict -> the verdict to store.

    Ordered by specificity, not by likelihood: the already-approved case has to
    be tested before the general preflight refusal, because it arrives wearing
    the same `skipped_preflight` label and is NOT a failure.
    """
    approval = result.get("approval") or {}
    outcome = approval.get("outcome")
    blockers: list[str] = list(approval.get("blockers") or [])
    remaining: list[str] = list(result.get("remaining") or [])
    verified = bool(result.get("verified"))

    # ---- the product moved ------------------------------------------------
    if outcome == "approved":
        return Verdict(
            status="VERIFIED",
            outcome="APPROVED",
            reason=(
                f"Approved and moved to "
                f"{approval.get('stage_after') or 'APPROVED'}. "
                f"A Shopify upsert was enqueued."
            ),
            approved=True,
            retryable=False,
        )

    # ---- ready, but the move was not asked for ----------------------------
    if outcome == "would_approve":
        return Verdict(
            status="VERIFIED",
            outcome="READY_NOT_MOVED",
            reason=(
                "Passed every check. Not moved, because the agent is in shadow "
                "mode."
            ),
            approved=False,
            retryable=False,
        )

    # ---- already approved -------------------------------------------------
    #
    # NOT a failure, and reading it as one is worse than cosmetic: on a batch it
    # inflates the not-ready count with products that are finished and sends
    # someone looking for a defect that is not there. The preflight refuses them
    # only because re-approving records no movement, so onApprovedArrival never
    # fires — the product is done. (repair_product.py:695 makes the same call.)
    stage_only = [b for b in blockers if b.startswith("stage is ")]
    if (
        outcome == "skipped_preflight"
        and len(blockers) == len(stage_only) == 1
        and "APPROVED" in stage_only[0]
    ):
        return Verdict(
            status="VERIFIED",
            outcome="ALREADY_APPROVED",
            reason="Already approved — nothing to do.",
            approved=False,
            retryable=False,
        )

    # ---- the product left Review while we worked on it ---------------------
    #
    # The preflight's own stage check catches this, which is the strongest
    # possible guard because it lives in the step that does the publishing: a
    # human rejecting a product mid-verification must not then have it approved.
    if outcome == "skipped_preflight" and stage_only:
        return Verdict(
            status="CANCELLED",
            outcome="LEFT_REVIEW",
            reason=(
                f"The product left the Review stage during verification "
                f"({stage_only[0]}), so it was not approved."
            ),
            approved=False,
            retryable=False,
        )

    if outcome == "not_found":
        return Verdict(
            status="CANCELLED",
            outcome="PRODUCT_DELETED",
            reason="The product no longer exists.",
            approved=False,
            retryable=False,
        )

    if outcome == "failed":
        return Verdict(
            status="FAILED",
            outcome="APPROVE_FAILED",
            reason=("; ".join(blockers) or "The approval step failed.")[:500],
            approved=False,
            retryable=True,
        )

    # ---- held ---------------------------------------------------------------
    #
    # Either Hermes still has blockers no repair could clear, or the preflight
    # refused on a required field. Both are HELD_FOR_HUMAN, but the recorded
    # cause differs, and Hermes' rule wins when both fired: it is the more
    # specific statement about what is wrong.
    rule = _worst_rule(remaining)
    if rule:
        return Verdict(
            status="HELD_FOR_HUMAN",
            outcome=rule,
            reason=(
                "; ".join(remaining[:3])
                if len(remaining) > 1
                else rule
            )[:500],
            approved=False,
            retryable=False,
        )
    if blockers:
        return Verdict(
            status="HELD_FOR_HUMAN",
            outcome=_preflight_code(blockers),
            reason="; ".join(blockers)[:500],
            approved=False,
            retryable=False,
        )

    # ---- verified but the approve step never ran ---------------------------
    #
    # Shadow mode with no approve attempt at all, or --no-approve. `verified`
    # is the only cue that matters, so it is honoured.
    if verified:
        return Verdict(
            status="VERIFIED",
            outcome="READY_NOT_MOVED",
            reason="Passed every check. Not moved.",
            approved=False,
            retryable=False,
        )

    # ---- nothing recognisable ---------------------------------------------
    #
    # Deliberately NOT treated as verified. "We could not tell" must never read
    # as "it passed" — the same rule the gate client applies when it refuses to
    # coerce a malformed `verified`.
    return Verdict(
        status="FAILED",
        outcome=str(outcome or "UNKNOWN").upper(),
        reason=(
            "The verification finished without a recognisable verdict. "
            "This is a defect in the agent, not in the product."
        ),
        approved=False,
        retryable=True,
    )
