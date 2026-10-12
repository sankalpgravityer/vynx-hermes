#!/usr/bin/env python
"""Copy a tenant's sizing guide under a new name — the same sizes and EU sizes.

    python scripts/copy_sizing_guide.py --tenant Klekt --from Shoes --to "Women Shoes"          # dry run
    python scripts/copy_sizing_guide.py --tenant Klekt --from Shoes --to "Women Shoes" --apply  # WRITES

13 Oct 2026: Klekt had one "Shoes" guide for men's and women's shoes alike, and
wanted a "Women Shoes" with the same ladder (EU 35–50) and its own chart image.
The chart image is NOT copied — it is uploaded from the frontend's sizing page
for the new guide. No product is moved onto the new guide by this script.

One ProductSize row, written as vnyx-api's createProductSize writes it (name,
sizes, euSizes, tenantId; sizeChartImages empty). Refuses when the tenant already
has a guide of that name (@@unique([tenantId, name])).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app.services.auto_approval import db  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tenant", required=True, help="tenant name, e.g. Klekt")
    ap.add_argument("--from", dest="source", required=True, help="the guide to copy, e.g. Shoes")
    ap.add_argument("--to", dest="target", required=True, help='the new guide\'s name, e.g. "Women Shoes"')
    ap.add_argument("--apply", action="store_true", help="WRITE the new guide (default: dry run)")
    args = ap.parse_args()
    target = args.target.strip()
    if not target:
        sys.exit("--to is empty")

    with psycopg.connect(db.dsn(), row_factory=dict_row) as conn:
        t = conn.execute('SELECT id::text AS id FROM "Tenant" WHERE name = %(n)s', {"n": args.tenant}).fetchone()
        if not t:
            sys.exit(f"no tenant named {args.tenant!r}")
        guides = conn.execute(
            'SELECT id::text AS id, name, sizes, "euSizes", "sizeChartImages" FROM "ProductSize"'
            ' WHERE "tenantId" = %(t)s::uuid', {"t": t["id"]}).fetchall()
        src = next((g for g in guides if g["name"] == args.source), None)
        if not src:
            sys.exit(f"{args.tenant} has no guide named {args.source!r} "
                     f"(it has: {', '.join(sorted(g['name'] for g in guides))})")
        clash = next((g for g in guides if g["name"].strip().lower() == target.lower()), None)
        if clash:
            sys.exit(f"{args.tenant} already has a guide named {clash['name']!r} - nothing written")

        print(f"tenant : {args.tenant}")
        print(f"copy   : {src['name']!r} -> {target!r}")
        print(f"sizes  : {len(src['sizes'])} - {', '.join(src['sizes'])}")
        print(f"EU     : {len(src['euSizes'])} - {', '.join(src['euSizes'])}")
        print(f"chart  : not copied ({len(src['sizeChartImages'])} on {src['name']!r}) - upload it from the frontend")
        if not args.apply:
            print("\ndry run - nothing written. Add --apply to create it.")
            return 0
        row = conn.execute(
            'INSERT INTO "ProductSize" (id, name, sizes, "euSizes", "sizeChartImages", "tenantId",'
            ' "createdAt", "updatedAt")'
            " VALUES (gen_random_uuid(), %(n)s, %(s)s, %(e)s, '{}', %(t)s::uuid, now(), now())"
            ' RETURNING id::text AS id',
            {"n": target, "s": list(src["sizes"]), "e": list(src["euSizes"]), "t": t["id"]}).fetchone()
        conn.commit()
        print(f"\ncreated {target!r} ({row['id']}) for {args.tenant}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
