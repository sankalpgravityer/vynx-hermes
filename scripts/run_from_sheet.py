#!/usr/bin/env python
"""Verify a hand-picked list of products, ONE RUN PER PRODUCT, from a laptop.

    # look first — nothing written, nothing spent beyond the model calls
    python scripts/run_from_sheet.py --sheet reports/boas_approved_products.xlsx

    # let the repairs land, still nothing approved
    python scripts/run_from_sheet.py --sheet ... --apply

    # the irreversible one
    python scripts/run_from_sheet.py --sheet ... --apply --approve


WHY THIS EXISTS RATHER THAN "PRESS RUN NOW 1,900 TIMES"

The Runs tab is fed by AutoApprovalRun / AutoApprovalRunProduct rows, and the
obvious way to make a product appear there is to INSERT those rows saying it was
verified. Do not do that. A run record that says "verified" for work nobody did
is a lie told to whoever reads it next, and the steps[]/deltas[] columns would
be empty in a way no real run ever produces.

So this drives the ACTUAL worker path — `claim_next` then `runner.verify_one`,
the same two calls `aa.pump` makes — against a run this script opens. The
history is genuine because the work is genuine; the only thing that differs from
a Celery run is which process called it.


ONE RUN PER TENANT, REUSED, AND LEFT OPEN.

The first invocation for a tenant opens a MANUAL_FULL_REVIEW run — the scope the
Runs tab labels "Whole queue" — and every product from every later invocation
joins it. Same shape the arrival trigger uses, for the same reason: one row
someone can open and read, rather than one row per product with the history
buried under them.

It is not closed at the end. Closing it would make the next invocation open a
second one, which is the thing this avoids. Close it from the UI's Stop when the
tenant is done.


WHAT IT WILL NOT DO

  * It refuses a product that is not eligible, with the reason, rather than
    forcing it. The eligibility predicate is the agent's, unchanged.
  * `--approve` is separate from `--apply` and prompts, because approving
    publishes a Shopify listing and there is no undo.
  * It never writes AutoApprovalRunProduct by hand beyond the enqueue row that
    `verify_one` then fills in itself.


DO NOT INTERRUPT IT MID-PRODUCT.

Each product takes 1-3 minutes with writes on, and Ctrl+C during `verify_one`
leaves the queue row IN_PROGRESS holding a lease. In the deployed agent the
sweep's lease reaper requeues that within the hour; run standalone there is no
reaper, so the row is stranded until someone releases it by hand and the product
sits at verificationStatus=QUEUED, which the eligibility predicate refuses.

THE BACKEND MUST BE RUNNING AND ON THE SAME DATABASE.

Every repair step is an HTTP call to vnyx-api, which runs the TypeScript that
owns the writes. Point VNYX_API_URL at it and the preflight asserts both sides
name the same database before anything starts.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from psycopg.types.json import Jsonb  # noqa: E402

from app.services.auto_approval import (  # noqa: E402
    claim,
    config as cfgmod,
    db,
    events,
    preflight,
    runner,
    sweep,
)

# The agent's eligibility predicate, spelled out so a refusal can name the exact
# clause that failed rather than just "not eligible".
CLAUSES: list[tuple[str, str]] = [
    ("in Review", "p.\"currentStage\" = 'REVIEW'"),
    # DROPPED BY --retry. A SHADOW pass records a real verdict and leaves the
    # product VERIFIED, so the sensible ramp — dry run, then --apply, then
    # --approve — made its own second step ineligible. The API has the same
    # escape hatch: ManualScope='retry' sets allowTerminal.
    ("never verified", "p.\"verificationStatus\" = 'NOT_STARTED'"),
    ("not deleted", "p.\"isDeleted\" = false"),
    ("not archived", "p.\"isArchived\" = false"),
    ("generation finished",
     "p.\"generationStatus\" = ANY(%(gen)s::\"ProductGenerationStatus\"[])"),
    ("not regenerating", "p.\"isRegenerating\" = false"),
    ("review status pending",
     "p.\"reviewStatus\" = ANY(ARRAY['PENDING','PENDING_DECISON']::\"ProductReviewStatus\"[])"),
    ("not decision-rejected",
     "(p.\"decisionApprovalStatus\" IS NULL"
     " OR p.\"decisionApprovalStatus\" <> 'REJECTED'::\"DecisionApprovalStatus\")"),
]


# Mirrors APPROVABLE_FROM in vnyx-api's scripts/approve-products.ts. REJECTED is
# absent from both: approving a rejected product resurrects it.
APPROVABLE_STAGES = ("REVIEW", "LABEL", "MISSING_LABEL", "PHOTOBOOTH")


class _Tee:
    """Write to the terminal AND a file, from inside the process.

    WHY NOT THE SHELL. Every PowerShell option fails one half of this:

      * `| Out-File` and `| Tee-Object` put stdout in a PIPELINE, so the
        "Type APPROVE to continue" prompt goes to the file and the run looks
        hung while it blocks on input nobody can see.
      * `Start-Transcript` leaves the console interactive but does NOT capture a
        native command's output on Windows PowerShell 5.1 — it records the
        command line and nothing the process printed.
      * Both mangle non-ASCII: the console code page is not UTF-8, so the em
        dashes and middots came out as `ù` and `╖`.

    Doing it here fixes all three. The prompt still reaches the terminal, the
    file gets every byte, and it is opened UTF-8 explicitly so the log reads
    correctly whatever the console is set to.

    Line-buffered, so `Get-Content -Wait` in another window follows it live and
    a killed run still leaves a complete log up to the last line.
    """

    def __init__(self, stream: Any, path: Path) -> None:
        self.stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(path, "a", encoding="utf-8", buffering=1, newline="")

    def write(self, text: str) -> int:
        self.stream.write(text)
        self.file.write(text)
        return len(text)

    def flush(self) -> None:
        self.stream.flush()
        self.file.flush()

    # input() asks for these; a bare object would break the prompt.
    def isatty(self) -> bool:
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.stream.fileno()


def read_sheet(path: Path, reason_col: str | None = None) -> list[dict[str, str]]:
    """Product ids out of the audit workbook.

    Accepts a `Product id` column (what review_audit writes) or a `SKU` column,
    so a sheet someone trimmed by hand still works.

    `reason_col` names a column whose value becomes that product's hold reason —
    the audit sheet already diagnoses each row ("upper rig for a bottom
    garment", "women rig on a men product"), and one blanket string would throw
    that away. Matched case-insensitively, because a header typed by hand is not
    reliably capitalised.
    """
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        sys.exit(f"{path} is empty.")

    header = [str(h).strip().lower() if h else "" for h in rows[0]]

    def col(*names: str) -> int | None:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_id = col("product id", "productid", "id")
    i_sku = col("sku")
    i_tenant = col("tenant")
    if i_id is None and i_sku is None:
        sys.exit(f"{path} has neither a 'Product id' nor a 'SKU' column.")

    i_reason = col(reason_col.strip().lower()) if reason_col else None
    if reason_col and i_reason is None:
        sys.exit(f"{path} has no column named {reason_col!r}. "
                 f"Columns: {', '.join(h for h in header if h)}")

    out: list[dict[str, str]] = []
    for r in rows[1:]:
        if not r or all(v in (None, "") for v in r):
            continue
        out.append({
            "id": str(r[i_id]).strip() if i_id is not None and r[i_id] else "",
            "sku": str(r[i_sku]).strip() if i_sku is not None and r[i_sku] else "",
            "tenant": str(r[i_tenant]).strip() if i_tenant is not None and r[i_tenant] else "",
            "reason": (str(r[i_reason]).strip()
                       if i_reason is not None and r[i_reason] else ""),
        })
    return out


def resolve(entry: dict[str, str], include_failed: bool,
            retry: bool = False,
            stages: list[str] | None = None) -> dict[str, Any] | None:
    """The product row, plus which eligibility clauses it fails."""
    where = 'p.id = %(id)s::uuid' if entry["id"] else 'p.sku = %(sku)s'
    clauses = [c for c in CLAUSES if not (retry and c[0] == "never verified")]
    if stages and any(s not in APPROVABLE_STAGES for s in stages):
        # These names are interpolated into SQL below, so they are checked
        # against a closed set rather than trusted. They come from a flag today;
        # this keeps that true if a caller ever passes them from elsewhere.
        raise ValueError(f"unknown stage in {stages}")
    if stages and stages != ["REVIEW"]:
        # BOAS has 5,387 products at LABEL against 183 at REVIEW, and the seven
        # in this sheet each had their cut-outs, all five renders, a label and a
        # size chart — complete records whose stage flag never moved. So the
        # clause is widened rather than dropped: the other seven still apply,
        # and the approval pre-flight remains the real gate.
        clauses = [
            ('in ' + '/'.join(s.title() for s in stages),
             'p."currentStage" = ANY(ARRAY[' +
             ','.join(f"'{s}'" for s in stages) + ']::"ProductStage"[])')
            if name == "in Review" else (name, sql)
            for name, sql in clauses
        ]
    checks = ",\n               ".join(
        f'({sql}) AS "chk_{i}"' for i, (_, sql) in enumerate(clauses)
    )
    row = db.fetch_one(
        f"""
        SELECT p.id, p.sku, p.title, p."tenantId", t.name AS tenant,
               p."currentStage", p."generationStatus", p."verificationStatus",
               {checks}
          FROM "Product" p JOIN "Tenant" t ON t.id = p."tenantId"
         WHERE {where}
        """,
        {"id": entry["id"], "sku": entry["sku"],
         "gen": sweep.generation_statuses(include_failed)},
    )
    if not row:
        return None
    row["failed_clauses"] = [
        name for i, (name, _) in enumerate(clauses) if not row[f"chk_{i}"]
    ]
    return row


# The scope the Runs tab labels "Whole queue". One per tenant, reused by every
# invocation, so a day of hand-picked products is a single row in the history.
RUN_SOURCE = "MANUAL_FULL_REVIEW"


def open_run(tenant_id: str, cfg_row: dict[str, Any]) -> tuple[str, bool]:
    """The tenant's open Whole-queue run, or a new one. Returns (id, created).

    ONE LONG-LIVED RUN PER TENANT, the shape the arrival trigger already uses:
    keep a RUNNING run open and drop every product into it. A run per product
    was right while this script existed to prove one product at a time; it makes
    the Runs tab unreadable once there are dozens.

    Deliberately NOT closed at the end of an invocation — the next one joins it.
    """
    existing = cfgmod.find_open_run(tenant_id, RUN_SOURCE)
    if existing:
        return str(existing["id"]), False
    return sweep._create_scheduled_run(tenant_id, cfg_row, source=RUN_SOURCE), True


def enqueue_one(run_id: str, row: dict[str, Any], max_attempts: int,
                retry: bool = False) -> bool:
    """One queue row, and flip the product to QUEUED.

    ON CONFLICT DO NOTHING against the partial unique index, and the Product
    update is conditional — so a product the live agent grabbed a moment ago is
    simply not counted rather than double-queued.

    RE-QUEUING GIVES BACK THE COUNTER IT ALREADY SPENT. The run's four counts
    are denormalised — _finish increments one of them per terminal write — and
    that is only sound while a product reaches a verdict once. Re-queuing a
    terminal row breaks it: the row goes back to QUEUED but the increment it
    already caused stays, so the same product is counted twice. BOAS ended the
    day reading 260 of 247 with cancelledCount=59 against zero cancelled rows,
    and the Runs tab showed 105%.

    So the old status is read first and, if the re-queue takes, its counter is
    decremented. GREATEST(...,0) because these columns have been drifting for a
    while and a correction must not push one negative.
    """
    superseded: str | None = None
    if retry:
        prev = db.fetch_one(
            'SELECT status FROM "AutoApprovalRunProduct"'
            ' WHERE "runId" = %(r)s::uuid AND "productId" = %(p)s::uuid',
            {"r": run_id, "p": str(row["id"])},
        )
        if prev and prev["status"] not in ("QUEUED", "IN_PROGRESS"):
            superseded = prev["status"]

    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH inserted AS (
                  INSERT INTO "AutoApprovalRunProduct"
                    ("runId", "tenantId", "productId", "productSku",
                     "productTitle", status, priority, "availableAt",
                     "maxAttempts", "updatedAt")
                  VALUES (%(run)s::uuid, %(t)s::uuid, %(p)s::uuid, %(sku)s,
                          %(title)s, 'QUEUED'::"VerificationStatus", 10,
                          (now() AT TIME ZONE 'UTC'), %(max)s,
                          (now() AT TIME ZONE 'UTC'))
                  -- RE-QUEUE IN PLACE, because the run is reused.
                  --
                  -- @@unique([runId, productId]) allows one row per product per
                  -- run, and this script now keeps ONE run open per tenant. So
                  -- the second attempt at a product is not a new row -- it is
                  -- the same row, and a plain DO NOTHING reported it as
                  -- "already queued in another run" and refused every retry.
                  -- One run per tenant and retry are only compatible if the
                  -- existing row is reset.
                  --
                  -- Gated on `retry` and on the row being TERMINAL: a QUEUED or
                  -- IN_PROGRESS row belongs to something that is still working,
                  -- and the WHERE is what stops this stealing it. Attempts go
                  -- back to zero because this is a fresh decision by a person,
                  -- not a continuation of the failed one.
                  ON CONFLICT ("runId", "productId") DO UPDATE
                     SET status        = 'QUEUED'::"VerificationStatus",
                         outcome       = NULL,
                         reason        = NULL,
                         error         = NULL,
                         attempts      = 0,
                         "claimedAt"   = NULL,
                         "leaseExpiresAt" = NULL,
                         "startedAt"   = NULL,
                         "completedAt" = NULL,
                         "availableAt" = (now() AT TIME ZONE 'UTC'),
                         "updatedAt"   = (now() AT TIME ZONE 'UTC')
                   WHERE %(retry)s
                     AND "AutoApprovalRunProduct".status <> ALL
                         (ARRAY['QUEUED','IN_PROGRESS']::"VerificationStatus"[])
                  -- xmax = 0 marks a genuine INSERT; a row that came through
                  -- the DO UPDATE carries the updating transaction's id. It is
                  -- the only way to tell the two apart here, and `totalProducts`
                  -- below needs to: counting a re-queue as a new product is why
                  -- the denominator grew to 247 for a 196-row run.
                  RETURNING "productId", (xmax = 0) AS is_new
                ), flipped AS (
                  UPDATE "Product" p
                     SET "verificationStatus" = 'QUEUED'::"VerificationStatus",
                         "verificationOutcome" = NULL,
                         "verificationReason" = NULL
                    FROM inserted i
                   WHERE p.id = i."productId"
                     -- Under --retry a TERMINAL status is overwritten, but an
                     -- in-flight one never is: QUEUED / IN_PROGRESS means some
                     -- other run owns this product right now.
                     AND (
                       CASE WHEN %(retry)s
                         THEN p."verificationStatus" <> ALL
                              (ARRAY['QUEUED','IN_PROGRESS']::"VerificationStatus"[])
                         ELSE p."verificationStatus"
                              = 'NOT_STARTED'::"VerificationStatus"
                       END
                     )
                  RETURNING p.id
                )
                UPDATE "AutoApprovalRun"
                   SET "totalProducts" = "totalProducts"
                       + (SELECT count(*) FROM inserted WHERE is_new)
                 WHERE id = %(run)s::uuid
                RETURNING (SELECT count(*)::int FROM inserted) AS n
                """,
                {"run": run_id, "t": str(row["tenantId"]), "p": str(row["id"]),
                 "sku": row["sku"], "title": row["title"], "max": max_attempts,
                 "retry": retry},
            )
            n = cur.fetchone()

            # Hand back the counter the superseded verdict spent. Inside the
            # same transaction as the re-queue, so the row and the count cannot
            # disagree even if this dies halfway.
            if superseded and n and n[0]:
                column = {
                    "VERIFIED": "verifiedCount",
                    "HELD_FOR_HUMAN": "heldCount",
                    "FAILED": "failedCount",
                    "CANCELLED": "cancelledCount",
                }.get(superseded)
                if column:
                    cur.execute(
                        f'UPDATE "AutoApprovalRun"'
                        f'   SET "{column}" = GREATEST("{column}" - 1, 0)'
                        f' WHERE id = %(r)s::uuid',
                        {"r": run_id},
                    )
        conn.commit()
    return bool(n and n[0])


def release_one(run_id: str, product_id: str, reason: str) -> None:
    """Undo one enqueue: cancel the row and put the product back.

    CANCELLED rather than deleted, so the attempt stays on the Runs tab — the
    same choice stopAgent makes. The Product update is conditional on QUEUED so
    it cannot stomp on a status something else has since set.
    """
    with db.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH cancelled AS (
                  UPDATE "AutoApprovalRunProduct"
                     SET status = 'CANCELLED'::"VerificationStatus",
                         reason = %(why)s,
                         "completedAt" = (now() AT TIME ZONE 'UTC'),
                         "updatedAt"   = (now() AT TIME ZONE 'UTC')
                   WHERE "runId" = %(r)s::uuid
                     AND "productId" = %(p)s::uuid
                     AND status = 'QUEUED'::"VerificationStatus"
                  RETURNING "productId"
                )
                UPDATE "Product" p
                   SET "verificationStatus" = 'NOT_STARTED'::"VerificationStatus"
                  FROM cancelled c
                 WHERE p.id = c."productId"
                   AND p."verificationStatus" = 'QUEUED'::"VerificationStatus"
                """,
                {"r": run_id, "p": product_id, "why": reason},
            )
        conn.commit()


REJECT_REASON = "care label is missing"


def reject_missing_label(run_id: str, row: dict[str, Any], tenant_id: str,
                         apply: bool) -> bool:
    """Move a care-label-less product to REJECTED and record it FAILED.

    CHECKED BEFORE THE CHAIN RUNS, not after. A care label is a photograph of a
    physical tag: nothing generates one, the extract step has nothing to read,
    and brand / size / material / composition all fall out of it. So the whole
    verification is foreclosed — running it anyway costs two minutes and a
    vision call to arrive at a conclusion that was knowable from one count.

    The rejection goes through vnyx-api's reject step, which writes exactly what
    POST /products/bulk action=REJECT writes, so the stage machine's hooks fire.
    Publishing is not a risk here: onApprovedArrival is the only hook that
    enqueues a Shopify upsert, and a rejection never reaches it.

    Returns True when it handled the product.
    """
    labels = db.fetch_one(
        """
        SELECT count(*)::int AS n FROM "ProductMedia"
         WHERE "productId" = %(p)s::uuid AND view = 'LABEL'
           AND "isCurrent" = true AND "deletedAt" IS NULL
           AND "mediaType" = 'IMAGE'
        """,
        {"p": str(row["id"])},
    )
    if labels and labels["n"]:
        return False

    print(f"    no care label — rejecting rather than verifying")
    if not apply:
        # Same reason as the generation-failed branch: an un-released QUEUED row
        # is claimed by the NEXT product's claim_next, which then reports
        # "another worker holds this tenant" for the remainder of the sheet.
        release_one(run_id, str(row["id"]),
                    "released: dry run, nothing was written")
        print("    (dry run: nothing written)")
        return True

    return _record_rejection(run_id, row, tenant_id,
                             outcome="NO_CARE_LABEL", reason=REJECT_REASON)


def _record_rejection(run_id: str, row: dict[str, Any], tenant_id: str, *,
                      outcome: str, reason: str,
                      was_held: bool = False, quiet: bool = False) -> bool:
    """Archive the product and record the queue row FAILED with `reason`.

    Shared by both rejection rules. `was_held` says the chain already ran and
    wrote HELD_FOR_HUMAN, so this is CONVERTING a verdict rather than writing a
    first one — the run's heldCount has to come back down as failedCount goes
    up, or the Runs tab shows the product twice.
    """
    # Through the step runner, so the write is vnyx-api's own.
    import importlib
    rp = importlib.import_module("scripts.repair_product")
    ok, out, _ = rp.run_step(
        None, "reject-products.ts",
        ["--product", str(row["id"]), "--apply", "--reason", reason],
        timeout_s=120, quiet=True,
    )
    if not ok:
        # Never silenced: a rejection that did not happen must be visible even
        # when the caller asked for quiet output.
        print(f"    REJECT STEP FAILED: {(out or '')[-160:]}")
        return False

    # FAILED on the queue row, with the reason. Note this overloads FAILED,
    # which the schema documents as "the RUN errored, not the product" — asked
    # for explicitly, and the reason column says which kind it is.
    db.execute(
        """
        UPDATE "AutoApprovalRunProduct"
           SET status = 'FAILED'::"VerificationStatus",
               outcome = %(oc)s, reason = %(why)s,
               "completedAt" = (now() AT TIME ZONE 'UTC'),
               "updatedAt"   = (now() AT TIME ZONE 'UTC')
         WHERE "runId" = %(r)s::uuid AND "productId" = %(p)s::uuid
        """,
        {"r": run_id, "p": str(row["id"]), "why": reason, "oc": outcome},
    )
    db.execute(
        """
        UPDATE "Product"
           SET "verificationStatus" = 'FAILED'::"VerificationStatus",
               "verificationOutcome" = %(oc)s,
               "verificationReason"  = %(why)s,
               "verificationRunId"   = %(r)s::uuid,
               "verificationCheckedAt" = (now() AT TIME ZONE 'UTC')
         WHERE id = %(p)s::uuid
        """,
        {"r": run_id, "p": str(row["id"]), "why": reason, "oc": outcome},
    )
    db.execute(
        f"""
        UPDATE "AutoApprovalRun"
           SET "failedCount" = "failedCount" + 1
               {', "heldCount" = GREATEST("heldCount" - 1, 0)' if was_held else ''}
         WHERE id = %(r)s::uuid
        """,
        {"r": run_id},
    )
    events.emit(tenant_id, "warn",
                f"{row['sku']} rejected — {reason}",
                run_id=run_id, product_id=str(row["id"]))
    if not quiet:
        print(f"    -> REJECTED · FAILED · {reason}")
    return True


def hold_one(run_id: str, row: dict[str, Any], tenant_id: str,
             reason: str, apply: bool) -> bool:
    """Record one product HELD_FOR_HUMAN with a given reason. No chain, no move.

    FOR A DEFECT SOMEONE HAS ALREADY DIAGNOSED. The 236 products in the
    mannequin sheet were photographed on the wrong rig — an upper-body form
    holding a skirt. No rule finds that, no repair fixes it, and the answer is
    a re-shoot. Running the chain over them would spend ~30s of model calls
    each to arrive at a verdict that was known before the run started.

    NOT A REJECTION. `reject-products.ts` archives the product and sets
    reviewStatus REJECTED; these are sound records with a bad photograph, so
    they stay in Review where someone can act on them. Nothing about the
    product is written except its verification cache — the stage, the review
    status and the imagery are untouched.

    The outcome code is derived from the reason so the Runs tab groups them:
    "mannequin mismatch" -> MANNEQUIN_MISMATCH.
    """
    import re

    outcome = re.sub(r"[^A-Z0-9]+", "_", reason.upper()).strip("_")[:40]
    if not apply:
        print(f"    would hold: {outcome} — {reason}")
        release_one(run_id, str(row["id"]),
                    "released: dry run, nothing was written")
        return True

    db.execute(
        """
        UPDATE "AutoApprovalRunProduct"
           SET status = 'HELD_FOR_HUMAN'::"VerificationStatus",
               outcome = %(oc)s, reason = %(why)s,
               "completedAt" = (now() AT TIME ZONE 'UTC'),
               "updatedAt"   = (now() AT TIME ZONE 'UTC')
         WHERE "runId" = %(r)s::uuid AND "productId" = %(p)s::uuid
           AND status = 'QUEUED'::"VerificationStatus"
        """,
        {"r": run_id, "p": str(row["id"]), "oc": outcome, "why": reason},
    )
    db.execute(
        """
        UPDATE "Product"
           SET "verificationStatus" = 'HELD_FOR_HUMAN'::"VerificationStatus",
               "verificationOutcome" = %(oc)s,
               "verificationReason"  = %(why)s,
               "verificationRunId"   = %(r)s::uuid,
               "verificationCheckedAt" = (now() AT TIME ZONE 'UTC')
         WHERE id = %(p)s::uuid
        """,
        {"r": run_id, "p": str(row["id"]), "oc": outcome, "why": reason},
    )
    db.execute(
        'UPDATE "AutoApprovalRun" SET "heldCount" = "heldCount" + 1'
        ' WHERE id = %(r)s::uuid', {"r": run_id},
    )
    events.emit(tenant_id, "warn", f"{row['sku']} held — {reason}",
                run_id=run_id, product_id=str(row["id"]))
    print(f"    -> HELD_FOR_HUMAN {outcome} · {reason}")
    return True


def reject_missing_attrs(run_id: str, row: dict[str, Any], tenant_id: str,
                         apply: bool, quiet: bool = False) -> str | None:
    """Reject a product whose brand or size the care label never yielded.

    CHECKED AFTER THE CHAIN, unlike reject_missing_label — and that ordering is
    the whole point. Brand and size are read OFF the care label by the extract
    and care-label steps, so asking before they run would reject products the
    pipeline was about to fix. Asking afterwards means the label has had its
    chance and the answer is settled.

    Read from the product rather than from the held verdict's text. The same
    absence surfaces as `no brand` from the approval pre-flight and as DATA.010
    from the rule engine, and on BOA-006139 it arrived as the bare string
    "DATA.010; DATA.010" — which names no field at all. The columns say which
    one is missing without any parsing.

    "MISSING" MEANS PLACEHOLDER TOO, and testing only for NULL missed every
    product this was written for. BOAS does not leave the brand empty — it
    writes the literal string "Unknown", so BOA-006159 and BOA-006188 both held
    with `no brand` while a NULL test saw a perfectly good value. The rule
    engine's own placeholder set is imported rather than restated so the two
    cannot drift; `read_property` comes with it for the alias scan, which is
    equally load-bearing here since this tenant stores `Brand`, not `brand`.
    """
    from app.product_audit import PLACEHOLDERS, read_property

    def blank(*values: Any) -> bool:
        """True unless at least one value is a real answer."""
        return not any(
            v is not None and str(v).strip().lower() not in PLACEHOLDERS
            for v in values
        )

    r = db.fetch_one(
        'SELECT p.properties, p."internationalSize" AS size'
        ' FROM "Product" p WHERE p.id = %(p)s::uuid',
        {"p": str(row["id"])},
    ) or {}
    props = r.get("properties") if isinstance(r.get("properties"), dict) else {}

    missing = []
    if blank(read_property(props, "brand")):
        missing.append("brand")
    if blank(r.get("size"), read_property(props, "international_size")):
        missing.append("size")
    if not missing:
        return False

    why = f"{' and '.join(missing)} missing — not readable from the care label"
    if not quiet:
        print(f"    {why} — rejecting rather than holding")
    if not apply:
        if not quiet:
            print("    (dry run: nothing written)")
        return why
    done = _record_rejection(run_id, row, tenant_id,
                             outcome="NO_BRAND_OR_SIZE", reason=why,
                             was_held=True, quiet=quiet)
    # The reason doubles as the truthy result, so the sequential caller's
    # `and reject_missing_attrs(...)` reads the same as before while the
    # parallel one can print it inside its own block.
    return why if done else None


class Parallel:
    """Verify N products of one tenant at once, STARTING AS THEY ARRIVE.

    A POOL FED BY A QUEUE, not a batch collected and then run. The first version
    enqueued every product before starting any, which is invisible at ten
    products and awful at a hundred and fifty: each enqueue is several round
    trips to a database ~180ms away, so 150 products meant ~20 minutes of
    apparent inactivity before a single one was verified. Worse, an interrupt
    during that window strands every row it had queued, because nothing reaps a
    QUEUED row on this path.

    Now the main loop submits each product the moment its row exists and the
    workers are already draining. Product 1 is being matted while product 2 is
    still being looked up.

    WHY THIS DOES NOT CLAIM.
    `AutoApprovalRunProduct_one_in_progress_per_tenant` is a UNIQUE INDEX on
    (tenantId) WHERE status = 'IN_PROGRESS' — the database permits exactly one
    in-flight row per tenant, and claim_next also takes a per-tenant advisory
    lock. Running this script in several terminals therefore does not
    parallelise anything: one works and the rest print "another worker holds
    this tenant". Splitting the sheet does not help either, because the lock is
    on the tenant, not the sheet.

    So the rows stay QUEUED while they are worked on and are finished from
    QUEUED. No schema change, and the two properties that matter survive:

      * NO DOUBLE WORK — `one_open_per_product` is a unique index over
        (productId) WHERE status IN (QUEUED, IN_PROGRESS), so a product cannot
        have two open rows no matter how many workers ask.
      * EXACTLY-ONCE COUNTERS — _finish's UPDATE is still conditional on the
        status it expects, so the first writer flips the row and any second sees
        rowcount 0.

    WHAT IS GIVEN UP, and it is a real cost rather than a footnote: the lease.
    An IN_PROGRESS row carries `leaseExpiresAt` and the reaper reclaims it if
    the worker dies. A QUEUED row being worked on looks exactly like one that
    has not started, so:

      * Kill this script mid-run and those rows stay QUEUED. Harmless — the next
        invocation picks them up and the work is idempotent — but nothing
        reclaims them on its own.
      * DO NOT RUN A CELERY WORKER FOR THIS TENANT AT THE SAME TIME. It would
        claim these QUEUED rows into IN_PROGRESS and verify them a second time,
        concurrently. Wasteful rather than corrupting (the conditional write
        still admits only one verdict), but it doubles the provider spend.

    Threads, not processes: every step is a subprocess or an HTTP call, so the
    GIL is released for essentially the whole of it, and db.connection() opens
    and closes per call so each thread gets its own.
    """

    def __init__(self, rs_for_run: dict[str, Any], workers: int,
                 stages: list[str], reject_attrs: bool, apply: bool) -> None:
        import queue
        import threading

        self.tally = {"ok": 0, "held": 0, "failed": 0}
        self.lock = threading.Lock()
        self.rs = cfgmod.settings_from_snapshot(rs_for_run)
        self.stages = stages
        self.reject_attrs = reject_attrs
        self.apply = apply
        self.submitted = 0
        self.q: Any = queue.Queue()
        self.threads = [
            threading.Thread(target=self._drain, daemon=True)
            for _ in range(max(1, workers))
        ]
        for th in self.threads:
            th.start()

    def submit(self, job: dict[str, Any]) -> None:
        self.submitted += 1
        self.q.put(job)

    def close(self) -> dict[str, int]:
        """Stop taking work and wait for what is in flight."""
        for _ in self.threads:
            self.q.put(None)
        for th in self.threads:
            th.join()
        return self.tally

    def _drain(self) -> None:
        while True:
            job = self.q.get()
            if job is None:
                return
            try:
                self._one(job)
            except Exception as exc:  # noqa: BLE001 — a worker must never die
                with self.lock:
                    self.tally["failed"] += 1
                    print(f"  WORKER CRASHED on {job['row']['sku']}: "
                          f"{exc.__class__.__name__}: {exc}")

    def _one(self, job: dict[str, Any]) -> None:
        rs, stages = self.rs, self.stages
        reject_attrs, apply, lock, tally = (
            self.reject_attrs, self.apply, self.lock, self.tally)
        row, n, total = job["row"], job["n"], job["total"]
        started = time.perf_counter()
        label = f"[{n}/{total}] {row['sku']}"

        # THE STAND-IN FOR THE CLAIM. claim_next normally stamps `startedAt` and
        # burns an attempt; without it the Runs tab shows a product that
        # finished but never began, and a row that failed three times still
        # reads attempts=0.
        #
        # Conditional on QUEUED, so it is also the mutual exclusion this path
        # would otherwise lack: if a second process — another copy of this
        # script, or a Celery worker mid-claim — got there first, rowcount is 0
        # and this thread drops the product instead of verifying it twice.
        taken = db.execute(
            """
            UPDATE "AutoApprovalRunProduct"
               SET "startedAt" = (now() AT TIME ZONE 'UTC'),
                   attempts    = attempts + 1,
                   "updatedAt" = (now() AT TIME ZONE 'UTC')
             WHERE id = %(i)s::uuid
               AND status = 'QUEUED'::"VerificationStatus"
            """,
            {"i": job["claimed"]["id"]},
        )
        if taken == 0:
            with lock:
                print(f"  {label}: SKIPPED — another worker took this row")
            return

        try:
            runner.verify_one(job["claimed"], rs, ignore_stop=True,
                              allow_stage=stages, expect_status="QUEUED",
                              silent=True)
        except Exception as exc:  # noqa: BLE001 — one product must not stop the batch
            with lock:
                tally["failed"] += 1
                print(f"  {label}: WORKER ERROR {exc.__class__.__name__}: {exc}")
            return

        after = db.fetch_one(
            'SELECT status, outcome, reason, approved, steps'
            ' FROM "AutoApprovalRunProduct" WHERE id = %(i)s::uuid',
            {"i": job["claimed"]["id"]},
        ) or {}
        st = after.get("status")

        # QUIET, because this runs OUTSIDE the print lock — it shells out to
        # reject-products.ts and holding the lock across a subprocess would
        # stall the other workers' output. Its two lines landed in the middle
        # of a different product's block otherwise, which is exactly the
        # interleaving the block was introduced to stop.
        rejected = (st == "HELD_FOR_HUMAN" and reject_attrs
                    and reject_missing_attrs(job["run_id"], row,
                                             str(row["tenantId"]), apply,
                                             quiet=True))

        # ONE BLOCK PER PRODUCT, under the lock. repair() was silenced, so this
        # is where the narration happens — as a unit, so two products finishing
        # together cannot interleave into a single nonsensical step list.
        with lock:
            took = time.perf_counter() - started
            print(f"\n  {label} · {(row['title'] or '')[:52]}")
            ran = [s.get("step") for s in (after.get("steps") or [])
                   if isinstance(s, dict) and s.get("ran")]
            if ran:
                print(f"        ran: {', '.join(ran)}")
            if rejected:
                tally["failed"] += 1
                print(f"        -> REJECTED · FAILED  ({took:.0f}s)")
                print(f"           {rejected}")
                return
            extra = " · APPROVED, Shopify upsert enqueued" if after.get("approved") else ""
            print(f"        -> {st} {after.get('outcome') or ''}  ({took:.0f}s){extra}")
            if after.get("reason"):
                print(f"           {after['reason']}")
            if st == "VERIFIED":
                tally["ok"] += 1
            elif st == "HELD_FOR_HUMAN":
                tally["held"] += 1
            else:
                tally["failed"] += 1



def verify_shopify(run_id: str, apply: bool) -> int:
    """Re-drive every product this run approved whose listing never published.

    WHY A PASS AT THE END rather than trusting the approval.
    Approving fires onApprovedArrival, which enqueues the Shopify push
    best-effort and swallows its own errors by design — a queue problem must not
    fail a reviewer's approval. The cost is that a push which never happened is
    invisible: the product reads APPROVED, the catalogue considers it published,
    and Shopify does not have it. On a 150-product batch that was 72 of 123, and
    NONE of them had a single MarketplaceSyncLog row — the job was never created,
    so there was nothing to retry and nothing to see.

    I could not establish why from the data to hand: the products that failed and
    the ones that succeeded differ only in `MarketplaceListing.status` (ERROR vs
    PUBLISHED), with the same variant count, the same shopifyProductId presence,
    and failures spread evenly across the whole run rather than clustered in an
    outage. So this does not try to prevent the gap; it CLOSES it, by asking the
    only question that matters once the batch is done — did the listing publish?
    — and re-driving the ones that did not.

    Cheap when everything worked: one indexed query, and zero steps to run.
    """
    rows = db.fetch_all(
        """
        SELECT rp."productId" AS id, rp."productSku" AS sku,
               l.status AS lstatus, l."lastSyncStatus" AS ls
          FROM "AutoApprovalRunProduct" rp
          JOIN "Product" p ON p.id = rp."productId"
          LEFT JOIN "MarketplaceListing" l ON l."productId" = p.id
         WHERE rp."runId" = %(r)s::uuid
           AND rp.approved = true
           AND (l.id IS NULL OR l."lastSyncStatus" IS DISTINCT FROM 'SUCCESS')
         ORDER BY rp."productSku"
        """,
        {"r": run_id},
    ) or []
    if not rows:
        print("  every approved product reached Shopify")
        return 0

    print(f"  {len(rows)} approved product(s) did NOT reach Shopify:")
    for r in rows[:8]:
        print(f"    {r['sku']}  listing={r['lstatus']}/{r['ls']}")
    if len(rows) > 8:
        print(f"    ... and {len(rows) - 8} more")

    if not apply:
        print("  (dry run: not re-driving)")
        return len(rows)

    import importlib
    rp_mod = importlib.import_module("scripts.repair_product")
    ok = 0
    for r in rows:
        try:
            good, _out, _res = rp_mod.run_step(
                None, "resync-listings.ts",
                ["--product", str(r["id"]), "--apply"],
                timeout_s=120, quiet=True,
            )
            ok += 1 if good else 0
        except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
            print(f"    {r['sku']}: re-drive failed — {exc}")
    print(f"  re-driven: {ok}/{len(rows)} — the worker publishes them from here")
    return len(rows) - ok


def close_run(run_id: str, reason: str) -> None:
    db.execute(
        """
        UPDATE "AutoApprovalRun"
           SET status = 'COMPLETED'::"AutoApprovalRunStatus",
               "completedAt" = (now() AT TIME ZONE 'UTC'),
               "completionReason" = %(why)s
         WHERE id = %(r)s::uuid AND status = 'RUNNING'::"AutoApprovalRunStatus"
        """,
        {"r": run_id, "why": reason},
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet", required=True, help="xlsx with a Product id or SKU column")
    ap.add_argument("--apply", action="store_true",
                    help="let the repairs write. Without it every step is a dry run.")
    ap.add_argument("--approve", action="store_true",
                    help="ALSO move REVIEW -> APPROVED, which publishes to Shopify. "
                         "Works whether or not the tenant's agent is enabled — the "
                         "enabled flag is the stop button for the unattended loop, "
                         "and you are not it.")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products")
    ap.add_argument("--include-failed", action="store_true",
                    help="also accept products whose generation FAILED")
    ap.add_argument("--reject-missing-label", action="store_true",
                    help="a product with no care label photograph is REJECTED "
                         "instead of held, recorded FAILED with the reason. "
                         "Needs --apply.")
    ap.add_argument("--reject-missing-attrs", action="store_true",
                    help="after the chain has run, a product still without a "
                         "brand or a size is REJECTED instead of held — the "
                         "care label has had its chance. Needs --apply.")
    ap.add_argument("--workers", type=int, default=1, metavar="N",
                    help="verify N products of the SAME tenant at once (default "
                         "1). Trades the claim lease for throughput — see the "
                         "notes on the Parallel class. Measured 1.5-1.7x on two "
                         "workers, so 725 products at 4 is roughly 12 hours "
                         "rather than 32.")
    ap.add_argument("--hold-only", metavar="REASON",
                    help="do NOT verify anything: record every eligible product "
                         "in the sheet as HELD_FOR_HUMAN with this reason, and "
                         "move on. For a defect already diagnosed elsewhere "
                         "(a mannequin mismatch, say) where the chain would "
                         "spend model calls to reach a known answer. Refuses to "
                         "combine with --approve or the --reject-* flags.")
    ap.add_argument("--hold-column", metavar="COLUMN",
                    help="hold ONLY the rows that have a value in this column, "
                         "using that value as the reason, and send every other "
                         "row through the normal chain. Unlike --hold-only this "
                         "composes with --approve and the --reject-* flags, so "
                         "one sheet can hold its mannequin mismatches and "
                         "verify the rest.")
    ap.add_argument("--hold-reason-column", metavar="COLUMN",
                    help="with --hold-only: take each product's reason from this "
                         "sheet column instead of the flag's text, so the "
                         "diagnosis already in the sheet survives. A row with "
                         "the column empty falls back to the --hold-only text.")
    ap.add_argument("--reject-placeholder-title", metavar="PREFIX",
                    help="reject a product whose generation never completed AND "
                         "whose title still starts with PREFIX — e.g. "
                         "\"Generating Product\", the placeholder written at "
                         "creation and replaced when generation succeeds. "
                         "Needs --apply.")
    ap.add_argument("--reject-failed-generation", action="store_true",
                    help="a product whose generationStatus is FAILED is "
                         "REJECTED instead of skipped — the renders never "
                         "completed and re-running cannot produce them. Only "
                         "applies when that is the sole thing wrong with it. "
                         "Needs --apply.")
    ap.add_argument("--include-label", action="store_true",
                    help="also accept products at stage LABEL, and let the "
                         "approval pre-flight move them. Every required-field "
                         "check still applies; only the stage clause is "
                         "relaxed.")
    ap.add_argument("--retry", action="store_true",
                    help="re-verify a product that already has a verdict — which "
                         "a dry run leaves behind. Refuses one that is in flight.")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--log", metavar="FILE",
                    help="also append everything to FILE, in UTF-8. Use this "
                         "rather than a shell redirect: a pipeline swallows the "
                         "APPROVE prompt, and Start-Transcript does not capture "
                         "a native command's output on PowerShell 5.1.")
    args = ap.parse_args()

    # HOLD-ONLY IS EXCLUSIVE. It never runs the chain, so pairing it with
    # --approve reads as "approve these" and would do the opposite; pairing it
    # with a --reject-* flag asks for two different terminal states at once.
    # Failing here is better than a silent precedence rule nobody remembers.
    if args.hold_only:
        clash = [n for n, v in (
            ("--approve", args.approve),
            ("--reject-missing-label", args.reject_missing_label),
            ("--reject-missing-attrs", args.reject_missing_attrs),
            ("--reject-failed-generation", args.reject_failed_generation),
        ) if v]
        if clash:
            sys.exit(f"--hold-only cannot be combined with {', '.join(clash)}. "
                     f"It records a verdict without verifying, so there is "
                     f"nothing to approve or reject.")

    if args.log:
        sys.stdout = _Tee(sys.stdout, Path(args.log))  # type: ignore[assignment]
        print(f"logging to {args.log}")

    # ---- the host is configured before anything is claimed ----------------
    pf = preflight.check()
    print(f"transport     : {pf.get('transport')}")
    print(f"our database  : {pf.get('database')}")
    print(f"their database: {pf.get('remoteDatabase')}")
    if not pf["ok"]:
        for p in pf["problems"]:
            print(f"  PROBLEM: {p}")
        return 2

    entries = read_sheet(Path(args.sheet),
                         args.hold_reason_column or args.hold_column)
    if args.limit:
        entries = entries[: args.limit]
    print(f"\n{len(entries)} product(s) in {args.sheet}")

    mode = "LIVE" if args.approve else "SHADOW"
    print(f"mode          : {mode}"
          f"{'  (repairs write)' if args.apply else '  (dry run — nothing is written)'}")
    if args.approve:
        print("stop guard    : bypassed — this script approves whether or not "
              "the tenant is armed")

    if args.approve and not args.yes:
        print(
            "\n  APPROVING PUBLISHES A SHOPIFY LISTING and there is no undo —\n"
            "  the listing has to be removed on Shopify's side.\n"
        )
        if input("  Type APPROVE to continue: ").strip() != "APPROVE":
            print("  stopped.")
            return 1

    # One list, used by all three gates that test the stage: the eligibility
    # predicate here, the re-check at claim, and the approval pre-flight on the
    # far side of the step runner. They have to agree or a product is admitted
    # and then refused.
    stages = ["REVIEW", "LABEL"] if args.include_label else ["REVIEW"]

    ok = held = failed = skipped = 0
    pool: Parallel | None = None
    run_ids: set[str] = set()
    run_started = time.perf_counter()
    for n, entry in enumerate(entries, 1):
        label = entry["sku"] or entry["id"]
        row = resolve(entry, args.include_failed, retry=args.retry,
                      stages=stages)
        if row is None:
            print(f"\n[{n}/{len(entries)}] {label}: NOT FOUND")
            skipped += 1
            continue
        # GENERATION FAILED IS TERMINAL, so it is rejected rather than skipped.
        #
        # The eligibility predicate refuses these, and rightly — there is
        # nothing to verify when the renders never finished. But skipping leaves
        # them sitting in Review forever, re-examined by every future run and
        # never resolvable: no amount of re-checking produces the images that
        # failed to generate. Klekt has 11 and Kilo Kilo had 12.
        #
        # Only when generation is the ONLY thing wrong. A product that also left
        # Review, or already has a verdict, is somebody else's business — the
        # subset test is what keeps this from rejecting on a coincidence.
        # GENERATION IS THE ONE CLAUSE A PRE-CHAIN REJECTION MAY LOOK PAST.
        #
        # Everything else the predicate refuses on — left Review, already
        # verified, decision-rejected — means the product is somebody else's
        # business. Generation is different: COMPLETE will never arrive on its
        # own, so a product stuck at FAILED or IDLE is a permanent resident of
        # Review unless something ends it.
        only_gen_blocks = set(row["failed_clauses"]) <= {"generation finished"}

        # A TITLE THE PIPELINE NEVER REPLACED. "Generating Product..." is the
        # placeholder written at creation and overwritten when generation
        # succeeds; still carrying it means nothing was ever produced. On the
        # BOAS last batch that is 35 of 61 — 25 at IDLE and 10 at FAILED — and
        # without this they are skipped by every run forever.
        title_now = str(row.get("title") or "").strip().lower()
        placeholder_title = (
            bool(args.reject_placeholder_title)
            and only_gen_blocks
            and row["generationStatus"] != "COMPLETE"
            and title_now.startswith(args.reject_placeholder_title.strip().lower())
        )

        failed_gen = (
            args.reject_failed_generation
            and row["generationStatus"] == "FAILED"
            and only_gen_blocks
        )

        # A MISSING CARE LABEL IS TERMINAL WHATEVER GENERATION SAYS, and until
        # now it was never reached for these: --reject-missing-label is checked
        # AFTER the eligibility skip, so a product blocked on generation fell
        # out first and kept its missing label unrecorded. 47 of the 61 have no
        # label at all.
        label_gate = args.reject_missing_label and only_gen_blocks

        if row["failed_clauses"] and not (
                placeholder_title or failed_gen or label_gate):
            print(f"\n[{n}/{len(entries)}] {row['sku']}: SKIPPED — fails "
                  f"{', '.join(row['failed_clauses'])} "
                  f"(stage={row['currentStage']}, "
                  f"generation={row['generationStatus']}, "
                  f"verification={row['verificationStatus']})")
            skipped += 1
            continue

        tenant_id = str(row["tenantId"])
        cfg_row = db.fetch_one(
            'SELECT * FROM "AutoApprovalConfig" WHERE "tenantId" = %(t)s::uuid',
            {"t": tenant_id},
        )
        if not cfg_row:
            print(f"\n[{n}/{len(entries)}] {row['sku']}: SKIPPED — {row['tenant']} "
                  f"has no AutoApprovalConfig row. Open the agent screen for that "
                  f"tenant once to create it.")
            skipped += 1
            continue

        # The run's frozen snapshot carries the MODE THIS INVOCATION ASKED FOR,
        # not the tenant's stored one — otherwise a tenant left on SHADOW would
        # silently ignore --approve, and the Runs tab would disagree with what
        # actually happened.
        cfg_row = dict(cfg_row)
        cfg_row["mode"] = mode
        cfg_row["shadowWritesRepairs"] = bool(args.apply)

        run_id, created = open_run(tenant_id, cfg_row)

        # A REUSED RUN CARRIES THE FIRST INVOCATION'S SNAPSHOT, and this one may
        # have been given different flags. Letting the two disagree is exactly
        # the failure the snapshot exists to prevent — the Runs tab would show
        # SHADOW over products the run actually approved.
        #
        # So when they differ the snapshot is brought up to date AND the change
        # is written to the event log. That second half is what keeps it honest:
        # the run-level settings read true, and when they changed sits on the
        # record between the products either side of it.
        if not created:
            snap = db.fetch_one(
                'SELECT "configSnapshot" FROM "AutoApprovalRun"'
                ' WHERE id = %(r)s::uuid', {"r": run_id})["configSnapshot"] or {}
            was, wrote = snap.get("mode"), bool(snap.get("shadowWritesRepairs"))
            if was != mode or wrote != bool(args.apply):
                db.execute(
                    """
                    UPDATE "AutoApprovalRun"
                       SET "configSnapshot" = jsonb_set(
                             jsonb_set("configSnapshot", '{mode}', %(m)s::jsonb),
                             '{shadowWritesRepairs}', %(w)s::jsonb)
                     WHERE id = %(r)s::uuid
                    """,
                    {"r": run_id, "m": f'"{mode}"',
                     "w": "true" if args.apply else "false"},
                )
                events.emit(
                    tenant_id, "warn",
                    f"run settings changed: mode {was} -> {mode}, "
                    f"writes {wrote} -> {bool(args.apply)}",
                    run_id=run_id,
                )

        run_ids.add(run_id)
        if not enqueue_one(run_id, row, int(cfg_row["maxAttempts"]),
                           retry=args.retry):
            print(f"\n[{n}/{len(entries)}] {row['sku']}: SKIPPED — it already has "
                  f"an open row"
                  f"{'' if args.retry else ' (pass --retry to re-verify it)'}")
            skipped += 1
            continue

        events.emit(tenant_id, "queue",
                    f"{row['sku']} queued by run_from_sheet.py",
                    run_id=run_id, product_id=str(row["id"]))

        print(f"\n[{n}/{len(entries)}] {row['tenant']} · {row['sku']} · "
              f"{(row['title'] or '')[:56]}")
        if created:
            print(f"    opened the Whole-queue run for {row['tenant']}: {run_id}")

        # PARALLEL PATH: collect now, verify after every product is queued.
        #
        # Enqueuing the whole sheet first is deliberate. `one_open_per_product`
        # rejects a product that already has an open row, so doing all the
        # inserts up front settles which products this invocation owns before
        # any long-running work starts — rather than discovering a conflict
        # twenty minutes in, with threads already committed to it.
        if args.workers > 1:
            queued = db.fetch_one(
                'SELECT * FROM "AutoApprovalRunProduct"'
                ' WHERE "runId" = %(r)s::uuid AND "productId" = %(p)s::uuid',
                {"r": run_id, "p": str(row["id"])},
            )
            if not queued:
                print("    SKIPPED — the queue row vanished after enqueue")
                skipped += 1
                continue
            if pool is None:
                snap = db.fetch_one(
                    'SELECT "configSnapshot" FROM "AutoApprovalRun"'
                    ' WHERE id = %(r)s::uuid', {"r": run_id})["configSnapshot"]
                print(f"\n  starting {args.workers} worker(s) — products are "
                      f"verified as they are queued, not after")
                print("  no lease on these rows: do not run a Celery worker "
                      "for this tenant until this finishes\n")
                pool = Parallel(snap, args.workers, stages,
                                args.reject_missing_attrs, args.apply)
            pool.submit({"row": row, "claimed": queued, "run_id": run_id,
                         "n": n, "total": len(entries)})
            continue

        # HOLD-ONLY: record the verdict and move to the next product. Placed
        # before every other branch because it is the whole job — nothing is
        # matted, no model is called and the product is not moved.
        if args.hold_only:
            hold_one(run_id, row, tenant_id,
                     entry.get("reason") or args.hold_only, args.apply)
            held += 1
            continue

        # SELECTIVE HOLD, and it is checked BEFORE the rejections on purpose.
        #
        # A product can qualify for both — on the Midtex sheet 3 of the 49
        # mannequin mismatches also have a missing attribute or a failed
        # generation. Holding wins because rejection ARCHIVES the product,
        # and a garment photographed on the wrong rig is a re-shoot, not a
        # write-off: the record is sound, the photograph is not. Rejecting it
        # would take a fixable product out of Review to save one re-check.
        if args.hold_column and entry.get("reason"):
            hold_one(run_id, row, tenant_id, entry["reason"], args.apply)
            held += 1
            continue

        # Terminal before the chain starts, like a missing care label: the
        # renders never completed and re-running cannot produce them.
        if placeholder_title or failed_gen:
            # Named for what actually happened, because "generation failed" on a
            # product that never started is simply untrue and whoever reads the
            # Rejected tab has to act on the difference: FAILED needs a retry,
            # IDLE means it was never queued.
            if row["generationStatus"] == "FAILED":
                code, why = "GENERATION_FAILED", "generation is failed"
            else:
                code, why = ("GENERATION_NEVER_RAN",
                             f"generation never ran (status "
                             f"{row['generationStatus']}) — the title is still "
                             f"the placeholder")
            print(f"    {why} — rejecting rather than skipping")
            if not args.apply:
                # RELEASE WHAT THE ENQUEUE JUST CREATED. A dry run that leaves
                # the row QUEUED blocks every product after it: claim_next takes
                # the OLDEST queued row for the tenant, so the next iteration
                # claims this one instead of its own and reports "another worker
                # holds this tenant" — for the rest of the sheet.
                release_one(run_id, str(row["id"]),
                            "released: dry run, nothing was written")
                print("    (dry run: nothing written)")
                failed += 1
                continue
            if _record_rejection(run_id, row, tenant_id,
                                 outcome=code, reason=why):
                failed += 1
                continue
            # The reject step refused; leave it alone rather than pressing on
            # into a chain the eligibility predicate already said no to.
            skipped += 1
            continue

        # Foreclosed before the chain starts — see reject_missing_label.
        if args.reject_missing_label and reject_missing_label(
                run_id, row, tenant_id, args.apply):
            failed += 1
            continue

        # It got past the terminal rules only because generation is incomplete.
        # Nothing downstream can run, so release the row and say why rather than
        # handing an unverifiable product to the chain.
        if not only_gen_blocks or row["generationStatus"] != "COMPLETE":
            if row["generationStatus"] != "COMPLETE":
                release_one(run_id, str(row["id"]),
                            "released: generation is not complete")
                print(f"    SKIPPED — generation is "
                      f"{row['generationStatus']}, nothing to verify")
                skipped += 1
                continue

        claimed = claim.claim_next(
            tenant_id, int(cfg_row["leaseSeconds"]),
            include_failed=bool(cfg_row["includeFailedGeneration"]),
            stages=stages,
        )
        if not claimed or str(claimed["productId"]) != str(row["id"]):
            # Another worker took it, or took something else first. Both mean
            # "not ours to run".
            #
            # RELEASE WHAT WE JUST ENQUEUED. Leaving it QUEUED orphans it: the
            # run is about to be closed, so nothing will ever claim that row
            # again, and the product sits at verificationStatus=QUEUED where the
            # eligibility predicate refuses it forever. Only a Celery worker's
            # `cancel_stale_queued` would clean it up, and the whole point of
            # this script is running without one.
            release_one(run_id, str(row["id"]),
                        "released: the claim went to another worker")
            print("    SKIPPED — another worker holds this tenant (row released)")
            skipped += 1
            continue

        started = time.perf_counter()
        rs = cfgmod.settings_from_snapshot(
            db.fetch_one('SELECT "configSnapshot" FROM "AutoApprovalRun"'
                         ' WHERE id = %(r)s::uuid', {"r": run_id})["configSnapshot"]
        )
        # ignore_stop=True: this invocation IS the human decision that the
        # tenant's `enabled` flag stands in for in the unattended loop. Without
        # it, approving one product by hand would require arming the background
        # agent for the whole tenant first — more dangerous than the thing the
        # guard protects against.
        runner.verify_one(claimed, rs, ignore_stop=True, allow_stage=stages)

        after = db.fetch_one(
            'SELECT status, outcome, reason, approved FROM "AutoApprovalRunProduct"'
            ' WHERE id = %(i)s::uuid', {"i": claimed["id"]},
        )
        # The run stays RUNNING on purpose — the next invocation joins it.

        st = (after or {}).get("status")
        print(f"    -> {st} {(after or {}).get('outcome') or ''}"
              f"  ({time.perf_counter() - started:.1f}s)")
        if (after or {}).get("reason"):
            print(f"       {after['reason']}")
        if (after or {}).get("approved"):
            print("       APPROVED — Shopify upsert enqueued")

        # AFTER the chain, so the care-label and extract steps have had their
        # chance to supply what is missing. See reject_missing_attrs.
        if (st == "HELD_FOR_HUMAN" and args.reject_missing_attrs
                and reject_missing_attrs(run_id, row, tenant_id, args.apply)):
            failed += 1
            continue

        if st == "VERIFIED":
            ok += 1
        elif st == "HELD_FOR_HUMAN":
            held += 1
        else:
            failed += 1

    if pool is not None:
        print(f"\n{'-' * 58}")
        print(f"all {pool.submitted} product(s) queued — waiting for the "
              f"{args.workers} worker(s) to drain")
        print(f"{'-' * 58}")
        t0 = time.perf_counter()
        tally = pool.close()
        ok += tally["ok"]; held += tally["held"]; failed += tally["failed"]
        done = tally["ok"] + tally["held"] + tally["failed"]
        tail = time.perf_counter() - t0
        total = time.perf_counter() - run_started
        if done:
            print(f"\n{done} product(s) in {total/60:.1f} min total "
                  f"({total/done:.0f}s each, wall clock)")
            print(f"  of which {tail/60:.1f} min was after the last was queued")

    # THE LAST THING, and only when something was actually approved. An
    # approval that never reached Shopify is the one failure this whole script
    # cannot see from its own verdicts — the row says APPROVED either way.
    if args.approve and run_ids:
        print(f"\n{'-' * 58}")
        print("checking every approved product actually reached Shopify")
        print(f"{'-' * 58}")
        for rid in sorted(run_ids):
            verify_shopify(rid, args.apply)

    print(f"\n{'-' * 58}")
    print(f"verified {ok} · held {held} · failed {failed} · skipped {skipped}")
    print("One Whole-queue run per tenant, still open — run this again and")
    print("the next products join the same run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
