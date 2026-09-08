#!/usr/bin/env python
"""Report products that reached review with a missing size chart or missing
attribute data.

Three sheets. **Summary** counts each gap and names the root cause; **Products**
is one row per affected product saying exactly what it is missing; **Sizing
guides** is the root-cause sheet — the guides that have no chart images, and how
many products each one blocks.

Scoped BY DEFAULT to the two stages a reviewer works — REVIEW and APPROVED.

    # PRODUCTION, every tenant. Pass the connection string with --db.
    python scripts/audit_product_data.py \
        --db "postgresql://user:pass@prod-host:5432/vnyx" --all-tenants

    # One tenant, to a named file.
    python scripts/audit_product_data.py --db "postgresql://..." \
        --tenant 1779ea6e-6231-4215-b836-ef829de9ab4b --out bleckmann.xlsx

    The connection string can also come from DATABASE_URL. `--dsn` is an alias
    for `--db`.

READ-ONLY, AND SAFE TO POINT AT PRODUCTION. No `--apply`, no UPDATE, no DDL —
and the connection enforces that rather than merely promising it.


THE SIZE CHART IS NOT A PROPERTY OF THE PRODUCT

`ProductMediaView.SIZE_CHART` is, in the schema's own words, "copied from
ProductSize.sizeChartImages". So a product's chart is downstream of TWO things:
which sizing guide is selected on it (`Product.sizingGuideId` -> `ProductSize`),
and whether that guide has any images uploaded. Checking only the product tells
you a chart is absent; it cannot tell you why, and the why is where the fix is.

Measured on production, REVIEW + APPROVED, the split is stark:

    182   no sizing guide selected at all
  1,302   guide selected, but THE GUIDE HAS NO CHART IMAGES
     48   guide has images, but none were copied to the product

Those 1,302 are not 1,302 problems. They are TEN sizing guides — nine of them
Bleckmann's — with no images uploaded, and no amount of per-product work fixes
any of them. That is why the third sheet exists.


CHART / GUIDE AGREEMENT

The report also states, per product, whether the chart it carries actually came
from the guide it names. On production 2,699 of 2,703 agree and 4 carry a chart
with no guide selected, so this is a rare defect rather than a common one — but
a chart that disagrees with the guide is the kind of thing nothing else would
ever surface, and it is one column.
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
DEFAULT_STAGES = ["REVIEW", "APPROVED"]

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
BAD_FILL = PatternFill("solid", fgColor="FDECEA")
WARN_FILL = PatternFill("solid", fgColor="FFF8E1")
OK_FILL = PatternFill("solid", fgColor="E8F5E9")

# Values that are present in the column but mean nothing. `policy.yaml` keeps
# the same list under `confidence.placeholders`; the AI writes "Unknown" rather
# than leaving a key out, so a plain empty-check would call these populated.
PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "-", "null", "tbd"}

# The titles the API writes on creation, which the analyze worker overwrites
# once it has a verdict. A product still carrying one, with empty properties,
# never finished analysing — so EVERY field is missing and none of it is a
# data-entry problem. Left in, these sort straight to the top of the sheet with
# seven gaps each and bury the products somebody can actually fix.
#
# TWO forms, not one. "Generating Product…" is the obvious one (52 products);
# "Pending - hanger <uuid>" is the second (21 more) and is easy to miss because
# it looks like real text. The ProductMedia.altText comment in schema.prisma
# names it explicitly — "rows created while the product is still `Pending -
# hanger …`" — which is how it turned up here.
#
# Excluded by default; --include-never-analysed puts them back.
PLACEHOLDER_TITLES = ("generating product", "pending - hanger")


def blank(value) -> bool:
    """Missing, blank, or a placeholder — all the same to a reader.

    Handles the list form too: `properties.gender` is written as `["men"]` by
    the analyze worker and as a bare string elsewhere.
    """
    if value is None:
        return True
    if isinstance(value, list):
        return not any(not blank(v) for v in value)
    return str(value).strip().lower() in PLACEHOLDERS


# Attribute checks, in the order they appear on the sheet. The lambda is given
# the parsed `properties` dict plus the product row, because some of these live
# in a column and some in the JSON.
ATTRIBUTES: list[tuple[str, str]] = [
    ("gender", "Gender"),
    ("size", "Size"),
    ("brand", "Brand"),
    ("color", "Colour"),
    ("material", "Material"),
    ("condition", "Condition"),
]


# A jacket is an upper. A product filed under "Jackets" whose sizing guide reads
# "Women Bottoms" shows its customer a waist/hip/thigh/inseam table for a coat.
# Nothing in the system notices, because the guide is a free choice and no rule
# ties it to the category.
#
# DELIBERATELY CONSERVATIVE. Only garments whose category is UNAMBIGUOUSLY an
# upper or a bottom are judged. Shoes, bags, accessories, caps and dresses have
# no obvious right answer, and a false "wrong guide" is worse than a quiet one.
# UPPER is tested FIRST: "Denim Jackets" would otherwise be claimed by the
# bottom keyword "denim".
UPPER_CATEGORY = re.compile(
    r"jacket|coat|blazer|shirt|tee|t-shirt|\btop\b|blouse|sweater|hoodie"
    r"|sweatshirt|knit|jumper|cardigan|tank|polo|vest", re.I)
BOTTOM_CATEGORY = re.compile(
    r"trouser|jean|short|skirt|legging|jogger|chino|\bpant", re.I)


def category_side(category: str | None, sub: str | None) -> str | None:
    """'upper' | 'bottom' | None when the category does not clearly say."""
    text = f"{category or ''} {sub or ''}"
    if UPPER_CATEGORY.search(text):
        return "upper"
    if BOTTOM_CATEGORY.search(text):
        return "bottom"
    return None


def guide_side(guide: str | None) -> str | None:
    """Kids and Defaults guides serve either side, so they are never wrong."""
    g = (guide or "").lower()
    if "bottom" in g:
        return "bottom"
    if "upper" in g or "dressshirt" in g:
        return "upper"
    return None


@dataclass
class Row:
    product_id: str
    product_code: str | None
    title: str | None
    properties: dict | None
    intl_size: str | None
    guide_id: str | None
    guide_name_on_product: str | None
    guide_name: str | None
    guide_image_count: int
    chart_urls: list[str] = field(default_factory=list)
    guide_urls: list[str] = field(default_factory=list)
    has_care_label: bool = True
    master_category: str | None = None
    category: str | None = None
    sub_category: str | None = None
    review_status: str | None = None
    stage: str | None = None
    tenant_id: str | None = None
    tenant_name: str | None = None
    owner: str | None = None
    created_at: datetime | None = None

    # ---- attributes -------------------------------------------------------
    def prop(self, *keys):
        p = self.properties if isinstance(self.properties, dict) else {}
        for k in keys:
            if k in p and not blank(p[k]):
                return p[k]
        return None

    def missing_attr(self, key: str) -> bool:
        if key == "gender":
            return self.prop("gender") is None
        if key == "size":
            # The column first, the JSON as a fallback — the export reads the
            # column, but the analyze worker writes the JSON.
            return (blank(self.intl_size)
                    and self.prop("international_size", "eu_size",
                                  "waist_size") is None)
        if key == "brand":
            # Both cases are in use on production; treating them as one field
            # is the difference between 229 missing brands and thousands.
            return self.prop("brand", "Brand") is None
        return self.prop(key) is None

    # ---- the size chart ---------------------------------------------------
    @property
    def chart_state(self) -> str:
        """One of five states, ordered by where the fix lives."""
        if not self.guide_id:
            return ("chart present, but NO sizing guide selected"
                    if self.chart_urls else "no sizing guide selected")
        if self.guide_name is None:
            return "sizing guide id does not resolve"
        if self.guide_image_count == 0:
            return "GUIDE HAS NO CHART IMAGES"
        if not self.chart_urls:
            return "guide has images, none copied to the product"
        if set(self.chart_urls) & set(self.guide_urls):
            return "ok"
        return "CHART DOES NOT MATCH THE SELECTED GUIDE"

    @property
    def chart_ok(self) -> bool:
        return self.chart_state == "ok"

    # ---- the guide against the product's own category ---------------------
    @property
    def guide_verdict(self) -> str:
        """Prose for the sheet. `guide_wrong` is the machine-readable half."""
        name = self.guide_name or self.guide_name_on_product
        if not name:
            return "No sizing guide selected"
        want = category_side(self.category, self.sub_category)
        if want is None:
            return (f'Not judged — "{self.category or "?"}" is neither clearly '
                    f"an upper nor a bottom")
        have = guide_side(name)
        if have is None:
            return f'Not judged — guide "{name}" is not upper/bottom specific'
        if want == have:
            return "Yes"
        return (f'NO — "{self.category}" is a {want}, but the guide is '
                f'"{name}" ({have}s)')

    @property
    def guide_wrong(self) -> bool:
        return self.guide_verdict.startswith("NO —")

    @property
    def missing_list(self) -> list[str]:
        out = []
        # FIRST, because it is upstream of the rest. Brand, size and
        # composition are read off the care label, so a product without one is
        # missing the evidence rather than merely the answer: measured on
        # production, 23.8% of products with no care label have no brand
        # against 2.8% of those with one.
        if not self.has_care_label:
            out.append("care label")
        if not self.chart_ok:
            out.append("size chart")
        # A guide that contradicts the category is a defect in its own right,
        # and counted here so it reaches the Gaps column that drives the
        # backfill's --min-gaps filter. The chart can be perfectly in step with
        # the guide and still be the wrong chart for the garment.
        if self.guide_wrong:
            out.append("wrong sizing guide")
        if blank(self.category):
            out.append("category")
        if blank(self.sub_category):
            out.append("sub-category")
        out.extend(label for key, label in ATTRIBUTES
                   if self.missing_attr(key))
        return out

    @property
    def never_analysed(self) -> bool:
        return (
            (self.title or "").strip().lower().startswith(PLACEHOLDER_TITLES)
            and not (self.properties or {})
        )

    @property
    def has_gap(self) -> bool:
        return bool(self.missing_list)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def connect(dsn: str, statement_timeout_s: int):
    """A connection that CANNOT write, and cannot run away on a busy database.

    `conn.read_only` means POSTGRES rejects a write, not merely that this file
    contains none; `statement_timeout` stops a query pinning a backend; and
    `application_name` makes the connection identifiable in pg_stat_activity.
    """
    conn = psycopg.connect(
        dsn,
        connect_timeout=30,
        application_name="hermes-product-data-audit (read-only)",
        options=f"-c statement_timeout={int(statement_timeout_s) * 1000}",
    )
    conn.read_only = True
    return conn


SQL = """
SELECT p.id::text,
       p."productCode",
       p.title,
       p.properties,
       p."internationalSize",
       p."sizingGuideId",
       p."sizingGuide",
       ps.name,
       COALESCE(array_length(ps."sizeChartImages", 1), 0),
       COALESCE(ps."sizeChartImages", ARRAY[]::text[]),
       COALESCE(ARRAY(
         SELECT m.url FROM "ProductMedia" m
          WHERE m."productId" = p.id AND m."isCurrent"
            AND m."deletedAt" IS NULL AND m.view::text = 'SIZE_CHART'
       ), ARRAY[]::text[]),
       -- The care-label macro. Upstream of brand, size, material and
       -- composition: it is the photograph a human or the AI reads those off,
       -- so a product without one is missing the SOURCE of the attributes
       -- rather than just the attributes.
       EXISTS (
         SELECT 1 FROM "ProductMedia" m
          WHERE m."productId" = p.id AND m."isCurrent"
            AND m."deletedAt" IS NULL AND m.view::text = 'LABEL'
       ),
       p."masterCategory",
       p.category,
       p."subCategory",
       p."reviewStatus"::text,
       p."currentStage"::text,
       p."tenantId"::text,
       t.name,
       COALESCE(NULLIF(u.name, ''), u.email),
       p."createdAt"
FROM "Product" p
LEFT JOIN "ProductSize" ps ON ps.id::text = p."sizingGuideId"
LEFT JOIN "Tenant" t ON t.id = p."tenantId"
LEFT JOIN "User"   u ON u.id = p."createdById"
WHERE p."isDeleted" = false
  {tenant_clause}
  {stage_clause}
  {status_clause}
ORDER BY p."createdAt" DESC
{limit_clause}
"""


def fetch(dsn: str, tenant: str | None, stages: list[str] | None,
          limit: int | None, statement_timeout_s: int,
          progress: bool, statuses: list[str] | None = None) -> list[Row]:
    sql = SQL.format(
        tenant_clause='AND p."tenantId" = %(tenant)s::uuid' if tenant else "",
        stage_clause='AND p."currentStage"::text = ANY(%(stages)s)' if stages else "",
        status_clause='AND p."reviewStatus"::text = ANY(%(statuses)s)' if statuses else "",
        limit_clause="LIMIT %(limit)s" if limit else "",
    )
    params: dict[str, object] = {}
    if tenant:
        params["tenant"] = tenant
    if stages:
        params["stages"] = stages
    if statuses:
        params["statuses"] = statuses
    if limit:
        params["limit"] = limit

    if progress:
        print("Reading products…", flush=True)
    with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        rows = []
        for rec in cur.fetchall():
            (pid, code, title, props, intl, gid, gname_p, gname, gcount,
             gurls, curls, has_label, master, cat, sub, rstatus, stage, tid,
             tname, owner, created) = rec
            rows.append(Row(
                product_id=pid, product_code=code, title=title,
                properties=props, intl_size=intl, guide_id=gid,
                guide_name_on_product=gname_p, guide_name=gname,
                guide_image_count=gcount, guide_urls=list(gurls or []),
                chart_urls=list(curls or []), has_care_label=bool(has_label),
                master_category=master, category=cat, sub_category=sub,
                review_status=rstatus,
                stage=stage, tenant_id=tid, tenant_name=tname, owner=owner,
                created_at=created,
            ))
    if progress:
        print(f"  {len(rows):,} live product(s)", flush=True)
    return rows


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
    """With tenantId, without fromTab.

    Product search is tenant-scoped, so a link carrying the wrong tenant
    returns "no result found" — which reads as a missing product rather than a
    wrong link. The tab filters on current stage and would hide the product the
    same way, so stage gets its own column instead.
    """
    if not tenant_id:
        return ""
    return f"{base.rstrip('/')}/product/{product_id}/edit?tenantId={tenant_id}"


def redact(dsn: str) -> str:
    m = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    if not m:
        return "(dsn not shown)"
    return f"{m.group(1)}://{m.group(2)}@{m.group(3)}/{m.group(4)}"


HEADERS = (
    ["Missing", "Gaps", "Care label", "Size chart state", "Sizing guide",
     "Sizing guide matches category?", "Guide images", "Charts on product",
     "Master category", "Category", "Sub-category"]
    + [label for _, label in ATTRIBUTES]
    + ["Product ID", "Product code", "Title", "Review status", "Stage",
       "Tenant", "Tenant ID", "Account owner", "Created", "Edit URL"]
)
# By NAME, not position. The positional version silently mis-sized every column
# after the first insertion, and the fills below index off the same map — a
# hardcoded `column=8` is a bug waiting for the next column to be added.
_WIDTH_BY_NAME = {
    "Missing": 52, "Gaps": 7, "Care label": 12, "Size chart state": 40,
    "Sizing guide": 20, "Sizing guide matches category?": 62,
    "Guide images": 13, "Charts on product": 16,
    "Master category": 15, "Category": 22, "Sub-category": 22,
    "Product ID": 38, "Product code": 14, "Title": 44, "Review status": 15,
    "Stage": 12, "Tenant": 20, "Tenant ID": 38, "Account owner": 28,
    "Created": 12, "Edit URL": 74,
}
WIDTHS = {i: _WIDTH_BY_NAME.get(h, 11) for i, h in enumerate(HEADERS, start=1)}
COL = {h: i for i, h in enumerate(HEADERS, start=1)}


def build_workbook(rows: list[Row], app_base: str, dsn_label: str,
                   tenant: str | None, stages: list[str] | None,
                   cap: int, include_stalled: bool = False,
                   list_all: bool = False) -> Workbook:
    excluded = [] if include_stalled else [r for r in rows if r.never_analysed]
    # Everything below counts only products whose analysis actually finished.
    rows = [r for r in rows if include_stalled or not r.never_analysed]
    # `list_all` puts EVERY product in scope on the sheet, not just the ones
    # with a gap. Worth having: "show me the review queue and what each row is
    # missing" is a different question from "show me the defects", and a sheet
    # that silently omits the healthy rows cannot answer the first one.
    affected = list(rows) if list_all else [r for r in rows if r.has_gap]

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    def section(label: str) -> None:
        ws.append([])
        ws.append([label])
        cell = ws.cell(row=ws.max_row, column=1)
        cell.font = Font(bold=True, size=12)
        cell.fill = SECTION_FILL

    ws.append(["Missing size charts and product data"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["Generated", datetime.now(timezone.utc)
               .strftime("%Y-%m-%d %H:%M UTC")])
    ws.append(["Database", dsn_label])
    ws.append(["Tenant", tenant or "every tenant"])
    ws.append(["Stage", ", ".join(stages) if stages else "every stage"])
    ws.append(["Products in scope", len(rows)])
    ws.append(["With at least one gap", len(affected)])
    ws.append(["Missing means",
               "absent, blank, or a placeholder such as \"Unknown\" — the AI "
               "writes those rather than omitting the key."])
    if excluded:
        ws.append([
            "Excluded",
            f"{len(excluded)} product(s) whose analysis never finished "
            f"(placeholder title, empty properties). Every field is missing on "
            f"those and none of it is a data-entry problem — re-run the "
            f"analysis. Pass --include-never-analysed to list them.",
        ])
        ws.cell(row=ws.max_row, column=1).fill = WARN_FILL

    # ---- the size chart, by WHERE THE FIX LIVES -----------------------------
    section("Size chart — and where the fix actually lives")
    ws.append(["State", "Products", "Who fixes it"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    order = [
        ("ok", "Nobody — the chart matches its guide.", OK_FILL),
        ("GUIDE HAS NO CHART IMAGES",
         "A TENANT ADMIN, once per guide. Not a product problem: no per-product "
         "work fixes any of these. See the 'Sizing guides' sheet.", BAD_FILL),
        ("guide has images, none copied to the product",
         "Re-apply the sizing guide on the product.", WARN_FILL),
        ("no sizing guide selected",
         "Pick a sizing guide on the product.", WARN_FILL),
        ("chart present, but NO sizing guide selected",
         "A chart with nothing behind it — pick the guide it came from.",
         WARN_FILL),
        ("CHART DOES NOT MATCH THE SELECTED GUIDE",
         "The product carries a chart from a DIFFERENT guide. Decide which is "
         "right, then re-apply.", BAD_FILL),
        ("sizing guide id does not resolve",
         "Dangling reference — the guide was deleted.", BAD_FILL),
    ]
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.chart_state] += 1
    for state, who, fill in order:
        if not counts.get(state):
            continue
        ws.append([state, counts[state], who])
        for col in range(1, 4):
            ws.cell(row=ws.max_row, column=col).fill = fill

    # ---- the care label, and what it costs ----------------------------------
    #
    # Not just a count. The care label is the photograph brand, size and
    # composition are READ OFF, so its absence explains the attribute gaps
    # rather than sitting beside them — and the sheet should show that link
    # rather than leave a reader to assume it.
    section("Care label — the source of brand, size and composition")
    no_label = [r for r in rows if not r.has_care_label]
    with_label = [r for r in rows if r.has_care_label]
    ws.append(["", "Products", "No brand", "No size", "No material"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    def _rates(group: list[Row]) -> list:
        n = len(group) or 1
        out = []
        for key in ("brand", "size", "material"):
            k = sum(1 for r in group if r.missing_attr(key))
            out.append(f"{k} ({100 * k / n:.1f}%)")
        return out

    ws.append(["NO care label", len(no_label), *_rates(no_label)])
    for col in range(1, 6):
        ws.cell(row=ws.max_row, column=col).fill = BAD_FILL
    ws.append(["Has one", len(with_label), *_rates(with_label)])
    for col in range(1, 6):
        ws.cell(row=ws.max_row, column=col).fill = OK_FILL
    if no_label and with_label:
        a = sum(1 for r in no_label if r.missing_attr("brand")) / len(no_label)
        b = (sum(1 for r in with_label if r.missing_attr("brand"))
             / len(with_label))
        if b:
            ws.append([])
            ws.append([
                f"A product with no care label is {a / b:.1f}x more likely to "
                f"be missing its brand. Photograph the label first — filling "
                f"the fields in by hand without one is guesswork."
            ])
    ws.append([
        "Material is the exception: it is missing at a similar rate either "
        "way, so the care label does not explain that gap and something else "
        "does."
    ])

    # ---- attributes ---------------------------------------------------------
    section("Attribute data")
    ws.append(["Field", "Missing", "% of scope"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    total = len(rows) or 1
    for key, label in ATTRIBUTES:
        n = sum(1 for r in rows if r.missing_attr(key))
        ws.append([label, n, f"{100 * n / total:.1f}%"])
        if n:
            ws.cell(row=ws.max_row, column=1).fill = (
                BAD_FILL if n / total > 0.1 else WARN_FILL
            )

    # ---- the guide against the product's own category ----------------------
    section("Sizing guide vs the product's own category")
    ws.append(["Verdict", "Products", "Meaning"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    verdicts: dict[str, int] = defaultdict(int)
    for r in rows:
        v = r.guide_verdict
        key = ("WRONG GUIDE" if v.startswith("NO —")
               else "correct" if v == "Yes"
               else "no guide" if v == "No sizing guide selected"
               else "not judged")
        verdicts[key] += 1
    for key, note in (
        ("WRONG GUIDE",
         "The category says upper and the guide says bottoms, or the reverse. "
         "The customer is shown the wrong measurements."),
        ("correct", "Guide and category agree."),
        ("not judged",
         "Shoes, bags, accessories, caps, dresses — no obvious right answer, "
         "so not flagged."),
        ("no guide", "No sizing guide selected at all."),
    ):
        if verdicts.get(key):
            ws.append([key, verdicts[key], note])
            if key == "WRONG GUIDE":
                ws.cell(row=ws.max_row, column=1).fill = BAD_FILL
    ws.append([])
    ws.append(['A wrong sizing guide, and a missing category or sub-category, '
               'are counted in the "Missing" and "Gaps" columns on the '
               'Products sheet.'])

    # ---- how many gaps at once ---------------------------------------------
    section("Gaps per product")
    ws.append(["Gaps", "Products"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    per: dict[int, int] = defaultdict(int)
    for r in rows:
        per[len(r.missing_list)] += 1
    for n in sorted(per):
        ws.append([f"{n} gap(s)" if n else "none — complete", per[n]])

    # ---- by tenant ----------------------------------------------------------
    section("By tenant")
    ws.append(["Tenant", "Tenant ID", "Affected", "In scope", "% affected"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    bt: dict[tuple, list[int]] = defaultdict(lambda: [0, 0])
    for r in rows:
        key = (r.tenant_name or "(unknown)", r.tenant_id or "")
        bt[key][0] += 1 if r.has_gap else 0
        bt[key][1] += 1
    for (name, tid), (aff, seen) in sorted(bt.items(), key=lambda kv: -kv[1][0]):
        ws.append([name, tid, aff, seen, f"{100 * aff / (seen or 1):.1f}%"])

    _autosize(ws, {1: 46, 2: 40, 3: 62, 4: 14, 5: 14})

    # ---- Products -----------------------------------------------------------
    ws2 = wb.create_sheet("Products")
    _header(ws2, HEADERS)
    for r in sorted(affected, key=lambda x: (-len(x.missing_list),
                                             x.tenant_name or ""))[:cap]:
        ws2.append(
            [", ".join(r.missing_list), len(r.missing_list),
             "ok" if r.has_care_label else "MISSING",
             r.chart_state,
             r.guide_name or r.guide_name_on_product or "",
             r.guide_verdict,
             r.guide_image_count, len(r.chart_urls),
             r.master_category or "MISSING",
             r.category or "MISSING",
             r.sub_category or "MISSING"]
            + ["MISSING" if r.missing_attr(k) else "ok"
               for k, _ in ATTRIBUTES]
            + [r.product_id, r.product_code or "", r.title or "",
               r.review_status or "", r.stage or "", r.tenant_name or "",
               r.tenant_id or "", r.owner or "",
               r.created_at.date().isoformat() if r.created_at else "",
               edit_url(app_base, r.product_id, r.tenant_id)]
        )
        if not r.has_care_label:
            ws2.cell(row=ws2.max_row, column=COL["Care label"]).fill = BAD_FILL
        if not r.chart_ok:
            ws2.cell(row=ws2.max_row, column=COL["Size chart state"]).fill = (
                BAD_FILL if "NO CHART IMAGES" in r.chart_state
                or "NOT MATCH" in r.chart_state else WARN_FILL
            )
        verdict_cell = ws2.cell(
            row=ws2.max_row, column=COL["Sizing guide matches category?"])
        if r.guide_wrong:
            verdict_cell.fill = BAD_FILL
        elif r.guide_verdict == "Yes":
            verdict_cell.fill = OK_FILL
        elif r.guide_verdict == "No sizing guide selected":
            verdict_cell.fill = WARN_FILL
        for name in ("Master category", "Category", "Sub-category"):
            if ws2.cell(row=ws2.max_row, column=COL[name]).value == "MISSING":
                ws2.cell(row=ws2.max_row, column=COL[name]).fill = WARN_FILL
        for k, label in ATTRIBUTES:
            if r.missing_attr(k):
                ws2.cell(row=ws2.max_row, column=COL[label]).fill = WARN_FILL
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    if len(affected) > cap:
        ws2.append([])
        ws2.append([f"… {len(affected) - cap:,} more row(s) not listed."])
    ws2.auto_filter.ref = (f"A1:{get_column_letter(len(HEADERS))}"
                           f"{min(len(affected), cap) + 1}")
    _autosize(ws2, WIDTHS)

    # ---- Sizing guides — the root cause -------------------------------------
    ws3 = wb.create_sheet("Sizing guides")
    _header(ws3, ["Tenant", "Sizing guide", "Chart images", "Products using it",
                  "Products with no chart", "Verdict"])
    guides: dict[tuple, list] = defaultdict(lambda: [0, 0, 0])
    for r in rows:
        if not r.guide_name:
            continue
        key = (r.tenant_name or "(unknown)", r.guide_name, r.guide_image_count)
        guides[key][0] += 1
        guides[key][1] += 0 if r.chart_urls else 1
    for (tname, gname, imgs), (used, nochart, _) in sorted(
            guides.items(), key=lambda kv: (kv[0][2] != 0, -kv[1][1])):
        verdict = ("NO CHART IMAGES UPLOADED — every product on this guide is "
                   "missing its chart, and only a tenant admin can fix it"
                   if imgs == 0 else
                   "ok" if nochart == 0 else
                   f"has images, but {nochart} product(s) did not get one")
        ws3.append([tname, gname, imgs, used, nochart, verdict])
        if imgs == 0:
            for col in range(1, 7):
                ws3.cell(row=ws3.max_row, column=col).fill = BAD_FILL
        elif nochart:
            ws3.cell(row=ws3.max_row, column=6).fill = WARN_FILL
    _autosize(ws3, {1: 22, 2: 26, 3: 14, 4: 18, 5: 22, 6: 78})
    return wb


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report products missing a size chart or attribute data. "
                    "Read-only."
    )
    p.add_argument("--db", "--dsn", dest="db", default=None,
                   help="Postgres connection string. Falls back to DATABASE_URL.")
    p.add_argument("--tenant", default=None, help="Tenant uuid to audit.")
    p.add_argument("--all-tenants", action="store_true",
                   help="Every tenant. Required if --tenant is not given.")
    p.add_argument("--stage", default=",".join(DEFAULT_STAGES),
                   help=f"Comma-separated ProductStage values (default "
                        f"{','.join(DEFAULT_STAGES)}). --all-stages for every one.")
    p.add_argument("--review-status", default=None, metavar="STATUS",
                   help="Comma-separated ReviewStatus values, e.g. PENDING. "
                        "This is what the UI's Review tab filters on — 2,628 "
                        "products, of which 552 sit at LABEL stage and are "
                        "therefore MISSED by --stage REVIEW alone. Use this "
                        "with --all-stages to match the tab exactly.")
    p.add_argument("--all-stages", action="store_true",
                   help="Every stage, including LABEL and the terminal ones.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap the number of products read.")
    p.add_argument("--out", default=None, help="Output .xlsx path.")
    p.add_argument("--max-detail-rows", type=int,
                   default=DEFAULT_MAX_DETAIL_ROWS,
                   help="Cap rows on the Products sheet (default 100,000).")
    p.add_argument("--statement-timeout", type=int, default=300,
                   metavar="SECONDS", help="Per-query timeout (default 300).")
    p.add_argument("--app-base-url", default=DEFAULT_APP_BASE,
                   help=f"Front-end origin for the Edit URL column "
                        f"(default {DEFAULT_APP_BASE}).")
    p.add_argument("--all-products", action="store_true",
                   help="List every product in scope, not only those with a "
                        "gap. Complete rows show an empty Missing column.")
    p.add_argument("--include-never-analysed", action="store_true",
                   help="Also list products whose analysis never finished. "
                        "Excluded by default — they need a re-run, not data.")
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

    statuses = ([s.strip().upper() for s in args.review_status.split(",") if s.strip()]
                if args.review_status else None)
    if progress and statuses:
        print(f"Review   : {', '.join(statuses)}")

    rows = fetch(dsn, args.tenant, stages, args.limit, args.statement_timeout,
                 progress, statuses)
    if not rows:
        return print("No live products matched.") or 1

    out = Path(args.out) if args.out else Path(
        f"reports/product-data-{datetime.now():%Y-%m-%d-%H%M}.xlsx"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    wb = build_workbook(rows, args.app_base_url, redact(dsn), args.tenant,
                        stages, args.max_detail_rows,
                        args.include_never_analysed, args.all_products)
    wb.save(out)

    counted = (rows if args.include_never_analysed
               else [r for r in rows if not r.never_analysed])
    affected = [r for r in counted if r.has_gap]
    print("\n--- summary ---")
    print(f"  in scope                    : {len(counted):,}")
    if len(rows) != len(counted):
        print(f"  excluded (never analysed)   : {len(rows) - len(counted):,}"
              f"  (re-run the analysis)")
    print(f"  with at least one gap       : {len(affected):,}")
    print("\n  size chart:")
    counts: dict[str, int] = defaultdict(int)
    for r in counted:
        counts[r.chart_state] += 1
    for state, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"    {state:<48}: {n:,}")
    no_label = sum(1 for r in counted if not r.has_care_label)
    print(f"\n  care label missing          : {no_label:,}")
    print("\n  attributes missing:")
    for key, label in ATTRIBUTES:
        print(f"    {label:<48}: "
              f"{sum(1 for r in counted if r.missing_attr(key)):,}")
    print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
