"""Is a product actually published to Shopify? Read-only.

THREE FACTS, and they move at different times -- which is why the UI badge can
say one thing while the catalogue says another:

  Product.currentStage            APPROVED the instant reviewStatus flips.
                                  Says nothing about the channel.
  MarketplaceListing.lastSyncStatus   PENDING once queued, SUCCESS once the
                                  worker finishes. PENDING with syncAttempts=0
                                  means NOTHING HAS PICKED IT UP -- a job sitting
                                  in a queue no worker is reading, which is what
                                  happens when the enqueue went to a different
                                  Redis than the worker.
  externalListingId               the Shopify product id. This, and only this,
                                  means the listing exists on the store.

Usage:
  python scripts/track_publish.py --sheet <xlsx>            # every id in it
  python scripts/track_publish.py --product <uuid,uuid>
  python scripts/track_publish.py --stage APPROVED          # whole stage
  python scripts/track_publish.py --tenants                 # tenant ids
"""

import argparse
import sys
from collections import Counter

try:
    import psycopg
except ImportError:
    sys.exit("psycopg is required:  pip install psycopg[binary]")

DSN = "postgresql://postgres:Copenhagen%40event1@34.7.102.18:5432/vnyx-prod"

SQL = """
SELECT p.id::text,
       p.title,
       t.name,
       p."currentStage"::text,
       p."reviewStatus"::text,
       p."shopifyProductId",
       l."lastSyncStatus"::text,
       l."externalListingId",
       l."syncAttempts",
       l."lastSyncAt",
       l."lastSyncError"
FROM "Product" p
LEFT JOIN "Tenant" t ON t.id = p."tenantId"
LEFT JOIN "MarketplaceListing" l ON l."productId" = p.id
WHERE {where}
ORDER BY t.name, p.title
"""


def verdict(stage, sync, ext, attempts) -> str:
    """One line a human can act on."""
    if ext:
        return "PUBLISHED"
    if stage != "APPROVED":
        return f"not approved ({stage})"
    if sync is None:
        return "approved, NO LISTING ROW — never enqueued"
    if sync == "PENDING" and not attempts:
        return "approved, QUEUED BUT UNTOUCHED — no worker has read it"
    if sync == "PENDING":
        return f"approved, in progress (attempt {attempts})"
    if sync == "FAILED":
        return "approved, SYNC FAILED"
    if sync == "SKIPPED":
        return "approved, skipped by the sync rules"
    return f"approved, {sync}, no Shopify id yet"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet", help="xlsx with a 'Product ID' column")
    ap.add_argument("--product", "--products", dest="product",
                    help="Comma-separated product uuids.")
    ap.add_argument("--stage", help="Every product at this stage, e.g. APPROVED.")
    ap.add_argument("--tenants", action="store_true",
                    help="Print tenant ids (needed for the bulk-sync call).")
    ap.add_argument("--detail", action="store_true",
                    help="One line per product, not just the totals.")
    ap.add_argument("--db", default=DSN)
    args = ap.parse_args()

    if args.tenants:
        with psycopg.connect(args.db, connect_timeout=30) as cn, cn.cursor() as c:
            c.execute(
                'SELECT t.name, t.id::text, a.status::text '
                'FROM "Tenant" t '
                'LEFT JOIN "MarketplaceAccount" a ON a."tenantId" = t.id '
                'ORDER BY t.name'
            )
            print(f"{'tenant':22} {'tenantId':38} marketplace")
            for name, tid, status in c.fetchall():
                print(f"{str(name)[:21]:22} {tid:38} {status or '(none)'}")
        return 0

    ids: list[str] = []
    if args.sheet:
        import openpyxl
        wb = openpyxl.load_workbook(args.sheet, read_only=True)
        for ws in wb.worksheets:
            rows = list(ws.iter_rows(values_only=True))
            head = next(
                (i for i, r in enumerate(rows)
                 if r and any(str(c).strip() == "Product ID"
                              for c in r if c is not None)),
                None,
            )
            if head is None:
                continue
            col = list(rows[head]).index("Product ID")
            ids += [str(r[col]).strip() for r in rows[head + 1:]
                    if r and r[col]]
        wb.close()
        ids = list(dict.fromkeys(ids))
    if args.product:
        ids += [s.strip() for s in args.product.split(",") if s.strip()]

    if not ids and not args.stage:
        return ap.error("Pass --sheet, --product, --stage or --tenants.")

    where = ('p."currentStage"::text = %(stage)s' if args.stage
             else "p.id = ANY(%(ids)s::uuid[])")
    params = {"stage": args.stage} if args.stage else {"ids": ids}

    with psycopg.connect(args.db, connect_timeout=40) as cn, cn.cursor() as c:
        c.execute(SQL.format(where=where), params)
        rows = c.fetchall()

    tally: Counter = Counter()
    for r in rows:
        (pid, title, tenant, stage, review, shop_id, sync, ext, attempts,
         last_at, err) = r
        v = verdict(stage, sync, ext, attempts)
        tally[v] += 1
        if args.detail:
            print(f"{pid[:8]}  {str(title)[:40]:42} {str(tenant)[:16]:18} {v}")
            if err:
                print(f"          error: {str(err)[:96]}")

    if args.detail:
        print()
    print(f"{len(rows):,} product(s)")
    for v, n in tally.most_common():
        print(f"  {n:6,}  {v}")

    stuck = tally.get("approved, QUEUED BUT UNTOUCHED — no worker has read it", 0)
    if stuck:
        print(f"\n{stuck:,} listing(s) are queued with nothing consuming the "
              f"queue. Re-drive them from the production app:")
        print("  POST /marketplace-accounts/bulk-sync?tenantId=<uuid>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
