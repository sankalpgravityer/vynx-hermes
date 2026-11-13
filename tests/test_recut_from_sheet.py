"""scripts/recut_from_sheet.py (12 Nov 2026): re-cut a list's cut-outs and push them to
Shopify, and nothing else. No network, no database."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import recut_from_sheet as rc  # noqa: E402
from app.imaging import cutout_integrity as ci  # noqa: E402

OK = ci.IntegrityVerdict("ok")
BAD = ci.IntegrityVerdict("bad", reasons=["CUTOUT DEFECT — BACK cut-out: background (stand)"], bad_views=["BACK"])


@pytest.fixture
def world(monkeypatch):
    w: dict[str, Any] = {"views": ["BACK", "FRONT"], "verdicts": [OK], "calls": [],
                         "on_shopify": "123", "status": "Active", "pending": False}

    def fake_step(vnyx_api, script, args, **kw):
        w["calls"].append([script, *map(str, args)])
        if script == "resync-listings.ts":
            return True, "queued: 1", None
        return True, "  abc FRONT -> hermes  3000x4000", None

    monkeypatch.setattr(rc, "_cut_views", lambda pid: list(w["views"]))
    monkeypatch.setattr(rc, "_dedupe", lambda dsn, pid: 0)
    monkeypatch.setattr(rc, "_look", lambda pid: w["verdicts"].pop(0) if w["verdicts"] else OK)
    monkeypatch.setattr(rc, "_fallback", lambda: ["gemini-paint"])
    monkeypatch.setattr(rc.rp, "run_step", fake_step)
    monkeypatch.setattr(rc.rp, "remote_supports", lambda *a: True)
    monkeypatch.setattr(rc.rp, "shopify_product_id", lambda dsn, pid: w["on_shopify"])
    monkeypatch.setattr(rc.rp, "vnyx_status", lambda dsn, pid: w["status"])
    monkeypatch.setattr(rc.rr, "_price_change_pending", lambda pid: w["pending"])
    return w


def _one(apply=True):
    return rc.recut_one("dsn", "pid", "SKU-1", apply=apply, vnyx_api=Path("."))


def _scripts(w):
    return [c[0] for c in w["calls"]]


def test_a_dry_run_cuts_nothing(world):
    assert _one(apply=False)["result"] == "WOULD re-cut BACK, FRONT"
    assert world["calls"] == []


def test_it_recuts_every_view_unguarded_orders_and_pushes(world):
    r = _one()
    assert r["result"].startswith("RE-CUT — passed")
    assert _scripts(world) == ["backfill-bg-removal.ts", "rebuild-media-cache.ts", "resync-listings.ts"]
    cut = world["calls"][0]
    assert "--replace" in cut and "--keep-better" not in cut and "--views" not in cut


def test_only_the_cut_outs_are_touched(world):
    _one()
    touched = set(_scripts(world))
    for other in ("backfill-imagery.ts", "approve-products.ts", "settle-stock.ts", "reconcile.ts"):
        assert other not in touched


def test_a_view_the_ai_still_refuses_goes_to_gemini_once(world):
    world["verdicts"] = [BAD, OK]
    r = _one()
    cuts = [c for c in world["calls"] if c[0] == "backfill-bg-removal.ts"]
    assert len(cuts) == 2
    assert cuts[1][cuts[1].index("--views") + 1] == "BACK"
    assert cuts[1][cuts[1].index("--bg-strategies") + 1] == "gemini-paint"
    assert r["result"].startswith("RE-CUT — passed")


def test_still_refused_after_gemini_says_so(world):
    world["verdicts"] = [BAD, BAD]
    assert "still refuses" in _one()["result"]


@pytest.mark.parametrize("status", ["Draft", "Archived"])
def test_never_pushes_a_draft_or_archived_product(world, status):
    world["status"] = status
    _one()
    assert "resync-listings.ts" not in _scripts(world)


def test_never_pushes_while_a_price_change_is_pending(world):
    world["pending"] = True
    _one()
    assert "resync-listings.ts" not in _scripts(world)


def test_nothing_to_cut_is_skipped(world):
    world["views"] = []
    assert _one()["result"].startswith("SKIPPED")
    assert world["calls"] == []


NOTED = ci.IntegrityVerdict("ok", soft=[
    "CUTOUT DEFECT — FRONT cut-out: cloth broken (zipper) — along the central front zipper [clear]",
    "CUTOUT DEFECT — FRONT cut-out: cloth broken (collar) — left side of the collar opening [clear]"])


def test_a_clear_broken_cloth_note_is_believed_and_goes_to_gemini(world):
    # BEV-000103 (12 Nov 2026): the chain only NOTES a broken zipper and collar; every
    # product here was judged bad by a person, so the note sends FRONT to Gemini.
    world["verdicts"] = [NOTED, OK]
    r = _one()
    cuts = [c for c in world["calls"] if c[0] == "backfill-bg-removal.ts"]
    assert len(cuts) == 2 and cuts[1][cuts[1].index("--views") + 1] == "FRONT"
    assert r["result"].startswith("RE-CUT — passed")


def test_a_clear_note_still_there_after_gemini_says_look_at_it(world):
    world["verdicts"] = [NOTED, NOTED]
    assert "still refuses" in _one()["result"]


def test_a_minor_note_is_not_a_failure(world):
    world["verdicts"] = [ci.IntegrityVerdict("ok", soft=[
        "CUTOUT DEFECT — BACK cut-out: cloth broken (hem) — slight [minor]"])]
    _one()
    assert len([c for c in world["calls"] if c[0] == "backfill-bg-removal.ts"]) == 1


def _answer(world, text):
    def fake_step(vnyx_api, script, args, **kw):
        world["calls"].append([script, *map(str, args)])
        if script == "resync-listings.ts":
            return True, "queued: 1", None
        return True, text if script == "backfill-bg-removal.ts" else "", None
    rc.rp.run_step = fake_step


def test_nothing_written_is_not_called_passed(world, monkeypatch):
    # BOA-002434 (12 Nov 2026): every attempt refused, the old cut-outs stayed, and the
    # AI passed the OLD ones — the run said "passed". It must say nothing was re-cut.
    monkeypatch.setattr(rc.rp, "run_step", rc.rp.run_step)
    _answer(world, "  abc FRONT  FAILED: background removal failed: gemini-paint#1: re-drew the garment\n"
                   "  abc BACK   FAILED: background removal failed: cloth-seg-ft#1: tore")
    r = _one()
    assert r["result"].startswith("NOT RE-CUT") and "FRONT" in r["result"]


def test_one_view_refused_is_partly(world, monkeypatch):
    monkeypatch.setattr(rc.rp, "run_step", rc.rp.run_step)
    _answer(world, "  abc BACK   -> hermes + #ebebeb  2000x2667 on source 3000x4000  [replace]\n"
                   "  abc FRONT  FAILED: fetch failed")
    r = _one()
    assert r["result"].startswith("PARTLY — BACK re-cut; FRONT kept the old cut-out")
    assert "run it again" in r["result"]


def test_push_only_never_recuts(world):
    r = rc.push_one("dsn", "pid", "SKU-1", apply=True, vnyx_api=Path("."))
    assert _scripts(world) == ["rebuild-media-cache.ts", "resync-listings.ts"]
    assert r["result"] == "PUSH — Shopify push queued"


def test_push_only_keeps_the_safety_rules(world):
    world["status"] = "Draft"
    rc.push_one("dsn", "pid", "SKU-1", apply=True, vnyx_api=Path("."))
    assert "resync-listings.ts" not in _scripts(world)


def test_an_other_view_the_ai_refuses_is_reported_not_sent(world):
    # SKU-000055 (12 Nov 2026): --views OTHER made vnyx-api answer 400 and the product's
    # whole log was lost. OTHER is reported as left as it was; nothing is sent for it.
    world["views"] = ["BACK", "FRONT", "OTHER"]
    world["verdicts"] = [ci.IntegrityVerdict("bad", reasons=["CUTOUT DEFECT — OTHER cut-out: colour (x)"],
                                             bad_views=["OTHER"])]
    r = _one()
    cuts = [c for c in world["calls"] if c[0] == "backfill-bg-removal.ts"]
    assert len(cuts) == 1                                   # no --views OTHER call
    assert "OTHER kept the old cut-out" in r["result"] and "FRONT and BACK only" in r["result"]


def test_a_vnyx_api_error_does_not_lose_the_product(world, monkeypatch):
    def boom(vnyx_api, script, args, **kw):
        world["calls"].append([script, *map(str, args)])
        if script == "backfill-bg-removal.ts":
            raise rc.rp.StepFailed("backfill-bg-removal.ts: vnyx-api returned 400")
        return True, "queued: 1", None
    monkeypatch.setattr(rc.rp, "run_step", boom)
    r = _one()
    assert r["result"].startswith("NOT RE-CUT") and "returned 400" in r["result"]
