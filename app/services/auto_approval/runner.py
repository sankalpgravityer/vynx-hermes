"""Verify one product, and drain a tenant's queue.

THE PIPELINE IS repair_product.repair(), UNCHANGED. Nothing here reimplements a
check, a repair or an approval: repair() already owns the seven ordered steps and
the reasons for their order (fields settled before a single paid render, because
the renderer picks the model's gender off masterCategory/gender and getting it
wrong costs ~200s and a paid image call per view).

What this module adds is the part repair() has no opinion about: claiming work
safely, recording what happened, and stopping when asked.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from app import product_audit
from app.config import settings
from app.llm import health
from app.services.auto_approval import claim, config as cfgmod, db, events
from app.services.auto_approval.outcome import Verdict, classify
from app.services.auto_approval.schedule import in_window, window_from_row

log = logging.getLogger("auto-approval.runner")

# How long one pump task may hold the single worker slot.
#
# Without a bound a 4,000-product run holds it for days, no other tenant is
# served, and Celery's soft time limit fires mid-product. The next tick resumes
# with no state to carry, because "where it left off" is the table.
PUMP_BUDGET_S = int(__import__("os").getenv("AUTO_APPROVAL_PUMP_BUDGET_S", "900"))

# How long a tenant stays stopped after the canary trips. Process-local, like
# the vision-health pause: the next beat tick would otherwise pick the tenant
# straight back up and spend another product's renders discovering the same
# thing. No column is written — this is the loop declining, not the tenant
# being switched off — so a worker restart clears it, which is right: the
# operator restarting the worker is the operator saying "try again".
CANARY_PAUSE_S = int(__import__("os").getenv("AUTO_APPROVAL_CANARY_PAUSE_S", "3600"))
_stopped: dict[str, tuple[float, str]] = {}   # tenant_id -> (until, why)


def _load_repair():
    """Import repair_product.repair, making `scripts/` importable first.

    Belt to celery_app.py's braces: that handles the worker, this handles a
    direct call from pytest or a REPL, so the import does not depend on how the
    process happened to be started. `scripts/` has no __init__.py, so it only
    resolves as a namespace package when the repo root is on sys.path.
    """
    import sys

    root = str(Path(__file__).resolve().parents[3])
    if root not in sys.path:
        sys.path.insert(0, root)
    from scripts.repair_product import repair

    return repair


def _vnyx_api_dir() -> Path:
    """The vnyx-api checkout whose scripts every write goes through.

    Must contain `scripts/` AND `node_modules/` — `tsconfig.json` excludes
    scripts/, so `npx tsx` is the only way they run.
    """
    import os

    raw = os.getenv("VNYX_API_DIR")
    if raw:
        return Path(raw)
    if os.getenv("VNYX_API_URL"):
        # REMOTE transport: the steps run on the vnyx-api host, so there is no
        # local checkout and this path is never read. repair() still takes the
        # argument, so hand it something harmless rather than making the
        # signature conditional.
        return Path(".")
    raise RuntimeError(
        "Neither VNYX_API_URL nor VNYX_API_DIR is set. The worker needs one: "
        "the URL to run vnyx-api's repair steps remotely, or the path to a "
        "local checkout to spawn them."
    )


def _finish(
    row: dict[str, Any],
    verdict: Verdict,
    *,
    blocking_rules: list[str] | None = None,
    preflight: list[str] | None = None,
    steps: list[dict[str, Any]] | None = None,
    deltas: dict[str, Any] | None = None,
    error: str | None = None,
    duration_ms: int | None = None,
    expect_status: str = "IN_PROGRESS",
) -> None:
    """Write the terminal status, the product's cache and the run counters.

    ONE TRANSACTION, and the run-product UPDATE is conditional on the status the
    caller expects to find — so a row the lease reaper already took back is not
    resurrected, and the counters cannot be incremented twice for one product.
    That conditional is the whole reason the counters can be denormalised at all.

    `expect_status` EXISTS FOR THE PARALLEL PATH and defaults to the claim-based
    one, so the Celery worker is unchanged. A tenant may have only one
    IN_PROGRESS row — `AutoApprovalRunProduct_one_in_progress_per_tenant` is a
    unique index, not a convention — so scripts/run_from_sheet.py --workers
    leaves its rows QUEUED while it works and finishes them from there. What
    matters is that the update stays CONDITIONAL: whichever status it starts
    from, the first writer flips it and any second writer sees rowcount 0. The
    exactly-once property is preserved; only the starting state differs.
    """
    counter = {
        "VERIFIED": "verifiedCount",
        "HELD_FOR_HUMAN": "heldCount",
        "FAILED": "failedCount",
        "CANCELLED": "cancelledCount",
    }.get(verdict.status)

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE "AutoApprovalRunProduct"
                   SET status              = %(st)s::"VerificationStatus",
                       outcome             = %(oc)s,
                       reason              = %(rs)s,
                       error               = %(err)s,
                       "blockingRules"     = %(br)s::text[],
                       "preflightProblems" = %(pf)s::text[],
                       steps               = %(steps)s::jsonb,
                       deltas              = %(deltas)s::jsonb,
                       approved            = %(ap)s,
                       "completedAt"       = now(),
                       "durationMs"        = %(dur)s,
                       "leaseExpiresAt"    = NULL,
                       "updatedAt"         = now()
                 WHERE id = %(id)s::uuid
                   AND status = %(expect)s::"VerificationStatus"
                """,
                {
                    "id": row["id"],
                    "expect": expect_status,
                    "st": verdict.status,
                    "oc": verdict.outcome,
                    "rs": verdict.reason[:500],
                    "err": error,
                    "br": blocking_rules or [],
                    "pf": preflight or [],
                    "steps": Jsonb(steps) if steps is not None else None,
                    "deltas": Jsonb(deltas) if deltas is not None else None,
                    "ap": verdict.approved,
                    "dur": duration_ms,
                },
            )
            if cur.rowcount == 0:
                # The reaper got there first. Drop the result rather than
                # overwriting a row that now belongs to a future attempt: the
                # product may be verified twice, which is wasteful and never
                # incorrect.
                conn.rollback()
                log.warning(
                    "run_product=%s was reaped mid-verification — result discarded",
                    row["id"],
                )
                events.emit(
                    row["tenantId"],
                    "warn",
                    f"{row['productSku']}: lease expired mid-verification, "
                    f"result discarded and the product will be retried",
                    run_id=row["runId"],
                    product_id=row["productId"],
                )
                return

            # The product's denormalised cache. CANCELLED never lands here — a
            # withdrawn product carries no verdict and goes back to NOT_STARTED
            # so it is eligible again.
            if verdict.status == "CANCELLED":
                cur.execute(
                    """
                    UPDATE "Product"
                       SET "verificationStatus"  = 'NOT_STARTED'::"VerificationStatus",
                           "verificationOutcome" = NULL,
                           "verificationReason"  = NULL
                     WHERE id = %(p)s::uuid
                    """,
                    {"p": row["productId"]},
                )
            else:
                cur.execute(
                    """
                    UPDATE "Product"
                       SET "verificationStatus"    = %(st)s::"VerificationStatus",
                           "verificationOutcome"   = %(oc)s,
                           "verificationReason"    = %(rs)s,
                           "verificationRunId"     = %(run)s::uuid,
                           "verificationCheckedAt" = now()
                     WHERE id = %(p)s::uuid
                    """,
                    {
                        "p": row["productId"],
                        "st": verdict.status,
                        "oc": verdict.outcome,
                        "rs": verdict.reason[:500],
                        "run": row["runId"],
                    },
                )

            if counter:
                cur.execute(
                    f"""
                    UPDATE "AutoApprovalRun"
                       SET "{counter}"   = "{counter}" + 1,
                           "approvedCount" = "approvedCount" + %(ap)s
                     WHERE id = %(run)s::uuid
                    """,
                    {"run": row["runId"], "ap": 1 if verdict.approved else 0},
                )
        conn.commit()

    tag = {
        "VERIFIED": "pass",
        "HELD_FOR_HUMAN": "hold",
        "FAILED": "fail",
        "CANCELLED": "warn",
    }.get(verdict.status, "done")

    events.emit(
        row["tenantId"],
        tag,
        f"{row['productSku']} → {verdict.status.lower().replace('_', ' ')}"
        f" · {verdict.outcome}"
        + (f" · {round((duration_ms or 0) / 1000)}s" if duration_ms else ""),
        run_id=row["runId"],
        run_product_id=row["id"],
        product_id=row["productId"],
        detail={
            "outcome": verdict.outcome,
            "reason": verdict.reason,
            "blockingRules": blocking_rules or [],
            "preflightProblems": preflight or [],
            "approved": verdict.approved,
            "durationMs": duration_ms,
        },
    )


# ─── The two terminal rules the agent applies to every tenant ──────────────
#
# DEFAULTS, not policy. Both describe a product that cannot become approvable by
# re-running anything, so holding one for a human is a queue that never drains:
#
#   no care label      a photograph of a physical tag. Nothing generates one,
#                      and brand / size / material are all read off it.
#   no brand or size   after the care-label pass has already had its chance.
#
# A mannequin mismatch deliberately is NOT here: TAX.005 in rules/consistency.py
# already holds those, which is right — the record is sound and the photograph
# is a re-shoot, so archiving it would take a fixable product out of Review.
#
# These live here rather than in AutoApprovalConfig on purpose: they are the
# same for every tenant, so a column would be a switch nobody ever moves.

_NO_LABEL = "care label is missing"


def _archive(row: dict[str, Any], reason: str) -> bool:
    """Run vnyx-api's reject step. Returns False if it refused.

    The SAME script POST /products/bulk action=REJECT uses, so the stage
    machine's hooks fire and the product lands in Rejected exactly as a human
    rejection would. Publishing is not a risk: onApprovedArrival is the only
    hook that enqueues a Shopify upsert and a rejection never reaches it.
    """
    import importlib

    rp = importlib.import_module("scripts.repair_product")
    try:
        ok, out, _ = rp.run_step(
            None, "reject-products.ts",
            ["--product", str(row["productId"]), "--apply", "--reason", reason],
            timeout_s=120, quiet=True,
        )
    except Exception as exc:  # noqa: BLE001 — a refusal must not kill the pump
        log.warning("reject step failed for %s: %s", row["productSku"], exc)
        return False
    if not ok:
        log.warning("reject step returned non-zero for %s: %s",
                    row["productSku"], (out or "")[-200:])
    return bool(ok)


def _has_care_label(product_id: str) -> bool:
    r = db.fetch_one(
        """
        SELECT count(*)::int AS n FROM "ProductMedia"
         WHERE "productId" = %(p)s::uuid AND view = 'LABEL'
           AND "isCurrent" = true AND "deletedAt" IS NULL
           AND "mediaType" = 'IMAGE'
        """,
        {"p": product_id},
    )
    return bool(r and r["n"])


def _missing_attrs(product_id: str) -> list[str]:
    """Which of brand / size the product still lacks. Placeholders count.

    BOAS and Klekt do not leave these empty — they store the literal string
    "Unknown", so a NULL test reports a perfectly good value and every product
    this rule exists for slips through. product_audit owns that placeholder set
    and `read_property` owns the alias scan (this tenant stores `Brand`, not
    `brand`); both are imported rather than restated so they cannot drift.
    """
    from app.product_audit import PLACEHOLDERS, read_property

    r = db.fetch_one(
        'SELECT p.properties, p."internationalSize" AS size'
        ' FROM "Product" p WHERE p.id = %(p)s::uuid',
        {"p": product_id},
    ) or {}
    props = r.get("properties") if isinstance(r.get("properties"), dict) else {}

    def blank(*values: Any) -> bool:
        return not any(
            v is not None and str(v).strip().lower() not in PLACEHOLDERS
            for v in values
        )

    missing = []
    if blank(read_property(props, "brand")):
        missing.append("brand")
    if blank(r.get("size"), read_property(props, "international_size")):
        missing.append("size")
    return missing


def verify_one(row: dict[str, Any], rs: cfgmod.RunSettings, *,
               ignore_stop: bool = False,
               allow_stage: list[str] | None = None,
               expect_status: str = "IN_PROGRESS",
               silent: bool = False) -> str | None:
    """One claimed product, start to finish. Never raises.

    RETURNS None, or one sentence saying why the RUN must stop — the canary
    (a repair write triggered paid regeneration). The product itself is always
    finished or released before that is returned; the caller's only job is to
    take no more work. Every caller may ignore the value and lose nothing but
    the stop.

    `ignore_stop` skips the `enabled` re-read described at the approve decision
    below. DEFAULT FALSE, so the Celery path is untouched — the only caller that
    passes it is scripts/run_from_sheet.py, where a person has typed --approve
    and then confirmed at a prompt.

    `allow_stage` widens which stages the approval pre-flight will move a
    product FROM, and is likewise passed by that one caller. Also default-None,
    so the unattended agent keeps approving only out of REVIEW: a stage flag
    that lags the work is a judgement about a specific batch someone is
    watching, not a rule the background loop should apply to the catalogue.

    `expect_status` is the status the row is expected to still hold when the
    verdict is written — IN_PROGRESS for the claim-based path, QUEUED for
    --workers, which cannot use IN_PROGRESS because only one row per tenant may
    hold it. See _finish.
    """
    repair = _load_repair()

    tenant_id = str(row["tenantId"])
    product_id = str(row["productId"])

    events.emit(
        tenant_id,
        "check",
        f"{row['productSku']} · {(row.get('productTitle') or '')[:60]}"
        f" — verification started",
        run_id=row["runId"],
        run_product_id=row["id"],
        product_id=product_id,
    )

    # Cross-tenant refusal. Should never fire — every queue row was written by
    # vnyx-api after resolveTenantScope — but the worker's database access is
    # unscoped, so this turns the one thing that could go wrong into a recorded
    # refusal instead of a silent write.
    with db.connection(read_only=True) as conn:
        with conn.cursor() as cur:
            if not db.assert_tenant(cur, row["id"], tenant_id, product_id):
                _finish(
                    row,
                    Verdict(
                        "CANCELLED",
                        "TENANT_MISMATCH",
                        "The queue row's tenant does not match the product's. Refused.",
                        False,
                        False,
                    ),
                    expect_status=expect_status,
                )
                return

    events.emit(
        tenant_id,
        "queue",
        f"mode {rs.mode}"
        f" · {'writes on' if rs.apply else 'writes off (shadow)'}"
        f" · evidence {'on' if rs.use_llm else 'off'}"
        f" · min confidence {rs.min_extraction_confidence}"
        f"{' · renders skipped' if rs.skip_render else ''}",
        run_id=row["runId"],
        run_product_id=row["id"],
        product_id=product_id,
    )

    # THE STOP GUARD, and it is preventive rather than observational.
    #
    # `approve` is decided HERE, before repair() starts, because repair() cannot
    # be told to change its mind halfway. A stop that has already landed
    # therefore prevents the approval outright; one that lands during repair()
    # may still let this single product through, which is what the stop
    # response tells the operator rather than promising more than it can.
    #
    # WHAT `enabled` ACTUALLY GUARDS, and why one caller may skip it.
    #
    # It is read fresh rather than taken from the run's snapshot because it is
    # the STOP button: pressing Stop has to take effect on the next product, not
    # at the end of the run. That reasoning is about the UNATTENDED loop — beat
    # ticks, the sweep dispatches a pump, and nobody is watching. The flag is
    # the only way a human can interrupt it.
    #
    # It is not the right gate for a person running one named product from a
    # terminal with --approve, who has already confirmed at a prompt. There the
    # instruction IS the human decision the flag stands in for, and requiring a
    # tenant to be armed as well means arming the background agent just to
    # approve a single product by hand — which is strictly more dangerous than
    # the thing it was protecting against.
    #
    # So the opt-out is explicit, defaults off, and is passed by exactly one
    # caller. The Celery path never sets it.
    approve = rs.approve and (ignore_stop or cfgmod.is_enabled(tenant_id))

    t0 = time.monotonic()

    # NO CARE LABEL — decided BEFORE the chain, because the chain cannot help.
    # A care label is a photograph of a physical tag: nothing generates one, the
    # extract step has nothing to read, and brand / size / material all come off
    # it. Running the chain anyway costs ~2 minutes and a vision call to reach a
    # conclusion one count already gave.
    #
    # Only when the repairs are allowed to write. A shadow pass must not archive
    # a product; it records what it would have done and stops there.
    if rs.apply and not _has_care_label(product_id):
        events.emit(tenant_id, "warn",
                    f"{row['productSku']} — {_NO_LABEL}, rejecting",
                    run_id=row["runId"], run_product_id=row["id"],
                    product_id=product_id)
        if _archive(row, _NO_LABEL):
            _finish(
                row,
                Verdict("FAILED", "NO_CARE_LABEL", _NO_LABEL, False, False),
                duration_ms=int((time.monotonic() - t0) * 1000),
                expect_status=expect_status,
            )
            return
        # The reject step refused. Fall through and verify it normally rather
        # than leave the row unfinished — a held product is recoverable, a
        # stranded one is not.

    try:
        result = repair(
            db.dsn(),
            product_id,
            apply=rs.apply,
            vnyx_api=_vnyx_api_dir(),
            infer=rs.infer_attributes,
            min_confidence=rs.min_extraction_confidence,
            skip_render=rs.skip_render,
            approve=approve,
            skip_bin=rs.skip_bin_placement,
            # Frozen in the run's configSnapshot, so a mid-run Brain edit cannot
            # change how the products still queued are judged.
            severity_overrides=rs.severity_overrides,
            allow_stage=allow_stage,
            # Several products narrate at once under --workers, and interleaved
            # line-by-line output reads as one product doing the wrong steps in
            # the wrong order. The caller prints a block per product instead.
            silent=silent,
            quiet=True,
        )
    except product_audit.ProductNotFound:
        _finish(
            row,
            Verdict("CANCELLED", "PRODUCT_DELETED", "The product no longer exists.", False, False),
            duration_ms=int((time.monotonic() - t0) * 1000),
            expect_status=expect_status,
        )
        return
    except BaseException as exc:  # noqa: BLE001
        # BaseException on purpose: Celery's SoftTimeLimitExceeded derives from
        # it, and letting that escape would strand the row IN_PROGRESS until the
        # reaper noticed instead of releasing the lease now.
        name = type(exc).__name__
        retryable = name in {
            "SoftTimeLimitExceeded",
            "StepFailed",
            "OperationalError",
            "InterfaceError",
            "TimeoutExpired",
        }
        duration = int((time.monotonic() - t0) * 1000)

        if retryable and row["attempts"] < row["maxAttempts"]:
            claim.release_lease(
                str(row["id"]),
                backoff_seconds=claim.backoff_seconds(int(row["attempts"])),
            )
            events.emit(
                tenant_id,
                "warn",
                f"{row['productSku']}: {name} — retrying "
                f"(attempt {row['attempts']} of {row['maxAttempts']})",
                run_id=row["runId"],
                run_product_id=row["id"],
                product_id=product_id,
                detail={"error": str(exc)[:500]},
            )
            return

        _finish(
            row,
            Verdict(
                "FAILED",
                "TIMEOUT" if name == "SoftTimeLimitExceeded" else "STEP_FAILED",
                f"{name}: {exc}"[:500],
                False,
                False,
            ),
            error=str(exc)[:2000],
            duration_ms=duration,
            expect_status=expect_status,
        )
        return

    duration = int((time.monotonic() - t0) * 1000)
    steps = list(result.get("steps") or [])

    # One line per step, from repair()'s own record. This is the audit trail and
    # it costs nothing to produce.
    for step in steps:
        tag, message = events.step_line(step)
        events.emit(
            tenant_id,
            tag,
            message,
            run_id=row["runId"],
            run_product_id=row["id"],
            product_id=product_id,
            detail=step,
        )

    verdict = classify(result)

    # THE CANARY. Every step above writes through vnyx-api's own update path,
    # and that path has hooks: a change to certain fields can re-queue the
    # product for ANALYSIS, which regenerates its images at a paid call per
    # view. Nothing in this chain intends that. So repair() reads the product's
    # generationStatus before and after, and a flip to GENERATING that the
    # render step did not cause means a repair write is triggering paid
    # regeneration — for this product and, unchecked, for every one behind it.
    # This product is finished normally (the flip is on its row); the RUN
    # stops, and the reason is what this function returns.
    gen = result.get("generation") or {}
    abort: str | None = None
    if gen.get("regeneration_triggered"):
        abort = (
            f"canary: {row['productSku']} flipped {gen.get('before')} → "
            f"{gen.get('after')} during the chain without the render step "
            f"running — a repair write is triggering paid regeneration. No "
            f"further product was started."
        )
        events.emit(tenant_id, "fail", abort, run_id=row["runId"],
                    run_product_id=row["id"], product_id=product_id, detail=gen)

    # NOT JUDGED, BECAUSE THE JUDGE WAS ABSENT. `vision_unavailable` names the
    # steps — care label, gate — whose provider read failed at the API rather
    # than returning an answer. A hold reached that way says nothing about the
    # product, so it is put back with a backoff like any other retryable
    # failure, and only once the attempts are spent is it held, under a code
    # that names the provider rather than the product. See app/llm/health.py.
    unavailable = list(result.get("vision_unavailable") or [])
    if unavailable and verdict.status == "HELD_FOR_HUMAN":
        where = ", ".join(unavailable)
        if row["attempts"] < row["maxAttempts"]:
            claim.release_lease(
                str(row["id"]),
                backoff_seconds=claim.backoff_seconds(int(row["attempts"])),
            )
            events.emit(
                tenant_id, "warn",
                f"{row['productSku']}: vision provider unavailable during {where} "
                f"— not judged, retrying (attempt {row['attempts']} of "
                f"{row['maxAttempts']})",
                run_id=row["runId"], run_product_id=row["id"],
                product_id=product_id,
                detail={"vision_unavailable": unavailable, "health": health.snapshot()},
            )
            return abort
        verdict = Verdict(
            "HELD_FOR_HUMAN", "VISION_UNAVAILABLE",
            f"The vision provider was unavailable during {where} on every "
            f"attempt ({row['maxAttempts']}), so the product was not judged. "
            f"Re-run once the provider is back.",
            False, False,
        )

    # NO BRAND OR SIZE — decided AFTER the chain, and that ordering is the whole
    # point. Both are read off the care label by the extract and care-label
    # steps, so asking first would reject products the pipeline was about to
    # fix. Asking now means the label has had its chance and the answer is
    # settled: nothing further will produce them.
    #
    # Only converts a HOLD. A product that VERIFIED is approvable and must not
    # be archived over a field the pre-flight was content with; a FAILED one
    # already has its terminal verdict.
    #
    # AND NEVER WHEN THE READER WAS DOWN. During a provider outage every label
    # is "not readable", and this rule would archive the queue product by
    # product — the exact incident the outage guard exists for. A product on
    # `vision_unavailable` has not had its chance; it is held above, not
    # rejected here.
    if (rs.apply and verdict.status == "HELD_FOR_HUMAN" and not unavailable
            and (missing := _missing_attrs(product_id))):
        why = f"{' and '.join(missing)} missing — not readable from the care label"
        events.emit(tenant_id, "warn", f"{row['productSku']} — {why}, rejecting",
                    run_id=row["runId"], run_product_id=row["id"],
                    product_id=product_id)
        if _archive(row, why):
            _finish(
                row,
                Verdict("FAILED", "NO_BRAND_OR_SIZE", why, False, False),
                blocking_rules=list(result.get("remaining") or []),
                steps=steps,
                duration_ms=duration,
                expect_status=expect_status,
            )
            return abort

    _finish(
        row,
        verdict,
        blocking_rules=list(result.get("remaining") or []),
        preflight=list((result.get("approval") or {}).get("blockers") or []),
        steps=steps,
        deltas={
            "before": result.get("before"),
            "after": result.get("after"),
            "stageBefore": result.get("stage_before"),
            "stageAfter": result.get("stage_after"),
            "gate": result.get("gate"),
            "photos": result.get("photos"),
            "generation": gen or None,
        },
        duration_ms=duration,
        expect_status=expect_status,
    )
    return abort


def stop_reason(abort: str | None = None) -> str | None:
    """Should the loop take another product? None if yes, else why not.

    The two run-level stops in one place, so the Celery pump and the sheet
    driver cannot disagree about them: the canary (passed in, from verify_one)
    and the vision outage (asked of app/llm/health.py). Call BETWEEN products.
    """
    if abort:
        return abort
    try:
        health.check()
    except health.VisionOutage as exc:
        return f"vision outage — {exc}"
    return None


def pump(tenant_id: str) -> dict[str, Any]:
    """Drain one tenant's queue, one product at a time.

    The loop, not a single claim per tick: a 4,000-product run would otherwise
    take 4,000 minutes of waiting rather than 4,000 products of working. Bounded
    by PUMP_BUDGET_S so it yields the worker slot and Celery's soft time limit
    never fires mid-product.
    """
    started = time.monotonic()
    processed = 0
    reason = "empty"

    while True:
        if not cfgmod.is_enabled(tenant_id):
            reason = "stopped"
            break

        # A tenant the canary stopped stays stopped until the pause passes —
        # otherwise this tick would spend another product's renders finding
        # the same thing. Checked before claiming so no row is taken for it.
        until, why = _stopped.get(tenant_id, (0.0, ""))
        if until > time.monotonic():
            log.warning("tenant=%s stopped by canary for another %ds: %s",
                        tenant_id, int(until - time.monotonic()), why)
            reason = "canary"
            break
        _stopped.pop(tenant_id, None)

        # And a provider still in its outage cooldown means nothing would be
        # judged, so nothing is claimed. Silent here — the event was written
        # when the outage was detected — and check() clears the window itself
        # once the cooldown is over.
        pre_stop = stop_reason()
        if pre_stop:
            log.warning("tenant=%s not pumping: %s", tenant_id, pre_stop)
            reason = "vision outage"
            break

        cfg_row = cfgmod.load_config(tenant_id)
        if cfg_row is None:
            reason = "no config"
            break

        # The window governs the SCHEDULED trigger. A manual run someone
        # explicitly asked for is drained regardless — that is what "Run now"
        # means.
        if (
            cfg_row["scheduleEnabled"]
            and not in_window(window_from_row(cfg_row), _utcnow())
            and not cfgmod.has_open_manual_run(tenant_id)
        ):
            reason = "outside window"
            break

        row = claim.claim_next(
            tenant_id,
            int(cfg_row["leaseSeconds"]),
            bool(cfg_row["includeFailedGeneration"]),
        )
        if row is None:
            reason = "empty or busy"
            break

        run = db.fetch_one(
            'SELECT "configSnapshot" FROM "AutoApprovalRun" WHERE id = %(r)s::uuid',
            {"r": row["runId"]},
        )
        rs = cfgmod.settings_from_snapshot(
            run and run["configSnapshot"], int(cfg_row["maxAttempts"])
        )

        abort = verify_one(row, rs)
        processed += 1

        claim.close_finished_runs()

        # THE TWO RUN-LEVEL STOPS, asked between products and never inside one.
        stop = stop_reason(abort)
        if stop:
            if abort:
                _stopped[tenant_id] = (time.monotonic() + CANARY_PAUSE_S, abort)
                reason = "canary"
            else:
                # The outage's own event, once, on the run it interrupted. The
                # cooldown check at the top of the loop is silent after this.
                events.emit(tenant_id, "fail", stop, run_id=row["runId"],
                            detail={"health": health.snapshot()})
                reason = "vision outage"
            log.error("tenant=%s run stopped: %s", tenant_id, stop)
            break

        if time.monotonic() - started > PUMP_BUDGET_S:
            reason = "budget"
            break

    if processed:
        log.info("tenant=%s processed=%d stop=%s", tenant_id, processed, reason)
    return {"tenantId": tenant_id, "processed": processed, "stopped": reason}


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
