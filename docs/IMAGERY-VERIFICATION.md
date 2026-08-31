# Imagery verification and on-model generation

How Hermes answers two questions about a product's pictures:

1. **Are the AI on-model renders there?** — and if not, generate them, including
   from a front photo alone.
2. **Have the backgrounds actually been removed?** — not "does the row say so", but
   does the picture agree.

Report-only by default. Nothing in this feature writes to the database, and Hermes
never connects to Postgres at all: vnyx-api hands it every fact in the request body,
exactly as `/v1/review-queue` already works. No schema change, no migration, no new
column.

---

## Why this exists

vnyx already generates cloth-on-model renders with Nano Banana Pro
(`services/banana-nano.ts`, `gemini-3-pro-image-preview`) and already removes
backgrounds (`services/process-image-background.ts`). Nothing checks whether either
actually *happened*. Generation can be skipped, can die mid-flight, can quota-fail
per view; background removal can fail-soft with a provider returning the original
bytes. The only way to notice today is for a human to open the product.

### The gap, measured

Read-only sweep of tenant `6045eee9-6b87-45f2-a582-2b47ea752c39` on `vnyx-dev-2`,
2026-08-24 — 2,026 live products (`isDeleted = false`, `isArchived = false`):

| finding | products | |
|---|---:|---|
| `IMG.001` no AI renders at all | **89** | **78 of them marked `generationStatus = COMPLETE`** |
| `IMG.002` a required view missing | 231 | 212 missing one, 19 missing both |
| `IMG.003` no garment photo to generate from | 189 | needs a reshoot, not a render |
| `IMG.005` ¾ / close-up absent (advisory) | 955 | |
| `IMG.010` garment image still has its background | 1,573 | *pre-`IMG.022`; see trap 3 — most were mis-filed, not un-matted* |
| `IMG.012` generation marked `FAILED` | 15 | |
| `IMG.011` generation stuck in flight | 8 | |
| `IMG.021` renders present but mislabelled | 404 | |
| `IMG.020` generation not applicable | 0 | this tenant sells no footwear |

**Bringing every product up to front + back costs 428 Nano Banana calls.**

### `generationStatus` is not a signal

78 of the 89 products with no model images at all are marked `COMPLETE`. The pipeline
finished, wrote no images, and said it was done — which is precisely why nobody has
noticed. `Product.aiGeneratedImages` is no better: it is one of the six legacy
`String[]` columns `ProductMedia` replaced, and it is empty both on products whose
renders are missing and on some that have them.

**The `ProductMedia` rows are the only truth.** Every check here reads them.

### The three sample products

`2b40ac19…`, `e5127957…`, `c1f34fa6…` share one exact pattern:

```
FRONT  BG_REMOVED  isCurrent   1787040843942-0-processed.png
FRONT  RAW         superseded  1787040796288-front-original.jpg   ← archived, correct
BACK   RAW         isCurrent   1787040797540-back-original.jpg    ← IMG.010
LABEL  RAW         isCurrent   1787040963202-care-0.jpg           ← excluded by design
SIZE_CHART RAW     isCurrent   3c34533a-….webp                    ← excluded by design
(no AI_* row at all)                                              ← IMG.001
```

The front was matted, the back original was left un-matted, and no render was ever
written — while `generationStatus` says `COMPLETE`. A back photograph **does** exist
on all three; it has simply never been through the segmenter.

---

## What counts as a live asset

```
isCurrent = true  AND  deletedAt IS NULL  AND  mediaType = 'IMAGE'
```

`isCurrent` is the materialized "nothing derives from this" flag. A `RAW` upload whose
cut-out exists is *superseded*, not live — counting it would report every successfully
matted product as still needing work. `deletedAt` is a human having removed the asset,
a different fact from being superseded. Both take an asset out of the gallery.

---

## Five traps in the real data

A naive check walks into every one of these and produces the same false-positive
flood the pricing validator once did. Four were found before the feature shipped;
trap 3 only surfaced when it ran against a second tenant, and it was the biggest.

### 1. 404 products have five `AI_FRONT` rows and nothing else

An older run filed every render under one view. Asking "does `AI_FRONT_34` exist?"
marks all 404 incomplete and would spend ~2,000 calls reproducing pictures the product
already has.

**Rule:** count *rows* as well as *distinct views*. `≥5` AI rows across a single
distinct view is `IMG.021` — a labelling defect, low severity, **never regenerate**.

### 2. 904 size charts are filed under view `OTHER`

`needsBackgroundRemoval()` includes `OTHER`, so applying it literally flags ~904 size
charts as un-matted. Their URL gives them away: the size-guide uploader writes under
`/size-charts/`, garment photography under `/products/`.

**Rule:** exclude `OTHER` rows whose URL carries a size-chart marker.

### 3. 5,889 products have their cut-outs filed under the wrong view

The largest false positive in the data, and it is not close. On BOAS:

| view | RAW | BG_REMOVED |
|---|---:|---:|
| FRONT | **6,514** | 35 |
| BACK | **6,236** | 21 |
| OTHER | 2,221 | **12,477** |

12,311 of those `OTHER` cut-outs are `-processed.png` files under `/products/` —
they are the FRONT and BACK cut-outs, written with the wrong view and **no
derivation edge** back to the original, which is why the originals still read
`isCurrent`. 5,889 of 6,585 products are in this state.

Reading `processing == RAW` literally therefore reports nearly the whole catalog
as needing background removal, and acting on it would re-mat twelve thousand
already-matted images: a provider bill, and a worse picture at the end of it
(matting an already-matted image erodes the silhouette further).

**Rule:** a RAW original is outstanding work only when no orphan cut-out could be
its counterpart — `needs_segmenter()`. Otherwise it is `IMG.022`, a labelling
defect. The difference on BOAS:

| | products | images |
|---|---:|---:|
| genuinely needs the segmenter (`IMG.010`) | **256** | 423 |
| already matted, mis-filed (`IMG.022`) | **5,528** | — |

Deliberately **not** matched pairwise. There is no honest way to say which
original a given cut-out came from — the derivation edge is absent and the
filenames share only the product folder — so the finding says "relabel", never
"these specific two are the same photo".

### 4. The required AI set is a policy choice, not a fact

The ¾ views were added by a later version of the pipeline. Demanding them makes 955
products defective for missing something most of the catalog never had.

**Decided:** required = `AI_FRONT` + `AI_BACK`. The ¾ views and close-up are advisory
(`IMG.005`, low). Lives in `config/policy.yaml` under `imagery.required_views`, so
raising the bar is a config edit and a `/v1/policy/reload`, not a code change.

### 5. Generation legitimately does not apply

- The tenant has `ImageGenerationSettings.isModelGenerationEnabled = false`.
- The product is **footwear** — the analyze worker skips it outright, because every
  `MannequinType` frames the item as apparel worn on a torso.
- There is no garment photograph at all (189 products) → `IMG.003`, a photobooth
  problem, not an AI one.

Reported as a *reason* (`IMG.020`), never as a violation. Footwear matching is ported
verbatim from `isFootwearCategory` in `helpers/formatters.ts` and is **word-boundary,
not substring** — a naive `'boot' in text` classifies BOOTCUT JEANS as footwear.

---

## Part 1 — Verification

### Two layers, split by cost

**Layer 1 — metadata rules. Pure CPU, no network.** A new `imagery` group in
`app/rules/__init__.py`'s `REGISTRY`, so it also runs free on every `/v1/review-queue`
page alongside pricing and taxonomy.

| id | severity | meaning |
|---|---|---|
| `IMG.001` | high | No AI renders at all, and the product *is* generatable |
| `IMG.002` | medium | A required view (`AI_FRONT` / `AI_BACK`) is missing |
| `IMG.003` | high | No garment photography — cannot generate |
| `IMG.004` | low | One usable source photo — a back render would be inferred |
| `IMG.005` | low | Advisory: ¾ or close-up absent; not a defect under current policy |
| `IMG.010` | high | A live garment image is still `processing = RAW` |
| `IMG.011` | low | Stale `isRegenerating` / `GENERATING` — report only |
| `IMG.012` | medium | `generationStatus = FAILED` |
| `IMG.013` | medium | Row claims the segmenter ran; the pixels disagree (layer 2 only) |
| `IMG.020` | low | Generation not applicable — carries the reason |
| `IMG.021` | low | AI renders present but mislabelled |
| `IMG.022` | low | Cut-outs filed under `OTHER` — relabel, do **not** re-mat |

`IMG.011` is deliberately **report-only**: `services/generation-reaper.ts` already owns
recovering stranded products, with two independent safety guards. Duplicating that fix
here could let a second job start and pay twice for the same work.

**Layer 2 — pixels. Network, single product only.** Never runs in the bulk queue path.
Download each live garment image and inspect with Pillow:

| observation | verdict |
|---|---|
| alpha channel + high fraction of transparent border pixels | genuine cut-out |
| opaque, near-uniform border colour | composited on the tenant's backdrop — legitimate (this tenant sets `background = #ffffff`, `autoApplyBackground = true`) |
| opaque, noisy border | **`IMG.013`** — the row claims `BG_REMOVED`, the picture disagrees |

Only genuinely ambiguous cases escalate to a Gemini flash vision call returning a
schema-forced `{background: transparent | solid_studio | real_scene, confidence}`,
reusing the `_generate` / `_schema_config` machinery already in `app/llm/gemini.py`.
Off unless `use_llm: true`.

Every finding carries `basis: "metadata" | "pixels" | "vision"`, so a reviewer can see
what judged it.

### The endpoint

`POST /v1/imagery/verify` — report-only, same discipline as `/v1/review-queue`.

```jsonc
// request
{
  "product":  { "id", "tenantId", "masterCategory", "category", "subCategory",
                "generationStatus", "isRegenerating", "updatedAt", "editUrl" },
  "media":    [ { "url", "view", "origin", "processing", "mediaType",
                  "isCurrent", "deletedAt", "position" } ],
  "settings": { "isModelGenerationEnabled", "isCloseUpEnabled", "isRemoveBgEnabled",
                "bgRemovalProvider", "background", "autoApplyBackground" },
  "use_llm": false,
  "check_pixels": true,
  "include_advisory": false        // count the 3/4 and close-up views as work
}

// response
{
  "product_id", "correct", "status",           // clean | needs_review | blocked
  "findings": [ Finding ],                      // the shape hermes-client.ts already types
  "generation_plan": {                          // THE DECISION — act on this
    "should_generate": true,
    "views": ["AI_BACK"],                       // empty when only matting is due
    "matte_first": [ { "url", "view", "processing" } ],
    "reason": "missing AI_BACK"                 // always set, both ways
  },
  "ai_views":    { "required": [], "present": [], "missing": [], "advisory_missing": [] },
  "backgrounds": [ { "url", "view", "processing", "verdict", "basis", "confidence" } ],
  "generatable", "not_generatable_reason", "source_images": [],
  "duration_ms", "llm_calls"
}
```

`Finding` and `Severity` are reused verbatim from `app/models.py`, so `HermesFinding`
in the TS client and anything already rendering findings work unchanged.

### Hermes decides; callers obey

`generation_plan` is the field a caller acts on. Everything else in the verdict is
the evidence behind it.

This was not the original shape. The verdict used to report only facts —
`ai_views.missing`, `needs_background_removal` — and each caller then worked out
"so should I generate?" for itself. Three of them did: the Model images button,
the sweep after generation, and `scripts/backfill-imagery.ts`. That is three
copies of one rule set, free to disagree on the questions that are not obvious:

  * does a mislabelled set (five `AI_FRONT` rows) count as missing? **No** — it
    needs relabelling, and regenerating it would spend ~2,000 calls reproducing
    pictures that already exist.
  * is matting alone worth a job? **Yes** — an original with no cut-out is a
    visible defect whether or not a render is also absent.
  * does an advisory ¾ view justify a call? **Only when asked for**, which is why
    `include_advisory` is a request field rather than something applied to the
    answer afterwards.

A backfill that quietly disagreed with the button would be discovered as a bill.
So the rules answer once, in `app/rules/imagery.py::generation_plan`, and the
callers obey.

**Check `should_generate`, never `len(views)`.** A product whose renders are all
present but whose originals were never matted has real work and an empty `views`.

### The log line

Every verify call logs one line, at INFO, under the logger `model-image-verifier`:

```
2026-08-27 11:10:53,765 INFO model-image-verifier product=37263600-… present=[AI_FRONT] missing=[AI_BACK] matte=0 -> GENERATE [AI_BACK] (missing AI_BACK)
2026-08-27 11:11:02,118 INFO model-image-verifier product=68117fe3-… present=[AI_FRONT,AI_BACK] missing=[] matte=0 -> SKIP (every required view is present …)
```

Both outcomes log, deliberately. A verifier that only speaks when it finds
something wrong cannot be told apart from one that is not running — which is
precisely how 265 products came to sit at `generationStatus = COMPLETE` with no
renders and nothing in any log either way.

### How the Model images button reaches it

The frontend cannot call Hermes directly — it binds `127.0.0.1:8080` and the Next.js
app is on Vercel. Everything goes through vnyx-api:

```
[Model images]  →  POST /review-verification/products/:id/verify-images   USER,  sync
                →  verifyProductImagery()   services/hermes-client.ts
                →  POST /v1/imagery/verify  (Hermes)          ~3s with pixel checks

[Generate …]    →  POST /review-verification/products/:id/repair-images   ADMIN, 202
                →  modelImagesQueue  →  workers/model-images.worker.ts
                →  generateProductImagery()  →  POST /v1/imagery/generate (Hermes)
                →  addWatermark → uploadImageBlobToR2 → addImages → clear isRegenerating
```

Split into two calls because they differ in cost and permission. Verifying is free
and read-only, so any **USER** may do it and it runs on click. Repairing spends
credits and writes to the gallery, so it is **ADMIN** — the same line
`/products/:id/remove-background` already draws — and it returns 202 because five
views take minutes.

The repair route verifies *before* enqueueing. That is what makes "nothing to do"
and "footwear, so never" instant answers instead of a minute of spinner followed by
an empty result.

Progress reuses `Product.isRegenerating` and the existing `useRegenerationPolling`
hook unchanged, so the gallery behaves exactly as it does after a tile regenerate.

Fail-soft like the rest of `hermes-client.ts`: Hermes down means "verification
unavailable", never a 502 that takes the product page down with it.

---

## Automatic sweep after generation

Generation reports `COMPLETE` whether or not it produced anything — 78 of the 89
products with no render at all are marked COMPLETE. So the check runs itself.

`analyzeWorker.on('completed')` calls `enqueueImageryRepair()` for the product
that just finished. Hooked on the worker event rather than at the four places
that write `COMPLETE`: one insertion point instead of four, and a future path
that completes some other way cannot slip past it. It never throws — an
unhandled rejection in an event handler takes the process down, and a safety net
must not become the thing that breaks the pipeline it watches.

The verify is metadata-only, so a healthy product costs milliseconds and queues
nothing. A job is created only when a required view is genuinely absent or a
garment original still needs the segmenter. `run-inspection` and
`regenerate-description` jobs share that queue and are skipped — they produce no
imagery. Set `IMAGERY_SWEEP_AFTER_GENERATE=false` to turn it off.

`enqueueImageryRepair()` is ONE implementation shared with the operator's
button, so their guards cannot drift: the job slot is claimed the same way, the
verify happens before the job exists, and `isRegenerating` is raised only once
the job is genuinely queued — and rolled back if the enqueue throws.

## Naming

Generated objects use the repo's canonical `mediaSlug()`, the same helper
`analyze.worker.ts` calls, so a render produced by the repair path is
indistinguishable from a first-pass one:

```
products/<userId>/boas-t-shirt-burgundy-gen-front-34-a1c324ea.jpg
                  └── brand ─┴─ category ─┴ colour ┘ └ marker ┘ └ uuid8 ┘
```

Brand, the SPECIFIC category (`subCategory` beats `category`) and colour, then
the marker, then eight hex characters of a uuid.

Two parts are load-bearing rather than cosmetic:

- **The marker.** `aiViewFromUrl` in `services/products.ts` types an AI render by
  parsing `-gen-front-34` / `-gen-back-34` / `-gen-closeup` / `-gen-back` back out
  of the url, and `isAlreadyBgRemoved` tests for `-bg-removed.` to decide the
  segmenter has run. Dropping either silently mistypes a render or re-mattes an
  image.
- **The suffix.** brand+category+colour+marker is identical for every render of a
  view on a product, so without it a regenerate OVERWRITES the previous object at
  the same key — the old url starts serving different bytes.

The slug context is its own query: `getProductById` does NOT include the brand
relation, so reading `product.brand?.name` off it yields `undefined` and a slug
quietly missing its brand.

## Part 2 — Generation

`app/imaging/nanobanana.py` is a faithful port of `services/banana-nano.ts`: same
`gemini-3-pro-image-preview`, same front-first-then-reference sequencing so one model
identity carries across every view, same top/bottom framing driven by
`category`/`subCategory` (**not** `mannequinType`, which only carries the model type
and reports "Men Top" for a pair of jeans), same 180°-rear language, same body-type
descriptors.

Prompt parity is load-bearing: a drifted prompt makes backfilled products visibly
different from API-generated ones in the same gallery. `tests/test_nanobanana_prompt.py`
pins the prompt text.

Three things it adds on top of the TypeScript version:

### Front-only products

`generateClothOnModel` takes a front *and* a back buffer; `regenerate-image.worker.ts`
quietly does `imageBuffers[1] || imageBuffers[0]`. Hermes makes that explicit: with one
source photo it passes the front for both slots **and** adds prompt language stating
the rear is not photographed and must be inferred from the garment's construction —
rather than handing the model a duplicate front and hoping. Flagged `IMG.004` so a
reviewer knows the back render is an inference.

On this tenant the case is currently **latent**: 14 products have a `FRONT` and no
`BACK`, but all 14 already have their renders. Worth building — it will occur — but it
is not what is affecting the three sample products, whose back photo exists.

### Gap-filling instead of full regeneration

Generate only the missing views, passing the product's existing `AI_FRONT` render as
the identity reference — exactly what `generateSingleImage(..., frontReferenceImage)`
already exists to do. The 212 products missing one view cost 1 call each instead of 5,
and the new render matches the model already in the gallery.

### Matting runs before generating

A product can reach Review with its FRONT matted and its BACK never touched. On
`c1f34fa6` the front has a proper cut-out with its raw upload superseded, while
the back is still `RAW` with nothing derived from it — the **Original** toggle
shows two photographs, the **Edited** view shows one. Generation then seeds from
an un-matted photograph, and the model is handed a garment hanging on a
stockroom wall.

So the repair flow is two steps:

1. **`matteGarmentImages()`** — every original without a counterpart goes through
   the segmenter, via `replaceWithDerived` so the cut-out takes the original's
   slot and supersedes it (leaving the Edited gallery correct and the pristine
   upload available under Original). The payload is then REBUILT, because those
   originals are no longer the live gallery.
2. **Generate**, now against the cut-outs.

The worklist is Hermes' `needs_background_removal`, never a local
`processing === 'RAW'` scan — two things look identical to that test and are not
work: a size chart mis-filed under `OTHER`, and an original whose cut-out exists
under the wrong view (`IMG.022`). Re-matting either pays a provider call to
erode a silhouette that is already done. An empty list means nothing is called.

**The configured segmenter is not always the one that works.** `vnyx-gemini`
(the v7 endpoint) is Gemini-backed, so it declines exactly the garments the
generation models decline — one layer earlier. On the `RODMAN 91` Bulls jersey
from `c1f34fa6`:

| segmenter | result |
|---|---|
| v7 (`vnyx-gemini`) — the tenant's setting | `500 Gemini did not return an image. finish_reason=IMAGE_OTHER` |
| **v2 (`vnyxv2`)** | **clean RGBA cut-out in 8.6s** |

That is *why* that product's back was never matted and only its front reached the
Edited gallery — not a missed run, a refusal. So `segment()` falls through
`BG_FALLBACK_PROVIDERS` on failure; without it the matting pass would fail on
precisely the products it exists to fix.

Matting is **serial**, unlike generation. Every provider path retries a 429 by
sleeping in a loop, so firing several at once turns one rate limit into several
stalled requests holding the worker open. A failure on one image is reported and
the rest continue; the count comes back as `matted` so the UI can say the Edited
gallery was fixed even where a view was later refused.

### Which API key generates

`GOOGLE_NANO_BANANA_API_KEY`, and only that one. `GEMINI_API_KEY` drives the text
and vision evidence layer and is never used for generation.

Separate because the two have separate quotas and separate costs: a rules-only
deployment needs no image key at all, and an image backfill exhausting the image
quota must not take the verification path down with it. vnyx-api draws the same
line in `utils/rate-limiter.ts`.

**Comma-separated, rotated round-robin**, parsed exactly as that file parses it.
The rotation is load-bearing rather than decorative — up to four views generate
concurrently and every retry and fallback adds another call, so a single key
meets its per-minute limit quickly. `_next_key()` takes a lock because those
views run on a thread pool; an unguarded read-modify-write would hand the same
key to two concurrent calls and skip another.

Unset means `/v1/imagery/generate` answers 503 with that named in the message.
Verification is unaffected and stays free.

### The tenant's cast of models

`ImageGenerationSettings` carries more than a default gender and age: a tenant
defines named **personalities** — BOAS has 20, each with a gender, age, skin tone,
hair colour and hair style — and the pipeline picks one per product so the catalog
does not look like one person wearing the entire shop.

The first port of `build_prompt` omitted that block entirely, so every render came
out generic and the whole configuration had no effect. `choose_personality()` now
mirrors `analyze.worker.ts`:

- gated on `personalitiesEnabled`, skipping entries with `enabled: false`
- **gender-filtered against the product**, so a women's blouse never draws a male
  model; an entry with no gender set is legacy and stays eligible for anything
- the personality's own `age` overrides the tenant default — a cast entry's "24"
  is the point of having named models
- `'any'` never reaches the model as a literal: "any skin tone" is an instruction,
  and a worse one than saying nothing

Also now honoured: `lightingTexture` (suppressed when `none`) and
`realisticSkinDetails`, both appended the way the regenerate modal appends them.

**The chosen personality is stored on `Product.imageSettings` and reused.** A view
filled in later has to show the SAME face as the renders beside it; drawing a fresh
personality would describe a different one while the reference image shows the
original, and the two instructions fight. Hermes returns `image_settings`, the
repair path merges it into the existing column rather than replacing it (that
column also records which segmenter and backdrop produced the cut-outs, which this
path learns nothing about).

The tenant's `customPrompt` leads the composed prompt, exactly as
`banana-nano.ts` places it — BOAS's is 2,825 characters covering garment fidelity,
background, exposure and styling. Putting it last, or letting it replace the
structured text, would make renders from this path differ from the ones already in
the gallery.

### Source selection from typed rows

The TypeScript path uses `resolveImagesByView` on filename keywords. Hermes has the
real `view` column, so it picks `FRONT` / `BACK` directly and prefers the `BG_REMOVED`
derivative over its `RAW` original.

### Persisting the result

`POST /v1/imagery/generate` takes source image URLs plus resolved settings and returns
the renders **as base64, with a per-view result**. Hermes stores nothing and holds no
vnyx or R2 credentials.

vnyx-api persists through its own `addWatermark` → `uploadImageBlobToR2` → `addImages`,
so `position`, `isCurrent`, `derivedFromId`, the `Product.images` cache rebuild and the
credit deduction all stay in the one file that owns them. Re-implementing `addImages` in
Python would put those invariants in two languages.

Five views take ~205s — too long for a button to block on — so the vnyx-api side reuses
the existing async pattern: set `isRegenerating`, enqueue, return 202, frontend polls.
`/analyze/regenerate-image` cannot serve this itself: it 400s when a product has no AI
image yet, which is exactly the 89.

### Background removal needs no new code

`POST /products/:id/remove-background` already does the right thing —
`replaceWithDerived`, the tenant's `bgRemovalProvider` (`vnyx-gemini` here), dimension
measurement. The verify response names the offending URLs; the UI calls it per URL.

---

## Two things that only showed up when it ran for real

### The model refuses some garments, and that is not a bug

Product `2b40ac19…` is catalogued as "Vintage adidas Black Tank Top". It is a Toronto
Raptors jersey: the front reads `TORONTO 10`, the back reads **`DEROZAN 10`**. Gemini
declines to render a person wearing a named real athlete's kit and returns
`finish_reason = IMAGE_OTHER` with no image and no exception.

Measured, three runs each: the front generates every time, the back is refused every
time. Nothing about the prompt, the image size, the key or the quota — an hour went
into eliminating each of those before looking at the picture.

A refusal does not always announce itself. An Aaron Rodgers jersey — `RODGERS
12`, no photograph of anyone, just a named player — came back with **no
finish_reason at all**, an empty response in four seconds. The escalation was
gated on matching a refusal code, so it read as "not a refusal", nothing was
tried, and the product was reported unfixable without a single fallback call.

The gate is now narrower in the right way: **anything that produced no image
escalates**, and only a TRANSIENT fault (already retried inside `_one`) stops the
chain. The reason is also read from all three places it can hide —
`candidate.finish_reason`, `prompt_feedback.block_reason` (set when the request
is blocked outright, leaving `candidates` empty), and any text part the model
answered with instead.

Two further consequences, both implemented:

- **The reason is reported.** A bare "no image returned" is what sent that
  investigation the wrong way. `finish_reason` now reaches the caller, and a refusal
  is labelled as the model declining rather than as a fault.
- **Front-facing views retry once without the back reference.** Both photographs are
  attached to *every* view's request, so one offending photo blocks views that do not
  depend on it. Dropping it recovers the front — verified: `AI_FRONT` produced a
  correct 1728×2432 render of a male model in the jersey. Back views are **not**
  retried this way: dropping the back photo there does not route around the refusal,
  it invents the reverse of a licensed garment.

A second product settled what the limit actually is. `bf86ee4f…` is catalogued as
"Vintage Ace Ventura Black T-Shirt"; the print is three photographs of the actor's
face. Both views refuse, and the front-only retry refuses too. Then the decisive
test — asking for a **ghost-mannequin shot with no person at all**:

| prompt | result |
|---|---|
| model wearing the garment | `IMAGE_OTHER` |
| ghost mannequin, explicitly no person, no body, no skin | `IMAGE_OTHER` |

So the trigger is the **printed artwork**, not the act of rendering a person, and
there is no prompt-side workaround.

There is, however, a MODEL-side one. The same prompt and image across every
available model, on a Bruce Springsteen tour tee:

| model | result |
|---|---|
| gemini-3-pro-image-preview | refused |
| gemini-3-pro-image | refused |
| gemini-3.1-flash-image | refused |
| gemini-3.1-flash-lite-image | refused |
| **gemini-3.1-flash-image-preview** | **rendered it** |
| **gemini-2.5-flash-image** | **rendered it** |

Hence `imagery.generation.fallback_models`, and the three-step escalation in
`_attempt`: primary with both photos → primary with the back dropped (front views
only) → each fallback in turn. Measured outcomes on the three problem products:

| product | print | outcome |
|---|---|---|
| Raptors jersey | `DEROZAN 10` on the back | front rendered once the back photo was dropped |
| Springsteen tee | band photo, front and back text | **2/2** via `gemini-3.1-flash-image-preview` |
| Ace Ventura tee | three photos of the actor's face | back rendered; **front refused by all three models** |

So the hard limit is real but narrower than it first looked: it is per-VIEW, not
per-product, and only bites when that specific side carries the likeness. A view
nothing will render needs a photograph, or a different provider —
`services/openai-image-generation.ts` is already wired behind
`IMAGE_GENERATION_PROVIDER` and untested against this case.

### One model for the whole set

The escalation used to run PER VIEW, and that produced a visible defect: on the
DeRozan jersey the primary rendered the front and refused the back, so the front
came from Gemini and the back from gpt-image — **two different men in one product
gallery**. Worse than a missing image, because it ships looking deliberate.

Two causes, both fixed:

- The fallback vendor was never handed the front render. `build_prompt(...,
  has_front_reference=False)` and garment photos only, so it had nothing to match
  and invented its own person.
- Escalation was per view, so two views could legitimately land on two vendors.

`generate()` now escalates over the SET: every requested view is tried on one
model, and only if that model cannot do all of them does the next get a turn. The
common case costs exactly what it did before — one call per view on the primary.
Only a product that needs a fallback pays for the retries, and it gets a
consistent set in exchange. When nothing can do the full set, the most complete
attempt is returned rather than nothing.

The cheap recovery is kept but moved INSIDE a model: a front-facing view that
came back empty is retried once without the back photograph, on the same model,
so recovering it cannot introduce a second one. Never for back views — dropping
the back photo there invents the reverse of a licensed garment rather than
routing around the refusal. Never after a transient fault either, which says
nothing about the garment.

**Across runs**, the model that produced a product's renders is recorded as
`imageSettings.generatedWith` and goes first in the chain next time, so a view
filled in months later is made by the same model as the ones beside it. If that
model can no longer do the job, the response says so explicitly rather than
quietly shipping a mismatch.

### A repair mattes FRONT and BACK, and nothing else

Two different questions, deliberately answered by two functions:

| | `needs_segmenter` (IMG.010) | `needs_matting_for_generation` (`matte_first`) |
|---|---|---|
| asks | which garment photographs are un-matted? | what is worth a provider call before generating? |
| views | `garment_views` — FRONT, BACK, **OTHER** | `matte_views` — FRONT, BACK |
| mis-filed cut-outs | exempt the product | do **not** exempt it |

Generation seeds from FRONT and BACK (`source_images`), so matting the extra
OTHER angles buys nothing for the render — on one product that was four
segmenter calls to produce two useful cut-outs.

The second row matters more. `needs_segmenter` exempts a product when it has at
least as many orphan cut-outs as RAW originals: 5,889 products have their
cut-outs filed under OTHER, and re-matting them all would be twelve thousand
pointless calls. Correct for a catalog report — wrong for generation, because a
cut-out under OTHER cannot be identified as *the front*. On `d29c474f` that
exemption meant both renders seeded from un-matted originals: a garment
photographed on a stockroom wall.

So the repair's test is per-view and literal — **a FRONT or BACK that is RAW and
has no background-removed sibling of its own view gets matted; one that already
has a cut-out is left alone and the existing one is used.** IMG.010 keeps
reporting the wider set; it is simply not work this repair pays for.

### The chain is deliberately short: one Gemini, then a different vendor

`fallback_models` ships **empty**. The escalation order is
`gemini-3-pro-image-preview` → `gpt-image-1.5`, matching what
`services/banana-nano.ts` has always done: one model, no chain.

Other Gemini names genuinely do sometimes render a garment the primary refuses
— the measurements below are real. They are no longer listed because of what
the chain costs when it does **not** work, which is the common case. Each extra
model is a full set of attempts at ~25s per view; on a refused two-view product
the walk from primary through both fallbacks to gpt-image took **265–295s**, of
which ~200s was Gemini models declining in sequence. A refusal is a policy
decision about the print, so a second Gemini rarely disagrees; the change of
vendor is the escalation that actually pays.

Re-add a name to `fallback_models` if a class of garment turns up that
gpt-image also refuses — the mechanism is unchanged, only the list is empty.

### gpt-image renders are conformed to the tenant's shape

Gemini accepts an `aspect_ratio` and returns that shape. gpt-image offers three
fixed canvases (1024×1536, 1536×1024, 1024×1024), so a 5:7 tenant — whose
Gemini renders come back **3:4** — got **2:3** from the fallback. Two shapes in
one gallery, and the product page crops the taller one, cutting off the
bottom-right corner where the `vnyx.ai` watermark is applied. The missing
watermark was the symptom; the mismatched gallery was the defect.

`openai_image.conform_to_ratio` centre-crops the render to the **resolved**
ratio — the same 3:4 Gemini was given, not the raw 5:7 — so both vendors'
views are identical in shape. Cropped rather than scaled: squashing a person to
fit a ratio is worse than losing a little headroom, and the prompt puts the
garment mid-frame so a centred crop keeps it. Best-effort — an unreadable or
un-croppable render is returned as-is, because a wrongly-shaped render still
beats no render at the end of an escalation chain.

### When no single model can do the set

A John Cena WWE tee settled what happens then. The print is a photograph of the
wrestler's face, and the models split:

| model | front | back |
|---|---|---|
| gemini-3-pro-image-preview | refused | refused |
| gemini-3.1-flash-image-preview | refused | **rendered** |
| gemini-2.5-flash-image | **rendered** | refused |
| gpt-image-1.5 | `400 moderation_blocked` | rendered |

Both views were producible; no single model could produce both. Keeping one
model's partial therefore reported *"could not generate the front"* while a
perfectly good front sat in memory — which is exactly what an operator hit.

So the resolution order is:

1. **A consistent complete set** — every view from one model. Always tried first.
2. Failing that, and with `allow_mixed_models` on (the default), **the complete
   set assembled from whichever models managed each view**, flagged `MIXED
   MODELS` so nobody ships it unaware. The first model to render a view wins, so
   the primary's output is never replaced by a weaker model's.
3. With that switch off, the best single-model partial instead — matching but
   incomplete.

Completeness is the default because a missing image is not something an operator
can judge, and a warned mismatch is.

OpenAI signals its refusals as **`400 moderation_blocked`**, not a 200 with an
empty body — a different shape from Gemini's, and one that must not be mistaken
for a malformed request. Confirmed against that front with both the full prompt
and a short neutral one: the image is what is rejected, not the wording.

### The last step: a different vendor

Asked to make the refusals go away, the tempting move is to tell the model it
must comply. That does not work and is not attempted here — the block is on the
REQUEST (`prompt_feedback.block_reason`, zero candidates, under four seconds
against ~25s for a real render), so a classifier rejects it before the generation
model reads any instruction. The prompt is part of what is classified.

What does work is a different vendor. Measured on the `RODGERS 12` Packers
jersey, the one Gemini would not touch under any wording or model:

| provider | result |
|---|---|
| gemini-3-pro-image-preview | `prompt blocked: BlockedReason.OTHER` |
| gemini-3.1-flash-image-preview | `prompt blocked: BlockedReason.OTHER` |
| gemini-2.5-flash-image | `FinishReason.IMAGE_OTHER` |
| **gpt-image-1** | **rendered it correctly** |

So `app/imaging/openai_image.py` is the final step of the escalation, gated on
`imagery.generation.openai_fallback` and an `OPENAI_API_KEY`. It is reached only
after every Gemini model has produced nothing, so it costs nothing on the
overwhelming majority of products, and it is a procurement choice for a lawful
use — the shop owns the garment and is selling it — not an attempt to get around
the first vendor's decision.

Aspect ratio maps onto gpt-image-1's fixed canvases (`5:7` → `1024x1536`); it has
no arbitrary-ratio option.

Because a run can therefore succeed partially, the worker returns its
`RepairResult`, the status endpoint serves it, and the UI names the views that
were produced and the ones that were not — rather than reporting an unqualified
success over a half-filled gallery.

How much of the catalog this affects is not knowable from titles. On BOAS (6,585 live
products) only 49 — 0.7% — name a team, film or band outright, but a graphic tee whose
print happens to be a face is invisible to a title search. The rate will only be known
by running it.

### A refusal message that was not a refusal

Worth recording because the symptom pointed straight at the wrong layer. An
operator pressed Generate on a product, waited, and was told *"the image model
declined this garment"* — repeatedly, including after the fallback chain landed
and that same product had been verified generating 2/2 views.

Nothing was being generated at all. `Queue.add()` is **idempotent on a custom
jobId**: with a retained record under that id — completed or failed — it returns
the existing job and enqueues nothing. The route still answered 202, the UI still
spun, and the status endpoint then served the dead job's `failedReason`.

The id was only freed when `Product.isRegenerating` was true, and the worker's
`finally` clears that flag on failure — so the cleanup was skipped in precisely
the case that leaves a dead record behind. Every retry after the first failure
replayed the first failure's message.

Now `claimJobSlot()` (services/product-imagery.ts) always frees a terminal
record and blocks only on a genuinely live job, with the invariant covered in
`test-cases/services/product-imagery-job-slot.test.ts`.

**The lesson worth keeping: a stale error and a fresh one are indistinguishable
to the person reading it.** Any surface that replays a stored failure needs to be
certain the work actually re-ran.

### Transient 504s, and a 40-second IPv6 stall

`504 DEADLINE_EXCEEDED` arrives about four seconds into a request that normally takes
twenty-five. Retried in `_attempt` (2 retries, 3s backoff, transient faults only) —
inside the view rather than at the worker, because a worker-level retry re-runs the
whole job and redoes the views that already succeeded. A refusal is never retried:
same request, same answer, twice the wait.

Separately, on this dev host the *first* connection to a host stalls for 40-60s
before falling back to IPv4:

| | default | forced IPv4 |
|---|---:|---:|
| R2 image | 43.1s | 1.2s |
| Gemini API | 64.4s | 0.3s |

Set `HERMES_IMAGE_FETCH_IPV4=true` (see `.env.example`). It applies to both the image
fetches and the Gemini SDK, and takes verify from 31.9s to 2.8s. Leave it **off**
anywhere IPv6 works — binding to `0.0.0.0` makes an IPv6-only network unreachable.

---

## The audit sweep

`scripts/audit_product_images.py` produces the spreadsheet the numbers above come from.

```bash
python scripts/audit_product_images.py --tenant 6045eee9-6b87-45f2-a582-2b47ea752c39
python scripts/audit_product_images.py --all-tenants --limit 500
```

**Read-only.** SELECT only; there is deliberately no `--apply` flag, no UPDATE and no
DDL, and the connection is opened `read_only`. Writes to
`reports/imagery-audit-<tenant>-<date>.xlsx` with three tabs:

- **Summary** — per-finding counts and headline numbers.
- **Findings** — one row per affected product, worst-first, with `AI views present` /
  `missing`, `Un-matted images`, `Finding ids`, a plain-words `What to do`, and an
  `Edit URL` deep link built the same way `buildEditUrl` builds it.
- **Un-matted images** — one row per `RAW` image, since the fix is per-image.

It shares `REQUIRED_AI_VIEWS`, the size-chart exclusion and the footwear regex with the
rule engine, so the spreadsheet and the Verify button never disagree about a product.

---

## Where the code lives

**Hermes** — `app/rules/imagery.py` (layer 1), `app/imaging/background.py` (layer 2),
`app/imaging/nanobanana.py` (the generator port), `app/net.py` (parallel fetch + the
IPv4 switch), the two endpoints in `app/main.py`, `imagery:` in `config/policy.yaml`.
Tests: `tests/test_imagery_rules.py` (27), `tests/test_background_detection.py` (10),
`tests/test_nanobanana_prompt.py` (33).

**vnyx-api** — `services/product-imagery.ts` (payload + persistence),
`services/hermes-client.ts` (two new calls), `workers/model-images.worker.ts`,
`modelImagesQueue` in `lib/queue.ts`, two routes in `routes/review-verification.ts`.
No schema change, no migration.

**vnyx-ui** — `components/product-edit/ModelImagesButton.tsx`,
`lib/api/services/product-imagery.ts`, wired into `ProductEditTopBar` and
`ProductEditView`.

## Known limits

- **`IMG.010` will dominate.** 1,573 of 2,026 products have an un-matted image. Correct,
  but loud enough that the review screen probably wants its own filter for it rather
  than mixing it in with the pricing findings.
- **The pixel heuristic is a heuristic.** A garment shot against a plain white wall
  looks like a studio composite. That is what the vision tie-breaker is for; without it
  `IMG.013` stays advisory rather than blocking.
- **Inferred back renders are inventions.** Flagged `IMG.004`, but whether that is
  acceptable for the catalog is a business call.
- **Fetch cost.** A product with 12 images means 12 downloads per click. Capped by
  `imagery.max_images_fetched`, and the reason Layer 2 never runs in the bulk path.
