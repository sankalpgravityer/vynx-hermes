"""Refine a fine-tuned parser's cut-out of HUNG BOTTOMS (1 Oct 2026).

Runs app/imaging/garment_cutout.py — the photobooth refinement written for vnyx — on a
candidate the parser (v2, v1, stock cloth-seg) or a mask strategy has just made, before
any check sees it:

    the parser's alpha ──► wall wire / specks out ──► waistband top edge rebuilt
                       ──► hanger clips found, the fabric under them filled in from
                           the garment's own texture ──► wall between fringe threads
                           out ──► edge colour decontaminated

Every pixel outside what the clips hid is the photograph's own. So the cut-out the
checks judge — the leftover check, the tear check, the keep-better comparison — is
the refined one, and a hanger the parser kept no longer gets the candidate refused.

BOTTOMS ONLY, and not dungarees. The script assumes nothing above the waistband line is
garment, which is true of jeans, shorts and skirts on a hanger and false of a shirt's
collar, a hood or a dungaree bib. Which garment it is comes from the caller's hint
(the product's category and subcategory); with no hint nothing is refined.

NEVER WORSE THAN THE INPUT. The refined cut-out is used only when the script ran, made
up no more than `max_generated_frac` of the garment, and took away no more than
`max_lost_frac` of what the parser kept; otherwise the parser's own cut-out goes on
to the checks unchanged, and the reason is logged.
"""
from __future__ import annotations

import io
import logging
import time
from copy import deepcopy
from typing import Any

from app.config import policy

log = logging.getLogger("hermes.cutout")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Families (policy `garment_families`) the refinement runs on, and types
    # (policy `garment_types`) inside them it must not: a dungaree bib stands
    # above the waistband line the script cuts at.
    "families": ["bottoms"],
    "skip_types": ["dungarees"],
    # The script's own review threshold: more of the garment made up than this,
    # and the parser's cut-out is kept instead.
    "max_generated_frac": 0.02,
    # What the parser kept that the refinement took away, as a share of the
    # garment. A hanger, its clips and the wall between fringe threads are a few
    # percent; a waistband line drawn across real fabric is far more.
    "max_lost_frac": 0.08,
    # WHAT WAS TAKEN AWAY THAT THE PHOTO SHOWS AS GARMENT — nearer the waistband's
    # colour than the wall's, not hanger metal, and more than a sliver along the
    # redrawn edge — as a share of the garment. BOA-001412 (grey shorts on the
    # near-white wall): the waistband line dipped into the waistband, 0.43% and
    # 0.62% of the garment on its two views, while everything taken away stayed
    # at 1.7-2.0%, under `max_lost_frac`. Twenty good refinements of hung bottoms
    # measured at most 0.12% (1 Oct 2026).
    "max_fabric_taken": 0.0025,
    # THE WAISTBAND MUST STAND OUT FROM THE WALL to be redrawn. Below this
    # garment/wall contrast the script itself says "check the top edge", and
    # nobody checks in auto-approval: cream and white jeans on the white wall
    # (KIL-001216 FRONT, BOA-005343 BACK) came back with the waistband top cut
    # flat or notched. So the parser's cut-out is kept. 0 refines regardless.
    "min_waistband_contrast": 4.0,
    # Big-LaMa TorchScript for the fabric under the clips; null (or no torch on
    # the machine) uses the script's exemplar fill, which copies real texture.
    "lama_path": None,
    # THE SCRIPT RUNS ON THE GARMENT'S NEIGHBOURHOOD, not the whole frame: the
    # garment's box plus this many pixels above (the hanger, and the rows the
    # waistband search and the clip search look at: 120 and 200 above the edge)
    # and `crop_pad_px` on the other sides. Most of its steps work on every pixel
    # of the frame they are given, and a hung pair of shorts is a fifth of a
    # 3000x4000 photo.
    "crop_pad_top_px": 400,
    "crop_pad_px": 150,
}


def config(pol: dict[str, Any] | None = None) -> dict[str, Any]:
    """`imagery.cutout.refine` with defaults filled in. `refine: false` turns it off."""
    out = deepcopy(DEFAULTS)
    block = (((pol if pol is not None else policy()).get("imagery") or {})
             .get("cutout") or {}).get("refine")
    if block is False:
        out["enabled"] = False
    elif isinstance(block, dict):
        for k, v in block.items():
            if v is not None:
                out[k] = v
    return out


def wanted(garment: str | None, rcfg: dict[str, Any],
           pol: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Should this garment be refined? (yes, why)."""
    from app.imaging import quality_gate

    if not rcfg.get("enabled"):
        return False, "refinement is off"
    if not garment or not str(garment).strip():
        return False, "no garment hint"
    pol = pol if pol is not None else policy()
    family = quality_gate.garment_family(garment, pol)
    if family not in {str(f) for f in rcfg.get("families") or []}:
        return False, f"not a family it refines ({family or 'unknown'})"
    kind = quality_gate.garment_type(garment, pol)
    if kind and kind in {str(t) for t in rcfg.get("skip_types") or []}:
        return False, f"{kind} are not refined"
    return True, f"{family}{f' ({kind})' if kind else ''}"


def fabric_taken(raw: Any, before: Any, after: Any) -> float:
    """How much the refinement took away that the PHOTO shows as garment, as a share
    of the garment.

    Taking away is the refinement's job — the hanger bar, the clips, wall the parser
    kept above the waistband — so the amount alone says little. What it must not take
    is fabric. Each removed pixel is compared, in the photo, with the waistband's own
    colour and with the wall's (lightness at full weight: a grey waistband on a white
    wall differs in little else); nearer the waistband and not hanger metal (grey,
    very bright or very dark) counts as fabric. A sliver along the new edge is the
    edge being redrawn, so it is opened away first.
    """
    import cv2
    import numpy as np

    removed = before & ~after
    garment = float(before.sum())
    if not removed.any() or not after.any() or not garment:
        return 0.0
    ys, xs = np.where(after)
    gy0, gy1 = int(ys.min()), int(ys.max())
    gh = gy1 - gy0
    # The top of the garment, where the waistband line is drawn: its colour, and
    # the wall's around it.
    t0, t1 = max(0, gy0 - gh // 10), gy0 + max(1, gh // 5)
    x0, x1 = max(0, int(xs.min()) - 100), int(xs.max()) + 100
    lab = cv2.cvtColor(np.ascontiguousarray(raw[t0:t1, x0:x1]), cv2.COLOR_RGB2LAB).astype(np.float32)
    top_after, top_before = after[t0:t1, x0:x1], before[t0:t1, x0:x1]
    wall_zone = ~top_before & ~top_after
    if top_after.sum() < 100 or wall_zone.sum() < 100:
        return 0.0
    fab_ref = np.median(lab[top_after], axis=0)
    wall_ref = np.median(lab[wall_zone], axis=0)

    def dist(ref):
        return np.sqrt((lab[..., 0] - ref[0]) ** 2 + (lab[..., 1] - ref[1]) ** 2
                       + (lab[..., 2] - ref[2]) ** 2)

    L = lab[..., 0]
    chroma = np.hypot(lab[..., 1] - 128, lab[..., 2] - 128)
    fab_low = float(np.percentile(L[top_after], 2))
    metal = (chroma < 4.5) & ((L > max(225.0, wall_ref[0] + 20)) | (L < min(95.0, fab_low - 15)))
    # On a COLOURED garment a grey or black pixel is the hanger, not the garment:
    # the black bar over navy or blue denim is as dark as the denim and nearer it
    # than the wall (BOA-003065, BOA-005350 measured 0.39-0.46% before this, the
    # bar alone). A grey or black garment keeps the plain test.
    fab_chroma = float(np.median(chroma[top_after]))
    if fab_chroma > 8:
        metal |= chroma < max(4.5, 0.4 * fab_chroma)
    fabric = removed[t0:t1, x0:x1] & (dist(fab_ref) < dist(wall_ref)) & ~metal
    fabric = cv2.morphologyEx(fabric.astype(np.uint8), cv2.MORPH_OPEN, np.ones((7, 7), np.uint8)) > 0
    return float(fabric.sum()) / garment


def refine(source: bytes, cut_png: bytes,
           rcfg: dict[str, Any]) -> tuple[bytes | None, dict[str, Any]]:
    """The refined cut-out, or None and why. Never raises.

    `source` is the upright photograph the cut-out was made from, and `cut_png` an RGBA
    cut-out of the same size.
    """
    import numpy as np
    from PIL import Image

    started = time.perf_counter()

    def no(why: str) -> tuple[None, dict[str, Any]]:
        return None, {"refined": False, "why": why,
                      "ms": int((time.perf_counter() - started) * 1000)}

    try:
        raw = np.asarray(Image.open(io.BytesIO(source)).convert("RGB"))
        cut = np.asarray(Image.open(io.BytesIO(cut_png)).convert("RGBA"))
    except Exception as exc:  # noqa: BLE001
        return no(f"could not decode ({exc.__class__.__name__})")
    if cut.shape[:2] != raw.shape[:2]:
        return no(f"the cut-out is {cut.shape[1]}x{cut.shape[0]}, "
                  f"the photograph {raw.shape[1]}x{raw.shape[0]}")
    before = cut[..., 3] >= 128
    if not before.any():
        return no("the cut-out has no garment")

    from app.imaging import garment_cutout as gc

    H, W = before.shape
    ys, xs = np.where(cut[..., 3] > 0)
    top, side = int(rcfg.get("crop_pad_top_px") or 0), int(rcfg.get("crop_pad_px") or 0)
    y0, y1 = max(0, int(ys.min()) - top), min(H, int(ys.max()) + 1 + side)
    x0, x1 = max(0, int(xs.min()) - side), min(W, int(xs.max()) + 1 + side)

    gcfg = gc.Config(max_generated_frac=float(rcfg.get("max_generated_frac") or 0.02),
                     stop_if_over_generated=True,
                     stop_if_contrast_below=float(rcfg.get("min_waistband_contrast") or 0.0))
    try:
        part, rep = gc.cutout(np.ascontiguousarray(raw[y0:y1, x0:x1]),
                              alpha=np.ascontiguousarray(cut[y0:y1, x0:x1, 3]),
                              lama_path=rcfg.get("lama_path") or None, cfg=gcfg)
    except gc.TooMuchHidden as exc:
        return no(f"it would make up too much of the garment ({exc})")
    except gc.LowContrast as exc:
        return no(f"the waistband is too close to the wall's colour to redraw ({exc})")
    except Exception as exc:  # noqa: BLE001 — the parser's cut-out still stands
        return no(f"{exc.__class__.__name__}: {exc}")
    rgba = np.zeros((H, W, 4), np.uint8)
    rgba[y0:y1, x0:x1] = part

    after = rgba[..., 3] >= 128
    garment = float(before.sum())
    lost = float((before & ~after).sum()) / garment
    added = float((after & ~before).sum()) / garment
    info: dict[str, Any] = {
        "clips": rep.clips_found,
        "generated_frac": round(rep.generated_frac, 4),
        "label_pixels_rebuilt": rep.label_pixels_generated,
        "wires": rep.wire_lines_removed,
        "waistband_contrast": round(rep.waistband_contrast, 1),
        "backend": rep.inpaint_backend,
        "lost_frac": round(lost, 4),
        "added_frac": round(added, 4),
        "notes": list(rep.reasons),
    }
    max_gen = float(rcfg.get("max_generated_frac") or 0.02)
    if rep.generated_frac > max_gen:
        out = no(f"it would make up {rep.generated_frac:.1%} of the garment (max {max_gen:.0%})")
        out[1].update(info)
        return out
    max_lost = float(rcfg.get("max_lost_frac") or 0.08)
    if lost > max_lost:
        out = no(f"it would take away {lost:.1%} of the garment (max {max_lost:.0%})")
        out[1].update(info)
        return out
    piece = fabric_taken(raw, before, after)
    info["fabric_taken"] = round(piece, 4)
    max_piece = float(rcfg.get("max_fabric_taken") or 0.0025)
    if piece > max_piece:
        out = no(f"it would cut away fabric, {piece:.2%} of the garment "
                 f"(max {max_piece:.2%})")
        out[1].update(info)
        return out

    # Neutral under full transparency, as every strategy here writes it: nothing that
    # flattens this onto a colour may show what used to be behind the garment.
    rgba = rgba.copy()
    rgba[rgba[..., 3] == 0] = (255, 255, 255, 0)
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG", optimize=True)
    info.update(refined=True, ms=int((time.perf_counter() - started) * 1000))
    return buf.getvalue(), info
