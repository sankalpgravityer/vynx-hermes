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
    # `care_label_urls` counts too. A product whose gallery is empty but whose
    # care label was photographed is exactly the case worth asking about — the
    # label carries brand, material and size — and gating on `p.images` alone
    # skipped it silently.
    if (p.images or p.care_label_urls) and (
        (fields_touched & visual_fields) or wants_taxonomy(p, pol)
    ):
        ev.vision = llm.audit_images(p, _claims(p), taxonomy_options(p, pol))

    if p.description and ({"description", "title"} & fields_touched or ids & {"TEXT.004"}):
        ev.text_verdicts = llm.audit_description(p, _claims(p))

    ev.llm_calls = getattr(llm, "calls", 0)
    return ev


def wants_taxonomy(p: ProductSnapshot, pol: dict[str, Any] | None) -> bool:
    """Should the vision call carry the taxonomy question, with no rule asking?

    THE WHOLE POINT, AND THE REASON IT CANNOT BE FINDING-DRIVEN. Every other
    question this layer asks is raised by a rule first: a finding names a field,
    `fields_touched` picks it up, the call is made. A garment filed under a
    category that EXISTS IN THE TREE raises no finding at all — TAX.002 and
    TAX.003 read columns and both are correct about MID-000615's
    `Women > Dresses > Casual Dress`. So waiting for a rule means waiting
    forever, and the question has to be asked on its own account.

    Which makes it a real cost decision rather than a free ride, and `always_ask`
    is where it is made:

      true   ask on every product that has a garment photograph and a tree to
             choose from. This is what "the field is never empty and never wrong"
             actually requires, and on any product that already triggered the
             vision call it is free — same call, same images, more output tokens.
             On a clean product it is one cached Gemini call, once per
             `llm.cache.ttl_hours`.
      false  ask only alongside a question some rule already raised, which is the
             behaviour before this existed: cheaper, and blind to exactly the
             case above.

    A care label alone is not enough — `p.images` is required. Filing a garment
    from a photograph of its wash tag is not a question worth paying for.
    """
    cfg = (pol or {}).get("taxonomy_from_picture") or {}
    if not cfg.get("enabled", True) or not cfg.get("always_ask", True):
        return False
    return bool(p.images) and bool(taxonomy_options(p, pol))


def taxonomy_options(p: ProductSnapshot, pol: dict[str, Any] | None) -> dict[str, list[str]]:
    """`{category: [subcategory, ...]}` the picture may be filed under, or `{}`.

    THE TENANT'S OWN TREE, NARROWED TO THE PRODUCT'S MASTER CATEGORY. Narrowed
    because the master is the anchor everything else hangs off (readiness phase
    1): a Women's product filed under a Men's category is a different, already
    detected fault, and offering the whole tree invites the model to move the
    product across roots on the strength of a photograph — which is precisely
    what `readiness.master.photo_check` refuses to let a picture do.

    EMPTY WHEN THERE IS NO MASTER, and that is deliberate rather than a
    fallback to the whole tree. With no anchor there is no answer to narrow to,
    the master planner escalates the product anyway
    (MASTER_CATEGORY_UNRESOLVED), and asking the question would spend output
    tokens on a suggestion nothing may act on.

    Empty also when the feature is off in policy, so the prompt drops the whole
    section and the call returns to exactly its previous shape.
    """
    cfg = (pol or {}).get("taxonomy_from_picture") or {}
    if not cfg.get("enabled", True):
        return {}
    if not (p.catalog and p.catalog.categories and p.master_category):
        return {}
    # The tenant's own spelling of the root, matched loosely — 'WOMEN' and
    # 'Women' are the same root, and `decide_master` already treats them so.
    from app.readiness import flat

    want = flat(p.master_category)
    for root, branch in p.catalog.categories.items():
        if flat(root) == want:
            return {str(cat): [str(s) for s in (subs or [])]
                    for cat, subs in (branch or {}).items()}
    return {}


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
        # The RETURN VALUE, not `payload`: patch_product now splits the write
        # across two endpoints (columns via PUT, `properties` via PATCH) which
        # fail independently, so it reports which fields actually landed. Taking
        # `payload` here would claim a price AND a size write when only the price
        # went through.
        applied = writer.patch_product(p.id, payload, tenant_id=p.tenant_id)
        if len(applied) < len(payload):
            missed = sorted(set(payload) - set(applied))
            notes.append(
                f"Write-back to VNYX did not apply: {', '.join(missed)}."
            )
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
