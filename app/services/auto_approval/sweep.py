"""The 60-second tick: evaluate windows, dispatch pumps, reap, tidy.

Deliberately does NO verification itself. It is short — a handful of indexed
queries — so it never blocks the single worker slot behind a 21-minute product.
The pumps it dispatches are separate tasks that queue behind whatever is running.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.services.auto_approval import claim, config as cfgmod, db, events, preflight
from app.services.auto_approval.schedule import (
    hhmm,
    in_window,
    window_from_row,
)

log = logging.getLogger("auto-approval.sweep")


def generation_statuses(include_failed: bool) -> list[str]:
    """Which generationStatus values are eligible.

    THE POINT OF THE WHOLE CHECK: deriveStage never looks at this column, so a
    product sits at stage REVIEW while its renders are still being produced --
    the review screen calls those "Generating" rather than "To Review".
    Verifying one would repair images the pipeline is seconds from producing:
    double the credits, duplicate ProductMedia rows, and a race over what the
    gallery shows.

    GENERATING and IDLE are therefore never eligible, and not configurable.
    FAILED is the tenant's choice, defaulting off.
    """
    return ["COMPLETE", "FAILED"] if include_failed else ["COMPLETE"]

# Checked once per process, not per tick — the answer cannot change without a
# redeploy, and it costs filesystem stats.
_preflight_cache: dict[str, Any] | None = None


def preflight_result() -> dict[str, Any]:
    global _preflight_cache
    if _preflight_cache is None:
        _preflight_cache = preflight.check()
        if not _preflight_cache["ok"]:
            log.error(
                "worker preflight FAILED — refusing to pump: %s",
                "; ".join(_preflight_cache["problems"]),
            )
        else:
            # Named at boot, once. A worker pointed at the wrong database is
            # otherwise indistinguishable from one pointed at the right one
            # until it writes something.
            log.info(
                "worker preflight ok — database %s, vnyx-api %s",
                _preflight_cache.get("database", "?"),
                _preflight_cache.get("vnyxApiDir", "?"),
            )
    return _preflight_cache


def _create_scheduled_run(
    tenant_id: str, cfg_row: dict[str, Any], source: str = "SCHEDULED"
) -> str:
    """Open a run with a frozen snapshot.

    The snapshot is built HERE rather than fetched from vnyx-api because the
    worker is the thing that opens the run — there is no HTTP request to hang it
    off. It mirrors config.ts::snapshotConfig's shape; the Hermes-side fields it
    cannot know (policy mtime) are read from the local policy file.

    `source` is a parameter because the arrival catch-up below needs an
    ON_ARRIVAL run, and its products must land in the SAME run the arrival hook
    uses — otherwise one arrival session would be split across two runs in the
    Runs tab for no reason the user could see.
    """
    from app.config import policy, settings as hermes_settings

    try:
        mtime = hermes_settings().policy_path.stat().st_mtime
        policy_mtime = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
    except OSError:
        policy_mtime = None

    snapshot = {
        "mode": cfg_row["mode"],
        "shadowWritesRepairs": cfg_row["shadowWritesRepairs"],
        "brain": {
            "ruleGroups": cfg_row["ruleGroups"] or [],
            "severityOverrides": cfg_row["severityOverrides"],
            "useLlm": cfg_row["useLlm"],
            "readCareLabel": cfg_row["readCareLabel"],
            "minExtractionConfidence": cfg_row["minExtractionConfidence"],
            "inferAttributes": cfg_row["inferAttributes"],
            "skipRender": cfg_row["skipRender"],
        },
        "execution": {
            "skipBinPlacement": cfg_row["skipBinPlacement"],
            "maxAttempts": cfg_row["maxAttempts"],
            "leaseSeconds": cfg_row["leaseSeconds"],
        },
        "schedule": {
            "enabled": cfg_row["scheduleEnabled"],
            "start": hhmm(cfg_row["scheduleStartMinute"]),
            "end": hhmm(cfg_row["scheduleEndMinute"]),
            "timezone": cfg_row["scheduleTimezone"],
            "days": cfg_row["scheduleDays"],
        },
        "hermes": {
            "policyMtime": policy_mtime,
            "model": (policy().get("llm") or {}).get("model_reasoning"),
            "blockingFloor": "high",
        },
        "openedBy": "hermes-sweep",
        "snapshotAt": datetime.now(timezone.utc).isoformat(),
    }

    row = db.write_returning(
        """
        INSERT INTO "AutoApprovalRun" ("tenantId", source, "configSnapshot")
        VALUES (%(t)s::uuid, %(src)s::"AutoApprovalRunSource", %(s)s::jsonb)
        RETURNING id
        """,
        {"t": tenant_id, "src": source, "s": Jsonb(snapshot)},
    )
    return str(row["id"])


# How far back the arrival catch-up below will look. Bounds it to products that
# arrived recently enough that the arrival hook SHOULD have caught them, which is
# what keeps it from sweeping in a pre-existing Review backlog — the thing
# `startOnArrival` deliberately does not do.
ARRIVAL_CATCHUP_HOURS = 24


def _catch_up_arrivals(
    tenant_id: str, cfg_row: dict[str, Any]
) -> tuple[int, str | None]:
    """Queue recent Review arrivals the arrival hook had to skip.

    THE HOLE THIS FILLS. `onReviewArrivalAutoApproval` applies the full
    eligibility predicate, which excludes a product whose renders are still being
    produced — correct, because verifying a half-generated product wastes the
    check. But a product enters REVIEW roughly two seconds after it is created,
    while its five renders take minutes, so the hook sees GENERATING and returns.
    Nothing then re-fires it when generation completes, and the product sits at
    NOT_STARTED forever: eligible on every measure, queued by nobody.

    It is not a rare edge. Five of the fourteen products in one tenant's Review
    section were stranded this way, and it reads to the user as "the agent only
    picks up one product of each pair" — because the split-gender worker sets
    generationStatus COMPLETE on the COPY before it enters Review, so the copy
    passes the gate and the original never does.

    The alternative fix — calling the arrival hook from wherever generationStatus
    becomes COMPLETE — touches nine call sites across three workers and two
    routes, and would silently stop working the tenth time someone writes that
    column. This is one place, in the loop that already runs every 60 seconds,
    and it is self-healing: whatever the reason a product became eligible without
    being queued, the next tick queues it.

    BOUNDED BY `currentStageAt`, not open-ended. The plan is explicit that
    enabling the agent must not sweep up everything already sitting in Review
    (§13.3); a 24-hour horizon catches the arrival it missed while leaving that
    promise intact.

    Returns (queued, run_id).
    """
    pending = db.fetch_one(
        """
        SELECT count(*)::int AS n
          FROM "Product" p
         WHERE p."tenantId" = %(t)s::uuid
           AND p."currentStage" = 'REVIEW'::"ProductStage"
           AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
           AND p."isDeleted" = false
           AND p."isArchived" = false
           AND p."generationStatus" = ANY(%(gen)s::"ProductGenerationStatus"[])
           AND p."isRegenerating" = false
           AND p."reviewStatus" = ANY(ARRAY['PENDING','PENDING_DECISON']::"ProductReviewStatus"[])
           AND (p."decisionApprovalStatus" IS NULL
                OR p."decisionApprovalStatus" <> 'REJECTED'::"DecisionApprovalStatus")
           AND p."currentStageAt" >= now() - (%(hours)s * interval '1 hour')
        """,
        {
            "t": tenant_id,
            "gen": generation_statuses(bool(cfg_row["includeFailedGeneration"])),
            "hours": ARRIVAL_CATCHUP_HOURS,
        },
    )
    if not pending or not int(pending["n"]):
        return 0, None

    # The arrival hook's run, so a caught-up product is indistinguishable from
    # one queued on time. Created only once there is something to put in it —
    # the same reason the scheduled path counts before creating.
    open_run = cfgmod.find_open_run(tenant_id, "ON_ARRIVAL")
    run_id = (
        str(open_run["id"])
        if open_run
        else _create_scheduled_run(tenant_id, cfg_row, source="ON_ARRIVAL")
    )

    n = _enqueue_recent_arrivals(
        tenant_id,
        run_id,
        int(cfg_row["maxAttempts"]),
        bool(cfg_row["includeFailedGeneration"]),
    )
    return n, run_id


def _enqueue_recent_arrivals(
    tenant_id: str, run_id: str, max_attempts: int, include_failed: bool
) -> int:
    """The catch-up's INSERT. Same shape as `_enqueue_eligible`, plus the horizon.

    Kept separate rather than adding a parameter to `_enqueue_eligible`: that
    query's WHERE clause is written verbatim to match `Product_review_eligible_idx`,
    and a conditionally-appended predicate is exactly the kind of edit that
    silently drops a partial index. This one accepts a filter on `currentStageAt`
    after the index has already narrowed the set.

    Returns how many rows this call inserted — not the run's total, which is what
    `_enqueue_eligible` returns for its own (different) caller.
    """
    with db.connection() as conn:
        # row_factory=dict_row explicitly: `db.connection()` hands back
        # psycopg's DEFAULT tuple factory, and only `db.cursor()` applies
        # dict_row. `_enqueue_eligible` above opens a bare cursor too and gets
        # away with it because it discards the returned row and re-counts with
        # `db.fetch_one`; this function reads its result by name, so it has to
        # ask for names.
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH inserted AS (
                  INSERT INTO "AutoApprovalRunProduct"
                    ("runId", "tenantId", "productId", "productSku", "productTitle",
                     status, priority, "availableAt", "maxAttempts", "updatedAt")
                  SELECT %(run)s::uuid, p."tenantId", p.id, p.sku, p.title,
                         'QUEUED'::"VerificationStatus", 10, now(), %(max)s, now()
                    FROM "Product" p
                   WHERE p."tenantId" = %(t)s::uuid
                     AND p."currentStage" = 'REVIEW'::"ProductStage"
                     AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
                     AND p."isDeleted" = false
                     AND p."isArchived" = false
                     AND p."generationStatus" = ANY(%(gen)s::"ProductGenerationStatus"[])
                     AND p."isRegenerating" = false
                     AND p."reviewStatus" = ANY(ARRAY['PENDING','PENDING_DECISON']::"ProductReviewStatus"[])
                     AND (p."decisionApprovalStatus" IS NULL
                          OR p."decisionApprovalStatus" <> 'REJECTED'::"DecisionApprovalStatus")
                     AND p."currentStageAt" >= now() - (%(hours)s * interval '1 hour')
                   ORDER BY p."currentStageAt" ASC
                  ON CONFLICT DO NOTHING
                  RETURNING "productId"
                ), flipped AS (
                  UPDATE "Product" p
                     SET "verificationStatus" = 'QUEUED'::"VerificationStatus",
                         "verificationOutcome" = NULL,
                         "verificationReason" = NULL
                    FROM inserted i
                   WHERE p.id = i."productId"
                     AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
                  RETURNING p.id
                ), bumped AS (
                  UPDATE "AutoApprovalRun"
                     SET "totalProducts" = "totalProducts" + (SELECT count(*) FROM inserted)
                   WHERE id = %(run)s::uuid
                  RETURNING id
                )
                SELECT count(*)::int AS n FROM inserted
                """,
                {
                    "run": run_id,
                    "t": tenant_id,
                    "max": max_attempts,
                    "gen": generation_statuses(include_failed),
                    "hours": ARRIVAL_CATCHUP_HOURS,
                },
            )
            row = cur.fetchone()
        conn.commit()
    return int(row["n"]) if row else 0


def _count_eligible(tenant_id: str, include_failed: bool) -> int:
    """How many products a scheduled run would have to work on.

    Asked BEFORE a run is created, so an empty window creates nothing at all —
    see the comment at the call site. The predicate is written in the same exact
    form as the enqueue below, so both are served by
    `Product_review_eligible_idx`: Postgres only uses a partial index when the
    query repeats its predicate verbatim.
    """
    row = db.fetch_one(
        """
        SELECT count(*)::int AS n
          FROM "Product" p
         WHERE p."tenantId" = %(t)s::uuid
           AND p."currentStage" = 'REVIEW'::"ProductStage"
           AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
           AND p."isDeleted" = false
           AND p."isArchived" = false
           AND p."generationStatus" = ANY(%(gen)s::"ProductGenerationStatus"[])
           AND p."isRegenerating" = false
           AND p."reviewStatus" = ANY(ARRAY['PENDING','PENDING_DECISON']::"ProductReviewStatus"[])
           AND (p."decisionApprovalStatus" IS NULL
                OR p."decisionApprovalStatus" <> 'REJECTED'::"DecisionApprovalStatus")
        """,
        {"t": tenant_id, "gen": generation_statuses(include_failed)},
    )
    return int(row["n"]) if row else 0


def _enqueue_eligible(
    tenant_id: str, run_id: str, max_attempts: int, include_failed: bool
) -> int:
    """Queue every eligible Review product for a scheduled run.

    The WHERE clause is spelled out in this exact form on purpose: the hot path
    is served by `Product_review_eligible_idx`, a PARTIAL index, and Postgres
    only uses one when the query's predicate matches verbatim. Narrowing or
    reordering these four conditions silently drops to a sequential scan on the
    largest table in the schema.

    INSERT ... SELECT rather than a loop: one statement for the whole sweep
    instead of one round trip per product.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH inserted AS (
                  INSERT INTO "AutoApprovalRunProduct"
                    ("runId", "tenantId", "productId", "productSku", "productTitle",
                     status, priority, "availableAt", "maxAttempts", "updatedAt")
                  SELECT %(run)s::uuid, p."tenantId", p.id, p.sku, p.title,
                         'QUEUED'::"VerificationStatus", 0, now(), %(max)s, now()
                    FROM "Product" p
                   WHERE p."tenantId" = %(t)s::uuid
                     AND p."currentStage" = 'REVIEW'::"ProductStage"
                     AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
                     AND p."isDeleted" = false
                     AND p."isArchived" = false
                     AND p."generationStatus" = ANY(%(gen)s::"ProductGenerationStatus"[])
                     AND p."isRegenerating" = false
                     AND p."reviewStatus" = ANY(ARRAY['PENDING','PENDING_DECISON']::"ProductReviewStatus"[])
                     AND (p."decisionApprovalStatus" IS NULL
                          OR p."decisionApprovalStatus" <> 'REJECTED'::"DecisionApprovalStatus")
                   ORDER BY p."currentStageAt" ASC
                  ON CONFLICT DO NOTHING
                  RETURNING "productId"
                ), flipped AS (
                  UPDATE "Product" p
                     SET "verificationStatus" = 'QUEUED'::"VerificationStatus",
                         "verificationOutcome" = NULL,
                         "verificationReason" = NULL
                    FROM inserted i
                   WHERE p.id = i."productId"
                     AND p."verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
                  RETURNING p.id
                )
                UPDATE "AutoApprovalRun"
                   SET "totalProducts" = "totalProducts" + (SELECT count(*) FROM inserted)
                 WHERE id = %(run)s::uuid
                RETURNING "totalProducts"
                """,
                {
                    "run": run_id,
                    "t": tenant_id,
                    "max": max_attempts,
                    "gen": generation_statuses(include_failed),
                },
            )
            row = cur.fetchone()
        conn.commit()

    counted = db.fetch_one(
        """
        SELECT count(*)::int AS n FROM "AutoApprovalRunProduct"
         WHERE "runId" = %(r)s::uuid
        """,
        {"r": run_id},
    )
    return int(counted["n"]) if counted else 0


def _stop_for_window(tenant_id: str, reason: str) -> int:
    """Release the queue when the window closes, WITHOUT disabling the agent.

    A closing window is not the operator switching the agent off, and tomorrow's
    window has to open by itself — so `enabled` is left alone. The IN_PROGRESS
    row is untouched: that product finishes, because nothing in repair()'s chain
    can be safely interrupted.
    """
    released = db.execute(
        """
        WITH released AS (
          UPDATE "AutoApprovalRunProduct"
             SET status        = 'CANCELLED'::"VerificationStatus",
                 outcome       = 'WINDOW_CLOSED',
                 reason        = %(reason)s,
                 "completedAt" = now(),
                 "leaseExpiresAt" = NULL,
                 "updatedAt"   = now()
           WHERE "tenantId" = %(t)s::uuid
             AND status = 'QUEUED'::"VerificationStatus"
          RETURNING "productId", "runId"
        ), bumped AS (
          UPDATE "AutoApprovalRun" r
             SET "cancelledCount" = r."cancelledCount" + 1
            FROM released x WHERE r.id = x."runId"
          RETURNING 1
        )
        UPDATE "Product" p
           SET "verificationStatus"  = 'NOT_STARTED'::"VerificationStatus",
               "verificationOutcome" = NULL,
               "verificationReason"  = NULL
          FROM released x
         WHERE p.id = x."productId"
           AND p."verificationStatus" = 'QUEUED'::"VerificationStatus"
        """,
        {"t": tenant_id, "reason": reason},
    )

    # Close the scheduled run, unless its product is still running — the
    # completion check picks that up when it lands.
    db.execute(
        """
        UPDATE "AutoApprovalRun" r
           SET status = 'CANCELLED'::"AutoApprovalRunStatus",
               "completedAt" = now(),
               "completionReason" = %(reason)s
         WHERE r."tenantId" = %(t)s::uuid
           AND r.status = 'RUNNING'::"AutoApprovalRunStatus"
           AND r.source = 'SCHEDULED'::"AutoApprovalRunSource"
           AND NOT EXISTS (
             SELECT 1 FROM "AutoApprovalRunProduct" q
              WHERE q."runId" = r.id
                AND q.status = 'IN_PROGRESS'::"VerificationStatus"
           )
        """,
        {"t": tenant_id, "reason": reason},
    )
    return released


def sweep() -> dict[str, Any]:
    """One tick. Returns a summary for the Celery log."""
    pf = preflight_result()
    now = datetime.now(timezone.utc)

    configs = cfgmod.enabled_configs()
    dispatched: list[str] = []

    # BEFORE the dispatch decisions below, so a run the premature close had
    # ended gets its pump on THIS tick: has_open_manual_run() only sees RUNNING
    # runs, and a queued product under a COMPLETED run is otherwise stranded.
    reopened = claim.reopen_orphaned_runs()

    for cfg_row in configs:
        tenant_id = str(cfg_row["tenantId"])

        # Heartbeat first, so the config screen shows the worker as alive even
        # when its host is misconfigured — "the worker is running but cannot
        # work" is a different message from "the worker is down".
        cfgmod.touch_heartbeat(tenant_id, pf)

        if not pf["ok"]:
            events.emit(
                tenant_id,
                "warn",
                "Worker cannot run verifications: "
                + "; ".join(pf["problems"])[:600],
                detail=pf,
            )
            continue

        window = window_from_row(cfg_row)
        open_now = in_window(window, now)
        scheduled_run = cfgmod.find_open_run(tenant_id, "SCHEDULED")

        # ---- window edges -------------------------------------------------
        #
        # "Was open / was closed" needs no stored state: derive it from whether
        # a RUNNING SCHEDULED run exists. Run existence IS the edge detector —
        # it survives a restart for free and makes a doubled beat tick harmless
        # rather than merely unlikely.
        #
        # COUNT BEFORE CREATING, and this ordering is the fix for a real defect:
        # creating the run first and closing it when it turned out to be empty
        # produced one COMPLETED run PER TICK — a 0-product run every 60 seconds
        # for as long as the window stayed open, because the next tick then saw
        # no RUNNING run and made another. Eight of them appeared within eight
        # minutes of the first test.
        #
        # A run is a record that work happened. No eligible products means no
        # work, so there is nothing to record and nothing to say — silence is
        # correct here, and it is what keeps the Runs tab and the log readable
        # overnight.
        if cfg_row["scheduleEnabled"] and open_now and not scheduled_run:
            pending = _count_eligible(
                tenant_id, bool(cfg_row["includeFailedGeneration"])
            )
            if pending:
                run_id = _create_scheduled_run(tenant_id, cfg_row)
                n = _enqueue_eligible(
                    tenant_id,
                    run_id,
                    int(cfg_row["maxAttempts"]),
                    bool(cfg_row["includeFailedGeneration"]),
                )
                events.emit(
                    tenant_id,
                    "boot",
                    f"Schedule window opened ({hhmm(cfg_row['scheduleStartMinute'])}"
                    f"–{hhmm(cfg_row['scheduleEndMinute'])} {cfg_row['scheduleTimezone']})"
                    f" — {n} product(s) queued",
                    run_id=run_id,
                    detail={"queued": n},
                )
        elif cfg_row["scheduleEnabled"] and not open_now and scheduled_run:
            released = _stop_for_window(tenant_id, "schedule window closed")
            events.emit(
                tenant_id,
                "done",
                f"Schedule window closed — {released} queued product(s) released",
                run_id=str(scheduled_run["id"]),
                detail={"released": released},
            )

        # ---- arrival catch-up ---------------------------------------------
        #
        # Products the arrival hook had to skip because their renders were still
        # producing. See _catch_up_arrivals: the hook fires ~2s after creation
        # and generation takes minutes, so without this they stay NOT_STARTED
        # forever and the user sees the agent silently ignore them.
        #
        # Gated on startOnArrival, because that flag is what says "products
        # arriving in Review should be verified". A tenant running on schedule
        # only gets them from the window sweep instead.
        if cfg_row["startOnArrival"] and (
            not cfg_row["arrivalRespectsWindow"] or open_now
        ):
            caught, catch_run = _catch_up_arrivals(tenant_id, cfg_row)
            if caught:
                events.emit(
                    tenant_id,
                    "queue",
                    f"{caught} product(s) queued that arrived in Review while "
                    f"their renders were still generating",
                    run_id=catch_run,
                    detail={"queued": caught, "reason": "arrival-catch-up"},
                )

        # ---- dispatch a pump ----------------------------------------------
        should_pump = (
            open_now
            or bool(cfg_row["startOnArrival"])
            or cfgmod.has_open_manual_run(tenant_id)
        )
        if should_pump:
            dispatched.append(tenant_id)

    # ---- housekeeping, across all tenants -------------------------------
    reaped = claim.reap_expired_leases()
    stale = claim.cancel_stale_queued()
    closed = claim.close_finished_runs()
    rolled = claim.roll_over_arrival_runs()
    pruned = claim.prune_old_events()

    summary = {
        "tenants": len(configs),
        "dispatched": dispatched,
        "reaped": reaped,
        "staleCancelled": stale,
        "runsClosed": closed,
        "runsReopened": reopened,
        "arrivalRunsRolled": rolled,
        "eventsPruned": pruned,
        "preflightOk": pf["ok"],
    }
    if any(v for k, v in summary.items() if k not in {"tenants", "preflightOk"}):
        log.info("sweep: %s", summary)
    return summary
