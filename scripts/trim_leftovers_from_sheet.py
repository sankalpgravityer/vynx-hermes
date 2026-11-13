#!/usr/bin/env python
"""Retire AI renders an OLDER set left behind, re-order the gallery, re-push to Shopify.

    # look first: which renders would go, nothing written
    python scripts/trim_leftovers_from_sheet.py --sheet ~/leftover_renders.xlsx

    # retire, re-order, push
    python scripts/trim_leftovers_from_sheet.py --sheet ~/leftover_renders.xlsx --apply --workers 2 --yes

WHY (13 Nov 2026, the client: "the second model doesn't match the listing", BOA-007716):
a sequence re-render re-makes only the sequence's shots, so an older set's renders the
sequence no longer has — a second full front and a close-up from 26 Oct, on a woman —
stayed live beside the new men's set. The chain now retires them after every re-render
(repair_product `regen`); this cleans the products already approved.

The rule is the chain's own `seqtrim` (shot_sequence.leftover_renders): a render goes
only when every shot of the product's sequence has a render, it is outside the sequence
or a surplus copy of a shot (the newest are kept), and it is older than every render
kept. Soft-deleted (deletedAt), never removed. Then rebuild-media-cache.ts and the push
under recut_from_sheet.py's rules (Active in vnyx, no pending price change). Nothing
else is touched. Writes need --apply.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import recut_from_sheet as rc  # noqa: E402
import repair_product as rp  # noqa: E402
import rerender_from_sheet as rr  # noqa: E402
import run_from_sheet as rfs  # noqa: E402

from app.rules import shot_sequence  # noqa: E402
from app.services.auto_approval import db, preflight  # noqa: E402

_print_lock = threading.Lock()


def leftovers(dsn: str, product_id: str) -> list[dict[str, Any]]:
    resolved = shot_sequence.from_payload(
        (rp.needs(dsn, product_id)["loaded"]["record"] or {}).get("shotSequence"))
    if resolved is None:
        return []
    rows = db.fetch_all(
        'SELECT id::text, url, view::text, "shotKey", "createdAt" FROM "ProductMedia"'
        ' WHERE "productId" = %(p)s::uuid AND "deletedAt" IS NULL AND "isCurrent" = true'
        "   AND \"mediaType\"::text = 'IMAGE' AND view::text LIKE 'AI\\_%%'",
        {"p": product_id})
    return shot_sequence.leftover_renders(resolved, rows)


def trim_one(dsn: str, product_id: str, sku: str, *, apply: bool, vnyx_api: Path) -> dict[str, Any]:
    log: list[str] = []
    old = leftovers(dsn, product_id)
    names = ", ".join(f'{r["view"]} ({r["createdAt"]:%d %b})' for r in old)
    if not old:
        return {"sku": sku, "result": "SKIPPED — no render left over from an older set", "log": log}
    if not apply:
        return {"sku": sku, "result": f"WOULD retire {len(old)}: {names}", "log": log}
    n = db.execute(
        'UPDATE "ProductMedia" SET "deletedAt" = now(), "deletedById" = NULL'
        ' WHERE "productId" = %(p)s::uuid AND id::text = ANY(%(ids)s) AND "deletedAt" IS NULL',
        {"p": product_id, "ids": [r["id"] for r in old]})
    log.append(f"retired {n}: {names}")
    log.append(rc._order_and_push(dsn, product_id, vnyx_api, log))
    return {"sku": sku, "result": f"TRIMMED {n}", "log": log}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="xlsx with a Product id or SKU column")
    src.add_argument("--product", action="append", metavar="SKU_OR_ID")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args()

    pf = preflight.check()
    print(f"our database  : {pf.get('database')}\ntheir database: {pf.get('remoteDatabase')}")
    if not pf["ok"]:
        for p in pf["problems"]:
            print(f"  PROBLEM: {p}")
        return 2
    entries = [rfs.product_entry(v) for v in args.product] if args.product else rfs.read_sheet(Path(args.sheet))
    products = list(dict.fromkeys(rr._resolve(entries)))[: args.limit or None]
    print(f"\n{len(products)} product(s) · retire older-set renders · "
          f"{'LIVE' if args.apply else 'DRY RUN'} · {args.workers} worker(s)")
    if args.apply and not args.yes and input("Retire now? [y/N] ").strip().lower() != "y":
        return 1
    dsn, vnyx_api, tally, t0 = db.dsn(), Path(rp.DEFAULT_VNYX_API), {}, time.monotonic()

    def run(i: int, pid: str, sku: str) -> None:
        try:
            r = trim_one(dsn, pid, sku, apply=args.apply, vnyx_api=vnyx_api)
        except Exception as exc:  # noqa: BLE001
            r = {"sku": sku, "result": f"FAILED — {type(exc).__name__}: {exc}", "log": []}
        with _print_lock:
            k = r["result"].split(" ")[0]
            tally[k] = tally.get(k, 0) + 1
            print(f"\n[{i}/{len(products)}] {sku}")
            for line in r["log"]:
                print(f"    {line}")
            print(f"    -> {r['result']}", flush=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for f in as_completed([pool.submit(run, i, pid, sku) for i, (pid, sku) in enumerate(products, 1)]):
            f.result()
    print("\n" + "-" * 58 + "\n" + " · ".join(f"{k.lower()} {v}" for k, v in sorted(tally.items())))
    print(f"{(time.monotonic() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
