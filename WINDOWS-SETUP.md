# Running Hermes locally on Windows 10

Goal: Hermes on `localhost:8080`, driven from Postman, with no Gemini key and no
VNYX token needed. You'll be comparing output against your current VNYX values
inside 15 minutes.

The order matters. Steps 1–7 get the rule engine running for free. Only after
you've looked at that output do you turn on Gemini (step 8) and the VNYX
connection (step 9).

---

## Step 1 — Install Python

Grab **Python 3.12** from [python.org/downloads/windows](https://www.python.org/downloads/windows/).
Pick the *Windows installer (64-bit)*.

In the installer, **tick "Add python.exe to PATH"** at the bottom before you
click Install. Almost every setup problem on Windows traces back to this box.

Open a new PowerShell window and check:

```powershell
python --version
```

You want `Python 3.12.x`. If you get "Python was not found" or it opens the
Microsoft Store, PATH didn't take — re-run the installer, choose *Modify*, and
tick the box.

> Python 3.10 or 3.11 also work. 3.9 and below won't — the code uses `X | None`
> type syntax.

---

## Step 2 — Unpack the project

Extract `hermes.zip` somewhere without spaces or OneDrive sync in the path.
`C:\hermes` is ideal. Avoid `C:\Users\You\OneDrive\Documents\...` — OneDrive
locks files mid-write and you'll get confusing permission errors.

```powershell
cd C:\hermes
dir
```

You should see `app`, `config`, `samples`, `postman`, `tests`, `requirements.txt`.

---

## Step 3 — Create a virtual environment

```powershell
python -m venv .venv
```

Activate it:

```powershell
.\.venv\Scripts\Activate.ps1
```

**If you get "running scripts is disabled on this system"** — that's Windows'
default execution policy. Fix it for this session only:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

That's scoped to the current window and reverts when you close it, so it changes
nothing permanently.

**Or skip PowerShell entirely** — open Command Prompt (`cmd`) instead and run:

```cmd
.venv\Scripts\activate.bat
```

Either way, your prompt should now start with `(.venv)`.

---

## Step 4 — Install dependencies

```powershell
pip install -r requirements.txt
```

Takes a minute or two. `google-genai` pulls in a fair few packages even though
you won't use it yet.

> **If `google-genai` fails to build**, you can still run everything in steps
> 5–7. Install just the core and move on:
> ```powershell
> pip install fastapi "uvicorn[standard]" pydantic httpx PyYAML python-dotenv pytest
> ```
> Hermes detects the missing SDK and runs rules-only automatically.

---

## Step 5 — Prove it works before starting the server

```powershell
pytest -q
```

Expect `15 passed`. These tests use your actual Levi's record as the fixture, so
a green run means the rule engine already agrees with what I showed you.

Now see the report for that record:

```powershell
python demo.py
```

```
==============================================================================
 BLOCKED        publishable=False   llm_calls=0   10ms
==============================================================================

FINDINGS
  [critical] ID.001      Bin location 'ZONE A-02-12-3' does not match the printed
                         bin barcode 'A-04-10-4'...
  [high    ] PRICE.002   Price/retail ratio 0.58 sits above the Grade C band [0.22-0.48].
  [high    ] TAX.005     Mannequin 'Women Top' is wrong for Men > Bottoms...
  [high    ] SIZE.002    EU size 42 does not convert from W32 (expected ~48).
  ...

BEFORE / AFTER
  FIELD           WAS                       BECOMES                   ACTION
  --------------------------------------------------------------------------
  retail_price    66.99                     110.99                    propose
  mannequin       Women Top                 Men Bottom                apply
  eu_size         42                        48                        apply
```

Try the other samples — each one exercises a different repair path:

```powershell
python demo.py samples\02-price-above-retail.json
python demo.py samples\03-grounded-retail-moves-price.json
python demo.py samples\04-human-locked-price.json
python demo.py samples\05-clean-control.json
```

Sample 05 should come back `CLEAN` with no findings. Run it — it's your proof
that the rules aren't just firing on everything.

---

## Step 6 — Create your .env

```powershell
Copy-Item .env.example .env
```

The defaults are already set up for local testing: `HERMES_LLM_ENABLED=false`
and `HERMES_DRY_RUN=true`. No keys needed. Leave it alone for now.

> If you edit `.env` in Notepad, make sure it saves as `.env` and not `.env.txt`.
> File Explorer hides extensions by default. Use `notepad .env` from PowerShell,
> or check with `dir .env*`.

---

## Step 7 — Start the server

```powershell
uvicorn app.main:app --reload --port 8080
```

```
INFO:     Uvicorn running on http://127.0.0.1:8080 (Press CTRL+C to quit)
INFO:     Application startup complete.
```

Leave this window open — it's your log. Open a browser:

- **http://localhost:8080/healthz** → should show `"ok": true`, `"llm_enabled": false`
- **http://localhost:8080/docs** → interactive Swagger UI, free with FastAPI.
  You can fire requests from here before you even open Postman.

If Windows Firewall prompts, "Allow on private networks" is fine. You only need
localhost.

> **Port 8080 already taken?** Use `--port 8090` and change `baseUrl` in Postman
> to match. To find the culprit: `netstat -ano | findstr :8080`.

---

## Step 8 — Test from Postman

Import the collection:

1. Postman → **Import** → drag in `postman\Hermes.postman_collection.json`
2. Open **Postman Console** with `Ctrl+Alt+C` and keep it visible

That console is the point. Each request has a test script that prints the
findings and patches as a readable list, so you don't have to squint at raw JSON.

Run the requests in order:

| Request | What to look for |
|---|---|
| `00 - Health check` | `llm_enabled: false`, `dry_run: true` |
| `01 - Your real Levi's record` | 6 findings, 3 patches, status `blocked` |
| `02 - Price ABOVE retail` | `PRICE.001` fires **critical** — this is your bug |
| `03 - Grounded retail` | Same numbers as 02, but the **price** moves instead |
| `04 - Operator-locked price` | Hermes refuses to touch a locked field |
| `05 - Clean control` | status `clean`, zero findings |
| `06 - Missing retail anchor` | escalates without evidence to work from |

Compare **02 against 03**. Identical prices, opposite repair. In 02, `price`
carries `market` provenance and `retail_price` is only `derived`, so Hermes
raises the anchor. In 03, `retail_price` is `grounded`, so it outranks the price
and Hermes lowers the price into the Grade C band instead. That's the
provenance arbitration doing its job, and it's the knob you'll be tuning.

---

## Comparing against your current output

The response body gives you the diff directly:

```json
{
  "status": "blocked",
  "publishable": false,
  "patches": [
    {
      "field": "retail_price",
      "old_value": 66.99,        // what VNYX generated
      "new_value": 110.99,       // what Hermes says it should be
      "action": "propose",
      "reason": "Ratio was 0.58. The selling price has stronger provenance...",
      "confidence": 0.75
    }
  ]
}
```

`old_value` is your current AI output, `new_value` is Hermes' verdict. To test
your own records, copy any product JSON from your VNYX API response into the
`product` field of request 01 and re-send. The mapping in
`app\vnyx_client.py` accepts both camelCase and snake_case keys, so a raw paste
usually just works.

Everything also appends to `logs\audit.jsonl` — one JSON object per run, which
you can open in Excel or pipe through `jq` once you've processed a few hundred
records.

---

## ⚠️ One thing you need to decide

Look at sample 02's output: retail gets back-solved from €66.99 to **€228.95**.

That's arithmetically correct — €79.99 at a Grade C target of 0.35 implies an RRP
around €229 — but it's almost certainly *not* what happened. What actually
happened is the market price lookup returned garbage, and back-solving the anchor
from a garbage price amplifies the error instead of catching it.

Hermes only *proposes* that change rather than applying it, because it exceeds
the 60% delta cap. So nothing breaks. But it tells you the provenance defaults
need a decision from you:

**Option A — trust the retail anchor** (what I'd suggest starting with). In
`app\vnyx_client.py`, change the default:

```python
provenance.setdefault("price", Provenance.AI)          # was MARKET
provenance.setdefault("retail_price", Provenance.DERIVED)
```

Now the price always moves and the anchor stays put. Predictable, and errors
get clamped rather than amplified.

**Option B — keep MARKET, but earn it.** Only tag a price as `market` when your
Google lookup actually returned comparable sold listings with sources. If it fell
back to a heuristic, tag it `ai`. This is more work upstream but strictly better,
because provenance then means something real.

Sample 03 shows you exactly what Option A looks like in practice.

---

## Step 9 — Turn on Gemini (optional, costs money)

Get a key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey),
then edit `.env`:

```
HERMES_LLM_ENABLED=true
GEMINI_API_KEY=your_actual_key
```

Restart uvicorn (`Ctrl+C`, then the same command). Confirm on `/healthz` that
`llm_enabled` is now `true`.

Test the branch that needs it:

```powershell
python demo.py samples\06-missing-retail-anchor.json --llm
```

With no retail anchor and no evidence, Hermes escalates. With Gemini on, it
searches for the original Levi's RRP, sets the anchor, and recomputes the price
from there — and the patch carries `sources` you can click through and verify.

Watch `llm_calls` in every response. On samples 01–05 it should stay at **0**,
because the rule engine already resolved everything. That's the cost control
working: model calls only happen when a finding actually asks for evidence.

---

## Step 10 — Connect to VNYX (optional)

To fetch and write by product id rather than pasting payloads, add to `.env`:

```
VNYX_BASE_URL=https://dev.vnyx.ai/api
VNYX_API_TOKEN=your_token
```

**Keep `HERMES_DRY_RUN=true`.** With it on, `/v1/reconcile` computes everything
and logs it but writes nothing back. Run request 07 in Postman and confirm the
fetch works and `applied` comes back empty. Only flip `HERMES_DRY_RUN=false`
once you've read a few hundred lines of `audit.jsonl` and agree with the calls.

You'll likely need to adjust `to_snapshot()` and `_FIELD_MAP` in
`app\vnyx_client.py` to match your real API shape. It's the only file that knows
about VNYX — nothing else changes.

---

## Docker instead (optional)

Docker Desktop on Windows 10 needs WSL2 and Home edition needs build 19044+.
It's more setup than it's worth for local testing, but if you already have it:

```powershell
docker compose up --build
```

Same endpoints on the same port. Native Python with `--reload` is better while
you're iterating on rules, since edits take effect instantly.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `python: command not found` | PATH box unticked during install. Re-run installer → Modify → tick it. |
| `Activate.ps1 cannot be loaded` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, or use `cmd` + `activate.bat`. |
| `ModuleNotFoundError: No module named 'app'` | You're not in `C:\hermes`. `cd` there — uvicorn resolves `app.main` relative to the working directory. |
| `ModuleNotFoundError: fastapi` | venv not activated (no `(.venv)` in prompt), or you installed into system Python. |
| `Address already in use` | Use `--port 8090`, update `baseUrl` in Postman. |
| Postman: `ECONNREFUSED` | Server not running, or wrong port. Use `localhost`, not `0.0.0.0`. |
| `.env` seems ignored | Saved as `.env.txt`. Check with `dir .env*`. |
| Everything returns `clean` | Payload keys aren't mapping. Check `to_snapshot()` in `app\vnyx_client.py` against your JSON. |
| Unicode errors in the console | `chcp 65001` before running, or use Windows Terminal instead of legacy console. |

---

## Tuning loop

The fastest iteration cycle while testing:

1. Edit `config\policy.yaml` — grade bands, tolerances, thresholds
2. POST `/v1/policy/reload` (request 09 in Postman) — no restart
3. Re-send request 01 and compare

Start with `pricing.grade_bands`. Mine are estimates. Your actual sell-through
data should replace them, and that's the single change with the biggest effect on
how often Hermes flags a price.
