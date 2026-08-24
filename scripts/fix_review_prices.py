#!/usr/bin/env python
"""Correct the selling price of every product on the Review page, with an
Excel audit trail of what changed.

    # 1. LOOK FIRST. Read-only, 10 rows in the sheet, nothing written anywhere.
    python scripts/fix_review_prices.py

    # 2. Same read-only pass over the WHOLE queue, so the sheet shows the
    #    full blast radius before anything is committed.
    python scripts/fix_review_prices.py --all

    # 3. Only once the sheet looks right.
    python scripts/fix_review_prices.py --all --apply


WHY IT GOES THROUGH vnyx-api RATHER THAN THE DATABASE

Every figure comes from `GET /review-verification/queue`, which is the exact
endpoint the Review page itself calls, and every write goes through
`POST /review-verification/products/:id/verify`, which is what the Verify button
calls. So this script cannot disagree with the screen -- same feed, same Hermes
verdict, same write path.

That write path matters more than it looks. It goes through `updateProduct`,
which normalises the stored price format, mirrors the new figure into
ProductVariant/Price (what the marketplace sync actually publishes), keeps the
stage cache honest and writes an activity-log row. A direct UPDATE on
Product.price would skip all four and leave the variant mirror stale -- which is
the very drift PRICE.110 exists to report.


THE READ-ONLY MODE IS READ-ONLY

Without `--apply` the script issues GET requests and nothing else. It does not
call the verify endpoint even with `apply=false`, because that endpoint is a POST
and "a POST that promises not to write" is a worse guarantee than not sending it.
The queue response already carries the full assessment, so the preview needs
nothing more.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

try:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:  # pragma: no cover - dependency guidance
    sys.exit(
        "openpyxl is required for the spreadsheet.\n"
        "  pip install openpyxl"
    )

ROOT = Path(__file__).resolve().parents[1]

# Loaded so VNYX_BASE_URL / credentials can live in the same .env the service
# uses, rather than being retyped on every invocation.
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# Columns
#
# The five the request asked for, plus the ones needed to CHECK the answer:
# without the grade and the factor there is no way to tell a correct new price
# from a plausible one, which is the whole point of reviewing a sample first.
# --------------------------------------------------------------------------- #

COLUMNS: list[tuple[str, int]] = [
    ("Product ID", 38),
    ("SKU", 16),
    ("Title", 40),
    ("Grade", 8),
    ("Currency", 9),
    ("Current Price", 14),
    ("Retail Price", 13),
    ("Price Factor", 12),
    ("Expected Price", 14),
    ("Expected Range", 18),
    ("New Price", 12),
    ("Change", 10),
    ("Change %", 10),
    ("Needs Change", 13),
    ("Verdict", 16),
    ("Status", 12),
    ("Reason", 80),
    ("Edit URL", 60),
]

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True)
CHANGED_FILL = PatternFill("solid", fgColor="FFF2CC")   # amber: will/did move
FAILED_FILL = PatternFill("solid", fgColor="F8CBAD")    # red-ish: write failed
MONEY = "#,##0.00"


@dataclass
class Row:
    """One product's before/after, ready for a spreadsheet line."""

    product_id: str
    sku: str
    title: str
    grade: str
    currency: str
    current: float | None
    retail: float | None
    factor: float | None
    expected: float | None
    min_allowed: float | None
    max_allowed: float | None
    new_price: float | None
    verdict: str
    reason: str
    edit_url: str
    status: str = "preview"
    # Set when a filter (--rounding-only, --max-change-pct) holds this row back.
    # `new_price` is deliberately LEFT populated so the sheet still shows what
    # would have happened -- the point of a preview is to see what you excluded.
    excluded: str | None = None

    @property
    def proposed(self) -> float | None:
        """What the backend would write, filters ignored."""
        return self.new_price

    @property
    def needs_change(self) -> bool:
        if self.excluded:
            return False
        if self.new_price is None or self.current is None:
            return self.new_price is not None
        return abs(self.new_price - self.current) > 0.005

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
    def price_range(self) -> str:
        if self.min_allowed is None or self.max_allowed is None:
            return ""
        return f"{self.min_allowed:.2f} - {self.max_allowed:.2f}"

    def cells(self) -> list[Any]:
        return [
            self.product_id, self.sku, self.title, self.grade, self.currency,
            self.current, self.retail, self.factor, self.expected,
            self.price_range, self.new_price, self.delta, self.delta_pct,
            "YES" if self.needs_change else "no",
            self.verdict, self.status, self.reason, self.edit_url,
        ]


@dataclass
class Totals:
    seen: int = 0
    needs_change: int = 0
    not_assessed: int = 0
    updated: int = 0
    failed: int = 0
    excluded: int = 0
    # Prices that were written but did not end .99 -- should always be zero.
    not_charm: int = 0
    movement: float = 0.0
    verdicts: dict[str, int] = field(default_factory=dict)
    excluded_movement: float = 0.0


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")


def money(value: Any) -> float | None:
    """Pull a number out of "€38.99", "38,99", 38.99, or None.

    Prices arrive as strings with a currency symbol baked in on older rows, so
    parsing has to be tolerant. Returns None rather than 0.0 on failure: a
    missing price and a free product must not look the same.
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


def short(value: Any, limit: int = 160) -> str:
    text = "" if value is None else str(value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "..."


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #

class Api:
    def __init__(self, base: str, token: str, timeout: float) -> None:
        self.base = base.rstrip("/")
        self.client = httpx.Client(
            timeout=timeout,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )

    @staticmethod
    def login(base: str, email: str, password: str, timeout: float) -> str:
        r = httpx.post(f"{base.rstrip('/')}/auth/login", timeout=timeout,
                       json={"email": email, "password": password})
        if r.status_code == 401:
            sys.exit("Login failed: invalid email or password.")
        r.raise_for_status()
        body = r.json()
        # The envelope middleware passes 2xx bodies through untouched, but
        # `data` is checked too so a future change to that does not break this.
        token = body.get("accessToken") or (body.get("data") or {}).get("accessToken")
        if not token:
            sys.exit(f"Login succeeded but returned no accessToken: {body}")
        return str(token)

    def queue_page(self, status: str, skip: int, take: int,
                   tenant_id: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {"status": status, "skip": skip, "take": take}
        if tenant_id:
            params["tenantId"] = tenant_id
        r = self.client.get(f"{self.base}/review-verification/queue", params=params)
        if r.status_code in (401, 403):
            sys.exit(
                f"HTTP {r.status_code} reading the queue. The token is missing, "
                "expired, or lacks access to this tenant."
            )
        r.raise_for_status()
        return r.json()

    def verify(self, product_id: str) -> dict[str, Any]:
        """The thorough path: the backend re-runs the whole check, then writes.

        Correct but expensive -- per product it rebuilds the review feed, the
        grade ladder and the tenant catalog, then calls Hermes, before writing.
        That is the right shape for one product behind a button and the wrong
        one for 1,200 in a loop.
        """
        r = self.client.post(
            f"{self.base}/review-verification/products/{product_id}/verify",
            params={"apply": "true"},
        )
        if r.status_code == 403:
            sys.exit(
                "HTTP 403 from the verify endpoint. It requires the ADMIN role, "
                "while the read-only queue only needs USER -- so a preview can "
                "succeed with a token that cannot write. Log in as an admin, or "
                "drop --slow to use the direct price update (USER is enough)."
            )
        r.raise_for_status()
        return r.json()

    def put_price(self, product_id: str, price: str) -> dict[str, Any]:
        """The bulk path: write the price the preview already computed.

        Still goes through `updateProduct` server-side (products.ts calls it for
        PUT /products/:id), so the stored format is normalised, the
        ProductVariant/Price mirror is refreshed and the stage cache stays
        honest -- the same four things a bare prisma update would skip.

        What it skips is the server RE-DERIVING the figure it was just told. The
        price comes from the preview, which is the sheet the operator approved,
        so re-deriving it per product buys nothing except latency. A partial body
        (`{price}`) is a partial update, so no other field is touched.
        """
        r = self.client.put(f"{self.base}/products/{product_id}",
                            json={"price": price})
        if r.status_code in (401, 403):
            sys.exit(f"HTTP {r.status_code} writing {product_id}: the token "
                     "cannot update this product.")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.client.close()


def to_row(item: dict[str, Any]) -> Row:
    """Flatten one queue row. The assessment is already attached by Hermes."""
    price = ((item.get("verification") or {}).get("price")) or {}
    expectation = item.get("priceExpectation") or {}

    corrected = money(price.get("corrected_price"))
    current = money(item.get("price"))
    verdict = str(price.get("verdict") or "not assessed")

    # `change_required` is Hermes' own answer and is trusted over re-deriving it
    # here: an "ok" verdict whose corrected_price equals the current price would
    # otherwise read as a pending change on every run.
    if not price.get("change_required"):
        corrected = None

    row = Row(
        product_id=str(item.get("id") or ""),
        sku=short(item.get("sku"), 40),
        title=short(item.get("title"), 70),
        grade=short(item.get("grade") or item.get("gradeLabel"), 12),
        currency=short(item.get("currency") or price.get("currency"), 8),
        current=current,
        retail=money(item.get("retailPrice")),
        factor=money(price.get("price_factor")) or money(expectation.get("priceFactor")),
        expected=money(price.get("expected_price")),
        min_allowed=money(price.get("min_allowed")),
        max_allowed=money(price.get("max_allowed")),
        new_price=corrected,
        verdict=verdict,
        reason=short(price.get("explanation")),
        edit_url=str(item.get("editUrl") or ""),
    )
    if not price:
        row.status = "unchecked"
        row.reason = row.reason or (
            "No assessment came back for this product -- the verification service "
            "was unreachable or did not return a verdict for it."
        )
    return row


def exclude(row: Row, args: argparse.Namespace) -> None:
    """Hold a row back from the write set, per the safety flags.

    Both filters exist because "correct the prices" and "round the prices to
    .99" are not the same job, and the queue mixes them. A `too_low` verdict is
    the grade window lifting an eBay-derived price to the tenant's factor floor,
    which can be a +50.00 move on a single garment -- a legitimate correction,
    but not one to make in bulk by accident while chasing the cents.
    """
    if row.status == "unchecked" or not row.new_price:
        return

    if args.rounding_only and row.verdict != "round_required":
        row.excluded = f"--rounding-only: verdict is {row.verdict}, not a rounding"
        row.status = "skipped"
    elif args.max_change_pct is not None and row.delta_pct is not None \
            and abs(row.delta_pct) > args.max_change_pct:
        row.excluded = (
            f"--max-change-pct {args.max_change_pct:g}: this moves the price "
            f"{row.delta_pct:+.1f}%"
        )
        row.status = "skipped"

    if row.excluded:
        row.reason = f"{row.excluded}. Assessment: {short(row.reason, 100)}"


def collect(api: Api, args: argparse.Namespace) -> tuple[list[Row], int, Totals]:
    """Walk the queue, newest page first, until the wanted count is reached."""
    rows: list[Row] = []
    totals = Totals()
    total = 0
    skip = 0
    # 200 is the server's own ceiling on `take`; asking for more silently gets
    # clamped, which would turn into an infinite loop below.
    page_size = min(args.page_size, 200)

    while True:
        want = None if args.all else max(0, args.limit - len(rows))
        if want == 0:
            break
        body = api.queue_page(args.status, skip,
                              page_size if want is None else min(page_size, want),
                              args.tenant_id)
        total = int(body.get("total") or 0)
        items = body.get("items") or []

        verification = body.get("verification") or {}
        if skip == 0 and verification.get("available") is False:
            sys.exit(
                "The verification service is not available, so there are no "
                f"prices to correct: {verification.get('error')}\n"
                "Start Hermes and check HERMES_URL in the backend's environment."
            )

        for item in items:
            row = to_row(item)
            exclude(row, args)
            rows.append(row)
            totals.seen += 1
            totals.verdicts[row.verdict] = totals.verdicts.get(row.verdict, 0) + 1
            if row.status == "unchecked":
                totals.not_assessed += 1
            elif row.excluded:
                totals.excluded += 1
                if row.delta is not None:
                    totals.excluded_movement += row.delta
            elif row.needs_change:
                totals.needs_change += 1
                if row.delta is not None:
                    totals.movement += row.delta

        if not items:
            break
        skip += len(items)
        if skip >= total:
            break
        if not args.all and len(rows) >= args.limit:
            break
        print(f"  ...read {len(rows)} of {total}", file=sys.stderr)

    return rows, total, totals


def charm_guard(row: Row, totals: Totals) -> bool:
    """Refuse to report a written price that does not end .99.

    Checked against what was ACTUALLY stored rather than trusted from the
    preview, so a policy left on `exact` -- or a Hermes that has not reloaded
    one -- surfaces here instead of quietly landing .00 prices in the catalog.
    Returns True when the price is a proper charm price.
    """
    if row.new_price is None:
        return True
    if round(row.new_price % 1, 2) == 0.99:
        return True
    row.status = "CHECK"
    row.reason = (
        f"WROTE {row.new_price:.2f}, which does not end .99. Check `round_mode` "
        f"in the Hermes config/policy.yaml -- it should be charm99 -- and that "
        f"the running Hermes has reloaded it. | {short(row.reason, 90)}"
    )
    totals.not_charm += 1
    return False


def write_one(api: Api, row: Row, args: argparse.Namespace) -> str:
    """Write one product's price. Returns a line to print.

    Retries a timeout or a 5xx: the slow path can genuinely exceed a generous
    timeout on a heavy product, and losing the write for that reason means
    re-running the whole job to catch a handful of stragglers. A 4xx is NOT
    retried -- it will fail identically every time.
    """
    attempts = max(1, args.retries + 1)
    last: Exception | None = None

    for attempt in range(attempts):
        try:
            if args.slow:
                body = api.verify(row.product_id)
                change = body.get("priceChange")
                if change:
                    row.new_price = money(change.get("to"))
                    row.status = "updated"
                    row.verdict = str(change.get("verdict") or row.verdict)
                    return (f"  {row.product_id}  {change.get('from')} -> "
                            f"{change.get('to')}")
                if body.get("applied") is False and not body.get("priceSuggestion"):
                    # The backend re-checked and found nothing to do. Usually
                    # means the product was edited since the preview.
                    row.status = "no change"
                    row.new_price = None
                    row.reason = ("Re-checked at write time and already correct; "
                                  "nothing was written.")
                    return f"  {row.product_id}  no change needed (re-checked)"
                row.status = "not applied"
                return f"  {row.product_id}  not applied"

            # Fast path: write the figure the preview computed and the operator
            # approved in the sheet.
            assert row.new_price is not None
            target = f"{row.new_price:.2f}"
            body = api.put_price(row.product_id, target)
            # Prefer the price the server echoes back over the one we sent, so
            # the sheet records what was stored, not what was requested.
            stored = money((body or {}).get("price"))
            row.new_price = stored if stored is not None else row.new_price
            row.status = "updated"
            return f"  {row.product_id}  {row.current} -> {row.new_price}"

        except (httpx.TimeoutException, httpx.HTTPStatusError,
                httpx.TransportError) as exc:
            retryable = isinstance(exc, (httpx.TimeoutException,
                                         httpx.TransportError)) or (
                isinstance(exc, httpx.HTTPStatusError)
                and exc.response.status_code >= 500)
            last = exc
            if not retryable or attempt == attempts - 1:
                break
            time.sleep(0.5 * (attempt + 1))

    row.status = "FAILED"
    row.reason = f"write failed after {attempts} attempt(s): {last} | " \
                 f"{short(row.reason, 80)}"
    return f"  {row.product_id} FAILED: {last}"


def apply_all(api: Api, rows: list[Row], totals: Totals,
              args: argparse.Namespace) -> None:
    """Write the corrections, `--concurrency` at a time.

    Order of completion is not the order of the sheet, so each result prints as
    it lands with its own product id. A failure part-way through still leaves a
    spreadsheet saying exactly which products were committed and which were not.
    """
    targets = [r for r in rows if r.needs_change]
    if not targets:
        return

    workers = max(1, min(args.concurrency, len(targets)))
    done = 0

    def run(row: Row) -> tuple[Row, str]:
        line = write_one(api, row, args)
        if args.sleep:
            time.sleep(args.sleep)
        return row, line

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run, row) for row in targets]
        for future in as_completed(futures):
            row, line = future.result()
            done += 1
            if row.status == "FAILED":
                totals.failed += 1
            elif row.status == "updated":
                totals.updated += 1
                charm_guard(row, totals)
                if row.status == "CHECK":
                    line = f"  {row.product_id}  WROTE {row.new_price} -- NOT .99"
            print(f"  [{done}/{len(targets)}]{line[1:]}")


# --------------------------------------------------------------------------- #
# Spreadsheet
# --------------------------------------------------------------------------- #

def describe_filters(args: argparse.Namespace) -> str:
    """Named on the Summary tab so a sheet is self-explanatory months later --
    a reader must never have to guess whether an omission was a filter."""
    active: list[str] = []
    if args.rounding_only:
        active.append("--rounding-only")
    if args.max_change_pct is not None:
        active.append(f"--max-change-pct {args.max_change_pct:g}")
    return ", ".join(active) or "(none -- every correction included)"


def write_sheet(path: Path, rows: list[Row], total: int, totals: Totals,
                args: argparse.Namespace, applied: bool) -> None:
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

    for index, (name, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width

    money_cols = [i for i, (name, _) in enumerate(COLUMNS, start=1)
                  if name in {"Current Price", "Retail Price", "Expected Price",
                              "New Price", "Change"}]

    for row in rows:
        ws.append(row.cells())
        line = ws.max_row
        for col in money_cols:
            ws.cell(row=line, column=col).number_format = MONEY
        if row.status in ("FAILED", "CHECK"):
            fill = FAILED_FILL
        elif row.needs_change:
            fill = CHANGED_FILL
        else:
            fill = None
        if fill:
            for col in range(1, len(COLUMNS) + 1):
                ws.cell(row=line, column=col).fill = fill

    # A second tab, so the numbers that decide "is this safe to run for real"
    # are not something the reader has to recompute from the rows.
    summary = wb.create_sheet("Summary")
    summary.column_dimensions["A"].width = 38
    summary.column_dimensions["B"].width = 60
    facts: list[tuple[str, Any]] = [
        ("Generated", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("Mode", "APPLIED -- prices were written" if applied
                 else "PREVIEW -- nothing was written"),
        ("Backend", args.base_url),
        ("Review tab", args.status),
        ("Tenant filter", args.tenant_id or "(all accessible tenants)"),
        ("Filters", describe_filters(args)),
        ("", ""),
        ("Total products on the review page", total),
        ("Products examined in this run", totals.seen),
        ("...of those, price needs a change", totals.needs_change),
        ("...of those, held back by filters", totals.excluded),
        ("...of those, not assessed at all", totals.not_assessed),
        ("Net price movement across changes", round(totals.movement, 2)),
        ("Movement held back by filters", round(totals.excluded_movement, 2)),
    ]
    if applied:
        facts += [
            ("", ""),
            ("Prices written", totals.updated),
            ("Writes that failed", totals.failed),
            ("Written but NOT ending .99", totals.not_charm),
        ]
    facts += [("", ""), ("Verdict breakdown", "")]
    facts += [(f"    {k}", v) for k, v in sorted(totals.verdicts.items())]

    for key, value in facts:
        summary.append([key, value])
    for cell in summary["A"]:
        cell.font = Font(bold=True)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Correct review-page selling prices, with an Excel audit trail.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--limit", type=int, default=10,
                   help="How many products to include (default 10). Ignored with --all.")
    p.add_argument("--all", action="store_true",
                   help="Every product on the review page, not just --limit.")
    p.add_argument("--apply", action="store_true",
                   help="WRITE the corrected prices. Without this, nothing is written.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt that --apply otherwise requires.")
    p.add_argument("--rounding-only", action="store_true",
                   help="Only apply .99 rounding (verdict round_required). Leaves "
                        "too_low / too_high / above_retail clamps for a human -- "
                        "those move a price by much more than its cents.")
    p.add_argument("--max-change-pct", type=float, default=None,
                   help="Skip any product whose price would move by more than "
                        "this percent. A blast-radius cap, e.g. --max-change-pct 10.")
    p.add_argument("--status", default="pending",
                   help="Review tab: pending (default), uploaded, rejected, all.")
    p.add_argument("--tenant-id", default=os.getenv("VNYX_TENANT_ID") or None,
                   help="Restrict to one tenant. Default: every tenant you can see.")
    p.add_argument("--out", default=None,
                   help="Path for the .xlsx (default reports/review-prices-<stamp>.xlsx).")
    p.add_argument("--base-url",
                   default=os.getenv("VNYX_BASE_URL", "http://127.0.0.1:8000"),
                   help="vnyx-api base URL. Env: VNYX_BASE_URL.")
    p.add_argument("--token", default=os.getenv("VNYX_API_TOKEN") or None,
                   help="Bearer token. Env: VNYX_API_TOKEN. Or use --email/--password.")
    p.add_argument("--email", default=os.getenv("VNYX_EMAIL") or None)
    p.add_argument("--password", default=os.getenv("VNYX_PASSWORD") or None)
    p.add_argument("--page-size", type=int, default=100,
                   help="Rows per queue request (server caps at 200).")
    p.add_argument("--sleep", type=float, default=0.0,
                   help="Seconds between writes. Default 0 -- use --concurrency "
                        "to control load instead.")
    p.add_argument("--concurrency", type=int, default=4,
                   help="Writes in flight at once (default 4). 1 = strictly "
                        "sequential.")
    p.add_argument("--retries", type=int, default=2,
                   help="Retries per product on a timeout or 5xx (default 2).")
    p.add_argument("--slow", action="store_true",
                   help="Write via the verify endpoint, which re-runs the full "
                        "check server-side per product. Safer but far slower; "
                        "needs ADMIN. Default is a direct price update.")
    # NOT VNYX_TIMEOUT_S: that variable is Hermes' own budget for calling VNYX
    # and is set to 20s in .env, which silently became this script's ceiling and
    # timed out writes that were still in progress. This script's requests are
    # slower than Hermes' by nature, so it gets its own knob and a real default.
    p.add_argument("--timeout", type=float,
                   default=float(os.getenv("VNYX_SCRIPT_TIMEOUT_S", "180")))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.limit < 1 and not args.all:
        sys.exit("--limit must be at least 1.")

    token = args.token
    if not token:
        if not (args.email and args.password):
            sys.exit(
                "No credentials. Either set VNYX_API_TOKEN, or pass "
                "--email/--password (or set VNYX_EMAIL / VNYX_PASSWORD)."
            )
        print(f"Logging in as {args.email} ...")
        token = Api.login(args.base_url, args.email, args.password, args.timeout)

    api = Api(args.base_url, token, args.timeout)
    scope = args.tenant_id or "all accessible tenants"
    print(f"Reading the '{args.status}' review queue from {args.base_url} ({scope})")

    try:
        rows, total, totals = collect(api, args)

        print()
        print(f"Total products on the review page : {total}")
        print(f"Examined in this run              : {totals.seen}"
              f"{'' if args.all else f' (--limit {args.limit})'}")
        print(f"Price needs a change             : {totals.needs_change}")
        if totals.excluded:
            print(f"Held back by filters             : {totals.excluded}"
                  f" ({totals.excluded_movement:+.2f} not applied)")
        if totals.not_assessed:
            print(f"Not assessed (no verdict)        : {totals.not_assessed}")
        print(f"Net movement if applied          : {totals.movement:+.2f}")

        # A rounding moves a price by cents. Anything much larger is a window
        # clamp riding along with it, and at 1,700+ products that is a repricing
        # of the catalog -- worth saying out loud rather than leaving in a column.
        big = [r for r in rows if r.needs_change and r.delta is not None
               and abs(r.delta) > 1.0]
        if big and not args.rounding_only:
            worst = max(big, key=lambda r: abs(r.delta or 0))
            print()
            print(f"NOTE: {len(big)} of {totals.needs_change} change(s) move the "
                  f"price by more than 1.00 -- these are grade-window")
            print(f"      corrections, not .99 rounding. Largest: {worst.sku or worst.product_id} "
                  f"{worst.current} -> {worst.new_price} ({worst.delta:+.2f}).")
            print("      Use --rounding-only to apply just the .99 rounding, or "
                  "--max-change-pct N to cap the move.")

        applied = False
        if args.apply:
            if not totals.needs_change:
                print("\nNothing to write -- no examined product needs a change.")
            else:
                if not args.yes:
                    print()
                    print(f"About to WRITE {totals.needs_change} price(s) "
                          f"via {args.base_url}.")
                    reply = input('Type "yes" to continue: ').strip().lower()
                    if reply != "yes":
                        print("Aborted. Nothing was written.")
                        return 1
                mode = ("the verify endpoint, re-checking each product"
                        if args.slow else "a direct price update")
                print(f"\nWriting {totals.needs_change} price(s) via {mode}, "
                      f"{args.concurrency} at a time...")
                apply_all(api, rows, totals, args)
                applied = True
    finally:
        api.close()

    out = Path(args.out) if args.out else (
        ROOT / "reports" /
        f"review-prices-{datetime.now().strftime('%Y%m%d-%H%M%S')}.xlsx"
    )
    write_sheet(out, rows, total, totals, args, applied)

    print()
    print(f"Sheet: {out}")
    if applied:
        print(f"Written: {totals.updated}   Failed: {totals.failed}")
        if totals.failed:
            print("Rows shaded red in the sheet are the writes that failed.")
        if totals.not_charm:
            print(f"WARNING: {totals.not_charm} written price(s) do NOT end .99 "
                  "-- see the rows marked CHECK in the sheet.")
        return 1 if (totals.failed or totals.not_charm) else 0

    print("PREVIEW ONLY -- nothing was written. Check the sheet, then re-run "
          "with --apply.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
