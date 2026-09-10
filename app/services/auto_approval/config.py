"""Reading AutoApprovalConfig, and writing the worker's heartbeat.

The worker reads the RUN'S SNAPSHOT for everything that shapes a verification,
and the LIVE config only for `enabled` — which is a stop signal and must not be
stale. That split is what makes a mid-run Brain edit harmless without a lock.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from app.services.auto_approval import db


@dataclass
class RunSettings:
    """What one verification needs, read from the run's frozen snapshot."""
    mode: str                       # 'SHADOW' | 'LIVE'
    shadow_writes_repairs: bool
    use_llm: bool
    read_care_label: bool
    min_extraction_confidence: int
    infer_attributes: bool
    skip_render: bool
    skip_bin_placement: bool
    max_attempts: int
    lease_seconds: int
    rule_groups: list[str]
    severity_overrides: dict[str, str]

    @property
    def apply(self) -> bool:
        """Does this run write anything at all?

        LIVE always writes. SHADOW writes only when the operator explicitly
        turned on shadowWritesRepairs — true shadow mode writes nothing, which
        is the only honest way to compare the agent's verdicts against a
        reviewer's before letting it act.
        """
        return self.mode == "LIVE" or self.shadow_writes_repairs

    @property
    def approve(self) -> bool:
        """THE IRREVERSIBLE FLAG.

        Approving publishes a live Shopify listing and there is no undo, so only
        LIVE mode sets it — and the runner re-checks `enabled` before letting it
        through.
        """
        return self.mode == "LIVE"


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def settings_from_snapshot(snapshot: Any, fallback_max_attempts: int = 3) -> RunSettings:
    """Build RunSettings from AutoApprovalRun.configSnapshot.

    Every field is defaulted, because a snapshot written by an older version of
    the API is still a valid snapshot — and the defaults chosen here match the
    column defaults so an absent key behaves like a fresh config rather than
    like something exotic.
    """
    snap = _as_dict(snapshot)
    brain = _as_dict(snap.get("brain"))
    execution = _as_dict(snap.get("execution"))

    return RunSettings(
        mode=str(snap.get("mode") or "SHADOW"),
        shadow_writes_repairs=bool(snap.get("shadowWritesRepairs")),
        use_llm=bool(brain.get("useLlm", True)),
        read_care_label=bool(brain.get("readCareLabel", True)),
        min_extraction_confidence=int(brain.get("minExtractionConfidence") or 70),
        infer_attributes=bool(brain.get("inferAttributes")),
        skip_render=bool(brain.get("skipRender")),
        skip_bin_placement=bool(execution.get("skipBinPlacement", True)),
        max_attempts=int(execution.get("maxAttempts") or fallback_max_attempts),
        lease_seconds=int(execution.get("leaseSeconds") or 1800),
        rule_groups=list(brain.get("ruleGroups") or []),
        severity_overrides=_as_dict(brain.get("severityOverrides")),
    )


def enabled_configs() -> list[dict[str, Any]]:
    """Every tenant with the agent switched on. The sweep's scan.

    Served by @@index([enabled, scheduleEnabled]).
    """
    return db.fetch_all(
        """
        SELECT * FROM "AutoApprovalConfig"
         WHERE enabled = true
         ORDER BY "tenantId"
        """
    )


def load_config(tenant_id: str) -> dict[str, Any] | None:
    return db.fetch_one(
        'SELECT * FROM "AutoApprovalConfig" WHERE "tenantId" = %(t)s::uuid',
        {"t": tenant_id},
    )


def is_enabled(tenant_id: str) -> bool:
    """The LIVE stop signal, read fresh every time.

    Deliberately not taken from the snapshot: a stop must take effect on the
    next product, not at the end of the run.
    """
    row = db.fetch_one(
        'SELECT enabled FROM "AutoApprovalConfig" WHERE "tenantId" = %(t)s::uuid',
        {"t": tenant_id},
    )
    return bool(row and row["enabled"])


def touch_heartbeat(tenant_id: str, preflight: dict[str, Any] | None = None) -> None:
    """Record that the worker is alive, and what its host looks like.

    Without this a stopped Celery worker is indistinguishable from an idle agent
    — the exact failure mode vnyx-api's own gate logging warns about ("a
    verifier that only speaks when it finds something wrong cannot be
    distinguished from a verifier that is not running"). The config screen turns
    it into a banner.
    """
    from psycopg.types.json import Jsonb

    db.execute(
        """
        UPDATE "AutoApprovalConfig"
           SET "workerSeenAt"    = now(),
               "workerPreflight" = COALESCE(%(pf)s::jsonb, "workerPreflight"),
               "updatedAt"       = now()
         WHERE "tenantId" = %(t)s::uuid
        """,
        {"t": tenant_id, "pf": Jsonb(preflight) if preflight is not None else None},
    )


def has_open_manual_run(tenant_id: str) -> bool:
    """Is there a manual run waiting to be drained?

    The pump runs for a manual run even outside the schedule window: someone
    asked for it explicitly, and the window governs the SCHEDULED trigger, not
    an operator's button.
    """
    row = db.fetch_one(
        """
        SELECT 1 AS x FROM "AutoApprovalRun"
         WHERE "tenantId" = %(t)s::uuid
           AND status = 'RUNNING'::"AutoApprovalRunStatus"
           AND source IN ('MANUAL_SINGLE'::"AutoApprovalRunSource",
                          'MANUAL_DATE_RANGE'::"AutoApprovalRunSource",
                          'MANUAL_FULL_REVIEW'::"AutoApprovalRunSource",
                          'MANUAL_RETRY'::"AutoApprovalRunSource")
         LIMIT 1
        """,
        {"t": tenant_id},
    )
    return row is not None


def find_open_run(tenant_id: str, source: str) -> dict[str, Any] | None:
    return db.fetch_one(
        """
        SELECT id, "configSnapshot" FROM "AutoApprovalRun"
         WHERE "tenantId" = %(t)s::uuid
           AND source = %(s)s::"AutoApprovalRunSource"
           AND status = 'RUNNING'::"AutoApprovalRunStatus"
         ORDER BY "startedAt" DESC
         LIMIT 1
        """,
        {"t": tenant_id, "s": source},
    )
