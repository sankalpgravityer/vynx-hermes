#!/usr/bin/env python
"""Report products whose taxonomy is incomplete: no category, no sub-category,
or nothing at all.

Two sheets. **Summary** counts the gap and breaks it down by bucket, by
NULL-vs-empty-string and by tenant; **Products** is one row per affected
product, worst bucket first, with an autofilter so it can be sorted or sliced
in Excel.

Scoped BY DEFAULT to the two stages a reviewer works — REVIEW and APPROVED.

    # PRODUCTION, every tenant. Pass the connection string with --db.
    python scripts/audit_product_taxonomy.py \
        --db "postgresql://user:pass@prod-host:5432/vnyx" --all-tenants

    # One tenant, to a named file.
    python scripts/audit_product_taxonomy.py --db "postgresql://..." \
        --tenant 6045eee9-6b87-45f2-a582-2b47ea752c39 --out boas-taxonomy.xlsx

    # Widen it: every stage, including LABEL and the terminal ones.
    python scripts/audit_product_taxonomy.py --db "postgresql://..." \
        --all-tenants --all-stages

    The connection string can also come from DATABASE_URL. `--dsn` is an alias
    for `--db`.


SUGGESTING AND APPLYING — a three-step loop, with a human in the middle

    # 1. Ask for a suggested master / category / sub-category per product,
    #    chosen from THAT TENANT'S own category tree. Still writes nothing.
    python scripts/audit_product_taxonomy.py --db "..." --all-tenants --check

    # 2. Open the sheet. Correct the three "Suggested …" columns, or blank a
    #    row out to skip it. This is the review step and it is the point.

    # 3. Dry run the write, then do it.
    python scripts/audit_product_taxonomy.py --db "..." \
        --from-sheet reports/product-taxonomy-....xlsx
    python scripts/audit_product_taxonomy.py --db "..." \
        --from-sheet reports/product-taxonomy-....xlsx --apply


READING IS READ-ONLY AND SAFE TO POINT AT PRODUCTION.

Every path except the last one opens the connection READ ONLY, so Postgres —
not this file's good intentions — rejects a write. The single exception is
`--apply`, which needs `--from-sheet` as well: there is no way to go from a
fresh LLM answer to a database write without a person looking at a spreadsheet
in between. See `connect()` and `apply_mode()`.


WHY THIS MATTERS

`classifyGarmentType` reads CATEGORY and SUB-CATEGORY, never masterCategory:
masterCategory holds Men / Women / Kids / Unisex, which says who wears the item,
not what it is. A product with a masterCategory and nothing else therefore has
NO garment class at all, so on-model generation frames it with the fallback
"complete the outfit with plain complementary garments" and full-body framing —
a shoe, a bag and a pair of jeans all rendered as though they were a shirt.

That is the same failure the footwear and accessory classes were added to fix,
except here it cannot be fixed by better framing: the data to choose a framing
is not there. These products need a category before they need a render.


THE TRAP: MISSING IS NOT THE SAME AS NULL

On the production catalogue, `masterCategory` is NULL on 515 live products and
an EMPTY STRING on 7 more; category is NULL on 517 and empty on 14; subCategory
is NULL on 518 and empty on 30. A report that tests `IS NULL` alone misses 51
products, and every one of them behaves exactly like a null downstream —
`lowerTrim(category)` gives "" either way, and no keyword matches "".

So "missing" here means NULL **or** blank after trimming, and the two are
counted separately on the Summary so a data fix can tell them apart.
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

# `--check` imports app.config and app.llm.gemini, and this file lives in
# scripts/, so the repo root is not on the path when it is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg is required:  pip install 'psycopg[binary]'")

try:
    from openpyxl import Workbook, load_workbook
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

# The stages a reviewer actually works. From ProductStage in schema.prisma:
#   REVIEW    reviewStatus = PENDING   — waiting to be looked at
#   APPROVED  reviewStatus = ACCEPTED  — signed off
#
# Everything else is upstream of review (LABEL, DECISION, PHOTOBOOTH,
# MISSING_LABEL, IMPORTED) or terminal (REJECTED, DELETED, ARCHIVED). A product
# still at LABEL has not been categorised YET, which is not the same defect as
# one that reached review without a category — so mixing them makes the report
# unactionable. Override with --stage, or read everything with --all-stages.
DEFAULT_STAGES = ["REVIEW", "APPROVED"]

HDR_FILL = PatternFill("solid", fgColor="1F3864")
HDR_FONT = Font(bold=True, color="FFFFFF")
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
BAD_FILL = PatternFill("solid", fgColor="FDECEA")
WARN_FILL = PatternFill("solid", fgColor="FFF8E1")
OK_FILL = PatternFill("solid", fgColor="E8F5E9")


# --------------------------------------------------------------------------- #
# The buckets
#
# Ordered worst-first, which is also the order the sheets appear in. "Worst" is
# how little there is to work with, not how many products are in the bucket.
# --------------------------------------------------------------------------- #

BUCKET_ALL_THREE = "all_three_missing"
BUCKET_MASTER_ONLY = "master_only"
BUCKET_NO_MASTER = "master_missing_only"
BUCKET_NO_CATEGORY = "category_missing_only"
BUCKET_NO_SUBCATEGORY = "subcategory_missing_only"
BUCKET_COMPLETE = "complete"
BUCKET_NEVER_ANALYSED = "never_analysed"

BUCKET_LABEL = {
    BUCKET_ALL_THREE: "All three missing",
    BUCKET_MASTER_ONLY: "Master only — no category or sub-category",
    BUCKET_NO_MASTER: "Master missing, category present",
    BUCKET_NO_CATEGORY: "Category missing",
    BUCKET_NO_SUBCATEGORY: "Sub-category missing",
    BUCKET_COMPLETE: "Complete",
    BUCKET_NEVER_ANALYSED: "Never analysed (excluded)",
}

BUCKET_MEANING = {
    BUCKET_ALL_THREE: (
        "Nothing to classify on. No garment class, no gender, no mannequin — "
        "generation falls back to a generic full-body shot of an invented "
        "outfit. Needs categorising before it needs a render."
    ),
    BUCKET_MASTER_ONLY: (
        "Gender is known, the garment is not. classifyGarmentType reads "
        "category/subCategory only, so this gets no class at all and is framed "
        "as though it were a torso garment."
    ),
    BUCKET_NO_MASTER: (
        "The garment class resolves, but gender does not, so the model's "
        "gender falls back to the tenant default and may contradict the "
        "listing."
    ),
    BUCKET_NO_CATEGORY: (
        "Sub-category alone. Usually still classifiable, since "
        "classifyGarmentType checks both fields."
    ),
    BUCKET_NO_SUBCATEGORY: (
        "Category alone. Classifiable, but the narrower field is what "
        "distinguishes e.g. a denim jacket from a jacket."
    ),
    BUCKET_COMPLETE: "All three fields populated.",
    BUCKET_NEVER_ANALYSED: (
        "Placeholder title and empty properties — the analyze job never "
        "finished, so NOTHING was written. Not a categorisation defect. "
        "Excluded from the Products sheet; re-run the analysis."
    ),
}

# Which buckets are worth a detail sheet, in order.
DETAIL_BUCKETS = [
    BUCKET_ALL_THREE,
    BUCKET_MASTER_ONLY,
    BUCKET_NO_MASTER,
    BUCKET_NO_CATEGORY,
    BUCKET_NO_SUBCATEGORY,
]


@dataclass
class Row:
    product_id: str
    title: str | None
    master: str | None
    category: str | None
    sub_category: str | None
    review_status: str | None
    stage: str | None
    tenant_id: str | None
    tenant_name: str | None
    owner: str | None
    created_at: datetime | None
    product_code: str | None
    image_count: int
    properties: dict | None = None

    # Filled by classify()
    bucket: str = BUCKET_COMPLETE
    blank_not_null: str = ""

    # Filled by suggest() — only when --check is passed.
    suggested_master: str = ""
    suggested_category: str = ""
    suggested_sub: str = ""
    suggest_confidence: str = ""
    suggest_basis: str = ""
    off_tree: str = ""


# The title the API writes on creation and the analyze worker overwrites once
# it has a verdict. A product still carrying it never finished analysing.
PLACEHOLDER_TITLE = "generating product"


def never_analysed(r: "Row") -> bool:
    """Did analysis ever complete for this product?

    Worth separating, because it splits the report into two problems with two
    different fixes. On production, 50 of the 71 review-stage products with no
    taxonomy at all still carry the placeholder title — those are not products
    somebody forgot to categorise, they are analyze jobs that never finished,
    and re-running the analysis fills in all three fields at once. The other 21
    have a real title and genuinely need categorising.
    """
    return (r.title or "").strip().lower().startswith(PLACEHOLDER_TITLE)


def missing(value: str | None) -> bool:
    """NULL or blank after trimming.

    Both reach `lowerTrim(category)` as "" and match no keyword, so a report
    that separated them would describe a distinction the pipeline does not make.
    They ARE counted separately on the Summary, because a data fix does care.
    """
    return value is None or value.strip() == ""


def classify(r: Row) -> None:
    # Products that never finished analysing are EXCLUDED, not bucketed.
    #
    # They have no title and no properties, so nothing about them is a
    # categorisation defect — the analysis simply never ran and wrote nothing.
    # On the dev database they were 11 of 21 rows and on production 50 of 77,
    # so leaving them in buries the products somebody can actually act on under
    # a majority that needs a completely different fix. Counted on the Summary,
    # kept off the Products sheet; --include-never-analysed puts them back.
    if never_analysed(r) and not (r.properties or {}):
        r.bucket = BUCKET_NEVER_ANALYSED
        return
    _classify_fields(r)


def _classify_fields(r: Row) -> None:
    """Bucket a row on its three taxonomy fields alone.

    Split out so `--include-never-analysed` can re-bucket the excluded rows on
    what they actually contain instead of on the exclusion.
    """
    m, c, s = missing(r.master), missing(r.category), missing(r.sub_category)

    if m and c and s:
        r.bucket = BUCKET_ALL_THREE
    elif not m and c and s:
        r.bucket = BUCKET_MASTER_ONLY
    elif m:
        r.bucket = BUCKET_NO_MASTER
    elif c:
        r.bucket = BUCKET_NO_CATEGORY
    elif s:
        r.bucket = BUCKET_NO_SUBCATEGORY
    else:
        r.bucket = BUCKET_COMPLETE

    # Which of the missing fields are an empty string rather than NULL. Worth
    # naming per row: an empty string usually means something WROTE it, so the
    # two have different causes and different fixes.
    blanks = [
        name
        for name, value in (
            ("master", r.master), ("category", r.category),
            ("subCategory", r.sub_category),
        )
        if value is not None and value.strip() == ""
    ]
    r.blank_not_null = ", ".join(blanks)


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
        application_name="hermes-taxonomy-audit (read-only)",
        options=f"-c statement_timeout={int(statement_timeout_s) * 1000}",
    )
    conn.read_only = True
    return conn


SQL = """
SELECT p.id::text,
       p.title,
       p."masterCategory",
       p.category,
       p."subCategory",
       p."reviewStatus"::text,
       p."currentStage"::text,
       p."tenantId"::text,
       t.name,
       COALESCE(NULLIF(u.name, ''), u.email),
       p."createdAt",
       p."productCode",
       COALESCE(array_length(p.images, 1), 0),
       p.properties
FROM "Product" p
LEFT JOIN "Tenant" t ON t.id = p."tenantId"
LEFT JOIN "User"   u ON u.id = p."createdById"
WHERE p."isDeleted" = false
  {tenant_clause}
  {stage_clause}
ORDER BY p."createdAt" DESC
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

    for r in rows:
        classify(r)
    if progress:
        print(f"  {len(rows):,} live product(s)", flush=True)
    return rows


# --------------------------------------------------------------------------- #
# Suggesting  (--check)
#
# The values are a CHOICE, not free text: every tenant keeps its own three-level
# `Category` tree, and `Product.category` / `subCategory` hold the NAME of a node
# in it. Measured on BOAS, 6,305 of 6,305 categorised products name a real node.
# So a suggestion that is not in the tenant's tree is not a suggestion, it is a
# typo waiting to be pasted in — every answer is validated against the tree and
# discarded if it does not match.
#
# This is also why the tree is read per tenant rather than hardcoded here: the
# tenants do not share one taxonomy (BOAS has 3 roots and 103 leaves, Bleckmann
# has 4 and 61), and a table baked into this file would be wrong for most of
# them the day someone edits their categories.
# --------------------------------------------------------------------------- #

TAXONOMY_SQL = """
SELECT c."tenantId"::text, g.name, p.name, c.name
FROM "Category" c
JOIN "Category" p ON p.id = c."parentId"
JOIN "Category" g ON g.id = p."parentId"
WHERE c."isActive" AND c."deletedAt" IS NULL
  AND p."isActive" AND p."deletedAt" IS NULL
  AND g."isActive" AND g."deletedAt" IS NULL
ORDER BY 2, 3, 4
"""


def fetch_taxonomy(dsn: str, statement_timeout_s: int
                   ) -> dict[str, list[tuple[str, str, str]]]:
    """Every valid (master, category, sub) triple, per tenant."""
    out: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
        cur.execute(TAXONOMY_SQL)
        for tenant_id, master, category, sub in cur.fetchall():
            out[tenant_id].append((master, category, sub))
    return out


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "master": {"type": "string"},
        "category": {"type": "string"},
        "sub_category": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "basis": {"type": "string"},
    },
    "required": ["master", "category", "sub_category", "confidence", "basis"],
}

SUGGEST_SYSTEM = (
    "You categorise second-hand clothing for a resale catalogue. You are given "
    "one product's known details and the COMPLETE list of category paths that "
    "this shop allows. Choose exactly one path from that list.\n\n"
    "Rules:\n"
    "- The three values you return MUST be copied verbatim from one single line "
    "of the allowed list. Never invent a value, never mix levels from two "
    "different lines, never correct the shop's spelling.\n"
    "- Fields the product already has are correct UNLESS the notes say the "
    "value is not one of this shop's choices. Treat the valid ones as fixed "
    "and choose a path consistent with them; REPLACE the invalid ones.\n"
    "- `basis` is a short phrase naming the evidence you used, e.g. "
    "\"title says Joggers; properties.gender = men\".\n"
    "- If the details genuinely do not identify a garment, return empty strings "
    "for all three and confidence \"low\". Guessing is worse than abstaining: a "
    "wrong category is pasted into a live catalogue."
)


def off_tree_fields(r: Row, allowed: list[tuple[str, str, str]]) -> list[str]:
    """Which of the product's EXISTING values are not choices for this tenant.

    A separate defect from a missing value, and a more confusing one: the field
    looks populated, so nothing flags it, but it names a category the shop does
    not have. On the dev database three Bleckmann products sit at
    `category = "Shirts"` while that tenant's depth-2 nodes under Men are
    Bottoms, Caps, Jackets, Shoes, Suits, Sweaters & Hoodies, Swimwear and
    T-Shirts & Polos — "Shirts" is not among them.

    It also changes what the model should be told. Left to "existing fields are
    correct, keep them", it abstains on every one of these, because no path can
    satisfy a value that is not in the list.
    """
    masters = {m for m, _, _ in allowed}
    pairs = {(m, c) for m, c, _ in allowed}
    cats = {c for _, c, _ in allowed}
    subs = {s for _, _, s in allowed}

    def val(v: str | None) -> str:
        return (v or "").strip()

    master, category, sub = val(r.master), val(r.category), val(r.sub_category)
    out = []

    if master and master not in masters:
        out.append("masterCategory")

    # The PAIR, not just the name. "Active wear" is a real node in Bleckmann's
    # tree — under Kids and Unisex — so a flat name check passes a product
    # sitting at `Women > Active wear`, which is not a path that shop has. The
    # pair is only meaningful when the master itself is valid; otherwise fall
    # back to the name, since there is nothing to pair against.
    if category:
        ok = ((master, category) in pairs
              if master and master in masters
              else category in cats)
        if not ok:
            out.append("category")

    # Same idea one level down: the full triple when both parents are good.
    if sub:
        ok = (
            (master, category, sub) in allowed
            if master in masters and (master, category) in pairs
            else sub in subs
        )
        if not ok:
            out.append("subCategory")

    return out


def _evidence(r: Row, off_tree: list[str]) -> str:
    """What is actually known about this product, as plain text."""
    props = r.properties if isinstance(r.properties, dict) else {}
    known = [f"title: {r.title or '(none)'}"]
    for key in ("Brand", "brand", "color", "colour", "gender", "material",
                "fit", "model", "size", "international_size"):
        value = props.get(key)
        if value in (None, "", "Unknown", "unknown", []):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        known.append(f"{key}: {value}")
    for field, label, value in (("masterCategory", "existing masterCategory", r.master),
                                ("category", "existing category", r.category),
                                ("subCategory", "existing subCategory", r.sub_category)):
        if not (value and value.strip()):
            continue
        if field in off_tree:
            known.append(
                f"{label}: {value}  <-- NOT one of this shop's choices. It is "
                f"wrong; REPLACE it with the closest allowed path."
            )
        else:
            known.append(f"{label} (ALREADY SET, keep it): {value}")
    return "\n".join(known)


def suggest(rows: list[Row], taxonomy: dict[str, list[tuple[str, str, str]]],
            pol: dict, api_key: str, progress: bool) -> None:
    """Fill the suggestion columns in place. Best-effort, never raises."""
    from app.llm.gemini import GeminiEvidence

    client = GeminiEvidence(api_key, pol)
    model = pol["llm"]["model_fast"]

    for n, r in enumerate(rows, 1):
        allowed = taxonomy.get(r.tenant_id or "", [])
        if not allowed:
            r.suggest_basis = "this tenant has no category tree configured"
            continue

        # Still guarded, because --include-never-analysed can put these back.
        # Asking with no title and no properties produces a confident guess off
        # the tenant's most common path, which is worse than an honest blank:
        # it looks like an answer.
        if never_analysed(r) and not (r.properties or {}):
            r.suggest_basis = ("no evidence — never analysed, so there is no "
                               "title and no properties to reason from")
            r.suggest_confidence = "none"
            continue

        off = off_tree_fields(r, allowed)
        r.off_tree = ", ".join(off)
        listing = "\n".join(f"{m} > {c} > {s}" for m, c, s in allowed)
        prompt = (
            f"ALLOWED CATEGORY PATHS for this shop:\n{listing}\n\n"
            f"PRODUCT:\n{_evidence(r, off)}"
        )
        got = client._generate(model, prompt, SUGGEST_SCHEMA, SUGGEST_SYSTEM)
        if not got:
            r.suggest_basis = "the model did not answer"
            continue

        triple = (str(got.get("master", "")).strip(),
                  str(got.get("category", "")).strip(),
                  str(got.get("sub_category", "")).strip())
        if not any(triple):
            r.suggest_confidence = got.get("confidence", "low")
            r.suggest_basis = got.get("basis", "") or "abstained"
        elif triple in allowed:
            r.suggested_master, r.suggested_category, r.suggested_sub = triple
            r.suggest_confidence = got.get("confidence", "")
            r.suggest_basis = got.get("basis", "")
        else:
            # Not in the tree. Reported, never written: the whole point of
            # reading the tenant's own taxonomy is that only its values are
            # safe to paste back in.
            r.suggest_confidence = "rejected"
            r.suggest_basis = (
                f"model returned {' > '.join(triple)}, which is not a path in "
                f"this tenant's category tree"
            )

        if progress and n % 10 == 0:
            print(f"  suggested {n}/{len(rows)}…", flush=True)



# --------------------------------------------------------------------------- #
# Applying  (--apply --from-sheet)
#
# THE ONLY WRITE IN THIS FILE, and it is deliberately awkward to reach: it needs
# --apply AND a sheet that a person has already looked at. Applying straight
# from a fresh --check run is not offered, because then nothing would ever have
# been reviewed and the LLM would be writing to the catalogue unsupervised.
#
# The sheet is the interface. Edit the three "Suggested …" columns in Excel —
# correct them, blank them out to skip a row — and this writes exactly what is
# in the file. It is not re-generated, re-asked or second-guessed here.
#
# Four things are checked before any row is written:
#
#   1. The triple is a real path in THAT TENANT'S tree, re-read live. A human
#      typing into a spreadsheet is exactly as capable of inventing a category
#      as a language model is.
#   2. The product's CURRENT values still match what the sheet recorded. If
#      someone categorised it in the meantime, the sheet is stale and this skips
#      rather than overwriting their work.
#   3. There is something to change. A row whose suggestion equals what is
#      already there is a no-op, not a write.
#   4. `categoryId` is resolved to the depth-2 node for the chosen path. On the
#      dev database it points at the category node on 9,919 of 10,067 products,
#      so writing the three name strings alone would leave the FK disagreeing
#      with them.
# --------------------------------------------------------------------------- #

UPDATE_SQL = """
UPDATE "Product"
   SET "masterCategory" = %(master)s,
       category         = %(category)s,
       "subCategory"    = %(sub)s,
       "categoryId"     = %(category_id)s,
       "updatedAt"      = now()
 WHERE id = %(id)s::uuid
   AND "isDeleted" = false
   AND COALESCE(btrim("masterCategory"), '') = %(was_master)s
   AND COALESCE(btrim(category), '')         = %(was_category)s
   AND COALESCE(btrim("subCategory"), '')    = %(was_sub)s
"""


def connect_writable(dsn: str, statement_timeout_s: int):
    """A normal connection. Named so the difference from `connect()` is loud.

    `connect()` opens READ ONLY and every other code path in this file uses it;
    this is the one exception, reached only from the apply path.
    """
    conn = psycopg.connect(
        dsn,
        connect_timeout=30,
        application_name="hermes-taxonomy-apply (WRITES)",
        options=f"-c statement_timeout={int(statement_timeout_s) * 1000}",
    )
    return conn


def read_sheet(path: Path) -> list[dict]:
    """The reviewed sheet, back as rows. Columns are found by NAME.

    By name and not by index, because the whole point of the sheet is that a
    person opens it in Excel — and people insert columns, hide them and reorder
    them. A positional read would silently write the wrong field.
    """
    wb = load_workbook(path, data_only=True)
    if "Products" not in wb.sheetnames:
        raise SystemExit(f"{path} has no 'Products' sheet — is it the right file?")
    ws = wb["Products"]
    header = [(c.value or "").strip() if isinstance(c.value, str) else ""
              for c in ws[1]]
    need = ["Product ID", "Master", "Category", "Sub-category",
            "Suggested master", "Suggested category", "Suggested sub-category"]
    missing_cols = [h for h in need if h not in header]
    if missing_cols:
        raise SystemExit(
            f"{path} is missing the column(s) {missing_cols}. Produce the sheet "
            f"with --check first."
        )
    idx = {h: header.index(h) for h in need}
    idx.update({h: header.index(h) for h in ("Tenant", "Tenant ID", "Title")
                if h in header})

    def cell(row, name):
        i = idx.get(name)
        if i is None or i >= len(row):
            return ""
        v = row[i]
        return str(v).strip() if v is not None else ""

    out = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        pid = cell(row, "Product ID")
        if not pid:
            continue
        out.append({
            "id": pid,
            "title": cell(row, "Title"),
            "tenant": cell(row, "Tenant"),
            "tenant_id": cell(row, "Tenant ID"),
            "was": (cell(row, "Master"), cell(row, "Category"),
                    cell(row, "Sub-category")),
            "to": (cell(row, "Suggested master"),
                   cell(row, "Suggested category"),
                   cell(row, "Suggested sub-category")),
        })
    return out


def category_node_ids(dsn: str, statement_timeout_s: int
                      ) -> dict[tuple[str, str, str], str]:
    """(tenantId, master, category) -> the depth-2 Category row's id."""
    out: dict[tuple[str, str, str], str] = {}
    sql = """
        SELECT p."tenantId"::text, g.name, p.name, p.id::text
        FROM "Category" p
        JOIN "Category" g ON g.id = p."parentId"
        WHERE p."isActive" AND p."deletedAt" IS NULL
          AND g."isActive" AND g."deletedAt" IS NULL
    """
    with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
        cur.execute(sql)
        for tenant_id, master, category, node_id in cur.fetchall():
            out[(tenant_id, master, category)] = node_id
    return out


def apply_mode(dsn: str, sheet: Path, do_write: bool,
               statement_timeout_s: int) -> int:
    rows = read_sheet(sheet)
    if not rows:
        return print(f"No product rows in {sheet}.") or 1

    taxonomy = fetch_taxonomy(dsn, statement_timeout_s)
    node_ids = category_node_ids(dsn, statement_timeout_s)

    # The CURRENT values, read now. The UPDATE's WHERE clause is what actually
    # prevents overwriting someone else's edit, but a dry run that cannot see
    # it would print "would write" for a row the real run then refuses — and a
    # dry run you have to second-guess is worse than none. So the same check is
    # made here, against live data, and the SQL guard stays as the race-proof
    # backstop for the seconds between the two.
    current: dict[str, tuple[str, str, str]] = {}
    with connect(dsn, statement_timeout_s) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT id::text, COALESCE(btrim("masterCategory"), \'\'), '
            "COALESCE(btrim(category), ''), "
            'COALESCE(btrim("subCategory"), \'\') '
            'FROM "Product" WHERE id = ANY(%s::uuid[]) AND "isDeleted" = false',
            ([r["id"] for r in rows],),
        )
        for pid, m, c, s in cur.fetchall():
            current[pid] = (m, c, s)

    planned: list[tuple[dict, str]] = []
    skipped: list[tuple[dict, str]] = []
    for r in rows:
        live = current.get(r["id"])
        if live is None:
            skipped.append((r, "no such live product (deleted since?)"))
            continue
        if live != tuple(x if x != "-" else "" for x in r["was"]):
            skipped.append((r, (
                f"changed since the sheet was made — now "
                f"{' / '.join(x or '-' for x in live)}; re-run --check"
            )))
            continue
        master, category, sub = r["to"]
        if not (master or category or sub):
            skipped.append((r, "no suggestion in the sheet"))
            continue
        if not (master and category and sub):
            skipped.append((r, "suggestion is only partly filled in — all "
                               "three are needed"))
            continue
        # BEFORE the tree check, deliberately. A row whose suggestion equals
        # what is already stored is a no-op, and validating a value nobody is
        # going to write only produces a misleading complaint: a product
        # already sitting on an off-tree path would be reported as "the
        # suggestion is not a path", which blames the wrong thing.
        if r["to"] == r["was"]:
            skipped.append((r, "already set to this"))
            continue
        allowed = taxonomy.get(r["tenant_id"], [])
        if not allowed:
            skipped.append((r, "no category tree for this tenant"))
            continue
        if (master, category, sub) not in allowed:
            skipped.append((r, f"{master} > {category} > {sub} is not a path "
                               f"in this tenant's tree"))
            continue
        node = node_ids.get((r["tenant_id"], master, category))
        if not node:
            skipped.append((r, f"no Category row for {master} > {category}"))
            continue
        planned.append((r, node))

    print(f"\nSheet     : {sheet}")
    print(f"Rows      : {len(rows)}")
    print(f"To write  : {len(planned)}")
    print(f"Skipped   : {len(skipped)}")
    print(f"Mode      : {'APPLY — WRITES TO THE DATABASE' if do_write else 'DRY RUN'}\n")

    for r, _ in planned:
        was = " / ".join(x or "-" for x in r["was"])
        to = " / ".join(r["to"])
        print(f"  {'WRITE' if do_write else 'would write'}  {r['id']}  "
              f"[{r['tenant']}]")
        print(f"      {was}   ->   {to}")
    for r, why in skipped:
        print(f"  skip   {r['id']}  — {why}")

    if not do_write:
        print("\nNothing was written. Re-run with --apply to do it.")
        return 0
    if not planned:
        print("\nNothing to write.")
        return 0

    written, stale = 0, []
    with connect_writable(dsn, statement_timeout_s) as conn:
        with conn.cursor() as cur:
            for r, node in planned:
                master, category, sub = r["to"]
                cur.execute(UPDATE_SQL, {
                    "id": r["id"], "master": master, "category": category,
                    "sub": sub, "category_id": node,
                    "was_master": r["was"][0] if r["was"][0] != "-" else "",
                    "was_category": r["was"][1] if r["was"][1] != "-" else "",
                    "was_sub": r["was"][2] if r["was"][2] != "-" else "",
                })
                if cur.rowcount == 1:
                    written += 1
                else:
                    # The guard in the WHERE clause did not match: the product
                    # changed since the sheet was produced. Reported, not
                    # forced — someone else's edit is not this script's to undo.
                    stale.append(r)
        conn.commit()

    print(f"\n--- applied ---")
    print(f"  written        : {written}")
    print(f"  changed since  : {len(stale)}  (the sheet is stale for these; "
          f"re-run --check)")
    for r in stale:
        print(f"    {r['id']}  [{r['tenant']}]")
    return 0


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

    The tab is a filter over the product's CURRENT stage, so a stale one hides
    the product exactly as effectively as a wrong tenant does — and the stage
    has its own column here. The tenantId is the part that must be right:
    product search is tenant-scoped, so opening one tenant's product under
    another's id returns "no result found", which reads as a missing product
    rather than a wrong link.
    """
    if not tenant_id:
        return ""
    return f"{base.rstrip('/')}/product/{product_id}/edit?tenantId={tenant_id}"


def redact(dsn: str) -> str:
    m = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    if not m:
        return "(dsn not shown)"
    return f"{m.group(1)}://{m.group(2)}@{m.group(3)}/{m.group(4)}"


BASE_HEADERS = [
    "Issue", "Analysed?", "Product ID", "Product code", "Title", "Master",
    "Category", "Sub-category", "Blank (not null)", "Review status", "Stage",
    "Images", "Tenant", "Tenant ID", "Account owner", "Created", "Edit URL",
]
# Appended by --check. Placed AFTER the existing values, not beside them, so a
# reader compares "what it has" with "what is proposed" rather than losing
# track of which column is which.
SUGGEST_HEADERS = [
    "Suggested master", "Suggested category", "Suggested sub-category",
    "Confidence", "Based on", "Existing value off-tree",
]
DETAIL_WIDTHS = {
    1: 40, 2: 26, 3: 38, 4: 14, 5: 46, 6: 12, 7: 20, 8: 20, 9: 18, 10: 16,
    11: 12, 12: 8, 13: 20, 14: 38, 15: 30, 16: 12, 17: 74,
    18: 18, 19: 22, 20: 24, 21: 12, 22: 60, 23: 22,
}


def _products_sheet(wb, rows: list[Row], app_base: str, cap: int,
                    checked: bool) -> None:
    """ONE sheet for every incomplete product, worst bucket first.

    One sheet rather than one per bucket: the buckets are a property of the
    row, so `Issue` carries them and the reader can filter or sort on it in
    Excel. Five near-empty tabs is worse than one list you can sort.
    """
    ws = wb.create_sheet("Products")
    headers = BASE_HEADERS + (SUGGEST_HEADERS if checked else [])
    _header(ws, headers)

    order = {b: i for i, b in enumerate(DETAIL_BUCKETS)}
    ordered = sorted(
        rows,
        key=lambda r: (order.get(r.bucket, 99),
                       r.tenant_name or "", r.created_at or datetime.min),
    )

    for r in ordered[:cap]:
        ws.append([
            BUCKET_LABEL[r.bucket],
            "NO — never analysed" if never_analysed(r) else "yes",
            r.product_id,
            r.product_code or "",
            r.title or "",
            r.master or "",
            r.category or "",
            r.sub_category or "",
            r.blank_not_null,
            r.review_status or "",
            r.stage or "",
            r.image_count,
            r.tenant_name or "",
            r.tenant_id or "",
            r.owner or "",
            r.created_at.date().isoformat() if r.created_at else "",
            edit_url(app_base, r.product_id, r.tenant_id),
            *([r.suggested_master, r.suggested_category, r.suggested_sub,
               r.suggest_confidence, r.suggest_basis, r.off_tree]
              if checked else []),
        ])
        fill = (BAD_FILL if r.bucket in (BUCKET_ALL_THREE, BUCKET_MASTER_ONLY)
                else WARN_FILL)
        ws.cell(row=ws.max_row, column=1).fill = fill
        if never_analysed(r):
            ws.cell(row=ws.max_row, column=2).fill = WARN_FILL
        if checked:
            # Green only where there is something to paste in. An abstention
            # and a rejected answer must not look like an answer.
            first = len(BASE_HEADERS) + 1
            got = bool(r.suggested_master or r.suggested_category
                       or r.suggested_sub)
            for col in range(first, first + 3):
                ws.cell(row=ws.max_row, column=col).fill = (
                    OK_FILL if got else WARN_FILL
                )

    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)
    if len(ordered) > cap:
        ws.append([])
        ws.append([f"… {len(ordered) - cap:,} more row(s) not listed "
                   f"(--max-detail-rows {cap})."])
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}" \
                         f"{min(len(ordered), cap) + 1}"
    _autosize(ws, DETAIL_WIDTHS)


def build_workbook(rows: list[Row], app_base: str, dsn_label: str,
                   tenant: str | None, stages: list[str] | None,
                   cap: int, checked: bool = False) -> Workbook:
    by_bucket: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        by_bucket[r.bucket].append(r)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"

    def section(label: str) -> None:
        ws.append([])
        ws.append([label])
        cell = ws.cell(row=ws.max_row, column=1)
        cell.font = Font(bold=True, size=12)
        cell.fill = SECTION_FILL

    ws.append(["Product taxonomy audit"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws.append(["Generated", datetime.now(timezone.utc)
               .strftime("%Y-%m-%d %H:%M UTC")])
    ws.append(["Database", dsn_label])
    ws.append(["Tenant", tenant or "every tenant"])
    ws.append(["Stage", ", ".join(stages) if stages else "every stage"])
    ws.append(["Products in scope", len(rows)])
    ws.append([
        "Missing means",
        "NULL or blank after trimming — both reach the pipeline as \"\" and "
        "match no keyword.",
    ])

    # ---- the buckets --------------------------------------------------------
    section("By completeness  (worst first)")
    ws.append(["Bucket", "Products", "% of live", "What it means"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    total = len(rows) or 1
    for bucket in DETAIL_BUCKETS + [BUCKET_COMPLETE]:
        n = len(by_bucket.get(bucket, []))
        ws.append([
            BUCKET_LABEL[bucket], n, f"{100 * n / total:.1f}%",
            BUCKET_MEANING[bucket],
        ])
        fill = (OK_FILL if bucket == BUCKET_COMPLETE
                else BAD_FILL if bucket in (BUCKET_ALL_THREE, BUCKET_MASTER_ONLY)
                else WARN_FILL)
        for col in range(1, 5):
            ws.cell(row=ws.max_row, column=col).fill = fill
    ws.append([
        "TOTAL INCOMPLETE",
        sum(len(by_bucket.get(b, [])) for b in DETAIL_BUCKETS),
    ])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    ws.cell(row=ws.max_row, column=2).font = Font(bold=True)

    # ---- what was left out --------------------------------------------------
    # The rows this report is actually about: a real taxonomy defect. NOT
    # "everything that is not complete" — the never-analysed bucket is neither.
    incomplete_rows = [r for r in rows if r.bucket in DETAIL_BUCKETS]
    excluded = [r for r in rows if r.bucket == BUCKET_NEVER_ANALYSED]
    if excluded:
        section("Excluded from the Products sheet")
        ws.append(["Reason", "Products", "What to do"])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        ws.append([
            "Never analysed — placeholder title, empty properties",
            len(excluded),
            "Not a categorisation problem: the analyze job never finished, so "
            "no title and no taxonomy were ever written. Re-run the analysis "
            "and all three fields are filled at once. Pass "
            "--include-never-analysed to list them anyway.",
        ])
        for col in range(1, 4):
            ws.cell(row=ws.max_row, column=col).fill = WARN_FILL

    # ---- what --check produced ---------------------------------------------
    if checked:
        section("Suggested categories  (--check)")
        ws.append(["Outcome", "Products", "Meaning"])
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
        got = [r for r in incomplete_rows
               if r.suggested_master or r.suggested_category or r.suggested_sub]
        rejected = [r for r in incomplete_rows
                    if r.suggest_confidence == "rejected"]
        none_ev = [r for r in incomplete_rows if r.suggest_confidence == "none"]
        ws.append(["Suggested", len(got),
                   "A valid path from this tenant's own category tree. Check "
                   "it, then paste it in."])
        ws.cell(row=ws.max_row, column=1).fill = OK_FILL
        ws.append(["No evidence to work from", len(none_ev),
                   "Never analysed: no title and no properties. Re-run the "
                   "analysis rather than guessing."])
        ws.cell(row=ws.max_row, column=1).fill = WARN_FILL
        off = [r for r in incomplete_rows if r.off_tree]
        if off:
            ws.append(["Existing value is not a choice", len(off),
                       "A field that LOOKS populated but names a category this "
                       "tenant does not have. Nothing flags these today. See "
                       "the \"Existing value off-tree\" column."])
            ws.cell(row=ws.max_row, column=1).fill = BAD_FILL
        ws.append(["Model answered off-tree", len(rejected),
                   "The model named a path this tenant does not have. "
                   "Discarded, never written — see \"Based on\"."])
        ws.cell(row=ws.max_row, column=1).fill = BAD_FILL
        by_conf: dict[str, int] = defaultdict(int)
        for r in got:
            by_conf[r.suggest_confidence or "(none)"] += 1
        if by_conf:
            ws.append([])
            ws.append(["Confidence of the suggestions"])
            ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
            for level in ("high", "medium", "low"):
                if by_conf.get(level):
                    ws.append([level, by_conf[level]])
        ws.append([])
        ws.append(["Suggestions are a PROPOSAL. Nothing is written to the "
                   "database — this script has no --apply."])

    # ---- NULL vs empty string ----------------------------------------------
    section("NULL vs empty string  (per field, across every live product)")
    ws.append(["Field", "NULL", "Empty string", "Populated"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
    for name, get in (
        ("masterCategory", lambda r: r.master),
        ("category", lambda r: r.category),
        ("subCategory", lambda r: r.sub_category),
    ):
        nulls = sum(1 for r in rows if get(r) is None)
        blanks = sum(1 for r in rows
                     if get(r) is not None and get(r).strip() == "")
        ws.append([name, nulls, blanks, len(rows) - nulls - blanks])
    ws.append([])
    ws.append([
        "An empty string usually means something WROTE it, so it has a "
        "different cause from a field that was never set."
    ])

    # ---- per tenant ---------------------------------------------------------
    section("Incomplete by tenant")
    ws.append(["Tenant", "Tenant ID", "All three missing", "Master only",
               "Other gaps", "Incomplete", "Live", "% incomplete"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)

    per: dict[tuple, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        key = (r.tenant_name or "(unknown)", r.tenant_id or "")
        per[key]["live"] += 1
        if r.bucket != BUCKET_COMPLETE:
            per[key]["incomplete"] += 1
            per[key][r.bucket] += 1
    for (name, tid), c in sorted(per.items(),
                                 key=lambda kv: -kv[1]["incomplete"]):
        other = c["incomplete"] - c[BUCKET_ALL_THREE] - c[BUCKET_MASTER_ONLY]
        ws.append([
            name, tid, c[BUCKET_ALL_THREE], c[BUCKET_MASTER_ONLY], other,
            c["incomplete"], c["live"],
            f"{100 * c['incomplete'] / (c['live'] or 1):.1f}%",
        ])

    # ---- what the gap costs -------------------------------------------------
    section("Why it matters")
    for line in (
        "classifyGarmentType() reads category and subCategory only. "
        "masterCategory says WHO wears the item (Men / Women / Kids / Unisex), "
        "not WHAT it is.",
        "With neither field set there is no garment class, so on-model "
        "generation uses the fallback framing: a full-body shot with a generic "
        "invented outfit. A bag, a shoe and a pair of jeans all come out framed "
        "as though they were a shirt.",
        "Better framing cannot fix these — the data needed to choose a framing "
        "is not there. They need categorising first.",
    ):
        ws.append([line])

    _autosize(ws, {1: 42, 2: 40, 3: 20, 4: 16, 5: 14, 6: 14, 7: 12, 8: 14})

    # ---- the one detail sheet ----------------------------------------------
    incomplete = [r for r in rows if r.bucket in DETAIL_BUCKETS]
    _products_sheet(wb, incomplete, app_base, cap, checked)
    return wb


def main() -> int:
    p = argparse.ArgumentParser(
        description="Report products with an incomplete category taxonomy. "
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
                   help="Cap rows per detail sheet (default 100,000). A cap "
                        "that fires is stated on the sheet; the Summary is "
                        "always computed over every product.")
    p.add_argument("--statement-timeout", type=int, default=300,
                   metavar="SECONDS",
                   help="Per-query timeout (default 300).")
    p.add_argument("--app-base-url", default=DEFAULT_APP_BASE,
                   help=f"Front-end origin for the Edit URL column "
                        f"(default {DEFAULT_APP_BASE}).")
    p.add_argument("--from-sheet", default=None, metavar="XLSX",
                   help="A sheet produced by --check, after you have reviewed "
                        "it. Switches to apply mode: the three 'Suggested …' "
                        "columns are read back and written to the database. "
                        "Without --apply it is a dry run.")
    p.add_argument("--apply", action="store_true",
                   help="With --from-sheet, ACTUALLY write. This is the only "
                        "write in this script.")
    p.add_argument("--include-never-analysed", action="store_true",
                   help="Also list products whose analysis never finished "
                        "(placeholder title, empty properties). Excluded by "
                        "default — they need a re-run, not a category.")
    p.add_argument("--check", action="store_true",
                   help="Also SUGGEST a master / category / sub-category for "
                        "each incomplete product, chosen from that tenant's own "
                        "category tree. Adds five columns. Costs one LLM call "
                        "per product and needs GEMINI_API_KEY.")
    p.add_argument("--quiet", action="store_true", help="No progress output.")
    args = p.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL")
    if not dsn:
        return print("No database. Pass --db or set DATABASE_URL.") or 2
    # Apply mode is a different program: it reads a reviewed sheet and
    # writes, rather than reading the catalogue and reporting.
    if args.from_sheet:
        return apply_mode(dsn, Path(args.from_sheet).expanduser(), args.apply,
                          args.statement_timeout)
    if args.apply:
        return print("--apply only makes sense with --from-sheet. Produce a "
                     "sheet with --check, review it, then apply it.") or 2

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

    if args.include_never_analysed:
        # Re-bucket them on their actual fields rather than the exclusion.
        for r in rows:
            if r.bucket == BUCKET_NEVER_ANALYSED:
                r.bucket = BUCKET_COMPLETE
                _classify_fields(r)

    if args.check:
        from app.config import policy

        api_key = os.getenv("GEMINI_API_KEY", "")
        if not api_key:
            return print("--check needs GEMINI_API_KEY (put it in .env).") or 2
        incomplete = [r for r in rows if r.bucket in DETAIL_BUCKETS]
        if progress:
            print(f"\nSuggesting categories for {len(incomplete)} product(s)…",
                  flush=True)
        taxonomy = fetch_taxonomy(dsn, args.statement_timeout)
        if progress:
            paths = sum(len(v) for v in taxonomy.values())
            print(f"  {paths:,} category path(s) across {len(taxonomy)} tenant(s)",
                  flush=True)
        suggest(incomplete, taxonomy, policy(), api_key, progress)

    out = Path(args.out) if args.out else Path(
        f"reports/product-taxonomy-{datetime.now():%Y-%m-%d-%H%M}.xlsx"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    wb = build_workbook(rows, args.app_base_url, redact(dsn), args.tenant,
                        stages, args.max_detail_rows, args.check)
    wb.save(out)

    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.bucket] += 1
    print("\n--- summary ---")
    for bucket in DETAIL_BUCKETS:
        print(f"  {BUCKET_LABEL[bucket]:<44}: {counts[bucket]:,}")
    print(f"  {'Complete':<44}: {counts[BUCKET_COMPLETE]:,}")
    if counts[BUCKET_NEVER_ANALYSED]:
        print(f"\n  excluded — never analysed          : "
              f"{counts[BUCKET_NEVER_ANALYSED]:,}  "
              f"(re-run the analysis; --include-never-analysed to list them)")
    print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
