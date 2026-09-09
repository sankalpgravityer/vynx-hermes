#!/usr/bin/env python
"""Report products that reached review without a hanger id.

Two sheets. **Summary** counts the gap by stage, by tenant and by NULL-vs-blank,
and profiles the values that ARE set; **Products** is one row per affected
product with an autofilter.

Scoped BY DEFAULT to the two stages a reviewer works — REVIEW and APPROVED.

    # PRODUCTION, every tenant. Pass the connection string with --db.
    python scripts/audit_product_hanger.py \
        --db "postgresql://user:pass@prod-host:5432/vnyx" --all-tenants

    # One tenant, to a named file.
    python scripts/audit_product_hanger.py --db "postgresql://..." \
        --tenant 6045eee9-6b87-45f2-a582-2b47ea752c39 --out boas-hangers.xlsx

    # Every stage, including LABEL and the terminal ones.
    python scripts/audit_product_hanger.py --db "postgresql://..." \
        --all-tenants --all-stages

    The connection string can also come from DATABASE_URL. `--dsn` is an alias
    for `--db`.

READ-ONLY, AND SAFE TO POINT AT PRODUCTION.

No `--apply`, no UPDATE, no DDL — and the connection enforces that rather than
merely promising it. See `connect()`.


THE TRAP: EMPTY IS MOSTLY NOT NULL

Measured on production, REVIEW + APPROVED: 1,663 products have `hanger IS NULL`
and a further 774 hold an EMPTY STRING. An `IS NULL` report therefore misses
just under a third of them, and every one behaves identically to a null for
anyone looking for the garment on a rail.

So "empty" here means NULL **or** blank after trimming, and the Summary counts
the two separately, because a field somebody wrote as "" has a different cause
from one that was never set.


WHAT IS NOT REPORTED AS MISSING

A hanger id that is set but looks odd. The temptation is to flag short values as
junk, and it would be wrong: `B11`, `A09`, `HOLI17` and `ER12` are real rail
labels, and `R` — which appears on 530 products — is 528 of them on Kilo Kilo
Vintage alone, so it is that tenant's convention rather than a defect. Calling
those defective would have put 530 correct products on the list.

The Summary profiles the value shapes instead, so anything genuinely odd is
visible without this script deciding it is wrong.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg is required:  pip install 'psycopg[binary]'")

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover
    sys.exit("openpyxl is required:  pip install openpyxl")

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


DEFAULT_APP_BASE = "https://try.vnyx.ai"
DEFAULT_MAX_DETAIL_ROWS = 100_000

# From ProductStage in schema.prisma: REVIEW is reviewStatus=PENDING and
# APPROVED is reviewStatus=ACCEPTED. Everything else is upstream of review
# (LABEL, DECISION, PHOTOBOOTH, MISSING_LABEL, IMPORTED) or terminal (REJECTED,
# DELETED, ARCHIVED). A product still at LABEL has not been hung YET, which is
# not the same defect as one that reached review without a hanger.
DEFAULT_STAGES = ["REVIEW", "APPROVED"]

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
BAD_FILL = PatternFill("solid", fgColor="FDECEA")
WARN_FILL = PatternFill("solid", fgColor="FFF8E1")
OK_FILL = PatternFill("solid", fgColor="E8F5E9")

TIGHT_SIBLINGS = 20

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


@dataclass
class Row:
    product_id: str
    product_code: str | None
    title: str | None
    hanger: str | None
    lpn: str | None
    bin_code: str | None
    review_status: str | None
    stage: str | None
    tenant_id: str | None
    tenant_name: str | None
    owner: str | None
    created_at: datetime | None
    stage_at: datetime | None

    # Filled by find_siblings().
    sib_via: str = ""        # 'LPN' | 'bin'
    sib_code: str = ""       # the carton or bin they share
    sib_product: str = ""
    sib_title: str = ""
    sib_hanger: str = ""
    sib_stage: str = ""
    sib_count: int = 0

    @property
    def signal(self) -> str:
        """How useful the sibling pointer actually is.

        Measured on production: 2,340 of the matches are LPN cartons, but the
        median carton holds 757 hung products. "It is in that carton" is a
        true statement and a useless instruction. 894 sit in a carton of 20 or
        fewer, and those are the ones worth walking to.
        """
        if not self.sib_via:
            return ""
        if self.sib_count <= TIGHT_SIBLINGS:
            return f"tight — {self.sib_count} hung item(s)"
        return f"broad — {self.sib_count} hung items"

    @property
    def missing(self) -> bool:
        return self.hanger is None or self.hanger.strip() == ""

    @property
    def how(self) -> str:
        """NULL or empty string — the two have different causes."""
        if self.hanger is None:
            return "NULL — never set"
        if self.hanger.strip() == "":
            return "empty string — something wrote a blank"
        return ""

    @property
    def days_in_stage(self) -> int | None:
        """Tolerates a naive OR an aware timestamp.

        `timestamp` and `timestamptz` both appear across this schema and
        psycopg maps them to naive and aware datetimes respectively; comparing
        the wrong one against `utcnow` raises rather than returning a wrong
        number, which is how this surfaced.
        """
        if not self.stage_at:
            return None
        at = self.stage_at
        now = (datetime.now(timezone.utc) if at.tzinfo
               else datetime.now())
        return (now - at).days


def value_shape(value: str) -> str:
    """A label for the SHAPE of a hanger id that IS set.

    Descriptive, never a verdict. Short codes are real rail labels here.
    """
    v = value.strip()
    if UUID_RE.match(v):
        return "uuid"
    if len(v) <= 2:
        return "1-2 characters"
    if re.fullmatch(r"[A-Za-z]{1,4}\d{1,4}", v):
        return "rail code (letters + digits)"
    return "other"


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

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
        application_name="hermes-hanger-audit (read-only)",
        options=f"-c statement_timeout={int(statement_timeout_s) * 1000}",
    )
    conn.read_only = True
    return conn


SQL = """
SELECT p.id::text,
       p."productCode",
       p.title,
       p.hanger,
       -- LPN and bin are the OTHER ways to find a garment physically, and they
       -- turn a flat list into a prioritised one: no hanger but a known bin is
       -- an inconvenience, while no hanger AND no LPN AND no bin means nobody
       -- can locate the item at all. Both are one-to-many, so a lateral picks
       -- the most recent rather than fanning the row out.
       (SELECT l."lpnCode" FROM "LpnLine" ll
          JOIN "Lpn" l ON l.id = ll."lpnId"
         WHERE ll."productId" = p.id
         ORDER BY l."createdAt" DESC LIMIT 1),
       (SELECT bl."binCode" FROM "BinAssignment" ba
          JOIN "BinLocation" bl ON bl.id = ba."binLocationId"
         WHERE ba."productId" = p.id
         ORDER BY ba."assignedAt" DESC LIMIT 1),
       p."reviewStatus"::text,
       p."currentStage"::text,
       p."tenantId"::text,
       t.name,
       COALESCE(NULLIF(u.name, ''), u.email),
       p."createdAt",
       p."currentStageAt"
FROM "Product" p
LEFT JOIN "Tenant" t ON t.id = p."tenantId"
LEFT JOIN "User"   u ON u.id = p."createdById"
WHERE p."isDeleted" = false
  {tenant_clause}
  {stage_clause}
ORDER BY p."currentStageAt" ASC NULLS LAST
{limit_clause}
"""


def fetch(dsn: str, tenant: str | None, stages: list[str] | None,
          limit: int | None, statement_timeout_s: int,
          progress: bool) -> list[Row]:
    sql = SQL.format(
        tenant_clause='AND p."tenantId" = %(tenant)s::uuid' if tenant else "",
        stage_clause='AND p."currentStage"::text = ANY(%(stages)s)' if stages else "",
        limit_clause="LIMIT %(limit)s" if limit else "",
    )
    params: dict[str, object] = {}
    if tenant:
        params["tenant"] = tenant
    if stages:
        params["stages"] = stages
    if limit:
        params["limit"] = limit

    if progress:
        print("Reading products…", flush=True)
    with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = [Row(*rec) for rec in cur.fetchall()]
    if progress:
        print(f"  {len(rows):,} live product(s)", flush=True)
    return rows




# --------------------------------------------------------------------------- #
# "Is this product somewhere else with a hanger id?"
#
# The literal question cannot be answered as asked, and it is worth being clear
# why. A product row holds ONE `currentStage`, so it cannot be in two at once;
# `ProductStageMovement` records the transitions but not the hanger, so there is
# no history of what the value used to be; and there is no parent/copy link on
# `Product`. Matching duplicate rows on title finds exactly ONE pair in the
# whole production catalogue, because titles are generated and near-unique.
#
# So the useful reading is the physical one: the SAME CARTON. A product with no
# hanger sitting in an LPN whose other items are hung was not un-hangable — it
# was skipped, and the carton tells you where to go and look. On production that
# describes 2,334 of 2,499, which turns "2,499 unknowns" into "one pass over a
# few dozen cartons".
#
# LPN first, bin second: the carton is the unit a person actually walks to, and
# a bin can hold several cartons.
# --------------------------------------------------------------------------- #

SIB_LPN_SQL = """
SELECT ll."productId"::text            AS missing_id,
       l."lpnCode",
       sp.id::text, sp.title, sp.hanger, sp."currentStage"::text,
       count(*) OVER (PARTITION BY ll."productId") AS n
FROM "LpnLine" ll
JOIN "Lpn"     l  ON l.id  = ll."lpnId"
JOIN "LpnLine" sll ON sll."lpnId" = ll."lpnId" AND sll."productId" <> ll."productId"
JOIN "Product" sp ON sp.id = sll."productId"
WHERE ll."productId" = ANY(%(ids)s::uuid[])
  AND sp."isDeleted" = false
  AND btrim(COALESCE(sp.hanger, '')) <> ''
"""

SIB_BIN_SQL = """
SELECT ba."productId"::text            AS missing_id,
       bl."binCode",
       sp.id::text, sp.title, sp.hanger, sp."currentStage"::text,
       count(*) OVER (PARTITION BY ba."productId") AS n
FROM "BinAssignment" ba
JOIN "BinLocation"   bl  ON bl.id = ba."binLocationId"
JOIN "BinAssignment" sba ON sba."binLocationId" = ba."binLocationId"
                        AND sba."productId" <> ba."productId"
JOIN "Product" sp ON sp.id = sba."productId"
WHERE ba."productId" = ANY(%(ids)s::uuid[])
  AND sp."isDeleted" = false
  AND btrim(COALESCE(sp.hanger, '')) <> ''
"""


def find_siblings(dsn: str, missing: list[Row], statement_timeout_s: int,
                  progress: bool) -> None:
    """Fill the sib_* fields in place. Best-effort; never raises."""
    if not missing:
        return
    ids = [r.product_id for r in missing]
    by_id = {r.product_id: r for r in missing}
    if progress:
        print(f"Looking for hung siblings of {len(ids):,} product(s)…",
              flush=True)

    def load(sql: str, via: str) -> None:
        with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
            cur.execute(sql, {"ids": ids})
            for mid, code, sid, stitle, shanger, sstage, n in cur.fetchall():
                r = by_id.get(mid)
                # LPN wins: it is loaded first and a bin match must not
                # overwrite the more specific answer.
                if r is None or r.sib_via:
                    continue
                r.sib_via, r.sib_code = via, code or ""
                r.sib_product, r.sib_title = sid, stitle or ""
                r.sib_hanger, r.sib_stage = shanger or "", sstage or ""
                r.sib_count = int(n or 0)

    load(SIB_LPN_SQL, "LPN")
    load(SIB_BIN_SQL, "bin")
    if progress:
        found = sum(1 for r in missing if r.sib_via)
        print(f"  {found:,} have a hung sibling", flush=True)


SIB_HEADERS = [
    "Signal", "Product ID", "Title", "Stage", "Days in stage",
    "Shared via", "Carton / bin", "Hung siblings",
    "Sibling hanger id", "Sibling stage", "Sibling product", "Sibling title",
    "Tenant", "Edit URL",
]
SIB_WIDTHS = {
    1: 24, 2: 38, 3: 44, 4: 12, 5: 13, 6: 12, 7: 20, 8: 14,
    9: 38, 10: 14, 11: 38, 12: 44, 13: 20, 14: 74,
}


def _siblings_sheet(wb, missing: list[Row], app_base: str, cap: int) -> None:
    found = [r for r in missing if r.sib_via]
    ws = wb.create_sheet("Found elsewhere")
    _header(ws, SIB_HEADERS)
    # Tightest carton first. Sorting by tenant would bury the 894 precise
    # pointers under 1,446 that say "somewhere in this pallet".
    for r in sorted(found, key=lambda x: (x.sib_count,
                                          x.tenant_name or ""))[:cap]:
        ws.append([
            r.signal,
            r.product_id, r.title or "", r.stage or "",
            r.days_in_stage if r.days_in_stage is not None else "",
            r.sib_via, r.sib_code, r.sib_count,
            r.sib_hanger, r.sib_stage, r.sib_product, r.sib_title,
            r.tenant_name or "",
            edit_url(app_base, r.product_id, r.tenant_id),
        ])
        ws.cell(row=ws.max_row, column=1).fill = (
            OK_FILL if r.sib_count <= TIGHT_SIBLINGS else WARN_FILL
        )
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    if not found:
        ws.append(["No product in this scope shares a carton or bin with a "
                   "product that has a hanger id."])
    if len(found) > cap:
        ws.append([])
        ws.append([f"… {len(found) - cap:,} more row(s) not listed."])
    ws.auto_filter.ref = (f"A1:{get_column_letter(len(SIB_HEADERS))}"
                          f"{min(len(found), cap) + 1}")
    _autosize(ws, SIB_WIDTHS)


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

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


def edit_url(base: str, product_id: str, tenant_id: str | None) -> str:
    """Deliberately WITHOUT `fromTab`.

    The tab filters on the product's CURRENT stage, so a stale one hides the
    product exactly as effectively as a wrong tenant does — and the stage has
    its own column here. The tenantId is the part that must be right: product
    search is tenant-scoped, so opening one tenant's product under another's id
    returns "no result found", which reads as a missing product rather than a
    wrong link.
    """
    if not tenant_id:
        return ""
    return f"{base.rstrip('/')}/product/{product_id}/edit?tenantId={tenant_id}"


def redact(dsn: str) -> str:
    m = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    if not m:
        return "(dsn not shown)"
    return f"{m.group(1)}://{m.group(2)}@{m.group(3)}/{m.group(4)}"


HEADERS = [
    "How it is empty", "Product ID", "Product code", "Title",
    "Review status", "Stage", "Days in stage", "LPN", "Bin",
    "Tenant", "Tenant ID", "Account owner", "Created", "Edit URL",
]
WIDTHS = {
    1: 30, 2: 38, 3: 14, 4: 46, 5: 16, 6: 12, 7: 14, 8: 18, 9: 14,
    10: 20, 11: 38, 12: 30, 13: 12, 14: 74,
}


def build_workbook(rows: list[Row], app_base: str, dsn_label: str,
                   tenant: str | None, stages: list[str] | None,
                   cap: int) -> Workbook:
    missing = [r for r in rows if r.missing]
    present = [r for r in rows if not r.missing]

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    def section(label: str) -> None:
        ws.append([])
        ws.append([label])
        cell = ws.cell(row=ws.max_row, column=1)
        cell.font = Font(bold=True, size=12)
        cell.fill = SECTION_FILL

    ws.append(["Missing hanger id"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["Generated", datetime.now(timezone.utc)
               .strftime("%Y-%m-%d %H:%M UTC")])
    ws.append(["Database", dsn_label])
    ws.append(["Tenant", tenant or "every tenant"])
    ws.append(["Stage", ", ".join(stages) if stages else "every stage"])
    ws.append(["Products in scope", len(rows)])
    ws.append([
        "Empty means",
        "NULL or blank after trimming — both leave the garment unfindable on a "
        "rail, and roughly a third of them are blanks rather than nulls.",
    ])

    # ---- the headline -------------------------------------------------------
    section("The gap")
    ws.append(["", "Products", "% of scope"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    total = len(rows) or 1
    ws.append(["MISSING a hanger id", len(missing),
               f"{100 * len(missing) / total:.1f}%"])
    for col in range(1, 4):
        ws.cell(row=ws.max_row, column=col).fill = BAD_FILL
    ws.append(["Has one", len(present), f"{100 * len(present) / total:.1f}%"])
    for col in range(1, 4):
        ws.cell(row=ws.max_row, column=col).fill = OK_FILL

    # ---- null vs blank ------------------------------------------------------
    section("NULL vs empty string")
    ws.append(["How", "Products", "Cause"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    nulls = sum(1 for r in missing if r.hanger is None)
    blanks = len(missing) - nulls
    ws.append(["NULL — never set", nulls,
               "No writer ever touched the column for this product."])
    ws.append(["Empty string", blanks,
               "Something WROTE a blank. Different cause, and invisible to an "
               "IS NULL query."])
    if blanks:
        ws.cell(row=ws.max_row, column=1).fill = WARN_FILL

    # ---- can the garment still be found? ------------------------------------
    #
    # The most useful number in the report, and the reason the LPN and bin
    # joins are worth two subqueries. A missing hanger id is an inconvenience
    # when the item is in a known bin and a genuine loss when it is not: on
    # production only 103 of 2,446 fall in the second group. Without this
    # split the sheet reads as 2,446 equally urgent problems.
    section("Can the garment still be found?")
    ws.append(["", "Products", "Meaning"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    lost = [r for r in missing if not (r.lpn or r.bin_code)]
    findable = len(missing) - len(lost)
    ws.append(["No hanger, but has an LPN or bin", findable,
               "Locatable. The hanger id is missing metadata, not a missing "
               "garment."])
    ws.cell(row=ws.max_row, column=1).fill = WARN_FILL
    ws.append(["No hanger, no LPN, no bin", len(lost),
               "Nothing records where this item physically is. Start here."])
    ws.cell(row=ws.max_row, column=1).fill = BAD_FILL

    # ---- the same carton ----------------------------------------------------
    sib = [r for r in missing if r.sib_via]
    section("Is it somewhere else WITH a hanger id?")
    ws.append(["", "Products", "Meaning"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    tight = [r for r in sib if r.sib_count <= TIGHT_SIBLINGS]
    ws.append([
        f"Same carton, {TIGHT_SIBLINGS} or fewer hung items", len(tight),
        "The useful ones. A small carton was hung and this item was skipped — "
        "go and look in it. Top of the 'Found elsewhere' sheet.",
    ])
    ws.cell(row=ws.max_row, column=1).fill = OK_FILL
    ws.append([
        "Same carton, but a large one", len(sib) - len(tight),
        "True but weak: the median carton here holds 757 hung products, so "
        "this points at a pallet rather than a shelf.",
    ])
    ws.cell(row=ws.max_row, column=1).fill = WARN_FILL
    ws.append([
        "No hung neighbour anywhere", len(missing) - len(sib),
        "Nothing nearby carries a hanger id either.",
    ])
    ws.cell(row=ws.max_row, column=1).fill = BAD_FILL
    ws.append([])
    ws.append([
        "The same product cannot sit in two stages: a row holds one "
        "currentStage, ProductStageMovement records transitions but not the "
        "hanger, and there is no copy link on Product. Matching duplicate rows "
        "on title finds ONE pair in the whole catalogue. So this asks the "
        "physical question instead — same carton, same bin."
    ])

    # ---- by stage -----------------------------------------------------------
    section("By stage")
    ws.append(["Stage", "Missing", "Has one", "In scope", "% missing"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    by_stage: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        by_stage[r.stage or "(none)"][0 if r.missing else 1] += 1
    for stage, (miss, has) in sorted(by_stage.items(), key=lambda kv: -kv[1][0]):
        ws.append([stage, miss, has, miss + has,
                   f"{100 * miss / (miss + has or 1):.1f}%"])

    # ---- by tenant ----------------------------------------------------------
    section("By tenant")
    ws.append(["Tenant", "Tenant ID", "Missing", "In scope", "% missing"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    per: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        key = (r.tenant_name or "(unknown)", r.tenant_id or "")
        per[key][0] += 1 if r.missing else 0
        per[key][1] += 1
    for (name, tid), (miss, seen) in sorted(per.items(), key=lambda kv: -kv[1][0]):
        ws.append([name, tid, miss, seen, f"{100 * miss / (seen or 1):.1f}%"])

    # ---- how long they have been sitting ------------------------------------
    section("How long the affected products have been in this stage")
    ws.append(["Age", "Products"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    buckets = [("0-7 days", 0, 7), ("8-30 days", 8, 30),
               ("31-90 days", 31, 90), ("over 90 days", 91, 10**6)]
    ages = [r.days_in_stage for r in missing if r.days_in_stage is not None]
    for label, lo, hi in buckets:
        ws.append([label, sum(1 for d in ages if lo <= d <= hi)])
    if len(ages) < len(missing):
        ws.append(["unknown", len(missing) - len(ages)])

    # ---- what the SET values look like --------------------------------------
    section("Shape of the hanger ids that ARE set  (not a defect list)")
    ws.append(["Shape", "Products", "Example"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    shapes: dict[str, list] = defaultdict(lambda: [0, ""])
    for r in present:
        s = value_shape(r.hanger or "")
        shapes[s][0] += 1
        if not shapes[s][1]:
            shapes[s][1] = (r.hanger or "").strip()
    for shape, (n, example) in sorted(shapes.items(), key=lambda kv: -kv[1][0]):
        ws.append([shape, n, example])
    ws.append([])
    ws.append([
        "Short codes are NOT flagged as defective. B11, A09, HOLI17 and ER12 "
        "are real rail labels, and R — 530 products — is 528 of them on one "
        "tenant, so it is that tenant's convention. This section exists so "
        "anything genuinely odd is visible without the script calling it wrong."
    ])

    _autosize(ws, {1: 42, 2: 40, 3: 46, 4: 14, 5: 14})

    # ---- the detail sheet ---------------------------------------------------
    ws2 = wb.create_sheet("Products")
    _header(ws2, HEADERS)
    for r in missing[:cap]:
        ws2.append([
            r.how,
            r.product_id,
            r.product_code or "",
            r.title or "",
            r.review_status or "",
            r.stage or "",
            r.days_in_stage if r.days_in_stage is not None else "",
            r.lpn or "",
            r.bin_code or "",
            r.tenant_name or "",
            r.tenant_id or "",
            r.owner or "",
            r.created_at.date().isoformat() if r.created_at else "",
            edit_url(app_base, r.product_id, r.tenant_id),
        ])
        if r.hanger is not None:
            ws2.cell(row=ws2.max_row, column=1).fill = WARN_FILL
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    if len(missing) > cap:
        ws2.append([])
        ws2.append([f"… {len(missing) - cap:,} more row(s) not listed "
                    f"(--max-detail-rows {cap})."])
    ws2.auto_filter.ref = (f"A1:{get_column_letter(len(HEADERS))}"
                           f"{min(len(missing), cap) + 1}")
    _autosize(ws2, WIDTHS)

    _siblings_sheet(wb, missing, app_base, cap)
    return wb


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report products that reached review without a hanger id. "
                    "Read-only."
    )
    p.add_argument("--db", "--dsn", dest="db", default=None,
                   help="Postgres connection string. Falls back to DATABASE_URL.")
    p.add_argument("--tenant", default=None, help="Tenant uuid to audit.")
    p.add_argument("--all-tenants", action="store_true",
                   help="Every tenant. Required if --tenant is not given.")
    p.add_argument("--stage", default=",".join(DEFAULT_STAGES),
                   help=f"Comma-separated ProductStage values (default "
                        f"{','.join(DEFAULT_STAGES)} — the two stages a "
                        f"reviewer works). Use --all-stages for every stage.")
    p.add_argument("--all-stages", action="store_true",
                   help="Every stage, including LABEL and the terminal ones.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of products read.")
    p.add_argument("--out", default=None, help="Output .xlsx path.")
    p.add_argument("--max-detail-rows", type=int,
                   default=DEFAULT_MAX_DETAIL_ROWS,
                   help="Cap rows on the Products sheet (default 100,000). A "
                        "cap that fires is stated on the sheet; the Summary is "
                        "always computed over every product.")
    p.add_argument("--statement-timeout", type=int, default=300,
                   metavar="SECONDS", help="Per-query timeout (default 300).")
    p.add_argument("--app-base-url", default=DEFAULT_APP_BASE,
                   help=f"Front-end origin for the Edit URL column "
                        f"(default {DEFAULT_APP_BASE}).")
    p.add_argument("--quiet", action="store_true", help="No progress output.")
    args = p.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL")
    if not dsn:
        return print("No database. Pass --db or set DATABASE_URL.") or 2
    if not args.tenant and not args.all_tenants:
        return print("Pass --tenant <uuid> or --all-tenants.") or 2

    stages = None if args.all_stages else [
        s.strip().upper() for s in args.stage.split(",") if s.strip()
    ]

    progress = not args.quiet
    if progress:
        print(f"Database : {redact(dsn)}")
        print(f"Tenant   : {args.tenant or 'every tenant'}")
        print(f"Stage    : {', '.join(stages) if stages else 'every stage'}")
        print("Mode     : READ-ONLY (no --apply exists)")

    rows = fetch(dsn, args.tenant, stages, args.limit, args.statement_timeout,
                 progress)
    if not rows:
        return print("No live products matched.") or 1

    find_siblings(dsn, [r for r in rows if r.missing],
                  args.statement_timeout, progress)

    out = Path(args.out) if args.out else Path(
        f"reports/product-hanger-{datetime.now():%Y-%m-%d-%H%M}.xlsx"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    wb = build_workbook(rows, args.app_base_url, redact(dsn), args.tenant,
                        stages, args.max_detail_rows)
    wb.save(out)

    missing = [r for r in rows if r.missing]
    nulls = sum(1 for r in missing if r.hanger is None)
    print("\n--- summary ---")
    print(f"  in scope              : {len(rows):,}")
    print(f"  MISSING a hanger id   : {len(missing):,}  "
          f"({100 * len(missing) / (len(rows) or 1):.1f}%)")
    print(f"    NULL                : {nulls:,}")
    print(f"    empty string        : {len(missing) - nulls:,}")
    print(f"  has one               : {len(rows) - len(missing):,}")
    lost = sum(1 for r in missing if not (r.lpn or r.bin_code))
    print(f"\n  of the {len(missing):,} missing:")
    print(f"    locatable via LPN/bin : {len(missing) - lost:,}")
    print(f"    NOT locatable at all  : {lost:,}  <- start here")
    sib = sum(1 for r in missing if r.sib_via)
    tight = sum(1 for r in missing
                if r.sib_via and r.sib_count <= TIGHT_SIBLINGS)
    print(f"    same carton as a hung product     : {sib:,}")
    print(f"      of which a SMALL carton (<={TIGHT_SIBLINGS})    : {tight:,}"
          f"  <- actually findable")
    print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
