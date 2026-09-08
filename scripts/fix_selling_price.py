"""Bring Product.price back inside the grade's price window. Bulk, no backend.

WHY NOT THE TYPESCRIPT ONE. That script writes through `updateProduct`, which
re-reads the grade, runs the stage machinery and fires a Shopify enqueue per
product. For a price correction none of that is needed and it costs minutes per
product. This is two SQL statements for the whole sheet.

WHAT THAT COSTS. `updateProduct` is also what pushes a change to the channel.
A direct UPDATE does not, so a product already live on Shopify keeps its old
channel price until something syncs it. The sheet marks those rows, and the
count is printed at the end -- they need a bulk-sync afterwards. Products still
in review have no listing, so nothing is stale for them.

THE RULE is a faithful port of `verifySellingPrice` in
src/services/product-pricing.ts, cross-checked against that implementation on a
sample before this was used:

    expected   = retailPrice x Grade.priceFactor
    minAllowed = charm99(expected x 0.8)
    ceiling    = charm99AtMost(retailPrice x 0.95 - 0.01)
    maxAllowed = min(charm99(expected x 1.2), ceiling)

Inside the window the price stands. Outside it is clamped to the NEAREST edge,
not snapped to `expected` -- the smallest change that makes the record
coherent.

GRADE IS PER TENANT. The product carries a letter in
`qualityGrading->>'grade'`; the factor lives on a Grade row keyed by
(tenantId, code). Codes mean different things per tenant, so the letter alone
is never enough.

Usage:
  python scripts/fix_selling_price.py --sheet <xlsx>              # dry run
  python scripts/fix_selling_price.py --sheet <xlsx> --apply
  ... --only-overpriced   only lower prices above the window
  ... --skip-rounding     leave prices already inside the window alone
  ... --out <xlsx>        default: <input>-priced-<when>.xlsx
"""

import argparse
import math
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")
try:
    import psycopg
except ImportError:
    sys.exit("psycopg is required:  pip install psycopg[binary]")

DSN = "postgresql://postgres:Copenhagen%40event1@34.7.102.18:5432/vnyx-prod"
TOLERANCE = 0.20
HARD_MAX_RATIO = 0.95

NEW_COLUMNS = [
    "Grade",
    "Retail price",
    "Selling price (old)",
    "Selling price (new)",
    "Allowed window",
    "Price change",
    "Reason",
    "Shopify",
]
HDR_FILL = PatternFill("solid", fgColor="1F2937")
UP = PatternFill("solid", fgColor="E8F5E9")
DOWN = PatternFill("solid", fgColor="FDECEA")
FLAT = PatternFill("solid", fgColor="F5F5F5")


# --------------------------------------------------------------------------- #
# The rule, ported verbatim
# --------------------------------------------------------------------------- #
def charm99(v: float) -> float:
    if not (v > 0):
        return 0.0
    return round(math.floor(v) + 0.99, 2)


def charm99_at_most(v: float) -> float:
    if v < 0.99:
        return 0.0
    whole = math.floor(v)
    cand = round(whole + 0.99, 2)
    if cand <= v + 1e-9:
        return cand
    return round(max(whole - 1 + 0.99, 0), 2)


def retail_ceiling(retail: float) -> float:
    return charm99_at_most(retail * HARD_MAX_RATIO - 0.01)


def to_fixed2(x: float) -> float:
    """JavaScript's Number.prototype.toFixed(2), exactly.

    LOAD-BEARING, and it cost a wrong answer to find. `applyGradePriceFactor`
    returns `(retail * factor).toFixed(2)` as a STRING which the verifier then
    parses back, so `expected` is already rounded to cents before the window is
    computed. Skipping that step changes the window: retail 44.99 at factor 0.5
    is 22.495 raw, which gives a maximum of 26.99, against 22.50 rounded, which
    gives 27.99 -- a whole euro of difference on the ceiling.

    Decimal(float) rather than Decimal(repr(float)): toFixed rounds the exact
    binary double, and repr() gives the shortest string that round-trips, which
    is not always the same number to round.
    """
    from decimal import Decimal, ROUND_HALF_UP

    return float(Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def money(v) -> float | None:
    if v is None:
        return None
    s = str(v).strip().replace("€", "").replace(",", "").strip()
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return n if n > 0 else None


def verdict(price, retail, factor, mult_enabled):
    """(new_price|None, reason, window_or_None). new_price None = no change."""
    actual = money(price)
    r = money(retail)

    if mult_enabled:
        return None, "skipped - multiplier derives retail", None
    if factor is None:
        return None, "skipped - grade has no price factor", None
    if r is None:
        return None, "skipped - no retail price", None

    # Rounded to cents FIRST — see to_fixed2. This is applyGradePriceFactor.
    expected = to_fixed2(r * float(factor))
    if not (expected > 0):
        return None, "skipped - expected price not positive", None

    lo = charm99(to_fixed2(expected * (1 - TOLERANCE)))
    hi = min(charm99(to_fixed2(expected * (1 + TOLERANCE))), retail_ceiling(r))
    window = f"{lo:.2f} - {hi:.2f}"

    if lo > hi:
        return None, "skipped - window sits above the retail ceiling", window
    if actual is None:
        target = min(max(charm99(expected), lo), hi)
        return target, "no price today - set from the window", window

    if lo <= actual <= hi:
        rounded = min(charm99(actual), hi)
        if abs(rounded - actual) <= 0.005:
            return None, "already inside the window", window
        return rounded, "inside the window, rounded to .99", window

    clamped = lo if actual < lo else hi
    side = "below" if actual < lo else "above"
    return clamped, f"{side} the window - clamped to the nearest edge", window


# --------------------------------------------------------------------------- #
def header_row(rows):
    for i, r in enumerate(rows):
        if r and any(str(c).strip() == "Product ID" for c in r if c is not None):
            return i
    return None


SELECT_SQL = """
SELECT p.id::text,
       p.price,
       p."retailPrice",
       p."qualityGrading"->>'grade'            AS grade,
       g."priceFactor",
       g."priceMultiplierEnabled",
       (l."externalListingId" IS NOT NULL)     AS live_on_channel
FROM "Product" p
LEFT JOIN "Grade" g
       ON g.code = p."qualityGrading"->>'grade'
      AND g."tenantId" = p."tenantId"
LEFT JOIN "MarketplaceListing" l ON l."productId" = p.id
WHERE p.id = ANY(%s::uuid[])
"""


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--out")
    ap.add_argument("--db", default=DSN)
    ap.add_argument("--apply", action="store_true",
                    help="Write the prices. Without it, a dry run that still "
                         "produces the full sheet.")
    ap.add_argument("--only-overpriced", action="store_true",
                    help="Only lower prices above the window; never raise.")
    ap.add_argument("--skip-rounding", action="store_true",
                    help="Leave prices already inside the window alone.")
    args = ap.parse_args()

    src = Path(args.sheet).expanduser()
    out = (Path(args.out).expanduser() if args.out
           else src.with_name(
               f"{src.stem}-priced-{datetime.now():%Y-%m-%d-%H%M}.xlsx"))

    wb = openpyxl.load_workbook(src)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    head = header_row(rows)
    if head is None:
        sys.exit("No 'Product ID' column found.")
    hdr = [str(c) if c is not None else "" for c in rows[head]]
    id_col = hdr.index("Product ID") + 1

    order, row_of = [], {}
    for r in range(head + 2, ws.max_row + 1):
        v = ws.cell(row=r, column=id_col).value
        if not v:
            continue
        pid = str(v).strip()
        order.append(pid)
        row_of.setdefault(pid, []).append(r)

    print(f"Sheet     : {src.name}")
    print(f"Products  : {len(order):,}")
    print(f"Mode      : {'APPLY - writing prices' if args.apply else 'DRY RUN'}")
    if args.only_overpriced:
        print("Scope     : only prices ABOVE the window")
    if args.skip_rounding:
        print("Scope     : skipping .99 rounding-only changes")

    # ---- one read for everything ------------------------------------------
    with psycopg.connect(args.db, connect_timeout=40) as cn, cn.cursor() as cur:
        cur.execute(SELECT_SQL, (order,))
        db = {r[0]: r for r in cur.fetchall()}
    print(f"Found     : {len(db):,} in the database\n")

    updates, tally, live_changing = [], {}, 0
    verdicts = {}

    for pid in order:
        rec = db.get(pid)
        if rec is None:
            verdicts[pid] = (None, None, None, "product not found", None, False)
            tally["not found"] = tally.get("not found", 0) + 1
            continue
        _id, price, retail, grade, factor, mult, live = rec
        new, reason, window = verdict(price, retail, factor, mult)

        if new is not None:
            before = money(price)
            direction = ("set" if before is None
                         else "raise" if new > before else "lower")
            if args.only_overpriced and direction != "lower":
                new, reason = None, f"{direction} - skipped (--only-overpriced)"
            elif args.skip_rounding and "rounded to .99" in reason:
                new, reason = None, "rounding only - skipped (--skip-rounding)"

        verdicts[pid] = (grade, retail, price, reason, window, live)
        key = reason.split(" - ")[0]
        tally[key] = tally.get(key, 0) + 1

        if new is not None:
            # Keep whatever format this product already used: 2,593 rows store
            # the euro symbol and 12,504 do not, and a batch is no place to
            # migrate one into the other as a side effect.
            text = (f"€{new:.2f}" if "€" in str(price or "")
                    else f"{new:.2f}")
            updates.append((pid, text))
            verdicts[pid] = (grade, retail, price, reason, window, live)
            if live:
                live_changing += 1

    print("--- verdicts ---")
    for k, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {n:6,}  {k}")
    print(f"\n  {len(updates):,} price change(s)"
          f"{'' if args.apply else ' (dry run)'}")

    # ---- one write for everything -----------------------------------------
    if args.apply and updates:
        with psycopg.connect(args.db, connect_timeout=60) as cn:
            with cn.cursor() as cur:
                CHUNK = 500
                done = 0
                for i in range(0, len(updates), CHUNK):
                    part = updates[i:i + CHUNK]
                    values = ",".join(["(%s::uuid,%s)"] * len(part))
                    flat: list = []
                    for pid, text in part:
                        flat += [pid, text]
                    cur.execute(
                        f'UPDATE "Product" AS p '
                        f'SET price = v.price, "updatedAt" = now() '
                        f'FROM (VALUES {values}) AS v(id, price) '
                        f'WHERE p.id = v.id',
                        flat,
                    )
                    done += cur.rowcount
                    print(f"  written {done:,}/{len(updates):,}")
            cn.commit()
        print(f"\n  {done:,} row(s) updated")

    # ---- the sheet ---------------------------------------------------------
    new_at = {}
    for pid, text in updates:
        new_at[pid] = text

    start = max(i for i, c in enumerate(rows[head], start=1) if c is not None) + 1
    for off, name in enumerate(NEW_COLUMNS):
        c = ws.cell(row=head + 1, column=start + off, value=name)
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HDR_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(start + off)].width = (
            46 if name == "Reason" else 18)

    for pid, rlist in row_of.items():
        grade, retail, old, reason, window, live = verdicts.get(
            pid, (None, None, None, "not processed", None, False))
        new_text = new_at.get(pid)
        before, after = money(old), money(new_text)
        delta = (f"{after - before:+.2f}"
                 if before is not None and after is not None else "")
        for r in rlist:
            for off, value in enumerate([
                grade, retail, old, new_text or "", window or "", delta,
                reason, "live - needs a sync" if live and new_text else
                ("live" if live else "not on Shopify"),
            ]):
                cell = ws.cell(row=r, column=start + off, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=False)
            fill = FLAT
            if delta.startswith("+"):
                fill = UP
            elif delta.startswith("-"):
                fill = DOWN
            for off in range(len(NEW_COLUMNS)):
                ws.cell(row=r, column=start + off).fill = fill

    tmp = out.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)
    print(f"\n  sheet written to {out}")

    if live_changing:
        print(f"\n  {live_changing:,} changed product(s) are LIVE on Shopify.")
        print("  A direct database write does not reach the channel, so their")
        print("  listing price is now stale. Re-sync them per tenant:")
        print("    POST /marketplace-accounts/bulk-sync?tenantId=<uuid>")
    if not args.apply:
        print("\nDry run - nothing written. Re-run with --apply.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
