"""The hung-bottoms refinement (app/imaging/refine.py + garment_cutout.py), 1 Oct 2026.

Bottoms on a hanger: the parser keeps the hanger bar and the clips, which the refinement
takes out; anything else — a top, dungarees, no hint — is left alone. The refined cut-out
is used only when it does no harm.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import policy  # noqa: E402
from app.imaging import cutout, refine  # noqa: E402

RCFG = refine.config({})
POL = policy()
_SHIPPED_CONFIG = refine.config
WALL = (205, 203, 200)
DENIM = (45, 75, 140)


def hung_shorts(seed: int = 3):
    """600x800: denim shorts hung from a hanger bar by two clips, on a grey wall.

    Returns (photo, the parser's mask — garment AND the hanger it wrongly kept, the
    garment alone)."""
    rng = np.random.default_rng(seed)
    arr = np.empty((800, 600, 3), np.float32)
    arr[:] = WALL
    garment = np.zeros((800, 600), bool)
    garment[200:600, 120:480] = True
    garment[470:600, 285:315] = False                  # between the legs
    arr[garment] = DENIM
    arr += rng.normal(0, 3, arr.shape)                 # texture, so nothing is flat
    hanger = np.zeros((800, 600), bool)
    hanger[183:191, 100:500] = True                    # the bar, above the waistband
    for x0 in (150, 430):
        hanger[176:206, x0:x0 + 22] = True             # clips, gripping the top edge
    arr[hanger] = (55, 55, 55)
    arr[120:183, 298:302] = (55, 55, 55)               # the hook
    hanger[120:183, 298:302] = True
    photo = np.clip(arr, 0, 255).astype(np.uint8)
    return photo, garment | hanger, garment


def png(arr, mask=None):
    if mask is not None:
        arr = np.dstack([arr, np.where(mask, 255, 0).astype(np.uint8)])
    b = io.BytesIO()
    Image.fromarray(arr).save(b, "PNG")
    return b.getvalue()


def rgba(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGBA"))


# --- which garments ----------------------------------------------------------

@pytest.mark.parametrize("garment", ["Bottoms Shorts", "Bottoms Jeans", "Bottoms Trousers",
                                     "Bottoms Skirts", "Bottoms"])
def test_bottoms_are_refined(garment):
    assert refine.wanted(garment, RCFG, POL)[0] is True


@pytest.mark.parametrize("garment,why", [
    ("Bottoms Dungarees", "dungarees"),                # the bib stands above the waistband
    ("Tops T-Shirts", "tops"),                         # the collar would be cut off
    ("Outerwear Jackets", "outerwear"),
    ("Tops Short Sleeve Shirts", "unknown"),           # two families: no guess
    (None, "no garment hint"),
    ("", "no garment hint"),
])
def test_everything_else_is_not(garment, why):
    ok, reason = refine.wanted(garment, RCFG, POL)
    assert ok is False and why in reason


def test_policy_can_turn_it_off():
    off = refine.config({"imagery": {"cutout": {"refine": False}}})
    assert refine.wanted("Bottoms Shorts", off, POL) == (False, "refinement is off")
    tighter = refine.config({"imagery": {"cutout": {"refine": {"max_lost_frac": 0.01}}}})
    assert tighter["max_lost_frac"] == 0.01 and tighter["enabled"] is True


def test_the_shipped_policy_has_it_off_hung_photos_go_to_hanger_isnet():
    """Since 1 Oct 2026 a photo hung on the wall is cut by `hanger-isnet`, which keeps
    the clips and removes no cloth; this refinement, which redraws the waistband,
    is off. Switched back on it still refines bottoms only."""
    cfg = refine.config()
    assert cfg["enabled"] is False
    assert cfg["families"] == ["bottoms"] and "dungarees" in cfg["skip_types"]


# --- the refinement itself -------------------------------------------------------

def test_the_hanger_the_parser_kept_is_taken_out():
    photo, kept, garment = hung_shorts()
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is not None, info
    a = rgba(out)[..., 3] >= 128
    assert a.shape == kept.shape
    assert not a[120:183, 298:302].any(), "the hook is still there"
    assert not a[183:191, 100:500].any(), "the hanger bar is still there"
    # the garment itself survives
    assert (a & garment).sum() / garment.sum() > 0.97
    assert info["refined"] is True and info["lost_frac"] <= RCFG["max_lost_frac"]


def test_nothing_shows_through_the_transparency():
    photo, kept, _ = hung_shorts()
    out, _ = refine.refine(png(photo), png(photo, kept), RCFG)
    px = rgba(out)
    clear = px[..., 3] == 0
    assert clear.any() and (px[clear][:, :3] == 255).all()


def test_a_different_size_is_not_refined():
    photo, kept, _ = hung_shorts()
    small = np.asarray(Image.fromarray(photo).resize((300, 400)))
    out, info = refine.refine(png(small), png(photo, kept), RCFG)
    assert out is None and "the photograph 300x400" in info["why"]


def test_a_script_failure_keeps_the_parsers_cut_out(monkeypatch):
    from app.imaging import garment_cutout as gc

    def boom(*a, **k):
        raise gc.NoWaistband("no waistband edge could be measured")

    monkeypatch.setattr(gc, "cutout", boom)
    photo, kept, _ = hung_shorts()
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is None and "NoWaistband" in info["why"]


def test_a_refinement_that_takes_away_garment_is_refused(monkeypatch):
    from app.imaging import garment_cutout as gc

    photo, kept, _ = hung_shorts()
    real = gc.cutout

    def greedy(raw, alpha=None, **k):
        out, rep = real(raw, alpha=alpha, **k)
        out = out.copy()
        out[200:400, ..., 3] = 0                        # a waistband line drawn far too low
        return out, rep

    monkeypatch.setattr(gc, "cutout", greedy)
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is None and "take away" in info["why"]


def test_fabric_cut_away_is_refused_even_under_the_loss_limit(monkeypatch):
    """BOA-001412: the waistband line dipped into the waistband — 2% lost in all,
    under max_lost_frac, but part of it what the photo shows as fabric."""
    from app.imaging import garment_cutout as gc

    photo, kept, _ = hung_shorts()
    real = gc.cutout

    def bite(raw, alpha=None, **k):
        out, rep = real(raw, alpha=alpha, **k)
        out = out.copy()
        out[200:260, 260:340, 3] = 0                     # a notch out of the front waistband
        return out, rep

    monkeypatch.setattr(gc, "cutout", bite)
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is None and "cut away fabric" in info["why"]
    assert info["lost_frac"] < RCFG["max_lost_frac"]


def test_the_hanger_taken_away_is_not_counted_as_fabric():
    photo, kept, garment = hung_shorts()
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is not None and info["fabric_taken"] <= RCFG["max_fabric_taken"]


def test_a_waistband_the_colour_of_the_wall_is_not_redrawn():
    """KIL-001216 FRONT: cream jeans on the white wall, the waistband cut flat."""
    rng = np.random.default_rng(5)
    photo, kept, garment = hung_shorts()
    pale = photo.astype(np.float32)
    pale[garment] = np.array(WALL, np.float32) + (4, 4, -2) + rng.normal(0, 3, (garment.sum(), 3))
    out, info = refine.refine(png(np.clip(pale, 0, 255).astype(np.uint8)), png(photo, kept), RCFG)
    assert out is None and "too close to the wall" in info["why"]


def test_it_runs_on_the_garments_neighbourhood_and_returns_the_full_frame(monkeypatch):
    from app.imaging import garment_cutout as gc

    photo, kept, _ = hung_shorts()
    big = np.empty((1600, 1200, 3), np.uint8)
    big[:] = WALL
    big[400:1200, 300:900] = photo                     # the same scene, far from the edges
    big_kept = np.zeros((1600, 1200), bool)
    big_kept[400:1200, 300:900] = kept
    seen = []
    real = gc.cutout

    def spy(raw, alpha=None, **k):
        seen.append(raw.shape[:2])
        return real(raw, alpha=alpha, **k)

    monkeypatch.setattr(gc, "cutout", spy)
    out, info = refine.refine(png(big), png(big, big_kept), RCFG)
    assert out is not None, info
    assert rgba(out).shape[:2] == (1600, 1200)
    h, w = seen[0]
    assert h < 1600 and w < 1200, "the script was handed the whole frame"
    assert not (rgba(out)[583:591, 400:900, 3] >= 128).any(), "the hanger bar is still there"


def test_a_refinement_that_makes_up_too_much_is_refused(monkeypatch):
    from app.imaging import garment_cutout as gc

    photo, kept, _ = hung_shorts()
    real = gc.cutout

    def inventive(raw, alpha=None, **k):
        out, rep = real(raw, alpha=alpha, **k)
        rep.generated_frac = 0.05
        return out, rep

    monkeypatch.setattr(gc, "cutout", inventive)
    out, info = refine.refine(png(photo), png(photo, kept), RCFG)
    assert out is None and "make up 5.0%" in info["why"]


def test_no_waistband_is_said_plainly():
    """BOA-006118 BACK: np.interp's 'array of sample points is empty' before."""
    from app.imaging import garment_cutout as gc

    flat = np.full((800, 600, 3), WALL, np.uint8)       # nothing differs from the wall
    alpha = np.zeros((800, 600), np.uint8)
    alpha[200:600, 120:480] = 255
    with pytest.raises(gc.NoWaistband):
        gc.cutout(flat, alpha=alpha)


# --- in the chain ------------------------------------------------------------------

def _chain(monkeypatch, cut):
    # Off in the shipped policy (hung photos go to hanger-isnet); on here.
    monkeypatch.setattr(refine, "config", lambda pol=None: {**_SHIPPED_CONFIG(pol), "enabled": True})
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (cut, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))


def test_the_chain_refines_bottoms_before_checking_them(monkeypatch):
    photo, kept, _ = hung_shorts()
    cut = png(photo, kept)
    _chain(monkeypatch, cut)
    seen = []
    monkeypatch.setattr(cutout, "_kept_backdrop",
                        lambda s, o, c=None: (seen.append(o), (True, "ok"))[1])
    report = {}
    out, err, provider = cutout.remove_background(
        png(photo), strategies=["cloth-seg-ft"], garment="Bottoms Shorts", report=report)
    assert provider == "cloth-seg-ft" and out != cut
    assert seen == [out], "the checks judged the refined cut-out"
    assert report["refine"]["refined"] is True
    assert not (rgba(out)[183:191, 100:500, 3] >= 128).any()


def test_the_chain_leaves_tops_and_unhinted_photos_alone(monkeypatch):
    photo, kept, _ = hung_shorts()
    cut = png(photo, kept)
    _chain(monkeypatch, cut)
    monkeypatch.setattr(refine, "refine", lambda *a, **k: pytest.fail("refined a top"))
    for garment in ("Tops T-Shirts", None):
        report = {}
        out, err, provider = cutout.remove_background(
            png(photo), strategies=["cloth-seg-ft"], garment=garment, report=report)
        assert out == cut and report.get("refine") is None


def test_a_failed_refinement_still_returns_the_parsers_cut_out(monkeypatch):
    photo, kept, _ = hung_shorts()
    cut = png(photo, kept)
    _chain(monkeypatch, cut)
    monkeypatch.setattr(refine, "refine", lambda *a, **k: (None, {"refined": False, "why": "x"}))
    report = {}
    out, err, provider = cutout.remove_background(
        png(photo), strategies=["cloth-seg-ft"], garment="Bottoms Jeans", report=report)
    assert out == cut and report["refine"] == {"refined": False, "why": "x"}
