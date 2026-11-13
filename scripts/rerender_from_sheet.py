#!/usr/bin/env python
"""Re-render a product list's AI images on the current prompt — renders ONLY.

    # look first: which products and views would be re-rendered, nothing made
    python scripts/rerender_from_sheet.py --sheet reports/shoes_rerender_4_nov/shoes_review_approved.xlsx \\
        --before "2026-10-31 20:00"

    # re-render, check, re-push to Shopify
    python scripts/rerender_from_sheet.py --sheet ... --before "2026-10-31 20:00" --apply --workers 4

    # one product (or a few): --product BOA-007115 --product KLE-002797

WHY NOT run_from_sheet --rerender (4 Nov 2026, the user): that runs the WHOLE chain —
reconcile, copy, data repairs, the approval — on every product. For a backlog whose
only problem is pictures made on the old prompt (the footwear framing), this does the
pictures and nothing else:

  1. the product's AI views whose current render was made before --before
     (repair_product.stale_render_views — the same rule --rerender uses);
  2. re-rendered through vnyx-api's backfill-imagery.ts, exactly as the chain's regen
     step calls it (by the product's sequence, `--replace`, when it has one);
  3. judged by the image gate and the photo audit, and whatever they refuse is
     re-rendered again — up to --rounds rounds in all;
  4. the gallery re-ordered (rebuild-media-cache.ts, the chain's `order` step);
  5. re-pushed to Shopify (resync-listings.ts) when the product is on Shopify — never
     an Archived product that still holds stock, the chain's ARCHIVED_STOCKED rule.

It never touches the data, the price, the cut-outs, the copy, the stage or the
approval, and it opens no AutoApprovalRun: the Runs tab is for verification, and
nothing here is verified. Writes need --apply; without it the plan is printed.

The vnyx-api transport and the database are the chain's own (VNYX_API_URL /
DATABASE_URL), and the preflight refuses to start when the two name different
databases. Paid: one image-model call per view rendered.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import repair_product as rp  # noqa: E402
import run_from_sheet as rfs  # noqa: E402

from app.config import policy  # noqa: E402
from app.services.auto_approval import db, preflight  # noqa: E402

_print_lock = threading.Lock()


def _when(value: str) -> datetime:
    """An ISO date/time; without an offset it is UTC."""
    try:
        t = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a date/time: {value!r} (e.g. '2026-10-31 20:00')") from exc
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _resolve(entries: list[dict[str, str]]) -> list[tuple[str, str]]:
    """(product id, sku) for each sheet row — by id, else by SKU."""
    out: list[tuple[str, str]] = []
    for e in entries:
        if e["id"]:
            row = db.fetch_one('SELECT id::text AS id, sku FROM "Product" WHERE id = %(v)s::uuid', {"v": e["id"]})
        else:
            row = db.fetch_one('SELECT id::text AS id, sku FROM "Product" WHERE sku = %(v)s', {"v": e["sku"]})
        if row:
            out.append((row["id"], row["sku"]))
        else:
            print(f"  NOT FOUND: {e['id'] or e['sku']}")
    return out


def _price_change_pending(product_id: str) -> bool:
    """A listing of this product has a channel-price change waiting to go out. Any
    channel, deliberately: stricter than Shopify alone, and it needs no join."""
    row = db.fetch_one(
        'SELECT bool_or(COALESCE("pendingPriceSync", false)) AS pending'
        '  FROM "MarketplaceListing" WHERE "productId" = %(p)s::uuid', {"p": product_id})
    return bool(row and row["pending"])


def _read_render(out: str) -> tuple[list[str], list[str]]:
    """(written, failed) labels from backfill-imagery.ts's summary lines."""
    written: list[str] = []
    failed: list[str] = []
    for line in (out or "").splitlines():
        m = re.search(r"wrote \[([^\]]*)\]", line)
        if m:
            written = [v.strip() for v in m.group(1).split(",") if v.strip() and v.strip() != "nothing"]
        m = re.search(r"could not produce \[([^\]]*)\]", line)
        if m:
            failed = [v.strip() for v in m.group(1).split(",") if v.strip()]
    return written, failed


def _judge(dsn: str, product_id: str) -> tuple[list[str], list[str]]:
    """(views the checks refuse, their reasons), on the rows as they are now."""
    from app import readiness
    from app.imaging import photo_audit, quality_gate

    pol = policy()
    fresh = rp.needs(dsn, product_id)["loaded"]
    record = fresh["record"]
    gate = quality_gate.judge(
        fresh["media"],
        gender=(readiness.root_gender(record.get("masterCategory"), pol) or record.get("gender")),
        category=record.get("category"), subcategory=record.get("subCategory"), pol=pol,
        size=record.get("size") or record.get("internationalSize"),
        kids=readiness.is_kids(record.get("masterCategory"), record.get("mannequinType"), pol,
                             size=record.get("internationalSize") or record.get("size"),
                             category=record.get("category")),
    )
    photos = photo_audit.judge(
        fresh["media"],
        grade_severity=record.get("gradeSeverity"),
        grade_label=record.get("gradeLabel") or record.get("grade"),
        pol=pol,
        product_gender=readiness.root_gender(record.get("masterCategory"), pol),
        category=record.get("category"), subcategory=record.get("subCategory"),
    )
    views: list[str] = []
    why: list[str] = []
    if gate.action == "regen":
        views += quality_gate.regen_views(gate, pol)
        why.append(f"gate {gate.code}: {'; '.join(gate.reasons)}")
    pv = photo_audit.regen_views(photos, pol)
    if pv:
        views += pv
        why.append("photos: " + "; ".join(r for r in photos.reasons if r.startswith("RENDER DEFECT")))
    return sorted(set(views)), why


def rerender_one(dsn: str, product_id: str, sku: str, *, before: datetime, apply: bool,
                 rounds: int, vnyx_api: Path, only: list[str] | None = None,
                 via_hermes: bool = False) -> dict[str, Any]:
    """One product: stale views → render → check → re-render refused → order → Shopify."""
    t0 = time.monotonic()
    log: list[str] = []
    res: dict[str, Any] = {"sku": sku, "id": product_id, "result": "", "log": log}
    stale = rp.stale_render_views(dsn, product_id, before)
    if only:
        # --views (13 Nov 2026): the three-quarter back alone, say — the other renders
        # are fine and each costs a paid call.
        stale = [v for v in stale if v in only]
    if not stale:
        res["result"] = "SKIPPED — every AI image is already on the current prompt"
        return res
    state = rp.needs(dsn, product_id)
    # --via-hermes (13 Nov 2026): render through vnyx-api's non-sequence path, which
    # asks Hermes' /v1/imagery/generate for the picture — Hermes' prompt (the fixed rear
    # three-quarter, the kids-size re-cast) without waiting for a vnyx-api deploy.
    # `--views` without `--sequence` re-makes each named view and retires its old render.
    by_seq = rp._sequence_render(state) and not via_hermes
    if by_seq:
        seq_views = rp._sequence_views(state) or []
        outside = [v for v in stale if v not in seq_views]
        stale = [v for v in seq_views if v in stale]
        if outside:
            log.append(f"not in the product's sequence, left as is: {', '.join(outside)}")
    if not stale:
        res["result"] = "SKIPPED — the old renders are not in the product's sequence"
        return res

    common = ["--db", dsn, "--product", product_id]
    live = ["--apply"] if apply else []
    seq_args = ["--sequence", "--replace"] if by_seq else []
    if not apply:
        res["result"] = f"WOULD re-render {', '.join(stale)}{' (by its sequence)' if by_seq else ''}"
        return res

    views = stale
    made_any = False
    refused: list[str] = []
    for n in range(1, rounds + 1):
        ok, out, _ = rp.run_step(vnyx_api, "backfill-imagery.ts",
                                 [*common, *live, "--views", ",".join(views), *seq_args],
                                 timeout_s=1800, quiet=True)
        written, failed = _read_render(out)
        made_any = made_any or bool(written)
        log.append(f"round {n}: rendered {', '.join(views)} — wrote {', '.join(written) or 'nothing'}"
                   + (f"; could not produce {', '.join(failed)}" if failed else "")
                   + ("" if ok else "; backfill-imagery.ts returned non-zero"))
        try:
            refused, why = _judge(dsn, product_id)
        except Exception as exc:  # noqa: BLE001 — a failed look is reported, not fatal
            log.append(f"round {n}: the checks could not run ({type(exc).__name__}: {exc})")
            refused, why = [], []
            break
        if not refused:
            log.append(f"round {n}: checks passed")
            break
        log.append(f"round {n}: checks refused {', '.join(refused)} — {' | '.join(why)}")
        views = [v for v in refused if not by_seq or v in (rp._sequence_views(state) or [])] or refused

    if made_any:
        # the chain's `order` step with its default (a person's manual order respected)
        ok, _, _ = rp.run_step(vnyx_api, "rebuild-media-cache.ts", [*common, *live], timeout_s=120, quiet=True)
        log.append("gallery re-ordered" if ok else "gallery re-order FAILED")

        # Shopify: only a product already listed — and only one whose push can carry
        # the new pictures and NOTHING ELSE that changes (the user, 4 Nov 2026):
        #   * Active here: an update of an Active product leaves Shopify's status out
        #     of the PUT (vnyx-api upsert.ts keepShopifyStatus), so a Shopify draft
        #     stays a draft. A Draft or Archived product's push SENDS its status, so
        #     it is not pushed at all — its new pictures stay in vnyx.
        #   * no price change waiting on the listing (pendingPriceSync): the push
        #     sends vnyx's price, and with a change pending that would move it.
        status = rp.vnyx_status(dsn, product_id)
        if not rp.shopify_product_id(dsn, product_id):
            log.append("not on Shopify — nothing pushed")
        elif status != "Active":
            log.append(f"{status} in vnyx — NOT pushed (a push would send its status); "
                       "the new pictures are in vnyx only")
        elif _price_change_pending(product_id):
            log.append("a price change is waiting on the Shopify listing — NOT pushed, "
                       "so the push cannot move the price")
        elif not rp.remote_supports("includeSynced"):
            log.append("NOT pushed — this vnyx-api cannot re-push a synced listing; deploy vnyx-api")
        else:
            ok, out, _ = rp.run_step(vnyx_api, "resync-listings.ts",
                                     ["--product", product_id, "--include-synced", *live],
                                     timeout_s=120, quiet=True)
            m = re.search(r"queued\s*:\s*(\d+)", out or "")
            log.append("Shopify push queued" if ok and m and int(m.group(1)) else "Shopify push NOT queued")

    still_old = rp.stale_render_views(dsn, product_id, before)
    if refused:
        res["result"] = f"RE-RENDERED, but the checks still refuse {', '.join(refused)} — look at it"
    elif still_old:
        res["result"] = f"PARTLY — still old: {', '.join(still_old)}"
    else:
        res["result"] = "RE-RENDERED — every AI image is on the current prompt and passed the checks"
    res["secs"] = round(time.monotonic() - t0)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="xlsx with a Product id or SKU column")
    src.add_argument("--product", action="append", metavar="SKU_OR_ID", help="one product; repeat for a few")
    ap.add_argument("--before", type=_when, required=True, metavar="WHEN",
                    help="re-render the AI images made before this time (UTC unless an offset is given)")
    ap.add_argument("--apply", action="store_true", help="render and write; without it the plan is printed")
    ap.add_argument("--workers", type=int, default=1, metavar="N", help="products at a time (default 1)")
    ap.add_argument("--rounds", type=int, default=2,
                    help="render rounds per product, the first included (default 2: one retry of what the checks refuse)")
    ap.add_argument("--views", default="", metavar="AI_BACK_34[,…]",
                    help="only these AI views (e.g. AI_BACK_34); default every old one")
    ap.add_argument("--via-hermes", action="store_true",
                    help="render through Hermes' generator (the non-sequence path), not vnyx-api's sequence renderer")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    pf = preflight.check()
    print(f"transport     : {pf.get('transport')}")
    print(f"our database  : {pf.get('database')}")
    print(f"their database: {pf.get('remoteDatabase')}")
    if not pf["ok"]:
        for p in pf["problems"]:
            print(f"  PROBLEM: {p}")
        return 2

    entries = [rfs.product_entry(v) for v in args.product] if args.product else rfs.read_sheet(Path(args.sheet))
    products = _resolve(entries)
    if args.limit:
        products = products[: args.limit]
    print(f"\n{len(products)} product(s) · re-render AI images made before "
          f"{args.before:%Y-%m-%d %H:%M %Z} · {'LIVE' if args.apply else 'DRY RUN — nothing is rendered'}"
          f" · {args.workers} worker(s) · {args.rounds} round(s)")
    if args.apply and not args.yes:
        if input("Re-render now? This calls the paid image model. [y/N] ").strip().lower() != "y":
            print("stopped")
            return 1

    dsn = db.dsn()
    vnyx_api = Path(rp.DEFAULT_VNYX_API)
    tally: dict[str, int] = {}
    t0 = time.monotonic()

    def run(i: int, pid: str, sku: str) -> None:
        try:
            r = rerender_one(dsn, pid, sku, before=args.before, apply=args.apply,
                             rounds=max(1, args.rounds), vnyx_api=vnyx_api,
                             only=[v.strip().upper() for v in args.views.split(",") if v.strip()] or None,
                             via_hermes=args.via_hermes)
        except Exception as exc:  # noqa: BLE001 — one product must not stop the batch
            r = {"sku": sku, "result": f"FAILED — {type(exc).__name__}: {exc}", "log": []}
        key = r["result"].split(" ")[0].rstrip(",")
        with _print_lock:
            tally[key] = tally.get(key, 0) + 1
            print(f"\n[{i}/{len(products)}] {sku}" + (f"  ({r['secs']}s)" if r.get("secs") else ""))
            for line in r["log"]:
                print(f"    {line}")
            print(f"    -> {r['result']}", flush=True)

    if args.workers > 1:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run, i, pid, sku) for i, (pid, sku) in enumerate(products, 1)]
            for f in as_completed(futures):
                f.result()
    else:
        for i, (pid, sku) in enumerate(products, 1):
            run(i, pid, sku)

    print("\n" + "-" * 58)
    print(" · ".join(f"{k.lower()} {v}" for k, v in sorted(tally.items())) or "nothing done")
    print(f"{(time.monotonic() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    sys.exit(main())
