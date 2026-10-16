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

# --------------------------------------------------------------------------- #
# --build-sheet: which verified live products lead with a soft cut-out NOW
# --------------------------------------------------------------------------- #
# The 15 Oct sheet was a snapshot; products verified since are not on it. These
# are the same rules the client's check used: Active, on Shopify, not deleted or
# archived, the agent's verdict VERIFIED, and the FIRST gallery image a cut-out
# of 1195 px or less on its long side. Read-only.
SOFT_LEAD = 1195

LIVE_LEADS_SQL = """
SELECT p.id::text AS pid, p.sku, t.name AS tenant, p."createdAt"::date::text AS created,
       p."tenantId"::text AS tenant_id, m.id::text AS mid, m.url, m.view::text AS view,
       m.origin::text AS origin, m.processing::text AS processing, m.width, m.height,
       m."createdAt"::timestamp(0)::text AS uploaded
  FROM "Product" p
  JOIN "Tenant" t ON t.id = p."tenantId"
  JOIN LATERAL (SELECT * FROM "ProductMedia" x
                 WHERE x."productId" = p.id AND x.url = p.images[1]
                 ORDER BY x."deletedAt" NULLS FIRST, x."createdAt" DESC LIMIT 1) m ON true
 WHERE p.status = 'Active' AND p."shopifyProductId" IS NOT NULL
   AND NOT p."isDeleted" AND NOT p."isArchived"
   AND p."verificationStatus" = 'VERIFIED'
   AND m.processing IN ('BG_REMOVED', 'COMPOSITED')
   {tenant}
"""

LINEAGE_SQL = """
SELECT id::text AS id, "productId"::text AS pid, url, view::text AS view,
       origin::text AS origin, processing::text AS processing, width, height,
       "derivedFromId"::text AS src
  FROM "ProductMedia"
 WHERE "productId" = ANY(%(pids)s::uuid[]) AND view IN ('FRONT', 'BACK', 'OTHER')
"""

# A C-copy's parent: same tenant, the SKU without the "C", sharing a file
# (the rule vnyx-api's copy-parent.ts applies before using it).
PARENTS_SQL = """
SELECT c.id::text AS pid, par.sku AS parent_sku,
       (SELECT max(greatest(coalesce(m.width, 0), coalesce(m.height, 0)))
          FROM "ProductMedia" m
         WHERE m."productId" = par.id AND m."deletedAt" IS NULL
           AND m.processing IN ('BG_REMOVED', 'COMPOSITED') AND m.view IN ('FRONT', 'BACK')
           AND NOT EXISTS (SELECT 1 FROM "ProductMedia" d
                            WHERE d."derivedFromId" = m.id AND d."deletedAt" IS NULL)
       ) AS parent_best,
       EXISTS (SELECT 1 FROM "ProductMedia" r
                WHERE r."productId" = par.id AND r."deletedAt" IS NULL
                  AND r.processing = 'RAW' AND r.view IN ('FRONT', 'BACK')) AS parent_has_raw
  FROM "Product" c
  JOIN "Product" par ON par."tenantId" = c."tenantId" AND par.sku = substr(c.sku, 2)
                    AND par.id <> c.id
 WHERE c.id = ANY(%(pids)s::uuid[])
   AND EXISTS (SELECT 1 FROM "ProductMedia" a
                 JOIN "ProductMedia" b ON b.url = a.url AND b."productId" = par.id
                WHERE a."productId" = c.id)
"""

RAW_SLOTS_SQL = """
SELECT DISTINCT view::text AS view, origin::text AS origin
  FROM "ProductMedia"
 WHERE "productId" = %(pid)s::uuid AND processing = 'RAW' AND "deletedAt" IS NULL
   AND view IN ('FRONT', 'BACK') AND "mediaType" = 'IMAGE'
"""

SHEET_COLS = ["sku", "tenant", "product_created", "lead_origin", "lead_view", "lead_file",
              "lead_px", "lead_uploaded", "cause", "raw_to_recut_from", "raw_px",
              "parent_sku", "parent_best_cutout_px", "on_15_oct_sheet"]


def classify(lead: dict[str, Any], rows: list[dict[str, Any]],
             parent: dict[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """Why the lead cut-out is soft, and the RAW to re-cut it from (or None).
    The same three causes as the 15 Oct sheet."""
    by_id = {r["id"]: r for r in rows}
    chain, cur, seen = [], by_id.get(lead["mid"]), set()
    while cur and cur["id"] not in seen and len(chain) < 12:
        seen.add(cur["id"])
        chain.append(cur)
        cur = by_id.get(cur["src"]) if cur["src"] else None
    raw = next((r for r in chain if r["processing"] == "RAW"), None)
    if raw:
        cuts = sum(1 for r in chain if r["processing"] != "RAW")
        return ("raw stored, re-cut kept soft size" if cuts > 1
                else "raw stored, never re-cut"), raw
    if parent:
        return "copy without raw (parent holds it)", None
    # A raw of the same view and origin, unlinked: the re-cut finds it too.
    same = [r for r in rows if r["processing"] == "RAW" and r["view"] == lead["view"]
            and r["origin"] == lead["origin"]]
    if same:
        return "raw stored, never re-cut", same[0]
    # KIL-001816 (16 Oct 2026): the lead is an old WEB `<ts>-N-processed` cut-out
    # (or a re-cut of one) with no WEB photograph, beside sharp PHOTOBOOTH /
    # DECISION cut-outs of the same view. A re-cut can only remake it from itself;
    # what fixes it is removing it, so the sharp one leads.
    if any(r["processing"] == "RAW" and r["view"] == lead["view"] for r in rows):
        return "extra cut-out, no photo of its origin (remove it)", None
    return "no raw found", None


def build_sheet(tenant: str | None) -> tuple[list[dict[str, Any]], Path]:
    leads = db.fetch_all(LIVE_LEADS_SQL.format(tenant="AND t.name = %(tenant)s" if tenant else ""),
                         {"tenant": tenant})
    say(f"verified live products leading with a cut-out: {len(leads)}")
    missing = [r["url"] for r in leads if not (r["width"] and r["height"])]
    if missing:
        from app.net import image_dims_all

        say(f"reading the size of {len(missing)} cut-out(s) with no stored dimensions…")
        dims = image_dims_all(missing, timeout_s=15, deadline_s=max(60, len(missing) // 4))
        for r in leads:
            d = dims.get(r["url"])
            if d and not (r["width"] and r["height"]):
                r["width"], r["height"] = d
    soft = [r for r in leads if 0 < long_side(r) <= SOFT_LEAD]
    unknown = [r for r in leads if not long_side(r)]

    pids = [r["pid"] for r in soft]
    rows: dict[str, list[dict[str, Any]]] = {}
    parents: dict[str, dict[str, Any]] = {}
    for i in range(0, len(pids), 1000):
        chunk = pids[i:i + 1000]
        for r in db.fetch_all(LINEAGE_SQL, {"pids": chunk}):
            rows.setdefault(r["pid"], []).append(r)
        for r in db.fetch_all(PARENTS_SQL, {"pids": chunk}):
            parents[r["pid"]] = r

    old: set[str] = set()
    if SHEET.exists():
        old = {s for s, _ in read_skus(SHEET)}

    out = []
    for r in soft:
        parent = parents.get(r["pid"]) if r["sku"][:1].upper() == "C" else None
        cause, raw = classify(r, rows.get(r["pid"], []), parent)
        out.append({
            "sku": r["sku"], "tenant": r["tenant"], "product_created": r["created"],
            "lead_origin": r["origin"], "lead_view": r["view"],
            "lead_file": r["url"].rsplit("/", 1)[-1], "lead_px": f"{r['width']}x{r['height']}",
            "lead_uploaded": r["uploaded"], "cause": cause,
            "raw_to_recut_from": raw["url"] if raw else "",
            "raw_px": f"{raw['width']}x{raw['height']}" if raw and raw["width"] else "",
            "parent_sku": parent["parent_sku"] if parent else "",
            "parent_best_cutout_px": parent["parent_best"] if parent else "",
            "on_15_oct_sheet": "yes" if r["sku"] in old else "no",
        })
    out.sort(key=lambda x: (x["cause"], x["tenant"], x["sku"]))

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"verified_soft_leads_{datetime.now():%Y-%m-%d}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=SHEET_COLS)
        w.writeheader()
        w.writerows(out)
    if unknown:
        say(f"({len(unknown)} lead cut-out(s) could not be measured — left off the sheet)")
    return out, path


def latest_sheet() -> Path:
    """The newest verified_soft_leads_<date>.csv, else the 15 Oct one."""
    found = sorted(REPORT_DIR.glob("verified_soft_leads_*.csv"))
    return found[-1] if found else SHEET


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
    if not before:
        res["status"] = "no cut-out"
        return res
    soft = [r for r in before if long_side(r) < SHARP]
    if not soft:
        res["status"] = "already sharp"
        return res

    # WHAT A RE-CUT CAN FIX. A copy: anything (vnyx-api goes to the parent). Any
    # other product: a soft cut-out with a photograph of its own view AND origin.
    # One without (KIL-001816's old WEB cut-outs beside sharp booth ones) can only
    # be remade from itself — reported, never sent.
    is_copy = bool(res["copy"]) and bool(db.fetch_all(PARENTS_SQL, {"pids": [pid]}))
    slots = {(r["view"], r["origin"]) for r in db.fetch_all(RAW_SLOTS_SQL, {"pid": pid})}
    extra = [] if is_copy else [r for r in soft if (r["view"], r["origin"]) not in slots]
    extra_slots = {(r["view"], r["origin"]) for r in extra}
    views = sorted({r["view"] for r in soft if (r["view"], r["origin"]) not in extra_slots})
    extra_note = ("extra cut-out(s) with no photo of their origin, not re-cut — remove them: "
                  + ", ".join(f"{r['view']}/{r['origin']} {r['width']}x{r['height']}"
                              for r in extra)) if extra else ""
    if not views:
        res["status"], res["note"] = "skipped: no photo to re-cut", extra_note
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
    still = sorted({r["view"] for r in after if long_side(r) < SHARP
                    and (r["view"], r["origin"]) not in extra_slots})
    res["status"] = "fixed" if not still else f"still soft: {', '.join(still)}"
    res["note"] = "; ".join(x for x in [*kept, *failed, extra_note] if x)[:500]

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
    ap.add_argument("--build-sheet", action="store_true",
                    help="list today's verified live products with a soft lead cut-out "
                         "(read-only, any database) and stop")
    ap.add_argument("--tenant", help="--build-sheet: one tenant only (e.g. BOAS)")
    ap.add_argument("--sheet", type=Path, default=None,
                    help="CSV with a `sku` column (default: the newest verified_soft_leads_*.csv)")
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
    if a.build_sheet:                  # read-only: allowed against any database
        from collections import Counter

        say(f"Database : {at} (read-only)")
        rows, path = build_sheet(a.tenant)
        say(f"\nsoft lead cut-outs (<= {SOFT_LEAD} px), verified + live: {len(rows)}")
        for cause, n in Counter(r["cause"] for r in rows).most_common():
            say(f"  {cause:40} {n}")
        say("by tenant: " + ", ".join(f"{t} {n}" for t, n in
                                      Counter(r["tenant"] for r in rows).most_common()))
        new = sum(1 for r in rows if r["on_15_oct_sheet"] == "no")
        say(f"not on the 15 Oct sheet: {new}")
        say(f"sheet: {path}")
        return 0
    if host.lower() not in LOCAL_HOSTS and not a.prod:
        sys.exit(f"REFUSED: DATABASE_URL points at {at}, not the local database. "
                 f"Pass --prod only with the go-ahead to write there.")
    if a.sync and not a.apply:
        sys.exit("--sync re-pushes what --apply wrote; pass both")
    provider = os.getenv("AUTO_APPROVAL_BG_PROVIDER", "hermes").strip() or "hermes"

    sheet = a.sheet or latest_sheet()
    wanted = ([(s.strip(), "") for s in a.skus.split(",") if s.strip()] if a.skus
              else read_skus(sheet))
    if not a.skus:
        say(f"Sheet    : {sheet}")
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
