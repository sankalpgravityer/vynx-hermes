"""The pixel-match framing test: a zoomed cut-out of a LIGHT garment on a LIGHT wall.

BOAS, 30 Sep 2026: 23 approved products with zoomed cut-outs went through a live
run as "every cut-out is on its canvas". The overlap test abstained (the garment
is nearly the wall's colour, separation < 90) and scale_outliers could not see
it (FRONT and BACK zoomed alike). The pixel match asks whether the cut-out's
garment pixels are the photograph's own, at the same place — no wall involved.
"""
from __future__ import annotations

import io
import random

from PIL import Image, ImageDraw

from app.imaging import cutouts

W, H = 600, 800
BACKDROP = (235, 235, 235)


def _photo(seed: int = 3) -> tuple[Image.Image, Image.Image]:
    """(the photograph, the garment's mask): a pale knit on a pale lit wall."""
    rng = random.Random(seed)
    img = Image.new("RGB", (W, H))
    px = img.load()
    for y in range(H):
        for x in range(W):
            v = 205 + int(20 * x / W) + rng.randint(-4, 4)          # a lit studio wall
            px[x, y] = (v, v, v - 4)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon([(200, 260), (400, 260), (430, 560), (170, 560)], fill=255)
    knit = Image.new("RGB", (W, H))
    kp = knit.load()
    for y in range(H):
        for x in range(W):
            # a textured pale garment: stripes and grain, close to the wall's colour
            v = 190 + (18 if (y // 9) % 2 else 0) + rng.randint(-10, 10)
            kp[x, y] = (v, v - 3, v - 8)
    img.paste(knit, mask=mask)
    return img, mask


def _encode(im: Image.Image, fmt: str = "PNG") -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt)
    return buf.getvalue()


def _honest_cutout(photo: Image.Image, mask: Image.Image) -> Image.Image:
    """The photograph's own pixels under the mask, composited on the backdrop."""
    out = Image.new("RGB", photo.size, BACKDROP)
    out.paste(photo, mask=mask)
    return out


def _zoomed(cut: Image.Image, factor: float = 1.6) -> Image.Image:
    """Cropped about the garment and scaled back up, same size and ratio."""
    cw, ch = int(W / factor), int(H / factor)
    x0, y0 = (W - cw) // 2, (410 - ch // 2)
    return cut.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.Resampling.LANCZOS)


def _cfg():
    from app.config import policy
    return cutouts.config(policy())


def test_the_old_overlap_test_abstains_on_this_photograph():
    """The premise: this garment is too close to its wall for `overlap` to judge."""
    photo, mask = _photo()
    m = cutouts.garment_hole(_encode(_zoomed(_honest_cutout(photo, mask))), _encode(photo, "JPEG"), _cfg())
    assert m is not None and m["separation"] < 90


def test_an_honest_cut_out_matches_its_photograph():
    photo, mask = _photo()
    m = cutouts.garment_hole(_encode(_honest_cutout(photo, mask)), _encode(photo, "JPEG"), _cfg())
    assert m["pixel_match"] > 0.9, m
    why, _fixable = cutouts.frame_problem(m, _cfg(), derived=True)
    assert why is None


def test_a_zoomed_cut_out_is_caught_where_overlap_abstains_and_can_be_re_cut():
    photo, mask = _photo()
    m = cutouts.garment_hole(_encode(_zoomed(_honest_cutout(photo, mask))), _encode(photo, "JPEG"), _cfg())
    assert m["pixel_match"] < 0.5, m
    why, fixable = cutouts.frame_problem(m, _cfg(), derived=True)
    assert why and "does not line up" in why and fixable


def test_without_a_derivation_edge_it_is_a_note_not_a_defect():
    photo, mask = _photo()
    m = cutouts.garment_hole(_encode(_zoomed(_honest_cutout(photo, mask))), _encode(photo, "JPEG"), _cfg())
    why, fixable = cutouts.frame_problem(m, _cfg(), derived=False)
    assert why and not fixable and "different photographs" in why


def test_a_brightened_honest_cut_out_still_matches():
    """An enhance that only lifts brightness or contrast is not a re-framing."""
    photo, mask = _photo()
    cut = _honest_cutout(photo, mask).point(lambda v: min(255, int(v * 1.15 + 5)))
    m = cutouts.garment_hole(_encode(cut), _encode(photo, "JPEG"), _cfg())
    assert m["pixel_match"] > 0.9, m


def test_it_can_be_switched_off():
    photo, mask = _photo()
    m = cutouts.garment_hole(_encode(_zoomed(_honest_cutout(photo, mask))), _encode(photo, "JPEG"), _cfg())
    off = {**_cfg(), "garment_check": {**(_cfg().get("garment_check") or {}), "pixel_match_min": 0}}
    why, _f = cutouts.frame_problem(m, off, derived=True)
    assert why is None or "pixel match" not in why
