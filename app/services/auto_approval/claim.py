"""Claiming, leasing and reaping queue rows.

THE CONCURRENCY DESIGN IS TWO PARTIAL UNIQUE INDEXES (see the migration):

    UNIQUE ("productId") WHERE status IN ('QUEUED','IN_PROGRESS')
    UNIQUE ("tenantId")  WHERE status = 'IN_PROGRESS'

Everything in this file — the advisory lock, the single-statement CTE, the
conditional UPDATEs — makes those quiet rather than making them true. That
distinction matters here more than in a single-language design, because
`worker_concurrency` is a config value someone will eventually raise for
throughput and a second worker container is one line of compose away.
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg import errors as pg_errors
from psycopg.rows import dict_row

from app.services.auto_approval import db

log = logging.getLogger("auto-approval.claim")

# ---------------------------------------------------------------------------
# The claim.
#
# ONE STATEMENT, so select-then-update cannot reopen the race it exists to
# close. Modelled directly on vnyx-api's services/print-queue.ts:463, which
# does the same thing for label jobs and documents why:
#
#   "FOR UPDATE SKIP LOCKED is the whole safety story: two stations running this
#    concurrently cannot select the same row, because the second steps OVER rows
#    the first has locked instead of blocking on them."
# ---------------------------------------------------------------------------
_CLAIM_SQL = """
WITH claimed AS (
  SELECT p.id
  FROM "AutoApprovalRunProduct" p
  JOIN "Product" pr ON pr.id = p."productId"
  WHERE p."tenantId"    = %(tenant_id)s::uuid
    AND p.status        = 'QUEUED'::"VerificationStatus"
    AND p."availableAt" <= now()
    -- Eligibility RE-CHECKED AT CLAIM, not only at enqueue: a product can leave
    -- Review while it waits. cancel_stale_queued() tidies those rows up, and
    -- this clause makes sure one cannot be claimed in the meantime even if the
    -- sweep has not run yet.
    --
    -- Parameterised, defaulting to REVIEW alone, so the unattended loop is
    -- unchanged. scripts/run_from_sheet.py --include-label is the only caller
    -- that widens it.
    AND pr."currentStage" = ANY(%(stages)s::"ProductStage"[])
    AND pr."isDeleted"  = false
    AND pr."isArchived" = false
    -- The pipeline must STILL be finished with it. A product can start
    -- regenerating between enqueue and claim, and verifying one then would
    -- repair images a job in flight is about to replace.
    AND pr."generationStatus" = ANY(%(gen)s::"ProductGenerationStatus"[])
    AND pr."isRegenerating" = false
    AND pr."reviewStatus" = ANY(ARRAY['PENDING','PENDING_DECISON']::"ProductReviewStatus"[])
  -- Manual work first (an operator is waiting on that one), then the oldest
  -- arrival, so garments come out in the order they came in.
  ORDER BY p.priority DESC, p."createdAt" ASC
  -- FOR UPDATE OF p, not the join: locking Product too would contend with every
  -- normal product write in the application.
  FOR UPDATE OF p SKIP LOCKED
  LIMIT 1
)
UPDATE "AutoApprovalRunProduct" q
   SET status           = 'IN_PROGRESS'::"VerificationStatus",
       "claimedAt"      = now(),
       "leaseExpiresAt" = now() + (%(lease_seconds)s * interval '1 second'),
       -- ON CLAIM, not on failure: a worker that claims and then vanishes has
       -- burned an attempt, so three silent disappearances stop the product
       -- rather than retrying it forever. Same decision PrintJob records.
       attempts         = q.attempts + 1,
       "startedAt"      = now(),
       "updatedAt"      = now()
  FROM claimed c
 WHERE q.id = c.id
RETURNING q.*;
"""


def claim_next(
    tenant_id: str, lease_seconds: int, include_failed: bool = False,
    stages: list[str] | None = None,
) -> dict[str, Any] | None:
    """Take at most one queue row for this tenant, or return None.

    Returns None for three different situations, all of which mean "do not work
    right now": nothing queued, another pump holds the tenant, or the
    one-in-progress index refused us. Only the first is really "empty", but the
    caller's behaviour is identical, and NONE of them burns an attempt.

    `stages` defaults to REVIEW alone — the unattended loop's behaviour,
    unchanged. Widening it is a deliberate act by an operator working a named
    batch; see scripts/run_from_sheet.py --include-label.
    """
    conn = db.product_audit.connect(db.dsn(), read_only=False, statement_timeout_s=60)
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            # Quiet the contention before touching the queue. Releases
            # automatically on commit, rollback OR process death — which is what
            # a Redis lock cannot promise, and why there is no Redis lock here.
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%(key)s)) AS got",
                {"key": f"auto-approval:{tenant_id}"},
            )
            row = cur.fetchone()
            if not row or not row["got"]:
                conn.rollback()
                return None

            try:
                cur.execute(
                    _CLAIM_SQL,
                    {
                        "tenant_id": tenant_id,
                        "lease_seconds": int(lease_seconds),
                        "gen": (
                            ["COMPLETE", "FAILED"]
                            if include_failed
                            else ["COMPLETE"]
                        ),
                        "stages": stages or ["REVIEW"],
                    },
                )
            except pg_errors.UniqueViolation:
                # The one-in-progress-per-tenant index refused us. Not an error:
                # another worker is mid-product. Roll back and let the next tick
                # try — deliberately WITHOUT incrementing attempts, because the
                # product was never actually worked on.
                conn.rollback()
                log.info("tenant=%s busy — another product is in progress", tenant_id)
                return None

            claimed = cur.fetchone()
        conn.commit()
        return dict(claimed) if claimed else None
    finally:
        conn.close()


def release_lease(run_product_id: str, *, backoff_seconds: int) -> None:
    """Put a row back on the queue with a delay.

    Used by the retryable-failure path. Conditional on IN_PROGRESS so a row the
    reaper already took back is not resurrected.
    """
    db.execute(
        """
        UPDATE "AutoApprovalRunProduct"
           SET status           = 'QUEUED'::"VerificationStatus",
               "leaseExpiresAt" = NULL,
               "claimedAt"      = NULL,
               "startedAt"      = NULL,
               "availableAt"    = now() + (%(backoff)s * interval '1 second'),
               "updatedAt"      = now()
         WHERE id = %(id)s::uuid
           AND status = 'IN_PROGRESS'::"VerificationStatus"
        """,
        {"id": run_product_id, "backoff": int(backoff_seconds)},
    )


def backoff_seconds(attempts: int) -> int:
    """Retry delay.

    The same curve vnyx-api's print-queue uses, ported rather than reinvented so
    the two queues in this system do not disagree about what a retry feels like:
    30s, 2min, 5min, then capped.
    """
    ladder = [30, 120, 300]
    if attempts <= 0:
        return ladder[0]
    return ladder[min(attempts - 1, len(ladder) - 1)]


def reap_expired_leases() -> dict[str, int]:
    """Return rows whose lease outlived their worker.

    A worker killed mid-product (OOM, redeploy, SIGKILL) leaves a row
    IN_PROGRESS with a lease nobody is holding. `attempts` was already
    incremented at claim, so three crashes stop the product rather than looping
    forever — which is the whole reason the increment is where it is.

    One statement per outcome rather than a blanket updateMany, so the two cases
    are countable and the FAILED rows get their outcome recorded.
    """
    requeued = db.execute(
        """
        UPDATE "AutoApprovalRunProduct"
           SET status           = 'QUEUED'::"VerificationStatus",
               "leaseExpiresAt" = NULL,
               "claimedAt"      = NULL,
               "startedAt"      = NULL,
               "availableAt"    = now() + interval '30 seconds',
               "updatedAt"      = now()
         WHERE status = 'IN_PROGRESS'::"VerificationStatus"
           AND "leaseExpiresAt" < now()
           AND attempts < "maxAttempts"
        """
    )

    # Out of attempts. Terminal, and the product's cache follows so it is not
    # re-picked by arrival or the scheduler.
    failed = db.execute(
        """
        WITH dead AS (
          UPDATE "AutoApprovalRunProduct"
             SET status         = 'FAILED'::"VerificationStatus",
                 outcome        = 'LEASE_EXPIRED',
                 reason         = 'The worker stopped responding while this product was being verified, and it is out of attempts.',
                 "completedAt"  = now(),
                 "leaseExpiresAt" = NULL,
                 "updatedAt"    = now()
           WHERE status = 'IN_PROGRESS'::"VerificationStatus"
             AND "leaseExpiresAt" < now()
             AND attempts >= "maxAttempts"
          RETURNING "productId", "runId"
        ), bumped AS (
          UPDATE "AutoApprovalRun" r
             SET "failedCount" = r."failedCount" + 1
            FROM dead d WHERE r.id = d."runId"
          RETURNING 1
        )
        UPDATE "Product" p
           SET "verificationStatus"    = 'FAILED'::"VerificationStatus",
               "verificationOutcome"   = 'LEASE_EXPIRED',
               "verificationCheckedAt" = now()
          FROM dead d
         WHERE p.id = d."productId"
        """
    )

    if requeued or failed:
        log.info("reaped leases: %d requeued, %d failed", requeued, failed)
    return {"requeued": requeued, "failed": failed}


def cancel_stale_queued() -> int:
    """Withdraw QUEUED rows whose product is no longer eligible.

    A backstop for the vnyx-api hook: it catches moves that bypassed
    recordStageTransition (a bulk SQL update, a restore, a direct edit). The
    claim query already refuses these rows, so this is tidiness rather than
    safety — but a queue depth that counts unclaimable rows is a queue depth
    nobody can trust.

    The PRODUCT goes back to NOT_STARTED, not CANCELLED: it carries no verdict
    because it was never checked, and it should be eligible again if it returns
    to Review.
    """
    return db.execute(
        """
        WITH stale AS (
          UPDATE "AutoApprovalRunProduct" q
             SET status        = 'CANCELLED'::"VerificationStatus",
                 outcome       = 'LEFT_REVIEW',
                 reason        = 'The product left the Review stage before it could be verified.',
                 "completedAt" = now(),
                 "leaseExpiresAt" = NULL,
                 "updatedAt"   = now()
            FROM "Product" pr
           WHERE pr.id = q."productId"
             AND q.status = 'QUEUED'::"VerificationStatus"
             AND (pr."currentStage" <> 'REVIEW'::"ProductStage"
                  OR pr."isDeleted" = true
                  OR pr."isArchived" = true)
          RETURNING q."productId", q."runId"
        ), bumped AS (
          UPDATE "AutoApprovalRun" r
             SET "cancelledCount" = r."cancelledCount" + 1
            FROM stale s WHERE r.id = s."runId"
          RETURNING 1
        )
        UPDATE "Product" p
           SET "verificationStatus"  = 'NOT_STARTED'::"VerificationStatus",
               "verificationOutcome" = NULL,
               "verificationReason"  = NULL
          FROM stale s
         WHERE p.id = s."productId"
           AND p."verificationStatus" = 'QUEUED'::"VerificationStatus"
        """
    )


def close_finished_runs() -> int:
    """Mark runs COMPLETED once nothing is left in flight.

    Idempotent (`AND status='RUNNING'`), and a backstop for the per-product
    check in the runner — a run whose last product was CANCELLED rather than
    processed would otherwise sit RUNNING forever.

    ON_ARRIVAL runs are deliberately excluded: they are a long-lived session,
    rolled over daily instead.
    """
    return db.execute(
        """
        UPDATE "AutoApprovalRun" r
           SET status           = 'COMPLETED'::"AutoApprovalRunStatus",
               "completedAt"    = now(),
               "completionReason" = COALESCE(r."completionReason", 'all products processed')
         WHERE r.status = 'RUNNING'::"AutoApprovalRunStatus"
           AND r.source <> 'ON_ARRIVAL'::"AutoApprovalRunSource"
           AND NOT EXISTS (
             SELECT 1 FROM "AutoApprovalRunProduct" q
              WHERE q."runId" = r.id
                AND q.status IN ('QUEUED'::"VerificationStatus",
                                 'IN_PROGRESS'::"VerificationStatus")
           )
        """
    )


def roll_over_arrival_runs(max_age_hours: int = 24) -> int:
    """Close an ON_ARRIVAL session and let the next arrival open a fresh one.

    Without this a busy tenant's arrival run accumulates a month of products and
    the run-detail page becomes unbounded. Only closed when nothing is in
    flight, so a roll-over never orphans work.
    """
    return db.execute(
        """
        UPDATE "AutoApprovalRun" r
           SET status           = 'COMPLETED'::"AutoApprovalRunStatus",
               "completedAt"    = now(),
               "completionReason" = 'rolled over'
         WHERE r.status = 'RUNNING'::"AutoApprovalRunStatus"
           AND r.source = 'ON_ARRIVAL'::"AutoApprovalRunSource"
           AND r."startedAt" < now() - (%(hours)s * interval '1 hour')
           AND NOT EXISTS (
             SELECT 1 FROM "AutoApprovalRunProduct" q
              WHERE q."runId" = r.id
                AND q.status IN ('QUEUED'::"VerificationStatus",
                                 'IN_PROGRESS'::"VerificationStatus")
           )
        """,
        {"hours": int(max_age_hours)},
    )


def prune_old_events(days: int = 30) -> int:
    """Delete event rows for runs that finished long ago.

    ~10 rows per product means a 4,000-product nightly run is ~40k rows. Run and
    run-product rows are kept indefinitely — they are the audit trail and are a
    tenth of the volume.
    """
    return db.execute(
        """
        DELETE FROM "AutoApprovalEvent" e
         USING "AutoApprovalRun" r
         WHERE e."runId" = r.id
           AND r.status IN ('COMPLETED'::"AutoApprovalRunStatus",
                            'CANCELLED'::"AutoApprovalRunStatus")
           AND r."completedAt" < now() - (%(days)s * interval '1 day')
        """,
        {"days": int(days)},
    )
