#!/usr/bin/env python
"""Is the RUNNING Hermes server serving the picture-driven taxonomy repair?

    python scripts/check_taxonomy_fix.py                       # default :8080
    python scripts/check_taxonomy_fix.py --url http://127.0.0.1:8080

WHY THIS EXISTS. `repair_product.py` runs the cut-out checks in ITS OWN process,
so an edit to app/imaging/ is live the moment the CLI starts. The taxonomy
repair does not: the chain's `reconcile` step shells out to vnyx-api, which
calls `POST {HERMES_BASE_URL}/v1/approval-gate` — a different, long-running
process that holds whatever code it was started with. `POST /v1/policy/reload`
re-reads the YAML and does NOT re-import Python, so a stale server with a fresh
policy is a state that looks healthy and silently does the old thing.

And the chain cannot tell you either way. On a product that is filed correctly
the repair legitimately plans nothing, which is indistinguishable from a server
that has never heard of it.

So this posts a SYNTHETIC product — a tank top deliberately filed under
`Women > Dresses > Casual Dress`, the MID-000615 defect — and looks for a
TAX.010 entry in the plan. Nothing is written: /v1/approval-gate is report-only
and the product id is not real.

It DOES spend one vision call, because the answer comes from the photographs.
Point `--front` / `--back` at any two garment cut-outs; the defaults are
MID-000615's own.
"""
from __future__ import annotations

import argparse
import json
import sys

FRONT = ("https://pub-bdb106864d784e55876db1813082d13d.r2.dev/products/"
         "2d1f6f31-2642-44c0-a401-bad4d4f4b74c/"
         "sina-casual-dress-dark-grey-red-trim-processed-35b45c0f.png")
BACK = ("https://pub-bdb106864d784e55876db1813082d13d.r2.dev/products/"
        "2d1f6f31-2642-44c0-a401-bad4d4f4b74c/"
        "sina-casual-dress-dark-grey-red-trim-processed-639dc2d7.png")

TENANT = "00000000-0000-0000-0000-0000000000aa"

# A tenant tree with BOTH branches real, which is the whole point: the wrong
# answer is structurally valid, so no column rule can object to it.
CATALOG = {
    TENANT: {
        "categories": {
            "Women": {
                "Dresses": ["Casual Dress", "Midi", "Maxi"],
                "T-Shirts & Tops": ["Tops", "Tank Tops", "T-Shirts"],
                "Bottoms": ["Jeans", "Trousers"],
            }
        },
        "sizingGuides": {},
        "colors": [], "materials": [], "brands": [],
    }
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--front", default=FRONT)
    ap.add_argument("--back", default=BACK)
    ap.add_argument("--json", action="store_true", help="print the whole plan")
    args = ap.parse_args()

    import httpx

    product = {
        "id": "00000000-0000-0000-0000-0000000000bb",
        "tenantId": TENANT,
        "sku": "PROBE-001",
        "title": "Probe garment",
        "masterCategory": "Women",
        "category": "Dresses",          # <- structurally valid, factually wrong
        "subCategory": "Casual Dress",  # <-
        "properties": {"gender": ["women"], "brand": "Probe", "color": "Grey"},
        "images": [args.front, args.back],
    }
    media = [
        {"url": args.front, "view": "FRONT", "origin": "PHOTOBOOTH",
         "processing": "BG_REMOVED", "isCurrent": True, "position": 10000},
        {"url": args.back, "view": "BACK", "origin": "PHOTOBOOTH",
         "processing": "BG_REMOVED", "isCurrent": True, "position": 11000},
    ]

    try:
        r = httpx.post(f"{args.url.rstrip('/')}/v1/approval-gate",
                       json={"product": product, "media": media,
                             "catalog": CATALOG, "use_llm": True},
                       timeout=180)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  cannot reach {args.url}: {type(exc).__name__} {exc}")
        return 2
    if r.status_code != 200:
        print(f"FAIL  {args.url} returned {r.status_code}: {r.text[:300]}")
        return 2

    body = r.json()
    # `repair_plan`, not `plan` — the key the gate actually returns. Reading the
    # wrong one makes every server look stale, which is the most misleading
    # answer a check like this can give.
    plan = body.get("repair_plan") or []
    if args.json:
        print(json.dumps(body, indent=2, ensure_ascii=False))

    tax = [a for a in plan if a.get("reason") == "TAX.010"]
    print(f"server   {args.url}")
    print(f"filed as Women > Dresses > Casual Dress   (both exist in the tree)")
    print(f"plan     {len(plan)} entr{'y' if len(plan) == 1 else 'ies'}")

    if not tax:
        print()
        print("STALE — no TAX.010 in the plan.")
        print("  The running server has not been restarted since the taxonomy")
        print("  repair landed. `POST /v1/policy/reload` does NOT re-import")
        print("  Python; stop uvicorn and start it again.")
        for a in plan:
            print(f"    {a.get('kind')} {a.get('field')} = {a.get('value')!r}"
                  f"  ({a.get('reason')})")
        return 1

    print()
    print("LIVE — the server is serving the picture-driven repair:")
    for a in tax:
        print(f"    {a.get('kind')} {a.get('field')} = {a.get('value')!r}")
        if a.get("detail"):
            print(f"      {a['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
