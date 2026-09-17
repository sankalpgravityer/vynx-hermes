"""One product id in — every check, the repairs that can be applied, and a sheet.

    POST /v1/product-audit  {"product_id": "...", "apply": true}

Reads Postgres directly with `DATABASE_URL`, assembles the same payload
`GET /review-verification/feed` would have produced, runs the full rule set plus
the approval-gate rules, applies the repairs that are safe to apply from here,
and writes an .xlsx saying what was wrong and what changed.


WHY THE PAYLOAD IS REBUILT HERE RATHER THAN FETCHED

vnyx-api's `buildReviewFeed` is the reference implementation and this mirrors it
field for field — the same `PROPERTY_ALIASES` precedence, the same grade join,
the same price parsing. It is duplicated instead of called because the whole
point of this endpoint is that it needs nothing but a connection string: no
running API, no JWT, no tenant scoping to satisfy.

Three things it adds that the feed does NOT carry, and which are the reason the
imagery and size-chart rules have been silent until now:

  media[]              the live `ProductMedia` rows, typed — plus, flagged
                       `isCurrent: false`, the superseded RAW originals a
                       cut-out was made from (readiness phase 3: the canvas
                       check compares the two). `imagery.check_imagery` is a
                       no-op without them, and the feed sends only a count off
                       the legacy `Product.images` array.
  size-chart facts     `sizingGuideId` -> `ProductSize.sizeChartImages`, plus the
                       SIZE_CHART rows actually copied onto the product. Five
                       distinct states hide behind "no chart", and who fixes it
                       differs per state.
  careLabelCount       from `ProductMedia view='LABEL'`, not `careLabelImages`.
                       IMG.030 blocks approval on this number, so the two sources
                       disagreeing is a correctness problem rather than a tidiness
                       one. The legacy figure is kept beside it as
                       `legacyCareLabelCount` so a disagreement is visible.


WHAT IT WILL AND WILL NOT WRITE

Applied, in one transaction:

  set_property   `properties = properties || <patch>::jsonb`. A server-side jsonb
                 merge, so there is no read-merge-write race on the column that
                 holds most of a product's attributes — which is the reason
                 `vnyx_client.patch_product` refuses these outright.
  set_column     the plain text columns in `_WRITABLE_COLUMNS`. Nothing with a
                 relation, an enum, or a mirror behind it.

Refused, and reported instead:

  price          `updateProduct` does not merely write the column. It re-parses
                 the money string, mirrors the figure into `ProductVariant`,
                 upserts `Inventory`, then upserts a `Price` row per CONNECTED
                 marketplace account — each one the base run through THAT
                 account's ordered price-rule chain and its terminal rounding.
                 This tenant has two connected accounts and one of them is not
                 Shopify, so it takes the rules path. A Python copy of that would
                 drift from `services/product-variants.ts` within a month, and a
                 partial copy leaves the mirror stale, which is precisely the
                 drift PRICE.110 exists to catch. The corrected figure IS computed
                 and reported; apply it with
                 `POST /review-verification/products/:id/verify`.
  create_size_chart / generate_images
                 need R2 credentials and an image model. Reported with the seed
                 or the view list attached.
  escalate / propose
                 a human decision by definition.

`apply` defaults to False. Nothing is written unless it is passed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from app import approval
from app.config import ROOT
from app.rules import gate as _gate

log = logging.getLogger("hermes.product-audit")

DEFAULT_APP_BASE = os.getenv("PUBLIC_APP_BASE_URL", "https://try.vnyx.ai")
REPORT_DIR = Path(os.getenv("HERMES_REPORT_DIR", ROOT / "reports" / "generated"))

# Canonical attribute -> the `properties` keys seen in the wild, in precedence
# order. Lifted verbatim from PROPERTY_ALIASES in vnyx-api's
# services/review-verification.ts, which lifted it from the fallback chains in
# services/openai.ts. Both spellings of several of these are live in stored rows,
# so a single-key lookup reads None on half the catalog — and a rule engine
# cannot tell None-because-unmapped from None-because-missing.
PROPERTY_ALIASES: dict[str, tuple[str, ...]] = {
    "gender": ("gender",),
    "brand": ("brand",),
    "color": ("color", "colour"),
    "material": ("material",),
    "size": ("international_size", "size"),
    "euSize": ("eu_size", "euSize"),
    "waist": ("waist",),
    "waistSize": ("waist_size", "waistSize"),
    "lengthSize": ("length_size", "lengthSize"),
    "fit": ("fit",),
    "condition": ("condition",),
    "model": ("model",),
    "supplier": ("supplier",),
    "sizeType": ("size_type", "sizeType"),
    "productType": ("producttype", "product_type", "productType"),
    "subCategory": ("sub_category", "subCategory"),
    "masterCategory": ("mastercategory", "masterCategory"),
}

# Columns this module is willing to UPDATE. Deliberately short.
#
# Every one is a plain text column with no relation, enum, mirror or cache behind
# it. `price` is absent for the reason in the module docstring; `category` and
# `masterCategory` are absent because `categoryId` is a separate column that would
# be left pointing at the old branch; `sku` and `productCode` are identifiers,
# which the policy's escalate_only_fields covers.
_WRITABLE_COLUMNS: dict[str, str] = {
    "sizingGuide": "sizingGuide",
    "subCategory": "subCategory",
    "mannequinType": "mannequinType",
    "internationalSize": "internationalSize",
    "summary": "summary",
    "title": "title",
}

# Snapshot field -> the `properties` key to write it under.
#
# The canonical snake_case spelling, NOT the camelCase one. vnyx-api's
# PROPERTY_ALIASES gives `eu_size` precedence when READING, so writing `euSize`
# creates a second key beside the original, the reader keeps returning the stale
# value, and SIZE.002 survives its own repair. app/approval.py carries the same
# warning; this is the table that has to honour it.
_PROPERTY_KEYS: dict[str, str] = {
    "gender": "gender",
    "brand": "brand",
    "color": "color",
    "material": "material",
    "size": "international_size",
    "eu_size": "eu_size",
    "waist": "waist",
    "length_size": "length_size",
    "fit": "fit",
    "condition": "condition",
    "model": "model",
    "supplier": "supplier",
}

# The drift pairs name their field by the SNAPSHOT field
# (`international_size`, `master_category`, `subcategory`), and the table above
# is keyed by a partly different set — it calls the first of those `size`. So
# every properties-side DRIFT.001 repair was planned, refused with "no
# `properties` key mapped", and reported as needing a human. Silently: the
# column-side half worked, so the rule looked functional.
#
# Derived rather than retyped, so a pair added to COLUMN_PROPERTY_PAIRS cannot
# be repairable in one direction only. The test asserts both directions for
# every pair.
for _field, _column, _prop in _gate.COLUMN_PROPERTY_PAIRS:
    _PROPERTY_KEYS.setdefault(_field, _prop)

PLACEHOLDERS = {"", "unknown", "n/a", "na", "none", "-", "null", "tbd"}

# The values VNYX stores in BOTH a column and the `properties` map, derived from
# the one table that already names them so the two cannot drift apart. A repair
# to either side writes both — see the comment in apply_plan.
_PROPERTY_FOR_COLUMN: dict[str, str] = {
    column: prop for _field, column, prop in _gate.COLUMN_PROPERTY_PAIRS
}
_COLUMN_FOR_PROPERTY: dict[str, str] = {
    prop: column for _field, column, prop in _gate.COLUMN_PROPERTY_PAIRS
}


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def connect(dsn: str, *, read_only: bool, statement_timeout_s: int = 60):
    """A connection that identifies itself and cannot run away on a busy box.

    `read_only` is enforced by POSTGRES rather than by this file containing no
    writes — the audit path opens READ ONLY so a future edit that introduced a
    write would fail rather than run. The apply path opens writable and is the
    only caller that does.
    """
    conn = psycopg.connect(
        dsn,
        connect_timeout=30,
        application_name=(
            "hermes-product-audit (read-only)" if read_only
            else "hermes-product-audit (apply)"
        ),
        # timezone=UTC, and it is load-bearing rather than tidy.
        #
        # Every timestamp column this codebase touches is `timestamp WITHOUT time
        # zone`, written by two sides that disagreed about which clock to store:
        # Prisma Client materialises `@default(now())` itself and sends a JS Date,
        # which is always UTC, while `now()` from here resolves against the SESSION
        # timezone -- local time on a developer box.
        #
        # Any interval spanning both writers is then wrong by the host's UTC
        # offset. It showed up as single-product Auto Approval runs reporting
        # "5h 34m" on an IST machine: startedAt (Prisma, UTC) subtracted from
        # completedAt (here, local) is +5:30 of nothing.
        #
        # Passed as a connection OPTION, not `SET TIME ZONE`: plain SET is
        # transactional, so a rolled-back transaction would silently restore the
        # local clock and the skew would come back intermittently.
        options=(
            f"-c statement_timeout={int(statement_timeout_s) * 1000} "
            f"-c timezone=UTC"
        ),
    )
    conn.read_only = read_only
    return conn


def _blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return not any(not _blank(v) for v in value)
    return str(value).strip().lower() in PLACEHOLDERS


def read_property(props: dict[str, Any], canonical: str) -> Any:
    """One canonical attribute out of the `properties` map.

    Explicit aliases first, then a case-insensitive scan — which is not
    decorative: this tenant stores the brand under `Brand`, and the alias list
    (copied from vnyx-api) only names `brand`. Without the scan every product
    here reads as having no brand at all.
    """
    for key in PROPERTY_ALIASES.get(canonical, (canonical,)):
        if key in props and not _blank(props[key]):
            return props[key]
    want = canonical.lower()
    for key, value in props.items():
        if key.lower().replace("_", "") == want.replace("_", "") and not _blank(value):
            return value
    return None


_MONEY_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def parse_amount(raw: Any) -> float | None:
    """`Product.price` is a String column holding both "38.99" and "€38.99"."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    m = _MONEY_RE.search(str(raw).replace(",", "."))
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _price_symbol(raw: Any) -> str | None:
    for sym in ("€", "$", "£"):
        if sym in str(raw or ""):
            return sym
    return None


PRODUCT_SQL = """
SELECT p.id::text, p."tenantId"::text, p.sku, p."productCode", p.hanger,
       p.title, p.summary, p.price, p."retailPrice", p."retailPriceBreakdown",
       p.properties, p."propertyConfidence", p."qualityGrading",
       p."operatorDefects", p."masterCategory", p.category, p."subCategory",
       p."sizingGuide", p."sizingGuideId", p."internationalSize",
       p."mannequinType", p."inventoryQuantity", p.images, p."careLabelImages",
       p."reviewStatus"::text, p."currentStage"::text,
       p."generationStatus"::text, p.source::text, p."isRegenerating",
       p."createdAt", p."updatedAt",
       p."mediaManualOrder", p."imageSettings",
       b.name AS brand_relation,
       t.name AS tenant_name
  FROM "Product" p
  LEFT JOIN "Brand"  b ON b.id = p."brandId"
  LEFT JOIN "Tenant" t ON t.id = p."tenantId"
 WHERE p.id = %s::uuid
"""

# The default variant only — the row syncDefaultVariant mirrors Product.price
# into, and the one PRICE.110 compares against.
VARIANT_SQL = """
SELECT "basePrice", "baseCurrency"
  FROM "ProductVariant"
 WHERE "productId" = %s::uuid AND "isDefault" = true
 LIMIT 1
"""

# Active placement, newest first — the same shape getProductById exposes as
# currentBin / currentLpn, so the identity rules see what the detail page shows.
PLACEMENT_SQL = """
SELECT bl."binCode", bl."binNumber", z.code, w.code, l."lpnCode"
  FROM "BinAssignment" a
  LEFT JOIN "BinLocation"   bl ON bl.id = a."binLocationId"
  LEFT JOIN "WarehouseZone" z  ON z.id  = bl."zoneId"
  LEFT JOIN "Warehouse"     w  ON w.id  = bl."warehouseId"
  LEFT JOIN "Lpn"           l  ON l.id  = a."lpnId"
 WHERE a."productId" = %s::uuid AND a."removedAt" IS NULL
 ORDER BY a."assignedAt" DESC
 LIMIT 1
"""

# LIVE media, plus the superseded RAW originals of the garment views.
#
# `isCurrent` is the materialized "nothing derives from this" flag and
# `deletedAt` is a human having removed the asset — two different facts, both of
# which take a row out of the gallery. This product carries rows that are
# isCurrent AND soft-deleted, so testing only the first counts five superseded
# renders as present and reports a finished product as needing none.
#
# THE ARCHIVE COMES TOO (readiness phase 3). A RAW FRONT / BACK / OTHER that a
# cut-out superseded is loaded with `isCurrent = false`, because the canvas
# check (IMG.026) compares a cut-out with the photograph it was cut from, and
# that photograph is, by construction, no longer live. Every consumer that
# counts pictures ON the product filters on `isCurrent` — `needs_from`, the
# rules' `live_media`, the gate and the photo audit — and the archived rows
# exist for the pairing and nothing else. `id`, `width`, `height` and
# `derivedFromId` are what the pairing and the check read.
MEDIA_SQL = """
SELECT id::text, url, view::text, origin::text, processing::text, "mediaType"::text,
       position, width, height, "derivedFromId"::text, "isCurrent"
  FROM "ProductMedia"
 WHERE "productId" = %s::uuid
   AND "deletedAt" IS NULL
   AND ("isCurrent" = true
        OR (processing = 'RAW' AND view IN ('FRONT', 'BACK', 'OTHER')))
 ORDER BY "isCurrent" DESC, position
"""

GRADE_SQL = """
SELECT code, label, severity::text, "priceFactor", "priceMultiplier",
       "priceMultiplierEnabled", "autoReject", position
  FROM "Grade"
 WHERE "tenantId" = %s::uuid
 ORDER BY position
"""

SIZE_GUIDE_SQL = """
SELECT id::text, name, sizes, "euSizes",
       COALESCE(array_length("sizeChartImages", 1), 0),
       COALESCE("sizeChartImages", ARRAY[]::text[])
  FROM "ProductSize"
 WHERE "tenantId" = %s::uuid
 ORDER BY name
"""

# Every master > category > sub path the tenant actually offers. The dropdown is
# built from this tree, so a value absent from it is one no reviewer could have
# picked — which is the most literal reading of "wrong review data" there is.
CATEGORY_SQL = """
WITH RECURSIVE tree AS (
  SELECT id, name, "parentId", 1 AS depth, name::text AS path
    FROM "Category"
   WHERE "tenantId" = %(tid)s::uuid AND "parentId" IS NULL
     AND "isActive" AND "deletedAt" IS NULL
  UNION ALL
  SELECT c.id, c.name, c."parentId", t.depth + 1, t.path || '>' || c.name
    FROM "Category" c
    JOIN tree t ON c."parentId" = t.id
   WHERE c."tenantId" = %(tid)s::uuid AND c."isActive" AND c."deletedAt" IS NULL
)
SELECT path, depth FROM tree WHERE depth <= 3
"""

OPTIONS_SQL = {
    "colors": 'SELECT name FROM "ColorSettings" WHERE "tenantId" = %s::uuid',
    "materials": 'SELECT name FROM "MaterialSettings" WHERE "tenantId" = %s::uuid',
    "brands": 'SELECT name FROM "Brand" WHERE "tenantId" = %s::uuid',
}

IMAGERY_SETTINGS_SQL = """
SELECT "isModelGenerationEnabled", "isRemoveBgEnabled", "isCloseUpEnabled",
       gender, "defaultShots", background, "autoApplyBackground"
  FROM "ImageGenerationSettings"
 WHERE "tenantId" = %s::uuid
 LIMIT 1
"""

# The tenant's working vocabulary: which subcategory values its live products
# actually carry, and how often. The tree says what MAY be chosen; this says what
# IS chosen, and when a title names two valid options the planner prefers the
# one the tenant files under. One GROUP BY per tenant, alongside the other five.
SUBCATEGORY_USAGE_SQL = """
SELECT "subCategory", count(*)::int
  FROM "Product"
 WHERE "tenantId" = %s::uuid AND "isDeleted" = false
   AND "subCategory" IS NOT NULL AND "subCategory" <> ''
 GROUP BY "subCategory"
"""

# The same vocabulary one level up, for readiness phase 1 (docs/READINESS-PLAN.md
# §4 step 2): where this tenant files each subcategory (`Master>Category>Sub`)
# and which sizing guide it puts on each branch (`Master>Category>Guide`). Two
# more GROUP BYs per tenant, loaded once with the rest of the context.
BRANCH_USAGE_SQL = """
SELECT "masterCategory", category, "subCategory", count(*)::int
  FROM "Product"
 WHERE "tenantId" = %s::uuid AND "isDeleted" = false
   AND "masterCategory" IS NOT NULL AND "masterCategory" <> ''
   AND category IS NOT NULL AND category <> ''
   AND "subCategory" IS NOT NULL AND "subCategory" <> ''
 GROUP BY 1, 2, 3
"""

GUIDE_USAGE_SQL = """
SELECT "masterCategory", category, "sizingGuide", count(*)::int
  FROM "Product"
 WHERE "tenantId" = %s::uuid AND "isDeleted" = false
   AND "masterCategory" IS NOT NULL AND "masterCategory" <> ''
   AND category IS NOT NULL AND category <> ''
   AND "sizingGuide" IS NOT NULL AND "sizingGuide" <> ''
 GROUP BY 1, 2, 3
"""


class ProductNotFound(Exception):
    pass


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)


def load_tenant_context(cur, tenant_id: str) -> dict[str, Any]:
    """The five option lists, the grade ladder and the imagery settings.

    All TENANT-scoped, so a bulk sweep loads this ONCE per tenant rather than
    once per product. Six queries against tables with a thousand-plus rows each
    (this tenant has 1,097 brands and 741 materials); doing them per product
    turned a 600-product review queue into 3,600 needless round trips.

    Kept as a function rather than inlined so `load` and `load_batch` cannot
    build the catalog differently — the single-product endpoint and the sweep
    disagreeing about a tenant's own configuration is the exact failure this
    whole feature exists to catch.
    """
    cur.execute(GRADE_SQL, (tenant_id,))
    grades = cur.fetchall()

    cur.execute(SIZE_GUIDE_SQL, (tenant_id,))
    guides = cur.fetchall()

    cur.execute(CATEGORY_SQL, {"tid": tenant_id})
    paths = cur.fetchall()

    options: dict[str, list[str]] = {}
    for key, sql in OPTIONS_SQL.items():
        cur.execute(sql, (tenant_id,))
        options[key] = [r[0] for r in cur.fetchall() if r[0]]

    cur.execute(IMAGERY_SETTINGS_SQL, (tenant_id,))
    imagery_row = cur.fetchone()

    cur.execute(SUBCATEGORY_USAGE_SQL, (tenant_id,))
    usage = {str(name): int(n) for name, n in cur.fetchall() if name}

    cur.execute(BRANCH_USAGE_SQL, (tenant_id,))
    branch_usage = {f"{m}>{c}>{s}": int(n) for m, c, s, n in cur.fetchall()}

    cur.execute(GUIDE_USAGE_SQL, (tenant_id,))
    guide_usage = {f"{m}>{c}>{g}": int(n) for m, c, g, n in cur.fetchall()}

    categories: dict[str, dict[str, list[str]]] = {}
    for path, depth in paths:
        parts = path.split(">")
        if depth == 1:
            categories.setdefault(parts[0], {})
        elif depth == 2:
            categories.setdefault(parts[0], {}).setdefault(parts[1], [])
        elif depth == 3:
            categories.setdefault(parts[0], {}).setdefault(parts[1], []).append(parts[2])

    catalog = {
        "categories": categories,
        "sizingGuides": {
            name: {"sizes": list(sizes or []), "euSizes": list(eu or [])}
            for _gid, name, sizes, eu, _n, _urls in guides
        },
        "colors": options["colors"],
        "materials": options["materials"],
        "brands": options["brands"],
        "subcategoryUsage": usage,
        "branchUsage": branch_usage,
        "guideUsage": guide_usage,
    }
    grade_ladder = [
        {"code": c, "label": lbl, "severity": sev,
         "priceFactor": float(pf) if pf is not None else None,
         "priceMultiplier": float(pm) if pm is not None else None,
         "priceMultiplierEnabled": pme, "autoReject": ar, "position": pos}
        for c, lbl, sev, pf, pm, pme, ar, pos in grades
    ]
    imagery_settings = None
    if imagery_row:
        imagery_settings = {
            "isModelGenerationEnabled": imagery_row[0],
            "isRemoveBgEnabled": imagery_row[1],
            "isCloseUpEnabled": imagery_row[2],
            "gender": imagery_row[3],
            "defaultShots": imagery_row[4],
            # The backdrop a cut-out should sit on (readiness phase 3, decision
            # 1): the tenant's setting, white when unset. Read by
            # app/imaging/cutouts.expected_backdrop.
            "background": imagery_row[5],
            "autoApplyBackground": imagery_row[6],
        }

    return {
        "catalog": catalog,
        "grade_ladder": grade_ladder,
        "imagery_settings": imagery_settings,
        # Kept whole so the chart state can resolve a guide by id.
        "guides": guides,
    }


def load(dsn: str, product_id: str, app_base: str = DEFAULT_APP_BASE) -> dict[str, Any]:
    """Everything about one product, shaped as the review feed plus the extras.

    Returns `{record, media, catalog, grade_ladder, imagery_settings, chart}`.
    """
    # Checked here rather than left to the `::uuid` cast. Postgres raises
    # InvalidTextRepresentation on a malformed id, which surfaced as a 500 —
    # "the service broke" rather than "that is not an id", and the difference
    # matters to anyone scripting against this.
    if not _UUID_RE.match(product_id.strip()):
        raise ProductNotFound(product_id)

    with connect(dsn, read_only=True) as conn, conn.cursor() as cur:
        cur.execute(PRODUCT_SQL, (product_id,))
        row = cur.fetchone()
        if row is None:
            raise ProductNotFound(product_id)

        tenant_id = row[1]

        cur.execute(VARIANT_SQL, (product_id,))
        variant = cur.fetchone()

        cur.execute(PLACEMENT_SQL, (product_id,))
        placement = cur.fetchone() or (None, None, None, None, None)

        cur.execute(MEDIA_SQL, (product_id,))
        media_rows = cur.fetchall()

        ctx = load_tenant_context(cur, tenant_id)

    return build(row, variant, placement, media_rows, ctx, app_base)


def build(row: tuple, variant: tuple | None, placement: tuple,
          media_rows: list[tuple], ctx: dict[str, Any],
          app_base: str = DEFAULT_APP_BASE) -> dict[str, Any]:
    """Assemble one product's payload from rows already fetched.

    Split out of `load` so the bulk sweep and the single-product endpoint share
    one assembler — see the note on load_tenant_context.
    """
    (pid, tenant_id, sku, product_code, hanger, title, summary, price,
     retail_price, retail_breakdown, properties, confidence, grading,
     operator_defects, master_category, category, sub_category,
     sizing_guide, sizing_guide_id, international_size, mannequin,
     inventory, images, care_label_images, review_status, stage,
     generation_status, source, is_regenerating, created_at, updated_at,
     media_manual_order, image_settings,
     brand_relation, tenant_name) = row

    guides = ctx["guides"]
    catalog = ctx["catalog"]
    grade_ladder = ctx["grade_ladder"]
    imagery_settings = ctx["imagery_settings"]

    props: dict[str, Any] = properties if isinstance(properties, dict) else {}
    conf_in: dict[str, Any] = confidence if isinstance(confidence, dict) else {}
    grading = grading if isinstance(grading, dict) else {}

    # ---- the grade row this product is on ----------------------------------
    grade_code = grading.get("grade") if isinstance(grading, dict) else None
    grade_row = next(
        (g for g in grade_ladder
         if str(g["code"]).strip().lower() == str(grade_code or "").strip().lower()),
        None,
    )

    price_amount = parse_amount(price)
    retail_amount = parse_amount(retail_price)
    price_factor = grade_row.get("priceFactor") if grade_row else None
    expected_price = (
        round(retail_amount * price_factor, 2)
        if retail_amount is not None and price_factor else None
    )

    variant_base = float(variant[0]) if variant and variant[0] is not None else None
    variant_currency = variant[1] if variant else None
    drift = (
        round(price_amount - variant_base, 2)
        if price_amount is not None and variant_base is not None else None
    )

    breakdown = retail_breakdown if isinstance(retail_breakdown, dict) else {}
    retail_source = breakdown.get("source")
    retail_provenance = {
        "GRADE_MULTIPLIER": "derived",
        "EBAY_NEW": "market",
    }.get(str(retail_source or ""), "unknown")

    # ---- media, and the three facts the feed never carries -----------------
    media = [
        {"id": mid, "url": url, "view": view, "origin": origin, "processing": processing,
         "mediaType": media_type, "isCurrent": bool(is_current), "deletedAt": None,
         "position": position, "width": width, "height": height,
         "derivedFromId": derived_from}
        for (mid, url, view, origin, processing, media_type, position,
             width, height, derived_from, is_current) in media_rows
    ]
    # What is ON the product. The archived originals ride along in `media` for
    # the cut-out pairing (see MEDIA_SQL) and count as nothing here.
    live_rows = [m for m in media if m["isCurrent"]]
    chart_media = [m["url"] for m in live_rows if m["view"] == "SIZE_CHART"]
    care_label_media = [m for m in live_rows if m["view"] == "LABEL"]

    guide = next((g for g in guides if g[0] == str(sizing_guide_id or "")), None)
    chart = {
        "sizingGuideId": sizing_guide_id,
        "sizingGuideName": guide[1] if guide else None,
        "guideImageCount": guide[4] if guide else 0,
        "guideImages": list(guide[5]) if guide else [],
        "chartOnProduct": chart_media,
        "state": _chart_state(sizing_guide_id, guide, chart_media),
    }

    edit_url = (
        f"{app_base.rstrip('/')}/product/{pid}/edit?tenantId={tenant_id}"
        if tenant_id else None
    )

    record: dict[str, Any] = {
        "id": pid,
        "tenantId": tenant_id,
        "tenantName": tenant_name,
        "editUrl": edit_url,

        "sku": sku,
        "productCode": product_code,
        "hanger": hanger,
        "lpnCode": placement[4],
        "binCode": placement[0],
        "binNumber": placement[1],
        "binZoneCode": placement[2],
        "binWarehouseCode": placement[3],

        "masterCategory": master_category or read_property(props, "masterCategory"),
        "category": category,
        "subCategory": sub_category or read_property(props, "subCategory"),
        "sizingGuide": sizing_guide,
        "mannequinType": mannequin,

        "title": title,
        "summary": summary,

        # Currency: the price string's own symbol is the most local evidence,
        # the variant mirror's ISO code the fallback, EUR last — the app writes
        # prices as `€{n}`, so it is the implicit default rather than a guess.
        "currency": {"€": "EUR", "$": "USD", "£": "GBP"}.get(
            _price_symbol(price) or "", variant_currency or "EUR"),
        "price": price,
        "priceAmount": price_amount,
        "retailPrice": retail_price,
        "retailPriceAmount": retail_amount,
        "inventoryQuantity": inventory,
        "retailProvenance": retail_provenance,
        "retailSource": retail_source,

        "grade": grade_code,
        "gradeLabel": grade_row.get("label") if grade_row else None,
        "gradeSeverity": grade_row.get("severity") if grade_row else None,
        "gradingSeverity": grading.get("severity"),
        "defects": [
            d if isinstance(d, str) else (d.get("label", "") if isinstance(d, dict) else str(d))
            for d in (grading.get("defects") or [])
        ],
        "operatorDefects": list(operator_defects or []),

        "priceExpectation": {
            "priceFactor": price_factor,
            "expectedPrice": expected_price,
        },
        "variantPriceDrift": {
            "variantBasePrice": variant_base,
            "drift": drift,
        },

        "gender": read_property(props, "gender"),
        "brand": read_property(props, "brand"),
        "color": read_property(props, "color"),
        "material": read_property(props, "material"),
        "size": read_property(props, "size"),
        "euSize": read_property(props, "euSize"),
        "internationalSize": international_size,
        "waist": read_property(props, "waist"),
        "lengthSize": read_property(props, "lengthSize"),
        "fit": read_property(props, "fit"),
        "condition": read_property(props, "condition"),
        "model": read_property(props, "model"),
        "supplier": read_property(props, "supplier"),
        "brandRelation": brand_relation,

        "properties": props,
        "propertyConfidence": {
            k: float(v) for k, v in conf_in.items()
            if isinstance(v, (int, float))
        },

        "reviewStatus": review_status,
        "currentStage": stage,
        "generationStatus": generation_status,
        "isRegenerating": bool(is_regenerating),
        "source": source,
        # Readiness phase 5: a gallery a person arranged is never re-ordered
        # (IMG.025 stays silent), and the product's own render settings carry
        # the build the pictures were made with (`imageSettings.bodyType`).
        "mediaManualOrder": bool(media_manual_order),
        "imageSettings": image_settings if isinstance(image_settings, dict) else {},
        "createdAt": created_at.isoformat() if created_at else None,
        "updatedAt": updated_at.isoformat() if updated_at else None,

        # The count IMG.030 reads. From ProductMedia, not the legacy column.
        "careLabelCount": len(care_label_media),
        "legacyCareLabelCount": len(care_label_images or []),
        "imageCount": len([m for m in live_rows if m["mediaType"] == "IMAGE"]),
        "legacyImageCount": len(images or []),
        # THE GALLERY CACHE ITSELF (readiness phase 5). `Product.images` is
        # vnyx-api's display order, and IMG.025 compares it with the order the
        # rows imply; IMG.023 / IMG.024 read its first entry. Until now the
        # record carried only its COUNT, so on a product loaded from the
        # database those three rules were silent — the review feed sends the
        # list, the loader did not.
        "images": [str(u) for u in (images or []) if u],

        # Chart facts. Flattened onto the record so a future rule can read them
        # off the snapshot without a second argument.
        "sizingGuideId": sizing_guide_id,
        "sizeChartImageCount": chart["guideImageCount"],
        "sizeChartOnProduct": len(chart_media),

        # The UNRESOLVED column values, for DRIFT.001.
        #
        # Every attribute above went through the alias chain and holds whichever
        # of the two copies won. These are the columns as stored, so the rule can
        # ask whether the two copies agree — which the resolved view cannot
        # express, because resolving it is exactly what hides the disagreement.
        "columnValues": {
            "internationalSize": international_size,
            "masterCategory": master_category,
            "subCategory": sub_category,
            "sizingGuide": sizing_guide,
            "title": title,
            "summary": summary,
        },
    }

    return {
        "record": record,
        "media": media,
        "catalog": catalog,
        "grade_ladder": grade_ladder,
        "imagery_settings": imagery_settings,
        "chart": chart,
    }


# Product ids per media query. Small enough that `= ANY($1)` keeps using the
# index on a production-sized ProductMedia, large enough that the round trips do
# not dominate — the same figure audit_product_images.py settled on.
MEDIA_BATCH = 2000

PRODUCTS_BY_ID_SQL = PRODUCT_SQL.replace(
    'WHERE p.id = %s::uuid', 'WHERE p.id = ANY(%s::uuid[])'
)

VARIANTS_BATCH_SQL = """
SELECT "productId"::text, "basePrice", "baseCurrency"
  FROM "ProductVariant"
 WHERE "productId" = ANY(%s::uuid[]) AND "isDefault" = true
"""

# The same rows as MEDIA_SQL, for many products — live, plus the superseded RAW
# garment originals flagged `isCurrent = false` (readiness phase 3).
MEDIA_BATCH_SQL = """
SELECT "productId"::text, id::text, url, view::text, origin::text, processing::text,
       "mediaType"::text, position, width, height, "derivedFromId"::text, "isCurrent"
  FROM "ProductMedia"
 WHERE "productId" = ANY(%s::uuid[])
   AND "deletedAt" IS NULL
   AND ("isCurrent" = true
        OR (processing = 'RAW' AND view IN ('FRONT', 'BACK', 'OTHER')))
 ORDER BY "productId", "isCurrent" DESC, position
"""

PLACEMENTS_BATCH_SQL = """
SELECT DISTINCT ON (a."productId")
       a."productId"::text, bl."binCode", bl."binNumber", z.code, w.code,
       l."lpnCode"
  FROM "BinAssignment" a
  LEFT JOIN "BinLocation"   bl ON bl.id = a."binLocationId"
  LEFT JOIN "WarehouseZone" z  ON z.id  = bl."zoneId"
  LEFT JOIN "Warehouse"     w  ON w.id  = bl."warehouseId"
  LEFT JOIN "Lpn"           l  ON l.id  = a."lpnId"
 WHERE a."productId" = ANY(%s::uuid[]) AND a."removedAt" IS NULL
 ORDER BY a."productId", a."assignedAt" DESC
"""


def load_batch(cur, product_ids: list[str],
               contexts: dict[str, dict[str, Any]],
               app_base: str = DEFAULT_APP_BASE) -> list[dict[str, Any]]:
    """Assemble many products with four queries instead of four per product.

    `contexts` is tenant id -> load_tenant_context(...), filled in by the caller
    and reused across batches. Products whose tenant is missing from it are
    skipped rather than judged against no catalog: a product judged with an empty
    catalog reports its perfectly good category as invalid, which is worse than
    not reporting it.
    """
    cur.execute(PRODUCTS_BY_ID_SQL, (product_ids,))
    rows = cur.fetchall()

    cur.execute(VARIANTS_BATCH_SQL, (product_ids,))
    variants = {r[0]: (r[1], r[2]) for r in cur.fetchall()}

    cur.execute(PLACEMENTS_BATCH_SQL, (product_ids,))
    placements = {r[0]: tuple(r[1:]) for r in cur.fetchall()}

    cur.execute(MEDIA_BATCH_SQL, (product_ids,))
    media: dict[str, list[tuple]] = {}
    for r in cur.fetchall():
        media.setdefault(r[0], []).append(tuple(r[1:]))

    out = []
    for row in rows:
        pid, tenant_id = row[0], row[1]
        ctx = contexts.get(tenant_id)
        if ctx is None:
            continue
        out.append(build(
            row,
            variants.get(pid),
            placements.get(pid, (None, None, None, None, None)),
            media.get(pid, []),
            ctx,
            app_base,
        ))
    return out


def _chart_state(guide_id: Any, guide: tuple | None, on_product: list[str]) -> str:
    """One of six states, ordered by WHERE THE FIX LIVES.

    Ported from scripts/audit_product_data.py, which measured the split on
    production: 182 products with no guide at all, 1,302 whose guide has no chart
    images — and those 1,302 are ten guides, not 1,302 problems. Collapsing this
    into "no size chart" sends 1,302 people to a product page that cannot fix it.
    """
    if not guide_id:
        return ("chart present, but NO sizing guide selected" if on_product
                else "no sizing guide selected")
    if guide is None:
        return "sizing guide id does not resolve"
    if guide[4] == 0:
        return "GUIDE HAS NO CHART IMAGES"
    if not on_product:
        return "guide has images, none copied to the product"
    if set(on_product) & set(guide[5] or []):
        return "ok"
    return "CHART DOES NOT MATCH THE SELECTED GUIDE"


# --------------------------------------------------------------------------- #
# Fixing
# --------------------------------------------------------------------------- #

def _explain_refusal(action: dict[str, Any]) -> str:
    kind = action["kind"]
    field = action.get("field") or ""
    if kind == "escalate":
        return "human decision — never auto-applied"
    if kind == "propose":
        return "needs a reviewer to authorise"
    if kind == "create_size_chart":
        return "needs R2 credentials; create the guide in the app"
    if kind == "generate_images":
        return "needs the image model; use POST /v1/imagery/generate"
    if kind == "set_column" and field in ("price", "retailPrice"):
        return ("price writes must go through updateProduct — it mirrors into "
                "ProductVariant, Inventory and a Price row per connected "
                "marketplace account")
    if kind == "set_column":
        return f"column '{field}' is not in the writable set"
    if kind == "set_property":
        return f"no `properties` key mapped for '{field}'"
    return "not applicable from this endpoint"


def apply_plan(dsn: str, product_id: str, plan: list[dict[str, Any]],
               expected_updated_at: str | None,
               conn=None) -> dict[str, Any]:
    """Execute the applicable subset of a repair plan. One transaction.

    `expected_updated_at` is an optimistic-concurrency guard: the row is
    re-checked inside the transaction and the whole thing aborts if anything
    edited the product between the read and the write. Without it a reviewer
    saving the edit screen while this runs would have their change silently
    overwritten by a value computed from the pre-edit record.

    `conn` lets a bulk caller reuse ONE writable connection across many products.
    Opening a fresh one per product is fine for the single-product endpoint and
    wrong for a sweep: a 205-product repair run against a remote database would
    pay 205 connection handshakes and can exhaust a server-side connection limit
    part-way through, leaving the run half-applied. Each product still commits on
    its own, so one conflict does not roll back the products before it.
    """
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    prop_patch: dict[str, Any] = {}
    col_patch: dict[str, Any] = {}

    for action in plan:
        kind = action["kind"]
        field = action.get("field")
        value = action.get("value")

        if kind == "set_property" and field in _PROPERTY_KEYS:
            key = _PROPERTY_KEYS[field]
            prop_patch[key] = value
            storage = [f"properties.{key}"]
            # BOTH SIDES, always.
            #
            # A value VNYX stores twice has to be repaired twice. Writing one
            # copy converts the defect rather than clearing it: repairing
            # subCategory alone took a product whose column was off-tree and left
            # one whose column and `properties` copy disagree — a fresh DRIFT.001
            # manufactured by the repair that was supposed to settle it. Observed
            # on this product, not hypothesised.
            twin = _COLUMN_FOR_PROPERTY.get(key)
            if twin and twin in _WRITABLE_COLUMNS:
                col_patch[_WRITABLE_COLUMNS[twin]] = value
                storage.append(f"column {twin}")
            applied.append({**action, "storage": " + ".join(storage)})
        elif kind == "set_column" and field in _WRITABLE_COLUMNS:
            col_patch[_WRITABLE_COLUMNS[field]] = value
            storage = [f"column {field}"]
            key = _PROPERTY_FOR_COLUMN.get(field)
            if key:
                prop_patch[key] = value
                storage.append(f"properties.{key}")
            applied.append({**action, "storage": " + ".join(storage)})
        else:
            skipped.append({**action, "why": _explain_refusal(action)})

    if not applied:
        return {"applied": [], "skipped": skipped, "wrote": False,
                "conflict": False}

    owned = conn is None
    if owned:
        conn = connect(dsn, read_only=False)
    try:
        with conn.cursor() as cur:
            # Re-read inside the transaction and lock the row.
            cur.execute(
                'SELECT "updatedAt" FROM "Product" WHERE id = %s::uuid FOR UPDATE',
                (product_id,),
            )
            current = cur.fetchone()
            if current is None:
                raise ProductNotFound(product_id)

            now_stamp = current[0].isoformat() if current[0] else None
            if expected_updated_at and now_stamp != expected_updated_at:
                conn.rollback()
                return {
                    "applied": [], "wrote": False, "conflict": True,
                    "skipped": skipped + [
                        {**a, "why": "aborted — the product changed mid-audit"}
                        for a in applied
                    ],
                    "message": (
                        f"Product changed during the audit "
                        f"(updatedAt {expected_updated_at} -> {now_stamp}). "
                        f"Nothing was written. Re-run the audit."
                    ),
                }

            sets: list[str] = []
            params: list[Any] = []

            if prop_patch:
                # Server-side jsonb merge. `||` is applied by Postgres to the
                # stored value inside this transaction, so the other keys in the
                # map are untouched and there is no read-merge-write window —
                # which is the whole reason vnyx_client.patch_product refuses
                # these rather than doing it over HTTP.
                sets.append('properties = COALESCE(properties, \'{}\'::jsonb) || %s::jsonb')
                params.append(Jsonb(prop_patch))

            for column, value in col_patch.items():
                sets.append(f'"{column}" = %s')
                params.append(value)

            # Raw SQL bypasses Prisma's @updatedAt, so it is set explicitly.
            # Leaving it stale would make a repaired product look untouched to
            # every cache and reconciler keyed on it.
            sets.append('"updatedAt" = %s')
            params.append(datetime.now(timezone.utc).replace(tzinfo=None))
            params.append(product_id)

            cur.execute(
                f'UPDATE "Product" SET {", ".join(sets)} WHERE id = %s::uuid',
                params,
            )
        conn.commit()
    finally:
        if owned:
            conn.close()

    return {"applied": applied, "skipped": skipped, "wrote": True,
            "conflict": False}


# --------------------------------------------------------------------------- #
# The audit
# --------------------------------------------------------------------------- #

def audit(dsn: str, product_id: str, *, apply: bool = False,
          use_llm: bool = False, app_base: str = DEFAULT_APP_BASE,
          write_sheet: bool = True,
          sheet_path: str | None = None,
          severity_overrides: dict[str, str] | None = None,
          ) -> dict[str, Any]:
    """Verify one product, repair what can be repaired, report the rest.

    The gate is asked TWICE when anything was written. The second answer is
    computed from the stored record rather than from the plan that was supposed
    to produce it — a repair can fail silently, and a verdict that trusted its
    own plan would report a product fixed whose writes never landed.
    """
    started = time.perf_counter()

    loaded = load(dsn, product_id, app_base)
    record = loaded["record"]
    before = _snapshot_values(record)

    first = approval.run_gate(
        {**record, "media": loaded["media"]},
        catalog=loaded["catalog"],
        imagery_settings=loaded["imagery_settings"],
        llm=None if not use_llm else _llm(),
        severity_overrides=severity_overrides,
    )

    write_result: dict[str, Any] = {"applied": [], "skipped": [], "wrote": False,
                                    "conflict": False}
    second: dict[str, Any] | None = None
    after: dict[str, Any] = before

    if apply:
        write_result = apply_plan(
            dsn, product_id, first["repair_plan"], record.get("updatedAt")
        )
        if write_result["wrote"]:
            reloaded = load(dsn, product_id, app_base)
            after = _snapshot_values(reloaded["record"])
            second = approval.run_gate(
                {**reloaded["record"], "media": reloaded["media"]},
                catalog=reloaded["catalog"],
                imagery_settings=reloaded["imagery_settings"],
                llm=None if not use_llm else _llm(),
                # The SAME overrides on the re-verify. Skipping them here would
                # make a product that passed pass 1 fail pass 2 on a finding the
                # tenant had explicitly downgraded, and the audit would report
                # a repair that "did not land" when it landed fine.
                severity_overrides=severity_overrides,
            )
    else:
        # Nothing was written, so every applicable action is a would-be change.
        # Classified with the same function the apply path uses, so a dry run and
        # a real run never disagree about what is applicable.
        for action in first["repair_plan"]:
            field = action.get("field")
            applicable = (
                (action["kind"] == "set_property" and field in _PROPERTY_KEYS)
                or (action["kind"] == "set_column" and field in _WRITABLE_COLUMNS)
            )
            if applicable:
                write_result["applied"].append({**action, "storage": "(dry run)"})
            else:
                write_result["skipped"].append(
                    {**action, "why": _explain_refusal(action)}
                )

    final = second or first
    result = {
        "product_id": product_id,
        "tenant_id": record["tenantId"],
        "tenant": record["tenantName"],
        "title": record["title"],
        "sku": record["sku"],
        "stage": record["currentStage"],
        "review_status": record["reviewStatus"],
        "edit_url": record["editUrl"],
        "applied": apply and write_result["wrote"],
        "conflict": write_result["conflict"],

        # The verdict BEFORE anything was touched, and after.
        "verified_before": first["verified"],
        "verified_after": final["verified"],
        "human_intervention_needed": final["human_intervention_needed"],

        "issues": first["field_issues"],
        "blocking": first["blocking"],
        "advisory": first["advisory"],
        "findings": first["findings"],
        "price": first["price"],
        "chart": loaded["chart"],

        "fixes": write_result["applied"],
        "not_fixed": write_result["skipped"],
        "remaining": final["blocking"] if second else first["blocking"],

        "counts": {
            "issues": len(first["field_issues"]),
            "blocking": len(first["blocking"]),
            "advisory": len(first["advisory"]),
            "fixed": len(write_result["applied"]) if write_result["wrote"] else 0,
            "would_fix": len(write_result["applied"]) if not write_result["wrote"] else 0,
            "needs_a_human": len(write_result["skipped"]),
        },
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }

    if write_result.get("message"):
        result["message"] = write_result["message"]

    if write_sheet:
        result["sheet"] = str(
            write_workbook(result, record, before, after, loaded, sheet_path)
        )
    return result


def _llm():
    from app.llm.gemini import GeminiClient  # local import: optional dependency
    return GeminiClient()


_TRACKED = [
    "title", "summary", "masterCategory", "category", "subCategory",
    "sizingGuide", "mannequinType", "internationalSize", "gender", "brand",
    "color", "material", "size", "euSize", "waist", "lengthSize", "fit",
    "condition", "model", "supplier", "price", "retailPrice",
]


def _snapshot_values(record: dict[str, Any]) -> dict[str, Any]:
    return {k: record.get(k) for k in _TRACKED}


# --------------------------------------------------------------------------- #
# The sheet
# --------------------------------------------------------------------------- #

HDR_FILL = "1F3864"
BAD = "FDECEA"
WARN = "FFF8E1"
OK = "E8F5E9"
INFO = "D9E2F3"

_SEV_FILL = {"critical": BAD, "high": BAD, "medium": WARN, "low": INFO}


def write_workbook(result: dict[str, Any], record: dict[str, Any],
                   before: dict[str, Any], after: dict[str, Any],
                   loaded: dict[str, Any],
                   path: str | None = None) -> Path:
    """Four sheets: what is wrong, what changed, what a human still owns, context.

    Separate sheets rather than one wide one because they answer different
    questions and are read by different people — Issues is the reviewer's,
    Changes is the audit trail, Needs a human is the queue.
    """
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def fill(colour: str) -> PatternFill:
        return PatternFill("solid", fgColor=colour)

    def header(ws, headers: list[str], widths: list[int]) -> None:
        ws.append(headers)
        for cell in ws[1]:
            cell.fill = fill(HDR_FILL)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for i, w in enumerate(widths, start=1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = "A2"

    wb = Workbook()

    # ---- Summary ----------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Product audit"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)

    verdict = (
        "PASSES — nothing blocking" if result["verified_after"]
        else "BLOCKED — see Issues"
    )
    rows = [
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Product", result["title"]),
        ("Product ID", result["product_id"]),
        ("SKU", result["sku"]),
        ("Tenant", f'{result["tenant"]} ({result["tenant_id"]})'),
        ("Stage", f'{result["stage"]} / {result["review_status"]}'),
        ("", ""),
        ("Verdict", verdict),
        ("Before the audit", "verified" if result["verified_before"] else "not verified"),
        ("After the audit", "verified" if result["verified_after"] else "not verified"),
        ("Fields with an issue", result["counts"]["issues"]),
        ("Blocking", result["counts"]["blocking"]),
        ("Advisory", result["counts"]["advisory"]),
        (
            "Fixed automatically" if result["applied"] else "Would fix (dry run)",
            result["counts"]["fixed"] or result["counts"]["would_fix"],
        ),
        ("Left for a human", result["counts"]["needs_a_human"]),
        ("", ""),
        ("Size chart", loaded["chart"]["state"]),
        ("Sizing guide", loaded["chart"]["sizingGuideName"] or "— none selected"),
        ("Care label images", record["careLabelCount"]),
        ("Live media rows", len([m for m in loaded["media"] if m.get("isCurrent", True)])),
        ("", ""),
        ("Edit URL", result["edit_url"]),
    ]
    for label, value in rows:
        ws.append([label, value])
    ws.cell(row=9, column=2).fill = fill(OK if result["verified_after"] else BAD)
    if result.get("message"):
        ws.append([])
        ws.append(["NOTE", result["message"]])
        ws.cell(row=ws.max_row, column=2).fill = fill(WARN)
    ws.column_dimensions["A"].width = 24
    ws.column_dimensions["B"].width = 92
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # ---- Issues -----------------------------------------------------------
    ws = wb.create_sheet("Issues")
    header(
        ws,
        ["Field", "Severity", "Blocking?", "Rules", "What is wrong",
         "Current value", "Proposed value", "Outcome"],
        [20, 11, 11, 22, 74, 30, 30, 30],
    )
    # What ACTUALLY happened to each field, keyed the way _field_issues keys its
    # rows.
    #
    # Not derived from `issue["action"]`. That names the plan entry's KIND, and a
    # planned `set_column` the apply step refused still reads as `set_column` —
    # so the price row, which is deliberately never written from here, reported
    # itself as "fixed" while also appearing under "Needs a human". A report that
    # contradicts itself about whether a price changed is worse than no report.
    def key(name: Any) -> str:
        return "".join(ch for ch in str(name or "").lower() if ch.isalnum())

    done = {key(a.get("field")) for a in result["fixes"]}
    refused = {key(a.get("field")): a for a in result["not_fixed"]}

    for issue in result["issues"]:
        field = issue["field"]
        k = key(field)
        if k in done:
            outcome = "fixed" if result["applied"] else "would fix"
        elif k in refused:
            outcome = {
                "create_size_chart": "needs the app",
                "generate_images": "needs the image model",
                "escalate": "needs a human",
                "propose": "needs authorising",
            }.get(refused[k]["kind"], "needs a human — see that sheet")
        else:
            outcome = "no repair available"
        ws.append([
            field,
            issue["severity"],
            "yes" if issue["blocking"] else "no",
            ", ".join(dict.fromkeys(issue["rules"])),
            " ".join(dict.fromkeys(issue["messages"])),
            _show(_current_value(field, before, record)),
            _show(issue.get("fix")),
            outcome,
        ])
        colour = _SEV_FILL.get(issue["severity"], INFO)
        for col in range(1, 9):
            ws.cell(row=ws.max_row, column=col).alignment = Alignment(
                vertical="top", wrap_text=True)
        ws.cell(row=ws.max_row, column=2).fill = fill(colour)
    if ws.max_row == 1:
        ws.append(["— no issues found —"])
        ws.cell(row=2, column=1).fill = fill(OK)
    ws.auto_filter.ref = f"A1:H{max(ws.max_row, 2)}"

    # ---- Changes ----------------------------------------------------------
    ws = wb.create_sheet("Changes")
    header(
        ws,
        ["Field", "Stored as", "Was", "Now", "Because", "Applied?"],
        [20, 26, 34, 34, 20, 11],
    )
    for action in result["fixes"]:
        field = action.get("field") or "(product)"
        key = _camel(field)
        ws.append([
            field,
            action.get("storage", ""),
            _show(before.get(key)),
            _show(action.get("value")),
            action.get("reason", ""),
            "yes" if result["applied"] else "dry run",
        ])
        for col in range(1, 7):
            ws.cell(row=ws.max_row, column=col).alignment = Alignment(
                vertical="top", wrap_text=True)
        ws.cell(row=ws.max_row, column=4).fill = fill(
            OK if result["applied"] else WARN)
    if ws.max_row == 1:
        ws.append(["— nothing to change —"])
    ws.auto_filter.ref = f"A1:F{max(ws.max_row, 2)}"

    # ---- Needs a human ----------------------------------------------------
    ws = wb.create_sheet("Needs a human")
    header(
        ws,
        ["Field", "Action", "Proposed value", "Why it was not applied", "Rule"],
        [20, 20, 34, 66, 14],
    )
    for action in result["not_fixed"]:
        ws.append([
            action.get("field") or "(product)",
            action["kind"],
            _show(action.get("value")),
            action.get("why", ""),
            action.get("reason", ""),
        ])
        for col in range(1, 6):
            ws.cell(row=ws.max_row, column=col).alignment = Alignment(
                vertical="top", wrap_text=True)
        ws.cell(row=ws.max_row, column=4).fill = fill(WARN)
    if ws.max_row == 1:
        ws.append(["— nothing outstanding —"])
        ws.cell(row=2, column=1).fill = fill(OK)

    # ---- Snapshot ---------------------------------------------------------
    #
    # Every field the rules read, with its value before and after. This is what
    # makes a verdict checkable: a rule that fired on a null is a mapping bug
    # rather than a data problem, and the only way to tell is to see the value
    # the engine actually got.
    ws = wb.create_sheet("Snapshot")
    header(ws, ["Field", "Before", "After", "Changed?"], [24, 46, 46, 11])
    for key in _TRACKED:
        was, now = before.get(key), after.get(key)
        changed = was != now
        ws.append([key, _show(was), _show(now), "yes" if changed else ""])
        if changed:
            for col in range(1, 5):
                ws.cell(row=ws.max_row, column=col).fill = fill(OK)

    ws.append([])
    ws.append(["Live media", ""])
    ws.cell(row=ws.max_row, column=1).font = Font(bold=True)
    for m in loaded["media"]:
        if not m.get("isCurrent", True):
            continue  # the archived originals are evidence, not gallery
        ws.append([m["view"], f'{m["origin"]} / {m["processing"]}', m["url"], ""])

    target = Path(path) if path else (
        REPORT_DIR / f'audit-{result["product_id"][:8]}-'
                     f'{datetime.now():%Y-%m-%d-%H%M%S}.xlsx'
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write to a temp name and rename, so a reader never opens a half-written
    # workbook and a failed save leaves the previous file intact.
    tmp = target.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, target)
    return target


def _camel(field: str) -> str:
    """Snapshot field name -> the key `_TRACKED` stores it under."""
    return {
        "eu_size": "euSize",
        "length_size": "lengthSize",
        "master_category": "masterCategory",
        "sizing_guide": "sizingGuide",
        "mannequin": "mannequinType",
        "description": "summary",
        "subcategory": "subCategory",
        "international_size": "internationalSize",
        "retail_price": "retailPrice",
    }.get(field, field)


def _current_value(field: str, before: dict[str, Any],
                   record: dict[str, Any]) -> Any:
    """What the product holds for a field a rule named.

    Rules name fields the product does not carry as a column — `variant_base_price`
    is a join, `price_factor` comes off the grade row. Falling through to None for
    those printed an empty "Current value" beside a finding that was entirely
    about that value, which reads as a bug in the audit rather than as the field
    being derived.
    """
    key = _camel(field)
    if key in before:
        return before[key]
    if key in record:
        return record[key]
    derived = {
        "variant_base_price": ("variantPriceDrift", "variantBasePrice"),
        "price_factor": ("priceExpectation", "priceFactor"),
        "expected_price": ("priceExpectation", "expectedPrice"),
    }.get(field)
    if derived:
        block = record.get(derived[0])
        if isinstance(block, dict):
            return block.get(derived[1])
    return None


def _show(value: Any) -> str:
    """A cell a person can read. Long HTML summaries are truncated."""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        text = json.dumps(value, default=str, ensure_ascii=False)
    else:
        text = str(value)
    text = text.strip()
    return text if len(text) <= 600 else text[:597] + "…"
