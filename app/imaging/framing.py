"""Crop and centre a cut-out: every garment at one scale, in the middle of its frame.

30 Sep 2026. A booth photographs the garment wherever it hangs — high, low, small
in a corner of a 3000x4000 frame — so the cut-outs that come out of it are the
same photographs with the wall taken away, and a gallery of them looks untidy:
one shirt fills the picture, the next is a small thing near the top.

THIS IS A CROP, NOTHING ELSE (3 Oct 2026; a crop and a resize until then). The
garment's own pixels are cut out of the cut-out along its bounding box and pasted,
untouched, in the centre of a transparent canvas of the PHOTOGRAPH'S RATIO, sized
so the garment fills the standard share of it. No model looks at it and nothing is
re-drawn, sharpened, relit — or enlarged: a small garment gets a smaller canvas,
not bigger pixels. Only a garment too big for the standard is scaled, DOWN, onto
the photograph's own canvas. (`upscale: true` restores the old enlargement.)

    before   3000x4000, shirt 1200x1500 at the top left
    after    3000x4000 would need the shirt at 1.87x (4000 x 0.70 / 1500), so
             the canvas is 1607x2143 instead and the shirt is at 1x — centred,
             70% of the height, every pixel the photograph's

WHAT THE CHECKS NEED TO KNOW ABOUT IT. The garment no longer sits where the
photograph has it, so every test that laid the cut-out over its photograph at
the same coordinates would call the framing a zoom. cutouts.garment_hole
REGISTERS the photograph to the cut-out first (it finds the scale and shift
that line the two up) and measures there; see cutouts._register.
"""
from __future__ import annotations

import io
import logging
from copy import deepcopy
from typing import Any

log = logging.getLogger("hermes.cutout")

# `imagery.cutout.framing` in policy.
DEFAULTS: dict[str, Any] = {
    # Frame every cut-out Hermes returns from /v1/imagery/remove-background (the
    # auto-approval matte) and /v1/imagery/cutout. A request can still pass
    # `frame: false` for the photograph's own framing.
    "enabled": True,
    # The empty band left on EACH side of the axis the garment fills: 0.15 is a
    # garment 70% of the frame's height (or width, for a wide one). Measured on
    # the BOAS shop's own product page, 1 Oct 2026: a 3:4 photo, the garment
    # centred, 15% clear above and below, 19% at the sides. (0.05 — 90%, edge to
    # edge — until then.) It also keeps the other checks quiet by construction:
    # a framed box covers at most 0.70 x 0.70 = 0.49 of the frame, under
    # `box_fill_max`, and no edge is touched.
    "margin": 0.15,
    # NEVER ENLARGE THE GARMENT (3 Oct 2026). Reaching the standard by scaling a
    # small garment up makes it soft — the photo's pixels spread over more pixels,
    # no detail added. With `upscale: false` the CANVAS shrinks around the garment
    # instead: the same 3:4 frame (the photo's ratio), the garment at 70% of it,
    # its pixels exactly as photographed. A garment too big for the standard is
    # still scaled DOWN onto the photo's canvas. `upscale: true` brings back the
    # old behaviour, capped at `max_scale`.
    "upscale": False,
    # The largest enlargement when `upscale` is on. 2.5x takes a garment from 28%
    # of a frame's height to 70%.
    "max_scale": 2.5,
    # The alpha at which a pixel is garment, for the bounding box.
    "alpha_threshold": 128,
    # Loose specks — a thread, a speck of the wall the mask kept — must not
    # decide where the box is. A separate piece smaller than this share of the
    # garment is left out of the box (and, outside it, out of the picture). A
    # real part of the garment that is detached in the mask (a belt, a tie) is
    # far bigger than a speck.
    "speck_max": 0.005,
    # Pixels of the garment's soft edge kept outside the hard box.
    "edge_pad_px": 3,
    # --- an OPAQUE cut-out (the one already on file) ------------------------
    # Its backdrop is the four corners' colour when they agree this closely;
    # a pixel this far (RGB) from it is garment; and the band round the
    # garment's box may hold at most this share of garment before the edge
    # counts as unclear and nothing is cropped.
    "corner_fraction": 0.04,
    "corner_agreement": 12,
    "backdrop_tolerance": 24,
    "edge_clear_max": 0.005,
    # Already framed: nothing is resampled a second time.
    "same_scale": 0.01,
    "same_offset": 0.005,
    # --- the judge (cutouts.standard_problem) ------------------------------
    # How far from the standard a cut-out may sit before it is re-framed.
    "fill_tolerance": 0.05,
    "center_tolerance": 0.03,
}


def config(pol: dict[str, Any] | None = None) -> dict[str, Any]:
    """`imagery.cutout.framing` with defaults filled in."""
    if pol is None:
        from app.config import policy

        pol = policy()
    out = deepcopy(DEFAULTS)
    over = (((pol or {}).get("imagery") or {}).get("cutout") or {}).get("framing")
    if isinstance(over, bool):
        over = {"enabled": over}
    over = over or {}
    for k, v in over.items():
        if v is not None:
            out[k] = v
    return out


def target_fill(cfg: dict[str, Any]) -> float:
    """The share of the frame the garment fills on its limiting axis."""
    return max(0.1, 1.0 - 2.0 * float(cfg.get("margin") or 0.0))


def _garment_box(alpha: Any, cfg: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1), exclusive, of the garment without its loose specks."""
    import numpy as np

    mask = alpha >= int(cfg.get("alpha_threshold") or 128)
    total = int(mask.sum())
    if total == 0:
        return None
    boxes: list[tuple[int, int, int, int]] = []
    try:
        import cv2

        n, _lbl, stats, _c = cv2.connectedComponentsWithStats(mask.astype(np.uint8), connectivity=8)
        floor = float(cfg.get("speck_max") or 0.0) * total
        for i in range(1, n):
            if stats[i, cv2.CC_STAT_AREA] >= floor:
                x, y, w, h = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                                                          cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
                boxes.append((x, y, x + w, y + h))
    except Exception:  # noqa: BLE001 — no cv2: every garment pixel counts
        boxes = []
    if not boxes:
        rows = np.where(mask.any(axis=1))[0]
        cols = np.where(mask.any(axis=0))[0]
        boxes = [(int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1)]
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _flat_backdrop(rgb: Any, cfg: dict[str, Any]) -> Any:
    """The colour of a flat, painted backdrop from the four corners, or None.

    A stored cut-out is opaque: vnyx-api flattens it onto the tenant's
    backdrop. That backdrop is one colour by construction, so all four corners
    agree; a photograph's wall does not, and then there is no backdrop to find
    the garment against.
    """
    import numpy as np

    h, w = rgb.shape[:2]
    k = max(2, int(round(min(w, h) * float(cfg.get("corner_fraction") or 0.04))))
    patches = [rgb[:k, :k], rgb[:k, -k:], rgb[-k:, :k], rgb[-k:, -k:]]
    medians = np.array([np.median(p.reshape(-1, 3), axis=0) for p in patches])
    colour = np.median(medians, axis=0)
    if np.abs(medians - colour).max() > float(cfg.get("corner_agreement") or 12):
        return None
    return colour


def frame_cutout(png: bytes, cfg: dict[str, Any] | None = None, *,
                 canvas: tuple[int, int] | None = None) -> tuple[bytes, dict[str, Any]]:
    """The cut-out with its garment scaled to the standard and centred.

    Returns (png, info). The canvas is the input's — the photograph's size and
    ratio, so the canvas check still holds — or `canvas` when given: a stored
    cut-out on a smaller canvas (896x1195 for a 3000x4000 photograph) is
    framed onto the photograph's size, or vnyx-api would pad it back out and
    shrink the garment again.

    TWO KINDS OF INPUT. A transparent cut-out (a segmenter's) is located by its
    alpha. An OPAQUE one — the cut-out already on file, flattened onto the
    tenant's backdrop — by that backdrop's colour, and is framed onto a canvas
    of the same colour. The input is returned untouched, with `framed: False`
    and a `note`, when the garment cannot be located safely, or when it is
    already framed.
    """
    import numpy as np
    from PIL import Image

    cfg = cfg if cfg is not None else config()
    upscale = bool(cfg.get("upscale", False))
    try:
        im = Image.open(io.BytesIO(png))
        im.load()
        # The photograph's colour profile rides along (see cutout._png_with_icc).
        icc = im.info.get("icc_profile")
        im = im.convert("RGBA")
    except Exception as exc:  # noqa: BLE001
        return png, {"framed": False, "note": f"could not decode ({exc.__class__.__name__})"}
    alpha = np.asarray(im.getchannel("A"))
    backdrop = None
    if float((alpha == 0).mean()) < 0.02:
        rgb = np.asarray(im.convert("RGB")).astype(np.float32)
        backdrop = _flat_backdrop(rgb, cfg)
        if backdrop is None:
            return png, {"framed": False,
                         "note": "opaque, and its corners are not one flat backdrop — "
                                 "the garment cannot be located"}
        far = np.sqrt(((rgb - backdrop) ** 2).sum(axis=2)) > float(cfg.get("backdrop_tolerance") or 24)
        try:
            import cv2

            far = cv2.morphologyEx(far.astype(np.uint8), cv2.MORPH_OPEN,
                                   np.ones((3, 3), np.uint8)).astype(bool)
        except Exception:  # noqa: BLE001
            pass
        alpha = np.where(far, 255, 0).astype(np.uint8)

    # Onto the requested canvas first (fitted inside, centred), so everything
    # below works on the photograph's own size. Without `upscale`, only when that
    # canvas is not larger: a stored cut-out smaller than its photograph is framed
    # on its own canvas rather than blown up to the photograph's.
    resized = False
    if canvas and tuple(canvas) != im.size and (
            upscale or (int(canvas[0]) <= im.width and int(canvas[1]) <= im.height)):
        CW, CH = int(canvas[0]), int(canvas[1])
        s = min(CW / im.width, CH / im.height)
        nw, nh = max(1, int(round(im.width * s))), max(1, int(round(im.height * s)))
        fill_rgba = ((255, 255, 255, 0) if backdrop is None
                     else (*[int(v) for v in backdrop], 255))
        base = Image.new("RGBA", (CW, CH), fill_rgba)
        base.paste(im.resize((nw, nh), Image.Resampling.LANCZOS), ((CW - nw) // 2, (CH - nh) // 2))
        a = Image.new("L", (CW, CH), 0)
        a.paste(Image.fromarray(alpha).resize((nw, nh), Image.Resampling.BILINEAR),
                ((CW - nw) // 2, (CH - nh) // 2))
        im, alpha, resized = base, np.asarray(a), True
    W, H = im.size

    box = _garment_box(alpha, cfg)
    if box is None:
        return png, {"framed": False, "note": "no garment in the cut-out"}
    if backdrop is not None:
        # CROPPING BY COLOUR MUST NOT CUT THE GARMENT. A white shirt on a white
        # backdrop reads as backdrop at its edges, and a box drawn round what
        # DID separate would slice the rest off. So the band just outside the
        # box has to be backdrop; if it is not, the edge is not clear and the
        # picture is left as it is.
        x0, y0, x1, y1 = box
        m = max(2, int(round(min(W, H) * 0.02)))
        ring = np.zeros((H, W), bool)
        ring[max(0, y0 - m):min(H, y1 + m), max(0, x0 - m):min(W, x1 + m)] = True
        ring[y0:y1, x0:x1] = False
        # Solid garment only: a resized mask has a soft, one-pixel edge that
        # is the garment's own outline, not a piece beyond it.
        solid = alpha >= int(cfg.get("alpha_threshold") or 128)
        if ring.any() and float(solid[ring].mean()) > float(cfg.get("edge_clear_max") or 0.005):
            return png, {"framed": False,
                         "note": "the garment's edge is not clear against the backdrop — "
                                 "not cropped, so none of it is cut off"}
    # The HARD box decides the scale and the centre; the crop takes a few pixels
    # more so the garment's soft edge comes along.
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    fill = target_fill(cfg)
    # The scale that would meet the standard on this canvas.
    need = min(fill * W / bw, fill * H / bh)
    same = float(cfg.get("same_scale") or 0.0)
    CW, CH = W, H
    if upscale:
        scale = min(need, float(cfg.get("max_scale") or 2.5))
    elif need > 1.0 + same:
        # NO ENLARGEMENT: the frame shrinks to the garment instead — the photo's
        # ratio, the garment at the standard fill, its pixels untouched.
        scale = 1.0
        CW, CH = max(1, int(round(W / need))), max(1, int(round(H / need)))
    else:
        # Within `same_scale` of the standard: moved, not resampled. Too big:
        # scaled down onto the canvas.
        scale = 1.0 if need > 1.0 - same else need
    gl, gt = (CW - bw * scale) / 2, (CH - bh * scale) / 2    # where the hard box lands
    info: dict[str, Any] = {
        "framed": True, "scale": round(scale, 4), "canvas": [CW, CH],
        "garment_box": [x0, y0, x1, y1],
        "placed_at": [round(gl), round(gt), round(gl + bw * scale), round(gt + bh * scale)],
        "fill_w": round(bw * scale / CW, 4), "fill_h": round(bh * scale / CH, 4),
        "capped": upscale and scale >= float(cfg.get("max_scale") or 2.5) - 1e-6,
        "cropped": (CW, CH) != (W, H),
        "source": "alpha" if backdrop is None else "backdrop",
        # THE OUTPUT MEETS THE STANDARD AS IT IS — vnyx-api must not pad it back
        # out to the photograph's canvas (which would shrink the garment again).
        "standard": True,
    }

    if not resized and (CW, CH) == (W, H) and (abs(scale - 1.0) <= same
            and abs(gl - x0) <= max(1.0, float(cfg.get("same_offset") or 0.0) * W)
            and abs(gt - y0) <= max(1.0, float(cfg.get("same_offset") or 0.0) * H)):
        info.update(framed=False, note="already framed")
        return png, info

    # Opaque: take the whole clear band round the box (it is backdrop — checked
    # above), so a faint shadow at the hem comes along.
    pad = int(cfg.get("edge_pad_px") or 0) if backdrop is None else m
    px0, py0 = max(0, x0 - pad), max(0, y0 - pad)
    px1, py1 = min(W, x1 + pad), min(H, y1 + pad)
    crop = im.crop((px0, py0, px1, py1))
    left = int(round(gl - (x0 - px0) * scale))
    top = int(round(gt - (y0 - py0) * scale))
    # Where the hard box ACTUALLY lands: rounding `left` after the pad can put it a
    # pixel off `round(gl)`, and a caller comparing pixels needs the real place.
    bx, by = left + (x0 - px0) * scale, top + (y0 - py0) * scale
    info["placed_at"] = [round(bx), round(by), round(bx + bw * scale), round(by + bh * scale)]
    if scale == 1.0:
        # NOT RESAMPLED AT ALL: the garment's pixels are the cut-out's, moved.
        rgb, a_ch = crop.convert("RGB"), crop.getchannel("A")
    else:
        nw = max(1, int(round((px1 - px0) * scale)))
        nh = max(1, int(round((py1 - py0) * scale)))
        # RGB and alpha resized apart, as _cutout_png does: a transparent pixel's
        # colour must not bleed into the garment's edge as a dark fringe.
        resample = Image.Resampling.LANCZOS
        rgb = crop.convert("RGB").resize((nw, nh), resample)
        a_ch = crop.getchannel("A").resize((nw, nh), resample)
    buf = io.BytesIO()
    extra = {"icc_profile": icc} if icc else {}
    if backdrop is not None:
        # Opaque: the crop carries its own backdrop, laid on a canvas of the
        # same colour, so the join cannot be seen.
        out_im = Image.new("RGB", (CW, CH), tuple(int(v) for v in backdrop))
        out_im.paste(rgb, (left, top))
        out_im.save(buf, format="PNG", optimize=True, **extra)
    else:
        garment = Image.merge("RGBA", (*rgb.split(), a_ch))
        out_im = Image.new("RGBA", (CW, CH), (255, 255, 255, 0))
        # Only the soft-edge pad can reach past the canvas; paste clips it.
        out_im.paste(garment, (left, top))
        arr = np.asarray(out_im).copy()
        arr[arr[:, :, 3] == 0] = (255, 255, 255, 0)
        Image.fromarray(arr, "RGBA").save(buf, format="PNG", optimize=True, **extra)
    log.info("framing: garment %dx%d at (%d,%d) %s and centred on %dx%d%s",
             bw, bh, x0, y0, "not resampled" if scale == 1.0 else f"scaled {scale:.2f}x",
             CW, CH, " (capped)" if info["capped"] else "")
    return buf.getvalue(), info
