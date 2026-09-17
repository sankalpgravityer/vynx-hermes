#!/usr/bin/env python
"""Every product in Review, one row each, saying exactly what it is missing.

    # LOOK FIRST. Read-only, and the connection enforces it.
    python scripts/audit_review_products.py --db "postgresql://user:pass@host:5432/vnyx"

    # A slice, while you are still deciding.
    python scripts/audit_review_products.py --db "..." --limit 50

    # FIX what can be fixed.
    python scripts/audit_review_products.py --db "..." --apply

    # One tenant, to a named file.
    python scripts/audit_review_products.py --db "..." \
        --tenant 514d1b2c-ccb4-4c90-9ac1-b75e7dbb1e95 --out boas-review.xlsx

The connection string can also come from DATABASE_URL.


THE COLUMNS

One per question a reviewer actually asks, each answered from the tenant's own
configuration rather than a constant in this file:

    AI model images     how many of the five on-model renders exist, and which
                        are absent. Footwear is exempt — every MannequinType
                        frames the item on a torso, so the analyze worker skips
                        generation and reporting it would flag the whole shoe
                        catalog.
    Background removed  garment photographs still carrying their background.
                        Care labels and size charts are excluded: a macro of a
                        wash tag is all fabric, the segmenter has no foreground
                        to find, and it mangles them.
    Price               against the window THIS tenant's grade factor implies,
                        not a hardcoded band.
    Brand               present, and selectable in the tenant's own brand list.
    Size                present, and the column agreeing with the `properties`
                        copy — the two disagree more often than either is empty.
    Sizing guide        whether the chart matches the master category, and when
                        it does not, which chart does.

Every verdict comes from the same rule engine `/v1/product-audit` uses, so this
sheet and that endpoint can never disagree about the same product.


WHAT --apply WRITES

The repairs the rule engine can compute AND this process can safely execute:
sizing guide, sub-category, and the column/properties drift pairs. Each lands
through app.product_audit.apply_plan, in one transaction per product, guarded on
`updatedAt`.

It does NOT write price, AI renders or background removal. Those need
updateProduct's variant/Inventory/Price mirroring, an image model, and R2
respectively — all of which live in vnyx-api. They are reported with the
computed value so a human or the proper endpoint can act on them.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    sys.exit("openpyxl is required:  pip install openpyxl")

from app import approval, product_audit  # noqa: E402
from app.config import policy, settings  # noqa: E402
from app.rules import imagery as imagery_rules  # noqa: E402
from app.vnyx_client import to_snapshot  # noqa: E402

HDR = PatternFill("solid", fgColor="1F3864")
BAD = PatternFill("solid", fgColor="FDECEA")
WARN = PatternFill("solid", fgColor="FFF8E1")
OK = PatternFill("solid", fgColor="E8F5E9")
SECTION = PatternFill("solid", fgColor="D9E2F3")

# Rows written to the detail sheet. Excel's ceiling is 1,048,576 and a workbook
# near it is unopenable in practice. The Summary is always computed over EVERY
# product; only the listing is capped, and a cap that fires is stated on the
# sheet rather than silently truncating.
MAX_DETAIL_ROWS = 100_000

# Ids per batch. The media query uses `= ANY($1)`, which keeps its index at this
# size on a production-sized ProductMedia.
BATCH = 500


# --------------------------------------------------------------------------- #
# Selecting the review section
# --------------------------------------------------------------------------- #

# `reviewStatus`, NOT `currentStage`.
#
# THE TWO ARE DIFFERENT AXES AND THEY DISAGREE BY HUNDREDS OF PRODUCTS. On
# production: currentStage='REVIEW' is 1,271 and reviewStatus='PENDING' is 1,789,
# because 552 products sit in the LABEL stage while still awaiting review. The
# first version of this script defaulted to the stage and reported 1,271 against
# a UI showing 1,787 — the count did not match and there was no way to see why.
#
# The Review tab is reviewStatus='PENDING'. That is not an inference:
# vnyx-api's services/products.ts:1472 is `if (status === 'pending') {
# where.reviewStatus = 'PENDING' }`, and the tab links to ?tab=pending.
#
# Both axes are exposed. --status is the tab (and the default); --stage filters
# the pipeline stage and can be combined with it.
REVIEW_SQL = """
SELECT p.id::text
  FROM "Product" p
 WHERE p."isDeleted" = false
   AND p."isArchived" = false
   {status}
   {stage}
   {tenant}
 ORDER BY p."createdAt" DESC
 {limit}
"""

# Tab name -> reviewStatus, copied from products.ts:1472. `PENDING_DECISON` is
# misspelled in the enum itself; matching the typo is required, not a mistake.
TAB_STATUS = {
    "pending": "PENDING",
    "uploaded": "ACCEPTED",
    "rejected": "REJECTED",
    "photobooth": "PENDING_PHOTOBOOTH",
    "decision": "PENDING_DECISON",
    "imported": "IMPORTED",
}


EXPLICIT_SQL = """
SELECT p.id::text
  FROM "Product" p
 WHERE p.id = ANY(%s::uuid[])
   AND p."isDeleted" = false
   AND p."isArchived" = false
"""


def select_explicit(cur, ids: list[str], limit: int | None) -> list[str]:
    """A named list of products, in the order the CALLER gave them.

    Order is preserved deliberately. The sheet is sorted worst-first, so "the top
    5 rows" means the five products with the most gaps — and re-sorting them by
    `createdAt` the way the queue query does would silently apply to five
    different products than the ones that were read.

    Deleted and archived rows are still dropped: a targeted list says which
    products to look at, not that retired stock should be repaired.
    """
    cur.execute(EXPLICIT_SQL, (ids,))
    live = {r[0] for r in cur.fetchall()}
    ordered = [i for i in dict.fromkeys(ids) if i in live]
    return ordered[:limit] if limit else ordered


_ID_HEADERS = {"product id", "productid", "product_id", "product uuid",
               "product", "id", "uuid"}
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def read_sheet_ids(path: Path) -> list[str]:
    """Product ids from any sheet that carries a column of them, in sheet order.

    Sheet order IS gap order — an audit's Products sheet is written worst-first
    — so `--from-sheet x.xlsx --limit 5` means the five worst, which is what
    someone looking at the top of the sheet is asking for.

    THE HEADER IS MATCHED LOOSELY, and every value is checked against the uuid
    shape. This used to demand the exact string "Product ID"; a sheet somebody
    exported with "Product id" (the approved-window workbook writes exactly
    that) matched nothing and the run died with "Pass --product or
    --from-sheet" while pointing at a perfectly good file. The uuid test is
    what makes the loose match safe: a column that is not ids yields nothing
    rather than nonsense. With no recognisable header at all, the column
    holding the most uuids wins — which is what makes a hand-made list work.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ids: list[str] = []
    try:
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            if not rows:
                continue
            col: int | None = None
            start = 0
            # A header sits at the top; an audit workbook puts a title and a
            # few summary lines above it, never twenty-five.
            for i, row in enumerate(rows[:25]):
                for j, cell in enumerate(row or ()):
                    if (cell is not None
                            and str(cell).strip().lower().replace("-", "_") in _ID_HEADERS):
                        col, start = j, i + 1
                        break
                if col is not None:
                    break
            if col is None:
                counts: dict[int, int] = {}
                for row in rows:
                    for j, cell in enumerate(row or ()):
                        if cell and _UUID.match(str(cell).strip()):
                            counts[j] = counts.get(j, 0) + 1
                if not counts:
                    continue
                col = max(counts, key=lambda k: counts[k])
            for row in rows[start:]:
                if row and len(row) > col and row[col]:
                    value = str(row[col]).strip()
                    if _UUID.match(value):
                        ids.append(value)
    finally:
        wb.close()
    return list(dict.fromkeys(ids))


def select_products(cur, *, statuses: list[str] | None,
                    stages: list[str] | None, tenant: str | None,
                    limit: int | None) -> list[str]:
    sql = REVIEW_SQL.format(
        status='AND p."reviewStatus"::text = ANY(%(statuses)s)' if statuses else "",
        stage='AND p."currentStage"::text = ANY(%(stages)s)' if stages else "",
        tenant='AND p."tenantId" = %(tenant)s::uuid' if tenant else "",
        limit="LIMIT %(limit)s" if limit else "",
    )
    params: dict[str, Any] = {}
    if statuses:
        params["statuses"] = statuses
    if stages:
        params["stages"] = stages
    if tenant:
        params["tenant"] = tenant
    if limit:
        params["limit"] = limit
    cur.execute(sql, params)
    return [r[0] for r in cur.fetchall()]


def count_axes(cur, tenant: str | None) -> dict[str, int]:
    """Both counts, always printed.

    So "the script says 1,271 and the screen says 1,787" is answered on the spot
    rather than by someone reading the SQL.
    """
    where = 'p."isDeleted"=false AND p."isArchived"=false'
    params: dict[str, Any] = {}
    if tenant:
        where += ' AND p."tenantId" = %(tenant)s::uuid'
        params["tenant"] = tenant
    cur.execute(
        f'''SELECT
              count(*) FILTER (WHERE p."reviewStatus"::text = 'PENDING'),
              count(*) FILTER (WHERE p."currentStage"::text = 'REVIEW')
            FROM "Product" p WHERE {where}''', params)
    pending, review = cur.fetchone()
    return {"reviewStatus=PENDING": pending, "currentStage=REVIEW": review}


TENANTS_SQL = """
SELECT DISTINCT p."tenantId"::text
  FROM "Product" p
 WHERE p.id = ANY(%s::uuid[])
"""


# --------------------------------------------------------------------------- #
# The per-product columns
# --------------------------------------------------------------------------- #

def _rule_ids(verdict: dict[str, Any]) -> set[str]:
    return set(verdict.get("findings") or [])


def _finding(verdict: dict[str, Any], rule_id: str) -> dict[str, Any] | None:
    for key in ("blocking", "advisory"):
        for f in verdict.get(key) or []:
            if f["rule_id"] == rule_id:
                return f
    return None


def ai_images_column(p, pol) -> tuple[str, bool]:
    """"3 of 5 — missing AI_BACK, AI_CLOSEUP", and whether it is a gap."""
    reason = imagery_rules.not_generatable_reason(p, pol)
    if reason:
        # Footwear, or the tenant switched model generation off. Not a defect.
        return f"n/a — {reason}", False

    report = imagery_rules.view_report(p, pol)
    all_views = (pol.get("imagery") or {}).get("all_views") or []
    have = [v for v in all_views if v in report.present]

    if imagery_rules.is_mislabelled(report, pol):
        return (f"{report.row_count} renders, all filed as "
                f"{report.present[0]} — relabel, do not regenerate"), True

    absent = [v for v in all_views if v not in report.present]
    if not absent:
        return f"{len(have)} of {len(all_views)}", False
    return (f"{len(have)} of {len(all_views)} — missing "
            f"{', '.join(absent)}"), True


def background_column(p, pol) -> tuple[str, bool]:
    """Garment photographs still carrying a background."""
    garments = imagery_rules.garment_photos(p, pol)
    if not garments:
        return "no garment photo", True
    still_raw = imagery_rules.unmatted(p, pol)
    if not still_raw:
        return f"{len(garments)} of {len(garments)} done", False
    done = len(garments) - len(still_raw)
    views = ", ".join(sorted({m.view for m in still_raw}))
    return f"{done} of {len(garments)} done — {views} still RAW", True


def price_column(verdict: dict[str, Any]) -> tuple[str, bool]:
    """The price verdict, and whether it counts as a gap.

    `round_required` is NOT a gap. models.py is explicit that it is "inside the
    window, wrong cents" and that "a rounding is not a data defect, so it stays
    out of the findings list and leaves `correct` true". Counting it flagged 16
    of the first 25 products whose price is perfectly fine, which buries the ones
    that are genuinely wrong.

    `no_anchor` and `no_factor` ARE gaps, but neither is about the price being
    wrong — one has no retail price to judge against, the other has no configured
    factor. Printing them through the window template gave "outside None-None",
    which reads as a bug in the audit.
    """
    price = verdict.get("price") or {}
    v = price.get("verdict")
    actual = price.get("actual_price")

    if v in (None, "ok"):
        return f"ok — {actual}", False
    if v == "round_required":
        return f"ok — {actual}, would round to {price.get('corrected_price')}", False
    if v == "no_price":
        return "MISSING — no price set", True
    if v == "no_anchor":
        return (f"cannot judge — {actual} with no retail price to measure "
                f"against"), True
    if v == "no_factor":
        return (f"cannot judge — grade {price.get('grade')} has no priceFactor "
                f"configured on this tenant"), True

    lo, hi = price.get("min_allowed"), price.get("max_allowed")
    window = f"{lo}-{hi}" if lo is not None and hi is not None else "the window"
    return (f"{v} — {actual} outside {window} "
            f"(grade {price.get('grade')} x{price.get('price_factor')}), "
            f"should be {price.get('corrected_price')}"), True


def brand_column(record: dict[str, Any], rules: set[str]) -> tuple[str, bool]:
    brand = record.get("brand")
    if not brand:
        return "MISSING", True
    if "ATTR.001" in rules:
        return f"'{brand}' not in the tenant's brand list", True
    # The relation and the properties copy are two different stores.
    relation = record.get("brandRelation")
    if relation and str(relation).strip().lower() != str(brand).strip().lower():
        return f"'{brand}' but the Brand relation says '{relation}'", True
    return str(brand), False


def size_column(record: dict[str, Any], verdict: dict[str, Any],
                rules: set[str]) -> tuple[str, bool]:
    # `record["size"]` came through the alias chain, which already treats a
    # placeholder as absent. `internationalSize` is the raw column and does not —
    # it holds the literal "Unknown" on a large slice of this catalog, and using
    # it as a fallback printed "Unknown / EU ?" as though it were a size.
    size = record.get("size")
    if not size and not product_audit._blank(record.get("internationalSize")):
        size = record.get("internationalSize")
    drift = _finding(verdict, "DRIFT.001")
    if drift and "international_size" in (drift.get("fields") or []):
        d = drift["detail"]
        return (f"column {d['column_value']!r} vs properties "
                f"{d['property_value']!r}"), True
    if not size:
        return "MISSING", True
    if "SIZE.002" in rules:
        f = _finding(verdict, "SIZE.002") or {}
        return f"{size} — EU size disagrees with the chart ({f.get('message','')[:60]})", True
    if "SIZE.001" in rules:
        return f"{size} — size fields disagree with each other", True
    return f"{size} / EU {record.get('euSize') or '?'}", False


def guide_column(record: dict[str, Any], verdict: dict[str, Any],
                 rules: set[str]) -> tuple[str, bool, str | None]:
    """Does the chart match the master category, and which one should it be.

    Returns (text, is_gap, suggested_guide).
    """
    current = record.get("sizingGuide")
    for rule_id in ("SIZE.014", "SIZE.013", "SIZE.011", "SIZE.012", "SIZE.010"):
        if rule_id not in rules:
            continue
        f = _finding(verdict, rule_id) or {}
        suggested = (f.get("detail") or {}).get("suggested")
        if isinstance(suggested, list):
            suggested = suggested[0] if len(suggested) == 1 else None
        return (f.get("message", rule_id), True,
                suggested if isinstance(suggested, str) else None)
    if not current:
        return "MISSING", True, None
    return f"{current} — matches {record.get('masterCategory')}", False, None


def chart_column(chart: dict[str, Any]) -> tuple[str, bool]:
    state = chart.get("state") or "unknown"
    return state, state != "ok"


def care_label_column(record: dict[str, Any]) -> tuple[str, bool]:
    """Yes / No, from the MEDIA ROWS.

    Upstream of brand, size and composition rather than beside them: the care
    label is the photograph those are read off, so a product without one is
    missing the EVIDENCE, not merely the answer. audit_product_data.py measured
    the cost on production — 23.8% of products with no care label have no brand,
    against 2.8% of those with one.

    Counted from `ProductMedia view='LABEL' AND isCurrent AND deletedAt IS NULL`,
    not from the legacy `Product.careLabelImages` array. Where the two disagree
    the count says so, because IMG.030 blocks approval on this number and a
    silent disagreement between two stores of the same fact is what this whole
    audit exists to surface.
    """
    live = record.get("careLabelCount") or 0
    legacy = record.get("legacyCareLabelCount") or 0
    if live and live != legacy:
        return f"Yes ({live}) — legacy column says {legacy}", False
    if live:
        return f"Yes ({live})" if live > 1 else "Yes", False
    if legacy:
        return f"No — but the legacy column claims {legacy}", True
    return "No", True


# --------------------------------------------------------------------------- #
# The sweep
# --------------------------------------------------------------------------- #

COLUMNS = [
    ("Missing", 46),
    ("Gaps", 6),
    ("AI model images", 40),
    ("Background removed", 34),
    ("Price", 52),
    ("Brand", 30),
    ("Size", 40),
    ("Sizing guide", 56),
    ("Size chart", 34),
    ("Care label", 34),
    ("Fixed", 34),
    ("Master category", 15),
    ("Category", 20),
    ("Sub-category", 20),
    ("Product ID", 38),
    ("Product code", 14),
    ("SKU", 14),
    ("Title", 40),
    ("Stage", 11),
    ("Review status", 14),
    ("Tenant", 18),
    ("Created", 12),
    ("Edit URL", 70),
]


def sweep(dsn: str, *, statuses: list[str] | None, stages: list[str] | None,
          tenant: str | None, limit: int | None, apply: bool,
          progress: bool, explicit: list[str] | None = None) -> dict[str, Any]:
    pol = policy()
    started = time.perf_counter()

    with product_audit.connect(dsn, read_only=True, statement_timeout_s=300) as conn, \
            conn.cursor() as cur:
        axes = count_axes(cur, tenant)
        if explicit:
            # A named list is a targeted run and is NOT narrowed by the tab or
            # stage filters — the caller already said which products. Same rule
            # vnyx-api's buildReviewFeed follows for `productIds`.
            ids = select_explicit(cur, explicit, limit)
        else:
            ids = select_products(cur, statuses=statuses, stages=stages,
                                  tenant=tenant, limit=limit)
        if progress:
            selection = (
                f"{len(explicit)} named product(s)" if explicit
                else " and ".join(filter(None, [
                    f"reviewStatus in {statuses}" if statuses else "",
                    f"currentStage in {stages}" if stages else "",
                ])) or "everything live"
            )
            print(f"Selecting {selection}", flush=True)
            # Both axes, always. The Review TAB counts reviewStatus; the pipeline
            # stage is a different number and the two are hundreds apart.
            for name, count in axes.items():
                marker = "  <- the Review tab" if name == "reviewStatus=PENDING" else ""
                print(f"  {name:24} {count:>7,}{marker}", flush=True)
            print(f"  selected                 {len(ids):>7,}"
                  + (f"  (--limit {limit})" if limit else ""), flush=True)
        if not ids:
            return {"rows": [], "total": 0, "duration_s": 0.0, "axes": axes}

        cur.execute(TENANTS_SQL, (ids,))
        tenant_ids = [r[0] for r in cur.fetchall()]
        if progress:
            print(f"Tenants: {len(tenant_ids)}  (loading catalogs)", flush=True)
        contexts = {t: product_audit.load_tenant_context(cur, t) for t in tenant_ids}

        loaded: list[dict[str, Any]] = []
        for start in range(0, len(ids), BATCH):
            chunk = ids[start:start + BATCH]
            loaded.extend(product_audit.load_batch(cur, chunk, contexts))
            if progress:
                print(f"  read {min(start + BATCH, len(ids)):,}/{len(ids):,}",
                      end="\r", flush=True)
    if progress:
        print(f"  read {len(loaded):,} product(s)          ", flush=True)

    # ONE writable connection for the whole run, opened only when repairing.
    # Each product still commits separately — see apply_plan.
    write_conn = product_audit.connect(
        dsn, read_only=False, statement_timeout_s=60) if apply else None

    try:
        rows = _judge(loaded, pol, dsn, write_conn, apply, progress)
    finally:
        if write_conn is not None:
            write_conn.close()

    return {
        "rows": rows,
        "total": len(loaded),
        "axes": axes,
        "duration_s": round(time.perf_counter() - started, 1),
    }


def _judge(loaded: list[dict[str, Any]], pol: dict[str, Any], dsn: str,
           write_conn, apply: bool, progress: bool) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for n, item in enumerate(loaded, start=1):
        record, media = item["record"], item["media"]
        verdict = approval.run_gate(
            {**record, "media": media},
            catalog=item["catalog"],
            imagery_settings=item["imagery_settings"],
        )
        p = to_snapshot({**record, "media": media},
                        catalog=item["catalog"],
                        imagery_settings=item["imagery_settings"])
        rules = _rule_ids(verdict)

        ai_text, ai_gap = ai_images_column(p, pol)
        bg_text, bg_gap = background_column(p, pol)
        price_text, price_gap = price_column(verdict)
        brand_text, brand_gap = brand_column(record, rules)
        size_text, size_gap = size_column(record, verdict, rules)
        guide_text, guide_gap, suggested = guide_column(record, verdict, rules)
        chart_text, chart_gap = chart_column(item["chart"])
        label_text, label_gap = care_label_column(record)

        missing = [
            label for label, gap in (
                ("AI images", ai_gap), ("background", bg_gap),
                ("price", price_gap), ("brand", brand_gap), ("size", size_gap),
                ("sizing guide", guide_gap), ("size chart", chart_gap),
                ("care label", label_gap),
            ) if gap
        ]

        fixed = ""
        if apply and verdict["repair_plan"]:
            result = product_audit.apply_plan(
                dsn, record["id"], verdict["repair_plan"],
                record.get("updatedAt"), conn=write_conn,
            )
            if result["conflict"]:
                fixed = "skipped — changed mid-sweep"
            elif result["wrote"]:
                fixed = "; ".join(
                    f'{a.get("field")}={a.get("value")}' for a in result["applied"]
                )
        elif verdict["repair_plan"]:
            fixable = [
                a for a in verdict["repair_plan"]
                if (a["kind"] == "set_column"
                    and a.get("field") in product_audit._WRITABLE_COLUMNS)
                or (a["kind"] == "set_property"
                    and a.get("field") in product_audit._PROPERTY_KEYS)
            ]
            if fixable:
                fixed = "would fix: " + "; ".join(
                    f'{a.get("field")}={a.get("value")}' for a in fixable
                )

        rows.append({
            "cells": [
                ", ".join(missing) or "—",
                len(missing),
                ai_text, bg_text, price_text, brand_text, size_text,
                guide_text, chart_text, label_text, fixed,
                record.get("masterCategory"), record.get("category"),
                record.get("subCategory"),
                record["id"], record.get("productCode"), record.get("sku"),
                record.get("title"), record.get("currentStage"),
                record.get("reviewStatus"), record.get("tenantName"),
                (record.get("createdAt") or "")[:10],
                record.get("editUrl"),
            ],
            "gaps": {
                "AI model images": ai_gap, "Background removed": bg_gap,
                "Price": price_gap, "Brand": brand_gap, "Size": size_gap,
                "Sizing guide": guide_gap, "Size chart": chart_gap,
                "Care label": label_gap,
            },
            "suggested_guide": suggested,
            "n_missing": len(missing),
        })

        if progress and n % 25 == 0:
            print(f"  judged {n:,}/{len(loaded):,}", end="\r", flush=True)

    if progress:
        print(f"  judged {len(rows):,} product(s)          ", flush=True)

    return rows


# --------------------------------------------------------------------------- #
# The sheet
# --------------------------------------------------------------------------- #

def index_of(name: str) -> int:
    """Zero-based position of a column in a row's `cells`, by NAME.

    Hardcoding the number is a bug waiting for the next column to be inserted —
    the same reason audit_product_data.py keys its widths by name.
    """
    return [n for n, _ in COLUMNS].index(name)


def redact(dsn: str) -> str:
    m = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    return f"{m.group(1)}://{m.group(2)}@{m.group(3)}/{m.group(4)}" if m else "(hidden)"


def write_workbook(result: dict[str, Any], out: Path, *, dsn: str,
                   selection: str, tenant: str | None, applied: bool) -> Path:
    rows = result["rows"]
    wb = Workbook()

    # ---- Summary ----------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Review queue — what each product is missing"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    for label, value in (
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Database", redact(dsn)),
        ("Selection", selection),
        ("Tenant", tenant or "every tenant"),
        ("Products", result["total"]),
        ("With at least one gap", sum(1 for r in rows if r["n_missing"])),
        ("Clean", sum(1 for r in rows if not r["n_missing"])),
        ("Mode", "APPLIED — repairs written" if applied else "read-only (pass --apply to fix)"),
        ("Took", f'{result["duration_s"]}s'),
    ):
        ws.append([label, value])

    # Both axes, stated on the sheet.
    #
    # The Review TAB is reviewStatus='PENDING' (products.ts:1472). `currentStage`
    # is the pipeline position and is a different, smaller number — 1,271 against
    # 1,789 on production, because 552 products sit in LABEL while still awaiting
    # review. Printing only one of them is how a sheet comes to disagree with the
    # screen for no visible reason.
    ws.append([])
    ws.append(["Axis", "Products", "What it means"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = SECTION
    for name, count in (result.get("axes") or {}).items():
        ws.append([name, count,
                   "what the Review tab counts"
                   if name == "reviewStatus=PENDING"
                   else "position in the pipeline — a different question"])

    ws.append([])
    ws.append(["Gap", "Products", "Fixable from here"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = SECTION
    fixable_by_column = {
        "AI model images": "no — needs the image model (POST /v1/imagery/generate)",
        "Background removed": "no — needs R2 (backfill-bg-removal.ts)",
        "Price": "no — needs updateProduct's variant/Price mirroring",
        "Brand": "no — add the option, or a human picks",
        "Size": "yes, when one copy is a placeholder",
        "Sizing guide": "yes, when exactly one chart fits",
        "Size chart": "no — a tenant admin uploads chart images",
        "Care label": "no — the garment must be re-photographed",
    }
    for name, note in fixable_by_column.items():
        count = sum(1 for r in rows if r["gaps"].get(name))
        ws.append([name, count, note])
        if count:
            ws.cell(row=ws.max_row, column=2).fill = BAD if "no —" in note else WARN
        else:
            ws.cell(row=ws.max_row, column=2).fill = OK

    ws.append([])
    ws.append(["Products by number of gaps"])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    tally: dict[int, int] = {}
    for r in rows:
        tally[r["n_missing"]] = tally.get(r["n_missing"], 0) + 1
    for n in sorted(tally):
        ws.append([f"{n} gap(s)" if n else "clean", tally[n]])

    # ---- what the repair actually changed ----------------------------------
    #
    # Its own section because the gap counts above will NOT move for most of it,
    # and a reader who sees "repaired 11" beside seven unchanged numbers
    # reasonably concludes nothing happened. The repairable fields are largely
    # gender, mannequin and sub-category, and none of those is one of the seven
    # columns — those seven are what was ASKED for, not what happens to be
    # fixable.
    #
    # The gap columns also describe the state BEFORE the repair: each product is
    # judged and then repaired in the same pass, so the row records what was
    # wrong. Re-run to see the new state.
    fields: dict[str, int] = {}
    for r in rows:
        text = str(r["cells"][index_of("Fixed")] or "")
        if not text or text.startswith("skipped"):
            continue
        for part in text.replace("would fix: ", "").split("; "):
            name = part.split("=")[0].strip()
            if name:
                fields[name] = fields.get(name, 0) + 1
    if fields:
        ws.append([])
        ws.append([
            "Repairs written" if applied else "Repairs available (--apply writes them)",
            "Products",
        ])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
            cell.fill = SECTION
        for name, count in sorted(fields.items(), key=lambda kv: -kv[1]):
            ws.append([name, count])
            ws.cell(row=ws.max_row, column=2).fill = OK if applied else WARN
        ws.append([
            "Note",
            "Most repairable fields are not among the seven columns above, so "
            "those counts will not drop by the same number. The columns also "
            "record the state BEFORE the repair — re-run to see the result.",
        ])

    for col, width in ((1, 26), (2, 12), (3, 62)):
        ws.column_dimensions[get_column_letter(col)].width = width
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # ---- Products ---------------------------------------------------------
    ws = wb.create_sheet("Products")
    ws.append([name for name, _ in COLUMNS])
    for cell in ws[1]:
        cell.fill = HDR
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "C2"

    index = {name: i for i, (name, _) in enumerate(COLUMNS, start=1)}
    # Worst first — the sheet's job is to put the products needing most work at
    # the top, not to preserve the database's ordering.
    for row in sorted(rows, key=lambda r: -r["n_missing"])[:MAX_DETAIL_ROWS]:
        ws.append(row["cells"])
        at = ws.max_row
        for name, is_gap in row["gaps"].items():
            ws.cell(row=at, column=index[name]).fill = BAD if is_gap else OK
        ws.cell(row=at, column=index["Missing"]).fill = (
            BAD if row["n_missing"] else OK
        )
        if row["cells"][index["Fixed"] - 1]:
            ws.cell(row=at, column=index["Fixed"]).fill = (
                OK if applied else WARN
            )
        for col in range(1, len(COLUMNS) + 1):
            ws.cell(row=at, column=col).alignment = Alignment(
                vertical="top", wrap_text=False)
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{max(ws.max_row, 2)}"

    if len(rows) > MAX_DETAIL_ROWS:
        ws.append([f"… {len(rows) - MAX_DETAIL_ROWS:,} more rows not listed; "
                   f"the Summary counts all of them."])

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)
    return out


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--db", "--dsn", dest="db",
                    help="connection string. Defaults to DATABASE_URL.")
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs that can be written from here.")
    ap.add_argument(
        "--limit", type=int,
        help=("stop after this many. With --from-sheet or --products it takes "
              "the first N IN THAT ORDER (the sheet is worst-first); otherwise "
              "the N most recently created."))
    ap.add_argument(
        "--from-sheet", dest="from_sheet",
        help=("read Product IDs from a sheet this script generated and audit "
              "only those, in sheet order. Combine with --limit 5 to take the "
              "five worst rows off the top."))
    ap.add_argument(
        "--products", help="comma-separated product ids, in the order given.")
    ap.add_argument("--tenant", help="one tenant id.")
    ap.add_argument(
        "--status", default="pending",
        help=("which TAB, by reviewStatus. Default 'pending' — the Review tab. "
              f"One of: {', '.join(TAB_STATUS)}, or 'any'."))
    ap.add_argument(
        "--stage", default=None,
        help=("additionally filter by pipeline stage (REVIEW, LABEL, APPROVED…). "
              "A DIFFERENT axis from --status; see the note in this file."))
    ap.add_argument("--out", help="sheet path.")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")

    statuses: list[str] | None = None
    if args.status and args.status.lower() != "any":
        statuses = []
        for token in args.status.split(","):
            token = token.strip()
            if not token:
                continue
            # Accept the tab name ('pending') or the raw enum ('PENDING'), so a
            # reader of the database and a reader of the UI both get what they
            # meant.
            resolved = TAB_STATUS.get(token.lower(), token.upper())
            statuses.append(resolved)

    stages = ([s.strip().upper() for s in args.stage.split(",") if s.strip()]
              if args.stage else None)

    explicit: list[str] | None = None
    if args.from_sheet and args.products:
        sys.exit("Pass --from-sheet or --products, not both.")
    if args.from_sheet:
        sheet = Path(args.from_sheet).expanduser()
        if not sheet.exists():
            sys.exit(f"No such sheet: {sheet}")
        explicit = read_sheet_ids(sheet)
        if not explicit:
            sys.exit(f"No 'Product ID' column found in {sheet}")
    elif args.products:
        explicit = [s.strip() for s in args.products.split(",") if s.strip()]

    progress = not args.quiet
    selection = (
        f"{len(explicit)} named product(s)"
        + (f" (first {args.limit} of them)" if args.limit else "")
        if explicit else
        " and ".join(filter(None, [
            f"reviewStatus in {statuses}" if statuses else "",
            f"currentStage in {stages}" if stages else "",
        ])) or "everything live"
    )

    try:
        result = sweep(dsn, statuses=statuses, stages=stages,
                       tenant=args.tenant, limit=args.limit,
                       apply=args.apply, progress=progress, explicit=explicit)
    except psycopg.OperationalError as exc:
        # A wrong host, a wrong password or a firewall are the three most likely
        # things to go wrong when someone pastes a production URL, and psycopg's
        # multi-line "tried ::1, tried 127.0.0.1" dump buries which one it was.
        sys.exit(f"Could not connect to {redact(dsn)}\n  "
                 + str(exc).strip().splitlines()[-1])
    except KeyboardInterrupt:
        # Interrupting a --apply run is safe: each product commits on its own, so
        # the ones already written stay written. Say so rather than leaving the
        # operator wondering whether the database is half-repaired.
        sys.exit("\nInterrupted. Products already repaired are committed; "
                 "re-run to continue.")
    if not result["rows"]:
        print("Nothing in that stage.")
        return 0

    out = Path(args.out) if args.out else (
        product_audit.REPORT_DIR
        / f'review-audit-{datetime.now():%Y-%m-%d-%H%M%S}.xlsx'
    )
    write_workbook(result, out, dsn=dsn, selection=selection,
                   tenant=args.tenant, applied=args.apply)

    rows = result["rows"]
    print()
    print(f'  products      {result["total"]:,}')
    print(f'  with a gap    {sum(1 for r in rows if r["n_missing"]):,}')
    for name in ("AI model images", "Background removed", "Price", "Brand",
                 "Size", "Sizing guide", "Size chart", "Care label"):
        print(f'    {name:20} {sum(1 for r in rows if r["gaps"].get(name)):,}')
    if args.apply:
        fixed_at = index_of("Fixed")
        texts = [str(r["cells"][fixed_at] or "") for r in rows]
        repaired = sum(1 for t in texts if t and not t.startswith("skipped"))
        conflicts = sum(1 for t in texts if t.startswith("skipped"))
        print(f"  repaired      {repaired:,}"
              + (f"   ({conflicts} skipped — changed mid-sweep)" if conflicts else ""))
        print("    (mostly gender / mannequin / sub-category, which are not "
              "among the columns above)")
    print()
    print(f"  {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
