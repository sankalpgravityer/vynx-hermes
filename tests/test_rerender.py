"""run_from_sheet --rerender (1 Nov 2026): re-make the AI images on the current
prompt even when every check passed, then judge them like any re-render.
No network, no database."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import repair_product as rp  # noqa: E402
from scripts import run_from_sheet as rfs  # noqa: E402
from tests.test_approve_rejected import _repair, chain  # noqa: E402,F401


@pytest.fixture
def renders(chain, monkeypatch):
    calls: list[list[str]] = []

    def fake_step(vnyx_api, script, args, **kw):
        calls.append([script, *map(str, args)])
        return True, "", {"ok": True, "applied": [], "failed": [], "results": []}

    monkeypatch.setattr(rp, "run_step", fake_step)
    asked: list[object] = []
    monkeypatch.setattr(rp, "stale_render_views",
                        lambda dsn, pid, before=None: (asked.append(before), ["AI_BACK", "AI_FRONT"])[1])
    return {"calls": calls, "asked": asked}


def _imagery(calls):
    return [c for c in calls if c[0] == "backfill-imagery.ts"]


def test_nothing_is_re_rendered_without_the_flag(renders):
    _repair(approve_from_rejected=False, skip_render=False)
    assert _imagery(renders["calls"]) == []


def test_the_flag_re_renders_the_stale_views_though_every_check_passed(renders):
    when = datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc)
    _repair(approve_from_rejected=False, skip_render=False, force_rerender=True,
            rerender_before=when)
    made = _imagery(renders["calls"])
    assert made, "no re-render ran"
    views = made[0][made[0].index("--views") + 1].split(",")
    assert set(views) == {"AI_BACK", "AI_FRONT"}
    assert renders["asked"][0] is when
    # once: the checks passed the new images, so no second forced round
    assert len(made) == 1


def test_rendered_before_reads_utc_by_default():
    assert rfs._when("2026-11-01 14:30") == datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)
    assert rfs._when("2026-11-01T20:00+05:30").utcoffset().total_seconds() == 19800


def test_a_sequence_re_render_retires_the_older_sets_leftovers(renders, monkeypatch):
    # BOA-007716 (13 Nov 2026): the sequence re-made its 4 shots; the 26 Oct second
    # front and close-up (another model) stayed live. They are retired after the
    # re-render, when they can first be told apart from the new set.
    from datetime import datetime as dt
    from app.services.auto_approval import db as aadb

    monkeypatch.setattr(rp, "_sequence_render", lambda st: True)
    monkeypatch.setattr(rp, "_sequence_views", lambda st: ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK"])
    rendered = {"done": False}
    real = rp.run_step

    def step(vnyx_api, script, args, **kw):
        if script == "backfill-imagery.ts":
            rendered["done"] = True
        return real(vnyx_api, script, args, **kw)
    monkeypatch.setattr(rp, "run_step", step)
    four = rp.shot_sequence.resolve(overrides=[], default_shots=[
        {"shot": k, "count": 1, "enabled": True}
        for k in ("three_quarter_front", "three_quarter_back", "full_front", "full_back")])
    monkeypatch.setattr(rp.shot_sequence, "from_payload", lambda v: four)
    old = [{"id": "x", "view": "AI_CLOSEUP", "createdAt": dt(2026, 10, 26)}]
    monkeypatch.setattr(rp.shot_sequence, "leftover_renders",
                        lambda res, rows: list(old) if rendered["done"] else [])
    monkeypatch.setattr(aadb, "fetch_all", lambda *a, **k: [])
    deleted = []
    monkeypatch.setattr(aadb, "execute", lambda sql, params=None: (deleted.append(params), 1)[1])
    r = _repair(approve_from_rejected=False, skip_render=False, force_rerender=True)
    assert deleted and deleted[0]["ids"] == ["x"]
    regen = next(s for s in r["steps"] if s["step"] == "regen")
    assert "retired 1 render(s)" in regen["note"]
