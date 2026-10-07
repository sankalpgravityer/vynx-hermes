"""The garment parsers' edges, and white trim put back on wall photos (7 Oct 2026).

MID-000430: rembg resized the parser's 768x768 LABEL map to the photograph, so the
outline was a ~5 px staircase ("the boundary comes zig-zag"); and on the wall photo the
white back-collar band (the hanger's hook goes through it) and both white cuffs were cut
away against a white wall. The first fix for the trim read texture, took the sleeve's
shadow with it and left a ragged outline; the one tested here floods the wall up to the
photograph's edges (cutout._recover_trim).
"""
from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout  # noqa: E402

ECFG = {"enabled": True, "radius": 0.004, "eps": 1e-4, "band": [0.44, 0.56],
        "trim_origins": ["WEB", "MANUAL"], "trim_min_score": 0.12, "trim_wall_score": 0.02,
        "trim_edge": 10.0, "trim_gap": 0.008, "trim_ring": 0.05, "trim_max_add": 0.04,
        "trim_garment_de": 10.0, "trim_garment_share": 0.005, "trim_wall_de": 4.0,
        "trim_wall_de_max": 15.0, "trim_margin": 2.0, "trim_min_piece": 0.001, "trim_above": 0.01}
W, H = 800, 1000
WALL = (236, 236, 232)
DARK = (28, 28, 32)
COLLAR = (226, 226, 220)
RIB = (220, 220, 213)


def _rng():
    return np.random.default_rng(7)


def collar_mask():
    """The white V collar, its tips standing above the shoulders as MID-000430's do."""
    c = np.zeros((H, W), np.uint8)
    cv2.line(c, (300, 226), (400, 330), 1, 18)
    cv2.line(c, (500, 226), (400, 330), 1, 18)
    return c > 0


def body_mask():
    """A dark tee with a white V collar: the garment the parser kept."""
    m = np.zeros((H, W), np.uint8)
    pts = np.array([[120, 330], [300, 260], [500, 260], [680, 330], [640, 470], [580, 440],
                    [580, 900], [220, 900], [220, 440], [160, 470]], np.int32)
    cv2.fillPoly(m, [pts], 1)
    return (m > 0) | collar_mask()


def band_mask():
    """The white back-collar band seen between the collar's arms: an arc ~20 px thick,
    3.5% of the garment's width (MID-000430's: 45 px on 1320, 3.4%), below the tips."""
    b = np.zeros((H, W), np.uint8)
    cv2.ellipse(b, (400, 300), (110, 52), 0, 180, 360, 1, 20)
    b = (b > 0) & (np.arange(H)[:, None] < 300)
    return b & ~body_mask()


def scene(*, band=False, shadow=False, wire=False, pocket=False, clip=False):
    """(rgb, parser score, body mask) — the wall noisy as a photo's is, the score as the
    parser's: the body certain, the band half-seen, a blurred lean out into the wall.
    `pocket`: no band — the wall itself shows between the collar's arms, closed in by a
    hanger wire along where the band's top would be. `clip`: a light grey clip on the
    collar's tip, standing above the garment."""
    rng = _rng()
    rgb = np.empty((H, W, 3), np.float32)
    rgb[:] = WALL
    body = body_mask()
    raw_score = body * 0.95
    if shadow:
        # A soft shadow to the body's right: 10 L darker at the edge, gone 40 px out, no step.
        d = cv2.distanceTransform((~body).astype(np.uint8), cv2.DIST_L2, 5)
        right = np.arange(W)[None, :] > 560
        fall = np.clip(1 - d / 40.0, 0, 1) * right * ~body
        rgb -= (fall * 26)[..., None]
        raw_score = raw_score + fall * 0.45
    if band:
        b = band_mask()
        rgb[b] = RIB
        rgb[b] += rng.normal(0, 2.5, (int(b.sum()), 1))                  # rib knit
        top = b & ~(np.roll(b, 2, axis=0))                                # its top edge: a seam line
        rgb[top] = (196, 196, 192)
        raw_score = raw_score + b * 0.3
    if pocket:
        b = band_mask()
        top = b & ~(np.roll(b, 3, axis=0))
        rgb[top] = (120, 40, 40)                                          # the wire, the wall below it
        raw_score = raw_score + b * 0.3
    if clip:
        # Camouflaged and garment-coloured like the band (13 from the wall, 9 from the
        # collar): only where it stands tells it apart.
        cv2.rectangle(rgb, (478, 196), (522, 240), (200, 200, 196), -1)
        cv2.rectangle(rgb, (478, 196), (522, 240), (90, 90, 90), 2)
        cl_ = np.zeros((H, W), np.uint8)
        cv2.rectangle(cl_, (478, 196), (522, 240), 1, -1)
        raw_score = raw_score + (cl_ > 0) * 0.8
    if wire:
        cv2.line(rgb, (330, 262), (250, 120), (190, 40, 40), 6)          # a hanger wire off the shoulder
    rgb[body] = DARK
    rgb[collar_mask()] = COLLAR
    rgb += rng.normal(0, 0.8, rgb.shape)
    score = np.clip(cv2.GaussianBlur(raw_score.astype(np.float32), (0, 0), 12) + body * 0.95, 0, 1)
    return np.clip(rgb, 0, 255).astype(np.uint8), score.astype(np.float32), body


# --- the parser's own score, and its edge on the photograph ----------------- #

def test_the_score_is_half_where_the_argmax_changes():
    logits = np.zeros((1, 4, 768, 768), np.float32)
    logits[0, 0, :, :384] = 5.0           # background on the left
    logits[0, 2, :, 384:] = 5.0           # a garment class on the right
    logits[0, :, :, 380] = 0.0            # undecided: background = every class
    sess = SimpleNamespace(inner_session=SimpleNamespace(run=lambda _o, _f: [logits]),
                           normalize=lambda img, mean, std, size: {"x": None})
    s = cutout._cloth_score(sess, Image.new("RGB", (10, 10)))
    assert s.shape == (768, 768)
    assert s[10, 100] < 0.02 and s[10, 600] > 0.98
    assert s[10, 380] == pytest.approx(0.5, abs=1e-3)     # fg = max over classes, not their sum


def test_the_outline_follows_the_photograph_not_the_models_grid():
    """A diagonal edge: rembg's label-map resize draws stairs; the snapped score does not."""
    truth = np.zeros((H, W), np.uint8)
    cv2.fillPoly(truth, [np.array([[150, 100], [650, 180], [560, 900], [180, 860]], np.int32)], 1)
    rgb = np.where(truth[..., None] > 0, np.array(DARK), np.array(WALL)).astype(np.uint8)
    coarse = cv2.resize(truth.astype(np.float32), (96, 120), interpolation=cv2.INTER_AREA)
    alpha, _snapped = cutout._cloth_alpha(rgb, coarse, ECFG)
    stairs = cv2.resize((coarse >= 0.5).astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST)
    edge_zone = cv2.dilate(cv2.Canny(truth * 255, 50, 150), np.ones((5, 5), np.uint8)) > 0
    wrong_new = ((alpha >= 0.5) != (truth > 0))
    wrong_old = ((stairs > 0) != (truth > 0))
    assert not (wrong_new & ~edge_zone).any()             # within 2 px of the true edge everywhere
    assert wrong_new.sum() < 0.25 * wrong_old.sum()


def test_edge_pixels_take_the_garments_colour_not_the_walls():
    rgb = np.zeros((20, 20, 3), np.uint8)
    rgb[:, :10] = DARK
    rgb[:, 10:] = WALL
    alpha = np.zeros((20, 20), np.float32)
    alpha[:, :10] = 1.0
    alpha[:, 10] = 0.5
    out = cutout._decontaminate(rgb, alpha)
    assert tuple(out[5, 10]) == DARK
    assert tuple(out[5, 15]) == WALL                       # fully transparent: untouched


# --- white trim put back ----------------------------------------------------- #

def test_the_white_collar_band_behind_the_neck_is_put_back():
    rgb, score, body = scene(band=True)
    alpha = body.astype(np.float32)
    out, share = cutout._recover_trim(rgb, alpha, score, ECFG)
    band = band_mask()
    core = cv2.erode(band.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    assert share > 0
    assert (out[core] >= 0.5).mean() > 0.97
    near = cv2.dilate((band | body).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
    assert (out[~near] >= 0.5).sum() == 0                  # no wall with it


def test_the_put_back_bands_top_is_a_smooth_curve():
    """'The boundary comes zig-zag': the band's new top edge sits on the photograph's."""
    rgb, score, body = scene(band=True)
    out, _ = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG)
    band = band_mask()
    devs = []
    for x in range(300, 501, 4):
        col = np.where(band[:, x])[0]
        got = np.where(out[:, x] >= 0.5)[0]
        # Where it is a band: its tapering ends, thinner than a hanger wire, are not
        # told apart from one and stay out.
        if col.size >= 12 and got.size:
            devs.append(abs(int(got.min()) - int(col.min())))
    assert len(devs) > 20 and max(devs) <= 2 and np.mean(devs) <= 1.0


def test_a_shadow_on_the_wall_is_not_garment():
    """The first try's mistake: the shadow beside a sleeve, whose edge read as texture."""
    rgb, score, body = scene(shadow=True)
    out, share = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG)
    outside = cv2.dilate(body.astype(np.uint8), np.ones((7, 7), np.uint8)) == 0
    assert (out[outside] >= 0.5).sum() == 0
    assert share == 0


def test_a_hanger_wire_off_the_shoulder_is_not_put_back():
    rgb, score, body = scene(band=True, wire=True)
    out, _ = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG)
    wire = np.zeros((H, W), np.uint8)
    cv2.line(wire, (330, 262), (250, 120), 1, 6)
    far_part = (wire > 0) & (np.arange(H)[:, None] < 200)
    assert (out[far_part] >= 0.5).sum() == 0


def test_wall_closed_in_by_a_hanger_wire_is_not_garment():
    """The regression's commonest wrong answer: wall closed in by a wire, an arm, a hook —
    the flood cannot reach it, the parser leans toward it, and it is the wall's colour."""
    rgb, score, body = scene(pocket=True)
    out, share = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG)
    core = cv2.erode(band_mask().astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    assert (out[core] >= 0.5).sum() == 0 and share == 0


def test_a_clip_standing_above_the_garment_is_not_put_back():
    """Hardware rises above a hung garment; the band lies below the collar's tips."""
    rgb, score, body = scene(band=True, clip=True)
    notes: list = []
    out, share = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG, notes)
    assert (out[198:212, 482:518] >= 0.5).sum() == 0
    assert any(n.get("why") == "rises above the garment" for n in notes)
    core = cv2.erode(band_mask().astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    assert (out[core] >= 0.5).mean() > 0.97                # the band still comes back


def test_a_piece_the_garment_has_no_colour_for_is_not_put_back():
    """A dark tee with no white anywhere: a white band behind it is not told from the wall
    (MID-000430's BACK view, where the parser lost every white part, stays as it was)."""
    rgb, score, body = scene(band=True)
    rgb[collar_mask()] = DARK
    out, share = cutout._recover_trim(rgb, body.astype(np.float32), score, ECFG)
    assert share == 0


def test_more_than_the_cap_is_not_trim_and_nothing_is_added():
    rgb, score, body = scene(band=True)
    alpha = body.astype(np.float32)
    out, share = cutout._recover_trim(rgb, alpha, score, {**ECFG, "trim_max_add": 0.001})
    assert share == 0 and out is alpha


def _band_files():
    rgb, score, body = scene(band=True)
    src = io.BytesIO()
    Image.fromarray(rgb).save(src, "PNG")
    a = np.where(body, 255, 0).astype(np.uint8)
    cut = io.BytesIO()
    Image.fromarray(np.dstack([rgb, a]), "RGBA").save(cut, "PNG")
    return src.getvalue(), cut.getvalue(), score


def test_finishing_puts_the_trim_back_from_the_parsers_score_for_that_photo(monkeypatch):
    monkeypatch.setattr(cutout, "_cloth_edges_cfg", lambda: dict(ECFG))
    src, cut, score = _band_files()
    # No parse of this photo memoised: unchanged, byte for byte.
    assert cutout._finish_trim(src, cut, "cloth-seg", "t") == cut
    cutout._trim_memo_put(src, cutout._CLOTH_MODEL, score)
    out = cutout._finish_trim(src, cut, "cloth-seg", "t")
    got = np.asarray(Image.open(io.BytesIO(out)).getchannel("A"))
    core = cv2.erode(band_mask().astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    assert (got[core] >= 128).mean() > 0.97
    # …taken from the memo: a second finish of the same photo has nothing to use.
    assert cutout._finish_trim(src, cut, "cloth-seg", "t") == cut


def test_only_a_garment_parsers_cut_out_is_finished_and_only_from_its_own_score(monkeypatch):
    monkeypatch.setattr(cutout, "_cloth_edges_cfg", lambda: dict(ECFG))
    src, cut, score = _band_files()
    cutout._trim_memo_put(src, cutout._CLOTH_MODEL, score)
    assert cutout._finish_trim(src, cut, cutout.HANGER, "t") == cut          # IS-Net's: no score
    monkeypatch.setattr(cutout, "_CLOTH_FT_PATH", "v2.onnx")
    assert cutout._finish_trim(src, cut, "cloth-seg-ft", "t") == cut         # the stock parser's score
    assert cutout._finish_trim(src, cut, "cloth-seg", "t") != cut


def test_finishing_keeps_the_cut_outs_own_colours(monkeypatch):
    """A refined hung cut-out has painted where the clips were; only the trim put back
    takes the photograph's pixels."""
    monkeypatch.setattr(cutout, "_cloth_edges_cfg", lambda: dict(ECFG))
    src, cut, score = _band_files()
    arr = np.asarray(Image.open(io.BytesIO(cut))).copy()
    arr[600:640, 380:420, :3] = (10, 120, 10)                               # painted by an earlier step
    b = io.BytesIO()
    Image.fromarray(arr, "RGBA").save(b, "PNG")
    cutout._trim_memo_put(src, cutout._CLOTH_MODEL, score)
    out = np.asarray(Image.open(io.BytesIO(cutout._finish_trim(src, b.getvalue(), "cloth-seg", "t"))))
    assert tuple(out[620, 400, :3]) == (10, 120, 10)


def test_the_parser_memoises_its_score_for_the_photo(monkeypatch):
    rgb, _score, body = scene(band=True)
    logits = np.zeros((1, 2, 768, 768), np.float32)
    small = cv2.resize(body.astype(np.uint8), (768, 768), interpolation=cv2.INTER_NEAREST) > 0
    logits[0, 1][small] = 6.0
    logits[0, 0][~small] = 6.0
    sess = SimpleNamespace(inner_session=SimpleNamespace(run=lambda _o, _f: [logits]),
                           normalize=lambda img, mean, std, size: {"x": None})
    monkeypatch.setattr(cutout, "_cloth_session_for", lambda model: sess)
    monkeypatch.setattr(cutout, "_cloth_unavailable", None)
    src = io.BytesIO()
    Image.fromarray(rgb).save(src, "PNG")
    png, err = cutout._cloth_seg(src.getvalue())
    assert err is None and png
    key = cutout._trim_key(src.getvalue(), cutout._CLOTH_MODEL)
    assert key in cutout._TRIM_MEMO and cutout._TRIM_MEMO[key].shape == (768, 768)


# --- a top that reaches the floor kept its stand (7 Oct 2026) --------------------- #

def _alpha_png(mask):
    a = np.where(mask, 255, 0).astype(np.uint8)
    rgb = np.full(mask.shape + (3,), 40, np.uint8)
    b = io.BytesIO()
    Image.fromarray(np.dstack([rgb, a]), "RGBA").save(b, "PNG")
    return b.getvalue()


def _on_its_stand():
    """MID-000442's FRONT as Gemini cut it: the tank top, the pole above it, the form
    below and the podium running off the foot of the photograph."""
    m = body_mask().copy()
    m[40:260, 390:410] = True                     # the pole
    m[880:1000, 250:550] = True                   # the podium, cut by the frame
    return m


def test_a_cut_out_that_reaches_the_floor_kept_the_stand():
    stood, why = cutout._kept_stand(_alpha_png(_on_its_stand()), {})
    assert stood and "stand" in why


def test_a_clean_top_or_a_thread_at_the_foot_is_not_a_stand():
    assert cutout._kept_stand(_alpha_png(body_mask()), {}) == (False, "")
    thread = body_mask().copy()
    thread[900:1000, 400:404] = True              # 0.5% of the width: a loose string, not a podium
    assert cutout._kept_stand(_alpha_png(thread), {})[0] is False


@pytest.mark.parametrize("garment, refused", [("T-Shirts & Polos Tank Top", True),
                                              (None, True), ("Bottoms Jeans", False)])
def test_the_chain_refuses_a_top_on_its_stand_and_moves_on(monkeypatch, garment, refused):
    rgb, _score, body = scene()
    rgb[collar_mask()] = DARK
    src = io.BytesIO()
    Image.fromarray(rgb).save(src, "JPEG", quality=95)
    stand = np.dstack([rgb, np.where(_on_its_stand(), 255, 0).astype(np.uint8)])
    clean = np.dstack([rgb, np.where(body, 255, 0).astype(np.uint8)])
    pngs = {}
    for k, arr in (("stand", stand), ("clean", clean)):
        b = io.BytesIO()
        Image.fromarray(arr, "RGBA").save(b, "PNG")
        pngs[k] = b.getvalue()
    calls: list[str] = []
    monkeypatch.setattr(cutout, "_CLOTH_FT_PATH", "v2.onnx")
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (calls.append("v2"), (pngs["stand"], None))[1])
    monkeypatch.setattr(cutout, "_cloth_seg", lambda data: (calls.append("stock"), (pngs["clean"], None))[1])
    for name in ("_kept_backdrop",):
        monkeypatch.setattr(cutout, name, lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))
    out, err, provider = cutout.remove_background(src.getvalue(), timeout_s=1,
                                                  strategies=["cloth-seg-ft", "cloth-seg"], garment=garment)
    assert provider == ("cloth-seg" if refused else "cloth-seg-ft"), err
    assert calls == (["v2", "stock"] if refused else ["v2"])


# --- the chain finishes wall photos only -------------------------------------- #

@pytest.mark.parametrize("origin, garment, expect", [
    ("WEB", "T-Shirts & Polos Sports T-shirt", True), ("MANUAL", None, True),
    ("PHOTOBOOTH", "T-Shirts & Polos Sports T-shirt", False), (None, None, False),
    # Bottoms on a clip hanger: the wall between the bar and the waistband (MBF-000128).
    ("WEB", "Underware Panties", False), ("WEB", "Bottoms Jeans", False)])
def test_trim_is_put_back_on_wall_photos_of_tops_only(monkeypatch, origin, garment, expect):
    rgb, _score, body = scene()
    rgb[collar_mask()] = DARK       # a white collar on a white wall is for the leftover check
    src = io.BytesIO()
    Image.fromarray(rgb).save(src, "JPEG", quality=95)
    a = np.where(body, 255, 0).astype(np.uint8)
    png = io.BytesIO()
    Image.fromarray(np.dstack([rgb, a]), "RGBA").save(png, "PNG")
    seen: dict[str, object] = {}

    def fake_finish(data, out, name, label):
        seen["finished"] = name
        return out

    monkeypatch.setattr(cutout, "_cloth_seg", lambda data: (png.getvalue(), None))
    monkeypatch.setattr(cutout, "_finish_trim", fake_finish)
    monkeypatch.setattr(cutout, "_cloth_edges_cfg", lambda: dict(ECFG))
    out, err, provider = cutout.remove_background(src.getvalue(), timeout_s=1, strategies=["cloth-seg"],
                                                  origin=origin, garment=garment)
    assert provider == "cloth-seg", err
    assert seen.get("finished") == ("cloth-seg" if expect else None)
