# Deploying the Auto Approval worker on Ubuntu

For a Hermes host that has **no vnyx-api checkout**. You already run the FastAPI
service under systemd; this adds Redis and two Celery units beside it.

---

## What this host needs, and what it does not

| Needs | Does **not** need |
|---|---|
| Python venv with `celery[redis]`, `redis`, `psycopg` | Node.js |
| `DATABASE_URL` — the same database vnyx-api uses | A vnyx-api checkout or its `node_modules` |
| Redis, local, for the Celery broker | R2 credentials |
| `VNYX_API_URL` — where vnyx-api serves | OpenAI / Bedrock / background-removal keys |
| `AUTO_APPROVAL_INTERNAL_SECRET` — shared with vnyx-api | vnyx-api's BullMQ Redis |
| The Hermes checkout **including `scripts/`** | |

That short right-hand column is the point of the remote transport. `repair()`
still runs vnyx-api's own scripts — it must, because every write it causes lands
on an invariant vnyx-api owns — but it now asks the vnyx-api host to run them
instead of spawning them here:

```
Hermes host                              vnyx-api host
-----------                              -------------
celery worker
  repair_product.repair()
    step 1 matte      ── HTTPS ──▶  POST /internal/auto-approval/step
    step 2 relabel                    └─ npx tsx scripts/<step>.ts
    step 3 extract                       (checkout, node_modules, R2,
    step 4 care label  (local Python)     OpenAI, BullMQ all live HERE)
    step 5 reconcile  ── HTTPS ──▶
    step 6 render     ── HTTPS ──▶
    step 7 approve    ── HTTPS ──▶
  claim / status / log ── psycopg ──▶  PostgreSQL (shared)
```

`VNYX_API_DIR` is now only for a single-box developer setup. **Remove it from
this host's environment** — with `VNYX_API_URL` set it is ignored anyway, but
leaving it invites someone to believe a checkout is expected.

> **Both hosts must use the same database.** The connection string is
> deliberately never sent over the wire — the step runner uses its own
> `DATABASE_URL` — so a mismatch would not fail, it would quietly verify
> products in one database and repair them in another. The worker's preflight
> compares the two at boot and refuses to start on a mismatch.

---

## 1 · vnyx-api side (one variable)

On the vnyx-api host, add to its environment and restart it:

```bash
AUTO_APPROVAL_INTERNAL_SECRET=$(openssl rand -hex 32)
```

Then verify the endpoint exists:

```bash
curl -s -H "x-internal-secret: $AUTO_APPROVAL_INTERNAL_SECRET" \
  https://api-dev.example.com/internal/auto-approval/ping
# {"ok":true,"database":"…","steps":[…6…],"missingScripts":[]}

curl -s -o /dev/null -w '%{http_code}\n' \
  https://api-dev.example.com/internal/auto-approval/ping
# 401  — no secret, correctly refused
```

**`/internal` must not be reachable from the internet.** The shared secret is
the second lock, not the only one. In nginx, in front of vnyx-api:

```nginx
location /internal/ {
    allow 10.0.0.0/8;          # your private network / the Hermes host
    deny  all;
    proxy_pass http://127.0.0.1:4000;
    # The render step legitimately takes ~30 minutes: five views at ~200s each
    # plus the matting pass. The default 60s proxy read timeout would abort it
    # and the worker would record a step failure for work that was succeeding.
    proxy_read_timeout 2100s;
    proxy_send_timeout 2100s;
}
```

If the two hosts are not on a shared private network, put this behind a VPN or
an mTLS-terminating proxy. A shared secret over TLS is acceptable on a private
network; it is not a substitute for one.

---

## 2 · Redis

```bash
sudo apt update && sudo apt install -y redis-server
```

Ubuntu's package already ships a `redis-server.service` and enables it. Two
things worth setting in `/etc/redis/redis.conf`:

```conf
# Loopback only. The broker carries ticks that name a tenant, but there is no
# reason for it to be reachable off-box.
bind 127.0.0.1 -::1

# Survive a reboot with the beat schedule intact. Losing the broker loses no
# WORK — the queue is a Postgres table, which is the whole point of the design —
# but a persisted broker means a restart does not drop an in-flight tick.
appendonly yes
```

```bash
sudo systemctl enable --now redis-server
redis-cli ping        # PONG
```

Databases `1` and `2` are used (broker and results). Nothing else on this host
should use them.

---

## 3 · Environment file

The units below read one file, so the secrets are not in unit files that
`systemctl cat` prints for any user.

```bash
sudo install -o root -g hermes -m 0640 /dev/null /etc/hermes/auto-approval.env
sudo -e /etc/hermes/auto-approval.env
```

```ini
# The SAME database vnyx-api uses. The preflight refuses to start on a mismatch.
DATABASE_URL=postgresql://user:pass@db-host:5432/vnyx

# Where the repair steps run. Setting this selects the remote transport, so this
# host needs no Node and no vnyx-api checkout.
VNYX_API_URL=https://api-dev.example.com
AUTO_APPROVAL_INTERNAL_SECRET=<the same value as on the vnyx-api host>

# Celery's own broker and result backend. Local Redis, dbs 1 and 2.
CELERY_BROKER_URL=redis://127.0.0.1:6379/1
CELERY_RESULT_BACKEND=redis://127.0.0.1:6379/2

# The FastAPI service on this host. Two steps call it — the rule verdict and the
# renders — so loopback is correct.
HERMES_BASE_URL=http://127.0.0.1:8080

# How often the sweep evaluates every enabled tenant's schedule window.
AUTO_APPROVAL_SWEEP_SECONDS=60
# How long one pump may hold the single worker slot before yielding.
AUTO_APPROVAL_PUMP_BUDGET_S=900

# NOT set on this host, deliberately:
#   VNYX_API_DIR  — only for a single-box developer setup
#   REDIS_URL     — vnyx-api's BullMQ. The approve step enqueues the Shopify
#                   upsert from the vnyx-api process now, so this worker has no
#                   business reaching that Redis.
```

---

## 4 · The two systemd units

### `/etc/systemd/system/hermes-worker.service`

```ini
[Unit]
Description=Hermes Auto Approval worker
# Wants, not Requires: the queue is a Postgres table, so the worker starting
# before Redis is ready is a retry, not a failure.
Wants=redis-server.service network-online.target
After=redis-server.service network-online.target

[Service]
Type=simple
User=hermes
Group=hermes
WorkingDirectory=/srv/hermes
EnvironmentFile=/etc/hermes/auto-approval.env

# --pool=solo, and this is a design choice rather than a limitation: ONE product
# at a time is the requirement. The per-tenant invariant is a partial unique
# index in Postgres, so raising this would not break correctness — it would just
# start losing races against that index, and put several paid render jobs in
# flight at once.
ExecStart=/srv/hermes/.venv/bin/celery -A app.celery_app worker \
  --loglevel=info --pool=solo --concurrency=1 -Q auto-approval

Restart=always
RestartSec=10

# LONGER THAN THE SOFT TIME LIMIT (30 min). Celery handles SIGTERM by finishing
# the current task before exiting; a shorter stop timeout would SIGKILL a worker
# mid-product, stranding the row until the lease reaper noticed and turning
# every deploy into a 30-minute wait.
KillSignal=SIGTERM
TimeoutStopSec=2100

# The worker writes nothing outside its venv and /tmp.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/hermes

[Install]
WantedBy=multi-user.target
```

### `/etc/systemd/system/hermes-beat.service`

```ini
[Unit]
Description=Hermes Auto Approval scheduler (celery beat)
Wants=redis-server.service network-online.target
After=redis-server.service network-online.target

[Service]
Type=simple
User=hermes
Group=hermes
WorkingDirectory=/srv/hermes
EnvironmentFile=/etc/hermes/auto-approval.env

# EXACTLY ONE OF THESE, EVER. Two beat processes double every tick: two pumps a
# minute and two scheduled runs at a window edge. systemd will not start a
# second copy of a named unit, which is most of the protection; the design makes
# a doubled tick HARMLESS rather than impossible (the window-edge check asks
# "does a RUNNING run exist" instead of comparing timestamps), so do not template
# this unit or add an instance.
ExecStart=/srv/hermes/.venv/bin/celery -A app.celery_app beat \
  --loglevel=info --schedule=/var/lib/hermes/celerybeat-schedule

Restart=always
RestartSec=10

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
# The schedule file records the last tick so a restart does not immediately
# refire one.
ReadWritePaths=/srv/hermes /var/lib/hermes

[Install]
WantedBy=multi-user.target
```

```bash
sudo mkdir -p /var/lib/hermes && sudo chown hermes:hermes /var/lib/hermes
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-worker hermes-beat
```

---

## 5 · Verify

```bash
# The worker's own assertions, at boot. This is the line to look for:
sudo journalctl -u hermes-worker -n 40 --no-pager | grep preflight
#   worker preflight ok — database db-host:5432/vnyx, vnyx-api https://api-dev…
```

A failure names exactly what is wrong and the worker **refuses to pump** rather
than failing every product one at a time:

| Journal line | Fix |
|---|---|
| `Cannot reach the step runner at …` | nginx `/internal` allow-list, or the URL |
| `rejected the shared secret` | `AUTO_APPROVAL_INTERNAL_SECRET` differs between hosts |
| `has no AUTO_APPROVAL_INTERNAL_SECRET configured` | Set it on the **vnyx-api** host and restart it |
| `Database mismatch: … uses X but … uses Y` | Point both at the same database |
| `DATABASE_URL is not set in the environment` | It must be in the env file, not just Hermes' `.env` |
| `missing script(s)` | The vnyx-api host is on a revision without them — `scripts/verify-and-repair.ts` especially |

Then the beat tick:

```bash
sudo journalctl -u hermes-beat -f
# Scheduler: Sending due task auto-approval-sweep (aa.sweep)
```

And end to end, from the app: enable the agent for one tenant in **SHADOW**
mode, and within a minute the Overview screen should show the worker as running
and start filling its live log.

---

## 6 · Operating it

```bash
# Stop processing. Queued products stay queued — the queue is a table.
sudo systemctl stop hermes-beat hermes-worker

# Deploy: warm shutdown, so the in-flight product finishes first.
sudo systemctl restart hermes-worker      # may take up to 30 min to stop
sudo systemctl restart hermes-beat        # immediate

# Watch one product go through.
sudo journalctl -u hermes-worker -f | grep -E 'aa.pump|preflight|step'
```

**Stopping the units is not the same as stopping the agent.** Stopping systemd
leaves queued rows queued and the tenant's `enabled` flag on, so work resumes on
the next start. Stopping the *agent* (the Overview screen's Stop button, or
`PATCH /auto-approval/config {enabled:false}`) releases the queue back to
`NOT_STARTED` and closes the runs. Use the screen for an operational pause and
systemd for a deploy.

---

## 7 · What is still worth knowing

**The render step can take half an hour.** Five views at ~200s each plus matting.
That is why the proxy timeout, the Celery soft limit (30 min) and
`TimeoutStopSec` (35 min) are all set the way they are; leaving any of them at a
default breaks the longest legitimate step.

**One product at a time, per tenant, is enforced by the database** — a partial
unique index on `AutoApprovalRunProduct(tenantId) WHERE status='IN_PROGRESS'` —
not by `--pool=solo`. Two workers on two hosts would still be correct; they
would simply contend.

**A Redis outage costs no work.** The broker carries ticks, never product ids.
Queued rows are in Postgres and drain when Redis returns.

**Going LIVE is the irreversible step.** `SHADOW` records verdicts and writes
nothing. `LIVE` approves, and approving publishes a Shopify listing that has to
be removed on Shopify's side. On the vnyx-api host, `SHOPIFY_SYNC_DISABLED=true`
blocks the push while leaving everything else working — the right setting until
you mean to publish.
