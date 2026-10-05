"""
hanger_clean_cutout.py - remove the hanger from the PHOTO, then cut out the garment.

    raw ─► segment (IS-Net / BiRefNet) ─► hanger mask: thin parts at the top only
        ─► LaMa inpaints the hanger out of the photo ─► segment the cleaned photo
        ─► merge: outside the hanger mask nothing the first pass called garment is dropped

Design rule: this script never removes cloth.
  * Only THIN structures (bar, hook, wire, clip arms) that touch the hanger bar or sit above
    the garment are masked. Tags, labels, belt loops and the waistband are wider than the
    thin limit and are never touched. Clips may stay on the garment (by design).
  * Outside the hanger mask the final alpha is max(first pass, second pass), so the second
    segmentation can only ADD garment, never take it away.
  * Every image gets a report; anything unusual is flagged instead of "fixed".

Usage
    python hanger_clean_cutout.py photo.jpg out_dir/
    python hanger_clean_cutout.py photos/ out_dir/ --model birefnet-general   # folder, GPU model
Outputs per photo: <name>.png (RGBA), <name>_white.jpg, <name>_debug.jpg, <name>.json

Requirements: numpy opencv-python scipy pillow rembg onnxruntime(-gpu) torch
Optional:     pymatting (edge colour clean-up)
LaMa weights: downloaded automatically to --lama-path if missing
              (https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt)

IN HERMES (3 Oct 2026: the quality revision — half-scale LaMa, guided-filter edges, the
photo's own canvas and colour profile). Vendored as written; app/imaging/cutout.py calls
`cutout()` below as the `hanger-isnet` strategy, for photographs hung on the wall (not the
photobooth, whose podium and stand the fine-tuned cloth-seg is trained to remove).
Changes, each marked "HERMES:":
  - `segmenter()` / `lama()` cache the IS-Net session and the LaMa model across calls.
  - LaMa is optional: without torch or the weights file, the hanger is painted out with
    OpenCV's Telea inpainting (`telea`). Nothing is downloaded inside a request.
  - `cutout(raw)` returns (rgba, report) for one photo, for the strategy to call.
  - HoughLinesP results are reshaped to (N, 4): OpenCV 5 (installed here) returns (N, 4),
    OpenCV 4 (N, 1, 4); `lines[:, 0]` crashed on the first photo with a bar in it.
  - A bar candidate must be part of what the first segmentation kept: the wall's pencil
    guide lines above the hanger have wall on both sides, pass `wall_beside`, and were
    being taken for the bar (BLM-001025 FRONT).
  - Inside the hanger mask, pixels the inpainting made the wall's colour are dropped
    (`_drop_painted_wall`): IS-Net kept painted wall next to the clips.
  - Config.stop_without_bar (off by default) returns right after the bar search when
    there is no bar, skipping the inpainting and the 2nd pass.
  - `refine_alpha` falls back to a guided filter written with cv2.boxFilter
    (`_guided_filter`) when OpenCV has no ximgproc (opencv-python, as installed here) —
    without it the edge refinement silently did nothing.
"""
from __future__ import annotations

import argparse
import gc
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import cv2
import numpy as np
from scipy import ndimage as ndi

LAMA_URL = "https://github.com/Sanster/models/releases/download/add_big_lama/big-lama.pt"


# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Config:
    model: str = "isnet-general-use"      # any rembg model: birefnet-general, isnet-general-use ...
    crop_pad_frac: float = 0.10           # context around the garment for the 2nd seg pass
    crop_pad_bottom_frac: float = 0.20    # extra room below for fringe / hanging threads
    thin_frac: float = 0.02               # "thin" = narrower than 2% of garment width (min 15 px)
    top_band_frac: float = 0.06           # hanger parts must start above top + 6% of height
    mask_grow_frac: float = 0.025         # grow hanger mask 2.5% of width (covers shadows/edges)
    lama_margin: int = 160                # context around each inpaint region
    remove_wall_wire: bool = True         # straight vertical wall wire between the legs
    speck_max_frac: float = 0.0002        # drop tiny blobs (<0.02% of garment) ...
    speck_min_dist_frac: float = 0.03     # ... only if they are far from the garment
    edge_colour_cleanup: bool = True      # pymatting foreground estimation on soft edges
    max_hanger_on_garment_frac: float = 0.03   # review flag if mask covers >3% of garment
    lama_scale: float = 0.5               # LaMa works at ~512px anyway: inpaint at half res, paste
                                          # back only the hanger pixels (mostly wall) -> ~4x faster
    refine_edges: bool = True             # guided filter: sharpen model alpha at full photo res
    keep_canvas: bool = True              # output same width x height as the input photo
    # HERMES: stop right after the bar search when no bar is found — the caller does
    # not use a cut-out without one, and the inpainting and the 2nd pass are most of
    # the time. Off for the CLI.
    stop_without_bar: bool = False


@dataclass
class Report:
    file: str = ""
    seconds: float = 0.0
    bar_found: bool = False
    hanger_px: int = 0
    hanger_on_garment_px: int = 0
    garment_px_first: int = 0
    garment_px_final: int = 0
    garment_px_lost_outside_mask: int = 0     # must be 0: proof that no cloth was removed
    wire_lines_removed: int = 0
    specks_removed: int = 0
    needs_review: bool = False
    reasons: list = field(default_factory=list)
    inpaint_backend: str = ""                 # HERMES: "lama" or "telea"


# ─────────────────────────────────────────────────────────────────────────────
# Segmentation
# ─────────────────────────────────────────────────────────────────────────────
class Segmenter:
    def __init__(self, model: str):
        from rembg import new_session
        self.session = new_session(model)

    def _mask(self, rgb):
        from PIL import Image
        from rembg import remove
        return np.asarray(remove(Image.fromarray(rgb), session=self.session, only_mask=True)).astype(np.float32) / 255

    def __call__(self, rgb, cfg: Config, box=None):
        """Full frame to locate the garment, then a tight crop for resolution.
        Pass the box from the 1st pass to skip the full-frame step on the 2nd pass."""
        H, W = rgb.shape[:2]
        if box is None:
            a1 = self._mask(rgb) > 0.5
            body = _largest(cv2.morphologyEx(a1.astype(np.uint8), cv2.MORPH_OPEN, np.ones((25, 25), np.uint8)) > 0)
            if not body.any():
                return self._mask(rgb), (0, 0, W, H)
            ys, xs = np.where(body)
            gh = ys.max() - ys.min()
            p, pb = int(cfg.crop_pad_frac * gh), int(cfg.crop_pad_bottom_frac * gh)
            box = (max(0, xs.min() - p), max(0, ys.min() - p), min(W, xs.max() + p), min(H, ys.max() + pb))
        x0, y0, x1, y1 = box
        out = np.zeros((H, W), np.float32)
        out[y0:y1, x0:x1] = self._mask(rgb[y0:y1, x0:x1])
        return out, box


# ─────────────────────────────────────────────────────────────────────────────
# LaMa
# ─────────────────────────────────────────────────────────────────────────────
class Lama:
    def __init__(self, path: str):
        import torch
        if not os.path.exists(path):
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            torch.hub.download_url_to_file(LAMA_URL, path)
        self.torch = torch
        torch.set_num_threads(max(1, os.cpu_count() or 1))
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = torch.jit.load(path, map_location=self.device).eval()

    def __call__(self, rgb, mask):
        """rgb uint8 HxWx3, mask bool HxW -> uint8 with ONLY mask pixels changed."""
        torch = self.torch
        H, W = mask.shape
        ph, pw = (8 - H % 8) % 8, (8 - W % 8) % 8
        img = np.pad(rgb, ((0, ph), (0, pw), (0, 0)), mode="reflect")
        m = np.pad(mask, ((0, ph), (0, pw)))
        x = torch.from_numpy(img).permute(2, 0, 1)[None].float().div(255).to(self.device)
        mt = torch.from_numpy(m.astype(np.float32))[None, None].to(self.device)
        with torch.inference_mode():
            y = self.model(x, mt)[0].permute(1, 2, 0).cpu().numpy()
        y = np.clip(y * 255, 0, 255).astype(np.uint8)[:H, :W]
        out = rgb.copy()
        out[mask] = y[mask]
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _largest(m):
    lb, n = ndi.label(m)
    if n == 0:
        return np.zeros_like(m, bool)
    return lb == (np.argmax(ndi.sum(m, lb, range(1, n + 1))) + 1)


def _disc(k):
    k = int(k) | 1
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))


def garment_geometry(A):
    """Main body, its typical top row (median over columns, so clips/hook don't count), size."""
    hard = A > 0.3
    body = _largest(cv2.morphologyEx(hard.astype(np.uint8), cv2.MORPH_OPEN, _disc(61)) > 0)
    ys, xs = np.where(body)
    cols = np.flatnonzero(body.any(0))
    top = int(np.median(body[:, cols].argmax(0)))
    return hard, body, top, ys.max() - ys.min(), xs.max() - xs.min()


def hanger_mask(A, raw, cfg: Config, rep: Report):
    """Thin structures at the top that belong to the hanger. Never wide parts (tags, loops)."""
    H, W = A.shape
    hard, body, top, gh, gw = garment_geometry(A)
    _ys, _xs = np.where(body); xs_min, xs_max = _xs.min(), _xs.max()
    k = max(15, int(cfg.thin_frac * gw))
    thin = hard & ~(cv2.morphologyEx(hard.astype(np.uint8), cv2.MORPH_OPEN, _disc(k)) > 0)
    rows = np.arange(H)[:, None]
    band_bottom = top + int(cfg.top_band_frac * gh)
    thin &= rows < band_bottom

    # the hanger bar, found in the PHOTO (not the mask): a long, thin, dark, near-horizontal
    # line around the waistband top. Black-hat with a tall kernel lights up thin dark lines.
    # (In the mask the bar often merges with the waistband, so it doesn't look "thin" there.)
    L = cv2.cvtColor(raw, cv2.COLOR_RGB2LAB)[..., 0].astype(np.float32)
    r0, r1 = max(0, top - int(0.15 * gh)), min(H, band_bottom)
    bh = cv2.morphologyEx(L[r0:r1], cv2.MORPH_BLACKHAT, np.ones((31, 1), np.uint8))
    dark = (bh > 25).astype(np.uint8) * 255
    # colour work only on the top part of the photo (keeps memory low)
    rb = min(H, band_bottom + int(0.05 * gh))
    lab_ = np.zeros((H, W, 3), np.float32)          # rows below rb are never read
    lab_[:rb] = cv2.cvtColor(cv2.GaussianBlur(raw[:rb], (9, 9), 0), cv2.COLOR_RGB2LAB).astype(np.float32)
    wall_ref = np.median(lab_[: max(10, top // 3)].reshape(-1, 3), axis=0)
    dwall = np.full((H, W), 1e3, np.float32)
    dwall[:rb] = np.sqrt((0.5 * (lab_[:rb, :, 0] - wall_ref[0])) ** 2 + (lab_[:rb, :, 1] - wall_ref[1]) ** 2
                         + (lab_[:rb, :, 2] - wall_ref[2]) ** 2)
    gap_zone = np.zeros((H, W), bool)
    bar = None
    lines = cv2.HoughLinesP(dark, 1, np.pi / 720, 120, minLineLength=int(0.4 * gw), maxLineGap=80)
    if lines is not None:
        def inside_body(l):
            n_ = 50
            xx = np.linspace(l[0], l[2], n_).astype(int)
            yy = np.clip(np.linspace(l[1], l[3], n_).astype(int) + r0, 0, H - 1)
            return body[yy, xx].mean()
        def wall_beside(l, off=14):
            # fraction of the line with wall colour right above or below it: true for a hanger
            # bar, false for stitching/seams (garment on both sides)
            n_ = 50
            xx = np.linspace(l[0], l[2], n_).astype(int)
            yy = np.linspace(l[1], l[3], n_).astype(int) + r0
            hits = 0
            for x_, y_ in zip(xx, yy):
                for dy in (-off, off):
                    yv = int(np.clip(y_ + dy, 0, H - 1))
                    d = lab_[yv, x_] - wall_ref
                    if np.sqrt((0.5 * d[0]) ** 2 + d[1] ** 2 + d[2] ** 2) < 12:
                        hits += 1
                        break
            return hits / n_

        def on_object(l):
            # HERMES: the share of the line the 1st segmentation kept. The real bar is
            # part of what IS-Net cut out; a pencil guide line drawn on the wall is wall
            # (it has wall on both sides, so `wall_beside` alone passes it).
            n_ = 50
            xx = np.linspace(l[0], l[2], n_).astype(int)
            yy = np.clip(np.linspace(l[1], l[3], n_).astype(int) + r0, 0, H - 1)
            return hard[yy, xx].mean()

        # HERMES: reshape, not lines[:, 0] — OpenCV 5 returns (N, 4), OpenCV 4 (N, 1, 4)
        cand = [l for l in lines.reshape(-1, 4)
                if abs(int(l[3]) - int(l[1])) < 0.08 * abs(int(l[2]) - int(l[0]))
                and (inside_body(l) < 0.3 or wall_beside(l) > 0.6)   # seams: garment both sides
                and on_object(l) > 0.5]                              # HERMES: not a pencil line
        if cand:
            # the bar is the topmost long line (seams and hems are lower)
            l = min(cand, key=lambda q: (int(q[1]) + int(q[3])) / 2 - 0.05 * abs(int(q[2]) - int(q[0])))
            bar = (int(l[0]), int(l[1]) + r0, int(l[2]), int(l[3]) + r0)
    rep.bar_found = bar is not None
    bar_px = np.zeros((H, W), np.uint8)
    bar_core = np.zeros((H, W), bool)
    below_limit = np.zeros((H, W), bool)
    if bar is not None:
        x1, y1, x2, y2 = bar
        # extend to the full hanger width (ends are often lighter / capped)
        s_ = (y2 - y1) / max(1, x2 - x1)
        xa, xb = max(0, xs_min - int(0.15 * gw)), min(W - 1, xs_max + int(0.15 * gw))
        pa, pb = (xa, int(round(y1 + s_ * (xa - x1)))), (xb, int(round(y1 + s_ * (xb - x1))))
        # bar thickness from the dark response along the line
        cols = np.linspace(min(x1, x2), max(x1, x2), 40).astype(int)
        th = []
        for c in cols:
            yc = int(round(y1 + s_ * (c - x1))) - r0
            seg_ = dark[max(0, yc - 20):yc + 21, c] > 0
            if seg_.any():
                th.append(seg_.sum())
        t = int(np.clip(np.median(th) if th else 9, 5, 30))
        cv2.line(bar_px, pa, pb, 1, t + 6)
        # real bar pixels: dark thin-line response or thin foreground inside a wide band around
        # the fitted line (bars sag and end caps sit a little off the straight fit)
        band_ = np.zeros((H, W), np.uint8)
        cv2.line(band_, pa, pb, 1, t + 50)
        dark_full = np.zeros((H, W), bool)
        dark_full[r0:r1] = dark > 0
        bar_core = (band_ > 0) & (dark_full | thin | ((bar_px > 0) & hard))
        # the garment hangs BELOW the bar: never go more than a few px below the bar line
        # (the segmentation's "body" can swallow the bar, so it is not used as the limit here)
        xx = np.arange(W)
        line_y = y1 + s_ * (xx - x1)
        below_limit = np.arange(H)[:, None] > (line_y[None, :] + t / 2 + 14)
        bar_core &= ~below_limit
        # wall strip between the bar and the waistband: segmentation often fills it in.
        # Only wall-coloured pixels there are handed to the 2nd pass (fabric never is).
        rows_ = np.arange(H)[:, None]
        under_bar = (rows_ > line_y[None, :]) & (rows_ < line_y[None, :] + t / 2 + 0.03 * gh)
        under_bar[:, :max(0, min(x1, x2) - 20)] = False
        under_bar[:, max(x1, x2) + 20:] = False
        gap_zone = under_bar & (dwall < 12)
        # where the bar lies over the waistband edge it is still removed (only its own
        # thickness); the cleaned photo + 2nd pass decide what was behind it. The line itself
        # was chosen to lie mostly OUTSIDE the garment, so seams/stitching are never picked.
    # keep only thin parts that touch the bar or lie wholly above the garment top
    # (a drawstring or strap hanging from the waist touches neither -> kept as garment)
    lb, n = ndi.label(thin)
    keep = np.zeros((H, W), bool)
    for i, sl in enumerate(ndi.find_objects(lb), start=1):
        comp = lb[sl] == i
        touches_bar = bar_px[sl][comp].any()
        above_top = sl[0].stop <= top + 5
        if touches_bar or above_top:
            keep[sl] |= comp
    keep |= bar_core
    if bar is None:
        rep.reasons.append("hanger bar not found: only parts above the garment were removed")

    grow = max(15, int(cfg.mask_grow_frac * gw))
    mask = cv2.dilate(keep.astype(np.uint8), _disc(grow)) > 0
    # the grown mask may cover wall and shadows, but never wide garment parts (tags, labels,
    # waistband, belt loops, clips): those keep their original pixels
    wide = cv2.morphologyEx(hard.astype(np.uint8), cv2.MORPH_OPEN, _disc(k)) > 0
    wide = cv2.erode(wide.astype(np.uint8), _disc(5)) > 0
    near_thin = cv2.dilate(keep.astype(np.uint8), _disc(9)) > 0
    mask &= ~(wide & ~near_thin)
    # the bar band itself is always masked, even where it passes through a clip: any bar
    # fragment left visible makes LaMa redraw the whole bar from it
    if bar_core.any():
        grown_bar = cv2.dilate(bar_core.astype(np.uint8), _disc(13)) > 0
        mask |= grown_bar & ~below_limit
    return mask, body, top, gap_zone


def inpaint_regions(rgb, mask, lama: Lama, margin, max_side=1024, scale=1.0):
    """Inpaint each mask region with context, optionally at reduced scale (only the masked
    pixels are replaced, so the rest of the photo keeps full original quality). Long regions
    are done in overlapping tiles; each tile hides ALL masked pixels it contains (otherwise
    LaMa sees the rest of the bar in the context and redraws it)."""
    out = rgb.copy()
    H, W = mask.shape
    lb, n = ndi.label(cv2.dilate(mask.astype(np.uint8), np.ones((31, 31), np.uint8)))
    side = int(max_side / scale)                  # tile size in full-res pixels
    m_ = int(margin / scale) if scale < 1 else margin       # same context at the reduced scale
    for sl in ndi.find_objects(lb):
        y0, y1 = max(0, sl[0].start - m_), min(H, sl[0].stop + m_)
        x0, x1 = max(0, sl[1].start - m_), min(W, sl[1].stop + m_)
        step = max(64, side - 2 * m_)
        ty = list(range(y0, y1, step)) if (y1 - y0) > side else [y0]
        tx = list(range(x0, x1, step)) if (x1 - x0) > side else [x0]
        for cy in ty:
            for cx in tx:
                cy1 = y1 if len(ty) == 1 else min(y1, cy + step)
                cx1 = x1 if len(tx) == 1 else min(x1, cx + step)
                sel_core = np.zeros((H, W), bool)
                sel_core[cy:cy1, cx:cx1] = True
                m_core = mask & sel_core
                if not m_core.any():
                    continue
                Y0, Y1 = max(0, cy - m_), min(H, cy1 + m_)
                X0, X1 = max(0, cx - m_), min(W, cx1 + m_)
                tile, tm = out[Y0:Y1, X0:X1], mask[Y0:Y1, X0:X1]
                if scale < 1:
                    hs, ws = max(8, int(tile.shape[0] * scale)), max(8, int(tile.shape[1] * scale))
                    small = cv2.resize(tile, (ws, hs), interpolation=cv2.INTER_AREA)
                    sm = cv2.resize(cv2.dilate(tm.astype(np.uint8), np.ones((3, 3), np.uint8)),
                                    (ws, hs), interpolation=cv2.INTER_NEAREST) > 0
                    filled = cv2.resize(lama(small, sm), (tile.shape[1], tile.shape[0]),
                                        interpolation=cv2.INTER_CUBIC)
                else:
                    filled = lama(tile, tm)
                sel = m_core[Y0:Y1, X0:X1]
                out[Y0:Y1, X0:X1][sel] = filled[sel]
    return out


def remove_wall_wire(A, body, rep):
    """Long straight vertical lines outside the garment body (wall wire between the legs)."""
    H, W = A.shape
    solid = cv2.morphologyEx((A > 0.5).astype(np.uint8), cv2.MORPH_OPEN, np.ones((15, 15), np.uint8)) > 0
    thin = ((A > 0.08) & ~solid).astype(np.uint8) * 255
    lines = cv2.HoughLinesP(thin, 1, np.pi / 360, 200, minLineLength=400, maxLineGap=40)
    wire = np.zeros((H, W), np.uint8)
    if lines is not None:
        for x1, y1, x2, y2 in lines.reshape(-1, 4):          # HERMES: OpenCV 4 and 5 shapes
            if abs(int(x2) - int(x1)) < 0.05 * abs(int(y2) - int(y1)):
                s = (int(x2) - int(x1)) / (int(y2) - int(y1))
                xa, xb = x1 + s * (0 - y1), x1 + s * (H - 1 - y1)
                cv2.line(wire, (int(round(xa)), 0), (int(round(xb)), H - 1), 1, 11)
                rep.wire_lines_removed += 1
    A = A.copy()
    A[(wire > 0) & ~body] = 0
    return A


def drop_far_specks(A, cfg, rep):
    m = A > 0.2
    lb, n = ndi.label(m)
    if n <= 1:
        return A
    sz = ndi.sum(m, lb, range(1, n + 1))
    main = int(np.argmax(sz)) + 1
    dist = ndi.distance_transform_edt(lb != main)
    ys, _ = np.where(lb == main)
    min_d = cfg.speck_min_dist_frac * (ys.max() - ys.min())
    A = A.copy()
    for i in range(1, n + 1):
        if i == main or sz[i - 1] > cfg.speck_max_frac * sz[main - 1]:
            continue
        if dist[lb == i].min() > min_d:          # tiny AND far away: wall dots, not threads
            A[lb == i] = 0
            rep.specks_removed += 1
    return A


def drop_floating_above(A, body, top, rep):
    """Blobs not connected to the garment that lie entirely above its top (hook tips, bar
    ends). On a hung garment nothing detached can be above the waistband."""
    m = A > 0.03                      # include faint ghosts (soft hook / wire remnants)
    lb, n = ndi.label(m)
    if n <= 1:
        return A
    main_ids = np.unique(lb[body & m])
    A = A.copy()
    for i, sl in enumerate(ndi.find_objects(lb), start=1):
        if i in main_ids:
            continue
        if sl[0].stop <= top + 5:
            A[sl][lb[sl] == i] = 0
            rep.specks_removed += 1
    return A


def _guided_filter(guide, src, radius, eps):
    """HERMES: He et al.'s guided filter (grey guide), the same call shape as
    cv2.ximgproc.guidedFilter, with box filters only — for OpenCV builds without the
    contrib modules. guide and src float32 in [0, 1]."""
    k = (2 * int(radius) + 1, 2 * int(radius) + 1)

    def box(x):
        return cv2.boxFilter(x, cv2.CV_32F, k, normalize=True, borderType=cv2.BORDER_REFLECT)

    I, p = guide.astype(np.float32), src.astype(np.float32)
    mean_I, mean_p = box(I), box(p)
    cov_Ip = box(I * p) - mean_I * mean_p
    var_I = box(I * I) - mean_I * mean_I
    a = cov_Ip / (var_I + eps)
    b = mean_p - a * mean_I
    return box(a) * I + box(b)


def refine_alpha(rgb, A):
    """The models predict alpha at ~1024 px and it is upscaled to the photo, which softens
    edges. A guided filter with the full-res photo as guide snaps the edge back onto the real
    garment boundary. Only the edge band changes; solid interior and empty background stay."""
    try:
        gf = cv2.ximgproc.guidedFilter
    except AttributeError:
        gf = _guided_filter        # HERMES: opencv-python has no ximgproc
    ys, xs = np.where(A > 0.02)
    if ys.size == 0:
        return A
    H, W = A.shape
    y0, y1, x0, x1 = max(0, ys.min() - 20), min(H, ys.max() + 20), max(0, xs.min() - 20), min(W, xs.max() + 20)
    a = A[y0:y1, x0:x1].astype(np.float32)
    guide = cv2.cvtColor(rgb[y0:y1, x0:x1], cv2.COLOR_RGB2GRAY).astype(np.float32) / 255
    r = gf(guide, a, 4, 1e-3)
    band = (cv2.dilate((a > 0.02).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0) & \
           ~(cv2.erode((a > 0.98).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0)
    out = A.copy()
    sub = out[y0:y1, x0:x1]
    sub[band] = np.clip(r[band], 0, 1)
    return out


def edge_colour_cleanup(rgb, A):
    try:
        from pymatting import estimate_foreground_ml
    except ImportError:
        return rgb
    ys, xs = np.where(A > 0)
    H, W = A.shape
    y0, y1, x0, x1 = max(0, ys.min() - 10), min(H, ys.max() + 10), max(0, xs.min() - 10), min(W, xs.max() + 10)
    I = rgb[y0:y1, x0:x1].astype(np.float64) / 255
    a = A[y0:y1, x0:x1].astype(np.float64)
    F = estimate_foreground_ml(I, a)
    F = np.where(a[..., None] > 0.98, I, F)
    out = rgb.copy()
    out[y0:y1, x0:x1] = (F * 255).round().astype(np.uint8)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def process(raw, seg: Segmenter, lama: Lama, cfg: Config = Config()):
    """raw: HxWx3 uint8 RGB. Returns rgba, report, debug dict."""
    rep = Report()
    t = time.time()

    A0, box = seg(raw, cfg)                             # 1st pass: garment + hanger
    mask, body, top, gap_zone = hanger_mask(A0, raw, cfg, rep)    # 2. thin hanger parts only
    if cfg.stop_without_bar and not rep.bar_found:      # HERMES: see Config
        rep.seconds = round(time.time() - t, 1)
        empty = np.zeros(raw.shape[:2] + (4,), np.uint8)
        return empty, rep, dict(raw=raw, mask=mask, clean=raw, A0=A0, A=A0)
    clean = inpaint_regions(raw, mask, lama, cfg.lama_margin, scale=cfg.lama_scale) if mask.any() else raw.copy()
    A2, _ = seg(clean, cfg, box=box)                    # 3. 2nd pass on the cleaned photo

    # 4. merge - outside the hanger mask the 2nd pass can only ADD garment
    trust2 = mask | gap_zone
    A = np.where(trust2, A2, np.maximum(A0, A2)).astype(np.float32)
    lost_merge = ((A0 > 0.5) & ~trust2 & (A <= 0.5)).sum()   # 0 by construction
    # HERMES: inside the hanger mask, what the inpainting turned into WALL is wall,
    # whatever the 2nd pass says. Next to a clip IS-Net kept a patch of freshly
    # painted wall as part of the object (BOA-001263 BACK). Fabric the bar lay over
    # is painted fabric-coloured and is not touched by this.
    if mask.any():
        A = _drop_painted_wall(raw, clean, A0, A, mask, top)
    if cfg.remove_wall_wire:
        A = remove_wall_wire(A, body, rep)
    A = drop_far_specks(A, cfg, rep)
    A = drop_floating_above(A, body, top, rep)
    solid_near = cv2.dilate((A > 0.5).astype(np.uint8), _disc(15)) > 0
    faint_above = (np.arange(A.shape[0])[:, None] < top - 5) & (A < 0.5) & ~solid_near
    A[faint_above] = 0

    if cfg.refine_edges:
        A = refine_alpha(clean, A)
    rgb = edge_colour_cleanup(clean, A) if cfg.edge_colour_cleanup else clean

    g0 = A0 > 0.5
    rep.garment_px_first = int((g0 & ~mask).sum())
    rep.garment_px_final = int((A > 0.5).sum())
    rep.hanger_px = int(mask.sum())
    rep.hanger_on_garment_px = int((mask & body).sum())
    rep.garment_px_lost_outside_mask = int(lost_merge)
    wire_speck_px = int((g0 & ~mask & (A <= 0.5)).sum())
    if rep.hanger_on_garment_px > cfg.max_hanger_on_garment_frac * max(1, body.sum()):
        rep.reasons.append("hanger mask covers a lot of garment: check the waistband")
    if rep.garment_px_lost_outside_mask > 0:
        rep.reasons.append("merge dropped garment pixels (should never happen)")
    if wire_speck_px > 0.01 * max(1, rep.garment_px_first):
        rep.reasons.append(f"wire/speck rules removed {wire_speck_px} px: check between the legs")
    rep.needs_review = bool(rep.reasons)
    rep.seconds = round(time.time() - t, 1)
    rgba = np.dstack([rgb, (np.clip(A, 0, 1) * 255).round().astype(np.uint8)])
    return rgba, rep, dict(raw=raw, mask=mask, clean=clean, A0=A0, A=A)


def _drop_painted_wall(raw, clean, A0, A, mask, top, max_dist=12.0):
    """HERMES: alpha 0 for hanger-mask pixels the inpainting made the wall's colour.
    The wall's colour is measured on the photo around the hanger, where the first
    pass saw no object."""
    H, W = A.shape
    ys, xs = np.where(mask)
    y0, y1 = max(0, ys.min() - 120), min(H, ys.max() + 120)
    x0, x1 = max(0, xs.min() - 120), min(W, xs.max() + 120)
    lab_raw = cv2.cvtColor(np.ascontiguousarray(raw[y0:y1, x0:x1]), cv2.COLOR_RGB2LAB).astype(np.float32)
    zone = (A0[y0:y1, x0:x1] < 0.05) & ~(cv2.dilate(mask[y0:y1, x0:x1].astype(np.uint8),
                                                    np.ones((15, 15), np.uint8)) > 0)
    if zone.sum() < 200:
        return A
    wall = np.median(lab_raw[zone], axis=0)
    lab = cv2.cvtColor(np.ascontiguousarray(clean[y0:y1, x0:x1]), cv2.COLOR_RGB2LAB).astype(np.float32)
    painted_wall = mask[y0:y1, x0:x1] & (np.sqrt(((lab - wall) ** 2).sum(-1)) < max_dist)
    A = A.copy()
    A[y0:y1, x0:x1][painted_wall] = 0
    return A


# ─────────────────────────────────────────────────────────────────────────────
# HERMES: one photo in, (rgba, report) out, with the models cached
# ─────────────────────────────────────────────────────────────────────────────
_SEGMENTERS: dict = {}
_LAMAS: dict = {}


def segmenter(model: str) -> Segmenter:
    """HERMES: one rembg session per model for the life of the process."""
    if model not in _SEGMENTERS:
        _SEGMENTERS[model] = Segmenter(model)
    return _SEGMENTERS[model]


def telea(rgb, mask):
    """HERMES: OpenCV inpainting, the stand-in for LaMa. Same contract: only mask pixels
    change. Fine for the thin bar and hook on a plain wall; LaMa is better where the bar
    crosses patterned fabric (Telea leaves a grey smear there)."""
    filled = cv2.inpaint(np.ascontiguousarray(rgb), mask.astype(np.uint8), 7, cv2.INPAINT_TELEA)
    out = rgb.copy()
    out[mask] = filled[mask]
    return out


def lama(path: str | None):
    """HERMES: the cached LaMa model, or None when torch or the weights are missing.
    Never downloads: a request must not wait on a 200 MB fetch."""
    if not path or not os.path.isfile(path):
        return None
    if path not in _LAMAS:
        try:
            import torch  # noqa: F401
        except Exception:  # noqa: BLE001
            _LAMAS[path] = None
        else:
            _LAMAS[path] = Lama(path)
    return _LAMAS[path]


def cutout(raw, cfg: Config | None = None, lama_path: str | None = None):
    """HERMES: the garment cut out of one upright RGB photo, on the photo's own canvas.
    Returns (rgba, report)."""
    cfg = cfg or Config()
    model = lama(lama_path)
    rgba, rep, _dbg = process(raw, segmenter(cfg.model), model or telea, cfg)
    rep.inpaint_backend = "lama" if model else "telea"
    return rgba, rep


def crop_to_content(rgba, pad_frac=0.06):
    ys, xs = np.where(rgba[..., 3] > 0)
    p = int(pad_frac * max(np.ptp(ys), np.ptp(xs)))
    H, W = rgba.shape[:2]
    return rgba[max(0, ys.min() - p):min(H, ys.max() + p), max(0, xs.min() - p):min(W, xs.max() + p)]


def on_white(rgba):
    a = rgba[..., 3:].astype(np.float32) / 255
    return (rgba[..., :3] * a + 255 * (1 - a)).round().astype(np.uint8)


def debug_board(d, h=900):
    raw, mask, clean, A0, A = d["raw"], d["mask"], d["clean"], d["A0"], d["A"]
    ys, xs = np.where(A > 0.5)
    pad = int(0.08 * (ys.max() - ys.min()))
    y0, y1 = max(0, ys.min() - 3 * pad), min(raw.shape[0], ys.max() + pad)
    x0, x1 = max(0, xs.min() - pad), min(raw.shape[1], xs.max() + pad)
    ov = raw.copy()
    ov[mask] = (ov[mask] * 0.4 + np.array([255, 0, 0]) * 0.6).astype(np.uint8)
    comp = lambda img, a: (img * a[..., None] + 255 * (1 - a[..., None])).astype(np.uint8)
    tiles = [(ov, "hanger mask"), (clean, "cleaned photo"), (comp(raw, A0), "1st pass (raw)"),
             (comp(clean, A), "final")]
    out = []
    for img, t in tiles:
        c = img[y0:y1, x0:x1]
        c = cv2.resize(c, (int(c.shape[1] * h / c.shape[0]), h), interpolation=cv2.INTER_AREA)
        bar = np.full((50, c.shape[1], 3), 255, np.uint8)
        cv2.putText(bar, t, (10, 36), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (30, 30, 30), 2, cv2.LINE_AA)
        out.append(np.vstack([bar, c]))
    return np.hstack(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="photo or folder of photos")
    ap.add_argument("out_dir")
    ap.add_argument("--model", default=Config.model, help="rembg model, e.g. birefnet-general")
    ap.add_argument("--lama-path", default=os.path.expanduser("~/.cache/lama/big-lama.pt"))
    ap.add_argument("--no-debug", action="store_true")
    ap.add_argument("--crop", action="store_true", help="crop to the garment (default: keep the input canvas size)")
    ap.add_argument("--lama-scale", type=float, default=Config.lama_scale,
                    help="1.0 = inpaint at full res (slower), 0.5 = default")
    args = ap.parse_args()

    cfg = Config(model=args.model, lama_scale=args.lama_scale)
    seg, lama = Segmenter(cfg.model), Lama(args.lama_path)
    inp = Path(args.input)
    files = sorted(p for p in (inp.iterdir() if inp.is_dir() else [inp])
                   if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"})
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    from PIL import Image, ImageOps
    for f in files:
        im = ImageOps.exif_transpose(Image.open(f))       # same orientation as viewers show
        icc = im.info.get("icc_profile")                  # keep the camera colour profile
        raw = np.asarray(im.convert("RGB"))
        rgba, rep, dbg = process(raw, seg, lama, cfg)
        rep.file = f.name
        if args.crop:
            rgba = crop_to_content(rgba)
        Image.fromarray(rgba, "RGBA").save(out / f"{f.stem}.png", icc_profile=icc, compress_level=6)
        Image.fromarray(on_white(rgba)).save(out / f"{f.stem}_white.jpg", quality=98, subsampling=0,
                                             icc_profile=icc)
        if not args.no_debug:
            cv2.imwrite(str(out / f"{f.stem}_debug.jpg"), cv2.cvtColor(debug_board(dbg), cv2.COLOR_RGB2BGR),
                        [cv2.IMWRITE_JPEG_QUALITY, 88])
        (out / f"{f.stem}.json").write_text(json.dumps(asdict(rep), indent=2))
        flag = "REVIEW" if rep.needs_review else "ok"
        print(f"{f.name}: {flag} ({rep.seconds}s) {'; '.join(rep.reasons)}")
        del raw, rgba, dbg
        gc.collect()


if __name__ == "__main__":
    main()
