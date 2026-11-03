# Hermes background removal in the generation phase, Gemini only as the last resort

Plan, 3 Nov 2026 (revised the same day: no Gemini painting inside Hermes).

**Status, 3 Nov 2026: built, not deployed, nothing committed.** Hermes
(`_ai_gate`, `judge_pair`, `ai_check` on the endpoint and in policy; 12 new tests,
full suite 2,070 green), vnyx-api (`services/hermes-cutout.ts` with the breaker,
limiter and legacy tail; intake, decision, late cut-out, editor route and readiness
matte on it; `hermes` in the settings enum; 17 new tests, type-check clean) and
vnyx-ui (Hermes in both provider lists, result toast, type-check clean). Decisions
A–D below were taken with the recommended option. Deploy notes:
`docs/DEPLOY-BACKGROUND-REMOVAL.md` §27. Local DB only throughout; no DB change.

## 1. What is asked

1. **Hermes** cuts a photo out with **our own models only**: v3 (the bag parser),
   IS-Net (object and hanger cuts), v2 and v1 (the fine-tuned cloth-seg parsers).
   Every candidate passes the pixel checks it passes today **and a new AI check**
   (the cut-out against the raw photograph, the same Gemini judge the `cutcheck`
   step uses). A flagged candidate is refused and the next model runs.
2. Only when **none** of our models produced a good cut-out does Gemini extract the
   product from the raw photo — **the same Gemini prompt the generation phase uses
   today**, i.e. the backend's `vnyx-gemini` provider (the v7 endpoint at
   `vnyxremoveapi.vnyx.ai`). Hermes does not paint, mask or call Gemini for pixels
   at all; its `gemini-paint` / `openai-paint` strategies stay out of the chain.
3. **vnyx-api's generation phase** (the analyze worker, where every intake photo is
   matted today) uses Hermes as the segmenter.
4. If Hermes fails for any reason — unreachable, busy, timed out, no cut-out — the
   backend carries on exactly as it does today: VNYX Gemini, then V2, then the
   original photo.
5. The frontend gets one more provider option, **Hermes**, in the image editor and
   in the tenant's image-generation settings.
6. A decision on how many cut-outs run in parallel in Hermes and in the backend.

## 1a. The order, in one list

The generation phase calls the **same Hermes endpoint with the same hints** the
auto-approval `matte` step uses (`backfill-bg-removal.ts --provider hermes`, with
`garment` and `origin`). Hermes decides the order from the hints, as it does there:

1. **Hermes, our models** (one ORT slot, each candidate through the pixel checks and
   the new AI check; a refused candidate hands over to the next):
   - a bag → v3 bag parser, then IS-Net object cut
   - shoes → IS-Net object cut
   - a photo hung on the wall (origin WEB or MANUAL) → IS-Net hanger cut first
   - then, for every photo: **v2** (`cloth-seg-ft`), then **v1** (`cloth-seg-ft-backup`)
2. **Gemini v7** (`vnyx-gemini`, the backend's current intake call) — only when Hermes
   answered "no good cut-out", was busy, timed out or was unreachable.
3. **V2 at vnyxremoveapi** (`vnyxv2`) — only when Gemini v7 failed too.
4. **The original photo** — as today, when everything failed.

Gemini is never asked first and never asked by Hermes. The photobooth and decision
panel skip the hanger cut and start at v2, as the matte step does today.

## 2. What exists today (measured, not to re-derive)

### vnyx-api

| Where | What it does |
|---|---|
| `src/services/process-image-background.ts` | `removeBackground(blob, provider, hints)`. Providers `removebg`, `vnyx` (v1), `vnyxv2`, `vnyx-gemini` (v7, **the Gemini extraction used at intake today**) all post to `vnyxremoveapi.vnyx.ai` behind Cloudflare's 100 s cut-off. Provider `hermes` already exists: POST `/v1/imagery/remove-background`, 420 s timeout, **throws** on failure, no fallback. |
| `src/services/image-processing.ts` `removeBackgroundAI` | A ghost-mannequin prompt on Nano Banana Pro. **Dead code** by its own note: nothing in the analyse flow reaches it. Not the fallback. |
| `src/services/inspection.ts` | `resolveBgRemovalProvider(tenantId, productSnapshot, flow)`: the product's own `imageSettings.bgRemovalProvider` wins, then the tenant column for the flow (`web` / `decision` / `regenerate`), then `vnyxv2`. `BG_REMOVAL_RETIRED` + `BG_REMOVAL_REPLACEMENT=hermes` can re-route retired providers at resolve time (off by default). `bgRemovedForVision` / `bgRemoveForDecision` swallow errors and hand the input back. |
| `src/workers/analyze.worker.ts` ~1195-1311 | **The generation phase.** Per product: images in chunks of 5, `Promise.all` per chunk, `removeBackground(fileBlob, bgRemovalProvider)` with **no hints**. A failed image keeps its original URL. `ANALYZE_CONCURRENCY = 5` jobs, so up to **25 segmenter calls in flight** from this worker alone. The provider used is recorded into `product.imageSettings.bgRemovalProvider`. |
| `src/services/decision.ts` ~839-870, ~1020-1045 | Decision cut-outs and the late cut-out: **sequential**, 3 attempts, through `bgRemoveForDecision`. |
| `src/services/product-imagery.ts` ~270-330 | `segment()`: preferred provider, then `vnyxv2`; `removebg` disabled. Used by the readiness matte. |
| `src/routes/products.ts` ~2794-3010 | `POST /products/:id/remove-background`: the editor's "Change background". `provider` from the body is **unvalidated** and passed straight through, so `hermes` already reaches `removeBackground` and a Hermes failure is a 500. |
| `src/routes/image-generation-settings.ts` ~55-70 | Zod enum for the three tenant columns: `removebg`, `vnyx`, `vnyxv2`, `vnyx-gemini`. The Prisma columns are plain `String` — **no DB change** to add a value. |
| `src/index.ts` | Every BullMQ worker (analyze, regenerate, model-images, …) runs **inside the API process**, so one in-process limiter covers all callers. |

### Hermes

| Where | What it does |
|---|---|
| `app/main.py` ~881-1000 | `POST /v1/imagery/remove-background` (`CutoutRequest`: image, `strategies`, `skip`, `previous`, `frame`, `garment`, `origin`). Runs `cutout.remove_background`, then framing. `CutoutResponse` carries `provider`, `error`, `kept_existing`, `framing`, `refine`. |
| `app/imaging/cutout.py` ~2843-2880 | `remove_background`: holds one of `imagery.cutout.max_concurrent` ORT slots (`app/imaging/ort_memory.py`), waits up to `max_wait_s`, else answers "Hermes is busy" with provider `none`. |
| `app/imaging/cutout.py` ~2885-3440 | `_remove_background`: the chain, one loop over `(name, kind, fn)` candidates. Policy chain today is **already our models only**: `object-isnet`, `hanger-isnet`, `cloth-seg-ft` (v2), `cloth-seg-ft-backup` (v1), plus `cloth-seg-ft-bag` (v3) on the bags route. Every candidate passes the pixel checks (`_is_cutout`, stand, torn, backdrop, object checks). The paid strategies exist in code and are commented out of policy since 10 Oct 2026 — they stay out. |
| `app/imaging/cutout_integrity.py` | The AI check: `judge(pairs=[{view, cutout, original}], category, model)` sends the cut-out (on white, whole + top 38%) and the raw to Gemini flash, structured answer with defects `cloth_broken` / `background` / `colour` and severity; `_fails` decides what holds. Today it takes **URLs** of stored rows, and runs only after the cut-out is stored (readiness `integrity` check, chain `cutcheck` step). |
| `config/policy.yaml` ~727-861 | `imagery.cutout.strategies`, `max_concurrent: 4`, `max_wait_s: 300`. |
| `Dockerfile` | `uvicorn --workers 2`. The ORT slot semaphore is **per process**, so 2 workers × 4 = 8 cut-outs can run at once. Verify how prod is launched before relying on the cap (see §7). |

## 3. Target flow

```
analyze worker (per image, hints = origin + garment when known)
  │
  ├─ provider resolved to "hermes"?  no ──► legacy path, unchanged
  │                                   yes
  ├─ breaker open / healthz failed (cached 30 s)? ──► Gemini v7 → V2 → original
  ├─ take one of HERMES_BG_MAX_INFLIGHT (4) backend slots (wait ≤ 240 s, else Gemini v7 → …)
  ├─ POST /v1/imagery/remove-background
  │     Hermes, inside one ORT slot (max_concurrent 4):
  │       for each of our models, in the chain's fixed order
  │         v3 bag parser (bags) · IS-Net object (shoes, bags) · IS-Net hanger (WEB, MANUAL)
  │         · v2 cloth-seg-ft · v1 cloth-seg-ft-backup
  │           ├─ pixel checks (as today)        fail ──► next model
  │           ├─ AI check (new): cut-out vs raw  flagged ──► next model
  │           │                                  AI unavailable ──► accept (policy)
  │           └─ pass ──► framing ──► ok, provider = that model, ai_check = pass
  │       nothing passed ──► ok:false, error = every refusal, ai_check = last flag
  │
  ├─ ok ──► cut-out, log provider + ai_check, record provider "hermes" on the product
  ├─ ok:false / "Hermes is busy"        ──► Gemini v7 (vnyx-gemini) → V2 → original, this image only
  ├─ error / timeout / 5xx / refused    ──► open breaker 60 s; Gemini v7 → V2 → original
  │
  └─ Gemini v7 = removeBackground(blob, 'vnyx-gemini'), unchanged code; V2 = 'vnyxv2'
```

What does **not** change: the pixel checks and the chain order (both measured
properties), the readiness `integrity` check and the `cutcheck` step on stored rows,
and the Gemini v7 call itself. The v7 result is stored without an AI check, as it is
today [decision A offers to change that].

Cost of the AI check: one Gemini flash call per candidate that passes the pixel
checks. A good photo costs one call; a photo every model gets wrong costs up to four
(v3 and IS-Net only run on their routes, so three is the common worst case).

## 4. Hermes changes

### Modify

- **`app/imaging/cutout.py`**
  - In the candidate loop, after the last pixel check and before `return`: the
    **AI gate**. `cutout_integrity.judge_pair(data, out, category=garment)`; a
    `flagged` answer appends `"{label}: AI check — {defects}"` to `attempts` and
    `continue`s to the next model, exactly like a pixel-check refusal. An answer the
    AI could not give follows `ai_check.on_unavailable` (default `accept`).
  - Skip the gate for `frame-only` and for the keep-better comparison path (those
    judge nothing new).
  - `report["ai_check"] = {ran, flagged, model, verdicts: [{strategy, ok, defects, summary}]}`
    so the caller sees every model's verdict, not just the last.
  - No change to the chain itself: `gemini-paint` / `openai-paint` remain
    unlisted; the gate never adds them.
- **`app/imaging/cutout_integrity.py`**
  - `judge_pair(raw: bytes, cutout: bytes, *, category, model) -> (answer, note)`:
    one pair from bytes, same prompt/schema/`_prepare` as `judge`, cache keyed on
    sha256 of both buffers. `flagged(answer) -> list[str]` reuses `_fails`, so the
    gate holds for exactly the three defects the stored-row check holds for.
- **`app/imaging/ort_memory.py`** — no change needed: the gate is a network call
  made while the slot is held. Measured later; if the wait for Gemini (5-10 s) hurts
  throughput, split the slot then. Not before.
- **`app/main.py`**
  - `CutoutRequest.ai_check: bool | None` (None = policy; scripts can turn it off).
  - `CutoutResponse.ai_check: dict | None`.
  - `/v1/imagery/cutout` (URL in, PNG out): header `X-Cutout-AI-Check: pass|flagged|skipped`.
- **`config/policy.yaml`** — new block under `imagery.cutout`:

  ```yaml
  ai_check:
    enabled: true
    model: null              # null = llm.model_fast (gemini-2.5-flash)
    on_unavailable: accept   # the judge could not answer → the pixel checks decide
    max_concurrent: 4        # Gemini flash calls in flight (per process)
  ```
- **`docs/DEPLOY-BACKGROUND-REMOVAL.md`** — a section on the gate, the key it
  needs (`GEMINI_API_KEY`, already there for the rules) and the per-process slot
  caveat.

### Add

- **`tests/test_cutout_ai_check.py`** — pass; v2 flagged → v1 accepted; all
  flagged → `ok:false` with every verdict in `ai_check`; AI unavailable → accept;
  `ai_check: false` on the request skips it; `frame-only` skips it; the paid
  strategies never run whatever the gate says.
- **`tests/fixtures/cutout-ai-check/`** — one raw, one good cut-out, one with a stand
  left in (reuse what `tests/fixtures/` already holds where possible).

## 5. vnyx-api changes

### Add

- **`src/services/hermes-cutout.ts`** (new) — everything Hermes-specific that the
  callers share:
  - `hermesAvailable()`: `/healthz` probe (reuse `hermesHealth`, 3 s), result cached
    30 s; a **breaker** opened for `HERMES_BG_BREAKER_MS` (60 s) by a connection
    error, abort/timeout, 502/503/504 or non-JSON body. `ok:false` and "busy" do
    **not** open it (Hermes is alive, this photo just was not cut).
  - `withHermesSlot(fn)`: in-process limiter, `HERMES_BG_MAX_INFLIGHT` (4), queue
    wait `HERMES_BG_QUEUE_WAIT_MS` (240 s) after which the caller falls back.
  - `cutoutWithFallback(blob, provider, hints) -> { buffer, providerUsed, aiCheck, fellBack, keepCanvas }`:
    `hermes` → gate → slot → `removeBackground('hermes')` → on `ok:false`, busy or
    any error, the legacy chain `HERMES_BG_FALLBACK` (default `vnyx-gemini,vnyxv2`;
    `removebg` filtered as today) through the **unchanged** `removeBackground` →
    throws only when every provider failed, so the caller's existing "use the
    original" branch runs.
  - Absorbs `segment()` from `product-imagery.ts` so there is one fallback chain.

### Modify

- **`src/services/process-image-background.ts`** — parse `ai_check` from the Hermes
  body and return it on the `cutout` outcome; send `ai_check` through when the
  caller sets it. `removeBackground('hermes')` itself stays strict (throws): the
  keep-better path must never silently fall back to a provider that cannot guard.
  The `vnyx-gemini` branch is untouched.
- **`src/workers/analyze.worker.ts`** ~1260 — call `cutoutWithFallback` with hints
  `{ origin: 'WEB' | 'PHOTOBOOTH' | 'DECISION', garment }` (garment only on the
  post-decision path, where the category is known). Log the provider used per image.
  `CHUNK_SIZE` and `ANALYZE_CONCURRENCY` unchanged.
- **`src/services/inspection.ts`** — `bgRemovedForVision` / `bgRemoveForDecision`
  go through `cutoutWithFallback`; gain an optional `hints` argument.
- **`src/services/decision.ts`** — pass `origin` + `garment` hints (two call sites).
- **`src/services/product-imagery.ts`** — `segment()` delegates to the new module.
- **`src/routes/products.ts`** `/products/:id/remove-background` — validate
  `provider` against the enum (today anything goes), use `cutoutWithFallback`,
  pass `origin` + `garment` from the target row and product, and return
  `providerUsed` + `aiCheck` + `fellBack` so the editor can say what happened
  [decision B].
- **`src/routes/image-generation-settings.ts`** — add `hermes` to the three Zod
  enums and the swagger comment.
- **`src/routes/internal-auto-approval.ts`** — the agent's re-matte keeps
  `removeBackgroundKeepingBetter` (strict), but runs inside `withHermesSlot` so it
  shares the cap.
- **`.env.example`** — `HERMES_BG_MAX_INFLIGHT`, `HERMES_BG_QUEUE_WAIT_MS`,
  `HERMES_BG_BREAKER_MS`, `HERMES_BG_FALLBACK`. `HERMES_BG_REMOVAL_TIMEOUT_MS` can
  come down from 420 s: the chain is now four local models at ~30 s each plus
  four gate calls, so 240 s covers it.
- **tests** — `src/services/__tests__/hermes-cutout.test.ts` (new): breaker opens on
  ECONNREFUSED and not on `ok:false`; busy → legacy for one image; limiter holds at
  4; fallback order `vnyx-gemini` → `vnyxv2` → throw; strict path untouched.

## 6. vnyx-ui changes

### Modify

- **`src/lib/constants/bg-removal.ts`** — `{ value: 'hermes', label: 'Hermes (AI checked)' }`.
- **`src/components/product-settings/image-generation/ImageGenerationSettings.tsx`**
  — same option in `BG_PROVIDER_OPTIONS` (the three tenant dropdowns).
- **`src/lib/api/services/products.ts`** — `removeBackground` response type gains
  `providerUsed`, `aiCheck`, `fellBack`.
- **`src/components/product-edit/hooks/useImageEditor.ts`** — toast when
  `fellBack` ("Hermes could not cut this photo, used VNYX Gemini") naming the
  provider that produced the picture.

No new frontend files. The editor modal (`ImageEditorModal.tsx`) renders the
constant and needs no change.

## 7. How many run in parallel

| Stage | Today | Plan | Why |
|---|---|---|---|
| Hermes ORT cut-out slots | `max_concurrent: 4` **per uvicorn process** (Dockerfile runs 2 → up to 8) | **4 in total.** Keep 4 if prod runs one worker; set 2 if it runs two. Check the prod unit file first. | 4 cores / 31 GB; each full-size cut-out ≈ 0.8 GB and ~20-30 s of CPU. More slots on 4 cores only time-slice. |
| Hermes AI check | — | **4** Gemini flash calls in flight (`ai_check.max_concurrent`), made while the slot is held | One flash call per candidate; the slot holds ~5-10 s longer per candidate. Measure before splitting the slot. |
| Backend → Hermes | unlimited (analyze alone can send 25) | **4 in flight** (`HERMES_BG_MAX_INFLIGHT`), queue wait ≤ 240 s then legacy | Equal to Hermes' slots, so vnyx-api never fills Hermes' 300 s wait queue; the Hermes queue stays for scripts (`run_from_sheet`, `shopify_cutouts.py`) that call it directly. |
| Backend → Gemini v7 / V2 (fallback) | 5 jobs × chunk of 5 = 25 | unchanged | The fallback must behave exactly as today. The v7 service 500s on overlapping requests (decision.ts comment); nothing new there. |
| Decision flow | sequential, 3 attempts | unchanged | Two photos per hanger; not a throughput concern. |

Expected timing per photo through Hermes: one model ~20-40 s plus ~5-10 s for its
AI check; a photo every model refuses ~2-3 minutes before the backend turns to
Gemini v7 (~15-20 s). With 25 photos queued from one worker and 4 slots, the last
starts after ~3 minutes, inside the 240 s backend wait; past that it goes to Gemini
v7 rather than failing. The BullMQ job lock for the analyze queue has to be checked
against a product with 8 photos taking ~5 minutes of matte (today ~1 minute).

## 8. Failure matrix

| Event | Hermes answer | Backend does | Breaker |
|---|---|---|---|
| A model passes pixel checks and the AI check | `ok`, provider = that model | store | — |
| v2 flagged, v1 passes | `ok`, provider `cloth-seg-ft-backup`, both verdicts in `ai_check` | store | — |
| AI check unavailable | `ok` on pixel checks alone, `ai_check.ran=false` | store | — |
| Every model refused (pixel or AI) | `ok:false`, every refusal in `error` + `ai_check` | Gemini v7 → V2 → original, this image | no |
| Hermes busy past `max_wait_s` | provider `none`, "busy" | Gemini v7 → V2 → original, this image | no |
| Connection refused / timeout / 5xx / HTML body | — | open breaker 60 s; every Hermes call in that window goes straight to Gemini v7 → V2 | yes |
| Backend queue wait > 240 s | — | Gemini v7 → V2 → original, this image | no |
| Gemini v7 and V2 fail too | — | original photo kept (today's behaviour) | — |

## 9. Rollout

1. Deploy Hermes with `ai_check.enabled: false`, confirm `/healthz` and one
   `/v1/imagery/remove-background` round trip from the API host.
2. Deploy vnyx-api; nothing changes until a tenant selects Hermes.
3. Switch **one tenant** (BOAS test tenant) in Product settings → Image generation,
   `web` column first, watch `[bg-removal]` log lines for `providerUsed` and
   `fellBack` counts for a day.
4. Turn `ai_check.enabled: true` on Hermes; measure the flagged rate per model and
   how often the backend ends up on Gemini v7.
5. Widen per tenant. The env route-all switch (`BG_REMOVAL_RETIRED=vnyx-gemini,vnyxv2,vnyx`
   + `BG_REMOVAL_REPLACEMENT=hermes`) becomes safe once the fallback exists, but a
   per-tenant setting is the recommended switch [decision C].

Keys needed on the Hermes server: `GEMINI_API_KEY` (the judge; already there for
the rules) and `HERMES_CLOTH_SEG_FT_PATH` as today. No image-generation key.

## 10. Cost per photo (estimates, to be measured in step 4)

- AI check: one Gemini flash call per candidate with two ~1536 px images. Fractions
  of a cent; one to three calls per photo.
- Gemini v7 fallback: unchanged from today, and only for photos our models could
  not cut.

## 11. Decisions for the user

- **A.** Should the Gemini v7 result also go through Hermes' AI check before it is
  stored? Today it is stored unchecked. Recommended: not now; the readiness
  `integrity` check still looks at it afterwards.
- **B.** Manual "Change background" with Hermes selected and every model refused or
  Hermes down: fall back to Gemini v7 with a toast (recommended), or fail so the
  operator knows.
- **C.** Switch per tenant through settings (recommended) or route every tenant
  through the env retire switch.
- **D.** Run the AI check for every caller of `/remove-background` (agent re-matte,
  scripts) by policy default, with a per-request `ai_check` override (recommended),
  or only when the request asks.

## 12. Verification

- Hermes unit tests (§4) and vnyx-api unit tests (§5) green.
- Local run: Hermes on 8080 with `ai_check.enabled: true`, API on 8000, one BOAS
  product through `/analyze/v3` with the tenant on `hermes`; confirm the stored
  `-processed.png` and the log line naming the model and the gate result.
- One photo our models cannot cut: the log shows every refusal, then Gemini v7
  producing the stored cut-out.
- Kill Hermes mid-run: the remaining photos of the product come out through
  Gemini v7, no image keeps its background, the job finishes.
- Editor: select Hermes, Change background, see the provider used in the toast.
