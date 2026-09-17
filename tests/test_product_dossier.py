"""scripts/product_dossier.py — which picture each fault belongs to, and the
sheet reader both it and repair_product.py share.

No database, no network, no model: `image_notes` takes the dicts the checks
return, `collapse` takes findings, and the sheet reader takes a workbook
openpyxl writes here.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from scripts import product_dossier as pd  # noqa: E402
from scripts.audit_review_products import read_sheet_ids  # noqa: E402

POL = policy()

IDS = ["d9241dc8-b4d2-4571-8140-dba1aae99b8b", "c24a8b14-cd7a-4298-b096-badeaf9ca9bc"]


def sheet(tmp_path: Path, rows: list[list[Any]], name: str = "s.xlsx") -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    out = tmp_path / name
    wb.save(out)
    return out


# --------------------------------------------------------------------------- #
# The sheet reader
# --------------------------------------------------------------------------- #

def test_the_header_is_matched_however_it_was_capitalised(tmp_path: Path):
    """The approved-window workbook writes 'Product id'; the reader demanded
    'Product ID' and found nothing in a perfectly good file."""
    for header in ("Product id", "Product ID", "product_id", "PRODUCT ID", "Product UUID"):
        p = sheet(tmp_path, [["SKU", header, "Title"],
                             ["KLE-1", IDS[0], "a"], ["KLE-2", IDS[1], "b"]],
                  name=f"{header.replace(' ', '_')}.xlsx")
        assert read_sheet_ids(p) == IDS, header


def test_a_column_that_is_not_ids_yields_nothing_rather_than_nonsense(tmp_path: Path):
    p = sheet(tmp_path, [["SKU", "Product id"], ["KLE-1", "not-a-uuid"], ["KLE-2", ""]])
    assert read_sheet_ids(p) == []


def test_with_no_header_the_column_holding_the_most_uuids_wins(tmp_path: Path):
    """A list somebody pasted together by hand still works."""
    other = "13b0eb74-9ddf-4cd1-b962-778be57adefe"           # a tenant id, once
    p = sheet(tmp_path, [[other, IDS[0]], ["", IDS[1]]])
    assert read_sheet_ids(p) == IDS


def test_rows_keep_sheet_order_and_repeats_collapse(tmp_path: Path):
    p = sheet(tmp_path, [["Product id"], [IDS[1]], [IDS[0]], [IDS[1]]])
    assert read_sheet_ids(p) == [IDS[1], IDS[0]]


def test_the_title_rows_above_a_header_are_skipped(tmp_path: Path):
    p = sheet(tmp_path, [["Product repair"], ["Generated", "today"], [],
                         ["SKU", "Product id"], ["KLE-1", IDS[0]]])
    assert read_sheet_ids(p) == [IDS[0]]


def test_the_dossier_and_the_repair_chain_read_a_sheet_the_same_way(tmp_path: Path):
    p = sheet(tmp_path, [["Product id"], [IDS[0]]])
    assert pd.ids_from_sheet(p) == read_sheet_ids(p) == [IDS[0]]


# --------------------------------------------------------------------------- #
# Which pictures, in which order
# --------------------------------------------------------------------------- #

MEDIA = [
    {"url": "https://r2/ai-front.jpg", "view": "AI_FRONT", "origin": "AI",
     "processing": "GENERATED", "mediaType": "IMAGE", "isCurrent": True, "position": 2},
    {"url": "https://r2/front-cut.png", "view": "FRONT", "origin": "WEB",
     "processing": "BG_REMOVED", "mediaType": "IMAGE", "isCurrent": True, "position": 5},
    {"url": "https://r2/front-raw.jpg", "view": "FRONT", "origin": "WEB",
     "processing": "RAW", "mediaType": "IMAGE", "isCurrent": False, "position": 9},
    {"url": "https://r2/label.jpg", "view": "LABEL", "origin": "WEB",
     "processing": "RAW", "mediaType": "IMAGE", "isCurrent": True, "position": 7},
    {"url": "https://r2/gone.jpg", "view": "BACK", "origin": "WEB", "processing": "RAW",
     "mediaType": "IMAGE", "isCurrent": True, "position": 8, "deletedAt": "2026-01-01"},
    {"url": "https://r2/clip.mp4", "view": "FRONT", "origin": "WEB", "processing": "RAW",
     "mediaType": "VIDEO", "isCurrent": True, "position": 12},
]


def test_the_archived_photograph_is_shown_beside_the_cutout_cut_from_it():
    """Half the point of the page: the raw and the edited, side by side. The
    originals are superseded rows, so 'live only' would have hidden them."""
    groups = [(p["group"], p["url"].rsplit("/", 1)[-1]) for p in pd.pictures(MEDIA)]
    assert groups == [("raw", "front-raw.jpg"), ("cutout", "front-cut.png"),
                      ("render", "ai-front.jpg"), ("other", "label.jpg")]


def test_deleted_rows_and_video_are_not_pictures():
    urls = {p["url"] for p in pd.pictures(MEDIA)}
    assert "https://r2/gone.jpg" not in urls and "https://r2/clip.mp4" not in urls


# --------------------------------------------------------------------------- #
# Which fault belongs to which picture
# --------------------------------------------------------------------------- #

AUDIT_MEDIA = [
    {"url": "https://r2/front-cut.png", "view": "FRONT", "processing": "BG_REMOVED",
     "mediaType": "IMAGE", "isCurrent": True, "position": 0},
    {"url": "https://r2/back-cut.png", "view": "BACK", "processing": "BG_REMOVED",
     "mediaType": "IMAGE", "isCurrent": True, "position": 1},
    {"url": "https://r2/ai-front.jpg", "view": "AI_FRONT", "processing": "GENERATED",
     "mediaType": "IMAGE", "isCurrent": True, "position": 2},
    {"url": "https://r2/ai-back.jpg", "view": "AI_BACK", "processing": "GENERATED",
     "mediaType": "IMAGE", "isCurrent": True, "position": 3},
    {"url": "https://r2/ai-closeup.jpg", "view": "AI_CLOSEUP", "processing": "GENERATED",
     "mediaType": "IMAGE", "isCurrent": True, "position": 4},
]


def test_every_check_lands_its_fault_on_the_right_picture():
    """Four sources, four ways of naming a picture: the photo audit answers by
    index into the list it was sent, the frame test by view, the cut-out
    measurement by url, a rule by the url in its detail."""
    loaded = {"media": AUDIT_MEDIA}
    rep = {
        # The audit sends the cut-outs first, so its numbering is
        # 1 FRONT · 2 BACK · 3 AI_FRONT · 4 AI_BACK · 5 AI_CLOSEUP.
        "audited": {"bad_views": ["AI_FRONT", "AI_CLOSEUP"], "raw": {
            "same_model": False, "odd_renders": [5],          # 5 = AI_CLOSEUP
            "images": [
                {"index": 1, "ok": True, "issue": "", "missing_parts": ["collar"]},
                {"index": 3, "ok": False, "issue": "two models in frame", "missing_parts": []},
            ]}},
        "judged": {"action": "regen", "code": "IMAGE_QUALITY", "unavailable": False,
                   "lead_url": "https://r2/ai-front.jpg",
                   "reasons": ["BAD FACE — the model's face is AI-corrupted"],
                   "composition": {"checks": [{"view": "AI_BACK", "problem": "an empty band of 22%"}]}},
        "measured": {"checks": [{"url": "https://r2/back-cut.png",
                                 "problems": ["canvas: aspect 0.750"], "flaws": []}]},
        "issues": [{"rule_id": "IMG.027", "message": "not on the tenant's backdrop",
                    "detail": {"url": "https://r2/front-cut.png"}}],
    }
    notes = pd.image_notes(loaded, rep, POL)
    assert notes["https://r2/front-cut.png"] == ["collar missing from the cut-out",
                                                 "IMG.027: not on the tenant's backdrop"]
    assert notes["https://r2/ai-front.jpg"] == ["two models in frame",
                                                "BAD FACE — the model's face is AI-corrupted"]
    assert notes["https://r2/ai-closeup.jpg"] == ["a different model than the other renders"]
    assert notes["https://r2/back-cut.png"] == ["canvas: aspect 0.750"]
    assert notes["https://r2/ai-back.jpg"] == ["frame: an empty band of 22%"]


def test_a_gate_that_could_not_run_blames_no_picture():
    rep = {"judged": {"action": "review", "code": "VISION_UNAVAILABLE", "unavailable": True,
                      "lead_url": "https://r2/ai-front.jpg", "reasons": ["429"]},
           "issues": []}
    assert pd.image_notes({"media": AUDIT_MEDIA}, rep, POL) == {}


def test_an_odd_model_answer_the_verdict_threw_away_is_never_drawn():
    """KLE-000028: the model answered `odd_renders: [1]`, which is the FRONT
    CUT-OUT — no model in it, and `decide()` had already dismissed it. The page
    drew a red border on it anyway."""
    rep = {"audited": {"bad_views": [], "raw": {"same_model": False, "odd_renders": [1, 3],
                                                "images": []}},
           "issues": []}
    assert pd.image_notes({"media": AUDIT_MEDIA}, rep, POL) == {}
    # Kept once the verdict agrees, and only for the render it named.
    rep["audited"]["bad_views"] = ["AI_FRONT"]
    assert pd.image_notes({"media": AUDIT_MEDIA}, rep, POL) == {
        "https://r2/ai-front.jpg": ["a different model than the other renders"]}


def test_an_index_outside_the_list_is_ignored():
    rep = {"audited": {"raw": {"images": [{"index": 99, "ok": False, "issue": "x"},
                                          {"index": "?", "ok": False}]}},
           "issues": []}
    assert pd.image_notes({"media": AUDIT_MEDIA}, rep, POL) == {}


# --------------------------------------------------------------------------- #
# The findings block
# --------------------------------------------------------------------------- #

def finding(rid: str, message: str, fields: list[str], blocking: bool = False) -> dict[str, Any]:
    return {"rule_id": rid, "message": message, "fields": fields, "severity": "low",
            "blocking": blocking, "fix": {"text": "reported only"}}


def test_a_rule_that_fired_eight_times_is_one_row_naming_its_fields():
    """Eight identical confidence findings pushed a broken render off the page."""
    issues = [finding("CONF.001", f"Field '{f}' has low extraction confidence", [f])
              for f in ("fit", "model", "waist", "eu_size", "material", "supplier", "condition")]
    issues.insert(0, finding("GATE:IMAGE_QUALITY", "BROKEN BODY", ["images"], blocking=True))
    rolled = pd.collapse(issues)
    assert [r["rule_id"] for r in rolled] == ["GATE:IMAGE_QUALITY", "CONF.001"]
    conf = rolled[1]
    assert "+6 more: fit, model, waist, eu_size, material, supplier…" in conf["message"]
    assert rolled[0]["message"] == "BROKEN BODY"          # a lone finding is untouched


def test_a_group_counts_as_blocking_when_any_of_its_findings_does():
    issues = [finding("DRIFT.001", "a", ["x"]), finding("DRIFT.001", "b", ["y"], blocking=True)]
    assert pd.collapse(issues)[0]["blocking"] is True
