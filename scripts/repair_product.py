#!/usr/bin/env python
"""Take one product from whatever state it is in to as finished as it can get.

    # LOOK FIRST. Nothing written, no model called, nothing spent.
    python scripts/repair_product.py --db "postgresql://..." --product <uuid>

    # DO IT.
    python scripts/repair_product.py --db "..." --product <uuid> --apply

    # A batch, or the worst rows of an audit sheet.
    python scripts/repair_product.py --db "..." --products a,b,c --apply
    python scripts/repair_product.py --db "..." \
        --from-sheet reports/generated/prod-review.xlsx --limit 10 --apply

Four steps, each SKIPPED when the product does not need it. Nothing here is a new
implementation: every step shells out to the script that already owns that work,
so a render produced by this and one produced by the Model images button are the
same render.

    1  matte        scripts/backfill-bg-removal.ts   cut out the garment photos
    2  extract      scripts/backfill-product-data.ts read the care label -> brand,
                                                     size, material, description
    3  reconcile    app.product_audit                drift, taxonomy, sizing guide
    4  render       scripts/backfill-imagery.ts      the five on-model views
    5  re-audit     app.product_audit                the verdict that counts


THE ORDER IS NOT THE ORDER YOU WOULD GUESS, AND IT IS THE POINT OF THIS FILE

"Generate the missing images, then fill in the missing fields" is the obvious
sequence and it is the expensive one.

Hermes picks the MODEL'S GENDER from the product's own masterCategory and gender
property (`_product_gender` in app/main.py — "a Men's shirt gets a male model
even when the tenant's default is female"). A product whose gender is absent or
wrong therefore renders the wrong model, at ~200s and a paid image call per view,
and the only fix is to correct the field and render all five again.

So the fields are settled BEFORE a single render is paid for:

    matte -> extract -> reconcile -> render

Matting comes first because the cut-outs are the cleaner input when the extractor
is allowed to look at the garment at all (see --infer), and because the renderer
needs them anyway.


--infer, AND WHY IT IS OFF BY DEFAULT

Attributes are read from the CARE LABEL ALONE unless --infer is passed. That is
not caution for its own sake; backfill-product-data.ts records the failure it
prevents:

    Shown a garment photo, the model reads the GARMENT: on a green jersey whose
    care label says only "MADE IN CANADA / 100% POLYESTER / STYLE #", it returned
    brand "Reebok" — off the shirt's appearance, not off the label. That is a
    plausible guess written into a catalogue as a fact, and it is worse than
    leaving the field empty, because nothing downstream can tell the two apart.

--infer sends the cut-outs alongside the label. It is the right choice for
`description` and `color`, which cannot be read off a wash tag at all, and the
wrong one for `brand`, `size` and `material`, which can. The confidence floor
(--min-confidence, default 70) is what catches the worst of it: a leather jacket
came back as material "Knit" at 45 and trousers as fit "One Size" at 30, and both
were dropped.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import settings  # noqa: E402

# Where the TypeScript half lives. Every write this script causes happens in
# there, because that is where R2, the ProductMedia invariants, the price mirror
# and the credit accounting are.
DEFAULT_VNYX_API = os.getenv("VNYX_API_DIR", r"E:\vynx\vnyx-api")

_TTY = sys.stdout.isatty()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):  # pragma: no cover
    pass


def _encodable(sample: str) -> bool:
    try:
        sample.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_OK = _encodable("→·")
ARROW = "→" if _OK else "->"
DOT = "·" if _OK else "|"


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


RED, YELLOW, GREEN, DIM, BOLD = "31", "33", "32", "2", "1"


# --------------------------------------------------------------------------- #
# Running the TypeScript half
# --------------------------------------------------------------------------- #

class StepFailed(Exception):
    pass


def run_node(vnyx_api: Path, script: str, args: list[str], *,
             timeout_s: int, quiet: bool) -> tuple[bool, str]:
    """Shell out to one of vnyx-api's scripts. Returns (ok, output).

    `npx tsx`, not a compiled build: tsconfig.json only includes `src`, so
    scripts/ is never type-checked or emitted and tsx is the only way it runs.
    """
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        raise StepFailed("npx not found on PATH — Node is required for this step")

    cmd = [npx, "tsx", f"scripts/{script}", *args]
    try:
        proc = subprocess.run(
            cmd, cwd=str(vnyx_api), capture_output=True, text=True,
            timeout=timeout_s, encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise StepFailed(f"{script} timed out after {timeout_s}s") from None

    out = (proc.stdout or "") + (proc.stderr or "")
    if not quiet:
        for line in out.splitlines():
            if line.strip():
                print(paint(f"      {line}", DIM))
    return proc.returncode == 0, out


# --------------------------------------------------------------------------- #
# What the product still needs
# --------------------------------------------------------------------------- #

def needs(dsn: str, product_id: str) -> dict[str, Any]:
    """Read the product once and say which steps apply.

    Uses the audit's own view rather than a second set of queries, so "needs a
    description" here and "description missing" on the sheet cannot disagree.
    """
    loaded = product_audit.load(dsn, product_id)
    record, media = loaded["record"], loaded["media"]

    live = [m for m in media if m["mediaType"] == "IMAGE"]
    garments = [m for m in live if m["view"] in ("FRONT", "BACK", "OTHER")]
    unmatted = [m for m in garments if m["processing"] == "RAW"]
    renders = {m["view"] for m in live if m["view"].startswith("AI_")}

    summary = (record.get("summary") or "").strip()
    return {
        "loaded": loaded,
        "title": record.get("title"),
        "description_missing": not summary,
        "description_chars": len(summary),
        "care_label": record.get("careLabelCount") or 0,
        "unmatted": len(unmatted),
        "renders": len(renders),
        "renders_missing": 5 - len(renders),
        "attributes_missing": [
            name for name, value in (
                ("brand", record.get("brand")),
                ("size", record.get("size")),
                ("material", record.get("material")),
                ("color", record.get("color")),
                ("condition", record.get("condition")),
            ) if not value
        ],
    }


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #

def repair(dsn: str, product_id: str, *, apply: bool, vnyx_api: Path,
           infer: bool, min_confidence: int, skip_render: bool,
           quiet: bool) -> dict[str, Any]:
    started = time.perf_counter()
    state = needs(dsn, product_id)
    steps: list[dict[str, Any]] = []

    print()
    print(paint(str(state["title"] or "(no title)"), BOLD),
          paint(f'{DOT} {product_id}', DIM))
    print(paint(
        f'  description {state["description_chars"]} chars {DOT} '
        f'care label {state["care_label"]} {DOT} '
        f'{state["unmatted"]} unmatted {DOT} {state["renders"]}/5 renders {DOT} '
        f'missing {", ".join(state["attributes_missing"]) or "nothing"}', DIM))

    def step(name: str, why: str, run) -> None:
        if why:
            print(f'  {paint("skip ", DIM)} {name:12} {paint(why, DIM)}')
            steps.append({"step": name, "ran": False, "why": why})
            return
        print(f'  {paint("run  ", GREEN if apply else YELLOW)} {name:12}'
              f'{"" if apply else paint("  (dry run)", DIM)}')
        t0 = time.perf_counter()
        try:
            note = run()
            ok = True
        except StepFailed as exc:
            note, ok = str(exc), False
            print(f'        {paint("FAILED: " + note, RED)}')
        steps.append({"step": name, "ran": True, "ok": ok, "note": note,
                      "seconds": round(time.perf_counter() - t0, 1)})

    common = ["--db", dsn, "--product", product_id]
    live = ["--apply"] if apply else []

    # ---- 1. matte ---------------------------------------------------------
    def _matte() -> str:
        ok, out = run_node(vnyx_api, "backfill-bg-removal.ts",
                           [*common, *live], timeout_s=600, quiet=quiet)
        if not ok:
            raise StepFailed("background removal returned non-zero")
        return f'{state["unmatted"]} garment photo(s)'

    step("matte", "" if state["unmatted"] else "every garment photo is already cut out",
         _matte)

    # ---- 2. extract -------------------------------------------------------
    #
    # BEFORE the renders, not after. See the module docstring: the renderer picks
    # the model's gender off masterCategory/gender, so rendering first can spend
    # five paid calls putting the wrong model in the clothes.
    def _extract() -> str:
        args = [*common, *live, "--min-confidence", str(min_confidence)]
        if infer:
            args.append("--infer")
        ok, out = run_node(vnyx_api, "backfill-product-data.ts", args,
                           timeout_s=900, quiet=quiet)
        if not ok:
            raise StepFailed("extraction returned non-zero")
        return "read the care label" + (" and the cut-outs" if infer else "")

    want_extract = state["description_missing"] or state["attributes_missing"]
    if not state["care_label"] and not infer:
        extract_why = ("no care label to read — attributes are never inferred "
                       "from the garment (pass --infer to override)")
    elif not want_extract:
        extract_why = "description and every attribute already present"
    else:
        extract_why = ""
    step("extract", extract_why, _extract)

    # ---- 3. reconcile -----------------------------------------------------
    #
    # In-process: this is the drift / taxonomy / sizing-guide repair, and it is
    # the same apply_plan the audit endpoint uses. Runs AFTER extraction so it
    # settles the values extraction just wrote, and BEFORE rendering so the
    # mannequin and gender are right when the model is chosen.
    def _reconcile() -> str:
        result = product_audit.audit(dsn, product_id, apply=apply,
                                     write_sheet=False)
        if result["conflict"]:
            raise StepFailed(result.get("message", "changed mid-repair"))
        fixed = [f'{a.get("field")}={a.get("value")}' for a in result["fixes"]]
        return "; ".join(fixed) if fixed else "nothing to reconcile"

    step("reconcile", "", _reconcile)

    # ---- 4. render --------------------------------------------------------
    def _render() -> str:
        ok, out = run_node(vnyx_api, "backfill-imagery.ts",
                           [*common, *live], timeout_s=1800, quiet=quiet)
        if not ok:
            raise StepFailed("imagery backfill returned non-zero")
        return f'{state["renders_missing"]} view(s)'

    if skip_render:
        render_why = "--no-render"
    elif not state["renders_missing"]:
        render_why = "all five views already exist"
    else:
        render_why = ""
    step("render", render_why, _render)

    # ---- 5. the verdict that counts ---------------------------------------
    #
    # Recomputed from the STORED record, never from the plan. A step can report
    # success and leave the field unwritten, and a report that trusted its own
    # steps would call the product finished.
    after = needs(dsn, product_id)
    final = product_audit.audit(dsn, product_id, apply=False, write_sheet=False)

    return {
        "product_id": product_id,
        "title": state["title"],
        "applied": apply,
        "before": {k: state[k] for k in
                   ("description_chars", "unmatted", "renders",
                    "attributes_missing")},
        "after": {k: after[k] for k in
                  ("description_chars", "unmatted", "renders",
                   "attributes_missing")},
        "steps": steps,
        "verified": final["verified_after"],
        "remaining": [f["rule_id"] for f in final["remaining"]],
        "issues": final["counts"]["issues"],
        "seconds": round(time.perf_counter() - started, 1),
    }


def report(r: dict[str, Any]) -> None:
    b, a = r["before"], r["after"]
    print()
    print(paint("  result", BOLD))

    def line(label: str, was: Any, now: Any) -> None:
        changed = was != now
        arrow = f'{was} {ARROW} {now}' if changed else str(now)
        print(f'    {label:14} {paint(arrow, GREEN if changed else DIM)}')

    line("description", f'{b["description_chars"]} chars',
         f'{a["description_chars"]} chars')
    line("unmatted", b["unmatted"], a["unmatted"])
    line("renders", f'{b["renders"]}/5', f'{a["renders"]}/5')
    line("missing attrs", ", ".join(b["attributes_missing"]) or "none",
         ", ".join(a["attributes_missing"]) or "none")
    print(f'    {"issues left":14} {r["issues"]}'
          + (f'  ({", ".join(r["remaining"])})' if r["remaining"] else ""))
    print(f'    {"took":14} {r["seconds"]}s')


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db")
    ap.add_argument("--product", "--products", dest="products",
                    help="one uuid, or a comma-separated list.")
    ap.add_argument("--from-sheet", dest="from_sheet",
                    help="Product IDs from an audit sheet, worst-first.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--apply", action="store_true",
                    help="ACTUALLY do it. Spends money on the render step.")
    ap.add_argument("--infer", action="store_true",
                    help=("let the extractor see the garment cut-outs as well as "
                          "the care label. Fills description and colour; risks a "
                          "guessed brand. See the note in this file."))
    ap.add_argument("--min-confidence", type=int, default=70,
                    help="drop extracted values scoring below this (0-100).")
    ap.add_argument("--no-render", action="store_true",
                    help="skip the paid image generation step.")
    ap.add_argument("--vnyx-api", default=DEFAULT_VNYX_API,
                    help=f"path to the vnyx-api repo. Default {DEFAULT_VNYX_API}")
    ap.add_argument("--out", help="write a JSON summary here.")
    ap.add_argument("--quiet", action="store_true",
                    help="hide the sub-scripts' own output.")
    args = ap.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")

    vnyx_api = Path(args.vnyx_api)
    if not (vnyx_api / "scripts" / "backfill-product-data.ts").exists():
        sys.exit(f"No vnyx-api scripts at {vnyx_api}. Pass --vnyx-api <path> "
                 f"or set VNYX_API_DIR.")

    ids: list[str] = []
    if args.from_sheet:
        from scripts.audit_review_products import read_sheet_ids  # noqa
        ids = read_sheet_ids(Path(args.from_sheet))
    elif args.products:
        ids = [s.strip() for s in args.products.split(",") if s.strip()]
    if not ids:
        sys.exit("Pass --product <uuid[,uuid]> or --from-sheet <xlsx>.")
    if args.limit:
        ids = ids[:args.limit]

    if not args.apply:
        print(paint("DRY RUN — nothing is written and no model is called. "
                    "Pass --apply to do it.", YELLOW))

    results = []
    for pid in ids:
        try:
            r = repair(dsn, pid, apply=args.apply, vnyx_api=vnyx_api,
                       infer=args.infer, min_confidence=args.min_confidence,
                       skip_render=args.no_render, quiet=args.quiet)
        except product_audit.ProductNotFound:
            print(paint(f"no product {pid}", RED))
            continue
        report(r)
        results.append(r)

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({"applied": args.apply,
                        "generated": datetime.now(timezone.utc).isoformat(),
                        "results": results}, indent=2, default=str),
            encoding="utf-8")
        print()
        print(paint("  json:", DIM), args.out)

    print()
    done = sum(1 for r in results if r["verified"])
    print(f'  {len(results)} product(s) {DOT} {done} now pass every blocking rule')
    return 0


if __name__ == "__main__":
    sys.exit(main())
