"""run_from_sheet --approve-rejected: Rejected straight to Approved (25 Oct 2026).

vnyx-api approves only from REVIEW and refuses REJECTED. The user wants rejected
BOAS products that now pass to go straight to Approved without ever showing in
Review, so repair() puts the STAGE at REVIEW for the approval call alone and puts
it back when the product is not approved. No network, no database.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.imaging import photo_audit as pa  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402
from app.services.auto_approval import claim  # noqa: E402
from scripts import repair_product as rp  # noqa: E402
from scripts import run_from_sheet as rfs  # noqa: E402

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-0000000000ab"


def test_the_claim_admits_a_rejected_review_state_only_when_asked():
    assert "REJECTED" not in claim.review_statuses(["REVIEW"])
    assert "REJECTED" in claim.review_statuses(["REVIEW", "REJECTED"])


def test_eligibility_widens_to_rejected_but_keeps_decision_rejections_out():
    clauses = dict(rfs.eligibility_clauses(retry=True, stages=["REVIEW", "REJECTED"]))
    assert "'REJECTED'" in clauses["review status pending"]
    assert "'REJECTED'" in next(sql for name, sql in clauses.items() if name.startswith("in "))
    assert "not decision-rejected" in clauses
    # without the flag nothing changes
    plain = dict(rfs.eligibility_clauses(retry=True, stages=["REVIEW"]))
    assert "REJECTED" not in plain["review status pending"]


@pytest.fixture
def chain(monkeypatch):
    world: dict[str, Any] = {"flips": [], "outcome": "approved", "raise": False,
                             "blocked": False}

    def fake_needs(dsn, pid):
        return {
            "loaded": {"record": {"gender": ["men"], "category": "T-Shirts & Polos",
                                  "subCategory": "T-Shirt", "sizingGuide": "Men Uppers",
                                  "updatedAt": None},
                       "media": [{"url": f"https://x/{v}.png", "view": v, "mediaType": "IMAGE",
                                  "position": i} for i, v in enumerate(
                                      ["AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"])],
                       "catalog": {}},
            "title": "Vintage Nike Black T-Shirt Men L", "tenant": "BOAS", "sku": "BOA-1",
            "stage": "REJECTED", "review_status": "REJECTED", "edit_url": None,
            "description_missing": False, "description_chars": 100, "care_label": 1,
            "unmatted": 0, "unmatted_views": [], "leftover_raw": 0, "renders": 5,
            "render_rows": 5, "renders_missing": 0, "generation_status": "COMPLETE",
            "is_regenerating": False, "attributes_missing": [],
        }

    def fake_flip(dsn, pid, frm, to):
        world["flips"].append((frm, to))
        return True

    def fake_approve_check(vnyx_api, dsn, pid, *, apply, **kw):
        world["approve_apply"] = apply
        if world["raise"]:
            raise RuntimeError("vnyx-api down")
        return {"outcome": world["outcome"] if apply else "would_approve", "problems": [],
                "stageBefore": "REVIEW", "stageAfter": "APPROVED", "publishStatus": "archived"}

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", lambda *a, **k: (True, "", {"ok": True, "applied": [], "failed": [], "results": []}))
    monkeypatch.setattr(rp, "flip_rejected_stage", fake_flip)
    monkeypatch.setattr(rp, "approve_check", fake_approve_check)
    monkeypatch.setattr(rp, "shopify_product_id", lambda dsn, pid: None)
    monkeypatch.setattr(qg, "judge", lambda media, **kw: (
        GateVerdict("review", "IMAGE_QUALITY", ["bad lead"]) if world["blocked"] else GateVerdict("ok")))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(rp.product_audit, "audit",
                        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}})
    return world


def _repair(**kw: Any) -> dict[str, Any]:
    args = dict(apply=True, vnyx_api=Path("."), infer=False, min_confidence=70, skip_render=True,
                approve=True, skip_bin=True, quiet=True, silent=True, skip_matte=True,
                approve_from_rejected=True)
    args.update(kw)
    return rp.repair(DSN, PID, **args)


def test_an_approved_product_is_moved_once_and_not_put_back(chain):
    _repair()
    assert chain["flips"] == [("REJECTED", "REVIEW")]
    assert chain["approve_apply"] is True


def test_a_product_the_preflight_holds_goes_back_to_rejected(chain):
    chain["outcome"] = "skipped_preflight"
    _repair()
    assert chain["flips"] == [("REJECTED", "REVIEW"), ("REVIEW", "REJECTED")]


def test_an_error_during_approval_still_puts_it_back(chain):
    # The crash propagates out of repair(); the stage must be back regardless.
    chain["raise"] = True
    with pytest.raises(RuntimeError, match="vnyx-api down"):
        _repair()
    assert chain["flips"] == [("REJECTED", "REVIEW"), ("REVIEW", "REJECTED")]


def test_a_picture_refusal_never_moves_the_stage(chain):
    chain["blocked"] = True
    _repair()
    assert chain["flips"] == []


def test_without_the_flag_or_on_a_dry_run_nothing_moves(chain):
    _repair(approve_from_rejected=False)
    _repair(apply=False)
    _repair(approve=False)
    assert chain["flips"] == []
