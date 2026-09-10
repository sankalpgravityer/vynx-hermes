# Auto Approval Agent — running it locally

Five processes, one database, one Redis. Everything below targets the **local**
database only:

```
postgresql://postgres:root@localhost:5433/vnyx-dev-2
```

Never the remote dev or prod database. Hermes' `.env` already points here, and
the worker's preflight refuses to start unless `DATABASE_URL` is set explicitly
in the environment — so it cannot inherit a different one by accident.

---

## 0 · Prerequisites

Checked on this machine already, but verify after a reboot:

| Requirement | Check | Status here |
|---|---|---|
| PostgreSQL on **5433** with `vnyx-dev-2` | `psql -h localhost -p 5433 -U postgres -d vnyx-dev-2 -c 'select 1'` | ✅ 114 migrations applied |
| Redis on **6379** | `python -c "import redis;redis.Redis(port=6379).ping()"` | ✅ db1 + db2 up |
| Hermes venv | `.venv/Scripts/python.exe -c "import celery,redis,psycopg,httpx"` | ✅ celery 5.4.0 |
| **The two matching secrets** | `AUTO_APPROVAL_INTERNAL_SECRET` is the same in `hermes/.env` and `vnyx-api/.env` | ✅ |
| Node ≥ 18 with `npx` | `node --version && npx --version` | ✅ v22.14.0 / 10.9.2 — for **vnyx-api**, not the worker |
| `vnyx-api` deps installed | `test -d E:/vnyx/vnyx-api/node_modules` | ✅ |
| The six repair scripts | `ls E:/vnyx/vnyx-api/scripts/{approve-products,backfill-bg-removal,backfill-product-data,relabel-ai-views,backfill-imagery,verify-and-repair}.ts` | ✅ |
| `vnyx-ui` deps installed | `test -d E:/vnyx/vnyx-ui/node_modules` | ✅ |

**The worker calls vnyx-api for every repair step.** It no longer spawns them
here, so the backend on **:8000** is a hard dependency of the worker — start
terminal 1 before terminal 3. The upside is that this machine runs the same
shape as the Ubuntu host: no Node, no checkout and no R2/OpenAI keys are needed
*by the worker*, only by vnyx-api, which already has them.

**Redis has three databases here, and the worker needs only two of them:**

| Env var | Points at | Whose | Needed by the worker? |
|---|---|---|---|
| `CELERY_BROKER_URL` → `/1` | the agent's tick | Hermes' | Yes |
| `CELERY_RESULT_BACKEND` → `/2` | ad-hoc task results | Hermes' | Yes |
| `REDIS_URL` → `/0` | vnyx-api's BullMQ queues | **vnyx-api's** | **No — commented out** |

`REDIS_URL` used to be load-bearing and silent when wrong: the worker spawned
the approve step, which enqueues the Shopify upsert onto a BullMQ queue whose
only consumer lives inside the vnyx-api process — so an unreachable value meant
**approvals committed and listings never published, with no error anywhere.**
That step now runs *inside* vnyx-api, which enqueues onto its own queue using
the connection it already holds. The failure mode is not mitigated; it is
unreachable from here.

---

## 1 · The five processes

**PowerShell, not bash.** `VAR=value command` sets nothing in PowerShell — it is
bash syntax. Everything the worker needs now lives in Hermes' `.env`
(`VNYX_API_URL`, `AUTO_APPROVAL_INTERNAL_SECRET`, `CELERY_BROKER_URL`,
`CELERY_RESULT_BACKEND`, `HERMES_BASE_URL`), and `DATABASE_URL` there already
points at the local database — so the commands below need no variables at all.

A shell variable still wins if you set one: python-dotenv does not override an
already-set value, which is what lets `run-worker.ps1` point a one-off run
somewhere else without editing the file.

> **Use forward slashes in `.env`.** dotenv processes backslash escapes, so a
> Windows path written with backslashes does not survive it: a backslash
> followed by `v` becomes a vertical tab, and the value silently resolves to a
> directory that does not exist. The preflight then reports
> `VNYX_API_DIR does not exist` naming a string that looks nothing like what
> you typed. Write `VNYX_API_DIR=E:/vnyx/vnyx-api` — Windows accepts forward
> slashes everywhere that matters here.

> **`VNYX_API_DIR` is the local-dev transport, not the deployment one.** This
> guide runs everything on one machine, so the worker spawns vnyx-api's repair
> scripts itself with `npx tsx` — which is why it needs the checkout, its
> `node_modules` and vnyx-api's credentials.
>
> A deployed Hermes host sets `VNYX_API_URL` + `AUTO_APPROVAL_INTERNAL_SECRET`
> instead, and the steps run on the vnyx-api host over one authenticated HTTP
> call each. That host then needs no Node, no checkout, and neither R2 nor
> OpenAI nor BullMQ credentials. Setting the URL takes precedence over the
> directory, so you can point this same machine at a remote runner by
> uncommenting two lines in `.env`. See
> [DEPLOY-UBUNTU-WORKER.md](DEPLOY-UBUNTU-WORKER.md).

### Terminal 1 — vnyx-api (backend, :4000)

```powershell
cd E:\vnyx\vnyx-api
yarn dev
```

Serves the API **and** every BullMQ worker in one process — including the
shopify-sync worker that drains what an approval enqueues. Swagger at
`http://localhost:4000/docs`.

### Terminal 2 — Hermes FastAPI (rule service, :8080)

```powershell
cd "E:\hermes local\files (1)\hermes\hermes"
.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8080
```

Check it: `curl http://localhost:8080/healthz`. The `rule_groups` in that
response is what populates the Brain screen's rule list.

### Terminal 3 — Celery worker (does the verifying)

```powershell
cd "E:\hermes local\files (1)\hermes\hermes"
.\scripts\run-worker.ps1
```

The script sets the environment, asserts Node and the six scripts are present,
and prints the database it resolved before starting — so a worker pointed at the
wrong database is visible immediately rather than after it writes something.

Bare `celery -A app.celery_app worker --pool=solo -Q auto-approval` also works
now that `.env` carries the variables. `--pool=solo` is required: Windows has no
`fork()`, so the default prefork pool cannot start. It is also what this worker
wants anyway — one product at a time is the requirement, not a limitation.

A clean boot logs:

```
worker preflight ok — database localhost:5433/vnyx-dev-2, vnyx-api E:/vnyx/vnyx-api
```

### Terminal 4 — Celery beat (the 60-second tick)

```powershell
cd "E:\hermes local\files (1)\hermes\hermes"
.\scripts\run-beat.ps1
```

**Exactly one beat process, ever.** Two would double every tick. Nothing enforces
that, which is why the window-edge check asks "does a RUNNING run exist" rather
than comparing timestamps — a doubled tick is made harmless, not impossible.

### Terminal 5 — vnyx-ui (frontend, :3000)

```powershell
cd E:\vnyx\vnyx-ui
yarn dev
```

`.env.local` must have `NEXT_PUBLIC_API_URL=http://localhost:4000`.

## 2 · Checking it on the frontend

Go to **`/agents/auto-approval`**. Four tabs: Overview, Brain, Schedule, Runs.
(Memory is gone from the nav — it had no backend.)

### What is already set up for you

The **Bever** tenant (`34c354a5-…`) is enabled in the database, in **SHADOW**
mode with renders skipped, and **3 products are already queued**. Pick Bever in
the tenant switcher.

- **SHADOW** means the agent verifies and records a verdict and **writes
  nothing** — no repairs, no approvals, no Shopify. That is the right place to
  start.
- **renders skipped** means it will not spend money on image generation.

So the moment the worker starts, it should pick up those three products.

### The order to look at things

1. **Overview** — the switch should read *Automation enabled*, and the header
   pill should say **Armed**. If it says **Worker down**, terminal 3 is not
   running. If it says **Rules unreachable**, terminal 2 is not.
2. Within 60 seconds the worker claims the first product. The header goes to
   **Verifying**, a card appears naming the SKU and which of the seven steps it
   is on, and the **Live log** starts filling — one line per step, polled every
   5 seconds.
3. **Only one product is ever in flight.** That is enforced by a partial unique
   index, not by the worker: `UNIQUE ("tenantId") WHERE status='IN_PROGRESS'`.
4. **Runs** — the run appears with its counters. Click it: the drawer shows each
   product, its verdict, *and its step timeline* — which of the seven steps ran,
   which were skipped and why, and what changed.
5. **Brain** — the rule groups are read from Hermes' `/healthz`, so this list is
   live rather than hardcoded. Toggle one off, press Save Brain, and the next run
   will skip it. The 21 approve-preflight checks are shown read-only below.
6. **Schedule** — the window is currently `00:00 → 00:00`, which the design
   treats as **24 hours** (someone setting that means "always"). Set a real
   window to see it open and close.

### Try the manual scopes

**Overview → Run now** gives you the three scopes. Start with **Single** and a
product UUID:

```bash
PGPASSWORD=root psql -h localhost -p 5433 -U postgres -d vnyx-dev-2 -t -c \
"SELECT id, sku FROM \"Product\"
  WHERE \"tenantId\"='34c354a5-3415-4513-85b8-d40c2ec3af7e'
    AND \"currentStage\"='REVIEW' AND \"verificationStatus\"='NOT_STARTED' LIMIT 3;"
```

A run that enqueues nothing is still recorded, so "why did nothing happen?" is
answerable from the Runs tab rather than being invisible.

### Try Start on Arrival

With the switch on, move a product into Review and it should be queued within a
second of the transition:

```bash
cd E:/vnyx/vnyx-api
DATABASE_URL="postgresql://postgres:root@localhost:5433/vnyx-dev-2" \
npx tsx -e "
const { prisma } = await import('./src/db/prisma.js');
const { updateProduct } = await import('./src/services/products.js');
// A product currently OUT of Review, moved back in. reviewStatus is what the
// stage derives from — writing currentStage directly would not fire the hook.
await updateProduct('<some-product-id>', { reviewStatus: 'PENDING' }, null);
await prisma.\$disconnect(); process.exit(0);
"
```

Products **already sitting** in Review are deliberately never swept this way —
the hook fires only on a transition *into* Review. Use Run now for the backlog.

---

## 3 · Testing the price fix by hand

This is worth doing on its own, because it is the thing that was broken. The old
chain planned a price repair and then refused to write it.

```bash
cd "E:/hermes local/files (1)/hermes/hermes"
# LOOK FIRST — nothing written, no model called.
.venv/Scripts/python.exe scripts/repair_product.py \
  --db "postgresql://postgres:root@localhost:5433/vnyx-dev-2" \
  --vnyx-api "E:/vnyx/vnyx-api" \
  --product <product-id> --no-render --no-sheet

# DO IT — repairs, still does not approve.
.venv/Scripts/python.exe scripts/repair_product.py \
  --db "postgresql://postgres:root@localhost:5433/vnyx-dev-2" \
  --vnyx-api "E:/vnyx/vnyx-api" \
  --product <product-id> --no-render --no-sheet --apply
```

Watch the `reconcile` step. It now shells out to `verify-and-repair.ts`, so a
`price` in its applied list is the fix working. Terminal 2 must be up — that step
calls `/v1/approval-gate`.

`--approve` is deliberately separate and **publishes a live Shopify listing**.
Leave it off locally unless that is what you mean to test.

---

## 4 · When something looks wrong

| Symptom | Cause |
|---|---|
| Header says **Worker down** | Terminal 3 is not running, or has not ticked in 3 minutes |
| Header says **Rules unreachable** | Terminal 2 is not running |
| Red banner naming Node or `node_modules` | The worker's preflight failed — it refuses to pump rather than failing every product at step 1 |
| Nothing queues, no error | Check the tenant. Config is per tenant, and only Bever is enabled |
| Products queue but never start | Beat (terminal 4) is not running, so no tick is dispatched |
| Every product fails at the `reconcile` step | Terminal 2 is down, or `VNYX_API_DIR` is wrong |
| Approvals happen but nothing reaches Shopify | `REDIS_URL` is missing from terminal 3 — see §0 |
| Banner says the step runner rejected the secret | Remote transport: `AUTO_APPROVAL_INTERNAL_SECRET` differs between the two hosts |
| Banner says **Database mismatch** | Remote transport: the two hosts point at different databases. The DSN is never sent over the wire, so this would otherwise verify in one database and repair in another |

Useful queries:

```sql
-- the queue
SELECT "productSku", status, attempts, "availableAt", "leaseExpiresAt"
  FROM "AutoApprovalRunProduct" ORDER BY priority DESC, "createdAt";

-- the log
SELECT id, type, message FROM "AutoApprovalEvent" ORDER BY id DESC LIMIT 30;

-- who is enabled
SELECT "tenantId", enabled, mode, "startOnArrival", "scheduleEnabled",
       "workerSeenAt", "workerPreflight"->'ok' AS preflight_ok
  FROM "AutoApprovalConfig";
```

### Resetting between tests

```sql
-- Put everything back to NOT_STARTED and clear the history.
UPDATE "Product" SET "verificationStatus"='NOT_STARTED', "verificationOutcome"=NULL,
       "verificationReason"=NULL, "verificationRunId"=NULL, "verificationCheckedAt"=NULL
 WHERE "verificationStatus" <> 'NOT_STARTED';
DELETE FROM "AutoApprovalEvent";
DELETE FROM "AutoApprovalRunProduct";
DELETE FROM "AutoApprovalRun";
```

---

## 5 · Going LIVE (do this last, and deliberately)

`SHADOW → LIVE` is what lets the agent approve products, and approving
**publishes a live Shopify listing with no undo** — a listing has to be removed
on Shopify's side. The UI confirms first and the API independently requires a
Super Admin.

The safe ramp, one side effect at a time:

1. `mode=SHADOW`, `shadowWritesRepairs=false`, `skipRender=true` — verify only.
   **You are here.**
2. `shadowWritesRepairs=true` — the free database repairs land. Watch the price
   repairs specifically.
3. `skipRender=false` — now it costs money. Watch the render count per product.
4. `mode=LIVE` — now it publishes.

Each step adds exactly one kind of side effect, so anything that goes wrong is
attributable.
