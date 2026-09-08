# Hermes

A verification agent that sits behind your VNYX enrichment pipeline and checks
AI-generated product records before they reach a sales channel. It finds
contradictions, repairs the ones it can prove, and escalates the ones it can't.

Runs as a self-contained FastAPI microservice. Gemini supplies evidence;
deterministic rules make every decision.

---

## The core idea

> **The model never writes a value. It only supplies evidence.**

The bug you described — selling price above retail price — is arithmetic, not
judgement. It should never have been a model's decision in the first place. So
Hermes splits the work in two:

| Layer | Job | Powered by |
|---|---|---|
| **Rule engine** | Detect contradictions. Compute repairs. | Pure Python. No network. ~10ms. |
| **Evidence layer** | Answer questions the rules can't: *what did this actually retail for? does the photo match the record?* | Gemini + Google Search grounding + vision |
| **Resolver** | Decide which side of a contradiction to move | Provenance ranking |

Gemini is explicitly forbidden from writing `price`, `retail_price`,
`inventory`, or any identifier — see `guardrails.llm_forbidden_fields` in
`config/policy.yaml`. If a model response contains one of those fields, it's
dropped before the resolver ever sees it.

---

## Pipeline

```
VNYX enrichment finishes
        │
        ▼
   POST /v1/webhooks/product-enriched
        │
        ▼
┌───────────────────────────────────────────────┐
│ 1. DETECT     run_all()  →  findings[]        │  deterministic, no network
├───────────────────────────────────────────────┤
│ 2. EVIDENCE   only if a finding asked for it  │  Gemini: grounded RRP,
│               →  Evidence                     │  vision audit, copy audit
├───────────────────────────────────────────────┤
│ 3. RESOLVE    findings + evidence → patches[] │  provenance arbitration
├───────────────────────────────────────────────┤
│ 4. RE-VERIFY  apply to a shadow copy,         │  ◄── the safety net
│               re-run the hard invariants      │
│               still broken? → escalate, write │
│               nothing                          │
├───────────────────────────────────────────────┤
│ 5. GATE       locked fields, delta caps,      │
│               confidence floors               │
├───────────────────────────────────────────────┤
│ 6. WRITE      PATCH /product/{id} + audit log │
└───────────────────────────────────────────────┘
```

Step 4 is what makes this different from "ask an LLM to double-check it". Every
patch set is applied to a shadow copy and pushed back through
`verify_invariants()`. If the repaired record would *still* violate a hard
pricing rule, the patches are downgraded to `escalate` and nothing is written.
A record can never leave Hermes marked *repaired* while still broken.

---

## How the price bug gets fixed

Three mechanisms, in order.

**1. A window around the tenant's own grade factor, not a single threshold.**
`price < retail` is too weak — a Grade C item at 95% of retail is technically
valid and commercially wrong. So the expected price is computed and a window is
allowed around it:

```
expected     = retail_price * price_factor
min_allowed  = expected * (1 - tolerance)
max_allowed  = expected * (1 + tolerance)
correct      = min_allowed <= price <= max_allowed
```

`price_factor` is **the tenant's own `Grade.priceFactor`**, delivered with every
product by the VNYX feed — not a number in `policy.yaml`. That matters because
grade codes are per-tenant: one shop's Grade D is "Good", another's is "Recycle",
so a hardcoded band mis-prices every custom scale. `grade_targets` in the policy
file is only the fallback for a grade with no factor configured, and using it
raises `PRICE.101` so the fallback is never silent.

A price inside the window is left completely alone. Outside, it is clamped to the
**nearest** edge, not pulled to the target — the smallest change that makes the
record valid.

Plus `hard_max_ratio: 0.95` as an absolute ceiling that nothing may cross,
checked independently of the factor. A misconfigured factor at or above that
ceiling is itself reported, as `PRICE.102`.

**Why the window has a tolerance when the arithmetic is exact:**
`price = retail_price * priceFactor` is only guaranteed on ONE VNYX code path — a
manual regrade (`updateProduct` requires a prior grade and no explicit price in
the same request). A product priced at analyze time takes its price from an eBay
used-listing search instead, which has no arithmetic relation to the factor.
Exact equality would flag most of the catalog. `exact_factor_match` on every
assessment records which of the two a given row is.

**2. Provenance arbitration.** When price and retail disagree, Hermes moves the
one it trusts less:

```
human (locked)  100   ← never overwritten, always escalated
grounded         80   ← search-grounded RRP with citable sources
market           60   ← "Powered by Google" comparable
derived          40   ← "Derived from grade multiplier"
ai               20
unknown           0
```

In your current setup `price` is MARKET (60) and `retail_price` is DERIVED (40),
so Hermes back-solves the *retail anchor* rather than clobbering a real market
price — and only ever *proposes* that, because raising an RRP without evidence
is a guess.

**3. Grounded RRP lookup.** When the anchor is weak, Hermes asks Gemini with
Google Search grounding for the item's *original* retail price — never a resale
price, since resale price is ours to compute. A grounded answer (80) outranks a
derived one (40), replaces the anchor, and the ratio usually corrects itself.

---

## What it found in your Levi's record

Running the record from your screenshots (`python demo.py`), no LLM, 19ms:

```
STATUS: blocked   publishable=False   llm_calls=0

[critical] ID.001     Bin location 'ZONE A-02-12-3' does not match the printed
                      bin barcode 'A-04-10-4'. Pickers will be sent to the
                      wrong shelf.
[high    ] PRICE.002  38.99 is 58% of retail. Grade C expects 40% (26.80) with
                      a +/-20% tolerance, so the window is 21.44-32.16.
[high    ] TAX.005    Mannequin 'Women Top' is wrong for Men > Bottoms.
[high    ] SIZE.002   EU size 42 does not convert from W32 (expected ~48).
[medium  ] PRICE.101  Grade C has no priceFactor configured, so this price was
                      judged against the policy default (0.4) rather than the
                      tenant's own ladder.
[low     ] CONF.001   Field 'model' has low extraction confidence (0% < 70%).
[low     ] CONF.001   Field 'supplier' has low extraction confidence (0% < 70%).

[apply   ] price     : 38.99 -> 32.16   (pricing.anchor is retail)
[apply   ] mannequin : 'Women Top' -> 'Men Bottom'
[apply   ] eu_size   : '42' -> '48'
```

Two things to read off this:

`PRICE.101` fires because the sample fixture carries no `priceFactor` — it is a
hand-written payload, not the real feed. Against `GET
/review-verification/feed` the tenant's own factor arrives with the record and
`rule` reads `backend_factor` instead. **Set a `priceFactor` on each grade and
this finding disappears**, along with the guessed 0.40 window.

The bin mismatch is marked `critical` and blocks publication, but Hermes will
**not** auto-fix it — a physical shelf location is ground truth that lives in
the warehouse, not in the database. `escalate_only_fields` in the policy covers
all identifiers for the same reason. (Note this fires on the *fixture*, which
supplies an explicit `binBarcode`; see `ID.001` in the rules table for why it is
dormant against real VNYX data, and `ID.004` for the check that replaces it.)

---

## Rules

| ID | Severity | Checks |
|---|---|---|
| `PRICE.001` | critical | price ≥ 95% of retail (independent of the factor) |
| `PRICE.002` | high | price above the window the grade factor implies |
| `PRICE.003` | high | price below that window — we are underselling |
| `PRICE.010/011` | critical/medium | price missing, non-positive, or below floor |
| `PRICE.020` | high | retail anchor missing |
| `PRICE.030` | medium | currency unset |
| `PRICE.101` | medium | grade has no `priceFactor`; judged against the policy fallback |
| `PRICE.102` | high | the configured `priceFactor` is itself at/above the ceiling |
| `PRICE.110` | high | `Product.price` ≠ the `ProductVariant.basePrice` the channel publishes |
| `TAX.001–004` | high | master/category/subcategory triple (against the **tenant's** tree), gender agreement |
| `TAX.005` | high | mannequin rig vs gender+category |
| `TAX.006` | low | sizing guide is one the tenant configured |
| `SIZE.001` | high | waist / size / international size disagree |
| `SIZE.002` | high | EU size vs the product's own sizing chart |
| `ATTR.001–003` | medium | brand / colour / material is not in the tenant's option list, so the edit screen cannot offer it |
| `SIZE.003` | medium | inseam plausibility |
| `GRADE.001` | high | condition ↔ grade mapping |
| `GRADE.002/003` | high/medium | Grade A with defects, Grade C/D without |
| `ID.001` | critical | bin code vs a duplicate location code — **dormant against VNYX**, which has no second copy (the barcode encodes `binNumber`, a scan id, not the location string) |
| `ID.002/003` | medium/high | LPN = SKU, placeholder identifiers (`sku`/`product_code` required; bin/LPN absent is normal pre-putaway) |
| `ID.004` | high | bin code does not sit inside the zone the bin belongs to |
| `TEXT.001–004` | varies | title/description vs structured fields |
| `DATA.001/002` | medium/high | placeholder values, negative inventory |
| `CONF.001` | low | field confidence below threshold |
| `LLM.001` | varies | vision or copy audit contradicts the record |

Adding a rule is one function returning `list[Finding]`, registered in
`app/rules/__init__.py`.

### What decides `correct`

`correct: false` means a finding fired **at or above the severity floor**, which
defaults to `medium`. It is not "any finding at all": `CONF.001` fires on every
low-confidence extracted field and `TEXT.002` on any title that does not restate
the colour. Both are worth reading; neither should hand back an edit URL. They
stay in `findings`, are counted in `advisory_count`, and leave `correct` true.
Pass `minSeverity=low` to have everything count.

### The tenant's config always wins

Four things in `policy.yaml` are **fallbacks**, used only when the feed supplies
no catalog — and they are not close approximations:

| Policy guess | One real tenant's actual config |
|---|---|
| `Men: Tops / Outerwear / Footwear / Bottoms / Accessories` | `Accessories / Backpacks & Bags / Bottoms / Jackets / Shirts / Shoes / Sweaters & Hoodies / T-Shirts & Polos / Vests` — no Tops, Outerwear or Footwear at all |
| `women_bottoms_waist_in_to_eu: 28 → 40` | Women Bottoms chart: `W28 → EU 36` |
| sizing guide named `"{master} {category}"` | `Defaults`, `Kids`, `Men Bottoms`, `Men DressShirts`, `Men Uppers`, `Women Bottoms`, `Women Uppers` |
| `grade_targets: C → 0.40` | `Grade.priceFactor` per grade, per tenant |

Validating a men's t-shirt (`Men > T-Shirts & Polos`) or a correct women's W28
against the left column flags it every time. Every finding carries a `basis`
field recording which side judged it.

---

## Run it

```bash
cp .env.example .env          # add GEMINI_API_KEY and VNYX_API_TOKEN
docker compose up --build
curl localhost:8080/healthz
```

Leave `HERMES_DRY_RUN=true` for the first few days. Hermes computes everything
and logs it to `audit.jsonl` but writes nothing. Read the log, tune the bands in
`config/policy.yaml`, then flip it.

Local, without Docker:

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
pytest -q          # 15 tests, uses your real Levi's record as the fixture
python demo.py     # runs that record through the pipeline and prints the report
```

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/healthz` | liveness + active model + dry-run state |
| `POST` | `/v1/review-queue` | **judge a page of the review feed. Report-only — never writes** |
| `POST` | `/v1/validate` | dry run — findings and proposed patches, writes nothing |
| `POST` | `/v1/reconcile` | detect, repair, re-verify, write |
| `POST` | `/v1/batch` | background sweep over a list of product ids |
| `POST` | `/v1/webhooks/product-enriched` | HMAC-verified hook from your pipeline |
| `POST` | `/v1/readability` | **can the text in this photograph be read? multipart upload, ~1.5s budget** |
| `POST` | `/v1/policy/reload` | re-read `policy.yaml` without a restart |

`/v1/review-queue` is the one vnyx-api calls. It takes the whole page in one
request, returns a verdict per product, and is report-only *by which code runs* —
it never touches the resolver, so there is no patch to escape even if
`HERMES_DRY_RUN` were misconfigured. The answer to a failed check is the
`edit_url` that arrived with the record.

```bash
curl -X POST localhost:8080/v1/review-queue \
  -H 'content-type: application/json' \
  -d @- <<'JSON'
{"products": [{"id": "1a351701-...", "tenantId": "34c354a5-...",
               "editUrl": "https://dev.vnyx.ai/product/1a351701-.../edit?tenantId=34c354a5-...&fromTab=pending",
               "priceAmount": 58.99, "retailPriceAmount": 66.99,
               "grade": "C", "gradeLabel": "Lived In",
               "priceExpectation": {"priceFactor": 0.58, "expectedPrice": 38.85}}]}
JSON
```

```json
{ "checked": 1, "incorrect": 1,
  "verdicts": [{ "product_id": "1a351701-...", "correct": false,
                 "status": "needs_review", "worst_severity": "high",
                 "findings": [{"rule_id": "PRICE.002", "severity": "high", "...": "..."}],
                 "price": {"verdict": "too_high", "rule": "backend_factor",
                           "expected_price": 38.85, "max_allowed": 46.63},
                 "edit_url": "https://dev.vnyx.ai/product/1a351701-.../edit?tenantId=34c354a5-...&fromTab=pending" }] }
```

```bash
curl -X POST localhost:8080/v1/validate \
  -H 'content-type: application/json' \
  -d '{"product_id":"1a351701-7568-4414-8975-ce07d664ab95",
       "tenant_id":"34c354a5-3415-4513-85b8-d40c2ec3af7e"}'
```

### `/v1/readability` — the odd one out

Every other endpoint judges a product *record*. This one judges a single
uploaded photograph and knows nothing about a product. What it shares is the
thing that defines Hermes: a deterministic verdict with a stated reason, no
model call, no write-back. It is a verifier of pixels instead of columns.

It is also the only endpoint with a hard latency budget — it fires at shutter
press on a warehouse phone, so ~1.5s covers the upload, the answer and the
operator's retake decision.

```bash
curl -X POST localhost:8080/v1/readability -F "image=@care-label.jpg"
```

```json
{ "readable": false,
  "message": "Only part of the label is readable. Move closer so the whole label fills the frame. Also — hold the camera still and let it focus.",
  "reasons": ["not_enough_text", "out_of_focus"],
  "confidence": 0.9612, "line_count": 2, "detected_count": 3,
  "text": "COTTON 100%\nSIZE M / EU 40",
  "decode_ms": 34, "ocr_ms": 680, "duration_ms": 739 }
```

`readable` and `message` are the whole contract for a simple client.

`min_lines` / `min_chars` are floors on how much text must come back legible, and
they default low (1 line, 3 characters) **because a floor above what the label
actually carries rejects a good photograph** — an adidas neck label reads
"adidas" and "S" and nothing else, so `min_lines=3` fails a perfect capture of
one. Raise them only per capture type you know: a full care label is where 4–6 is
right. That floor is still what catches text too small to read, which confidence
alone gets wrong — see the docs.

PP-OCRv6 through RapidOCR's ONNX build, on CPU. **`docs/READABILITY.md` carries
the measured thresholds, the latency table, the `onnxruntime` pin that Windows
needs, and the two findings that shaped the design** — that no pixel statistic
can be used as a gate, and that confidence alone cannot decide.

## Models

Set in `config/policy.yaml` under `llm`:

- `model_reasoning: gemini-3.7-flash` — grounded RRP lookup (needs search + reasoning)
- `model_fast: gemini-3.6-flash` — vision audit and copy entailment (high volume)

Both calls use structured outputs with a JSON Schema, so responses are validated
before they touch the resolver. `gemini-3.5-flash-lite` is a cheaper option for
the vision pass if you're running high volume.

---

## Wiring it in

### Wired in now: the review-queue sweep

`vnyx-api` fronts it, so callers keep one base URL and the existing JWT auth:

```
you / the review screen
   │  GET /review-verification/queue?tenantId=…&status=pending&onlyIncorrect=true
   ▼
vnyx-api :4000                      JWT auth + tenant scoping (unchanged)
   │  flattened feed             ── services/review-verification.ts
   │    + grade ladder             (Grade.priceFactor per grade)
   │    + tenant catalog           (categories, sizing charts, colours,
   │                                materials, brands — the same five lists
   │                                the edit screen fills its dropdowns from)
   │  POST /v1/review-queue      ── services/hermes-client.ts
   ▼
hermes :8080                        deterministic rules, ~10ms/record
   │
   ▼  verdict + edit_url per product, merged into the list response
```

Note the API host: `api-dev.vnyx.ai`, with **no** path prefix — `/products`,
`/categories`, `/sizes`. `dev.vnyx.ai` is the Next.js frontend and serves none of
these.

Hermes never calls back. Everything it needs is in the request body, so it holds
no VNYX credentials, implements no tenant scoping, and cannot reach a product the
caller was not already authorised to read.

If Hermes is down the queue still returns its rows, every verdict is
`correct: null`, and `verification.available` is false — so "everything is
correct" is never confused with "nothing was checked".

Set on the vnyx-api side: `HERMES_BASE_URL`, `HERMES_TIMEOUT_MS`,
`PUBLIC_APP_BASE_URL`.

**To run both locally and test it:** see [TESTING.md](TESTING.md) and import
`postman/Review-Verification.postman_collection.json`. Folder 3 of that
collection needs neither a database nor a login, so it is the quickest way to
exercise the rule engine.

### Other integration points

1. **Webhook** — have your enrichment job POST the finished product to
   `/v1/webhooks/product-enriched`. Lowest latency, catches everything at source.
2. **Stage gate** — call `/v1/validate` on the Photobooth → Review transition and
   block promotion when `publishable=false`. This is where your Stage Timeline
   already has a natural hook.
3. **Nightly sweep** — page `GET /review-verification/queue` over everything in
   Review, to catch records that predate Hermes.

The only file that knows about your API shape is `app/vnyx_client.py` — adjust
`to_snapshot()` and `_FIELD_MAP` there and nothing else changes. It accepts both
the flattened verification feed and a raw `GET /products/{id}` payload, because a
webhook delivers the latter.

**Where the flattening lives, and why.** `Product` spreads one garment's truth
across four shapes: real columns, a free-form `properties` JSON map (keyed
per-tenant, with both `eu_size` and `euSize` live in production), relation rows
that can disagree with the `properties` copy, and the `ProductVariant`/`Price`
mirrors the marketplace sync actually publishes. Resolving all four happens in
`services/review-verification.ts`, next to the schema it depends on — so a
renamed property key is a one-line TypeScript fix, not a silent `None` that a
rule engine scores as a violation.
