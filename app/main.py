"""Hermes microservice."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import policy, reload_policy, settings
from app.models import (
    Finding, ProductSnapshot, ProductVerdict, ReconcileResult, ReconcileStatus,
    ReviewQueueResponse, Severity,
)
from app.pipeline import reconcile
from app.rules import REGISTRY, run_all
from app.rules.pricing import assess
from app.vnyx_client import VnyxClient, to_snapshot
from app import agent_cli

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("hermes")

app = FastAPI(title="Hermes", version="1.0.0",
              description="Deterministic verification agent for AI-generated "
                          "product records.")

_vnyx: VnyxClient | None = None


def vnyx() -> VnyxClient:
    global _vnyx
    if _vnyx is None:
        _vnyx = VnyxClient()
    return _vnyx


def make_llm() -> Any | None:
    """Build the evidence layer, or return None to run rules-only.

    Imported lazily so Hermes still boots and serves the rule engine when the
    Gemini SDK isn't installed or no API key is configured.
    """
    s = settings()
    if not s.llm_enabled or not s.gemini_api_key:
        return None
    try:
        from app.llm.gemini import GeminiEvidence
    except ImportError as exc:
        log.warning("Gemini SDK unavailable (%s); running rules-only.", exc)
        return None
    return GeminiEvidence(s.gemini_api_key, policy())


def audit(result: ReconcileResult) -> None:
    path = settings().audit_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "product_id": result.product_id,
            "status": result.status.value,
            "findings": [f.rule_id for f in result.findings],
            "patches": [
                {"field": p.field, "old": p.old_value, "new": p.new_value,
                 "action": p.action.value, "rule": p.rule_id,
                 "confidence": p.confidence}
                for p in result.patches
            ],
            "applied": result.applied,
            "llm_calls": result.llm_calls,
        }
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        log.warning("audit write failed: %s", exc)


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #

class CheckRequest(BaseModel):
    """Either send a raw VNYX product payload, or just an id for Hermes to fetch."""
    product: dict[str, Any] | None = None
    product_id: str | None = None
    tenant_id: str | None = None
    use_llm: bool = True


class BatchRequest(BaseModel):
    product_ids: list[str] = Field(default_factory=list)
    tenant_id: str | None = None
    apply: bool = False


def _load(req: CheckRequest) -> ProductSnapshot:
    if req.product:
        return to_snapshot(req.product)
    if req.product_id:
        return vnyx().fetch_product(req.product_id, req.tenant_id)
    raise HTTPException(400, "Provide either `product` or `product_id`.")


def _run(req: CheckRequest, apply: bool) -> ReconcileResult:
    snapshot = _load(req)
    llm = make_llm() if req.use_llm else None
    result = reconcile(snapshot, apply=apply, llm=llm, writer=vnyx() if apply else None)
    audit(result)
    return result


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.get("/healthz")
def healthz() -> dict[str, Any]:
    s = settings()
    return {
        "ok": True,
        "llm_enabled": s.llm_enabled and bool(s.gemini_api_key),
        "model": policy()["llm"]["model_reasoning"],
        "dry_run": s.dry_run,
        "rule_groups": list(REGISTRY),
    }


@app.post("/v1/validate", response_model=ReconcileResult)
def validate(req: CheckRequest) -> ReconcileResult:
    """Dry run. Reports findings and proposed patches; writes nothing."""
    return _run(req, apply=False)


@app.post("/v1/reconcile", response_model=ReconcileResult)
def reconcile_endpoint(req: CheckRequest) -> ReconcileResult:
    """Detect, repair, re-verify, and write safe patches back to VNYX."""
    return _run(req, apply=True)


@app.post("/v1/batch")
def batch(req: BatchRequest, tasks: BackgroundTasks) -> dict[str, Any]:
    def worker() -> None:
        for pid in req.product_ids:
            try:
                _run(CheckRequest(product_id=pid, tenant_id=req.tenant_id), req.apply)
            except Exception as exc:  # noqa: BLE001
                log.error("batch item %s failed: %s", pid, exc)

    tasks.add_task(worker)
    return {"queued": len(req.product_ids), "apply": req.apply}


@app.post("/v1/webhooks/product-enriched", response_model=ReconcileResult)
async def webhook(request: Request,
                  x_vnyx_signature: str | None = Header(default=None)) -> ReconcileResult:
    """Hook this to fire the moment your enrichment pipeline finishes a product."""
    body = await request.body()
    secret = settings().webhook_secret
    if secret:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not x_vnyx_signature or not hmac.compare_digest(expected, x_vnyx_signature):
            raise HTTPException(401, "Bad signature.")
    payload = json.loads(body)
    return _run(CheckRequest(product=payload.get("product", payload)), apply=True)


# --------------------------------------------------------------------------- #
# Nous Research Hermes AGENT — prompt passthrough
#
# A different Hermes from the one this service is. See app/agent_cli.py; the
# short version is that this service decides nothing with a model, while the
# agent is an autonomous LLM with shell and filesystem tools. These endpoints
# only relay a prompt to its CLI and hand back the reply.
# --------------------------------------------------------------------------- #

class AgentPromptRequest(BaseModel):
    """One prompt for the agent."""

    prompt: str = Field(..., min_length=1, description="What to ask the agent.")
    # Per-run overrides. Both are passed straight to the CLI, which applies them
    # without touching ~/.hermes/config.yaml — so a request cannot change the
    # machine's default model for everyone else.
    model: str | None = Field(
        default=None, description='e.g. "anthropic/claude-sonnet-4.6"'
    )
    provider: str | None = Field(
        default=None, description='e.g. "nous", "openrouter"'
    )
    timeout_s: float | None = Field(
        default=None, gt=0, description="Seconds to wait. Capped server-side."
    )
    cwd: str | None = Field(
        default=None,
        description=(
            "Working directory for the run. The agent reads and writes files "
            "relative to this, so point it at the project you want it to act on."
        ),
    )


class AgentPromptResponse(BaseModel):
    response: str
    duration_ms: int
    model: str | None = None
    provider: str | None = None
    session_id: str | None = None
    # tokens / estimated_cost_usd / api_calls, from the CLI's own usage report.
    usage: dict[str, Any] | None = None
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)


@app.get("/v1/agent/health")
def agent_health() -> dict[str, Any]:
    """Is the agent CLI installed and switched on.

    Distinguishes the two failure modes a caller cares about: `installed: false`
    means go and install it; `enabled: false` means it is there but deliberately
    gated off.
    """
    return agent_cli.status()


@app.post("/v1/agent/prompt", response_model=AgentPromptResponse)
async def agent_prompt(req: AgentPromptRequest) -> AgentPromptResponse:
    """Send a prompt to the Hermes agent and return its final answer.

    Stateless — a fresh one-shot run per call, with no conversation carried over.

    `async def`, not `def`: the run takes seconds to minutes, and the adapter
    awaits the subprocess rather than blocking. A sync handler would occupy a
    threadpool worker for the whole run.
    """
    try:
        run = await agent_cli.run_prompt(
            req.prompt,
            model=req.model,
            provider=req.provider,
            timeout_s=req.timeout_s,
            cwd=req.cwd,
        )
    except agent_cli.AgentUnavailable as exc:
        # 503: correctly configured request, service not in a position to serve it.
        raise HTTPException(503, str(exc)) from exc
    except agent_cli.AgentTimeout as exc:
        raise HTTPException(504, str(exc)) from exc
    except agent_cli.AgentFailed as exc:
        # 502: the upstream we depend on failed. Its stderr is the only useful
        # diagnostic, so it is passed through rather than swallowed.
        raise HTTPException(
            502, f"{exc} — stderr: {exc.stderr or '(empty)'}"
        ) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    return AgentPromptResponse(
        response=run.response,
        duration_ms=run.duration_ms,
        model=run.model,
        provider=run.provider,
        session_id=run.session_id,
        usage=run.usage,
        truncated=run.truncated,
        notes=run.notes,
    )


@app.post("/v1/policy/reload")
def policy_reload() -> dict[str, Any]:
    reload_policy()
    pr = policy()["pricing"]
    # `grade_targets`, not `grade_bands`: the policy moved to a target+tolerance
    # model and the old key no longer exists, so reading it raised KeyError here
    # and in every pricing check.
    return {
        "reloaded": True,
        "grade_targets": pr["grade_targets"],
        "tolerance": pr["tolerance"],
        "factor_tolerance": pr.get("factor_tolerance", pr["tolerance"]),
        "hard_max_ratio": pr["hard_max_ratio"],
    }


# --------------------------------------------------------------------------- #
# Review-queue verification — report-only
# --------------------------------------------------------------------------- #

class ReviewQueueRequest(BaseModel):
    """A page of the VNYX review-verification feed, ready to judge.

    Everything needed is in the body. Hermes makes no callback to VNYX on this
    path, so it holds no credentials, implements no tenant scoping, and cannot
    reach a product the caller was not already authorised to read.
    """

    products: list[dict[str, Any]] = Field(default_factory=list)
    # The tenant's grade ladder, echoed into responses for explainability. The
    # per-product priceFactor already rides on each record, so this is context,
    # not an input to any rule.
    grade_ladder: dict[str, Any] = Field(default_factory=dict)
    # tenantId -> that tenant's option lists (categories, sizing guides, colours,
    # materials, brands). LOAD-BEARING, unlike grade_ladder: the taxonomy, sizing
    # and catalog rules validate against these, and without them they fall back to
    # policy.yaml's generic tables or stay silent. Keyed by tenant because the
    # lists differ per tenant — merging them would let one tenant's colours
    # validate another's product.
    catalog: dict[str, Any] = Field(default_factory=dict)
    use_llm: bool = False
    # Restrict to a subset of REGISTRY (e.g. ["pricing"]). None = every group.
    rule_groups: list[str] | None = None
    # Severity at or above which a finding makes a product `correct: false`.
    # 'low' means every finding counts; 'medium' (the default) treats the cosmetic
    # copy and confidence rules as advisories.
    min_severity: Severity = Severity.MEDIUM


_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


def _worst(findings: list[Finding]) -> Severity | None:
    if not findings:
        return None
    return max((f.severity for f in findings), key=lambda s: _SEVERITY_RANK[s])


def _at_or_above(findings: list[Finding], floor: Severity) -> list[Finding]:
    return [f for f in findings if _SEVERITY_RANK[f.severity] >= _SEVERITY_RANK[floor]]


@app.post("/v1/review-queue", response_model=ReviewQueueResponse)
def review_queue(req: ReviewQueueRequest) -> ReviewQueueResponse:
    """Judge a page of review-queue products. Writes nothing, ever.

    This is the endpoint vnyx-api calls from GET /review-verification/queue. It
    is detect-only by design — no resolver, no patches, no write-back. The answer
    to a failed check is the `edit_url` that came in with the record, so a human
    fixes it on the screen that owns the field.

    Deliberately NOT built on `reconcile()`: that pipeline exists to compute and
    apply repairs, and running it here would spend the resolver's work on patches
    nobody will write — and would risk one escaping if HERMES_DRY_RUN were ever
    misconfigured. Report-only is enforced by which code runs, not by a flag.
    """
    started = time.perf_counter()
    pol = policy()
    llm = make_llm() if req.use_llm else None

    verdicts: list[ProductVerdict] = []
    for raw in req.products:
        # Resolve this product's tenant catalog before mapping, so the rules see
        # the tenant's own categories/charts/option lists rather than the policy
        # fallbacks. A product whose tenant is absent from `catalog` degrades to
        # the fallbacks rather than failing.
        tenant_id = str(raw.get("tenantId") or raw.get("tenant_id") or "")
        snapshot = to_snapshot(raw, catalog=req.catalog.get(tenant_id))
        findings = run_all(snapshot, pol, only=req.rule_groups)

        # The evidence layer can only ADD findings here, never patches — the
        # pipeline's arbitration is what turns evidence into a value, and that is
        # the part this endpoint does not run.
        llm_calls = 0
        if llm is not None and any(f.needs_evidence for f in findings):
            from app.pipeline import gather_evidence

            ev = gather_evidence(snapshot, findings, pol, llm)
            llm_calls = ev.llm_calls
            for verdict in list(ev.vision.verdicts) + list(ev.text_verdicts):
                if verdict.verdict != "contradict":
                    continue
                findings.append(Finding(
                    rule_id="LLM.001",
                    severity=Severity.MEDIUM,
                    fields=[verdict.field],
                    message=(
                        f"Evidence contradicts '{verdict.field}': the record says "
                        f"{getattr(snapshot, verdict.field, None)!r}, the images or "
                        f"copy suggest {verdict.observed_value!r}. {verdict.evidence}"
                    ),
                    detail={"observed": verdict.observed_value,
                            "confidence": verdict.confidence},
                ))

        critical = [f for f in findings if f.severity is Severity.CRITICAL]
        disqualifying = _at_or_above(findings, req.min_severity)

        if critical:
            status = ReconcileStatus.BLOCKED
        elif disqualifying:
            status = ReconcileStatus.NEEDS_REVIEW
        else:
            # Advisory-only findings leave the record CLEAN: they are worth
            # reading, not worth stopping for.
            status = ReconcileStatus.CLEAN

        verdicts.append(ProductVerdict(
            product_id=snapshot.id,
            correct=not disqualifying,
            status=status,
            publishable=not critical,
            findings=findings,
            advisory_count=len(findings) - len(disqualifying),
            min_severity=req.min_severity,
            worst_severity=_worst(findings),
            # Reported on every product, clean or not: "is this price right" and
            # "why was this flagged" want the same numbers.
            price=assess(snapshot, pol),
            edit_url=snapshot.edit_url,
            llm_calls=llm_calls,
        ))

    return ReviewQueueResponse(
        verdicts=verdicts,
        rule_groups=req.rule_groups or list(REGISTRY),
        checked=len(verdicts),
        incorrect=sum(1 for v in verdicts if not v.correct),
        duration_ms=int((time.perf_counter() - started) * 1000),
    )
