"""scripts/run_grid.py — the page, drawn from rows shaped like the run tables.

`render()` is pure, so the whole sheet is tested without a database: what is
shown, in what order the statuses are offered, and that nothing a tenant typed
can become markup.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_grid  # noqa: E402

RUN = {
    "id": "adeb4cd2-4399-4676-a30c-5b781a73f6f3", "tenant_id": "514d1b2c-0000-0000-0000-000000000000",
    "tenant": "BOAS", "source": "MANUAL_SINGLE", "status": "COMPLETED",
    "config": {"mode": "SHADOW", "shadowWritesRepairs": True}, "params": {},
    "total": 3, "verified": 1, "held": 1, "failed": 1, "cancelled": 0, "approved": 0,
    "started_at": datetime(2026, 9, 15, 14, 21), "completed_at": datetime(2026, 9, 15, 14, 22),
    "completion_reason": "all products processed", "report_url": None,
}


def _row(**over):
    base = {
        "id": "r1", "product_id": "d219de17-edd8-4267-8986-2c83c95f09cb",
        "sku": "CBOA-006107", "title": "Vintage REALTREE Camouflage Blazer Women L",
        "status": "VERIFIED", "outcome": "READY_NOT_MOVED", "reason": "Passed every check.",
        "error": None, "blocking_rules": [], "preflight_problems": [],
        "steps": [{"step": "twin", "ran": True, "ok": True,
                   "note": "inherited size='L' from BOA-006107", "seconds": 0.2},
                  {"step": "matte", "ran": False, "why": "every garment view already has a cut-out"},
                  {"step": "gate", "ran": True, "ok": True, "note": "passed — on AI_FRONT (cached)",
                   "seconds": 1.2}],
        "deltas": None, "approved": False, "attempts": 1, "duration_ms": 36331,
        "completed_at": datetime(2026, 9, 15, 14, 22), "stage": "REVIEW",
        "images": ["https://r2/lead.jpg"], "render": "https://r2/women-front.jpg",
    }
    base.update(over)
    return base


def test_page_carries_the_run_the_verdicts_and_the_steps():
    rows = [
        _row(),
        _row(id="r2", sku="CBOA-006134", status="HELD_FOR_HUMAN", outcome="PRICE.003",
             reason="PRICE.003; DATA.010; SIZE.010", blocking_rules=["PRICE.003", "SIZE.010"],
             preflight_problems=["no sizing guide"], render=None),
        _row(id="r3", sku="CBOA-006129", status="FAILED", outcome="APPROVE_FAILED",
             error="StepFailed: boom", approved=False, attempts=2),
    ]
    page = run_grid.render(RUN, rows)
    assert page.startswith("<!doctype html>")
    assert "BOAS" in page and "adeb4cd2" in page and "SHADOW + repairs" in page
    for sku in ("CBOA-006107", "CBOA-006134", "CBOA-006129"):
        assert sku in page
    assert "inherited size=&#x27;L&#x27; from BOA-006107" in page
    assert "every garment view already has a cut-out" in page
    assert 'class="chip rule">PRICE.003' in page and 'class="chip pre">no sizing guide' in page
    assert "StepFailed: boom" in page and "attempt 2" in page
    # every terminal status present gets a filter button with its count
    assert 'data-filter="VERIFIED" class="ok">verified <b>1</b>' in page
    assert 'data-filter="HELD_FOR_HUMAN" class="held">held <b>1</b>' in page
    assert 'data-filter="FAILED" class="bad">failed <b>1</b>' in page
    assert 'data-filter="CANCELLED"' not in page
    assert "/product/d219de17-edd8-4267-8986-2c83c95f09cb/edit?tenantId=" in page


def test_tenant_text_cannot_become_markup():
    row = _row(title='<img src=x onerror=alert(1)> "quoted"', reason="<script>alert(2)</script>",
               blocking_rules=["<b>X</b>"])
    page = run_grid.render(RUN, [row])
    assert "<img src=x" not in page and "<script>alert(2)" not in page and "<b>X</b>" not in page
    assert "&lt;script&gt;alert(2)&lt;/script&gt;" in page


def test_picture_prefers_the_judged_render_then_the_lead():
    assert run_grid.pick_image(_row()) == ("https://r2/women-front.jpg", "render")
    assert run_grid.pick_image(_row(render=None)) == ("https://r2/lead.jpg", "lead")
    assert run_grid.pick_image(_row(render=None, images=[])) == (None, "none")
    page = run_grid.render(RUN, [_row(render=None, images=[])])
    assert 'class="noimg">no image' in page


def test_mode_line_and_missing_steps_degrade_gracefully():
    run = {**RUN, "config": None, "completion_reason": None, "completed_at": None}
    page = run_grid.render(run, [_row(steps=None, duration_ms=None, completed_at=None)])
    assert "No step record." in page
    assert "MANUAL_SINGLE · — · COMPLETED" in page
    assert "finished —" in page
