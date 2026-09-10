"""Postgres access for the Auto Approval worker.

WHY THIS TALKS TO POSTGRES DIRECTLY. The queue is a table
(`AutoApprovalRunProduct`) rather than a message broker, and vnyx-api writes it
through Prisma while this worker claims it through psycopg. Neither side owns
it, which is why every invariant is a database constraint rather than
application code — see the two partial unique indexes in the migration.

That also means the worker does NOT go through vnyx-api's HTTP API for queue
purposes: there is no service credential (a static VNYX_API_TOKEN "expires
within the hour and the writes start failing quietly"), and a network round trip
inside the claim would reopen the race the single-statement CTE closes.

TENANT SAFETY. `repair()` takes a raw DSN and performs no tenant scoping — the
trade app/main.py:358 already names for /v1/product-audit. Three things contain
it:

  1. the worker only ever acts on a product id it CLAIMED from the queue, and
     every row there was written by vnyx-api after resolveTenantScope;
  2. `claim_next` returns the row's tenantId and `verify_one` re-reads the
     product's own tenantId and refuses a mismatch (see assert_tenant);
  3. the deployment should give this worker a column-scoped database role — it
     needs INSERT/UPDATE on three tables and UPDATE on five Product columns,
     nothing more. The subprocess steps use vnyx-api's own connection for
     everything else.
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.rows import dict_row

from app import product_audit
from app.config import settings


def dsn() -> str:
    """The database the whole feature runs against.

    Same resolution order as the rest of Hermes: an explicit env var wins, then
    whatever `settings()` loaded from .env. Raises rather than silently falling
    back to a default, because "which database" is the one thing this worker
    must never guess.
    """
    url = os.getenv("DATABASE_URL") or settings().database_url
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. The Auto Approval worker reads and writes "
            "the queue directly, so it cannot start without one."
        )
    return url


@contextmanager
def connection(*, read_only: bool = False, statement_timeout_s: int = 60) -> Iterator[Any]:
    """A writable (or read-only) connection, closed on exit.

    Reuses product_audit.connect rather than opening one a second way: it
    already sets the statement timeout and an identifying application_name, so a
    runaway query on a busy box shows up as `hermes-product-audit` in
    pg_stat_activity instead of as an anonymous connection.

    THE SESSION IS PINNED TO UTC, and this is not cosmetic.

    Every timestamp column this feature touches is `timestamp WITHOUT time zone`,
    and it has two writers that disagreed about which clock to store:

      * Prisma Client materialises `@default(now())` ITSELF and sends a JS Date,
        which is always UTC. So every vnyx-api-written row is UTC.
      * this worker writes `now()`, which Postgres resolves against the SESSION
        timezone. On a developer box that is local time.

    Mixing them makes any interval across the two sides wrong by the host's UTC
    offset. It surfaced as run durations of "5h 34m" for single-product runs on
    an IST machine: `AutoApprovalRun.startedAt` (Prisma, UTC) subtracted from
    `completedAt` (this worker, local) is exactly +5:30 of nothing.

    It reads as a display bug and mostly is one — the claim, the lease and the
    backoff all compare `now()` against columns `now()` wrote, so they stay
    self-consistent and were never at risk. But `Product.verificationCheckedAt`
    and every `completedAt` are read by vnyx-api and rendered to a user, so the
    two sides have to agree, and UTC is the only defensible choice for a naive
    column.

    Pinned in `product_audit.connect` as a connection OPTION rather than with
    `SET TIME ZONE` here: plain SET is transactional, so a rolled-back
    transaction would quietly restore the local clock and the skew would return
    intermittently. It is set per connection rather than left to the server
    because it must hold whoever's Postgres this is — a database that happens to
    run in UTC would otherwise hide the bug until someone moved it.
    """
    conn = product_audit.connect(
        dsn(), read_only=read_only, statement_timeout_s=statement_timeout_s
    )
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def cursor(*, read_only: bool = False, statement_timeout_s: int = 60) -> Iterator[Any]:
    """A dict-row cursor on its own connection, committed on clean exit."""
    with connection(read_only=read_only, statement_timeout_s=statement_timeout_s) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur
        if not read_only:
            conn.commit()


def fetch_all(sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    with cursor(read_only=True) as cur:
        cur.execute(sql, params or {})
        return list(cur.fetchall())


def fetch_one(sql: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
    with cursor(read_only=True) as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()


def execute(sql: str, params: dict[str, Any] | None = None) -> int:
    with cursor() as cur:
        cur.execute(sql, params or {})
        return cur.rowcount


def write_returning(
    sql: str, params: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """A WRITE that hands a row back — INSERT ... RETURNING.

    Separate from fetch_one on purpose. product_audit.connect enforces
    `read_only` in POSTGRES rather than by convention, so calling fetch_one for
    an INSERT raises ReadOnlySqlTransaction instead of quietly working — which
    is the point of that design, and the reason this needs its own function
    rather than a flag on the read path.
    """
    with cursor() as cur:
        cur.execute(sql, params or {})
        return cur.fetchone()


def assert_tenant(cur, run_product_id: str, tenant_id: str, product_id: str) -> bool:
    """Does the product actually belong to the tenant the queue row claims?

    Cheap, and it turns a cross-tenant write — the one thing the worker's
    unscoped database access could cause — into a recorded refusal rather than a
    silent one. Should never fire; if it does, something wrote the queue outside
    vnyx-api's scoping.
    """
    cur.execute(
        'SELECT "tenantId" FROM "Product" WHERE id = %(pid)s::uuid',
        {"pid": product_id},
    )
    row = cur.fetchone()
    if row is None:
        return False
    actual = str(row["tenantId"] if isinstance(row, dict) else row[0])
    return actual == str(tenant_id)
