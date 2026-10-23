"""When the sequence render leaves views missing, Hermes' own generator makes them.

23 Oct 2026, the user: vnyx-api's sequence render runs on one Gemini call with no
second vendor. TST-000078 failed five shots in 33s; BOAS and Klekt products
"rendered 3 of 6" in ~5s and wrote nothing — silently. The render step (and a
regen round) now asks backfill-imagery.ts --views WITHOUT --sequence, which goes
through Hermes' /v1/imagery/generate: its Gemini models, then OpenAI gpt-image.

No network, no database, no subprocess.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy_overlay  # noqa: E402
from app.imaging import photo_audit as pa  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000078"


def _media(views):
    return [{"url": f"https://x/{v}.png", "view": v, "mediaType": "IMAGE", "position": i}
            for i, v in enumerate(views)]


@pytest.fixture
def chain(monkeypatch):
    """A product with AI_FRONT only; its sequence still wants full_back (and a
    second full front). `world["views"]` is what the stored product holds."""
    world: dict[str, Any] = {"views": ["AI_FRONT"], "scripts": [],
                             "sequence_writes": [], "hermes_writes": ["AI_BACK"]}

    def fake_needs(dsn, pid):
        return {
            "loaded": {"record": {"gender": ["men"], "category": "T-Shirts & Polos",
                                  "subCategory": "Tank Top", "sizingGuide": None,
                                  "updatedAt": None},
                       "media": _media(world["views"]), "catalog": {}},
            "title": "Vintage Adidas Scarlet Tank Top Men L", "tenant": "T", "sku": "TST-000078",
            "stage": "REVIEW", "review_status": "PENDING", "edit_url": None,
            "description_missing": False, "description_chars": 100, "care_label": 1,
            "unmatted": 0, "unmatted_views": [], "leftover_raw": 0,
            "renders": len(world["views"]), "render_rows": len(world["views"]),
            "renders_missing": 2, "renders_expected": 6,
            "sequence": "the tenant's default sequence",
            "missing_shots": ["full_back", "full_front#2"],
            "generation_status": "COMPLETE", "is_regenerating": False,
            "attributes_missing": [],
        }

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        world["scripts"].append((script, list(args)))
        if script == "backfill-imagery.ts":
            if "--sequence" in args:
                made = list(world["sequence_writes"])
            else:
                asked = args[args.index("--views") + 1].split(",")
                made = [v for v in asked if v in world["hermes_writes"]]
            world["views"] = world["views"] + [m for m in made if m not in world["views"]]
            return True, f"  wrote [{', '.join(made) or 'nothing'}]\n", None
        if script == "verify-and-repair.ts":
            return True, "", {"ok": True, "applied": [], "failed": []}
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "remote_supports", lambda *o: True)
    monkeypatch.setattr(rp, "approve_check",
                        lambda *a, **k: {"outcome": "would_approve", "problems": []})
    monkeypatch.setattr(rp, "shopify_product_id", lambda dsn, pid: None)
    monkeypatch.setattr(qg, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(rp.product_audit, "audit",
                        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}})
    return world


def _repair(**kw: Any) -> dict[str, Any]:
    args = dict(apply=True, vnyx_api=Path("."), infer=False, min_confidence=70,
                skip_render=False, approve=False, skip_bin=True, quiet=True, silent=True,
                skip_matte=True)
    args.update(kw)
    return rp.repair(DSN, PID, **args)


def _render_step(r):
    return next(s for s in r["steps"] if s["step"] == "render")


def _backfills(world):
    return [a for s, a in world["scripts"] if s == "backfill-imagery.ts"]


def test_a_silent_empty_sequence_render_goes_to_hermes_for_the_missing_view(chain):
    r = _repair()
    calls = _backfills(chain)
    assert "--sequence" in calls[0]
    # the fallback: --views, no --sequence, and only the view still missing —
    # full_front#2 asks for nothing because AI_FRONT is already on file
    fb = [a for a in calls if "--sequence" not in a]
    assert fb and fb[0][fb[0].index("--views") + 1] == "AI_BACK"
    assert "AI_BACK" in chain["views"]
    assert "Hermes generator (Gemini, then OpenAI) for AI_BACK: made them" in _render_step(r)["note"]


def test_nothing_is_asked_when_the_sequence_render_made_everything(chain):
    chain["sequence_writes"] = ["AI_BACK"]
    r = _repair()
    assert all("--sequence" in a for a in _backfills(chain))
    assert "Hermes generator" not in _render_step(r)["note"]


def test_a_view_hermes_cannot_make_either_is_reported_missing(chain):
    chain["hermes_writes"] = []
    r = _repair()
    assert "still missing AI_BACK" in _render_step(r)["note"]


def test_no_fallback_on_a_dry_run(chain):
    _repair(apply=False)
    assert all("--sequence" in a for a in _backfills(chain))


def test_the_policy_switch_turns_it_off(chain):
    with policy_overlay({"imagery": {"render_fallback_hermes": False}}):
        r = _repair()
    assert all("--sequence" in a for a in _backfills(chain))
    assert "Hermes generator" not in _render_step(r)["note"]
