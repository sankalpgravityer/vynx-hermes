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

    # OFFLINE. The rule engine over a saved product: no database, no network,
    # nothing written. Save one with --dump-fixture; tests/fixtures/repair/ ships one.
    python scripts/repair_product.py --fixture tests/fixtures/repair/
    python scripts/repair_product.py --db "..." --product <uuid> \
        --dump-fixture reports/fixtures/<sku>.json

Four steps, each SKIPPED when the product does not need it. Nothing here is a new
implementation: every step shells out to the script that already owns that work,
so a render produced by this and one produced by the Model images button are the
same render.

    0  master       app.readiness                    which root the product belongs to
                                                     — the anchor; written by reconcile
    1  matte        scripts/backfill-bg-removal.ts   cut out the garment photos
    2  extract      scripts/backfill-product-data.ts read the care label -> brand,
                                                     size, material, description
    3  reconcile    app.product_audit                drift, taxonomy, sizing guide
    4  render       scripts/backfill-imagery.ts      the five on-model views
    4b gate         app.imaging.quality_gate         one look at the lead render:
                                                     model, face, body, gender, BUILD
    4c photos       app.imaging.photo_audit          the cut-outs and EVERY render:
                                                     wear vs grade, a part of the
                                                     garment the mask ate, render
                                                     defects, one model across the set
    4c rematte      scripts/backfill-bg-removal.ts   re-cut a cut-out the photo audit
                                                     refused (--replace), then look again
    4c regen        scripts/backfill-imagery.ts      re-render what the gate or the
                                                     photo audit refused (--views,
                                                     once), then look again
    4d order        scripts/rebuild-media-cache.ts   the gallery in the catalog order
    4e copy         scripts/regenerate-copy.ts       title + description from the
                                                     settled record, when a field
                                                     they read changed
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
import builtins
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import policy, settings  # noqa: E402
from app.llm import cache as vision_cache  # noqa: E402

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


def _trailing_int(line: str) -> int | None:
    """The number at the end of a `label : 12` summary line, or None."""
    tail = line.rsplit(":", 1)[-1].strip()
    return int(tail) if tail.isdigit() else None


# The words and finish reasons that mean the image model CHOSE not to render
# (nanobanana._REFUSAL_REASONS), as they reach the render script's notes.
_REFUSAL_TOKENS = ("declin", "refus", "image_other", "image_safety",
                   "prohibited", "safety", "blocklist", "recitation")


def _refusal_text(line: str) -> bool:
    """Does a render note say the model declined, rather than failed?"""
    low = (line or "").lower()
    return any(tok in low for tok in _REFUSAL_TOKENS)


# Which of vnyx-api's scripts each step runs. The REMOTE transport names the
# step rather than the script — the server resolves it through its own closed
# map, so nothing this file sends can become a filename over there.
STEP_FOR_SCRIPT = {
    "backfill-bg-removal.ts": "matte",
    "relabel-ai-views.ts": "relabel",
    "backfill-product-data.ts": "extract",
    "verify-and-repair.ts": "reconcile",
    "fix-selling-price.ts": "price",
    "reject-products.ts": "reject",
    "backfill-imagery.ts": "render",
    # Readiness phases 2 and 5: the title/description regeneration and the
    # gallery cache rebuild, both on vnyx-api since deploy ①.
    "regenerate-copy.ts": "copy",
    "rebuild-media-cache.ts": "reorder",
    "approve-products.ts": "approve",
    "resync-listings.ts": "resync",
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


# What the step runner said it accepts, cached for the process. `None` means not
# asked yet; an empty set means asked and it would not say. The sentinel below
# means there was nobody to ask because the steps are spawned locally.
_LOCAL_TRANSPORT = "*local*"
_REMOTE_OPTIONS: set[str] | None = None


def remote_options() -> set[str]:
    """The option names this deployment of vnyx-api accepts.

    `GET /internal/auto-approval/ping` has listed them since readiness phase 2,
    precisely so a Hermes about to send an option the server does not know can
    tell before the 400. Nothing consulted it until now.

    It matters because `--bg-strategies` and `--keep-better` (items 6 and 8 of
    docs/PICTURE-CHECK-FIXES.md) are sent by the chain but land on a vnyx-api
    whose `backfill-bg-removal.ts` does not take them yet. Without this probe the
    first re-matte of every run dies on a 400 halfway through the chain — after
    the master, matte and reconcile steps have already written.

    Failing open is not available here. `--keep-better` is the guard that stops a
    re-matte STORING a cut-out worse than the one it replaces (KIL-001625 lost a
    large part of a shirt back exactly that way), so dropping it silently and
    re-cutting anyway is the one outcome worse than not re-cutting at all. The
    caller's answer is to skip the re-matte and say so.

    Never raises: a ping that fails answers "nothing", which is the same
    conservative branch as an old server.
    """
    global _REMOTE_OPTIONS
    if _REMOTE_OPTIONS is not None:
        return _REMOTE_OPTIONS

    base = vnyx_api_url()
    if not base:
        # LOCAL TRANSPORT: there is no schema to answer 400, because the step is
        # a spawn of the checkout's own script. Nothing can be asked and nothing
        # needs to be — an argument the script does not know is a flag it does
        # not read, not a rejected request. So this reports the sentinel and
        # `remote_supports` lets the call through, leaving the checkout's
        # currency to whoever maintains the checkout.
        _REMOTE_OPTIONS = {_LOCAL_TRANSPORT}
        return _REMOTE_OPTIONS

    import httpx

    try:
        r = httpx.get(
            f"{base}/internal/auto-approval/ping",
            headers={"x-internal-secret": os.getenv("AUTO_APPROVAL_INTERNAL_SECRET", "")},
            timeout=20,
        )
        body = r.json() if r.status_code == 200 else {}
        _REMOTE_OPTIONS = {str(o) for o in (body.get("options") or [])}
    except Exception:  # noqa: BLE001 — a probe that cannot run is not a failure
        _REMOTE_OPTIONS = set()
    return _REMOTE_OPTIONS


def remote_supports(*options: str) -> bool:
    """May the chain send these options?

    VETOES ONLY ON POSITIVE EVIDENCE. False is returned in exactly one case: a
    server answered the ping, listed the options it takes, and one of these was
    not on the list. That is the case that would 400 mid-chain, and it is the
    only one worth stopping for.

    Everything else is a yes — the local transport, where there is no request to
    reject; and a ping that did not answer, because a server that cannot be
    reached will not run the step either, and inventing a skip for it would
    replace a clear connection error with a misleading "left the cut-outs
    alone". A guess in either direction is wrong here, so this only acts on what
    it was actually told.
    """
    known = remote_options()
    if not known or _LOCAL_TRANSPORT in known:
        return True
    return all(o in known for o in options)


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
    if "--provider" in args:
        options["bgProvider"] = args[args.index("--provider") + 1]
    # Re-matte a view that already has a cut-out (readiness phase 3). The
    # option exists on vnyx-api since deploy ① (phase 2); before that deploy
    # the strict body schema answers 400 and the step fails loudly, which is
    # the intended order of operations.
    if "--replace" in args:
        options["replace"] = True
    # WHICH SEGMENTERS THE RE-MATTE MAY ASK (item 6 of
    # docs/PICTURE-CHECK-FIXES.md). Sent as a list of the names
    # app/imaging/cutout.py defines (`STRATEGY_NAMES`) and validated against a
    # closed enum on the server, like `views` below — the rule that nothing
    # from a request becomes a free-form argv value holds here too. vnyx-api
    # forwards them to /v1/imagery/remove-background as `strategies`.
    if "--bg-strategies" in args:
        options["bgStrategies"] = [
            s.strip()
            for s in args[args.index("--bg-strategies") + 1].split(",")
            if s.strip()
        ]
    # DO NOT REPLACE A CUT-OUT WITH A WORSE ONE (item 8). The script sends the
    # cut-out being superseded to Hermes as `previous_url`, and writes nothing
    # when Hermes answers `kept_existing`. Both options need the vnyx-api
    # deploy that carries them; before it the strict body schema answers 400
    # and the step fails loudly, which is the same order of operations
    # `--replace` itself went through and is preferable to a silent no-op that
    # would let the KIL-001625 damage through again.
    if "--keep-better" in args:
        options["keepBetter"] = True
    # `--views` MEANS TWO DIFFERENT THINGS, and the script decides which.
    #
    # On backfill-imagery.ts it is the on-model views to (re)generate —
    # AI_FRONT and friends (readiness phase 4). On backfill-bg-removal.ts it is
    # the GARMENT views to re-cut, FRONT and BACK. Both are closed enums on the
    # server and they do not overlap, so sending one under the other's key is a
    # 400: "Invalid enum value. Expected 'AI_FRONT' | ... , received 'FRONT'".
    #
    # Keyed off the script rather than off the values, because guessing from
    # the content would quietly pick a key for an unknown view instead of
    # failing where the mistake is.
    if "--views" in args:
        picked = [
            v.strip().upper()
            for v in args[args.index("--views") + 1].split(",")
            if v.strip()
        ]
        key = "matteViews" if script == "backfill-bg-removal.ts" else "views"
        options[key] = picked
    # The copy step's two halves (readiness phase 5). Neither flag means both.
    if "--title" in args:
        options["title"] = True
    if "--description" in args:
        options["description"] = True
    # reorder: rebuild a gallery flagged mediaManualOrder too (policy says the
    # flag is not to be trusted — readiness.gallery.respect_manual).
    if "--include-manual" in args:
        options["includeManual"] = True
    if "--allow-stage" in args:
        options["allowStage"] = [
            s.strip().upper()
            for s in args[args.index("--allow-stage") + 1].split(",")
            if s.strip()
        ]
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
    return needs_from(product_audit.load(dsn, product_id))


def needs_from(loaded: dict[str, Any]) -> dict[str, Any]:
    """`needs()` on a product already loaded — from the database or a fixture.

    Split out so `--fixture` judges a file with exactly the predicates the live
    chain uses; a second copy of "is this view matted" would drift.
    """
    record, media = loaded["record"], loaded["media"]

    # ON the product. The loader also carries the superseded RAW originals a
    # cut-out was made from (readiness phase 3, `isCurrent: false`); counting
    # them here would report every matted view as still having a raw beside it.
    live = [m for m in media if m["mediaType"] == "IMAGE"
            and m.get("isCurrent", True) and not m.get("deletedAt")]
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
    # The views whose cut-out EXISTS and can therefore be judged (readiness
    # phase 3): on its canvas, on the tenant's backdrop. FRONT/BACK only — the
    # views generation seeds from and the matte step pays for.
    cutout_views = sorted({m["view"] for m in garments
                           if m["view"] in ("FRONT", "BACK") and m["processing"] != "RAW"})
    # Garment PHOTOGRAPHS on file — what a render is seeded from (readiness
    # phase 4). A size chart filed under OTHER is not one (imagery.is_size_chart).
    markers = [str(k).lower() for k in
               ((policy().get("imagery") or {}).get("size_chart_url_markers") or [])]
    garment_photos = [m for m in garments
                      if not any(k in str(m.get("url") or "").lower() for k in markers)]

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
        "cutout_views": cutout_views,
        "garment_photos": len(garment_photos),
        "master": record.get("masterCategory"),
        "mannequin": record.get("mannequinType"),
        "size": record.get("size") or record.get("internationalSize"),
        "renders": len(renders),
        "render_rows": len(render_rows),
        "renders_missing": 5 - len(renders),
        # Read before and after the chain by the canary in repair(): a flip to
        # GENERATING that the render step did not cause means a repair write is
        # triggering paid regeneration.
        "generation_status": record.get("generationStatus"),
        "is_regenerating": bool(record.get("isRegenerating")),
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


def _generation_in_flight(state: dict[str, Any]) -> bool:
    """Is a generation genuinely running for this product right now?

    GENERATING / isRegenerating, AND touched within `imagery.stale_generation_hours`
    (the same threshold IMG.011 and vnyx-api's generation reaper use). Older than
    that the flag is a stranded row and must not block a repair forever.
    """
    if state.get("generation_status") != "GENERATING" and not state.get("is_regenerating"):
        return False
    stamp = state["loaded"]["record"].get("updatedAt")
    if not stamp:
        return True
    try:
        touched = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if touched.tzinfo is None:
            touched = touched.replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    hours = float((policy().get("imagery") or {}).get("stale_generation_hours") or 1)
    age_h = (datetime.now(timezone.utc) - touched).total_seconds() / 3600
    return age_h < hours


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #

# ATTRIBUTES THAT DO NOT JUSTIFY A VISION CALL ON THEIR OWN.
#
# `material` is the case this exists for. It is missing on most of the catalogue,
# and the bulk extractor only recovers it when the fibre composition happens to be
# printed legibly on the tag — the three BOAS products checked most recently were
# all "missing material" with two care labels each. A seventeen-field pass costs
# 60-250 seconds and a paid vision call, and usually returns "Unknown" anyway,
# which the placeholder list then reads as still empty. Material is not a
# condition of approval either (`rules.severity_overrides` in policy.yaml), so
# the call buys nothing on any axis.
#
# Brand and size are NOT in here and should not be: they are on every tag, and
# the narrow care-label pass reads them cheaply and better.
#
# `--infer` overrides it. That flag means "look at the garment, not just the
# label", which is the one way material is sometimes recoverable, so asking for
# it explicitly is asking for this too.
#
# MODULE LEVEL so `needs()` (live) and `needs_from()` (fixture) read the SAME
# set. While this lived inside the chain runner the offline report promised an
# `extract` step on a product whose only gap was material — a step the live run
# then skipped, so the fixture and the chain disagreed about the same record.
NOT_WORTH_EXTRACT = frozenset({"material"})

# The rules that say "this record does not describe this garment".
#
# NAMED ONE BY ONE, not matched on a `TAX.` prefix. The first version of this did
# use the prefix and swept in TAX.005 and TAX.007, which are about the MANNEQUIN
# RIG — a rendering choice the title never mentions. A product with no rig
# configured would then have had its copy withheld forever, which is the opposite
# of the point: this guard exists to stop ONE specific failure, not to stop the
# copy step working.
#
#   TAX.001-004   the master category, category or sub-category is not one the
#                 tenant's tree holds, or they disagree with the gender
#   GENDER.001    the gender itself is inconsistent
#   SIZE.011      the sizing guide belongs to the other gender
#   DRIFT.001     the column and its `properties` twin disagree about one of them
#
# These are exactly the fields the generated title is written from. A price, a
# care label or a missing rig says nothing about whether the title describes the
# garment, so none of them withholds the copy.
_COPY_BLOCKING_RULES = frozenset({
    "TAX.001", "TAX.002", "TAX.003", "TAX.004",
    "GENDER.001", "SIZE.011", "DRIFT.001",
})


def _blocking_taxonomy(snap: Any) -> list[Any]:
    """Findings that mean the record misdescribes the garment.

    NO SEVERITY FLOOR, and that is deliberate. The first version required HIGH,
    borrowing the approval gate's own bar, and it let TAX.003 through — an
    invalid SUB-category, which the rules rank MEDIUM because it needs a
    reviewer's eye rather than stopping the product. But the title is generated
    from the sub-category, so a MEDIUM TAX.003 still produces a title naming a
    garment type this tenant's catalogue does not have.

    Severity answers "should this stop an approval". The question here is
    different and narrower: "does the record still misdescribe the garment". The
    rule list above already answers it, so ranking it a second time only
    reintroduces the gap.

    Run against the STORED record — a step can report success and leave the field
    unwritten, so a check that trusted the plan would clear a product whose
    repair never landed.
    """
    from app.rules import run_all
    from app.rules import gate as gate_rules

    pol = policy()
    return [f for f in run_all(snap, pol) + gate_rules.check_gate(snap, pol)
            if str(f.rule_id) in _COPY_BLOCKING_RULES]

# --------------------------------------------------------------------------- #
# Offline: fixtures
# --------------------------------------------------------------------------- #
FIXTURE_KEYS = ("record", "media", "catalog", "grade_ladder", "imagery_settings", "chart")


def dump_fixture(dsn: str, product_id: str, path: Path) -> Path:
    """Write one product exactly as `product_audit.load()` sees it.

    The file is the rule engine's whole input — the record, the typed media
    rows, the tenant's option lists, grade ladder, imagery settings and chart
    facts — so `--fixture` later judges the product the database held at this
    moment, with no database. It is REAL TENANT DATA (SKU, title, URLs, every
    attribute): keep it under reports/ unless it has been trimmed for a test,
    the way tests/fixtures/repair/ was.
    """
    loaded = product_audit.load(dsn, product_id)
    doc: dict[str, Any] = {k: loaded.get(k) for k in FIXTURE_KEYS}
    doc["_fixture"] = {
        "source": "scripts/repair_product.py --dump-fixture",
        "sku": (loaded["record"] or {}).get("sku"),
        "product_id": product_id,
        "database": _redact(dsn),
        "dumped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=1, default=str, ensure_ascii=False),
                    encoding="utf-8")
    return path


def run_fixture(path: Path, *,
                severity_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    """The rule engine over one fixture file. No database, no network, no writes.

    WHAT RUNS: `needs_from` (which chain steps the product would get) and
    `approval.run_gate` — every rule, every planner, the price assessment —
    exactly as the live chain's reconcile step runs them, minus the evidence
    layer: no model is called, so a finding that wanted a photograph looked at
    stays a finding. WHAT DOES NOT: the sub-scripts (matte, relabel, extract,
    render, approve) live in vnyx-api and need its database; the image gate
    needs the model. This is the auditor's `--fixture` self-test — does the
    engine run end to end, and what does it say about a product whose answer is
    known — not a rehearsal of a repair.
    """
    from app import approval, twins

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("record"), dict):
        raise ValueError(f"{path}: not a fixture — no 'record'. Write one with --dump-fixture.")
    loaded: dict[str, Any] = {k: data.get(k) for k in FIXTURE_KEYS}
    loaded["media"] = loaded["media"] or []
    state = needs_from(loaded)
    gate = approval.run_gate({**loaded["record"], "media": loaded["media"]},
                             catalog=loaded["catalog"],
                             imagery_settings=loaded["imagery_settings"],
                             llm=None, severity_overrides=severity_overrides)

    # The chain's own predicates, so the fixture's answer and a live dry run
    # cannot disagree about the shape of the work. Relabel is left out: its
    # test is the sub-script's, run against the media rows on the host.
    would: list[str] = []
    # The anchor: listed when the master would move or cannot be settled, the
    # same test the chain's `master` step prints.
    from app import readiness
    from app.vnyx_client import to_snapshot

    master_decision = readiness.decide_master(
        to_snapshot({**loaded["record"], "media": loaded["media"]},
                    catalog=loaded["catalog"],
                    imagery_settings=loaded["imagery_settings"]),
        policy())
    if master_decision["action"] in ("set", "unresolved"):
        would.append("master")
    if twins.parent_sku(state["sku"], policy()):
        would.append("twin")
    # A missing cut-out, or one the stored dimensions already show on the wrong
    # canvas (IMG.026 — the fixture carries no pixels, so IMG.027 cannot fire
    # here). The live chain measures; the fixture reads what is on the rows.
    if state["unmatted"] or any(rid in ("IMG.026", "IMG.027") for rid in gate["findings"]):
        would.append("matte")
    # Same test the live chain makes — a gap the extractor is not worth calling
    # for (material) must not appear here as a step that would run.
    if state["description_missing"] or (
        set(state["attributes_missing"]) - NOT_WORTH_EXTRACT
    ):
        would.append("extract")
    if state["care_label"] and any(a in ("brand", "size") for a in state["attributes_missing"]):
        would.append("care label")
    if gate["repair_plan"]:
        would.append("reconcile")
    if state["renders_missing"]:
        would.append("render")

    return {
        "fixture": str(path),
        "sku": state["sku"], "title": state["title"], "tenant": state["tenant"],
        "stage": state["stage"],
        "verified": bool(gate["verified"]),
        "blocking": gate["blocking"],
        "advisory": gate["advisory"],
        "findings": gate["findings"],
        "repair_plan": gate["repair_plan"],
        "price": gate.get("price"),
        "master": master_decision,
        "would_run": would,
        "state": {k: state[k] for k in ("description_chars", "care_label", "unmatted",
                                        "renders", "attributes_missing")},
        "dumped": data.get("_fixture") or {},
    }


def _fixture_paths(args: list[str]) -> list[Path]:
    """Files as given; a directory means every *.json in it, sorted."""
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out.extend(sorted(p.glob("*.json")))
        else:
            out.append(p)
    return out


def run_fixtures(paths: list[Path], *, out: str | None = None) -> int:
    """`--fixture`: every file, one report each. Exit 0 when all of them ran —
    a blocked product is a result, not a failure of the self-test."""
    print(paint("OFFLINE — fixtures only. No database, no network, nothing written; "
                "the rules and planners run, the model and the sub-scripts do not.",
                YELLOW))
    if not paths:
        print(paint("  no fixture files found", RED))
        return 1
    results: list[dict[str, Any]] = []
    failed = 0
    width = len(str(len(paths)))
    for n, path in enumerate(paths, start=1):
        head = f'[{str(n).rjust(width)}/{len(paths)}]'
        try:
            r = run_fixture(path)
        except Exception as exc:  # noqa: BLE001 — report it and run the rest
            failed += 1
            print(f'{paint(head, BOLD)}  '
                  f'{paint(f"{path.name}: {type(exc).__name__}: {exc}", RED)}')
            continue
        results.append(r)
        st = r["state"]
        print(f'{paint(head, BOLD)}  {path.name} {DOT} {r["sku"]} {DOT} '
              f'{(r["title"] or "")[:60]}')
        print(paint(f'        {r["tenant"]} {DOT} {r["stage"]} {DOT} '
                    f'{st["description_chars"]} chars {DOT} care label {st["care_label"]} '
                    f'{DOT} {st["unmatted"]} unmatted {DOT} {st["renders"]}/5 renders {DOT} '
                    f'missing {", ".join(st["attributes_missing"]) or "nothing"}', DIM))
        verdict = (paint("verified", GREEN) if r["verified"]
                   else paint(f'{len(r["blocking"])} blocking', RED))
        print(f'        {verdict} {DOT} {len(r["advisory"])} advisory {DOT} '
              f'{len(r["repair_plan"])} planned action(s)')
        for f in r["blocking"]:
            print(f'          - {f["rule_id"]} [{f["severity"]}] {f["message"]}')
        for f in r["advisory"]:
            print(paint(f'          · {f["rule_id"]} [{f["severity"]}] {f["message"]}', DIM))
        for a in r["repair_plan"]:
            target = a.get("field") or a.get("view") or ""
            value = a.get("value")
            shown = f' = {str(value)[:60]}' if value is not None else ""
            print(paint(f'          plan {a.get("kind")} {target}{shown} ({a.get("reason")})', DIM))
        if r["would_run"]:
            print(paint(f'        chain would run: {", ".join(r["would_run"])}', DIM))

    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(
            json.dumps({"generated": datetime.now(timezone.utc).isoformat(),
                        "fixtures": results}, indent=2, default=str),
            encoding="utf-8")
        print()
        print(paint("  json: ", DIM) + out)

    ok = sum(1 for r in results if r["verified"])
    print()
    print(f'  {len(results)} fixture(s) ran {DOT} {ok} verified {DOT} '
          f'{len(results) - ok} blocked'
          + (f' {DOT} {paint(f"{failed} could not run", RED)}' if failed else ''))
    return 1 if failed else 0


def approve_check(vnyx_api: Path, dsn: str, product_id: str, *,
                  apply: bool, skip_bin: bool,
                  quiet: bool,
                  allow_stage: list[str] | None = None) -> dict[str, Any]:
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
    if allow_stage:
        # Relaxes ONLY the stage clause of the pre-flight; every required-field
        # check still applies. See approve-products.ts for why that is not
        # --force.
        args.extend(["--allow-stage", ",".join(allow_stage)])
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
           quiet: bool, progress: str = "",
           severity_overrides: dict[str, str] | None = None,
           allow_stage: list[str] | None = None,
           silent: bool = False,
           ) -> dict[str, Any]:
    # SILENT SHADOWS THE BUILTIN, deliberately and only inside this function.
    #
    # repair() narrates itself across ~20 print() calls, which is exactly right
    # for one product in a terminal and unreadable for several at once: run
    # --workers 2 and two products interleave line by line, so one product
    # appears to run `reconcile` twice and `matte` after `approve`. Threading a
    # flag through every call site would be noise; assigning the name here makes
    # it local to repair() and to the closures inside it, so `step()` and the
    # rest pick it up with no edit. The caller gets the same data from the
    # returned dict, which is what run_from_sheet prints atomically instead.
    print = (lambda *a, **k: None) if silent else builtins.print  # noqa: A001

    started = time.perf_counter()
    state = needs(dsn, product_id)
    steps: list[dict[str, Any]] = []

    # Steps whose VISION READ failed at the provider — an API error, a timeout,
    # a rejected key — rather than returning an answer. Reported to the caller
    # because the difference decides a verdict: "the label could not be read"
    # and "the label reader was down" must not both become "no size". The
    # runner retries a product on this list instead of judging it.
    vision_unavailable: list[str] = []

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
            # The in-process steps (twin, care label, gate) have no subprocess
            # output to show, so their one-line result is printed here — the
            # same line the run log records as the step's note. Subprocess
            # steps get their summary echoed too, which costs one dim line.
            if note:
                print(f'        {paint(note, DIM)}')
        except StepFailed as exc:
            note, ok = str(exc), False
            print(f'        {paint("FAILED: " + note, RED)}')
        steps.append({"step": name, "ran": True, "ok": ok, "note": note,
                      "seconds": round(time.perf_counter() - t0, 1)})

    common = ["--db", dsn, "--product", product_id]
    live = ["--apply"] if apply else []

    # ---- 0. twin ----------------------------------------------------------
    #
    # BEFORE EVERYTHING, because it changes what "missing" means for every step
    # that follows. A C-twin is the same garment as its parent listed under the
    # other gender; duplication copied the pictures and not the measurements, so
    # the twin arrives with blank brand / size and the default rules would
    # reject it for facts its parent already carries. Gender-neutral fields only
    # — see app/twins.py for the two lists and why.
    #
    # Through apply_plan like the care-label pass: one transaction, guarded on
    # updatedAt, both copies of a doubled value written.
    twin_note = ""

    def _twin() -> str:
        nonlocal state, twin_note
        from app import twins

        record = state["loaded"]["record"]
        parent_sku = twins.parent_sku(record.get("sku"), policy())
        parent = twins.load_parent(dsn, parent_sku or "", str(record["tenantId"]))
        if parent is None:
            return f"parent {parent_sku} is not in this tenant — nothing to inherit"
        plan = twins.inheritance_plan(record, parent, policy())
        if not plan:
            return f"parent {parent_sku} holds nothing the twin lacks"
        summary = ", ".join(f'{a["field"]}={a["value"]!r}' for a in plan)
        if apply:
            outcome = product_audit.apply_plan(
                dsn, product_id, plan, record.get("updatedAt"))
            if outcome["conflict"]:
                raise StepFailed(outcome.get("message", "changed mid-repair"))
            if outcome["skipped"]:
                raise StepFailed("; ".join(
                    s.get("why", "?") for s in outcome["skipped"]))
            # Re-read: the extract / care-label decisions below key off what is
            # still missing, and that just changed.
            state = needs(dsn, product_id)
        twin_note = f'from {parent_sku}: {summary}'
        return f'{"inherited" if apply else "would inherit"} {summary} from {parent_sku}'

    from app import twins as _twins

    is_twin = _twins.parent_sku(state["sku"], policy()) is not None
    step("twin", "" if is_twin else "not a C-twin", _twin)

    # ---- 0b. master -------------------------------------------------------
    #
    # THE ANCHOR, said out loud at the head of the run (readiness phase 1,
    # docs/READINESS-PLAN.md §4 step 1). Every field the approval hangs off is
    # settled from the master category — the gender follows it, the branch must
    # sit under it, the guide is chosen for its gender, the rig is derived from
    # it — so which root this product belongs to is decided first and printed,
    # before anything is paid for.
    #
    # REPORT-ONLY HERE. The same decision (`readiness.decide_master`) is what
    # `approval._plan_master` makes inside run_gate, and the reconcile step
    # executes that plan through vnyx-api's updateProduct — the one writer that
    # owns `categoryId` and the taxonomy columns. `apply_plan` refuses
    # masterCategory by design (see product_audit._WRITABLE_COLUMNS), so this
    # step never writes; it says what reconcile is about to do, or that no root
    # fits and the product will hold as MASTER_CATEGORY_UNRESOLVED.
    master_decision: dict[str, Any] = {}

    def _master() -> str:
        nonlocal master_decision
        from app import readiness
        from app.vnyx_client import to_snapshot

        loaded = state["loaded"]
        snap = to_snapshot({**loaded["record"], "media": loaded["media"]},
                           catalog=loaded.get("catalog"),
                           imagery_settings=loaded.get("imagery_settings"))
        master_decision = readiness.decide_master(snap, policy())
        action = master_decision["action"]
        if action == "keep":
            return f'{master_decision["master"]!r} is a tenant root — the anchor stands'
        if action == "set":
            return (f'{loaded["record"].get("masterCategory")!r} {ARROW} '
                    f'{master_decision["master"]!r} ({master_decision["basis"]}: '
                    f'{master_decision["detail"]}); reconcile writes it')
        if action == "unresolved":
            return (f'UNRESOLVED — {master_decision["detail"]}; the product holds '
                    f'as MASTER_CATEGORY_UNRESOLVED')
        return master_decision["detail"]

    from app import readiness as _readiness

    master_why = ("" if _readiness.enabled(policy())
                  else "disabled in policy (readiness.enabled)")
    step("master", master_why, _master)

    # ---- 1. matte ---------------------------------------------------------
    #
    # TWO QUESTIONS NOW, not one (readiness phase 3, docs/READINESS-PLAN.md §4
    # step 3). "Does every garment view have a cut-out?" — as before, from the
    # rows. And "is each cut-out RIGHT?" — on its photograph's own canvas and
    # on the tenant's backdrop — from the pixels, because 91% of the cut-outs
    # with stored dimensions sit zoomed on a different canvas than their
    # original and nothing in a row says so. A wrong cut-out is re-cut from
    # the raw archive through `--replace` (phase 2: Hermes segmenter, source
    # canvas, tenant backdrop) and measured again; one still wrong after that
    # is CUTOUT_UNFIXABLE — a flag while `readiness.cutouts.hold` is soft, a
    # hold once it is block. Enforced by _approve exactly like the gate. See
    # app/imaging/cutouts.py.
    cutout_before: Any = None     # measured before the segmenter ran
    cutout_verdict: Any = None    # what _approve enforces
    # Whether this run's matte step re-cut the product's cut-outs (or would,
    # in a dry run) — the rematte step below asks before cutting them again.
    matte_state: dict[str, Any] = {"replaced": False}

    def _cutouts_now() -> Any:
        """Measure the live cut-outs against their originals and the backdrop."""
        from app.imaging import cutouts
        from app.vnyx_client import to_snapshot

        fresh = needs(dsn, product_id)["loaded"]
        snap = to_snapshot({**fresh["record"], "media": fresh["media"]},
                           catalog=fresh.get("catalog"),
                           imagery_settings=fresh.get("imagery_settings"))
        return cutouts.judge(snap, policy())

    def _matte() -> str:
        nonlocal cutout_before, cutout_verdict
        from app.imaging import cutouts as _cut

        # FORCE THE SEGMENTER HERE, not in vnyx-api's environment.
        #
        # BG_REMOVAL_RETIRED would do it globally, but that re-points every
        # caller — the web upload, the analyze worker, Sync now — for all twenty
        # tenants, as a side effect of deploying a file. This chain is the only
        # caller that needs the Hermes cut-out, so it asks per call.
        #
        # It is also the only thing that can win: resolveBgRemovalProvider reads
        # the PRODUCT's own imageSettings snapshot before the tenant column, and
        # 14,696 products carry a baked-in `vnyx-gemini` pointing at a host that
        # sits behind a 100s proxy timeout and returns a 524 error PAGE.
        #
        # Settable per deployment, and emptying it restores the old behaviour.
        provider = os.getenv("AUTO_APPROVAL_BG_PROVIDER", "hermes").strip()
        force = ["--provider", provider] if provider else []

        # WHAT IS WRONG WITH THE CUT-OUTS THAT EXIST, measured before anything
        # runs. Two downloads and two header reads at most; no model.
        replace: list[str] = []
        parts: list[str] = []
        if cutouts_on and state.get("cutout_views"):
            cutout_before = _cutouts_now()
            # Stands as the verdict until the re-check replaces it, so a
            # re-matte that FAILS leaves the measured defect on record rather
            # than nothing.
            cutout_verdict = cutout_before
            if cutout_before.action == "bad":
                replace = list(cutout_before.bad_views)
                fixable = [r for r in cutout_before.reasons
                           if r not in set(getattr(cutout_before, "unfixable_reasons", []))]
                if replace:
                    parts.append(f're-matte {", ".join(replace)} ({"; ".join(fixable)})')
                # A flaw no re-cut can clear (the form's neck where the inside
                # of the collar should be): said, never re-matted for.
                if getattr(cutout_before, "unfixable_views", None):
                    parts.append("not fixable by a re-cut — "
                                 + "; ".join(cutout_before.unfixable_reasons))
            elif cutout_before.action == "unknown":
                parts.append("cut-outs could not be measured: "
                             + "; ".join(cutout_before.reasons))
        if state["unmatted"]:
            parts.append("matte " + ", ".join(state["unmatted_views"]))

        if not state["unmatted"] and not replace:
            # Nothing for the segmenter. The measurement is the step's result.
            return (cutout_before.summary() if cutout_before is not None
                    else "every garment view already has a cut-out")

        if not apply and replace:
            # A DRY RUN NEVER HOLDS ON WHAT IT WOULD FIX. The re-matte would
            # run and the re-check would follow; neither can here, so the
            # verdict says so instead of carrying the pre-repair defect into
            # the approval decision.
            cutout_verdict = _cut.CutoutVerdict(
                "pending", cutout_before.hold, reasons=list(cutout_before.reasons),
                bad_views=list(cutout_before.bad_views), checks=cutout_before.checks,
                expected=cutout_before.expected)

        args = [*common, *live, *force]
        if replace:
            # `--keep-better` beside `--replace`, always (item 8 of
            # docs/PICTURE-CHECK-FIXES.md). `replaceWithDerived` supersedes the
            # old row and cannot be undone, so "is the new one at least as
            # complete" has to be asked BEFORE the swap; Hermes answers it from
            # the two cut-outs' garments, each segmented against its own flat
            # backdrop, and writes nothing when the answer is no.
            #
            # A server that cannot take the guard does not get the replace. Not
            # the other way round: re-cutting without it is how KIL-001625 lost a
            # large part of its shirt back, and a silent downgrade to the unsafe
            # call is worse than leaving the existing cut-out alone.
            if not remote_supports("keepBetter"):
                matte_state["replace_skipped"] = "vnyx-api does not accept keepBetter"
                # NAME THE DEFECT IN THE REFUSAL. Without this the note said only
                # that the re-cut was declined, so a reader learned the chain
                # could not act and never learned WHAT it had found — the
                # measurement was on the row in the JSON and nowhere a person
                # would look. A refusal that hides its own finding reads as the
                # check having found nothing.
                return ("left the existing cut-outs alone — this vnyx-api cannot take "
                        "--keep-better, and re-cutting without it can store a cut-out "
                        "worse than the one it replaces. STILL WRONG, unrepaired: "
                        + "; ".join(fixable or cutout_before.reasons))
            args.extend(["--replace", "--keep-better"])
            # ONLY THE VIEWS THAT ARE WRONG. `--replace` on its own redoes every
            # covered view, so a product with one defective cut-out paid for two
            # segmenter calls and had a sound picture re-cut for no reason — and
            # the note above already said "re-matte FRONT" while both were being
            # redone, which made the log a poor guide to what had happened.
            #
            # Gated like `keepBetter`: a deployment that cannot take the option
            # keeps the old whole-product behaviour rather than losing the
            # re-matte over it. The guard is what may never be dropped; this is
            # an economy.
            if remote_supports("matteViews"):
                args.extend(["--views", ",".join(replace)])
            matte_state["replaced"] = True
        ok, out, _ = run_step(vnyx_api, "backfill-bg-removal.ts", args,
                              timeout_s=600, quiet=quiet)
        if not ok:
            raise StepFailed("background removal returned non-zero")

        # EXIT CODE 0 IS NOT ENOUGH HERE, and trusting it hid a real outage.
        #
        # backfill-bg-removal.ts processes each image independently and exits 0
        # whether or not any cut-out was produced -- correct for a bulk backfill,
        # where one bad image should not abandon the other four hundred. Read as
        # a STEP result it is wrong: the chain printed a green `run matte`, the
        # product kept its RAW images, and IMG.010 then blocked approval with
        # nothing in the log to connect the two.
        #
        # What it was hiding: the segmenter at vnyxremoveapi.vnyx.ai sits behind
        # Cloudflare, whose origin timeout is 100s and cannot be raised below
        # Enterprise. Two images took 240s and both came back as a 524 error
        # PAGE -- HTML with a 200-shaped body, which is why nothing downstream
        # noticed.
        #
        # So the summary the script already prints is parsed, and a step that
        # wrote nothing while failing something says so.
        written = failed = kept = None
        for line in (out or "").splitlines():
            low = line.lower()
            if "cut-outs written" in low:
                written = _trailing_int(line)
            elif "kept" in low and ":" in line:
                kept = _trailing_int(line)
            elif "failed" in low and ":" in line:
                failed = _trailing_int(line)
        if failed and not written:
            raise StepFailed(
                f"background removal produced no cut-outs ({failed} image(s) "
                f"failed). The segmenter is unreachable or timing out — see the "
                f"vnyx-api log for the provider's response."
            )
        note = ("would " if not apply else "") + "; ".join(parts)
        if failed:
            note += f' ({written} written, {failed} FAILED)'
        if kept:
            # NOT A FAILURE, AND IT MUST NOT READ AS ONE (item 8). Every
            # candidate was a real cut-out; each had less garment than the one
            # already on file, so nothing was replaced and the picture the
            # catalog shows is unchanged. Said out loud because the alternative
            # — a defect on record with no explanation — is what queues the
            # product to be sent through the identical re-matte next run.
            note += (f' ({kept} view(s): the re-matte lost part of the garment, so the '
                     f'ORIGINAL CUT-OUT WAS KEPT — re-cutting it again will do the same)')

        # THE RE-CHECK. Every fix is followed by the check that asked for it
        # (§4, principles): the cut-outs are measured again from the rows the
        # step just wrote. Still wrong is a verdict, not a retry.
        if apply and cutouts_on and (replace or state["unmatted"]):
            cutout_verdict = _cutouts_now()
            if cutout_verdict.action == "bad":
                note += (" — STILL WRONG after the re-matte: "
                         + "; ".join(cutout_verdict.reasons)
                         + (" (holds as CUTOUT_UNFIXABLE)" if cutout_verdict.blocks
                            else " (soft — readiness.cutouts.hold)"))
            elif cutout_verdict.action == "ok":
                note += " — re-checked: on canvas and on the tenant's backdrop"
            elif cutout_verdict.action == "unknown":
                note += " — re-check could not measure the new cut-outs"
        return note

    from app.imaging import cutouts as _cutouts_mod

    cutouts_on = _cutouts_mod.enabled(policy())
    matte_why = ""
    if state["unmatted"]:
        matte_why = ""
    elif cutouts_on and state.get("cutout_views"):
        # Every view has a cut-out; whether each is RIGHT is the step's job now.
        matte_why = ""
    elif cutouts_on:
        matte_why = "no garment photograph to cut out or to check"
    else:
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

    # See NOT_WORTH_EXTRACT at module level for why material is in it, and why
    # this set has to be the same one `needs_from()` reads.
    missing = set(state["attributes_missing"])
    worth_extracting = missing if infer else missing - NOT_WORTH_EXTRACT

    want_extract = state["description_missing"] or worth_extracting
    # Everything still missing that ONLY the care-label pass below can do better.
    narrow_only = (
        state["care_label"]
        and not state["description_missing"]
        and worth_extracting <= {"brand", "size"}
    )
    if not state["care_label"] and not infer:
        extract_why = ("no care label to read — attributes are never inferred "
                       "from the garment (pass --infer to override)")
    elif not want_extract and missing:
        # Something IS missing; it is just not worth the call.
        extract_why = (f'only {", ".join(sorted(missing))} missing — not worth a '
                       f'vision pass (use --infer to try anyway)')
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
        # THE READER WAS DOWN, not the label illegible. Every provider that
        # could be asked failed at the API, so nothing about brand or size is
        # known — and a product must not be rejected on a silence. Marked for
        # the runner, then recorded as a failed step like any other.
        if label_read.get("api_failed"):
            vision_unavailable.append("care label")
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
        if not apply:
            # A DRY RUN SAYS WHAT IT WOULD WRITE (readiness phase 1). The script
            # applies nothing without --apply and its results carry only what
            # was applied, so a shadow run used to read "nothing to reconcile"
            # for a product with a five-field cascade planned. The plan is
            # recomputed here with the same run_gate the service just ran, on
            # the record as loaded, and listed — that is how the cascade is
            # hand-checked before it is allowed to write.
            from app import approval

            loaded = state["loaded"]
            gate = approval.run_gate({**loaded["record"], "media": loaded["media"]},
                                     catalog=loaded.get("catalog"),
                                     imagery_settings=loaded.get("imagery_settings"),
                                     llm=None, severity_overrides=severity_overrides)
            would = [f'{a.get("field")}={a.get("value")!r} ({a.get("reason")})'
                     for a in gate["repair_plan"]
                     if a["kind"] in ("set_column", "set_property")]
            held = [f'{a.get("field")} ({a.get("code") or a.get("reason")})'
                    for a in gate["repair_plan"] if a["kind"] == "escalate"]
            parts = []
            if would:
                parts.append("would write " + ", ".join(would))
            if held:
                parts.append("a person decides " + ", ".join(held))
            if parts:
                note = "; ".join(parts)
        return note

    step("reconcile", "", _reconcile)

    # ---- 3b. selling price ------------------------------------------------
    #
    # WHY THIS IS NOT PART OF reconcile. The gate computes a corrected price
    # and puts it in the plan, but resolver._gate downgrades any numeric change
    # over `pricing.auto_apply_max_delta_pct` (60%) from APPLY to PROPOSE, and
    # verifyAndRepair executes no `propose` without `--authorize`. So the
    # products whose price was MOST wrong were precisely the ones nothing
    # touched: BOA-006108 sat at 356.99 against a 61.99 retail — a 93%
    # correction — and blocked on PRICE.001 every single pass.
    #
    # Raising the threshold or passing --authorize would both defeat a guard
    # that is right for a plan assembled from rules and evidence. This step is
    # narrower than either: fix-selling-price.ts calls `verifySellingPrice`,
    # the same function analyze and regrade use, and clamps to the nearest edge
    # of the grade's own window. It guesses nothing, so the size of the change
    # is not evidence that the change is risky.
    #
    # AFTER reconcile, deliberately. Reconcile can repair the grade and the
    # retail anchor, and both are inputs to the window — running this first
    # would clamp against numbers that are about to change.
    #
    # ALWAYS RUN in apply mode rather than gated on a price finding: the script
    # is a no-op when the price is already inside the window, it costs no model
    # call, and having it decide is what keeps Hermes from holding a second
    # opinion about what a correct price is.
    def _price() -> str:
        ok, _, payload = run_step(vnyx_api, "fix-selling-price.ts",
                                  [*common, *(["--apply"] if apply else [])],
                                  timeout_s=120, quiet=quiet,
                                  results_name="price.json")
        if not ok:
            raise StepFailed("fix-selling-price.ts returned non-zero")

        rows = (payload or {}).get("results") or []
        row = rows[0] if rows else None
        if not row:
            return "price already inside the window"
        before, after = row.get("before"), row.get("after")
        if after is None or before == after:
            return "price already inside the window"
        verb = "would set" if not apply else "set"
        return f'{verb} {before} {ARROW} {after} ({row.get("reason") or "clamped"})'

    # CONSIGNMENT: the selling price is the consignor's under contract. The gate
    # already refuses to plan a price write for these tenants (approval.py); this
    # keeps the clamp step from doing what the gate declined to.
    from app.approval import is_consignment
    price_why = ("consignment tenant — the selling price is the consignor's, "
                 "not written" if is_consignment(
                     str(state["loaded"]["record"].get("tenantId") or ""), policy())
                 else "")
    step("price", price_why, _price)

    # ---- 4. render --------------------------------------------------------
    #
    # WHAT THE SCRIPT SAID, kept (readiness phase 4). backfill-imagery.ts
    # exits 0 on a partial set and names the views it could not produce, and
    # Hermes' generator tells a REFUSAL (the model declining a named athlete's
    # kit, a licensed print — finish_reason IMAGE_OTHER / SAFETY) from a fault.
    # A product every model declines cannot be rendered by re-running anything;
    # that is one of decision 8's two rejections, and it is decided from these
    # lines, not from an exit code.
    render_report: dict[str, Any] = {"attempted": False, "views": [], "written": [],
                                     "failed": [], "refused": False}
    regen_report: dict[str, Any] = {"attempted": False, "views": [], "written": [],
                                    "failed": [], "refused": False}

    def _read_render(out: str, into: dict[str, Any]) -> None:
        for line in (out or "").splitlines():
            m = re.search(r"wrote \[([^\]]*)\]", line)
            if m:
                into["written"] = [v.strip() for v in m.group(1).split(",")
                                   if v.strip() and v.strip() != "nothing"]
            m = re.search(r"could not produce \[([^\]]*)\]", line)
            if m:
                into["failed"] = [v.strip() for v in m.group(1).split(",") if v.strip()]
            if "note:" in line and _refusal_text(line):
                into["refused"] = True

    def _render() -> str:
        render_report["attempted"] = True
        ok, out, _ = run_step(vnyx_api, "backfill-imagery.ts",
                           [*common, *live], timeout_s=1800, quiet=quiet)
        _read_render(out, render_report)
        if not ok:
            raise StepFailed("imagery backfill returned non-zero")
        note = f'{state["renders_missing"]} view(s)'
        if render_report["failed"]:
            note += f' — could not produce {", ".join(render_report["failed"])}'
            if render_report["refused"]:
                note += " (the image model declined the print)"
        return note

    from app import readiness as _rd

    is_kids = _rd.is_kids(state.get("master"), state.get("mannequin"), policy())
    if skip_render:
        render_why = "--no-render"
    elif not state["renders_missing"]:
        render_why = "all five views already exist"
    elif is_kids and _rd.config(policy()).get("kids_renders") == "hold":
        # Decision 4 is `generate`; this branch exists so a tenant can turn it
        # off without a code change, and says so.
        render_why = "Kids — renders left to a person (readiness.kids_renders: hold)"
    elif _generation_in_flight(state):
        # CREDIT-RESUME SAFETY. A generation already running will deliver these
        # renders; firing a second one pays twice for the same five pictures and
        # races the first for the rows. backfill-imagery.ts does not check this
        # itself. Only a LIVE run is respected: past `stale_generation_hours` the
        # flag is a stranded row, not work in progress, and the render goes ahead.
        render_why = ("generation already in flight — not paying for a second "
                      "run while the first is live")
    else:
        render_why = ""
    step("render", render_why, _render)

    # ---- 4b. gate ---------------------------------------------------------
    #
    # ONE LOOK AT THE LEAD RENDER before anything is published. Every step
    # above reads columns; none of them looks at the picture a customer will
    # see. A render can carry a melted face, a floating leg, no model at all,
    # or a model of the wrong gender — and pass every rule, because the rules
    # ask whether AI_FRONT exists, not whether it is any good.
    #
    # After render, so it judges what was just produced; before approve, so it
    # can withhold the move. One Gemini flash call, ~3s. It runs in dry-run
    # too, so a shadow run reports what the gate WOULD have refused — that is
    # how its false-block rate is measured before it is allowed to block.
    #
    # A gate that could not run (provider down, image unreachable) is not a
    # refusal: it lands on `vision_unavailable` and the step is recorded as
    # failed, and the runner retries the product instead of holding it. See
    # app/imaging/quality_gate.py and app/llm/health.py.
    gate_verdict: Any = None

    def _judge_lead() -> Any:
        """One look at the lead render, on the rows as they are NOW."""
        from app.imaging import quality_gate

        # Re-read: the render step may have just produced the row this judges.
        fresh = needs(dsn, product_id)["loaded"]
        record = fresh["record"]
        # THE MASTER CATEGORY IS THE REFERENCE (readiness phase 1). The render
        # is judged against the gender the master implies; the gender property
        # is the fallback for a root that implies none (Unisex, Kids). On a
        # product whose property still says the other gender, reconcile has
        # already planned it to the master — in a dry run nothing was written,
        # and judging against the stale property would refuse a correct render.
        #
        # AND AGAINST THE GARMENT'S SIZE (readiness phase 4): the model's build
        # is held to the band the size implies — never for Kids, footwear or
        # an accessory, and never when the size implies no build.
        from app import readiness

        return quality_gate.judge(
            fresh["media"],
            gender=(readiness.root_gender(record.get("masterCategory"), policy())
                    or record.get("gender")),
            category=record.get("category"),
            subcategory=record.get("subCategory"),
            pol=policy(),
            size=record.get("size") or record.get("internationalSize"),
            kids=readiness.is_kids(record.get("masterCategory"),
                                   record.get("mannequinType"), policy()),
        )

    def _gate() -> str:
        nonlocal gate_verdict
        gate_verdict = _judge_lead()
        if gate_verdict.unavailable:
            vision_unavailable.append("gate")
            raise StepFailed("vision unavailable — " + "; ".join(gate_verdict.reasons))
        return gate_verdict.summary()

    gate_why = ("" if (policy().get("quality_gate") or {}).get("enabled", True)
                else "disabled in policy (quality_gate.enabled)")
    step("gate", gate_why, _gate)

    # ---- 4c. photos -------------------------------------------------------
    #
    # The other picture: what the garment PHOTOGRAPHS show, which the render
    # cannot — wear against the grade, and whether each gallery image is what
    # its slot says. One call for both. Enforced by _approve exactly like the
    # gate; `photo_audit.required` decides whether "could not run" holds.
    # See app/imaging/photo_audit.py.
    #
    # BEFORE THE REGEN, because it is the only check that sees the four renders
    # the gate does not. MID-000253 (17 Sep 2026): the lead AI_FRONT passed the
    # gate while the AI_FRONT_34 beside it had the model's knees smeared to a
    # white blur — this audit saw it and, as a soft flag, nothing followed.
    # Now a render it calls defective (RENDER_DEFECT, `bad_views`) is one more
    # thing the regen step re-renders.
    photo_verdict: Any = None

    def _judge_photos() -> Any:
        """One look at the photographs and every render, on the rows as they are NOW."""
        from app.imaging import photo_audit

        fresh = needs(dsn, product_id)["loaded"]
        record = fresh["record"]
        # The gender the MASTER implies, so the audit can say whether the
        # garment photographs agree with the anchor (readiness phase 1). None
        # for Unisex / Kids, where nothing is implied and nothing is checked.
        from app import readiness

        return photo_audit.judge(
            fresh["media"],
            grade_severity=record.get("gradeSeverity"),
            grade_label=record.get("gradeLabel") or record.get("grade"),
            pol=policy(),
            product_gender=readiness.root_gender(record.get("masterCategory"), policy()),
        )

    def _photos() -> str:
        nonlocal photo_verdict
        from app.imaging import photo_audit

        photo_verdict = _judge_photos()
        if photo_verdict.unavailable:
            vision_unavailable.append("photos")
            raise StepFailed("vision unavailable — " + "; ".join(photo_verdict.reasons))
        return photo_audit.summary(photo_verdict)

    photos_why = ("" if (policy().get("photo_audit") or {}).get("enabled", True)
                  else "disabled in policy (photo_audit.enabled)")
    step("photos", photos_why, _photos)

    # ---- 4c. rematte ------------------------------------------------------
    #
    # A CUT-OUT THE PHOTO AUDIT CALLS DEFECTIVE is re-cut from the raw archive
    # — the same call the matte step makes on a measured defect — and the
    # audit looks again. MID-000569 (17 Sep 2026): the sweater's ribbed
    # neckband was missing from both FRONT cut-outs, plain on the edit screen;
    # the canvas and backdrop measurements had nothing to say about a garment
    # part the mask ate, and only a look at the picture does. Held only under
    # `readiness.cutouts.hold: block`; while soft, a defect the re-cut did not
    # clear stays a flag. Skipped when the matte step re-cut this product this
    # run already: the same segmenter would return the same cut.
    rematte_report: dict[str, Any] = {"attempted": False, "views": [], "written": None,
                                      "failed": None, "kept": None, "strategies": None,
                                      "after": None}

    def _cutout_defects(v: Any) -> str:
        return "; ".join(r for r in [*v.reasons, *v.soft] if str(r).startswith("CUTOUT DEFECT"))

    def _rematte() -> str:
        nonlocal photo_verdict, cutout_verdict
        from app.imaging import cutout as _cutout
        from app.imaging import photo_audit

        views = photo_audit.rematte_views(photo_verdict, policy())
        why = _cutout_defects(photo_verdict)
        # A DIFFERENT SEGMENTER, NOT THE SAME ONE (item 6 of
        # docs/PICTURE-CHECK-FIXES.md). Which one depends on what is wrong:
        #
        #   something LEFT IN   a stand, a hanger, a hand. cloth-seg is a
        #                       clothing parser and the podium is directly
        #                       beneath the clothing, so re-running it returns
        #                       the same podium and the same verification
        #                       passes it again — which is exactly why
        #                       MID-000521 carried "stand visible at bottom" on
        #                       all four cut-outs through every repair run. The
        #                       mask strategies are asked instead, and Hermes
        #                       intersects their answer with cloth-seg so the
        #                       garment parser still decides what is cloth.
        #   something MISSING   a collar, a strap, part of a back. The default
        #                       chain, because cloth-seg is still the best first
        #                       answer for what IS the garment and a mask
        #                       strategy is not more likely to find the collar.
        plan = _cutout.rematte_strategies(why)
        rematte_report.update({"attempted": True, "views": views, "strategies": plan})
        if not apply:
            tail = (f"; asking {', '.join(plan)} instead of the default chain, because "
                    f"something was left in rather than cut out" if plan else "")
            return (f"would re-cut {', '.join(views)} from the raw archive ({why}); "
                    f"the photo audit would judge the new cut-outs{tail}")

        # Same rule as the matte step: no guard, no re-cut. A re-matte is the one
        # place that DELIBERATELY supersedes a live cut-out, so it is the last
        # place to accept a best-effort call.
        need = {"keepBetter": "--keep-better"}
        if plan:
            need["bgStrategies"] = "--bg-strategies"
        if not remote_supports(*need):
            missing = ", ".join(need.values())
            rematte_report["skipped"] = f"vnyx-api does not accept {missing}"
            return (f"left the existing cut-outs alone — this vnyx-api cannot take "
                    f"{missing}; re-cutting without them either stores a worse "
                    f"cut-out or asks the same segmenter that left this in")

        provider = os.getenv("AUTO_APPROVAL_BG_PROVIDER", "hermes").strip()
        args = [*common, *live, *(["--provider", provider] if provider else []),
                "--replace", "--keep-better"]
        if plan:
            args.extend(["--bg-strategies", ",".join(plan)])
        ok, out, _ = run_step(vnyx_api, "backfill-bg-removal.ts", args,
                              timeout_s=600, quiet=quiet)
        if not ok:
            raise StepFailed("background removal returned non-zero")
        written = failed = kept = None
        for line in (out or "").splitlines():
            low = line.lower()
            if "cut-outs written" in low:
                written = _trailing_int(line)
            elif "kept" in low and ":" in line:
                kept = _trailing_int(line)
            elif "failed" in low and ":" in line:
                failed = _trailing_int(line)
        rematte_report.update({"written": written, "failed": failed, "kept": kept})
        if failed and not written:
            raise StepFailed(f"background removal produced no cut-outs ({failed} image(s) "
                             f"failed). The segmenter is unreachable or timing out.")
        note = f"re-cut {', '.join(views)} from the raw archive ({why})"
        if plan:
            note += f" with {', '.join(plan)} — the same segmenter would return the same cut"
        if failed:
            note += f" ({written} written, {failed} FAILED)"

        # LOOK AGAIN. The measurements on the new cut-outs, then the audit.
        if cutouts_on and state.get("cutout_views"):
            cutout_verdict = _cutouts_now()
        second = _judge_photos()
        rematte_report["after"] = second.as_dict()
        photo_verdict = second
        if second.unavailable:
            vision_unavailable.append("photos")
            raise StepFailed(note + " — vision unavailable on the second look: "
                             + "; ".join(second.reasons))
        still = bool(photo_audit.rematte_views(second, policy()))
        if still:
            note += (" — STILL flagged after the re-cut: " + _cutout_defects(second)
                     + (" (holds as CUTOUT_DEFECT)" if second.blocks
                        else " (soft — readiness.cutouts.hold)"))
        elif second.action == "ok":
            note += " — photo audit again: passed"
        else:
            note += f" — photo audit again: {photo_audit.summary(second)}"
        if kept:
            # THE PICTURE ON THE PAGE IS THE ONE THAT WAS ALREADY THERE (item
            # 8): the replacement had less garment than it and was refused, so
            # nothing changed. Said out loud, and said harder when the audit is
            # still flagging — otherwise the row reads as a re-cut that did not
            # help and the product comes back through the same step next run to
            # be cut the same way, which is the loop §2.1 exists to break.
            note += (f"; {kept} view(s) were NOT replaced — the new cut-out lost part of the "
                     f"garment and the ORIGINAL WAS KEPT"
                     + (", so this defect is the one that was already on file and a further "
                        "re-cut will not clear it" if still else ""))
        return note

    from app.imaging import photo_audit as _pa

    if photo_verdict is None or not _pa.rematte_views(photo_verdict, policy()):
        rematte_why = "the photo audit found every cut-out whole"
    elif matte_state["replaced"]:
        rematte_why = ("the matte step re-cut this product this run already — the same "
                       "segmenter would return the same cut")
    else:
        rematte_why = ""
    step("rematte", rematte_why, _rematte)

    # ---- 4c. regen --------------------------------------------------------
    #
    # THE ONE PAID RETRY (readiness phase 4, docs/READINESS-PLAN.md §4 step 5).
    # A refusal used to be the end of it: the product held, a person opened
    # the edit screen and pressed Regenerate, and the same picture came back
    # unless they also fixed the gender. Now the fields are settled first
    # (master → gender, size → build, phases 1 and 2), so a re-render has a
    # real chance of being right, and the chain takes it — ONCE.
    #
    # What is re-rendered follows the refusals (quality_gate.regen_views and
    # photo_audit.regen_views, merged): a gender or build problem is the
    # MODEL's, and one model carries the set, so every view goes; a broken
    # face or body on the lead is that picture's, so only the lead does; a
    # frame the figure does not fill, or a render the photo audit calls
    # defective, is that VIEW's, so only it does — the renderer keeps the model
    # identity from the product's stored personality and its AI_FRONT. Through
    # the render step's `views` option (phase 2), which retires the old render
    # of each named view. Then every check that asked looks again, once. A
    # second refusal holds the product with the second verdict; the budget
    # (`readiness.max_regenerations_per_run`) is spent, never looped.
    #
    # The canary (repair()'s generation check) counts this step as a render
    # the chain caused, so the flip to GENERATING it produces is not an alarm.
    regeneration: dict[str, Any] = {"attempted": False, "views": [], "code": None,
                                    "asked_by": [], "before": None, "after": None,
                                    "photos_before": None, "photos_after": None}

    def _regen_plan() -> dict[str, Any]:
        """Who asked for a re-render, and of which views.

        The gate's refusal and the photo audit's render defects, merged in the
        renderer's order. `code` is the gate's when it asked (a set refusal
        names the fix), else RENDER_DEFECT; `why` quotes each asker's reasons.
        """
        from app.imaging import photo_audit, quality_gate

        pol = policy()
        askers: list[tuple[str, str, list[str], list[str]]] = []      # label, code, reasons, views
        if gate_verdict is not None and gate_verdict.action == "regen":
            askers.append(("gate", str(gate_verdict.code), list(gate_verdict.reasons),
                           quality_gate.regen_views(gate_verdict, pol)))
        if photo_verdict is not None:
            pv = photo_audit.regen_views(photo_verdict, pol)
            if pv:
                askers.append(("photos", "RENDER_DEFECT",
                               [r for r in photo_verdict.reasons if r.startswith("RENDER DEFECT")]
                               or list(photo_verdict.reasons), pv))
        all_views = list((pol.get("imagery") or {}).get("all_views")
                         or ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"])
        wanted = {v for _, _, _, vs in askers for v in vs}
        views = [v for v in all_views if v in wanted] + sorted(v for v in wanted if v not in all_views)
        return {
            "askers": [a for a, _, _, _ in askers],
            "views": views,
            "all_views": all_views,
            "code": next((c for a, c, _, _ in askers if a == "gate"), None)
                    or next((c for _, c, _, _ in askers), None),
            "why": "; ".join(f'{c}: {"; ".join(rs)}' for _, c, rs, _ in askers),
        }

    def _regen() -> str:
        nonlocal gate_verdict, photo_verdict
        from app.imaging import photo_audit

        plan = _regen_plan()
        views = plan["views"]
        regeneration.update({"attempted": True, "views": views, "code": plan["code"],
                             "asked_by": plan["askers"],
                             "before": gate_verdict.as_dict() if gate_verdict is not None else None,
                             "photos_before": photo_verdict.as_dict() if photo_verdict is not None else None})
        regen_report["attempted"] = True
        regen_report["views"] = views
        ok, out, _ = run_step(vnyx_api, "backfill-imagery.ts",
                              [*common, *live, "--views", ",".join(views)],
                              timeout_s=1800, quiet=quiet)
        _read_render(out, regen_report)
        if not ok:
            raise StepFailed("regeneration returned non-zero")

        scope = "the whole set" if set(views) >= set(plan["all_views"]) else ", ".join(views)
        why = plan["why"]
        if not apply:
            # Nothing was rendered, so there is nothing new to judge. The first
            # verdicts stand — a dry run reports what was refused and what the
            # live run would re-render.
            return f"would regenerate {scope} ({why}); the checks would judge the new set"

        note = f"regenerated {scope} ({why})"
        if regen_report["failed"]:
            note += f' — could not produce {", ".join(regen_report["failed"])}'
            if regen_report["refused"]:
                note += " (the image model declined the print)"

        # LOOK AGAIN, ONCE. Every fix is followed by the check that asked for
        # it; the second verdicts are the ones _approve enforces. The gate
        # always (its lead may be among the views; otherwise the cache answers
        # for free), the photo audit when it asked.
        outcomes: list[str] = []
        second = _judge_lead()
        regeneration["after"] = second.as_dict()
        gate_verdict = second
        if second.unavailable:
            vision_unavailable.append("gate")
            raise StepFailed(note + " — vision unavailable on the second look: "
                             + "; ".join(second.reasons))
        if second.action == "regen":
            outcomes.append(f'REFUSED AGAIN ({second.code}: {"; ".join(second.reasons)})')
        elif second.action == "ok":
            outcomes.append("gate again: passed")
        else:
            outcomes.append(f"gate again: {second.summary()}")

        if "photos" in plan["askers"]:
            second_p = _judge_photos()
            regeneration["photos_after"] = second_p.as_dict()
            photo_verdict = second_p
            if second_p.unavailable:
                vision_unavailable.append("photos")
                raise StepFailed(note + " — vision unavailable on the photo audit's second look: "
                                 + "; ".join(second_p.reasons))
            if photo_audit.regen_views(second_p, policy()):
                outcomes.append("photo audit REFUSED AGAIN ("
                                + "; ".join(r for r in second_p.reasons if r.startswith("RENDER DEFECT")) + ")")
            elif second_p.action == "ok":
                outcomes.append("photo audit again: passed")
            else:
                outcomes.append(f"photo audit again: {photo_audit.summary(second_p)}")

        note += " — " + "; ".join(outcomes)
        if any("REFUSED AGAIN" in o for o in outcomes):
            note += "; the budget is spent, the product holds"
        return note

    budget = int(_rd.config(policy()).get("max_regenerations_per_run") or 0)
    regen_plan = _regen_plan()
    if not regen_plan["askers"]:
        regen_why = "the gate did not ask for a re-render, nor did the photo audit"
    elif skip_render:
        regen_why = "--no-render"
    elif budget < 1:
        regen_why = "readiness.max_regenerations_per_run is 0"
    elif not regen_plan["views"]:
        regen_why = "nothing to regenerate for this verdict"
    elif _generation_in_flight(state):
        regen_why = ("generation already in flight — not paying for a second "
                     "run while the first is live")
    else:
        regen_why = ""
    step("regen", regen_why, _regen)

    def _snapshot_now() -> Any:
        """The product as the rules see it, on the rows as they are NOW."""
        from app.vnyx_client import to_snapshot

        fresh = needs(dsn, product_id)["loaded"]
        return to_snapshot({**fresh["record"], "media": fresh["media"]},
                           catalog=fresh.get("catalog"),
                           imagery_settings=fresh.get("imagery_settings"))

    # ---- 4d. order --------------------------------------------------------
    #
    # THE GALLERY IN THE CATALOG ORDER (readiness phase 5, decision 2). `images`
    # is vnyx-api's cache of the rows in display order, rebuilt on every media
    # mutation and never otherwise — so a product nothing touched keeps the
    # order the day it was made, and the order changed (phase 2: uploads and
    # the booth ahead of the portal). IMG.025 compares the cache with what the
    # rows imply; the fix is never a position write from here, it is the
    # rebuild vnyx-api owns (`rebuild-media-cache.ts`, the `reorder` step). A
    # gallery a person arranged is left as arranged.
    order_report: dict[str, Any] = {"checked": False, "in_order": None, "manual": False,
                                    "divergence": None, "rebuilt": False}

    def _order() -> str:
        from app.rules import imagery

        snap = _snapshot_now()
        order_report["checked"] = True
        respect_manual = bool(imagery.gallery_config(policy()).get("respect_manual", True))
        if snap.media_manual_order:
            order_report["manual"] = True
            if respect_manual:
                return "a person arranged this gallery — left as arranged"
        divergence = imagery.gallery_divergence(snap.images, imagery.gallery_order(snap, policy()))
        order_report["in_order"] = divergence is None
        order_report["divergence"] = divergence
        if divergence is None:
            return "gallery in the catalog order" + (
                " (flagged as arranged by a person; the flag is not trusted — readiness.gallery.respect_manual)"
                if snap.media_manual_order else "")
        desc = (f'position {divergence["index"] + 1} shows {divergence["actual"]} where '
                f'{divergence["expected"]} belongs')
        # A flagged gallery is rebuilt only because policy says the flag is not
        # to be trusted; the script is told so explicitly.
        extra = ["--include-manual"] if snap.media_manual_order else []
        ok, _, _ = run_step(vnyx_api, "rebuild-media-cache.ts", [*common, *live, *extra],
                            timeout_s=120, quiet=quiet)
        if not ok:
            raise StepFailed("media cache rebuild returned non-zero")
        if not apply:
            return f"would rebuild the media cache — {desc}"
        order_report["rebuilt"] = True
        # The check that asked for the fix, again.
        again = imagery.gallery_divergence(
            _snapshot_now().images, imagery.gallery_order(_snapshot_now(), policy()))
        order_report["in_order"] = again is None
        if again is None:
            return f"rebuilt the media cache — was: {desc}; now in the catalog order"
        return (f'rebuilt the media cache — still out of order: position '
                f'{again["index"] + 1} shows {again["actual"]} where {again["expected"]} belongs')

    from app.rules import imagery as _imagery

    order_why = ("" if _imagery.gallery_enabled(policy())
                 else "disabled in policy (readiness.gallery)")
    step("order", order_why, _order)

    # ---- 4e. copy ---------------------------------------------------------
    #
    # THE TITLE AND DESCRIPTION, FROM THE SETTLED RECORD (readiness phase 5,
    # §4 step 8). The title template is "Vintage [Brand] [Colour] [Subcategory]
    # [Gender] [Size]"; every one of those may have moved this run — the master
    # cascade, the care-label size, the extractor's brand — and the copy that
    # was written before they did now says the wrong thing (TEXT.007 on a
    # C-twin, TEXT.008 on a re-sized product). So: when a field the copy reads
    # CHANGED this run, or the TEXT rules already disagree with the record,
    # regenerate both through `regenerate-copy.ts` — the same two services the
    # edit screen's buttons call, writing only title and summary — and run the
    # TEXT rules again on the result. Not charged (decision 5 open).
    copy_report: dict[str, Any] = {"triggers": [], "changed": [], "rules": [], "ran": False,
                                   "title_before": None, "title_after": None, "rules_after": None}

    def _norm(value: Any) -> str:
        if isinstance(value, (list, tuple)):
            return ",".join(sorted(str(v).strip().lower() for v in value if str(v).strip()))
        return str(value or "").strip().lower()

    def _copy_triggers() -> tuple[list[str], list[str]]:
        from app.rules.consistency import check_copy

        cfg = _rd.config(policy()).get("copy") or {}
        before_rec = state["loaded"]["record"]
        fresh = needs(dsn, product_id)["loaded"]
        now_rec = fresh["record"]
        changed = [f for f in (cfg.get("trigger_fields") or [])
                   if _norm(before_rec.get(f)) != _norm(now_rec.get(f))]
        from app.vnyx_client import to_snapshot

        snap = to_snapshot({**now_rec, "media": fresh["media"]}, catalog=fresh.get("catalog"),
                           imagery_settings=fresh.get("imagery_settings"))
        wanted = set(cfg.get("trigger_rules") or [])
        rules = sorted({f.rule_id for f in check_copy(snap, policy()) if f.rule_id in wanted})
        return changed, rules

    def _copy() -> str:
        from app.rules.consistency import check_copy

        why = ", ".join([*(f"{f} changed" for f in copy_report["changed"]), *copy_report["rules"]])
        ok, _, payload = run_step(vnyx_api, "regenerate-copy.ts", [*common, *live],
                                  timeout_s=180, quiet=quiet, results_name="copy.json")
        if payload is None:
            raise StepFailed("regenerate-copy.ts returned no result"
                             + ("" if ok else " and exited non-zero"))
        if not payload.get("ok"):
            raise StepFailed(f'{payload.get("outcome") or "failed"}: '
                             f'{payload.get("error") or ", ".join(payload.get("failed") or []) or "no detail"}')
        if not apply:
            return f"would regenerate title and description ({why})"
        copy_report["ran"] = True
        title = payload.get("title") or {}
        copy_report["title_before"], copy_report["title_after"] = title.get("before"), title.get("after")
        # THE TEXT RULES AGAIN on the result — every fix is followed by the
        # check that asked for it.
        after = sorted({f.rule_id for f in check_copy(_snapshot_now(), policy())
                        if f.rule_id.startswith("TEXT.")})
        copy_report["rules_after"] = after
        return (f'regenerated title and description ({why}) — title now '
                f'"{title.get("after")}"; TEXT rules after: {", ".join(after) or "clean"}')

    if not (_rd.config(policy()).get("copy") or {}).get("enabled", True):
        copy_why = "disabled in policy (readiness.copy)"
    else:
        changed_fields, text_rules = _copy_triggers()
        copy_report["changed"], copy_report["rules"] = changed_fields, text_rules
        copy_report["triggers"] = [*(f"{f} changed" for f in changed_fields), *text_rules]
        # NEVER DESCRIBE DATA THE RULES HAVE ALREADY REJECTED.
        #
        # The title is generated FROM the taxonomy, gender and size. If those are
        # still blocking after `reconcile` ran, regenerating the copy does not
        # repair anything — it launders a known-bad record into prose and makes
        # the damage much harder to see, because a wrong title reads as a real
        # product while a wrong `subCategory` reads as a bug.
        #
        # 18 Sep 2026 is the case. `reconcile` wrote `category: 'hoodie'` on an
        # Adidas t-shirt and TAX.002 rejected it on the next line; `copy` then
        # ran on "category changed" and produced "Vintage Adidas Deep Burgundy
        # Hoodie Women S". Same run: "Men's/Unisex" on one product, a New Balance
        # title that stopped naming New Balance on another.
        #
        # Checked against the STORED record after reconcile, not against the plan
        # — a step can report success and leave the field unwritten.
        blocking_now = sorted({
            f.rule_id for f in _blocking_taxonomy(_snapshot_now())
        })
        if not copy_report["triggers"]:
            copy_why = ("nothing the title or description reads changed, "
                        "and the copy agrees with the record")
        elif blocking_now:
            copy_report["withheld"] = blocking_now
            copy_why = (f'the record still fails {", ".join(blocking_now)} — the title is '
                        f'generated from those fields, so regenerating it now would only '
                        f'describe the wrong product convincingly')
        else:
            copy_why = ""
    step("copy", copy_why, _copy)

    # ---- 5. approve -------------------------------------------------------
    #
    # The CHECK always runs; the MOVE needs --approve. See the module docstring:
    # approving publishes a live Shopify listing and there is no undo.
    verdict: dict[str, Any] = {}

    def _approve() -> str:
        nonlocal verdict
        # THE GATE'S REFUSAL IS ENFORCED HERE, not by skipping the step. The
        # pre-flight still runs without --apply so the product's readiness is
        # known and recorded; only the move is withheld. `approved` cannot come
        # back from a call that was never allowed to apply.
        # Three picture verdicts, one rule: any refusing withholds the move.
        # The gate comes first because it is the definition of "may this leave
        # Review"; the photo audit is evidence added to it; the cut-outs
        # (readiness phase 3) refuse only once `readiness.cutouts.hold` is
        # block, and only for a defect the re-matte did not clear.
        refusing = [(label, v) for label, v in (("image gate", gate_verdict),
                                                ("photo audit", photo_verdict),
                                                ("cut-outs", cutout_verdict))
                    if v is not None and v.blocks]
        blocked = bool(refusing)
        # Only ever passes --apply when BOTH flags are set. `--apply` alone
        # repairs; publishing has to be asked for separately.
        verdict = approve_check(vnyx_api, dsn, product_id,
                                apply=apply and approve and not blocked,
                                skip_bin=skip_bin,
                                quiet=quiet, allow_stage=allow_stage)
        if blocked:
            gate_problems = [f"{label}: {r}" for label, v in refusing for r in v.reasons]
            # A real refusal names its code; only when EVERY refusing verdict is
            # "could not run" is the outcome the retryable gate_unavailable.
            real = [v for _, v in refusing if not v.unavailable]
            if verdict.get("outcome") in ("would_approve", "approved"):
                # Ready by every column check, refused on the picture. A
                # distinct outcome, so the verdict names the gate rather than
                # reporting "ready, not moved".
                verdict = {**verdict,
                           "outcome": ("gate_blocked" if real else "gate_unavailable"),
                           "problems": gate_problems}
            else:
                # Not ready anyway. The gate's reasons ride along so the row
                # shows everything a human has to fix, not just the first thing.
                verdict = {**verdict,
                           "problems": [*(verdict.get("problems") or []),
                                        *gate_problems]}
            verdict["gate_code"] = (real or [refusing[0][1]])[0].code
        outcome = verdict.get("outcome", "?")
        problems = verdict.get("problems") or []
        if outcome == "approved":
            return (f'APPROVED — {verdict.get("stageBefore")} '
                    f'{ARROW} {verdict.get("stageAfter")}, Shopify upsert enqueued')
        if outcome == "would_approve":
            return "ready — pass --approve to move it"
        if outcome == "gate_blocked":
            return "NOT approved — refused on the pictures: " + "; ".join(problems)
        if outcome == "gate_unavailable":
            return "NOT approved — the picture check could not run: " + "; ".join(problems)
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
    # The tenant's Brain overrides apply to the FINAL verdict too. Without them
    # the chain would repair against a downgraded rule set and then be judged
    # against the un-downgraded one, so a product the tenant had explicitly
    # stopped blocking on would still come back held.
    final = product_audit.audit(dsn, product_id, apply=False, write_sheet=False,
                                severity_overrides=severity_overrides)

    # THE ANCHOR'S VERDICT (readiness phase 1). Unresolved at the head of the
    # chain, or still blocking after reconcile had its turn (TAX.001, or the
    # master among DATA.010's empty fields): either way no root fits, nothing
    # downstream can be settled, and the outcome names it rather than whichever
    # rule sorted first.
    master_unresolved = master_decision.get("action") == "unresolved" or any(
        f.get("rule_id") == "TAX.001"
        or (f.get("rule_id") == "DATA.010"
            and "master_category" in (f.get("fields") or []))
        for f in final["remaining"]
    )

    # UNFIXABLE BY ANY RE-RUN (readiness phase 4, decision 8). Two defects no
    # step can repair: no garment photograph to render from (IMG.003), and a
    # print every image model declines. Named here with the reason a person
    # would read; whether that becomes a REJECTION is the runner's call under
    # `readiness.unfixable` — the chain reports, the agent archives.
    #
    # FROM THE RULE, NOT FROM A COUNT. IMG.003 is silent when the loader saw no
    # media rows at all, by design — and 185 approved products on the local
    # clone carry a populated `images` cache and not one ProductMedia row. A
    # zero-row count read as "no photograph" would have rejected every one of
    # them; the rule's silence is the protection.
    remaining_ids = [f["rule_id"] for f in final["remaining"]]
    unfixable: dict[str, str] | None = None
    if "IMG.003" in remaining_ids:
        unfixable = {
            "code": "NO_GARMENT_PHOTO",
            "reason": ("NO_GARMENT_PHOTO — no garment photograph to render from; "
                       "reshoot required"),
        }
    elif any(r["attempted"] and r["refused"] and not r["written"]
             for r in (render_report, regen_report)):
        unfixable = {
            "code": "RENDER_REFUSED",
            "reason": ("RENDER_REFUSED — the image models decline this print; "
                       "photograph it on a model or list it without renders"),
        }

    render_ran = any(s.get("step") in ("render", "regen") and s.get("ran") for s in steps)

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
            "gate_code": verdict.get("gate_code"),
        },
        "gate": gate_verdict.as_dict() if gate_verdict is not None else None,
        "photos": photo_verdict.as_dict() if photo_verdict is not None else None,
        # The cut-outs as measured BEFORE the matte step and AFTER it (readiness
        # phase 3). `after` is what _approve enforced; in a dry run it is the
        # `pending` verdict, never the pre-repair defect.
        "cutouts": {
            "before": cutout_before.as_dict() if cutout_before is not None else None,
            "after": cutout_verdict.as_dict() if cutout_verdict is not None else None,
        },
        "master": master_decision,
        "master_unresolved": master_unresolved,
        # The one paid retry (readiness phase 4): what the gate refused, what was
        # re-rendered, and what the gate said the second time.
        "regeneration": regeneration,
        "render_report": render_report,
        # A cut-out the photo audit refused, re-cut from the raw archive, and
        # what the audit said afterwards.
        "rematte": rematte_report,
        # Readiness phase 5: the gallery order as checked and rebuilt, and the
        # copy regeneration with its triggers.
        "order": order_report,
        "copy": copy_report,
        # A defect no re-run can clear, with the reason a person would read; the
        # runner turns it into a rejection under `readiness.unfixable`.
        "unfixable": unfixable,
        "vision_unavailable": vision_unavailable,
        # THE CANARY'S EVIDENCE. `regeneration_triggered` is the one fact the
        # runner stops a run on: the status flipped to GENERATING across the
        # chain and the render step is not what did it. A flip WITH the render
        # step running is reported but not acted on — the generation path owns
        # that transition and this cannot tell an intended one from an accident.
        "generation": {
            "before": state["generation_status"],
            "after": after["generation_status"],
            # The regen step is a render the chain caused (readiness phase 4).
            "render_ran": render_ran,
            "regeneration_triggered": (
                after["generation_status"] == "GENERATING"
                and state["generation_status"] != "GENERATING"
                and not render_ran
            ),
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

    # The gate's one line, coloured by what it decided: a refusal is the thing
    # this block exists to make visible, a pass is quiet, and "could not run"
    # is neither — it is the provider, not the product.
    gate = r.get("gate") or {}
    if gate:
        action = gate.get("action")
        text = "; ".join(gate.get("reasons") or []) or action or "?"
        if gate.get("soft"):
            text += f'  (soft: {", ".join(gate["soft"])})'
        colour = {"regen": RED, "review": YELLOW, "ok": DIM}.get(action, DIM)
        label = {"ok": "passed", "regen": "REFUSED", "review": "could not decide",
                 "skipped": "skipped"}.get(action, action)
        print(f'    {"image gate":14} {paint(label, colour)} {paint(text, DIM)}'
              + (f'  {paint("on " + gate["lead_view"], DIM)}' if gate.get("lead_view") else ""))

    # The one paid retry (readiness phase 4): what was re-rendered and what the
    # gate said afterwards. Quiet when the gate never asked.
    regen = r.get("regeneration") or {}
    if regen.get("attempted"):
        after_g = regen.get("after") or {}
        outcome_txt = ({"ok": "gate again: passed", "regen": "REFUSED AGAIN — held",
                        "review": "gate again: could not decide"}
                       .get(after_g.get("action"), "not re-judged (dry run)"))
        print(f'    {"regenerated":14} '
              f'{paint(", ".join(regen.get("views") or []) or "nothing", DIM)} '
              f'{paint("for " + str(regen.get("code")), DIM)}  '
              f'{paint(outcome_txt, GREEN if after_g.get("action") == "ok" else YELLOW)}')

    rem = r.get("rematte") or {}
    if rem.get("attempted"):
        after_p = rem.get("after") or {}
        still = any(str(s).startswith("CUTOUT DEFECT") for s in
                    [*(after_p.get("reasons") or []), *(after_p.get("soft") or [])])
        outcome_txt = ("not re-judged (dry run)" if not after_p
                       else "STILL flagged" if still else "photo audit again: passed")
        if rem.get("kept"):
            # The one outcome that is neither a fix nor a failure (item 8): the
            # replacement had less garment than what is on file and was
            # refused, so the picture is unchanged and nothing should re-queue
            # it. Said on the line a person actually reads.
            outcome_txt += f' — ORIGINAL KEPT on {rem["kept"]} view(s), the re-cut lost garment'
        asked = "for CUTOUT_DEFECT" + (
            " with " + ", ".join(rem["strategies"]) if rem.get("strategies") else "")
        good = bool(after_p) and not still and not rem.get("kept")
        print(f'    {"re-cut":14} {paint(", ".join(rem.get("views") or []) or "nothing", DIM)} '
              f'{paint(asked, DIM)}  {paint(outcome_txt, GREEN if good else YELLOW)}')

    unfix = r.get("unfixable") or {}
    if unfix:
        print(f'    {"unfixable":14} {paint(unfix.get("code") or "?", RED)} '
              f'{paint(unfix.get("reason") or "", DIM)}')

    # Phase 5: the gallery order and the copy, one line each when they did something.
    order = r.get("order") or {}
    if order.get("checked") and order.get("divergence"):
        d = order["divergence"]
        state_txt = ("rebuilt" if order.get("rebuilt") else "would rebuild") + (
            "" if order.get("in_order") or not order.get("rebuilt") else " — still out of order")
        print(f'    {"gallery order":14} {paint(state_txt, GREEN if order.get("in_order") else YELLOW)} '
              f'{paint(f"was: position {d["index"] + 1} {d["actual"]} where {d["expected"]} belongs", DIM)}')
    cp = r.get("copy") or {}
    if cp.get("triggers"):
        print(f'    {"copy":14} {paint("regenerated" if cp.get("ran") else "would regenerate", GREEN if cp.get("ran") else YELLOW)} '
              f'{paint(", ".join(cp["triggers"]), DIM)}'
              + (f'  {paint("TEXT after: " + (", ".join(cp["rules_after"]) or "clean"), DIM)}'
                 if cp.get("rules_after") is not None else ""))

    # The photo audit's line, same colouring: a hold (GRADE SUSPECT, or an
    # IMAGE DEFECT when policy blocks on it) and a render it refused
    # (RENDER DEFECT — re-rendered by the regen step) are red; soft flags ride
    # in grey.
    photos = r.get("photos") or {}
    if photos:
        action = photos.get("action")
        text = "; ".join(photos.get("reasons") or []) or action or "?"
        if photos.get("soft"):
            text += f'  (soft: {"; ".join(photos["soft"])})'
        colour = {"review": RED, "regen": RED, "ok": DIM}.get(action, DIM)
        label = {"ok": "passed", "regen": "REFUSED", "review": "HELD", "skipped": "skipped"}.get(action, action)
        print(f'    {"photo audit":14} {paint(label, colour)} {paint(text, DIM)}')

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
    # NOT `--sheet`: that is the OUTPUT workbook, below. The input list and the
    # output report are both xlsx and one letter apart in the head, so they are
    # named for their direction.
    ap.add_argument("--from-sheet", "--product-sheet", dest="from_sheet",
                    help=("INPUT: run every product listed in this xlsx, in sheet "
                          "order (worst-first in an audit workbook). The column of "
                          "product ids is found by its header or by its shape."))
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
                    help=("OUTPUT: where to write this run's report. Defaults to "
                          "reports/generated/repair-<stamp>.xlsx. The product "
                          "LIST to run is --from-sheet."))
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--quiet", action="store_true",
                    help="hide the sub-scripts' own output.")
    ap.add_argument(
        "--fixture", nargs="+", metavar="PATH",
        help=("OFFLINE SELF-TEST: run the rule engine over fixture file(s) written "
              "by --dump-fixture. No database, no network, no writes; --out "
              "writes the verdicts as JSON. A directory means every *.json in it. "
              "tests/fixtures/repair/ ships one."))
    ap.add_argument(
        "--dump-fixture", metavar="PATH",
        help=("write the one --product as a fixture for --fixture, then stop. "
              "The file is real tenant data; keep it under reports/."))
    args = ap.parse_args()

    # ---- offline: a fixture, the rules, nothing else ------------------------
    if args.fixture:
        return run_fixtures(_fixture_paths(args.fixture), out=args.out)

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No --db, and no DATABASE_URL.")

    if args.dump_fixture:
        ids = [s.strip() for s in (args.products or "").split(",") if s.strip()]
        if len(ids) != 1:
            sys.exit("--dump-fixture takes exactly one --product <uuid>.")
        path = dump_fixture(dsn, ids[0], Path(args.dump_fixture))
        print(f'fixture: {path}')
        print(paint("  real tenant data — keep it under reports/ unless trimmed for a test", DIM))
        return 0

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
                    "fix-selling-price.ts",
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
        # Not "no model is called" any more: the image gate judges the lead
        # render in dry-run too, one flash call per product, so a shadow pass
        # reports what it would refuse. Nothing generates and nothing is written.
        print(paint("DRY RUN — nothing is written. Up to two vision calls per "
                    "product (the image gate, the photo audit); no renders, no "
                    "repairs. Pass --apply to do it.", YELLOW))
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
    # What the vision cache saved this run, when it was consulted at all.
    cache_line = vision_cache.summary()
    if cache_line:
        print(paint(f'  {cache_line}', DIM))
    if ready and not (args.approve and args.apply):
        print(paint('  Add --approve to move the ready ones (publishes to '
                    'Shopify).', DIM))
    return 0


if __name__ == "__main__":
    sys.exit(main())
