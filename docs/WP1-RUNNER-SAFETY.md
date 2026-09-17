# WP1 — Runner safety: what it does, what changed, what is next

| | |
|---|---|
| **Delivered** | 15 September 2026 |
| **Scope** | Work package 1 of the auditor → Hermes parity plan (`docs/PARITY-PLAN.md`): items #1 vision-outage abort, #2+3 image quality gate with the unverified-gender block, #4 regeneration canary |
| **Tested against** | The **local** database only (`localhost:5433/vnyx-dev-2`), with a local vnyx-api as the step runner. Unit tests, a read-only gate calibration on 50 real products, a forced outage, two chain dry runs, a sheet run, and one live UI run (CBOA-005721) |
| **Needs to deploy** | Hermes code + `config/policy.yaml`, then restart the Celery worker and the FastAPI service. **No** database migration. **No** new environment variable on either side. **No** vnyx-api change |
| **Plan artifact** | https://claude.ai/code/artifact/33b9ae22-b661-418b-a151-d256381b1c02 |

---

## 1. In one paragraph

Before WP1 the agent judged a product entirely from its columns and trusted every vision call to have worked. It could approve a render nobody had looked at, reject a whole queue for "no brand or size" because the label reader's API key had run out, and never notice that one of its own repair writes had kicked a product back into paid regeneration. WP1 closes those three holes: the agent now **looks at the lead render once before approving**, **tells a provider outage apart from an illegible label and stops instead of rejecting**, and **stops the run if a write triggers regeneration**.

---

## 2. The four behaviours

### 2.1 Image quality gate (#2)

A new `gate` step runs after `render` and before `approve`. It fetches the product's live `AI_FRONT` render (fallback `AI_FRONT_34`) and asks Gemini one question with seven answers: is a model present, is the face intact, what gender does the model present, is this a usable lead image, is the body anatomically coherent, what is wrong with it, is it a front or back view. One flash call, ~3 s of model time.

The answer becomes an action, and every caller applies the same policy:

| Action | Meaning | What the agent does |
|---|---|---|
| `ok` | fit to publish | approve may proceed |
| `regen` | no model / corrupted face / broken body / **wrong gender** | product is **held**, outcome `IMAGE_QUALITY` or `MODEL_GENDER_MISMATCH`; the approve step is **withheld even under LIVE** |
| `review` | the gate could not decide (provider down, image unreachable) | product is **retried**, not held — see 2.3 |
| `skipped` | no render to judge, or gate disabled in policy | nothing; the pre-flight already refuses a product with no render |

Soft flags — an unusable lead, an androgynous model, a lead that shows the back — are recorded and never block. Accessories (caps, belts, bags) are exempt from "no model".

The gate runs in **shadow mode too**, so a dry run reports what it would have refused. That is how its false-block rate was measured before it was allowed to block anything (see §6).

### 2.2 Unverified-gender block (#3)

Hermes derives a product's gender from the master category or the mannequin and the renderer picks the model from that. Until now nothing checked the result against the picture. The gate is the first time a photograph confirms the gender: a **contradiction** is `MODEL_GENDER_MISMATCH` and blocks; a gate that **could not run** is `VISION_UNAVAILABLE` and the product is retried. The agent never approves gender-blind.

### 2.3 Vision-outage abort (#1)

Every in-process vision call — the care-label read (Gemini, then OpenAI) and the gate — is counted by outcome over a sliding window of the last 20 calls (`app/llm/health.py`). **Only provider failures count**: a 429, a 5xx, a rejected key, a timeout, a refused connection. A model that answers "I cannot read this" is a successful call with an unhelpful answer and is never counted — otherwise a batch of hard labels would look like an outage.

Between products the Celery pump and the sheet driver ask `runner.stop_reason()`. When at least 6 calls are in the window and 70 % of them failed, the run **stops** for a 600 s cooldown. Products already judged keep their verdicts; the one in hand is retried; nothing further is written. One `fail` event is recorded on the run.

Two things make this safe at the product level rather than only at the run level:

- `care_label.read()` now tells `api_error` from `no_answer` per provider and sets `api_failed` when every provider that could be asked failed. `repair()` puts such steps on `result["vision_unavailable"]`.
- **The `NO_BRAND_OR_SIZE` rejection is suppressed for those products.** During an outage every label is "not readable"; before WP1 that rule would have archived the queue one product at a time. Such a product is released with the normal backoff while attempts remain and held as `VISION_UNAVAILABLE` when they run out — a code that names the provider, not the product.

### 2.4 Regeneration canary (#4)

Every repair step writes through vnyx-api's own update path, and that path has hooks that can re-queue a product for analysis — which regenerates its images at a paid call per view. `repair()` now reads `generationStatus` before and after the chain. A flip to `GENERATING` that the render step did not cause sets `generation.regeneration_triggered`; the runner writes a `fail` event, the pump parks that tenant for an hour (process-local, no column written), and the sheet driver stops submitting. The product itself is finished normally — the flip is on its row.

---

## 3. Seen in the UI — CBOA-005721, 15 September

A single-product run from the Auto Approval screen, BOAS, mode SHADOW with shadow-writes-repairs on:

```
1 · Background removal   124.2s   BACK, FRONT
2 · Relabel renders        2.7s   recovered the true view from the filenames
3 · Read the care label           only material missing — not worth a vision pass
4 · Brand & size, narrowly        brand and size are both already present
5 · Repair & verify       33.3s   mannequinType
    price                  2.8s   price already inside the window
6 · Generate renders              all five views already exist
    gate                  11.3s   REFUSED — the model presents as women but the product is listed as men (on AI_FRONT)
7 · Approve preflight      2.8s   NOT ready: no EU size; image gate: the model presents as women but the product is listed as men

→ HELD_FOR_HUMAN · DATA.010 · 2m 57s
```

The product is the C-twin of BOA-005721 — the men's copy of a women's listing — and it carries the parent's **women's** renders while its record says men. Every column check passed it. The gate did not. The recorded outcome is `DATA.010` (no EU size) because Hermes' own rule is kept as the more specific cause; the gate's refusal sits alongside it in the pre-flight list and in the step log, and the approve step was withheld. Had the EU size been present, the outcome would have been `MODEL_GENDER_MISMATCH` and the product would still not have moved.

The parent, BOA-005721, is already approved with a **men's** model under a title that says *Women W21* — a data problem outside WP1's scope, noted for the team.

---

## 4. Files changed

**14 code files** — 8 modified, 2 new modules, 4 new test files — plus 2 documents. `git diff --stat` on the modified files: **515 insertions, 43 deletions**.

### New

| File | Lines | What it is |
|---|---|---|
| `app/llm/health.py` | 179 | Vision-provider health: sliding-window counters, `is_api_error`, `check()` raising `VisionOutage`, cooldown, `snapshot()` |
| `app/imaging/quality_gate.py` | 277 | The gate: prompt, JSON schema, `GateVerdict`, lead selection by typed media view, `decide()` (pure) and `judge()` (the call) |
| `tests/test_vision_health.py` | 84 | 9 tests — thresholds, window, cooldown, error classification |
| `tests/test_quality_gate.py` | 139 | 22 tests — every decision branch, lead selection, unavailable vs bad answer, gender shapes, policy switches |
| `tests/test_outcome_gate.py` | 42 | 6 tests — `gate_blocked` / `gate_unavailable` → verdicts |
| `tests/test_repair_gate_wiring.py` | 165 | 9 tests — the chain's gate/approve/canary/label wiring with every I/O edge replaced |

### Modified

| File | +/− | What changed |
|---|---|---|
| `app/services/auto_approval/runner.py` | 138 | `verify_one` returns a stop reason; canary; `vision_unavailable` retry-then-hold; `NO_BRAND_OR_SIZE` suppressed when the reader was down; `stop_reason()`; pump checks the canary pause and the outage before claiming and after each product; `deltas` carry `gate` and `generation` |
| `scripts/repair_product.py` | 131 | `needs()` returns generation status; `vision_unavailable` list; care-label `api_failed` handling; new `gate` step; approve withholds the move and rewrites the outcome to `gate_blocked` / `gate_unavailable` or appends the gate's reasons; result carries `gate`, `vision_unavailable`, `generation`; CLI prints the gate line and an honest dry-run banner |
| `app/llm/care_label.py` | 74 | Both readers return `(reading, status)` with `ok / no_answer / api_error / not_configured`; `read()` reports `unavailable`, `api_failed`, and an `error` naming the providers; providers with no key are not counted as tried |
| `scripts/run_from_sheet.py` | 66 | Sequential path and `Parallel` honour the stop reason; `retry` tally for not-judged products; releases the row it just enqueued when the run stops |
| `config/policy.yaml` | 61 | New `llm.health` block (window, min_samples, error_ratio, cooldown_s) and new top-level `quality_gate` block (enabled, required, lead_views, block_on, accessory_terms, model) — every threshold lives here, none in code |
| `app/llm/gemini.py` | 49 | `_generate` records ok/error with the health module and sets `last_error_kind` (`api` vs `answer`); `_fetch_images` now goes through `app.net.fetch_all` (IPv4 switch, phase timeouts, truncated-body retry) instead of a bare `httpx.Client` |
| `app/services/auto_approval/outcome.py` | 31 | `classify()` maps `gate_blocked` → HELD under the gate's code, `gate_unavailable` → HELD `VISION_UNAVAILABLE`, retryable |
| `tests/test_column_drift.py` | 8 | One existing assertion updated to the readers' new `(None, "no_answer")` shape |

### Documents

| File | Purpose |
|---|---|
| `docs/PARITY-PLAN.md` | The whole plan; §11 records WP1 as delivered with the measurements |
| `docs/WP1-RUNNER-SAFETY.md` | This file |

Not part of WP1 and not for committing: the `.xlsx` sheets in the repo root, `celerybeat-schedule`, and the two `run-*.ps1` scripts, which pre-date this work and are untracked local files.

---

## 5. Configuration

Everything tunable is in `config/policy.yaml`; nothing is hard-coded.

```yaml
llm:
  health:
    window: 20          # recent calls consulted
    min_samples: 6      # below this, no verdict — three products' worth
    error_ratio: 0.7    # fraction that must be provider failures
    cooldown_s: 600     # how long the loop stays stopped

quality_gate:
  enabled: true
  required: true        # no GEMINI_API_KEY → block approval rather than skip the gate
  lead_views: ["AI_FRONT", "AI_FRONT_34"]
  block_on: ["no_model", "bad_face", "broken_body", "gender_mismatch"]
  accessory_terms: [cap, caps, beanie, ...]
  model: null           # null = llm.model_fast
```

One optional environment variable: `AUTO_APPROVAL_CANARY_PAUSE_S` (default 3600) — how long a tenant stays parked after the canary trips. Nothing needs setting for the defaults.

---

## 6. Tests and results

| Test | Result |
|---|---|
| Unit — the 4 new files | **46 tests pass**. Full suite **621 pass**; the 4 failures in `tests/test_column_drift.py` pre-date this work (mannequin rule expects "Men Top", current TAX.005 suggests "Women Top") |
| Gate, read-only, 25 hand-**approved** Bleckmann products | **0 refused** of 24 judged; 1 `VISION_UNAVAILABLE` on a genuine Gemini 503 — classed as retry, not refusal |
| Gate, read-only, 25 **Review** products | 22 ok · **1 refused** (BOA-006114, men's model in a red gown on a women's denim vest — confirmed by eye) · 2 transient fetch/socket failures classed as retry |
| Gate, read-only, 60 Review products with label + brand + size | 50 ok · **2 refused**, both C-twins carrying the parent's opposite-gender renders (CBOA-005721, CKIL-001059) · 8 `VISION_UNAVAILABLE` on a two-second DNS blip — retry, not refusal |
| Forced outage — 7 products, invalid key | 7/7 counted, `VisionOutage` raised at 100 %, nothing judged |
| Chain dry runs (`repair_product.py`) | gate refused BOA-006114, passed BOA-006108 |
| Sheet run, shadow (`run_from_sheet.py`) | rows carry `steps[gate]`, `deltas.gate`, `deltas.generation`; events read `gate: REFUSED — …` / `gate: passed — on AI_FRONT` |
| **UI run, CBOA-005721** | §3 above — the gate refused a women's model on a men's listing and the move was withheld |

---

## 7. Reading the new outcomes

| Where | What you will see | Meaning |
|---|---|---|
| Run log, step line | `gate: passed — on AI_FRONT` | the lead render is fit to publish |
| Run log, step line | `gate: REFUSED — <reason> (on AI_FRONT)` | held; the reason names the defect |
| Run log, step line | `gate: FAILED — vision unavailable — …` | the gate could not run; the product is retried |
| Outcome | `IMAGE_QUALITY` | no model / corrupted face / broken body |
| Outcome | `MODEL_GENDER_MISMATCH` | the model's gender contradicts the record |
| Outcome | `VISION_UNAVAILABLE` | every attempt hit a provider failure; re-run when the provider is back |
| Event, `warn` | `<sku>: vision provider unavailable during care label, gate — not judged, retrying (attempt 1 of 3)` | the retry path |
| Event, `fail` on the run | `vision outage — the vision provider failed 6 of the last 6 calls (100%) …` | the run stopped; cooldown running |
| Event, `fail` on the run | `canary: <sku> flipped COMPLETE → GENERATING during the chain without the render step running …` | a repair write triggered regeneration; the tenant is parked |
| Pre-flight problems | `image gate: <reason>` | the gate's finding, alongside the pre-flight's own |

---

## 8. Known limits

- **Gate latency is 8–14 s per product on the dev host**, not the ~3 s estimated; the model call is ~3 s and the R2 image fetch is the rest. The fetch now goes through the IPv4/retry client. Measure again on the server.
- **`--workers` rows have no lease**, so a provider-failed product stays `QUEUED` for the next invocation instead of backing off. The driver prints "NOT JUDGED … left queued". Same gap the existing retry path has on that mode.
- **Counters are process-local.** The FastAPI service's own Gemini calls (evidence for `verify-and-repair.ts`) are not in the worker's window; the worker's calls are where a false rejection would originate.
- The `gate_blocked` branch — a product ready by every column check and badly rendered — was not reached on real local data (none exists). It is covered by `tests/test_repair_gate_wiring.py`.

---

## 9. What is next

The plan's order stands: **WP4 next, starting with #14**, then WP3, then WP2.

| Order | Item | Why this order |
|---|---|---|
| **now** | **#14 Rejection unpublishes** (WP4, vnyx-api) | The only remaining item with a measured cost: **170 rejected products still live on Shopify**. vnyx-api already enqueues the unpublish job on rejection; the Shopify adapter's `unpublishListing` is a no-op. ~50 lines plus a one-off re-enqueue. Needs one decision first: DRAFT or ARCHIVED for a rejected product on the store |
| then | #15 oversell guard, #16 publication verification (WP4) | Same codebase, same deploy; both protect the re-sync path that runs after every batch |
| then | WP3 — rules, gallery order, **C-twins** | Today's UI run made #10 concrete: twins carry the parent's opposite-gender renders and lack EU size, both of which the twin rule should handle before the gate has to catch them |
| last | WP2 — cache, HTML grid, offline self-test | Convenience, no measured cost |

WP4 items change vnyx-api, so they ship in a vnyx-api deploy, and #14 must be checked against a real Shopify store on one product before the 170 are touched.
