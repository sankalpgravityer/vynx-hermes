"""scripts/rerender_from_sheet.py (4 Nov 2026): re-render a list's old AI images and
nothing else. No network, no database."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import rerender_from_sheet as rr  # noqa: E402

BEFORE = datetime(2026, 10, 31, 20, 0, tzinfo=timezone.utc)


@pytest.fixture
def world(monkeypatch):
    w: dict[str, Any] = {"stale": ["AI_BACK", "AI_FRONT"], "seq": False, "refuse": [[]],
                         "calls": [], "on_shopify": "123", "status": "Active", "sellable": 0}

    def fake_step(vnyx_api, script, args, **kw):
        w["calls"].append([script, *args])
        if script == "backfill-imagery.ts":
            views = args[args.index("--views") + 1]
            w["stale"] = []                      # rendered: nothing old is left
            return True, f"wrote [{views}]", None
        if script == "resync-listings.ts":
            return True, "queued: 1", None
        return True, "", None

    def fake_judge(dsn, pid):
        nxt = w["refuse"].pop(0) if w["refuse"] else []
        return nxt, (["gate IMAGE_QUALITY: extra fingers"] if nxt else [])

    monkeypatch.setattr(rr.rp, "stale_render_views", lambda dsn, pid, before: list(w["stale"]))
    monkeypatch.setattr(rr.rp, "needs", lambda dsn, pid: {"sequence": "x" if w["seq"] else None})
    monkeypatch.setattr(rr.rp, "_sequence_render", lambda st: w["seq"])
    monkeypatch.setattr(rr.rp, "_sequence_views", lambda st: ["AI_FRONT", "AI_BACK"])
    monkeypatch.setattr(rr.rp, "run_step", fake_step)
    monkeypatch.setattr(rr.rp, "remote_supports", lambda *a: True)
    monkeypatch.setattr(rr.rp, "shopify_product_id", lambda dsn, pid: w["on_shopify"])
    monkeypatch.setattr(rr.rp, "vnyx_status", lambda dsn, pid: w["status"])
    monkeypatch.setattr(rr.rp, "vnyx_sellable", lambda dsn, pid: w["sellable"])
    monkeypatch.setattr(rr, "_judge", fake_judge)
    monkeypatch.setattr(rr, "_price_change_pending", lambda pid: w.get("pending", False))
    return w


def _one(**kw):
    args = dict(before=BEFORE, apply=True, rounds=2, vnyx_api=Path("."))
    args.update(kw)
    return rr.rerender_one("dsn", "pid", "SKU-1", **args)


def _scripts(w):
    return [c[0] for c in w["calls"]]


def test_a_product_already_on_the_new_prompt_is_skipped(world):
    world["stale"] = []
    assert _one()["result"].startswith("SKIPPED")
    assert world["calls"] == []


def test_a_dry_run_renders_nothing(world):
    r = _one(apply=False)
    assert r["result"].startswith("WOULD re-render AI_BACK, AI_FRONT")
    assert world["calls"] == []


def test_it_renders_the_old_views_orders_and_pushes_to_shopify(world):
    r = _one()
    assert r["result"].startswith("RE-RENDERED")
    assert _scripts(world) == ["backfill-imagery.ts", "rebuild-media-cache.ts", "resync-listings.ts"]
    render = world["calls"][0]
    assert render[render.index("--views") + 1] == "AI_BACK,AI_FRONT" and "--apply" in render


def test_by_the_sequence_it_replaces(world):
    world["seq"] = True
    _one()
    assert "--sequence" in world["calls"][0] and "--replace" in world["calls"][0]


def test_a_refused_view_is_rendered_again_once(world):
    world["refuse"] = [["AI_FRONT"], []]
    r = _one()
    renders = [c for c in world["calls"] if c[0] == "backfill-imagery.ts"]
    assert len(renders) == 2 and renders[1][renders[1].index("--views") + 1] == "AI_FRONT"
    assert r["result"].startswith("RE-RENDERED")


def test_still_refused_after_the_last_round_says_so(world):
    world["refuse"] = [["AI_FRONT"], ["AI_FRONT"]]
    assert "still refuse AI_FRONT" in _one()["result"]


@pytest.mark.parametrize("status", ["Draft", "Archived"])
def test_never_pushes_a_draft_or_archived_product(world, status):
    # 4 Nov 2026: their push would send the status; only Active keeps Shopify's.
    world["status"] = status
    r = _one()
    assert "resync-listings.ts" not in _scripts(world)
    assert r["result"].startswith("RE-RENDERED")      # the pictures are still re-done


def test_never_pushes_while_a_price_change_is_pending(world):
    world["pending"] = True
    _one()
    assert "resync-listings.ts" not in _scripts(world)


def test_not_on_shopify_pushes_nothing(world):
    world["on_shopify"] = None
    _one()
    assert "resync-listings.ts" not in _scripts(world)


def test_it_reads_the_render_summary():
    assert rr._read_render("x\nwrote [full_front, closeup]\ncould not produce [full_back]") == (
        ["full_front", "closeup"], ["full_back"])
