"""The three Celery tasks.

Names are stable strings (`aa.sweep`, `aa.pump`, `aa.verify_one`) rather than
module paths, so moving this file does not orphan a queued tick or a beat entry.
"""
from __future__ import annotations

import logging
from typing import Any

from app.celery_app import app
from app.services.auto_approval import claim, config as cfgmod, db, runner, sweep as sweepmod

log = logging.getLogger("auto-approval.tasks")


@app.task(name="aa.sweep", ignore_result=True)
def sweep_task() -> dict[str, Any]:
    """Every 60s: evaluate each enabled tenant's window, dispatch pumps, reap.

    Short by design — a handful of indexed queries — so it never blocks the
    single worker slot behind a 21-minute product.
    """
    summary = sweepmod.sweep()
    for tenant_id in summary.get("dispatched", []):
        # A deterministic-ish key is deliberately NOT used here. Celery has no
        # equivalent of BullMQ's jobId coalescing, so a second pump for the same
        # tenant is possible — and harmless: it re-runs claim_next, which the
        # advisory lock and the one-in-progress index both refuse. That is the
        # payoff for putting no work identity on the broker.
        pump_task.delay(tenant_id)
    return summary


@app.task(name="aa.pump", ignore_result=True)
def pump_task(tenant_id: str) -> dict[str, Any]:
    """Drain one tenant's queue, one product at a time.

    CARRIES NO WORK IDENTITY — only a tenant id. A stale, duplicated or replayed
    tick is therefore harmless: it re-runs claim_next(), which is safe to call
    any number of times. Putting product ids on the broker would reintroduce
    every duplicate-delivery problem the table exists to remove.
    """
    if not sweepmod.preflight_result()["ok"]:
        # The sweep has already emitted the warn event with the detail.
        return {"tenantId": tenant_id, "processed": 0, "stopped": "preflight"}
    return runner.pump(tenant_id)


@app.task(name="aa.verify_one", bind=True)
def verify_one_task(self, run_product_id: str) -> dict[str, Any]:
    """Re-run one queue row by hand. An operator tool.

    The NORMAL path is sweep -> pump -> claim_next -> verify_one (the plain
    function), not this task: the claim has to happen in the worker that will do
    the work, or the lease belongs to nobody.

    This exists for the case where a row is already IN_PROGRESS with a live
    lease — a stuck product an operator wants to push — and it deliberately does
    NOT claim, so it cannot steal a row from a healthy worker.
    """
    row = db.fetch_one(
        'SELECT * FROM "AutoApprovalRunProduct" WHERE id = %(id)s::uuid',
        {"id": run_product_id},
    )
    if row is None:
        return {"ok": False, "error": "no such queue row"}
    if row["status"] != "IN_PROGRESS":
        return {
            "ok": False,
            "error": (
                f"row is {row['status']}, not IN_PROGRESS — let the pump claim "
                f"it instead of running it out of band"
            ),
        }

    run = db.fetch_one(
        'SELECT "configSnapshot" FROM "AutoApprovalRun" WHERE id = %(r)s::uuid',
        {"r": row["runId"]},
    )
    cfg_row = cfgmod.load_config(str(row["tenantId"]))
    rs = cfgmod.settings_from_snapshot(
        run and run["configSnapshot"],
        int(cfg_row["maxAttempts"]) if cfg_row else 3,
    )
    runner.verify_one(row, rs)
    claim.close_finished_runs()
    return {"ok": True, "runProductId": run_product_id}
