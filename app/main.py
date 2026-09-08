"""Hermes microservice."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import (
    BackgroundTasks, FastAPI, File, Form, Header, HTTPException, Request,
    UploadFile,
)
from pydantic import BaseModel, Field

from app.config import policy, reload_policy, settings
from app.imaging import background, legibility, nanobanana, openai_image
from app.models import (
    BackgroundCheck, BackgroundVerdict, Finding, ImageryGenerateResponse,
    ImagerySettings, ImageryVerdict, LegibilityVerdict, ProductSnapshot,
    ProductVerdict, ReconcileResult, ReconcileStatus, ReviewQueueResponse,
    Severity, SourceImage,
)
from app.pipeline import reconcile
from app.rules import REGISTRY, run_all
from app.rules import imagery as imagery_rules
from app.rules.pricing import assess
from app.vnyx_client import VnyxClient, to_snapshot
from app import agent_cli

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("hermes")

# Its own logger, and a name that says what it is: every verify decision is
# tagged `model-image-verifier` so "did anything check this product's renders,
# and what did it conclude?" is one grep rather than an inference.
verifier_log = logging.getLogger("model-image-verifier")

# Its sibling for the generation half: which of the tenant's named models is
# wearing the garment, and which image model rendered it. Both were only ever
# returned in `notes` — visible to the caller, invisible in the Hermes log,
# so "is it using the personalities from settings/image-generation?" could not
# be answered by looking.
generator_log = logging.getLogger("model-image-generator")

# And one for the readability checker, for the same reason both of the above have
# their own: it runs at shutter press many times per product, and "why was this
# frame rejected, and did we make the budget" has to be answerable from the log
# rather than by reproducing an operator's photograph.
readability_log = logging.getLogger("text-readability")

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Pay the OCR graph-build cost at boot, not on the first shutter press.

    ONNX Runtime compiles its execution graph on the first inference — measured at
    611ms against ~150ms of steady-state overhead. /v1/readability is a latency
    budget end to end, so that cost cannot be allowed to land on a request.

    Deliberately non-fatal. `warm_up` swallows its own failures and logs them, so
    a deployment without the OCR wheel still boots and serves the rest of Hermes;
    the endpoint reports the problem per request, with an install hint.
    """
    legibility.warm_up(policy().get("readability") or {})
    yield


app = FastAPI(title="Hermes", version="1.0.0",
              description="Deterministic verification agent for AI-generated "
                          "product records.",
              lifespan=lifespan)

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
    gen = (policy().get("imagery") or {}).get("generation") or {}
    return {
        "ok": True,
        "llm_enabled": s.llm_enabled and bool(s.gemini_api_key),
        "model": policy()["llm"]["model_reasoning"],
        "dry_run": s.dry_run,
        "rule_groups": list(REGISTRY),
        # Reported separately from `llm_enabled` because they are separate keys
        # with separate quotas: verification can be healthy while generation is
        # switched off, and "why did Generate answer 503" should be answerable
        # from here rather than from the source.
        "image_generation": {
            "enabled": bool(s.nano_banana_keys),
            # The COUNT, never the keys. More than one is what lets concurrent
            # views rotate instead of queueing behind one per-minute limit.
            "keys": len(s.nano_banana_keys),
            "model": gen.get("model"),
            "fallback_models": gen.get("fallback_models") or [],
            "openai_fallback": bool(gen.get("openai_fallback"))
            and openai_image.available(),
        },
        # Reported so a deployment can see BEFORE the first shutter press whether
        # /v1/readability will answer or 503 — the OCR wheel and its native
        # runtime are the one dependency of this service that fails at import
        # rather than at call time.
        "readability": {
            "ocr_available": legibility.available(),
            "working_px": (policy().get("readability") or {}).get("working_px"),
        },
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


# --------------------------------------------------------------------------- #
# Imagery — on-model renders and background removal
#
# Two endpoints behind one button on the product page. `verify` is report-only
# and fast enough to block on; `generate` produces the missing renders and hands
# the BYTES back rather than storing them, because Hermes holds no VNYX or R2
# credentials and the invariants around writing a ProductMedia row — position,
# isCurrent, derivedFromId, the Product.images cache rebuild — live in
# vnyx-api's services/product-media.ts and must not be reimplemented here.
# --------------------------------------------------------------------------- #

class ImageryRequest(BaseModel):
    """One product's imagery state, as vnyx-api reads it out of Postgres.

    Same contract as /v1/review-queue: everything needed is in the body, Hermes
    makes no callback, holds no credentials, and cannot reach a product the caller
    was not already authorised to read. The only network it touches is fetching
    image bytes from their public URLs.
    """

    product: dict[str, Any] = Field(default_factory=dict)
    # The LIVE ProductMedia rows. Send them typed — a URL alone cannot say whether
    # a picture is a care label the segmenter must never touch, an AI render, or a
    # raw upload still waiting to be cut out.
    media: list[dict[str, Any]] = Field(default_factory=list)
    # The tenant's ImageGenerationSettings row. Omitting it makes the rules
    # permissive rather than wrong: without it there is no way to know that a
    # tenant switched model generation off, and reporting that choice as a defect
    # is the single largest source of false positives this service has had.
    settings: dict[str, Any] | None = None


class ImageryVerifyRequest(ImageryRequest):
    # Download each garment photo and look at it. The metadata says whether the
    # segmenter RAN; only the pixels say whether it WORKED.
    check_pixels: bool = True
    # Escalate genuinely ambiguous backgrounds to the vision model. A plain wall
    # and a studio sweep produce the same border statistic, and this is the only
    # way to tell them apart — at one model call per doubtful image.
    use_llm: bool = False
    # Should the plan count the ¾ and close-up views as work? Passed here rather
    # than applied by the caller afterwards, so the decision Hermes returns is
    # the whole decision and nothing is re-derived downstream.
    include_advisory: bool = False


class ImageryGenerateRequest(ImageryRequest):
    # Which views to render, as VNYX ProductMediaView names. Empty means "whatever
    # is missing from the required set", which is what the repair button wants.
    views: list[str] = Field(default_factory=list)
    # Also fill in the ¾ and close-up views. Off by default: they are advisory,
    # and turning them on triples the cost of a repair.
    include_advisory: bool = False


def _imagery_snapshot(req: ImageryRequest) -> ProductSnapshot:
    return to_snapshot(
        {**req.product, "media": req.media},
        imagery_settings=req.settings,
    )


def _product_gender(p: ProductSnapshot) -> str | None:
    """The model's gender, taken from the PRODUCT rather than the tenant default.

    Mirrors the analyze worker: a Men's shirt gets a male model even when the
    tenant's default is female, because the alternative is a catalog where the
    model contradicts the listing. `masterCategory` is preferred over the
    `gender` property because it is the field the taxonomy is built on.
    """
    for value in (p.master_category, p.gender):
        low = (value or "").strip().lower()
        if low in ("men", "man", "male"):
            return "male"
        if low in ("women", "woman", "female"):
            return "female"
    return None


@app.post("/v1/imagery/verify", response_model=ImageryVerdict)
def imagery_verify(req: ImageryVerifyRequest) -> ImageryVerdict:
    """Are this product's pictures finished? Writes nothing, ever.

    Layer 1 is the `imagery` rule group — pure arithmetic over the media rows,
    the same code that runs on every review-queue page. Layer 2 downloads the
    garment photographs and looks at them, which is why it lives here and not in
    the bulk path: a 50-product page would mean hundreds of downloads.
    """
    started = time.perf_counter()
    pol = policy()
    cfg = pol.get("imagery") or {}
    snapshot = _imagery_snapshot(req)

    findings = list(imagery_rules.check_imagery(snapshot, pol))
    report = imagery_rules.view_report(snapshot, pol)
    reason = imagery_rules.not_generatable_reason(snapshot, pol)
    photos = imagery_rules.garment_photos(snapshot, pol)

    # ---- layer 2: what the pixels say ---------------------------------------
    backgrounds: list[BackgroundCheck] = []
    llm_calls = 0
    if req.check_pixels and photos:
        backgrounds = background.check_media(photos, cfg.get("pixels") or {})

        llm = make_llm() if req.use_llm else None
        for check in backgrounds:
            if llm is None or not background.is_ambiguous(check, cfg):
                continue
            # Only the doubtful ones, and only the doubtful ones — see
            # `is_ambiguous`. Re-fetched rather than cached from the pass above so
            # a large page never holds every image in memory at once.
            try:
                import httpx as _httpx

                with _httpx.Client(timeout=15, follow_redirects=True) as client:
                    data = client.get(check.url).content
                verdict, confidence, why = llm.classify_background(data)
            except Exception as exc:  # noqa: BLE001 — evidence is best-effort
                log.warning("background vision call failed for %s: %s",
                            check.url[:60], exc)
                continue
            llm_calls = getattr(llm, "calls", llm_calls)
            if verdict == "unknown":
                continue
            check.verdict = {
                "transparent": BackgroundVerdict.TRANSPARENT,
                "solid_studio": BackgroundVerdict.BACKDROP,
                "real_scene": BackgroundVerdict.SCENE,
            }[verdict]
            check.basis = "vision"
            check.confidence = confidence
            check.detail = why

        # IMG.013 — the row says the segmenter ran; the picture disagrees.
        #
        # The one finding metadata alone can never produce, and the reason this
        # layer exists: every bg-removal provider path retries a 429 forever but
        # accepts whatever a 200 returns, so a provider failing soft leaves a row
        # marked BG_REMOVED on top of an untouched photograph.
        #
        # Confidence-gated. The uncertain middle band is exactly where a plain
        # wall lives, and reporting those would make the finding untrustworthy.
        for check in backgrounds:
            if check.processing == "RAW":
                continue  # already IMG.010; saying it twice adds nothing
            if check.verdict is BackgroundVerdict.SCENE and check.confidence >= 0.5:
                findings.append(Finding(
                    rule_id="IMG.013", severity=Severity.MEDIUM, fields=["images"],
                    message=(
                        f"This image is recorded as {check.processing}, but it still "
                        f"has a background. {check.detail}."
                    ),
                    detail={"url": check.url, "view": check.view,
                            "basis": check.basis, "confidence": check.confidence},
                ))

    # ---- verdict --------------------------------------------------------------
    #
    # HIGH is the floor, not MEDIUM as on the review queue. This endpoint answers
    # one question — are the pictures finished — and the advisory findings it
    # returns (a missing ¾ view, a mislabelled render, generation not applying)
    # are all things a reviewer may reasonably ship. Letting them flip `correct`
    # would light up almost every product and make the button useless.
    disqualifying = [
        f for f in findings
        if _SEVERITY_RANK[f.severity] >= _SEVERITY_RANK[Severity.HIGH]
    ]
    status = (
        ReconcileStatus.NEEDS_REVIEW if disqualifying
        else ReconcileStatus.CLEAN
    )

    # ---- the decision, and the line that says what it was ---------------------
    #
    # Logged at INFO on every call, one line per product, whichever way it goes.
    # A verifier that only speaks when it finds something wrong cannot be
    # distinguished from a verifier that is not running — which is exactly how
    # this went unnoticed: 265 products marked COMPLETE with no renders, and
    # nothing in any log either way.
    plan = imagery_rules.generation_plan(snapshot, pol, req.include_advisory)
    verifier_log.info(
        "product=%s present=[%s] missing=[%s] matte=%d -> %s%s (%s)",
        snapshot.id,
        ",".join(report.present),
        ",".join(report.missing),
        len(plan.matte_first),
        "GENERATE" if plan.should_generate else "SKIP",
        f" [{','.join(plan.views)}]" if plan.views else "",
        plan.reason,
    )

    return ImageryVerdict(
        product_id=snapshot.id,
        correct=not disqualifying,
        status=status,
        findings=findings,
        advisory_count=len(findings) - len(disqualifying),
        worst_severity=_worst(findings),
        ai_views=report,
        backgrounds=backgrounds,
        generation_plan=plan,
        generatable=reason is None,
        not_generatable_reason=reason,
        source_images=imagery_rules.source_images(snapshot, pol),
        # Lifted out of IMG.010's detail so the caller can act without parsing a
        # message. `needs_segmenter` — not the raw RAW count — so a mis-filed
        # cut-out is never re-matted.
        needs_background_removal=[
            SourceImage(url=m.url, view=m.view, processing=m.processing)
            for m in imagery_rules.needs_segmenter(snapshot, pol)
        ],
        edit_url=snapshot.edit_url,
        llm_calls=llm_calls,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


@app.post("/v1/imagery/generate", response_model=ImageryGenerateResponse)
def imagery_generate(req: ImageryGenerateRequest) -> ImageryGenerateResponse:
    """Render the missing on-model views and hand the bytes back.

    Stores nothing. The caller writes them through its own upload + `addImages`
    path, so watermarking, R2 keys, gallery position, the derivation edge, the
    `Product.images` cache rebuild and the credit deduction all stay in the one
    codebase that owns those invariants.

    Refuses rather than guesses when generation does not apply — footwear, a
    tenant that switched it off, or no photograph to work from. A 409 with the
    reason is a better answer than five renders nobody asked for.
    """
    started = time.perf_counter()
    pol = policy()
    cfg = (pol.get("imagery") or {}).get("generation") or {}
    snapshot = _imagery_snapshot(req)

    s = settings()
    # Image generation runs on GOOGLE_NANO_BANANA_API_KEY and nothing else.
    # GEMINI_API_KEY drives the text and vision evidence layer, which has its own
    # quota and its own cost; borrowing it here would let an image backfill
    # exhaust the budget the verification path depends on.
    if not s.nano_banana_keys:
        raise HTTPException(
            503,
            "No GOOGLE_NANO_BANANA_API_KEY configured; image generation is "
            "disabled. (GEMINI_API_KEY drives the evidence layer and is "
            "deliberately not used for generation.)",
        )

    reason = imagery_rules.not_generatable_reason(snapshot, pol)
    if reason:
        raise HTTPException(409, f"Cannot generate for this product: {reason}.")

    report = imagery_rules.view_report(snapshot, pol)
    wanted = req.views or (
        report.missing + (report.advisory_missing if req.include_advisory else [])
    )
    if not wanted:
        return ImageryGenerateResponse(
            product_id=snapshot.id, model=cfg.get("model") or "",
            duration_ms=int((time.perf_counter() - started) * 1000),
            notes=["Nothing missing — every required view is already on the product."],
        )

    sources = imagery_rules.source_images(snapshot, pol)
    generator = nanobanana.NanoBanana(s.nano_banana_keys, pol)

    # Front first, back second — `source_images` already ordered them that way,
    # preferring a matted derivative over its raw original.
    front_src = next((x for x in sources if x.view == "FRONT"), sources[0])
    back_src = next((x for x in sources if x.view == "BACK"), None)
    extras = [x for x in sources if x is not front_src and x is not back_src]

    context = extras[: int(cfg.get("max_context_images") or 1)]
    urls = [front_src.url] + ([back_src.url] if back_src else []) + [
        x.url for x in context
    ]
    buffers = generator.fetch(urls, timeout=float(cfg.get("timeout_s") or 300) / 10)

    # Resolved BY URL. Indexing a list positionally is how a truncated 6 MB front
    # photo silently promoted the BACK photograph into the front slot — the model
    # was then asked for a front view of the garment's reverse, and nothing about
    # the output said so.
    front_bytes = buffers.get(front_src.url)
    if front_bytes is None:
        # Recoverable for the back and the context images; not for this one.
        # There is no meaningful generation without the front photograph, and
        # substituting another view is exactly the bug above.
        raise HTTPException(
            502,
            "Could not download the front photograph for this product "
            f"({front_src.url[:80]}). Nothing was generated.",
        )
    back_bytes = buffers.get(back_src.url) if back_src else None
    additional = [buffers[x.url] for x in context if x.url in buffers]

    # An existing render is the cheapest way to keep one model identity across a
    # set: generating only the missing back view against the product's own
    # AI_FRONT costs one call and matches what is already in the gallery, where
    # regenerating all five costs five and replaces images nobody complained about.
    front_reference = None
    notes: list[str] = []
    existing_front = next(
        (m for m in imagery_rules.ai_renders(snapshot) if m.view == "AI_FRONT"), None
    )
    if existing_front and "AI_FRONT" not in wanted:
        got = generator.fetch([existing_front.url])
        if existing_front.url in got:
            front_reference = got[existing_front.url]
            notes.append(
                "Kept the model identity from the product's existing AI_FRONT render."
            )

    # ---- which model wears it ------------------------------------------------
    #
    # Traits already stored on the product win. A gap-fill generating AI_BACK
    # beside an existing AI_FRONT must put the SAME person in it, and drawing a
    # fresh personality would describe a different one while the reference image
    # shows the original — the two instructions fight and the result matches
    # neither.
    # NOT `settings` — that name is the module-level config accessor imported
    # at the top, and shadowing it here made `s = settings()` above resolve to
    # this local before it was assigned.
    gen_settings = snapshot.imagery_settings or ImagerySettings()
    gender = _product_gender(snapshot)
    stored = req.product.get("imageSettings") or {}
    chosen: dict[str, Any] | None = None

    if any(stored.get(k) for k in ("skinTone", "hairColor", "hairStyle")):
        gen_settings = nanobanana.apply_personality(gen_settings, stored)
        notes.append(
            f"Reused the model already on this product"
            + (f" ({stored.get('personalityName')})."
               if stored.get("personalityName") else ".")
        )
    else:
        chosen = nanobanana.choose_personality(gen_settings, gender)
        gen_settings = nanobanana.apply_personality(gen_settings, chosen)
        if chosen:
            notes.append(f"Model: {chosen.get('name') or 'unnamed personality'}.")

    # Say which person is wearing it and where that choice came from. The three
    # cases read differently on purpose: a REUSED model is the product's own
    # stored traits (so a filled-in view matches the renders beside it), a CAST
    # pick is a fresh draw from the tenant's personalities, and NO CAST means
    # the tenant has none enabled and the generic settings describe the model.
    _pool = [
        p for p in (gen_settings.personalities or [])
        if isinstance(p, dict) and p.get("enabled") is not False
    ]
    if stored.get("personalityName") or any(
        stored.get(k) for k in ("skinTone", "hairColor", "hairStyle")
    ):
        _source = f"REUSED from the product ({stored.get('personalityName') or 'unnamed'})"
    elif chosen:
        _source = (
            f"CAST pick '{chosen.get('name') or 'unnamed'}' "
            f"from {len(_pool)} enabled personality(ies)"
        )
    elif not gen_settings.personalities_enabled:
        _source = "NO CAST — personalities disabled for this tenant; using the generic settings"
    else:
        _source = "NO CAST — no enabled personality matched this product's gender"

    generator_log.info(
        "product=%s gender=%s model=%s | %s | aspect=%s resolution=%s",
        snapshot.id,
        gender or "unspecified",
        gen_settings.personality_name or "-",
        _source,
        gen_settings.aspect_ratio or "auto",
        gen_settings.resolution or "2K",
    )

    ctx = nanobanana.PromptContext(
        settings=gen_settings,
        category=snapshot.category,
        sub_category=snapshot.subcategory,
        mannequin_type=snapshot.mannequin,
        gender=gender,
        back_inferred=back_bytes is None,
    )
    if ctx.back_inferred:
        notes.append(
            "No back photograph exists, so any rear view is inferred from the "
            "front rather than observed."
        )

    # Whatever produced the renders already on this product goes first in the
    # chain. Filling in a back view with a different model than the front was
    # made with puts two different people in one gallery — which is what
    # happened on the DeRozan jersey: a Gemini front beside a gpt-image back.
    preferred_model = (stored or {}).get("generatedWith")
    if preferred_model:
        notes.append(f"Preferring {preferred_model}, which made the existing renders.")

    views = generator.generate(
        views=wanted, front=front_bytes, back=back_bytes, ctx=ctx,
        additional=additional, front_reference=front_reference,
        preferred_model=preferred_model,
    )

    # If the set had to be produced by a different model, the new views will not
    # match the ones already on the product. Say so plainly — the operator can
    # regenerate the whole set for consistency, and a silent mismatch is exactly
    # the complaint this flow exists to answer.
    used = next((v.model for v in views if v.ok and v.model), None)
    generator_log.info(
        "product=%s rendered %d/%d view(s) with %s%s",
        snapshot.id,
        sum(1 for v in views if v.ok),
        len(views),
        used or "nothing",
        f" (wearing {gen_settings.personality_name})"
        if gen_settings.personality_name else "",
    )
    if used and preferred_model and used != preferred_model and report.present:
        notes.append(
            f"NOTE: these were produced with {used}, but the renders already on "
            f"the product came from {preferred_model}. The two will not look like "
            f"the same person — regenerate every view to get a consistent set."
        )

    return ImageryGenerateResponse(
        product_id=snapshot.id,
        views=views,
        failed=[v.view for v in views if not v.ok],
        back_inferred=ctx.back_inferred,
        model=generator.model,
        llm_calls=generator.calls,
        duration_ms=int((time.perf_counter() - started) * 1000),
        notes=notes + generator.errors,
        # Only when something was actually produced — recording a model against a
        # product with no renders would pin a face to images that do not exist.
        image_settings=({
            "personalityName": gen_settings.personality_name,
            "age": gen_settings.age,
            "skinTone": gen_settings.skin_tone,
            "hairColor": gen_settings.hair_color,
            "hairStyle": gen_settings.hair_style,
            "tattoos": gen_settings.tattoos,
            "piercings": gen_settings.piercings,
            "ethnicity": gen_settings.ethnicity,
            "bodyType": gen_settings.body_type,
            "aspectRatio": gen_settings.aspect_ratio,
            "resolution": gen_settings.resolution,
            # The model these renders came from. Read back on the next run so a
            # view added later is made by the same one.
            "generatedWith": used,
        } if any(v.ok for v in views) else None),
    )


# --------------------------------------------------------------------------- #
# Readability — can the text in this photograph be read?
#
# The odd one out in this service, and worth saying why it lives here anyway.
# Every other endpoint judges a product RECORD; this judges one uploaded frame
# and knows nothing about a product. What it shares is the thing that defines
# Hermes: a deterministic verdict with a stated reason, no model call, and no
# write-back. It is a verifier, of pixels instead of columns.
#
# It is also the only endpoint with a hard latency budget. It fires at shutter
# press on a warehouse phone, so ~1.5s covers the upload, the answer and the
# operator's retake decision. That is what shapes every choice in
# app/imaging/legibility.py.
# --------------------------------------------------------------------------- #

@app.post("/v1/readability", response_model=LegibilityVerdict)
def readability(
    image: UploadFile = File(
        ...,
        description="The photograph, as multipart/form-data. JPEG or PNG.",
    ),
    min_lines: int | None = Form(
        default=None,
        description=(
            "How many legible lines this frame must contain. Overrides the policy "
            "default. Raise it when the client knows what it is photographing — a "
            "care label has six lines, a size tag has two."
        ),
    ),
    min_chars: int | None = Form(
        default=None,
        description="How many legible characters this frame must contain.",
    ),
) -> LegibilityVerdict:
    """Is the text in this image readable, and if not, what should be fixed?

    `readable` and `message` are the whole contract for a simple client: keep the
    frame, or show the message and ask for another. Everything else on the
    response is the evidence behind that, including the text itself — a caller
    that has already paid the upload usually wants it, and a second round trip to
    fetch it would cost another budget's worth of time.

    MULTIPART, NOT BASE64 IN JSON, and not a URL. Base64 inflates the payload by
    a third for nothing, and upload is the largest line item in the budget on a
    mobile connection. A URL would mean Hermes fetching the bytes itself, adding a
    second network hop to a request that has no room for one.

    `def`, not `async def`: FastAPI runs a sync handler in its threadpool, so the
    CPU-bound inference stays off the event loop without this file having to
    manage an executor. Concurrent calls serialise inside the imaging layer — see
    the lock there for why that is deliberate rather than a limitation.
    """
    cfg = policy().get("readability") or {}

    data = image.file.read()
    if not data:
        raise HTTPException(400, "Empty upload.")

    # Checked after reading rather than from a Content-Length header, which a
    # client controls and can lie about. Read cost is bounded by the server's own
    # request-size limits well before this.
    cap_mb = float(cfg.get("max_upload_mb") or 12)
    if len(data) > cap_mb * 1024 * 1024:
        raise HTTPException(
            413,
            f"Image is {len(data) / 1024 / 1024:.1f}MB; the cap is {cap_mb:.0f}MB. "
            "Resize on the device before uploading — 1280-1600px on the long edge "
            "at quality 80 is about 200KB, reads the same, and is the difference "
            "between fitting the latency budget and not.",
        )

    try:
        verdict = legibility.assess(
            data, cfg, min_lines=min_lines, min_chars=min_chars
        )
    except legibility.OcrUnavailable as exc:
        # 503, not 500: the request is well-formed and the service is simply not
        # in a position to serve it. Same distinction /v1/imagery/generate draws
        # for a missing generation key.
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    # One line per call, whichever way it goes — the same reasoning as the imagery
    # verifier's log. A checker that only speaks up on failure cannot be told
    # apart from a checker that is not running, and here it also carries the
    # latency split, which is the number a deployment has to watch.
    readability_log.info(
        "%s conf=%.3f lines=%d/%d chars=%d | %dx%d->%dx%d sharp=%.0f "
        "bright=%.0f | decode=%dms ocr=%dms total=%dms%s",
        "READABLE" if verdict.readable else "REJECTED",
        verdict.confidence,
        verdict.line_count,
        verdict.detected_count,
        verdict.char_count,
        verdict.frame.width, verdict.frame.height,
        verdict.frame.working_width, verdict.frame.working_height,
        verdict.frame.sharpness,
        verdict.frame.brightness,
        verdict.decode_ms,
        verdict.ocr_ms,
        verdict.duration_ms,
        f" | {','.join(r.value for r in verdict.reasons)}"
        if verdict.reasons else "",
    )
    return verdict
