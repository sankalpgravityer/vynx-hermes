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
) -> None:
    """Write the terminal status, the product's cache and the run counters.

    ONE TRANSACTION, and the run-product UPDATE is conditional on IN_PROGRESS —
    so a row the lease reaper already took back is not resurrected, and the
    counters cannot be incremented twice for one product. That conditional is
    the whole reason the counters can be denormalised at all.
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
                   AND status = 'IN_PROGRESS'::"VerificationStatus"
                """,
                {
                    "id": row["id"],
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


def verify_one(row: dict[str, Any], rs: cfgmod.RunSettings) -> None:
    """One claimed product, start to finish. Never raises."""
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
    approve = rs.approve and cfgmod.is_enabled(tenant_id)

    t0 = time.monotonic()
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
            quiet=True,
        )
    except product_audit.ProductNotFound:
        _finish(
            row,
            Verdict("CANCELLED", "PRODUCT_DELETED", "The product no longer exists.", False, False),
            duration_ms=int((time.monotonic() - t0) * 1000),
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
        },
        duration_ms=duration,
    )


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

        verify_one(row, rs)
        processed += 1

        claim.close_finished_runs()

        if time.monotonic() - started > PUMP_BUDGET_S:
            reason = "budget"
            break

    if processed:
        log.info("tenant=%s processed=%d stop=%s", tenant_id, processed, reason)
    return {"tenantId": tenant_id, "processed": processed, "stopped": reason}


def _utcnow():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc)
