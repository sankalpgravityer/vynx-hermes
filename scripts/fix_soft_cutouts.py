"""Re-cut soft garment cut-outs at full resolution, for a list of products.

    python scripts/fix_soft_cutouts.py                          # dry run, the 15 Oct sheet
    python scripts/fix_soft_cutouts.py --apply --workers 2      # write (local DB)
    python scripts/fix_soft_cutouts.py --skus CVIN-001283,BOA-006142 --apply
    python scripts/fix_soft_cutouts.py --apply --sync           # + re-push live listings

WHY (15 Oct 2026). 497 verified live products led with a ~1 MP cut-out
(896x1195 or smaller) while a 3000x4000 photograph sat on file:

  * 350 are C-copies (CBOA-, CVIN-), the gender split. `duplicateProduct`
    clones only current rows, so the copy has its parent's soft intake cut-out
    and no RAW; every re-cut re-framed that cut-out. vnyx-api's
    backfill-bg-removal.ts now finds the parent (src/services/copy-parent.ts)
    and either SHARES the parent's full-resolution cut-out (no cut), or cuts
    ONCE from the parent's photograph and places the result on both products.
  * 147 have their own RAW and were never re-cut, or were re-cut on 6-7 Oct and
    kept the soft size. They are re-cut from their own photograph.

Both go through the chain's own call — `backfill-bg-removal.ts --replace
--keep-better --provider hermes --views <the soft views>` — so this is the
repair the auto-approval chain makes, run over a list. The products the agent
has not reached yet are fixed by the chain itself when it does.

FIXED MEANS MEASURED. A product counts as fixed only when every live FRONT/BACK
cut-out is at least 2000 px on its long side AFTER the run; anything else is
reported with what the re-cut said (kept on file, failed, no photograph).

LOCAL DATABASE ONLY, unless --prod: Hermes' .env has pointed at prod before,
and this writes. Writes go through vnyx-api's scripts, never SQL from here.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg.conninfo import conninfo_to_dict  # noqa: E402

from app.services.auto_approval import db  # noqa: E402
from app.services.auto_approval.runner import _vnyx_api_dir  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

SHEET = ROOT / "reports" / "soft cutouts" / "verified_soft_leads_2026-10-15.csv"
REPORT_DIR = ROOT / "reports" / "soft cutouts"
SHARP = 2000                       # long side, px — the same line as copy-parent.ts
LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

PRODUCT_SQL = """
SELECT p.id::text, p.sku, t.name AS tenant, p."shopifyProductId" AS shop
  FROM "Product" p JOIN "Tenant" t ON t.id = p."tenantId"
 WHERE p.sku = ANY(%(skus)s) AND NOT p."isDeleted"
"""

# The live garment cut-outs: not deleted, nothing live derived from them.
CUTOUTS_SQL = """
SELECT m.view::text AS view, m.origin::text AS origin, m.url, m.width, m.height
  FROM "ProductMedia" m
 WHERE m."productId" = %(pid)s::uuid AND m."deletedAt" IS NULL
   AND m."mediaType" = 'IMAGE' AND m.view IN ('FRONT', 'BACK')
   AND m.processing IN ('BG_REMOVED', 'COMPOSITED')
   AND NOT EXISTS (SELECT 1 FROM "ProductMedia" d
                    WHERE d."derivedFromId" = m.id AND d."deletedAt" IS NULL)
 ORDER BY m.view, m.origin
"""

_print = threading.Lock()


def say(*a: Any) -> None:
    with _print:
        print(*a, flush=True)


def where(dsn: str) -> tuple[str, str]:
    info = conninfo_to_dict(dsn)
    host = str(info.get("host") or "")
    return host, f"{host or 'localhost'}:{info.get('port') or 5432}/{info.get('dbname') or '?'}"


def read_skus(path: Path) -> list[tuple[str, str]]:
    """(sku, tenant) from a CSV with a `sku` column (tenant optional)."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or "sku" not in rows[0]:
        sys.exit(f"{path} has no `sku` column")
    return list(dict.fromkeys((r["sku"].strip(), (r.get("tenant") or "").strip())
                              for r in rows if r.get("sku")))


def long_side(c: dict[str, Any]) -> int:
    return max(int(c.get("width") or 0), int(c.get("height") or 0))


def cutouts(pid: str) -> list[dict[str, Any]]:
    rows = db.fetch_all(CUTOUTS_SQL, {"pid": pid})
    missing = [r["url"] for r in rows if not (r["width"] and r["height"])]
    if missing:                    # no stored size: read it from the file header
        from app.net import image_dims_all

        dims = image_dims_all(missing, timeout_s=15, deadline_s=60)
        for r in rows:
            d = dims.get(r["url"])
            if d and not (r["width"] and r["height"]):
                r["width"], r["height"] = d
    return rows


def soft_views(rows: list[dict[str, Any]]) -> list[str]:
    return sorted({r["view"] for r in rows if long_side(r) < SHARP})


def sizes(rows: list[dict[str, Any]]) -> str:
    return ", ".join(f"{r['view'][0]}/{(r['origin'] or '?')[:4]} {r['width']}x{r['height']}"
                     for r in rows)


def fix_one(prod: dict[str, Any], *, dsn: str, apply: bool, sync: bool,
            provider: str, quiet: bool) -> dict[str, Any]:
    sku, pid = prod["sku"], prod["id"]
    res: dict[str, Any] = {"sku": sku, "tenant": prod["tenant"], "copy": sku[:1].upper() == "C",
                           "status": "", "action": "", "before": "", "after": "",
                           "parents": "", "shopify": "", "note": ""}
    before = cutouts(pid)
    res["before"] = sizes(before)
    views = soft_views(before)
    if not before:
        res["status"] = "no cut-out"
        return res
    if not views:
        res["status"] = "already sharp"
        return res

    vnyx_api = _vnyx_api_dir()
    args = ["--db", dsn, "--product", pid, *(["--apply"] if apply else []),
            "--provider", provider, "--replace", "--keep-better", "--views", ",".join(views)]
    try:
        ok, out, _ = rp.run_step(vnyx_api, "backfill-bg-removal.ts", args,
                                 timeout_s=900, quiet=quiet)
    except rp.StepFailed as exc:       # vnyx-api unreachable, timeout: keep the row
        res["status"], res["note"] = "error", str(exc)[:500]
        return res
    text = out or ""
    shared = len(re.findall(r"\[reuse-parent\]|parent's cut-out .*shared", text))
    from_parent = len(re.findall(r"replace-from-parent|re-cut from parent", text))
    res["action"] = ("shared from parent" if shared and not from_parent
                     else "cut from parent's photo" if from_parent
                     else "re-cut from own photo")
    parents = rp.parents_updated(text)
    res["parents"] = ", ".join(parents.values())
    kept = [ln.strip() for ln in text.splitlines() if "KEPT the cut-out on file" in ln]
    failed = [ln.strip() for ln in text.splitlines() if "FAILED" in ln]
    if not ok:
        res["status"], res["note"] = "error", "backfill-bg-removal.ts returned non-zero"
        return res
    if not apply:
        res["status"] = f"would re-cut {', '.join(views)}"
        res["note"] = "; ".join(ln.strip() for ln in text.splitlines() if "<-" in ln)[:500]
        return res

    after = cutouts(pid)
    res["after"] = sizes(after)
    still = soft_views(after)
    res["status"] = "fixed" if not still else f"still soft: {', '.join(still)}"
    res["note"] = "; ".join([*kept, *failed])[:500]

    if sync:
        pushed = []
        for target_id, target_sku in [(pid, sku), *parents.items()]:
            if target_id != pid and not rp.shopify_product_id(dsn, target_id):
                continue
            if target_id == pid and not prod.get("shop"):
                pushed.append(f"{target_sku}: not on Shopify")
                continue
            ok2, out2, _ = rp.run_step(vnyx_api, "resync-listings.ts",
                                       ["--product", target_id, "--include-synced", "--apply"],
                                       timeout_s=120, quiet=quiet)
            m = re.search(r"queued\s*:\s*(\d+)", out2 or "")
            pushed.append(f"{target_sku}: " + ("queued" if ok2 and m and int(m.group(1))
                                               else "NOT queued"))
        res["shopify"] = "; ".join(pushed)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sheet", type=Path, default=SHEET, help="CSV with a `sku` column")
    ap.add_argument("--skus", help="comma-separated SKUs instead of the sheet")
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    ap.add_argument("--sync", action="store_true",
                    help="after --apply, re-push live listings (and updated parents) to Shopify")
    ap.add_argument("--workers", type=int, default=2,
                    help="products in parallel (Hermes cuts two at a time; default 2)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--prod", action="store_true",
                    help="allow a non-local DATABASE_URL (prod: only with the owner's go-ahead)")
    ap.add_argument("--verbose", action="store_true", help="echo each step's output")
    a = ap.parse_args()

    dsn = db.dsn()
    host, at = where(dsn)
    if host.lower() not in LOCAL_HOSTS and not a.prod:
        sys.exit(f"REFUSED: DATABASE_URL points at {at}, not the local database. "
                 f"Pass --prod only with the go-ahead to write there.")
    if a.sync and not a.apply:
        sys.exit("--sync re-pushes what --apply wrote; pass both")
    provider = os.getenv("AUTO_APPROVAL_BG_PROVIDER", "hermes").strip() or "hermes"

    wanted = ([(s.strip(), "") for s in a.skus.split(",") if s.strip()] if a.skus
              else read_skus(a.sheet))
    found = db.fetch_all(PRODUCT_SQL, {"skus": [s for s, _ in wanted]})
    tenant_of = {s: t for s, t in wanted}
    prods = [p for p in found if not tenant_of.get(p["sku"]) or tenant_of[p["sku"]] == p["tenant"]]
    missing = sorted({s for s, _ in wanted} - {p["sku"] for p in prods})
    if a.limit:
        prods = prods[:a.limit]

    say(f"Database : {at}")
    say(f"Mode     : {'APPLY' if a.apply else 'DRY RUN'}{' + Shopify re-push' if a.sync else ''}")
    say(f"Products : {len(prods)} ({sum(p['sku'][:1].upper() == 'C' for p in prods)} C-copies)"
        + (f"; {len(missing)} SKU(s) not found: {', '.join(missing[:10])}" if missing else ""))

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futs = {pool.submit(fix_one, p, dsn=dsn, apply=a.apply, sync=a.sync,
                            provider=provider, quiet=not a.verbose): p for p in prods}
        for i, f in enumerate(as_completed(futs), 1):
            p = futs[f]
            try:
                r = f.result()
            except Exception as exc:  # noqa: BLE001 — one product never stops the list
                r = {"sku": p["sku"], "tenant": p["tenant"], "status": "error",
                     "note": f"{type(exc).__name__}: {exc}"[:500]}
            results.append(r)
            say(f"[{i}/{len(prods)}] {r['sku']:13} {r['status']:24} {r.get('action', '')}"
                + (f"  (+ parent {r['parents']})" if r.get("parents") else ""))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"fix_soft_cutouts_{'apply' if a.apply else 'dryrun'}_{datetime.now():%Y-%m-%d-%H%M}.csv"
    cols = ["sku", "tenant", "copy", "status", "action", "before", "after", "parents", "shopify", "note"]
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(results, key=lambda r: (str(r.get("status")), r["sku"])))

    from collections import Counter
    say("\n--- summary ---")
    for k, n in Counter(str(r.get("status")).split(":")[0] for r in results).most_common():
        say(f"  {k:24} {n}")
    say(f"  report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
