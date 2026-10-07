"""The neck opening of a top's cut-out, finished (app/imaging/neckline.py), 7 Oct 2026.

MID-000351: on the PHOTOBOOTH photo the parser cut the white mannequin neck out of the
collar and the empty U read as a torn collar; on the DECISION photo it kept black studio
fragments and a piece of the hanger in the neckline.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import neckline  # noqa: E402

CURTAIN = (100, 100, 102)
TEAL = (20, 150, 190)
FORM = (215, 210, 214)
CFG = {**neckline.DEFAULTS, "enabled": True}
W, H = 600, 800


def _png(rgb, mask):
    b = io.BytesIO()
    Image.fromarray(np.dstack([rgb, np.where(mask, 255, 0).astype(np.uint8)]), "RGBA").save(b, "PNG")
    return b.getvalue()


def _jpeg(rgb):
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, "JPEG", quality=97)
    return b.getvalue()


def tee():
    """A T-shirt's silhouette with a U neck: the garment mask, and the neck opening."""
    m = np.zeros((H, W), np.uint8)
    pts = np.array([[60, 260], [200, 200], [250, 190], [350, 190], [400, 200], [540, 260],
                    [520, 360], [450, 330], [450, 740], [150, 740], [150, 330], [80, 360]], np.int32)
    cv2.fillPoly(m, [pts], 1)
    hole = np.zeros((H, W), np.uint8)
    cv2.ellipse(hole, (300, 190), (70, 75), 0, 0, 180, 1, -1)   # the U below the tips
    m[hole > 0] = 0
    return m > 0, hole > 0


def booth():
    """The photobooth: curtain, the form's neck rising through the collar, the tee."""
    rgb = np.full((H, W, 3), CURTAIN, np.uint8)
    body, hole = tee()
    neck = np.zeros((H, W), bool)
    neck[40:300, 225:375] = True
    rgb[neck] = FORM
    rgb[230:300, 225:245] = (130, 132, 140)                    # the form's shadowed side
    rgb[body] = TEAL
    return rgb, body, hole


def test_the_booth_form_goes_back_inside_the_collar():
    rgb, body, hole = booth()
    out, rep = neckline.finish(_jpeg(rgb), _png(rgb, body), origin="PHOTOBOOTH",
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert rep["done"] == "form", rep
    a = np.asarray(Image.open(io.BytesIO(out)).getchannel("A")) >= 128
    # The opening under the tips' line is full — its shadowed side too.
    assert a[hole & (np.arange(H)[:, None] > 196)].mean() > 0.97
    assert a[235:290, 230:240].all()
    # Nothing above the tips' line comes back: the form is cut flat at the collar.
    assert not a[60:180, 260:340].any()
    # The form's pixels are the photograph's.
    rgb_out = np.asarray(Image.open(io.BytesIO(out)).convert("RGB"))
    assert np.abs(rgb_out[250, 300].astype(int) - np.array(FORM)).max() < 12


def test_a_neckline_showing_the_studio_is_not_filled_with_it():
    """A boat neck on a narrow form: most of the opening is curtain."""
    rgb, body, hole = booth()
    rgb[hole & ~np.zeros_like(hole)] = CURTAIN
    rgb[40:300, 285:315] = FORM                                 # a narrow neck
    rgb[body] = TEAL
    out, rep = neckline.finish(_jpeg(rgb), _png(rgb, body), origin="PHOTOBOOTH",
                               garment="T-Shirts & Tops T-Shirts", cfg=CFG)
    assert rep["done"] is None and "studio" in rep["skip"]


def decision():
    """The decision panel: black studio, a white hanger piece and black fragments kept
    in the neckline by the parser."""
    rgb = np.full((H, W, 3), (20, 20, 22), np.uint8)
    body, hole = tee()
    rgb[body] = TEAL
    kept = body.copy()
    # The junk sits BEHIND the collar (the hanger is inside the garment), so it is only
    # ever seen — and painted — in the opening.
    frag = np.zeros((H, W), bool)
    frag[215:235, 270:300] = True                               # floating black spot
    frag &= ~body
    rgb[frag] = (25, 25, 28)
    hang = np.zeros((H, W), bool)
    hang[210:262, 330:356] = True                               # white hanger piece on the wall
    hang &= ~body
    rgb[hang] = (235, 235, 230)
    sliver = np.zeros((H, W), bool)
    sliver[205:262, 326:330] = True                             # its shadowed edge, teal-ish
    sliver &= ~body
    rgb[sliver] = (30, 120, 150)
    # The junk is what lies in the OPENING; where the hanger touches the collar, the
    # collar is garment and stays.
    return rgb, kept | frag | hang | sliver, body, (frag | hang | sliver) & ~body


def test_hanger_and_studio_fragments_are_cleared_from_a_hung_neckline():
    rgb, cut, body, junk = decision()
    out, rep = neckline.finish(_jpeg(rgb), _png(rgb, cut), origin="DECISION",
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert rep["done"] == "cleared", rep
    a = np.asarray(Image.open(io.BytesIO(out)).getchannel("A")) >= 128
    # JPEG edge noise aside: the junk is gone, the collar and the shirt are kept.
    assert (a & junk).sum() <= 0.005 * junk.sum()
    inner = cv2.erode(body.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    assert (inner & ~a).sum() <= 0.001 * inner.sum()


def test_a_clean_hung_neckline_is_left_alone():
    rgb = np.full((H, W, 3), (20, 20, 22), np.uint8)
    body, _hole = tee()
    rgb[body] = TEAL
    out, rep = neckline.finish(_jpeg(rgb), _png(rgb, body), origin="WEB",
                               garment="Sweaters & Hoodies Sweater", cfg=CFG)
    a = np.asarray(Image.open(io.BytesIO(out)).getchannel("A")) >= 128
    assert (a == body).mean() > 0.999


@pytest.mark.parametrize("garment", ["Bottoms Jeans", "Shoes Running", "Backpacks & Bags Bags"])
def test_never_bottoms_shoes_or_bags(garment):
    rgb, body, _hole = booth()
    png = _png(rgb, body)
    out, rep = neckline.finish(_jpeg(rgb), png, origin="PHOTOBOOTH", garment=garment, cfg=CFG)
    assert out == png and rep["done"] is None


def test_no_neck_dip_no_change():
    """A back view: the collar is the top of a convex outline, nothing to finish."""
    rgb = np.full((H, W, 3), CURTAIN, np.uint8)
    m = np.zeros((H, W), np.uint8)
    cv2.fillPoly(m, [np.array([[60, 260], [300, 180], [540, 260], [450, 740], [150, 740]], np.int32)], 1)
    rgb[m > 0] = TEAL
    png = _png(rgb, m > 0)
    out, rep = neckline.finish(_jpeg(rgb), png, origin="PHOTOBOOTH", garment="T-Shirts & Tops T-Shirts",
                               cfg=CFG)
    assert out == png and rep["done"] is None


def test_off_in_policy():
    rgb, body, _hole = booth()
    png = _png(rgb, body)
    out, rep = neckline.finish(_jpeg(rgb), png, origin="PHOTOBOOTH", garment="T-Shirts & Tops T-Shirts",
                               cfg={**CFG, "enabled": False})
    assert out == png


def test_the_shipped_policy_turns_it_on_for_the_booth():
    cfg = neckline.config()
    assert cfg["enabled"] and cfg["fill_origins"] == ["PHOTOBOOTH"]
    assert cfg["clear_origins"] == ["DECISION"]
    assert cfg["mannequin_origins"] == ["WEB", "MANUAL"]


@pytest.mark.parametrize("origin", ["WEB", "MANUAL", None])
def test_a_wall_photos_neckline_is_never_cleared(origin):
    """MID-000430 (7 Oct 2026): on the wall the opening shows the garment's own back collar
    band, wall-coloured; clearing cut it ragged and chopped the collar tips flat. Junk
    the decision panel's clearing would take stays — a hanger piece is no tear."""
    rgb, cut, _body, _junk = decision()
    png = _png(rgb, cut)
    out, rep = neckline.finish(_jpeg(rgb), png, origin=origin,
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert out == png and rep["done"] is None
    # WEB and MANUAL look for a mannequin first (7 Oct 2026); there is none here.
    assert ("no mannequin" in rep["skip"]) if origin else ("not finished" in rep["skip"])


# --- WEB photos taken in the booth: the form goes back only when it is there ----
#
# MID-000480 (7 Oct 2026): a Midtex WEB photo shot on the booth's mannequin showed the
# same hollow above the collar as MID-000351's photobooth one. The user's rule: put the
# mannequin's neck back only when a mannequin appears in the photo.

def test_a_web_photo_on_a_mannequin_gets_the_form_back():
    rgb, body, hole = booth()
    out, rep = neckline.finish(_jpeg(rgb), _png(rgb, body), origin="WEB",
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert rep["done"] == "form", rep
    assert rep["mannequin"]["fill"] >= 0.35 and rep["mannequin"]["columns"] >= 0.25
    a = np.asarray(Image.open(io.BytesIO(out)).getchannel("A")) >= 128
    assert a[hole & (np.arange(H)[:, None] > 196)].mean() > 0.97
    assert not a[60:180, 260:340].any()                        # cut flat at the collar


def test_a_web_photo_hung_on_the_wall_is_left_open():
    """The wall above the collar, and a hanger's thin hook: no neck, nothing put back."""
    rgb = np.full((H, W, 3), (232, 230, 226), np.uint8)
    body, hole = tee()
    rgb[60:200, 297:303] = (90, 90, 95)                        # the hook
    rgb[hole] = (232, 230, 226)                                # the wall through the opening
    rgb[body] = TEAL
    png = _png(rgb, body)
    out, rep = neckline.finish(_jpeg(rgb), png, origin="WEB",
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert out == png and rep["done"] is None and "no mannequin" in rep["skip"]
    assert rep["mannequin"]["columns"] < 0.25


def test_a_garment_filling_the_frame_cannot_show_a_neck():
    """OTR-000002: the sweater reaches the top of the photo — no room above the collar."""
    rgb, body, hole = booth()
    up = 170
    rgb = np.roll(rgb, -up, axis=0)
    body, hole = np.roll(body, -up, axis=0), np.roll(hole, -up, axis=0)
    png = _png(rgb, body)
    out, rep = neckline.finish(_jpeg(rgb), png, origin="WEB",
                               garment="T-Shirts & Polos Sports T-shirt", cfg=CFG)
    assert out == png and "no mannequin" in rep["skip"]
    assert "no room" in rep["mannequin"]["note"]


def test_the_chain_finishes_the_neck_only_after_the_checks_accept(monkeypatch):
    """Wired in cutout.remove_background: after every check, on the garment chain."""
    from app.imaging import cutout

    rgb, body, _hole = booth()
    calls = []
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (_png(rgb, body), None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda out: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda *a, **k: (calls.append("checked"), (True, "ok"))[1])
    monkeypatch.setattr(cutout, "_torn_garment", lambda *a, **k: (False, "ok"))
    monkeypatch.setattr(cutout, "_finish_neck",
                        lambda data, out, origin, garment, label: (calls.append(("finished", origin)), out)[1])
    cutout.remove_background(_jpeg(rgb), strategies=["cloth-seg-ft"], origin="PHOTOBOOTH",
                             garment="T-Shirts & Tops T-Shirts")
    assert calls == ["checked", ("finished", "PHOTOBOOTH")]
