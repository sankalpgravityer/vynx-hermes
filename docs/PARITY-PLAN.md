# Hermes ↔ vnyx-auto-approve parity plan

| | |
|---|---|
| **Status** | **WP1 delivered and tested, 15 September 2026** — see §11 and `docs/WP1-RUNNER-SAFETY.md` (behaviours, file list, UI evidence). Next: #14 (rejection unpublishes), then the rest of WP4, then WP3, then WP2. |
| **Prepared** | 15 September 2026 |
| **Source codebase** | `vnyx-auto-approve` @ 2026-08-24 (`C:\Users\Mahesh\Downloads\vnyx-auto-approve\vnyx-auto-approve`) |
| **Target codebase** | `hermes` @ working tree, plus `vnyx-api` (`E:\vnyx\vnyx-api`) for anything that touches Shopify |
| **Companion pages** | Capability matrix: https://claude.ai/code/artifact/d778ee83-c5d9-4632-92bd-77382581c645 · This plan, visualised (timeline, cut toggle): https://claude.ai/code/artifact/33b9ae22-b661-418b-a151-d256381b1c02 |

---

## 1. Purpose and scope

Bring every capability the standalone auditor (`vnyx-auto-approve`) has into the Hermes Auto Approval agent. Anything Hermes already does — in its own idiom — is skipped. Hermes may end up with more than the auditor; it must not end up with less.

The scope is the 33-row capability matrix from the companion page. Of those rows, **17 items** are absent or partial in Hermes and are ported here. **8 rows** are present in Hermes and skipped. **4 rows** are Hermes-only (durable queue, scheduling, shadow mode, credential isolation) and are untouched.

---

## 2. Pre-check finding: rejection *already tries* to unpublish

Before estimating, each candidate was checked against the code rather than the documentation. One finding changes the shape of the plan:

vnyx-api enqueues a `listing.unpublish` job the moment a product enters `REJECTED` or `DELETED` — `src/services/product-stage.ts:210-231` (`recordStageTransition` → `enqueueUnpublishForProduct`). The worker runs it. The Shopify adapter then does nothing:

```ts
// src/services/marketplace-sync/adapters/shopify.ts:141-147
async unpublishListing(_ctx, _externalListingId): Promise<void> {
  // Not on the current sync path (only listing.publish is dispatchable).
  // No-op rather than throw so a stray call can't DLQ.
}
```

`unpublishMarketplaceListing` (`sync-listing.ts:442-450`) then stamps the local row `ARCHIVED / SUCCESS` while the product stays live on the store. **That is the 170 rejected-but-live products.**

Consequence: "rejection unpublishes" is not a Hermes feature to port. It is a vnyx-api method to implement (~50 lines) plus a one-off re-enqueue for the 170. No Hermes change.

---

## 3. Two constraints that shape every port

Nothing is copied line-for-line. Two structural differences force a rewrite in Hermes's idiom:

### 3.1 Credential isolation

The auditor holds a Shopify Admin token and a VNYX API token on the machine it runs on and calls both directly. Hermes deliberately holds neither: every write goes through `POST /internal/auto-approval/step` on vnyx-api, which runs a named script from its own environment (`src/routes/internal-auto-approval.ts`).

So every auditor feature that touches Shopify — oversell guard, publication verification, unpublish, re-read verification, gallery reorder — becomes **a vnyx-api script plus a new step name**. Two consequences:

- The step enum is a `.strict()` zod schema. A step name vnyx-api does not know is a 400. **vnyx-api deploys before Hermes sends the new step** — the same ordering the `resync` step needed this week.
- The Hermes host still holds no keys after this plan. That is a property worth keeping.

### 3.2 Rule-engine shape

The auditor's `audit_product()` is one 860-line function with 63 `add("CODE", …)` calls that mix *checks* and *fixes* on a raw API dict. Hermes separates `Finding(rule_id="X.nnn")` (in `app/rules/*.py`, severities in `config/policy.yaml`) from planner actions (in `app/approval.py`) that repair. Roughly half the auditor's codes are fixes Hermes already performs as planner actions — those are not gaps. The full code-by-code mapping is in Appendix A.

### 3.3 Standing rules from this engagement

- **No database migrations.** No new tables, no Prisma schema change. The auditor's SQLite regen queue is therefore not ported as a table (see §8).
- **No vnyx-api environment changes.** Behaviour that only the agent wants is passed per call (as `--provider hermes` is today), never switched on globally.
- **DB writes only through the step runner**, never from Hermes directly.

---

## 4. Work packages

Estimates are focused working time for one engineer, given as low–high. Sizes are lines to write, not including tests. "Evidence" says why the item is worth doing — a measured number from this week's production runs, a documented incident in the auditor, or neither.

### WP1 — Runner safety · Hermes only · 2.6–3.35 h · **DELIVERED (§11)**

| # | Item | Auditor source | What is missing in Hermes | Where it lands | Size | Estimate | Evidence |
|---|---|---|---|---|---|---|---|
| 1 | **Vision-outage abort** | `vnyx_auto_approve.py:81-111` — `_gemini_ok/_err` counters, `abort_if_gemini_broken(min_samples=6, err_ratio=0.7)`, checked every 3 items *before* the write phase | Nothing counts Gemini failures. An outage today would surface as a queue of `HELD_FOR_HUMAN` — and, worse, as `NO_BRAND_OR_SIZE` rejections, because the label read fails and the post-chain reject fires | Counters in `app/llm/gemini.py` `GeminiEvidence._generate` (distinguish API error — 429/5xx/timeout — from a valid "uncertain" answer). Health check every 3 products in `runner.py pump()` and in `run_from_sheet.py Parallel` → stop the run with reason `vision outage`, release the claimed row, emit one run-level event. **Suppress the `_missing_attrs` reject when the label step failed for API reasons** — otherwise an outage mass-rejects, which is the auditor's documented incident | ~90 across 3 files | 45–60 min | Incident (auditor, 2026-07) |
| 2+3 | **Image quality gate** + **unverified-gender approval block** | `image_quality_gate.py` (122 lines) — one combined Gemini call returns `model_present, face_ok, gender, lead_ok, body_coherent, body_issue, view`; `Verdict.action ∈ ok/reorder/regen/review`. `approve_verify.py:197-224 preflight()` blocks approval on `GENDER_UNVERIFIED` | Hermes never looks at the lead image before approving. Gender is derived from master category or mannequin (`approval.py:452-508 _plan_gender`) and never confirmed against the photo | New `app/imaging/quality_gate.py` (prompt + `_VISION_SCHEMA`-style schema + `Verdict`). New `gate` step in `scripts/repair_product.py` between `render` and `approve`; a `regen`/`review` verdict sets `approve=False` and adds a blocker. Runner maps it to `HELD_FOR_HUMAN` with outcome `IMAGE_QUALITY` or `GENDER_UNVERIFIED`. Gender rule: gate's model-gender ≠ product gender → `regen`; gate says `Unknown` **and** the product's gender came from a `DATA.010` derivation → `GENDER_UNVERIFIED` hold. Accessories exempt from NO MODEL (policy list) | ~120 new + ~60 integration | 1.5–2 h | Incident (BOA-003977 broken body; BOA-005792) |
| 4 | **Regeneration canary** | `shopify_sold_backfill.py:338-345` — after the first PUT, re-read `generationStatus`; abort the run if it flipped to `GENERATING` | Nothing checks that a repair write did not trigger a paid regeneration | After the first product's first write in a run, re-read via `repair_product.needs()`; `GENERATING` → stop the run. In `runner.py` and `run_from_sheet.py` | ~25 | 20 min | Incident (auditor) |

### WP2 — Cache and observability · Hermes only · 1.9–2.4 h

| # | Item | Auditor source | What is missing in Hermes | Where it lands | Size | Estimate | Evidence |
|---|---|---|---|---|---|---|---|
| 5 | **Vision response cache** | `gemini_cache.py` (56 lines) — JSON file, key = namespace + normalised URL **keeping `?v=`** (Shopify bumps it when the file changes; stripping it poisoned the cache on 2026-07-29), never caches a failure, atomic write | No cache; every re-run re-reads every image | `app/llm/cache.py` — Redis-backed (Celery already has Redis), JSON-file fallback, key includes a prompt version. Wrap `audit_images`, `classify_background`, the care-label read, and the new gate. Never cache an empty/failed result | ~80 + 4 call sites | 45 min | None measured. Smaller win than in the auditor: Hermes reads each image once per run; the value is on `--retry` and re-verification |
| 6 | **Reviewable HTML grid** | `visual_grid.py` (552 lines) — contact sheet of a Shopify collection with Gemini flags and admin links | Run history is queryable per step but there is no visual sheet a person can scan | `scripts/run_grid.py` — one run id → HTML: lead image, SKU, verdict, outcome code, blockers, `edit_url`. Data from `AutoApprovalRunProduct` + `ProductMedia`; no Shopify scanning | ~200 | 45–60 min | None |
| 7 | **Offline self-test** | `--fixture` flag exercises the whole rule engine with no network and no writes | `tests/` has fixtures and `test_agent_cli.py`, but `repair_product.py` cannot run against a fixture file | `repair_product.py --fixture path.json` → snapshot from file, rules only, no DSN | ~60 | 30–40 min | None |

### WP3 — Rule-engine parity · Hermes only · 5.3–6.1 h

| # | Item | Auditor source | What is missing in Hermes | Where it lands | Size | Estimate | Evidence |
|---|---|---|---|---|---|---|---|
| 8 | **Missing rule codes** | 63 `add()` codes in `vnyx_auto_approve.py:1447-2310`; 67 documented | 43 `rule_id`s in `app/rules/`. Mapped by *meaning* (Appendix A): **11 checks are genuinely absent** — material too long; inventory ≠ 1; retail price below floor; supplier not canonical; condition not in tenant's grade labels; W/L size format; raw image as lead; stale gender word in description; vision sees wear on a grade-A item; title template; category ↔ image mismatch | One rule each in the matching `app/rules/*.py`, a `policy.yaml` severity entry, a test case. Category↔image and wear-on-A reuse evidence the gate / `audit_images` already return — no extra Gemini call | ~25 each ≈ 275 | 2.5–3 h | Auditor rule doc; none measured on Hermes |
| 9 | **Gallery order repair** | `vnyx_auto_approve.py:206-326` — `_img_kind` filename parser, `_GEN_SEQUENCE` position inference, `plan_image_order()` stable sort by canonical rank; returns `review` when no recognisable lead | `relabel` step fixes view filing only; nothing writes `position` | Canonical rank table in `policy.yaml` → `reorder-media.ts` (new vnyx-api script, new step) writes `position`. **No filename or generation-order inference needed**: `MediaAsset.view` is typed in the DB (`app/models.py:103-131`). Ship the TS script with the WP4 deploy so WP3 needs no extra deploy | ~60 TS + ~50 Py | 1–1.25 h | Auditor: order rules per channel (Etsy/Depop reject AI lead) |
| 10 | **C-twin inheritance** + cross-batch parent lookup | `vnyx_auto_approve.py:2853-2916`, `policy.py:327-359` — `TWIN_PREFIXES`, `TWIN_INHERIT_PROPS`, `TWIN_NEVER_INHERIT`, lazy tenant index | No notion of a twin | Prefix table per tenant in `policy.yaml`; `_twin_parent_sku()`; **one indexed query** (`Product WHERE sku = parent AND tenantId`) — no lazy index; `set_property` plan entries in `approval.py` before the gate for blank gender-neutral fields only; never gender, category, mannequin, sizing guide, EU size, title, summary | ~90 | 45 min | **Measured: 5 products this week** (337 `NO_BRAND_OR_SIZE` rejections, 42 twins, 37 whose parent is also blank) |
| 11 | **Tenant vocabulary** (`observed_subs`) | `vnyx_auto_approve.py:3265-3271, 1234-1238` — subcategories counted from the run, used as a candidate pool for `keep_category` tenants | Hermes already loads the tenant's real taxonomy, so the auditor's reason for this does not exist here | Tie-breaker only in `approval.py _plan_subcategory`: among valid candidates prefer the one this tenant already uses most (one `GROUP BY`, cached per run) | ~40 | 30 min | None — low value |
| 12 | **Consignment no-write** | `policy.py` price-floor / contractual rule | Floors exist; no "never write price for this tenant" rule | `policy.yaml` per-tenant `pricing.no_write: true` → `_price` step skipped, PRICE findings report-only | ~20 | 15 min | Contractual |
| 13 | **Paid-regen vocabulary** + **credit-resume safety** | `regen_queue.py:40-66` — `_HARD_REGEN` / `_SOFT_ONLY`; `claim_resumable()` never re-fires a spent credit | `render` runs off `generation_plan` (missing views), not off a vision verdict; so the vocabulary applies to the new gate's `regen` action | In `quality_gate.py`: only NO MODEL / BAD FACE / BROKEN BODY / gender mismatch may request a re-render; `BAD LEAD`, `GENDERLESS?` never. **Verify** (not assume) that `render` refuses to fire while `generationStatus = GENERATING` and not stale — `IMG.011` suggests it already does | ~15 + verification | 20 min | Incident (BOA-005792 binned as soft flag) |

### WP4 — vnyx-api side · TypeScript · 3.5–4.5 h

| # | Item | Auditor source | What is missing | Where it lands | Size | Estimate | Evidence |
|---|---|---|---|---|---|---|---|
| 14 | **Rejection unpublishes** | `approve_verify.py:110-141` — `unpublish_only()` / `draft_and_unpublish()`: `publishableUnpublish` from the storefront publications, then status DRAFT | See §2: the job exists, the adapter is a no-op | Implement `unpublishListing` in `adapters/shopify.ts` — `publishableUnpublish` for the account's `settings.publicationId[]` (`services/api-keys.ts` already stores them; `product.service.ts:615 publishablePublish` is the mirror to copy) + status `DRAFT`. One-off `scripts/reenqueue-unpublish.ts`: rejected products with an `externalListingId`, dry-run first, **verify one in Shopify admin before the 170** | ~50 + ~30 | 1–1.25 h | **Measured: 170 products live** |
| 15 | **Oversell guard** | `vnyx_auto_approve.py:3013-3039 sync_if_not_archived()` — refuse to sync an item Shopify has ARCHIVED (sold) | A re-drive re-asserts Active+published and un-archives a sold item | Before an upsert on a listing that has an `externalListingId`, read Shopify status; `ARCHIVED` → skip, record `SKIPPED / sold`. In `shopify-sync.worker.ts` upsert path so `resync-listings.ts` re-drives are covered | ~40 | 45 min | Incident (BEV-000075 oversold twice) |
| 16 | **Publication verification** | `approve_verify.py:143-158 _ensure_published()`; `policy.py:361+` — "ACTIVE is an admin state, not on the storefront" | Sync success = product upserted; nobody checks it reached the publications | After upsert SUCCESS, query `publishedOnPublication` for each configured publication; if not, `publishablePublish` and re-check; record on listing `meta`. `scripts/track_publish.py` reads the flag | ~60 TS + ~10 Py | 45–60 min | Incident (228 active-but-unpublished, auditor 2026-07-14) |
| 17 | **Per-item write-then-re-read** *(optional — recommend skip)* | `sync_verify.py` (124 lines) — poll until fields + media land, repush text once | Hermes verifies the push at end of run (`run_from_sheet.py` verification pass → `resync`) | `verify-listing.ts` step + poll after `approve`; drift → `APPROVED_UNVERIFIED` hold. Adds 15–60 s per approved product | ~120 TS + ~40 Py | 1.5 h | None beyond #16. **Recommendation: keep the end-of-run pass plus #16 and skip this** |

---

## 5. Execution order and deploy dependencies

```
WP1 ─▶ #14 ─▶ [deploy vnyx-api ①] ─▶ #15 #16 (#17) + reorder-media.ts ─▶ [deploy vnyx-api ②]
                                                                            │
                                    WP3 (#8 #9-Py #10 #11 #12 #13) ◀────────┘
                                                                            │
                                    WP2 (#5 #6 #7) ─▶ [deploy hermes worker] ◀┘
```

- **WP1 first**: pure Python, no deploy dependency, and it is the part that protects the unattended loop.
- **#14 second**: the only item with a measured cost right now. Ships in vnyx-api deploy ①.
- **All new step names ship in deploy ②** (`reorder-media`, `verify-listing` if taken) so WP3 needs no further vnyx-api deploy. Hermes must not send a step before the deploy that knows it (§3.1).
- **Hermes worker restart once**, at the end. The cloth-seg model is already on the server; nothing new to install.

---

## 6. Time

Planned at the **upper** estimate; the lower figure is shown for the range.

| Block | Low | High |
|---|---|---|
| WP1 — runner safety | 2.6 h | 3.35 h |
| WP4 — vnyx-api side (incl. #17) | 3.5 h | 4.5 h |
| WP3 — rule-engine parity | 5.3 h | 6.1 h |
| WP2 — cache & observability | 1.9 h | 2.4 h |
| Regression per WP (§7) | 1.5 h | 1.5 h |
| Deploys (vnyx-api ×2, Hermes ×1) | 0.75 h | 0.75 h |
| **Full parity (Cut B)** | **~16.5 h** | **~18.5 h** |
| Cut B without #17 | ~15 h | ~17 h |

At ~7.5 productive hours a day that is **two to two-and-a-half working days**.

### The two cuts

| | Contents | Work | All-in (with its regression + deploy) | What it buys |
|---|---|---|---|---|
| **Cut A — recommended first** | WP1 (#1, #2+3, #4) + #14 | 3.6–4.6 h | **~4–5 h** | Every item with a measured or incident-backed cost: the three safety features, the canary, and the 170 live rejections |
| **Cut B — full parity** | Everything in §4 | 15–16.5 h | **~16.5–18.5 h** | The rest: rules, twins, order, cache, grid — real, but their cost this week was 5 products (twins) or zero (no outage occurred) |

A 3–4 hour budget buys Cut A's code and most of its testing; it does not buy Cut B.

---

## 7. Testing protocol (per work package)

1. **Shadow first.** A 20-product sheet per tenant (BOAS, Kilo Kilo, Klekt, Midtex) through `run_from_sheet.py` with writes off. Compare verdicts to this week's completed runs: nothing that VERIFIED may now HOLD without a named new reason.
2. **Then five with `--approve`**, one tenant, watched. For WP4 items, confirm in Shopify admin (status, publications) — not from the listing row.
3. **Forced failure for #1**: run three products with an invalid Gemini key; the run must stop with `vision outage`, zero verdicts written, rows released.
4. **Gate calibration for #2+3**: run the gate read-only over the ~995 products approved this week; the false-block rate must be reported before it is allowed to block anything.
5. **#14**: one product, then the 170, dry-run listing first.

Every product through the full chain is ~2 min; a cut-out is ~36 s on the server; a gate call ~3 s. Test cycles, not typing, dominate the estimates.

---

## 8. Skipped — already present, or not applicable

| Auditor capability | Why not ported |
|---|---|
| Durable queue with leases, scheduling & windows, shadow mode, run history, credential isolation | Hermes-only; already better than the auditor's CSV ledger |
| Named held states (`APPROVE-ATTENTION` / `-REGEN`) | Present: `HELD_FOR_HUMAN` + outcome code |
| Per-tenant leniency profiles; ordered execution | Present: `policy.yaml` + severity overrides; steps settle fields before paying for renders |
| **Generation-order type inference** | N/A. The auditor had only URLs; Hermes has `ProductMedia.view` typed in the DB |
| **Regeneration queue as a SQLite table** | N/A. Hermes's Postgres claim/lease queue plus the product row's `generationStatus` *is* that queue. A side table would be a second source of truth, and migrations are out of scope. The two *behaviours* it carried (vocabulary, resume) are ported in #13 |
| Cross-batch parent lookup as a lazy tenant index | Collapses into one indexed SQL query in #10 |
| `shopify_sold_backfill.py` (Shopify ARCHIVED → VNYX archived reconciler) | Not in the matrix. A vnyx-api reconciler, ~1.5 h, if wanted — see §9 |

---

## 9. To confirm during the work

1. Does Hermes's `reconcile` step regenerate title and description the way `vnyx_regenerate.py` does (via `/analyze/regenerate-description`, ~2 credits per product)? If not: ~1 h as a vnyx-api script step.
2. Does `Product` carry Shopify tags? If not, `TAGS_MISMATCH` is N/A.
3. Is the sold→archived reconciler wanted (§8, last row)?
4. #14: DRAFT or ARCHIVED for a rejected product on Shopify? The auditor uses DRAFT + unpublish so a later approval can re-publish; ARCHIVED hides it from admin lists too.

---

## 10. How the estimates were made

For each item: the auditor's implementation was read (file and line ranges above), the corresponding Hermes code was read to find what exists, the integration points were counted (a new step name = a `.strict()` enum edit + a deploy-ordering constraint; a new rule = rule + policy entry + test), and a test cycle was added at the measured per-product cost. Value was checked against production before ranking — the C-twin figure (5) came from a query, and demoted that item from second place to tenth.

Not included: waiting on other people (a Shopify credential, a review), and anything in §9 that turns out to be wanted.

---

## 11. WP1 — as delivered (15 September 2026)

Tested against the **local** database only (`localhost:5433/vnyx-dev-2`), with a local vnyx-api on :8000 as the step runner. No migration was needed; no environment variable was added on either side. Two things ship: code, and two new blocks in `config/policy.yaml` (`llm.health`, `quality_gate`). The worker needs a restart to load them.

### What landed

| # | Behaviour | Where |
|---|---|---|
| 1 | **Vision-outage abort.** Every in-process vision call is counted by outcome over a sliding window (`llm.health`: window 20, min 6 samples, 70% errors, 600 s cooldown). Only *provider* failures count — `google.genai.errors.APIError`, `httpx.HTTPError`, connection/timeout errors; a parse failure or an "uncertain" answer never does. Between products the pump and the sheet driver ask `runner.stop_reason()`; on an outage the run stops, an event is written once, and the tenant is not pumped again until the cooldown clears the window. | `app/llm/health.py` (new); counters wired in `app/llm/gemini.py` `_generate` and both readers in `app/llm/care_label.py`; checks in `runner.pump()` and `run_from_sheet.py` (sequential and `Parallel`) |
| 1 | **A product the provider failed on is retried, not judged.** `care_label.read()` now distinguishes `api_error` from `no_answer` per provider and sets `api_failed` when every provider that could be asked failed (or none is configured). `repair()` puts such steps on `result["vision_unavailable"]`. The runner releases the row with the normal backoff while attempts remain, and holds it as `VISION_UNAVAILABLE` when they run out. **The `NO_BRAND_OR_SIZE` rejection is suppressed for these products** — the incident the guard exists for. | `care_label.read`, `repair_product._label_pass`, `runner.verify_one` |
| 2+3 | **Image quality gate.** New `gate` step between `render` and `approve`: one Gemini flash call on the live `AI_FRONT` (fallback `AI_FRONT_34`) returning model present / face ok / gender / lead ok / body coherent / body issue / view. `regen` → `HELD_FOR_HUMAN` with outcome `IMAGE_QUALITY` or `MODEL_GENDER_MISMATCH`; the move is withheld even under `--apply --approve`, and the pre-flight still runs so readiness is recorded. On a not-ready product the gate's reasons ride along in `preflightProblems`. Soft flags (BAD LEAD, gender unclear, lead shows the back) never block. Accessories exempt from NO MODEL. Runs in shadow mode too. | `app/imaging/quality_gate.py` (new); `repair_product.py` `_gate` / `_approve`; `outcome.classify` (`gate_blocked`, `gate_unavailable`); `policy.yaml` `quality_gate` |
| 2+3 | **Unverified-gender block, Hermes form.** The gate is the first time a photograph confirms the product's gender. A contradiction is `MODEL_GENDER_MISMATCH`; a gate that could not run is `VISION_UNAVAILABLE` and retried — never approve gender-blind. | same |
| 4 | **Regeneration canary.** `repair()` reads `generationStatus` before and after the chain; a flip to `GENERATING` that the render step did not cause sets `generation.regeneration_triggered`. The runner writes a `fail` event and returns the stop reason; the pump parks the tenant for `AUTO_APPROVAL_CANARY_PAUSE_S` (default 3600 s, process-local, no column written); the sheet driver stops submitting. | `repair_product.needs/repair`, `runner.verify_one/pump`, `run_from_sheet.py` |
| — | `verify_one()` now returns `str | None` — a stop reason, or nothing. Every existing caller may ignore it. `GeminiEvidence._fetch_images` now goes through `app.net.fetch_all` (IPv4 switch, phase timeouts, truncated-body retry) instead of a bare `httpx.Client`. | `runner.py`, `gemini.py` |

### Test results

| Test | Result |
|---|---|
| Unit — `tests/test_vision_health.py`, `test_quality_gate.py`, `test_outcome_gate.py`, `test_repair_gate_wiring.py` | **46 new tests pass.** Full suite 620 pass; the 4 failures in `test_column_drift.py` pre-date this work (mannequin rules) and are unchanged. |
| Gate calibration — 25 **APPROVED** Bleckmann products (approved by hand) | **0 refused** of 24 judged. 1 `VISION_UNAVAILABLE` (a genuine Gemini 503 "high demand"), correctly classed as retry, not refusal. |
| Gate calibration — 25 **REVIEW** products with a render | 22 ok · **1 refused** (BOA-006114, `MODEL_GENDER_MISMATCH`: a men's-presenting model in a red gown on a product listed as a women's denim vest — verified by eye, a real catch) · 2 `VISION_UNAVAILABLE` (one transient socket abort, one R2 fetch failure — both retry, not refusal). 2 soft "lead shows the back" flags; BOA-005445's `AI_FRONT` is indeed a back view — WP3 #9 territory. |
| Forced outage — 7 products, invalid `GEMINI_API_KEY` | 7/7 counted as API errors (400 `API_KEY_INVALID`), `health.check()` raised `VisionOutage` at 100% — **the guard trips**. Nothing judged: every product came back `review · VISION_UNAVAILABLE`. |
| Chain — `repair_product.py` dry run, BOA-006114 and BOA-006108 | Gate step ran on both; refused the first (reason appended to the approval blockers), passed the second. 47.6 s and 30.3 s. |
| Runner — `run_from_sheet.py` shadow, 2-product sheet | Both `HELD_FOR_HUMAN` (Hermes rule wins as the more specific cause). Rows carry `steps[gate]`, `deltas.gate`, `deltas.generation`; the gate's reason is in `preflightProblems`; events show `gate: REFUSED — …` / `gate: passed — on AI_FRONT`. |

### Known limits, stated

- **Gate latency is 8–14 s per product on this host**, not the ~3 s estimated — the flash call is ~3 s, the R2 fetch is the rest. The fetch now goes through the IPv4/retry client; measure again on the server.
- **`--workers` rows have no lease**, so a product the provider failed on stays `QUEUED` for the next invocation rather than backing off (the driver prints "NOT JUDGED … left queued"). Same gap the existing retry path has on that mode.
- **Counters are process-local.** The FastAPI service's own Gemini calls (evidence for `verify-and-repair.ts`) are not in the worker's window. The worker's calls — label read and gate — are where the damage would be done.
- The `gate_blocked` branch (ready product, bad render) could not be reached on real local data — no REVIEW product is both. It is covered by `test_repair_gate_wiring.py`.

---

## Appendix A — Auditor rule code → Hermes

`covered` = Hermes has the check under the named rule or planner. `planner` = a fix Hermes performs as an `approval.py` action, not a finding. `new` = WP3 #8 unless another item is named. `N/A` = the condition cannot arise in Hermes.

| Auditor code | Hermes | Notes |
|---|---|---|
| GENDER_FILLED | planner `_plan_gender` (DATA.010) | |
| GENDER_CASE | write path | Hermes writes the `[gender]` list shape |
| GENDER_UNRECOGNIZED, GENDER_UNDETERMINED | GENDER.001 | |
| GENDER_UNVERIFIED, GENDERLESS_MODEL | **new — WP1 #2+3** | first image-based gender read in Hermes |
| GENDER_SETTING_STALE, PROP_GENDER_MISMATCH | DRIFT.001 | column vs `properties` |
| MASTERCATEGORY_MISMATCH | TAX.001, TAX.004 | |
| PROP_MASTERCATEGORY_MISMATCH, PROP_INTERNATIONAL_SIZE | DRIFT.001 | |
| CATEGORY_FIX, CATEGORY_UNRESOLVED, CATEGORY_MISSING | TAX.002/003 + `_plan_subcategory` | |
| CATEGORY_IMAGE_MISMATCH | **new IMG.040** | rides on the gate's call |
| SIZING_GUIDE_MISMATCH | SIZE.011–014 | |
| MANNEQUIN_MISMATCH | TAX.005 | |
| SUPPLIER_MISMATCH | **new ATTR.005** | per-tenant canonical supplier in policy |
| EU_SIZE_WRONG | SIZE.002 + `_plan_eu_size` | |
| INTERNATIONAL_SIZE_MISMATCH | SIZE.001 | |
| WAIST_FORMAT, LENGTH_FORMAT | **new SIZE.004/005** | W32 vs 32; L34 vs 34 |
| SIZE_FALLBACK, SIZE_MISSING | DATA.010 + `NO_BRAND_OR_SIZE` default | added this week |
| SIZE_FROM_TITLE | TEXT.003 detects; **new** fill planner | small |
| SIZE_FROM_LABEL, SIZE_LABEL_MISMATCH, BRAND_FROM_LABEL, BRAND_LABEL_MISMATCH | `app/llm/care_label.py` + extract step | |
| BRAND_NORMALIZE, BRAND_FALLBACK | ATTR.001 + planner | |
| NO_BRAND, NO_LABEL_NO_DATA | `NO_CARE_LABEL`, `NO_BRAND_OR_SIZE` defaults | added this week |
| COLOR_MISSING | DATA.010 | |
| COLOR_CASE, COLOR_NORMALIZE, COLOR_NOT_CANONICAL, COLOR_UNKNOWN | ATTR.002 | tenant colour list membership |
| COLOR_NOT_IN_TITLE | TEXT.002 | |
| MATERIAL_TOO_LONG | **new TEXT.005** | |
| CONDITION_FALLBACK, CONDITION_INVALID | GRADE.001 partial; **new ATTR.004** | condition not in tenant's grade labels |
| GRADE_SUSPECT, GRADE_CONSERVATIVE, EXPECTED_BROKEN, CONDITION_GRADE_MISMATCH | GRADE.001–003; **new GRADE.004** | vision wear on grade A, from `visible_defects` already returned |
| RAW_LEAD | **new IMG.023** | |
| NO_PROCESSED_SET | IMG.010 | |
| IMAGE_DEFECT, IMAGES_NO_MODEL, IMAGES_MODEL_SHOTS, IMAGES_INCOMPLETE | IMG.001/002 + **WP1 gate** | |
| IMAGE_ORDER, IMAGE_ORDER_VISION_FIX, IMAGE_ORDER_UNCLEAR | **new — WP3 #9** | |
| TITLE_FORMAT | **new TEXT.006** + planner | title template |
| TITLE_CLEANUP | `--reject-placeholder-title`; **new** cleanup planner | small |
| SUMMARY_STALE | **new TEXT.007** | stale gender/size word in description |
| TAGS_MISMATCH | confirm (§9.2) | N/A if `Product` has no tags |
| INVENTORY_NOT_ONE | **new DATA.003** | DATA.002 checks negative only |
| PRICE_TOO_LOW | PRICE.003, PRICE.011 | |
| RETAIL_PRICE_LOW | **new PRICE.012** | floor from brand/category |
| TWIN_INHERITED | **new — WP3 #10** | |
