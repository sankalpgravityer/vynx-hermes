"""
garment_cutout.py - clean garment cutouts from hanger photos, with occluded parts recovered.

    raw photo ──► segment (your model or IS-Net) ──► remove wall wire / specks
              ──► rebuild waistband top edge (hanger confuses every seg model here)
              ──► detect clips (+ jaws, bar end-caps) ──► hidden fabric mask
              ──► inpaint ONLY hidden pixels: label part from label pixels, denim part with LaMa
              ──► remove wall showing between fringe threads ──► edge colour decontamination
              ──► RGBA + report (how much was generated, needs_review flag)

Every pixel outside the hidden mask is the original photo. Nothing runs a generative model
over the whole image, so labels / size tags / defects can't be rewritten.

Usage
    from garment_cutout import cutout
    rgba, report = cutout(raw_rgb)                      # IS-Net (rembg) segmentation
    rgba, report = cutout(raw_rgb, alpha=your_alpha)    # your fine-tuned model's soft mask

CLI
    python garment_cutout.py raw.jpg out.png [--lama /path/big-lama.pt] [--white out.jpg]

Requirements: numpy opencv-contrib-python scipy pymatting
Optional:     rembg onnxruntime (default segmenter), torch + big-lama.pt (best inpainting;
              https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt)

IN HERMES (1 Oct 2026). Vendored as written; app/imaging/refine.py is what calls it, after
the fine-tuned parser's cut-out and before the checks, for hung bottoms only. Changes to the
original, each marked "HERMES:":
  - waistband_edge raises NoWaistband when no top edge can be measured, instead of
    np.interp's "array of sample points is empty" (BOA-006118 BACK).
  - Config.stop_if_over_generated (off by default) stops before the slow exemplar fill
    when more than max_generated_frac would be made up.
  - Report.waistband_contrast carries the contrast behind the "check the top edge" note,
    and Config.stop_if_contrast_below (off by default) stops there when it is too low.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

import cv2
import numpy as np
from scipy import ndimage as ndi


# ─────────────────────────────────────────────────────────────────────────────
# Config / report
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    crop_pad_frac: float = 0.10         # context around the garment for the 2nd seg pass
    crop_pad_bottom_frac: float = 0.20  # extra room below: fringe / threads hang down
    # waistband edge
    edge_search_above: int = 120
    edge_search_below: int = 160
    edge_keep_detail_px: float = 4.0
    edge_min_run: int = 24              # fabric must continue this many rows (hanger bar is thinner)    # measured edge kept if within this of the smooth fit
    # clips / hanger
    metal_max_chroma: float = 4.5
    metal_bright_L: float = 225
    metal_dark_L: float = 120
    clip_min_px: int = 500
    clip_min_above: float = 0.25        # clip must stick out above the waistband line
    # inpainting
    max_generated_frac: float = 0.02    # >2% of garment generated -> needs_review
    # HERMES: raise TooMuchHidden before inpainting when the hidden area is already over
    # max_generated_frac. The exemplar fill is the slow step (115 s on BOA-005343 FRONT,
    # 3.1% hidden) and Hermes refuses such a result anyway. Off for the CLI.
    stop_if_over_generated: bool = False
    # HERMES: raise LowContrast right after the waistband is measured when its contrast is
    # below this (the script's own "check the top edge" figure is 4). 0 = never (the CLI).
    stop_if_contrast_below: float = 0.0
    # fringe
    hem_start_frac: float = 0.62        # hem band starts at this fraction of garment height
    fringe_wall_ab: float = 3.5         # Lab a/b distance to wall colour
    fringe_wall_std: float = 4.5        # local L std: wall is smooth, threads have outlines


@dataclass
class Report:
    garment_px: int = 0
    generated_px: int = 0
    generated_frac: float = 0.0
    clips_found: int = 0
    label_found: bool = False
    label_pixels_generated: int = 0
    wire_lines_removed: int = 0
    inpaint_backend: str = ""
    needs_review: bool = False
    reasons: list = field(default_factory=list)
    # HERMES: the measured garment/wall contrast at the waistband (the "< 4: check the
    # top edge" figure), so a caller with no one to check can decide on it.
    waistband_contrast: float = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Segmentation (default: IS-Net through rembg; replace with your fine-tuned model)
# ─────────────────────────────────────────────────────────────────────────────
_SESS = None


def isnet_alpha(rgb: np.ndarray, cfg: Config) -> np.ndarray:
    """Two passes: full frame to find the garment, then a tight crop for resolution."""
    global _SESS
    from rembg import new_session, remove
    from PIL import Image
    if _SESS is None:
        _SESS = new_session("isnet-general-use")
    H, W = rgb.shape[:2]
    a1 = np.asarray(remove(Image.fromarray(rgb), session=_SESS, only_mask=True)) > 128
    body = cv2.morphologyEx(a1.astype(np.uint8), cv2.MORPH_OPEN, np.ones((25, 25), np.uint8))
    lb, n = ndi.label(body)
    sz = ndi.sum(body, lb, range(1, n + 1))
    ys, xs = np.where(lb == np.argmax(sz) + 1)
    gh = ys.max() - ys.min()
    p, pb = int(cfg.crop_pad_frac * gh), int(cfg.crop_pad_bottom_frac * gh)
    x0, y0, x1, y1 = max(0, xs.min() - p), max(0, ys.min() - p), min(W, xs.max() + p), min(H, ys.max() + pb)
    a2 = np.asarray(remove(Image.fromarray(rgb[y0:y1, x0:x1]), session=_SESS, only_mask=True))
    out = np.zeros((H, W), np.float32)
    out[y0:y1, x0:x1] = a2.astype(np.float32) / 255
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Inpainting backends
# ─────────────────────────────────────────────────────────────────────────────
_LAMA = None


def lama_inpaint(rgb: np.ndarray, mask: np.ndarray, model_path: str) -> np.ndarray:
    """Big-LaMa TorchScript. rgb uint8 HxWx3, mask bool -> uint8 (only mask pixels change)."""
    global _LAMA
    import torch
    if _LAMA is None:
        _LAMA = torch.jit.load(model_path, map_location="cpu").eval()
    H, W = mask.shape
    ph, pw = (8 - H % 8) % 8, (8 - W % 8) % 8
    img = np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="reflect")
    m = np.pad(mask, ((0, ph), (0, pw)))
    x = torch.from_numpy(img).permute(2, 0, 1)[None].float() / 255
    mt = torch.from_numpy(m.astype(np.float32))[None, None]
    with torch.inference_mode():
        y = _LAMA(x, mt)[0].permute(1, 2, 0).numpy()
    y = np.clip(y * 255, 0, 255).astype(np.uint8)[:H, :W]
    out = rgb.copy()
    out[mask] = y[mask]
    return out


def criminisi(img, hole, valid_src, patch=11, search=160, search_y=None):
    """Exemplar inpainting (Criminisi 2004). Copies real texture; used for the label and as
    a no-torch fallback. valid_src limits where patches may be copied from."""
    search_y = search if search_y is None else search_y
    img = img.astype(np.float32).copy()
    hole = hole.copy()
    H, W = hole.shape
    r = patch // 2
    C = (~hole).astype(np.float32)
    lab = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    full_ok = cv2.erode((valid_src & ~hole).astype(np.uint8), np.ones((patch, patch), np.uint8)) > 0
    while hole.any():
        front = np.argwhere(cv2.dilate((~hole).astype(np.uint8), np.ones((3, 3), np.uint8)).astype(bool) & hole)
        gray = cv2.cvtColor(img.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float32)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, 3); gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, 3)
        gx[hole] = 0; gy[hole] = 0
        m = hole.astype(np.float32)
        nx = cv2.Sobel(m, cv2.CV_32F, 1, 0, 3); ny = cv2.Sobel(m, cv2.CV_32F, 0, 1, 3)
        best, bp = -1, None
        for y, x in front:
            y0, y1, x0, x1 = max(0, y - r), min(H, y + r + 1), max(0, x - r), min(W, x + r + 1)
            conf = C[y0:y1, x0:x1].mean()
            gm = np.hypot(gx[y0:y1, x0:x1], gy[y0:y1, x0:x1])
            k = np.unravel_index(gm.argmax(), gm.shape)
            ix, iy = -gy[y0:y1, x0:x1][k], gx[y0:y1, x0:x1][k]
            nn = np.hypot(nx[y, x], ny[y, x]) + 1e-6
            pr = conf * (abs(ix * nx[y, x] / nn + iy * ny[y, x] / nn) / 255 + 0.001)
            if pr > best:
                best, bp = pr, (y, x, conf)
        y, x, conf = bp
        y0, y1, x0, x1 = max(0, y - r), min(H, y + r + 1), max(0, x - r), min(W, x + r + 1)
        tm = (~hole[y0:y1, x0:x1]).astype(np.uint8)
        sy0, sy1 = max(0, y - search_y), min(H, y + search_y)
        sx0, sx1 = max(0, x - search), min(W, x + search)
        res = cv2.matchTemplate(lab[sy0:sy1, sx0:sx1], lab[y0:y1, x0:x1], cv2.TM_SQDIFF, mask=np.dstack([tm] * 3))
        ok = np.zeros(res.shape, bool)
        okc = full_ok[sy0:sy1, sx0:sx1][(y - y0):(y - y0) + res.shape[0], (x - x0):(x - x0) + res.shape[1]]
        ok[:okc.shape[0], :okc.shape[1]] = okc
        res = np.where(ok, res, np.inf)
        if not np.isfinite(res).any():
            search *= 2; search_y *= 2
            if search > 4 * max(H, W):
                raise RuntimeError("criminisi: no valid source patch")
            continue
        my, mx = np.unravel_index(np.argmin(res), res.shape)
        hm = hole[y0:y1, x0:x1]
        img[y0:y1, x0:x1][hm] = img[sy0 + my:sy0 + my + (y1 - y0), sx0 + mx:sx0 + mx + (x1 - x0)][hm]
        lab[y0:y1, x0:x1][hm] = lab[sy0 + my:sy0 + my + (y1 - y0), sx0 + mx:sx0 + mx + (x1 - x0)][hm]
        C[y0:y1, x0:x1][hm] = conf
        hole[y0:y1, x0:x1][hm] = False
    return img.astype(np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _lab(rgb):
    lab = cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB).astype(np.float32)
    return lab[..., 0], lab[..., 1], lab[..., 2]


def _largest(m):
    lb, n = ndi.label(m)
    if n == 0:
        return m.copy()
    sz = ndi.sum(m, lb, range(1, n + 1))
    return lb == np.argmax(sz) + 1


def remove_wires(A, rep):
    """Long straight near-vertical lines (wall string, panel seams) the model kept."""
    solid = cv2.morphologyEx((A > 0.5).astype(np.uint8), cv2.MORPH_OPEN, np.ones((15, 15), np.uint8)) > 0
    thin = ((A > 0.08) & ~solid).astype(np.uint8) * 255
    lines = cv2.HoughLinesP(thin, 1, np.pi / 360, threshold=200, minLineLength=400, maxLineGap=40)
    wire = np.zeros(A.shape, np.uint8)
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):   # HERMES: OpenCV 5 returns (N, 4), 4 (N, 1, 4)
            if abs(x2 - x1) < 0.1 * abs(y2 - y1):
                # a wall wire is one straight line: extend the segment over the full height so the
                # short pieces visible between the legs are removed too
                sl = (x2 - x1) / (y2 - y1)
                H_ = A.shape[0]
                xa, xb = x1 + sl * (0 - y1), x1 + sl * (H_ - 1 - y1)
                cv2.line(wire, (int(round(xa)), 0), (int(round(xb)), H_ - 1), 1, 11)
                rep.wire_lines_removed += 1
    body = cv2.morphologyEx((A > 0.5).astype(np.uint8), cv2.MORPH_OPEN, np.ones((25, 25), np.uint8)) > 0
    A = A.copy()
    A[(wire > 0) & ~body] = 0
    return A, body


class NoWaistband(ValueError):
    """HERMES: the photo has no measurable top edge, so the rebuild cannot run."""


class TooMuchHidden(ValueError):
    """HERMES: more would be made up than max_generated_frac allows (see Config)."""


class LowContrast(ValueError):
    """HERMES: the waistband is too close to the wall's colour to redraw (see Config)."""


def wall_distance(L, a, b, ref):
    """Perceptual-ish distance to the wall colour; L down-weighted (wall shading varies)."""
    return np.sqrt((0.5 * (L - ref[0])) ** 2 + (a - ref[1]) ** 2 + (b - ref[2]) ** 2)


def waistband_edge(L, a, b, body, A, cfg):
    """Top edge where colour leaves the wall colour (measured per photo, so it works for
    light, grey, dark garments alike); robust cubic fit rejects hanger spikes."""
    H, W = L.shape
    bmain = _largest(body)
    ys, xs = np.where(bmain)
    gx0, gx1 = xs.min(), xs.max()
    # typical top of the garment = median first row over columns (clips / hanger stick up,
    # so the minimum would be the hanger, not the waistband)
    cols = np.flatnonzero(bmain.any(0))
    gy0 = int(np.median(bmain[:, cols].argmax(0)))
    r0, r1 = max(0, gy0 - cfg.edge_search_above), min(H, gy0 + cfg.edge_search_below)
    sl = (slice(r0, r0 + 30), slice(gx0, gx1))
    wall = np.array([np.median(L[sl]), np.median(a[sl]), np.median(b[sl])])
    fl = (slice(r1 - 40, r1), slice(gx0, gx1))
    fab = np.array([np.median(L[fl]), np.median(a[fl]), np.median(b[fl])])
    dw = cv2.GaussianBlur(wall_distance(L, a, b, wall), (0, 0), 2)
    wall_level = np.percentile(dw[sl], 90)
    fab_level = np.median(dw[fl])
    T = (wall_level + fab_level) / 2
    # fabric = away from the wall colour, nearer the garment colour than the wall, and not metal
    df = cv2.GaussianBlur(wall_distance(L, a, b, fab), (0, 0), 2)
    fab_L_low = float(np.percentile(L[_largest(body)], 2))
    chm = np.hypot(a - 128, b - 128)
    metal = (chm < cfg.metal_max_chroma) & ((L < min(cfg.metal_dark_L, fab_L_low - 10)) |
                                            (L > max(cfg.metal_bright_L, wall[0] + 20)))
    metal = cv2.dilate(metal.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    # colour alone fails when wall shadows look like light fabric; the model alone fails where
    # the hanger blurs it. Each is too permissive in different places, so require both.
    fabric = (dw > T) & (df < dw) & ~metal & (A > 0.5)
    n = cfg.edge_min_run
    run = ndi.uniform_filter1d(fabric[r0:r1].astype(np.float32), n, axis=0, origin=-(n // 2)) > 0.9
    top = np.full(W, np.nan)
    for x in range(gx0, gx1 + 1):
        idx = np.flatnonzero(run[:, x])
        if idx.size:
            top[x] = r0 + idx[0]
    info = dict(wall=wall, fab=fab, contrast=float(fab_level - wall_level),
                fab_L_low=fab_L_low)
    # columns with hanger metal near the top are unreliable: leave them out of the fit
    kk = max(15, int(0.015 * (gx1 - gx0)))
    mb = cv2.morphologyEx(metal[r0:r1].astype(np.uint8), cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kk, kk)))
    bad_cols = cv2.dilate(mb, np.ones((1, 61), np.uint8)).any(0)
    # the garment's real width at the waistband (skip hanger bar ends sticking out)
    gcols = np.flatnonzero(body[min(H - 1, gy0 + 60)])
    gl, gr = (gcols.min(), gcols.max()) if gcols.size else (gx0, gx1)
    top[:gl + 5] = np.nan; top[gr - 5:] = np.nan
    xi = np.arange(W)
    ok = ~np.isnan(top) & ~bad_cols
    ok[:gl] = False; ok[gr + 1:] = False
    if ok.sum() < 50:
        ok = ~np.isnan(top)
    # HERMES: no fabric run anywhere along the top (a garment the wall's colour, or nothing
    # in the band) means there is no waistband to rebuild; say so plainly.
    if ok.sum() < 3:
        raise NoWaistband("no waistband edge could be measured")
    # local spike rejection (hook, bar shadows): compare with a wide running median
    base = np.interp(xi, np.flatnonzero(ok), top[ok])
    rmed = ndi.median_filter(base, size=81)
    good = ok & (np.abs(top - rmed) < 8)
    if good.sum() < 3:
        raise NoWaistband("the waistband edge is too broken to fit")
    meas = np.interp(xi, np.flatnonzero(good), top[good])
    meas = ndi.median_filter(meas, size=15)
    gx_ = np.flatnonzero(good)
    cf_ref = np.polyfit(gx_, top[gx_], 2)
    # bridge each unreliable span (clips) straight across from robust neighbour heights
    edge = meas.copy()
    spans, nsp = ndi.label(~good & (xi >= gl) & (xi <= gr))
    for i in range(1, nsp + 1):
        sp = np.flatnonzero(spans == i)
        a0, b0 = sp[0], sp[-1]
        left = top[max(gl, a0 - 40):a0][good[max(gl, a0 - 40):a0]]
        right = top[b0 + 1:min(gr, b0 + 41)][good[b0 + 1:min(gr, b0 + 41)]]
        ya = np.median(left) if left.size else (np.median(right) if right.size else meas[a0])
        yb = np.median(right) if right.size else ya
        if abs(ya - yb) > 6:
            # neighbours disagree: trust the one that agrees with the overall waistband curve
            ref = np.polyval(cf_ref, (a0 + b0) / 2)
            ya = yb = ya if abs(ya - ref) < abs(yb - ref) else yb
        edge[sp] = np.interp(sp, [a0, b0], [ya, yb])
    edge = ndi.gaussian_filter1d(edge, 6)
    edge[:gl] = edge[gl]; edge[gr:] = edge[gr]       # flat beyond the garment
    return edge, (r0, r1, gx0, gx1), info


def detect_clips(L, a, b, ch, edge, extent, info, cfg):
    """Blobby metal that sticks up above the waistband line, grown into its grey jaw."""
    H, W = L.shape
    r0, r1, gx0, gx1 = extent
    dark_L = min(cfg.metal_dark_L, info["fab_L_low"] - 10)       # never call the garment itself metal
    bright_L = max(cfg.metal_bright_L, info["wall"][0] + 20)
    metal = (ch < cfg.metal_max_chroma) & ((L > bright_L) | (L < dark_L))
    # clips are striped (specular + dark jaw + grey); close first so the stripes form one blob
    metal = cv2.morphologyEx(metal.astype(np.uint8), cv2.MORPH_CLOSE,
                             cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15)))
    k = max(9, int(0.01 * (gx1 - gx0)))
    disc = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
    blobs = cv2.morphologyEx(metal, cv2.MORPH_OPEN, disc) > 0
    rows = np.arange(H)[:, None].astype(np.float32)
    zone = (rows > edge[None] - 200) & (rows < edge[None] + 120)
    zone[:, :max(0, gx0 - 60)] = False
    zone[:, gx1 + 60:] = False
    # jaw: grey, and clearly neither wall colour nor garment colour
    loose = ((cv2.GaussianBlur(ch, (0, 0), 1) < 6.0) & (wall_distance(L, a, b, info["wall"]) > 12)
             & (wall_distance(L, a, b, info["fab"]) > 12))
    # a clip's shiny head and dark jaw are often separate blobs: group vertically-close parts
    bz = blobs & zone
    lb, n = ndi.label(cv2.dilate(bz.astype(np.uint8), np.ones((41, 7), np.uint8)) > 0)
    clips = np.zeros((H, W), bool)
    count = 0
    for i in range(1, n + 1):
        c = (lb == i) & bz
        if c.sum() < cfg.clip_min_px:
            continue
        if (c & (rows < edge[None])).sum() / c.sum() < cfg.clip_min_above or not (c & (rows >= edge[None])).any():
            continue
        cy, cx = np.where(c)
        hh = cy.max() - cy.min()
        win = np.zeros_like(c)
        win[cy.min():cy.max() + int(0.9 * hh), max(0, cx.min() - k):cx.max() + k] = True
        cand = (loose & win) | ((cv2.dilate(c.astype(np.uint8), disc) > 0) & (metal > 0)) | c
        # connect across thin bright seams between the clip body and its jaw
        cl, _ = ndi.label(cv2.dilate(cand.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0)
        ids = np.unique(cl[c])
        part = np.isin(cl, ids[ids > 0])
        clips |= cv2.morphologyEx((part & cand).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8)) > 0
        count += 1
    return clips, count


def find_label(L, a, b):
    """Warm saturated leather patch -> its rectangle (includes any occluded corner)."""
    chroma = np.hypot(a - 128, b - 128)
    m = cv2.morphologyEx(((chroma > 14) & (b > 140)).astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
    if m.sum() < 2000:
        return np.zeros(L.shape, bool)
    core = _largest(m > 0)
    box = cv2.boxPoints(cv2.minAreaRect(np.argwhere(core)[:, ::-1].astype(np.float32))).astype(np.int32)
    rect = np.zeros(L.shape, np.uint8)
    cv2.fillPoly(rect, [box], 1)
    return rect > 0


def clean_fringe(rgb, A, cfg, rep=None):
    """Seg models fill the wall between hanging threads. Wall there is smooth and wall-coloured
    (Lab a/b); threads have darker outlines. Fade alpha only on wall-like pixels."""
    L, a, b = _lab(rgb)
    a = cv2.GaussianBlur(a, (0, 0), 1.5); b = cv2.GaussianBlur(b, (0, 0), 1.5)
    ys, _ = np.where(A > 0.5)
    y0 = int(ys.min() + cfg.hem_start_frac * (ys.max() - ys.min()))
    bg = A < 0.02
    bg[:y0] = False
    if bg.sum() < 1000:
        return A
    wa, wb, wL = np.median(a[bg]), np.median(b[bg]), np.median(L[bg])
    body = A > 0.9
    ga, gb = np.median(a[body]), np.median(b[body])
    if np.hypot(ga - wa, gb - wb) < 2 * cfg.fringe_wall_ab:
        # garment and wall share a hue: colour can't separate threads from wall -> don't touch
        if rep is not None:
            rep.reasons.append("fringe cleanup skipped (garment hue close to wall)")
        return A
    d = np.hypot(a - wa, b - wb)
    Ls = cv2.GaussianBlur(L, (0, 0), 1.2)
    Lstd = np.sqrt(np.maximum(cv2.GaussianBlur(L * L, (0, 0), 1.2) - Ls * Ls, 0))
    wl = (d < cfg.fringe_wall_ab) & (Ls > wL - 45) & (Lstd < cfg.fringe_wall_std) & (A > 0.03)
    wl[:y0] = False
    wl = cv2.morphologyEx(wl.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)) > 0
    # safety: keep only wall-like pixels that touch real background (never interior fabric)
    near_bg = cv2.dilate((A < 0.02).astype(np.uint8), np.ones((41, 41), np.uint8)) > 0
    lbw, nw = ndi.label(wl)
    ids = np.unique(lbw[wl & near_bg])
    wl = np.isin(lbw, ids[ids > 0])
    w = np.where(wl, 1.0, cv2.GaussianBlur(wl.astype(np.float32), (0, 0), 1.2))
    A = A * (1 - w)
    A[A < 0.05] = 0
    return A


def drop_specks(A):
    m = A > 0.2
    lb, n = ndi.label(m)
    if n <= 1:
        return A
    sz = ndi.sum(m, lb, range(1, n + 1))
    main_id = int(np.argmax(sz)) + 1
    dist = ndi.distance_transform_edt(lb != main_id)
    A = A.copy()
    for i in range(1, n + 1):
        if i != main_id and (sz[i - 1] < 80 or dist[lb == i].min() > 25):
            A[lb == i] = 0
    A[~(cv2.dilate((A > 0.2).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)] = 0
    return A


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def cutout(raw: np.ndarray,
           alpha: Optional[np.ndarray] = None,
           lama_path: Optional[str] = None,
           protect_mask: Optional[np.ndarray] = None,
           cfg: Config = Config()):
    """
    raw          HxWx3 uint8 RGB original photo
    alpha        optional HxW soft mask (0..1 float or 0..255 uint8) from your seg model
    lama_path    path to big-lama.pt; if None or torch missing -> exemplar fallback
    protect_mask optional HxW bool: never generate here (defects, care tag ...). If the
                 occlusion overlaps it, it is left as-is and the result is flagged.
    returns      (rgba uint8 HxWx4, Report)
    """
    rep = Report()
    H, W = raw.shape[:2]
    if alpha is None:
        alpha = isnet_alpha(raw, cfg)
    A = alpha.astype(np.float32) / (255 if alpha.dtype == np.uint8 else 1)
    L, la, lb_ = _lab(raw)
    ch = cv2.GaussianBlur(np.hypot(la - 128, lb_ - 128), (0, 0), 2)

    # 1. wires
    A, body = remove_wires(A, rep)

    # 2. waistband edge + alpha rebuild in the top band
    edge, extent, info = waistband_edge(L, la, lb_, body, A, cfg)
    rep.waistband_contrast = float(info["contrast"])
    if info["contrast"] < cfg.stop_if_contrast_below:
        raise LowContrast(f"garment/wall contrast at the waistband is {info['contrast']:.1f}")
    if info["contrast"] < 4:
        rep.reasons.append("low garment/wall contrast at the waistband: check the top edge")
    r0, r1, gx0, gx1 = extent
    hard = A > 0.5
    rows = np.arange(H)[:, None].astype(np.float32)
    soft = np.clip((rows - edge[None] + 0.5) / 1.5, 0, 1)
    band = np.zeros((H, W), bool)
    band[r0:r1 + 60, gx0:gx1 + 1] = True
    sidecols = np.zeros((H, W), bool)
    for y in range(r0, min(H, r1 + 60)):
        xx = np.flatnonzero(hard[min(y + 40, H - 1), gx0:gx1 + 1])
        if xx.size:
            sidecols[y, gx0 + xx.min():gx0 + xx.max() + 1] = True
    A = np.where(band & sidecols, np.minimum(A, soft), A)   # model alpha, capped by the edge line
    A[rows < edge[None] - 1] = 0      # for hung bottoms nothing above the waistband line is garment
    # hanger bar ends stick out sideways at waistband height: outside the garment's own width
    # (measured a little lower, padded) nothing in the top band is garment
    side_pad = cv2.dilate(sidecols.astype(np.uint8), np.ones((1, 17), np.uint8)) > 0
    topband = band & (rows < edge[None] + 40)
    A[topband & ~side_pad] = 0
    dark_metal = (ch < cfg.metal_max_chroma) & (L < min(95, info["fab_L_low"] - 15))
    A[topband & ~sidecols & (cv2.dilate(dark_metal.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0)] = 0

    # 3. clips, end caps -> hidden fabric
    clips, rep.clips_found = detect_clips(L, la, lb_, ch, edge, extent, info, cfg)
    occl = cv2.dilate(clips.astype(np.uint8), np.ones((17, 17), np.uint8)) > 0
    hidden = occl & (rows >= edge[None] - 1) & sidecols
    caps = (ch < 4.5) & (L < min(95, info["fab_L_low"] - 15)) & (rows >= edge[None] - 2) & (rows < edge[None] + 22) & sidecols
    caps = cv2.dilate(cv2.morphologyEx(caps.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8)),
                      np.ones((9, 9), np.uint8)) > 0
    hidden |= caps & (rows >= edge[None] - 1)
    hidden |= (cv2.dilate(clips.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0) & (A > 0.5)
    if protect_mask is not None and (hidden & protect_mask).any():
        rep.reasons.append("occlusion overlaps protected region; left unrepaired there")
        hidden &= ~protect_mask
    A[hidden] = np.maximum(A, soft)[hidden]

    # 4. specks, holes, opaque interior
    A = drop_specks(A)
    filled = ndi.binary_fill_holes(A > 0.5)
    A[cv2.erode(filled.astype(np.uint8), np.ones((9, 9), np.uint8)) > 0] = 1
    if cfg.stop_if_over_generated:
        share = float(hidden.sum()) / max(int((A > 0.5).sum()), 1)
        if share > cfg.max_generated_frac:
            raise TooMuchHidden(f"{share:.1%} of the garment is hidden and would be made up")

    # 5. inpaint hidden pixels only
    out = raw.copy()
    label = find_label(L, la, lb_)
    rep.label_found = bool(label.any())
    clipzone = cv2.dilate(clips.astype(np.uint8), np.ones((21, 21), np.uint8)) > 0
    hole_label = hidden & label
    hole_denim = hidden & ~label
    rep.label_pixels_generated = int(hole_label.sum())

    def regions(mask, pad):
        lab_, _ = ndi.label(cv2.dilate(mask.astype(np.uint8), np.ones((15, 15), np.uint8)))
        for ys, xs in ndi.find_objects(lab_):
            yield max(0, ys.start - pad), min(H, ys.stop + pad), max(0, xs.start - pad), min(W, xs.stop + pad)

    # label corner: copy only from the label itself
    for Y0, Y1, X0, X1 in regions(hole_label, 170):
        hh = hole_label[Y0:Y1, X0:X1]
        if hh.any():
            out[Y0:Y1, X0:X1] = criminisi(out[Y0:Y1, X0:X1], hh, (label & ~hidden & ~clipzone)[Y0:Y1, X0:X1],
                                          patch=9, search=150)
    # denim: LaMa with wall/hanger/label hidden from its context, exemplar fallback
    use_lama = lama_path is not None
    if use_lama:
        try:
            import torch  # noqa
        except Exception:
            use_lama = False
    rep.inpaint_backend = "lama" if use_lama else "exemplar"
    done = np.zeros((H, W), bool)
    for Y0, Y1, X0, X1 in regions(hole_denim, 0):
        cy, cx = (Y0 + Y1) // 2, (X0 + X1) // 2
        S = 512
        Y0, X0 = max(0, cy - S // 2), max(0, cx - S // 2)
        Y1, X1 = min(H, Y0 + S), min(W, X0 + S)
        h = (cv2.dilate(hole_denim[Y0:Y1, X0:X1].astype(np.uint8), np.ones((5, 5), np.uint8)) > 0)
        h &= ~done[Y0:Y1, X0:X1] & ~label[Y0:Y1, X0:X1]
        if not h.any():
            continue
        if use_lama:
            ctx = (A[Y0:Y1, X0:X1] < 0.5) & (cv2.dilate(h.astype(np.uint8), np.ones((61, 121), np.uint8)) > 0)
            ctx |= cv2.dilate(label[Y0:Y1, X0:X1].astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
            filled = lama_inpaint(out[Y0:Y1, X0:X1], h | ctx, lama_path)
        else:
            valid = (A > 0.97) & ~clipzone & ~(cv2.dilate(label.astype(np.uint8), np.ones((15, 15), np.uint8)) > 0)
            filled = criminisi(out[Y0:Y1, X0:X1], h, valid[Y0:Y1, X0:X1], patch=17, search=260, search_y=40)
        out[Y0:Y1, X0:X1][h] = filled[h]
        done[Y0:Y1, X0:X1] |= h

    # 6. fringe: wall between threads
    A = drop_specks(clean_fringe(raw, A, cfg, rep))

    # 7. edge colour decontamination (alpha-aware foreground estimate)
    from pymatting import estimate_foreground_ml
    ys, xs = np.where(A > 0)
    Y0, Y1, X0, X1 = max(0, ys.min() - 10), min(H, ys.max() + 10), max(0, xs.min() - 10), min(W, xs.max() + 10)
    I = out[Y0:Y1, X0:X1].astype(np.float64) / 255
    a = A[Y0:Y1, X0:X1].astype(np.float64)
    F = estimate_foreground_ml(I, a)
    F = np.where(a[..., None] > 0.98, I, F)
    rgb = out.copy()
    rgb[Y0:Y1, X0:X1] = (F * 255).round().astype(np.uint8)

    rep.garment_px = int((A > 0.5).sum())
    rep.generated_px = int(hidden.sum())
    rep.generated_frac = rep.generated_px / max(rep.garment_px, 1)
    if rep.generated_frac > cfg.max_generated_frac:
        rep.reasons.append(f"generated {rep.generated_frac:.1%} of garment")
    if rep.label_pixels_generated > 0:
        rep.reasons.append(f"{rep.label_pixels_generated} label pixels reconstructed - check label text")
    rep.needs_review = bool(rep.reasons)
    return np.dstack([rgb, (A * 255).round().astype(np.uint8)]), rep


def crop_to_content(rgba, pad_frac=0.06):
    ys, xs = np.where(rgba[..., 3] > 0)
    p = int(pad_frac * max(np.ptp(ys), np.ptp(xs)))
    H, W = rgba.shape[:2]
    return rgba[max(0, ys.min() - p):min(H, ys.max() + p), max(0, xs.min() - p):min(W, xs.max() + p)]


def on_background(rgba, color=(255, 255, 255)):
    a = rgba[..., 3:].astype(np.float32) / 255
    return (rgba[..., :3] * a + np.array(color, np.float32) * (1 - a)).round().astype(np.uint8)


if __name__ == "__main__":
    import argparse, json
    p = argparse.ArgumentParser()
    p.add_argument("raw"); p.add_argument("out_png")
    p.add_argument("--alpha", help="mask from your own model (png)")
    p.add_argument("--lama", help="path to big-lama.pt")
    p.add_argument("--white", help="also write a white-background jpg")
    args = p.parse_args()
    raw = cv2.cvtColor(cv2.imread(args.raw), cv2.COLOR_BGR2RGB)
    alpha = cv2.imread(args.alpha, cv2.IMREAD_GRAYSCALE) if args.alpha else None
    rgba, rep = cutout(raw, alpha, args.lama)
    rgba = crop_to_content(rgba)
    cv2.imwrite(args.out_png, cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    if args.white:
        cv2.imwrite(args.white, cv2.cvtColor(on_background(rgba), cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 95])
    print(json.dumps(asdict(rep), indent=2))
