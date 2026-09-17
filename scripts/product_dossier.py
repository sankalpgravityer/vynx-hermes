#!/usr/bin/env python
"""One page per product, as a PDF: every picture, every attribute, every fault.

    python scripts/product_dossier.py --db "postgresql://..." --sheet list.xlsx
    python scripts/product_dossier.py --db "..." --product <uuid>[,<uuid>] --out report.pdf

WHAT IT IS FOR. Before a batch is repaired, somebody has to LOOK at it. The
audit sheets say `IMAGE DEFECT — AI_CLOSEUP render: …` in a cell, which is the
right shape for a spreadsheet and useless for judging whether the finding is
real. This lays the product out the way a person checks it: the raw
photographs and the cut-outs cut from them side by side, the five renders
beside those, every attribute the rules read, and — under each picture that a
check refused — the sentence naming what is wrong with THAT picture.

Read-only, always. It runs exactly the checks `check_product.py --measure-images
--judge-images` runs (the rules, the cut-out measurement, the image gate, the
photo audit) and writes nothing to the database. Two vision calls per product,
cached, plus the image downloads.

WHY PILLOW AND NOT A PDF LIBRARY. The page is mostly photographs: laying it out
as an image and handing the images to Pillow's own PDF writer needs no
dependency this repo does not already have for the imaging work. The text is
rasterised at 150 dpi, so it prints cleanly and does not select — an acceptable
trade for a document whose job is to be looked at.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import io
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import policy  # noqa: E402

# --------------------------------------------------------------------------- #
# The page
# --------------------------------------------------------------------------- #

DPI = 150
PAGE = (1240, 1754)                      # A4 portrait at 150 dpi
MARGIN = 44
CONTENT = PAGE[0] - 2 * MARGIN

INK = (24, 26, 31)
MUTED = (110, 116, 128)
RULE = (214, 218, 226)
PANEL = (246, 247, 249)
BAD = (192, 42, 42)
BAD_SOFT = (232, 146, 60)
GOOD = (32, 122, 72)
WHITE = (255, 255, 255)

_FONT_DIRS = ["C:/Windows/Fonts", "/usr/share/fonts/truetype/dejavu", "/Library/Fonts"]
_FACES = {
    "regular": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
    "bold": ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"],
    "mono": ["consola.ttf", "cour.ttf", "DejaVuSansMono.ttf"],
}
_cache: dict[tuple[str, int], Any] = {}


def font(face: str, size: int):
    key = (face, size)
    if key not in _cache:
        for name in _FACES[face]:
            for d in _FONT_DIRS:
                p = Path(d) / name
                if p.exists():
                    _cache[key] = ImageFont.truetype(str(p), size)
                    return _cache[key]
        _cache[key] = ImageFont.load_default()
    return _cache[key]


def wrap(text: str, f: Any, width: int, max_lines: int = 99) -> list[str]:
    """Greedy word wrap to a pixel width; the last line is elided when it runs on."""
    words = str(text or "").split()
    lines: list[str] = []
    line = ""
    for w in words:
        trial = f"{line} {w}".strip()
        if f.getlength(trial) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = w
            if len(lines) == max_lines:
                break
    if line and len(lines) < max_lines:
        lines.append(line)
    if len(lines) == max_lines and (len(" ".join(lines).split()) < len(words)):
        last = lines[-1]
        while last and f.getlength(last + " …") > width:
            last = last.rsplit(" ", 1)[0] if " " in last else last[:-1]
        lines[-1] = last + " …"
    return lines


def text(d: ImageDraw.ImageDraw, xy: tuple[int, int], s: str, f: Any,
         fill: tuple[int, int, int] = INK) -> None:
    d.text(xy, s, font=f, fill=fill)


# --------------------------------------------------------------------------- #
# What each picture is, and what is wrong with it
# --------------------------------------------------------------------------- #

GROUPS: list[tuple[str, str]] = [
    ("raw", "GARMENT PHOTOGRAPHS — as taken"),
    ("cutout", "CUT-OUTS — background removed (what the renders are made from)"),
    ("render", "AI RENDERS — what the shopper sees"),
    ("other", "CARE LABEL & SIZE CHART"),
]


def group_of(m: dict[str, Any]) -> str:
    view = str(m.get("view") or "")
    processing = str(m.get("processing") or "")
    if view.startswith("AI_"):
        return "render"
    if view in ("LABEL", "SIZE_CHART"):
        return "other"
    if processing == "BG_REMOVED":
        return "cutout"
    return "raw"


def pictures(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every picture worth showing, grouped and ordered.

    Archived RAW originals ride along: they are the photographs the cut-outs
    were cut from, and half the point of the page is the two side by side.
    """
    out: list[dict[str, Any]] = []
    for m in media:
        if (m.get("mediaType") or "IMAGE") != "IMAGE" or not m.get("url"):
            continue
        if m.get("deletedAt"):
            continue
        out.append({**m, "group": group_of(m)})
    order = {g: i for i, (g, _) in enumerate(GROUPS)}
    out.sort(key=lambda m: (order.get(m["group"], 9), not m.get("isCurrent", True),
                            str(m.get("view") or ""), m.get("position") or 0))
    return out


def image_notes(loaded: dict[str, Any], rep: dict[str, Any],
                pol: dict[str, Any]) -> dict[str, list[str]]:
    """url -> every fault a check found in THAT picture.

    Four sources, each of which knows its picture by a different name: the
    photo audit answers by index into the list it sent, the frame test by view,
    the cut-out measurement by url, the rules by the url in their detail.
    """
    from app.imaging import photo_audit

    media = loaded["media"]
    by_url: dict[str, list[str]] = defaultdict(list)
    by_view: dict[str, list[str]] = defaultdict(list)

    def add_url(url: Any, note: str) -> None:
        if url and note and note not in by_url[str(url)]:
            by_url[str(url)].append(note)

    # 1. The photo audit: one entry per image it was sent, in order.
    #
    # NEVER MORE THAN THE VERDICT CONCLUDED. The model's raw answer is not the
    # finding: `decide()` throws some of it away — an odd-model index pointing
    # at a cut-out, or at so many renders that there is no majority to compare
    # with. Drawing a red border straight from the raw answer put "a different
    # model than the other renders" under KLE-000028's FRONT CUT-OUT, which has
    # no model in it and which the audit had already dismissed. So the odd-model
    # note is only drawn where the verdict kept it (`bad_views`).
    audited = rep.get("audited") or {}
    raw = audited.get("raw") or {}
    if raw:
        sent, _ = photo_audit.select_images(media, pol)
        kept = {str(v) for v in (audited.get("bad_views") or [])}

        def sent_image(i: Any) -> dict[str, Any] | None:
            try:
                i = int(i)
            except (TypeError, ValueError):
                return None
            return sent[i - 1] if 1 <= i <= len(sent) else None

        for entry in raw.get("images") or []:
            if not isinstance(entry, dict):
                continue
            im = sent_image(entry.get("index")) or {}
            if entry.get("ok") is False:
                add_url(im.get("url"), str(entry.get("issue") or "does not match its slot"))
            for part in entry.get("missing_parts") or []:
                add_url(im.get("url"), f"{part} missing from the cut-out")
        if raw.get("same_model") is False:
            for odd in raw.get("odd_renders") or []:
                im = sent_image(odd) or {}
                if im.get("kind") == "render" and str(im.get("view")) in kept:
                    add_url(im.get("url"), "a different model than the other renders")

    # 2. The image gate: the lead it judged, and the frame test over every render.
    judged = rep.get("judged") or {}
    if judged.get("action") in ("regen", "review") and not judged.get("unavailable"):
        for reason in judged.get("reasons") or []:
            add_url(judged.get("lead_url"), str(reason))
    for check in (judged.get("composition") or {}).get("checks") or []:
        if check.get("problem"):
            by_view[str(check.get("view"))].append(f"frame: {check['problem']}")

    # 3. The cut-out measurement, which carries its own urls.
    for check in (rep.get("measured") or {}).get("checks") or []:
        for problem in check.get("problems") or []:
            add_url(check.get("url"), str(problem))
        for flaw in check.get("flaws") or []:
            add_url(check.get("url"), str(flaw))
        if check.get("note"):
            add_url(check.get("url"), str(check["note"]))

    # 4. The rules that name a picture (IMG.026, IMG.027).
    for issue in rep.get("issues") or []:
        detail = issue.get("detail") or {}
        if detail.get("url"):
            add_url(detail["url"], f'{issue["rule_id"]}: {issue["message"]}')

    # Fold the view-keyed notes onto the AI render of that view.
    if by_view:
        for m in media:
            view = str(m.get("view") or "")
            if view in by_view and m.get("isCurrent", True):
                for note in by_view[view]:
                    add_url(m.get("url"), note)
    return dict(by_url)


# --------------------------------------------------------------------------- #
# The attributes, as the rules read them
# --------------------------------------------------------------------------- #

# The findings column: severity, rule id, then the message. Wide enough for
# `PHOTOS:RENDER_DEFECT`, which is the longest id the page can print — at 150
# the id ran straight over the message it belonged to.
FIND_MSG = 248


def collapse(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per rule, worst first, with the repeats counted.

    A product whose extraction was unsure about eight fields produces eight
    identical CONF.001 findings, and on a page with room for eight they push
    the broken render off the bottom. The rule is what a reader acts on; the
    field list belongs beside it, not under it.
    """
    out: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for issue in issues:
        rid = str(issue.get("rule_id"))
        if rid not in seen:
            row = dict(issue)
            row["_fields"] = list(issue.get("fields") or [])
            seen[rid] = row
            out.append(row)
            continue
        row = seen[rid]
        row["_fields"] += [f for f in (issue.get("fields") or []) if f not in row["_fields"]]
        row["_extra"] = row.get("_extra", 0) + 1
        # The worst severity in the group decides the row's colour.
        if issue.get("blocking"):
            row["blocking"] = True
    for row in out:
        extra = row.get("_extra") or 0
        if extra:
            fields = ", ".join(str(f) for f in row["_fields"][:6])
            row["message"] = (f'{row["message"]}  (+{extra} more: {fields}'
                              + ("…)" if len(row["_fields"]) > 6 else ")"))
    return out


def _clean(v: Any) -> str:
    if v is None or v == "" or v == []:
        return "—"
    if isinstance(v, list):
        return ", ".join(str(x) for x in v)
    return str(v)


def attributes(record: dict[str, Any], loaded: dict[str, Any],
               rep: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """(label, value, suspect) — `suspect` when a finding names this field."""
    fields_flagged: set[str] = set()
    for issue in rep.get("issues") or []:
        fields_flagged |= {str(f) for f in issue.get("fields") or []}

    chart = loaded.get("chart") or {}
    media = rep.get("media") or {}
    rows: list[tuple[str, str, str | None]] = [
        ("SKU", record.get("sku"), None),
        ("Stage", f'{record.get("currentStage")} · {record.get("reviewStatus")}', None),
        ("Master category", record.get("masterCategory"), "master_category"),
        ("Category", record.get("category"), "category"),
        ("Subcategory", record.get("subCategory"), "subcategory"),
        ("Gender", record.get("gender"), "gender"),
        ("Size", record.get("size"), "size"),
        ("EU size", record.get("euSize"), "eu_size"),
        ("International size", record.get("internationalSize"), "international_size"),
        ("Sizing guide", record.get("sizingGuide"), "sizing_guide"),
        ("Mannequin", record.get("mannequinType"), "mannequin"),
        ("Brand", record.get("brand"), "brand"),
        ("Colour", record.get("color"), "color"),
        ("Material", record.get("material"), "material"),
        ("Fit", record.get("fit"), "fit"),
        ("Condition", record.get("condition"), "condition"),
        ("Grade", f'{record.get("grade")} · {record.get("gradeLabel") or "—"} '
                  f'({record.get("gradeSeverity") or "—"})', "grade"),
        ("Price", f'{record.get("price")} {record.get("currency") or ""}'.strip(), "price"),
        ("Retail price", record.get("retailPrice"), "retail_price"),
        ("Supplier", record.get("supplier"), "supplier"),
        ("Inventory", record.get("inventoryQuantity"), "inventory"),
        ("Description", f'{len(record.get("summary") or "")} chars', "description"),
        ("Care labels", record.get("careLabelCount"), None),
        ("Renders", f'{len(media.get("renders") or [])}/5', None),
        ("Cut-outs", ", ".join(media.get("cutouts") or []) or "none", None),
        ("Size chart", chart.get("state"), None),
        ("Generation", record.get("generationStatus"), None),
        ("Gallery", "arranged by a person" if record.get("mediaManualOrder") else "catalog order", None),
    ]
    return [(label, _clean(value), bool(field and field in fields_flagged))
            for label, value, field in rows]


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #

def thumb(data: bytes | None, box: tuple[int, int]) -> Image.Image | None:
    if not data:
        return None
    try:
        from PIL import ImageOps

        im = Image.open(io.BytesIO(data))
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            flat = Image.new("RGB", im.size, (250, 250, 250))
            flat.paste(im, mask=im.split()[-1] if im.mode == "RGBA" else None)
            im = flat
        else:
            im = im.convert("RGB")
        im.thumbnail(box, Image.Resampling.LANCZOS)
        return im
    except Exception:  # noqa: BLE001
        return None


def draw_page(rep: dict[str, Any], loaded: dict[str, Any], files: dict[str, bytes | None],
              notes: dict[str, list[str]], pol: dict[str, Any],
              index: int, total: int) -> Image.Image:
    record = loaded["record"]
    page = Image.new("RGB", PAGE, WHITE)
    d = ImageDraw.Draw(page)
    x = MARGIN
    y = MARGIN

    f_title = font("bold", 27)
    f_sku = font("bold", 16)
    f_meta = font("regular", 13)
    f_label = font("bold", 10)
    f_value = font("regular", 14)
    f_group = font("bold", 12)
    f_cap = font("regular", 11)
    f_capb = font("bold", 11)
    f_note = font("regular", 11)
    f_find = font("regular", 12)
    f_findb = font("bold", 12)

    # ---- header -----------------------------------------------------------
    text(d, (x, y), str(record.get("sku") or "—"), f_sku, MUTED)
    text(d, (PAGE[0] - MARGIN - int(f_meta.getlength(f"{index} of {total}")), y + 3),
         f"{index} of {total}", f_meta, MUTED)
    y += 24
    for line in wrap(record.get("title") or "(no title)", f_title, CONTENT, 2):
        text(d, (x, y), line, f_title)
        y += 34
    meta = (f'{record.get("tenantName")}  ·  {record.get("masterCategory")} › '
            f'{record.get("category")} › {record.get("subCategory")}  ·  '
            f'{record.get("currentStage")}  ·  {record.get("id")}')
    text(d, (x, y), meta, f_meta, MUTED)
    y += 26

    # ---- verdict ----------------------------------------------------------
    issues = rep.get("issues") or []
    blockers = [i for i in issues if i.get("blocking")]
    if rep.get("verified"):
        band, colour = "PASSES EVERY BLOCKING RULE", GOOD
    else:
        band = "NOT READY — " + ", ".join(dict.fromkeys(str(i["rule_id"]) for i in blockers))
        colour = BAD
    counts = (f'{len(issues)} finding(s) · {len(blockers)} blocking · '
              f'{len(rep.get("plan") or [])} planned write(s)')
    d.rectangle((x, y, PAGE[0] - MARGIN, y + 46), fill=PANEL)
    d.rectangle((x, y, x + 5, y + 46), fill=colour)
    text(d, (x + 16, y + 7), wrap(band, f_findb, CONTENT - 40, 1)[0], f_findb, colour)
    text(d, (x + 16, y + 26), counts, f_meta, MUTED)
    y += 60

    # ---- attributes -------------------------------------------------------
    rows = attributes(record, loaded, rep)
    cols = 4
    per_col = (len(rows) + cols - 1) // cols
    col_w = CONTENT // cols
    top = y
    for i, (label, value, suspect) in enumerate(rows):
        cx = x + (i // per_col) * col_w
        cy = top + (i % per_col) * 34
        text(d, (cx, cy), label.upper(), f_label, MUTED)
        vlines = wrap(value, f_value, col_w - 14, 1)
        text(d, (cx, cy + 13), vlines[0] if vlines else "—", f_value,
             BAD if suspect else INK)
    y = top + per_col * 34 + 8
    d.line((x, y, PAGE[0] - MARGIN, y), fill=RULE, width=1)
    y += 14

    # ---- findings, measured from the bottom up ----------------------------
    #
    # The faults are the point of the page, so they get their space first and
    # the pictures take what is left — never the other way round.
    rolled = collapse(issues)
    shown = rolled[:8]
    find_h = 26 + 4
    heights: list[int] = []
    for i in shown:
        lines = len(wrap(i["message"], f_find, CONTENT - FIND_MSG, 2))
        lines += len(wrap(i["fix"]["text"], f_note, CONTENT - FIND_MSG - 16, 1))
        h = 6 + lines * 15
        heights.append(h)
        find_h += h
    if len(rolled) > len(shown):
        find_h += 18
    find_top = PAGE[1] - MARGIN - find_h

    # ---- pictures ---------------------------------------------------------
    pics = [p for p in pictures(loaded["media"])]
    by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in pics:
        by_group[p["group"]].append(p)
    live_groups = [(g, title) for g, title in GROUPS if by_group.get(g)]

    per_row = 6
    cell_w = CONTENT // per_row
    rows_needed = sum((len(by_group[g]) + per_row - 1) // per_row for g, _ in live_groups)
    avail = find_top - y - 10
    # Each group costs a header; each row an image box plus its caption block.
    cap_h = 46
    box_h = int((avail - len(live_groups) * 24 - rows_needed * (cap_h + 10)) / max(1, rows_needed))
    box_h = max(96, min(box_h, 232))
    box_w = cell_w - 14

    for g, title in live_groups:
        text(d, (x, y), title, f_group, MUTED)
        y += 22
        for start in range(0, len(by_group[g]), per_row):
            chunk = by_group[g][start:start + per_row]
            for i, m in enumerate(chunk):
                cx = x + i * cell_w
                url = str(m["url"])
                faults = notes.get(url) or []
                frame = BAD if faults else RULE
                d.rectangle((cx, y, cx + box_w, y + box_h), outline=frame,
                            width=3 if faults else 1, fill=(252, 252, 253))
                im = thumb(files.get(url), (box_w - 8, box_h - 8))
                if im is not None:
                    page.paste(im, (cx + (box_w - im.width) // 2,
                                    y + (box_h - im.height) // 2))
                else:
                    miss = "not downloaded"
                    text(d, (cx + (box_w - int(f_cap.getlength(miss))) // 2,
                             y + box_h // 2 - 6), miss, f_cap, MUTED)
                cy = y + box_h + 4
                head = str(m.get("view") or "?")
                if not m.get("isCurrent", True):
                    head += " (archived)"
                text(d, (cx, cy), head, f_capb, BAD if faults else INK)
                dims = (f'{m.get("width")}×{m.get("height")}'
                        if m.get("width") and m.get("height") else "size unknown")
                text(d, (cx, cy + 13), f'{m.get("origin") or "—"} · {dims}', f_cap, MUTED)
                if faults:
                    line = wrap(faults[0], f_note, box_w, 1)[0]
                    text(d, (cx, cy + 27), line, f_note, BAD)
                    if len(faults) > 1:
                        text(d, (cx + box_w - 26, cy), f"+{len(faults) - 1}", f_cap, BAD)
            y += box_h + cap_h + 10
        y += 2

    # ---- the findings themselves ------------------------------------------
    y = find_top
    d.line((x, y, PAGE[0] - MARGIN, y), fill=RULE, width=1)
    y += 10
    text(d, (x, y), f"WHAT IS WRONG, AND WHAT --apply WOULD DO", f_group, MUTED)
    y += 22
    for issue, h in zip(shown, heights):
        sev = str(issue.get("severity") or "").upper()
        colour = BAD if issue.get("blocking") else BAD_SOFT if sev in ("HIGH", "CRITICAL") else MUTED
        text(d, (x, y), sev[:8], f_findb, colour)
        rule = wrap(str(issue["rule_id"]), f_findb, FIND_MSG - 78, 1)[0]
        text(d, (x + 64, y), rule, f_findb, INK)
        ty = y
        for line in wrap(str(issue["message"]), f_find, CONTENT - FIND_MSG, 2):
            text(d, (x + FIND_MSG, ty), line, f_find, INK)
            ty += 15
        for line in wrap("→ " + str(issue["fix"]["text"]), f_note, CONTENT - FIND_MSG - 16, 1):
            text(d, (x + FIND_MSG + 16, ty), line, f_note, MUTED)
            ty += 15
        y += h
    if len(rolled) > len(shown):
        text(d, (x, y), f"+ {len(rolled) - len(shown)} more finding(s) — see the JSON",
             f_note, MUTED)
    return page


# --------------------------------------------------------------------------- #
# Reading the product list
# --------------------------------------------------------------------------- #

def ids_from_sheet(path: Path) -> list[str]:
    """Product ids from any sheet that carries a column of them.

    ONE implementation, in `audit_review_products`, because `repair_product.py
    --from-sheet` reads the same files and two readers that disagree about
    which column holds the ids is a bug nobody would look for.
    """
    from scripts.audit_review_products import read_sheet_ids

    return read_sheet_ids(path)


# --------------------------------------------------------------------------- #
# One product
# --------------------------------------------------------------------------- #

def build(dsn: str, product_id: str, pol: dict[str, Any], *, judge: bool,
          measure: bool) -> tuple[dict[str, Any], dict[str, Any], dict[str, bytes | None], dict[str, list[str]]]:
    from app.net import fetch_all
    from scripts import check_product

    loaded = product_audit.load(dsn, product_id)
    rep = check_product.inspect(dsn, product_id, measure=measure, judge=judge, loaded=loaded)
    urls = [str(p["url"]) for p in pictures(loaded["media"])]
    files = fetch_all(urls, timeout_s=30, deadline_s=120) if urls else {}
    notes = image_notes(loaded, rep, pol)
    return rep, loaded, files, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db", help="database URL; else DATABASE_URL")
    ap.add_argument("--sheet", "--from-sheet", dest="sheet",
                    help="xlsx carrying a column of product ids")
    ap.add_argument("--product", "--products", dest="products",
                    help="one uuid, or a comma-separated list")
    ap.add_argument("--limit", type=int, help="only the first N of the sheet")
    ap.add_argument("--out", help="the PDF to write (default reports/generated/dossier-<date>.pdf)")
    ap.add_argument("--json", help="also write every report as JSON")
    ap.add_argument("--no-judge", action="store_true",
                    help="skip the image gate and the photo audit (no vision calls)")
    ap.add_argument("--no-measure", action="store_true",
                    help="skip the cut-out measurement")
    ap.add_argument("--workers", type=int, default=3,
                    help="products prepared in parallel (downloads dominate)")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass

    from app.config import settings

    dsn = args.db or settings().database_url
    if not dsn:
        print("Pass --db or set DATABASE_URL.", file=sys.stderr)
        return 2

    ids: list[str] = []
    if args.sheet:
        ids = ids_from_sheet(Path(args.sheet))
        if not ids:
            print(f"No product ids found in {args.sheet}", file=sys.stderr)
            return 1
    elif args.products:
        ids = [s.strip() for s in args.products.split(",") if s.strip()]
    if not ids:
        print("Pass --sheet <xlsx> or --product <uuid[,uuid]>.", file=sys.stderr)
        return 2
    available = len(ids)
    if args.limit:
        ids = ids[:args.limit]

    out = Path(args.out) if args.out else (
        product_audit.REPORT_DIR /
        f'dossier-{datetime.now().strftime("%Y-%m-%d-%H%M%S")}.pdf')
    pol = policy()
    judge, measure = not args.no_judge, not args.no_measure

    print(f'{available} product(s) from '
          f'{Path(args.sheet).name if args.sheet else "--product"}'
          + (f' — the first {len(ids)}' if len(ids) != available else ''))
    print(f'READ ONLY. The rules'
          + (', the cut-out measurement' if measure else '')
          + (', the image gate and the photo audit (two vision calls each, cached)'
             if judge else '')
          + '. Nothing is written to the database.\n')

    started = time.perf_counter()
    prepared: dict[str, Any] = {}
    failures: list[tuple[str, str]] = []

    def work(pid: str) -> tuple[str, Any]:
        try:
            return pid, build(dsn, pid, pol, judge=judge, measure=measure)
        except Exception as exc:  # noqa: BLE001
            return pid, exc

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        for n, (pid, result) in enumerate(pool.map(work, ids), start=1):
            if isinstance(result, Exception):
                failures.append((pid, f"{type(result).__name__}: {result}"))
                print(f'  [{n}/{len(ids)}] {pid}  FAILED — {type(result).__name__}: {result}')
                continue
            prepared[pid] = result
            rep = result[0]
            got = sum(1 for v in result[2].values() if v)
            print(f'  [{n}/{len(ids)}] {rep.get("sku") or pid}  '
                  f'{len(rep.get("issues") or [])} finding(s) · '
                  f'{got}/{len(result[2])} image(s) · '
                  f'{"blocked" if not rep.get("verified") else "clean"}')

    pages: list[Image.Image] = []
    ordered = [p for p in ids if p in prepared]
    for i, pid in enumerate(ordered, start=1):
        rep, loaded, files, notes = prepared[pid]
        pages.append(draw_page(rep, loaded, files, notes, pol, i, len(ordered)))

    if not pages:
        print("Nothing to write.", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    pages[0].save(out, "PDF", resolution=DPI, save_all=True, append_images=pages[1:])
    print(f'\n{len(pages)} page(s) → {out}  ({time.perf_counter() - started:.0f}s)')
    if failures:
        print(f'{len(failures)} product(s) could not be prepared:')
        for pid, why in failures:
            print(f'  {pid}  {why}')

    if args.json:
        import json

        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(
            {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "products": [prepared[p][0] for p in ordered]},
            indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        print(f'json: {args.json}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
