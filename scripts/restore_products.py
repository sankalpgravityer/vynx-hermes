#!/usr/bin/env python
"""Put a product's fields back to what they were before a repair run.

    # look first — this is the default, nothing is written
    python scripts/restore_products.py \
        --db "postgresql://..." \
        --before "reports/generated/dossier-BEFORE-midtex-part*of4.json" \
        --run reports/midtek_10_product.json

    # then, having read the diff
    python scripts/restore_products.py ... --apply

WHY THIS EXISTS. A `repair_product.py --apply` run on 18 Sep 2026 wrote master
categories, categories and sizes that its own blocking rules rejected in the
same breath (`applied: masterCategory` on one line, `TAX.001 Unknown master
category` on the next), and the `copy` step then rewrote each title to describe
that corrupted data. An Adidas t-shirt became "Deep Burgundy Hoodie" in a
category called `hoodie`; a New Balance product stopped naming New Balance.
This puts those fields back.

WHERE THE OLD VALUES COME FROM, and why it takes two sources.

  1. THE DOSSIER JSON (`--before`) is the authority for every structured field.
     It is a timestamped snapshot taken before the run, it carries the values as
     stored, and it cannot have been touched by the run that followed. Every
     field in RESTORE_FROM_DOSSIER comes from here and from nowhere else.

  2. `ActivityLog` is the only place the DESCRIPTION survives — the dossier does
     not record it. Each row holds a full `before` and `after` of the product
     row, so the pre-run text is either the `before` of the run's own first
     logged write, or the `after` of the last write that predates the run.

     But ActivityLog is INCOMPLETE: the chain's `reconcile` step writes without
     logging, so for some products the newest snapshot is stale and its summary
     is not the text the run replaced. Restoring that would swap one wrong
     description for a different wrong description, silently.

     So a description is restored ONLY when its length matches the length the
     run itself recorded seeing (`--run`, `before.description_chars`). That is a
     cheap, exact check against a number written by the thing that did the
     damage. Where it does not match, the field is LEFT ALONE and the product is
     listed under "description not restorable" — an honest gap beats a confident
     wrong answer.

WHAT IT DOES NOT TOUCH. Images, cut-outs and renders: the run replaced some of
those and several are genuinely better than what they replaced, so they are a
separate decision. `categoryId`, because it is a foreign key the taxonomy owns
and every failure in that run left it unchanged anyway. Stage and review status,
which the run never moved.

SAFETY. Dry run unless `--apply`. Every write happens in ONE transaction per
product with the row locked, and the current values are written to a rollback
file first, so this script's own effects can be undone the same way.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402

# Column on Product  ->  key in the dossier's `record` block.
#
# `title` is the dossier product's own top-level field rather than a record key,
# and is handled beside these.
# Columns restored from the dossier, and ONLY these.
#
# Measured, not assumed: across the 398 products of this tenant the run never
# touched, each of these six matches the live column on 398 of 398. The dossier
# is a faithful copy of them, so writing one back cannot invent a value.
#
# `euSize` is absent because EU size is not a column at all — it lives in
# `properties`, and naming it here produced `column "euSize" does not exist`.
#
# `internationalSize` is absent for a subtler and more dangerous reason. The
# dossier stores the snapshot's RESOLVED size, which is not the column: on the
# same 398 products it agreed only 377 times, 94.7%. Writing it back would have
# put 'M' over a perfectly good 'S' on MID-000209. Size is restored from the
# verbatim row snapshot instead — see SIZE_FROM_ACTIVITY_LOG below.
#
# `price` and `retailPrice` are absent because the chain's `price` step is a
# LEGITIMATE repair: it moved two products into their grade's window. Undoing
# that would re-break them. `--include-price` asks for it explicitly.
RESTORE_FROM_DOSSIER: dict[str, str] = {
    "masterCategory": "masterCategory",
    "category": "category",
    "subCategory": "subCategory",
    "sizingGuide": "sizingGuide",
    "mannequinType": "mannequinType",
}

PRICE_FROM_DOSSIER: dict[str, str] = {
    "price": "price",
    "retailPrice": "retailPrice",
}

# Columns taken from the ActivityLog row snapshot rather than the dossier,
# because the dossier's copy of them is a resolved value, not the stored one.
SIZE_FROM_ACTIVITY_LOG = ("internationalSize",)

# Keys inside the `properties` JSON. Merged, never replaced: `properties` holds
# far more than the chain touched, and writing the whole object back from a
# snapshot that only carries a few of its keys would delete the rest.
RESTORE_PROPERTIES: dict[str, str] = {
    "gender": "gender",
    "size": "size",
    "eu_size": "euSize",
    "internationalSize": "size",
}


def load_before(patterns: list[str]) -> dict[str, dict[str, Any]]:
    """Every product in the dossier snapshots, keyed by product id."""
    out: dict[str, dict[str, Any]] = {}
    for pat in patterns:
        for f in sorted(glob.glob(pat)) or [pat]:
            doc = json.loads(Path(f).read_text(encoding="utf-8"))
            for p in doc["products"]:
                out[str(p["product_id"])] = p
    return out


def _same(a: Any, b: Any) -> bool:
    """Compare as the database would see them after a write.

    Numbers arrive as Decimal from psycopg and as str or float from JSON, and
    `'14.99' != Decimal('14.99')` would report a difference on every price and
    then write it back unchanged.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, (int, float)) or isinstance(b, (int, float)):
        try:
            return abs(float(a) - float(b)) < 0.005
        except (TypeError, ValueError):
            pass
    return str(a).strip() == str(b).strip()


def row_snapshot(cur, pid: str, run_start: datetime) -> tuple[dict[str, Any] | None, str]:
    """The verbatim Product row as ActivityLog last saw it, and how trustworthy.

    Two candidates, and the difference between them decides what may be used:

      "clean"      the `after` of the last write BEFORE the run. Nothing in the
                   run has touched it, so every column in it is the pre-run value.
      "contaminated"  the `before` of the run's own first LOGGED write. The chain's
                   `reconcile` step writes without logging and runs before `copy`,
                   which does log — so this snapshot is already post-reconcile.
                   Its description is still the pre-run one (only `copy` rewrites
                   that, and this is its `before`), but its taxonomy and size
                   columns may already carry reconcile's damage.

    So a caller may read `summary` from either, and the size columns only from a
    clean one. Getting this backwards is how a restore quietly writes the
    corruption back in.
    """
    cur.execute('SELECT details FROM "ActivityLog" WHERE "entityId"=%s AND details ? \'after\' '
                'AND "createdAt" < %s ORDER BY "createdAt" DESC LIMIT 1', (pid, run_start))
    row = cur.fetchone()
    if row:
        return ((row[0] or {}).get("after") or {}), "clean"

    cur.execute('SELECT details FROM "ActivityLog" WHERE "entityId"=%s AND details ? \'before\' '
                'AND "createdAt" >= %s ORDER BY "createdAt" ASC LIMIT 1', (pid, run_start))
    row = cur.fetchone()
    if row:
        return ((row[0] or {}).get("before") or {}), "contaminated"
    return None, "none"


def contemporaneous(snap: dict[str, Any], before: dict[str, Any]) -> tuple[bool, str]:
    """Is this row snapshot from the same era as the dossier?

    A "clean" snapshot is only clean in the sense that the RUN did not write it.
    It can be days old — one here was from 16 September against a run on the
    18th — and anything legitimately edited in between would be undone by
    treating it as the pre-run state. MID-000209's EU size is the example: the
    old snapshot says 48, the dossier-era product is a women's S at 36, and
    restoring 48 would have replaced the run's damage with a two-day regression.

    The dossier is the trustworthy clock: it was taken about an hour before the
    run, and six of its fields match the live column on 398 of 398 untouched
    products. So the snapshot is accepted only when it agrees with the dossier on
    all of them. If it does, its other columns are from the same era; if it does
    not, it is describing a different version of this product and is refused.
    """
    rec = before.get("record") or {}
    checks = [("title", before.get("title"))] + [
        (col, rec.get(key)) for col, key in RESTORE_FROM_DOSSIER.items()
    ]
    for column, expected in checks:
        if expected is None:
            continue
        if not _same(snap.get(column), expected):
            return False, (f"the row snapshot disagrees with the dossier on {column} "
                           f"({snap.get(column)!r} vs {expected!r}) — it is from a "
                           f"different version of this product")
    return True, ""


def pre_run_summary(snap: dict[str, Any] | None, want_chars: int | None
                    ) -> tuple[str | None, str]:
    """The description as it stood before the run, or None with a reason.

    Accepted only when its length equals the length the run itself recorded
    seeing. That is an exact check against a number written by the thing that did
    the damage, and it is what separates "the pre-run text" from "some older text
    that happens to be lying in the log". Where it fails the field is left alone:
    swapping one wrong description for a different wrong one, silently, is worse
    than leaving the regenerated one in place where a person can see it.
    """
    if want_chars is None:
        return None, "no --run entry to verify against"
    if snap is None:
        return None, "no ActivityLog snapshot"
    text = snap.get("summary")
    if text is None:
        return None, "the snapshot carries no description"
    if len(str(text)) == want_chars:
        return str(text), "ActivityLog"
    return None, (f"ActivityLog holds {len(str(text))} chars, the run saw "
                  f"{want_chars} — stale snapshot, left alone")


def plan_for(cur, pid: str, before: dict[str, Any], run_start: datetime,
             want_chars: int | None, include_price: bool = False) -> dict[str, Any]:
    """What would change on this product, reading the row as it stands now."""
    cur.execute(
        'SELECT sku, title, summary, "masterCategory", category, "subCategory", '
        '"sizingGuide", "mannequinType", "internationalSize", price, '
        '"retailPrice", properties, "updatedAt" '
        'FROM "Product" WHERE id = %s::uuid', (pid,))
    row = cur.fetchone()
    if row is None:
        return {"pid": pid, "missing": True, "changes": {}, "properties": {}}

    (sku, title, summary, master, cat, sub, guide, rig, intl, price,
     retail, props, updated_at) = row
    now = {"masterCategory": master, "category": cat, "subCategory": sub,
           "sizingGuide": guide, "mannequinType": rig, "internationalSize": intl,
           "price": price, "retailPrice": retail}

    rec = before.get("record") or {}
    changes: dict[str, tuple[Any, Any]] = {}
    notes: list[str] = []

    wanted = dict(RESTORE_FROM_DOSSIER)
    if include_price:
        wanted.update(PRICE_FROM_DOSSIER)
    for column, key in wanted.items():
        old = rec.get(key)
        if old is None:
            continue                      # not captured — nothing to restore to
        if not _same(now.get(column), old):
            changes[column] = (now.get(column), old)

    old_title = before.get("title")
    if old_title and not _same(title, old_title):
        changes["title"] = (title, old_title)

    snap, trust = row_snapshot(cur, pid, run_start)

    # Two hurdles before a column may come from the snapshot: the run must not
    # have written it (trust), and it must describe the same era as the dossier.
    usable, era_why = (True, "")
    if snap is not None and trust == "clean":
        usable, era_why = contemporaneous(snap, before)
    elif trust != "clean":
        usable, era_why = False, "the only row snapshot is post-reconcile"
    if snap is None:
        usable, era_why = False, "no row snapshot"

    for column in SIZE_FROM_ACTIVITY_LOG:
        if not usable:
            if era_why:
                notes.append(f"{column} not restorable — {era_why}")
            continue
        old = (snap or {}).get(column)
        if old is not None and not _same(now.get(column), old):
            changes[column] = (now.get(column), old)

    text, why = pre_run_summary(snap, want_chars)
    if text is not None and not _same(summary, text):
        changes["summary"] = (f"{len(str(summary or ''))} chars",
                              f"{len(text)} chars")

    # Properties, from the same snapshot and under the same rule. The dossier's
    # `gender` and `euSize` are resolved values like its size, so they are not a
    # safe fallback here either.
    prop_changes: dict[str, tuple[Any, Any]] = {}
    current_props = dict(props or {})
    old_props = dict((snap or {}).get("properties") or {}) if usable else {}
    for key in RESTORE_PROPERTIES:
        if key not in current_props or key not in old_props:
            continue                      # never held it, or nothing to go back to
        if not _same(json.dumps(current_props.get(key), sort_keys=True),
                     json.dumps(old_props.get(key), sort_keys=True)):
            prop_changes[key] = (current_props.get(key), old_props.get(key))
    if not usable and current_props and era_why:
        notes.append(f"properties not restorable — {era_why}")

    return {"pid": pid, "sku": sku, "changes": changes, "properties": prop_changes,
            "summary_text": text, "summary_why": why, "updated_at": updated_at,
            "trust": trust, "notes": notes,
            "current": {"title": title, "summary": summary, **now,
                        "properties": current_props}}


def apply_one(conn, plan: dict[str, Any]) -> None:
    """One product, one transaction, the row locked while it is rewritten."""
    sets: list[str] = []
    args: list[Any] = []
    for column, (_, old) in plan["changes"].items():
        if column == "summary":
            continue                       # written from summary_text, not the label
        sets.append(f'"{column}" = %s')
        args.append(old)
    if plan.get("summary_text") is not None and "summary" in plan["changes"]:
        sets.append('summary = %s')
        args.append(plan["summary_text"])

    with conn.cursor() as cur:
        cur.execute('SELECT properties FROM "Product" WHERE id = %s::uuid FOR UPDATE',
                    (plan["pid"],))
        current = dict((cur.fetchone() or [{}])[0] or {})
        if plan["properties"]:
            # MERGE. `properties` carries keys this script never looked at.
            for key, (_, old) in plan["properties"].items():
                current[key] = old
            sets.append("properties = %s::jsonb")
            args.append(json.dumps(current))
        if not sets:
            return
        args.append(plan["pid"])
        cur.execute(f'UPDATE "Product" SET {", ".join(sets)} WHERE id = %s::uuid', args)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db", help="database URL; else DATABASE_URL")
    ap.add_argument("--before", nargs="+", required=True,
                    help="dossier JSON taken before the run; globs are expanded")
    ap.add_argument("--run", help="the run's own output JSON — needed to verify descriptions")
    ap.add_argument("--product", help="restrict to these product ids (comma separated)")
    ap.add_argument("--run-start", default="2026-09-18T13:20",
                    help="UTC instant just before the run began (ISO). Rows at or after "
                         "it are treated as the run's own writes.")
    ap.add_argument("--include-price", action="store_true",
                    help="also put price and retailPrice back. OFF by default: the chain's "
                         "price step is a legitimate repair (it moved two products into "
                         "their grade's window) and undoing it would re-break them.")
    ap.add_argument("--apply", action="store_true", help="write the restore (default: look only)")
    ap.add_argument("--rollback-out", default="reports/generated/restore-rollback.json",
                    help="where the CURRENT values are saved before writing")
    args = ap.parse_args(argv)

    from app.config import settings

    dsn = args.db or settings().database_url
    if not dsn:
        sys.exit("Pass --db or set DATABASE_URL.")

    before = load_before(args.before)
    if not before:
        sys.exit("No products found in --before.")

    want_chars: dict[str, int | None] = {}
    if args.run:
        for r in json.loads(Path(args.run).read_text(encoding="utf-8"))["results"]:
            want_chars[str(r["product_id"])] = (r.get("before") or {}).get("description_chars")

    ids = [s.strip() for s in (args.product or "").split(",") if s.strip()] or list(want_chars) \
        or list(before)
    ids = [i for i in ids if i in before]
    run_start = datetime.fromisoformat(args.run_start)

    plans: list[dict[str, Any]] = []
    with product_audit.connect(dsn, read_only=not args.apply) as conn, conn.cursor() as cur:
        for pid in ids:
            plans.append(plan_for(cur, pid, before[pid], run_start, want_chars.get(pid),
                                  include_price=args.include_price))

    touched = [p for p in plans if p.get("changes") or p.get("properties")]
    print(f'{len(plans)} product(s) examined · {len(touched)} with something to put back'
          f'{"" if args.apply else "  (LOOK ONLY — pass --apply to write)"}\n')

    for p in plans:
        if not (p.get("changes") or p.get("properties")):
            print(f'{p.get("sku")}  unchanged since the snapshot')
            continue
        print(f'{p["sku"]}')
        for column, (now, old) in p["changes"].items():
            print(f'    {column:<18} {str(now)[:52]!r}')
            print(f'    {"":<18} -> {str(old)[:52]!r}')
        for key, (now, old) in p["properties"].items():
            print(f'    prop:{key:<13} {str(now)[:52]!r} -> {str(old)[:52]!r}')
        if p.get("summary_text") is None and p.get("summary_why"):
            print(f'    description        NOT restorable — {p["summary_why"]}')
        for n in p.get("notes") or ():
            print(f'    note               {n}')
        print()

    if not args.apply:
        print("Nothing was written.")
        return 0

    out = Path(args.rollback_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"generated": datetime.now(timezone.utc).isoformat(),
         "note": "values as they stood immediately BEFORE this restore ran",
         "products": [{"product_id": p["pid"], "sku": p.get("sku"),
                       "current": p.get("current")} for p in touched]},
        indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f'rollback of the current values -> {out}\n')

    written = 0
    with product_audit.connect(dsn, read_only=False) as conn:
        for p in touched:
            try:
                apply_one(conn, p)
                conn.commit()
                written += 1
                print(f'{p["sku"]}  restored')
            except Exception as exc:       # noqa: BLE001 — one product must not stop the rest
                conn.rollback()
                print(f'{p["sku"]}  FAILED, nothing written for it: {exc}')

    print(f'\n{written} of {len(touched)} product(s) restored.')
    unrestored = [p["sku"] for p in touched if p.get("summary_text") is None]
    if unrestored:
        print(f'description still the regenerated one on: {", ".join(unrestored)}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
