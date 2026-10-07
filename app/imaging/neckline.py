"""The neck opening of a top's cut-out, finished (7 Oct 2026).

MID-000351, both FRONT cut-outs, the operator's report:

- PHOTOBOOTH: the collar opening shows the booth's WHITE MANNEQUIN NECK. The garment
  parser is right that it is not cloth and cuts it out — and the empty U left behind
  reads as a torn collar on the listing. The operator's call: keep the form there.
- DECISION (hung on the panel's hanger): the opening shows the black studio, a metal
  bracket and the white hanger. The parser kept scattered FRAGMENTS of them — black
  spots floating in the neckline.

So, once a cut-out is ACCEPTED (after every check, which must judge the segmenter's
own answer), its neck opening is finished:

    the BODY       the cut-out's largest solid piece (fragments and threads opened away)
    the COLLAR TIPS the highest point of the body's top edge either side of the neck dip
    the OPENING    between the tips, under the straight line joining them, above the body

- `fill` origins (the photobooth): the photograph's own pixels go back into the opening
  wherever they are NOT the studio backdrop (sampled from the photograph's top corners),
  cut flat along the tips' line — the form, lit or in shadow, and any inside of the
  garment behind it. Nothing is invented; it is the mannequin as photographed.
- `clear` origins (the decision panel): between the tips, whatever the cut-out kept
  above the collar's smoothed edge is cleared, and in a thin margin below it whatever is
  not one of the garment's own colours (sampled from the collar and the chest under it)
  — the hanger and the black studio hugging the collar.
- any other origin (the wall, an unknown one): left as it is. MID-000430 (7 Oct 2026):
  on a wall photo the opening shows the garment's own white back collar band, and
  clearing cut it ragged and chopped the front collar's tips flat.

Only a real neck: a dip in the middle of the top edge, deeper than `min_depth` and no
deeper than `max_depth` of the garment's height, narrower than `max_width` of its width
(a tank top's whole chest between the straps is not a neck), with both tips standing
above the dip. Bottoms, shoes and bags are never touched. Never raises.
"""
from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger("hermes.cutout")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "fill_origins": ["PHOTOBOOTH"],
    # Cleared only on these (7 Oct 2026, MID-000430): on a WALL photo the opening shows the
    # garment's own back collar band, which the wall's colour makes look like junk —
    # clearing cut it ragged and chopped the front collar's tips flat. The user's rule:
    # never tear the cloth to take a hanger out; a hanger piece may stay.
    "clear_origins": ["DECISION"],
    "work_px": 1024,
    "min_depth": 0.02,       # of the garment's height
    "max_depth": 0.22,
    "min_width": 0.10,       # of the garment's width, between the tips
    "max_width": 0.48,
    "centre_band": 0.16,     # the dip's lowest point lies within this of the centre
    "tip_band": 0.34,        # the tips lie within this of the centre
    # The studio backdrop, sampled from the photograph's top corners: within its lightness
    # range (widened by `backdrop_l_pad`) AND within `backdrop_ab` of its colour. Lightness
    # and colour apart, because one distance mixed them up: on MID-000351 the shadow
    # inside the collar (67,75,90) sat 27.7 from the grey curtain (99,99,101) — darker
    # and bluer, plainly not curtain, but under any single tolerance that keeps the
    # curtain beside the neck (14-21 away) out.
    "backdrop_l_pad": 8,
    "backdrop_ab": 6,
    "max_backdrop": 0.30,      # more of the opening than this is studio: not a neck on the form
    "max_fill": 0.06,          # never put back more than this share of the garment's area
    "smooth": 0.06,            # median window over the collar edge, of the garment's width
    "margin": 0.025,           # below the collar's edge, of the garment's height
    "garment_tolerance": 30,   # Lab distance to the garment's own colours
    "sliver": 0.012,           # thinner than this share of the garment's width beside a clearing
}


def config(pol: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.config import policy

    out = dict(DEFAULTS)
    block = (((pol if pol is not None else policy()).get("imagery") or {})
             .get("cutout") or {}).get("neckline")
    if block is False:
        out["enabled"] = False
    elif isinstance(block, dict):
        out.update({k: v for k, v in block.items() if v is not None})
    return out


def applies_to(garment: str | None) -> bool:
    """A top (or a garment of unknown type): never bottoms, footwear or accessories."""
    try:
        from app.imaging.nanobanana import classify_garment_type

        kind = classify_garment_type(garment, None) if garment else None
    except Exception:  # noqa: BLE001
        kind = None
    return kind in ("top", None)


def _opening(body: Any, cfg: dict[str, Any]) -> dict[str, Any] | None:
    """The neck opening on the body mask's grid, or None when there is no neck."""
    import numpy as np

    ys, xs = np.where(body)
    if ys.size < 500:
        return None
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()), int(xs.min()), int(xs.max())
    h, w = y1 - y0 + 1, x1 - x0 + 1
    H, W = body.shape
    has = body.any(axis=0)
    top = np.where(has, body.argmax(axis=0), H).astype(np.float64)
    cx = (x0 + x1) / 2.0
    cb, tb = float(cfg["centre_band"]) * w, float(cfg["tip_band"]) * w
    lo, hi = int(max(x0, cx - cb)), int(min(x1, cx + cb))
    if hi <= lo:
        return None
    xd = lo + int(np.argmax(top[lo:hi + 1]))
    if top[xd] >= H:
        return None
    left = np.arange(int(max(x0, cx - tb)), xd)
    right = np.arange(xd + 1, int(min(x1, cx + tb)) + 1)
    if left.size < 3 or right.size < 3:
        return None
    xl = int(left[np.argmin(top[left])])
    xr = int(right[np.argmin(top[right])])
    yl, yr = top[xl], top[xr]
    line_at = lambda x: yl + (yr - yl) * (x - xl) / max(1, xr - xl)  # noqa: E731
    depth = top[xd] - line_at(xd)
    width = xr - xl
    info = {"tips": [(xl, int(yl)), (xr, int(yr))], "bottom": (xd, int(top[xd])),
            "depth": round(depth / h, 4), "width": round(width / w, 4)}
    if not (float(cfg["min_depth"]) * h <= depth <= float(cfg["max_depth"]) * h):
        return {**info, "skip": "no neck dip of a neck's depth"}
    if not (float(cfg["min_width"]) * w <= width <= float(cfg["max_width"]) * w):
        return {**info, "skip": "the opening is not a neck's width"}
    # Both tips must stand above the dip — a back view's convex collar has no dip.
    if min(top[xd] - yl, top[xd] - yr) < 0.5 * depth:
        return {**info, "skip": "one side does not rise to a collar tip"}
    return {**info, "line": (xl, yl, xr, yr), "garment_h": h, "garment_w": w}


def _enclosed(barrier_full: Any, line: tuple[float, float, float, float], seed: tuple[int, int],
              box: tuple[int, int, int, int], thickness: int) -> Any | None:
    """The region the barrier and the tips' line enclose, flooded from `seed`, as a
    full-size mask — or None when the seed is blocked or the flood leaks out of `box`
    (a gap in the collar). Above the line inside the box is closed, so the flood
    cannot climb round the tips."""
    import cv2
    import numpy as np

    xl, yl, xr, yr = line
    bx0, by0, bx1, by1 = box
    barrier = barrier_full[by0:by1, bx0:bx1].astype(np.uint8)
    cv2.line(barrier, (int(round(xl - bx0)), int(round(yl - by0))),
             (int(round(xr - bx0)), int(round(yr - by0))), 1, thickness=thickness)
    yy = np.arange(barrier.shape[0])[:, None]
    xx = np.arange(barrier.shape[1])[None, :]
    line_at = (yl - by0) + (yr - yl) * (xx + bx0 - xl) / max(1.0, xr - xl)
    barrier[yy < line_at] = 1
    sx, sy = seed[0] - bx0, seed[1] - by0
    if not (0 <= sy < barrier.shape[0] and 0 <= sx < barrier.shape[1]) or barrier[sy, sx]:
        return None
    ffmask = np.zeros((barrier.shape[0] + 2, barrier.shape[1] + 2), np.uint8)
    cv2.floodFill(barrier, ffmask, (sx, sy), 2)
    region = barrier == 2
    if region[:, 0].any() or region[:, -1].any() or region[-1, :].any():
        return None
    out = np.zeros(barrier_full.shape, bool)
    out[by0:by1, bx0:bx1] = region
    return out


def finish(source: bytes, png: bytes, *, origin: str | None, garment: str | None,
           cfg: dict[str, Any] | None = None) -> tuple[bytes, dict[str, Any]]:
    """`png` with its neck opening finished, and what was done. Unchanged on any doubt."""
    import numpy as np
    from PIL import Image, ImageOps

    cfg = cfg or config()
    report: dict[str, Any] = {"done": None}
    if not cfg.get("enabled"):
        return png, {**report, "skip": "off in policy"}
    if not applies_to(garment):
        return png, {**report, "skip": f"not a top ({garment!r})"}
    origin_u = str(origin or "").upper()
    if origin_u not in {str(o).upper() for o in (cfg.get("fill_origins") or []) + (cfg.get("clear_origins") or [])}:
        return png, {**report, "skip": f"not finished on {origin or 'an unknown origin'}"}
    try:
        import cv2

        cut = Image.open(io.BytesIO(png)).convert("RGBA")
        raw = ImageOps.exif_transpose(Image.open(io.BytesIO(source))).convert("RGB")
        if raw.size != cut.size:
            return png, {**report, "skip": "the cut-out is not on the photograph's canvas"}
        arr = np.asarray(cut).copy()
        alpha = arr[..., 3]
        H, W = alpha.shape
        k = min(1.0, float(cfg["work_px"]) / max(H, W))
        sw, sh = max(8, int(round(W * k))), max(8, int(round(H * k)))
        small = cv2.resize((alpha >= 128).astype(np.uint8), (sw, sh), interpolation=cv2.INTER_NEAREST)
        # THE BODY: fragments and threads opened away, the largest piece kept.
        ys, xs = np.where(small > 0)
        if ys.size < 500:
            return png, {**report, "skip": "almost nothing kept"}
        gw = int(xs.max() - xs.min() + 1)
        r = max(3, int(round(0.012 * gw))) | 1
        opened = cv2.morphologyEx(small, cv2.MORPH_OPEN,
                                  cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (r, r)))
        n, lbl, stats, _ = cv2.connectedComponentsWithStats(opened, connectivity=8)
        if n <= 1:
            return png, {**report, "skip": "no solid body"}
        body_small = lbl == (int(np.argmax(stats[1:, cv2.CC_STAT_AREA])) + 1)
        op = _opening(body_small, cfg)
        if op is None or op.get("skip"):
            return png, {**report, **(op or {}), "skip": (op or {}).get("skip") or "no neck"}
        from scipy.ndimage import median_filter

        # Back to the photograph's grid.
        xl, yl, xr, yr = (v / k for v in op["line"])
        gh_full = float(op["garment_h"]) / k
        # THE COLLAR'S EDGE: the body's top profile on the coarse grid, median-smoothed
        # so a hanger piece or a sliver of studio stuck to the collar is not part of it,
        # then laid onto the photograph's columns.
        has_s = body_small.any(axis=0)
        top_s = np.where(has_s, body_small.argmax(axis=0), sh).astype(np.float64)
        win = max(3, int(round(float(cfg["smooth"]) * float(op["garment_w"])))) | 1
        top_s = median_filter(top_s, size=win, mode="nearest")
        cols = np.arange(int(np.ceil(xl)), int(np.floor(xr)) + 1)
        edge = np.interp(cols * k, np.arange(sw), top_s) / k
        line_y = yl + (yr - yl) * (cols - xl) / max(1.0, xr - xl)
        rows = np.arange(H)[:, None]
        above = np.zeros((H, W), bool)
        above[:, cols] = rows < edge[None, :]
        margin = np.zeros((H, W), bool)
        margin[:, cols] = (rows >= edge[None, :]) & (rows < (edge + float(cfg["margin"]) * gh_full)[None, :])
        opening = np.zeros((H, W), bool)
        opening[:, cols] = (rows >= np.ceil(line_y)[None, :]) & (rows < edge[None, :])

        lab = cv2.cvtColor(np.asarray(raw), cv2.COLOR_RGB2LAB).astype(np.float32)
        body_full = cv2.resize(body_small.astype(np.uint8), (W, H), interpolation=cv2.INTER_NEAREST) > 0
        garment_area = float(((alpha >= 128) & body_full).sum())
        fill_origins = {str(o).upper() for o in cfg.get("fill_origins") or []}
        # The box the opening is looked for in, the flood's seed, and the line's width.
        span = xr - xl
        box = (int(max(0, xl - 0.35 * span)), int(max(0, min(yl, yr) - 2)),
               int(min(W, xr + 0.35 * span + 1)), 0)
        xd_full = int(round(op["bottom"][0] / k))
        yd_full = int(round(edge[min(len(edge) - 1, max(0, xd_full - cols[0]))]))
        box = (box[0], box[1], box[2], int(min(H, yd_full + 0.06 * gh_full)))
        seed = (xd_full, int(round((np.interp(xd_full, [xl, xr], [yl, yr]) + yd_full) / 2)))
        thick = max(3, int(2 / k))
        if origin_u in fill_origins:
            # THE FORM: the whole opening, from the photograph's own pixels. Below the
            # tips' line and inside the collar is the form by construction — the garment
            # is dressed on it — and its shadowed side is the curtain's very grey
            # (MID-000351: a colour test left a strip of shadow open, which still read as
            # torn). Down to the collar's REAL edge at full resolution, not the smoothed
            # coarse one, so no seam is left along the collar.
            near = cv2.dilate(body_full.astype(np.uint8),
                              np.ones((int(4 / k) | 1, int(4 / k) | 1), np.uint8)) > 0
            body_px = (alpha >= 128) & near
            # THE REGION THE COLLAR AND THE TIPS' LINE ENCLOSE, flooded from the middle
            # of the opening — not the columns between the tips: the collar can curve
            # OUTWARD below its tip (MID-000351's left side), and a column test left
            # that strip open. A flood that leaks through a gap in the collar fills
            # nothing.
            fill = _enclosed(body_px, (xl, yl, xr, yr), seed, box, thick)
            if fill is None:
                return png, {**report, "skip": "the collar does not close round the neck"}
            # A NECKLINE WIDER THAN THE NECK shows the studio, not the form: then the
            # opening is not filled at all, rather than with curtain.
            cw, ch = max(8, W // 20), max(8, H // 20)
            corners = np.concatenate([lab[:ch, :cw].reshape(-1, 3), lab[:ch, -cw:].reshape(-1, 3)])
            l_lo, l_hi = np.percentile(corners[:, 0], [2, 98])
            pad = float(cfg["backdrop_l_pad"])
            ab = np.median(corners[:, 1:], axis=0)
            backdrop_like = ((lab[..., 0] >= l_lo - pad) & (lab[..., 0] <= l_hi + pad)
                             & (np.sqrt(((lab[..., 1:] - ab) ** 2).sum(-1)) <= float(cfg["backdrop_ab"])))
            back_share = float((fill & backdrop_like).sum()) / max(1.0, float(fill.sum()))
            if back_share > float(cfg["max_backdrop"]):
                return png, {**report, "skip": f"{back_share:.0%} of the opening is the studio, "
                                               f"not the form"}
            share = float(fill.sum()) / max(1.0, garment_area)
            if share > float(cfg["max_fill"]):
                return png, {**report, "skip": f"the form would be {share:.1%} of the garment"}
            # Above the tips' line between them: nothing but what the parser called body.
            stray = above & ~fill & (alpha > 0) & ~body_px
            arr[stray] = (255, 255, 255, 0)
            arr[fill, :3] = np.asarray(raw)[fill]
            arr[fill, 3] = 255
            report.update(done="form", filled=round(share, 4), cleared=int(stray.sum()),
                          backdrop_in_opening=round(back_share, 3))
        else:
            # THE GARMENT'S OWN COLOURS, from the collar and the chest BELOW THE NECK'S
            # LOWEST POINT — never from just under the smoothed edge, which a wide hanger
            # piece stuck to the collar can raise into itself (then the hanger would be
            # sampled as a garment colour and protected).
            band = np.zeros((H, W), bool)
            band[yd_full:int(min(H, yd_full + 0.08 * gh_full)), cols] = True
            sample = lab[band & (alpha >= 128)]
            if sample.shape[0] < 200:
                return png, {**report, "skip": "no collar to sample the garment's colours from"}
            if sample.shape[0] > 20000:
                sample = sample[np.random.default_rng(7).choice(sample.shape[0], 20000, replace=False)]
            kk = min(4, max(1, sample.shape[0] // 500))
            _c, labels_k, centres = cv2.kmeans(
                sample, kk, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 1.0),
                3, cv2.KMEANS_PP_CENTERS)
            weights = np.bincount(labels_k.ravel(), minlength=kk) / float(labels_k.size)
            centres = centres[weights >= 0.10]
            dist = np.min(np.stack([np.sqrt(((lab - c) ** 2).sum(-1)) for c in centres]), axis=0)
            garment_like = dist <= float(cfg["garment_tolerance"])
            # THE OPENING, flooded through everything that is NOT the garment's own
            # colour — the hanger, the bracket, the black studio — and stopped by the
            # collar and the tips' line. MID-000351's DECISION FRONT: a white piece of
            # the hanger hugged the collar's right wall, wide enough to pass for body
            # and too tall for an edge margin to reach. Whatever the cut-out kept in
            # the flooded region is cleared; an off-colour speck INSIDE the collar is
            # not reachable from the opening and stays.
            region = _enclosed((alpha >= 128) & garment_like, (xl, yl, xr, yr), seed, box, thick)
            above_px = (alpha > 0) & above
            if region is None:
                # Leaked, or the seed is garment (the inside back of the garment fills
                # the opening): only what floats above the collar's edge goes.
                stray = above_px
            else:
                stray = above_px | ((alpha > 0) & region)
                # SLIVERS LEFT STANDING BESIDE WHAT WAS CLEARED: the bracket's shadowed
                # edge is the collar's shadow colour, so the flood stops at it. Anything
                # thinner than `sliver` of the garment's width that touches the cleared
                # area goes; the collar band is many times thicker and only loses its
                # ragged rim.
                gw_full = float(op["garment_w"]) / k
                rr = max(5, int(round(float(cfg.get("sliver") or 0.012) * gw_full))) | 1
                kept_px = ((alpha >= 128) & ~stray).astype(np.uint8)
                solid = cv2.morphologyEx(kept_px, cv2.MORPH_OPEN,
                                         cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (rr, rr))) > 0
                thin = (kept_px > 0) & ~solid
                nt, tlbl = cv2.connectedComponents(thin.astype(np.uint8), connectivity=8)
                touch = cv2.dilate(stray.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
                hit = np.unique(tlbl[touch & thin])
                inside = np.zeros((H, W), bool)
                inside[box[1]:box[3], box[0]:box[2]] = True
                stray = stray | (np.isin(tlbl, hit[hit > 0]) & inside & ~solid)
            if not stray.any():
                return png, {**report, "skip": "the opening is already clean"}
            arr[stray] = (255, 255, 255, 0)
            report.update(done="cleared", cleared=int(stray.sum()),
                          cleared_share=round(float(stray.sum()) / max(1.0, garment_area), 4))
        report.update(depth=op["depth"], width=op["width"])
        buf = io.BytesIO()
        Image.fromarray(arr, "RGBA").save(buf, format="PNG", compress_level=6)
        return buf.getvalue(), report
    except Exception as exc:  # noqa: BLE001 — the accepted cut-out stands
        log.info("neckline: not finished (%s: %s)", exc.__class__.__name__, exc)
        return png, {**report, "skip": f"error ({exc.__class__.__name__})"}
