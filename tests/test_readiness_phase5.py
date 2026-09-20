"""Readiness phase 5 — the gallery order, the copy, the backlog.

docs/READINESS-PLAN.md §4 steps 7 and 8, cold: the catalog order as a sort key
and IMG.025, TEXT.008, the chain's `order` and `copy` steps with every I/O edge
replaced, the remote options, and the readiness audit's decision table. No
database, no network, no subprocess, no model.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import cutouts  # noqa: E402
from app.imaging import photo_audit as pa  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402
from app.models import MediaAsset, ProductSnapshot  # noqa: E402
from app.rules.consistency import check_copy  # noqa: E402
from app.rules.imagery import (  # noqa: E402
    check_imagery, gallery_divergence, gallery_label, gallery_order,
)
from app.vnyx_client import to_snapshot  # noqa: E402
from scripts import audit_readiness as ar  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

POL = policy()


# --------------------------------------------------------------------------- #
# The catalog order
# --------------------------------------------------------------------------- #

def m(view: str, origin: str, processing: str = "BG_REMOVED", position: int = 0,
      url: str | None = None) -> MediaAsset:
    return MediaAsset(url=url or f"https://r2/{view}-{origin}-{processing}-{position}.png",
                      view=view, origin=origin, processing=processing, position=position)


def snap(media: list[MediaAsset], images: list[str] | None = None, manual: bool = False) -> ProductSnapshot:
    return ProductSnapshot(id="p1", title="Vintage Zara Black Jacket Men M", master_category="Men",
                           category="Jackets", subcategory="Jackets", media=media,
                           images=images if images is not None else [x.url for x in media],
                           media_manual_order=manual)


def test_the_catalog_order_is_renders_uploads_booth_portal_label_chart_then_raws():
    rows = [
        m("SIZE_CHART", "SIZE_GUIDE", "RAW"),
        m("LABEL", "DECISION", "RAW"),
        m("BACK", "DECISION"), m("FRONT", "DECISION"),
        m("BACK", "PHOTOBOOTH"), m("FRONT", "PHOTOBOOTH"),
        m("FRONT", "WEB"), m("FRONT", "MANUAL"),
        m("AI_CLOSEUP", "AI", "GENERATED"), m("AI_FRONT", "AI", "GENERATED"), m("AI_FRONT_34", "AI", "GENERATED"),
        m("FRONT", "PHOTOBOOTH", "RAW"), m("FRONT", "DECISION", "RAW"),
    ]
    got = [gallery_label(x) for x in gallery_order(snap(rows), POL)]
    assert got == [
        "AI_FRONT_34/AI", "AI_FRONT/AI", "AI_CLOSEUP/AI",
        "FRONT/MANUAL", "FRONT/WEB",
        "FRONT/PHOTOBOOTH", "BACK/PHOTOBOOTH",      # the booth's front AND back …
        "FRONT/DECISION", "BACK/DECISION",          # … before the portal's (decision 2)
        "FRONT/PHOTOBOOTH/raw", "FRONT/DECISION/raw",
        "LABEL/DECISION/raw", "SIZE_CHART/SIZE_GUIDE/raw",
    ]


def test_divergence_names_the_first_slot_that_is_wrong_and_ignores_unknown_urls():
    rows = [m("AI_FRONT", "AI", "GENERATED"), m("FRONT", "PHOTOBOOTH"), m("FRONT", "DECISION")]
    expected = gallery_order(snap(rows), POL)
    assert gallery_divergence([r.url for r in expected], expected) is None
    swapped = [rows[0].url, rows[2].url, rows[1].url, "https://legacy/not-a-row.jpg"]
    d = gallery_divergence(swapped, expected)
    assert d["index"] == 1 and d["actual"] == "FRONT/DECISION" and d["expected"] == "FRONT/PHOTOBOOTH"
    assert d["actual_sequence"] == ["AI_FRONT/AI", "FRONT/DECISION", "FRONT/PHOTOBOOTH"]


def test_img025_is_soft_by_default_block_by_policy_and_silent_for_a_hand_arranged_gallery():
    rows = [m("AI_FRONT", "AI", "GENERATED"), m("FRONT", "PHOTOBOOTH"), m("FRONT", "DECISION")]
    wrong = [rows[0].url, rows[2].url, rows[1].url]
    found = {f.rule_id: f for f in check_imagery(snap(rows, images=wrong), POL)}
    assert "IMG.025" in found and found["IMG.025"].severity.value == "low"
    assert "position 2 shows FRONT/DECISION where FRONT/PHOTOBOOTH belongs" in found["IMG.025"].message
    block = copy.deepcopy(POL)
    block["readiness"]["gallery"]["hold"] = "block"
    assert {f.rule_id: f for f in check_imagery(snap(rows, images=wrong), block)}["IMG.025"].severity.value == "high"
    # The manual flag is NOT trusted by default (vnyx-api set it on every save);
    # a policy that trusts it keeps the rule silent on flagged galleries.
    assert "IMG.025" in {f.rule_id for f in check_imagery(snap(rows, images=wrong, manual=True), POL)}
    trusting = copy.deepcopy(POL)
    trusting["readiness"]["gallery"]["respect_manual"] = True
    assert "IMG.025" not in {f.rule_id for f in check_imagery(snap(rows, images=wrong, manual=True), trusting)}
    assert "IMG.025" not in {f.rule_id for f in check_imagery(snap(rows), POL)}
    off = copy.deepcopy(POL)
    off["readiness"]["gallery"]["enabled"] = False
    assert "IMG.025" not in {f.rule_id for f in check_imagery(snap(rows, images=wrong), off)}


# --------------------------------------------------------------------------- #
# TEXT.008 — the title contradicts the record
# --------------------------------------------------------------------------- #

def copy_snap(title: str, **over: Any) -> ProductSnapshot:
    base = {"id": "p1", "tenantId": "t1", "title": title, "summary": "A jacket.",
            "masterCategory": "Men", "category": "Jackets", "subCategory": "Jackets",
            "size": "M", "brand": "Zara", "color": "Black", "gender": ["men"]}
    base.update(over)
    return to_snapshot(base)


def text008(title: str, **over: Any) -> list[str]:
    return [f.message for f in check_copy(copy_snap(title, **over), POL) if f.rule_id == "TEXT.008"]


def test_a_title_ending_in_another_letter_size_contradicts_the_record():
    msgs = text008("Vintage Zara Black Jacket Men XL", size="M")
    assert msgs and "'XL'" in msgs[0] and "'M'" in msgs[0]
    assert text008("Vintage Zara Black Jacket Men M", size="M") == []
    assert text008("Vintage Zara Black Jacket Men 2XL", size="XXL") == []      # one scale
    assert text008("Vintage Levi's Blue Jeans Men 32", size="32",
                   category="Bottoms", subCategory="Jeans") == []           # numeric is TEXT.003's


def test_a_title_naming_another_garment_family_contradicts_the_record():
    msgs = text008("Vintage Levi's Blue Jeans Men M", subCategory="T-Shirts", category="Tops")
    assert msgs and "bottoms" in msgs[0] and "'T-Shirts'" in msgs[0]
    # Same family, adjacent families, or no family word: quiet.
    assert text008("Vintage Nike Grey Hoodie Men M", subCategory="T-Shirts", category="Tops") == []
    assert text008("Vintage Zara Black Jacket Men M", subCategory="T-Shirts", category="Tops") == []
    assert text008("Vintage Zara Black Piece Men M", subCategory="T-Shirts", category="Tops") == []
    assert text008("Vintage Zara Black Jeans Men M", subCategory=None, category=None) == []


# --------------------------------------------------------------------------- #
# The chain: order and copy
# --------------------------------------------------------------------------- #

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000001"
AI_URL, BOOTH_URL, PORTAL_URL = "https://x/ai.png", "https://x/booth.png", "https://x/portal.png"
MEDIA = [
    {"url": AI_URL, "view": "AI_FRONT", "origin": "AI", "processing": "GENERATED", "mediaType": "IMAGE", "position": 0},
    {"url": BOOTH_URL, "view": "FRONT", "origin": "PHOTOBOOTH", "processing": "BG_REMOVED", "mediaType": "IMAGE", "position": 1},
    {"url": PORTAL_URL, "view": "FRONT", "origin": "DECISION", "processing": "BG_REMOVED", "mediaType": "IMAGE", "position": 2},
]
IN_ORDER = [AI_URL, BOOTH_URL, PORTAL_URL]
OUT_OF_ORDER = [AI_URL, PORTAL_URL, BOOTH_URL]


def _record(**over: Any) -> dict[str, Any]:
    # `Outerwear > Jackets`, not `Jackets > Jackets`.
    #
    # The old path was not one the taxonomy holds — `Jackets` is a SUBCATEGORY
    # under `Outerwear`, never a category — so this fixture carried a standing
    # TAX.002. That was invisible while nothing read it, and became three
    # failures the moment the copy step learned to withhold on exactly that
    # finding (docs/RECONCILE-CORRUPTION.md §2.3). These tests are about whether
    # the copy step FIRES, so their product has to be one the rules are content
    # with; leaving it broken would have meant weakening the guard to keep a
    # typo working.
    base = {"id": PID, "tenantId": "t1", "gender": ["men"], "masterCategory": "Men",
            "category": "Outerwear", "subCategory": "Jackets", "size": "M", "brand": "Zara",
            "color": "Black", "sizingGuide": None, "updatedAt": None,
            "title": "Vintage Zara Black Jacket Men M", "summary": "A jacket.",
            "images": list(IN_ORDER), "mediaManualOrder": False}
    base.update(over)
    return base


def _state(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "loaded": {"record": record, "media": MEDIA, "catalog": {}, "imagery_settings": None},
        "title": record["title"], "tenant": "T", "sku": "T-1", "stage": "REVIEW",
        "review_status": "PENDING", "edit_url": None,
        "description_missing": False, "description_chars": 100,
        "care_label": 1, "unmatted": 0, "unmatted_views": [], "leftover_raw": 0,
        "cutout_views": [], "garment_photos": 2, "master": "Men", "mannequin": "Men Top", "size": "M",
        "renders": 5, "render_rows": 5, "renders_missing": 0,
        "generation_status": "COMPLETE", "is_regenerating": False, "attributes_missing": [],
    }


@pytest.fixture
def wired(monkeypatch):
    """The product BEFORE the chain, and what each write step turns it into."""
    calls: dict[str, Any] = {"approve": [], "steps": [], "rebuilt": False, "reconciled": False, "copied": False}
    world: dict[str, Any] = {
        "before": _record(),                                    # the pre-chain read
        "after_reconcile": None,                                # what reconcile writes (None = nothing)
        "after_rebuild": _record(),                             # what the cache looks like once rebuilt
        "after_copy": _record(title="Vintage Zara Black Jacket Men M (new)"),
        "copy_payload": {"ok": True, "outcome": "regenerated", "applied": ["title", "summary"],
                         "failed": [], "title": {"before": "old", "after": "Vintage Zara Black Jacket Men M (new)"}},
    }

    def current() -> dict[str, Any]:
        rec = dict(world["before"])
        if calls["reconciled"] and world["after_reconcile"]:
            rec.update(world["after_reconcile"])
        if calls["rebuilt"]:
            rec["images"] = world["after_rebuild"]["images"]
        if calls["copied"]:
            rec["title"] = world["after_copy"]["title"]
        return rec

    def fake_needs(dsn, pid):
        return _state(current())

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        calls["steps"].append((script, list(args)))
        if script == "verify-and-repair.ts":
            if "--apply" in args:
                calls["reconciled"] = True
            return True, "", {"ok": True, "applied": [], "failed": []}
        if script == "rebuild-media-cache.ts":
            if "--apply" in args:
                calls["rebuilt"] = True
            return True, "order would change : 1", None
        if script == "regenerate-copy.ts":
            if "--apply" in args:
                calls["copied"] = True
            return True, "", world["copy_payload"]
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "approve_check",
                        lambda *a, **k: calls["approve"].append(k["apply"]) or {"outcome": "would_approve", "problems": []})
    monkeypatch.setattr(qg, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(cutouts, "judge", lambda s, p, **kw: cutouts.CutoutVerdict("skipped"))
    monkeypatch.setattr(rp.product_audit, "audit",
                        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}})
    return {"calls": calls, "world": world}


def _repair(apply: bool = False) -> dict[str, Any]:
    return rp.repair(DSN, PID, apply=apply, vnyx_api=Path("."), infer=False, min_confidence=70,
                     skip_render=True, approve=False, skip_bin=True, quiet=True, silent=True)


def step(r: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in r["steps"] if s["step"] == name)


def scripts_run(calls: dict[str, Any], name: str) -> list[list[str]]:
    return [args for script, args in calls["steps"] if script == name]


def test_an_ordered_gallery_and_an_unchanged_record_touch_nothing(wired):
    r = _repair(apply=True)
    assert step(r, "order")["note"] == "gallery in the catalog order"
    assert step(r, "copy")["ran"] is False and "agrees with the record" in step(r, "copy")["why"]
    assert scripts_run(wired["calls"], "rebuild-media-cache.ts") == []
    assert scripts_run(wired["calls"], "regenerate-copy.ts") == []
    assert r["order"]["in_order"] is True and r["copy"]["triggers"] == []


def test_an_out_of_order_gallery_is_rebuilt_and_re_checked(wired):
    wired["world"]["before"] = _record(images=list(OUT_OF_ORDER))
    r = _repair(apply=True)
    args = scripts_run(wired["calls"], "rebuild-media-cache.ts")
    assert len(args) == 1 and "--apply" in args[0] and "--product" in args[0]
    note = step(r, "order")["note"]
    assert note.startswith("rebuilt the media cache") and "now in the catalog order" in note
    assert "position 2 shows FRONT/DECISION where FRONT/PHOTOBOOTH belongs" in note
    assert r["order"]["rebuilt"] is True and r["order"]["in_order"] is True


def test_a_dry_run_says_what_it_would_rebuild(wired):
    wired["world"]["before"] = _record(images=list(OUT_OF_ORDER))
    r = _repair(apply=False)
    args = scripts_run(wired["calls"], "rebuild-media-cache.ts")
    assert len(args) == 1 and "--apply" not in args[0]
    assert step(r, "order")["note"].startswith("would rebuild the media cache")
    assert r["order"]["rebuilt"] is False


def test_a_gallery_a_person_arranged_is_left_alone_when_the_flag_is_trusted(wired, monkeypatch):
    trusting = copy.deepcopy(POL)
    trusting["readiness"]["gallery"]["respect_manual"] = True
    monkeypatch.setattr(rp, "policy", lambda: trusting)
    wired["world"]["before"] = _record(images=list(OUT_OF_ORDER), mediaManualOrder=True)
    r = _repair(apply=True)
    assert "a person arranged" in step(r, "order")["note"]
    assert scripts_run(wired["calls"], "rebuild-media-cache.ts") == []
    assert r["order"]["manual"] is True


def test_a_flagged_gallery_is_rebuilt_by_default_because_the_flag_marks_a_save_not_a_choice(wired):
    """Production: 1,538 of 4,877 approved products carry mediaManualOrder and sit
    in the machine's own old order — updateProduct set it on every save."""
    wired["world"]["before"] = _record(images=list(OUT_OF_ORDER), mediaManualOrder=True)
    wired["world"]["after_rebuild"] = _record(mediaManualOrder=True)
    r = _repair(apply=True)
    args = scripts_run(wired["calls"], "rebuild-media-cache.ts")
    assert len(args) == 1 and "--include-manual" in args[0] and "--apply" in args[0]
    assert step(r, "order")["note"].startswith("rebuilt the media cache")
    assert r["order"]["manual"] is True and r["order"]["in_order"] is True
    # And IMG.025 judges such a gallery too.
    rows = [m("AI_FRONT", "AI", "GENERATED"), m("FRONT", "PHOTOBOOTH"), m("FRONT", "DECISION")]
    wrong = [rows[0].url, rows[2].url, rows[1].url]
    assert "IMG.025" in {f.rule_id for f in check_imagery(snap(rows, images=wrong, manual=True), POL)}


def test_a_field_the_title_reads_changing_this_run_regenerates_the_copy(wired):
    # Reconcile writes the gender (a Women master cascading, say): the title's
    # inputs moved, so the copy is regenerated and the TEXT rules run again.
    wired["world"]["after_reconcile"] = {"gender": ["women"], "masterCategory": "Women"}
    r = _repair(apply=True)
    assert r["copy"]["changed"] == ["gender", "masterCategory"]
    args = scripts_run(wired["calls"], "regenerate-copy.ts")
    assert len(args) == 1 and "--apply" in args[0]
    note = step(r, "copy")["note"]
    assert note.startswith("regenerated title and description (gender changed, masterCategory changed")
    assert "title now" in note and r["copy"]["title_after"].endswith("(new)")
    assert r["copy"]["rules_after"] is not None


def test_a_title_that_already_contradicts_the_record_regenerates_the_copy(wired):
    wired["world"]["before"] = _record(title="Vintage Zara Black Jacket Men XL")     # size is M
    r = _repair(apply=False)
    assert r["copy"]["rules"] == ["TEXT.008"]
    assert step(r, "copy")["note"] == "would regenerate title and description (TEXT.008)"
    assert "--apply" not in scripts_run(wired["calls"], "regenerate-copy.ts")[0]


def test_a_copy_script_failure_is_a_failed_step_not_a_silent_pass(wired):
    wired["world"]["before"] = _record(title="Vintage Zara Black Jacket Men XL")
    wired["world"]["copy_payload"] = {"ok": False, "outcome": "partial", "failed": ["title"], "error": None}
    r = _repair(apply=True)
    s = step(r, "copy")
    assert s["ran"] and s["ok"] is False and "partial" in s["note"]


def test_the_copy_step_has_a_policy_switch(wired, monkeypatch):
    off = copy.deepcopy(POL)
    off["readiness"]["copy"]["enabled"] = False
    monkeypatch.setattr(rp, "policy", lambda: off)
    wired["world"]["before"] = _record(title="Vintage Zara Black Jacket Men XL")
    r = _repair(apply=True)
    assert step(r, "copy")["ran"] is False and "disabled" in step(r, "copy")["why"]


def test_run_remote_names_the_new_steps_and_the_copy_options(monkeypatch):
    assert rp.STEP_FOR_SCRIPT["rebuild-media-cache.ts"] == "reorder"
    assert rp.STEP_FOR_SCRIPT["regenerate-copy.ts"] == "copy"
    sent: dict[str, Any] = {}

    class Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "output": "", "results": None}

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, json, headers, timeout: sent.update(json) or Resp())
    monkeypatch.setenv("VNYX_API_URL", "http://api.test")
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "s")
    rp.run_remote("regenerate-copy.ts", ["--db", DSN, "--product", PID, "--apply", "--title"],
                  timeout_s=10, quiet=True)
    assert sent["step"] == "copy" and sent["options"] == {"title": True}
    rp.run_remote("rebuild-media-cache.ts", ["--db", DSN, "--product", PID, "--apply"], timeout_s=10, quiet=True)
    assert sent["step"] == "reorder" and sent["options"] == {} and sent["apply"] is True
    rp.run_remote("rebuild-media-cache.ts", ["--db", DSN, "--product", PID, "--apply", "--include-manual"],
                  timeout_s=10, quiet=True)
    assert sent["options"] == {"includeManual": True}


# --------------------------------------------------------------------------- #
# The readiness audit's decision table
# --------------------------------------------------------------------------- #

RECORD = {"id": "p", "size": "XL", "imageSettings": {"bodyType": "m"}, "careLabelCount": 1}
RENDERS = [{"view": v, "isCurrent": True} for v in ar.AI_VIEWS]


def codes(issues: list[dict[str, str]]) -> list[str]:
    return sorted(i["code"] for i in issues)


def test_rules_map_to_named_issues_with_a_fix_each():
    findings = [
        {"rule_id": "TAX.003", "severity": "high", "message": "sub not under category"},
        {"rule_id": "IMG.025", "severity": "low", "message": "out of order"},
        {"rule_id": "IMG.026", "severity": "low", "message": "cropped"},
        {"rule_id": "TEXT.008", "severity": "medium", "message": "title says XL"},
        {"rule_id": "IMG.002", "severity": "medium", "message": "missing AI_BACK"},
        {"rule_id": "PRICE.003", "severity": "high", "message": "price outside window"},
        {"rule_id": "DRIFT.001", "severity": "low", "message": "copies differ"},
    ]
    issues = ar.assess_readiness(RECORD, RENDERS, {"state": "ok"}, findings)
    assert codes(issues) == ["BODY_TYPE_UNRECORDED", "CUTOUT_ZOOMED", "ORDER_WRONG", "RENDER_MISSING",
                             "RULE:PRICE.003", "SUBCATEGORY_NOT_UNDER_CATEGORY", "TITLE_STALE"]
    assert ar.fixes_for(issues) == ["rematte", "regen", "reorder", "copy"]
    assert ar.worst(issues) == "high"
    body = next(i for i in issues if i["code"] == "BODY_TYPE_UNRECORDED")
    assert "'m'" in body["detail"] and "implies 'xl'" in body["detail"] and body["fix"] == "none"


def test_evidence_from_the_two_picture_checks_becomes_issues():
    measured = {"action": "bad", "reasons": ["FRONT: frame: touches all 4 edges", "BACK: backdrop: border is #DADAD8"]}
    judged = {"action": "regen", "code": "BODY_SIZE_MISMATCH", "reasons": ["slim on an XL"], "soft": []}
    issues = ar.assess_readiness({**RECORD, "imageSettings": {"bodyType": "xl"}}, RENDERS, {"state": "ok"}, [],
                                 measured=measured, judged=judged)
    assert codes(issues) == ["CUTOUT_NOT_BACKDROP", "CUTOUT_ZOOMED", "RENDER_BODY_MISMATCH"]
    assert ar.fixes_for(issues) == ["rematte", "regen"]
    soft = ar.assess_readiness(RECORD, [], {"state": "ok"}, [],
                               judged={"action": "ok", "code": None, "reasons": [], "soft": ["build one band off — model average"]})
    assert codes(soft) == ["RENDER_BUILD_ONE_BAND"] and ar.fixes_for(soft) == []


def test_a_clean_product_has_no_issue_and_the_chart_state_is_read():
    assert ar.assess_readiness({**RECORD, "imageSettings": {"bodyType": "xl"}}, RENDERS, {"state": "ok"}, []) == []
    issues = ar.assess_readiness({**RECORD, "imageSettings": {}}, RENDERS, {"state": "GUIDE HAS NO CHART IMAGES"}, [])
    assert codes(issues) == ["SIZE_CHART_MISSING"]


def test_the_flagged_lists_carry_ids_per_fix_and_the_commands():
    rows = [{"pid": "a", "fixes": ["rematte", "copy"], "issues": []},
            {"pid": "b", "fixes": ["regen"], "issues": []},
            {"pid": "c", "fixes": [], "issues": []}]
    fl = ar.flagged(rows)
    assert fl["rematte"] == ["a"] and fl["copy"] == ["a"] and fl["regen"] == ["b"] and fl["reorder"] == []
    assert fl["counts"] == {"rematte": 1, "regen": 1, "reorder": 0, "copy": 1}
    assert set(fl["commands"]) == {"rematte", "regen", "reorder", "copy"}


def test_the_copy_is_withheld_while_the_record_still_misdescribes_the_garment(wired):
    """The 18 September 2026 corruption, at the step that made it visible.

    `reconcile` wrote `category: 'hoodie'` on an Adidas t-shirt; TAX.002 rejected
    it on the next line of the same run; `copy` then fired on "category changed"
    and produced "Vintage Adidas Deep Burgundy Hoodie Women S". The title is
    generated FROM the taxonomy, so regenerating it against a record the rules
    have already refused cannot repair anything — it only describes the wrong
    product convincingly, and a wrong title is far harder to spot than a wrong
    `subCategory`.

    The triggers still fire here. The step is withheld in spite of them, which is
    the whole point: this is not "nothing changed", it is "something changed and
    it is not safe to write about it yet".
    """
    wired["world"]["before"] = _record(subCategory="hoodie",
                                       title="Vintage Zara Black Jacket Men XL")
    r = _repair(apply=True)
    s = step(r, "copy")

    assert s["ran"] is False
    assert "TAX.003" in s["why"] or "TAX.002" in s["why"]
    assert "describe the wrong product" in s["why"]
    assert r["copy"]["withheld"]
    assert scripts_run(wired["calls"], "regenerate-copy.ts") == []


def test_a_record_the_rules_are_content_with_still_regenerates(wired):
    """The guard narrows the copy step, it does not switch it off."""
    wired["world"]["before"] = _record(title="Vintage Zara Black Jacket Men XL")
    r = _repair(apply=False)
    s = step(r, "copy")
    assert s["ran"] is not False or "would regenerate" in s.get("note", "")
    assert not r["copy"].get("withheld")
