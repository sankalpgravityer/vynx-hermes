"""Holes torn in the garment (cutout._torn_garment), 30 Sep 2026.

03520c1f's BACK: a black jumper with a green band, cut by v2 with white patches
through the green. A gap that shows the garment's colour in the photograph is
a tear and is refused; a gap that shows the wall (under an arm, through a neck)
is a real opening and is not.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout  # noqa: E402

CFG = cutout.config({})
WALL = (225, 223, 220)


def jumper():
    """A 300x400 photo: black body, green band, a wall gap between arm and body."""
    arr = np.zeros((400, 300, 3), np.uint8)
    arr[:] = WALL
    mask = np.zeros((400, 300), bool)
    arr[60:340, 70:230] = (25, 25, 28)
    mask[60:340, 70:230] = True
    arr[280:340, 70:230] = (40, 150, 80)               # the green band
    # the sleeve, with the wall showing between it and the body
    arr[80:300, 30:60] = (25, 25, 28)
    mask[80:300, 30:60] = True
    arr[80:90, 60:70] = (25, 25, 28)
    mask[80:90, 60:70] = True                          # joins sleeve to body at the top
    return arr, mask


def pair(arr, mask):
    raw = io.BytesIO()
    Image.fromarray(arr).save(raw, "JPEG", quality=95)
    rgba = np.dstack([arr, np.where(mask, 255, 0).astype(np.uint8)])
    cut = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(cut, "PNG")
    return raw.getvalue(), cut.getvalue()


def test_a_sound_cut_out_with_a_real_opening_is_not_torn():
    arr, mask = jumper()
    torn, why = cutout._torn_garment(*pair(arr, mask), CFG)
    assert torn is False, why


def test_holes_through_the_green_band_are_a_tear():
    arr, mask = jumper()
    mask = mask.copy()
    mask[295:315, 100:140] = False                     # white patches where the photo is green
    mask[300:320, 170:200] = False
    torn, why = cutout._torn_garment(*pair(arr, mask), CFG)
    assert torn is True and "tore" in why


def test_a_hole_that_shows_the_wall_is_an_opening_not_a_tear():
    arr, mask = jumper()
    arr, mask = arr.copy(), mask.copy()
    arr[150:200, 120:180] = WALL                       # the photo shows wall there too
    mask[150:200, 120:180] = False
    torn, _ = cutout._torn_garment(*pair(arr, mask), CFG)
    assert torn is False


def test_the_chain_moves_on_from_a_torn_cut_out(monkeypatch):
    """v2 torn -> v1 used; the torn one is never offered to the agreement rule."""
    arr, mask = jumper()
    raw, sound = pair(arr, mask)
    bad = mask.copy()
    bad[295:315, 100:140] = False
    _, torn = pair(arr, bad)
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (torn, None))
    monkeypatch.setattr(cutout, "_cloth_seg_ft_backup", lambda data: (sound, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    out, err, provider = cutout.remove_background(
        raw, strategies=["cloth-seg-ft", "cloth-seg-ft-backup"])
    assert provider == "cloth-seg-ft-backup" and out == sound


def _both_torn(monkeypatch):
    arr, mask = jumper()
    raw, _sound = pair(arr, mask)
    bad = mask.copy()
    bad[295:315, 100:140] = False
    _, torn = pair(arr, bad)
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (torn, None))
    monkeypatch.setattr(cutout, "_cloth_seg_ft_backup", lambda data: (torn, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    return arr, mask, raw


def test_when_both_parsers_tear_it_the_raw_photo_goes_to_gemini(monkeypatch):
    arr, mask, raw = _both_torn(monkeypatch)
    asked: list[str] = []
    _r, gemini_cut = pair(arr, mask)                   # Gemini's (sound) background removal

    def gemini(data, timeout_s, prompt=cutout.PROMPT):
        asked.append(prompt)
        return gemini_cut, None
    monkeypatch.setattr(cutout, "_gemini", gemini)
    out, err, provider = cutout.remove_background(
        raw, strategies=["cloth-seg-ft", "cloth-seg-ft-backup"])
    assert asked and asked[0] == cutout.PROMPT          # the background-removal prompt, not a mask
    assert provider == "gemini-paint" and out == gemini_cut


def test_when_gemini_cannot_either_nothing_replaces_the_existing_image(monkeypatch):
    _arr, _mask, raw = _both_torn(monkeypatch)
    monkeypatch.setattr(cutout, "_gemini", lambda data, timeout_s, prompt=None: (None, "no image"))
    out, err, provider = cutout.remove_background(
        raw, strategies=["cloth-seg-ft", "cloth-seg-ft-backup"])
    assert out is None and provider == "none" and "tore" in err
