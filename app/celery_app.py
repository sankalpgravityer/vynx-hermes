"""Celery for the Auto Approval Agent.

WHY CELERY AND NOT FastAPI BackgroundTasks. /v1/batch already uses those and
they die with the process: a redeploy mid-sweep loses the run and nothing
resumes it. The agent has to survive a restart, so the work item has to live
somewhere durable and something has to keep asking for it.

WHY THE QUEUE IS STILL POSTGRES. The broker carries a TICK, never a product id.
Redis therefore holds no backlog, so a flush loses nothing; two consumers cannot
be handed the same product because they are not handed products at all; and
"release every queued product" is one UPDATE rather than a broker-scanning
exercise. It is also the only shape that works across two languages — vnyx-api
enqueues through Prisma and cannot reach a Celery queue.

Run it:
    celery -A app.celery_app worker --loglevel=info --concurrency=1 -Q auto-approval
    celery -A app.celery_app beat   --loglevel=info
"""
from __future__ import annotations

import os
import sys

from celery import Celery
from celery.schedules import schedule as celery_schedule

# .env BEFORE the os.getenv calls below.
#
# app/config.py loads it too, but that module is imported lazily by the task
# modules — long after this file has already read the broker URL. Without this,
# `celery -A app.celery_app worker` with no shell variables set would silently
# use the hardcoded defaults rather than the file the rest of Hermes reads.
#
# NON-OVERRIDING, which is the whole reason it is safe: a variable already in
# the environment wins, so scripts/run-worker.ps1 can point a one-off run at a
# different database without editing .env.
try:
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))
except ImportError:  # python-dotenv is optional, as it is in app/config.py
    pass

# THE REPO ROOT ON sys.path, and this is not optional.
#
# `celery` is an installed console script, so sys.path[0] is the venv's Scripts
# directory rather than the repo root. `scripts/` has no __init__.py either, so
# it resolves only as a namespace package — and only when its PARENT is
# importable. Without this, the worker starts, claims a product, and dies on
# `from scripts.repair_product import repair`.
#
# It works under `python -c` from the repo because cwd is on the path there,
# which is exactly why this failed only under Celery.
#
# repair_product.py already does the same thing one level down for its own
# `from app import product_audit`.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

SWEEP_SECONDS = int(os.getenv("AUTO_APPROVAL_SWEEP_SECONDS", "60"))

app = Celery(
    "hermes",
    broker=os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1"),
    # Only aa.verify_one (the ad-hoc single-product task) has a result anyone
    # reads; the sweep and pump are fire-and-forget.
    backend=os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2"),
    include=["app.tasks.auto_approval"],
)

app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Windows has no fork(), so the default prefork pool cannot start. The solo
    # pool is also exactly what this worker wants everywhere: one product at a
    # time is the requirement, not a limitation.
    worker_pool=os.getenv("CELERY_WORKER_POOL", "solo"),
    # ONE. The per-tenant invariant is already a partial unique index, but a
    # concurrency of 1 keeps the common case out of that error path — and it
    # matters because repair() forks `npx tsx` subprocesses that each open their
    # own Prisma pool and can call a paid image model. Several at once would push
    # past the provider's per-minute limits, which vnyx-api's
    # ANALYZE_CONCURRENCY = 5 is the only other protection for.
    worker_concurrency=1,
    # PREFETCH 1. The default of 4 would reserve three more ticks behind a
    # 21-minute product, so a stop request would wait for all four.
    worker_prefetch_multiplier=1,
    # ACKS LATE. A worker killed mid-product must not have acknowledged its
    # tick. Safe because every task is idempotent: the pump re-claims from the
    # table, and a redelivered tick finds the row already IN_PROGRESS or
    # terminal.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # LIMITS ABOVE THE WORST CASE. repair() is 20-80s typical and ~21 minutes
    # with five renders (300s approve + 5 x 200s). The SOFT limit raises
    # SoftTimeLimitExceeded INSIDE the task, so it can release its lease and
    # record a FAILED status instead of vanishing and waiting for the reaper.
    task_soft_time_limit=30 * 60,
    task_time_limit=35 * 60,
    task_default_queue="auto-approval",
    # Celery 6 changes what broker_connection_retry means at startup; setting
    # this now keeps today's behaviour and silences the pending-deprecation
    # warning that otherwise prints twice on every boot.
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "auto-approval-sweep": {
            "task": "aa.sweep",
            "schedule": celery_schedule(run_every=SWEEP_SECONDS),
            # Ticks are worthless once late — a missed one is replaced by the
            # next, and replaying a backlog of them at startup would stack
            # pumps against the same tenant.
            "options": {"expires": max(SWEEP_SECONDS - 5, 10)},
        },
    },
)
