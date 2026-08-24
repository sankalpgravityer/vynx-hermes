# Deploying to 34.91.186.164 (Debian 13, systemd + uvicorn, no Docker)

Every command, in order. Confirmed against the server: **Debian GNU/Linux 13
(trixie)**, x86_64, Python 3.13.5, 7.9 GB RAM, 16 GB free disk.

Installs to **`/home/sankalp/vynx-hermes`**, reachable from **anywhere on the
internet** at `http://34.91.186.164` (port 80).

### What gets deployed

The **verification service** — the deterministic rule engine:

```
GET  /healthz
POST /v1/review-queue        report-only sweep, the endpoint vnyx-api calls
POST /v1/validate
POST /v1/reconcile           fetches AND WRITES to vnyx by id
POST /v1/batch               same, in bulk
POST /v1/webhooks/product-enriched
POST /v1/policy/reload
```

**Not** `/v1/agent/prompt`. That code (`app/agent_cli.py` and its tests) is still
uncommitted locally, so a fresh clone does not include it — and on a host open to
the internet it must stay out, because it grants shell access to anyone who can
reach it.

### Read this once before Step 8

You have chosen open internet access, and these endpoints have **no
authentication**. So with the port open, anybody who finds it can call
`/v1/policy/reload` and change how every price is judged, and — *if you fill in
`VNYX_API_TOKEN`* — call `/v1/reconcile` or `/v1/batch` and write to your product
database.

Two settings remove almost all of that risk **without reducing access at all**,
and Step 6 sets both:

- `VNYX_API_TOKEN=` (empty) — the write endpoints then have no credential and
  cannot change anything in vnyx.
- `HERMES_DRY_RUN=true` — patches are computed and logged, never written.

With those two, an open port exposes a read-only rule engine. `/v1/review-queue`,
the endpoint vnyx-api actually calls, takes everything in the request body and
never needed a credential in the first place.

If you later want a password on it, see **Locking it down** at the end — it is a
dozen lines and does not change the URL.

---

## Step 1 · Connect

```bash
ssh sankalp@34.91.186.164
```

Everything below runs on the server unless stated otherwise.

## Step 2 · Install prerequisites

```bash
sudo apt update
sudo apt install -y git python3-venv curl
```

`git` was absent on this box (Docker too, not needed).

**Why a venv is mandatory, not a preference:** Debian 13 marks its system Python
as externally managed (PEP 668), so a plain `pip install` fails with
`error: externally-managed-environment`. Everything installs into
`~/vynx-hermes/.venv`.

## Step 3 · Clone into your home directory

No `sudo` — it is your own directory, so the files are yours from the start and
there is no ownership to fix afterwards.

```bash
cd ~
git clone https://github.com/sankalpgravityer/vynx-hermes.git
cd ~/vynx-hermes
```

The repo is public, so no token or deploy key is needed.

## Step 4 · Virtualenv and dependencies

```bash
cd ~/vynx-hermes
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

Every pin has a prebuilt wheel for CPython 3.13 on x86_64, so no compiler is
needed. If a build is attempted anyway, `sudo apt install -y build-essential
python3-dev` and retry.

Confirm it imports before going further:

```bash
.venv/bin/python -c "from app.main import app; print('import OK')"
```

## Step 5 · Create the log directory

```bash
mkdir -p ~/vynx-hermes/logs
```

`logs/` is gitignored, so the clone does not contain it. The app would create it
on first write — but the systemd unit below runs with a read-only filesystem, so
it has to exist first and be granted write access explicitly (Step 8 does that
with `ReadWritePaths`).

## Step 6 · Configure `.env`

`.env` is gitignored, so start from the template:

```bash
cd ~/vynx-hermes
cp .env.example .env
chmod 600 .env
```

**The committed template is already right for this deployment** — check it reads:

```dotenv
HERMES_LLM_ENABLED=false
GEMINI_API_KEY=
VNYX_BASE_URL=https://api-dev.vnyx.ai
VNYX_API_TOKEN=
VNYX_TIMEOUT_S=20
HERMES_DRY_RUN=true
```

Nothing needs editing. In particular **leave `VNYX_API_TOKEN` empty** — see the
note above; it is what stops an open port from being able to write to your
catalog. And leave `HERMES_AUDIT_PATH` commented: it defaults to
`<project>/logs/audit.jsonl`, resolved from the code location rather than the
working directory, which is exactly Step 5's directory.

**Do not copy your laptop's `.env` across.** That one sets
`VNYX_BASE_URL=http://127.0.0.1:8000`, which is nothing on this server, and
carries a live `GEMINI_API_KEY` you have no reason to put on an internet-facing
host while `HERMES_LLM_ENABLED=false`.

Leave `HERMES_AGENT_ENABLED` unset. The agent code is not in this clone; keep it
that way here.

## Step 7 · Smoke test by hand

Bind to localhost first, so nothing is exposed while you check it works:

```bash
cd ~/vynx-hermes
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
```

Port 8080 here, not 80 — an unprivileged shell cannot bind a low port, and this
is only a "does the app start" check. The service gets port 80 in Step 8 via a
capability grant.

In a second SSH session:

```bash
curl -s http://127.0.0.1:8080/healthz
```

`rule_groups` must list **8** entries including `catalog`:

```json
{"ok":true,"llm_enabled":false,"model":"gemini-3.7-flash","dry_run":true,
 "rule_groups":["pricing","taxonomy","sizing","grading","identity","copy",
                "completeness","catalog"]}
```

`Ctrl+C` to stop. If this fails, fix it here — systemd will only hide the error.

## Step 8 · systemd service

```bash
sudo nano /etc/systemd/system/hermes.service
```

```ini
[Unit]
Description=Hermes product-review verification API
Documentation=https://github.com/sankalpgravityer/vynx-hermes
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
User=sankalp
Group=sankalp
WorkingDirectory=/home/sankalp/vynx-hermes

# ONE worker, deliberately. `policy()` is lru_cached per process, so
# POST /v1/policy/reload only clears the cache of the worker that handled the
# request — with two workers, half your traffic would keep judging against the
# old policy.yaml. The rule engine is ~10ms per record, so one worker is not the
# bottleneck. If you do raise this, reload policy with
# `systemctl restart hermes` instead of the endpoint.
#
# 0.0.0.0 = listen on every interface, which is what makes the public IP work.
# Port 80, not 8080: this VM already carries the `http-server` network tag, so
# GCP's stock default-allow-http rule permits tcp:80 from anywhere and no new
# firewall rule is needed. Step 9 shows the evidence.
ExecStart=/home/sankalp/vynx-hermes/.venv/bin/uvicorn app.main:app \
          --host 0.0.0.0 --port 80 --workers 1

Restart=always
RestartSec=3

Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONUNBUFFERED=1

# Ports below 1024 are privileged, but this does NOT need to run as root.
# AmbientCapabilities grants exactly the one capability required to bind a low
# port; CapabilityBoundingSet then caps the process at only that, so it gains
# nothing else. Both work alongside NoNewPrivileges=true — verified running.
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Hardening. NOTE: no ProtectHome here, on purpose — it would hide /home from
# the process, and both the code and the venv live there, so the service would
# fail to start at all. ProtectSystem=strict still makes the whole filesystem
# read-only, so the only writable path is the log directory named below.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/home/sankalp/vynx-hermes/logs
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true

[Install]
WantedBy=multi-user.target
```

Start it and have it survive reboots:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hermes
sudo systemctl status hermes --no-pager
ss -ltn | grep ':80 '
curl -s http://127.0.0.1/healthz
```

## Step 9 · Why no firewall rule is needed

Measured facts about this instance:

| | |
|---|---|
| Instance | `vynx-agent` |
| Zone | `europe-west4-b` |
| Project | `gen-lang-client-0657343461` |
| Network tags | `http-server`, `https-server`, `lb-health-check` |
| External IP | `34.91.186.164` |

That `http-server` tag is the whole answer. It means GCP's stock
`default-allow-http` rule already permits **tcp:80 from 0.0.0.0/0** to this VM.
Verified from outside without changing anything: **tcp/80 answered `connection
refused` in 2.9s** — the packet reached the host and nothing was listening — while
**tcp/8080 timed out after 12s**, the signature of a firewall dropping it.

Step 8's unit already uses port 80 for that reason, so there is nothing to do
here — go to Step 10 and verify.

### If you already built the unit on 8080

Switch it in place:

```bash
sudo cp /etc/systemd/system/hermes.service /etc/systemd/system/hermes.service.bak-8080
sudo sed -i 's|--host 0.0.0.0 --port 8080|--host 0.0.0.0 --port 80|' /etc/systemd/system/hermes.service
sudo sed -i '/^NoNewPrivileges=true/i AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE' /etc/systemd/system/hermes.service
sudo systemctl daemon-reload && sudo systemctl restart hermes
ss -ltn | grep ':80 '
curl -s http://127.0.0.1/healthz
```

### Alternative: keep port 8080

If you would rather stay on 8080, that needs a firewall rule, with the real
values for this instance:

```bash
gcloud compute firewall-rules create hermes-api-8080 \
  --direction=INGRESS --action=ALLOW \
  --rules=tcp:8080 --source-ranges=0.0.0.0/0 \
  --target-tags=hermes-api

gcloud compute instances add-tags vynx-agent \
  --zone=europe-west4-b --tags=hermes-api
```

Both commands run **from a machine with gcloud authenticated** — not from the
server. The VM's own service account cannot do it: `gcloud` is installed there
but returns `Request had insufficient authentication scopes` for firewall
operations.

Console route instead: **VPC network → Firewall → Create firewall rule**, target
tag `hermes-api`, source `0.0.0.0/0`, TCP `8080`; then add the `hermes-api` tag
to the VM under **Compute Engine → VM instances → Edit**.

## Step 10 · Verify from anywhere

```bash
curl -s http://34.91.186.164/healthz
```

Confirmed working from off-network — `HTTP 200 in 0.34s`:

```json
{"ok":true,"llm_enabled":false,"model":"gemini-3.7-flash","dry_run":true,
 "rule_groups":["pricing","taxonomy","sizing","grading","identity","copy",
                "completeness","catalog"]}
```

Interactive docs: **http://34.91.186.164/docs**

A real rule-engine call — no credentials, everything travels in the body:

```bash
curl -s -X POST http://34.91.186.164/v1/review-queue \
  -H 'content-type: application/json' \
  -d '{"products":[{"id":"11111111-1111-1111-1111-111111111111",
                    "tenantId":"22222222-2222-2222-2222-222222222222",
                    "priceAmount":58.99,"retailPriceAmount":66.99,
                    "grade":"C","gradeLabel":"Lived In","condition":"Lived In",
                    "priceExpectation":{"priceFactor":0.58,"expectedPrice":38.85}}]}'
```

Returns `correct: false`, `price.verdict: too_high`, `rule: backend_factor` —
58.99 against a Grade C factor of 0.58 on a 66.99 retail, expected 38.85.

---

## Operating it

```bash
sudo systemctl status hermes            # is it up
sudo systemctl restart hermes           # after editing .env
sudo journalctl -u hermes -f            # live logs
tail -f ~/vynx-hermes/logs/audit.jsonl  # every product judged
```

**What needs a restart, and what does not:**

| Changed | Action |
|---|---|
| `.env` | `sudo systemctl restart hermes` — dotenv loads once, at import |
| `config/policy.yaml` | `curl -X POST http://127.0.0.1/v1/policy/reload` — no restart |
| code (`git pull`) | restart |

That `.env` row is the one that catches people: nothing reloads it by itself.

**Updating:**

```bash
cd ~/vynx-hermes
git pull
.venv/bin/pip install -r requirements.txt      # in case deps moved
sudo systemctl restart hermes
curl -s http://127.0.0.1/healthz
```

**Rolling back** — `.env` and `logs/` are gitignored, so they survive:

```bash
cd ~/vynx-hermes
git log --oneline -5
git checkout <previous-sha>
sudo systemctl restart hermes
```

---

## Locking it down later

The port is open to everyone, so if you want a credential in front, this is the
cheapest version — a shared-secret header, no nginx, no certificates. Add to
`app/main.py`:

```python
import os, hmac
from fastapi import Request
from fastapi.responses import JSONResponse

API_KEY = os.getenv("HERMES_API_KEY", "")

@app.middleware("http")
async def require_api_key(request: Request, call_next):
    # /healthz stays open so uptime checks and load balancers still work.
    if API_KEY and request.url.path != "/healthz":
        sent = request.headers.get("x-api-key", "")
        # compare_digest, not ==, so a wrong key cannot be guessed a character
        # at a time by timing the response.
        if not hmac.compare_digest(sent, API_KEY):
            return JSONResponse({"detail": "invalid or missing x-api-key"}, 401)
    return await call_next(request)
```

Then `HERMES_API_KEY=<something long>` in `.env`, restart, and send
`-H "x-api-key: ..."`. Unset the variable and everything is open again, so it
cannot lock you out by accident.

For TLS and a clean `https://` URL you would put nginx in front with a
certificate and move uvicorn back to `127.0.0.1`; ask when you want that.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `error: externally-managed-environment` | pip ran outside the venv. Use `.venv/bin/pip` |
| `status=203/EXEC` | Wrong `ExecStart` path — check `/home/sankalp/vynx-hermes/.venv/bin/uvicorn` exists |
| `status=217/USER` | `User=sankalp` does not match the account on the box |
| Starts, then dies immediately with a path error | `ProtectHome` was left in the unit — it hides /home, where the code lives |
| `Permission denied` writing `audit.jsonl` | Step 5 skipped, or `ReadWritePaths` does not match the log directory |
| `curl localhost` works, external times out | Serving on 8080 without a firewall rule — switch to port 80 (Step 9) or add the rule |
| External says "connection refused" rather than hanging | uvicorn is on `127.0.0.1`; it must be `0.0.0.0` |
| `Permission denied` binding port 80 | `AmbientCapabilities=CAP_NET_BIND_SERVICE` missing from the unit |
| `/healthz` lists 7 rule groups | Stale checkout — `git pull`, then restart |
| `/v1/policy/reload` seems not to apply | More than one worker. Restart instead, or keep `--workers 1` |
| `/v1/reconcile` returns 404 for every product | `VNYX_BASE_URL` wrong; it must be `https://api-dev.vnyx.ai` |
| `/v1/reconcile` writes nothing | Intended — `VNYX_API_TOKEN` empty and/or `HERMES_DRY_RUN=true` |
