"""Readability endpoint and its judgement.

Split deliberately in two. `judge` is pure — lines and frame statistics in, a
verdict out — so the decision rules are tested directly against synthesised
inputs, with no OCR and no images. Only the handful of tests that have to prove
the whole path works actually run inference, because each one costs the better
part of a second.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from app.config import policy
from app.imaging import legibility
from app.models import FrameStats, LegibilityReason, TextLine


@pytest.fixture(scope="module")
def cfg() -> dict:
    return policy().get("readability") or {}


# --------------------------------------------------------------------------- #
# Fixtures: synthetic care labels
# --------------------------------------------------------------------------- #

LABEL_LINES = ["COTTON 100%", "MADE IN PORTUGAL", "SIZE M / EU 40",
               "MACHINE WASH 30C", "DO NOT BLEACH"]


def label_jpeg(long_edge: int = 2000, text_frac: float = 0.030,
               quality: int = 85) -> bytes:
    """A care label photograph. `text_frac` is cap height as a fraction of frame
    height — 0.030 is a label shot at arm's length, which is the normal case."""
    w, h = long_edge, int(long_edge * 0.75)
    pt = max(4, int(h * text_frac))
    img = Image.new("RGB", (w, h), (246, 245, 241))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", pt)
    except OSError:  # no Arial on CI
        font = ImageFont.load_default()
    for i, line in enumerate(LABEL_LINES):
        draw.text((int(w * 0.07), int(h * 0.10) + i * int(pt * 2.0)), line,
                  fill=(35, 33, 32), font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def flat_jpeg(value: int, size: tuple[int, int] = (1200, 900)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (value, value, value)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


def line(text: str, score: float) -> TextLine:
    return TextLine(text=text, confidence=score, kept=score >= 0.80,
                    box=[0, 0, 100, 20])


def clean_frame() -> FrameStats:
    """Frame statistics for a well-exposed sharp photograph, so a `judge` test
    exercises the decision rules without any pixel cause firing."""
    return FrameStats(width=2000, height=1500, working_width=800,
                      working_height=600, sharpness=800.0, brightness=180.0,
                      contrast=15.0, clipped_fraction=0.01, dark_fraction=0.0)


# --------------------------------------------------------------------------- #
# judge() — the decision rules, no OCR
# --------------------------------------------------------------------------- #

def test_confident_multiline_text_is_readable(cfg):
    lines = [line(t, 0.97) for t in LABEL_LINES]
    v = legibility.judge(lines, clean_frame(), cfg)
    assert v.readable
    assert v.reasons == []
    assert v.line_count == 5
    assert v.message == "Text is legible."
    assert "COTTON 100%" in v.text


def test_no_detections_reads_as_no_text_found(cfg):
    v = legibility.judge([], clean_frame(), cfg)
    assert not v.readable
    assert LegibilityReason.NO_TEXT_FOUND in v.reasons
    assert v.confidence == 0.0
    assert v.text == ""


def test_detected_but_all_weak_is_low_confidence_not_no_text(cfg):
    """The distinction the operator needs: pointing at the right thing badly is
    a different instruction from pointing at the wrong thing."""
    v = legibility.judge([line("C0TT0N", 0.31), line("l1lI", 0.22)],
                         clean_frame(), cfg)
    assert not v.readable
    assert LegibilityReason.LOW_CONFIDENCE in v.reasons
    assert LegibilityReason.NO_TEXT_FOUND not in v.reasons
    # Every line comes back even though none counted, so a caller tuning
    # thresholds can see what was rejected.
    assert len(v.lines) == 2
    assert v.line_count == 0


def test_high_confidence_fragments_fail_the_coverage_floor(cfg):
    """The case mean confidence alone gets wrong.

    Measured: text too small for the frame makes detection find a few fragments
    that the recognizer is then confident about — 3 of 6 lines at 0.97 with 63%
    of characters wrong. Confidence passes it; the line floor is what does not.
    """
    lines = [line("COTTON", 0.97)]
    v = legibility.judge(lines, clean_frame(), cfg, min_lines=4)
    assert not v.readable
    assert LegibilityReason.NOT_ENOUGH_TEXT in v.reasons
    # Confidence itself was never the problem, and the verdict should not claim
    # it was — that would send the operator to fix focus instead of distance.
    assert LegibilityReason.LOW_CONFIDENCE not in v.reasons
    assert v.confidence >= 0.90


def test_char_floor_catches_a_confident_single_glyph(cfg):
    v = legibility.judge([line("M", 0.99)], clean_frame(), cfg, min_chars=8)
    assert not v.readable
    assert LegibilityReason.NOT_ENOUGH_TEXT in v.reasons


def test_mostly_weak_lines_read_as_partially_legible(cfg):
    lines = [line("COTTON 100%", 0.96)] + [line("???", 0.3) for _ in range(4)]
    v = legibility.judge(lines, clean_frame(), cfg)
    assert not v.readable
    assert LegibilityReason.PARTIALLY_LEGIBLE in v.reasons
    assert v.detected_count == 5
    assert v.line_count == 1


def test_a_little_illegible_clutter_is_tolerated(cfg):
    """A fold, a seam or a barcode beside a clean label is normal, so the weak
    fraction is allowed to be non-zero."""
    lines = [line(t, 0.96) for t in LABEL_LINES] + [line("~~", 0.2)]
    v = legibility.judge(lines, clean_frame(), cfg)
    assert v.readable, v.message


def test_request_overrides_beat_policy_defaults(cfg):
    lines = [line("SIZE M", 0.98)]
    assert legibility.judge(lines, clean_frame(), cfg).readable
    assert not legibility.judge(lines, clean_frame(), cfg, min_lines=3).readable


# --------------------------------------------------------------------------- #
# Two real captures, replayed from their recorded OCR output
#
# Both came back `readable: false` from the live service and were reported as
# wrong. Only one of them was. Reconstructed from the responses rather than from
# the photographs so they run without fixtures and without inference.
# --------------------------------------------------------------------------- #

def adidas_neck_label() -> tuple[list[TextLine], FrameStats]:
    """A 3000x4000 phone photo of an adidas neck label, filling the frame.

    "adidas®" and "S" is ALL the text on that label, and both read at >0.92.
    """
    return (
        [
            TextLine(text="adidas®", confidence=0.9996, kept=True,
                     box=[172, 406, 246, 149]),
            TextLine(text="S", confidence=0.9277, kept=True,
                     box=[259, 507, 36, 40]),
        ],
        FrameStats(width=3000, height=4000, working_width=600,
                   working_height=800, sharpness=839.8, brightness=125.0,
                   contrast=63.3, clipped_fraction=0.0005, dark_fraction=0.018),
    )


def test_adidas_neck_label_is_readable_on_defaults(cfg):
    """THE REPORTED BUG, and it was in the example rather than the endpoint.

    Two legible lines and eight characters clear every policy default, so this
    frame is readable. It only failed because the request carried `min_lines=3`,
    which the shipped curl and Postman examples set — a floor a two-line neck
    label cannot meet.
    """
    lines, stats = adidas_neck_label()
    v = legibility.judge(lines, stats, cfg)
    assert v.readable, f"{v.message} (reasons={v.reasons})"
    assert v.text == "adidas®\nS"
    assert v.confidence == pytest.approx(0.9637, abs=1e-3)


def test_the_shipped_examples_do_not_reject_a_two_line_label(cfg):
    """Guards the fix at its source.

    The endpoint behaved correctly; the documented `min_lines` did not. Any
    example or collection default above 2 breaks ordinary neck and size tags,
    so the policy default has to stay low enough for one.
    """
    lines, stats = adidas_neck_label()
    assert int(cfg["min_lines"]) <= 2
    assert int(cfg["min_chars"]) <= 8
    assert legibility.judge(lines, stats, cfg).readable


def test_a_high_floor_on_clear_text_says_lower_the_floor(cfg):
    """When the caller's floor is the only thing unmet and the text read clearly,
    "move closer" is advice that cannot help — the label already fills the frame.
    """
    lines, stats = adidas_neck_label()
    v = legibility.judge(lines, stats, cfg, min_lines=6)
    assert not v.readable
    assert v.reasons == [LegibilityReason.NOT_ENOUGH_TEXT]
    assert "min_lines" in v.message
    assert "less text" in v.message
    assert "Move closer" not in v.message
    # The signal that earned the distinction.
    assert v.text_height_px >= int(cfg["resolved_text_min_height_px"])


def test_small_text_missing_its_floor_still_says_move_closer(cfg):
    """The other half of the same branch. Text at the detector's height floor is
    genuinely too far away, so the original instruction is the right one."""
    lines = [TextLine(text="COTTON", confidence=0.97, kept=True,
                      box=[10, 10, 60, 13])]
    v = legibility.judge(lines, clean_frame(), cfg, min_lines=4)
    assert not v.readable
    assert LegibilityReason.NOT_ENOUGH_TEXT in v.reasons
    assert "Move closer" in v.message
    assert "min_lines" not in v.message


def test_a_soft_frame_missing_its_floor_is_not_excused(cfg):
    """The lower-your-floor wording is only for a frame whose ONLY problem is the
    floor. A soft or low-confidence frame is a bad photograph whatever its line
    count, and must not be told its label simply carries less text."""
    lines, stats = adidas_neck_label()
    stats.sharpness = 40.0  # adds OUT_OF_FOCUS
    v = legibility.judge(lines, stats, cfg, min_lines=6)
    assert not v.readable
    assert "less text" not in v.message
    assert "Move closer" in v.message


def test_motion_blurred_hm_jacket_label_is_correctly_rejected(cfg):
    """The second reported capture — and this verdict was right.

    One fragment detected out of a whole woven label, recognised as "0" at 0.42.
    Nothing was legible, and `out_of_focus` names the cause: severe motion blur.
    """
    lines = [TextLine(text="0", confidence=0.4164, kept=False,
                      box=[256, 226, 219, 73])]
    stats = FrameStats(width=3000, height=4000, working_width=600,
                       working_height=800, sharpness=65.3, brightness=88.6,
                       contrast=52.9, clipped_fraction=0.0001,
                       dark_fraction=0.0587)
    v = legibility.judge(lines, stats, cfg)
    assert not v.readable
    assert LegibilityReason.LOW_CONFIDENCE in v.reasons
    assert LegibilityReason.OUT_OF_FOCUS in v.reasons
    # Detected but discarded, which is the distinction that makes the message
    # "too degraded" rather than "no text found".
    assert v.detected_count == 1
    assert v.line_count == 0
    assert v.text == ""


# --------------------------------------------------------------------------- #
# Pixel statistics explain, and only explain
# --------------------------------------------------------------------------- #

def test_pixel_causes_never_reject_a_readable_frame(cfg):
    """The load-bearing invariant of the whole design.

    A frame that is dark, soft, flat and glaring all at once still passes when the
    text reads — measured, a photograph at mean luma 28.8 recognised at 0.994.
    Any change that lets a pixel statistic gate will fail here.
    """
    awful = FrameStats(width=2000, height=1500, working_width=800,
                       working_height=600, sharpness=1.7, brightness=28.8,
                       contrast=2.1, clipped_fraction=0.98, dark_fraction=0.4)
    v = legibility.judge([line(t, 0.97) for t in LABEL_LINES], awful, cfg)
    assert v.readable
    assert v.reasons == []


def test_pixel_causes_are_appended_to_a_real_failure(cfg):
    dark = clean_frame()
    dark.brightness = 20.0
    dark.sharpness = 40.0
    v = legibility.judge([], dark, cfg)
    assert not v.readable
    assert LegibilityReason.NO_TEXT_FOUND in v.reasons
    assert LegibilityReason.TOO_DARK in v.reasons
    assert LegibilityReason.OUT_OF_FOCUS in v.reasons
    # The primary failure leads the sentence; the causes follow it.
    assert v.message.startswith("No text found.")
    assert "better light" in v.message


def test_message_is_always_populated(cfg):
    """A client shows `message` unconditionally, so it must never be empty."""
    for lines in ([], [line("x", 0.1)], [line(t, 0.99) for t in LABEL_LINES]):
        assert legibility.judge(lines, clean_frame(), cfg).message.strip()


def test_a_white_label_is_not_called_overexposed(cfg):
    """A care label IS white. Pristine frames measured 243.4 mean luma and read
    at 0.99, so an upper bound below that tells operators to fix the lighting on
    a correctly exposed photograph. Genuine glare measured 253+."""
    white = clean_frame()
    white.brightness = 243.7
    white.sharpness = 40.0  # force a failure so the causes are evaluated
    v = legibility.judge([], white, cfg)
    assert LegibilityReason.OVEREXPOSED not in v.reasons
    assert "direct light" not in v.message


def test_genuine_glare_is_still_reported(cfg):
    blown = clean_frame()
    blown.brightness = 253.4
    blown.clipped_fraction = 0.97
    v = legibility.judge([], blown, cfg)
    assert LegibilityReason.OVEREXPOSED in v.reasons
    assert LegibilityReason.GLARE in v.reasons


def test_a_dark_blank_frame_leads_with_the_light_instruction(cfg):
    """An empty frame that is also dark is empty BECAUSE it is dark. Leading with
    "check the lens is not covered" sends the operator to look at hardware when
    the fix is to turn a light on."""
    v = legibility.assess(flat_jpeg(18), cfg)
    assert LegibilityReason.BLANK_FRAME in v.reasons
    assert LegibilityReason.TOO_DARK in v.reasons
    assert v.message.startswith("Too dark")
    assert "lens is not covered" not in v.message


@pytest.mark.parametrize("count,expected", [
    (1, "hold the camera still and let it focus."),
    (2, "hold the camera still and let it focus and move into better light."),
    (3, "hold the camera still and let it focus, move into better light, and "
        "the label is washed out against its background."),
])
def test_causes_are_joined_readably(cfg, count, expected):
    """Three simultaneous causes are common — dark stockroom, arm's length, white
    label — and a plain join renders them as "a, and b, and c"."""
    stats = clean_frame()
    stats.sharpness = 40.0                                   # out of focus
    if count >= 2:
        stats.brightness = 20.0                              # too dark
    if count >= 3:
        stats.contrast = 2.0                                 # washed out
    message = legibility.judge([], stats, cfg).message
    assert message.endswith(expected), message
    assert ", and b" not in message
    # One "Also —" clause, never a run of them.
    assert message.count("Also") == 1


# --------------------------------------------------------------------------- #
# Decode, orientation and the blank-frame gate
# --------------------------------------------------------------------------- #

def test_prepare_downscales_and_reports_original_size():
    img, original = legibility.prepare(label_jpeg(long_edge=2400), 800)
    assert original == (2400, 1800)
    assert max(img.size) <= 800
    assert img.mode == "RGB"


def test_prepare_leaves_a_small_image_alone():
    img, original = legibility.prepare(label_jpeg(long_edge=600), 800)
    assert original == (600, 450)
    assert img.size == (600, 450)


def test_prepare_honours_exif_rotation():
    """A phone records portrait orientation in EXIF rather than in the pixels.
    Ignoring it hands the detector a sideways frame, and PP-OCR's horizontal-text
    assumption then finds nothing at all."""
    base = Image.open(io.BytesIO(label_jpeg(long_edge=1200)))
    buf = io.BytesIO()
    exif = base.getexif()
    exif[274] = 6  # Orientation: rotate 90 CW
    base.save(buf, "JPEG", exif=exif)
    img, _ = legibility.prepare(buf.getvalue(), 800)
    # 1200x900 landscape becomes 900x1200 portrait once the tag is applied.
    assert img.size[1] > img.size[0]


def test_undecodable_upload_raises_value_error():
    with pytest.raises(ValueError, match="Could not decode"):
        legibility.prepare(b"this is not an image", 800)


def test_blank_frame_does_not_report_tautological_causes(cfg):
    """A uniform frame has no edges and no contrast by definition, so "out of
    focus" and "washed out" restate the verdict instead of explaining it — and
    "let it focus" is advice nobody can act on when pointed at a grey wall."""
    v = legibility.assess(flat_jpeg(128), cfg)
    assert LegibilityReason.BLANK_FRAME in v.reasons
    assert LegibilityReason.OUT_OF_FOCUS not in v.reasons
    assert LegibilityReason.WASHED_OUT not in v.reasons
    assert "Also" not in v.message


def test_blank_frame_still_reports_exposure(cfg):
    """Exposure is the exception — "it is black" genuinely explains emptiness."""
    v = legibility.assess(flat_jpeg(250), cfg)
    assert LegibilityReason.BLANK_FRAME in v.reasons
    assert LegibilityReason.OVEREXPOSED in v.reasons


@pytest.mark.parametrize("value,name", [(4, "covered lens"), (252, "blown out"),
                                        (128, "grey wall")])
def test_blank_frame_skips_ocr(cfg, value, name):
    """The one pre-OCR gate. Worth having because OCR spends its full budget
    confirming there is nothing there, and this is the one answer the pixel
    statistics genuinely settle."""
    v = legibility.assess(flat_jpeg(value), cfg)
    assert not v.readable, name
    assert v.ocr_skipped
    assert LegibilityReason.BLANK_FRAME in v.reasons
    assert v.ocr_ms == 0


def test_blank_gate_floor_sits_below_the_readable_minimum(cfg):
    """Guards the margin rather than the threshold.

    Blank frames measured contrast <= 1.4; the least contrasty READABLE frame in
    the harness measured 2.1. A future edit that raises the gate past that starts
    rejecting legible photographs before OCR ever sees them.
    """
    assert float(cfg["blank_frame_max_contrast"]) < 2.1


# --------------------------------------------------------------------------- #
# The whole path, including inference
# --------------------------------------------------------------------------- #

ocr = pytest.mark.skipif(
    not legibility.available(),
    reason="RapidOCR/onnxruntime not installed in this environment",
)


@ocr
def test_a_normal_label_photo_is_readable(cfg):
    v = legibility.assess(label_jpeg(), cfg)
    assert v.readable, f"{v.message} (reasons={v.reasons})"
    assert v.confidence >= 0.90
    assert v.line_count >= 4
    assert "COTTON" in v.text.upper()
    assert not v.ocr_skipped
    assert v.ocr_ms > 0


@ocr
def test_text_far_too_small_is_rejected(cfg):
    """Below ~0.7% of frame height nothing recovers the text at any working
    resolution, so this must fail rather than return confident fragments."""
    v = legibility.assess(label_jpeg(text_frac=0.006), cfg, min_lines=4)
    assert not v.readable
    assert v.reasons


@ocr
def test_heavy_defocus_is_rejected(cfg):
    """Detection collapses to zero lines before confidence degrades, so this
    lands on NO_TEXT_FOUND rather than LOW_CONFIDENCE — and the pixel statistics
    are what turn that into a useful instruction."""
    from PIL import ImageFilter

    img = Image.open(io.BytesIO(label_jpeg())).filter(ImageFilter.GaussianBlur(14))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    v = legibility.assess(buf.getvalue(), cfg)
    assert not v.readable
    assert LegibilityReason.OUT_OF_FOCUS in v.reasons


@ocr
def test_jpeg_compression_alone_does_not_reject(cfg):
    """Measured: quality 12 still read at 0.988 with no character errors. A
    client compressing hard to save upload time must not be punished for it."""
    v = legibility.assess(label_jpeg(quality=12), cfg)
    assert v.readable, v.message


@ocr
def test_working_resolution_is_actually_applied(cfg):
    """Guards the RapidOCR pinning.

    `limit_type: max` discards the configured limit_side_len and picks its own
    from the input size, which silently put detection resolution — and the
    latency — back under the phone's control. If the pinning regresses, the
    working size reported here stops matching the policy.
    """
    v = legibility.assess(label_jpeg(long_edge=2400), cfg)
    assert max(v.frame.working_width, v.frame.working_height) == cfg["working_px"]
    assert v.frame.width == 2400


@ocr
def test_engine_is_reused_across_calls(cfg):
    """The warm-up would be pointless if each call rebuilt the graph."""
    first = legibility.engine(cfg)
    legibility.assess(label_jpeg(long_edge=800), cfg)
    assert legibility.engine(cfg) is first


@ocr
def test_concurrent_requests_do_not_corrupt_each_other(cfg):
    """RapidOCR's detector assigns `self.preprocess_op` on every call, so one
    shared instance is not re-entrant. Proves the lock holds."""
    import concurrent.futures

    payloads = [label_jpeg(long_edge=n) for n in (800, 1200, 1600, 2000)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda d: legibility.assess(d, cfg), payloads))
    for v in results:
        assert v.readable, v.message
        assert max(v.frame.working_width, v.frame.working_height) == cfg["working_px"]


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@ocr
def test_endpoint_accepts_multipart_and_answers(client):
    r = client.post(
        "/v1/readability",
        files={"image": ("label.jpg", label_jpeg(), "image/jpeg")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["readable"] is True
    assert body["message"]
    assert body["duration_ms"] > 0
    assert body["frame"]["working_width"] > 0


@ocr
def test_endpoint_passes_through_the_coverage_overrides(client):
    r = client.post(
        "/v1/readability",
        files={"image": ("label.jpg", label_jpeg(), "image/jpeg")},
        data={"min_lines": "50"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["readable"] is False
    assert "not_enough_text" in body["reasons"]


def test_endpoint_rejects_an_empty_upload(client):
    r = client.post("/v1/readability",
                    files={"image": ("empty.jpg", b"", "image/jpeg")})
    assert r.status_code == 400


def test_endpoint_rejects_a_non_image(client):
    r = client.post(
        "/v1/readability",
        files={"image": ("notes.txt", b"just some text", "text/plain")},
    )
    assert r.status_code == 400
    assert "decode" in r.json()["detail"].lower()


def test_endpoint_rejects_an_oversized_upload(client, cfg):
    cap = int(float(cfg["max_upload_mb"]) * 1024 * 1024)
    r = client.post(
        "/v1/readability",
        files={"image": ("huge.jpg", b"\xff" * (cap + 1024), "image/jpeg")},
    )
    assert r.status_code == 413
    # The message has to say what to do about it, not just refuse.
    assert "long edge" in r.json()["detail"]


def test_healthz_reports_ocr_status(client):
    body = client.get("/healthz").json()
    assert "readability" in body
    assert set(body["readability"]) == {"ocr_available", "working_px"}
