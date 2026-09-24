"""Build the comparison PDF from a finished bench run.

ONE SECTION PER PRODUCT, and each section answers a different question:

  1. Summary       what this product is, which class each builder resolved it to,
                   which human model wore it, and what the twenty renders cost.
  2. Grid          all 4 arms x 5 views on one page — "which stack is better" at
                   a glance, thumbnails.
  3. Per view      the same view from all 4 arms, side by side and large enough
                   to judge — five of these, one per view. This is where a crop
                   that came back as the wrong framing is actually visible.
  4. Prompts       the full text sent by each builder, for every view, so a
                   difference in the pictures can be traced to a difference in
                   the words.

Images are DOWNSCALED on the way in (350px for the grid, 900px for the per-view
pages). Embedding the originals would produce a 500 MB file nobody can open, and
the comparison does not need 2048px to show that a close-up came back as a
full-body shot.

    python scripts/bench_pdf.py
    python scripts/bench_pdf.py --out "C:/path/report.pdf"
"""

from __future__ import annotations

import argparse
import io
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
    PageTemplate, Paragraph, Spacer, Table, TableStyle,
)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from bench_images import (  # noqa: E402
    ARMS, OUT_ROOT, PRICES, VIEW_LABELS, cost_of,
)

PAGE = landscape(A4)
MARGIN = 14 * mm

INK = colors.HexColor("#1a1a1a")
MUTED = colors.HexColor("#6b6b6b")
RULE = colors.HexColor("#d8d8d8")
BAND = colors.HexColor("#f2f2f2")
VNYX = colors.HexColor("#8c3b00")
HERMES = colors.HexColor("#00456b")

ARM_ORDER = [a.key for a in ARMS]
ARM_BY_KEY = {a.key: a for a in ARMS}


def styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=19, leading=23,
                             textColor=INK, spaceAfter=4),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, leading=16,
                             textColor=INK, spaceBefore=8, spaceAfter=5),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontSize=10.5, leading=13,
                             textColor=INK, spaceBefore=6, spaceAfter=3),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontSize=8.8,
                               leading=12, textColor=INK, alignment=TA_LEFT),
        "small": ParagraphStyle("small", parent=base["BodyText"], fontSize=7.4,
                                leading=9.5, textColor=MUTED),
        "cell": ParagraphStyle("cell", parent=base["BodyText"], fontSize=7.6,
                               leading=9.5, textColor=INK),
        "mono": ParagraphStyle("mono", parent=base["BodyText"], fontName="Courier",
                               fontSize=6.2, leading=7.6, textColor=INK),
        "cap": ParagraphStyle("cap", parent=base["BodyText"], fontSize=6.4,
                              leading=7.8, textColor=MUTED),
    }


S = styles()


def scaled(path: Path, long_edge: int) -> tuple[io.BytesIO, int, int] | None:
    """Downscale once, into memory, as (buffer, width, height).

    A BytesIO rather than an ImageReader because platypus' Image() wants a
    filename or a file-like object and calls os.path.splitext on anything else.
    Returns None for a file that will not open — a missing render must not take
    the whole report down.
    """
    try:
        from PIL import Image as PILImage

        with PILImage.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((long_edge, long_edge), PILImage.Resampling.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=82)
            buf.seek(0)
            return buf, img.width, img.height
    except Exception:  # noqa: BLE001
        return None


def fitted(img: tuple[io.BytesIO, int, int], box_w: float, box_h: float) -> Image:
    buf, iw, ih = img
    buf.seek(0)
    scale = min(box_w / iw, box_h / ih)
    return Image(buf, width=iw * scale, height=ih * scale)


def money(x: float | None) -> str:
    return "—" if x is None else f"${x:,.4f}"


def grid_table(data: list[list[Any]], widths: list[float], header: bool = True,
               align_right: list[int] | None = None) -> Table:
    t = Table(data, colWidths=widths, repeatRows=1 if header else 0)
    cmds = [
        ("FONTSIZE", (0, 0), (-1, -1), 7.6),
        ("LEADING", (0, 0), (-1, -1), 9.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), INK),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if header:
        cmds += [
            ("BACKGROUND", (0, 0), (-1, 0), BAND),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, INK),
        ]
    for col in align_right or []:
        cmds.append(("ALIGN", (col, 0), (col, -1), "RIGHT"))
    t.setStyle(TableStyle(cmds))
    return t


def arm_label(key: str) -> str:
    a = ARM_BY_KEY[key]
    tier = f" · {a.quality}" if a.quality else ""
    return f"{a.prompt.upper()} prompt\n{a.model}{tier}"


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_runs(root: Path) -> list[dict[str, Any]]:
    runs = []
    for ctx_path in sorted(root.glob("*/*/context.json")):
        usage = ctx_path.parent / "usage.jsonl"
        if not usage.exists():
            continue
        ctx = json.loads(ctx_path.read_text(encoding="utf-8"))
        rows = [json.loads(l) for l in usage.read_text(encoding="utf-8").splitlines() if l.strip()]
        # A run recorded under an older arm layout would index ARM_BY_KEY on keys
        # this build does not have and take the whole report down. Skip it with a
        # word rather than half-render it.
        unknown = {r["arm"] for r in rows} - set(ARM_BY_KEY)
        if unknown:
            print(f"skipping {ctx_path.parent}: arms not in this build "
                  f"({', '.join(sorted(unknown))}) — re-run it")
            continue
        for r in rows:
            r["cost"] = cost_of(r["model"], r.get("usage") or {})
        prompts: dict[str, dict[str, str]] = defaultdict(dict)
        for p in sorted(ctx_path.parent.glob("prompt__*.txt")):
            _, builder, view = p.stem.split("__")
            prompts[builder][view] = p.read_text(encoding="utf-8")
        runs.append({"ctx": ctx, "rows": rows, "dir": ctx_path.parent,
                     "prompts": prompts})
    return runs


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

def cover(runs: list[dict[str, Any]]) -> list[Any]:
    all_rows = [r for run in runs for r in run["rows"]]
    total = sum(r["cost"] for r in all_rows if r["cost"] is not None)
    ok = sum(1 for r in all_rows if r["ok"])
    person = {run["ctx"]["gender"]: run["ctx"]["personality"] for run in runs}

    flow: list[Any] = [
        Paragraph("On-model renders — vnyx-api vs Hermes, four production stacks",
                  S["h1"]),
        Paragraph(
            f"{len(runs)} products · {len(all_rows)} renders · {ok} succeeded · "
            f"metered total {money(total)}. Every render was produced from the "
            f"same local source photographs, with the human model pinned per "
            f"gender from the local database's model library so that any "
            f"difference between two pictures is the prompt or the model, never "
            f"a different person.", S["body"]),
        Spacer(1, 6),
        Paragraph("The four arms", S["h2"]),
    ]

    per_arm = defaultdict(list)
    for r in all_rows:
        per_arm[r["arm"]].append(r)

    data = [["Arm", "Prompt", "Model", "Tier", "OK", "Mean s",
             "Mean out tok", "$/render", "$ / 5-render product"]]
    for key in ARM_ORDER:
        rows = per_arm.get(key)
        if not rows:
            continue
        a = ARM_BY_KEY[key]
        good = [r for r in rows if r["ok"]]
        costs = [r["cost"] for r in rows if r["cost"] is not None]
        out = [r["usage"].get("output_tokens") for r in good
               if (r.get("usage") or {}).get("output_tokens")]
        mean_cost = sum(costs) / len(costs) if costs else None
        data.append([
            key, a.prompt, a.model, a.quality or "—",
            f"{len(good)}/{len(rows)}",
            f"{sum(r['seconds'] for r in good) / len(good):.1f}" if good else "—",
            f"{sum(out) // len(out):,}" if out else "—",
            money(mean_cost),
            money(mean_cost * 5) if mean_cost else "—",
        ])
    flow.append(grid_table(data, [92, 44, 132, 38, 36, 40, 60, 54, 86],
                           align_right=[5, 6, 7, 8]))

    flow += [
        Spacer(1, 8),
        Paragraph("The human model, from the local model library", S["h2"]),
    ]
    pdata = [["Gender", "Name", "Age", "Skin tone", "Hair", "Piercings"]]
    for gender, p in sorted(person.items()):
        pdata.append([
            gender, p.get("name", "—"), p.get("age", "—"),
            p.get("skinTone") or "—",
            " ".join(x for x in (p.get("hairColor"), p.get("hairStyle")) if x) or "—",
            p.get("piercings") or "—",
        ])
    flow.append(grid_table(pdata, [50, 80, 30, 80, 200, 130]))

    flow += [
        Spacer(1, 8),
        Paragraph("Token prices used", S["h2"]),
        Paragraph(
            "Confirmed against the vendors' own pricing pages on 22 September 2026. "
            "Cost is computed from the token counts each provider reported for each "
            "call, not from a list price per image — image input is billed "
            "separately by OpenAI and every call here carries two or three "
            "reference photographs.", S["small"]),
    ]
    prdata = [["Model", "Text in $/M", "Image in $/M", "Image out $/M"]]
    used = {r["model"] for r in all_rows}
    for model, p in PRICES.items():
        if model in used:
            prdata.append([model, f"{p['in']:.2f}", f"{p['image_in']:.2f}",
                           f"{p['out']:.2f}"])
    flow.append(grid_table(prdata, [180, 80, 80, 90], align_right=[1, 2, 3]))
    return flow


def product_summary(run: dict[str, Any]) -> list[Any]:
    ctx, rows = run["ctx"], run["rows"]
    flow: list[Any] = [
        Paragraph(f"{ctx['product']}", S["h1"]),
        Paragraph(
            f"Filed as <b>{ctx['category']} / {ctx['subCategory']}</b> · "
            f"mannequinType <b>{ctx['mannequinType']}</b> · "
            f"Hermes resolved the class to <b>{ctx['hermesClass'] or 'none'}</b> · "
            f"worn by <b>{ctx['personality'].get('name')}</b> "
            f"({ctx['personality'].get('age')}, {ctx['gender']}) · group "
            f"{ctx['group']}", S["body"]),
        Spacer(1, 6),
    ]

    # Source photographs beside the cost table.
    src_cells: list[Any] = []
    folder = Path(ctx.get("_srcdir", "")) if ctx.get("_srcdir") else None
    for label in ("front", "back"):
        name = (ctx.get("sources") or {}).get(label)
        if not name:
            continue
        path = (run["dir"].parent.parent.parent / ctx["product"] / name)
        reader = scaled(path, 260)
        if reader:
            src_cells.append([fitted(reader, 92, 122),
                              Paragraph(f"source · {label}", S["cap"])])

    per_arm = defaultdict(list)
    for r in rows:
        per_arm[r["arm"]].append(r)

    data = [["Arm", "OK", "Mean s", "In tok", "Out tok", "$/render",
             "$ for 5 renders"]]
    total = 0.0
    for key in ARM_ORDER:
        group = per_arm.get(key)
        if not group:
            continue
        good = [r for r in group if r["ok"]]
        costs = [r["cost"] for r in group if r["cost"] is not None]
        tin = [r["usage"].get("input_tokens") for r in good
               if (r.get("usage") or {}).get("input_tokens")]
        tout = [r["usage"].get("output_tokens") for r in good
                if (r.get("usage") or {}).get("output_tokens")]
        total += sum(costs)
        data.append([
            key, f"{len(good)}/{len(group)}",
            f"{sum(r['seconds'] for r in good) / len(good):.1f}" if good else "—",
            f"{sum(tin) // len(tin):,}" if tin else "—",
            f"{sum(tout) // len(tout):,}" if tout else "—",
            money(sum(costs) / len(costs) if costs else None),
            money(sum(costs)),
        ])
    data.append(["ALL 20 RENDERS", "", "", "", "", "", money(total)])

    cost_tbl = grid_table(data, [108, 38, 40, 48, 48, 52, 66],
                          align_right=[2, 3, 4, 5, 6])
    cost_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, -1), (-1, -1), BAND),
        ("LINEABOVE", (0, -1), (-1, -1), 0.6, INK),
    ]))

    left: list[Any] = [Paragraph("What the twenty renders cost", S["h2"]), cost_tbl]
    if src_cells:
        src_tbl = Table([[c[0] for c in src_cells], [c[1] for c in src_cells]],
                        colWidths=[100] * len(src_cells))
        src_tbl.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
        right: list[Any] = [Paragraph("Source photographs", S["h2"]), src_tbl]
    else:
        right = []

    outer = Table([[left, right]], colWidths=[410, 330])
    outer.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))
    flow.append(outer)

    # Per-view detail — where the shape differences show up.
    flow += [Spacer(1, 8), Paragraph("Every render, in detail", S["h2"])]
    vdata = [["View", "Arm", "Shape", "s", "In tok", "Out tok", "$", "Result"]]
    for view in ctx["views"]:
        for key in ARM_ORDER:
            r = next((x for x in rows if x["arm"] == key and x["view"] == view), None)
            if not r:
                continue
            vdata.append([
                VIEW_LABELS.get(view, view), key, r.get("shape") or "—",
                f"{r['seconds']:.0f}",
                f"{(r['usage'].get('input_tokens') or 0):,}",
                f"{(r['usage'].get('output_tokens') or 0):,}",
                money(r["cost"]),
                "ok" if r["ok"] else (r["error"] or "failed")[:52],
            ])
    flow.append(grid_table(vdata, [88, 108, 86, 26, 46, 48, 48, 300],
                           align_right=[3, 4, 5, 6]))
    return flow


def grid_page(run: dict[str, Any]) -> list[Any]:
    ctx = run["ctx"]
    views = ctx["views"]
    flow: list[Any] = [
        Paragraph(f"{ctx['product']} — all four stacks, all five renders", S["h2"]),
        Paragraph(
            "Rows are the four production stacks, columns the five views. "
            "Thumbnails — the next pages show each view large enough to judge.",
            S["small"]),
        Spacer(1, 4),
    ]
    col = 132.0
    cell_h = 104.0
    header = [Paragraph("", S["cap"])] + [
        Paragraph(f"<b>{VIEW_LABELS.get(v, v)}</b>", S["cap"]) for v in views
    ]
    data: list[list[Any]] = [header]
    for key in ARM_ORDER:
        row: list[Any] = [Paragraph(arm_label(key).replace("\n", "<br/>"), S["cap"])]
        for view in views:
            r = next((x for x in run["rows"]
                      if x["arm"] == key and x["view"] == view), None)
            reader = scaled(run["dir"] / r["file"], 350) if r and r.get("file") else None
            row.append(fitted(reader, col - 8, cell_h - 6) if reader
                       else Paragraph("<i>no render</i>", S["cap"]))
        data.append(row)

    t = Table(data, colWidths=[96] + [col] * len(views),
              rowHeights=[16] + [cell_h] * len(ARM_ORDER))
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 1), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.25, RULE),
        ("BACKGROUND", (0, 0), (-1, 0), BAND),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    flow.append(t)
    return flow


def view_page(run: dict[str, Any], view: str) -> list[Any]:
    ctx = run["ctx"]
    flow: list[Any] = [
        Paragraph(f"{ctx['product']} — {VIEW_LABELS.get(view, view)}", S["h2"]),
    ]
    cells: list[Any] = []
    caps: list[Any] = []
    for key in ARM_ORDER:
        r = next((x for x in run["rows"]
                  if x["arm"] == key and x["view"] == view), None)
        reader = scaled(run["dir"] / r["file"], 900) if r and r.get("file") else None
        cells.append(fitted(reader, 178, 330) if reader
                     else Paragraph("<i>no render</i>", S["cap"]))
        if r:
            caps.append(Paragraph(
                f"<b>{key}</b><br/>{r['model']}"
                + (f" · {r['quality']}" if r.get("quality") else "")
                + f"<br/>{r.get('shape') or '—'} · {r['seconds']:.0f}s · "
                  f"{(r['usage'].get('output_tokens') or 0):,} out tok · "
                  f"{money(r['cost'])}", S["cap"]))
        else:
            caps.append(Paragraph(key, S["cap"]))

    t = Table([cells, caps], colWidths=[186] * len(ARM_ORDER),
              rowHeights=[336, 40])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, 0), "MIDDLE"),
        ("VALIGN", (0, 1), (-1, 1), "TOP"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    flow.append(t)
    return flow


def prompt_pages(run: dict[str, Any]) -> list[Any]:
    ctx = run["ctx"]
    flow: list[Any] = [
        Paragraph(f"{ctx['product']} — the prompts actually sent", S["h2"]),
        Paragraph(
            "Left: what vnyx-api's <i>buildShotPrompt</i> produced (used by the "
            "gemini-pro arm). Right: what Hermes' <i>build_prompt</i> produced "
            "(used by the other three). Verbatim, including the personality "
            "traits read from the model library.", S["small"]),
        Spacer(1, 4),
    ]
    for view in ctx["views"]:
        v = run["prompts"].get("vnyx", {}).get(view, "—")
        h = run["prompts"].get("hermes", {}).get(view, "—")
        head = Table(
            [[Paragraph(f"<b>{VIEW_LABELS.get(view, view)} · VNYX "
                        f"({len(v):,} chars)</b>", S["cap"]),
              Paragraph(f"<b>{VIEW_LABELS.get(view, view)} · HERMES "
                        f"({len(h):,} chars)</b>", S["cap"])]],
            colWidths=[372, 372])
        head.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#fdf1e6")),
            ("BACKGROUND", (1, 0), (1, 0), colors.HexColor("#e8f1f7")),
            ("TEXTCOLOR", (0, 0), (0, 0), VNYX),
            ("TEXTCOLOR", (1, 0), (1, 0), HERMES),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        body = Table(
            [[Paragraph(v.replace("&", "&amp;").replace("<", "&lt;"), S["mono"]),
              Paragraph(h.replace("&", "&amp;").replace("<", "&lt;"), S["mono"])]],
            colWidths=[372, 372])
        body.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BOX", (0, 0), (-1, -1), 0.25, RULE),
            ("INNERGRID", (0, 0), (-1, -1), 0.25, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.append(KeepTogether([head, body, Spacer(1, 7)]))
    return flow


def footer(canvas, doc) -> None:
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(MUTED)
    canvas.drawString(MARGIN, 9 * mm,
                      "vnyx render benchmark · generated from usage.jsonl")
    canvas.drawRightString(PAGE[0] - MARGIN, 9 * mm, f"page {doc.page}")
    canvas.setStrokeColor(RULE)
    canvas.setLineWidth(0.3)
    canvas.line(MARGIN, 12 * mm, PAGE[0] - MARGIN, 12 * mm)
    canvas.restoreState()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_ROOT / "render-comparison.pdf"))
    ap.add_argument("--root", default=str(OUT_ROOT))
    args = ap.parse_args()

    runs = load_runs(Path(args.root))
    if not runs:
        raise SystemExit(f"no finished runs under {args.root}")

    doc = BaseDocTemplate(
        args.out, pagesize=PAGE,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=17 * mm,
        title="vnyx on-model render comparison",
        author="bench_images.py",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="f")
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame], onPage=footer)])

    flow: list[Any] = cover(runs)
    for run in runs:
        flow.append(PageBreak())
        flow += product_summary(run)
        flow.append(PageBreak())
        flow += grid_page(run)
        for view in run["ctx"]["views"]:
            flow.append(PageBreak())
            flow += view_page(run, view)
        flow.append(PageBreak())
        flow += prompt_pages(run)

    doc.build(flow)
    size = Path(args.out).stat().st_size / 1e6
    print(f"{len(runs)} products -> {args.out}  ({size:.1f} MB)")


if __name__ == "__main__":
    main()
