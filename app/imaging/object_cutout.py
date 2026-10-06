"""Shoes and bags: the whole PRODUCT cut out of the photo with IS-Net (6 Oct 2026).

WHY NOT THE GARMENT PARSERS. cloth-seg v2 / v1 are clothing parsers: they have no class for a
shoe or a bag. Measured on 18 shoe and bag photos (BOAS, Klekt; 6 Oct 2026): on shoes v2 found
"no garment" or kept torn fragments on 11 of 12; on bags it ACCEPTED two cut-outs with half a
handle missing. SegFormer's clothes parser (mattmdjaga/segformer_b2_clothes, which has shoe and
bag labels) was tried too: it is a HUMAN parser, and with no person in the frame it shredded
every shoe and called a pair of trainers "Dress 66%". IS-Net (isnet-general-use, already on the
server for the hanger strategy) is a salient-object model, and a shoe on a table or a bag on a
hook is exactly the salient object: both shoes whole, laces and straps kept, the table gone.
BiRefNet-lite agreed with it (IoU 0.99+) but took 90-280 s a photo on CPU.

    raw ─► IS-Net, full frame: where the product is (every large piece — a pair of shoes apart
        is two — and everything thin attached to one: laces, ties, straps)
        ─► IS-Net again on a crop round it, for resolution; it may only ADD to the first pass
        ─► the wall wire (hanger_cutout.remove_wall_wire), far specks, and thin strands that run
           to the frame's edge (a table's edge, a seam) are dropped

Design rule, the hanger script's: nothing solid is removed. Only thin strands that REACH THE
FRAME'S EDGE are cut — a product's own lace does not run off the photograph.

`checks()` replaces the garment checks for these products: the backdrop checks read grey and
white leather on a grey studio table as "the backdrop colour" (a single 3-24% region on 10 of 12
clean shoe cut-outs), and the tear check read the wall seen through a bag's handle as a tear.
"""
from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

# WORD-BOUNDARY, like the footwear and accessory lists in app/imaging/nanobanana.py: a substring
# "boot" would send BOOTCUT jeans here, and "bag" BAGGY trousers.
FAMILIES: dict[str, re.Pattern[str]] = {
    "footwear": re.compile(
        r"\b(footwear|shoes?|sneakers?|trainers?|boots?|sandals?|heels?|loafers?|pumps?|mules?"
        r"|clogs?|espadrilles?|slippers?)\b", re.IGNORECASE),
    "bags": re.compile(
        r"\b(bags?|backpacks?|rucksacks?|handbags?|totes?|purses?|satchels?|clutch(?:es)?"
        r"|wallets?)\b", re.IGNORECASE),
}


def family_of(garment: str | None, families: list[str] | None = None) -> str | None:
    """'footwear' | 'bags' | None for the product's "Category Subcategory" text."""
    if not garment:
        return None
    for name in families or list(FAMILIES):
        rx = FAMILIES.get(str(name))
        if rx and rx.search(garment):
            return str(name)
    return None


@dataclass
class Config:
    model: str = "isnet-general-use"
    min_piece_share: float = 0.05   # a solid piece this share of the largest is the product
    solid_px: int = 25              # opening that tells a solid piece from a strand (full res)
    crop_pad_frac: float = 0.08     # context round the product for the crop pass
    edge_frac: float = 0.005        # the frame-edge band, as a share of the long side
    core_frac: float = 0.04         # the product's core: what survives an opening this share of its size
    strip_elongation: float = 5.0   # a straight strip is at least this much longer than wide
    strip_max_share: float = 0.05   # ... and at most this share of the product


@dataclass
class Report:
    pieces: int = 0
    wire_lines_removed: int = 0
    specks_removed: int = 0
    edge_strands_removed: int = 0
    crop_missed: float = 0.0        # share of the 1st pass the crop pass did not see (kept anyway)
    seconds: float = 0.0
    notes: list = field(default_factory=list)


def _pieces(a: np.ndarray, cfg: Config) -> tuple[np.ndarray, int]:
    """The product: every solid piece at least `min_piece_share` of the largest, plus
    everything attached to one. (mask, number of solid pieces)."""
    hard = (a > 0.5).astype(np.uint8)
    k = max(3, int(cfg.solid_px))
    solid = cv2.morphologyEx(hard, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    if n <= 1:
        return np.zeros(a.shape, bool), 0
    areas = stats[1:, cv2.CC_STAT_AREA]
    big = [i + 1 for i, s in enumerate(areas) if s >= cfg.min_piece_share * areas.max()]
    body = np.isin(lbl, big)
    _n2, lbl2 = cv2.connectedComponents(hard, connectivity=8)
    attached = np.unique(lbl2[body])
    return np.isin(lbl2, attached[attached > 0]), len(big)


def _edge_band(shape: tuple[int, int], frac: float) -> np.ndarray:
    H, W = shape
    e = max(4, int(round(frac * max(H, W))))
    band = np.zeros((H, W), bool)
    band[:e], band[-e:], band[:, :e], band[:, -e:] = True, True, True, True
    return band


def _elongation(ys: np.ndarray, xs: np.ndarray) -> float:
    """Length over width of a set of pixels (the ratio of its principal axes)."""
    if ys.size < 10:
        return 0.0
    cov = np.cov(np.vstack([xs, ys]).astype(np.float64))
    lo, hi = sorted(np.linalg.eigvalsh(cov))
    return float(np.sqrt(hi / max(lo, 1e-6)))


def _drop_edge_strands(a: np.ndarray, cfg: Config, rep: Report) -> np.ndarray:
    """Straight strips outside the product's core that run to the frame's edge: a table's
    lit edge, a seam in the wall. KLE-002828 and -2829: the table's right-hand edge, a strip
    30-40 px wide from the heel to the side of the frame, kept by IS-Net with the shoe.

    Only what is OUTSIDE the core (an opening `core_frac` of the product's size), TOUCHES the
    frame's edge, is a straight strip (`strip_elongation`) and is small (`strip_max_share`)
    goes. An arm reaching in, or a product the photo cuts off, is none of those and stays —
    for the edge check to refuse."""
    hard = a > 0.5
    if not hard.any():
        return a
    ys, xs = np.where(hard)
    size = max(int(ys.max() - ys.min()), int(xs.max() - xs.min()))
    k = max(9, int(cfg.core_frac * size)) | 1
    core = cv2.morphologyEx(hard.astype(np.uint8), cv2.MORPH_OPEN,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))) > 0
    rest = hard & ~core
    n, lbl = cv2.connectedComponents(rest.astype(np.uint8), connectivity=8)
    if n <= 1:
        return a
    band = _edge_band(a.shape, cfg.edge_frac)
    touching = np.unique(lbl[band & rest])
    out = a
    total = float(hard.sum())
    for i in touching[touching > 0]:
        sy, sx = np.where(lbl == i)
        if sy.size > cfg.strip_max_share * total or _elongation(sy, sx) < cfg.strip_elongation:
            continue
        if out is a:
            out = a.copy()
        out[sy, sx] = 0
        rep.edge_strands_removed += 1
    return out


def cutout(rgb: np.ndarray, cfg: Config | None = None) -> tuple[np.ndarray, Report]:
    """The product's alpha (float32 0..1, the photo's size) for one upright RGB photo."""
    from app.imaging import hanger_cutout as hc

    cfg = cfg or Config()
    rep = Report()
    t = time.perf_counter()
    seg = hc.segmenter(cfg.model)
    H, W = rgb.shape[:2]
    a1 = seg._mask(rgb)
    keep1, _ = _pieces(a1, cfg)
    if not keep1.any():
        rep.notes.append("no product found")
        rep.seconds = round(time.perf_counter() - t, 1)
        return np.zeros((H, W), np.float32), rep
    ys, xs = np.where(keep1)
    p = int(cfg.crop_pad_frac * max(int(ys.max() - ys.min()), int(xs.max() - xs.min())))
    x0, y0 = max(0, int(xs.min()) - p), max(0, int(ys.min()) - p)
    x1, y1 = min(W, int(xs.max()) + p + 1), min(H, int(ys.max()) + p + 1)
    a2 = np.zeros((H, W), np.float32)
    a2[y0:y1, x0:x1] = seg._mask(np.ascontiguousarray(rgb[y0:y1, x0:x1]))
    # THE CROP PASS ONLY ADDS: it sees the product at a higher resolution, and where it
    # drops something the full frame kept, the full frame's answer stands.
    rep.crop_missed = round(float((keep1 & (a1 > 0.5) & (a2 <= 0.5)).sum()) / max(1, int(keep1.sum())), 4)
    a = np.maximum(np.where(keep1, a1, 0), a2).astype(np.float32)
    keep, rep.pieces = _pieces(a, cfg)
    # Soft edges just outside the hard mask belong to it; everything else the crop pass
    # found (the wall, a second object in the crop) does not.
    near = cv2.dilate(keep.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    a = np.where(near, a, 0).astype(np.float32)
    k = max(3, int(cfg.solid_px))
    solid = cv2.morphologyEx((a > 0.5).astype(np.uint8), cv2.MORPH_OPEN, np.ones((k, k), np.uint8)) > 0
    hrep = hc.Report()
    a = hc.remove_wall_wire(a, solid, hrep)
    a = hc.drop_far_specks(a, hc.Config(), hrep)
    rep.wire_lines_removed, rep.specks_removed = hrep.wire_lines_removed, hrep.specks_removed
    a = _drop_edge_strands(a, cfg, rep)
    rep.seconds = round(time.perf_counter() - t, 1)
    return a, rep


# --------------------------------------------------------------------------- #
# The checks a shoe or bag cut-out has to pass (instead of the garment ones)
# --------------------------------------------------------------------------- #

def _gaps_read(raw: np.ndarray, mask: np.ndarray, pcfg: dict[str, Any]) -> list[tuple[int, float]]:
    """Every gap in the product — an enclosed hole, or a narrow bite out of the outline —
    with the share of it the photo shows in the PRODUCT's colour rather than the wall's.
    [(pixels, product-coloured share)]."""
    from app.imaging.cutout import _photo_says_garment

    m8 = mask.astype(np.uint8)
    flood = np.pad(1 - m8, 1, constant_values=1)
    cv2.floodFill(flood, None, (0, 0), 2)
    enclosed = (flood[1:-1, 1:-1] == 1) & ~mask
    unit = max(mask.shape) / 1024.0
    c = max(3, int(round(float(pcfg.get("torn_close_px") or 15) * unit))) | 1
    closed = cv2.morphologyEx(m8, cv2.MORPH_CLOSE, np.ones((c, c), np.uint8)).astype(bool)
    rim = max(1, int(round(float(pcfg.get("torn_rim_px") or 2) * unit)))
    dist_out = cv2.distanceTransform(1 - m8, cv2.DIST_L2, 5)
    gaps = enclosed | (closed & ~mask & (dist_out > rim))
    coloured = _photo_says_garment(raw, mask, pcfg)
    if coloured is None:
        return []
    n, lbl, stats, _ = cv2.connectedComponentsWithStats(gaps.astype(np.uint8), connectivity=8)
    # THE SPACE BETWEEN TWO SHOES IS NOT A TEAR. The closing bridges a pair standing close
    # together, and on a grey table beside grey leather the table there reads as the
    # product's colour (KLE-002829: 0.4% "torn", all of it between the left heel and the
    # right shoe's laces). A bite out of the outline that borders two separate pieces is
    # that space, and is left out.
    _np, pieces = cv2.connectedComponents(m8, connectivity=8)
    ring = np.ones((5, 5), np.uint8)
    out = []
    for i in range(1, n):
        x, y, w, h = (int(stats[i, j]) for j in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                                                  cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
        y0, y1, x0, x1 = max(0, y - 3), y + h + 3, max(0, x - 3), x + w + 3
        this = (lbl[y0:y1, x0:x1] == i)
        if not enclosed[y0:y1, x0:x1][this].any():
            near = cv2.dilate(this.astype(np.uint8), ring) > 0
            if len({int(v) for v in np.unique(pieces[y0:y1, x0:x1][near]) if v > 0}) >= 2:
                continue
        out.append((int(stats[i, cv2.CC_STAT_AREA]), float(coloured[y0:y1, x0:x1][this].mean())))
    return out


def checks(source: bytes, result: bytes, ocfg: dict[str, Any] | None = None,
           pcfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Is this a clean cut-out of a shoe or a bag? (ok, why)

    1. SOMETHING, AND NOT EVERYTHING: the product is at least `min_kept` and at most
       `max_kept` of the frame. An empty PNG passes every garment check (6 Oct 2026: a
       SegFormer cut-out keeping 0.0% of the frame was "accepted").
    2. CLEAR OF THE FRAME'S EDGE: a table top, the wall or a person's arm runs off the
       photograph; a shoe or a bag does not. KLE-002830 (the boots held up by a person:
       the arm reaches the right-hand edge) is refused here.
    3. NOT TORN: each gap is read as a whole. The wall seen through a bag's handle (8-30%
       of it object-coloured — pencil marks, the seam) is an opening; a gap at least
       `tear_coloured` object-coloured is a tear, and tears adding up to `torn_min` of
       the product refuse it. 0.9, not a half: on 70 held-out photos (6 Oct 2026) every
       gap read between 0.53 and 0.89 was shaded table or wall beside a dark sole or
       handle (7 clean cut-outs refused at 0.5, none at 0.9); a piece of leather the
       mask dropped is the leather's colour all through.
    """
    from PIL import Image

    ocfg = ocfg or {}
    pcfg = pcfg or {}
    try:
        cut = Image.open(io.BytesIO(result)).convert("RGBA")
        src = Image.open(io.BytesIO(source)).convert("RGB")
    except Exception as exc:  # noqa: BLE001
        return False, f"could not be decoded ({exc.__class__.__name__})"
    work = int(pcfg.get("torn_work_px") or 1024)
    k = min(1.0, work / float(max(cut.size)))
    size = (max(8, int(round(cut.width * k))), max(8, int(round(cut.height * k))))
    mask = np.asarray(cut.getchannel("A").resize(size, Image.Resampling.NEAREST)) >= 128
    kept = float(mask.mean())
    lo, hi = float(ocfg.get("min_kept") or 0.003), float(ocfg.get("max_kept") or 0.70)
    if kept < lo:
        return False, f"almost nothing kept ({kept:.2%} of the frame; at least {lo:.1%})"
    if kept > hi:
        return False, f"{kept:.0%} of the frame kept — the background was not removed (at most {hi:.0%})"
    band = _edge_band(mask.shape, float(ocfg.get("edge_frac") or 0.005))
    on_edge = float((mask & band).sum()) / float(band.sum())
    max_edge = float(ocfg.get("max_edge") if ocfg.get("max_edge") is not None else 0.002)
    if on_edge > max_edge:
        return False, (f"the cut-out reaches the frame's edge ({on_edge:.2%} of it) — a table, the "
                       f"wall or a person was kept, or the product runs off the photo")
    raw = np.asarray(src.resize(size, Image.Resampling.BILINEAR)).astype(np.float32)
    need = float(ocfg.get("tear_coloured") or 0.9)
    gaps = _gaps_read(raw, mask, pcfg)
    torn = sum(px for px, share in gaps if share >= need)
    share = torn / float(mask.sum())
    floor = float(pcfg.get("torn_min") or 0.0025)
    if share >= floor:
        return False, (f"the mask tore {share:.1%} of the product out — gaps the photo shows in the "
                       f"product's own colour, not the wall's")
    openings = sum(1 for _px, s in gaps if s < need)
    return True, (f"product {kept:.1%} of the frame, clear of the edge ({on_edge:.2%}), "
                  f"{openings} opening(s), torn {share:.2%}")
