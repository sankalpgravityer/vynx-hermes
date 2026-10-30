"""One product of a Reprice calculator run (15 Oct 2026).

The Overview's Run scope card can start a run "as Reprice calculator": a
section (Review / Approved / Rejected) × a brand tier (Luxury). vnyx-api marks
the run `runKind: reprice` in its frozen configSnapshot, and the runner sends
each claimed product here instead of into the repair chain.

WHAT IT DOES. The resale lookup (app/pricing/resale_lookup.py — the same code
as scripts/resale_price.py): identify the item from its photos and care label,
find USED listings with Google Search, keep the ones tied to a real page, price
them (app/pricing/resale_rules.py), find the original retail price and its
source. Then it applies the write rules below and, unless the run is a preview,
asks vnyx-api's `reprice` step to write the selling and/or retail price.

THE WRITE RULES (the user's choices, 15 Oct 2026; widened 29 Oct 2026):
  * selling price — every proposal is written, tier A, B or C alike (the
    user's rule, 29 Oct 2026: "no holds, write the price"). What protects the
    price is upstream: the same-item judge, the own-listing check, and
    price_item's guards (a cut needs three confirmed same-item listings,
    items under €25 are only raised). The tier is kept in the reason.
  * retail price  — only when its source page is proven (cited by Google, or
    opened and showing the amount).
  * the one HOLD left: a concrete authenticity doubt from the photos
    (auth_level "hold") — nothing is written, a person looks.
  * preview       — computes and reports, writes nothing. The default.

IN THE NORMAL FLOW TOO (16 Oct 2026). The Brain's "Reprice by brand tier"
compiles to `chain.repriceTiers`; the runner then calls `in_chain` before the
repair chain for a product whose brand is in one of those tiers (vnyx-api
answers the tier). Same lookup, same write rules — but a weak result (tier B/C,
no listings, a failed lookup) is only reported: the verification goes on with
the price as it was and is not held for it. At most one lookup per product in
REPRICE_COOLDOWN_DAYS; a shadow run previews.

IT DOES NOT READ THE BRAIN'S PRICE SETTINGS — "Leave prices alone" included.
A person chose these products and started this run; that is the calculator's
point, and the card says so. It writes nothing but the two prices (vnyx-api's
set-resale-price.ts) and leaves the product's verification verdict alone.
"""
from __future__ import annotations

import importlib
import logging
import os
import time
from typing import Any

from app import product_audit
from app.pricing import resale_lookup as RL
from app.services.auto_approval import claim, db, events
from app.services.auto_approval import config as cfgmod
from app.services.auto_approval.outcome import Verdict

log = logging.getLogger(__name__)

STEP_SCRIPT = "set-resale-price.ts"
# The normal flow's lookup is paid (~$0.13): a product is looked up at most
# once in this many days, whatever the earlier lookup found.
REPRICE_COOLDOWN_DAYS = 30
_TRANSIENT = ("429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "Timeout", "timed out")


def money(x: float | None) -> str:
    return f"€{x:,.2f}" if isinstance(x, (int, float)) else "—"


def _changed(new: float | None, old: float | None) -> bool:
    return new is not None and (old is None or abs(float(new) - float(old)) >= 0.005)


def decide(res: dict[str, Any]) -> dict[str, Any]:
    """The write rules, pure. -> {price, retail, status, outcome, reason}.

    `price` / `retail` are the amounts to write (None = leave it), `status` and
    `outcome` the run row's verdict.
    """
    tier = res.get("tier") or ""
    new_price, old_price = res.get("new_price"), res.get("vnyx_price")
    new_retail, old_retail = res.get("new_retail"), res.get("retail_price")

    # 29 Oct 2026, the user's rule: a proposed price is WRITTEN whatever the
    # tier — B and C included. The guards that make a proposal still stand
    # (price_item: a cut needs three listings confirmed the same item, items
    # under €25 are only raised, outliers dropped; the same-item judge and
    # the own-listing check before that). The one hold left is a concrete
    # authenticity doubt from the photos; the tier stays in the reason.
    doubt = res.get("auth_level") == "hold"
    price = new_price if not doubt and _changed(new_price, old_price) else None
    retail = new_retail if not doubt and _changed(new_retail, old_retail) else None

    parts = []
    if new_price:
        parts.append(f"selling {money(old_price)} → {money(new_price)}"
                     + ("" if price is not None or not _changed(new_price, old_price)
                        else " (not written)"))
    if new_retail:
        parts.append(f"retail {money(old_retail)} → {money(new_retail)}"
                     + ("" if retail is not None or not _changed(new_retail, old_retail)
                        else " (not written)"))
    elif res.get("retail_new_eur"):
        parts.append("retail found but its source is not proven — kept")
    evidence = (f"tier {tier} — {res.get('tier_reason')}" if tier
                else "no verified listings")
    reason = " · ".join([*parts, evidence])

    # A HOLD only where a person has a decision to make (the user's ask,
    # 29 Oct 2026: products were held with "€28.99 → €28.99"). When the
    # lookup found nothing, or agrees with the price, or a guard kept it,
    # nothing would be written whatever the tier — the product is done, and
    # the outcome code still says what happened. The one exception: concrete
    # authenticity doubt from the photos, which a person should see.
    # A "proposal" is a suggested selling price that differs from today's: a
    # tier B/C proposal is held for a person; no proposal is nothing to decide.
    if doubt:
        status, outcome = "HELD_FOR_HUMAN", "REPRICE_TIER_C"
    elif not new_price:
        status, outcome = "VERIFIED", "REPRICE_NO_LISTINGS"
    elif price is None and retail is None:
        status, outcome = "VERIFIED", "REPRICE_UNCHANGED"
    else:
        status, outcome = "VERIFIED", "REPRICED"
    return {"price": price, "retail": retail, "status": status, "outcome": outcome,
            "reason": reason}


def _listings(res: dict[str, Any]) -> list[dict[str, Any]]:
    """The run report's "Listings": the pages a price was picked from.

    A shoe is priced from the StockX page's RETAIL price, which is not a resale
    listing, so that page leads the list (23 Oct 2026: the run row showed no
    link at all for a shoe). Then the counted listings, then — for a shoe — the
    StockX pages found without a usable price, so a person can open them. The
    run row prints each as "€price status url"; no UI change needed.
    """
    out: list[dict[str, Any]] = []
    shoe = str(res.get("price_source") or "").startswith("StockX")
    if shoe and res.get("retail_url") and res.get("retail_new_eur"):
        out.append({"url": res["retail_url"], "priceEur": res.get("retail_new_eur"),
                    "status": f"StockX retail {res.get('retail_raw') or ''}".strip()
                              + ("" if res.get("new_retail") else " (not proven)"),
                    "verified": res.get("retail_verified")})
    # Every judged listing, labelled (the user's ask, 29 Oct 2026): the counted
    # ones first — "exact" (the same item, by photo or by name) or "similar"
    # (same model, colourway not confirmed) — then the ones set aside: our own
    # listing of this product, and different items, each with the reason. A
    # set-aside listing carries no priceEur, so the product page's Price
    # scanner keeps it out of the range; its price is in the label.
    label = {"same": "exact", "similar": "similar", "unchecked": "unchecked"}
    comps = res.get("comps") or []
    for c in comps:
        if not c.get("counted"):
            continue
        v = (c.get("match") or {}).get("verdict")
        tag = label.get(v or "", "") if v else ""
        out.append({"url": c.get("url"), "priceEur": c.get("price_eur"),
                    "status": f"{c.get('status') or 'listed'}{' · ' + tag if tag else ''}",
                    "verified": c.get("verified"),
                    **({"sameItem": v, "reason": (c["match"].get("reason") or "")[:300]} if v else {}),
                    **({"source": c["source"], "title": (c.get("title") or "")[:120]}
                       if c.get("source") else {})})
    # The set-aside rows carry their price at the FRONT of the label (the row
    # prints "— <label> <url>"), and at most a few of each, so the priced,
    # counted listings are what the row is about.
    own_rows = different_rows = 0
    for c in comps:
        price = money(c.get("price_eur")) if c.get("price_eur") else "no price"
        if c.get("own") and own_rows < 3:
            own_rows += 1
            out.append({"url": c.get("url"), "priceEur": None, "verified": c.get("verified"),
                        "status": f"{price} · our own listing, not evidence ({c['own'].get('why')})",
                        "sameItem": "own", "title": (c.get("title") or "")[:120]})
        elif (c.get("match") or {}).get("verdict") == "different" and different_rows < 6:
            different_rows += 1
            out.append({"url": c.get("url"), "priceEur": None, "verified": c.get("verified"),
                        "status": f"{price} · different item: {c['match'].get('reason') or '—'}",
                        "sameItem": "different", "title": (c.get("title") or "")[:120]})
    if shoe:
        out += [{"url": c.get("url"), "priceEur": None, "status": "StockX page, no price shown",
                 "verified": c.get("verified")}
                for c in res.get("comps") or []
                if not c.get("counted") and "stockx.com" in str(c.get("url") or "")]
    seen: set[str] = set()
    uniq = []
    for x in out:
        if x["url"] and x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    return uniq[:20]


def summary(res: dict[str, Any]) -> dict[str, Any]:
    """What the run report keeps: the numbers and the evidence, not the raw answer."""
    return {
        "before": res.get("vnyx_price"), "after": res.get("new_price"),
        "retailBefore": res.get("retail_price"), "retailAfter": res.get("new_retail"),
        "tier": res.get("tier"), "tierReason": res.get("tier_reason"),
        "confidence": res.get("confidence"), "nComps": res.get("n_comps"),
        "nSold": res.get("n_sold"), "basis": res.get("basis"),
        "retailFound": res.get("retail_new_eur"), "retailVerified": res.get("retail_verified"),
        "retailUrl": res.get("retail_url"), "retailNote": res.get("retail_note"),
        "identified": res.get("identified"), "authLevel": res.get("auth_level"),
        # "StockX only (footwear)" for a shoe, whose evidence is the StockX
        # page's retail price (retailUrl), not listings (23 Oct 2026).
        "priceSource": res.get("price_source"), "retailRaw": res.get("retail_raw"),
        # The same-item judge (29 Oct 2026): how many counted listings it
        # confirmed, and the ones it dropped — with its reason, for a person.
        "nSame": res.get("n_same"), "nOwn": res.get("n_own"),
        "imageSearch": res.get("image_search"), "textSearch": res.get("text_search"),
        "timings": res.get("timings"),
        "differentItems": [{"url": c.get("url"), "priceEur": c.get("price_eur"),
                            "title": (c.get("title") or "")[:120],
                            "reason": (c.get("match") or {}).get("reason")}
                           for c in res.get("comps") or []
                           if (c.get("match") or {}).get("verdict") == "different"][:8],
        "authenticity": (res.get("authenticity_concerns") or "")[:400],
        "listings": _listings(res),
        "photos": [p.get("url") for p in res.get("photos_sent") or [] if p.get("sent")],
        "searches": res.get("searches"), "costUsd": res.get("cost_usd"),
        "seconds": res.get("seconds"), "error": res.get("error"),
    }


def _write(product_id: str, price: float | None, retail: float | None) -> dict[str, Any]:
    """vnyx-api's `reprice` step. -> {ok, reason, result}. Never raises."""
    rp = importlib.import_module("scripts.repair_product")
    if not rp.remote_has_step(STEP_SCRIPT) or not rp.remote_supports("price", "retailPrice"):
        return {"ok": False, "missing": True,
                "reason": "this vnyx-api has no `reprice` step yet — deploy vnyx-api"}
    from app.services.auto_approval.runner import _vnyx_api_dir

    args = ["--product", product_id, "--apply"]
    if price is not None:
        args += ["--price", f"{price:.2f}"]
    if retail is not None:
        args += ["--retail-price", f"{retail:.2f}"]
    try:
        ok, out, results = rp.run_step(_vnyx_api_dir(), STEP_SCRIPT, args, timeout_s=180,
                                       quiet=True, results_name="reprice.json")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:300]}
    first = ((results or {}).get("results") or [{}])[0] if isinstance(results, dict) else {}
    reason = str(first.get("reason") or "")
    good = ok and reason in ("changed", "unchanged")
    if not reason:
        reason = next((ln for ln in reversed((out or "").splitlines()) if ln.strip()), "no answer")
    return {"ok": good, "reason": reason[:300], "result": first}


def apply_rules(product_id: str, res: dict[str, Any], *, preview: bool) -> dict[str, Any]:
    """A successful lookup -> the write rules applied, and written unless preview.

    -> {steps, report, status, outcome, reason, write_failed}; `write_failed`
    is the reason the write was refused, or None. Never raises.
    """
    d = decide(res)
    report = summary(res)
    if str(res.get("price_source") or "").startswith("StockX") and res.get("retail_new_eur"):
        found = (f"StockX retail {res.get('retail_raw') or money(res.get('retail_new_eur'))}"
                 f"{'' if res.get('new_retail') else ' (not proven)'}")
    else:
        found = f"{res.get('n_comps', 0)} verified listing(s)"
        if res.get("n_same") is not None:
            found += (f" — {res['n_same']} the same item"
                      + (f", {res['n_different']} different dropped" if res.get("n_different") else "")
                      + (f", {res['n_own']} our own listing(s) set aside" if res.get("n_own") else ""))
        ts = res.get("text_search") or {}
        if ts and not ts.get("ran"):
            found += f" (image search only: {ts.get('same_from_images')} same, Google skipped)"
    steps = [{"step": "lookup", "ran": True, "ok": True,
              "note": f"{found}, {res.get('searches', 0)} searches",
              "seconds": res.get("seconds")}]
    report["preview"] = preview
    status, outcome, reason = d["status"], d["outcome"], d["reason"]
    write_failed = None

    if d["price"] is None and d["retail"] is None:
        steps.append({"step": "reprice", "ran": False, "why": "nothing to write"})
    elif preview:
        steps.append({"step": "reprice", "ran": False, "why": "preview — nothing written"})
        report["wouldWrite"] = {"price": d["price"], "retail": d["retail"]}
        if outcome == "REPRICED":
            outcome = "REPRICE_PREVIEW"
        reason = "Preview: " + reason
    else:
        w = _write(product_id, d["price"], d["retail"])
        report["write"] = {"price": d["price"], "retail": d["retail"], **w}
        steps.append({"step": "reprice", "ran": True, "ok": w["ok"], "note": w["reason"]})
        if not w["ok"]:
            write_failed = w["reason"]
    return {"steps": steps, "report": report, "status": status, "outcome": outcome,
            "reason": reason, "write_failed": write_failed}


def brand_tier(product_id: str) -> dict[str, Any] | None:
    """vnyx-api's answer for one product: {brand, tier}, or None when it cannot
    be asked (no URL/secret, an older vnyx-api, a network error). Never raises."""
    base = (os.getenv("VNYX_API_URL") or "").rstrip("/")
    secret = os.getenv("AUTO_APPROVAL_INTERNAL_SECRET", "")
    if not base or not secret:
        return None
    import httpx

    try:
        r = httpx.get(f"{base}/internal/auto-approval/brand-tier/{product_id}",
                      headers={"x-internal-secret": secret}, timeout=20)
    except httpx.HTTPError as exc:
        log.warning("brand tier: vnyx-api unreachable (%s)", exc)
        return None
    if r.status_code != 200:
        log.warning("brand tier: vnyx-api answered %s (%s)", r.status_code, r.text[:200])
        return None
    body = r.json() or {}
    return body if isinstance(body, dict) else None


def recently_looked_up(product_id: str, days: int = REPRICE_COOLDOWN_DAYS) -> bool:
    """Did a lookup finish for this product in the last `days`? A preview, a
    lookup that errored and a write vnyx-api refused do not count."""
    with db.connection(read_only=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM "AutoApprovalRunProduct"
             WHERE "productId" = %(p)s::uuid
               AND "completedAt" > now() - make_interval(days => %(d)s)
               AND jsonb_typeof(deltas->'reprice') = 'object'
               AND coalesce((deltas->'reprice'->'write'->>'ok')::boolean, true)
               AND coalesce((deltas->'reprice'->>'preview')::boolean, false) = false
               AND deltas->'reprice'->>'error' IS NULL
             LIMIT 1
            """,
            {"p": product_id, "d": int(days)},
        )
        return cur.fetchone() is not None


def in_chain(row: dict[str, Any], rs: cfgmod.RunSettings) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The Brain's "Reprice by brand tier", before a normal verification's
    repair chain. -> (steps, report-or-None). Never raises, never holds: a weak
    or failed lookup is a step note and the price stays as it was. A run that
    does not write (shadow) previews."""
    product_id = str(row["productId"])
    tiers = set(rs.reprice_tiers)
    try:
        found = brand_tier(product_id)
        if found is None:
            return [{"step": "reprice", "ran": False,
                     "why": "brand tier unknown — vnyx-api did not answer"}], None
        tier = found.get("tier")
        if tier not in tiers:
            return [{"step": "reprice", "ran": False,
                     "why": f"brand {found.get('brand') or '—'} is "
                            f"{(tier or 'untiered').lower()} — not a tier the Brain reprices"}], None
        if recently_looked_up(product_id):
            return [{"step": "reprice", "ran": False,
                     "why": f"looked up within the last {REPRICE_COOLDOWN_DAYS} days"}], None
        res = RL.lookup(RL.item_from_payload(product_audit.load(db.dsn(), product_id)))
    except product_audit.ProductNotFound:
        raise
    except Exception as exc:  # noqa: BLE001
        res = {"error": f"{type(exc).__name__}: {exc}"}

    if res.get("error"):
        err = str(res["error"])[:300]
        return ([{"step": "lookup", "ran": True, "ok": False,
                  "note": f"resale lookup failed — price left as it was: {err}"}],
                {**summary(res), "preview": not rs.apply})

    a = apply_rules(product_id, res, preview=not rs.apply)
    report = {**a["report"], "brandTier": tier, "outcome": a["outcome"]}
    steps = a["steps"]
    if a["status"] != "VERIFIED":
        why = f"{a['outcome']} — selling price left as it was: {a['reason']}"[:300]
        if steps[-1].get("ran"):
            steps.append({"step": "reprice", "ran": False, "why": why})
        else:
            steps[-1]["why"] = why
    return steps, report


def reprice_one(row: dict[str, Any], rs: cfgmod.RunSettings, *,
                expect_status: str = "IN_PROGRESS") -> None:
    """One claimed product, start to finish. Never raises."""
    from app.services.auto_approval.runner import _finish

    t0 = time.monotonic()
    tenant_id, product_id = str(row["tenantId"]), str(row["productId"])
    preview = rs.reprice_preview
    ms = lambda: int((time.monotonic() - t0) * 1000)  # noqa: E731

    def finish(verdict: Verdict, **kw: Any) -> None:
        _finish(row, verdict, duration_ms=ms(), expect_status=expect_status,
                touch_product=False, **kw)

    events.emit(tenant_id, "check",
                f"{row['productSku']} · {(row.get('productTitle') or '')[:60]}"
                f" — reprice started{' (preview)' if preview else ''}",
                run_id=row["runId"], run_product_id=row["id"], product_id=product_id)

    with db.connection(read_only=True) as conn, conn.cursor() as cur:
        if not db.assert_tenant(cur, row["id"], tenant_id, product_id):
            finish(Verdict("CANCELLED", "TENANT_MISMATCH",
                           "The queue row's tenant does not match the product's. Refused.",
                           False, False))
            return

    try:
        item = RL.item_from_payload(product_audit.load(db.dsn(), product_id))
        res = RL.lookup(item)
    except product_audit.ProductNotFound:
        finish(Verdict("CANCELLED", "PRODUCT_DELETED", "The product no longer exists.",
                       False, False))
        return
    except Exception as exc:  # noqa: BLE001
        res = {"error": f"{type(exc).__name__}: {exc}"}

    err = res.get("error")
    if err:
        transient = any(s in str(err) for s in _TRANSIENT)
        if transient and row["attempts"] < row["maxAttempts"]:
            claim.release_lease(str(row["id"]),
                                backoff_seconds=claim.backoff_seconds(int(row["attempts"])))
            events.emit(tenant_id, "warn",
                        f"{row['productSku']}: lookup failed ({str(err)[:120]}) — retrying "
                        f"(attempt {row['attempts']} of {row['maxAttempts']})",
                        run_id=row["runId"], run_product_id=row["id"], product_id=product_id)
            return
        finish(Verdict("FAILED", "REPRICE_FAILED", f"Resale lookup failed: {err}"[:500],
                       False, False),
               error=str(err)[:2000], deltas={"reprice": summary(res)},
               steps=[{"step": "lookup", "ran": True, "ok": False, "note": str(err)[:300]}])
        return

    a = apply_rules(product_id, res, preview=preview)
    steps, report = a["steps"], a["report"]
    status, outcome, reason = a["status"], a["outcome"], a["reason"]
    if a["write_failed"]:
        finish(Verdict("FAILED", "REPRICE_WRITE_FAILED",
                       f"Prices not written: {a['write_failed']}"[:500], False, False),
               error=a["write_failed"], steps=steps, deltas={"reprice": report})
        return

    for s in steps:
        kind, line = events.step_line(s)
        events.emit(tenant_id, kind, f"{row['productSku']} · {line}", run_id=row["runId"],
                    run_product_id=row["id"], product_id=product_id)
    finish(Verdict(status, outcome, reason[:500], False, False),
           steps=steps, deltas={"reprice": report})
