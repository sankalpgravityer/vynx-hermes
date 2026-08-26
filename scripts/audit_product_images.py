#!/usr/bin/env python
"""Report which products are missing their AI on-model renders, and which still
have a background on their garment photos.

    # PRODUCTION, every tenant. Pass the connection string with --db.
    python scripts/audit_product_images.py \
        --db "postgresql://user:pass@prod-host:5432/vnyx" --all-tenants

    # One tenant, to a named file.
    python scripts/audit_product_images.py --db "postgresql://..." \
        --tenant 6045eee9-6b87-45f2-a582-2b47ea752c39 --out prod-boas.xlsx

    # A quick look before committing to the full sweep.
    python scripts/audit_product_images.py --db "postgresql://..." \
        --all-tenants --limit 500

    The connection string can also come from DATABASE_URL. `--dsn` still works
    as an alias for `--db`.

READ-ONLY, AND SAFE TO POINT AT PRODUCTION.

There is deliberately no `--apply` flag, no UPDATE and no DDL — but "the file
contains no writes" is a promise about today's code, so the connection enforces
it too. `conn.read_only` opens the transaction READ ONLY, which means POSTGRES
rejects a write; a future edit that introduced one would fail rather than run.
A `statement_timeout` stops any query pinning a backend, and the connection
identifies itself in `pg_stat_activity` as `hermes-imagery-audit (read-only)`.

Reads are batched and the detail sheets are capped, so a catalog far larger than
the dev database produces a workbook that opens. The Summary is always computed
over every product; a cap that fires is stated on the report rather than
silently truncating.


WHY `ProductMedia` AND NOT `Product.generationStatus`

Because generationStatus lies. On tenant 6045eee9 there are 278 live products
with no AI render at all, and 265 of them are marked COMPLETE — the pipeline
finished, wrote no images, and said it was done. Nothing downstream notices,
which is why these products sit in Review looking finished. The media rows are
the only record of what actually exists, so every check here reads them.

`Product.aiGeneratedImages` is no better: it is one of the six legacy String[]
columns ProductMedia replaced, and it is empty on exactly the products whose
renders are missing AND on some that have them.


WHAT COUNTS AS A LIVE ASSET

    isCurrent = true AND deletedAt IS NULL AND mediaType = 'IMAGE'

`isCurrent` is the materialized "nothing derives from this" flag — a RAW upload
whose cut-out exists is superseded, not live, and counting it would report every
successfully-matted product as still needing work. `deletedAt` is a human having
removed the asset, which is a different fact from being superseded; both take an
asset out of the gallery.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit('psycopg is required.\n  pip install "psycopg[binary]"')

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    sys.exit("openpyxl is required for the spreadsheet.\n  pip install openpyxl")

ROOT = Path(__file__).resolve().parents[1]

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# What "complete" means
# --------------------------------------------------------------------------- #

# The five views the current pipeline produces, in the canonical gallery order
# the analyze and regenerate workers write them in.
ALL_AI_VIEWS = ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"]

# REQUIRED: the two that make a product sellable. The three-quarter views were
# added by a later version of the pipeline, so demanding them would mark ~961 of
# 2,026 products on this tenant as defective for missing something most of the
# catalog never had. They are reported as advisory instead (IMG.005).
#
# Mirrors `imagery.required_views` in config/policy.yaml — the rule engine and
# this sweep must agree, or the spreadsheet and the Verify button disagree about
# the same product.
REQUIRED_AI_VIEWS = ["AI_FRONT", "AI_BACK"]
ADVISORY_AI_VIEWS = [v for v in ALL_AI_VIEWS if v not in REQUIRED_AI_VIEWS]

# Garment photography — the only views background removal applies to. Care
# labels and size charts are excluded because a macro of a wash tag is all
# fabric: the segmenter has no foreground to find and mangles it. This is
# `needsBackgroundRemoval()` in vnyx-api's services/product-media.ts.
GARMENT_VIEWS = ["FRONT", "BACK", "OTHER"]

# Size charts filed under the wrong view.
#
# 904 of the 926 live OTHER+RAW rows on tenant 6045eee9 are size-chart images
# that were written with view='OTHER' instead of 'SIZE_CHART'. Their URL still
# gives them away — the size-guide uploader puts them under /size-charts/, while
# garment photography goes under /products/. Without this exclusion the report
# claims ~904 size charts need their background removed, which is both wrong and
# loud enough to bury the real findings.
SIZE_CHART_URL_MARKERS = ("/size-charts/", "/size-chart/", "/sizecharts/")

# Footwear never gets an on-model render: every MannequinType frames the item as
# apparel worn on the torso, so the analyze worker skips generation outright.
# Reporting these as "missing model images" would be a false positive on the
# entire shoe catalog.
#
# WORD-BOUNDARY matching, ported verbatim from isFootwearCategory in
# vnyx-api/src/helpers/formatters.ts. A naive `'boot' in text` classifies BOOTCUT
# JEANS as footwear, and substring matching is how a "High Top" sneaker came to
# look like a TOP garment.
_FOOTWEAR_WORDS = [
    "footwear", "shoe", "shoes", "sneaker", "sneakers", "trainer", "trainers",
    "boot", "boots", "sandal", "sandals", "heel", "heels", "loafer", "loafers",
    "pump", "pumps", "mule", "mules", "clog", "clogs", "espadrille",
    "espadrilles", "slipper", "slippers",
]
_FOOTWEAR_RE = re.compile(r"\b(" + "|".join(_FOOTWEAR_WORDS) + r")\b", re.IGNORECASE)

# A product whose generation has been "in flight" longer than this is stranded,
# not working. A real run takes ~205s; vnyx-api's own generation-reaper uses one
# hour before it will touch a row, and this matches it so the two never disagree
# about which products are stuck.
STALE_GENERATION_HOURS = 1

# Product ids per media query. Small enough that `= ANY($1)` keeps using the
# index on a production-sized ProductMedia, large enough that the round trips do
# not dominate.
MEDIA_BATCH = 2000

# Rows written to the detail sheets. Excel's own ceiling is 1,048,576, and a
# workbook near it is unopenable in practice; a production catalog can exceed
# both. The Summary is always computed over EVERY product — only the detail
# listings are capped, and a cap that fires is reported rather than silent.
DEFAULT_MAX_DETAIL_ROWS = 100_000


def is_footwear(*values: str | None) -> bool:
    return any(v and _FOOTWEAR_RE.search(v) for v in values)


def is_size_chart_url(url: str | None) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in SIZE_CHART_URL_MARKERS)


# --------------------------------------------------------------------------- #
# Findings
#
# Same ids and severities the `imagery` rule group will report, so a row in this
# spreadsheet and a verdict from POST /v1/imagery/verify name the same defect.
# --------------------------------------------------------------------------- #

FINDINGS: dict[str, tuple[str, str]] = {
    "IMG.001": ("high",   "No AI model images at all"),
    "IMG.002": ("medium", "Required AI view missing"),
    "IMG.003": ("high",   "No garment photo — cannot generate"),
    "IMG.004": ("low",    "Only one source photo — back view would be inferred"),
    "IMG.005": ("low",    "Advisory: 3/4 or close-up view absent"),
    "IMG.010": ("high",   "Garment image still has its background"),
    "IMG.022": ("low",    "Cut-outs filed under OTHER — relabel, do not re-mat"),
    "IMG.011": ("low",    "Generation stuck in flight"),
    "IMG.012": ("medium", "Generation marked FAILED"),
    "IMG.020": ("info",   "Model generation not applicable"),
    "IMG.021": ("low",    "AI renders present but mislabelled"),
}

SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2, "info": 3}


@dataclass
class Row:
    product_id: str
    tenant_id: str
    title: str
    master_category: str
    category: str
    sub_category: str
    review_status: str
    stage: str
    generation_status: str
    is_regenerating: bool
    updated_at: datetime | None

    ai_views_present: list[str] = field(default_factory=list)
    ai_row_count: int = 0
    garment_photos: list[tuple[str, str, str]] = field(default_factory=list)  # view, processing, url
    unmatted: list[tuple[str, str, str]] = field(default_factory=list)
    # Background-removed images filed under OTHER instead of replacing the FRONT
    # or BACK they were made from. See the note on `outstanding` below.
    orphan_cutouts: list[tuple[str, str, str]] = field(default_factory=list)

    model_gen_enabled: bool = True
    close_up_enabled: bool = True

    findings: list[str] = field(default_factory=list)
    not_generatable_reason: str = ""

    # ---------------------------------------------------------------- derived
    @property
    def ai_views_missing(self) -> list[str]:
        return [v for v in REQUIRED_AI_VIEWS if v not in self.ai_views_present]

    @property
    def advisory_missing(self) -> list[str]:
        return [v for v in ADVISORY_AI_VIEWS if v not in self.ai_views_present]

    @property
    def mislabelled(self) -> bool:
        """Five renders all filed under one view.

        404 products on tenant 6045eee9 have five AI_FRONT rows and nothing else
        — an older run wrote every render under the same view. The images exist;
        only the labels are wrong. Regenerating them would spend ~2,000 Nano
        Banana calls reproducing pictures the product already has, so this is
        reported as a labelling defect and explicitly NOT as missing imagery.
        """
        return self.ai_row_count >= len(ALL_AI_VIEWS) and len(self.ai_views_present) == 1

    @property
    def generatable(self) -> bool:
        return not self.not_generatable_reason

    @property
    def outstanding(self) -> list[tuple[str, str, str]]:
        """RAW originals that genuinely still need the segmenter.

        A RAW original is only outstanding if no mis-filed cut-out could be its
        counterpart. On BOAS the cut-outs were written with view=OTHER and no
        derivation edge, leaving 5,889 of 6,585 products with FRONT/BACK reading
        RAW while the work was already done — so taking `processing` at face
        value here would ask for twelve thousand already-matted images to be
        matted again.
        """
        return [] if len(self.orphan_cutouts) >= len(self.unmatted) else self.unmatted

    @property
    def worst_severity(self) -> str:
        if not self.findings:
            return "clean"
        return min((FINDINGS[f][0] for f in self.findings), key=lambda s: SEVERITY_RANK[s])

    @property
    def missing_model_image(self) -> bool:
        return "IMG.001" in self.findings or "IMG.002" in self.findings


def judge(row: Row) -> None:
    """Apply the rules. Order matters only for readability; findings accumulate."""

    # --- can this product be generated at all? --------------------------------
    #
    # Established FIRST, because "no model images" is only a defect when model
    # images were supposed to exist. Getting this backwards is what turned the
    # pricing validator into noise: it flagged most of a correct catalog.
    if not row.model_gen_enabled:
        row.not_generatable_reason = "tenant has model generation disabled"
    elif is_footwear(row.category, row.sub_category, row.title):
        row.not_generatable_reason = "footwear — every mannequin frames the item on a torso"
    elif not row.garment_photos:
        row.not_generatable_reason = "no garment photograph to generate from"

    if row.not_generatable_reason and not row.garment_photos:
        row.findings.append("IMG.003")
    elif row.not_generatable_reason:
        row.findings.append("IMG.020")

    # --- the AI set -----------------------------------------------------------
    if row.mislabelled:
        # Has its renders; they are just filed wrong. No missing-imagery finding.
        row.findings.append("IMG.021")
    elif row.generatable:
        if row.ai_row_count == 0:
            row.findings.append("IMG.001")
        elif row.ai_views_missing:
            row.findings.append("IMG.002")

        if row.advisory_missing and row.ai_row_count > 0:
            row.findings.append("IMG.005")

        # One usable source photo means the back render is inferred from the
        # front rather than photographed. Worth knowing before it ships.
        if len(row.garment_photos) == 1 and row.ai_views_missing:
            row.findings.append("IMG.004")

    # --- backgrounds ----------------------------------------------------------
    if row.outstanding:
        row.findings.append("IMG.010")
    elif row.unmatted and row.orphan_cutouts:
        row.findings.append("IMG.022")

    # --- pipeline state -------------------------------------------------------
    if row.generation_status == "FAILED":
        row.findings.append("IMG.012")

    stale = False
    if row.updated_at is not None:
        # Prisma maps DateTime to `timestamp without time zone`, so psycopg hands
        # these back naive. They are stored in UTC; say so rather than letting the
        # subtraction below raise on a mixed-awareness pair.
        seen = row.updated_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - seen).total_seconds() / 3600
        stale = age_h > STALE_GENERATION_HOURS
    if stale and (row.is_regenerating or row.generation_status == "GENERATING"):
        row.findings.append("IMG.011")


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

_PRODUCT_SQL = """
SELECT p.id, p."tenantId", p.title, p."masterCategory", p.category, p."subCategory",
       p."reviewStatus", p."currentStage", p."generationStatus", p."isRegenerating",
       p."updatedAt",
       COALESCE(s."isModelGenerationEnabled", true),
       COALESCE(s."isCloseUpEnabled", true)
FROM "Product" p
LEFT JOIN "ImageGenerationSettings" s ON s."tenantId" = p."tenantId"
WHERE p."isDeleted" = false
  AND p."isArchived" = false
  {tenant_clause}
ORDER BY p."createdAt" DESC
{limit_clause}
"""

# Live assets only. See the module docstring for why isCurrent and deletedAt are
# both required, and why they mean different things.
_MEDIA_SQL = """
SELECT m."productId", m.view::text, m.processing::text, m.url
FROM "ProductMedia" m
WHERE m."productId" = ANY(%s)
  AND m."isCurrent" = true
  AND m."deletedAt" IS NULL
  AND m."mediaType" = 'IMAGE'
ORDER BY m."productId", m.position
"""


def connect(dsn: str, statement_timeout_s: int):
    """A connection that CANNOT write, and cannot run away on a busy database.

    Three independent guards, because this is pointed at production:

      1. `conn.read_only` — psycopg opens the transaction READ ONLY, so an
         INSERT/UPDATE/DDL is rejected by POSTGRES, not merely absent from this
         file. A future edit that added one would fail rather than execute.
      2. `statement_timeout` — no query from this script can pin a backend
         indefinitely. An audit is never worth degrading the live service.
      3. `application_name` — whoever is looking at `pg_stat_activity` while
         this runs can see exactly what it is and that it is read-only.
    """
    conn = psycopg.connect(
        dsn,
        connect_timeout=30,
        application_name="hermes-imagery-audit (read-only)",
        options=f"-c statement_timeout={int(statement_timeout_s) * 1000}",
    )
    conn.read_only = True
    return conn


def fetch(
    dsn: str,
    tenant: str | None,
    limit: int | None,
    statement_timeout_s: int = 300,
    progress: bool = True,
) -> list[Row]:
    tenant_clause = 'AND p."tenantId" = %s' if tenant else ""
    limit_clause = f"LIMIT {int(limit)}" if limit else ""
    sql = _PRODUCT_SQL.format(tenant_clause=tenant_clause, limit_clause=limit_clause)

    rows: dict[str, Row] = {}
    with connect(dsn, statement_timeout_s) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (tenant,) if tenant else ())
            for r in cur.fetchall():
                rows[str(r[0])] = Row(
                    product_id=str(r[0]),
                    tenant_id=str(r[1]),
                    title=r[2] or "",
                    master_category=r[3] or "",
                    category=r[4] or "",
                    sub_category=r[5] or "",
                    review_status=r[6] or "",
                    stage=r[7] or "",
                    generation_status=r[8] or "",
                    is_regenerating=bool(r[9]),
                    updated_at=r[10],
                    model_gen_enabled=bool(r[11]),
                    close_up_enabled=bool(r[12]),
                )

            if not rows:
                return []

            # Media in BATCHES rather than per product — a per-product query is
            # one round trip each, for data a single indexed scan returns. The
            # batch is deliberately modest: `= ANY($1)` with a very large array
            # can tip the planner off the (productId, ...) index onto a seq scan
            # of a table that holds millions of rows in production.
            ids = list(rows)
            if progress:
                print(f"  {len(ids)} products; reading media...", flush=True)
            for chunk_start in range(0, len(ids), MEDIA_BATCH):
                chunk = ids[chunk_start:chunk_start + MEDIA_BATCH]
                if progress and chunk_start and chunk_start % (MEDIA_BATCH * 10) == 0:
                    print(f"    {chunk_start}/{len(ids)}", flush=True)
                cur.execute(_MEDIA_SQL, (chunk,))
                for pid, view, processing, url in cur.fetchall():
                    row = rows[str(pid)]
                    if view.startswith("AI_"):
                        row.ai_row_count += 1
                        if view not in row.ai_views_present:
                            row.ai_views_present.append(view)
                    elif view in GARMENT_VIEWS:
                        if is_size_chart_url(url):
                            continue  # mis-filed size chart, not garment photography
                        row.garment_photos.append((view, processing, url))
                        if processing == "RAW":
                            row.unmatted.append((view, processing, url))
                        elif view == "OTHER":
                            row.orphan_cutouts.append((view, processing, url))

    out = list(rows.values())
    for row in out:
        judge(row)
    return out


# --------------------------------------------------------------------------- #
# Writing the workbook
# --------------------------------------------------------------------------- #

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
SEV_FILL = {
    "high":   PatternFill("solid", fgColor="F8CBAD"),
    "medium": PatternFill("solid", fgColor="FFE699"),
    "low":    PatternFill("solid", fgColor="E2EFDA"),
    "info":   PatternFill("solid", fgColor="F2F2F2"),
    "clean":  PatternFill("solid", fgColor="FFFFFF"),
}


def _header(ws, headers: list[str]) -> None:
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"


def _autosize(ws, widths: dict[int, int]) -> None:
    for idx, width in widths.items():
        ws.column_dimensions[get_column_letter(idx)].width = width


def edit_url(base: str, product_id: str, tenant_id: str, review_status: str) -> str:
    """Mirror buildEditUrl in vnyx-api/src/services/review-verification.ts.

    `fromTab` uses the GET /products filter vocabulary rather than the raw enum,
    so the link lands on the tab the reviewer would have come from.
    """
    tab = {
        "PENDING": "pending",
        "ACCEPTED": "uploaded",
        "REJECTED": "rejected",
        "PENDING_PHOTOBOOTH": "photobooth",
        "PENDING_DECISON": "decision",
    }.get(review_status, "pending")
    return f"{base.rstrip('/')}/product/{product_id}/edit?tenantId={tenant_id}&fromTab={tab}"


def build_workbook(rows: list[Row], app_base: str, dsn_label: str,
                   tenant: str | None,
                   max_detail_rows: int = DEFAULT_MAX_DETAIL_ROWS) -> Workbook:
    wb = Workbook()

    # ── Summary ───────────────────────────────────────────────────────────────
    ws = wb.active
    ws.title = "Summary"
    total = len(rows)
    affected = [r for r in rows if r.findings]

    # Worked out here, not in the sheets that apply them: the Summary is written
    # FIRST and has to state the caps, so the detail tabs cannot be the ones to
    # discover them.
    truncated_findings = max(0, len(affected) - max_detail_rows)
    truncated_images = max(
        0, sum(len(r.outstanding) for r in rows) - max_detail_rows
    )
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        for f in r.findings:
            counts[f] += 1

    ws.append(["Imagery audit"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([])
    for label, value in (
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M")),
        ("Database", dsn_label),
        ("Tenant", tenant or "(all tenants)"),
        ("Live products examined", total),
        ("Products with at least one finding", len(affected)),
        ("Required AI views", ", ".join(REQUIRED_AI_VIEWS)),
        ("Advisory AI views", ", ".join(ADVISORY_AI_VIEWS)),
    ):
        ws.append([label, value])
        ws.cell(ws.max_row, 1).font = Font(bold=True)

    ws.append([])
    ws.append(["Finding", "Severity", "What it means", "Products"])
    for cell in ws[ws.max_row]:
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
    for code, (sev, desc) in sorted(
        FINDINGS.items(), key=lambda kv: (SEVERITY_RANK[kv[1][0]], kv[0])
    ):
        ws.append([code, sev, desc, counts.get(code, 0)])
        ws.cell(ws.max_row, 2).fill = SEV_FILL[sev]

    ws.append([])
    ws.append(["Headline numbers"])
    ws.cell(ws.max_row, 1).font = Font(bold=True)
    no_ai = sum(1 for r in rows if r.generatable and r.ai_row_count == 0)
    no_ai_complete = sum(
        1 for r in rows
        if r.generatable and r.ai_row_count == 0 and r.generation_status == "COMPLETE"
    )
    for label, value in (
        ("Missing every model image", no_ai),
        ("  ...of which claim generationStatus = COMPLETE", no_ai_complete),
        ("Missing a required view but has some renders",
         sum(1 for r in rows if r.generatable and r.ai_row_count and r.ai_views_missing
             and not r.mislabelled)),
        ("Renders present but mislabelled", sum(1 for r in rows if r.mislabelled)),
        ("Genuinely needs background removal",
         sum(1 for r in rows if r.outstanding)),
        ("  ...images outstanding in total", sum(len(r.outstanding) for r in rows)),
        ("Cut-outs mis-filed under OTHER (already matted)",
         sum(1 for r in rows if "IMG.022" in r.findings)),
        ("Only one source photo (back would be inferred)",
         sum(1 for r in rows if len(r.garment_photos) == 1)),
        ("No garment photo at all — needs a reshoot",
         sum(1 for r in rows if not r.garment_photos)),
        ("Not applicable (footwear / tenant setting off)",
         sum(1 for r in rows if "IMG.020" in r.findings)),
    ):
        ws.append([label, value])

    # A cap that fired is stated on the face of the report. A truncated listing
    # that looks complete is worse than no listing: it reads as "that is all of
    # them" and the rest are never chased.
    if truncated_findings or truncated_images:
        ws.append([])
        ws.append(["NOT EVERYTHING IS LISTED"])
        ws.cell(ws.max_row, 1).font = Font(bold=True, color="C00000")
        if truncated_findings:
            ws.append([f"Findings tab shows the worst {max_detail_rows:,}",
                       f"{truncated_findings:,} more not listed"])
        if truncated_images:
            ws.append([f"Un-matted tab shows the first {max_detail_rows:,}",
                       f"{truncated_images:,} more not listed"])
        ws.append(["The counts above are over EVERY product; only the listings "
                   "are capped. Raise it with --max-detail-rows, or narrow the "
                   "run with --tenant."])

    # Per-tenant split — meaningless on a single tenant, essential across a
    # production database where the totals hide which shop needs the work.
    by_tenant: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        bucket = by_tenant[r.tenant_id]
        bucket["products"] += 1
        for f in r.findings:
            bucket[f] += 1
    if len(by_tenant) > 1:
        ws.append([])
        ws.append(["Per tenant"])
        ws.cell(ws.max_row, 1).font = Font(bold=True)
        codes = ["IMG.001", "IMG.002", "IMG.003", "IMG.010", "IMG.021", "IMG.022"]
        ws.append(["Tenant", "Products", *codes])
        for cell in ws[ws.max_row]:
            cell.fill = HDR_FILL
            cell.font = HDR_FONT
        for tid, bucket in sorted(
            by_tenant.items(), key=lambda kv: -kv[1]["products"]
        ):
            ws.append([tid, bucket["products"], *(bucket[c] for c in codes)])

    _autosize(ws, {1: 48, 2: 40, 3: 46, 4: 10})

    # ── Findings ──────────────────────────────────────────────────────────────
    ws = wb.create_sheet("Findings")
    _header(ws, [
        "Product ID", "Title", "Master", "Category", "Sub-category",
        "Review status", "Generation status",
        "Missing model image?", "AI views present", "AI views missing",
        "Advisory missing", "Un-matted images", "Source photos",
        "Generatable?", "Reason if not", "Severity", "Finding ids",
        "What to do", "Edit URL",
    ])

    # Worst first, then products missing everything before products missing one
    # view, so the top of the sheet is the backfill list.
    def sort_key(r: Row) -> tuple:
        return (
            SEVERITY_RANK.get(r.worst_severity, 9),
            0 if "IMG.001" in r.findings else 1,
            -len(r.ai_views_missing),
            -len(r.unmatted),
            r.title,
        )

    for r in sorted(affected, key=sort_key)[:max_detail_rows]:
        ws.append([
            r.product_id,
            r.title,
            r.master_category,
            r.category,
            r.sub_category,
            r.review_status,
            r.generation_status,
            "YES" if r.missing_model_image else "no",
            ", ".join(r.ai_views_present) or "—",
            ", ".join(r.ai_views_missing) or "—",
            ", ".join(r.advisory_missing) or "—",
            len(r.outstanding),
            len(r.garment_photos),
            "yes" if r.generatable else "NO",
            r.not_generatable_reason or "",
            r.worst_severity,
            ", ".join(r.findings),
            action_for(r),
            edit_url(app_base, r.product_id, r.tenant_id, r.review_status),
        ])
        ws.cell(ws.max_row, 16).fill = SEV_FILL[r.worst_severity]
        if r.missing_model_image:
            ws.cell(ws.max_row, 8).font = Font(bold=True, color="C00000")
    _autosize(ws, {1: 38, 2: 42, 3: 10, 4: 20, 5: 18, 6: 16, 7: 17, 8: 19,
                   9: 30, 10: 24, 11: 26, 12: 15, 13: 13, 14: 12, 15: 40,
                   16: 10, 17: 26, 18: 44, 19: 76})

    # ── Un-matted images ──────────────────────────────────────────────────────
    #
    # Its own tab because a product can have several, and the fix is per-image:
    # POST /products/:id/remove-background takes one imageUrl at a time.
    ws = wb.create_sheet("Un-matted images")
    _header(ws, ["Product ID", "Title", "View", "Processing", "File", "URL", "Edit URL"])
    written = 0
    for r in sorted(rows, key=lambda x: x.title):
        for view, processing, url in r.outstanding:
            if written >= max_detail_rows:
                break
            written += 1
            ws.append([
                r.product_id, r.title, view, processing,
                url.rsplit("/", 1)[-1], url,
                edit_url(app_base, r.product_id, r.tenant_id, r.review_status),
            ])
    _autosize(ws, {1: 38, 2: 42, 3: 10, 4: 13, 5: 36, 6: 90, 7: 76})

    return wb


def action_for(r: Row) -> str:
    """The single next step for this product, in plain words."""
    if "IMG.003" in r.findings:
        return "Photograph the garment — nothing to generate from"
    if "IMG.020" in r.findings:
        return "Nothing — generation does not apply"
    if "IMG.021" in r.findings:
        return "Relabel the existing renders; do NOT regenerate"
    if "IMG.001" in r.findings:
        src = "front photo only" if len(r.garment_photos) == 1 else "front + back"
        return f"Generate the model set from {src}"
    if "IMG.002" in r.findings:
        return f"Generate {', '.join(r.ai_views_missing)} using the existing render as reference"
    if "IMG.010" in r.findings:
        return f"Remove the background on {len(r.outstanding)} image(s)"
    if "IMG.022" in r.findings:
        return "Relabel the OTHER cut-outs as FRONT/BACK; do NOT re-mat"
    if "IMG.005" in r.findings:
        return "Optional: fill in the 3/4 and close-up views"
    return "Review"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def redact(dsn: str) -> str:
    m = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    if not m:
        return "(dsn not shown)"
    return f"{m.group(1)}://{m.group(2)}@{m.group(3)}/{m.group(4)}"


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report products missing AI on-model renders or background removal. "
                    "READ-ONLY — issues SELECT only.",
    )
    p.add_argument("--db", "--dsn", dest="db",
                   default=os.getenv("DATABASE_URL") or None,
                   help="Postgres connection string, e.g. "
                        "postgresql://user:pass@host:5432/dbname . "
                        "Env fallback: DATABASE_URL. `--dsn` is accepted as an "
                        "alias so existing commands keep working.")
    p.add_argument("--max-detail-rows", type=int, default=DEFAULT_MAX_DETAIL_ROWS,
                   help=f"Cap on rows in the detail sheets (default "
                        f"{DEFAULT_MAX_DETAIL_ROWS:,}). Summary counts always "
                        f"cover every product; a cap that fires is reported.")
    p.add_argument("--statement-timeout", type=int, default=300, metavar="SECONDS",
                   help="Server-side cap on any single query (default 300). "
                        "Keeps an audit from pinning a production backend.")
    p.add_argument("--quiet", action="store_true", help="No progress output.")
    p.add_argument("--tenant", default=None, help="Tenant uuid to audit.")
    p.add_argument("--all-tenants", action="store_true",
                   help="Audit every tenant. Ignores --tenant.")
    p.add_argument("--limit", type=int, default=None, help="Cap the number of products.")
    p.add_argument("--out", default=None, help="Output .xlsx path.")
    p.add_argument("--app-base-url",
                   default=os.getenv("PUBLIC_APP_BASE_URL", "https://dev.vnyx.ai"),
                   help="Frontend origin the Edit URLs point at.")
    args = p.parse_args()

    if not args.db:
        return int(bool(sys.stderr.write(
            "No database connection string. Pass --db, or set DATABASE_URL.\n"
        ))) or 2
    if not args.tenant and not args.all_tenants:
        return int(bool(sys.stderr.write(
            "Pass --tenant <uuid>, or --all-tenants to sweep everything.\n"
        ))) or 2

    tenant = None if args.all_tenants else args.tenant

    print(f"Reading {redact(args.db)} — READ ONLY, no writes are possible.")
    rows = fetch(
        args.db, tenant, args.limit,
        statement_timeout_s=args.statement_timeout,
        progress=not args.quiet,
    )
    if not rows:
        print("No live products matched.")
        return 0

    affected = [r for r in rows if r.findings]
    print(f"  {len(rows)} live products, {len(affected)} with findings.")

    out = Path(args.out) if args.out else (
        ROOT / "reports" /
        f"imagery-audit-{(tenant or 'all')[:8]}-{datetime.now():%Y-%m-%d}.xlsx"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    build_workbook(
        rows, args.app_base_url, redact(args.db), tenant,
        max_detail_rows=args.max_detail_rows,
    ).save(out)
    print(f"  wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
