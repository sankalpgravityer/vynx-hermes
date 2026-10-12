"""Cut-outs on Shopify, 12 Oct 2026 — which are decision images, which are duplicates,
and what the fix does with a product (app/imaging/gallery_cleanup, scripts/shopify_cutouts).

Shapes from production, file names shortened."""
from __future__ import annotations

import itertools
from typing import Any

import pytest

from app.imaging import gallery_cleanup as gc

_ids = itertools.count(1)
R2 = "https://r2.example/products/p1"


def row(url: str, **over: Any) -> dict[str, Any]:
    return {"id": f"m{next(_ids)}", "url": url, "view": "OTHER", "origin": "WEB",
            "processing": "RAW", "derivedFromId": None, "deletedAt": None,
            "createdAt": "2026-08-21", **over}


def files(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(gc.base(r["url"]) for r in rows)


def test_the_old_pair_goes_once_the_front_and_back_are_re_cut():
    """BOA-000314: a new full-size pair beside the July cut-outs."""
    front = row(f"{R2}/1776848573633-front-original.jpg", view="FRONT")
    back = row(f"{R2}/1776848575769-back-original.jpg", view="BACK")
    rows = [front, back,
            row(f"{R2}/sweat-bg-removed-ba57.png", view="FRONT", processing="BG_REMOVED", derivedFromId=front["id"]),
            row(f"{R2}/sweat-bg-removed-18bc.png", view="BACK", processing="BG_REMOVED", derivedFromId=back["id"]),
            row(f"{R2}/1776848586490-0-processed.png", processing="BG_REMOVED"),
            row(f"{R2}/1776848586400-1-processed.png", processing="BG_REMOVED")]
    assert files(gc.legacy_duplicates(rows)) == ["1776848586400-1-processed.png",
                                                  "1776848586490-0-processed.png"]


def test_a_product_never_re_cut_keeps_its_old_cut_outs():
    """BOA-000368: the old cut-outs are its only front/back pictures."""
    rows = [row(f"{R2}/1777389843195-0-original.png", view="FRONT"),
            row(f"{R2}/1777389846785-1-original.png", view="BACK"),
            row(f"{R2}/1777389858384-0-processed.png", processing="BG_REMOVED"),
            row(f"{R2}/1777389858194-1-processed.png", processing="BG_REMOVED")]
    assert gc.legacy_duplicates(rows) == []


def test_each_old_cut_out_is_matched_to_its_own_photo():
    """BOA-002599: a front and three back photos; photos 0, 1 and 3 re-cut."""
    originals = [row(f"{R2}/1782689662318-front-original.jpg", view="FRONT"),
                 row(f"{R2}/1782689664135-back-original.jpg", view="BACK"),
                 row(f"{R2}/1782689665586-back-original.jpg", view="BACK"),
                 row(f"{R2}/1782689666777-back-original.jpg", view="BACK")]
    cuts = [row(f"{R2}/jeans-bg-removed-{i}.png", view=originals[i]["view"], processing="BG_REMOVED",
                derivedFromId=originals[i]["id"]) for i in (0, 1, 3)]
    old = [row(f"{R2}/178268967{i}000-{i}-processed.png", processing="BG_REMOVED") for i in range(4)]
    assert files(gc.legacy_duplicates([*originals, *cuts, *old])) == [
        "1782689670000-0-processed.png", "1782689671000-1-processed.png", "1782689673000-3-processed.png"]


def test_a_size_chart_filed_as_a_photo_does_not_shift_the_index():
    """BOA-000279."""
    front = row(f"{R2}/1776840326423-front-original.jpg", view="FRONT")
    back = row(f"{R2}/1776840328349-back-original.jpg", view="BACK")
    rows = [front, back, row("https://r2.example/size-charts/u/g/1e6b.png"),
            row(f"{R2}/x-bg-removed-1.png", view="FRONT", processing="BG_REMOVED", derivedFromId=front["id"]),
            row(f"{R2}/x-bg-removed-2.png", view="BACK", processing="BG_REMOVED", derivedFromId=back["id"]),
            row(f"{R2}/1776840335252-0-processed.png", processing="BG_REMOVED"),
            row(f"{R2}/1776840335756-1-processed.png", processing="BG_REMOVED")]
    assert len(gc.legacy_duplicates(rows)) == 2


def test_an_unnumbered_old_cut_out_goes_only_when_every_photo_is_re_cut():
    front = row(f"{R2}/1776927020710-front-original.jpg", view="FRONT")
    back = row(f"{R2}/1776927024204-back-original.jpg", view="BACK")
    plain = [row(f"{R2}/1777550723121-bg-removed.png", processing="BG_REMOVED"),
             row(f"{R2}/1777550735192-bg-removed.png", processing="BG_REMOVED")]
    f_cut = row(f"{R2}/t-bg-removed-a.png", view="FRONT", processing="BG_REMOVED", derivedFromId=front["id"])
    b_cut = row(f"{R2}/t-bg-removed-b.png", view="BACK", processing="BG_REMOVED", derivedFromId=back["id"])
    assert len(gc.legacy_duplicates([front, back, f_cut, b_cut, *plain])) == 2
    assert gc.legacy_duplicates([front, back, f_cut, *plain]) == []


def test_two_cut_outs_of_the_same_photo_keep_the_newest():
    raw = row(f"{R2}/a-original_front.jpg", view="FRONT", origin="PHOTOBOOTH")
    old = row(f"{R2}/a-bg-removed-1.png", view="FRONT", origin="PHOTOBOOTH", processing="BG_REMOVED",
              derivedFromId=raw["id"], createdAt="2026-09-01")
    new = row(f"{R2}/a-bg-removed-2.png", view="FRONT", origin="PHOTOBOOTH", processing="BG_REMOVED",
              derivedFromId=raw["id"], createdAt="2026-10-10")
    assert gc.same_photo_duplicates([raw, old, new]) == [old]


def test_decision_images_are_reported_not_removed_and_never_sent():
    """KLE-000536: the decision cut-out is hidden by the sequence (not in the cache)."""
    dec_raw = row(f"{R2}/9c-inspection_front_0_90da.jpg", view="FRONT", origin="DECISION")
    dec_cut = row(f"{R2}/tee-bg-removed-8d29.png", view="FRONT", origin="DECISION",
                  processing="BG_REMOVED", derivedFromId=dec_raw["id"])
    booth = row(f"{R2}/f1-original_front.jpg", view="FRONT", origin="PHOTOBOOTH")
    booth_cut = row(f"{R2}/tee-bg-removed-3b40.png", view="FRONT", origin="PHOTOBOOTH",
                    processing="BG_REMOVED", derivedFromId=booth["id"])
    c = gc.classify([dec_raw, dec_cut, booth, booth_cut], sent=[booth_cut["url"]])
    assert c["decision"] == [dec_cut]
    assert c["duplicates"] == []
    assert c["sent_cutouts"] == [booth_cut]
    assert c["decision_still_sent"] == []


def test_an_inspection_folder_is_not_a_decision_image():
    """KLE-000888: an early photobooth cut-out stored under inspection/."""
    booth = row("https://r2.example/photobooth/c017-original_front.jpg", view="FRONT", origin="PHOTOBOOTH")
    cut = row("https://r2.example/inspection/3dbd-bg-removed.png", view="FRONT", origin="PHOTOBOOTH",
              processing="BG_REMOVED", derivedFromId=booth["id"])
    assert not gc.is_decision(cut, {r["id"]: r for r in (booth, cut)})


# --------------------------------------------------------------------------- #
# the fix
# --------------------------------------------------------------------------- #
@pytest.fixture
def fixer(monkeypatch):
    from app.services.auto_approval import db, events
    from scripts import repair_product as rp
    from scripts import shopify_cutouts as sc

    calls: dict[str, list[Any]] = {"sql": [], "steps": [], "events": []}
    monkeypatch.setattr(db, "execute", lambda sql, params=None: calls["sql"].append((sql, params)) or 2)
    monkeypatch.setattr(events, "emit", lambda *a, **k: calls["events"].append((a, k)))

    def run_step(vnyx_api, script, args, **kw):
        calls["steps"].append((script, list(args)))
        return True, "  queued    : 1" if script == "resync-listings.ts" else "", None

    monkeypatch.setattr(rp, "run_step", run_step)
    return sc, calls


PRODUCT = {"id": "00000000-0000-0000-0000-000000000001", "sku": "BOA-000314", "tenant_id": "t1",
           "vs": "VERIFIED", "images": [], "category": "Sweaters & Hoodies", "subcategory": "Hoodie"}


def _classes() -> dict[str, Any]:
    front = row(f"{R2}/1776848573633-front-original.jpg", view="FRONT")
    cut = row(f"{R2}/s-bg-removed-1.png", view="FRONT", processing="BG_REMOVED", derivedFromId=front["id"])
    old = row(f"{R2}/1776848586490-0-processed.png", processing="BG_REMOVED")
    return gc.classify([front, cut, old], sent=[cut["url"], old["url"]])


def test_the_fix_removes_rebuilds_checks_and_pushes(fixer, monkeypatch):
    sc, calls = fixer
    monkeypatch.setattr(sc, "ai_check", lambda p, cuts, by_id: ([], "", []))
    res = sc.fix(PRODUCT, _classes(), dsn="postgresql://x", push=True, ai=True)
    assert any('"deletedAt" = now()' in s for s, _ in calls["sql"])
    assert [s for s, _ in calls["steps"]] == ["rebuild-media-cache.ts", "resync-listings.ts"]
    assert "--include-synced" in calls["steps"][1][1]
    assert res["status"] == "VERIFIED" and not calls["events"]


def test_a_defective_cut_out_holds_the_product_for_a_person(fixer, monkeypatch):
    sc, calls = fixer
    defect = "CUTOUT DEFECT — FRONT cut-out: collar missing where the hanger was removed"
    monkeypatch.setattr(sc, "ai_check", lambda p, cuts, by_id: ([defect], "", ["a hanger stub"]))
    res = sc.fix(PRODUCT, _classes(), dsn="postgresql://x", push=True, ai=True)
    held = [(s, p) for s, p in calls["sql"] if "'HELD_FOR_HUMAN'" in s]
    assert held and "'CUTOUT_DEFECT'" in held[0][0]
    assert held[0][1]["r"].startswith("CUTOUT DEFECT") and "collar missing" in held[0][1]["r"]
    assert not [s for s, _ in calls["sql"] if "'FAILED'" in s]
    assert res["status"] == "HELD_FOR_HUMAN"
    (args, kw), = calls["events"]
    assert args[1] == "hold" and kw["detail"]["defects"] == [defect]
    # The push still goes: the clean-up is right whatever the cut-out looks like.
    assert [s for s, _ in calls["steps"]][-1] == "resync-listings.ts"


def test_no_ai_answer_concludes_nothing(fixer, monkeypatch):
    sc, calls = fixer
    monkeypatch.setattr(sc, "ai_check",
                        lambda p, cuts, by_id: ([], "AI check could not run — no GEMINI_API_KEY", []))
    res = sc.fix(PRODUCT, _classes(), dsn="postgresql://x", push=False, ai=True)
    assert res["status"] == "VERIFIED"
    assert not [p for s, p in calls["sql"] if "'HELD_FOR_HUMAN'" in s]


def test_a_product_checked_but_unchanged_is_not_pushed(fixer, monkeypatch):
    """--all: the AI look for every product, but only a changed gallery is sent."""
    sc, calls = fixer
    monkeypatch.setattr(sc, "ai_check", lambda p, cuts, by_id: ([], "", []))
    front = row(f"{R2}/a-original_front.jpg", view="FRONT", origin="PHOTOBOOTH")
    cut = row(f"{R2}/a-bg-removed.png", view="FRONT", origin="PHOTOBOOTH", processing="BG_REMOVED",
              derivedFromId=front["id"])
    clean = gc.classify([front, cut], sent=[cut["url"]])
    sc.fix({**PRODUCT, "images": [cut["url"]]}, clean, dsn="postgresql://x", push=True, ai=True)
    assert calls["steps"] == []


# --------------------------------------------------------------------------- #
# what fails a cut-out
# --------------------------------------------------------------------------- #
def test_broken_cloth_fails_at_any_severity_background_and_colour_only_when_clear():
    from app.imaging import cutout_integrity as ci

    answer = {"views": [
        {"view": "FRONT", "ok": False, "summary": "", "defects": [
            {"type": "cloth_broken", "part": "collar", "severity": "minor", "where": "back neckline"},
            {"type": "colour", "part": "logo", "severity": "minor", "where": "chest"}]},
        {"view": "BACK", "ok": False, "summary": "", "defects": [
            {"type": "background", "part": "stand", "severity": "clear", "where": "below the hem"},
            {"type": "background", "part": "wall", "severity": "minor", "where": "under the arm"},
            {"type": "colour", "part": "print", "severity": "clear", "where": "turned white"}]},
    ]}
    fails = ci.failing(answer)
    assert any("cloth broken (collar)" in f for f in fails)
    assert any("background (stand)" in f for f in fails)
    assert any("colour (print)" in f for f in fails)
    assert not any("(wall)" in f for f in fails)            # minor background: noted only
    assert not any("(logo)" in f for f in fails)            # minor colour: noted only
    assert any("(wall)" in n for n in ci.noted(answer))
    assert ci.failing({"views": [{"view": "FRONT", "ok": True, "defects": [], "summary": ""}]}) == []
