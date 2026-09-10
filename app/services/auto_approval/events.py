"""The operational log, worker side.

Mirrors vnyx-api's services/auto-approval/events.ts — same table, same eight
`type` values, which are exactly vnyx-ui's existing LogTag union so
LogStream.tsx renders them unchanged.

BEST EFFORT, never raising. The same treatment print-queue.ts gives its claim
events ("deliberately not awaited into the claim's critical path"): a log write
must never fail a verification.
"""
from __future__ import annotations

import logging
from typing import Any

from psycopg.types.json import Jsonb

from app.services.auto_approval import db

log = logging.getLogger("auto-approval.events")

# 'boot' | 'queue' | 'check' | 'pass' | 'hold' | 'fail' | 'warn' | 'done'
EventType = str


def emit(
    tenant_id: str,
    type_: EventType,
    message: str,
    *,
    run_id: str | None = None,
    run_product_id: str | None = None,
    product_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    try:
        db.execute(
            """
            INSERT INTO "AutoApprovalEvent"
              ("tenantId", "runId", "runProductId", "productId", type, message, detail)
            VALUES (%(t)s::uuid, %(r)s::uuid, %(rp)s::uuid, %(p)s::uuid,
                    %(ty)s, %(msg)s, %(d)s::jsonb)
            """,
            {
                "t": tenant_id,
                "r": run_id,
                "rp": run_product_id,
                "p": product_id,
                "ty": type_,
                "msg": (message or "")[:4000],
                "d": Jsonb(detail) if detail is not None else None,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a log write must never fail a run
        log.warning("event write failed: %s", exc)


def step_line(step: dict[str, Any]) -> tuple[EventType, str]:
    """One of repair()'s steps, as a log line.

    Formatted from data repair() ALREADY returns — no instrumentation inside the
    pipeline, which is what keeps "do not duplicate business logic" true for the
    log as well as for the verification.

    A step that ran and failed is a `warn`, not a `fail`: the chain continues and
    the verdict still comes from the final re-audit, so calling it a failure here
    would contradict the outcome the operator sees a moment later.
    """
    name = str(step.get("step") or "?")
    if not step.get("ran"):
        return "check", f"{name}: skipped — {step.get('why') or 'not needed'}"

    ok = step.get("ok", True)
    note = str(step.get("note") or "").strip()
    seconds = step.get("seconds")
    suffix = f" ({seconds}s)" if seconds else ""

    if not ok:
        return "warn", f"{name}: FAILED — {note or 'no detail'}{suffix}"
    return "check", f"{name}: {note or 'done'}{suffix}"
