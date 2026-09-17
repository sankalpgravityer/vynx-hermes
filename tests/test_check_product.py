"""scripts/check_product.py — the issue → fix mapping, cold, and the --apply wiring.

No database: `fix_for`, `issues_from` and `diff` take the dicts run_gate returns;
`main` is exercised with `inspect` and the chain replaced.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import check_product as cp  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

PLAN = [
    {"kind": "set_column", "field": "masterCategory", "value": "Men", "reason": "TAX.001"},
    {"kind": "set_property", "field": "gender", "value": ["men"], "reason": "TAX.004"},
    {"kind": "escalate", "field": "sizing_guide", "code": "SIZE.010", "detail": "no guide carries size 46"},
]


def f(rule_id: str, fields: list[str], severity: str = "high", message: str = "x") -> dict[str, Any]:
    return {"rule_id": rule_id, "fields": fields, "severity": severity, "message": message}


def test_a_planned_write_is_the_fix_for_the_rule_that_asked_for_it():
    fix = cp.fix_for(f("TAX.001", ["master_category"]), PLAN)
    assert fix["kind"] == "write" and fix["field"] == "masterCategory" and "'Men'" in fix["text"]
    fix = cp.fix_for(f("TAX.004", ["gender"]), PLAN)
    assert fix["kind"] == "write" and fix["value"] == ["men"]


def test_an_escalation_means_a_person_decides():
    fix = cp.fix_for(f("SIZE.010", ["sizing_guide"]), PLAN)
    assert fix["kind"] == "person" and "no guide carries size 46" in fix["text"]


def test_imagery_copy_and_price_rules_name_their_chain_step():
    assert cp.fix_for(f("IMG.025", ["images"], "low"), [])["step"] == "order"
    assert cp.fix_for(f("IMG.026", ["images"], "low"), [])["step"] == "matte"
    assert cp.fix_for(f("TEXT.008", ["title", "size"], "medium"), [])["step"] == "copy"
    render = cp.fix_for(f("IMG.002", ["images"], "medium"), [])
    assert render["step"] == "render" and "paid" in render["text"]
    assert cp.fix_for(f("PRICE.003", ["price"]), [])["step"] == "price"
    photo = cp.fix_for(f("IMG.003", ["images"]), [])
    assert photo["kind"] == "none" and "garment photograph" in photo["text"]


def test_an_empty_extractable_field_goes_to_the_care_label_pass():
    fix = cp.fix_for(f("DATA.010", ["material"]), [])
    assert fix["step"] == "extract" and "material" in fix["text"]
    assert cp.fix_for(f("SOME.999", ["thing"]), [])["text"] == "reported only"


def test_issues_are_listed_worst_first_with_advisory_marked():
    gate = {"blocking": [f("TAX.001", ["master_category"], "high"), f("SIZE.010", ["sizing_guide"], "high")],
            "advisory": [f("IMG.025", ["images"], "low"), f("TEXT.008", ["title"], "medium")],
            "repair_plan": PLAN}
    rows = cp.issues_from(gate)
    assert [r["rule_id"] for r in rows] == ["SIZE.010", "TAX.001", "TEXT.008", "IMG.025"]
    assert rows[0]["blocking"] and not rows[3]["blocking"]
    assert rows[1]["fix"]["kind"] == "write" and rows[0]["fix"]["kind"] == "person"


def test_picture_verdicts_are_issue_rows_and_a_defective_render_names_its_re_render():
    """MID-000253: the gate passed the lead; the photo audit refused the
    AI_FRONT_34. The row says which view goes, that the model stays, and that
    renders are paid."""
    from app.config import policy

    pol = policy()
    judged = {"action": "ok", "code": None, "reasons": [], "lead_view": "AI_FRONT", "bad_views": [],
              "unavailable": False}
    audited = {"action": "regen", "code": "RENDER_DEFECT",
               "reasons": ["RENDER DEFECT — AI_FRONT_34 render: AI artifact on legs"],
               "bad_views": ["AI_FRONT_34"], "unavailable": False, "raw": {"wear": "none"}}
    rows, blocks = cp.picture_issues(judged, audited, None, pol)
    assert blocks and [r["rule_id"] for r in rows] == ["PHOTOS:RENDER_DEFECT"]
    assert rows[0]["blocking"] and rows[0]["fix"]["step"] == "regen"
    assert "re-render AI_FRONT_34" in rows[0]["fix"]["text"] and "same model" in rows[0]["fix"]["text"]
    assert "--render" in rows[0]["fix"]["text"]

    # A hold for a person, and an audit that could not run, hold and do not hold.
    held = {**audited, "action": "review", "code": "GRADE_SUSPECT",
            "reasons": ["GRADE SUSPECT — major vs none"], "bad_views": []}
    rows, blocks = cp.picture_issues(None, held, None, pol)
    assert blocks and rows[0]["rule_id"] == "PHOTOS:GRADE_SUSPECT" and rows[0]["fix"]["kind"] == "person"
    down = {**audited, "action": "review", "code": "VISION_UNAVAILABLE", "reasons": ["429"],
            "bad_views": [], "unavailable": True}
    rows, blocks = cp.picture_issues(None, down, None, pol)
    assert not blocks and not rows[0]["blocking"] and "could not run" in rows[0]["fix"]["text"]

    # MID-000569: a cut-out missing its collar, flagged while the cut-out
    # policy is soft — an advisory row whose fix is the re-cut.
    eaten = {"action": "ok", "code": None, "reasons": [],
             "soft": ["CUTOUT DEFECT — FRONT cut-out: collar cut away by background removal"],
             "bad_views": [], "bad_cutouts": ["FRONT"], "unavailable": False, "raw": {"wear": "none"}}
    rows, blocks = cp.picture_issues(None, eaten, None, pol)
    assert not blocks and [r["rule_id"] for r in rows] == ["PHOTOS:CUTOUT_DEFECT"]
    assert not rows[0]["blocking"] and rows[0]["fix"]["step"] == "rematte"
    assert "re-cut FRONT" in rows[0]["fix"]["text"] and "collar cut away" in rows[0]["message"]

    # The gate's own refusal, beside it, in order: gate, photos, cut-outs.
    gate_bad = {"action": "regen", "code": "IMAGE_COMPOSITION", "reasons": ["FRAME — AI_BACK: empty band"],
                "lead_view": "AI_FRONT", "bad_views": ["AI_BACK"], "unavailable": False}
    measured = {"action": "bad", "reasons": ["FRONT: cropped"], "bad_views": ["FRONT"]}
    rows, blocks = cp.picture_issues(gate_bad, audited, measured, pol)
    assert [r["rule_id"] for r in rows] == ["GATE:IMAGE_COMPOSITION", "PHOTOS:RENDER_DEFECT", "CUTOUT:MEASURED"]
    assert "re-render AI_BACK" in rows[0]["fix"]["text"] and rows[2]["fix"]["step"] == "matte"


def test_diff_names_resolved_remaining_and_new():
    before = [{"rule_id": "TAX.001"}, {"rule_id": "IMG.025"}, {"rule_id": "PRICE.003"}]
    after = [{"rule_id": "PRICE.003"}, {"rule_id": "TEXT.008"}]
    assert cp.diff(before, after) == {"resolved": ["IMG.025", "TAX.001"], "remaining": ["PRICE.003"],
                                      "new": ["TEXT.008"]}


def _report(issues: list[str]) -> dict[str, Any]:
    return {"product_id": "p", "sku": "T-1", "title": "t", "tenant": "T", "stage": "REVIEW", "edit_url": None,
            "record": {k: None for k in ("masterCategory", "category", "subCategory", "gender", "size", "euSize",
                                         "sizingGuide", "mannequinType", "brand", "color", "material",
                                         "condition", "grade", "price", "retailPrice")},
            "media": {"renders": [], "cutouts": [], "raw_garments": [], "labels": 0, "chart": "ok",
                      "gallery_manual": False},
            "master": {"action": "keep", "detail": "Men"}, "verified": not issues,
            "issues": [{"rule_id": r, "severity": "high", "blocking": True, "fields": [], "message": "m",
                        "detail": {}, "fix": {"kind": "none", "step": None, "text": "reported only"}}
                       for r in issues],
            "plan": [], "held": [], "price": None, "measured": None, "judged": None}


def test_apply_runs_the_chain_without_approval_and_reports_the_diff(monkeypatch, capsys):
    calls: dict[str, Any] = {}
    reports = iter([_report(["TAX.001", "IMG.025"]), _report(["IMG.025"])])
    monkeypatch.setattr(cp, "inspect", lambda dsn, pid, **kw: next(reports))

    def fake_repair(dsn, pid, **kw):
        calls.update(kw)
        return {"steps": [{"step": "reconcile", "ran": True, "ok": True, "note": "wrote masterCategory"}],
                "verified": False, "remaining": ["IMG.025"]}

    monkeypatch.setattr(rp, "repair", fake_repair)
    rc = cp.main(["--db", "postgresql://test", "--product", "00000000-0000-0000-0000-000000000001", "--apply"])
    assert rc == 0
    assert calls["apply"] is True and calls["approve"] is False and calls["skip_render"] is True
    out = capsys.readouterr().out
    assert "ISSUES BEFORE (2)" in out and "ISSUES AFTER (1)" in out
    assert "RESOLVED 1: TAX.001" in out and "REMAINING 1: IMG.025" in out


def test_render_is_off_unless_asked(monkeypatch):
    calls: dict[str, Any] = {}
    monkeypatch.setattr(cp, "inspect", lambda dsn, pid, **kw: _report([]))
    monkeypatch.setattr(rp, "repair", lambda dsn, pid, **kw: calls.update(kw) or {"steps": []})
    cp.main(["--db", "postgresql://test", "--product", "00000000-0000-0000-0000-000000000001", "--apply", "--render"])
    assert calls["skip_render"] is False


def test_a_dry_run_never_touches_the_chain(monkeypatch, capsys):
    monkeypatch.setattr(cp, "inspect", lambda dsn, pid, **kw: _report(["TAX.001"]))
    monkeypatch.setattr(rp, "repair", lambda *a, **k: (_ for _ in ()).throw(AssertionError("chain ran")))
    rc = cp.main(["--db", "postgresql://test", "--product", "00000000-0000-0000-0000-000000000001"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ISSUES (1)" in out and "APPLYING" not in out
