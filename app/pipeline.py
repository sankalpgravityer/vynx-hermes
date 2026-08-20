"""Hermes pipeline.

    fetch → detect → gather evidence → resolve → RE-VERIFY → gate → write

The re-verify step is the important one. Every patch set is applied to a shadow
copy of the product and pushed back through the hard pricing invariants. If the
repaired record would still violate them, the patches are downgraded to
`escalate` and nothing is written. That is the structural guarantee that a
price-above-retail record can never leave Hermes marked as fixed.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from app.config import policy, settings
from app.models import (
    Action, Evidence, Finding, Patch, ProductSnapshot, ReconcileResult,
    ReconcileStatus, Severity,
)
from app.rules import run_all
from app.rules.pricing import verify_invariants

log = logging.getLogger("hermes.pipeline")


def _needs_evidence(findings: list[Finding]) -> bool:
    return any(f.needs_evidence for f in findings)


def _apply_to_copy(p: ProductSnapshot, patches: list[Patch]) -> ProductSnapshot:
    shadow = p.model_copy(deep=True)
    for patch in patches:
        if patch.action is Action.ESCALATE or patch.new_value is None:
            continue
        if hasattr(shadow, patch.field):
            setattr(shadow, patch.field, patch.new_value)
            shadow.provenance[patch.field] = patch.provenance
            shadow.confidence[patch.field] = patch.confidence
    return shadow


def gather_evidence(p: ProductSnapshot, findings: list[Finding],
                    pol: dict[str, Any], llm: Any | None) -> Evidence:
    """Spend model calls only on the questions the rule engine actually raised."""
    ev = Evidence()
    if llm is None or not pol["llm"]["enabled"]:
        return ev

    ids = {f.rule_id for f in findings}
    fields_touched = {field for f in findings for field in f.fields}

    if ids & {"PRICE.001", "PRICE.002", "PRICE.020", "PRICE.010"}:
        currency = p.currency or pol["pricing"]["default_currency"]
        ev.rrp = llm.ground_rrp(p, currency)

    visual_fields = {"brand", "color", "material", "fit", "defects", "grade",
                     "subcategory", "category"}
    if p.images and (fields_touched & visual_fields):
        ev.vision = llm.audit_images(p, _claims(p))

    if p.description and ({"description", "title"} & fields_touched or ids & {"TEXT.004"}):
        ev.text_verdicts = llm.audit_description(p, _claims(p))

    ev.llm_calls = getattr(llm, "calls", 0)
    return ev


def _claims(p: ProductSnapshot) -> dict[str, Any]:
    """The subset of the record the model is allowed to see and judge."""
    return {
        k: v for k, v in {
            "brand": p.brand, "color": p.color, "material": p.material,
            "fit": p.fit, "condition": p.condition, "grade": p.grade,
            "master_category": p.master_category, "category": p.category,
            "subcategory": p.subcategory, "size": p.size, "waist": p.waist,
            "defects": p.defects, "title": p.title,
        }.items() if v
    }


def reconcile(p: ProductSnapshot, *, apply: bool = False,
              llm: Any | None = None, writer: Any | None = None) -> ReconcileResult:
    started = time.perf_counter()
    pol = policy()
    notes: list[str] = []

    # 1. detect
    findings = run_all(p, pol)
    if not findings:
        return ReconcileResult(
            product_id=p.id, status=ReconcileStatus.CLEAN,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    # 2. evidence (only if something asked for it)
    ev = Evidence()
    if _needs_evidence(findings):
        ev = gather_evidence(p, findings, pol, llm)
    else:
        notes.append("No model calls needed — all findings were resolvable by rule.")

    # 3. resolve
    from app.resolver import resolve  # local import keeps the module graph acyclic
    patches = resolve(p, findings, ev, pol)

    # 4. RE-VERIFY: does the repaired record actually satisfy the invariants?
    shadow = _apply_to_copy(p, patches)
    residual_hard = verify_invariants(shadow, pol)
    if residual_hard:
        notes.append(
            "Proposed repair still violated a hard pricing invariant; all pricing "
            "patches were downgraded to human review rather than written."
        )
        for patch in patches:
            if patch.field in {"price", "retail_price"}:
                patch.action = Action.ESCALATE
        shadow = _apply_to_copy(p, patches)

    residual = run_all(shadow, pol)

    # 5. status + publish gate
    unresolved_critical = [f for f in residual if f.severity is Severity.CRITICAL]
    escalations = [pt for pt in patches if pt.action is Action.ESCALATE]
    proposals = [pt for pt in patches if pt.action is Action.PROPOSE]
    auto = [pt for pt in patches if pt.action is Action.APPLY]

    if unresolved_critical:
        status = ReconcileStatus.BLOCKED
    elif escalations or proposals:
        status = ReconcileStatus.NEEDS_REVIEW
    elif auto:
        status = ReconcileStatus.REPAIRED
    else:
        status = ReconcileStatus.NEEDS_REVIEW

    # 6. write back
    applied: list[str] = []
    if apply and auto and not settings().dry_run and writer is not None:
        payload = {pt.field: pt.new_value for pt in auto}
        if writer.patch_product(p.id, payload, tenant_id=p.tenant_id):
            applied = list(payload)
        else:
            notes.append("Write-back to VNYX failed; patches are unapplied.")
            status = ReconcileStatus.NEEDS_REVIEW

    return ReconcileResult(
        product_id=p.id,
        status=status,
        findings=findings,
        residual_findings=residual,
        patches=patches,
        applied=applied,
        publishable=not unresolved_critical,
        llm_calls=ev.llm_calls,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=notes,
    )
