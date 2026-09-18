"""Readiness phase 3 — the cut-outs.

docs/READINESS-PLAN.md §4 step 3, cold: the header reader, what backdrop a
tenant wants, the two judgements (canvas, backdrop) on synthesised images whose
properties are known exactly, the pairing of a cut-out with its original, the
rules that read the evidence, the verdict, and the chain's matte step with
every I/O edge replaced. No database, no network, no subprocess.
"""
from __future__ import annotations

import copy
import io
import random
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import net  # noqa: E402
from app.config import policy  # noqa: E402
from app.imaging import cutouts  # noqa: E402
from app.imaging import photo_audit as pa  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402
from app.models import ImagerySettings, MediaAsset, ProductSnapshot  # noqa: E402
from app.rules.imagery import check_imagery, cutout_pairs  # noqa: E402
from app.services.auto_approval import outcome  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

POL = policy()
CFG = cutouts.config(POL)


# --------------------------------------------------------------------------- #
# Synthesised pictures
# --------------------------------------------------------------------------- #

def encode(img: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    img.save(buf, fmt)
    return buf.getvalue()


def composite(w: int = 600, h: int = 800, rgb=(255, 255, 255)) -> Image.Image:
    """A cut-out composited onto a flat backdrop — what vnyx-api stores."""
    img = Image.new("RGB", (w, h), rgb)
    ImageDraw.Draw(img).ellipse((w // 5, h // 5, w * 4 // 5, h * 4 // 5), fill=(180, 40, 40))
    return img


def transparent(w: int = 600, h: int = 800) -> Image.Image:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((w // 5, h // 5, w * 4 // 5, h * 4 // 5), fill=(30, 30, 40, 255))
    return img


def scene(w: int = 600, h: int = 800, seed: int = 1) -> Image.Image:
    """A garment still in a room: a lighting gradient plus surface noise."""
    rng = random.Random(seed)
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            base = 90 + int(70 * (x / w)) + rng.randint(-45, 45)
            px[x, y] = (max(0, min(255, base)),) * 3
    ImageDraw.Draw(img).ellipse((w // 5, h // 5, w * 4 // 5, h * 4 // 5), fill=(180, 40, 40))
    return img


def with_badge(img: Image.Image) -> Image.Image:
    """A vnyx corner badge: opaque, small, inside the border ring."""
    w, h = img.size
    ImageDraw.Draw(img).rectangle((w - 90, h - 28, w - 8, h - 8), fill=(40, 40, 40))
    return img


# --------------------------------------------------------------------------- #
# The header reader
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP", "GIF"])
def test_dims_from_the_first_64kb_of_each_format(fmt):
    img = composite(1234, 567) if fmt != "GIF" else composite(1234, 567).convert("P")
    data = encode(img, fmt)
    assert net.dims_from_header(data[:65_536]) == (1234, 567)


def test_dims_lossless_webp_and_a_progressive_jpeg():
    lossless = io.BytesIO()
    composite(321, 654).save(lossless, "WEBP", lossless=True)
    assert net.dims_from_header(lossless.getvalue()[:4096]) == (321, 654)
    prog = io.BytesIO()
    composite(800, 600).save(prog, "JPEG", progressive=True, quality=80)
    assert net.dims_from_header(prog.getvalue()[:65_536]) == (800, 600)


def test_dims_are_unknown_not_wrong_on_garbage_or_a_cut_header():
    assert net.dims_from_header(b"hello world, not an image") is None
    assert net.dims_from_header(encode(composite())[:20]) is None
    assert net.dims_from_header(b"") is None


# --------------------------------------------------------------------------- #
# What the tenant wants behind the garment (decision 1)
# --------------------------------------------------------------------------- #

def test_backdrop_is_white_when_the_tenant_has_no_setting():
    for settings in (None, ImagerySettings(), {"background": ""}, {"background": None}):
        b = cutouts.expected_backdrop(settings, CFG)
        assert (b.kind, b.hex, b.source) == ("color", "#FFFFFF", "default")


def test_backdrop_follows_the_tenant_hex_transparent_or_image():
    assert cutouts.expected_backdrop({"background": "#ebebeb"}, CFG).hex == "#EBEBEB"
    assert cutouts.expected_backdrop(ImagerySettings(background="#FFF"), CFG).rgb == (255, 255, 255)
    assert cutouts.expected_backdrop({"background": "transparent"}, CFG).kind == "transparent"
    img = cutouts.expected_backdrop({"background": "/backgrounds/white.png"}, CFG)
    assert img.kind == "image" and "white.png" in img.describe()


def test_backdrop_ignores_auto_apply_false_the_prisma_default():
    """Eight of fifteen tenants sit on the default `false` with no colour set;
    they never chose transparency, and vnyx-api's resolveMatteBackdrop agrees."""
    b = cutouts.expected_backdrop({"background": None, "autoApplyBackground": False}, CFG)
    assert b.hex == "#FFFFFF"


# --------------------------------------------------------------------------- #
# The canvas
# --------------------------------------------------------------------------- #

def test_a_same_ratio_downscale_of_the_whole_frame_is_not_a_mismatch():
    """The Gemini segmenter's fixed 896×1195 on a 3000×4000 photograph, and its
    1279×816 on a 768×490 one: the frame scaled, the garment where it was."""
    assert cutouts.canvas_mismatch((3000, 4000), (3000, 4000), 0.02) is None
    assert cutouts.canvas_mismatch((896, 1195), (3000, 4000), 0.02) is None
    assert cutouts.canvas_mismatch((1279, 816), (768, 490), 0.02) is None


def test_an_original_stored_at_the_cameras_orientation_is_the_same_frame_turned():
    """MID-000253: the raw row says 4000×3000 (the sensor, with an EXIF
    rotation); the photograph everyone sees, and the segmenter cuts, is the
    3000×4000 portrait. Not a crop — and it cost a needless re-matte."""
    assert cutouts.canvas_mismatch((896, 1195), (4000, 3000), 0.02) is None
    assert cutouts.canvas_mismatch((3000, 4000), (4000, 3000), 0.02) is None
    # A real crop is still a crop, whichever way the original is stored.
    assert cutouts.canvas_mismatch((1000, 1000), (4000, 3000), 0.02)


def test_a_cutout_cropped_to_the_garment_has_the_garments_ratio_not_the_frames():
    why = cutouts.canvas_mismatch((1000, 1000), (3000, 4000), 0.02)
    assert why and "1000×1000" in why and "3000×4000" in why and "not the photograph's frame" in why
    assert cutouts.canvas_mismatch((2400, 4000), (3000, 4000), 0.02).endswith("tolerance 2%)")


def test_unknown_dimensions_are_not_compared():
    assert cutouts.canvas_mismatch((0, 0), (3000, 4000), 0.02) is None
    assert cutouts.canvas_mismatch((896, 1195), (None, None), 0.02) is None  # type: ignore[arg-type]


def bbox_crop(w: int = 600, h: int = 800, rgb=(255, 255, 255)) -> Image.Image:
    """What a segmenter that crops to the bounding box hands back: the garment
    touching every edge of its own canvas."""
    img = Image.new("RGB", (w, h), rgb)
    ImageDraw.Draw(img).ellipse((0, 0, w - 1, h - 1), fill=(180, 40, 40))
    return img


def sleeves_at_the_edge(w: int = 600, h: int = 800, rgb=(235, 235, 235)) -> Image.Image:
    """BOA-006127: the shirt reaches the left and right edges, not the top or
    bottom — in the original too. Not a crop."""
    img = Image.new("RGB", (w, h), rgb)
    ImageDraw.Draw(img).rectangle((0, h // 5, w - 1, h * 4 // 5), fill=(120, 30, 30))
    return img


def test_a_bounding_box_crop_touches_all_four_edges_and_is_named():
    m = cutouts.measure(encode(bbox_crop()), CFG, (255, 255, 255))
    assert m["edges_touched"] == 4
    why = cutouts.frame_mismatch(m, CFG)
    assert why and "all 4 edges" in why and "bounding box" in why


def test_a_garment_at_two_edges_is_not_a_crop_and_still_passes_the_backdrop():
    grey = cutouts.expected_backdrop({"background": "#EBEBEB"}, CFG)
    m = cutouts.measure(encode(sleeves_at_the_edge()), CFG, grey.rgb)
    assert m["edges_touched"] == 2
    assert cutouts.frame_mismatch(m, CFG) is None
    assert 0.5 <= m["coverage"] < 0.95           # sleeves in the ring
    assert cutouts.background_mismatch(m, grey, CFG) is None


def test_the_whole_frame_scaled_down_touches_no_edge():
    m = cutouts.measure(encode(composite(896, 1195)), CFG, (255, 255, 255))
    assert m["edges_touched"] == 0 and cutouts.frame_mismatch(m, CFG) is None


# --------------------------------------------------------------------------- #
# The backdrop, from the pixels
# --------------------------------------------------------------------------- #

WHITE = cutouts.expected_backdrop(None, CFG)
GREY = cutouts.expected_backdrop({"background": "#EBEBEB"}, CFG)
ALPHA = cutouts.expected_backdrop({"background": "transparent"}, CFG)


def border(img: Image.Image, expected: cutouts.Backdrop = WHITE) -> dict[str, Any]:
    m = cutouts.measure(encode(img), CFG, expected.rgb)
    assert m is not None
    return m


def test_measure_reports_the_canvas_and_the_ring():
    m = border(composite(600, 800))
    assert (m["width"], m["height"]) == (600, 800)
    assert m["transparent"] == 0 and m["rgb"] == [255, 255, 255] and m["coverage"] == 1.0


def test_white_composite_on_a_white_tenant_passes():
    assert cutouts.background_mismatch(border(composite()), WHITE, CFG) is None


def test_grey_composite_on_a_white_tenant_is_named_with_delta_e():
    why = cutouts.background_mismatch(border(composite(rgb=(235, 235, 235))), WHITE, CFG)
    assert why and "#EBEBEB" in why and "#FFFFFF" in why and "ΔE" in why


def test_grey_composite_on_a_grey_tenant_passes():
    m = border(composite(rgb=(235, 235, 235)), GREY)
    assert cutouts.background_mismatch(m, GREY, CFG) is None


def test_transparent_cutout_where_a_colour_is_wanted_and_vice_versa():
    t = border(transparent())
    assert t["transparent"] >= 0.85
    assert "transparent where" in cutouts.background_mismatch(t, WHITE, CFG)
    assert cutouts.background_mismatch(t, ALPHA, CFG) is None
    opaque = border(composite(), ALPHA)
    assert "opaque" in cutouts.background_mismatch(opaque, ALPHA, CFG)


def test_a_room_still_in_the_frame_fails_on_coverage():
    m = border(scene())
    assert m["coverage"] < 0.5
    why = cutouts.background_mismatch(m, WHITE, CFG)
    assert why and "background is still in the frame" in why


def test_a_corner_badge_does_not_fail_a_correct_cutout():
    """Coverage, not a standard deviation: a 2% badge is still 98% backdrop."""
    m = border(with_badge(composite()))
    assert m["coverage"] >= 0.95
    assert cutouts.background_mismatch(m, WHITE, CFG) is None
    assert m["edges_touched"] <= 2                # the badge sits on two edges


def test_delta_e_separates_white_from_the_house_grey():
    assert cutouts.delta_e((255, 255, 255), (255, 255, 255)) == 0
    assert cutouts.delta_e((255, 255, 255), (235, 235, 235)) > 4.0
    assert cutouts.delta_e((255, 255, 255), (250, 250, 250)) < 4.0


# --------------------------------------------------------------------------- #
# Pairing a cut-out with its original
# --------------------------------------------------------------------------- #

def asset(view: str, processing: str = "RAW", *, id: str | None = None,
          derived: str | None = None, current: bool = True,
          width: int | None = None, height: int | None = None,
          position: int = 0, url: str | None = None,
          border_: dict[str, Any] | None = None) -> MediaAsset:
    return MediaAsset(
        url=url or f"https://r2.dev/products/{view.lower()}-{processing.lower()}-{id or position}.png",
        view=view, processing=processing, id=id, derived_from_id=derived,
        is_current=current, width=width, height=height, position=position,
        border=border_,
    )


def snap(media: list[MediaAsset], settings: ImagerySettings | None = None) -> ProductSnapshot:
    return ProductSnapshot(id="p1", title="Vintage Zara Black Jacket Men M",
                           master_category="Men", category="Jackets",
                           media=media, imagery_settings=settings or ImagerySettings())


def test_pairs_follow_derived_from_id_to_the_archived_original():
    raw = asset("FRONT", "RAW", id="r1", current=False, width=3000, height=4000)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=896, height=1195)
    pairs = cutout_pairs(snap([cut, raw]), POL)
    assert [(v, c.id, r.id) for v, c, r in pairs] == [("FRONT", "c1", "r1")]


def test_pairs_fall_back_to_the_superseded_raw_of_the_same_view():
    archived = asset("FRONT", "RAW", id="r0", current=False, position=1)
    live_raw = asset("FRONT", "RAW", id="r9", current=True, position=9)
    cut = asset("FRONT", "BG_REMOVED", id="c1")           # no derivation edge
    back = asset("BACK", "BG_REMOVED", id="c2")
    pairs = {v: (c.id, r.id if r else None) for v, c, r in cutout_pairs(snap([cut, back, archived, live_raw]), POL)}
    assert pairs["FRONT"] == ("c1", "r0")                    # superseded first
    assert pairs["BACK"] == ("c2", None)                     # nothing of that view


def test_pairs_never_cross_views_or_count_other_cutouts():
    cut_other = asset("OTHER", "BG_REMOVED", id="o1")
    raw_back = asset("BACK", "RAW", id="rb", current=False)
    assert cutout_pairs(snap([cut_other, raw_back]), POL) == []


def test_pairs_fall_back_only_within_the_same_origin():
    """BLM-000408: a WEB cut-out beside a PHOTOBOOTH and a DECISION original is
    three photographs; comparing shapes across them would invent a defect."""
    cut = MediaAsset(url="https://r2/c.png", view="FRONT", processing="BG_REMOVED", id="c1", origin="WEB")
    booth = MediaAsset(url="https://r2/b.jpg", view="FRONT", processing="RAW", id="r1", origin="PHOTOBOOTH")
    decision = MediaAsset(url="https://r2/d.jpg", view="FRONT", processing="RAW", id="r2", origin="DECISION")
    assert cutout_pairs(snap([cut, booth, decision]), POL) == [("FRONT", cut, None)]
    web = MediaAsset(url="https://r2/w.jpg", view="FRONT", processing="RAW", id="r3", origin="WEB", is_current=False)
    assert cutout_pairs(snap([cut, booth, decision, web]), POL) == [("FRONT", cut, web)]


# --------------------------------------------------------------------------- #
# The rules: IMG.026 canvas, IMG.027 backdrop
# --------------------------------------------------------------------------- #

def ids(p: ProductSnapshot, pol: dict[str, Any] = POL) -> dict[str, Any]:
    return {f.rule_id: f for f in check_imagery(p, pol)}


def test_img026_reads_stored_dimensions_and_is_soft_by_default():
    raw = asset("FRONT", "RAW", id="r1", current=False, width=3000, height=4000)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=1000, height=1000)
    found = ids(snap([cut, raw]))
    assert "IMG.026" in found
    assert found["IMG.026"].severity.value == "low"
    assert found["IMG.026"].detail["original_canvas"] == [3000, 4000]


def test_img026_blocks_once_the_hold_is_switched_to_block():
    pol = copy.deepcopy(POL)
    pol["readiness"]["cutouts"]["hold"] = "block"
    raw = asset("FRONT", "RAW", id="r1", current=False, width=3000, height=4000)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=1000, height=1000)
    assert ids(snap([cut, raw]), pol)["IMG.026"].severity.value == "high"


def test_img026_is_silent_without_dimensions_or_for_a_same_ratio_downscale():
    raw = asset("FRONT", "RAW", id="r1", current=False)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=1000, height=1000)
    assert "IMG.026" not in ids(snap([cut, raw]))
    raw2 = asset("FRONT", "RAW", id="r1", current=False, width=3000, height=4000)
    cut2 = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=896, height=1195)
    assert "IMG.026" not in ids(snap([cut2, raw2]))


def test_img026_fires_on_a_bounding_box_crop_from_the_border_alone():
    """No original on file (a C-twin), but the garment touches all four edges."""
    cropped = {"transparent": 0.0, "rgb": [255, 255, 255], "coverage": 0.6, "stddev": 0.0,
               "edge_fill": [0.4, 0.5, 0.3, 0.3], "edges_touched": 4}
    found = ids(snap([asset("FRONT", "BG_REMOVED", id="c1", border_=cropped)]))
    assert "IMG.026" in found and "bounding box" in found["IMG.026"].message
    assert found["IMG.026"].detail["original"] is None


def test_img027_reads_the_border_the_judge_attached():
    # Measured against a WHITE tenant: none of the ring is white, all of it is
    # one other colour — a flat backdrop of the wrong colour, named with its hex.
    grey_on_white = {"transparent": 0.0, "rgb": [235, 235, 235], "coverage": 0.0,
                     "uniformity": 1.0, "stddev": 0.0, "edge_fill": [0, 0, 0, 0], "edges_touched": 0}
    found = ids(snap([asset("FRONT", "BG_REMOVED", id="c1", border_=grey_on_white)]))
    assert "IMG.027" in found and "#EBEBEB" in found["IMG.027"].message and "ΔE" in found["IMG.027"].message
    # The same ring measured against a tenant whose backdrop IS that grey.
    grey_on_grey = {**grey_on_white, "coverage": 1.0}
    assert "IMG.027" not in ids(snap([asset("FRONT", "BG_REMOVED", id="c1", border_=grey_on_grey)],
                                     ImagerySettings(background="#EBEBEB")))
    # A ring that is many colours: a background still in the frame.
    room = {**grey_on_white, "coverage": 0.1, "uniformity": 0.2}
    assert "background is still in the frame" in ids(snap([asset("FRONT", "BG_REMOVED", id="c1", border_=room)]))["IMG.027"].message


def test_rules_are_silent_when_the_phase_is_switched_off():
    pol = copy.deepcopy(POL)
    pol["readiness"]["cutouts"]["enabled"] = False
    raw = asset("FRONT", "RAW", id="r1", current=False, width=3000, height=4000)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", width=896, height=1195,
                border_={"transparent": 0.0, "rgb": [0, 0, 0], "coverage": 0.1, "stddev": 50})
    found = ids(snap([cut, raw]), pol)
    assert "IMG.026" not in found and "IMG.027" not in found


# --------------------------------------------------------------------------- #
# The verdict
# --------------------------------------------------------------------------- #

def fake_io(files: dict[str, bytes | None], dims: dict[str, tuple[int, int] | None] | None = None):
    def fetch(urls, timeout_s=0, deadline_s=0):
        return {u: files.get(u) for u in urls}

    def read_dims(urls, timeout_s=0, deadline_s=0):
        return {u: (dims or {}).get(u) for u in urls}

    return fetch, read_dims


# --------------------------------------------------------------------------- #
# The garment against its photograph: the neckline hole (MID-000569)
# --------------------------------------------------------------------------- #

def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def neck_pair(neck: str, *, shift: int = 0) -> tuple[bytes, bytes]:
    """A navy top on a grey studio backdrop, photographed and cut out. The
    neck notch at the top centre shows, in the PHOTOGRAPH, `form` (a white
    form's neck), `collar` (the garment itself), `backdrop` (a real opening);
    `whole` has no notch in the cut-out at all. `shift` moves the garment in
    the photograph so the two no longer cover the same frame."""
    back, navy, w, h = (180, 176, 174), (60, 60, 90), 300, 400
    body, notch = (60, 80, 240, 360), (120, 80, 180, 130)
    raw = Image.new("RGB", (w, h), back)
    d = ImageDraw.Draw(raw)
    d.rectangle(tuple(v + (shift if i % 2 == 0 else 0) for i, v in enumerate(body)), fill=navy)
    if neck == "form":
        d.rectangle(notch, fill=(255, 255, 255))
    elif neck == "backdrop":
        d.rectangle(notch, fill=back)
    # Composited on white — the fixture tenant's backdrop — so only the neckline speaks.
    cut = Image.new("RGBA", (w, h), (255, 255, 255, 255))
    dc = ImageDraw.Draw(cut)
    dc.rectangle(body, fill=navy + (255,))
    if neck != "whole":
        dc.rectangle(notch, fill=(255, 255, 255, 255))
    return _png(cut), _png(raw)


ON = {**CFG, "garment_check": {**(CFG.get("garment_check") or {}), "residue_check": True}}


def test_the_forms_neck_showing_through_the_collar_is_measured_and_cannot_be_re_cut():
    """MID-000569's booth FRONT: 2.2% of the garment, white in the photograph.
    Only reported behind `residue_check`, which is off — see the next test."""
    cut, raw = neck_pair("form")
    m = cutouts.garment_hole(cut, raw, CFG)
    assert m["aligned"] and m["residue"] > 0.05 and m["loss"] == 0 and m["opening"] == 0
    assert m["residue_rgb"] == [255, 255, 255]
    why, fixable = cutouts.hole_problem(m, ON)
    assert why and "form's neck" in why and "re-cut reproduces the hole" in why and not fixable


def test_a_brighter_patch_of_the_same_wall_is_not_a_form_and_is_not_reported():
    """KLE-000124: dungarees hanging open between their straps over wall at
    rgb(243,243,243) while the border ring reads rgb(200,198,197). The gap is
    wall, the cut-out is correct, and both views were flagged at 5.6% and 4.7%.
    The measurement cannot separate that from a form, so it is off."""
    lit = Image.new("RGB", (300, 400), (200, 198, 197))
    d = ImageDraw.Draw(lit)
    d.rectangle((60, 40, 240, 360), fill=(243, 242, 243))          # the lit centre
    d.rectangle((105, 130, 195, 340), fill=(60, 60, 90))           # the garment
    d.rectangle((120, 60, 135, 130), fill=(60, 60, 90))            # its two straps
    d.rectangle((165, 60, 180, 130), fill=(60, 60, 90))
    cut = Image.new("RGBA", (300, 400), (255, 255, 255, 255))
    dc = ImageDraw.Draw(cut)
    dc.rectangle((105, 130, 195, 340), fill=(60, 60, 90, 255))
    dc.rectangle((120, 60, 135, 130), fill=(60, 60, 90, 255))
    dc.rectangle((165, 60, 180, 130), fill=(60, 60, 90, 255))
    m = cutouts.garment_hole(_png(cut), _png(lit), CFG)
    assert m["aligned"] and m["loss"] == 0                          # nothing was cut away
    assert m["residue"] > 0.008                                     # and it still measures "an object"
    assert cutouts.hole_problem(m, CFG) == (None, False)            # …which is why it is off
    assert cutouts.hole_problem(m, ON)[0]                           # switched on, it would fire


def test_a_collar_the_mask_cut_away_is_garment_in_the_photograph_and_is_re_cut():
    cut, raw = neck_pair("collar")
    m = cutouts.garment_hole(cut, raw, CFG)
    assert m["loss"] > 0.05 and m["residue"] < 0.005          # a trace of edge blending, no more
    why, fixable = cutouts.hole_problem(m, CFG)
    assert why and "cut away at the collar" in why and fixable


def test_a_real_neck_opening_and_a_whole_garment_have_nothing_to_say():
    for neck in ("backdrop", "whole"):
        m = cutouts.garment_hole(*neck_pair(neck), CFG)
        assert cutouts.hole_problem(m, ON) == (None, False), neck
    assert cutouts.garment_hole(*neck_pair("backdrop"), CFG)["opening"] > 0.05


def test_two_pictures_that_do_not_cover_the_same_frame_are_not_asked_about_a_neckline():
    m = cutouts.garment_hole(*neck_pair("form", shift=120), CFG)
    assert m["aligned"] is False and cutouts.hole_problem(m, CFG) == (None, False)
    assert cutouts.hole_problem(None, CFG) == (None, False)
    assert cutouts.garment_hole(b"not a png", b"nor this", CFG) is None


# --------------------------------------------------------------------------- #
# The same-ratio zoom (KLE-000028)
# --------------------------------------------------------------------------- #
#
# The cut-out keeps the photograph's aspect ratio and touches no edge, so both
# older tests pass it; the garment inside stands 1.4x larger. Measured on the
# real pair: a correct cut-out overlaps its photograph 0.988-1.000, that one
# 0.519.

def zoomed_pair(scale: float = 1.45) -> tuple[bytes, bytes]:
    """The same garment, cut out at `scale` and padded back to the frame's ratio."""
    raw = Image.new("RGB", (300, 400), (180, 176, 174))
    ImageDraw.Draw(raw).rectangle((105, 60, 195, 290), fill=(60, 60, 90))
    cut = Image.new("RGBA", (300, 400), (255, 255, 255, 255))
    w, h = int(90 * scale), int(230 * scale)
    ImageDraw.Draw(cut).rectangle((150 - w // 2, 175 - h // 2, 150 + w // 2, 175 + h // 2),
                                  fill=(60, 60, 90, 255))
    return _png(cut), _png(raw)


def test_a_cutout_zoomed_inside_the_right_ratio_is_named_and_re_cut():
    m = cutouts.garment_hole(*zoomed_pair(), CFG)
    assert m["aligned"] is False and m["overlap"] < 0.9
    why, fixable = cutouts.frame_problem(m, CFG, derived=True)
    assert why and "zoomed or shifted" in why and "Re-cut it" in why and fixable
    # The ratio test and the edge test, which is why this had to be measured.
    assert cutouts.canvas_mismatch((896, 1195), (3000, 4000), 0.02) is None


def test_the_same_framing_says_nothing_about_framing():
    m = cutouts.garment_hole(*zoomed_pair(scale=1.0), CFG)
    assert m["aligned"] is True
    assert cutouts.frame_problem(m, CFG, derived=True) == (None, False)
    assert cutouts.frame_problem(None, CFG, derived=True) == (None, False)


def test_a_guessed_pairing_is_a_note_never_a_defect():
    """With no derivation edge the original was matched by view and origin, and
    two different photographs of the same view disagree honestly."""
    m = cutouts.garment_hole(*zoomed_pair(), CFG)
    why, fixable = cutouts.frame_problem(m, CFG, derived=False)
    assert why and "may simply be different photographs" in why and not fixable


def test_judge_re_cuts_a_zoomed_cutout_and_says_which_view():
    raw = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg",
                width=3000, height=4000)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png",
                width=896, height=1195)
    c, r = zoomed_pair()
    fetch, read_dims = fake_io({"https://r2/cut.png": c, "https://r2/raw.jpg": r})
    v = cutouts.judge(snap([cut, raw]), POL, fetch=fetch, read_dims=read_dims)
    assert v.action == "bad" and v.bad_views == ["FRONT"] and v.unfixable_views == []
    assert any("framing:" in reason for reason in v.reasons)
    # And the rules report the same thing from the measurement left on the row.
    assert "IMG.026" in ids(snap([cut, raw]))


def test_judge_re_cuts_a_lost_collar_and_leaves_the_forms_neck_alone():
    raw = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg", width=300, height=400)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    braw = asset("BACK", "RAW", id="r2", current=False, url="https://r2/braw.jpg", width=300, height=400)
    bcut = asset("BACK", "BG_REMOVED", id="c2", derived="r2", url="https://r2/bcut.png")
    fc, fr = neck_pair("form")
    bc, br = neck_pair("collar")
    fetch, read_dims = fake_io({"https://r2/cut.png": fc, "https://r2/raw.jpg": fr,
                                "https://r2/bcut.png": bc, "https://r2/braw.jpg": br})
    v = cutouts.judge(snap([cut, raw, bcut, braw]), POL, fetch=fetch, read_dims=read_dims)
    assert v.action == "bad"
    assert v.bad_views == ["BACK"]                       # the lost collar: re-cut
    assert v.unfixable_views == []                       # the form's neck: not reported at all
    assert any(r.startswith("BACK: neckline") and "cut away" in r for r in v.reasons)
    assert not any(r.startswith("FRONT:") for r in v.reasons)
    assert cut.border["garment"]["residue"] > 0.05       # the measurement still rides on the row


def test_judge_names_the_zoomed_view_and_writes_the_evidence_back():
    raw = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg")
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    back_raw = asset("BACK", "RAW", id="r2", current=False, url="https://r2/braw.jpg", width=600, height=800)
    back = asset("BACK", "BG_REMOVED", id="c2", derived="r2", url="https://r2/bcut.png")
    fetch, read_dims = fake_io(
        # FRONT: cropped to the garment — square, touching every edge. BACK: the
        # whole frame scaled down, which is fine.
        {"https://r2/cut.png": encode(bbox_crop(240, 240)), "https://r2/bcut.png": encode(composite(300, 400))},
        {"https://r2/raw.jpg": (600, 800)},
    )
    p = snap([cut, raw, back, back_raw])
    v = cutouts.judge(p, POL, fetch=fetch, read_dims=read_dims)
    assert v.action == "bad" and v.bad_views == ["FRONT"] and v.code == "CUTOUT_UNFIXABLE"
    assert v.hold == "soft" and not v.blocks and v.soft == v.reasons
    assert any(r.startswith("FRONT: canvas:") for r in v.reasons)
    assert any(r.startswith("FRONT: frame:") for r in v.reasons)
    assert not any(r.startswith("BACK:") for r in v.reasons)
    # Evidence back on the rows: the rules can now fire without a download.
    assert (cut.width, cut.height) == (240, 240) and cut.border["edges_touched"] == 4
    assert (raw.width, raw.height) == (600, 800)
    found = ids(p)
    assert "IMG.026" in found and "IMG.027" not in found


def test_judge_passes_a_correct_pair_and_blocks_only_in_block_mode():
    raw = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg", width=600, height=800)
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    fetch, read_dims = fake_io({"https://r2/cut.png": encode(composite(600, 800))})
    assert cutouts.judge(snap([cut, raw]), POL, fetch=fetch, read_dims=read_dims).action == "ok"

    pol = copy.deepcopy(POL)
    pol["readiness"]["cutouts"]["hold"] = "block"
    grey_cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    fetch, read_dims = fake_io({"https://r2/cut.png": encode(composite(600, 800, rgb=(235, 235, 235)))})
    v = cutouts.judge(snap([grey_cut, raw]), pol, fetch=fetch, read_dims=read_dims)
    assert v.action == "bad" and v.blocks and v.soft == []


def test_judge_is_unavailable_not_wrong_when_nothing_downloads():
    raw = asset("FRONT", "RAW", id="r1", current=False, url="https://r2/raw.jpg")
    cut = asset("FRONT", "BG_REMOVED", id="c1", derived="r1", url="https://r2/cut.png")
    fetch, read_dims = fake_io({})
    v = cutouts.judge(snap([cut, raw]), POL, fetch=fetch, read_dims=read_dims)
    assert v.action == "unknown" and v.unavailable and not v.blocks


def test_judge_skips_without_cutouts_or_when_disabled():
    assert cutouts.judge(snap([asset("FRONT", "RAW", id="r1")]), POL).action == "skipped"
    pol = copy.deepcopy(POL)
    pol["readiness"]["cutouts"]["enabled"] = False
    assert cutouts.judge(snap([asset("FRONT", "BG_REMOVED", id="c1")]), pol).action == "skipped"


def test_outcome_names_the_cutout_hold():
    v = outcome.classify({"approval": {"outcome": "gate_blocked", "gate_code": "CUTOUT_UNFIXABLE",
                                       "blockers": ["cut-outs: FRONT: frame: the garment touches all 4 edges"]}})
    assert (v.status, v.outcome, v.retryable) == ("HELD_FOR_HUMAN", "CUTOUT_UNFIXABLE", False)


# --------------------------------------------------------------------------- #
# The chain's matte step, every I/O edge replaced
# --------------------------------------------------------------------------- #

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000001"


def _state(**over: Any) -> dict[str, Any]:
    base = {
        "loaded": {
            "record": {"gender": ["men"], "category": "Jackets", "subCategory": "Jackets",
                       "masterCategory": "Men", "sizingGuide": None, "updatedAt": None},
            "media": [
                {"id": "c1", "url": "https://x/cut.png", "view": "FRONT", "processing": "BG_REMOVED",
                 "mediaType": "IMAGE", "position": 1, "isCurrent": True, "derivedFromId": "r1"},
                {"id": "r1", "url": "https://x/raw.jpg", "view": "FRONT", "processing": "RAW",
                 "mediaType": "IMAGE", "position": 0, "isCurrent": False},
                {"url": "https://x/f.png", "view": "AI_FRONT", "mediaType": "IMAGE", "position": 2},
            ],
            "catalog": {}, "imagery_settings": None,
        },
        "title": "Test jacket", "tenant": "T", "sku": "T-1", "stage": "REVIEW",
        "review_status": "PENDING", "edit_url": None,
        "description_missing": False, "description_chars": 100,
        "care_label": 1, "unmatted": 0, "unmatted_views": [], "leftover_raw": 0,
        "cutout_views": ["FRONT"],
        "renders": 5, "render_rows": 5, "renders_missing": 0,
        "generation_status": "COMPLETE", "is_regenerating": False,
        "attributes_missing": [],
    }
    base.update(over)
    return base


BAD = cutouts.CutoutVerdict("bad", "soft", code="CUTOUT_UNFIXABLE",
                            reasons=["FRONT: canvas: aspect 1.000 (1000×1000) against the original's 0.750 (3000×4000) — not the photograph's frame (tolerance 2%)"],
                            bad_views=["FRONT"])
BAD_BLOCK = cutouts.CutoutVerdict("bad", "block", code="CUTOUT_UNFIXABLE",
                                  reasons=list(BAD.reasons), bad_views=["FRONT"])
OK = cutouts.CutoutVerdict("ok", "soft")


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, Any] = {"approve": [], "matte_args": [], "judge": 0}
    states: list[dict[str, Any]] = [_state()]
    verdicts: list[Any] = [OK, OK]        # before, after

    def fake_needs(dsn, pid):
        return states[0]

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "backfill-bg-removal.ts":
            calls["matte_args"].append(list(args))
            return True, "  cut-outs written : 1", None
        if script == "verify-and-repair.ts":
            return True, "", {"ok": True, "applied": [], "failed": []}
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    approve_outcome: dict[str, Any] = {"outcome": "would_approve", "problems": []}

    def fake_approve_check(vnyx_api, dsn, pid, *, apply, skip_bin, quiet, allow_stage=None):
        calls["approve"].append({"apply": apply})
        return dict(approve_outcome)

    def fake_cut_judge(snapshot, pol, **kw):
        i = min(calls["judge"], len(verdicts) - 1)
        calls["judge"] += 1
        return verdicts[i]

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "approve_check", fake_approve_check)
    monkeypatch.setattr(qg, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(cutouts, "judge", fake_cut_judge)
    monkeypatch.setattr(
        rp.product_audit, "audit",
        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}},
    )
    return {"calls": calls, "states": states, "verdicts": verdicts, "approve": approve_outcome}


def _repair(apply: bool = False, approve: bool = False) -> dict[str, Any]:
    return rp.repair(DSN, PID, apply=apply, vnyx_api=Path("."), infer=False,
                     min_confidence=70, skip_render=True, approve=approve,
                     skip_bin=True, quiet=True, silent=True)


def matte_step(r: dict[str, Any]) -> dict[str, Any]:
    return next(s for s in r["steps"] if s["step"] == "matte")


def test_a_correct_cutout_is_measured_and_nothing_is_re_matted(wired):
    r = _repair(apply=True)
    step = matte_step(r)
    assert step["ran"] and step["ok"]
    assert "on its canvas" in step["note"]
    assert wired["calls"]["matte_args"] == []            # the segmenter never ran
    assert r["cutouts"]["after"]["action"] == "ok"
    assert r["approval"]["outcome"] == "would_approve"


def test_a_wrong_cutout_is_re_matted_with_replace_and_re_checked(wired):
    wired["verdicts"][:] = [BAD, OK]
    r = _repair(apply=True)
    args = wired["calls"]["matte_args"]
    assert len(args) == 1 and "--replace" in args[0] and "--apply" in args[0]
    assert "--provider" in args[0]
    note = matte_step(r)["note"]
    assert "re-matte FRONT" in note and "re-checked" in note
    assert r["cutouts"]["before"]["action"] == "bad"
    assert r["cutouts"]["after"]["action"] == "ok"
    assert r["approval"]["outcome"] == "would_approve"


def test_a_dry_run_lists_the_re_matte_and_never_holds_on_it(wired):
    wired["verdicts"][:] = [BAD_BLOCK, BAD_BLOCK]
    r = _repair(apply=False)
    args = wired["calls"]["matte_args"]
    assert len(args) == 1 and "--replace" in args[0] and "--apply" not in args[0]
    assert matte_step(r)["note"].startswith("would re-matte FRONT")
    assert r["cutouts"]["after"]["action"] == "pending"
    assert r["cutouts"]["after"]["blocks"] is False
    assert r["approval"]["outcome"] == "would_approve"
    assert r["approval"]["gate_code"] is None


def test_still_wrong_after_the_re_matte_holds_as_cutout_unfixable_in_block_mode(wired):
    wired["verdicts"][:] = [BAD_BLOCK, BAD_BLOCK]
    r = _repair(apply=True, approve=True)
    assert "STILL WRONG" in matte_step(r)["note"]
    assert r["approval"]["outcome"] == "gate_blocked"
    assert r["approval"]["gate_code"] == "CUTOUT_UNFIXABLE"
    assert any(b.startswith("cut-outs:") for b in r["approval"]["blockers"])
    # The move was withheld even under --apply --approve.
    assert wired["calls"]["approve"] == [{"apply": False}]
    verdict = outcome.classify(r)
    assert (verdict.status, verdict.outcome) == ("HELD_FOR_HUMAN", "CUTOUT_UNFIXABLE")


def test_still_wrong_after_the_re_matte_is_only_a_flag_while_soft(wired):
    wired["verdicts"][:] = [BAD, BAD]
    r = _repair(apply=True, approve=True)
    assert "soft" in matte_step(r)["note"]
    assert r["cutouts"]["after"]["blocks"] is False
    assert r["approval"]["outcome"] == "would_approve"
    assert wired["calls"]["approve"] == [{"apply": True}]


def test_a_failed_re_matte_leaves_the_measured_defect_on_record(wired, monkeypatch):
    wired["verdicts"][:] = [BAD_BLOCK, OK]

    def failing_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "backfill-bg-removal.ts":
            return False, "segmenter unreachable", None
        return True, "", {"ok": True, "applied": [], "failed": [], "results": []}

    monkeypatch.setattr(rp, "run_step", failing_run_step)
    r = _repair(apply=True, approve=True)
    assert matte_step(r)["ok"] is False
    assert r["cutouts"]["after"]["action"] == "bad"
    assert r["approval"]["outcome"] == "gate_blocked"


def test_no_cutout_and_nothing_unmatted_skips_the_step(wired):
    wired["states"][0] = _state(cutout_views=[], loaded={"record": {}, "media": [], "catalog": {}})
    r = _repair(apply=True)
    step = matte_step(r)
    assert step["ran"] is False and "no garment photograph" in step["why"]
    assert wired["calls"]["judge"] == 0


def test_needs_from_ignores_the_archived_originals():
    loaded = {"record": {"summary": "x"}, "media": [
        {"url": "a", "view": "FRONT", "processing": "BG_REMOVED", "mediaType": "IMAGE", "isCurrent": True},
        {"url": "b", "view": "FRONT", "processing": "RAW", "mediaType": "IMAGE", "isCurrent": False},
        {"url": "c", "view": "BACK", "processing": "RAW", "mediaType": "IMAGE", "isCurrent": True},
    ]}
    st = rp.needs_from(loaded)
    assert st["unmatted_views"] == ["BACK"] and st["leftover_raw"] == 0
    assert st["cutout_views"] == ["FRONT"]


def test_run_remote_sends_replace_as_a_typed_option(monkeypatch):
    sent: dict[str, Any] = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "output": "", "results": None}

    import httpx

    def fake_post(url, json, headers, timeout):
        sent.update(json)
        return Resp()

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    rp.run_remote("backfill-bg-removal.ts",
                  ["--db", DSN, "--product", PID, "--apply", "--provider", "hermes", "--replace"],
                  timeout_s=10, quiet=True)
    assert sent["step"] == "matte" and sent["apply"] is True
    assert sent["options"] == {"bgProvider": "hermes", "replace": True}
