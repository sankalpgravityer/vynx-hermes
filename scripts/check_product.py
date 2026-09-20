#!/usr/bin/env python
"""One product: every issue found, and what --apply would write.

    # LOOK. Nothing written, no subprocess, no model call.
    python scripts/check_product.py --db "postgresql://..." --product <uuid>

    # LOOK HARDER. Also download and measure the cut-outs (no model), and/or
    # judge the pictures: the lead render with the image gate and EVERY render
    # and cut-out with the photo audit (two vision calls, cached).
    python scripts/check_product.py --db "..." --product <uuid> --measure-images --judge-images

    # FIX IT. Every repair the chain can make, written through vnyx-api's own
    # scripts; then the checks again, and what was resolved. Renders are paid
    # and OFF unless --render. Approval is never part of this script.
    python scripts/check_product.py --db "..." --product <uuid> --apply
    python scripts/check_product.py --db "..." --product <uuid> --apply --render

    ... --json reports/check-<sku>.json      the whole report as JSON

WHAT THE DRY RUN IS. The product is loaded once, exactly as the Auto Approval
chain loads it, and every check the chain uses runs in-process: the rules (the
data fields, the taxonomy cascade from the master category, the sizing guide,
the price window, the title and description, the renders and cut-outs, the
gallery order) and the readiness decisions (which root the product belongs to,
and — when asked — whether the cut-outs are on the source canvas and the
tenant's backdrop, whether the lead render passes the gate, and whether the
photo audit finds wear the grade denies or a render with a visible AI defect:
MID-000253's AI_FRONT_34 had the model's knees smeared to white while its lead
passed the gate, and only the photo audit, which sees every render, catches
that). Each issue is listed with the FIX `--apply` would make: a value the
reconcile step writes, a step that runs (re-matte, re-order, re-write, render,
re-render one view with the same model), or "a person decides".

WHAT --apply IS. Not a second implementation. It runs the repair chain
(scripts/repair_product.py, the same code the Auto Approval agent runs) with
the repairs allowed to write, so every write lands through vnyx-api's own
scripts — the properties merge, the price mirror, the media invariants — and
then runs this script's checks again to show before and after. Nothing here
approves or publishes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import policy  # noqa: E402

RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# Which chain step repairs a finding when no planned write names it, and the
# sentence the report shows. `None` means nothing automatic can: a photograph,
# or a person.
STEP_FIX: dict[str, tuple[str | None, str]] = {
    "IMG.010": ("matte", "run background removal on the raw garment view(s)"),
    "IMG.026": ("matte", "re-cut from the raw archive on the source canvas (matte --replace)"),
    "IMG.027": ("matte", "re-cut on the tenant's backdrop (matte --replace)"),
    "IMG.021": ("relabel", "recover each render's true view from its filename"),
    "IMG.001": ("render", "generate the on-model views (paid — needs --render)"),
    "IMG.002": ("render", "generate the missing view(s) (paid — needs --render)"),
    "IMG.005": ("render", "advisory views only; generated with --render"),
    "IMG.023": ("order", "rebuild the media cache in the catalog order"),
    "IMG.024": ("order", "rebuild the media cache in the catalog order"),
    "IMG.025": ("order", "rebuild the media cache in the catalog order"),
    "IMG.003": (None, "needs a garment photograph — nothing can render without one "
                      "(rejected by the agent under readiness.unfixable)"),
    "IMG.030": (None, "needs a care-label photograph (the agent rejects without one)"),
    "IMG.011": (None, "reported only — vnyx-api's generation reaper owns a stranded run"),
    "IMG.012": (None, "reported only — the pipeline recorded FAILED; --render tries again"),
    "TEXT.001": ("copy", "regenerate the title and description from the record"),
    "TEXT.002": ("copy", "regenerate the title and description from the record"),
    "TEXT.003": ("copy", "regenerate the title and description from the record"),
    "TEXT.006": ("copy", "regenerate the title and description from the record"),
    "TEXT.007": ("copy", "regenerate the title and description from the record"),
    "TEXT.008": ("copy", "regenerate the title and description from the record"),
    "TEXT.004": ("extract", "write the description from the care label and cut-outs"),
    "PRICE.001": ("price", "clamp the selling price to the grade's window"),
    "PRICE.002": ("price", "clamp the selling price to the grade's window"),
    "PRICE.003": ("price", "clamp the selling price to the grade's window"),
}

# Fields the extractor reads off the care label when they are empty.
_EXTRACTABLE = {"brand", "size", "international_size", "material", "color", "description"}


def _fields(finding: dict[str, Any]) -> list[str]:
    return [str(f) for f in (finding.get("fields") or [])]


def fix_for(finding: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, Any]:
    """What --apply would do about one finding.

    In order: a planned WRITE that names the rule or one of its fields (the
    reconcile step executes it), then a planned ESCALATION (a person decides),
    then the chain step that owns the rule, then an empty field the extractor
    fills from the care label. Anything else is reported only.
    """
    rid = str(finding.get("rule_id") or "")
    fields = _fields(finding)
    # A write is this finding's fix when the plan says so — the same rule id.
    # Among several (DRIFT.001 writes three columns), the one on this
    # finding's field; never a write matched on the field alone, or a TEXT
    # finding on the title would claim the subcategory write as its own.
    writes = [a for a in plan if a.get("kind") in ("set_column", "set_property")
              and a.get("reason") == rid]
    writes.sort(key=lambda a: 0 if a.get("field") in fields else 1)
    for a in writes[:1]:
        return {"kind": "write", "step": "reconcile", "field": a.get("field"),
                "value": a.get("value"),
                "text": f"write {a.get('field')} = {a.get('value')!r} ({a.get('reason') or rid})"}
    for a in plan:
        if a.get("kind") == "escalate" and (a.get("reason") == rid or a.get("code") == rid
                                            or (a.get("field") in fields and fields)):
            detail = str(a.get("detail") or a.get("code") or a.get("reason") or rid)
            readable = [x for x in fields if x in _EXTRACTABLE]
            if readable:
                # The chain tries the care label before giving up on the field;
                # `material` only with --infer (it is rarely legible on a tag).
                hint = " (with --infer)" if readable == ["material"] else ""
                return {"kind": "step", "step": "extract", "field": a.get("field"),
                        "text": f"the extract step reads {', '.join(readable)} from the care label{hint}; "
                                f"else a person decides — {detail}"}
            return {"kind": "person", "step": None, "field": a.get("field"),
                    "text": "a person decides — " + detail}
    if rid in STEP_FIX:
        step, text = STEP_FIX[rid]
        return {"kind": "step" if step else "none", "step": step, "text": text}
    if rid == "DATA.010" and any(f in _EXTRACTABLE for f in fields):
        return {"kind": "step", "step": "extract",
                "text": f"read {', '.join(f for f in fields if f in _EXTRACTABLE)} from the care label"}
    return {"kind": "none", "step": None, "text": "reported only"}


def issues_from(gate: dict[str, Any]) -> list[dict[str, Any]]:
    """Every finding, worst first, each with its fix."""
    plan = list(gate.get("repair_plan") or [])
    rows: list[dict[str, Any]] = []
    for group, findings in (("blocking", gate.get("blocking") or []),
                            ("advisory", gate.get("advisory") or [])):
        for f in findings:
            rows.append({
                "rule_id": f.get("rule_id"), "severity": str(f.get("severity") or "").lower(),
                "blocking": group == "blocking", "fields": _fields(f),
                "message": str(f.get("message") or ""), "detail": f.get("detail") or {},
                "fix": fix_for(f, plan),
            })
    rows.sort(key=lambda r: (RANK.get(r["severity"], 9), str(r["rule_id"])))
    return rows


def picture_issues(judged: dict[str, Any] | None, audited: dict[str, Any] | None,
                   measured: dict[str, Any] | None, pol: dict[str, Any]
                   ) -> tuple[list[dict[str, Any]], bool]:
    """The picture verdicts as issue rows, each with its fix — and whether any
    of them holds the product.

    A gate refusal is what holds the product in the chain, so it belongs in the
    same list as the rules, with the views the regen step would re-render and
    the reminder that renders are paid. The photo audit's verdict likewise: a
    render it calls defective is re-rendered — that view alone, the same model —
    while wear the grade denies is a person's decision. A cut-out the
    measurement refused names the re-matte. A check that could not run holds
    nothing: it is the provider, not the product.
    """
    from app.imaging import photo_audit, quality_gate
    from app.imaging.quality_gate import GateVerdict

    rows: list[dict[str, Any]] = []
    blocks = False

    if judged and judged.get("action") in ("regen", "review"):
        views = quality_gate.regen_views(GateVerdict(
            judged.get("action"), judged.get("code"), list(judged.get("reasons") or []),
            lead_view=judged.get("lead_view"), bad_views=list(judged.get("bad_views") or [])), pol)
        real = not judged.get("unavailable")
        blocks = blocks or real
        if judged.get("action") == "regen" and views:
            fix = {"kind": "step", "step": "regen",
                   "text": f"re-render {', '.join(views)} (regen step, paid — needs --render)"}
        elif not real:
            fix = {"kind": "none", "step": None,
                   "text": "the gate could not run — retry once the vision provider answers and the render downloads"}
        else:
            fix = {"kind": "person", "step": None,
                   "text": "a person decides — the gate could not decide or the category disagrees with the picture"}
        rows.append({
            "rule_id": f"GATE:{judged.get('code') or judged.get('action')}", "severity": "high",
            "blocking": real, "fields": ["images"],
            "message": "; ".join(judged.get("reasons") or []) or "the image gate refused the render",
            "detail": {"lead_view": judged.get("lead_view"), "bad_views": judged.get("bad_views")},
            "fix": fix,
        })

    if audited and audited.get("action") in ("regen", "review"):
        real = not audited.get("unavailable")
        blocks = blocks or real
        views = photo_audit.regen_views(GateVerdict(
            audited.get("action"), audited.get("code"), list(audited.get("reasons") or []),
            bad_views=list(audited.get("bad_views") or []),
            unavailable=bool(audited.get("unavailable"))), pol)
        cut_views = photo_audit.rematte_views(GateVerdict(
            "ok", bad_cutouts=list(audited.get("bad_cutouts") or []),
            unavailable=bool(audited.get("unavailable"))), pol)
        if views:
            fix = {"kind": "step", "step": "regen",
                   "text": f"re-render {', '.join(views)} — that view only, same model "
                           f"(regen step, paid — needs --render)"}
        elif audited.get("code") == "CUTOUT_DEFECT" and cut_views:
            fix = {"kind": "step", "step": "rematte",
                   "text": f"re-cut {', '.join(cut_views)} from the raw archive (rematte step; "
                           f"the Hermes segmenter is free, its mask fallbacks are paid)"}
        elif not real:
            fix = {"kind": "none", "step": None,
                   "text": "the photo audit could not run — retry once the vision provider answers and every image downloads"}
        else:
            fix = {"kind": "person", "step": None,
                   "text": "a person decides — the photographs contradict the grade, a picture is not "
                           "what its slot says, or the garment contradicts the master category"}
        rows.append({
            "rule_id": f"PHOTOS:{audited.get('code') or audited.get('action')}", "severity": "high",
            "blocking": real, "fields": ["images"],
            "message": "; ".join(audited.get("reasons") or []) or "the photo audit refused",
            "detail": {"bad_views": audited.get("bad_views"),
                       "wear": (audited.get("raw") or {}).get("wear")},
            "fix": fix,
        })

    # A cut-out the audit calls defective while nothing holds on it (the
    # cut-out policy is soft): its own row, advisory, with the re-cut as the fix.
    if audited and audited.get("bad_cutouts") and audited.get("code") != "CUTOUT_DEFECT":
        cut_views = photo_audit.rematte_views(GateVerdict(
            "ok", bad_cutouts=list(audited["bad_cutouts"]),
            unavailable=bool(audited.get("unavailable"))), pol)
        text = "; ".join(str(r) for r in [*(audited.get("reasons") or []), *(audited.get("soft") or [])]
                         if str(r).startswith("CUTOUT DEFECT")) or "a cut-out is missing part of the garment"
        rows.append({
            "rule_id": "PHOTOS:CUTOUT_DEFECT", "severity": "medium", "blocking": False, "fields": ["images"],
            "message": text, "detail": {"bad_cutouts": audited["bad_cutouts"]},
            "fix": ({"kind": "step", "step": "rematte",
                     "text": f"re-cut {', '.join(cut_views)} from the raw archive (rematte step; "
                             f"the Hermes segmenter is free, its mask fallbacks are paid)"}
                    if cut_views else
                    {"kind": "none", "step": None, "text": "reported only (photo_audit.gallery.cutout_defects)"}),
        })

    if measured and measured.get("action") == "bad":
        recut = list(measured.get("bad_views") or [])
        stuck = list(measured.get("unfixable_views") or [])
        if recut:
            fix = {"kind": "step", "step": "matte",
                   "text": f"re-cut {', '.join(recut)} from the raw archive (matte --replace)"
                           + (f"; {', '.join(stuck)}: a re-cut cannot fix the neckline — see the message"
                              if stuck else "")}
        else:
            fix = {"kind": "person", "step": None,
                   "text": "a re-cut cannot fix this — the photograph never showed the inside of the "
                           "collar (the garment was on a form); use the flat-lay cut-out as the "
                           f"{', '.join(stuck) or 'garment view'}, or reshoot on a hanger"}
        rows.append({
            "rule_id": "CUTOUT:MEASURED", "severity": "medium", "blocking": bool(measured.get("blocks")),
            "fields": ["images"],
            "message": "; ".join(measured.get("reasons") or []),
            "detail": {"bad_views": recut, "unfixable_views": stuck},
            "fix": fix,
        })
    return rows, blocks


def hermes_url() -> str:
    import os

    return (os.getenv("HERMES_URL") or os.getenv("HERMES_API_URL") or "http://127.0.0.1:8080").rstrip("/")


def hermes_reachable(timeout_s: float = 2.0) -> bool:
    """Does the Hermes API answer? Any HTTP status counts; only no answer is a no."""
    try:
        import httpx

        httpx.get(f"{hermes_url()}/docs", timeout=timeout_s)
        return True
    except Exception:  # noqa: BLE001
        return False


def diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> dict[str, list[str]]:
    b = {str(r["rule_id"]) for r in before}
    a = {str(r["rule_id"]) for r in after}
    return {"resolved": sorted(b - a), "remaining": sorted(a & b), "new": sorted(a - b)}


# --------------------------------------------------------------------------- #
# The dry run
# --------------------------------------------------------------------------- #

def inspect(dsn: str, product_id: str, *, measure: bool = False, judge: bool = False,
            loaded: dict[str, Any] | None = None) -> dict[str, Any]:
    """Load once, run every check in-process. Reads only; a vision call only with `judge`."""
    from app import approval, readiness
    from app.vnyx_client import to_snapshot

    pol = policy()
    loaded = loaded or product_audit.load(dsn, product_id)
    record, media = loaded["record"], loaded["media"]
    gate = approval.run_gate({**record, "media": media}, catalog=loaded.get("catalog"),
                             imagery_settings=loaded.get("imagery_settings"), llm=None)
    snap = to_snapshot({**record, "media": media}, catalog=loaded.get("catalog"),
                       imagery_settings=loaded.get("imagery_settings"))
    master = readiness.decide_master(snap, pol)

    measured = judged = audited = None
    if measure:
        from app.imaging import cutouts

        measured = cutouts.judge(snap, pol).as_dict()
    if judge:
        from app.imaging import photo_audit, quality_gate

        judged = quality_gate.judge(
            media,
            gender=readiness.root_gender(record.get("masterCategory"), pol) or record.get("gender"),
            category=record.get("category"), subcategory=record.get("subCategory"), pol=pol,
            size=record.get("size") or record.get("internationalSize"),
            kids=readiness.is_kids(record.get("masterCategory"), record.get("mannequinType"), pol),
        ).as_dict()
        # EVERY render, not only the lead. The gate looks at one picture; the
        # photo audit sees the other four beside the cut-outs, and it is the
        # check that caught MID-000253's smeared knees on the AI_FRONT_34 while
        # the AI_FRONT passed. Same call the chain's `photos` step makes.
        audited = photo_audit.judge(
            media,
            grade_severity=record.get("gradeSeverity"),
            grade_label=record.get("gradeLabel") or record.get("grade"), pol=pol,
            product_gender=readiness.root_gender(record.get("masterCategory"), pol),
        ).as_dict()

    # THE PICTURE VERDICTS ARE ISSUES TOO — with their fix, ahead of the rules.
    issues = issues_from(gate)
    picture, picture_blocks = picture_issues(judged, audited, measured, pol)
    issues[:0] = picture

    live = [m for m in media if m.get("isCurrent", True) and (m.get("mediaType") or "IMAGE") == "IMAGE"]
    return {
        "product_id": product_id,
        "sku": record.get("sku"), "title": record.get("title"), "tenant": record.get("tenantName"),
        "stage": record.get("currentStage"), "edit_url": record.get("editUrl"),
        "record": {k: record.get(k) for k in (
            "masterCategory", "category", "subCategory", "gender", "size", "euSize", "sizingGuide",
            "mannequinType", "brand", "color", "material", "condition", "grade", "price", "retailPrice")},
        "media": {
            "renders": sorted({m["view"] for m in live if str(m.get("view", "")).startswith("AI_")}),
            "cutouts": sorted({m["view"] for m in live if m.get("view") in ("FRONT", "BACK")
                               and m.get("processing") != "RAW"}),
            "raw_garments": sorted({m["view"] for m in live if m.get("view") in ("FRONT", "BACK", "OTHER")
                                    and m.get("processing") == "RAW"}),
            "labels": record.get("careLabelCount") or 0,
            "chart": (loaded.get("chart") or {}).get("state"),
            "gallery_manual": bool(record.get("mediaManualOrder")),
        },
        "master": master,
        "verified": bool(gate.get("verified")) and not picture_blocks,
        "issues": issues,
        "plan": [a for a in (gate.get("repair_plan") or [])
                 if a.get("kind") in ("set_column", "set_property")],
        "held": [a for a in (gate.get("repair_plan") or []) if a.get("kind") == "escalate"],
        "price": gate.get("price"),
        "measured": measured,
        "judged": judged,
        "audited": audited,
    }


# --------------------------------------------------------------------------- #
# Printing
# --------------------------------------------------------------------------- #

def _short(text: Any, n: int) -> str:
    s = str(text or "").replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "…"


def print_report(rep: dict[str, Any], *, heading: str = "ISSUES") -> None:
    rec, med = rep["record"], rep["media"]
    print(f'\n{rep["sku"]} · {_short(rep["title"], 70)} · {rep["tenant"]} · {rep["stage"]}')
    print(f'  {rec["masterCategory"]} > {rec["category"]} > {rec["subCategory"]} · gender {rec["gender"]} · '
          f'size {rec["size"]} (EU {rec["euSize"]}) · guide {rec["sizingGuide"]} · rig {rec["mannequinType"]}')
    print(f'  brand {rec["brand"]} · colour {rec["color"]} · material {rec["material"]} · '
          f'grade {rec["grade"]} · price {rec["price"]} (retail {rec["retailPrice"]})')
    print(f'  renders {len(med["renders"])}/5 · cut-outs {", ".join(med["cutouts"]) or "none"} · '
          f'raw garments {", ".join(med["raw_garments"]) or "none"} · labels {med["labels"]} · '
          f'size chart {med["chart"]}' + (" · gallery arranged by a person" if med["gallery_manual"] else ""))
    m = rep["master"]
    print(f'  master: {m.get("action")} — {m.get("detail")}')

    issues = rep["issues"]
    blockers = [r["rule_id"] for r in issues if r["blocking"]]
    state = ("passes every blocking rule" if rep["verified"]
             else "blocked by " + ", ".join(blockers) if blockers else "blocked")
    print(f'\n{heading} ({len(issues)}) — {state}')
    if not issues:
        print("  none")
    for i, r in enumerate(issues, start=1):
        sev = r["severity"].upper()
        flag = "" if r["blocking"] else " (advisory)"
        print(f'  {i:2}. {sev:8} {r["rule_id"]:10} {", ".join(r["fields"]) or "-":22} {_short(r["message"], 120)}{flag}')
        print(f'      {"->":6} {r["fix"]["text"]}')

    # THE PICTURES THEMSELVES. Every rule above is metadata over the media
    # rows; nothing in it looks at a render or a cut-out. Say so when they
    # were not looked at, so a cropped model or a wall behind a cut-out is
    # not read as "no issue" — it was not checked.
    if rep.get("measured"):
        v = rep["measured"]
        print(f'\nCUT-OUTS (measured): {v.get("action")}' + (" — " + "; ".join(v.get("reasons") or []) if v.get("reasons") else ""))
    elif med["cutouts"]:
        print(f'\nCUT-OUTS: not measured — pass --measure-images to check the canvas and the backdrop (downloads, no model)')
    if rep.get("judged"):
        v = rep["judged"]
        print(f'RENDER (judged): {v.get("action")} {v.get("code") or ""}'
              + (" — " + "; ".join(v.get("reasons") or []) if v.get("reasons") else "")
              + (f'  (soft: {"; ".join(v["soft"])})' if v.get("soft") else "")
              + (f'  [model {v.get("gender_seen") or "?"}, build {v.get("build_seen") or "?"}, '
                 f'framing {v.get("framing_seen") or "?"}, on {v.get("lead_view") or "?"}]'))
        for c in ((v.get("composition") or {}).get("checks") or []):
            if not c.get("fetched", True):
                print(f'  frame {c["view"]:12} not downloaded')
                continue
            print(f'  frame {c["view"]:12} bottom margin {c.get("bottom_margin", 0):.0%} · '
                  f'height fill {c.get("height_fill", 0):.0%}'
                  + (f'  <- {c["problem"]}' if c.get("problem") else "  ok"))
    elif med["renders"] or med["cutouts"]:
        print(f'RENDER: not judged — pass --judge-images to check the lead render with the gate '
              f'(model present, face, body, gender, build, framing) and every render and cut-out '
              f'with the photo audit (wear vs grade, render defects); two vision calls, cached')
    if rep.get("audited"):
        v = rep["audited"]
        wear = str((v.get("raw") or {}).get("wear") or "")
        label = {"ok": "ok", "regen": "REFUSED", "review": "HELD", "skipped": "skipped"}.get(v.get("action"), v.get("action"))
        print(f'PHOTOS (judged): {label} {v.get("code") or ""}'
              + (" — " + "; ".join(v.get("reasons") or []) if v.get("reasons") else "")
              + (f'  (soft: {"; ".join(v["soft"])})' if v.get("soft") else "")
              + (f'  [wear seen: {wear}]' if wear and wear != "unknown" else "")
              + ("  (cached)" if v.get("cached") else ""))
        if v.get("bad_views"):
            print(f'  defective render(s): {", ".join(v["bad_views"])} — re-rendered one by one, '
                  f'same model (regen step; --apply --render)')
        if v.get("bad_cutouts"):
            print(f'  defective cut-out(s): {", ".join(v["bad_cutouts"])} — re-cut from the raw archive '
                  f'(rematte step; --apply)')

    if rep["plan"]:
        print(f'\nPLANNED WRITES ({len(rep["plan"])}) — what --apply writes through the reconcile step')
        for a in rep["plan"]:
            print(f'  {a.get("field")} = {a.get("value")!r}   ({a.get("reason")})')
    if rep["held"]:
        print(f'\nA PERSON DECIDES ({len(rep["held"])})')
        for a in rep["held"]:
            print(f'  {a.get("field")}: {a.get("detail") or a.get("code") or a.get("reason")}')
    price = rep.get("price") or {}
    if price.get("proposed_price") not in (None, price.get("current_price")) and price.get("proposed_price"):
        print(f'\nPRICE: {price.get("current_price")} -> {price.get("proposed_price")}  ({_short(price.get("explanation"), 110)})')


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", "--dsn", dest="db", help="database URL; else DATABASE_URL")
    ap.add_argument("--product", required=True, help="the product id (uuid)")
    ap.add_argument("--apply", action="store_true", help="write the fixes (the repair chain, no approval)")
    ap.add_argument("--render", action="store_true", help="with --apply: also generate renders (paid)")
    ap.add_argument("--infer", action="store_true",
                    help="with --apply: let the extractor read the garment cut-outs, not just the label")
    ap.add_argument("--measure-images", action="store_true", help="download and measure the cut-outs (no model)")
    ap.add_argument("--judge-images", action="store_true",
                    help="judge the pictures: the lead render with the image gate, every render and "
                         "cut-out with the photo audit (two vision calls, cached)")
    ap.add_argument("--vnyx-api", help="local vnyx-api checkout for --apply (else VNYX_API_DIR / VNYX_API_URL)")
    ap.add_argument("--json", help="write the report here")
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

    try:
        before = inspect(dsn, args.product, measure=args.measure_images, judge=args.judge_images)
    except product_audit.ProductNotFound:
        print(f"No product {args.product}", file=sys.stderr)
        return 1
    print_report(before, heading="ISSUES" if not args.apply else "ISSUES BEFORE")

    report: dict[str, Any] = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                              "applied": False, "before": before}

    if args.apply:
        from scripts import repair_product as rp

        vnyx_api = Path(args.vnyx_api or rp.DEFAULT_VNYX_API)
        print(f'\nAPPLYING — the repair chain writes through vnyx-api'
              f'{" (renders included)" if args.render else " (renders off; pass --render)"}')
        # The reconcile step (verify-and-repair.ts) asks the Hermes API for its
        # evidence layer and FAILS without it — the planned writes then do not
        # land and the run reads as "reconcile FAILED: gate_unavailable". Said
        # here, before anything runs, rather than discovered in the step list.
        if not hermes_reachable():
            print(f'  WARNING: the Hermes API does not answer at {hermes_url()} — the reconcile step '
                  f'will fail and its planned writes will not land. Start it: '
                  f'uvicorn app.main:app --port 8080')
        result = rp.repair(dsn, args.product, apply=True, vnyx_api=vnyx_api, infer=args.infer,
                           min_confidence=70, skip_render=not args.render, approve=False,
                           skip_bin=True, quiet=True, silent=True)
        for s in result.get("steps") or []:
            if not s.get("ran"):
                print(f'  skip  {s["step"]:12} {_short(s.get("why"), 100)}')
            else:
                mark = "ok   " if s.get("ok", True) else "FAIL "
                print(f'  {mark} {s["step"]:12} {_short(s.get("note"), 140)}')

        after = inspect(dsn, args.product, measure=args.measure_images, judge=args.judge_images)
        print_report(after, heading="ISSUES AFTER")
        d = diff(before["issues"], after["issues"])
        print(f'\nRESOLVED {len(d["resolved"])}: {", ".join(d["resolved"]) or "-"}')
        print(f'REMAINING {len(d["remaining"])}: {", ".join(d["remaining"]) or "-"}')
        if d["new"]:
            print(f'NEW {len(d["new"])}: {", ".join(d["new"])}')
        report.update({"applied": True, "steps": result.get("steps"), "after": after, "diff": d,
                       "chain": {k: result.get(k) for k in ("verified", "remaining", "gate", "photos",
                                                              "cutouts", "regeneration", "order", "copy",
                                                              "unfixable", "approval")}})

    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(report, indent=1, default=str, ensure_ascii=False), encoding="utf-8")
        print(f"\njson: {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
