"""Items 6, 7 and 8 of docs/PICTURE-CHECK-FIXES.md — making the cut-out.

§4 of that document says what each has to do, and this file is it, cold:

  7  a cut-out with a podium in it stops passing verification, on a booth
     photograph whose border is NOT uniform — the case that used to abstain and
     let the Nike tee (`fc8e53e7`) through with `ok: true`
  6  a cut-out with a stand left in is re-made by a DIFFERENT segmenter,
     intersected with cloth-seg so the garment parser still decides what is
     cloth
  8  a re-matte that loses part of the garment is refused before it is stored,
     and the refusal says the original was kept

Every picture is synthesised with Pillow, so its properties are known exactly,
and the two real files in the repository root are used when they are there —
the podium in `fc8e53e7` is the measurement the thresholds were set from. No
network, no model, no rembg.
"""
from __future__ import annotations

import base64
import io
import random
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import cutout  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

CFG = cutout.config()
W, H = 600, 800

# The real cloth-seg cut-out of the Nike booth photograph, podium and all. §1.1
# and §4 both name it; it is 3000×4000 and the measurements in
# `imagery.cutout`'s policy comments were taken from it.
NIKE = Path(__file__).resolve().parents[1] / \
    "fc8e53e7-4818-46c9-b6fc-f0479eeafe44-original_fron-cutout.png"


# --------------------------------------------------------------------------- #
# Synthesised pictures
# --------------------------------------------------------------------------- #

def encode(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def _shirt(draw: ImageDraw.ImageDraw, **kw: Any) -> None:
    """A garment with shoulders, sleeves and a hem — not a circle.

    The shape matters: items 8's two rules are about a PIECE of it going
    missing, and a blob has no strap to lose.
    """
    draw.polygon([(150, 200), (230, 160), (370, 160), (450, 200), (470, 330),
                  (400, 350), (400, 620), (200, 620), (200, 350), (130, 330)], **kw)


def lit_sweep(seed: int = 7) -> bytes:
    """A booth photograph: a lighting gradient across a wall, plus grain.

    This is the source whose border `_source_backdrop` cannot use — measured
    spread well over the 60 it accepts, exactly as the Nike booth photograph's
    71 (§1.1). It is the whole reason item 7 exists.
    """
    rng = random.Random(seed)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(W):
            v = 90 + int(90 * (x / W)) + int(40 * (y / H)) + rng.randint(-30, 30)
            px[x, y] = (max(0, min(255, v)),) * 3
    ImageDraw.Draw(img).polygon([(150, 200), (450, 200), (470, 620), (130, 620)],
                                fill=(180, 40, 40))
    return encode(img, "JPEG")


def cutout_png(podium: str = "none", garment=(180, 40, 40, 255)) -> bytes:
    """A cut-out as this module makes them: RGBA, (255,255,255,0) under alpha.

    `podium`: `none`, `one` (a single slab under the hem, MID-000521's case) or
    `broken` (the same slab in sixteen pieces — a slatted crate, or a podium a
    shadow cuts up — none of them a third of the 1% a single region may cover,
    which is the case the bottom band exists for).
    """
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    _shirt(d, fill=garment)
    # The podium is the BACKDROP'S colour and fully opaque: white, kept, and
    # not garment. That is precisely what `_own_leftovers` counts.
    if podium == "one":
        d.rectangle((170, 620, 430, 690), fill=(252, 252, 252, 255))
    elif podium == "broken":
        for i in range(16):
            x = 160 + i * 17
            d.rectangle((x, 624, x + 14, 650), fill=(252, 252, 252, 255))
    return encode(img)


def composited(missing: str = "none", back=(235, 235, 235)) -> bytes:
    """A cut-out as vnyx-api stores them: fully opaque on the tenant's backdrop.

    §0: 0.0% alpha-clear on MID-000521, MID-000591, BOA-006151 and BOA-006153.
    This is what a re-matte would supersede, and what item 8 compares against.
    """
    img = Image.new("RGB", (W, H), back)
    d = ImageDraw.Draw(img)
    _shirt(d, fill=(180, 40, 40))
    _damage(d, missing, back)
    return encode(img)


def transparent(missing: str = "none") -> bytes:
    """The same garment as a fresh Hermes cut-out, with a piece optionally gone."""
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    _shirt(d, fill=(180, 40, 40, 255))
    _damage(d, missing, (255, 255, 255, 0))
    return encode(img)


def _damage(d: ImageDraw.ImageDraw, missing: str, gone: Any) -> None:
    """The two shapes of damage §2.1 names, drawn back out of the garment."""
    if missing == "back":
        # KIL-001625: "large part of shirt back missing". Big enough to move
        # the total on its own.
        d.rectangle((200, 420, 400, 620), fill=gone)
    elif missing == "strap":
        # KIL-001644: "background mask cut into left strap; strap missing". A
        # few percent of the garment — invisible to a total, one contiguous
        # piece at the garment's edge.
        d.polygon([(400, 160), (450, 200), (468, 330), (440, 335), (415, 205), (385, 170)],
                  fill=gone)
    elif missing == "hole":
        # Not at an edge: a pinhole in the middle of the garment, which is a
        # different defect (garment_hole in cutouts.py) and not this rule's.
        d.ellipse((280, 420, 340, 480), fill=gone)


# --------------------------------------------------------------------------- #
# Item 7 — `_kept_backdrop` stops abstaining
# --------------------------------------------------------------------------- #

def test_the_source_border_of_a_booth_photograph_really_is_unusable():
    """The premise: this is the abstention item 7 is about, not a straw man."""
    verdict, why = cutout._source_backdrop(lit_sweep(), cutout_png("one"))
    assert verdict is None and "not uniform" in why


def test_a_podium_is_refused_on_a_source_the_old_check_gave_up_on():
    """§4, item 7. The exact shape of the Nike tee: uneven sweep, podium kept."""
    ok, why = cutout._kept_backdrop(lit_sweep(), cutout_png("one"))
    assert ok is False
    assert "podium" in why and "own backdrop colour" in why
    # …and it says why the first question could not be put, rather than hiding
    # the abstention that used to be the whole answer.
    assert "not uniform" in why


def test_a_podium_broken_into_pieces_is_caught_by_the_bottom_band():
    """No single region reaches 1%, and a podium is still a podium."""
    m = cutout._own_leftovers(cutout_png("broken"), CFG)
    assert m["largest"] < CFG["leftover_region_max"]
    assert m["bottom"] > CFG["leftover_bottom_max"]
    ok, why = cutout._kept_backdrop(lit_sweep(), cutout_png("broken"))
    assert ok is False and "bottom band" in why


def test_a_clean_cutout_on_the_same_unusable_source_is_accepted():
    ok, why = cutout._kept_backdrop(lit_sweep(), cutout_png("none"))
    assert ok is True
    m = cutout._own_leftovers(cutout_png("none"), CFG)
    assert m["largest"] < 0.005 and m["bottom"] == 0.0


def test_nothing_is_refused_when_the_garment_is_its_own_backdrop_s_colour():
    """A white shirt on white: every kept pixel looks like a podium.

    The honest answer is that this cannot be told apart — §0's rule that a
    measurement which cannot get a usable mask reports `unknown` and flags
    nothing. The alternative is refusing every candidate and leaving the
    product with no cut-out at all.
    """
    white = cutout_png("none", garment=(250, 250, 250, 255))
    m = cutout._own_leftovers(white, CFG)
    assert "note" in m and "backdrop" in m["note"]
    assert cutout._kept_backdrop(lit_sweep(), white)[0] is True


def test_a_white_panel_inside_the_garment_is_not_a_podium():
    """The one false reject this backdrop invites, and why it does not happen.

    The cut-out's backdrop is PURE WHITE, so a garment with a genuinely white
    panel or logo has a solid region of backdrop colour inside its outline —
    and every strategy in turn would be refused for it, leaving the product
    with no cut-out at all. What is left of the SET is never inside the
    silhouette: a podium hangs off the hem, a halo rings the outline.
    """
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    _shirt(d, fill=(180, 40, 40, 255))
    d.rectangle((250, 380, 360, 520), fill=(255, 255, 255, 255))
    m = cutout._own_leftovers(encode(img), CFG)
    assert m["scattered"] > 0.10        # the panel is there, and counted
    assert m["largest"] == 0.0          # …and it reaches no edge, so it is not a leftover
    assert cutout._kept_backdrop(lit_sweep(), encode(img))[0] is True


def test_a_composited_cutout_is_not_read_as_a_frame_full_of_leftovers():
    """Opaque everywhere: "what the segmenter kept" is the whole frame."""
    m = cutout._own_leftovers(composited(), CFG)
    assert "clear" in m["note"]


def test_the_check_still_refuses_on_the_source_backdrop_when_it_has_one():
    """The original path is untouched: a flat source still decides first."""
    flat = Image.new("RGB", (W, H), (200, 200, 200))
    ImageDraw.Draw(flat).polygon([(150, 200), (450, 200), (470, 620), (130, 620)],
                                 fill=(180, 40, 40))
    img = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(img)
    _shirt(d, fill=(180, 40, 40, 255))
    d.rectangle((170, 620, 430, 700), fill=(200, 200, 200, 255))   # the sweep, kept
    ok, why = cutout._kept_backdrop(encode(flat, "JPEG"), encode(img))
    assert ok is False and "the mask left" in why


@pytest.mark.skipif(not NIKE.exists(), reason="the Nike booth cut-out is not in the checkout")
def test_the_nike_booth_cutout_is_refused_and_the_same_picture_without_its_podium_is_not():
    """§4, item 7, on the real file — where the numbers in policy came from."""
    import numpy as np

    data = NIKE.read_bytes()
    with_podium = cutout._own_leftovers(data, CFG)
    assert with_podium["largest"] > CFG["leftover_region_max"]      # measured 1.70%
    assert with_podium["bottom"] > CFG["leftover_bottom_max"]       # measured 31.9%
    assert cutout._kept_backdrop(lit_sweep(), data)[0] is False

    # The same cut-out with everything below the hem taken off: the podium
    # starts at about 0.686 of the frame's height.
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    arr = np.asarray(im).copy()
    row = int(0.686 * arr.shape[0])
    arr[row:, :, 3] = 0
    arr[row:, :, :3] = 255
    clean = encode(Image.fromarray(arr, "RGBA"))
    without = cutout._own_leftovers(clean, CFG)
    assert without["largest"] < 0.005 and without["bottom"] == 0.0  # measured 0.02%, 0.0%
    assert cutout._kept_backdrop(lit_sweep(), clean)[0] is True


# --------------------------------------------------------------------------- #
# Item 6 — a leftover is fixed with a DIFFERENT segmenter
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("reason", [
    "CUTOUT DEFECT — FRONT cut-out: stand visible at bottom",
    "CUTOUT DEFECT — BACK cut-out: hanger and stand visible",
    "CUTOUT DEFECT — FRONT cut-out: a hand holding the garment",
])
def test_something_left_in_asks_the_mask_strategies(reason):
    """MID-000521's own words, on all four of its cut-outs (§1.1)."""
    assert cutout.rematte_strategies(reason) == ["gemini-mask", "openai-mask"]


@pytest.mark.parametrize("reason", [
    "CUTOUT DEFECT — FRONT cut-out: collar cut away by background removal",
    "CUTOUT DEFECT — BACK cut-out: large part of shirt back missing; back missing",
    "CUTOUT DEFECT — BACK cut-out: background mask cut into left strap; strap missing",
    "CUTOUT DEFECT — FRONT cut-out: does not match its slot",
])
def test_a_missing_garment_part_keeps_the_default_chain(reason):
    """A second segmenter is not the answer to a collar the mask ate."""
    assert cutout.rematte_strategies(reason) is None


def test_the_leftover_strategies_are_a_policy_list():
    off = {"imagery": {"cutout": {"leftover_strategies": []}}}
    assert cutout.rematte_strategies("stand visible", cutout.config(off)) is None


def test_the_intersection_keeps_only_what_both_masks_kept():
    """§1.1's measurement, as a picture: one keeps the podium, one the hanger."""
    import numpy as np

    with_podium = cutout_png("one")
    hanger = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    d = ImageDraw.Draw(hanger)
    _shirt(d, fill=(180, 40, 40, 255))
    d.rectangle((290, 60, 310, 160), fill=(90, 90, 90, 255))        # the hook
    merged, err = cutout._intersect_alpha(encode(hanger), with_podium)
    assert err is None
    a = np.asarray(Image.open(io.BytesIO(merged)).convert("RGBA"))
    assert a[655, 300, 3] == 0          # the podium, which cloth-seg kept
    assert a[100, 300, 3] == 0          # the hook, which the mask strategy kept
    assert a[400, 300, 3] == 255        # the garment, which both kept


def test_two_masks_that_barely_overlap_are_not_a_cutout():
    left = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    ImageDraw.Draw(left).rectangle((10, 10, 120, 120), fill=(10, 10, 10, 255))
    right = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    ImageDraw.Draw(right).rectangle((400, 600, 560, 760), fill=(10, 10, 10, 255))
    merged, err = cutout._intersect_alpha(encode(left), encode(right))
    assert merged is None and "barely overlap" in err


# --- the chain, with every provider replaced ------------------------------- #

@pytest.fixture
def providers(monkeypatch):
    """Every strategy stubbed, and a record of which ones were asked."""
    calls: list[str] = []
    answers: dict[str, Any] = {
        "cloth-seg": (cutout_png("one"), None),        # keeps the podium, as it does
        "gemini-mask": (None, "stubbed"),
        "openai-mask": (None, "stubbed"),
        "gemini-paint": (None, "stubbed"),
    }

    def fake_cloth(data):
        calls.append("cloth-seg")
        return answers["cloth-seg"]

    def fake_gemini(data, timeout_s, prompt=cutout.PROMPT):
        name = "gemini-mask" if prompt is cutout.MASK_PROMPT else "gemini-paint"
        calls.append(name)
        return answers[name]

    def fake_openai(data, timeout_s, prompt):
        calls.append("openai-mask")
        return answers["openai-mask"]

    monkeypatch.setattr(cutout, "_cloth_seg", fake_cloth)
    monkeypatch.setattr(cutout, "_gemini", fake_gemini)
    monkeypatch.setattr(cutout, "_openai", fake_openai)
    # The mask strategies return a SILHOUETTE; _apply_mask is what turns one
    # into a cut-out of the original bytes. Stubbed so a test can hand back a
    # finished cut-out and still exercise the chain around it.
    monkeypatch.setattr(cutout, "_apply_mask", lambda src, mask: (mask, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda data: (True, "transparent (stub)"))
    return {"calls": calls, "answers": answers}


def test_a_podium_stops_the_chain_accepting_the_cloth_seg_cut_out(providers):
    """§4, item 7, through the function the endpoint calls.

    cloth-seg produces its usual cut-out with the podium in it and every other
    provider is unavailable, so the honest answer is no cut-out at all — where
    before item 7 this returned `ok: true` with the podium.
    """
    out, err, provider = cutout.remove_background(lit_sweep(), timeout_s=1)
    assert out is None and provider == "none"
    assert "podium" in err or "bottom band" in err
    assert providers["calls"][0] == "cloth-seg"


def test_naming_the_mask_strategies_leaves_cloth_seg_out_of_the_chain(providers):
    """Item 6: the strategies a re-matte for a leftover asks for."""
    providers["answers"]["gemini-mask"] = (cutout_png("none"), None)
    out, err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, strategies=["gemini-mask", "openai-mask"])
    assert provider == "gemini-mask" and out is not None
    # cloth-seg was never asked for a CUT-OUT — but it was asked for its
    # opinion, which is the intersection.
    assert providers["calls"] == ["gemini-mask", "cloth-seg"]


def test_the_intersection_removes_the_podium_the_mask_strategy_left(providers):
    """Both halves of §1.1's measurement, through the chain.

    The mask strategy hands back a cut-out that still has the podium; cloth-seg
    does not think the podium is clothing; the intersection is what passes.
    """
    import numpy as np

    providers["answers"]["gemini-mask"] = (cutout_png("one"), None)
    providers["answers"]["cloth-seg"] = (cutout_png("none"), None)
    out, err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, strategies=["gemini-mask"])
    assert out is not None and provider == "gemini-mask", err
    assert np.asarray(Image.open(io.BytesIO(out)).convert("RGBA"))[655, 300, 3] == 0


def test_skip_leaves_cloth_seg_out_altogether(providers):
    providers["answers"]["gemini-mask"] = (cutout_png("none"), None)
    out, _err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, skip=["cloth-seg"])
    assert provider == "gemini-mask" and out is not None
    assert "cloth-seg" not in providers["calls"]


def test_an_unknown_strategy_name_is_said_rather_than_ignored(providers):
    out, err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, strategies=["gemini-msak"])
    assert out is None and provider == "none" and "gemini-msak" in err
    assert providers["calls"] == []


# --------------------------------------------------------------------------- #
# Item 8 — a re-matte that loses garment is never stored
# --------------------------------------------------------------------------- #

def test_a_re_matte_that_cuts_away_the_shirt_back_is_refused():
    """KIL-001625, as §2.1 describes it."""
    ok, why = cutout.garment_kept(composited(), transparent("back"))
    assert ok is False
    assert "less garment" in why and "KEPT" in why


def test_a_re_matte_that_cuts_into_one_strap_is_refused():
    """KIL-001644. Too small to move the total; one piece, at the edge."""
    ok, why = cutout.garment_kept(composited(), transparent("strap"))
    assert ok is False
    assert "one piece" in why and "edge" in why and "KEPT" in why


def test_an_honest_re_cut_of_the_same_garment_is_allowed():
    ok, why = cutout.garment_kept(composited(), transparent())
    assert ok is True and "KEPT" not in why


def test_a_pinhole_inside_the_garment_is_not_an_edge_loss():
    """The rule is `materially less garment, or a contiguous loss AT AN EDGE`."""
    assert cutout.garment_kept(composited(), transparent("hole"))[0] is True


def test_the_podium_coming_off_is_not_garment_loss():
    """Items 6 and 8 must not fight each other, and this is why they do not.

    The comparison is of the GARMENT — each cut-out segmented against its own
    flat backdrop — and a podium is the backdrop's own colour, so it was never
    in the old cut-out's garment mask. A re-matte that removes it therefore
    loses no garment and is stored, which is the entire point of item 6.
    """
    old = Image.new("RGB", (W, H), (235, 235, 235))
    d = ImageDraw.Draw(old)
    _shirt(d, fill=(180, 40, 40))
    d.rectangle((170, 620, 430, 690), fill=(240, 240, 240))     # the podium, kept
    ok, why = cutout.garment_kept(encode(old), transparent())
    assert ok is True, why


def test_two_cutouts_of_different_frames_are_not_compared():
    square = Image.new("RGB", (600, 600), (235, 235, 235))
    ImageDraw.Draw(square).ellipse((100, 100, 500, 500), fill=(180, 40, 40))
    ok, why = cutout.garment_kept(composited(), encode(square))
    assert ok is True and "different frames" in why


def test_a_candidate_whose_garment_cannot_be_derived_does_not_get_to_replace_one():
    """It cannot be SHOWN to keep the garment, and it is the one asking."""
    blank = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    ok, why = cutout.garment_kept(composited(), encode(blank))
    assert ok is False and "KEPT" in why


def test_the_chain_refuses_to_replace_and_says_the_original_was_kept(providers):
    """§2.1: the refusal is a precondition, and it reads as one.

    Not `provider: none` — nothing failed. The caller must be able to tell "no
    cut-out could be made" from "the one on file is better", because only the
    first is worth queueing the product for again.
    """
    providers["answers"]["cloth-seg"] = (transparent("back"), None)
    out, err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, previous=composited())
    assert out is None
    assert provider == cutout.KEPT_EXISTING
    assert "existing cut-out was KEPT" in err and "less garment" in err


def test_a_better_cut_out_still_replaces(providers):
    providers["answers"]["cloth-seg"] = (transparent(), None)
    out, err, provider = cutout.remove_background(
        lit_sweep(), timeout_s=1, previous=composited("back"))
    assert out is not None and provider == "cloth-seg", err


# --------------------------------------------------------------------------- #
# The endpoint
# --------------------------------------------------------------------------- #

def test_the_endpoint_carries_the_strategies_and_the_previous_cut_out(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app

    seen: dict[str, Any] = {}

    def fake_remove(data, timeout_s=180.0, *, strategies=None, skip=None, previous=None):
        seen.update({"strategies": strategies, "skip": skip,
                     "previous": previous, "timeout": timeout_s})
        return None, "the existing cut-out was KEPT: nothing was as complete", cutout.KEPT_EXISTING

    monkeypatch.setattr(cutout, "remove_background", fake_remove)
    body = {
        "image_base64": base64.b64encode(lit_sweep()).decode(),
        "previous_base64": base64.b64encode(composited()).decode(),
        "strategies": ["gemini-mask", "openai-mask"],
        "skip": [],
        "timeout_s": 5,
    }
    resp = TestClient(app).post("/v1/imagery/remove-background", json=body)
    assert resp.status_code == 200
    payload = resp.json()
    # ok is false — nothing was produced — but `kept_existing` says why, and it
    # is the field that stops the product being re-queued.
    assert payload["ok"] is False and payload["kept_existing"] is True
    assert payload["provider"] == cutout.KEPT_EXISTING
    assert seen["strategies"] == ["gemini-mask", "openai-mask"]
    assert seen["previous"] == composited()


def test_the_endpoint_still_answers_without_any_of_the_new_fields(monkeypatch):
    """The old contract, unchanged: a caller that knows nothing of 6, 7 or 8."""
    from fastapi.testclient import TestClient

    from app.main import app

    monkeypatch.setattr(
        cutout, "remove_background",
        lambda data, timeout_s=180.0, **kw: (b"png-bytes", None, "cloth-seg"))
    resp = TestClient(app).post(
        "/v1/imagery/remove-background",
        json={"image_base64": base64.b64encode(lit_sweep()).decode()})
    payload = resp.json()
    assert payload["ok"] is True and payload["kept_existing"] is False
    assert base64.b64decode(payload["image_base64"]) == b"png-bytes"


# --------------------------------------------------------------------------- #
# The chain's own wiring
# --------------------------------------------------------------------------- #

def test_run_remote_sends_the_strategies_and_keep_better_as_typed_options(monkeypatch):
    """Nothing from a request becomes a free-form argv value, here too."""
    sent: dict[str, Any] = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "output": "", "results": None}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, json, headers, timeout: (
        sent.update(json) or Resp()))
    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    rp.run_remote(
        "backfill-bg-removal.ts",
        ["--db", "d", "--product", "p", "--apply", "--replace", "--keep-better",
         "--bg-strategies", "gemini-mask,openai-mask"],
        timeout_s=10, quiet=True)
    assert sent["options"] == {
        "replace": True, "keepBetter": True,
        "bgStrategies": ["gemini-mask", "openai-mask"],
    }


def test_a_re_matte_without_the_new_flags_sends_neither_option(monkeypatch):
    """The two options are opt-in per call — an older vnyx-api sees no change."""
    sent: dict[str, Any] = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "output": "", "results": None}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, json, headers, timeout: (
        sent.update(json) or Resp()))
    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    rp.run_remote("backfill-bg-removal.ts",
                  ["--db", "d", "--product", "p", "--apply", "--replace"],
                  timeout_s=10, quiet=True)
    assert sent["options"] == {"replace": True}


def test_the_policy_numbers_are_the_ones_the_module_defaults_to():
    """A policy edit is how a threshold moves; the two must agree today."""
    live, defaults = cutout.config(policy()), cutout.DEFAULTS
    for key in ("leftover_region_max", "leftover_bottom_max", "leftover_garment_min",
                "replace_area_drop_max", "replace_loss_region_max", "work_px"):
        assert live[key] == defaults[key], key
