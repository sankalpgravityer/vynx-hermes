#!/usr/bin/env python
"""Every product APPROVED in a date window, re-examined against the new checks.
Database only — nothing is written, no Shopify call, no model call.

    python scripts/audit_approved_window.py --db "postgresql://..." --from 2026-09-10 --to 2026-09-15
    python scripts/audit_approved_window.py --db "..." --from 2026-09-10 --to 2026-09-15 --tenant BOAS
    python scripts/audit_approved_window.py --db "..." --from 2026-09-10 --to 2026-09-15 --out report.xlsx
    python scripts/audit_approved_window.py --db "..." --product <uuid> --judge-images
    python scripts/audit_approved_window.py --db "..." --product <uuid> --rejudge
    python scripts/audit_approved_window.py --db "..." --product <uuid>,<uuid>

ONE PRODUCT, OR A FEW. `--product` examines the named products whatever stage
they are in — a product still in Review is checked for everything the window
run checks except the questions that only make sense once it was approved (who
approved it, past which refused verdict). Up to five named products are also
printed on the terminal, so the workbook is optional. `--judge-images` judges
only what the agent never recorded; `--rejudge` runs the image gate and the
photo audit NOW for the named products whatever the run record says, and the
fresh verdict is the one reported (the agent's own stays in the Agent column).
Same pictures under the same prompt come back from the vision cache with the
current policy applied; set HERMES_VISION_CACHE=0 to force a new model call.

WHAT IT ANSWERS. The agent gained checks this week that products approved
before them never met: the image gate, the photo audit, the twin step, the new
rules, publication verification. This walks every product that entered APPROVED
in the window — by the agent or by a person — and asks, from the database alone,
which of those checks it would fail today and what its channel state says. One
Excel workbook: a Summary sheet, one row per product, one row per issue.

WHAT IT READS. `ProductStageMovement` (who moved it to APPROVED and when) and
`Product.currentStageAt` as a fallback; the product itself through the same
loader the audit uses (`product_audit.load_batch`, so a category is judged
against the tenant's real tree); the last `AutoApprovalRunProduct` row for the
product (did the agent approve it, did the gate and the photo audit run, what
did they say); `MarketplaceListing` and the last `MarketplaceSyncLog` rows
(is it listed, did the publish land, was the publication verified). The Hermes
rules run offline over the loaded record — the same `approval.run_gate` the
chain's reconcile step calls, without the evidence layer.

WHAT IT DOES NOT DO. It does not fetch an image, ask the model or call Shopify:
a product's gate and photo-audit verdicts come from the run record, and a
product the agent never judged is reported as exactly that. It does not write
anywhere but the workbook. The connection is opened read-only.

ISSUE CODES, and why each is a risk:

  MANUAL_APPROVAL             moved to APPROVED by a person, never verified by the agent
  NO_GATE_RECORD              the lead render was never judged by the image gate
  NO_PHOTO_AUDIT              the photographs were never checked for wear or defects
  GATE_REFUSED_BUT_APPROVED   the gate refused the render and the product was approved anyway
  PHOTO_AUDIT_HELD_BUT_APPROVED  the photo audit held it (GRADE_SUSPECT / IMAGE_DEFECT); approved anyway
  GATE_WOULD_REFUSE / PHOTO_AUDIT_WOULD_HOLD   only with --judge-images: never judged when approved,
                              judged now, and refused / held
  AGENT_HELD_BUT_APPROVED     the agent's last verdict was HELD or FAILED; a person overrode it
  RULE:<id>                   a Hermes rule fires on it today, blocking or advisory, at its own severity
  RENDERS_INCOMPLETE          fewer than five AI views on file
  GENERATION_NOT_COMPLETE     generation status not COMPLETE, or regenerating
  NO_CARE_LABEL               no care-label photograph
  UNMATTED_VIEW               a garment view with no cut-out
  RAW_LEAD / LABEL_LEAD       the gallery leads with a raw photo or a label
  TWIN_BLANK                  a C-twin with blank fields its parent holds
  NO_LISTING                  tenant has a marketplace account, product has no listing row
  LISTING_NOT_PUBLISHED       listing exists but is not PUBLISHED
  LISTING_SYNC_FAILED         the last sync failed
  PUBLICATION_UNVERIFIED      the publish landed but the storefront publication check failed
  NO_SHOPIFY_ID               listed as PUBLISHED without a Shopify product id
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import policy, settings  # noqa: E402

BATCH = 200
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
AI_VIEWS = ("AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP")
GARMENT_VIEWS = ("FRONT", "BACK")
RANK = {"high": 0, "medium": 1, "low": 2}

# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #
APPROVED_SQL = """
WITH moved AS (
    SELECT DISTINCT ON (m."productId")
           m."productId"::text AS pid, m."enteredAt" AS approved_at,
           m."performedById"::text AS by_id, m."fromStage"::text AS from_stage
      FROM "ProductStageMovement" m
     WHERE m."toStage" = 'APPROVED'
       AND m."enteredAt" >= %(start)s AND m."enteredAt" < %(end)s
     ORDER BY m."productId", m."enteredAt" DESC
),
current_only AS (
    SELECT p.id::text AS pid, p."currentStageAt" AS approved_at,
           NULL::text AS by_id, NULL::text AS from_stage
      FROM "Product" p
     WHERE p."currentStage" = 'APPROVED'
       AND p."currentStageAt" >= %(start)s AND p."currentStageAt" < %(end)s
       AND NOT EXISTS (SELECT 1 FROM moved WHERE moved.pid = p.id::text)
),
found AS (SELECT * FROM moved UNION ALL SELECT * FROM current_only)
SELECT f.pid, f.approved_at, f.by_id, f.from_stage,
       p."currentStage"::text, p."shopifyProductId", p."verificationStatus"::text,
       p."verificationOutcome", t.name
  FROM found f
  JOIN "Product" p ON p.id = f.pid::uuid
  JOIN "Tenant"  t ON t.id = p."tenantId"
 WHERE p."isDeleted" = false
   AND (%(tenant)s::text IS NULL OR t.name ILIKE %(tenant)s)
 ORDER BY f.approved_at
"""

# `--product`: the named products whatever their stage, with their most recent
# move to APPROVED when there was one. A product still in Review is examined
# too — the approval-only checks are then simply not asked.
PRODUCTS_SQL = """
SELECT p.id::text, m."enteredAt", m."performedById"::text, m."fromStage"::text,
       p."currentStage"::text, p."shopifyProductId", p."verificationStatus"::text,
       p."verificationOutcome", t.name
  FROM "Product" p
  JOIN "Tenant" t ON t.id = p."tenantId"
  LEFT JOIN LATERAL (
        SELECT x."enteredAt", x."performedById", x."fromStage"
          FROM "ProductStageMovement" x
         WHERE x."productId" = p.id AND x."toStage" = 'APPROVED'
         ORDER BY x."enteredAt" DESC
         LIMIT 1) m ON true
 WHERE p.id = ANY(%(ids)s::uuid[])
   AND p."isDeleted" = false
 ORDER BY p.sku
"""

AGENT_SQL = """
SELECT DISTINCT ON (r."productId")
       r."productId"::text, r."runId"::text, r.status::text, r.outcome, r.approved,
       r.deltas, r."completedAt"
  FROM "AutoApprovalRunProduct" r
 WHERE r."productId" = ANY(%(ids)s::uuid[])
 ORDER BY r."productId", r."completedAt" DESC NULLS LAST, r."createdAt" DESC
"""

LISTINGS_SQL = """
SELECT l."productId"::text, m.code, l.status::text, l."lastSyncStatus"::text,
       l."externalListingId", l."lastSyncError", l."lastSyncAt"
  FROM "MarketplaceListing" l
  JOIN "MarketplaceAccount" a ON a.id = l."marketplaceAccountId"
  JOIN "Marketplace" m ON m.id = a."marketplaceId"
 WHERE l."productId" = ANY(%(ids)s::uuid[])
"""

SYNC_LOG_SQL = """
SELECT DISTINCT ON (s."productId", s.op)
       s."productId"::text, s.op, s.status, s."errorMessage", s."startedAt"
  FROM "MarketplaceSyncLog" s
 WHERE s."productId" = ANY(%(ids)s::uuid[])
   AND s.op IN ('publication-verify', 'listing.publish', 'publish', 'listing.unpublish')
 ORDER BY s."productId", s.op, s."startedAt" DESC
"""

ACCOUNTS_SQL = """
SELECT a."tenantId"::text, count(*)
  FROM "MarketplaceAccount" a
 GROUP BY a."tenantId"
"""

USERS_SQL = """
SELECT id::text, COALESCE(NULLIF(TRIM(CONCAT_WS(' ', "firstName", "lastName")), ''), email)
  FROM "User" WHERE id = ANY(%(ids)s::uuid[])
"""

PARENTS_SQL = """
SELECT p.id::text, p."tenantId"::text, upper(p.sku)
  FROM "Product" p
 WHERE p."isDeleted" = false
   AND (p."tenantId"::text, upper(p.sku)) IN (SELECT unnest(%(tids)s::text[]), unnest(%(skus)s::text[]))
"""


# --------------------------------------------------------------------------- #
# The assessment, cold
# --------------------------------------------------------------------------- #
def assess(*, record: dict[str, Any], media: list[dict[str, Any]], approved: dict[str, Any],
           agent: dict[str, Any] | None, blocking: list[dict[str, Any]],
           listings: list[dict[str, Any]], logs: dict[str, dict[str, Any]],
           has_account: bool, shopify_id: Any, twin_gains: list[str],
           advisory: list[dict[str, Any]] = (), judged: dict[str, Any] | None = None,
           ) -> list[dict[str, str]]:
    """Everything wrong with one approved product, as (code, severity, detail).

    Pure: every input is a plain dict or list, so the whole decision table is
    testable without a database. `blocking` and `advisory` are
    `approval.run_gate(...)["blocking"]` / `["advisory"]` computed by the caller;
    `logs` is op -> the latest sync-log row; `judged` is what `--judge-images`
    found NOW for a product the agent never judged: {"gate": verdict dict | None,
    "photos": verdict dict | None}.
    """
    issues: list[dict[str, str]] = []

    def add(code: str, severity: str, detail: str) -> None:
        issues.append({"code": code, "severity": severity, "detail": detail})

    # ---- who approved it, and with what evidence ----------------------------
    deltas = (agent or {}).get("deltas") or {}
    gate = deltas.get("gate") or {}
    photos = deltas.get("photos") or {}
    agent_approved = bool(agent and agent.get("approved"))
    judged = judged or {}
    # A product examined by id may never have been approved (still in Review,
    # or rejected). The checks that ask "who approved this, and past what
    # evidence" are then not questions; what was judged still is.
    ever_approved = approved.get("at") is not None or record.get("currentStage") == "APPROVED"
    but_approved = "_BUT_APPROVED" if ever_approved else ""

    if ever_approved and not agent_approved:
        who = approved.get("by") or ("the stage machine" if approved.get("from_stage") else "unknown")
        verif = f'{record.get("verificationStatus") or "—"}'
        if record.get("verificationOutcome"):
            verif += f' / {record["verificationOutcome"]}'
        add("MANUAL_APPROVAL", "medium",
            f"approved by {who}; agent verification {verif}"
            + (f'; agent\'s last verdict {agent["status"]} / {agent.get("outcome") or "—"}' if agent else ""))
    if ever_approved and agent and agent.get("status") in ("HELD_FOR_HUMAN", "FAILED") and not agent_approved:
        add("AGENT_HELD_BUT_APPROVED", "medium",
            f'agent said {agent["status"]} ({agent.get("outcome") or "—"}) and the product was approved anyway')

    if gate:
        if gate.get("action") in ("regen", "review"):
            add(f"GATE_REFUSED{but_approved}", "high" if ever_approved else "medium",
                f'gate {gate.get("action")} {gate.get("code") or ""}: {"; ".join(gate.get("reasons") or [])}'.strip())
    elif judged.get("gate"):
        now = judged["gate"]
        if now.get("action") in ("regen", "review") and not now.get("unavailable"):
            add("GATE_WOULD_REFUSE", "high",
                f'judged now — {now.get("code") or ""}: {"; ".join(now.get("reasons") or [])}'.strip())
        elif now.get("unavailable"):
            add("NO_GATE_RECORD", "medium", f'never judged; could not judge now — {"; ".join(now.get("reasons") or [])}')
    else:
        add("NO_GATE_RECORD", "medium", "the lead render was never judged by the image gate")

    if photos:
        if photos.get("action") in ("review", "regen"):
            add(f"PHOTO_AUDIT_HELD{but_approved}", "high" if ever_approved else "medium",
                f'photo audit {photos.get("code") or ""}: {"; ".join(photos.get("reasons") or [])}'.strip())
    elif judged.get("photos"):
        now = judged["photos"]
        if now.get("action") == "review":
            add("PHOTO_AUDIT_WOULD_HOLD", "high",
                f'judged now — {now.get("code") or ""}: {"; ".join(now.get("reasons") or [])}'.strip())
        elif now.get("action") == "regen":
            # A render the audit calls defective (MID-000253: knees blurred to
            # white on the AI_FRONT_34 while the lead passed the gate). Not a
            # hold for a person: the chain re-renders exactly these views.
            views = ", ".join(str(v) for v in (now.get("bad_views") or [])) or "the named view"
            add("RENDER_DEFECT", "high",
                f'judged now — {"; ".join(now.get("reasons") or [])} -> re-render {views} '
                f'(same model; repair_product.py --apply, or check_product.py --apply --render)')
        elif now.get("action") == "skipped" and "could not run" in " ".join(now.get("reasons") or []):
            add("NO_PHOTO_AUDIT", "low", f'never judged; could not judge now — {"; ".join(now.get("reasons") or [])}')
        elif now.get("soft") and not now.get("bad_cutouts"):
            add("PHOTO_AUDIT_SOFT_FLAGS", "low", "judged now — " + "; ".join(now["soft"])[:300])
        # A cut-out missing part of the garment (MID-000569: the neckband cut
        # away) rides beside whatever else was found; the chain re-cuts it.
        if now.get("bad_cutouts") and now.get("action") != "review":
            text = "; ".join(str(r) for r in [*(now.get("reasons") or []), *(now.get("soft") or [])]
                             if str(r).startswith("CUTOUT DEFECT")) or "a cut-out is missing part of the garment"
            add("CUTOUT_DEFECT", "medium",
                f'judged now — {text} -> re-cut {", ".join(str(v) for v in now["bad_cutouts"])} '
                f'from the raw archive (repair_product.py --apply)')
    else:
        add("NO_PHOTO_AUDIT", "low", "the photographs were never checked for wear or gallery defects")

    # ---- the rules, today ---------------------------------------------------
    #
    # EVERY finding, at its own severity, not only the blocking ones: the new
    # rules — inventory, the other gender in the copy, supplier, retail floor,
    # material as a sentence — are advisory by design, and a re-examination
    # that hid them would answer "is it approvable" when the question is "what
    # is wrong with it". CONF.001 fires once per low-confidence field and is
    # folded into one line so it cannot bury the rest.
    conf_fields: list[str] = []
    for f in [*blocking, *advisory]:
        rid = str(f.get("rule_id") or "")
        if rid == "CONF.001":
            conf_fields.extend(str(x) for x in (f.get("fields") or []))
            continue
        raw_sev = str(f.get("severity", "")).lower()
        sev = "high" if raw_sev in ("critical", "high") else ("medium" if raw_sev == "medium" else "low")
        add(f"RULE:{rid}", sev, str(f.get("message") or "")[:300])
    if conf_fields:
        add("RULE:CONF.001", "low",
            f'{len(conf_fields)} field(s) below the extraction-confidence floor: {", ".join(sorted(set(conf_fields)))}')

    # ---- the pictures on file -----------------------------------------------
    # The loader now also carries the superseded RAW originals (readiness phase
    # 3, so a cut-out can be compared with what it was cut from); they are not
    # on the product and must not count as pictures on file.
    live = [m for m in media if (m.get("mediaType") or "IMAGE") == "IMAGE"
            and m.get("isCurrent", True) and not m.get("deletedAt")]
    views = {m.get("view") for m in live}
    missing = [v for v in AI_VIEWS if v not in views]
    if missing:
        add("RENDERS_INCOMPLETE", "high", f'{5 - len(missing)}/5 renders — missing {", ".join(missing)}')
    if record.get("generationStatus") != "COMPLETE" or record.get("isRegenerating"):
        add("GENERATION_NOT_COMPLETE", "medium",
            f'generation {record.get("generationStatus")}'
            + (", regenerating" if record.get("isRegenerating") else ""))
    if not (record.get("careLabelCount") or 0):
        add("NO_CARE_LABEL", "high", "no care-label photograph on file")
    matted = {m.get("view") for m in live if m.get("view") in GARMENT_VIEWS and m.get("processing") == "BG_REMOVED"}
    unmatted = sorted({m.get("view") for m in live if m.get("view") in GARMENT_VIEWS
                       and m.get("processing") == "RAW"} - matted)
    if unmatted:
        add("UNMATTED_VIEW", "medium", f'no cut-out for {", ".join(unmatted)}')
    lead_url = (record.get("images") or [None])[0] if isinstance(record.get("images"), list) else None
    lead = next((m for m in live if m.get("url") == lead_url), None) if lead_url else None
    if lead is not None:
        if lead.get("view") in ("LABEL", "SIZE_CHART"):
            add("LABEL_LEAD", "medium", f'gallery leads with a {lead.get("view")}')
        elif lead.get("view") in GARMENT_VIEWS and lead.get("processing") == "RAW" and matted:
            add("RAW_LEAD", "medium", "gallery leads with the raw photograph while a cut-out exists")

    if twin_gains:
        add("TWIN_BLANK", "medium", f'blank {", ".join(twin_gains)} that the parent SKU holds')

    # ---- the channel, from this side --------------------------------------
    if has_account:
        if not listings and ever_approved:
            add("NO_LISTING", "high", "tenant has a marketplace account; the product has no listing row")
        for lst in listings:
            code = lst.get("code") or "?"
            if lst.get("lastSyncStatus") == "FAILED":
                add("LISTING_SYNC_FAILED", "high",
                    f'{code}: last sync FAILED — {str(lst.get("lastSyncError") or "")[:200]}')
            if lst.get("status") != "PUBLISHED":
                add("LISTING_NOT_PUBLISHED", "high",
                    f'{code}: listing {lst.get("status")} / sync {lst.get("lastSyncStatus") or "—"}')
            elif not lst.get("externalListingId") and not shopify_id:
                add("NO_SHOPIFY_ID", "medium", f"{code}: PUBLISHED with no Shopify product id")
        verify = logs.get("publication-verify")
        if verify and verify.get("status") == "FAILED":
            add("PUBLICATION_UNVERIFIED", "high",
                f'published to no sales channel — {str(verify.get("errorMessage") or "")[:200]}')

    return issues


def worst(issues: list[dict[str, str]]) -> str:
    return min((i["severity"] for i in issues), key=lambda s: RANK[s], default="none")


# --------------------------------------------------------------------------- #
# Gathering
# --------------------------------------------------------------------------- #
def _fetch_dicts(cur, sql: str, params: dict[str, Any], columns: tuple[str, ...]) -> list[dict[str, Any]]:
    cur.execute(sql, params)
    return [dict(zip(columns, r)) for r in cur.fetchall()]


def judge_now(record: dict[str, Any], media: list[dict[str, Any]], agent: dict[str, Any] | None,
              pol: dict[str, Any], force: bool = False) -> dict[str, Any]:
    """`--judge-images`: the gate and the photo audit, run now, for whichever of
    the two the agent never recorded — or both, with `force` (`--rejudge`).
    Vision calls (cached); no database write."""
    from app.imaging import photo_audit, quality_gate

    deltas = (agent or {}).get("deltas") or {}
    out: dict[str, Any] = {}
    if force or not deltas.get("gate"):
        out["gate"] = quality_gate.judge(
            media, gender=record.get("gender"), category=record.get("category"),
            subcategory=record.get("subCategory"), pol=pol).as_dict()
    if force or not deltas.get("photos"):
        out["photos"] = photo_audit.judge(
            media, grade_severity=record.get("gradeSeverity"),
            grade_label=record.get("gradeLabel") or record.get("grade"), pol=pol).as_dict()
    return out


def fresh_over_recorded(agent: dict[str, Any] | None, judged: dict[str, Any]) -> dict[str, Any] | None:
    """The run record as assess() should read it once something was judged now.

    A verdict judged now replaces the agent's recorded one for the same check,
    so `--rejudge` reports the fresh answer (GATE_WOULD_REFUSE, "judged now — …")
    rather than the old one. Without `--rejudge` nothing was judged that the
    agent had recorded, so this changes nothing. Status, outcome and `approved`
    are untouched — who approved it is still a matter of record.
    """
    if not agent or not judged:
        return agent
    deltas = agent.get("deltas") or {}
    return {**agent, "deltas": {k: v for k, v in deltas.items() if k not in judged}}


def verdict_line(v: dict[str, Any] | None) -> str:
    """One terminal line for a gate or photo-audit verdict dict."""
    if not v:
        return "not judged"
    action = str(v.get("action") or "")
    head = "could not run" if v.get("unavailable") else {
        "ok": "passed", "regen": "REFUSED", "review": "HELD", "skipped": "skipped"}.get(action, action)
    line = head + (f' {v["code"]}' if v.get("code") else "")
    if v.get("reasons"):
        line += " — " + "; ".join(str(r) for r in v["reasons"])
    if v.get("soft"):
        line += f' (soft: {"; ".join(str(s) for s in v["soft"])})'
    if v.get("cached"):
        line += " (cached)"
    return line[:300]


def gather(dsn: str, start: datetime | None, end: datetime | None, tenant: str | None,
           limit: int | None, quiet: bool = False,
           judge_images: bool = False, workers: int = 1,
           product_ids: list[str] | None = None, rejudge: bool = False) -> list[dict[str, Any]]:
    """Every approved product in the window — or the named products, whatever
    their stage — with everything assess() needs.

    `workers` parallelises only the `--judge-images` vision calls, per batch:
    they are network-bound (image download plus one model call each, 10–30 s),
    so eight threads make an hour of a working day. The database work stays on
    the one read-only connection and is fast anyway. `rejudge` judges every
    product now, recorded verdict or not; the CLI allows it only with
    `product_ids`, so a window run can never re-spend a day of vision calls.
    """
    from concurrent.futures import ThreadPoolExecutor

    from app import approval, twins

    pol = policy()
    with product_audit.connect(dsn, read_only=True, statement_timeout_s=180) as conn, conn.cursor() as cur:
        columns = ("pid", "approved_at", "by_id", "from_stage", "stage",
                   "shopify_id", "verification_status", "verification_outcome", "tenant")
        if product_ids:
            approved = _fetch_dicts(cur, PRODUCTS_SQL, {"ids": product_ids}, columns)
            missing = set(product_ids) - {a["pid"] for a in approved}
            if not quiet:
                print(f"{len(approved)} of {len(product_ids)} product(s) found"
                      + (f" — not found or deleted: {', '.join(sorted(missing))}" if missing else ""))
        else:
            assert start is not None and end is not None
            approved = _fetch_dicts(cur, APPROVED_SQL,
                                    {"start": start, "end": end, "tenant": tenant}, columns)
            if limit:
                approved = approved[:limit]
            if not quiet:
                print(f"{len(approved)} product(s) entered APPROVED between {start:%Y-%m-%d} and "
                      f"{(end - timedelta(days=1)):%Y-%m-%d}" + (f" for {tenant}" if tenant else ""))
        if not approved:
            return []

        cur.execute(ACCOUNTS_SQL)
        accounts = {r[0]: int(r[1]) for r in cur.fetchall()}

        by_ids = sorted({a["by_id"] for a in approved if a["by_id"]})
        users: dict[str, str] = {}
        if by_ids:
            try:
                cur.execute(USERS_SQL, {"ids": by_ids})
                users = {r[0]: r[1] for r in cur.fetchall()}
            except Exception:  # noqa: BLE001 — a User table shaped differently is not fatal
                conn.rollback()

        out: list[dict[str, Any]] = []
        contexts: dict[str, dict[str, Any]] = {}
        for i in range(0, len(approved), BATCH):
            chunk = approved[i:i + BATCH]
            ids = [a["pid"] for a in chunk]
            # Tenant contexts once each; load_batch skips products whose tenant is missing.
            cur.execute('SELECT DISTINCT "tenantId"::text FROM "Product" WHERE id = ANY(%s::uuid[])', (ids,))
            for (tid,) in cur.fetchall():
                if tid not in contexts:
                    contexts[tid] = product_audit.load_tenant_context(cur, tid)
            loaded = {l["record"]["id"]: l for l in product_audit.load_batch(cur, ids, contexts)}

            agents = {r["pid"]: r for r in _fetch_dicts(
                cur, AGENT_SQL, {"ids": ids},
                ("pid", "run_id", "status", "outcome", "approved", "deltas", "completed_at"))}
            listings: dict[str, list[dict[str, Any]]] = {}
            for r in _fetch_dicts(cur, LISTINGS_SQL, {"ids": ids},
                                  ("pid", "code", "status", "lastSyncStatus", "externalListingId",
                                   "lastSyncError", "lastSyncAt")):
                listings.setdefault(r["pid"], []).append(r)
            logs: dict[str, dict[str, dict[str, Any]]] = {}
            for r in _fetch_dicts(cur, SYNC_LOG_SQL, {"ids": ids},
                                  ("pid", "op", "status", "errorMessage", "startedAt")):
                logs.setdefault(r["pid"], {})[r["op"]] = r

            # Twins: the parent's record for every C-SKU in the chunk, one query.
            twin_of: dict[str, tuple[str, str]] = {}
            for pid, l in loaded.items():
                parent = twins.parent_sku(l["record"].get("sku"), pol)
                if parent:
                    twin_of[pid] = (l["record"]["tenantId"], parent.upper())
            parents: dict[tuple[str, str], dict[str, Any]] = {}
            if twin_of:
                cur.execute(PARENTS_SQL, {"tids": [t for t, _ in twin_of.values()],
                                          "skus": [s for _, s in twin_of.values()]})
                prow = cur.fetchall()
                if prow:
                    for pl in product_audit.load_batch(cur, [r[0] for r in prow], contexts):
                        parents[(pl["record"]["tenantId"], str(pl["record"]["sku"]).upper())] = pl["record"]

            # The vision calls for this chunk, in parallel when asked. Done before
            # the per-product loop so the loop itself stays simple and ordered.
            judged_by_pid: dict[str, dict[str, Any]] = {}
            if judge_images or rejudge:
                todo = [(a["pid"], loaded[a["pid"]]) for a in chunk if a["pid"] in loaded]
                if workers > 1 and len(todo) > 1:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        results = pool.map(
                            lambda item: (item[0], judge_now(item[1]["record"], item[1]["media"],
                                                             agents.get(item[0]), pol, force=rejudge)),
                            todo)
                        judged_by_pid = dict(results)
                else:
                    for pid, l in todo:
                        judged_by_pid[pid] = judge_now(l["record"], l["media"], agents.get(pid), pol,
                                                       force=rejudge)

            for a in chunk:
                l = loaded.get(a["pid"])
                if l is None:
                    continue
                record, media = l["record"], l["media"]
                gate = approval.run_gate({**record, "media": media}, catalog=l["catalog"],
                                         imagery_settings=l["imagery_settings"], llm=None)
                gains: list[str] = []
                key = twin_of.get(a["pid"])
                if key and key in parents:
                    gains = [x["field"] for x in twins.inheritance_plan(record, parents[key], pol)]
                approved_info = {"at": a["approved_at"], "by": users.get(a["by_id"] or "", a["by_id"]),
                                 "from_stage": a["from_stage"]}
                record = {**record, "verificationStatus": a["verification_status"],
                          "verificationOutcome": a["verification_outcome"]}
                agent_row = agents.get(a["pid"])
                judged = judged_by_pid.get(a["pid"], {})
                # With --rejudge the fresh verdicts stand in for the recorded ones.
                as_read = fresh_over_recorded(agent_row, judged)
                issues = assess(record=record, media=media, approved=approved_info,
                                agent=as_read, blocking=gate["blocking"], advisory=gate["advisory"],
                                listings=listings.get(a["pid"], []), logs=logs.get(a["pid"], {}),
                                has_account=bool(accounts.get(record["tenantId"])),
                                shopify_id=a["shopify_id"], twin_gains=gains, judged=judged)
                recorded = (as_read or {}).get("deltas") or {}
                out.append({
                    "pid": a["pid"], "sku": record.get("sku"), "title": record.get("title"),
                    "tenant": record.get("tenantName"), "approved_at": a["approved_at"],
                    "approved_by": approved_info["by"] or ("agent" if (agent_row or {}).get("approved") else "—"),
                    "stage_now": a["stage"], "verification": a["verification_status"],
                    "verification_outcome": a["verification_outcome"],
                    "agent": agent_row,
                    "gate": recorded.get("gate"), "photos": recorded.get("photos"),
                    "judged": judged,
                    "renders": sum(1 for v in AI_VIEWS if v in {m.get("view") for m in media}),
                    "care_labels": record.get("careLabelCount") or 0,
                    "listings": listings.get(a["pid"], []), "shopify_id": a["shopify_id"],
                    "publication_verify": (logs.get(a["pid"], {}).get("publication-verify") or {}).get("status"),
                    "blocking": [f["rule_id"] for f in gate["blocking"]],
                    "advisory": len(gate["advisory"]),
                    "issues": issues, "worst": worst(issues),
                    "edit_url": record.get("editUrl"),
                })
            if not quiet:
                print(f"  {min(i + BATCH, len(approved))}/{len(approved)} examined")
    return out


# --------------------------------------------------------------------------- #
# The workbook
# --------------------------------------------------------------------------- #
FILL = {"high": "FDECEA", "medium": "FFF8E1", "low": "EEF2FB", "none": "E8F5E9"}

PRODUCT_COLUMNS = [
    ("Worst", 9), ("Issues", 8), ("Issue codes", 46), ("SKU", 14), ("Title", 40), ("Tenant", 14),
    ("Approved at", 17), ("Approved by", 22), ("Stage now", 11), ("Agent verdict", 16),
    ("Gate", 26), ("Photo audit", 26), ("Renders", 8), ("Care labels", 10),
    ("Blocking rules today", 26), ("Advisory", 9), ("Listing", 22), ("Publication verify", 12),
    ("Shopify id", 16), ("Product id", 38), ("Edit URL", 60),
]
ISSUE_COLUMNS = [("Severity", 9), ("Code", 30), ("SKU", 14), ("Tenant", 14), ("Approved at", 17),
                 ("Detail", 100), ("Product id", 38), ("Edit URL", 60)]


METHOD_DB_ONLY = "database only — no Shopify call, no model call, nothing written"


def write_workbook(rows: list[dict[str, Any]], out: Path, *, dsn: str, start: datetime | None,
                   end: datetime | None, tenant: str | None, method: str = METHOD_DB_ONLY) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    hdr_fill = PatternFill("solid", fgColor="1F3864")
    hdr_font = Font(bold=True, color="FFFFFF")

    def header(ws, columns):
        for i, (name, width) in enumerate(columns, start=1):
            c = ws.cell(row=1, column=i, value=name)
            c.fill, c.font = hdr_fill, hdr_font
            ws.column_dimensions[get_column_letter(i)].width = width
        ws.freeze_panes = "A2"

    # ---- Summary ------------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    counts = Counter(i["code"] for r in rows for i in r["issues"])
    sev_of = {i["code"]: i["severity"] for r in rows for i in r["issues"]}
    affected = Counter(i["code"] for r in rows for i in {x["code"]: x for x in r["issues"]}.values())
    ws.cell(row=1, column=1, value="Approved products re-examined against the new checks").font = Font(bold=True, size=14)
    meta = [
        ("Window", (f"{start:%Y-%m-%d} to {(end - timedelta(days=1)):%Y-%m-%d} (inclusive, by the time the product entered APPROVED)"
                    if start and end else f"{len(rows)} product(s) named on the command line, any stage")),
        ("Tenant", tenant or "all"),
        ("Database", _redact(dsn)),
        ("Products", len(rows)),
        ("With at least one issue", sum(1 for r in rows if r["issues"])),
        ("Worst = high", sum(1 for r in rows if r["worst"] == "high")),
        ("Worst = medium", sum(1 for r in rows if r["worst"] == "medium")),
        ("Approved by the agent", sum(1 for r in rows if (r["agent"] or {}).get("approved"))),
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Method", method),
    ]
    for n, (k, v) in enumerate(meta, start=3):
        ws.cell(row=n, column=1, value=k).font = Font(bold=True)
        ws.cell(row=n, column=2, value=v)
    r0 = 3 + len(meta) + 1
    for i, name in enumerate(("Issue code", "Severity", "Occurrences", "Products affected", "Meaning"), start=1):
        c = ws.cell(row=r0, column=i, value=name)
        c.fill, c.font = hdr_fill, hdr_font
    for n, (code, cnt) in enumerate(sorted(counts.items(), key=lambda kv: (RANK[sev_of[kv[0]]], -kv[1])), start=r0 + 1):
        ws.cell(row=n, column=1, value=code)
        ws.cell(row=n, column=2, value=sev_of[code])
        ws.cell(row=n, column=3, value=cnt)
        ws.cell(row=n, column=4, value=affected[code])
        ws.cell(row=n, column=5, value=MEANING.get(code.split(":")[0], "a Hermes rule blocks this product today"))
        for col in range(1, 6):
            ws.cell(row=n, column=col).fill = PatternFill("solid", fgColor=FILL[sev_of[code]])
    for col, width in ((1, 34), (2, 10), (3, 13), (4, 18), (5, 90)):
        ws.column_dimensions[get_column_letter(col)].width = width

    # ---- Products -----------------------------------------------------------
    ws = wb.create_sheet("Products")
    header(ws, PRODUCT_COLUMNS)
    for n, r in enumerate(sorted(rows, key=lambda r: (RANK.get(r["worst"], 3), -len(r["issues"]), str(r["sku"]))), start=2):
        gate, photos, agent = r["gate"] or {}, r["photos"] or {}, r["agent"] or {}
        judged = r.get("judged") or {}
        listing = "; ".join(f'{l["code"]} {l["status"]}/{l["lastSyncStatus"] or "—"}' for l in r["listings"]) or "—"

        def picture(recorded: dict[str, Any], now: dict[str, Any] | None) -> str:
            if recorded:
                return f'{recorded.get("action")} {recorded.get("code") or ""}'.strip()
            if now:
                head = f'judged now: {now.get("action")} {now.get("code") or ""}'.strip()
                return head + (f' ({"; ".join(now["soft"])[:80]})' if now.get("soft") else "")
            return "not judged"
        values = [
            r["worst"], len(r["issues"]), ", ".join(sorted({i["code"] for i in r["issues"]})),
            r["sku"], (r["title"] or "")[:80], r["tenant"],
            r["approved_at"].strftime("%Y-%m-%d %H:%M") if r["approved_at"] else "",
            r["approved_by"] or "—", r["stage_now"],
            f'{agent.get("status")} / {agent.get("outcome") or "—"}' if agent else "never run",
            picture(gate, judged.get("gate")), picture(photos, judged.get("photos")),
            f'{r["renders"]}/5', r["care_labels"], ", ".join(r["blocking"]) or "—", r["advisory"],
            listing, r["publication_verify"] or "—", r["shopify_id"] or "—", r["pid"], r["edit_url"] or "",
        ]
        for col, v in enumerate(values, start=1):
            c = ws.cell(row=n, column=col, value=v)
            c.alignment = Alignment(vertical="top", wrap_text=col in (3, 5, 15, 17))
        ws.cell(row=n, column=1).fill = PatternFill("solid", fgColor=FILL.get(r["worst"], FILL["none"]))
    ws.auto_filter.ref = f"A1:{get_column_letter(len(PRODUCT_COLUMNS))}{max(ws.max_row, 2)}"

    # ---- Issues -------------------------------------------------------------
    ws = wb.create_sheet("Issues")
    header(ws, ISSUE_COLUMNS)
    flat = [(r, i) for r in rows for i in r["issues"]]
    flat.sort(key=lambda ri: (RANK[ri[1]["severity"]], ri[1]["code"], str(ri[0]["sku"])))
    for n, (r, i) in enumerate(flat, start=2):
        values = [i["severity"], i["code"], r["sku"], r["tenant"],
                  r["approved_at"].strftime("%Y-%m-%d %H:%M") if r["approved_at"] else "",
                  i["detail"], r["pid"], r["edit_url"] or ""]
        for col, v in enumerate(values, start=1):
            c = ws.cell(row=n, column=col, value=v)
            c.alignment = Alignment(vertical="top", wrap_text=col == 6)
        ws.cell(row=n, column=1).fill = PatternFill("solid", fgColor=FILL[i["severity"]])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(ISSUE_COLUMNS))}{max(ws.max_row, 2)}"

    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


MEANING = {
    "MANUAL_APPROVAL": "moved to APPROVED by a person; the agent never verified it",
    "NO_GATE_RECORD": "the lead render was never judged by the image gate (no model, bad face, wrong gender, broken body)",
    "NO_PHOTO_AUDIT": "the photographs were never checked for wear against the grade or for gallery defects",
    "GATE_REFUSED_BUT_APPROVED": "the image gate refused the render and the product was approved anyway",
    "PHOTO_AUDIT_HELD_BUT_APPROVED": "the photo audit held or refused it (GRADE_SUSPECT / IMAGE_DEFECT / RENDER_DEFECT) and it was approved anyway",
    "GATE_REFUSED": "not approved (yet); the image gate refused the render on the agent's last run",
    "PHOTO_AUDIT_HELD": "not approved (yet); the photo audit held or refused it on the agent's last run",
    "GATE_WOULD_REFUSE": "never judged when approved; judged now with --judge-images, the image gate refuses the render",
    "PHOTO_AUDIT_WOULD_HOLD": "never judged when approved; judged now with --judge-images, the photo audit holds it",
    "RENDER_DEFECT": "judged now with --judge-images: a render carries a visible AI defect (a smeared limb, duplicated garments, clutter) or shows a different model than the other renders — the chain re-renders that view with the same model",
    "CUTOUT_DEFECT": "judged now with --judge-images: a cut-out is missing part of the garment (a collar or sleeve the mask ate) or keeps a stand or hand — the chain re-cuts it from the raw archive",
    "PHOTO_AUDIT_SOFT_FLAGS": "judged now with --judge-images: passes, with soft flags worth a look",
    "AGENT_HELD_BUT_APPROVED": "the agent's last verdict was HELD or FAILED; a person overrode it",
    "RULE": "a Hermes rule blocks this product today — the Issues sheet carries each rule's own message",
    "RENDERS_INCOMPLETE": "fewer than five AI views on file — the listing is missing renders",
    "GENERATION_NOT_COMPLETE": "generation not COMPLETE or still regenerating",
    "NO_CARE_LABEL": "no care-label photograph — brand, size and material were never evidenced",
    "UNMATTED_VIEW": "a garment view has no background-removed cut-out",
    "RAW_LEAD": "the gallery leads with the raw photograph while a cut-out exists",
    "LABEL_LEAD": "the gallery leads with a label or size chart",
    "TWIN_BLANK": "a C-twin with blank fields its parent SKU holds — the twin step would have filled them",
    "NO_LISTING": "tenant has a marketplace account, product has no listing row — never enqueued",
    "LISTING_NOT_PUBLISHED": "the listing exists but is not PUBLISHED",
    "LISTING_SYNC_FAILED": "the last channel sync failed",
    "PUBLICATION_UNVERIFIED": "the product is on Shopify but the storefront publication check failed — visible to no sales channel",
    "NO_SHOPIFY_ID": "listed as PUBLISHED without a Shopify product id",
}


def _redact(dsn: str) -> str:
    try:
        head, tail = dsn.split("@", 1)
        return head.split("//", 1)[0] + "//***@" + tail
    except ValueError:
        return dsn


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _day(text: str) -> datetime:
    return datetime.combine(date.fromisoformat(text), datetime.min.time())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db", help="database URL; else DATABASE_URL")
    ap.add_argument("--from", dest="start", metavar="YYYY-MM-DD",
                    help="first day of the window (inclusive)")
    ap.add_argument("--to", dest="end", metavar="YYYY-MM-DD",
                    help="last day of the window (inclusive)")
    ap.add_argument("--product", "--products", dest="products", metavar="UUID[,UUID]",
                    help=("examine these products instead of a window, whatever stage they "
                          "are in. The approval-only checks are asked only of products that "
                          "were approved; everything else is checked regardless."))
    ap.add_argument("--tenant", help="one tenant by name (case-insensitive); default every tenant")
    ap.add_argument("--limit", type=int, help="examine only the first N products (oldest first)")
    ap.add_argument("--out", help="xlsx path. Default reports/generated/approved-<from>-<to>.xlsx")
    ap.add_argument("--judge-images", action="store_true",
                    help=("also run the image gate and the photo audit NOW for products the agent "
                          "never judged. One or two vision calls per such product (cached); still no "
                          "Shopify call and nothing written. Needs GEMINI_API_KEY."))
    ap.add_argument("--rejudge", action="store_true",
                    help=("with --product only: run the image gate and the photo audit NOW for the "
                          "named products whether or not the agent recorded a verdict, and report "
                          "the fresh verdict (the agent's stays in the Agent column). Implies "
                          "--judge-images. The same pictures under the same prompt come back from "
                          "the vision cache with today's policy applied; HERMES_VISION_CACHE=0 "
                          "forces a new model call."))
    ap.add_argument("--workers", type=int, default=1,
                    help=("parallel threads for --judge-images (default 1). Each product's judging is "
                          "10–30 s of downloads and one or two model calls; 8 threads turn a day into "
                          "an hour. Mind the model's rate limit."))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")
    product_ids = [s.strip() for s in (args.products or "").split(",") if s.strip()]
    if product_ids:
        bad = [p for p in product_ids if not _UUID_RE.match(p)]
        if bad:
            sys.exit(f"--product takes product ids (uuids), not: {', '.join(bad)}")
        start = end = None
        if (args.start or args.end) and not args.quiet:
            print("--product given: --from/--to ignored, examining the named product(s) whatever the date")
    else:
        if args.rejudge:
            sys.exit("--rejudge re-judges named products only; pass --product <uuid[,uuid]>.")
        if not (args.start and args.end):
            sys.exit("Pass --from and --to (YYYY-MM-DD), or --product <uuid[,uuid]>.")
        start, end = _day(args.start), _day(args.end) + timedelta(days=1)
        if end <= start:
            sys.exit("--to must not be before --from.")
    judge_images = args.judge_images or args.rejudge

    rows = gather(dsn, start, end, args.tenant, args.limit, quiet=args.quiet,
                  judge_images=judge_images, workers=max(1, args.workers),
                  product_ids=product_ids or None, rejudge=args.rejudge)
    if args.out:
        out = Path(args.out)
    elif product_ids:
        stem = rows[0]["sku"] if len(rows) == 1 and rows[0].get("sku") else f"{len(product_ids)}-products"
        out = product_audit.REPORT_DIR / f"product-{stem}.xlsx"
    else:
        out = product_audit.REPORT_DIR / (
            f"approved-{args.start}-{args.end}" + (f"-{args.tenant}" if args.tenant else "") + ".xlsx")
    if args.rejudge:
        method = "database, plus the image gate and the photo audit run now for every named product (--rejudge)"
    elif judge_images:
        method = "database, plus the image gate and the photo audit run now where the agent never judged (--judge-images)"
    else:
        method = METHOD_DB_ONLY
    write_workbook(rows, out, dsn=dsn, start=start, end=end, tenant=args.tenant, method=method)

    # A handful of products is read on the terminal, not in a spreadsheet.
    if product_ids and len(rows) <= 5:
        for r in rows:
            agent = r["agent"] or {}
            print(f"\n{r['sku']} · {(r['title'] or '')[:70]}")
            print(f"  {r['tenant']} · stage {r['stage_now']} · approved "
                  f"{r['approved_at'].strftime('%Y-%m-%d %H:%M') + ' by ' + str(r['approved_by']) if r['approved_at'] else 'never'}"
                  f" · agent {agent.get('status') + ' / ' + str(agent.get('outcome') or '—') if agent else 'never run'}")
            # The pictures' verdicts, whichever way they were reached: what the
            # agent recorded, or what was judged now. A clean pass says so too.
            judged = r.get("judged") or {}
            for label, key in (("gate", "gate"), ("photo audit", "photos")):
                if judged.get(key):
                    print(f"  {label:<12} judged now: {verdict_line(judged[key])}")
                elif r.get(key):
                    print(f"  {label:<12} on record:  {verdict_line(r[key])}")
                else:
                    print(f"  {label:<12} not judged")
            for i in sorted(r["issues"], key=lambda i: (RANK[i["severity"]], i["code"])):
                print(f"  {i['severity']:<7} {i['code']:<32} {i['detail'][:150]}")
            if not r["issues"]:
                print("  no issues")

    counts = Counter(i["code"] for r in rows for i in r["issues"])
    sev_of = {i["code"]: i["severity"] for r in rows for i in r["issues"]}
    print()
    print(f"{len(rows)} product(s) · {sum(1 for r in rows if r['issues'])} with issues · "
          f"{sum(1 for r in rows if r['worst'] == 'high')} high · "
          f"{sum(1 for r in rows if r['worst'] == 'medium')} medium")
    for code, n in sorted(counts.items(), key=lambda kv: (RANK[sev_of[kv[0]]], -kv[1]))[:15]:
        print(f"  {sev_of[code]:<7} {code:<32} {n}")
    print(f"  xlsx: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
