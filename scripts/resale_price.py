#!/usr/bin/env python
"""The resale price of one or more products, by SKU — LOCAL DATABASE ONLY.

    python scripts/resale_price.py --probe                      # does the key run Google Search?
    python scripts/resale_price.py KLE-000356                   # one product
    python scripts/resale_price.py KLE-000356 KLE-000067 --tenant Klekt
    python scripts/resale_price.py KLE-000356 --identify-only   # photos only, no searches
    python scripts/resale_price.py KLE-000356 --prod            # read the PRODUCT from prod, read-only


WHAT IT DOES, PER SKU

  1. Reads the product from the local Postgres (the same loader as
     /v1/product-audit): title, brand, size, condition, price, photos, care label.
  2. Gemini looks at a FRONT and a BACK photo and up to two care-label photos
     and identifies the item: brand, model, article code, era.
  3. Gemini runs Google Search (Interactions API) for USED listings of that
     item on Vinted, Grailed, Vestiaire, eBay, Depop, Marktplaats...
  4. Only listings tied to a real page count: the page Google cited, a listing
     on a cited site, or a page this tool opened that shows the same price.
  5. app/pricing/resale_rules.py prices them (currency, condition, sold vs
     asking, region, outliers, weighted median, .99) and gives a trust tier:
     A safe to accept, B spot-check, C a person must decide.

It prints the VNYX price beside the lookup's price, and writes a JSON + CSV
report to reports/resale/. IT WRITES NOTHING to the database or Shopify.


LOCAL DATABASE BY DEFAULT; PROD ONLY WITH --prod, AND THEN READ-ONLY

Without --prod it refuses to start unless DATABASE_URL points at localhost.
Hermes' .env has been switched between the local database and prod, and this
tool must never read prod by accident (14 Oct 2026).

--prod (the user's request, 15 Oct 2026) reads the products from vnyx-prod:
$VNYX_PROD_DATABASE_URL, else the `DATABASE_URL=...vnyx-prod` line (commented
or not) in Hermes' .env or vnyx-api's .env. The session is opened READ ONLY in
Postgres (default_transaction_read_only), so it cannot write even by mistake.
The connection string is never printed.


COST

About 8 Google searches per item: $14 per 1,000 after 5,000 free a month,
shared by every Gemini 3+ model on the key's Google project. Plus about $0.01
of tokens. --identify-only spends no searches. The same model in another size
reuses the listings for 21 days (.cache/resale/comp_cache.json; --fresh skips
it). A month's search count is kept in .cache/resale/search_ledger.json.

Key: HERMES_RESALE_GEMINI_API_KEY when set, else GEMINI_API_KEY.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from psycopg.conninfo import conninfo_to_dict  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from app import product_audit  # noqa: E402
from app.pricing import resale_lookup as RL  # noqa: E402
from app.services.auto_approval import db  # noqa: E402

LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1"}
REPORT_DIR = ROOT / "reports" / "resale"


def local_dsn() -> tuple[str, str]:
    """The DSN, or exit when it is not on this machine. -> (dsn, 'host:port/db')."""
    dsn = db.dsn()
    try:
        info = conninfo_to_dict(dsn)
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"REFUSED: DATABASE_URL could not be read ({type(exc).__name__}).")
    host = str(info.get("host") or "")
    where = f"{host or 'localhost'}:{info.get('port') or 5432}/{info.get('dbname') or '?'}"
    if host.lower() not in LOCAL_HOSTS:
        sys.exit(f"REFUSED: DATABASE_URL points at {where}, not the local database.\n"
                 f"This tool only runs against localhost. Set DATABASE_URL in "
                 f"{ROOT / '.env'} to the local database (e.g. localhost:5433/vnyx-dev-2).")
    return dsn, where


def prod_dsn() -> tuple[str, str]:
    """vnyx-prod's DSN for read-only use. -> (dsn, 'host:port/db'). Never printed."""
    dsn = os.getenv("VNYX_PROD_DATABASE_URL", "")
    if not dsn:
        api = Path(os.getenv("VNYX_API_DIR", "~/work/vnyx/vnyx-api")).expanduser() / ".env"
        for env in (ROOT / ".env", api):
            if not env.exists():
                continue
            for line in env.read_text(encoding="utf-8").splitlines():
                m = re.match(r'^\s*#?\s*DATABASE_URL\s*=\s*"?([^"\s]+/vnyx-prod[^"\s]*)"?', line)
                if m:
                    dsn = m.group(1)
                    break
            if dsn:
                break
    if not dsn:
        sys.exit("No prod connection string: set VNYX_PROD_DATABASE_URL, or keep a "
                 "`DATABASE_URL=...vnyx-prod` line (it may be commented out) in .env.")
    # Prisma's ?schema=public is not a libpq option.
    dsn = re.sub(r"([?&])schema=[^&]*&?", r"\1", dsn).rstrip("?&")
    info = conninfo_to_dict(dsn)
    return dsn, f"{info.get('host')}:{info.get('port') or 5432}/{info.get('dbname')}"


def resolve(dsn: str, skus: list[str], tenant: str | None) -> tuple[list[dict], list[str]]:
    """SKU -> product rows. -> (rows to look up, problems to print)."""
    wanted = [s.strip().upper() for s in skus if s.strip()]
    sql = """
        SELECT p.id::text AS id, p.sku, t.name AS tenant
          FROM "Product" p JOIN "Tenant" t ON t.id = p."tenantId"
         WHERE upper(p.sku) = ANY(%(skus)s) AND NOT p."isDeleted"
    """
    params: dict[str, Any] = {"skus": wanted}
    if tenant:
        sql += " AND t.name ILIKE %(tenant)s"
        params["tenant"] = tenant
    with product_audit.connect(dsn, read_only=True) as conn, \
            conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = list(cur.fetchall())
    by_sku: dict[str, list[dict]] = {}
    for r in rows:
        by_sku.setdefault(r["sku"].upper(), []).append(r)
    found, problems = [], []
    for s in dict.fromkeys(wanted):
        hits = by_sku.get(s, [])
        if not hits:
            problems.append(f"{s}: not found in the local database"
                            + (f" for tenant {tenant!r}" if tenant else ""))
        elif len(hits) > 1:
            problems.append(f"{s}: on {len(hits)} tenants "
                            f"({', '.join(sorted(h['tenant'] for h in hits))}) — add --tenant")
        else:
            found.append(hits[0])
    return found, problems


def money(x: Any) -> str:
    return f"€{x:,.2f}" if isinstance(x, (int, float)) else "—"


def _why_not(c: dict) -> str:
    if c.get("outlier"):
        return "outlier (over 3× from the others)"
    if not c.get("price_eur"):
        return "no usable price" + (f" ({c.get('price_raw')})" if c.get("price_raw") else "")
    if (c.get("price_confirm") or {}).get("ok") is False:
        return "page does not show this price"
    if not c.get("resale_site"):
        return "not a resale site"
    if c.get("kind") != "listing":
        return f"not a listing page ({c.get('kind')})"
    if not c.get("grounded"):
        pc = c.get("page_check") or {}
        return "not verified" + (f": {pc['why']}" if pc.get("why") else "")
    return "not counted"


def render(r: dict) -> str:
    L = ["─" * 78,
         f"{r.get('sku')} · {r.get('tenant') or '?'} · {r.get('review_status') or '?'}",
         f"{r.get('title') or ''} · {r.get('condition') or 'condition unknown'}"]
    if r.get("error"):
        L.append(f"  ERROR        {r['error']}")
    idt = r.get("identified") or {}
    if idt:
        L.append("  Identified   " + " · ".join(x for x in (
            idt.get("brand"), idt.get("model"),
            f"code {idt['article_code']}" if idt.get("article_code") else "",
            idt.get("era"), f"id {idt.get('id_confidence') or '?'}",
            f"interest {r.get('resale_interest') or '?'}") if x))
    L.append(f"  {'':<13}{'NOW (in VNYX)':>15}{'NEW (from listings)':>22}")
    sell_note = ""
    if "new_price" in r:
        sell_note = f"tier {r['tier']} — {r['tier_reason']}" if r.get("tier") else "no price found"
    L.append(f"  {'Selling':<13}{money(r.get('vnyx_price')):>15}"
             f"{money(r.get('new_price')) if 'new_price' in r else '—':>22}   {sell_note}")
    if "new_price" in r:
        src = ""
        if r.get("retail_new_eur"):
            src = (f"{r.get('retail_raw') or money(r['retail_new_eur'])} from "
                   f"{RL.domain(r.get('retail_url') or '') or 'no page given'}"
                   f" ({ {'cited': 'page cited by Google', 'page': 'page checked'}.get(r.get('retail_verified'), 'NOT proven') })")
        L.append(f"  {'Retail (new)':<13}{money(r.get('retail_price')):>15}"
                 f"{money(r.get('new_retail')) if r.get('new_retail') else '— keep':>22}   {src}")
        if r.get("retail_url"):
            L.append(f"  {'':<13}retail source: {r['retail_url']}")
        if r.get("retail_note"):
            L.append(f"  {'':<13}note: {r['retail_note']}")
    if r.get("decision"):
        L.append(f"  Decision     {r['decision']}")
    if "n_comps" in r:
        L.append(f"  Basis        {r.get('basis') or '—'} · {r['n_comps']} verified of "
                 f"{r.get('n_offered', 0)} found ({r.get('n_sold', 0)} sold, {r.get('n_eu', 0)} EU)"
                 f" · confidence {r.get('confidence')}")
        if r.get("model_estimate_eur"):
            L.append(f"  Model guess  {money(r['model_estimate_eur'])} (shown only; never used as the price)")
    for label, key in (("Flag", "price_flag"), ("Note", "price_note")):
        if r.get(key):
            L.append(f"  {label:<12} {r[key]}")
    if r.get("authenticity_concerns"):
        L.append(f"  Authenticity [{(r.get('auth_level') or 'note').upper()}] {r['authenticity_concerns']}")
    if r.get("catalogue_corrections"):
        L.append(f"  Catalogue    {r['catalogue_corrections']}")
    photos = r.get("photos_sent") or []
    if photos:
        L.append("  Photos sent to Gemini (identify call only; the search call gets no images)")
        for ph in photos:
            name = (ph.get("url") or "").rsplit("/", 1)[-1]
            L.append(f"    {'sent  ' if ph.get('sent') else 'FAILED'}  {ph.get('label', '?'):<48} {name}")
            L.append(f"            {ph.get('url')}")
    comps = r.get("comps") or []
    if comps:
        L.append("  Listings")
        for c in sorted(comps, key=lambda c: not c.get("counted")):
            state = "COUNTED" if c.get("counted") else "skipped"
            L.append(f"    {state:<8}{money(c.get('price_eur')):>10}  {(c.get('status') or '?'):<6} "
                     f"{(c.get('region') or '·'):<6} {(c.get('verified') or '·'):<5}  {c.get('url') or '(no url)'}")
            if not c.get("counted"):
                L.append(f"            why: {_why_not(c)}")
            pc = c.get("price_confirm")
            if pc:
                L.append(f"            price check: {pc.get('why')}")
    extra = []
    if r.get("cached_from"):
        extra.append(f"listings reused from {r['cached_from']}")
    if r.get("rescue"):
        extra.append("StockX second round ran" if str(r.get("price_source", "")).startswith("StockX")
                     else "EU second round ran")
    L.append(f"  Spent        {r.get('searches', 0)} searches · ~${r.get('cost_usd', 0):.3f} · "
             f"{r.get('seconds', 0)} s" + (f" · {'; '.join(extra)}" if extra else ""))
    return "\n".join(L)


CSV_COLS = ["sku", "tenant", "review_status", "title", "condition", "vnyx_price", "new_price",
            "retail_price", "new_retail", "retail_verified", "retail_url", "retail_note",
            "change_pct", "tier", "tier_reason", "decision", "confidence", "n_comps", "n_sold",
            "n_eu", "basis", "price_flag", "price_note", "identified_brand", "identified_model",
            "article_code", "resale_interest", "auth_level", "authenticity_concerns",
            "catalogue_corrections", "listing_links", "photos_sent", "searches", "cost_usd", "seconds", "error",
            "edit_url"]


def write_reports(results: list[dict]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    jpath = REPORT_DIR / f"resale_{stamp}.json"
    cpath = REPORT_DIR / f"resale_{stamp}.csv"
    jpath.write_text(json.dumps(results, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    with open(cpath, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLS)
        w.writeheader()
        for r in results:
            idt = r.get("identified") or {}
            vp, np_ = r.get("vnyx_price"), r.get("new_price")
            w.writerow({
                **{k: r.get(k) for k in CSV_COLS if k in r},
                "change_pct": f"{(np_ - vp) / vp * 100:+.0f}%" if vp and np_ else "",
                "identified_brand": idt.get("brand"), "identified_model": idt.get("model"),
                "article_code": idt.get("article_code"),
                "photos_sent": " | ".join(
                    f"{ph.get('label')}: {ph.get('url')}" + ("" if ph.get("sent") else " (FAILED)")
                    for ph in r.get("photos_sent") or []),
                "listing_links": " ".join(c.get("url") or "" for c in r.get("comps") or []
                                          if c.get("counted")),
            })
    return jpath, cpath


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("skus", nargs="*", help="one or more product SKUs, e.g. KLE-000356")
    ap.add_argument("--tenant", help="tenant name, when a SKU exists on more than one tenant")
    ap.add_argument("--probe", action="store_true",
                    help="one small search call: does the key run Google Search? (~2 searches)")
    ap.add_argument("--identify-only", action="store_true",
                    help="read the photos and label only; no Google searches")
    ap.add_argument("--fresh", action="store_true",
                    help="ignore listings cached from the same model in the last 21 days")
    ap.add_argument("--no-page-check", action="store_true",
                    help="do not open uncited listing pages to confirm their price")
    ap.add_argument("--prod", action="store_true",
                    help="read the products from vnyx-prod, in a READ-ONLY session")
    ap.add_argument("--workers", type=int, default=1, help="products looked up at once (default 1)")
    a = ap.parse_args()

    cfg = RL.config()
    key_src = "HERMES_RESALE_GEMINI_API_KEY" if os.getenv("HERMES_RESALE_GEMINI_API_KEY") else "GEMINI_API_KEY"
    if not RL.api_key():
        print("No Gemini key: set GEMINI_API_KEY (or HERMES_RESALE_GEMINI_API_KEY) in .env")
        return 2

    if a.probe:
        print(f"Probing {cfg['search_model']} with Google Search (key: {key_src}) ...")
        p = RL.probe()
        print(json.dumps(p, indent=1, ensure_ascii=False))
        print("OK: the key runs Google Search." if p["ok"] else
              "NOT OK: no search ran. Check the error above, the model name, and billing on the key's project.")
        return 0 if p["ok"] else 1

    if not a.skus:
        ap.print_help()
        return 2

    dsn, where = prod_dsn() if a.prod else local_dsn()
    month = RL.searches_this_month()
    print(f"Database {where} ({'PROD, ' if a.prod else ''}read only) · models {cfg['identify_model']} / {cfg['search_model']} "
          f"· key {key_src}")
    print(f"Searches this month so far: {month:,} of {int(cfg['free_searches_per_month']):,} free")

    rows, problems = resolve(dsn, a.skus, a.tenant)
    for p in problems:
        print("  " + p)
    if not rows:
        return 1

    def one(row: dict) -> dict:
        try:
            item = RL.item_from_payload(product_audit.load(dsn, row["id"]))
        except Exception as exc:  # noqa: BLE001
            return {"sku": row["sku"], "tenant": row["tenant"],
                    "error": f"could not load the product: {type(exc).__name__}: {exc}"}
        return RL.lookup(item, fresh=a.fresh, check_pages=not a.no_page_check,
                         identify_only=a.identify_only)

    print(f"Looking up {len(rows)} product(s); each takes about 30-90 s ...", flush=True)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as pool:
        futures = [pool.submit(one, r) for r in rows]
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            print(render(r), flush=True)

    order = {r["sku"]: i for i, r in enumerate(rows)}
    results.sort(key=lambda r: order.get(r.get("sku"), 0))
    jpath, cpath = write_reports(results)
    spent = sum(r.get("searches", 0) for r in results)
    cost = sum(r.get("cost_usd", 0) for r in results)
    month = RL.searches_this_month()
    print("─" * 78)
    print(f"{len(results)} looked up · {sum(1 for r in results if r.get('error'))} errors · "
          f"{spent} searches · ~${cost:.2f} at list price")
    print(f"Searches this month: {month:,} of {int(cfg['free_searches_per_month']):,} free"
          + (f" ({month - int(cfg['free_searches_per_month']):,} over)"
             if month > int(cfg['free_searches_per_month']) else ""))
    print(f"Report: {cpath}\n        {jpath}")
    print("Nothing was written to the database.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
