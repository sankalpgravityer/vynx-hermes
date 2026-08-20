# Verifying review data locally — step by step

Your setup: **backend API + Hermes on your machine, no frontend, pointed at the
remote dev database.** Everything below assumes exactly that.

Two processes, two ports, no conflict:

| Service | Port | Directory |
|---|---|---|
| `vnyx-api` (TypeScript) | **8000** | `E:\vynx\vnyx-api` |
| `hermes` (Python) | **8080** | this directory |

8000 is not a guess — it is `PORT=8000` in the vnyx-api `.env`.

No frontend is needed. The edit URL is *returned data*, not something the test
flow opens; you paste it into a browser at the end if you want to fix a record by
hand.

---

## Step 1 · Backend `.env` — add two lines

Open `E:\vynx\vnyx-api\.env` and append:

```dotenv
# Where Hermes listens. 127.0.0.1, NOT localhost.
HERMES_BASE_URL=http://127.0.0.1:8080

# Origin for the edit deep-link returned with each failed verdict.
PUBLIC_APP_BASE_URL=https://dev.vnyx.ai
```

Optional, defaults to 20000:

```dotenv
HERMES_TIMEOUT_MS=20000
```

**Why `127.0.0.1` and not `localhost`.** `uvicorn --port 8080` binds IPv4 only,
while Node's `fetch` on Windows frequently resolves `localhost` to `::1` first and
reports `ECONNREFUSED` on a service that is plainly running. This is the single
most likely cause of a 503 in Step 6.

**Why `https://dev.vnyx.ai` and not localhost.** Your `DATABASE_URL` points at the
remote dev database, so the products you are verifying are the same ones
`dev.vnyx.ai` serves — a link there opens the real record. The default would be
`FRONTEND_URL` (`http://localhost:3000`), which you are not running, so those
links would be dead. Pick the frontend that can see the data, not the one nearest
the API.

Change nothing else. This feature adds no table, no migration, and no new
dependency — it only reads columns that already exist.

## Step 2 · Hermes `.env` — change two lines

Open `.env` in this directory:

```dotenv
# Rules only. No Gemini key, no cost, ~10ms per record. Turn it on later, once
# the deterministic layer looks right to you.
HERMES_LLM_ENABLED=false

# Your local backend. The value currently in the file is https://dev.vnyx.ai/api,
# which is wrong twice over: that host is the frontend, and the API has no /api
# prefix. (The deployed API is https://api-dev.vnyx.ai.)
VNYX_BASE_URL=http://127.0.0.1:8000
```

Leave `HERMES_DRY_RUN=true`.

**Neither `VNYX_BASE_URL` nor `VNYX_API_TOKEN` is used by the endpoint this
feature runs on.** The backend sends the products, the grade ladder and the tenant
catalog in the request body of `POST /v1/review-queue`, so Hermes holds no
credentials and never calls back. Those two only matter for `/v1/reconcile` and
`/v1/batch`. Set them anyway so nothing surprises you later.

## Step 3 · Install and start both

```bash
# terminal 1 — the API
cd E:\vynx\vnyx-api
yarn install                    # first run only
yarn dev                        # -> http://localhost:8000

# terminal 2 — Hermes
cd "E:\hermes local\files (1)\hermes\hermes"
pip install -r requirements.txt # first run only
uvicorn app.main:app --reload --port 8080
```

**Redis must be running.** `REDIS_URL` is `127.0.0.1:6379` and `src/index.ts`
imports every BullMQ worker for its side effects, so the API process wants a local
Redis. If you see repeated `ECONNREFUSED 127.0.0.1:6379` in terminal 1, start
Redis (Docker: `docker run -d -p 6379:6379 redis:7-alpine`). The verification
endpoints themselves never touch Redis, so a noisy log is survivable — but a clean
start is easier to read.

Hermes in Docker instead works unchanged: `docker compose up --build` already maps
`8080:8080`.

## Step 4 · Smoke-check each service alone

```bash
curl http://127.0.0.1:8080/healthz
curl http://localhost:8000/review-verification/health
```

The first should list **8 rule groups** including `catalog`. If it lists 7, you are
running an older Hermes than the one these docs describe. `llm_enabled` should be
`false` after Step 2 — if it still says `true`, Hermes has not reloaded the `.env`
(restart it; `--reload` watches code, not env).

The second should return **401 `Missing bearer token`**, not 404. That is the
check that matters: 401 proves the route is mounted and your running backend has
this code. A 404 means the build did not pick it up — restart `yarn dev`.

**Error responses use the shared envelope:**

```json
{ "success": false, "message": "Missing bearer token",
  "error": { "code": "UNAUTHORIZED" } }
```

Success responses are plain (`{ items, total, … }`). The mismatch is deliberate
and documented in the router header — worth knowing so you read the right field
when a request fails.

## Step 5 · Import the collection

Import `postman/Review-Verification.postman_collection.json`.

Then set two collection variables (Collection → Variables):

| Variable | Value |
|---|---|
| `email` | your dev login |
| `password` | your dev password |

Everything else is pre-filled: `apiUrl` `http://localhost:8000`, `hermesUrl`
`http://localhost:8080`, `appUrl` `https://dev.vnyx.ai`.

**Open the Postman console: Ctrl+Alt+C.** The tests print a readable per-product
summary — findings, expected price, fix link — which is much easier to scan than
the raw JSON.

## Step 6 · Run folder 0 in order

| Request | Proves |
|---|---|
| `0.1 Hermes is up` | Hermes answers, 8 rule groups loaded |
| `0.2 Login` | your credentials work; **stores `accessToken` and `tenantId`** for every later request |
| `0.3 Backend can reach Hermes` | the *link* between the two — the backend calling Hermes over `HERMES_BASE_URL` |

`0.2` also prints every tenant you are a member of:

```
Tenants you can pass as tenantId:
  34c354a5-3415-…  Some Tenant (ADMIN)
```

If your default tenant has nothing in review, copy another id from that list into
the `tenantId` variable.

**`0.3` is the one that catches a bad env.** If `0.1` passes and `0.3` returns
503, `HERMES_BASE_URL` is wrong — almost always `localhost` where it should be
`127.0.0.1`.

## Step 7 · Look at the raw data first

Run **`2.1 Feed for the pending tab`** before any verdicts. It shows exactly what
the backend hands Hermes, with no rules applied, and prints per product:

```
BEV-000325 Regular Levi's Jeans in Blue size 32
  price=38.99 (38.99) retail=66.99
  grade=C/Lived In factor=0.58 expected=38.85 skipped=null
  variant mirror: 38.99 drift=0 inSync=true
  taxonomy: Men > Bottoms > Jeans guide=Men Bottoms
  attrs: brand=Levi Strauss & Co. color=Blue material=Denim size=32 eu=48 gender=men
```

Check three things in that output:

1. **`factor=` is not null.** Null means the grade has no `priceFactor` configured,
   so pricing falls back to a generic band and you will see `PRICE.101`.
2. **`catalog[…]` counts are non-zero** (categories / sizing guides / colours /
   materials / brands). Zero means the tenant has no options configured, and the
   taxonomy and attribute rules will stay silent.
3. **`attrs:` are populated, not null.** A null here is a mapping gap, not a data
   problem — tell me which field and I will fix the alias.

This is also the request to come back to whenever a verdict looks wrong: it
distinguishes "the rule misfired" from "the field arrived empty".

## Step 8 · Run the verification

**`1.1 Pending tab, all rows + verdicts`** — the endpoint this whole feature is.

```
GET http://localhost:8000/review-verification/queue
      ?tenantId={{tenantId}}&status=pending&take=20
```

Read the response like this:

- `verification.available` — did Hermes answer at all.
- `checked` / `incorrect` — how many were judged, how many failed.
- Per row, `verification.correct`:
  - `true` — clean
  - `false` — broken; `editUrl` is where a human fixes it
  - **`null` — NOT CHECKED.** Hermes did not answer. This is not a pass.

Then **`1.2 Only the incorrect ones`** for the actual work list. Rows that passed
are withheld; rows that could not be *checked* are kept, because dropping them
would present a Hermes outage as a clean queue.

## Step 9 · Verify the price case you originally reported

Put the product id in the `productId` variable and run **`1.5 Re-check one
product`**. For a price disagreement you will see:

```
x BEV-000325  Regular Levi's Jeans in Blue size 32
   price=58.99 retail=66.99 grade=C
   expected=38.85 (factor 0.58) delta=20.14
   [high] PRICE.002: 58.99 is 88% of retail. Grade C expects 58% (38.85) with a
          +/-20% tolerance, so the window is 31.08-46.63.
   [high] PRICE.110: Catalog price 58.99 does not match the published variant
          price 38.99 (drift +20.00).
   FIX HERE: https://dev.vnyx.ai/product/1a351701-…/edit?tenantId=34c354a5-…&fromTab=pending
```

Two distinct facts there, worth separating:

- **`PRICE.002`** — the price disagrees with *your tenant's own* grade ladder
  (`Grade.priceFactor`), not with an opinion in a config file.
- **`PRICE.110`** — `Product.price` and the `ProductVariant.basePrice` that the
  marketplace sync actually publishes have drifted apart. **No existing endpoint
  shows you this**, because none returns both. If the price on the review page
  looked wrong to you, this is a strong candidate for why.

Paste the `FIX HERE` link into a browser to open the real record on dev.

## Step 10 · Confirm the grade percentages come from your settings

The price check uses **your tenant's Regrade Factor**, read from the database on
every request. Nothing about grades is hardcoded in Hermes.

```
Product settings > Pricing Multiplier          Grade.priceFactor
        (Regrade Factor, e.g. 50%)                    │  stored as 0.50
                                                      ▼
buildGradeIndex()  one query per request  ──▶  priceExpectation.priceFactor
                                                      ▼
                        expectedPrice = retailPrice × priceFactor
                                                      ▼
               window_for() → rule: "backend_factor", ±factor_tolerance
```

Change a factor in that settings page and the next API call uses it — no
redeploy, no cache, no `policy.yaml` edit. `grade_targets` in `policy.yaml` is a
**fallback only**, used when a grade has no factor configured, and it announces
itself: the assessment reads `rule: "policy_band"` and `PRICE.101` fires.

Run **`2.3 Grade ladder`** to see exactly what the checker reads.

**Units.** The API accepts a whole percent on write (`50`) and divides by 100, so
the column holds `0.50`. On read it is asymmetric: `priceMultiplier` is scaled
back to `200`, `priceFactor` is not — it returns `0.5`. The feed reads the column
directly and always sees the fraction, so verification is unaffected. Do not
GET-then-PUT that payload unchanged, though, or `0.5` is stored as `0.005`.

### When the price check is tautological

If a grade's **Multiplier is the reciprocal of its Regrade Factor** and the
multiplier toggle is on, the check cannot fail. Worked through with a real BOAS
configuration:

| Grade | Regrade Factor | Multiplier | factor × mult |
|---|---|---|---|
| A As New | 50% | 200% | 1.0000 |
| B Good | 40% | 250% | 1.0000 |
| C Lived In | 25% | 400% | 1.0000 |
| D Recycle | 16.66% | 600% | 0.9996 |

With the toggle on, `retailPrice = price × multiplier`, so

```
expectedPrice = retailPrice × factor = price × multiplier × factor = price
```

— expected equals actual by construction. A 100.00 product verifies `ok` on every
one of those grades, and `exact_factor_match` is `true` (D lands at 99.96, inside
tolerance but not to the cent).

That is not a bug in either the config or the checker; it means **the price check
only says something for products whose retailPrice did NOT come from the
multiplier.** Those are the ones with

```
retailProvenance: "market"     (Product.retailPriceBreakdown.source = EBAY_NEW)
```

For a product priced 58.99 against an eBay retail of 66.99, the same four grades
give expected 33.49 / 26.80 / 16.75 / 11.16 — and all four report `too_high`,
because an eBay *used* price is not the multiplier's output. Filter on
`retailProvenance` when reading results: `derived` is self-consistent by
construction, `market` is where a real disagreement can show up.

## Step 11 · Tune what counts as incorrect

`correct: false` means a finding fired **at or above the severity floor**, default
`medium`. Cosmetic rules (`CONF.001` on low-confidence fields, `TEXT.002` on a
title that does not restate the colour) are reported but do not disqualify — they
appear in `findings` and are counted in `advisory_count`.

- **`1.3 minSeverity=low`** — everything counts. Expect many more incorrect rows,
  most of them cosmetic. Run it once to see what you are choosing to ignore.
- **`1.4 minSeverity=high`** — real defects only: pricing, sizing, taxonomy.

Pick the floor you want, then use that setting consistently.

---

## Folder 3 · Testing rules without the database

Folder 3 posts hand-built payloads straight to Hermes. **No database, no login,
no backend** — useful for exercising a rule, and the only part that still runs if
the dev DB is unreachable. Each request carries a real tenant catalog (2 category
trees, 7 sizing charts, 67 colours, 57 materials, 63 brands) in the `catalogJson`
variable.

Verified outcomes:

| Request | Result |
|---|---|
| 3.1 A correct product | `correct: true`, zero findings from all 8 groups |
| 3.2 Price differs from the grade factor | `PRICE.002` + `PRICE.110` |
| 3.3 Price at retail | `PRICE.001` critical, `publishable: false` |
| 3.4 Tenant catalog vs hardcoded tables | both products `correct: true` |
| 3.5 Colour the dropdown cannot offer | `ATTR.002`, with near matches |
| 3.6 Wrong EU size | `SIZE.002`, `basis: "tenant_chart"` |
| 3.7 Advisories | `correct: true`, `advisory_count: 3` |
| 3.8 Pricing rules only | `rule_groups: ["pricing"]` |

**3.4 is the one to read.** Both products are correct, and both would be rejected
by the built-in tables. Delete the `catalog` key from its body and re-send:

```
mens tshirt  correct=false  TAX.002   basis=policy
womens W28   correct=false  SIZE.002  basis=policy_table
```

That is the difference between validating against your tenant's configuration and
against a guess at it. Every finding carries `detail.basis` so you can always tell
which side judged it.

**`4.1 Reload policy.yaml`** re-reads the config with no restart — edit
`factor_tolerance` and re-run 3.2 to watch the window move.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Postman shows an EMPTY URL bar | An older export of the collection. Re-import the current file — every request now carries the `host`/`path` form Postman's importer needs, not just `raw` |
| `0.1` fails | Hermes not running, or on another port |
| `0.1` lists 7 rule groups, not 8 | Running an older Hermes; `catalog` group missing |
| `0.1` shows `llm_enabled: true` | Hermes has not reloaded `.env` — restart it (`--reload` watches code, not env) |
| Any `/review-verification/*` returns 404 | The running backend predates this code — restart `yarn dev` |
| `0.1` passes but `0.3` returns 503 | `HERMES_BASE_URL` wrong — check `127.0.0.1` vs `localhost` |
| Folder 1 returns 401 | Run `0.2 Login` first |
| Folder 1 returns 403 | `tenantId` is one you have no ACTIVE membership in |
| Folder 1 returns an empty `items` | Nothing in that tab for that tenant — try another `tenantId` from `0.2`, or `status=all` |
| `verification.available: false` | Backend is up but could not reach Hermes; rows still return, all `correct: null` |
| Everything comes back incorrect | Check `2.1` prints non-zero catalog counts — without the catalog, taxonomy and sizing fall back to generic tables |
| `PRICE.101` on every product | No `priceFactor` configured on the tenant's grades |
| `ECONNREFUSED 127.0.0.1:6379` in terminal 1 | Local Redis is not running. Harmless for these endpoints, noisy in the log |
| Edit links point somewhere useless | `PUBLIC_APP_BASE_URL` — it must name a frontend that can see the database this API is pointed at |
