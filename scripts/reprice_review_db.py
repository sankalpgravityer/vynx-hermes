#!/usr/bin/env python
"""Reprice the products in one Products tab, straight against Postgres.

    # 1. LOOK FIRST. 10 products, read-only, sheet only.
    python scripts/reprice_review_db.py --dsn "postgresql://USER:PASS@HOST:5432/DB"

    # 2. Write those 10.
    python scripts/reprice_review_db.py --dsn "..." --limit 10 --apply

    # 3. Everything on the Review page.
    python scripts/reprice_review_db.py --dsn "..." --all --apply


THE PRICE

    expected     = retailPrice x GRADE_PCT[grade]
    min_allowed  = charm99(expected x 0.80)
    max_allowed  = min(charm99(expected x 1.20), 95% of retail as a .99)

    inside the window   -> kept, unless the cents are not .99
    below the minimum   -> raised to min_allowed
    above the maximum   -> lowered to max_allowed
    at or above retail  -> lowered to max_allowed

The same +/-20% band the Hermes validator and the analyze worker use, so a
price this writes is one all three agree on. A price already inside its window
is NOT snapped to the target -- the correction is always the smallest one that
makes the record right, which is what keeps a good eBay-derived figure intact.

charm99 keeps the whole units and sets the cents to .99, so 19.50 becomes 19.99
and 15.60 becomes 15.99. Both bounds are charmed too, so a window that computes
to 15.60-23.40 is reported and applied as 15.99-23.99, and a clamped price lands
on a figure the shop would actually display. Every price this writes ends .99.

GRADE_PCT is a FIXED table applied to every tenant (see below). It deliberately
ignores each tenant's own `Grade.priceFactor`, which is what the API and the
Hermes validator use -- see the WARNING in `--help` output and the README note.


WHICH PRODUCTS

One Products tab, chosen with --stage, defined exactly as
services/review-verification.ts defines it:

    isArchived = false AND isDeleted = false AND reviewStatus = <the tab>

    --stage review     PENDING             (the default; the Review tab)
    --stage approved   ACCEPTED            (labelled Approved, ?tab=uploaded)
    --stage rejected   REJECTED
    --stage photobooth PENDING_PHOTOBOOTH
    --stage decision   PENDING_DECISON     (misspelled in the enum)

Ordered by createdAt DESC, which is the order the page itself shows, so a
`--limit 10` here is the same ten rows at the top of the screen.

scripts/reprice_approved_db.py is the same thing pointed at the Approved tab.


WHAT IT WRITES, AND WHAT IT DOES NOT

Two columns, by primary key, and nothing else:

    Product.price                   the selling price
    ProductVariant.basePrice        the default variant's mirror of it

The mirror is included because it is what the marketplace sync publishes.
`syncDefaultVariantSafe` in vnyx-api keeps those two in step on every normal
write; updating only Product.price would leave the channels selling at the old
figure -- exactly the drift the PRICE.110 rule exists to report.

NOT touched: `Price` rows (per-marketplace overrides, which vnyx-api's own sync
also leaves alone), `updatedAt` on anything but the two rows above, product
stage, review status, and the activity log. No schema is read for migration and
no DDL is issued -- this script only ever runs SELECT and UPDATE.

Because it bypasses the API there is no ActivityLog entry per product. The
spreadsheet IS the audit trail, so keep it.
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    import psycopg
except ImportError:  # pragma: no cover
    sys.exit("psycopg is required.\n  pip install \"psycopg[binary]\"")

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
# The grade table
#
# One ladder for every tenant, as specified. This is the whole point of the
# script -- it is NOT read from the Grade table, so a tenant that has its own
# priceFactor configured will be repriced to these numbers instead.
#
# D is 16.66%, not 1/6 (16.666...): the figure as given, so the arithmetic is
# reproducible by hand from the sheet.
# --------------------------------------------------------------------------- #

GRADE_PCT: dict[str, float] = {
    "A": 0.50,
    "B": 0.40,
    "C": 0.25,
    "D": 0.1666,
}

# Relative half-width of the allowed band around the target: 0.20 means the
# window is target +/- 20% OF THE TARGET, not 20 percentage points of retail.
# The same figure as `factor_tolerance` in config/policy.yaml and
# DEFAULT_FACTOR_TOLERANCE in vnyx-api's product-pricing.ts. Keep the three in
# step -- if they disagree, each will flag prices the others just approved.
DEFAULT_TOLERANCE = 0.20

# No second-hand item may be priced at or above this fraction of its own retail
# price, whatever the grade window works out to. Matches `hard_max_ratio`.
HARD_MAX_RATIO = 0.95

# Absolute floor under the window's lower bound: handling a garment costs more
# than this, so listing below it loses money on every sale. Matches `min_price`
# in config/policy.yaml -- without it this script and Hermes disagree on every
# product whose band bottoms out under 3.00, and each would flag prices the
# other had just written. Pass --min-price 0 to switch it off.
DEFAULT_MIN_PRICE = 3.00

# Names that suggest the connection is not a scratch database. Matched against
# the redacted DSN and used only to make the confirmation prompt harder to walk
# past -- 1,097 price changes read the same at the prompt whichever database is
# on the other end of it.
PROD_HINTS = re.compile(r"prod|live|production", re.I)

# The Products tabs, exactly as the API maps them.
#
# From `reviewStatusForTab` in services/review-verification.ts, cross-checked
# against the per-tab counts in services/products.ts. The UI labels the
# `uploaded` tab "Approved", which is why both names resolve to ACCEPTED --
# the URL says ?tab=uploaded while the badge says Approved.
#
# `PENDING_DECISON` is misspelled in the database enum. Kept verbatim; correcting
# it here would simply match nothing.
STAGES: dict[str, str] = {
    "review": "PENDING",
    "pending": "PENDING",
    "approved": "ACCEPTED",
    "uploaded": "ACCEPTED",
    "rejected": "REJECTED",
    "photobooth": "PENDING_PHOTOBOOTH",
    "decision": "PENDING_DECISON",
}

# The chosen tab. `reviewStatus` is an enum column and the value arrives as a
# bind parameter, so the comparison is made on text -- casting the parameter to
# the enum type would mean naming that type here, and the name is the schema's
# business, not this script's.
STAGE_WHERE = """
      p."isArchived" = false
  AND p."isDeleted"  = false
  AND p."reviewStatus"::text = %(review_status)s
"""

# Optional tenant scope.
#
# NOT cosmetic. The Products page is tenant-scoped for anyone who is not a
# platform admin: routes/products.ts sets `tenantId = getAccessibleTenantIds(req)`
# for an OWNER, so its Review badge counts only the tenants that account can see.
# An unscoped query here counts every tenant in the database, which is how this
# script reported 1780 against a screen showing 1778 -- and it would have
# repriced products the operator cannot even open. Pass --tenant-id to match a
# particular account's view.
TENANT_CLAUSE = '  AND (%(tenants)s::uuid[] IS NULL OR p."tenantId" = ANY(%(tenants)s::uuid[]))'

SELECT_SQL = f"""
SELECT p.id,
       p.sku,
       p.title,
       p."tenantId",
       t.name                       AS tenant_name,
       b.name                       AS brand,
       p.price                      AS current_price,
       p."retailPrice"              AS retail_price,
       p."qualityGrading" ->> 'grade' AS grade,
       p."retailPriceBreakdown"       AS retail_breakdown_before,
       v.id                         AS variant_id,
       v."basePrice"                AS variant_base_price
  FROM "Product" p
  LEFT JOIN "Tenant" t ON t.id = p."tenantId"
  LEFT JOIN "Brand"  b ON b.id = p."brandId"
  LEFT JOIN "ProductVariant" v
         ON v."productId" = p.id AND v."isDefault" = true
 WHERE {STAGE_WHERE}
{TENANT_CLAUSE}
 {{over_retail}}
 ORDER BY p."createdAt" DESC
 {{limit}}
"""

COUNT_SQL = f"""
SELECT count(*) FROM "Product" p
 WHERE {STAGE_WHERE}
{TENANT_CLAUSE}
 {{over_retail}}
"""

# Product.price and Product.retailPrice are TEXT, not numeric -- they hold
# things like "38.99", "EUR 38.99" and (historically) "38,99". So any numeric
# comparison needs a cast, and a cast that meets one malformed row aborts the
# whole query. Hence the guard: clean the string, and only cast what actually
# looks like a number. Anything else becomes NULL and is counted separately
# rather than crashing the count or being silently treated as zero.
NUMERIC_CAST = """
        CASE
          WHEN {col} IS NULL THEN NULL
          WHEN regexp_replace(
                 CASE WHEN position(',' in {col}) > 0
                       AND position('.' in {col}) = 0
                      THEN replace({col}, ',', '.')
                      ELSE {col} END,
                 '[^0-9.]', '', 'g') ~ '^[0-9]+(\\.[0-9]+)?$'
          THEN regexp_replace(
                 CASE WHEN position(',' in {col}) > 0
                       AND position('.' in {col}) = 0
                      THEN replace({col}, ',', '.')
                      ELSE {col} END,
                 '[^0-9.]', '', 'g')::numeric
          ELSE NULL
        END"""

# Optional: only products whose selling price is STRICTLY ABOVE their stored
# retail price. Applied in SQL rather than filtered afterwards so a 4,700-product
# stage reads 704 rows, and the sheet holds those 704 instead of burying them
# among 4,000 skips.
OVER_RETAIL_CLAUSE = f"""
  AND ({NUMERIC_CAST.format(col='p.price')}) IS NOT NULL
  AND ({NUMERIC_CAST.format(col='p."retailPrice"')}) IS NOT NULL
  AND ({NUMERIC_CAST.format(col='p.price')})
    > ({NUMERIC_CAST.format(col='p."retailPrice"')})
"""

# How many products are in the state that prompted all of this: a selling price
# ABOVE the retail price it is supposed to be a discount on. Reported for the
# Review stage and for the whole catalog, because "should I run this" and "how
# big is the problem" are different questions.
STATS_SQL = f"""
WITH parsed AS (
  SELECT p.id,
         ({NUMERIC_CAST.format(col='p.price')})         AS sp,
         ({NUMERIC_CAST.format(col='p."retailPrice"')}) AS rp,
         ({STAGE_WHERE.strip()})                       AS in_review
    FROM "Product" p
   WHERE p."isArchived" = false AND p."isDeleted" = false
)
SELECT in_review,
       count(*)                                                       AS total,
       count(*) FILTER (WHERE sp IS NOT NULL AND rp IS NOT NULL
                          AND sp > rp)                                AS over_retail,
       count(*) FILTER (WHERE sp IS NOT NULL AND rp IS NOT NULL
                          AND sp >= rp * {HARD_MAX_RATIO})            AS at_or_over_95,
       count(*) FILTER (WHERE rp IS NULL)                             AS no_retail,
       count(*) FILTER (WHERE sp IS NULL)                             AS no_price,
       COALESCE(sum(sp - rp) FILTER (WHERE sp IS NOT NULL
                                       AND rp IS NOT NULL
                                       AND sp > rp), 0)               AS excess
  FROM parsed
 GROUP BY in_review
 ORDER BY in_review DESC
"""

# Per-tenant breakdown, printed on every run. This is what makes a count
# mismatch against the screen self-diagnosing rather than a mystery.
TENANT_BREAKDOWN_SQL = f"""
SELECT COALESCE(t.name, '(unknown tenant)') AS tenant_name,
       p."tenantId",
       count(*)                             AS n
  FROM "Product" p
  LEFT JOIN "Tenant" t ON t.id = p."tenantId"
 WHERE {STAGE_WHERE}
 GROUP BY 1, 2
 ORDER BY n DESC
"""

# Set-based updates: one round trip for the whole batch rather than one per
# product. This is the reason to go to the database directly -- 1,200 UPDATEs
# issued one at a time is exactly the slow path we are replacing.
UPDATE_PRODUCT_SQL = """
UPDATE "Product" AS p
   SET price = v.price, "updatedAt" = now()
  FROM (SELECT (value ->> 0)::uuid AS id, value ->> 1 AS price
          FROM jsonb_array_elements(%s::jsonb)) AS v
 WHERE p.id = v.id
"""

UPDATE_VARIANT_SQL = """
UPDATE "ProductVariant" AS pv
   SET "basePrice" = v.price, "updatedAt" = now()
  FROM (SELECT (value ->> 0)::uuid AS id, (value ->> 1)::numeric AS price
          FROM jsonb_array_elements(%s::jsonb)) AS v
 WHERE pv.id = v.id
"""

RESTORE_SQL = """
UPDATE "Product" AS p
   SET price = v.price,
       "retailPrice" = CASE WHEN v.restore_retail THEN v.retail
                            ELSE p."retailPrice" END,
       "retailPriceBreakdown" = CASE WHEN v.restore_retail THEN v.breakdown
                                     ELSE p."retailPriceBreakdown" END,
       "updatedAt" = now()
  FROM (SELECT (value ->> 0)::uuid AS id,
               value ->> 1         AS price,
               value ->> 2         AS retail,
               (value -> 3)        AS breakdown,
               (value ->> 4)::bool AS restore_retail
          FROM jsonb_array_elements(%s::jsonb)) AS v
 WHERE p.id = v.id
"""

RESTORE_VARIANT_SQL = """
UPDATE "ProductVariant" AS pv
   SET "basePrice" = v.price, "updatedAt" = now()
  FROM (SELECT (value ->> 0)::uuid AS id, (value ->> 1)::numeric AS price
          FROM jsonb_array_elements(%s::jsonb)) AS v
 WHERE pv.id = v.id
"""

# Only for rows where a Google Shopping lookup replaced the retail anchor.
# `retailPriceBreakdown` is written alongside it, because leaving the old
# breakdown in place would have it explaining a figure that is no longer there --
# review-verification.ts reads that column to report where a retail price came
# from, and a stale answer is worse than an unfamiliar one.
UPDATE_RETAIL_SQL = """
UPDATE "Product" AS p
   SET "retailPrice" = v.retail,
       "retailPriceBreakdown" = v.breakdown,
       "updatedAt" = now()
  FROM (SELECT (value ->> 0)::uuid AS id,
               value ->> 1         AS retail,
               (value -> 2)        AS breakdown
          FROM jsonb_array_elements(%s::jsonb)) AS v
 WHERE p.id = v.id
"""


COLUMNS: list[tuple[str, int]] = [
    ("Product ID", 38),
    ("SKU", 16),
    ("Title", 38),
    ("Tenant", 16),
    ("Grade", 7),
    ("Grade %", 9),
    ("Current Price", 14),
    ("Retail Price", 13),
    ("New Retail (Google)", 19),
    ("Retail Source", 15),
    ("Retail Samples", 14),
    ("Retail Used", 12),
    ("Expected Price", 14),
    ("Min Allowed", 12),
    ("Max Allowed", 12),
    ("Expected Range", 18),
    ("New Price", 11),
    ("Change", 10),
    ("Change %", 10),
    ("Variant Before", 14),
    ("Verdict", 15),
    ("Status", 14),
    ("Reason", 70),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CHANGED_FILL = PatternFill("solid", fgColor="FFF2CC")
SKIPPED_FILL = PatternFill("solid", fgColor="EDEDED")
MONEY = "#,##0.00"

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def money(value: Any) -> float | None:
    """Parse "38.99", "EUR 38.99", "38,99", a Decimal, or None.

    None rather than 0.0 on failure: a product with no price and a product
    priced at zero must not end up looking the same in the sheet.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = _NUM.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def charm99(value: float) -> float:
    """Keep the whole units, set the cents to .99.

    15.60 -> 15.99      23.40 -> 23.99      19.00 -> 19.99

    Rounds UP within the unit, so it never quietly discounts. Matches `charm99`
    in app/rules/pricing.py and in vnyx-api's product-pricing.ts, so all three
    agree on the figure for a given input.
    """
    if not value > 0:
        return 0.0
    return round(math.floor(value) + 0.99, 2)


def charm99_at_most(value: float) -> float:
    """The largest `n.99` that does not exceed `value`.

    For upper bounds, where rounding UP would breach the very ceiling the bound
    exists to enforce.
    """
    if value < 0.99:
        return 0.0
    whole = math.floor(value)
    candidate = round(whole + 0.99, 2)
    if candidate <= value + 1e-9:
        return candidate
    return round(max(whole - 1 + 0.99, 0.0), 2)


def retail_ceiling99(retail: float) -> float:
    """The highest .99 the retail invariant allows.

    HARD_MAX_RATIO is a ratio the price must stay STRICTLY under, so the cent
    taken off before flooring is what keeps the ceiling off the forbidden value
    itself -- without it a 20.00 retail yields exactly the 19.00 this excludes.
    """
    return charm99_at_most(retail * HARD_MAX_RATIO - 0.01)


def currency_symbol(raw: Any) -> str:
    """The symbol already on this product's stored price, if any.

    Preserved on write. vnyx-api's parsePrice infers a variant's baseCurrency
    from this symbol, defaulting to EUR when there is none -- so writing a bare
    figure over a "$12.00" price would silently re-denominate the product.
    A euro sign is dropped, because bare two decimals is the format
    normalizeStoredPrice writes and EUR is what a bare price already means.
    """
    text = "" if raw is None else str(raw)
    for symbol in ("$", "£"):
        if symbol in text:
            return symbol
    return ""


def short(value: Any, limit: int = 120) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


# --------------------------------------------------------------------------- #
# Google Shopping retail lookup
#
# Used only when the stored selling price is at or above the stored RETAIL price.
# That combination means one of the two figures is wrong, and the retail one is
# the usual culprit: it comes from an automated new-goods estimate that returns a
# low number when it finds no good comparables. Clamping the selling price down
# to fit a bad anchor throws away a legitimately valuable garment's price, so
# where a second opinion is available it is worth asking for.
#
# SerpApi's `google_shopping` engine is preferred because it returns
# `extracted_price` as a number. Google Programmable Search is the fallback and
# needs prices scraped out of snippets, which is markedly less reliable.
# --------------------------------------------------------------------------- #

RETAIL_SOURCE = "GOOGLE_SHOPPING"

# Which rows are worth spending a paid lookup on.
#
#   over_retail   (default) the selling price is STRICTLY ABOVE the stored retail
#                 price. That is the only state where the two figures actually
#                 contradict each other, so it is the only one where a second
#                 opinion on the anchor is warranted.
#   at_or_over_95 the above, plus prices within 5% of retail -- the `above_retail`
#                 verdict, which treats 95% of RRP as already impossible.
#   over_grade_max the above, plus anything over its grade band. Much broader: a
#                 garment asking 30.00 against a 39.00 retail is well over its
#                 band while still being cheaper than retail, so its anchor is
#                 not in question and the call is usually wasted.
#   all           every row that would change, including underpriced ones.
#
# A product with NO retail price at all qualifies in every mode: there is no
# anchor to doubt, only one to supply.
LOOKUP_MODES = ("over_retail", "at_or_over_95", "over_grade_max", "all")
HIGH_VERDICTS = {"above_retail", "too_high"}

# review-verification.ts maps a breakdown source of GRADE_MULTIPLIER -> derived
# and EBAY_NEW -> market, and anything else -> "unknown". So this new value is
# safe to store (no crash, no mislabelling) but will read as unknown provenance
# in the UI until that mapping learns about it.
MIN_SAMPLES = 2

# When to believe a quote over the retail price already stored.
#
#   always              (default) the quote wins whenever enough listings agree.
#                       Google Shopping is treated as the authority on what a
#                       garment retails for new, and the stored figure as the
#                       estimate it replaces.
#   higher_than_retail  only when the quote EXCEEDS the stored retail by
#                       MIN_QUOTE_GAIN_PCT -- i.e. only when it argues the anchor
#                       was too LOW.
#   higher_than_price   stricter still: it must also clear the selling price, so
#                       it fully resolves the above-retail contradiction.
#
# `always` is the default because it is the only rule that consistently answers
# "what does this actually retail for". Be aware of the direction it cuts: when
# Google says a garment retails for LESS than recorded, the selling price falls
# FURTHER than it would have, not less. A 180.99 item against a stored 159.99
# retail is clamped to 96.99; against Google's 112.01 it is clamped to 67.99.
# That is the honest consequence of trusting the better-sampled number.
QUOTE_RULES = ("always", "higher_than_retail", "higher_than_price")

# A quote a hair above the stored figure is noise, not evidence. Below this it is
# not worth rewriting an anchor and every price derived from it.
MIN_QUOTE_GAIN_PCT = 5.0

# How far a quote may sit from the stored retail before it is treated as a bad
# MATCH rather than a correction.
#
# Learned the hard way. A production run with no such guard wrote quotes like
# 35.99 -> 241.80 and 26.99 -> 6.25: Google Shopping had matched a vague garment
# title ("Vintage Beige Sweater Men") against unrelated products, and the median
# of 40 unrelated listings is a confident-looking number that means nothing.
# Sample count does not help -- those had 40 listings each.
#
# A genuine anchor correction is a factor of two or three at most. An order of
# magnitude is a different product. Set 0 to disable and take every quote.
MAX_QUOTE_RATIO = 3.0

# Errors that mean the BACKEND is unusable, not that this one product could not
# be found. A dead key or an exhausted quota returns the same answer for every
# remaining product, so continuing costs hundreds of requests to learn nothing.
FATAL_ERROR_MARKERS = (
    "http 401", "http 403",
    "quota", "exhausted", "run out of searches", "ran out of searches",
    "invalid api key", "invalid_api_key", "api key",
    "billing", "not enabled", "daily limit", "rate_limit_exceeded",
)

# Rate limits, IN TOTAL, before a backend is treated as spent.
#
# Deliberately not a consecutive streak. The first version of this counted
# consecutive 429s and reset on any success -- which with several workers in
# flight meant one lucky reply kept resetting the counter, and a real run took
# 1,459 rate limits without ever tripping. A quota ceiling produces 429s mixed
# with the occasional success, so the total is the signal and the streak is not.
#
# A handful of 429s is a transient burst worth riding out. This many means the
# ceiling is not going to move within the run, and the throttle cannot fix a
# quota -- only a smaller job or a bigger plan can.
RATE_LIMIT_GIVE_UP = 25

# Headroom over the eligible count when no explicit budget is given.
#
# A run needs slightly more calls than products, because a product whose primary
# lookup finds nothing falls through to the fallback backend and is billed twice.
# It must NOT need three or four times as many -- 443 products costing 1,715
# calls is a runaway, and a ceiling is what turns that into a stop rather than a
# bill. 443 eligible gives a budget of 488.
BUDGET_HEADROOM_PCT = 10
BUDGET_HEADROOM_MIN = 25

# Billable requests per second, across every worker thread. Deliberately modest:
# a 429 costs the call and returns nothing, so running under the ceiling is
# cheaper than discovering it. Set 0 to remove the throttle.
DEFAULT_LOOKUP_RATE = 3.0

_PRICE_IN_TEXT = re.compile(r"(?:EUR|USD|GBP|€|\$|£)\s?(\d{1,6}(?:[.,]\d{1,2})?)"
                            r"|(\d{1,6}(?:[.,]\d{1,2})?)\s?(?:EUR|USD|GBP|€|\$|£)")


@dataclass
class RetailQuote:
    """A second opinion on what a garment retails for, new."""

    retail: float | None
    samples: int
    source: str
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return round((ordered[mid - 1] + ordered[mid]) / 2, 2)


def is_fatal_error(message: str) -> bool:
    """Does this error mean "stop calling", rather than "this product failed"?"""
    low = (message or "").lower()
    return any(marker in low for marker in FATAL_ERROR_MARKERS)


def prices_from_text(text: str) -> list[float]:
    out: list[float] = []
    for whole, trailing in _PRICE_IN_TEXT.findall(text or ""):
        raw = whole or trailing
        try:
            value = float(raw.replace(",", "."))
        except ValueError:
            continue
        if 1.0 <= value <= 100000.0:
            out.append(value)
    return out


class RetailLookup:
    """Google-backed retail estimate, with a per-query cache.

    The cache matters: a warehouse holds many copies of the same garment, and
    the queue here is ordered by creation time, so identical titles arrive in
    runs. Every cache hit is a paid API call not made.
    """

    def __init__(self, serpapi_key: str = "", google_key: str = "",
                 google_cx: str = "", country: str = "nl",
                 timeout: float = 20.0, min_samples: int = MIN_SAMPLES,
                 rate: float = DEFAULT_LOOKUP_RATE,
                 retries: int = 2,
                 max_rate_limits: int = RATE_LIMIT_GIVE_UP,
                 cache_path: Path | None = None,
                 budget: int | None = None) -> None:
        # One key. Multi-key rotation was tried and removed: a second key is
        # another account to keep topped up, and when it is also out of searches
        # the run just pays twice to learn the same thing.
        self.serpapi_key = serpapi_key.strip()
        self.google_key = google_key
        self.google_cx = google_cx
        self.country = country
        self.timeout = timeout
        self.min_samples = max(1, min_samples)
        self.retries = max(0, retries)
        self.max_rate_limits = max(1, max_rate_limits)
        self.budget = budget          # billable calls this run may make, total
        self.budget_hit = False
        self._cache: dict[str, RetailQuote] = {}
        self.cache_path = cache_path
        self.disk_hits = 0
        self._load_cache()
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self._interval = 0.0 if rate <= 0 else 1.0 / rate

        self.calls = 0            # every billable request, both backends
        self.serpapi_calls = 0
        self.fallback_calls = 0
        self.cache_hits = 0
        self.rate_limited = 0     # HTTP 429s seen, before any retry

        # Backends switched off mid-run, and why. Once a key is dead or a quota
        # is spent every remaining product gets the same answer, so the only
        # thing more calls buy is a bigger bill.
        self.disabled: dict[str, str] = {}
        self._429_total: dict[str, int] = {}
        self.skipped_calls = 0    # calls NOT made because a backend was disabled
        self.replaced_so_far = 0  # for the progress line

    def _note_failure(self, backend: str, error: str) -> None:
        """Trip the breaker when an error says the backend itself is done."""
        # Order matters. SerpApi reports an exhausted account WITH a 429 status
        # ("Your account has run out of searches"), so a status-first check
        # classified a spent quota as a transient rate limit and sat through 24
        # more pointless calls before giving up. What the body SAYS decides.
        fatal = is_fatal_error(error)
        with self._lock:
            if fatal:
                if backend not in self.disabled:
                    self.disabled[backend] = error
                return
            if "429" in error:
                seen = self._429_total.get(backend, 0) + 1
                self._429_total[backend] = seen
                if seen >= self.max_rate_limits and backend not in self.disabled:
                    self.disabled[backend] = (
                        f"{seen} rate limits (HTTP 429) -- the quota or plan "
                        f"ceiling will not clear during this run, and the "
                        f"throttle cannot fix a quota")

    def _note_success(self, backend: str) -> None:
        # Deliberately does NOT reset the 429 count -- see RATE_LIMIT_GIVE_UP.
        return

    def _throttle(self) -> None:
        """Space requests out across every worker thread.

        `--lookup-workers 4` on its own is not a rate limit: four threads each
        finishing in 200ms is 20 requests a second, which is enough to get a
        429 on most plans -- and a 429 costs the call without returning an
        answer. One shared slot time, guarded by a lock, keeps the whole run
        under a stated ceiling however many workers are running.
        """
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next_slot - now
            self._next_slot = max(now, self._next_slot) + self._interval
        if wait > 0:
            time.sleep(wait)

    def _get_json(self, url: str, backend: str) -> tuple[dict[str, Any] | None,
                                                         str | None]:
        """One throttled, retrying GET. Returns (payload, error).

        A 429 or a 5xx is retried with a widening backoff: transient, and losing
        the lookup means the product keeps a retail price we already believe is
        wrong. A 4xx other than 429 is not retried -- a bad key or a malformed
        query fails identically every time, and retrying just spends the quota.
        """
        import json as _json
        import urllib.error
        import urllib.request

        # Refuse before spending anything: budget first, then the breaker.
        with self._lock:
            over = self.budget is not None and self.calls >= self.budget
            if over:
                self.budget_hit = True
                self.skipped_calls += 1
                spent, cap = self.calls, self.budget
        if over:
            return None, (f"call budget reached ({spent}/{cap}) -- no further "
                          "lookups were bought")

        with self._lock:
            dead = self.disabled.get(backend)
            if dead:
                self.skipped_calls += 1
        if dead:
            return None, f"{backend} disabled for this run ({dead})"

        last = "unknown error"
        for attempt in range(self.retries + 1):
            self._throttle()
            try:
                with self._lock:
                    self.calls += 1
                    if backend == "serpapi":
                        self.serpapi_calls += 1
                    else:
                        self.fallback_calls += 1
                with urllib.request.urlopen(url, timeout=self.timeout) as r:
                    payload = _json.loads(r.read().decode("utf-8", "replace"))
                self._note_success(backend)
                return payload, None
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:160]
                except Exception:
                    pass
                last = f"HTTP {exc.code}" + (f": {short(body, 120)}" if body else "")
                if exc.code == 429:
                    with self._lock:
                        self.rate_limited += 1
                    last = f"HTTP 429 rate limited: {short(body, 100)}"
                retryable = exc.code == 429 or exc.code >= 500
            except urllib.error.URLError as exc:
                last = f"network error: {short(str(exc.reason), 100)}"
                retryable = True
            except Exception as exc:
                last = f"{type(exc).__name__}: {short(str(exc), 100)}"
                retryable = False

            if not retryable or attempt == self.retries:
                self._note_failure(backend, last)
                return None, last
            time.sleep(1.5 * (attempt + 1))
        self._note_failure(backend, last)
        return None, last

    # --- persistence ------------------------------------------------------ #
    #
    # The in-memory cache only ever helped WITHIN a run. An aborted run threw
    # away every answer it had paid for, so re-running re-bought the lot: 1,715
    # billed calls discarded because of one keystroke at the confirmation prompt.
    #
    # Successes are cached, and so are "nothing found for this title" results --
    # that is a property of the query and will not change between runs. Transport
    # and quota failures are NOT cached: those describe the state of the API, not
    # of the product, and caching them would poison the next run.

    def _load_cache(self) -> None:
        if not self.cache_path or not self.cache_path.exists():
            return
        try:
            import json as _json
            raw = _json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:                    # corrupt or unreadable
            print(f"   (ignoring unreadable lookup cache {self.cache_path}: "
                  f"{short(str(exc), 60)})")
            return
        for key, item in (raw.get("quotes") or {}).items():
            self._cache[key] = RetailQuote(
                retail=item.get("retail"), samples=int(item.get("samples") or 0),
                source=str(item.get("source") or "cache"),
                detail=item.get("detail") or {}, error=item.get("error"),
            )

    def save_cache(self) -> None:
        if not self.cache_path:
            return
        import json as _json
        payload = {"quotes": {
            key: {"retail": q.retail, "samples": q.samples, "source": q.source,
                  "detail": q.detail, "error": q.error}
            for key, q in self._cache.items()
        }}
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(_json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            print(f"   (could not save the lookup cache: {short(str(exc), 60)})")

    @staticmethod
    def _worth_caching(quote: RetailQuote) -> bool:
        """A verdict about the PRODUCT is worth keeping; one about the API is not."""
        if quote.retail is not None:
            return True
        error = (quote.error or "").lower()
        if is_fatal_error(error) or "429" in error or "disabled for this run" in error:
            return False
        if "network error" in error or "timed out" in error or "http 5" in error:
            return False
        # "no shopping results", "only N carried a usable price" -- about the query.
        return True

    @property
    def available(self) -> bool:
        return bool(self.serpapi_key or (self.google_key and self.google_cx))

    def describe(self) -> str:
        if self.serpapi_key:
            return "SerpApi google_shopping" + (
                " (Google Programmable Search as fallback)"
                if self.google_key and self.google_cx else "")
        if self.google_key and self.google_cx:
            return "Google Programmable Search"
        return "(no keys configured)"

    def query_for(self, row: "Row") -> str:
        """Brand plus title. The brand is prepended when the title omits it --
        a bare "Vintage Beige Sweater Men" matches almost anything."""
        title = (row.title or "").strip()
        brand = (row.brand or "").strip()
        if brand and brand.lower() not in title.lower():
            return f"{brand} {title}".strip()
        return title

    def lookup(self, row: "Row") -> RetailQuote:
        query = self.query_for(row)
        if not query:
            return RetailQuote(None, 0, "none", error="no title to search on")
        key = query.lower()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self.cache_hits += 1
        if cached is not None:
            return cached

        quote = self._fetch(query)
        if self._worth_caching(quote):
            with self._lock:
                self._cache[key] = quote
        return quote

    def _fetch(self, query: str) -> RetailQuote:
        """Try SerpApi, then Google as a fallback.

        NOTE: a fallback means TWO billable calls for one product, which is why
        `paid API calls made` can exceed `products eligible`. Both failures are
        reported together -- knowing only that the fallback failed hides the
        reason the primary did.
        """
        primary: RetailQuote | None = None
        if self.serpapi_key:
            primary = self._serpapi(query)
            if primary.retail is not None:
                return primary
            if not (self.google_key and self.google_cx):
                return primary

        if self.google_key and self.google_cx:
            second = self._google_cse(query)
            if second.retail is None and primary is not None:
                second.error = (f"serpapi: {primary.error} | then fallback "
                                f"google_cse: {second.error}")
            return second

        return RetailQuote(None, 0, "none", error="no API keys configured")

    def serpapi_quota(self) -> dict[str, Any] | None:
        """What SerpApi says is left on the key. None if it cannot be asked.

        `/account` is free -- it does not consume a search -- so this costs
        nothing and answers the question that otherwise takes ten products and a
        confusing pile of 429s to establish. SerpApi reports an exhausted account
        as an HTTP 429 on the SEARCH endpoint, which reads identically to being
        rate limited; the account endpoint distinguishes the two outright.
        """
        if not self.serpapi_key:
            return None

        import json as _json
        import urllib.error
        import urllib.parse
        import urllib.request

        url = ("https://serpapi.com/account?"
               + urllib.parse.urlencode({"api_key": self.serpapi_key}))
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return _json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:160]
            except Exception:
                pass
            return {"_error": f"HTTP {exc.code}"
                              + (f": {short(body, 120)}" if body else "")}
        except Exception as exc:
            return {"_error": f"{type(exc).__name__}: {short(str(exc), 90)}"}

    def preflight(self) -> Any:
        """Report the allowance, and switch the backend off if there is none.

        Disabling up front is the point: without it the run pays the ceiling in
        429s to rediscover something the account endpoint states plainly. Note
        the free plan is 250 searches a MONTH, which is well under the number of
        products a full run wants -- worth seeing before starting, not after.
        """
        quota = self.serpapi_quota()
        if quota is None:
            return None

        if "_error" in quota:
            print(f"   serpapi quota check failed ({quota['_error']}) -- "
                  "continuing anyway.")
            return None

        left = quota.get("total_searches_left")
        if left is None:
            left = quota.get("plan_searches_left")
        plan = quota.get("plan_name") or quota.get("plan_id") or "unknown plan"
        per_month = quota.get("searches_per_month")
        used = quota.get("this_month_usage")

        summary = f"   serpapi: {plan}"
        if per_month is not None:
            summary += f", {per_month}/month"
        if used is not None:
            summary += f", {used} used"
        if left is not None:
            summary += f", {left} left"
        print(summary)

        if isinstance(left, (int, float)) and left <= 0:
            reason = (f"the key has 0 searches left ({plan}"
                      + (f", {per_month}/month" if per_month else "") + ")")
            with self._lock:
                self.disabled["serpapi"] = reason
            print("   serpapi is OUT OF SEARCHES -- skipped entirely this run "
                  "rather than spending")
            print("   the rate-limit ceiling to rediscover it. The grade-window "
                  "repricing below is")
            print("   unaffected: it uses no API at all.")
        return left

    def _serpapi(self, query: str) -> RetailQuote:
        import urllib.parse

        params = urllib.parse.urlencode({
            "api_key": self.serpapi_key,
            "engine": "google_shopping",
            "q": query,
            "hl": self.country,
            "gl": self.country,
            "num": "20",
        })
        url = f"https://serpapi.com/search.json?{params}"
        data, error = self._get_json(url, "serpapi")
        if error:
            return RetailQuote(None, 0, "serpapi", error=error)
        assert data is not None

        # SerpApi reports quota and query problems in a 200 body.
        if data.get("error"):
            message = f"serpapi said: {short(data['error'], 110)}"
            # Quota and key problems come back as HTTP 200 with an error body,
            # so they have to be classified here rather than in _get_json.
            self._note_failure("serpapi", message)
            return RetailQuote(None, 0, "serpapi", error=message)

        results = data.get("shopping_results")
        if not results:
            return RetailQuote(None, 0, "serpapi",
                               error="google_shopping returned no shopping "
                                     "results for this title")

        found: list[float] = []
        titles: list[str] = []
        for item in (data.get("shopping_results") or []):
            value = item.get("extracted_price")
            if value is None:
                value = next(iter(prices_from_text(str(item.get("price") or ""))), None)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if 1.0 <= number <= 100000.0:
                found.append(number)
                titles.append(short(item.get("title"), 60))

        if len(found) < self.min_samples:
            return RetailQuote(
                None, len(found), "serpapi",
                error=f"{len(results)} shopping result(s) but only "
                      f"{len(found)} carried a usable price; "
                      f"{self.min_samples} needed")

        return RetailQuote(
            retail=round(_median(found), 2), samples=len(found), source="serpapi",
            detail={"query": query, "engine": "google_shopping",
                    "prices": found[:20], "min": min(found), "max": max(found),
                    "examples": titles[:3]},
        )

    def _google_cse(self, query: str) -> RetailQuote:
        import urllib.parse

        params = urllib.parse.urlencode({
            "key": self.google_key,
            "cx": self.google_cx,
            "q": query,
            "num": "10",
        })
        url = f"https://www.googleapis.com/customsearch/v1?{params}"
        data, error = self._get_json(url, "google_cse")
        if error:
            return RetailQuote(None, 0, "google_cse", error=error)
        assert data is not None

        items = data.get("items") or []
        if not items:
            return RetailQuote(None, 0, "google_cse",
                               error="custom search returned no results")

        found: list[float] = []
        for item in items:
            blob = " ".join(str(item.get(k) or "")
                            for k in ("title", "snippet", "htmlSnippet"))
            found.extend(prices_from_text(blob))

        if len(found) < self.min_samples:
            return RetailQuote(
                None, len(found), "google_cse",
                error=f"{len(items)} search result(s) but only {len(found)} "
                      f"price(s) could be read out of the snippets; "
                      f"{self.min_samples} needed")
        return RetailQuote(
            retail=round(_median(found), 2), samples=len(found),
            source="google_cse",
            detail={"query": query, "engine": "customsearch",
                    "prices": found[:20], "min": min(found), "max": max(found)},
        )


@dataclass
class Row:
    product_id: str
    sku: str
    title: str
    tenant: str
    brand: str
    grade: str | None
    current: float | None
    retail: float | None
    variant_id: str | None
    variant_before: float | None
    raw_price: Any
    raw_retail: Any = None
    breakdown_before: Any = None
    pct: float | None = None
    expected: float | None = None
    min_allowed: float | None = None
    max_allowed: float | None = None
    new_price: float | None = None
    verdict: str = ""
    status: str = "preview"
    reason: str = ""

    # Google Shopping retail lookup, when one was run for this row.
    retail_before: float | None = None
    new_retail: float | None = None
    retail_source: str = ""
    retail_samples: int | None = None
    retail_note: str = ""
    retail_breakdown: dict[str, Any] | None = None

    @property
    def retail_replaced(self) -> bool:
        return self.new_retail is not None

    @property
    def expected_range(self) -> str:
        if self.min_allowed is None or self.max_allowed is None:
            return ""
        return f"{self.min_allowed:.2f} - {self.max_allowed:.2f}"

    @property
    def delta(self) -> float | None:
        if self.new_price is None or self.current is None:
            return None
        return round(self.new_price - self.current, 2)

    @property
    def delta_pct(self) -> float | None:
        if self.delta is None or not self.current:
            return None
        return round(self.delta / self.current * 100, 2)

    @property
    def writable(self) -> bool:
        return self.new_price is not None and self.status in ("preview", "updated")

    def price_string(self) -> str:
        assert self.new_price is not None
        return f"{currency_symbol(self.raw_price)}{self.new_price:.2f}"

    def cells(self) -> list[Any]:
        return [
            self.product_id, self.sku, self.title, self.tenant,
            self.grade or "", None if self.pct is None else self.pct,
            self.current, self.retail_before if self.retail_replaced
            else self.retail, self.new_retail, self.retail_source,
            self.retail_samples, self.retail,
            self.expected, self.min_allowed, self.max_allowed,
            self.expected_range, self.new_price, self.delta, self.delta_pct,
            self.variant_before, self.verdict, self.status,
            (self.retail_note + " " + self.reason).strip(),
        ]


@dataclass
class Totals:
    seen: int = 0
    to_write: int = 0
    updated: int = 0
    variants_updated: int = 0
    movement: float = 0.0
    skipped: dict[str, int] = field(default_factory=dict)
    grades: dict[str, int] = field(default_factory=dict)
    verdicts: dict[str, int] = field(default_factory=dict)
    retail_eligible: int = 0
    retail_calls: int = 0
    retail_serpapi_calls: int = 0
    retail_fallback_calls: int = 0
    retail_rate_limited: int = 0
    retail_cache_hits: int = 0
    retail_seconds: float = 0.0
    retail_skipped_calls: int = 0
    retail_cached_total: int = 0
    retail_budget: int = 0
    retail_budget_hit: bool = False
    retail_quota_left: Any = None
    retail_disabled: dict[str, str] = field(default_factory=dict)
    retail_errors: dict[str, int] = field(default_factory=dict)
    retail_replaced: int = 0
    retail_rejected: int = 0
    retail_failed: int = 0
    retails_written: int = 0

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def price_row(record: dict[str, Any], totals: Totals,
              tolerance: float = DEFAULT_TOLERANCE,
              min_price: float = DEFAULT_MIN_PRICE) -> Row:
    """Turn one database row into a decision. Pure -- no I/O, no writes."""
    row = Row(
        product_id=str(record["id"]),
        sku=short(record.get("sku"), 40),
        title=short(record.get("title"), 70),
        tenant=short(record.get("tenant_name"), 40),
        brand=short(record.get("brand"), 40),
        grade=(record.get("grade") or None),
        current=money(record.get("current_price")),
        retail=money(record.get("retail_price")),
        variant_id=None if record.get("variant_id") is None
        else str(record["variant_id"]),
        variant_before=money(record.get("variant_base_price")),
        raw_price=record.get("current_price"),
        raw_retail=record.get("retail_price"),
        breakdown_before=record.get("retail_breakdown_before"),
    )

    grade = (row.grade or "").strip().upper()
    row.grade = grade or None
    totals.grades[grade or "(none)"] = totals.grades.get(grade or "(none)", 0) + 1

    if not grade:
        row.status = "skipped"
        row.reason = ("No grade on the product (qualityGrading->>'grade' is "
                      "empty), so no percentage applies.")
        totals.skip("no grade")
        return row

    if grade not in GRADE_PCT:
        row.status = "skipped"
        row.reason = (f"Grade {grade!r} is not in the fixed table "
                      f"({'/'.join(GRADE_PCT)}).")
        totals.skip(f"unknown grade {grade}")
        return row

    row.pct = GRADE_PCT[grade]

    if row.retail is None or row.retail <= 0:
        row.status = "skipped"
        row.reason = ("No usable retailPrice to take a percentage of "
                      f"(stored: {short(record.get('retail_price'), 30)!r}).")
        totals.skip("no retail price")
        return row

    row.expected = round(row.retail * row.pct, 2)

    # 0.99 is the smallest charm price there is, so anything targeting less than
    # 1.00 cannot be expressed without rounding UP past it -- grade D of a 3.00
    # retail targets 0.50 and would be written as 0.99, double the intent.
    # Skipped rather than written: doubling a price is not a rounding.
    if row.expected < 1.0 and row.expected < min_price:
        row.status = "skipped"
        row.reason = (f"{row.pct:.2%} of {row.retail:.2f} is {row.expected:.2f}. "
                      "The smallest charm price is 0.99, so pricing this would "
                      "round UP past the target rather than to it.")
        totals.skip("target below 1.00")
        return row

    # The window. Both ends charmed, the top additionally held under the retail
    # ceiling, because charming UP must not carry a bound past the invariant it
    # exists to enforce.
    row.min_allowed = charm99(max(round(row.expected * (1 - tolerance), 2),
                                  min_price))
    row.max_allowed = min(charm99(round(row.expected * (1 + tolerance), 2)),
                          retail_ceiling99(row.retail))

    if row.min_allowed > row.max_allowed:
        # Reachable on a small retail, where the charmed floor lands above the
        # 95% ceiling. No bound satisfies both, so it reports instead of guessing.
        row.status = "skipped"
        row.reason = (f"The charmed floor {row.min_allowed:.2f} is above the "
                      f"retail ceiling {row.max_allowed:.2f}; no price satisfies "
                      "both.")
        totals.skip("window inverted")
        return row

    ceiling = round(row.retail * HARD_MAX_RATIO, 2)

    if row.current is None or row.current <= 0:
        row.verdict = "no_price"
        row.new_price = min(max(charm99(row.expected), row.min_allowed),
                            row.max_allowed)
        row.reason = (f"No price stored. Grade {grade} targets {row.pct:.2%} of "
                      f"{row.retail:.2f} = {row.expected:.2f}.")
    elif row.current >= ceiling:
        # Checked before the window: a used item priced at its own RRP is wrong
        # no matter where the grade band happens to fall.
        row.verdict = "above_retail"
        row.new_price = row.max_allowed
        row.reason = (f"{row.current:.2f} is {row.current / row.retail:.0%} of "
                      f"retail; nothing may reach {HARD_MAX_RATIO:.0%}. Lowered "
                      "to the grade maximum.")
    elif row.current < row.min_allowed:
        row.verdict = "too_low"
        row.new_price = row.min_allowed
        row.reason = (f"{row.current:.2f} is below the {row.min_allowed:.2f} "
                      f"grade {grade} minimum. Raised to the minimum.")
    elif row.current > row.max_allowed:
        row.verdict = "too_high"
        row.new_price = row.max_allowed
        row.reason = (f"{row.current:.2f} is above the {row.max_allowed:.2f} "
                      f"grade {grade} maximum. Lowered to the maximum.")
    else:
        # In range, so the ratio is right and no clamp is warranted -- the
        # correction is always the smallest one that makes the record right. The
        # cents can still be wrong: 18.40 passes its window and is exactly the
        # ending charm pricing exists to remove. Capped at max_allowed so
        # rounding up cannot carry it out of the window it just passed.
        rounded = min(charm99(row.current), row.max_allowed)
        if abs(rounded - row.current) <= 0.005:
            row.verdict = "ok"
            row.status = "already correct"
            row.reason = (f"{row.current:.2f} is inside the grade {grade} window "
                          f"{row.min_allowed:.2f}-{row.max_allowed:.2f} and "
                          "already ends .99.")
            totals.verdicts["ok"] = totals.verdicts.get("ok", 0) + 1
            totals.skip("already correct")
            return row
        row.verdict = "round_required"
        row.new_price = rounded
        row.reason = (f"{row.current:.2f} is inside the grade {grade} window "
                      f"{row.min_allowed:.2f}-{row.max_allowed:.2f}, so the "
                      "ratio is right. Rounded for a .99 ending.")

    totals.verdicts[row.verdict] = totals.verdicts.get(row.verdict, 0) + 1
    totals.to_write += 1
    if row.delta is not None:
        totals.movement += row.delta
    return row


def fetch(dsn: str, limit: int | None, tenants: list[str] | None,
          review_status: str, only_over_retail: bool = False
          ) -> tuple[list[dict[str, Any]], int, list[tuple[str, str, int]],
                     list[dict[str, Any]]]:
    """Read the queue, plus the counts needed to explain its size.

    The per-tenant breakdown is ALWAYS unscoped, so a total that disagrees with
    the screen can be attributed to a tenant rather than guessed at.
    """
    args = {"tenants": tenants, "review_status": review_status}
    clause = OVER_RETAIL_CLAUSE if only_over_retail else ""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(COUNT_SQL.format(over_retail=clause), args)
            total = int((cur.fetchone() or [0])[0])

            cur.execute(TENANT_BREAKDOWN_SQL, args)
            breakdown = [(str(r[0]), str(r[1]), int(r[2])) for r in cur.fetchall()]

            cur.execute(STATS_SQL, args)
            stats = [dict(zip([d.name for d in (cur.description or [])], r))
                     for r in cur.fetchall()]

            sql = SELECT_SQL.format(
                over_retail=clause,
                limit="" if limit is None else f"LIMIT {int(limit)}")
            cur.execute(sql, args)
            names = [d.name for d in (cur.description or [])]
            records = [dict(zip(names, r)) for r in cur.fetchall()]
    return records, total, breakdown, stats


def write(dsn: str, rows: list[Row], totals: Totals) -> None:
    """Apply every writable row in ONE transaction.

    All or nothing on purpose. A partial commit would leave Product.price and
    ProductVariant.basePrice disagreeing for some products, which is a worse
    state to be in than not having run at all -- and harder to find afterwards.
    """
    targets = [r for r in rows if r.writable]
    if not targets:
        return

    product_pairs = [[r.product_id, r.price_string()] for r in targets]
    variant_pairs = [[r.variant_id, f"{r.new_price:.2f}"]
                     for r in targets if r.variant_id]

    import json

    # Rows whose anchor Google replaced. Written in the SAME transaction as the
    # prices derived from them: a committed retail beside an uncommitted price
    # (or the reverse) is a product whose two figures contradict each other, and
    # nothing downstream could tell which one to believe.
    retail_rows = [[r.product_id, f"{r.new_retail:.2f}", r.retail_breakdown]
                   for r in targets if r.retail_replaced]

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(UPDATE_PRODUCT_SQL, (json.dumps(product_pairs),))
            totals.updated = cur.rowcount
            if variant_pairs:
                cur.execute(UPDATE_VARIANT_SQL, (json.dumps(variant_pairs),))
                totals.variants_updated = cur.rowcount
            if retail_rows:
                cur.execute(UPDATE_RETAIL_SQL, (json.dumps(retail_rows),))
                totals.retails_written = cur.rowcount
        conn.commit()

    for row in targets:
        row.status = "updated"
        if row.variant_id is None:
            row.reason += (" Product.price written; this product has no default "
                           "ProductVariant, so there was no mirror to update.")


def relookup_retail(rows: list[Row], lookup: RetailLookup, totals: Totals,
                    tolerance: float, min_price: float, workers: int,
                    mode: str = "over_retail",
                    rule: str = "always",
                    min_gain: float = MIN_QUOTE_GAIN_PCT,
                    every: int = 1,
                    max_ratio: float = MAX_QUOTE_RATIO) -> None:
    """Ask Google for a retail price where the stored one cannot be right.

    Which rows qualify is set by `mode` -- see LOOKUP_MODES. The default asks
    only about products whose selling price is STRICTLY ABOVE their stored retail
    price, plus products with no retail price at all. Those are the two states
    where the stored anchor is either contradicted or absent; anywhere else it is
    merely a number we might disagree with, which is not worth a paid call.

    A quote is only ACCEPTED when it is above the current selling price. If
    Google agrees the garment retails for less than we are asking, the stored
    anchor was not the problem, and replacing one low number with another would
    just relabel the same clamp.
    """
    def missing_retail(row: Row) -> bool:
        return row.status == "skipped" and "retailPrice" in row.reason

    def qualifies(row: Row) -> bool:
        # No retail at all always qualifies, in every mode: there is no anchor to
        # doubt, only one to supply.
        if missing_retail(row):
            return True
        if mode == "over_retail":
            # Strictly above, measured against the two stored figures rather
            # than against a verdict. The `above_retail` verdict fires at 95% of
            # retail, which includes prices that are still BELOW it -- and a
            # price under its own retail is not a contradiction, so it is not
            # evidence that the anchor is wrong.
            return (row.current is not None and row.retail is not None
                    and row.current > row.retail)
        if mode == "at_or_over_95":
            return row.verdict == "above_retail"
        if mode == "over_grade_max":
            return row.verdict in HIGH_VERDICTS
        return row.writable          # mode == "all"

    targets = [r for r in rows if qualifies(r)]

    # Say why NOT, on every other row. Without this a blank Google column is
    # ambiguous -- no lookup, a lookup that found nothing, and a quote that was
    # found and rejected all look identical in the sheet.
    for row in rows:
        if row in targets or row.retail_note:
            continue
        if row.current is not None and row.retail is not None \
                and row.current <= row.retail:
            row.retail_note = (
                f"[google: not looked up -- the {row.current:.2f} selling price is "
                f"already below the {row.retail:.2f} retail price, so the two do "
                "not contradict each other]")
        elif row.verdict in ("ok", ""):
            row.retail_note = "[google: not needed -- price already correct]"
        elif row.verdict in HIGH_VERDICTS:
            row.retail_note = (
                f"[google: not looked up -- --lookup-when {mode} excludes "
                f"{row.verdict}]")
        else:
            row.retail_note = f"[google: not looked up -- verdict {row.verdict}]"

    if not targets:
        return

    print()
    cap = (f", hard cap {lookup.budget} billable calls" if lookup.budget
           else ", NO call cap")
    print(f"Looking up retail for {len(targets)} product(s) via "
          f"{lookup.describe()}{cap}")
    print()

    def run(row: Row) -> Row:
        quote = lookup.lookup(row)
        row.retail_source = quote.source
        row.retail_samples = quote.samples or None

        if quote.retail is None:
            # The query is included because it is the single most useful thing
            # for judging a failure: a wrong-looking search explains a missing
            # price far better than the error string does.
            row.retail_note = (
                f"[google: NO RETAIL FOUND. searched \"{lookup.query_for(row)}\" "
                f"via {quote.source} -- {quote.error}]")
            totals.retail_failed += 1
            return row

        # Does this quote justify replacing the anchor?
        reject: str | None = None
        stored = row.retail

        # Outlier first: an implausible quote is a bad match, and no acceptance
        # rule should be able to wave it through.
        if max_ratio > 0 and stored is not None and stored > 0:
            ratio = quote.retail / stored
            if ratio > max_ratio or ratio < 1.0 / max_ratio:
                reject = (f"it is {ratio:.1f}x the stored {stored:.2f} retail, "
                          f"outside the {max_ratio:g}x plausibility band -- this "
                          f"looks like a different product, not a correction")
        if rule == "higher_than_price" and row.current is not None \
                and quote.retail <= row.current:
            reject = (f"it does not clear the {row.current:.2f} asking price, so "
                      "it would not resolve the contradiction")
        elif rule == "always":
            pass
        elif stored is not None and stored > 0:
            gain = (quote.retail / stored - 1) * 100
            if gain < min_gain:
                reject = (f"it is only {gain:+.1f}% against the stored "
                          f"{stored:.2f} retail, under the {min_gain:.0f}% needed "
                          "to call the anchor wrong")

        if reject:
            row.retail_note = (
                f"[google: {quote.retail:.2f} from {quote.samples} listing(s), "
                f"NOT used -- {reject}]")
            totals.retail_rejected += 1
            return row

        row.retail_before = row.retail
        row.new_retail = quote.retail
        row.retail_breakdown = {
            "retailPriceEur": quote.retail,
            "source": RETAIL_SOURCE,
            "detail": {**quote.detail, "samples": quote.samples,
                       "replaced": row.retail_before,
                       "writtenBy": "scripts/reprice_review_db.py"},
            "skipped": {},
            "computedAt": datetime.now().astimezone().isoformat(),
        }
        totals.retail_replaced += 1
        with lookup._lock:
            lookup.replaced_so_far += 1
        return row

    totals.retail_eligible = len(targets)
    started = time.monotonic()
    done = [0]
    progress_lock = threading.Lock()
    total = len(targets)
    width = len(str(total))
    budget = lookup.budget

    def outcome(row: Row) -> str:
        """One line describing what happened to THIS product."""
        if row.retail_replaced:
            before = ("none" if row.retail_before is None
                      else f"{row.retail_before:.2f}")
            return (f"retail {before} -> {row.new_retail:.2f}  "
                    f"({row.retail_samples or 0} listings)")
        note = row.retail_note or ""
        if "NOT used" in note:
            return "quote found but not used"
        # Trim the bracketed note down to its reason.
        reason = note.split(" -- ", 1)[-1].rstrip("]") if " -- " in note else note
        return f"no retail: {short(reason, 62)}"

    def run_with_progress(row: Row) -> Row:
        out = run(row)
        with progress_lock:
            done[0] += 1
            n = done[0]
            credits = lookup.calls
            spent = (f"{credits}/{budget}" if budget else str(credits))
            if every > 0 and (n % every == 0 or n == total):
                label = (out.sku or out.product_id)[:14]
                print(f"   [{n:>{width}}/{total}] {label:<14} "
                      f"{outcome(out):<74} credits {spent}", flush=True)
        return out

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        list(pool.map(run_with_progress, targets))
    totals.retail_seconds = time.monotonic() - started
    totals.retail_calls = lookup.calls
    totals.retail_serpapi_calls = lookup.serpapi_calls
    totals.retail_fallback_calls = lookup.fallback_calls
    totals.retail_rate_limited = lookup.rate_limited
    totals.retail_cache_hits = lookup.cache_hits
    totals.retail_skipped_calls = lookup.skipped_calls
    totals.retail_disabled = dict(lookup.disabled)
    totals.retail_cached_total = len(lookup._cache)
    totals.retail_budget = lookup.budget or 0
    totals.retail_budget_hit = lookup.budget_hit

    # Saved even when the run is later aborted at the confirmation prompt: the
    # answers were paid for, and the point is that they survive the keystroke.
    lookup.save_cache()

    # Group the failures. 460 rows each saying "quota exhausted" is one problem
    # with one fix, and it should read that way rather than as 460 problems.
    for row in targets:
        if row.new_retail is not None or "NO RETAIL FOUND" not in row.retail_note:
            continue
        reason = row.retail_note.split(" -- ", 1)[-1].rstrip("]")
        key = short(reason, 90)
        totals.retail_errors[key] = totals.retail_errors.get(key, 0) + 1

    # Re-price every row whose anchor moved, from scratch, against the new
    # retail. Done as a second pass rather than inline so the price and the
    # window in the sheet always describe the SAME anchor -- a row showing a new
    # retail beside a window derived from the old one is unreadable.
    for row in targets:
        if not row.retail_replaced:
            continue
        before = row.retail_note
        fresh = price_row({
            "id": row.product_id, "sku": row.sku, "title": row.title,
            "tenantId": "", "tenant_name": row.tenant, "brand": row.brand,
            "current_price": row.raw_price,
            "retail_price": f"{row.new_retail:.2f}",
            "grade": row.grade, "variant_id": row.variant_id,
            "variant_base_price": row.variant_before,
        }, Totals(), tolerance, min_price)

        row.retail = fresh.retail
        row.expected = fresh.expected
        row.min_allowed = fresh.min_allowed
        row.max_allowed = fresh.max_allowed
        row.new_price = fresh.new_price
        row.verdict = fresh.verdict
        row.status = fresh.status
        row.reason = fresh.reason
        row.retail_note = (
            f"[google: retail {row.retail_before if row.retail_before else 'none'}"
            f" -> {row.new_retail:.2f} from {row.retail_samples} listing(s) via "
            f"{row.retail_source}]")
        if before and "NOT used" in before:
            row.retail_note = before


def write_undo(path: Path, rows: list[Row]) -> int:
    """Record exactly what to put back, for every row about to be written.

    Written BEFORE the transaction commits, and kept whether or not it succeeds.
    The spreadsheet is a readable audit trail but a lossy one -- it cannot hold
    the previous `retailPriceBreakdown` JSON, and that column explains where a
    retail price came from. This file holds the raw stored strings, so a restore
    puts back exactly what was there rather than a re-formatted approximation.
    """
    import json

    entries = []
    for row in rows:
        if not row.writable:
            continue
        entries.append({
            "id": row.product_id,
            "sku": row.sku,
            "variantId": row.variant_id,
            "price": row.raw_price,
            "retail": row.raw_retail,
            "retailBreakdown": row.breakdown_before,
            "retailWasReplaced": row.retail_replaced,
            "wrotePrice": None if row.new_price is None else f"{row.new_price:.2f}",
            "wroteRetail": (None if row.new_retail is None
                            else f"{row.new_retail:.2f}"),
        })

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "createdAt": datetime.now().astimezone().isoformat(),
        "note": ("Restores Product.price, ProductVariant.basePrice and (where it "
                 "was replaced) Product.retailPrice + retailPriceBreakdown to the "
                 "values held before this run. Apply with --revert-from."),
        "products": entries,
    }, indent=1, default=str), encoding="utf-8")
    return len(entries)


def revert(dsn: str, undo_path: Path, apply: bool) -> int:
    """Put back what a previous run wrote.

    Deliberately a separate mode with its own confirmation rather than a flag on
    the normal path: undoing 1,097 price changes is its own operation, and it
    should not be reachable by mistyping an option on a repricing run.
    """
    import json

    data = json.loads(undo_path.read_text(encoding="utf-8"))
    products = data.get("products") or []
    if not products:
        print(f"{undo_path} lists no products to restore.")
        return 0

    print(f"Undo file : {undo_path}")
    print(f"Written   : {data.get('createdAt', 'unknown')}")
    print(f"Products  : {len(products)}")
    retail_rows = [x for x in products if x.get("retailWasReplaced")]
    print(f"...of which had their retail price replaced too: {len(retail_rows)}")
    print()
    for item in products[:5]:
        print(f"   {item.get('sku', item['id']):<14} price "
              f"{item.get('wrotePrice')} -> {item.get('price')}"
              + (f"   retail {item.get('wroteRetail')} -> {item.get('retail')}"
                 if item.get("retailWasReplaced") else ""))
    if len(products) > 5:
        print(f"   ... and {len(products) - 5} more")

    if not apply:
        print()
        print("PREVIEW ONLY -- nothing was restored. Re-run with --apply.")
        return 0

    target = redact(dsn)
    if PROD_HINTS.search(target):
        print()
        print("  !!  THIS LOOKS LIKE A PRODUCTION DATABASE: " + target)
    print()
    print(f"About to RESTORE {len(products)} product price(s) in {target}.")
    if input('Type "restore" to continue: ').strip().lower() != "restore":
        print("Aborted. Nothing was restored.")
        return 1

    payload = [[x["id"], x.get("price") or "0.00", x.get("retail"),
                x.get("retailBreakdown"), bool(x.get("retailWasReplaced"))]
               for x in products]
    variants = [[x["variantId"], money(x.get("price")) or 0.0]
                for x in products if x.get("variantId")]

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(RESTORE_SQL, (json.dumps(payload, default=str),))
            restored = cur.rowcount
            variants_restored = 0
            if variants:
                cur.execute(RESTORE_VARIANT_SQL,
                            (json.dumps([[v[0], f"{v[1]:.2f}"] for v in variants]),))
                variants_restored = cur.rowcount
        conn.commit()

    print(f"Restored {restored} product row(s) and {variants_restored} "
          f"variant mirror(s).")
    return 0


def write_tenant_tab(wb: Any, rows: list[Row], breakdown: list[tuple],
                     tenants: list[str] | None, stage: str) -> None:
    """A tab answering "how much of this is whose".

    Two different counts, side by side on purpose:

      in the database   every product this tenant has in the stage, whatever
                        this run looked at
      in this run       what --limit, --tenant-id and --only-over-retail left

    Reading one as the other is how a 4,700 total gets compared against a 3,352
    badge, so they are never collapsed into a single column.
    """
    ws = wb.create_sheet("By tenant")
    headers = ["Tenant", "Tenant ID", f"In the database ({stage})",
               "In this run", "Price changes", "Already correct", "Skipped",
               "Net movement", "Retail replaced", "In scope"]
    widths = [26, 38, 22, 12, 14, 16, 10, 14, 16, 10]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    # Rows carry the tenant NAME, the breakdown carries name + id, so the join
    # is by name. Two tenants sharing a name would merge -- acceptable, and
    # visible, because the id column would then disagree with the counts.
    seen: dict[str, dict[str, Any]] = {}
    for row in rows:
        bucket = seen.setdefault(row.tenant or "(unknown)", {
            "n": 0, "changes": 0, "correct": 0, "skipped": 0, "movement": 0.0,
            "retail": 0})
        bucket["n"] += 1
        if row.writable:
            bucket["changes"] += 1
            if row.delta is not None:
                bucket["movement"] += row.delta
        elif row.status == "already correct":
            bucket["correct"] += 1
        else:
            bucket["skipped"] += 1
        if row.retail_replaced:
            bucket["retail"] += 1

    for name, tid, in_db in breakdown:
        b = seen.pop(name, None)
        in_scope = tenants is None or tid in tenants
        ws.append([
            name, tid, in_db,
            b["n"] if b else 0,
            b["changes"] if b else 0,
            b["correct"] if b else 0,
            b["skipped"] if b else 0,
            round(b["movement"], 2) if b else 0,
            b["retail"] if b else 0,
            "yes" if in_scope else "NO",
        ])
        if not in_scope:
            for col in range(1, len(headers) + 1):
                ws.cell(row=ws.max_row, column=col).fill = SKIPPED_FILL

    # Any tenant present in the rows but not in the breakdown -- should not
    # happen, and is worth seeing rather than dropping if it ever does.
    for name, b in seen.items():
        ws.append([name, "(not in the per-tenant count)", "", b["n"],
                   b["changes"], b["correct"], b["skipped"],
                   round(b["movement"], 2), b["retail"], "?"])

    line = ws.max_row + 1
    ws.append(["TOTAL", "", sum(n for _, _, n in breakdown), len(rows),
               sum(1 for r in rows if r.writable),
               sum(1 for r in rows if r.status == "already correct"),
               sum(1 for r in rows
                   if not r.writable and r.status != "already correct"),
               round(sum(r.delta for r in rows
                         if r.writable and r.delta is not None), 2),
               sum(1 for r in rows if r.retail_replaced), ""])
    for cell in ws[line]:
        cell.font = Font(bold=True)

    for col in (8,):
        for cell in ws[get_column_letter(col)][1:]:
            cell.number_format = MONEY


def write_sheet(path: Path, rows: list[Row], total: int, totals: Totals,
                args: argparse.Namespace, applied: bool,
                breakdown: list[tuple] | None = None,
                tenants: list[str] | None = None) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Prices"

    ws.append([name for name, _ in COLUMNS])
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{len(rows) + 1}"
    for index, (_, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    money_cols = [i for i, (name, _) in enumerate(COLUMNS, start=1)
                  if name in {"Current Price", "Retail Price", "Expected Price",
                              "Min Allowed", "Max Allowed", "New Price", "Change",
                              "Variant Before", "New Retail (Google)",
                              "Retail Used"}]
    pct_col = next(i for i, (name, _) in enumerate(COLUMNS, start=1)
                   if name == "Grade %")

    for row in rows:
        ws.append(row.cells())
        line = ws.max_row
        for col in money_cols:
            ws.cell(row=line, column=col).number_format = MONEY
        ws.cell(row=line, column=pct_col).number_format = "0.00%"
        fill = CHANGED_FILL if row.status in ("preview", "updated") else SKIPPED_FILL
        for col in range(1, len(COLUMNS) + 1):
            ws.cell(row=line, column=col).fill = fill

    summary = wb.create_sheet("Summary")
    summary.column_dimensions["A"].width = 40
    summary.column_dimensions["B"].width = 62
    facts: list[tuple[str, Any]] = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Mode", "APPLIED -- prices were written"
                 if applied else "PREVIEW -- nothing was written"),
        ("Database", redact(args.dsn)),
        ("Selection", f"reviewStatus={STAGES[args.stage]} "
                      f"({args.stage} tab), not archived, not deleted"
                      + (", selling price > retail price"
                         if args.only_over_retail else "")),
        ("Ordered by", "createdAt DESC (same as the Review page)"),
        ("", ""),
        ("Target", "retailPrice x grade %"),
        ("Allowed window", f"target +/-{args.tolerance:.0%}, both ends charmed to .99"),
        ("Hard ceiling", f"under {HARD_MAX_RATIO:.0%} of retail, always"),
        ("Price floor", f"{args.min_price:.2f}" if args.min_price
                        else "(disabled)"),
        ("Correction", "smallest move to the nearest edge; in-window prices keep "
                       "their figure and only gain a .99 ending"),
    ]
    facts += [(f"    grade {g}", f"{p:.2%}") for g, p in GRADE_PCT.items()]
    facts += [
        ("", ""),
        (f"Total products in the {args.stage} stage", total),
        ("Examined in this run", totals.seen),
        ("...to be written" if not applied else "...written", totals.to_write),
        ("Net price movement", round(totals.movement, 2)),
    ]
    if applied:
        facts += [
            ("Product rows updated", totals.updated),
            ("ProductVariant mirrors updated", totals.variants_updated),
            ("Retail prices replaced from Google", totals.retails_written),
        ]
    facts += [
        ("", ""),
        ("Tenant scope", args.tenant_id if args.tenant_id
                         else "(every tenant -- unscoped)"),
        ("Google retail lookup", "on" if args.lookup_retail else "off"),
    ]
    if args.lookup_retail:
        facts += [
            ("    asked about", f"--lookup-when {args.lookup_when}"),
            ("    quote accepted when", args.accept_quote
             + (f" (+{args.min_quote_gain:.0f}% minimum)"
                if args.accept_quote == "higher_than_retail" else "")),
            ("    listings required to agree", args.min_samples),
            ("    serpapi searches left at start",
             totals.retail_quota_left if totals.retail_quota_left is not None
             else "(not checked)"),
            ("    products eligible for a lookup", totals.retail_eligible),
            ("    paid API calls made", totals.retail_calls),
            ("    call budget", totals.retail_budget or "(none)"),
            ("    budget reached", "YES" if totals.retail_budget_hit else "no"),
            ("        of which serpapi", totals.retail_serpapi_calls),
            ("        of which fallback (2nd call)", totals.retail_fallback_calls),
            ("    rate limits hit (HTTP 429)", totals.retail_rate_limited),
            ("    served from cache (no charge)", totals.retail_cache_hits),
            ("    answers kept for the next run", totals.retail_cached_total),
            ("    retail price replaced", totals.retail_replaced),
            ("    quote found but not used", totals.retail_rejected),
            ("    no usable quote", totals.retail_failed),
            ("    calls skipped (backend spent)", totals.retail_skipped_calls),
        ]
    if totals.retail_disabled:
        facts += [("", ""), ("Backends switched off mid-run", "")]
        facts += [(f"    {k}", short(v, 200))
                  for k, v in totals.retail_disabled.items()]
    if totals.retail_errors:
        facts += [("", ""), ("Why no retail was found", "")]
        facts += [(f"    {k}", v) for k, v in sorted(
            totals.retail_errors.items(), key=lambda kv: -kv[1])]
    facts += [("", ""), ("Verdicts", "")]
    facts += [(f"    {k}", v) for k, v in sorted(totals.verdicts.items())]
    facts += [("", ""), ("Skipped, by reason", "")]
    facts += [(f"    {k}", v) for k, v in sorted(totals.skipped.items())]
    facts += [("", ""), ("Grades seen", "")]
    facts += [(f"    {k}", v) for k, v in sorted(totals.grades.items())]

    for key, value in facts:
        summary.append([key, value])
    for cell in summary["A"]:
        cell.font = Font(bold=True)

    if breakdown:
        write_tenant_tab(wb, rows, breakdown, tenants, args.stage)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def redact(dsn: str) -> str:
    """Host and database only. The sheet gets shared; the password must not."""
    match = re.match(r"(\w+)://([^:/@]+)(?::[^@]*)?@([^/]+)/([^?]+)", dsn or "")
    if not match:
        return "(dsn not shown)"
    return f"{match.group(1)}://{match.group(2)}@{match.group(3)}/{match.group(4)}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reprice Review-stage products directly in Postgres.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--dsn", default=os.getenv("DATABASE_URL") or None,
                   help="Postgres connection string. Env: DATABASE_URL.")
    p.add_argument("--limit", type=int, default=10,
                   help="How many products (default 10). Ignored with --all.")
    p.add_argument("--stage", default="review", choices=sorted(STAGES),
                   help="Which Products tab to reprice (default review). "
                        "'approved' and 'uploaded' are the same tab -- the URL "
                        "says uploaded, the badge says Approved.")
    p.add_argument("--all", action="store_true",
                   help="Every product in the chosen stage.")
    p.add_argument("--only-over-retail", action="store_true",
                   help="Only products whose selling price is strictly above "
                        "their stored retail price -- the rows where the two "
                        "figures contradict each other. Applied in SQL, so the "
                        "sheet contains just those products.")
    p.add_argument("--apply", action="store_true",
                   help="WRITE the prices. Without this, nothing is written.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt --apply otherwise requires.")
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                   help=f"Relative half-width of the allowed band around the "
                        f"grade target (default {DEFAULT_TOLERANCE} = +/-20%%). "
                        f"0 demands the exact charmed target.")
    p.add_argument("--min-price", type=float, default=DEFAULT_MIN_PRICE,
                   help=f"Absolute floor under the window (default "
                        f"{DEFAULT_MIN_PRICE:.2f}, matching the Hermes policy). "
                        f"0 disables it.")
    p.add_argument("--tenant-id", default=os.getenv("VNYX_TENANT_ID") or None,
                   help="Restrict to these tenants (comma-separated UUIDs). The "
                        "Products page is tenant-scoped for a non-admin account, "
                        "so leaving this off can count MORE products than the "
                        "screen shows. The per-tenant breakdown printed on every "
                        "run tells you which ids to pass.")
    p.add_argument("--lookup-retail", action="store_true",
                   help="For products priced at or above their own retail, ask "
                        "Google Shopping what they retail for and use that "
                        "figure instead. One paid API call per distinct product "
                        "title -- off by default.")
    p.add_argument("--lookup-when", choices=LOOKUP_MODES, default="over_retail",
                   help="Which products get a lookup. 'over_retail' (default): "
                        "the selling price is strictly above the stored retail "
                        "price. 'at_or_over_95': within 5%% of retail counts too. "
                        "'over_grade_max': anything over its grade band, even "
                        "when cheaper than retail. 'all': every changing row. A "
                        "product with no retail price qualifies in every mode.")
    p.add_argument("--accept-quote", choices=QUOTE_RULES, default="always",
                   help="When to believe a Google quote over the stored retail. "
                        "Default 'always': the quote wins whenever enough "
                        "listings agree. 'higher_than_retail' takes it only when "
                        "it argues the anchor was too low; 'higher_than_price' "
                        "also requires it to clear the selling price.")
    p.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                   help=f"Listings that must agree before a quote is usable "
                        f"(default {MIN_SAMPLES}). Raise it to demand more "
                        f"confidence before overwriting a retail price.")
    p.add_argument("--max-quote-ratio", type=float, default=MAX_QUOTE_RATIO,
                   help=f"Reject a quote more than this many times away from the "
                        f"stored retail, in either direction (default "
                        f"{MAX_QUOTE_RATIO:g}). Catches bad Google matches, which "
                        f"look confident and carry plenty of listings. 0 "
                        f"disables the check.")
    p.add_argument("--min-quote-gain", type=float, default=MIN_QUOTE_GAIN_PCT,
                   help=f"Percent a quote must exceed the stored retail by "
                        f"(default {MIN_QUOTE_GAIN_PCT:.0f}). A hair above is "
                        f"noise, not evidence.")
    p.add_argument("--lookup-workers", type=int, default=4,
                   help="Concurrent Google lookups (default 4).")
    p.add_argument("--lookup-rate", type=float, default=DEFAULT_LOOKUP_RATE,
                   help=f"Billable requests per second across ALL workers "
                        f"(default {DEFAULT_LOOKUP_RATE:g}). --lookup-workers is "
                        f"not a rate limit on its own. 0 disables the throttle.")
    p.add_argument("--lookup-timeout", type=float, default=20.0,
                   help="Seconds to wait on one lookup request (default 20). "
                        "This is what bounds the worst case: a hung request "
                        "holds its worker for the whole timeout, and with "
                        "retries that multiplies. Lower it for a predictable "
                        "run, raise it only if slow-but-valid replies are being "
                        "cut off.")
    p.add_argument("--lookup-budget", type=int, default=None,
                   help="Hard ceiling on billable API calls for this run. "
                        "Default: the eligible product count plus "
                        f"{BUDGET_HEADROOM_PCT}%% (minimum "
                        f"{BUDGET_HEADROOM_MIN}) of headroom for fallbacks -- so "
                        "443 eligible allows 488. Once reached, no further "
                        "lookups are bought. 0 removes the cap.")
    p.add_argument("--lookup-progress-every", type=int, default=1,
                   help="Print a line every N products (default 1 -- every "
                        "record). 0 silences it.")
    p.add_argument("--lookup-cache",
                   default=str(ROOT / "reports" / ".retail-lookup-cache.json"),
                   help="File the paid answers are kept in, so a re-run does not "
                        "buy them twice. Successes and per-title 'not found' "
                        "results are stored; quota and network failures are not. "
                        "Pass an empty string to disable.")
    p.add_argument("--max-rate-limits", type=int, default=RATE_LIMIT_GIVE_UP,
                   help=f"Total HTTP 429s before a backend is given up on "
                        f"(default {RATE_LIMIT_GIVE_UP}).")
    p.add_argument("--lookup-retries", type=int, default=2,
                   help="Retries per lookup on a 429 or 5xx (default 2). A 4xx "
                        "is never retried.")
    p.add_argument("--serpapi-key", default=os.getenv("SERPAPI_KEY") or "",
                   help="Env: SERPAPI_KEY. Preferred -- returns numeric prices.")
    p.add_argument("--google-key", default=os.getenv("GOOGLE_SEARCH_API_KEY") or "",
                   help="Env: GOOGLE_SEARCH_API_KEY. Fallback.")
    p.add_argument("--google-cx",
                   default=os.getenv("GOOGLE_SEARCH_ENGINE_ID") or "",
                   help="Env: GOOGLE_SEARCH_ENGINE_ID. Fallback.")
    p.add_argument("--country", default="nl",
                   help="Market for the Google query (hl/gl), default nl.")
    p.add_argument("--undo-file", default=None,
                   help="Where to record the previous values before writing "
                        "(default reports/undo-<stamp>.json). Restore with "
                        "--revert-from.")
    p.add_argument("--revert-from", default=None,
                   help="Undo a previous run: put back the prices recorded in "
                        "this undo file. Add --apply to actually write.")
    p.add_argument("--out", default=None,
                   help="Path for the .xlsx (default reports/db-reprice-<stamp>.xlsx).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.dsn:
        sys.exit("No database connection string. Pass --dsn, or set DATABASE_URL.")

    if args.revert_from:
        path = Path(args.revert_from)
        if not path.exists():
            sys.exit(f"No such undo file: {path}")
        return revert(args.dsn, path, args.apply)
    if not args.all and args.limit < 1:
        sys.exit("--limit must be at least 1.")

    review_status = STAGES[args.stage]
    stage_label = f"{args.stage} stage (reviewStatus={review_status})"

    tenants = None
    if args.tenant_id:
        tenants = [t.strip() for t in str(args.tenant_id).split(",") if t.strip()]

    print(f"Connecting to {redact(args.dsn)}")
    records, total, breakdown, stats = fetch(
        args.dsn, None if args.all else args.limit, tenants, review_status,
        args.only_over_retail)

    totals = Totals(seen=len(records))
    rows = [price_row(r, totals, args.tolerance, args.min_price)
            for r in records]

    if args.lookup_retail:
        lookup = RetailLookup(args.serpapi_key, args.google_key, args.google_cx,
                              args.country, min_samples=args.min_samples,
                              rate=args.lookup_rate,
                              retries=args.lookup_retries,
                              timeout=args.lookup_timeout,
                              max_rate_limits=args.max_rate_limits,
                              cache_path=(Path(args.lookup_cache)
                                          if args.lookup_cache else None))
        # The budget is sized from the products that actually qualify, which is
        # only known after the rows have been judged -- so it is set here rather
        # than in the constructor.
        # Must match `qualifies()` in relookup_retail for the chosen mode --
        # deriving it differently made a 443-product run report a 554 cap.
        def _eligible(r: Row) -> bool:
            if r.status == "skipped" and "retailPrice" in r.reason:
                return True
            if args.lookup_when == "over_retail":
                return (r.current is not None and r.retail is not None
                        and r.current > r.retail)
            if args.lookup_when == "at_or_over_95":
                return r.verdict == "above_retail"
            if args.lookup_when == "over_grade_max":
                return r.verdict in HIGH_VERDICTS
            return r.writable

        eligible = sum(1 for r in rows if _eligible(r))
        if args.lookup_budget is None:
            lookup.budget = eligible + max(
                BUDGET_HEADROOM_MIN, -(-eligible * BUDGET_HEADROOM_PCT // 100))
        elif args.lookup_budget > 0:
            lookup.budget = args.lookup_budget
        else:
            lookup.budget = None
        if not lookup.available:
            sys.exit("--lookup-retail needs SERPAPI_KEY, or both "
                     "GOOGLE_SEARCH_API_KEY and GOOGLE_SEARCH_ENGINE_ID. Set "
                     "them in the environment or pass them as flags.")
        totals.retail_quota_left = lookup.preflight()
        relookup_retail(rows, lookup, totals, args.tolerance, args.min_price,
                        args.lookup_workers, args.lookup_when,
                        args.accept_quote, args.min_quote_gain,
                        args.lookup_progress_every, args.max_quote_ratio)
        # Recount: the lookup pass moves rows between "to write" and "skipped",
        # so the headline figures have to be derived again rather than adjusted.
        totals.to_write = sum(1 for r in rows if r.writable)
        totals.movement = sum(r.delta for r in rows
                              if r.writable and r.delta is not None)

    print()
    print(f"Selling price ABOVE retail price (whole database, unscoped), split "
          f"by whether a\nproduct is in the {args.stage} stage:")
    print(f'   {"":<14}{"products":>9}{"price>retail":>14}{"at/over 95%":>13}'
          f'{"no retail":>11}{"no price":>10}{"excess EUR":>13}')
    for row in stats:
        label = (f"{args.stage} stage" if row["in_review"]
                 else "other stages")
        print(f'   {label:<14}{row["total"]:>9}{row["over_retail"]:>14}'
              f'{row["at_or_over_95"]:>13}{row["no_retail"]:>11}'
              f'{row["no_price"]:>10}{float(row["excess"]):>13,.2f}')
    if not stats:
        print("   (no products)")

    print()
    print(f"{args.stage.capitalize()}-stage products per tenant "
          f"(whole database, unscoped):")
    for name, tid, n in breakdown:
        mark = ("" if tenants is None or tid in tenants
                else "   <- EXCLUDED by --tenant-id")
        print(f"   {n:>6}  {name:<26} {tid}{mark}")
    if tenants is None and len(breakdown) > 1:
        print("   NOTE: no --tenant-id, so every tenant above is included. The")
        print("         Products page counts only the tenants YOUR login can")
        print("         see, which is why its badge can read lower than this.")

    print()
    scope = (" with price > retail" if args.only_over_retail else "")
    print(f"Products in the {args.stage} stage{scope}: {total}")
    print(f"Examined in this run              : {totals.seen}"
          f"{'' if args.all else f' (--limit {args.limit})'}")
    print(f"Prices to change                  : {totals.to_write}")
    print(f"Net movement                      : {totals.movement:+.2f}")
    if totals.retail_eligible:
        print()
        print("Google retail lookup:")
        print(f"   products eligible for a lookup   : {totals.retail_eligible}")
        if totals.retail_budget:
            print(f"   credits used / budget            : "
                  f"{totals.retail_calls} / {totals.retail_budget}")
        print(f"   paid API calls made              : {totals.retail_calls}"
              f"   (serpapi {totals.retail_serpapi_calls}"
              f", fallback {totals.retail_fallback_calls})")
        if totals.retail_fallback_calls:
            print("      a fallback is a SECOND billable call for the same "
                  "product, which is why")
            print("      this can exceed the eligible count")
        print(f"   served from the cache (no charge) : "
              f"{totals.retail_cache_hits}")
        if totals.retail_rate_limited:
            print(f"   HTTP 429 rate limits hit         : "
                  f"{totals.retail_rate_limited}"
                  f"   <- lower --lookup-rate")
        print(f"   retail price REPLACED            : {totals.retail_replaced}")
        print(f"   quote found but not used         : {totals.retail_rejected}"
              f"   (--accept-quote {args.accept_quote})")
        print(f"   no usable quote came back        : {totals.retail_failed}")
        if totals.retail_seconds:
            rate = totals.retail_calls / max(totals.retail_seconds, 0.001)
            print(f"   time spent on lookups            : "
                  f"{totals.retail_seconds:.0f}s "
                  f"({rate:.1f} calls/sec achieved)")
        if totals.retail_skipped_calls:
            print(f"   calls NOT made (backend spent)   : "
                  f"{totals.retail_skipped_calls}")
        if totals.retail_cached_total:
            print(f"   answers kept for the next run    : "
                  f"{totals.retail_cached_total}"
                  f"   ({args.lookup_cache})")
        if totals.retail_budget_hit:
            print()
            print(f"   *** CALL BUDGET OF {totals.retail_budget} REACHED ***")
            print("       Lookups stopped there; the remaining products keep "
                  "their stored retail")
            print("       price and are still repriced by the grade window. "
                  "Raise --lookup-budget")
            print("       to go further, or re-run -- the answers already "
                  "bought are cached.")
        if totals.retail_disabled:
            print()
            for backend, why in totals.retail_disabled.items():
                print(f"   *** {backend.upper()} WAS SWITCHED OFF MID-RUN ***")
                print(f"       {why}")
            print("       Every remaining product would have got the same "
                  "answer, so no")
            print("       further calls were made. Fix the key or quota and "
                  "re-run -- rows")
            print("       already written are skipped as 'already correct'.")
        if totals.retail_errors:
            print()
            print("   why no retail was found:")
            for reason, n in sorted(totals.retail_errors.items(),
                                    key=lambda kv: -kv[1]):
                print(f"      {n:>5}  {reason}")
    if totals.skipped:
        print("Skipped:")
        for reason, count in sorted(totals.skipped.items()):
            print(f"   {reason:<28} {count}")

    applied = False
    if args.apply and totals.to_write:
        if not args.yes:
            print()
            target = redact(args.dsn)
            if PROD_HINTS.search(target):
                print()
                print("  !!  " + "-" * 66)
                print("  !!  THIS LOOKS LIKE A PRODUCTION DATABASE")
                print(f"  !!  {target}")
                print("  !!  " + "-" * 66)
            print(f"About to UPDATE {totals.to_write} product price(s) and their "
                  f"variant mirrors in {target}.")
            if input('Type "yes" to continue: ').strip().lower() != "yes":
                print("Aborted. Nothing was written.")
                return 1
        undo_path = Path(args.undo_file) if args.undo_file else (
            ROOT / "reports" /
            f"undo-{args.stage}-"
            f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        n = write_undo(undo_path, rows)
        print(f"Undo file written first: {undo_path} ({n} products)")

        write(args.dsn, rows, totals)
        applied = True
        print()
        print(f"Product rows updated            : {totals.updated}")
        print(f"ProductVariant mirrors updated  : {totals.variants_updated}")
    elif args.apply:
        print("\nNothing to write.")

    out = Path(args.out) if args.out else (
        ROOT / "reports" /
        f"db-reprice-{args.stage}-"
        f"{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    )
    write_sheet(out, rows, total, totals, args, applied, breakdown, tenants)

    print()
    print(f"Sheet: {out}")
    if not applied:
        print("PREVIEW ONLY -- nothing was written. Check the sheet, then re-run "
              "with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
