#!/usr/bin/env python
"""Re-cut a product list's cut-outs and re-push them to Shopify — cut-outs ONLY.

    # look first: which products would be re-cut, nothing written
    python scripts/recut_from_sheet.py --sheet ~/bad_cutouts_recut.xlsx

    # re-cut, check, re-push to Shopify
    python scripts/recut_from_sheet.py --sheet ~/bad_cutouts_recut.xlsx --apply --workers 4 --yes

    # one product (or a few): --product BOA-000800 --product KLE-002388

WHY NOT run_from_sheet --recut (12 Nov 2026, the user: "i dont want to change any
other data, only fix the cut and sync with shopify"): that runs the WHOLE chain —
master, reconcile, copy, renders, data repairs, the approval. This does the cut-outs
and nothing else:

  1. every cut-out view of the product is re-cut from its original photograph through
     vnyx-api's backfill-bg-removal.ts (Hermes' chain: our models, then Gemini at full
     size; each candidate passes Hermes' tear, leftover-set and AI checks first). A view
     with a photograph but no cut-out yet is cut for the first time.
     WITHOUT --keep-better, on purpose: a person has already judged the cut-out on file
     bad, and the guard keeps the old one whenever the new one has "less garment" —
     which a cut-out with leftover background always does. A view Hermes cannot cut is
     left exactly as it was (vnyx-api writes nothing on a failure);
  2. a duplicate cut-out the re-cut made redundant is soft-deleted (the chain's
     `dedupe` step) and the AI looks at every cut-out the listing shows (the chain's
     `cutcheck`); a view it still refuses is re-cut once with `imagery.cutout.ai_fallback`
     (Gemini) and looked at again;
  3. the gallery re-ordered (rebuild-media-cache.ts — a person's manual order is kept);
  4. re-pushed to Shopify (resync-listings.ts) ONLY when the product is Active in vnyx
     (the push then leaves Shopify's status alone) and no price change is pending on
     the listing (the push sends vnyx's price) — rerender_from_sheet.py's rule.

It never touches the data, the price, the renders, the copy, the stage or the approval,
and opens no AutoApprovalRun. A C-copy (CBOA-, CVIN-, CKIL-) cut from its parent's
photograph also refreshes the parent's cut-out of that photograph — the same picture.
Writes need --apply; without it the plan is printed.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import repair_product as rp  # noqa: E402
import rerender_from_sheet as rr  # noqa: E402
import run_from_sheet as rfs  # noqa: E402

from app.config import policy  # noqa: E402
from app.services.auto_approval import db, preflight  # noqa: E402

_print_lock = threading.Lock()


def _media_rows(product_id: str) -> list[dict[str, Any]]:
    """The rows the chain's dedupe and cutcheck read."""
    return db.fetch_all(
        'SELECT id::text, url, view::text, origin::text, processing::text,'
        ' "derivedFromId"::text AS "derivedFromId", "deletedAt", "createdAt", "isCurrent"'
        ' FROM "ProductMedia" WHERE "productId" = %(p)s::uuid AND "mediaType"::text = \'IMAGE\'',
        {"p": product_id})


def _cut_views(product_id: str) -> list[str]:
    """Garment views with a live photograph or cut-out — what a re-cut can redo."""
    rows = db.fetch_all(
        'SELECT DISTINCT view::text AS view FROM "ProductMedia" WHERE "productId" = %(p)s::uuid'
        ' AND "deletedAt" IS NULL AND "mediaType"::text = \'IMAGE\''
        " AND processing::text IN ('RAW', 'BG_REMOVED')"
        " AND view::text NOT IN ('LABEL', 'SIZE_CHART') AND view::text NOT LIKE 'AI\\_%%'",
        {"p": product_id})
    return sorted(r["view"] for r in rows)


def _recut(dsn: str, product_id: str, vnyx_api: Path, views: list[str] | None,
           strategies: list[str] | None) -> tuple[bool, list[str]]:
    """One backfill-bg-removal.ts call: (ok, its summary lines). Never raises."""
    args = ["--db", dsn, "--product", product_id, "--apply", "--provider", "hermes", "--replace"]
    if views is not None:
        # SKU-000055 (12 Nov 2026): vnyx-api's re-cut call takes FRONT and BACK only and
        # answered 400 for OTHER, which also threw away the product's whole log.
        named = [v for v in views if v in REDO_VIEWS]
        if not named:
            return False, [f"- {v} FAILED: vnyx-api re-cuts FRONT and BACK only — {v} left as it was"
                           for v in views]
        views = named
    if views and rp.remote_supports("matteViews"):
        args += ["--views", ",".join(views)]
    if strategies:
        args += ["--bg-strategies", ",".join(strategies)]
    try:
        ok, out, _ = rp.run_step(vnyx_api, "backfill-bg-removal.ts", args, timeout_s=900, quiet=True)
    except rp.StepFailed as exc:
        return False, [f"- {v} FAILED: {exc}" for v in (views or ["ALL"])]
    lines = [ln.strip() for ln in (out or "").splitlines()
             if re.search(r"->|KEPT|FAILED|failed|error|written|cut-outs", ln)]
    return ok, lines


def _outcome(lines: list[str], written: set[str], failed: dict[str, str]) -> None:
    """Fold one re-cut's per-view lines into (views written, views refused → why).

    12 Nov 2026 (BOA-002434, BOA-006434, VIN-000639, VIN-001449): every attempt was
    refused, so vnyx-api wrote nothing and the OLD cut-out stayed — and the AI, shown
    that old cut-out, passed it, so the run said "passed the AI check" about a picture
    it never changed. What was written is now counted, not inferred from the look."""
    for ln in lines:
        m = re.match(r"\S+ (\w+)\s+(->|KEPT|FAILED:?)\s*(.*)", ln)
        if not m:
            continue
        view, what, why = m.groups()
        if what == "->":
            written.add(view)
            failed.pop(view, None)
        else:
            failed[view] = ("network error (fetch failed) — run it again" if "fetch failed" in why
                            else (why.split(": ", 1)[-1][:160] or what))


# The views vnyx-api's backfill-bg-removal.ts accepts in --views.
REDO_VIEWS = ("FRONT", "BACK")


def _dedupe(dsn: str, product_id: str) -> int:
    from app.imaging import gallery_cleanup as gc

    dupes = gc.classify(_media_rows(product_id), [])["duplicates"]
    if not dupes:
        return 0
    return db.execute(
        'UPDATE "ProductMedia" SET "deletedAt" = now(), "deletedById" = NULL'
        ' WHERE "productId" = %(p)s::uuid AND id::text = ANY(%(ids)s) AND "deletedAt" IS NULL',
        {"p": product_id, "ids": [d["id"] for d in dupes]})


def _look(product_id: str) -> Any:
    """The chain's cutcheck: the AI against every cut-out the listing shows."""
    from app.imaging import cutout_integrity as ci

    prod = db.fetch_one('SELECT images, category, "subCategory" AS sub FROM "Product"'
                        ' WHERE id = %(p)s::uuid', {"p": product_id}) or {}
    category = " > ".join(x for x in (prod.get("category"), prod.get("sub")) if x) or None
    return ci.check(_media_rows(product_id), list(prod.get("images") or []),
                    category=category, pol=policy())


# STRICTER THAN THE CHAIN (12 Nov 2026, BEV-000103). The chain holds broken cloth only
# on a strap, waistband or belt (cutout_integrity._HOLDS_BROKEN — the AI cried wolf on
# collars in the 17 Oct review) and NOTES the rest. Every product here was judged bad
# by a person, so a CLEAR broken-cloth note is believed: BEV-000103's v2 cut-out lost
# the black zip tape, the collar lining and the cuffs, the AI said so ("cloth broken
# (zipper) [clear]", "(collar) [clear]"), and the note let it pass. Gemini's cut of the
# same photo kept all three.
_STRICT = re.compile(r"CUTOUT DEFECT — (\S+)(?: \d+)? cut-out: cloth broken.*\[clear\]")


def _bad_views(verdict: Any) -> list[str]:
    """The views the chain's verdict holds, plus those with a clear broken-cloth note."""
    views = [b.split()[0] for b in (verdict.bad_views or [])] if verdict.action == "bad" else []
    for note in verdict.soft or []:
        m = _STRICT.match(note)
        if m:
            views.append(m.group(1))
    return list(dict.fromkeys(views))


def _fallback() -> list[str]:
    from app.imaging import cutout as co

    return [str(n) for n in (co.config(policy()).get("ai_fallback") or []) if n in co.STRATEGY_NAMES]


def recut_one(dsn: str, product_id: str, sku: str, *, apply: bool, vnyx_api: Path) -> dict[str, Any]:
    t0 = time.monotonic()
    log: list[str] = []
    res: dict[str, Any] = {"sku": sku, "id": product_id, "result": "", "log": log}
    views = _cut_views(product_id)
    if not views:
        res["result"] = "SKIPPED — no garment photograph or cut-out to cut"
        return res
    if not apply:
        res["result"] = f"WOULD re-cut {', '.join(views)}"
        return res

    written: set[str] = set()
    failed: dict[str, str] = {}
    ok, lines = _recut(dsn, product_id, vnyx_api, None, None)
    _outcome(lines, written, failed)
    log.append(f"re-cut {', '.join(views)}" + ("" if ok else " — backfill-bg-removal.ts returned non-zero"))
    log += [f"  {ln}" for ln in lines[-8:]]
    n = _dedupe(dsn, product_id)
    if n:
        log.append(f"removed {n} duplicate cut-out(s) the re-cut made redundant")

    from app.imaging import cutout_integrity as ci

    verdict = _look(product_id)
    bad = [v for v in _bad_views(verdict) if v in views]
    if bad:
        log.append("AI check: " + "; ".join(verdict.reasons + [n for n in verdict.soft if _STRICT.match(n)]))
        plan = _fallback()
        if plan and rp.remote_supports("bgStrategies"):
            ok, lines = _recut(dsn, product_id, vnyx_api, bad, plan)
            _outcome(lines, written, failed)
            log.append(f"re-cut {', '.join(bad)} with {', '.join(plan)}" + ("" if ok else " — non-zero"))
            log += [f"  {ln}" for ln in lines[-8:]]
            n = _dedupe(dsn, product_id)
            if n:
                log.append(f"removed {n} duplicate cut-out(s)")
            verdict = _look(product_id)
            bad = [v for v in _bad_views(verdict) if v in views]
    if verdict.action == "unknown":
        log.append("AI check could not run: " + (verdict.note or ""))
    elif bad:
        log.append("AI check STILL: " + "; ".join(verdict.reasons + [n for n in verdict.soft if _STRICT.match(n)]))
    else:
        log.append("AI check: " + ci.summary(verdict))

    log.append(_order_and_push(dsn, product_id, vnyx_api, log))

    kept = {v: w for v, w in failed.items() if v not in written}
    kept_txt = "; ".join(f"{v}: {w}" for v, w in kept.items())
    if not written:
        res["result"] = f"NOT RE-CUT — every attempt was refused, the old cut-outs are still live ({kept_txt})"
    elif kept:
        res["result"] = (f"PARTLY — {', '.join(sorted(written))} re-cut; {', '.join(kept)} kept the old "
                         f"cut-out ({kept_txt})" + (" — and the AI check still refuses it" if bad else ""))
    elif bad:
        res["result"] = "RE-CUT, but the AI check still refuses it — look at it"
    elif verdict.action == "unknown":
        res["result"] = "RE-CUT — the AI check could not run"
    else:
        res["result"] = "RE-CUT — passed the AI check"
    res["secs"] = round(time.monotonic() - t0)
    return res


def _order_and_push(dsn: str, product_id: str, vnyx_api: Path, log: list[str]) -> str:
    """The gallery rebuilt, then the Shopify push under the safety rules. Returns the push line."""
    ok, _, _ = rp.run_step(vnyx_api, "rebuild-media-cache.ts",
                           ["--db", dsn, "--product", product_id, "--apply"], timeout_s=120, quiet=True)
    log.append("gallery re-ordered" if ok else "gallery re-order FAILED")

    status = rp.vnyx_status(dsn, product_id)
    if not rp.shopify_product_id(dsn, product_id):
        pushed = "not on Shopify"
    elif status != "Active":
        pushed = f"{status} in vnyx — NOT pushed (a push would send its status)"
    elif rr._price_change_pending(product_id):
        pushed = "a price change is pending on the listing — NOT pushed"
    elif not rp.remote_supports("includeSynced"):
        pushed = "NOT pushed — this vnyx-api cannot re-push a synced listing"
    else:
        ok, out, _ = rp.run_step(vnyx_api, "resync-listings.ts",
                                 ["--product", product_id, "--include-synced", "--apply"],
                                 timeout_s=120, quiet=True)
        m = re.search(r"queued\s*:\s*(\d+)", out or "")
        pushed = "Shopify push queued" if ok and m and int(m.group(1)) else "Shopify push NOT queued"
    return pushed


def push_one(dsn: str, product_id: str, sku: str, *, apply: bool, vnyx_api: Path) -> dict[str, Any]:
    """--push-only: no re-cut — the gallery rebuilt and the Shopify push re-sent (12 Nov
    2026: 16 products' image calls were rate-limited by Shopify during the big run)."""
    log: list[str] = []
    if not apply:
        return {"sku": sku, "id": product_id, "result": "WOULD re-push to Shopify", "log": log}
    pushed = _order_and_push(dsn, product_id, vnyx_api, log)
    log.append(pushed)
    return {"sku": sku, "id": product_id, "result": f"PUSH — {pushed}", "log": log}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--sheet", help="xlsx with a Product id or SKU column")
    src.add_argument("--product", action="append", metavar="SKU_OR_ID", help="one product; repeat for a few")
    ap.add_argument("--apply", action="store_true", help="re-cut and write; without it the plan is printed")
    ap.add_argument("--workers", type=int, default=1, metavar="N", help="products at a time (default 1)")
    ap.add_argument("--limit", type=int, default=0, help="stop after N products")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--push-only", action="store_true",
                    help="no re-cut: rebuild the gallery and re-push to Shopify (same safety rules)")
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
    products = list(dict.fromkeys(rr._resolve(entries)))
    if args.limit:
        products = products[: args.limit]
    print(f"\n{len(products)} product(s) · cut-outs only · "
          f"{'LIVE' if args.apply else 'DRY RUN — nothing is cut'} · {args.workers} worker(s)")
    if args.apply and not args.yes:
        if input("Re-cut now? [y/N] ").strip().lower() != "y":
            print("stopped")
            return 1

    dsn = db.dsn()
    vnyx_api = Path(rp.DEFAULT_VNYX_API)
    tally: dict[str, int] = {}
    t0 = time.monotonic()

    def run(i: int, pid: str, sku: str) -> None:
        try:
            r = (push_one if args.push_only else recut_one)(dsn, pid, sku, apply=args.apply, vnyx_api=vnyx_api)
        except Exception as exc:  # noqa: BLE001 — one product must not stop the batch
            r = {"sku": sku, "result": f"FAILED — {type(exc).__name__}: {exc}", "log": []}
        key = r["result"].split(" — ")[0].split(",")[0]
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
