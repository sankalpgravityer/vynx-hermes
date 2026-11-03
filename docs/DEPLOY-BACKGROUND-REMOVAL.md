# Deploying the Hermes cut-out path to Ubuntu

Background removal now runs **inside Hermes**, on CPU, with no call to
`vnyxremoveapi.vnyx.ai`. This is what to install and set so an arriving product
gets a real cut-out.

---

## Why this replaced the vnyx segmenter

`vnyxremoveapi.vnyx.ai` sits behind a Cloudflare proxy whose origin timeout is
100 seconds and cannot be raised below Enterprise. Segmentation routinely
exceeds it, and the call comes back as a **524 error PAGE** — HTML with a
200-shaped body that nothing downstream inspected. Two images on BOA-006166 took
240 seconds and produced nothing while the step reported success.

The replacement is `u2net_cloth_seg`, a garment-parsing model run locally. It
was chosen by measurement, not preference — on the same two studio originals:

| approach | result |
|---|---|
| Gemini (magenta, grey, or mask) | retouches the photograph; leaves the scene |
| OpenAI image edit | halo around the sleeves, keeps the podium |
| rembg `isnet` / `u2net` | keeps the mannequin **and** the podium (14% backdrop) |
| rembg **`u2net_cloth_seg`** | **0% backdrop, garment intact, full resolution** |

`isnet` and `u2net` are *salient object* detectors, and in a studio shot the
salient object is the whole mannequin-on-podium assembly. `u2net_cloth_seg` is a
clothing parser, which is why the mannequin, stand, podium and floor disappear.

Gemini and OpenAI remain as fallbacks behind it.

---

## 1. System packages

```bash
sudo apt-get update
sudo apt-get install -y libgl1 libglib2.0-0
```

**`libgl1` is not optional.** `opencv-python` (as opposed to
`opencv-python-headless`) links against `libGL.so.1`, which a headless server
does not have. Without it `import cv2` raises, and `_kept_backdrop` silently
falls back to its blunt colour-only test — which passes cut-outs that still
contain a podium. The check degrades quietly, so this is worth verifying
rather than assuming (step 5).

---

## 2. Python packages

```bash
cd /opt/hermes            # wherever the checkout lives
source .venv/bin/activate
pip install rembg
```

`rembg` pulls `scipy`, `numba`, `llvmlite`, `scikit-image`, `pymatting` and
`pooch` — about 85 MB of wheels, ~300 MB installed.

It does **not** pull its own `onnxruntime`; it uses whatever is already
installed, so the `onnxruntime==1.20.1` pin in `requirements.txt` is unaffected.
That pin exists for Windows 10 and the comment beside it already notes Linux is
unaffected by the constraint.

Add it to `requirements.txt` so a rebuilt venv keeps it:

```
rembg==2.0.84
```

---

## 3. Where the model lives — `U2NET_HOME`

The model is a **168 MB download on first use**. Put it on a disk with room, and
somewhere the service user can write:

```
U2NET_HOME=/var/lib/hermes/models
```

Your Windows `E:\hermes-models` becomes `/var/lib/hermes/models` on the server —
`/var/lib/<service>` is the conventional place for data a service manages
itself, as opposed to `/opt` (application files) or `/tmp` (cleared on boot).

```bash
sudo mkdir -p /var/lib/hermes/models
sudo chown hermes:hermes /var/lib/hermes/models     # the user the unit runs as
```

**Do not leave it unset.** rembg defaults to `$HOME/.rembg`, and a systemd unit
with `DynamicUser=` or an empty `HOME` will re-download 168 MB on every restart,
or fail outright if the home is not writable.

---

## 4. Pre-fetch the model at deploy

Otherwise the first product of the day pays a 168 MB download plus a ~25 second
model load, inside a step that has a timeout.

```bash
sudo -u hermes U2NET_HOME=/var/lib/hermes/models \
  /opt/hermes/.venv/bin/python -c \
  "from rembg import new_session; new_session('u2net_cloth_seg'); print('model ready')"
```

Expect ~168 MB in `/var/lib/hermes/models/u2net_cloth_seg/`.

---

## 5. Verify before trusting it

```bash
sudo -u hermes U2NET_HOME=/var/lib/hermes/models \
  /opt/hermes/.venv/bin/python - <<'EOF'
import cv2                      # fails here if libgl1 is missing
from rembg import new_session
print("cv2", cv2.__version__)
s = new_session("u2net_cloth_seg")
print("cloth-seg session ok")
EOF
```

Both lines must print. If `import cv2` raises `libGL.so.1: cannot open shared
object file`, go back to step 1 — the pipeline will still *run* without it and
produce worse cut-outs without saying so.

---

## 6. systemd

Add to **both** `hermes-worker.service` and `hermes.service` (the API serves
`/v1/imagery/remove-background`, the worker drives the chain):

```ini
[Service]
Environment="U2NET_HOME=/var/lib/hermes/models"
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart hermes hermes-worker
```

`app/imaging/cutout.py` caches the session in module state, so it is loaded once
per process and reused — which is why the unit must not use `--reload`.

---

## 7. Routing — the agent does it, not the environment

**No change to vnyx-api's `.env` is required.** The agent asks for the Hermes
segmenter per call:

```
repair_product._matte()  ->  --provider hermes
      -> POST /internal/auto-approval/step  { step: "matte",
                                              options: { bgProvider: "hermes" } }
      -> npx tsx scripts/backfill-bg-removal.ts … --provider hermes
```

`--provider` short-circuits `resolveBgRemovalProvider` for that invocation only.

**Why not `BG_REMOVAL_RETIRED`.** That env var works, but it re-points EVERY
caller — the web upload, the analyze worker, the manual "Sync now" — for all
twenty tenants, as a side effect of deploying a file. The Auto Approval chain is
the only caller that needs the Hermes cut-out. Forcing it per call leaves every
other path exactly as the tenant configured it.

It is also the only thing that can win. `resolveBgRemovalProvider` reads the
**product's own** `imageSettings.bgRemovalProvider` snapshot before the tenant
column, and 14,696 products carry a baked-in value — so setting the tenant
column does nothing for them. A per-call override beats a snapshot; nothing else
does.

`bgProvider` crosses the internal route as a **closed enum**
(`hermes | removebg | vnyx-gemini | vnyxv2 | vnyx`), keeping the rule that
nothing from a request becomes a free-form argv value.

### The one variable Hermes needs

Only if you want something other than the default:

```
AUTO_APPROVAL_BG_PROVIDER=hermes     # the default; set empty to restore
                                     # the old product/tenant resolution
```

`HERMES_BASE_URL` must already point at Hermes on the vnyx-api side — that is
existing configuration for the `hermes` provider branch, not new.

Confirm the route is current:

```bash
curl -s -H "x-internal-secret: $AUTO_APPROVAL_INTERNAL_SECRET" \
  http://127.0.0.1:8000/internal/auto-approval/ping | jq '.steps, .missingScripts'
```

---

## 8. What you should see in the logs

vnyx-api, once per resolution:

```
[inspection] bg-removal provider 'vnyx-gemini' is retired … using 'hermes'
```

Hermes, per image:

```
cloth-seg model u2net_cloth_seg loaded in 0.9s
bg-removal cloth-seg#1 produced a cut-out in 17.7s (3278 KB, transparent (100% of the border is fully transparent))
```

**~20 seconds per 3000×4000 image**: roughly 10s inference, the rest unioning
the model's three output panels, PNG-encoding 12 megapixels of RGBA and running
both verifications over it. Comparable to one generative call, with no network,
no bill and no variance.

If you instead see `gemini-mask#1` lines, the provider did not resolve to
`hermes` — go back to step 7.

---

## 9. Memory

Each concurrent image holds a 12-megapixel pipeline; verification peaks around
170 MB. With `--workers 4` in the sheet script that is up to 8 images in flight
(two views per product), so size the box accordingly — on a 16 GB laptop with
3 GB free it ran fine, but it is the first thing to reduce if the worker starts
being OOM-killed.

---

## 10. The fine-tuned model — `cloth-seg-ft`

A second garment parser: `u2net_cloth_seg` fine-tuned on vnyx's own decision and
photobooth raws (28 Sep 2026). It is a separate strategy that is off by default.
When it is switched on, it runs **first**, and the stock `cloth-seg` runs only when
Hermes refuses its cut-out.

**The evidence.** It was tested on the frozen test split: 1,257 photos from 524
products that were never trained on. In a blind A/B review of the 357 photos
where the two models disagreed, reviewers chose the fine-tuned model 249 times and
the stock model 58 times; 37 were both fine and 13 were neither. It kept a solid
piece of the set (podium, floor, a second garment) on 46 photos, against 152 for
the stock model. The 58 losses are real, and they are the reason the stock model
stays in the chain behind it.

**What it needs.**

1. **The file.** Copy `cloth_seg_ft_v1.onnx` (176 MB, md5
   `ffd45cce9238eb28331d9a8ca5981f45`) next to the stock model:
   `/var/lib/hermes/models/cloth_seg_ft_v1.onnx`. Keep the version in the name
   and never overwrite the stock `u2net_cloth_seg.onnx`.
2. **The path**, in the systemd unit next to `U2NET_HOME`:
   ```
   Environment="HERMES_CLOTH_SEG_FT_PATH=/var/lib/hermes/models/cloth_seg_ft_v1.onnx"
   ```
3. **The switch**, in `config/policy.yaml` under `imagery.cutout`:
   ```
   strategies: ["cloth-seg-ft", "cloth-seg"]
   ```
4. Run `daemon-reload` and restart. The first cut-out logs
   `cloth-seg model /var/lib/hermes/models/cloth_seg_ft_v1.onnx loaded`, and each
   one it produces logs `bg-removal cloth-seg-ft#1 produced a cut-out`.

**Before switching it on for everyone,** run one day's products without
publishing, and compare the flagged-cut-out count with the same day on the stock
model.

**Rollback.** Put `strategies` back to `["cloth-seg"]` and restart. The path can
stay set; while the strategy isn't listed, it does nothing.

**If the path is set but the file is missing,** the strategy logs
`the fine-tuned cloth-seg model file is missing` and the chain carries on with
the stock model. It never fails the request.

**A newer version** (v2, …) ships the same way under its own file name. Point the
path at the new file and keep the old file until the new one has run for a while.

---

## 11. Two fine-tuned models, and the URL-in, PNG-out endpoint

Since 29 Sep 2026, Hermes can cut a photo with **v2** (`cloth_seg_ft_v2.onnx`),
fall back to **v1** (`cloth_seg_ft_v1.onnx`), and use **Gemini** only when both
fail. A new endpoint takes an image URL and returns the cut-out PNG directly.

### The chain, per photo

1. **v2** (`cloth-seg-ft`) cuts it, and Hermes's checks judge the result.
2. If it's refused, **v1** (`cloth-seg-ft-backup`) cuts it, with the same checks.
3. If both are refused **but their garments agree** (IoU ≥ `agreement_min_iou`,
   0.98), v2's cut-out is accepted (`cloth-seg-ft+agreed`). The leftover check
   misreads white and pale-patterned garments as white backdrop.
   - On Midtex, 11 of 12 refusals were such false alarms, and all 11 agreed.
   - On the fix queue, 522 of 525 usable photos accepted this way had been
     judged good by a person.
4. Otherwise **Gemini's mask** (`gemini-mask`, a **paid** call, tried twice) is
   intersected with v2's garment.
5. If nothing is left, the endpoint answers 422 and a person has to look.

### What goes on the server

1. **Deploy this code** (the files changed on 29 Sep: `app/imaging/cutout.py`,
   `app/main.py`, `config/policy.yaml` and the tests) the usual way to
   `/srv/hermes`.
2. **Copy the two model files** next to the stock model, never overwriting it:
   ```bash
   scp cloth_seg_ft_v2.onnx cloth_seg_ft_v1.onnx hermes-host:/tmp/
   sudo install -o hermes -g hermes -m 0644 /tmp/cloth_seg_ft_v2.onnx /var/lib/hermes/models/
   sudo install -o hermes -g hermes -m 0644 /tmp/cloth_seg_ft_v1.onnx /var/lib/hermes/models/
   md5sum /var/lib/hermes/models/cloth_seg_ft_v*.onnx
   #   0c6fb7f4cc32add8bc0368c08b1d6a5b  cloth_seg_ft_v2.onnx
   #   ffd45cce9238eb28331d9a8ca5981f45  cloth_seg_ft_v1.onnx
   ```
3. **Point Hermes at them.** Add to `hermes.service`, and to `hermes-worker.service`
   if the worker should use them too:
   ```ini
   [Service]
   Environment="HERMES_CLOTH_SEG_FT_PATH=/var/lib/hermes/models/cloth_seg_ft_v2.onnx"
   Environment="HERMES_CLOTH_SEG_FT_BACKUP_PATH=/var/lib/hermes/models/cloth_seg_ft_v1.onnx"
   # optional, recommended if the endpoint is reachable from outside the box:
   Environment="HERMES_CUTOUT_API_KEY=<a long random string>"
   ```
   Gemini uses the `GEMINI_API_KEY` Hermes already has.
4. **Restart:** `sudo systemctl daemon-reload && sudo systemctl restart hermes`.

The endpoint's chain is `imagery.cutout.url_strategies` in `config/policy.yaml`,
which defaults to `["cloth-seg-ft", "cloth-seg-ft-backup", "gemini-mask"]`. It
is **separate from `strategies`**, so vnyx-api's existing calls to
`/v1/imagery/remove-background` are unchanged. To use the same chain there too,
set `strategies` to the same list.

### Calling it

```bash
# POST: JSON in, PNG out
curl -sS -X POST http://127.0.0.1:8080/v1/imagery/cutout \
     -H 'content-type: application/json' -H 'X-Api-Key: <key, if set>' \
     -d '{"image_url": "https://pub-….r2.dev/inspection/….jpg"}' \
     -D headers.txt -o cutout.png

# GET: handy from a browser or a script
curl -sS -G http://127.0.0.1:8080/v1/imagery/cutout \
     --data-urlencode 'image_url=https://pub-….r2.dev/inspection/….jpg' -o cutout.png
```

| Response | Meaning |
| --- | --- |
| **200** `image/png` | RGBA, transparent background, at the photo's resolution: turned upright from EXIF the way a browser shows it (a 4000×3000 decision original with orientation 6 comes back 3000×4000). Headers: `X-Cutout-Provider` (`cloth-seg-ft`, `cloth-seg-ft-backup`, `cloth-seg-ft+agreed`, `gemini-mask`), `X-Cutout-Width`, `X-Cutout-Height`, `X-Duration-Ms`. |
| **422** JSON | `{ok: false, error, provider}`: nothing produced an acceptable cut-out. `error` lists every attempt and why it was refused. |
| **400** | The URL could not be fetched, or the bytes are not an image. |
| **401** | `HERMES_CUTOUT_API_KEY` is set and the request didn't send the right `X-Api-Key`. |
| **503** | The v2 model isn't configured or the file is missing. The endpoint refuses rather than sending every photo to the paid strategy. |

**Time:** about 15–30 s per photo on a CPU box. v2 alone takes about 15 s; the
fallback and the agreement check add about 12 s when they run. A Gemini fallback
adds its own time and cost. Set `timeout_s` in the body to cap the paid calls.

**It writes nothing.** The endpoint returns bytes and stores nothing, the same as
every other endpoint here. Saving the PNG is up to the caller.

**Rollback:** remove `cloth-seg-ft` from `url_strategies`, or unset the two
paths and restart. With the paths unset the endpoint answers 503 and nothing
else changes. The agreement rule is switched off with `agreement_min_iou: 0`.

---

## 12. Crop and centre every cut-out (30 Sep 2026)

After the background is removed, Hermes crops the garment out along its bounding
box, scales it by one factor so it fills **90% of the frame** on its limiting
axis (`margin: 0.05` a side), and centres it on a transparent canvas the
photograph's size. A crop and a resize — nothing is re-drawn or enhanced; the
largest enlargement is `max_scale: 2.5`. Code: `app/imaging/framing.py`;
settings: `imagery.cutout.framing` in `config/policy.yaml`.

Where it applies:

- `POST /v1/imagery/remove-background` — the auto-approval matte and re-matte.
  The response carries `framing: {scale, garment_box, placed_at, …}`.
- `POST|GET /v1/imagery/cutout` — headers `X-Cutout-Framed`, `X-Cutout-Scale`.

Both take `frame: false` (GET: `&frame=false`) for the photograph's own framing.
`framing.enabled: false` in policy turns it off everywhere.

The checks: `cutouts.garment_hole` **registers** the photograph to a framed
cut-out (finds the scale and shift) before the pixel match, overlap and collar
tests, so framing is not read as a zoom — a re-drawn garment still fails. A new
check re-cuts any cut-out that is not framed to the standard (off centre by more
than 3%, or filling outside 85–95%), so existing approved cut-outs are re-framed
the next time auto-approval runs on them (free: the local models). The
cross-view scale test is off while framing is on.

**The four methods, in order** (`imagery.cutout.strategies`, and `url_strategies`):

1. `cloth-seg-ft` — v2 (local, free)
2. `cloth-seg-ft-backup` — v1 (local, free)
3. `gemini-paint` — Gemini background removal on the regen model
   (`imagery.generation.model`, `gemini-3.1-flash-image`); PAID, two attempts
4. `openai-paint` — gpt-image background removal on a transparent background,
   quality `imagery.generation.openai_quality` (`medium`); PAID, two attempts

Each candidate must pass every check, including the **torn-garment check**
(`_torn_garment`): holes through the garment where the photograph shows the
garment's own colour, not the wall (`torn_min: 0.0025`; calibrated on 79 sound
views ≤ 0.14% against 0.40–2.45% torn). Paid results are brought to the
photograph's resolution, then framed. Each paid call is capped at
`paid_timeout_s: 120`; vnyx-api now waits up to 420 s for Hermes
(`HERMES_BG_REMOVAL_TIMEOUT_MS`).

**If all four fail**, a re-cut (auto-approval sends the cut-out on file with
`--keep-better`) gets the **existing image cropped and centred** onto the
photograph's canvas (`provider: existing-framed`). Stored cut-outs are opaque,
so the garment is located against their flat backdrop; when its edge is not
clear against that backdrop (white on white) nothing is cropped and the image
stays as it is (`kept_existing: true`, nothing written).

The **keep-better veto is off** (`keep_better_veto: false`): the image on file
no longer blocks a new cut-out for having "more garment" — measured, it was
keeping on-file pictures with the booth's pedestal or a hanger in them.

Deploy: Hermes only (restart `hermes`, `hermes-worker`, `hermes-beat`). The
vnyx-api change to `scripts/backfill-bg-removal.ts` (the canvas taken upright
from the EXIF orientation) should go out with it, or decision cut-outs are
squeezed back into the stored landscape frame.

Check on the server:

```bash
curl -s -o /tmp/f.png -D - "http://127.0.0.1:8080/v1/imagery/cutout?image_url=<raw url>" | grep -i x-cutout
# X-Cutout-Framed: yes   X-Cutout-Scale: 1.6...
```

---

## 13. Hung bottoms refined before they are checked (1 Oct 2026)

> **Off since the same day** (`refine.enabled: false`): photos hung on the wall now
> go to the hanger route in §14, which keeps the clips and removes no cloth. The
> code and the guards below stay; `enabled: true` brings it back.

Bottoms photographed on a hanger against the wall come out of v2/v1 with the
hanger bar, bits of the clips and a ragged waistband. Each v2 / v1 (and mask)
cut-out of **bottoms** now goes through `app/imaging/garment_cutout.py` — the
photobooth refinement script, vendored with small marked changes — via
`app/imaging/refine.py`, **before** the leftover, tear and keep-better checks:

1. wall wire and specks out
2. the waistband top edge rebuilt, and everything above it dropped (bar, hook, clip heads)
3. clips found; the fabric they hid filled from the garment's own texture
   (exemplar fill; Big-LaMa when `lama_path` is set and torch is installed)
4. the wall between fringe threads removed
5. the edge colour cleaned (pymatting)

Everything outside what the clips hid is the photograph's own pixels.

**Only bottoms, never dungarees.** The script treats everything above the
waistband line as not-garment, which would cut a collar, a hood or a bib. Which
garment it is comes from vnyx-api: `backfill-bg-removal.ts` sends the product's
category and subcategory as `garment` ("Bottoms Shorts"). No hint means no
refinement.

**Never worse than v2's own cut-out.** The refined one is used only when:

| guard (`imagery.cutout.refine`) | default | caught |
| --- | --- | --- |
| the script ran (no waistband → `NoWaistband`) | — | BOA-006118 BACK |
| `min_waistband_contrast` | 4.0 | cream/white jeans on the white wall (KIL-001216 FRONT, BOA-005343 BACK) |
| `max_generated_frac` (made up) | 2% | BOA-005343 FRONT, 3.1%: stopped before the slow fill |
| `max_lost_frac` (taken away overall) | 8% | |
| `max_fabric_taken` (taken away where the photo shows fabric) | 0.25% | grey shorts, waistband dipped (BOA-001412, 0.43% / 0.62%; 20 good ones ≤ 0.12%) |

Otherwise v2's cut-out goes on unchanged and the log says why. The response
carries `refine: {refined, clips, generated_frac, lost_frac, fabric_taken, …}`
(or `{refined: false, why}`); `/v1/imagery/cutout` sets `X-Cutout-Refined`.

**Cost.** CPU only, nothing paid. The script runs on the garment's box plus a
margin (`crop_pad_top_px: 400`, `crop_pad_px: 150`) rather than the whole
frame.

Deploy: Hermes (needs `pymatting` and `scipy`, already in the virtualenv) and
the vnyx-api change in the same release. Hermes alone works, but refines nothing
until vnyx-api sends `garment`. Off: `refine: false`.

---

## 14. Photos hung on the wall: IS-Net, clips kept (1 Oct 2026)

`app/imaging/hanger_cutout.py` (the hanger script, vendored with marked changes)
is the `hanger-isnet` strategy. IS-Net finds the whole garment — clips, fringe,
waistband — and only the THIN hanger parts at the top (bar, hook, wall wire) are
masked, painted out of the photo and the cleaned photo segmented again. Outside
the hanger mask the second pass can only add garment: no cloth is removed, and
the clips stay by design.

**Which photos.** vnyx-api's matte (`backfill-bg-removal.ts`) now sends the raw
photo's `origin`:

| origin | chain |
| --- | --- |
| `WEB`, `MANUAL` (hung on the wall) | hanger-isnet → gemini-paint → v2 → v1 → openai-paint |
| `PHOTOBOOTH`, `DECISION` (podium, stand) | v2 → v1 → gemini-paint → openai-paint, as before — the fine-tuned cloth-seg is trained to remove the podium and stand |
| none sent | as before |

**When IS-Net is not used** the photo goes to Gemini's background removal —
**once** (`imagery.cutout.paid_attempts: 1`, everywhere; it was 2) — then to v2/v1,
which keep the bar and clips on these photos, and gpt-image last:

- no hanger bar found (a model wearing it, a web image on white; also some
  hanger photos the bar search misses) — it stops right after the search;
- the script's own review flag ("hanger mask covers a lot of garment");
- `max_wall_kept` 0.4%: a solid, smooth (painted, not woven) region IS-Net kept
  beyond v2's outline — the lit white wall between a cream pair's legs
  (BOA-005343: 1.11% / 0.55%; every other view ≤ 0.25%);
- Hermes' own checks (leftover, torn).

**A painted cut-out must be the photo's own garment** (`paint_min_edge_match`
0.70, `paint_min_pixel_match` 0.50 as a floor, every route). The paint strategies
return a new picture; `_same_framing` only checks it is framed like the photo.
gpt-image REDREW all 12 hung garments it was given — "JACKS SURFBOARDS" came back
"SMCKS SAR BONBL'S" (and, on another call, an obscenity), "RipCurl" "PpCuy",
embroidery and creases invented — and every one used to pass. The garment's edge
structure is now correlated with the photo's at the same place: gpt-image
-0.02..0.63; Gemini's faithful cut-outs 0.73..0.94 (it relights, so its brightness
agreement drops to 0.54 on a faithful one); its zoomed or redrawn answers
-0.01..0.41; v2 0.99. That is also why gpt-image comes after v2/v1 on this route:
asked first it cost a minute and a call and was refused.

**Fixes to the script** (marked `HERMES:`): OpenCV 5 returns Hough lines as
(N, 4); a pencil guide line on the wall was taken for the bar (a bar must be part
of what IS-Net kept); the bar sits ON the waistband on this wall (wall above the
line counts as outside); painted-out wall inside the hanger mask is dropped.

**Painting the hanger out.** LaMa when `HERMES_LAMA_PATH` points at `big-lama.pt`
and torch is installed; OpenCV's Telea otherwise, which leaves a grey smear where
the bar crossed the waistband. CPU timings on the dev laptop, 3000x4000: ~50–80 s a
view with LaMa, ~20 s with Telea; a photo with no bar ~10 s.

**The clips are not a leftover** (`photo_audit.gallery.clips_are_leftovers: false`):
the photo audit no longer calls "hanger clips visible" a cut-out defect, or every
such cut-out would be re-cut on every run. A hanger, hook or stand still is.

**Margin.** `imagery.cutout.framing.margin` 0.15 (was 0.05): the garment fills 70%
of the frame on its limiting axis, centred — the BOAS shop page's own framing.
Every cut-out framed at 90% before is re-framed the next time auto-approval runs
on it (free).

**The waistband refinement of §13 is off** (`refine.enabled: false`).

On the server:

```bash
pip install torch --index-url https://download.pytorch.org/whl/cpu      # optional, for LaMa
mkdir -p /opt/hermes-models/lama && curl -L -o /opt/hermes-models/lama/big-lama.pt \
  https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt   # 196 MB
echo 'HERMES_LAMA_PATH=/opt/hermes-models/lama/big-lama.pt' >> .env
python -c "from rembg import new_session; new_session('isnet-general-use')"  # pre-fetch IS-Net (170 MB)
```

Deploy Hermes and vnyx-api together: without `origin`, Hermes runs the old chain.
Off: `hanger: false`.

---

## 15. The cut-out keeps the photograph's quality (3 Oct 2026)

The garment in a cut-out is now the photograph's own pixels, at the
photograph's size and in its colour profile. Measured on BOA-001263 FRONT
(3000x4000, hanger route): 98.25% of the opaque garment pixels are bit-identical
to the photo; the rest is the strip where the hanger bar crossed the waistband,
which has to be painted. Framing changes none of them (100% identical to the
cut-out).

| what | before | now |
| --- | --- | --- |
| framing (`framing.upscale`) | garment enlarged up to 2.5x to fill the photo's canvas | **never enlarged**: the canvas is cropped round the garment at the photo's ratio (BOA-001263: 1836x2448 out of 3000x4000, scale 1.0); a garment too big for the margin is still scaled DOWN |
| colour profile | dropped, so a photo shot in a wide-gamut profile was shown as sRGB | the photo's ICC profile goes into the PNG without re-encoding (`_png_with_icc`); painted answers (Gemini, gpt-image) carry none |
| EXIF rotation (`_upright`) | JPEG q95 | q98, no chroma subsampling, ICC kept |
| hanger script edges (`hanger.refine_edges`) | — | **off**: its guided filter WIDENED IS-Net's edges (BLM-001006 FRONT: 2.9 → 4.3 soft px per outline px) |
| `/v1/imagery/cutout` size | resized to the photo's size | left at its own size when framing is on (it is already the crop) |

The response's `framing` gains `canvas`, `cropped` and `standard: true`.

**vnyx-api must not pad it back out.** `backfill-bg-removal.ts` used to fit
every cut-out onto the raw photo's canvas (`fitToCanvas`), which would shrink
the garment back to its old size. `process-image-background.ts` now returns
`keepCanvas` when Hermes says `framing.standard`, and the matte keeps Hermes'
canvas. Ship both together; Hermes alone gives cut-outs that vnyx-api pads back
to the photo's size (no worse than before, but no better).

`framing.upscale: true` brings the enlargement back.

---

## 16. Shoes and bags: IS-Net as an object (6 Oct 2026)

A product whose category is footwear or a bag is cut by a new strategy,
`object-isnet` (`app/imaging/object_cutout.py`), and judged by its own checks.
v2 and v1 are clothing parsers with no class for a shoe or a bag; they are not
asked at all for these products.

**What was measured** (raws from prod, read-only; nothing written):

| | shoes (12) | bags (6) |
| --- | --- | --- |
| cloth-seg v2 | "no garment" or fragments on 11 | **passed** 2 bags with half a handle cut off |
| SegFormer `segformer_b2_clothes` (shoe and bag labels) | shredded all 12 — a human parser; with no person in the frame a pair of trainers read "Dress 66%" | partial bags; one empty cut-out was "accepted" by the garment checks |
| IS-Net (`isnet-general-use`) | both shoes whole, table gone | whole bag, straps kept |
| BiRefNet-lite | agreed with IS-Net (IoU 0.99+) | 90-280 s a photo on CPU — not usable |

Then the module, unchanged, on **70 held-out photos** (Klekt, BOAS, Bleckmann,
Osdorp, Kilo): 67 cut and passed, ~8 s each, every one checked by eye — no bad
cut-out passed. The 3 refused were right to be: boots held up by a person
(KLE-002830), a bag on a chair cut off by the frame (OSD-000003), and a basketball
jersey filed as a running shoe (KIL-001787).

**How it cuts.** IS-Net on the full frame finds the product (every large piece —
a pair standing apart is two — and everything thin attached: laces, ties,
straps); a second pass on a crop round it adds edge detail and may only ADD (the
crop pass alone dropped whole leather panels on KLE-002829); the wall wire, far
specks, and straight strips outside the product's core that run to the frame's
edge (a table's lit edge — KLE-002828/2829) are dropped.

**The checks** (instead of `_kept_backdrop` / `_torn_garment`, for every
strategy's cut-out of these products, the paid ones included):

- kept between `min_kept` (0.3%) and `max_kept` (70%) of the frame;
- clear of the frame's edge (`max_edge`) — a table, the wall, an arm reaches it;
- each gap read as a whole: a tear only when ≥ `tear_coloured` (0.9) of it is
  the product's colour in the photo; the wall through a handle, a heel's arch
  and the space between two shoes are openings.

The garment checks refused 10 of 12 clean shoe cut-outs (grey and white leather
on the grey table is "the backdrop colour") and 5 of 6 bags (the wall through
the handle is "a tear").

**Routing.** `object.families` matched as whole words against the category
vnyx-api sends as `garment` ("Shoes Running", "Men Shoes", "Backpacks & Bags
Tote Bags"); BOOTCUT jeans and BAGGY trousers do not match. The chain is then
`object-isnet` → `gemini-paint` → `openai-paint` (`object.then`), and the paid
prompts are told the product is footwear / a bag and that a hook, a table or a
hand holding it is not part of it. Every origin by default (`object.origins:
[]`). Garments are untouched: their routes are exactly as before.

**On the server: nothing to install.** It uses the IS-Net model the hanger
strategy already loads (`$U2NET_HOME/models/isnet-general-use/`), the same
session, no new memory. `transformers` / SegFormer are NOT needed. Deploy Hermes
and restart the three services; check the log for

    bg-removal: 'Shoes Running' is footwear — the object cut-out first, judged by the object checks
    bg-removal object-isnet: 2 piece(s), 0 wire(s), 1 edge strip(s), ...
    bg-removal object-isnet#1 produced a cut-out in 9.4s

**vnyx-api.** `backfill-bg-removal.ts` (auto-approval's matte / re-matte)
already sends the category. `product-imagery.ts` (the matting before a render)
now sends it too; until that ships, that one path cuts shoes and bags with the
garment chain as before. Either order is safe.

`object: false` turns it off.

**Known limits.** A product the photo itself cuts off, or one held by a person,
is refused (edge) and goes to the paid strategies — and to a human if they fail
too. A thin marker line drawn on the studio table can stay stuck to a heel
(KLE-002829, ~40 px): both IS-Net passes keep it, and a rule to drop it would
also drop real laces.

---

## 17. Memory: no ONNX arena, two cut-outs at a time (6 Oct 2026)

6 Oct 2026, 05:45 UTC, vynx-agent (7.8 GB, swap 4 GB full): `Out of memory: Killed
process (uvicorn) anon-rss:7694700kB`. Every request in flight died — the auto-approval run
showed "gate_unavailable: Hermes gate request failed: fetch failed" and "background removal
… unreachable or timing out", and a "no hanger bar" answer took 63 s instead of ~10.

Measured, 4 full-size photos at once (vnyx-api sends a product's photos in parallel):

| | resident when idle | peak | held afterwards | all 4 done |
| --- | --- | --- | --- | --- |
| before: ORT arena on, no cap | 3.0 GB | 6.2 GB | 5.7 GB | 45 s |
| arena off, no cap | 0.5 GB | 4.7 GB | 0.7 GB | 43 s |
| **now: arena off, 2 at a time** | 0.5 GB | **2.7 GB** | 0.7 GB | **46 s** |
| arena off, 1 at a time | 0.5 GB | 1.9 GB | 0.7 GB | 58 s |

(LaMa via torch adds ~1 GB on the server when the hanger route paints.) The arena kept
every ONNX session's biggest buffer for good; `app/imaging/ort_memory.py` builds the
sessions without it (`HERMES_ORT_ARENA=1` brings it back) and caps cut-outs at
`imagery.cutout.max_concurrent` (2). A request that waits more than `max_wait_s` (300 s,
under vnyx-api's 420 s) is refused as "Hermes is busy" — that photo is not cut this run;
the server is not killed.

Deploy: pull, `sudo systemctl restart hermes hermes-worker hermes-beat`. In the log,
`bg-removal: waited 12.3s for one of 2 cut-out slots` is the cap working.

---

## 18. Framing is not a re-cut; a defective cut-out is re-cut with other methods (6 Oct 2026)

Midtex part 1 (100 products): the matte step took 120-200 s a product, and 9 products
were left with a wrong cut-out (a block of studio, the mannequin below the hem, torn
side panels, a cut-away collar or strap) that every later step passed or skipped.

**Why so slow.** Almost every product was re-matted only because its cut-out was not
cropped and centred to the standard ("fills only 54% of the frame"). That re-ran the
whole chain per view — v2 (~6 s of model, ~5 s of PNG encoding, ~2.5 s of checks on
a 3000x4000 locally; more on the server, two at a time), v1 when a check refused, then
Gemini and gpt-image (paid, 30-120 s each) — to make the same cut-out again and frame
it. Now:

- `cutouts.judge` marks a view whose only problem is the framing standard
  (`CutoutVerdict.frame_only_views`), and the matte step sends those views as
  `--bg-strategies frame-only`: Hermes crops and centres the cut-out on file
  (`main.EXISTING_FRAMED`) without cutting again, ~2 s. If it cannot be framed safely,
  the view is re-cut as before.
- PNG encoding at zlib level 6 instead of `optimize=True`: 0.39 s against 1.02 s per
  3000x4000 save, no larger.

**Why defects stayed.** Nothing ever asked a DIFFERENT method. The matte step's
re-matte ran the default chain, got the identical answer ("neckline cut away" twice on
MID-000053's FRONT) and stopped at STILL WRONG; the rematte step skipped whenever the
matte step had re-cut ("the same segmenter would return the same cut"). Now the default
chain answering the same way twice in one run ESCALATES, once, to
`readiness.cutouts.recut_strategies`:

    recut_strategies: ["object-isnet", "gemini-paint", "openai-paint"]

- in the MATTE step, when the re-matte's re-check measures the same defect;
- in the REMATTE step, when the photo audit still flags a cut-out the matte step
  re-made this run (a flag on a product the matte step did not touch keeps the chain's
  own answer, as before — the current chain gets its shot at a cut an older one made);
- the flagged views only, `--keep-better` still on, never a second paid round.

`object-isnet` is first and free: the whole-product IS-Net cut now runs for a GARMENT
too when it is named without any parser (an explicit ask; the policy default and
`url_strategies` name the parsers, so their family gate is unchanged). Measured on
MID-000053's BACK DECISION photo — the dark layer behind the racerback read as
background to every parser, torn on every re-cut; both paid methods were refused
(Gemini left scene at the border, gpt-image redrew) — the IS-Net cut is whole, hanger
kept, and passes every garment check free.

**Photo audit.** A hanger, its hook or the decision panel's clamp left on a cut-out is
accepted (`photo_audit.gallery.hangers_are_leftovers: false`, like the clips). The
prompt now names the mannequin form below the hem and a block of the studio as `clear`
leftovers — both were passing.

**A defect the run cannot repair now HOLDS (6 Oct 2026, the operator's call):
`readiness.cutouts.hold: block`.** MID-000053 went through a full run with its FRONT
cut-out measurably wrong after every re-cut, flagged twice — and was approved and
synced to Shopify, because `soft` let every finding pass as a note. Now the measured
checks hold as CUTOUT_UNFIXABLE and the photo audit's cut-out defects as
CUTOUT_DEFECT; outcome HELD_FOR_HUMAN. Expect more holds in the first runs after
deploying — that is the point.

**Stray pieces (same product, same day).** Gemini's keyed answer left a corner of the
studio screen opaque at the frame's foot — dark, so every backdrop-COLOUR check passed
it, and a fragment floated beside the tank top on the live listing. Two rules now:

- a GARMENT cut-out drops any piece that is small beside the garment (< 5% of its
  area) and clear of it (bounding-box gap > 4% of its long side) before the checks
  (`_drop_stray_pieces`); a set's second garment is large and a detached button sits
  close, so both stay;
- the escalation's `object-isnet` cut of a garment is SINGLE-piece
  (`object_cutout.Config.single_piece`) — shoes keep their pair rules.

**vnyx-api** forwards `--bg-strategies` (route option `bgStrategies`, closed list of
Hermes' method names, advertised on `/ping`) to Hermes as `strategies`. Hermes sends it
only when `/ping` lists it, so either can deploy first; until vnyx-api is deployed the
matte step re-cuts as before and the rematte skip stays.

The photo-audit prompt changed, so every product's audit is asked once more (the cache
key includes the prompt).

---

## 19. The photograph's resolution is the cut-out's to keep (7 Oct 2026)

"Why is my quality degraded when we do background removal": a 674x899 FRONT cut-out
sat beside its 3000x4000 booth photo (MID-000249 — all four of its cut-outs measure
1/2.5 to 1/3.4 of their photographs' resolution). Two causes, two fixes:

- **The old providers wrote the model's render size and nothing measured it** — the
  canvas ratio check is blind to a same-ratio downscale by design. New measured check
  (`cutouts.resolution_problem`, `readiness.cutouts.resolution_max: 1.6`): the
  photograph's long side over (the registered garment scale x the cut-out's long
  side). Over the limit, the view is re-cut from the raw at full size by the matte
  step. Hermes' framed cut-outs measure ~1.0 whatever their canvas, so a small
  garment's small canvas is never flagged; when the two pictures neither line up nor
  register, nothing is said.
- **The paid paint methods still did it**: Gemini's and gpt-image's background
  removal return the model's ~1 MP render, and accepting one stored it. Once a paint
  passes the framing and fidelity checks, its SILHOUETTE is now applied to the
  original photograph like a mask strategy (`_paint_as_mask`,
  `imagery.cutout.paint_keep_resolution`): full resolution, the photo's own colours
  and ICC profile, the model's relighting gone with its pixels.

**The mannequin's neck stub (same day, the operator's call).** Cutting the form's
neck out of the collar opening makes the collar look torn, so the stub INSIDE the
collar opening is now accepted — the photo-audit prompt says it is kept on purpose,
and the leftovers filter treats "mannequin neck" like the hanger. The form below the
hem or beside the garment still counts, and the measured neckline-loss check (garment
fabric eaten at the collar) is unchanged.

Expect the first run over an old tenant to re-cut most of its catalogue once: every
render-size cut-out from the old providers is now flagged. That is the point — each
re-cut takes the original's pixels through the standard framing.

---

## 20. The neck opening of a top, finished (7 Oct 2026)

MID-000351, both FRONT cut-outs:

- **PHOTOBOOTH** — the collar opening shows the booth's white mannequin neck; the parser
  rightly cut it out, and the empty U read as a torn collar.
- **DECISION** — the opening shows the black studio, a metal bracket and the white
  hanger; the parser kept scattered fragments of them (black spots in the neckline).

`app/imaging/neckline.py`, called by `remove_background` AFTER every check has accepted
a cut-out (the checks still judge the segmenter's own answer), on the garment chain only:

1. The body = the cut-out's largest solid piece. The collar tips = the highest point of
   its top edge either side of a central dip. Only a real neck counts (depth 2-22% of
   the garment's height, width 10-48% of its width, both tips above the dip) — so tank
   tops, turtlenecks, V-neck cardigans, hoods, back views, bottoms, shoes and bags are
   never touched (checked on 16 Midtex photos of all of those).
2. The opening = the region the collar and the straight line between the tips enclose
   (flood-filled, so a collar that curves outward below its tip is covered; a flood
   that leaks through a gap in the collar does nothing).
3. **PHOTOBOOTH** (`fill_origins`): the opening is put back from the photograph's own
   pixels — the form, lit side and shadowed side — cut flat at the tips' line. If more
   than `max_backdrop` (30%) of the opening is the studio (a neckline wider than the
   form's neck), it is left open instead.
4. **DECISION** (`clear_origins`): the opening is flooded through everything that is
   NOT one of the garment's own colours (sampled from the collar and chest below the
   neck) and cleared. Thin slivers left standing beside the cleared area go too. The
   inside back of the garment, when it shows in the opening, is garment-coloured and
   stays.
5. **Any other origin** (the wall, an unknown one): left as it is. This was added 7 Oct
   for MID-000430 (§21). On a wall photo the opening shows the garment's own white back
   collar band. Clearing cut it ragged and chopped the front collar's tips flat.

The keep-better guard accepts the finished cut-outs (MID-000351: 104% and 100% of the
garment on file). Policy: `imagery.cutout.neckline` (`false` turns it off).

**Existing cut-outs change only when re-cut.** No check flags an unfinished neckline, so
products already in the catalogue keep theirs until their next re-cut, or a deliberate
one (`backfill-bg-removal.ts --product <ids> --views FRONT --replace --keep-better
--provider hermes --apply`).

---

## 21. Smooth parser edges; white trim put back on wall photos (7 Oct 2026)

MID-000430's FRONT (a black Adidas tee, white V collar and cuffs, on a wire hanger
against a white wall) showed three faults on the live listing:

- **a zig-zag outline.** rembg's `predict` takes the parser's ARGMAX label map at
  768x768 and LANCZOS-resizes the labels to the photograph — 5.2x on a 3000x4000 — so
  every cloth-seg outline, on every origin, was a staircase of ~5 px steps with wall in
  the risers.
- **the collar "missing".** The white BACK collar band, seen above the front neckline
  (the hanger's hook goes through it), was cut away: the parser scored it 0.06-0.6
  against a wall of the same colour.
- **a white cuff missing** on the left arm, the same way.

The hanger strategy never ran: the hanger is inside the shirt, so there is no bar to
find ("no hanger bar found in the photo") and the photo went to the parser.

**1. The parser's own score, snapped to the photograph** (`_cloth_score`, `_cloth_alpha`,
`_decontaminate`; every origin). The score is upsampled bilinearly, a guided filter
with the photo as guide (radius 0.4% of the long side) moves it onto the photograph's
edge, and a smoothstep over [0.44, 0.56] makes a 1-2 px antialiased edge.
Semi-transparent edge pixels take the nearest solid garment colour, so no wall rim
remains. Measured on a shoulder: the photo's edge at x 2071, rembg's at 2077 in steps,
the snapped one at 2072. If the session has no raw ONNX handle, rembg's own path is
used as before.

**2. White trim put back, wall photos only** (`_recover_trim` via `_finish_trim`;
`trim_origins` WEB, MANUAL). This runs AFTER the checks accept a parser's cut-out.
Joined to a white collar, the trim is a white ring that the leftover check would
rightly refuse on a podium. Colour cannot tell trim from a white wall, and texture
could not either: the first try took the sleeve's shadow, whose own edge reads as
texture, and left a ragged outline. An **edge** can. Trim meets the wall at a step (a
seam, a fold, its own shadow line); a shadow fades into the wall with none.

- The wall is flooded from where it is certain, through every pixel that is not an
  edge. Certain means beyond 2x `trim_ring` (5%) of the garment's width, or scored
  under `trim_wall_score` (0.02). An edge is a lightness gradient over `trim_edge`
  (10).
- Gaps narrower than `trim_gap` (0.8% of the width) are closed to the flood: at a
  collar's corner the band's edge and the front collar's leave one.
- Whatever the flood cannot reach, within `trim_ring` of the garment, is a candidate.
  It is kept when the parser leaned that way on average (`trim_min_score` 0.12) and it
  touches the garment.
- Pieces as thin as the gap (a hanger wire, a pencil line, a band's tapering tips) are
  opened away.
- Edge pixels go to the nearer side, and a guided filter draws the new outline on the
  photograph's edge.
- More than `trim_max_add` (4%) of the garment is not trim, and nothing is added.
- **Tops (or garments of unknown type) only.** Bottoms on a clip hanger are excluded:
  the wall between the bar and the waistband is a closed-in strip of exactly the kind
  this looks for (MBF-000128).
- **Each piece must look like lost trim** (`_trim_looks_like_garment`):
  - the garment has its colour: at least `trim_garment_share` (0.5%) of the garment's
    pixels lie within `trim_garment_de` (10 ΔE);
  - it is camouflaged against the wall right beside it, between `trim_wall_de` (4)
    and `trim_wall_de_max` (15) away, and nearer the garment by `trim_margin` (2);
  - it is at least `trim_min_piece` (0.1%) of the garment;
  - it does not rise above the garment's top by more than `trim_above` (1% of its
    height). What rises above a hung garment is the clip, hook or hanger.

**Regression, 69 wall photos from 14 tenants (prod raws, read-only):**

| version | photos with additions | wrong |
|---|---|---|
| edge flood alone | 59 | most: wall closed in by hanger wires or between an arm and the body, wall slivers on shoulders, wall hooks, clips, a hand |
| + colour tests | 18 | 8 hanger clips, 2 wall wedges inside dark pieces, a wall hook |
| + camouflage limit | 6 | 5 hanger hardware above the garment |
| + above-the-garment rule and tops only | **1** | **none: MID-000430's band and cuff** |

On MID-000430 FRONT, through the whole chain (`cloth-seg-ft+agreed`), it puts back 1.4%:
the whole back collar band with a smooth curved top, and the cuff. It adds nothing on
the sleeves' shadows. The thin red hanger wire is opened away, and no cloth goes with
it.

**A second sample of 69 wall photos** (drawn fresh, 43 of them tops) with the final
rules adds pieces on 4 photos. Three are right: MID-000430's band and cuff,
MID-000455's white mesh side panel at the hem (the trim stops at the mannequin's white
form below it), and a bra strap. One is wrong: a strip of the white mounting board
beside a bra (MBF-000065). That photo's parser cut-out already keeps part of the board
and fails the leftover check, so the chain only accepts it, and so trims it, if both
parsers agree.

**What it cannot do:** MID-000430's BACK view. There the parser dropped every white
part of the shirt, the collar and both cuffs, so the garment has no white to match the
pieces against, and they stay out. A re-cut still gives that view smooth edges.

Two other routes were tried for it and rejected:
- **IS-Net as a second opinion** for such pieces. On the 67 other photos it vouched for
  five pieces, and all five were a mannequin form, a mounting board or wall.
- **Gemini paint.** It redrew the shirt (pixel match 0.08), and the fidelity gate
  refused it.

The real fix is a parser trained on wall photos; the fine-tune so far saw only decision
and photobooth photos.

**User's rule, 7 Oct: "if removing the hanger would tear the cloth, keep the hanger".**
This step only ever ADDS garment the parser dropped near a hanger. It never removes
cloth for the hanger's sake. The hanger's thin wire is not put back, and keeping a
hanger is accepted everywhere it was before (`hangers_are_leftovers: false`; §14's
clips).

The same rule moved the neckline's clearing (§20) to the decision panel only. On
MID-000430 the clearing ran after this step: it cut the band it had just put back
ragged, and chopped the front collar's tips flat.

**Cost:** the parser memoises its 768x768 score per photo and model (~1 MB, at most
eight entries), and the finishing step re-snaps it. That adds 0.7-4 s per accepted
wall-photo cut-out, depending on size. Policy: `imagery.cutout.cloth_edges`;
`cloth_edges: false` restores rembg's mask, and an empty `trim_origins` turns the trim
step off.

**Existing cut-outs change only when re-cut** (same command as §20, the views
concerned). MID-000430:

```
npx tsx scripts/backfill-bg-removal.ts --product bb731b58-62fd-4780-8745-1eb22a8cd217 --views FRONT,BACK --origins WEB --replace --keep-better --provider hermes --apply
```

---

## 22. Booth photos filed as WEB: the stand kept; a cut-off render not re-made (7 Oct 2026)

Midtex auto-approval of six products. Four came out right; two did not:

- **MID-000442 FRONT** (black tank top): the cut-out kept the mannequin's pole, the
  white form under the hem and the podium.
- **MID-000445 BACK** (white tee): the same.
- **MID-000445 AI_BACK_34:** the model was cut mid-shin, and the regen step left it.

**Why the stand stayed.** Both photos are filed as **WEB**, but they were taken in the
booth: curtain, mannequin, pole, podium. WEB means "hung on the wall" to the chain
(§14), so the order is IS-Net's hanger cut, then Gemini, then the parsers.
- IS-Net found no hanger bar.
- On MID-000442, Gemini's answer kept the stand. Every check passed it: the border is
  87% clear, and the podium is grey cloth, not the curtain's colour.
- The re-cuts (IS-Net as an object, then the paid methods) kept the stand too.
- MID-000445's BACK reached IS-Net through the matte step's escalation.

**Fixes** (`app/imaging/cutout.py`):

1. **No bar, no hung photo.** When `hanger-isnet` fails with "no hanger bar found",
   every local cut (v2, v1, stock) is asked before the paid methods. The paid methods
   keep their own order. A WEB photo with no bar is a model, a web image, a hanger
   inside the shirt (MID-000430), or the booth's mannequin. The fine-tuned parsers were
   trained on the booth.
2. **A top that reaches the floor kept its stand** (`_kept_stand`). A top's cut-out
   that touches the bottom `stand_band` (0.5%) of the photograph across more than
   `stand_bottom_max` (3%) of its width is refused, and the chain moves on. This
   catches any method that keeps the podium, including IS-Net, Gemini and the
   escalation. It applies to tops and garments of unknown type. Bottoms are left out,
   because trousers hung on a wall can be framed to the photo's foot. Policy:
   `imagery.cutout.stand_check`, `stand_bottom_max`, `stand_band`.

On both photos the default chain now gives `cloth-seg-ft`: the garment alone, no pole,
form or podium. The readiness neckline check passes both, so the matte step does not
escalate them again. Before/after images: `reports/midtex tenant/MID-000442_FRONT_stand_before_after.jpg`
and `MID-000445_BACK_stand_before_after.jpg`.

**The render.** `photo_audit.gallery.framing` was `soft` (21 Sep 2026: "where the frame
cuts the model is not worth a paid render"), so a knee-up shot cut through the lower
leg was recorded and never re-rendered. The user reversed this on 7 Oct. It is now
`defect`: such a render is a RENDER_DEFECT and goes to the regen step, at one render
per view.

**Also seen:** Midtex's Brain has *Hold on bad cut-out* off, so
`readiness.cutouts.hold` arrives `soft` ("soft — readiness.cutouts.hold" in the log).
Those cut-out defects were flagged, and the products were approved and synced anyway.
Switch it on in the Brain to hold such products for a person instead.

**Repairing the two products:**
```
npx tsx scripts/backfill-bg-removal.ts --product 43492403-b3df-4d05-b880-33c070dd07c1 --views FRONT --origins WEB --replace --provider hermes --apply
npx tsx scripts/backfill-bg-removal.ts --product 44f01516-4f41-4bca-92e3-8cedb788a700 --views BACK --origins WEB --replace --provider hermes --apply
```
No `--keep-better`: the cut-out on file has the stand, so it holds more "garment" than
the correct one, and keep-better would refuse the fix. Then run auto-approval on
MID-000445 again. The photo audit now calls the cut-off AI_BACK_34 (and AI_FRONT_34) a
render defect, and the regen step re-makes them.

---

## 23. Every original gets its own cut-out: matting per view AND origin (7 Oct 2026)

MID-000447, 449, 451, 456 and 460 each have three garment photos: a DECISION front, a
WEB front and a WEB back. Both WEB photos have had cut-outs since 26 Aug. The DECISION
front never had one, so it was missing from the Edited gallery. It was never deleted,
just never made.

The cause was "a view is covered if ANY live row for it is already processed". That
rule lived in `backfill-bg-removal.ts` and in the chain's matte state
(`repair_product.needs_from`). The WEB front's cut-out made FRONT read as done, so the
DECISION front was never sent. The approval gate called it a defect all along:
`blocking=[IMG.010]` on every one of the five.

**Fix:** a RAW original is covered only by a live cut-out of its own view AND origin.
- vnyx-api: `src/services/cutout-coverage.ts` (`rawsWithoutCutout`), used by the
  backfill script, with tests in `test-cases/services/cutout-coverage.test.ts`.
- Hermes: `needs_from`, so the matte step runs and says `matte FRONT (DECISION)`.

A row with no origin still covers, and is covered by, its whole view, as before.
Generation's own rule (`needs_matting_for_generation`) stays per view: a render needs
one cut-out per view, not one per origin.

Dry run on MID-000447 after the change: `FRONT RAW … 1 image(s) need a cut-out`, the
DECISION front, and nothing else.

**Scale:** 31 active Midtex products have an original with no cut-out of its own (plus
5 archived). The list and one backfill command are in
`reports/midtex tenant/midtex_unmatted_decision_fronts.txt`. No `--replace`, so only
those originals are cut and nothing on file is touched. DECISION photos go to the
garment parser first (§14 routing), and the neckline is cleared there (§20).

---

## 24. Cut-outs at the photograph's resolution; ten regen rounds; no label = held, not rejected (7 Oct 2026)

**Small cut-outs.** 1,559 of Midtex's 2,006 current cut-outs are under half their
photograph's size. Most are the old provider's 896x1195 from August. 66 were made today
by "frame only": an old small cut-out was cropped and centred instead of re-cut,
leaving it smaller still (699x933 against 3000x4000).
- `cutouts.resolution_problem` measures the garment's scale by lining the cut-out up
  with the photograph. When that failed it said nothing, so framing looked like the
  view's only problem.
- New fallback: the canvas answers from below. A cut-out under
  1/`resolution_canvas_max` (3.0) of the photograph's long side is re-cut at full size.
- Hermes' own cut-outs sit at 1–2x, because the framing crops the canvas but keeps the
  photograph's pixels 1:1. The old ones sit at 3.3–6.2x.
- `reports/midtex tenant/midtex_lowres_cutouts.txt` lists the 447 active products
  holding such a cut-out, in 9 backfill commands of 50 products each (`--replace
  --keep-better`).

**Regen budget.** `readiness.regen_rounds` went from 3 to 10, both in policy and in the
code default. The Brain's switch still turns regen on or off.

**No care label.** The product now stays in Review: HELD_FOR_HUMAN, outcome
`NO_CARE_LABEL`, reason "care label is missing — add the care-label photograph and run
it again". It used to move to the Rejected tab. The check still runs before the chain,
so no paid call is spent. `readiness.no_care_label: reject` restores the old
behaviour. Missing brand or size after the chain is still rejected, unchanged.

---

## 25. The mannequin's neck on WEB photos taken in the booth (7 Oct 2026)

Midtex photos filed **WEB** are often booth shots: a mannequin on its podium. Their
cut-outs showed the hollow above the collar that MID-000351's photobooth one did, and
read as a broken garment (MID-000480; MID-000461–479 the same). §20 puts the form back
only on PHOTOBOOTH photos, and since §22 a WEB neckline is left as it is.

**The rule (the user's): put the neck back only when a mannequin is in the photo.**
- For WEB and MANUAL photos (`neckline.mannequin_origins`), `_mannequin_neck` looks
  for the form's neck rising above the collar.
- It measures a band above the collar tips' line, 2–12% of the garment's height. The
  centre between the tips is compared with the background beside the neck at the same
  height.
- A mannequin needs at least `mannequin_min_fill` (0.35) of the centre to differ from
  that background, and `mannequin_min_columns` (0.25) of its columns to differ top to
  bottom. A hanger's crossbar fills a few rows, never whole columns.
- If the band or the background beside it falls outside the photo, it is never a
  mannequin. That covers a garment filling the frame, like OTR-000002.

| photos | centre that differs | columns solid top to bottom |
|---|---|---|
| 11 on a mannequin (9 Midtex WEB + MID-000455, TST-000003) | 0.45–0.87 | 0.34–0.48 |
| 5 hung on a wall with a neck (KIL-000338, MID-000031/425/430) | 0.09–0.15 | 0.01 |
| OTR-000002 (filling the frame) | no room, not judged | — |

When a mannequin is seen, the fill is the photobooth's: the photograph's own pixels,
cut flat at the collar tips. All nine Midtex fronts come out filled: before/after
sheets are in `reports/midtex tenant/neck_mannequin_side_by_side/`
(`_overview_all_9.jpg`). PHOTOBOOTH is unchanged (always filled), and DECISION is
unchanged (cleared).

**Existing cut-outs change only when re-cut.** From vnyx-api:
```
npx tsx scripts/backfill-bg-removal.ts --product <ids> --views FRONT --origins WEB --replace --keep-better --provider hermes --apply
```

---

## 26. Bags: the v3 fine-tune, bags only (1 Nov 2026)

Round 3 of the cloth-seg fine-tune (`cloth_seg_ft_v3.onnx`) was trained with
bags added. Measured against the hand masks:

| Photo type | v3 against v2 |
| --- | --- |
| Decision and photobooth (1,257 ordinary photos) | tie: overlap 0.9921 vs 0.9925 |
| Bags | overlap 0.81 → 0.96; background left in 60 → 12; pieces missing 78 → 16 |
| Held products, missing pieces | 66 → 30 |
| Held products on a hanger or mannequin | worse: keeps the hook or neck on ~34 of 373 |

So v3 cuts **bags only**. A new strategy, `cloth-seg-ft-bag`, runs first on the
bags route (`object.families` → "bags"), ahead of `object-isnet`, and is judged by
the object checks like everything on that route. A v3 cut-out the checks refuse, or
a "no garment found", falls through to IS-Net exactly as before. Shoes stay IS-Net
first and garments never see v3: v2 and v1 are untouched.

**On the server.** Copy the file and point the unit at it, next to v2 and v1:

    Environment="HERMES_CLOTH_SEG_FT_BAG_PATH=/var/lib/hermes/models/cloth_seg_ft_v3.onnx"

Restart the three services. Without the variable, or with the file missing, the
bags route is IS-Net alone as it was, and the log says which:

    bg-removal: 'Backpacks & Bags Tote Bags' is bags — the bag parser (v3), then the object cut-out first, judged by the object checks
    bg-removal cloth-seg-ft-bag#1 produced a cut-out in 6.2s

**Nothing to change in vnyx-api.** The bag parser is the bags route's own first
step: any allow-list that lets `object-isnet` run (the policy default,
`url_strategies`, `readiness.cutouts.recut_strategies`) gets it. `object.bag_parser:
false` in policy turns it off without touching the environment.

**Not done.** v3 for held garments would need a v3.1 that drops the hanger habit,
tested on both sets, before the garment route switches.

---

## 27. The AI check before a cut-out is returned; Hermes first at intake (3 Nov 2026)

Plan and files: `docs/HERMES-BG-FALLBACK-PLAN.md`.

**In Hermes.** Every candidate the pixel checks accept — v3, IS-Net, v2, v1 — is
now shown to the Gemini judge beside the photograph it was cut from before the
chain returns it (`app/imaging/cutout.py` `_ai_gate`, `cutout_integrity.judge_pair`:
the same prompt, the same three defects and the same `_fails` rule the stored-row
`integrity` check holds on). A flagged candidate is refused like any other check's
refusal and the next model runs. When every model is refused the answer is
`ok:false` with every verdict in `ai_check`, and vnyx-api turns to its own Gemini
background removal (`vnyx-gemini`, the v7 endpoint). Hermes never paints: the paid
strategies stay out of the chain, and the gate never adds one.

    bg-removal cloth-seg-ft#1 refused by the AI check: AI check — CUTOUT DEFECT — cloth-seg-ft cut-out: cloth broken (strap) [clear]
    bg-removal cloth-seg-ft-backup#1: AI check passed

Policy, `imagery.cutout.ai_check`: `enabled`, `model` (null = `llm.model_fast`),
`on_unavailable` (`accept`: the pixel checks decide when the judge cannot answer;
`refuse`), `max_concurrent` (judge calls in flight per process). Off for one call
with `ai_check: false` on the request. The response carries
`ai_check: {ran, enabled, flagged, model, verdicts: [{strategy, ok, defects, noted,
summary}]}`; `/v1/imagery/cutout` sets `X-Cutout-AI-Check: pass|flagged|skipped`.

Cost: one flash call per accepted candidate — one for a good photo, up to four for
one every model gets wrong. The judge needs `GEMINI_API_KEY`, which the rules
already use. No image-generation key.

**In vnyx-api.** A new provider value, `hermes`, in the three tenant columns and
the editor's Provider list. Intake — the analyze worker, the decision flow, the
late cut-out, the editor's "Change background" and the readiness matte — goes
through `services/hermes-cutout.ts`: `/healthz` probe cached 30 s, a 60 s breaker
opened by a connection error / timeout / 5xx (never by `ok:false` or "busy"), an
in-process limiter of `HERMES_BG_MAX_INFLIGHT` (4, matched to `max_concurrent`)
with a 240 s queue wait, and the legacy chain `HERMES_BG_FALLBACK`
(`vnyx-gemini,vnyxv2`) behind it. The agent's guarded re-matte
(`removeBackgroundKeepingBetter`) stays strict and never falls back. The worker
sends `origin` and `garment` like the matte step does. The editor's response
carries `providerUsed`, `fellBack`, `fallbackReason` and `aiCheck`; the UI toasts
them.

**Rollout.** Deploy Hermes, confirm `/healthz` from the API host, deploy vnyx-api,
then switch one tenant's `web` column to Hermes in Product settings → Image
generation and watch `[hermes-cutout]` and `[bg-removal]` log lines for a day
before widening.

**Headwear keeps its display head (3 Nov 2026, TST-000019, the user's call).** A cap
is photographed on a mannequin head and the head stays in the picture; the garment
parsers cut the cap alone and left a small shape floating in the frame. `headwear`
is now a third `object.families` entry (caps, hats, beanies, visors, berets …;
"cap sleeve" is not a cap): the v3 parser runs first like bags, then IS-Net as an
object, judged by the object checks, and the AI check is told the head, face and
neck are part of the product (`cutout_integrity` `headwear`: a background finding
naming them holds nothing, before and after storage). Measured on TST-000019's
front: v3 kept the cap and the whole head, 10 s, AI check passed. Nothing to
change in vnyx-api: the category hint already names the family.

---

## What this does NOT cover

`BG_REMOVAL_RETIRED` changes which segmenter runs. It does not re-matte the
14,696 products that already have a cut-out from the old provider — those keep
whatever they have until something asks for a new one.
