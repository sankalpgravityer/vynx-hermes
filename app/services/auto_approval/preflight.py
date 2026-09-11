"""Deployment assertions, run once per worker process.

TWO SUPPORTED TOPOLOGIES, and the checks differ:

  REMOTE (VNYX_API_URL set) — the normal deployment. Hermes runs on its own
    host and the repair steps run on the vnyx-api host, one authenticated HTTP
    call each. This machine needs no Node, no vnyx-api checkout and none of
    vnyx-api's credentials: a database URL, two Redis URLs, that URL and a
    shared secret is the whole list.

  LOCAL (VNYX_API_DIR set) — a single-box developer setup, or repair_product.py
    run as a CLI. The steps are spawned here, so Node, the checkout, its
    node_modules and its credentials must all be present.

WHY ASSERT AT ALL. When any of it is missing, every product fails at step 1 —
`npx not found`, or a connection refused — which reads as a code bug rather
than as a host that was never provisioned. Asserting up front turns that into
one `warn` event, a refusal to pump, and a line on the config screen.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# The six steps repair_product.py drives. In LOCAL mode all must be present as
# scripts; in REMOTE mode the server reports the same list from /ping.
#
# `verify-and-repair.ts` is the one that closes the price-write gap — without it
# the chain runs and silently never fixes a price.
REQUIRED_SCRIPTS = (
    "approve-products.ts",
    "backfill-bg-removal.ts",
    "backfill-product-data.ts",
    "relabel-ai-views.ts",
    "backfill-imagery.ts",
    "verify-and-repair.ts",
    # The price clamp. Absent, the chain runs and silently leaves a price that
    # violates PRICE.001 — the exact failure verify-and-repair.ts was added to
    # close, one step further along.
    "fix-selling-price.ts",
)


def _redact(url: str) -> str:
    """Host, port and database name. Never the password."""
    try:
        u = urlparse(url)
        return f"{u.hostname}:{u.port or 5432}{u.path}"
    except ValueError:
        return "(unparseable)"


def check() -> dict[str, Any]:
    """Everything the worker needs, as a reportable dict."""
    result: dict[str, Any] = {"ok": True, "problems": [], "checkedAt": None}

    def problem(msg: str) -> None:
        result["ok"] = False
        result["problems"].append(msg)

    # ---- the database ----------------------------------------------------
    #
    # REQUIRED IN THE PROCESS ENV, not merely resolvable. app/config.py loads
    # .env, so an unset variable would silently fall back to whatever that file
    # holds — which on a developer machine is often the SHARED dev database. The
    # approve step publishes live Shopify listings from whichever database it is
    # pointed at, so "which database" is the one thing this worker must never
    # inherit by accident.
    raw_db = os.getenv("DATABASE_URL")
    result["databaseUrl"] = bool(raw_db)
    if not raw_db:
        problem(
            "DATABASE_URL is not set in the environment. Set it explicitly — "
            "falling back to .env risks pointing this worker at the wrong "
            "database."
        )
    else:
        result["database"] = _redact(raw_db)

    # ---- Celery's own broker ---------------------------------------------
    result["celeryBrokerConfigured"] = bool(os.getenv("CELERY_BROKER_URL"))
    if not os.getenv("CELERY_BROKER_URL"):
        problem("CELERY_BROKER_URL is not set.")

    api_url = (os.getenv("VNYX_API_URL") or "").rstrip("/")
    result["transport"] = "remote" if api_url else "local"

    if api_url:
        _check_remote(api_url, result, problem)
    else:
        _check_local(result, problem)

    from datetime import datetime, timezone

    result["checkedAt"] = datetime.now(timezone.utc).isoformat()
    return result


def _check_remote(api_url: str, result: dict[str, Any], problem) -> None:
    """The normal deployment: steps run on the vnyx-api host."""
    result["vnyxApiUrl"] = api_url

    secret = os.getenv("AUTO_APPROVAL_INTERNAL_SECRET", "")
    result["internalSecretConfigured"] = bool(secret)
    if not secret:
        problem(
            "AUTO_APPROVAL_INTERNAL_SECRET is not set. The step runner refuses "
            "an unauthenticated call, so every product would fail at its first "
            "step."
        )
        return

    # One call proves three things: the host is reachable, the secret matches,
    # and it has the scripts. Cheap enough to do at boot, and the alternative is
    # discovering all three one product at a time.
    try:
        import httpx

        resp = httpx.get(
            f"{api_url}/internal/auto-approval/ping",
            headers={"x-internal-secret": secret},
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001 — any transport failure is the same fact
        problem(f"Cannot reach the step runner at {api_url}: {exc}")
        return

    if resp.status_code == 401:
        problem(
            f"The step runner at {api_url} rejected the shared secret. "
            f"AUTO_APPROVAL_INTERNAL_SECRET must match on both hosts."
        )
        return
    if resp.status_code == 503:
        problem(
            f"{api_url} has no AUTO_APPROVAL_INTERNAL_SECRET configured, so it "
            f"cannot authenticate this worker."
        )
        return
    if resp.status_code != 200:
        problem(f"The step runner at {api_url} returned {resp.status_code}.")
        return

    body = resp.json()
    result["remoteDatabase"] = body.get("database")
    result["missingScripts"] = body.get("missingScripts") or []
    if result["missingScripts"]:
        problem(
            f"The step runner is missing script(s): "
            f"{', '.join(result['missingScripts'])}"
        )

    # BOTH SIDES MUST SHARE A DATABASE, and this is the check that earns its
    # keep. The DSN is deliberately never sent over the wire — the server uses
    # its own — so a mismatch would not fail, it would quietly verify products
    # in one database and repair them in another. Comparing the redacted forms
    # catches it at boot instead.
    ours = result.get("database")
    theirs = body.get("database")
    if ours and theirs and ours != theirs:
        problem(
            f"Database mismatch: this worker uses {ours} but the step runner at "
            f"{api_url} uses {theirs}. They must be the same database."
        )


def _check_local(result: dict[str, Any], problem) -> None:
    """The single-box setup: steps are spawned here."""
    npx = shutil.which("npx") or shutil.which("npx.cmd")
    node = shutil.which("node") or shutil.which("node.exe")
    result["node"] = bool(node)
    result["npx"] = bool(npx)
    if not npx or not node:
        problem(
            "Node is not on PATH. Without VNYX_API_URL the worker spawns "
            "vnyx-api's repair scripts locally with `npx tsx`, so it needs "
            "Node 18+ — or set VNYX_API_URL to run them on the vnyx-api host "
            "instead."
        )

    raw = os.getenv("VNYX_API_DIR")
    result["vnyxApiDir"] = raw
    if not raw:
        problem(
            "Neither VNYX_API_URL nor VNYX_API_DIR is set. Set the URL to run "
            "the repair steps on the vnyx-api host (the normal deployment), or "
            "the path to a local checkout to spawn them here."
        )
        return

    base = Path(raw)
    result["vnyxApiDirExists"] = base.is_dir()
    if not base.is_dir():
        problem(f"VNYX_API_DIR does not exist: {raw}")
        return

    has_modules = (base / "node_modules").is_dir()
    result["nodeModules"] = has_modules
    if not has_modules:
        problem(
            f"{raw}/node_modules is missing — `tsx` is resolved from there, so "
            f"run `yarn install` in that checkout."
        )

    missing = [s for s in REQUIRED_SCRIPTS if not (base / "scripts" / s).is_file()]
    result["missingScripts"] = missing
    if missing:
        problem(f"Missing script(s) in {raw}/scripts: {', '.join(missing)}")

    # Only the LOCAL transport needs this. When the steps run on the vnyx-api
    # host, that host's own process enqueues the Shopify upsert onto its own
    # BullMQ — so this worker needs no BullMQ Redis at all, which is a large
    # part of why the remote transport is the better shape.
    redis_url = os.getenv("REDIS_URL")
    result["bullmqRedisConfigured"] = bool(redis_url)
    if not redis_url:
        problem(
            "REDIS_URL is not set. Spawning the approve step locally means THIS "
            "process enqueues the Shopify upsert, and without a reachable "
            "BullMQ Redis the approval would commit while the listing never "
            "appeared — with no error anywhere."
        )
