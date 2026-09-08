"""Can the text in this photograph be read?

Answers one question about one uploaded frame, at shutter press, inside ~1.5s, and
says what to fix when the answer is no. No model call: PP-OCRv6 detection and
recognition through RapidOCR's ONNX build, plus pixel statistics to explain a
failure.


WHAT DECIDES, AND WHAT ONLY EXPLAINS

Confidence and coverage decide. Pixel statistics explain. That split is measured,
not stylistic, and both halves of it are counter-intuitive enough to be worth
stating:

  A pixel statistic cannot GATE. Across the degradation harness the readable and
  unreadable ranges of every one of them overlap. A defocused label at Laplacian
  variance 1.7 read perfectly at 0.99 confidence while a motion-blurred one at
  103.6 was unreadable — the measure tracks how much fine detail the frame holds,
  and a blurred photograph of large clean type holds little detail and plenty of
  legible text. Brightness is worse: a frame at mean luma 28.8, near-black to the
  eye, read at 0.994 because the recognizer normalises contrast per detected line.
  Rejecting on any of these fails photographs OCR handles fine.

  Confidence cannot decide alone either. When the text is too small, detection
  finds a few fragments and the recognizer is confident about exactly those — 3 of
  a label's 6 lines at 0.97 mean confidence with 63% of the characters wrong. Mean
  confidence passes that frame. A line and character floor is what catches it,
  which is why `min_lines` / `min_chars` are part of the decision and not
  decoration.

So the failure branches are ordered: what OCR found, then how sure it was, and
only then the pixel statistics — reached only once the verdict is already no, to
turn "not readable" into "move into better light".


THE TWO THINGS THAT COST TIME

Detection dominates, and its cost scales with the pixel count it is handed. So
the frame is downscaled to `working_px` first, and RapidOCR is pinned so it does
not resize again — see `_new_engine` for why that pinning is not optional.

Decoding a full-resolution phone JPEG is the other line item, and `Image.draft`
removes most of it: libjpeg can scale during the DCT pass, so a 3024px upload is
decoded straight to roughly the working size instead of being decoded in full and
then resampled.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Any

import numpy as np
from PIL import Image, ImageOps

from app.models import (
    FrameStats, LegibilityReason, LegibilityVerdict, TextLine,
)

log = logging.getLogger("hermes.imaging.legibility")


class OcrUnavailable(RuntimeError):
    """RapidOCR or its runtime is not installed or would not load.

    Its own type because the endpoint answers it with a 503 and an install hint,
    which is a different thing from an unreadable photograph.
    """


# --------------------------------------------------------------------------- #
# The engine
#
# One process-wide instance behind a lock. Built lazily, warmed at startup.
# --------------------------------------------------------------------------- #

_engine: Any | None = None
_engine_error: str | None = None

# TWO locks, with two different jobs. One lock covering both would deadlock the
# moment inference needed to resolve the engine while holding it.
#
# Construction: so that two requests arriving before the warm-up finishes do not
# both build an engine and load three models.
_build_lock = threading.Lock()

# Inference, serialised for two reasons:
#
#   1. RapidOCR's detector is not re-entrant. `TextDetector.__call__` assigns
#      `self.preprocess_op` on every call (ch_ppocr_det/main.py), so two
#      concurrent requests race over one instance's preprocessing config. Benign
#      under our pinned configuration, where it recomputes the same value every
#      time — but only by accident, and an upgrade could change it.
#
#   2. Even with a thread-safe engine, serialising is the right call on the boxes
#      this runs on. Inference is CPU-bound and already multi-threaded internally;
#      two requests in parallel on 2-4 vCPUs make each other slower and both can
#      miss a 1.5s deadline, where a queue of two has one hit it and one wait.
_infer_lock = threading.Lock()


def _new_engine(cfg: dict[str, Any]) -> Any:
    """Build a RapidOCR pinned to a fixed detection resolution.

    THE PINNING IS LOAD-BEARING. RapidOCR's `limit_type: max` does not do what it
    reads like: `TextDetector.get_preprocess` DISCARDS the configured
    `limit_side_len` on that path and substitutes 960, 1500 or 2000 chosen from
    the image's own long edge. Setting `max` with a value therefore silently
    leaves detection resolution — and so the latency — controlled by whatever the
    phone uploaded. Measured at 2.2s for a 1280px frame that should have taken
    0.9s.

    Only `limit_type: min` honours the number, and it means "upscale the short
    side up to at least this". Pinned far below any real short edge, its resize
    becomes a no-op and the resolution we chose in `prepare` is the resolution
    that runs.
    """
    try:
        from rapidocr import RapidOCR
    except ImportError as exc:
        raise OcrUnavailable(
            f"RapidOCR is not installed ({exc}). "
            "pip install rapidocr onnxruntime"
        ) from exc

    params = {
        # See the docstring. Not `max`.
        "Det.limit_type": "min",
        "Det.limit_side_len": 32,
        # The 180-degree orientation classifier. Off: it is a whole extra model
        # pass, and a frame held upside down is a different problem from an
        # illegible one — we would rather report "no text found" on a genuinely
        # inverted photo than pay for the check on every upload.
        "Global.use_cls": False,
        # We have already sized and normalised the frame ourselves.
        "Global.use_preprocess_img": False,
        # Physical cores, not the 8 logical ones. Hyperthreading does not help
        # these convolutions and the contention measurably hurts.
        "EngineConfig.onnxruntime.intra_op_num_threads": int(
            cfg.get("intra_op_threads") or 4
        ),
        # Keep every line the recognizer produces. RapidOCR's own `text_score`
        # would DROP the weak ones before we see them, and a frame whose lines all
        # scored 0.4 has to be distinguishable from a frame with no text in it —
        # they are different messages to the operator. Thresholding is ours.
        "Global.text_score": 0.0,
    }
    try:
        return RapidOCR(params=params)
    except Exception as exc:  # noqa: BLE001 — onnxruntime raises bare OSError
        raise OcrUnavailable(
            f"RapidOCR would not start ({type(exc).__name__}: {exc}). "
            "On Windows this is usually onnxruntime failing to load: it needs the "
            "Microsoft Visual C++ 2015-2022 redistributable, and versions above "
            "1.20.1 need a newer one than some machines carry."
        ) from exc


def engine(cfg: dict[str, Any] | None = None) -> Any:
    """The shared engine, built on first use.

    A previous construction failure is remembered rather than retried: the causes
    are all static — a missing wheel, an unloadable DLL — and retrying per request
    would put a multi-second import behind every upload.
    """
    global _engine, _engine_error
    with _build_lock:
        if _engine is not None:
            return _engine
        if _engine_error is not None:
            raise OcrUnavailable(_engine_error)
        try:
            _engine = _new_engine(cfg or {})
        except OcrUnavailable as exc:
            _engine_error = str(exc)
            raise
        return _engine


def warm_up(cfg: dict[str, Any] | None = None) -> float | None:
    """Build the engine and run one throwaway inference. Returns seconds taken.

    Called at startup because the FIRST inference is not representative: ONNX
    Runtime builds its execution graph on it, which measured 611ms here against
    ~150ms of steady-state overhead. Left to happen lazily, that cost lands on
    whichever operator presses the shutter first, in the one place the whole
    design is a latency budget.

    Never raises. A deployment without the OCR wheel should still boot and serve
    the rest of Hermes; the endpoint is what reports the problem, and it does so
    per request with an actionable message.
    """
    started = time.perf_counter()
    try:
        eng = engine(cfg)
        with _infer_lock:
            eng(np.zeros((320, 320, 3), dtype=np.uint8))
    except OcrUnavailable as exc:
        log.warning("readability OCR unavailable: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("readability warm-up failed: %s", exc)
        return None
    took = time.perf_counter() - started
    log.info("readability OCR warmed up in %.0fms", took * 1000)
    return took


def available() -> bool:
    """Is OCR usable, without building it. For /healthz."""
    return _engine is not None or _engine_error is None


# --------------------------------------------------------------------------- #
# Decode and downscale
# --------------------------------------------------------------------------- #

def prepare(data: bytes, working_px: int) -> tuple[Image.Image, tuple[int, int]]:
    """Decode to RGB at roughly `working_px` on the long edge.

    Returns the working image and the ORIGINAL pixel dimensions, which are worth
    reporting back: an operator whose client is uploading 4032px frames is paying
    upload time for detail this discards.
    """
    try:
        img = Image.open(io.BytesIO(data))
        # Read off the header, BEFORE draft. `draft` rescales the image in place,
        # so after it `img.size` is the reduced size and the true upload
        # dimensions are gone — which made this report a 2400px frame as 1200px.
        original = img.size
        # Ask libjpeg to scale during decode. It only honours powers of two, so
        # this lands at or above the target and the resample below finishes the
        # job — but it means a 3024px upload is never fully materialised. Measured
        # at 34ms to decode a 2400px label this way. Silently a no-op for PNG and
        # everything else.
        img.draft("RGB", (working_px, working_px))
        img.load()
    except Exception as exc:  # noqa: BLE001 — any malformed upload lands here
        raise ValueError(
            f"Could not decode the image ({type(exc).__name__}). "
            "Send JPEG or PNG bytes as multipart/form-data."
        ) from exc

    # Phone photographs carry rotation in EXIF rather than in the pixels. Without
    # this a portrait frame reaches the detector on its side, and PP-OCR's
    # horizontal-text assumption turns a perfectly good label into no detections
    # at all.
    before = img.size
    img = ImageOps.exif_transpose(img)
    if img.size == (before[1], before[0]) and before[0] != before[1]:
        # The tag rotated by a quarter turn, so the header dimensions we kept are
        # the other way round from what the operator actually photographed.
        original = (original[1], original[0])

    img = img.convert("RGB")
    if max(img.size) > working_px:
        img.thumbnail((working_px, working_px), Image.Resampling.LANCZOS)
    return img, original


def _laplacian_variance(gray: np.ndarray) -> float:
    """Variance of the 4-neighbour Laplacian. The standard focus measure.

    Written out rather than taken from cv2 because Hermes' imaging layer is
    Pillow + numpy throughout, and opencv is here only as a RapidOCR dependency —
    depending on it directly would make an incidental transitive package
    load-bearing.
    """
    # float32 rather than float64: same answer to the precision that matters at
    # this scale, half the memory traffic.
    g = gray.astype(np.float32)
    lap = (
        -4.0 * g[1:-1, 1:-1]
        + g[:-2, 1:-1] + g[2:, 1:-1]
        + g[1:-1, :-2] + g[1:-1, 2:]
    )
    return float(lap.var())


def frame_stats(img: Image.Image, original: tuple[int, int]) -> FrameStats:
    """Pixel statistics at the working resolution.

    Measured on the WORKING image, not the original, because that is the image OCR
    judged. Sharpness in particular is not comparable across resolutions — the
    same photograph scores differently at 800px and 3024px — so measuring it here
    keeps it consistent with the verdict it explains.
    """
    gray = np.asarray(img.convert("L"))
    total = gray.size or 1
    return FrameStats(
        width=original[0],
        height=original[1],
        working_width=img.size[0],
        working_height=img.size[1],
        sharpness=round(_laplacian_variance(gray), 1),
        brightness=round(float(gray.mean()), 1),
        contrast=round(float(gray.std()), 1),
        clipped_fraction=round(float((gray >= 250).sum()) / total, 4),
        dark_fraction=round(float((gray <= 8).sum()) / total, 4),
    )


# --------------------------------------------------------------------------- #
# Recognition
# --------------------------------------------------------------------------- #

def read_lines(img: Image.Image, cfg: dict[str, Any]) -> tuple[list[TextLine], int]:
    """Run OCR. Returns the lines and the inference time in milliseconds.

    Every detected line comes back, weak ones included and flagged — the caller's
    thresholds are applied in `judge`, not here, so a frame of unreadable text
    stays distinguishable from a frame with no text.
    """
    keep_above = float(cfg.get("line_score_min") or 0.80)
    arr = np.asarray(img)

    # Resolved BEFORE taking the inference lock. Doing it inside would mean
    # `engine()` reaching for the build lock while this one is held — the two
    # locks exist precisely so that ordering is not up for debate.
    eng = engine(cfg)

    started = time.perf_counter()
    with _infer_lock:
        result = eng(arr)
    ocr_ms = int((time.perf_counter() - started) * 1000)

    # All three come back None together when detection finds nothing — not empty
    # sequences, which is why this is a None check and not a truthiness one.
    texts = list(result.txts or ())
    scores = list(result.scores or ())
    boxes = (
        np.asarray(result.boxes)
        if getattr(result, "boxes", None) is not None
        else np.empty((0, 4, 2))
    )

    lines: list[TextLine] = []
    for i, text in enumerate(texts):
        score = float(scores[i]) if i < len(scores) else 0.0
        box: list[int] = []
        if i < len(boxes):
            pts = np.asarray(boxes[i], dtype=np.float32)
            x0, y0 = pts.min(axis=0)
            x1, y1 = pts.max(axis=0)
            box = [int(x0), int(y0), int(round(x1 - x0)), int(round(y1 - y0))]
        lines.append(TextLine(
            text=text,
            confidence=round(score, 4),
            kept=score >= keep_above,
            box=box,
        ))
    return lines, ocr_ms


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def _contributing(
    stats: FrameStats, cfg: dict[str, Any], blank: bool = False
) -> list[LegibilityReason]:
    """Pixel-level causes worth mentioning, for a frame that has ALREADY failed.

    Never called on a readable frame, and never able to cause a rejection. See the
    module docstring for why that restriction exists: every one of these
    thresholds has readable photographs on the wrong side of it.

    `blank` suppresses the two causes that are tautologies on an empty frame. A
    uniform frame has no edges and no contrast BY DEFINITION, so reporting soft
    focus and low contrast on one is restating the verdict — and telling someone
    to focus on a flat grey frame is advice they cannot act on. Exposure is kept,
    because "it is black" and "it is blown out" genuinely explain emptiness.
    """
    ex = cfg.get("explain") or {}
    out: list[LegibilityReason] = []
    if (not blank
            and stats.sharpness < float(ex.get("soft_focus_max_sharpness") or 120.0)):
        out.append(LegibilityReason.OUT_OF_FOCUS)
    if stats.brightness < float(ex.get("dark_max_brightness") or 45.0):
        out.append(LegibilityReason.TOO_DARK)
    elif stats.brightness > float(ex.get("bright_min_brightness") or 248.0):
        out.append(LegibilityReason.OVEREXPOSED)
    if stats.clipped_fraction > float(ex.get("glare_min_clipped_fraction") or 0.25):
        out.append(LegibilityReason.GLARE)
    if (not blank
            and stats.contrast < float(ex.get("flat_max_contrast") or 4.0)):
        out.append(LegibilityReason.WASHED_OUT)
    return out


# The lead sentence for whichever reason is primary. Imperative — the operator is
# holding a phone and needs to know what to change, not what a statistic did.
_LEAD: dict[LegibilityReason, str] = {
    LegibilityReason.BLANK_FRAME:
        "Nothing in frame to read — check the lens is not covered and point the "
        "camera at the label.",
    LegibilityReason.NO_TEXT_FOUND:
        "No text found. Fill more of the frame with the label and hold the camera "
        "square to it.",
    LegibilityReason.NOT_ENOUGH_TEXT:
        "Only part of the label is readable. Move closer so the whole label fills "
        "the frame.",
    LegibilityReason.LOW_CONFIDENCE:
        "The text is there but too degraded to read reliably. Retake it closer and "
        "steadier.",
    LegibilityReason.PARTIALLY_LEGIBLE:
        "Some lines read and others did not. Hold the camera square to the label "
        "so it is all in focus.",
    LegibilityReason.DECODE_FAILED:
        "The upload could not be read as an image.",
    # Normally a contributing cause, but it gets a lead sentence too — see
    # `_primary_of` for the one case where darkness IS the whole story.
    LegibilityReason.TOO_DARK:
        "Too dark to read anything. Move into better light or turn on the torch.",
}

# The alternative lead for NOT_ENOUGH_TEXT, used when the text that WAS found came
# back large and sharp.
#
# `_LEAD`'s sentence tells the operator to move closer, which is right when the
# floor was missed because the label is far away and half of it went undetected.
# It is actively wrong in the other case: an adidas neck label photographed at
# arm's length reads "adidas" and "S" at 0.99 confidence with the label already
# filling the frame, and "move closer" is advice that cannot help because nothing
# is missing. The frame is fine; the CALLER'S EXPECTATION is what does not fit,
# and the message has to say so or the operator retakes a good photograph forever.
_FEWER_LINES_BUT_CLEAR = (
    "Found {found} legible line(s) where {want} were required — but what is in "
    "frame read clearly at {conf:.0%}, so this label may simply carry less text. "
    "Lower min_lines/min_chars for this capture type if that is expected."
)

# The clause form, appended after the lead to explain it.
_CLAUSE: dict[LegibilityReason, str] = {
    LegibilityReason.OUT_OF_FOCUS: "hold the camera still and let it focus",
    LegibilityReason.TOO_DARK: "move into better light",
    LegibilityReason.OVEREXPOSED: "move out of direct light",
    LegibilityReason.GLARE: "change the angle to kill the reflection",
    LegibilityReason.WASHED_OUT: "the label is washed out against its background",
}

# Which reasons are verdicts in their own right, as opposed to contributing
# causes. Order is the order they are preferred in.
_PRIMARY = (
    LegibilityReason.BLANK_FRAME,
    LegibilityReason.NO_TEXT_FOUND,
    LegibilityReason.NOT_ENOUGH_TEXT,
    LegibilityReason.PARTIALLY_LEGIBLE,
    LegibilityReason.LOW_CONFIDENCE,
    LegibilityReason.DECODE_FAILED,
)


def _primary_of(reasons: list[LegibilityReason]) -> LegibilityReason:
    """Which reason leads the sentence.

    Almost always the first verdict in `_PRIMARY` order, with one exception: a
    frame that is BOTH empty and dark is empty BECAUSE it is dark, and leading
    with "check the lens is not covered" sends the operator to look at hardware
    when the fix is to turn a light on.
    """
    if (LegibilityReason.BLANK_FRAME in reasons
            and LegibilityReason.TOO_DARK in reasons):
        return LegibilityReason.TOO_DARK
    return next((r for r in reasons if r in _PRIMARY), reasons[0])


def _join(parts: list[str]) -> str:
    """"a", "a and b", "a, b, and c" — not the "a, and b, and c" that a plain
    join produces, which is what three simultaneous causes read like."""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _compose(
    reasons: list[LegibilityReason], lead_override: str | None = None
) -> str:
    """One sentence: the primary failure, plus the pixel causes that explain it.

    `lead_override` replaces the primary sentence where the reason code alone is
    too coarse to give correct advice — currently only NOT_ENOUGH_TEXT, which
    means two opposite things depending on whether the text found was resolved.
    """
    if not reasons:
        return "Text is legible."
    primary = _primary_of(reasons)
    message = lead_override or _LEAD.get(primary, "Not readable.")
    extra = [
        _CLAUSE[r] for r in reasons
        if r is not primary and r in _CLAUSE
    ]
    if extra:
        # Appended to the lead rather than listed separately: the operator gets
        # one instruction, and "also" marks the difference between what went
        # wrong and what probably caused it.
        message = f"{message} Also — {_join(extra)}."
    return message


def judge(
    lines: list[TextLine],
    stats: FrameStats,
    cfg: dict[str, Any],
    min_lines: int | None = None,
    min_chars: int | None = None,
) -> LegibilityVerdict:
    """Decide, and say why. Pure — no I/O, no OCR, so it is directly testable.

    `min_lines` / `min_chars` override the policy defaults per request, because
    how much text SHOULD be in frame is the caller's knowledge and not Hermes's: a
    care label has six lines, a size tag has two, and the same photograph is
    complete for one and half-missing for the other.
    """
    kept = [ln for ln in lines if ln.kept]
    scores = [ln.confidence for ln in kept]
    chars = sum(len(ln.text.strip()) for ln in kept)
    want_lines = int(min_lines if min_lines is not None else (cfg.get("min_lines") or 1))
    want_chars = int(min_chars if min_chars is not None else (cfg.get("min_chars") or 3))

    # Median rather than mean: one tall brand mark beside several small care lines
    # would drag a mean up and claim the small text was well resolved.
    heights = sorted(ln.box[3] for ln in kept if len(ln.box) == 4)
    text_height = heights[len(heights) // 2] if heights else 0

    reasons: list[LegibilityReason] = []
    if not lines:
        reasons.append(LegibilityReason.NO_TEXT_FOUND)
    elif not kept:
        # Detected, none of it legible. A different message from "no text": the
        # operator is pointing at the right thing and only the quality is wrong.
        reasons.append(LegibilityReason.LOW_CONFIDENCE)
    else:
        if len(kept) < want_lines or chars < want_chars:
            reasons.append(LegibilityReason.NOT_ENOUGH_TEXT)
        weak_fraction = 1.0 - (len(kept) / len(lines))
        if weak_fraction > float(cfg.get("max_weak_line_fraction") or 0.5):
            reasons.append(LegibilityReason.PARTIALLY_LEGIBLE)
        mean_score = sum(scores) / len(scores)
        if mean_score < float(cfg.get("mean_score_min") or 0.90):
            reasons.append(LegibilityReason.LOW_CONFIDENCE)

    # Only now, and only on a failure — see `_contributing`.
    if reasons:
        reasons += _contributing(stats, cfg)

    # Was the shortfall a bad photograph, or a floor set higher than this label
    # carries? Only worth asking when the coverage floor is the ONLY thing wrong:
    # a frame that also came back soft or low-confidence is a bad photograph
    # whatever its line count.
    lead: str | None = None
    resolved_min = int(cfg.get("resolved_text_min_height_px") or 20)
    if (reasons == [LegibilityReason.NOT_ENOUGH_TEXT]
            and text_height >= resolved_min
            and scores
            and sum(scores) / len(scores) >= float(cfg.get("mean_score_min") or 0.90)):
        lead = _FEWER_LINES_BUT_CLEAR.format(
            found=len(kept),
            want=max(want_lines, 1) if len(kept) < want_lines else want_chars,
            conf=sum(scores) / len(scores),
        )

    return LegibilityVerdict(
        readable=not reasons,
        message=_compose(reasons, lead_override=lead),
        reasons=reasons,
        confidence=round(sum(scores) / len(scores), 4) if scores else 0.0,
        min_confidence=round(min(scores), 4) if scores else 0.0,
        line_count=len(kept),
        char_count=chars,
        detected_count=len(lines),
        text_height_px=text_height,
        text="\n".join(ln.text for ln in kept),
        lines=lines,
        frame=stats,
    )


def assess(
    data: bytes,
    cfg: dict[str, Any],
    min_lines: int | None = None,
    min_chars: int | None = None,
) -> LegibilityVerdict:
    """The entry point: bytes in, verdict out.

    Raises ValueError for an undecodable upload and OcrUnavailable when the engine
    is missing; everything else is a verdict, including a frame with nothing in it.
    """
    started = time.perf_counter()
    working_px = int(cfg.get("working_px") or 800)

    t0 = time.perf_counter()
    img, original = prepare(data, working_px)
    decode_ms = int((time.perf_counter() - t0) * 1000)
    stats = frame_stats(img, original)

    # The one pre-OCR gate, and the only place a pixel statistic decides anything.
    #
    # An empty frame is the single case these numbers settle: a covered lens, a
    # blown-out frame, a grey wall and plain garment fabric all measured contrast
    # <= 1.4, against 2.1 for the least contrasty READABLE frame in the harness.
    # Worth gating because OCR spends its full ~900ms confirming there is nothing
    # there, and this is the one answer we can give without it.
    blank_max = float(cfg.get("blank_frame_max_contrast") or 1.5)
    if stats.contrast <= blank_max:
        reasons = [LegibilityReason.BLANK_FRAME] + _contributing(
            stats, cfg, blank=True
        )
        return LegibilityVerdict(
            readable=False,
            message=_compose(reasons),
            reasons=reasons,
            frame=stats,
            ocr_skipped=True,
            decode_ms=decode_ms,
            duration_ms=int((time.perf_counter() - started) * 1000),
        )

    lines, ocr_ms = read_lines(img, cfg)
    verdict = judge(lines, stats, cfg, min_lines=min_lines, min_chars=min_chars)
    verdict.decode_ms = decode_ms
    verdict.ocr_ms = ocr_ms
    verdict.duration_ms = int((time.perf_counter() - started) * 1000)
    return verdict
