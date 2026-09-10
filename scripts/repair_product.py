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
    5  approve      scripts/approve-products.ts      REVIEW -> APPROVED, if ready
    6  re-audit     app.product_audit                the verdict that counts


APPROVAL IS OPT-IN, AND THAT IS NOT TIMIDITY

The readiness CHECK always runs and is always reported. The MOVE needs
`--approve`, because of what approve-products.ts says it does:

    APPROVING PUBLISHES TO SHOPIFY. updateProduct -> syncProductStage sees a real
    REVIEW -> APPROVED movement and fires onApprovedArrival, which enqueues an
    upsert with `force: true`. That bypasses the tenant's autoSyncProducts
    preference but not the connected-account requirement -- so for a tenant with
    a connected account this creates a LIVE LISTING. There is no undo here: the
    listing has to be removed on Shopify's side.

A repair command should not publish a public listing as a side effect of tidying
a size field. So: `--apply` repairs, `--apply --approve` also publishes.

THE BACKEND MUST BE RUNNING FOR THE LISTING TO APPEAR. onApprovedArrival only
ENQUEUES the upsert, onto the BullMQ queue in REDIS_URL. The consumer is the
shopify-sync worker, which lives inside the vnyx-api process (src/index.ts
imports every worker for its side effects). With the API down the approval still
commits and the job simply waits in Redis until something drains it — and the
API must be pointed at the SAME database these products are in, or the worker
looks up ids that are not there.

AND THE STAGE IS DERIVED, NEVER WRITTEN. `deriveStage` returns APPROVED as soon
as reviewStatus is ACCEPTED, so the approval writes reviewStatus and lets the
stage follow. That is also why this shells out instead of doing it in Python: a
direct `UPDATE "Product" SET "reviewStatus"='ACCEPTED'` would move the product
and never fire onApprovedArrival, so it would look approved and never reach
Shopify at all.


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
# The REPO OF RECORD. All six scripts this chain drives live here now:
# backfill-bg-removal, relabel-ai-views, backfill-product-data,
# verify-and-repair, backfill-imagery and approve-products. An older second
# checkout at E:\vynx\vnyx-api used to be the only place that had five of them
# and is retired — see docs/auto-approval-agent-implementation-plan.md §1.2.
DEFAULT_VNYX_API = os.getenv("VNYX_API_DIR", r"E:\vnyx\vnyx-api")

# DEFENSIVE ABOUT sys.stdout, because this module is no longer only a CLI.
#
# The Auto Approval worker imports it, and Celery replaces sys.stdout with a
# LoggingProxy that has NO `.encoding` attribute — so a bare
# `sys.stdout.encoding` raised AttributeError at import time and the whole
# module failed to load. pytest's capture and a plain StringIO are the same
# shape. Nothing about sys.stdout is guaranteed beyond `write`.
#
# getattr with a default in every case, so an unusual stream degrades to plain
# ASCII output instead of stopping the import.
def _tty() -> bool:
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover
        return False


_TTY = _tty()
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):  # pragma: no cover
    pass


def _encodable(sample: str) -> bool:
    try:
        sample.encode(getattr(sys.stdout, "encoding", None) or "ascii")
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


# Which of vnyx-api's scripts each step runs. The REMOTE transport names the
# step rather than the script — the server resolves it through its own closed
# map, so nothing this file sends can become a filename over there.
STEP_FOR_SCRIPT = {
    "backfill-bg-removal.ts": "matte",
    "relabel-ai-views.ts": "relabel",
    "backfill-product-data.ts": "extract",
    "verify-and-repair.ts": "reconcile",
    "backfill-imagery.ts": "render",
    "approve-products.ts": "approve",
}


def vnyx_api_url() -> str | None:
    """The remote step runner's base URL, or None for the local transport.

    THE TRANSPORT SWITCH, and the whole reason it exists:
    `repair()` causes writes that belong to vnyx-api — the `properties` merge,
    the ProductVariant price mirror, recordStageTransition, the ProductMedia
    invariants, R2 keys, credit accounting — so it has always run vnyx-api's own
    scripts rather than reimplementing any of that.
    It used to do so by SPAWNING them, which required a vnyx-api checkout, its
    node_modules and its whole credential set (R2, OpenAI, Bedrock, the
    background-removal key) on whatever machine ran this file. Fine on a laptop
    with both repos; wrong when Hermes is deployed on its own host.

    With VNYX_API_URL set, each step becomes one authenticated HTTP call and the
    spawn happens on the host that already owns the checkout and the
    credentials. Hermes then needs only a database URL, a Redis URL, this URL
    and a shared secret.

    Local spawning is kept, not replaced: a single-box developer setup and the
    CLI both still work with VNYX_API_DIR alone.
    """
    return (os.getenv("VNYX_API_URL") or "").rstrip("/") or None


def run_remote(script: str, args: list[str], *, timeout_s: int,
               quiet: bool) -> tuple[bool, str, dict[str, Any] | None]:
    """Ask vnyx-api to run one step. Returns (ok, output, results).

    The DSN is deliberately NOT sent. The server's own DATABASE_URL is used, so
    a connection string never travels over the wire and this worker cannot point
    that host at a different database. Both sides must therefore agree on which
    database they use — the response reports which one it was, and the worker's
    preflight compares it, so a mismatch surfaces as a refusal rather than as
    inexplicable results.
    """
    import httpx

    base = vnyx_api_url()
    secret = os.getenv("AUTO_APPROVAL_INTERNAL_SECRET", "")
    if not base:
        raise StepFailed("VNYX_API_URL is not set")
    if not secret:
        raise StepFailed(
            "AUTO_APPROVAL_INTERNAL_SECRET is not set — the step runner will "
            "refuse an unauthenticated call")

    step = STEP_FOR_SCRIPT.get(script)
    if step is None:
        raise StepFailed(f"no remote step maps to {script}")

    # argv -> typed options. The remote end rebuilds argv itself from a closed
    # map, so the flags are re-expressed rather than forwarded.
    product_id = args[args.index("--product") + 1] if "--product" in args else None
    options: dict[str, Any] = {}
    if "--min-confidence" in args:
        options["minConfidence"] = int(args[args.index("--min-confidence") + 1])
    if "--infer" in args:
        options["infer"] = True
    if "--skip-bin" in args:
        options["skipBin"] = True
    if "--authorize" in args:
        options["authorize"] = True
    # `--apply` on the approve script is what publishes to Shopify, so it is
    # carried as its own flag rather than folded into `apply`.
    if step == "approve" and "--apply" in args:
        options["approve"] = True

    payload = {
        "step": step,
        "productId": product_id,
        "apply": "--apply" in args,
        "options": options,
    }

    try:
        # A little longer than the step's own budget, so the server's timeout
        # fires first and we get its output rather than a bare read timeout.
        resp = httpx.post(
            f"{base}/internal/auto-approval/step",
            json=payload,
            headers={"x-internal-secret": secret},
            timeout=httpx.Timeout(timeout_s + 30, connect=15),
        )
    except httpx.HTTPError as exc:
        raise StepFailed(f"{script}: cannot reach vnyx-api ({exc})") from None

    if resp.status_code != 200:
        raise StepFailed(
            f"{script}: vnyx-api returned {resp.status_code} "
            f"{resp.text[:300]}")

    body = resp.json()
    out = str(body.get("output") or "")
    if not quiet:
        for line in out.splitlines():
            if line.strip():
                print(paint(f"      {line}", DIM))
    if body.get("timedOut"):
        raise StepFailed(f"{script} timed out after {timeout_s}s")
    return bool(body.get("ok")), out, body.get("results")


def run_step(vnyx_api: Path, script: str, args: list[str], *,
             timeout_s: int, quiet: bool,
             results_name: str | None = None
             ) -> tuple[bool, str, dict[str, Any] | None]:
    """Run one step over whichever transport is configured.

    Uniform in the results: the two steps that report a structured verdict
    (approve, reconcile) get it back the same way whether the script wrote a
    file next to us or on another machine.
    """
    if vnyx_api_url():
        return run_remote(script, args, timeout_s=timeout_s, quiet=quiet)

    if results_name:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / results_name
            ok, output = run_node(vnyx_api, script, [*args, "--results", str(out)],
                                  timeout_s=timeout_s, quiet=quiet)
            results = None
            if out.exists():
                results = json.loads(out.read_text(encoding="utf-8"))
        return ok, output, results

    ok, output = run_node(vnyx_api, script, args,
                          timeout_s=timeout_s, quiet=quiet)
    return ok, output, None


def run_node(vnyx_api: Path, script: str, args: list[str], *,
             timeout_s: int, quiet: bool) -> tuple[bool, str]:
    """Spawn one of vnyx-api's scripts locally. Returns (ok, output).

    The LOCAL transport, used when VNYX_API_DIR is set and VNYX_API_URL is not —
    a single-box developer setup, or this file run as a CLI.

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
    render_rows = [m for m in live if m["view"].startswith("AI_")]
    renders = {m["view"] for m in render_rows}

    # PER VIEW, not per row — the test backfill-bg-removal.ts actually applies:
    # "does a live BG_REMOVED row exist for this view". Counting RAW ROWS instead
    # made the matte step run on every pass and change nothing, because a product
    # can hold a matted FRONT and a second, never-matted FRONT original at the
    # same time and the script (correctly) considers that view done.
    matted_views = {m["view"] for m in garments if m["processing"] == "BG_REMOVED"}
    unmatted_views = sorted(
        {m["view"] for m in garments if m["processing"] == "RAW"} - matted_views
    )
    # Live RAW rows sitting BESIDE a cut-out of the same view. Not something the
    # matte step will touch, and not nothing either: replaceWithDerived
    # supersedes the original it mattes, so a live RAW next to a live BG_REMOVED
    # is either a second source photo nobody matted or a supersede that did not
    # happen. IMG.010 reports it; this is here so the reason the matte step
    # skipped is visible rather than mysterious.
    leftover_raw = len([m for m in garments
                        if m["processing"] == "RAW" and m["view"] in matted_views])

    summary = (record.get("summary") or "").strip()
    return {
        "loaded": loaded,
        "title": record.get("title"),
        "tenant": record.get("tenantName"),
        "sku": record.get("sku"),
        "stage": record.get("currentStage"),
        "review_status": record.get("reviewStatus"),
        "edit_url": record.get("editUrl"),
        "description_missing": not summary,
        "description_chars": len(summary),
        "care_label": record.get("careLabelCount") or 0,
        "unmatted": len(unmatted_views),
        "unmatted_views": unmatted_views,
        "leftover_raw": leftover_raw,
        "renders": len(renders),
        "render_rows": len(render_rows),
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

def approve_check(vnyx_api: Path, dsn: str, product_id: str, *,
                  apply: bool, skip_bin: bool,
                  quiet: bool) -> dict[str, Any]:
    """Run approve-products.ts and read back its structured verdict.

    Shelled out rather than reimplemented for the reason in the module
    docstring: the pre-flight is only half of it, and the other half —
    updateProduct -> syncProductStage -> onApprovedArrival -> the Shopify
    enqueue — is a chain a Python UPDATE would skip silently.

    Without `--apply` the script performs the whole pre-flight and reports
    `would_approve` or `skipped_preflight` with the reasons, so the readiness
    answer costs nothing and is safe to run on every product.
    """
    args = ["--db", dsn, "--product", product_id]
    if apply:
        args.append("--apply")
    if skip_bin:
        args.append("--skip-bin")
    ok, _, payload = run_step(vnyx_api, "approve-products.ts", args,
                              timeout_s=300, quiet=quiet,
                              results_name="approve.json")
    if payload is None:
        raise StepFailed(
            "approve-products.ts returned no verdict"
            + ("" if ok else " and exited non-zero"))

    rows = payload.get("results") or []
    if not rows:
        raise StepFailed("approve-products.ts returned no verdict")
    return rows[0]


def repair(dsn: str, product_id: str, *, apply: bool, vnyx_api: Path,
           infer: bool, min_confidence: int, skip_render: bool,
           approve: bool, skip_bin: bool,
           quiet: bool, progress: str = "") -> dict[str, Any]:
    started = time.perf_counter()
    state = needs(dsn, product_id)
    steps: list[dict[str, Any]] = []

    print()
    print(f'{paint(progress, BOLD) + "  " if progress else ""}'
          f'{paint(str(state["title"] or "(no title)"), BOLD)} '
          f'{paint(f"{DOT} {product_id}", DIM)}')
    print(paint(
        f'  description {state["description_chars"]} chars {DOT} '
        f'care label {state["care_label"]} {DOT} '
        f'{state["unmatted"]} view(s) unmatted {DOT} '
        f'{state["renders"]}/5 renders {DOT} '
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
        ok, out, _ = run_step(vnyx_api, "backfill-bg-removal.ts",
                           [*common, *live], timeout_s=600, quiet=quiet)
        if not ok:
            raise StepFailed("background removal returned non-zero")
        return f'{", ".join(state["unmatted_views"])}'

    matte_why = ""
    if not state["unmatted"]:
        matte_why = "every garment view already has a cut-out"
        if state["leftover_raw"]:
            # Said out loud, because IMG.010 will still be in the findings and a
            # skipped matte step next to it reads as the step failing.
            matte_why += (f' ({state["leftover_raw"]} extra RAW row(s) live '
                          f'beside one — IMG.010, not fixable here)')
    step("matte", matte_why, _matte)

    # ---- 1b. relabel ------------------------------------------------------
    #
    # BEFORE the render decision, because it changes what "missing" means.
    #
    # An older generation path wrote every render of a product under AI_FRONT.
    # The five images exist and are correct — the filenames still say
    # `-back`, `-front-34`, `-closeup` — but the column says AI_FRONT five
    # times, so anything asking "does this have a back render" answers no.
    # 2,360 products on production carry 5 rows under 1 view.
    #
    # backfill-imagery.ts already refuses to regenerate these ("5 renders
    # already exist, all filed under AI_FRONT — this needs relabelling, not
    # regenerating"), which left the product stuck: the renders were never
    # regenerated, the labels were never fixed, and the approval pre-flight kept
    # failing on `missing AI_BACK render` for a product that has one.
    #
    # Pure metadata, no model, no image fetched — so it is free and safe to run
    # on every product. It only touches products whose labels are already wrong.
    def _relabel() -> str:
        ok, out, _ = run_step(vnyx_api, "relabel-ai-views.ts",
                           [*common, *live], timeout_s=300, quiet=quiet)
        if not ok:
            raise StepFailed("relabel returned non-zero")
        return "recovered the true view from the filenames"

    mislabelled = state["renders"] < state["render_rows"]
    step("relabel",
         "" if mislabelled else "every render is filed under its own view",
         _relabel)

    # The render step's decision depends on what relabel just fixed, so the
    # counts are re-read rather than reused. Skipping this re-read would leave
    # render regenerating five views the relabel had just recovered.
    if mislabelled and apply:
        state = {**state, **{k: needs(dsn, product_id)[k]
                             for k in ("renders", "renders_missing")}}

    # ---- 2. extract -------------------------------------------------------
    #
    # BEFORE the renders, not after. See the module docstring: the renderer picks
    # the model's gender off masterCategory/gender, so rendering first can spend
    # five paid calls putting the wrong model in the clothes.
    def _extract() -> str:
        args = [*common, *live, "--min-confidence", str(min_confidence)]
        if infer:
            args.append("--infer")
        ok, out, _ = run_step(vnyx_api, "backfill-product-data.ts", args,
                           timeout_s=900, quiet=quiet)
        if not ok:
            raise StepFailed("extraction returned non-zero")
        return "read the care label" + (" and the cut-outs" if infer else "")

    want_extract = state["description_missing"] or state["attributes_missing"]
    # Everything still missing that ONLY the care-label pass below can do better.
    narrow_only = (
        state["care_label"]
        and not state["description_missing"]
        and set(state["attributes_missing"]) <= {"brand", "size"}
    )
    if not state["care_label"] and not infer:
        extract_why = ("no care label to read — attributes are never inferred "
                       "from the garment (pass --infer to override)")
    elif not want_extract:
        extract_why = "description and every attribute already present"
    elif narrow_only:
        # A seventeen-field extraction to recover one size is a vision call
        # spent on a question the next step asks better. This is the exact case
        # the narrow pass was written for — the bulk extractor scored that size
        # 0% while the focused one read it cleanly.
        extract_why = (f'only {", ".join(state["attributes_missing"])} missing — '
                       f'the care-label pass reads those better')
    else:
        extract_why = ""
    step("extract", extract_why, _extract)

    # ---- 2b. the care-label second pass -----------------------------------
    #
    # Only for brand and size, only when the bulk extractor left them empty, and
    # only when a care label exists.
    #
    # getStructuredProductJSON asks for seventeen fields at once. On a Columbia
    # puffer whose label plainly reads "Columbia Sportswear Company", "MADE IN
    # CHINA" and "S", it returned brand at 98% and size at 0% — and `no size`
    # then blocked approval on a product whose size is in the photograph. Asked
    # that one question on its own, with the label images and nothing else,
    # Gemini read "S" at high confidence.
    #
    # A narrow question is a different question, not a louder one: no
    # seventeen-field schema competing for attention, and no garment photo to
    # read a brand off. Same confidence floor as the bulk pass — a guessed size
    # ships, an absent one gets fixed.
    label_read: dict[str, Any] = {}

    def _label_pass() -> str:
        nonlocal label_read
        from app.llm import care_label

        urls = [m["url"] for m in state["loaded"]["media"] if m["view"] == "LABEL"]
        wanted = tuple(f for f in ("brand", "size")
                       if f in state["attributes_missing"])
        # The attached guide's ladder, so a misread CHARACTER is caught by the
        # tenant's own configuration rather than by a confidence score. OpenAI
        # read this product's "S" as the digit "5" at 85%.
        guide = state["loaded"]["record"].get("sizingGuide")
        ladder = ((state["loaded"]["catalog"].get("sizingGuides") or {})
                  .get(guide or "", {}) or {}).get("sizes")
        label_read = care_label.read(urls, want=wanted,
                                     min_confidence=min_confidence,
                                     size_ladder=ladder)
        if label_read.get("error"):
            raise StepFailed(label_read["error"])

        found = {f: label_read[f] for f in wanted if label_read.get(f)}
        if not found:
            rejected = label_read.get("rejected") or {}
            return ("nothing legible"
                    + (f' (best confidence: {rejected})' if rejected else "")
                    + f' — tried {", ".join(label_read["tried"])}')

        if apply:
            # Through apply_plan so these land the same way every other repair
            # does: one transaction, guarded on updatedAt, and BOTH copies of a
            # doubled value written. `size` maps to properties.international_size
            # and mirrors into the internationalSize column.
            plan = [{"kind": "set_property",
                     "field": "international_size" if f == "size" else f,
                     "value": v, "reason": "CARE_LABEL"}
                    for f, v in found.items()]
            outcome = product_audit.apply_plan(
                dsn, product_id, plan, state["loaded"]["record"].get("updatedAt"))
            if outcome["conflict"]:
                raise StepFailed(outcome.get("message", "changed mid-repair"))
            if outcome["skipped"]:
                raise StepFailed("; ".join(
                    s.get("why", "?") for s in outcome["skipped"]))

        return (", ".join(f'{f}={v!r}' for f, v in found.items())
                + f' (read by {label_read.get("provider")})')

    if not state["care_label"]:
        label_why = "no care-label photograph"
    elif not any(f in state["attributes_missing"] for f in ("brand", "size")):
        label_why = "brand and size are both already present"
    else:
        label_why = ""
    step("care label", label_why, _label_pass)

    # ---- 3. reconcile -----------------------------------------------------
    #
    # The drift / taxonomy / sizing-guide / PRICE repair. Runs AFTER extraction
    # so it settles the values extraction just wrote, and BEFORE rendering so the
    # mannequin and gender are right when the model is chosen.
    #
    # SHELLED OUT, NOT IN-PROCESS, AND THE REASON IS THE PRICE.
    #
    # This used to call product_audit.audit(apply=True), which routes its writes
    # through apply_plan — and apply_plan REFUSES price writes by name:
    #
    #     "price writes must go through updateProduct — it mirrors into
    #      ProductVariant, Inventory and a Price row per connected marketplace
    #      account"
    #
    # So the chain planned a PRICE.002 repair and then declined to apply it. A
    # price-blocked product ran the whole pipeline, spent the renders, and came
    # out still unverified — forever, on every retry. PRICE.002 (price outside
    # the window the grade factor implies) is one of the most common findings in
    # this catalogue, so that was not an edge case.
    #
    # verify-and-repair.ts calls `verifyAndRepair`, the SAME function the
    # reviewer's "Verify & fix" button calls, so every write lands through
    # updateProduct with the price mirror, the money normalisation, the
    # properties merge and the ProductMedia cache rebuild. No verification logic
    # is duplicated, and no service credential is needed because this is a
    # subprocess rather than an HTTP call.
    #
    # A DRY RUN STILL RUNS IT, without --apply: the script reports the plan and
    # writes nothing, which is what makes `repair_product.py` without --apply
    # still a useful preview.
    def _reconcile() -> str:
        args = [*common]
        if apply:
            args.append("--apply")
        ok, _, payload = run_step(vnyx_api, "verify-and-repair.ts", args,
                                  timeout_s=600, quiet=quiet,
                                  results_name="verify.json")
        if payload is None:
            raise StepFailed(
                "verify-and-repair.ts returned no verdict"
                + ("" if ok else " and exited non-zero"))

        if not payload.get("ok"):
            # `gate_unavailable` is deliberately distinguished from a blocked
            # product: "we could not check" must never read as "it failed".
            outcome = payload.get("outcome") or "failed"
            raise StepFailed(
                f'{outcome}: {payload.get("error") or "no detail"}')

        applied = payload.get("applied") or []
        failed = payload.get("failed") or []
        note = ", ".join(applied) if applied else "nothing to reconcile"
        if failed:
            note += f' (failed: {", ".join(failed)})'
        return note

    step("reconcile", "", _reconcile)

    # ---- 4. render --------------------------------------------------------
    def _render() -> str:
        ok, out, _ = run_step(vnyx_api, "backfill-imagery.ts",
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

    # ---- 5. approve -------------------------------------------------------
    #
    # The CHECK always runs; the MOVE needs --approve. See the module docstring:
    # approving publishes a live Shopify listing and there is no undo.
    verdict: dict[str, Any] = {}

    def _approve() -> str:
        nonlocal verdict
        # Only ever passes --apply when BOTH flags are set. `--apply` alone
        # repairs; publishing has to be asked for separately.
        verdict = approve_check(vnyx_api, dsn, product_id,
                                apply=apply and approve, skip_bin=skip_bin,
                                quiet=quiet)
        outcome = verdict.get("outcome", "?")
        problems = verdict.get("problems") or []
        if outcome == "approved":
            return (f'APPROVED — {verdict.get("stageBefore")} '
                    f'{ARROW} {verdict.get("stageAfter")}, Shopify upsert enqueued')
        if outcome == "would_approve":
            return "ready — pass --approve to move it"
        if outcome == "skipped_preflight":
            return "NOT ready: " + "; ".join(problems)
        return f'{outcome}: {"; ".join(problems)}' if problems else outcome

    step("approve", "", _approve)

    # ---- 6. the verdict that counts ---------------------------------------
    #
    # Recomputed from the STORED record, never from the plan. A step can report
    # success and leave the field unwritten, and a report that trusted its own
    # steps would call the product finished.
    after = needs(dsn, product_id)
    final = product_audit.audit(dsn, product_id, apply=False, write_sheet=False)

    return {
        "product_id": product_id,
        "title": state["title"],
        "tenant": state["tenant"],
        "sku": state["sku"],
        "stage_before": state["stage"],
        "stage_after": after["stage"],
        "review_status": after["review_status"],
        "edit_url": state["edit_url"],
        "applied": apply,
        "before": {k: state[k] for k in
                   ("description_chars", "unmatted", "renders",
                    "attributes_missing")},
        "after": {k: after[k] for k in
                  ("description_chars", "unmatted", "renders",
                   "attributes_missing")},
        "steps": steps,
        "approval": {
            "outcome": verdict.get("outcome"),
            "ready": verdict.get("outcome") in ("approved", "would_approve"),
            "blockers": verdict.get("problems") or [],
            "stage_before": verdict.get("stageBefore"),
            "stage_after": verdict.get("stageAfter"),
        },
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

    ap = r.get("approval") or {}
    if ap.get("outcome") == "approved":
        moved = paint(f'APPROVED {ARROW} {ap.get("stage_after")}', GREEN)
        print(f'    {"approval":14} {moved}'
              f'  {paint("Shopify upsert enqueued", DIM)}')
    elif ap.get("outcome") == "would_approve":
        print(f'    {"approval":14} {paint("READY", GREEN)} '
              f'{paint("— pass --approve to move it", DIM)}')
    elif ap.get("blockers"):
        print(f'    {"approval":14} {paint("not ready", YELLOW)}')
        for b in ap["blockers"]:
            print(f'      {paint("- " + b, DIM)}')
    print(f'    {"took":14} {r["seconds"]}s')


# --------------------------------------------------------------------------- #
# The sheet
# --------------------------------------------------------------------------- #

HDR = "1F3864"
BAD = "FDECEA"
WARN = "FFF8E1"
OK = "E8F5E9"
INFO = "D9E2F3"


def _redact(dsn: str) -> str:
    from scripts.audit_review_products import redact
    return redact(dsn)


def _hms(seconds: float) -> str:
    """`2h 14m` / `14m 03s` / `43s`. Minutes matter at this scale; ms do not."""
    s = int(max(0, seconds))
    if s >= 3600:
        return f'{s // 3600}h {(s % 3600) // 60:02d}m'
    if s >= 60:
        return f'{s // 60}m {s % 60:02d}s'
    return f'{s}s'

SHEET_COLUMNS = [
    ("Approved?", 16),
    ("Why not", 62),
    ("__CHANGED__", 60),
    ("Stage", 20),
    ("Description", 20),
    ("Renders", 12),
    ("Unmatted views", 15),
    ("Attributes filled", 26),
    ("Still missing", 22),
    ("Issues left", 11),
    ("Rules left", 30),
    ("Steps", 44),
    ("Product ID", 38),
    ("SKU", 14),
    ("Title", 40),
    ("Tenant", 18),
    ("Took", 8),
    ("Edit URL", 70),
]


def approval_cell(r: dict[str, Any], applied: bool) -> tuple[str, str, str]:
    """(text, why-not, fill) for the approval columns.

    FOUR states, not two. "No" and "not ready" are different facts and so are
    "ready but you did not ask" and "ready and moved" — collapsing them would
    make a dry run look like a refusal.
    """
    ap = r.get("approval") or {}
    outcome = ap.get("outcome")
    blockers = ap.get("blockers") or []
    joined = "; ".join(blockers)
    if outcome == "approved":
        return (f'YES {ARROW} {ap.get("stage_after") or "APPROVED"}', "", OK)
    if outcome == "would_approve":
        return ("ready — not moved",
                "passed every check; --approve was not passed", INFO)
    if outcome == "skipped_preflight":
        # ALREADY APPROVED is not a failure, and reading it as one is worse than
        # cosmetic: on a batch it inflates the not-ready count with products
        # that are finished, and sends someone looking for a defect that is not
        # there. The pre-flight refuses them only because re-approving records
        # no movement, so onApprovedArrival never fires — the product is done.
        stage_only = [b for b in blockers if b.startswith("stage is ")]
        if len(blockers) == len(stage_only) == 1 and "APPROVED" in stage_only[0]:
            return ("already approved", "", OK)
        return ("no — not ready", joined, BAD)
    if outcome == "not_found":
        return ("no — not found", blockers, BAD)
    if outcome == "failed":
        return ("no — FAILED", blockers, BAD)
    return (outcome or "unknown", blockers, WARN)


def changed_cell(r: dict[str, Any]) -> str:
    """What this run actually added, from the steps that ran.

    The steps' own notes rather than a diff of the record: a diff says a field
    changed, the note says WHICH STEP changed it, and when a description arrives
    and a mannequin moves in the same run that is the difference between a
    readable audit trail and a puzzle.
    """
    parts: list[str] = []
    for s in r.get("steps") or []:
        if not s.get("ran") or not s.get("ok", True):
            continue
        note = str(s.get("note") or "").strip()
        if not note or note in ("nothing to reconcile",):
            continue
        if s["step"] == "approve":
            continue  # its own columns
        parts.append(f'{s["step"]}: {note}')
    return " | ".join(parts) or "nothing"


def write_sheet(results: list[dict[str, Any]], out: Path, *,
                applied: bool, approve: bool, dsn: str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    def fill(colour: str) -> PatternFill:
        return PatternFill("solid", fgColor=colour)

    wb = Workbook()

    # ---- Summary ----------------------------------------------------------
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Product repair"])
    ws.cell(row=1, column=1).font = Font(bold=True, size=14)
    states = [approval_cell(r, applied)[0] for r in results]
    moved = sum(1 for t in states if t.startswith("YES"))
    ready = sum(1 for t in states if t.startswith("ready"))
    already = sum(1 for t in states if t.startswith("already"))
    blocked = len(results) - moved - ready - already
    for label, value in (
        ("Generated", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        # Host and database, never the password — the same redaction the audit
        # sheet uses, reused rather than re-derived so one of them cannot start
        # leaking a credential the other hides.
        ("Database", _redact(dsn)),
        ("Mode", "APPLIED" if applied else "dry run — nothing written"),
        ("Approval", "--approve: ready products were MOVED" if (applied and approve)
         else "readiness reported only; --approve not passed"),
        ("Products", len(results)),
        ("Moved to APPROVED", moved),
        ("Ready, not moved", ready),
        ("Already approved", already),
        ("Not ready", blocked),
    ):
        ws.append([label, value])

    ws.append([])
    ws.append(["Why products are not ready", "Products"])
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = fill(INFO)
    reasons: dict[str, int] = {}
    for r in results:
        if approval_cell(r, applied)[0].startswith("already"):
            continue
        for b in (r.get("approval") or {}).get("blockers") or []:
            reasons[b] = reasons.get(b, 0) + 1
    for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
        ws.append([reason, count])
        ws.cell(row=ws.max_row, column=2).fill = fill(BAD)
    if not reasons:
        ws.append(["— nothing blocking —", 0])
        ws.cell(row=ws.max_row, column=1).fill = fill(OK)

    ws.column_dimensions["A"].width = 58
    ws.column_dimensions["B"].width = 46
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    # ---- Products ---------------------------------------------------------
    ws = wb.create_sheet("Products")
    headers = [("What changed" if applied else "What WOULD change")
               if name == "__CHANGED__" else name
               for name, _ in SHEET_COLUMNS]
    ws.append(headers)
    for cell in ws[1]:
        cell.fill = fill(HDR)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for i, (_, width) in enumerate(SHEET_COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "C2"

    for r in results:
        b, a = r["before"], r["after"]
        text, why, colour = approval_cell(r, applied)
        filled = sorted(set(b["attributes_missing"]) - set(a["attributes_missing"]))
        stage = (f'{r.get("stage_before")} {ARROW} {r.get("stage_after")}'
                 if r.get("stage_before") != r.get("stage_after")
                 else str(r.get("stage_after")))
        ws.append([
            text,
            why,
            changed_cell(r),
            stage,
            (f'{b["description_chars"]} {ARROW} {a["description_chars"]}'
             if b["description_chars"] != a["description_chars"]
             else str(a["description_chars"])),
            (f'{b["renders"]} {ARROW} {a["renders"]}/5'
             if b["renders"] != a["renders"] else f'{a["renders"]}/5'),
            (f'{b["unmatted"]} {ARROW} {a["unmatted"]}'
             if b["unmatted"] != a["unmatted"] else str(a["unmatted"])),
            ", ".join(filled) or "—",
            ", ".join(a["attributes_missing"]) or "—",
            r["issues"],
            ", ".join(dict.fromkeys(r["remaining"])) or "—",
            "; ".join(
                f'{s["step"]}{"" if s.get("ran") else " (skipped)"}'
                for s in r.get("steps") or []),
            r["product_id"], r.get("sku"), r.get("title"), r.get("tenant"),
            f'{r["seconds"]}s', r.get("edit_url"),
        ])
        at = ws.max_row
        ws.cell(row=at, column=1).fill = fill(colour)
        if why:
            ws.cell(row=at, column=2).fill = fill(BAD)
        if changed_cell(r) != "nothing":
            ws.cell(row=at, column=3).fill = fill(OK if applied else WARN)
        for col in range(1, len(SHEET_COLUMNS) + 1):
            ws.cell(row=at, column=col).alignment = Alignment(
                vertical="top", wrap_text=False)
    ws.auto_filter.ref = (
        f"A1:{get_column_letter(len(SHEET_COLUMNS))}{max(ws.max_row, 2)}")

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)
    return out


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
    ap.add_argument(
        "--approve", action="store_true",
        help=("move the product REVIEW -> APPROVED when it passes the "
              "pre-flight. PUBLISHES A LIVE SHOPIFY LISTING and cannot be "
              "undone from here. Readiness is reported either way; this flag "
              "only decides whether the move happens. Needs --apply too."))
    ap.add_argument(
        "--skip-bin", action="store_true",
        help=("skip default-bin placement during approval. It is best-effort "
              "and can sit for 45s a product when many share one LPN."))
    ap.add_argument("--vnyx-api", default=DEFAULT_VNYX_API,
                    help=f"path to the vnyx-api repo. Default {DEFAULT_VNYX_API}")
    ap.add_argument("--out", help="write a JSON summary here.")
    ap.add_argument("--sheet",
                    help=("xlsx path. Defaults to "
                          "reports/generated/repair-<stamp>.xlsx."))
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="hide the sub-scripts' own output.")
    args = ap.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")

    vnyx_api = Path(args.vnyx_api)
    if vnyx_api_url():
        # REMOTE transport: the scripts live on the vnyx-api host and this
        # machine has no checkout to probe. The server's own /ping reports the
        # equivalent, and the worker's preflight calls it.
        print(paint(f"steps run remotely on {vnyx_api_url()}", DIM))
    else:
        # Every script this chain spawns, named individually. Probing only one of
        # them meant a checkout missing verify-and-repair.ts started fine and
        # then failed mid-run at the reconcile step with a bare npx
        # file-not-found.
        required = ("backfill-bg-removal.ts", "relabel-ai-views.ts",
                    "backfill-product-data.ts", "verify-and-repair.ts",
                    "backfill-imagery.ts", "approve-products.ts")
        missing = [s for s in required
                   if not (vnyx_api / "scripts" / s).exists()]
        if missing:
            sys.exit(f"Missing script(s) in {vnyx_api / 'scripts'}: "
                     f"{', '.join(missing)}. Pass --vnyx-api <path>, set "
                     f"VNYX_API_DIR, or set VNYX_API_URL to run them remotely.")

    ids: list[str] = []
    if args.from_sheet:
        from scripts.audit_review_products import read_sheet_ids  # noqa
        ids = read_sheet_ids(Path(args.from_sheet))
    elif args.products:
        ids = [s.strip() for s in args.products.split(",") if s.strip()]
    if not ids:
        sys.exit("Pass --product <uuid[,uuid]> or --from-sheet <xlsx>.")

    # Both numbers, always. `--limit 10` against a 600-row sheet makes the
    # counter read `[3/10]`, and someone who typed 600 into their head needs to
    # see where the 10 came from rather than wondering which is wrong.
    available = len(ids)
    if args.limit:
        ids = ids[:args.limit]
    if args.from_sheet:
        print(paint(f'{available:,} product(s) in {Path(args.from_sheet).name}'
                    + (f' — running the first {len(ids):,} (--limit)'
                       if len(ids) != available else ''), DIM))
    elif len(ids) != available:
        print(paint(f'{available:,} named — running the first {len(ids):,}', DIM))

    if not args.apply:
        print(paint("DRY RUN — nothing is written and no model is called. "
                    "Pass --apply to do it.", YELLOW))
    elif args.approve:
        print(paint(
            "--approve: products passing the pre-flight will be MOVED to "
            "APPROVED, which publishes a live Shopify listing. No undo from "
            "here.", RED))
        print(paint(
            "  The listing only appears once something drains the queue — the "
            "vnyx-api process, running against THIS database.", DIM))

    # ---- progress ---------------------------------------------------------
    #
    # A run is long — 20-80s a product without rendering, ~200s a view with it —
    # so "which of these am I on" is the question an operator actually has an
    # hour in. Width-padded so the titles underneath stay in a column, and
    # carrying the running tallies because a bare position does not say how many
    # of them went through.
    total = len(ids)
    width = len(str(total))
    counts = {"moved": 0, "ready": 0, "already": 0, "blocked": 0, "failed": 0}
    run_started = time.perf_counter()

    def counter(n: int) -> str:
        done = n - 1
        parts = [f'[{str(n).rjust(width)}/{total}]']
        if done:
            tally = " ".join(
                f'{k}={v}' for k, v in counts.items() if v)
            if tally:
                parts.append(tally)
            # Mean of what has actually run, not a constant: the per-product
            # cost swings by an order of magnitude depending on how many steps
            # a product needs, so an estimate from the first one would be
            # useless by the tenth.
            elapsed = time.perf_counter() - run_started
            remaining = (elapsed / done) * (total - done)
            if total - done:
                parts.append(f'~{_hms(remaining)} left')
        return " ".join(parts)

    results = []
    for n, pid in enumerate(ids, start=1):
        try:
            r = repair(dsn, pid, apply=args.apply, vnyx_api=vnyx_api,
                       infer=args.infer, min_confidence=args.min_confidence,
                       skip_render=args.no_render, approve=args.approve,
                       skip_bin=args.skip_bin, quiet=args.quiet,
                       progress=counter(n))
        except product_audit.ProductNotFound:
            print(f'{paint(counter(n), BOLD)}  {paint("no product " + pid, RED)}')
            counts["failed"] += 1
            continue
        except KeyboardInterrupt:
            # Every product commits on its own, so stopping mid-run leaves the
            # finished ones finished. Say so, and still write the sheet for what
            # did run rather than throwing the record away.
            print()
            print(paint(f'Interrupted after {n - 1} of {total}. '
                        f'Completed products are committed.', YELLOW))
            break
        report(r)
        results.append(r)

        state = approval_cell(r, args.apply)[0]
        if state.startswith("YES"):
            counts["moved"] += 1
        elif state.startswith("ready"):
            counts["ready"] += 1
        elif state.startswith("already"):
            counts["already"] += 1
        else:
            counts["blocked"] += 1

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps({"applied": args.apply,
                        "generated": datetime.now(timezone.utc).isoformat(),
                        "results": results}, indent=2, default=str),
            encoding="utf-8")
        print()
        print(paint("  json: ", DIM) + args.out)

    sheet_path = None
    if results and not args.no_sheet:
        sheet_path = Path(args.sheet) if args.sheet else (
            product_audit.REPORT_DIR
            / f'repair-{datetime.now():%Y-%m-%d-%H%M%S}.xlsx')
        write_sheet(results, sheet_path, applied=args.apply,
                    approve=args.approve, dsn=dsn)
        print()
        print(paint("  sheet:", DIM), sheet_path)

    print()
    done = sum(1 for r in results if r["verified"])
    ready = sum(1 for r in results if (r.get("approval") or {}).get("ready"))
    moved = sum(1 for r in results
                if (r.get("approval") or {}).get("outcome") == "approved")
    line = (f'  {len(results)} product(s) {DOT} '
            f'{done} pass every blocking rule {DOT} '
            f'{ready} ready to approve')
    if args.approve and args.apply:
        line += f' {DOT} {moved} moved to APPROVED'
    print(line)
    if ready and not (args.approve and args.apply):
        print(paint('  Add --approve to move the ready ones (publishes to '
                    'Shopify).', DIM))
    return 0


if __name__ == "__main__":
    sys.exit(main())
