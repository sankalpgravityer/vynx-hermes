# Readability — `POST /v1/readability`

Answers one question about one photograph: **can the text in it be read?** And when
it cannot, says what to change.

Built for the capture screen. It fires at shutter press on a warehouse phone, so
the whole answer — upload, verdict, retake decision — has to land inside about
1.5 seconds. That budget is what shapes every choice below.

No model call. PP-OCRv6 text detection and recognition through RapidOCR's ONNX
build, plus pixel statistics to explain a failure. A vision-model call is 2–6s and
would blow the budget on its own.

---

## Using it

`multipart/form-data`, not base64 in JSON, and not a URL. Base64 inflates the
payload by a third for nothing, and upload is the largest single item in the
budget on a mobile connection. A URL would put a second network hop inside a
request with no room for one.

```bash
curl -X POST http://localhost:8000/v1/readability \
  -F "image=@care-label.jpg"
```

```json
{
  "readable": false,
  "message": "Only part of the label is readable. Move closer so the whole label fills the frame. Also — hold the camera still and let it focus.",
  "reasons": ["not_enough_text", "out_of_focus"],
  "confidence": 0.9612,
  "min_confidence": 0.9249,
  "line_count": 2,
  "char_count": 19,
  "detected_count": 3,
  "text": "COTTON 100%\nSIZE M / EU 40",
  "lines": [
    {"text": "COTTON 100%", "confidence": 0.9846, "kept": true, "box": [56, 62, 402, 55]}
  ],
  "frame": {
    "width": 2400, "height": 1800,
    "working_width": 800, "working_height": 600,
    "sharpness": 88.4, "brightness": 243.7, "contrast": 15.1,
    "clipped_fraction": 0.0051, "dark_fraction": 0.0001
  },
  "ocr_skipped": false,
  "decode_ms": 34, "ocr_ms": 680, "duration_ms": 739
}
```

`readable` and `message` are the whole contract for a simple client: keep the
frame, or show the message and ask for another. Everything else is the evidence
behind that — including `text`, returned because a caller that has already paid
the upload usually wants it, and fetching it separately would cost another
budget's worth of time.

### The two request knobs

| Form field  | Default | What it is |
|-------------|---------|------------|
| `min_lines` | 1       | How many legible lines this frame must contain |
| `min_chars` | 3       | How many legible characters this frame must contain |

These are floors on how much text must come back legible. They catch the failure
confidence alone gets wrong (see "Why coverage is part of the decision"), and
they are the knob to raise when you know what you are photographing — a full care
label has six lines and `min_lines: 4` is reasonable for one.

**But a floor above what the label actually carries rejects a good photograph,
and that is the mistake to avoid.** An adidas neck label reads `adidas®` and `S`
and nothing else. Photographed perfectly, filling the frame, both lines at >0.92
confidence, `min_lines=3` returns `readable: false` — correctly, by the rule it
was given, and uselessly, because there is no fourth line to find and no retake
that helps.

So: **leave them at the defaults unless you know the capture type**, and set them
per capture type rather than globally. If your capture screen photographs both
neck tags and care labels, it cannot use one floor for both.

When the floor is the *only* thing unmet and the text found came back large and
sharp, the message says so rather than telling the operator to move closer:

```json
{ "readable": false,
  "reasons": ["not_enough_text"],
  "message": "Found 2 legible line(s) where 6 were required — but what is in frame read clearly at 96%, so this label may simply carry less text. Lower min_lines/min_chars for this capture type if that is expected.",
  "text_height_px": 149 }
```

`text_height_px` is the median height of the legible lines in
working-resolution pixels, and it is what separates those two cases — see
`resolved_text_min_height_px` in the policy. Detected box height bottoms out at
13px on this detector, so 13 means "as small as it gets" while the adidas mark
above measured 149.

### Failure codes

`reasons` carries a list, not one value: a photograph taken in a dark stockroom
at arm's length is both underexposed *and* too far away, and reporting only the
first sends the operator to fix the wrong thing.

**Verdicts** — one of these is always the primary cause:

| Code | Meaning |
|------|---------|
| `blank_frame` | Nothing in frame at all. Settled without running OCR. |
| `no_text_found` | OCR ran, detection found nothing. |
| `not_enough_text` | Found text, but less than the caller said to expect. |
| `low_confidence` | Enough text, but the recognizer is not sure of it. |
| `partially_legible` | Some lines read cleanly, too many others did not. |
| `decode_failed` | The upload was not a decodable image. |

**Contributing causes** — appended to explain a verdict, never sufficient alone:
`out_of_focus`, `too_dark`, `overexposed`, `glare`, `washed_out`.

---

## How it decides

**Confidence and coverage decide. Pixel statistics explain.**

That split is the design, and it is measured rather than stylistic. Both halves
are counter-intuitive enough to be worth the space:

### A pixel statistic cannot gate

Across the degradation harness, the readable and unreadable ranges of *every*
pixel statistic overlap:

| Statistic | Readable range | Unreadable range |
|-----------|----------------|------------------|
| Laplacian variance | 1.7 – 837.6 | 0.1 – 103.6 |
| Contrast | 2.1 – 17.0 | 7.2 – 11.3 |
| Brightness | 28.8 – 253.5 | 243.4 |
| **Mean OCR confidence** | **0.973 – 0.994** | **0.000 – 0.783** |

A defocused label at Laplacian variance **1.7** read perfectly at 0.99
confidence, while a motion-blurred one at **103.6** was unreadable. The measure
tracks how much fine detail a frame holds, and a blurred photograph of large
clean type holds little detail and plenty of legible text.

Brightness is worse. A frame at mean luma **28.8** — near-black to the eye —
read at **0.994**, because the recognizer normalises contrast per detected line.
Glare covering 98% of the frame in clipped pixels still read at 0.978.

Gating on any of these fails photographs OCR handles fine. So they are read
*only* once the verdict is already no, to turn "not readable" into "move into
better light". `test_pixel_causes_never_reject_a_readable_frame` locks this in.

### Why coverage is part of the decision

Confidence alone is not enough either, and this is the subtler half.

When text is too small for the frame, detection finds a **few fragments** and the
recognizer is confident about exactly those. Measured at 0.7% cap height and
1024px working resolution: **3 of 6 lines at 0.97 mean confidence, with 63% of
the characters wrong.** Mean confidence passes that frame.

The line and character floors are what catch it — which is why `min_lines` /
`min_chars` are part of the decision and not decoration, and why a client should
set them to what it actually expects to see.

A related result about detected box height: **it cannot decide, but it can
explain.** It saturates at ~13px because of the detector's vertical padding, so
it reads the same for text at 1.2% and 0.7% of frame height — useless as a "text
too small" gate. What that saturation *does* tell you is the opposite direction:
height comfortably above 13px means the text was properly resolved, so a low line
count is the label carrying less text rather than lines being missed. That is the
only thing `text_height_px` and `resolved_text_min_height_px` are used for, and
they change the wording of a verdict, never the verdict.

### The one pre-OCR gate

An empty frame is the single case pixel statistics *do* settle. A covered lens, a
blown-out frame, a grey wall and plain garment fabric all measured contrast
**≤ 1.4**, against **2.1** for the least contrasty readable frame in the harness.
The gate sits at 1.5 and saves the ~900ms OCR spends confirming there is nothing
there.

---

## Two real captures, and which verdict was wrong

Both of these came back `readable: false` from the live service. Only one of them
was a defect, and the pair is worth keeping because they look identical from the
outside. Both are replayed as tests in `tests/test_readability.py`.

**An adidas neck label, 3000×4000, filling the frame.** OCR returned `adidas®` at
0.9996 and `S` at 0.9277 — which is all the text on that label. Mean 0.9637,
2 lines, 8 characters, sharpness 839.8. Against the defaults this is
`readable: true`, and the only reason it failed was a request carrying
`min_lines=3`, copied from an example that used to ship that value. **The
endpoint was right and the documented default was wrong**; the floor is now
disabled in the Postman collection and absent from every curl example.

**A motion-blurred H&M jacket label, 3000×4000.** One fragment detected out of an
entire woven label, recognised as `"0"` at 0.4164, discarded by the 0.80 line
floor. Sharpness 65.3. Verdict: `low_confidence` + `out_of_focus`, "The text is
there but too degraded to read reliably." **That verdict is correct** — the label
is not legible to a person either, and `out_of_focus` names the actual cause.
Note it lands on `low_confidence` and not `no_text_found`: something *was*
detected, so the operator is aimed at the right thing and only the exposure of
the shutter was wrong. That distinction is why the two codes exist separately.

The lesson from the pair: when this endpoint is wrong, suspect the floor before
the OCR. `confidence`, `line_count` and `detected_count` in the response are
enough to tell which, without the photograph.

## Latency

Detection dominates, and its cost scales with the pixel count it is handed.
Measured on an i7-8565U (4 cores, 15W — a deliberately pessimistic floor):

| Working px | Resize | OCR median | Total |
|------------|--------|-----------|-------|
| 512 | 73ms | 618ms | ~690ms |
| 640 | 67ms | 784ms | ~850ms |
| **800** | **34ms** | **680–944ms** | **~740–980ms** |
| 960 | 151ms | 1393ms | over budget |
| 1280 | 207ms | 2244ms | over budget |

`working_px: 800` is the policy default. Not 640, because 640 is where accuracy
starts to go: text at 0.9% of frame height came back with **52% of characters
wrong at 640px and perfect at 800px**.

### Accuracy against text size

Character error rate, by cap height as a fraction of frame height:

| Cap height | 640px | 800px | 1024px | 1280px |
|------------|-------|-------|--------|--------|
| 4.0% – 1.2% | 0.00 | 0.00 | 0.00 | 0.00 |
| 0.9% | **0.52** | 0.00 | 0.00 | 0.00 |
| 0.7% | 0.99 | 1.00 | 0.63 | 1.00 |

Below about 0.7% nothing recovers the text at any working resolution. That is a
"move closer" instruction, not a tuning problem.

### Two things that are not optional

**Warm up at startup.** ONNX Runtime builds its execution graph on the first
inference — 611ms measured, against ~150ms of steady-state overhead. Left to
happen lazily, that lands on whichever operator presses the shutter first. Done
in the FastAPI `lifespan` handler; non-fatal, so a deployment without the wheel
still boots.

**Resize on the device before uploading.** The largest item in the budget is the
one the server does not control. A raw 12MP JPEG is 3–5MB and takes 6+ seconds on
4G. Send **1280–1600px on the long edge at quality 80** (~200KB). Do not go below
~1024px — that destroys the legibility being measured and makes the thresholds
meaningless. Uploads past `max_upload_mb` (12) are refused with a 413 that says
this.

JPEG compression itself is not worth worrying about: quality 12 still read at
0.988 with no character errors.

---

## Deployment notes

### The `onnxruntime` pin is not incidental

`requirements.txt` pins **onnxruntime 1.20.1**. Newer releases fail to load on
Windows 10 carrying only the VC++ 2015-2019 redistributable (14.29):

```
ImportError: DLL load failed while importing onnxruntime_pybind11_state:
A dynamic link library (DLL) initialization routine failed.
```

It is raised by `onnxruntime.dll` itself, before Python is involved — `ctypes.WinDLL`
on the bare DLL reproduces it. Builds after 1.20.1 want the **2015-2022**
redistributable (14.40+). 1.20.1 is the newest release that loads against 14.29
*and* the first with cp313 wheels, so it is the one version covering both.

Linux is unaffected; any 1.2x works. Raise the pin once every machine running
Hermes has the 2022 redistributable. Accuracy is unchanged either way — same ONNX
models, same operators.

### Sizing the box

Memory is a non-issue: ~500MB–1GB RSS including Python, Pillow and ONNX Runtime,
against a 27MB wheel that bundles all three models. Latency is **core-bound**, so
`nproc` is the number that matters, not `free -h`. Check it before assuming the
budget holds:

```bash
nproc
lscpu | grep -E "Model name|Core|MHz"
```

Rough expectation at `working_px: 800`: 2 vCPU is tight and wants `working_px`
lowered, 4 vCPU is comfortable, 8 vCPU has room to raise it. **Re-run the harness
if you change it** — the accuracy table above is what you are trading against.

Inference is serialised behind a lock. Two reasons: RapidOCR's detector assigns
`self.preprocess_op` per call and is not re-entrant, and even with a thread-safe
engine, two CPU-bound inferences in parallel on a small box make each other
slower — a queue of two has one request hit the deadline instead of both missing
it. Scale with processes, not threads.

### A note on `limit_type`

`app/imaging/legibility.py` pins RapidOCR to `Det.limit_type: min` with a tiny
`limit_side_len`, and resizes the frame itself. This is load-bearing.
`limit_type: max` does not do what it reads like — `TextDetector.get_preprocess`
**discards** the configured `limit_side_len` on that path and substitutes 960,
1500 or 2000 chosen from the image's own long edge. Setting `max` with a value
silently leaves detection resolution, and therefore latency, under the control of
whatever the phone uploaded: measured at 2.2s for a 1280px frame that should have
taken 0.9s. Only `min` honours the number.

`test_working_resolution_is_actually_applied` guards it.

---

## Where the numbers came from

Every threshold in `config/policy.yaml` under `readability:` came from a harness
that renders a synthetic care label, applies one realistic phone-camera failure at
a time — motion blur, defocus, underexposure, sensor noise, low contrast, glare,
JPEG compression, and text size sweeps — and records both what OCR reported and
what the pixel statistics reported, scored against ground truth by character
error rate.

Two things to know before re-tuning:

- **Synthetic labels are optimistic.** Clean type on a flat background is the easy
  case. Calibrate against a few hundred of your own tag photographs before
  trusting the thresholds in production; the *shape* of the rules transfers, the
  exact numbers should be re-fit.
- **Accuracy transfers across platforms, latency does not.** Same ONNX models and
  same runtime means identical confidence scores and identical text on Windows and
  Linux, so threshold work done on a laptop is valid. The timings are not —
  re-measure on the target box.

## A stronger design, if the round trip is the problem

A network round trip inside 1.5s is fine on wifi and unreliable on a weak mobile
connection. Since this fires at shutter press, consider two stages: an on-device
check for instant feedback, this endpoint for the authoritative record.

On-device, Google ML Kit Text Recognition v2 (Android + iOS, free, offline,
50–200ms) and Apple's `VNRecognizeTextRequest` both return confidence scores
locally with no network. Cheaper still, a Laplacian variance and brightness check
on the device rejects the obviously hopeless frames in ~20ms before anything is
uploaded — with the caveat established above, that those statistics are only
trustworthy for rejecting *frames with no text in them*, never for rejecting text
OCR could have read.
