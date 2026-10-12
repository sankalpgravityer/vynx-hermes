"""The repair chain's `cutcheck` step pieces (12 Oct 2026): which cut-outs the
listing shows, the AI verdict, the photo audit deferring to it, and the hold code."""
from __future__ import annotations

from app.imaging import cutout_integrity as ci
from app.imaging import gallery_cleanup as gc
from app.imaging.quality_gate import GateVerdict
from app.services.auto_approval import outcome

R = "https://cdn.example/p1"


def row(id_, url, view="FRONT", origin="PHOTOBOOTH", processing="BG_REMOVED", derived=None,
        deleted=None, created="2026-10-01"):
    return {"id": id_, "url": f"{R}/{url}", "view": view, "origin": origin, "processing": processing,
            "derivedFromId": derived, "deletedAt": deleted, "createdAt": created}


def gallery():
    front = row("f", "front.jpg", processing="ORIGINAL")
    back = row("b", "back.jpg", view="BACK", processing="ORIGINAL")
    return front, back


# --------------------------------------------------------------------------- #
# which cut-outs the listing shows
# --------------------------------------------------------------------------- #
def test_the_cached_cutouts_and_a_recut_the_cache_does_not_list_yet():
    front, back = gallery()
    old_front = row("cf1", "front-cut-1.png", derived="f")
    new_front = row("cf2", "front-cut-2.png", derived="cf1", created="2026-10-12")   # this run's re-cut
    cut_back = row("cb", "back-cut.png", view="BACK", derived="b")
    rows = [front, back, old_front, new_front, cut_back]
    # the cache still lists the superseded FRONT cut-out
    shown = gc.listing_cutouts(rows, [old_front["url"], cut_back["url"]])
    assert [r["id"] for r in shown] == ["cb", "cf2"]


def test_decision_cutouts_size_charts_and_duplicates_are_not_looked_at():
    front, back = gallery()
    dec_photo = row("d", "inspection_front_0.jpg", origin="DECISION", processing="ORIGINAL")
    dec_cut = row("dc", "dec-cut.png", origin="DECISION", derived="d")
    a = row("c1", "a.png", derived="f", created="2026-10-01")
    b = row("c2", "b.png", derived="f", created="2026-10-02")     # same photo, newer: c1 is a duplicate
    chart = row("sc", "size-charts/x.png", view="OTHER", origin="MANUAL")
    chart["url"] = f"{R}/size-charts/x.png"
    rows = [front, back, dec_photo, dec_cut, a, b, chart]
    shown = gc.listing_cutouts(rows, [x["url"] for x in rows])
    assert [r["id"] for r in shown] == ["c2"]


# --------------------------------------------------------------------------- #
# the verdict
# --------------------------------------------------------------------------- #
def _answer(*defects_by_view):
    return {"views": [{"view": v, "ok": not ds, "summary": "", "defects": list(ds)}
                      for v, ds in defects_by_view]}


def test_check_names_the_failing_views_and_holds(monkeypatch):
    front, back = gallery()
    rows = [front, back, row("cf", "f.png", derived="f"), row("cb", "b.png", view="BACK", derived="b")]
    asked = {}

    def judge(pairs, category=None, model=None):
        asked["pairs"] = pairs
        return _answer(("FRONT", [{"type": "cloth_broken", "part": "collar", "severity": "minor", "where": "top"}]),
                       ("BACK", [{"type": "background", "part": "wall", "severity": "minor", "where": "side"}])), ""

    monkeypatch.setattr(ci, "judge", judge)
    pol = {"readiness": {"cutouts": {"hold": "block"}}}
    v = ci.check(rows, [], category="Women > Sweatshirts", pol=pol)
    assert [p["original"] for p in asked["pairs"]] == [front["url"], back["url"]]   # each against its photo
    assert v.action == "bad" and v.bad_views == ["FRONT"] and v.blocks
    assert any("(wall)" in s for s in v.soft)                 # minor background: seen, not held
    assert v.code == "CUTOUT_DEFECT"


def test_a_pass_does_not_hold_and_soft_policy_never_does(monkeypatch):
    front, back = gallery()
    rows = [front, row("cf", "f.png", derived="f")]
    monkeypatch.setattr(ci, "judge", lambda pairs, category=None, model=None: (_answer(("FRONT", [])), ""))
    assert ci.check(rows, [], category=None, pol={"readiness": {"cutouts": {"hold": "block"}}}).action == "ok"
    monkeypatch.setattr(ci, "judge", lambda pairs, category=None, model=None: (_answer(
        ("FRONT", [{"type": "cloth_broken", "part": "cuff", "severity": "clear", "where": ""}])), ""))
    soft = ci.check(rows, [], category=None, pol={"readiness": {"cutouts": {"hold": "soft"}}})
    assert soft.action == "bad" and not soft.blocks


def test_no_answer_holds_only_when_required(monkeypatch):
    front, _ = gallery()
    rows = [front, row("cf", "f.png", derived="f")]
    monkeypatch.setattr(ci, "judge", lambda pairs, category=None, model=None: (None, "AI check could not run — x"))
    v = ci.check(rows, [], category=None, pol={})
    assert v.action == "unknown" and v.unavailable and v.blocks
    off = ci.check(rows, [], category=None, pol={"readiness": {"cutouts": {"integrity": {"required": False}}}})
    assert not off.blocks


def test_nothing_to_look_at_is_skipped():
    front, back = gallery()
    v = ci.check([front, back], [], category=None, pol={})
    assert v.action == "skipped" and not v.blocks


def test_two_cutouts_of_one_view_are_told_apart(monkeypatch):
    front, _ = gallery()
    other = row("o", "o.jpg", view="FRONT", origin="WEB", processing="ORIGINAL")
    rows = [front, other, row("c1", "1.png", derived="f"), row("c2", "2.png", derived="o")]
    seen = {}
    monkeypatch.setattr(ci, "judge", lambda pairs, category=None, model=None:
                        (seen.setdefault("v", [p["view"] for p in pairs]) and _answer(("FRONT", [])), ""))
    ci.check(rows, [r["url"] for r in rows], category=None, pol={})
    assert seen["v"] == ["FRONT", "FRONT 2"]


# --------------------------------------------------------------------------- #
# the photo audit defers to it; the hold code on an approved product
# --------------------------------------------------------------------------- #
def test_the_photo_audit_cutout_finding_stops_holding_its_other_findings_do_not():
    from scripts.repair_product import _without_cutout_defects

    only_cut = GateVerdict("review", "CUTOUT_DEFECT", ["CUTOUT DEFECT — FRONT cut-out: neckline missing"])
    v = _without_cutout_defects(only_cut)
    assert not v.blocks and v.code is None and v.soft == ["CUTOUT DEFECT — FRONT cut-out: neckline missing"]

    both = GateVerdict("review", "CUTOUT_DEFECT", ["CUTOUT DEFECT — FRONT cut-out: neckline missing",
                                                  "MASTER CATEGORY — looks like a men's shirt"])
    v = _without_cutout_defects(both)
    assert v.blocks and v.code == "MASTER_CATEGORY_MISMATCH"
    assert v.reasons == ["MASTER CATEGORY — looks like a men's shirt"]

    other = GateVerdict("review", "IMAGE_DEFECT", ["IMAGE DEFECT — two models in frame"])
    assert _without_cutout_defects(other) is other


def test_an_approved_product_held_by_the_cutcheck_is_coded_cutout_defect():
    v = outcome.classify({"approval": {"outcome": "skipped_preflight", "blockers": [
        "stage is APPROVED", "cut-out check: CUTOUT DEFECT — BACK cut-out: cloth broken (waistband) [clear]"]},
        "remaining": [], "verified": True})
    assert v.status == "HELD_FOR_HUMAN" and v.outcome == "CUTOUT_DEFECT"
    assert "waistband" in v.reason


def test_held_products_kinds():
    from scripts.held_products import kind_of

    assert kind_of("CUTOUT_DEFECT") == "Cut-out"
    assert kind_of("PREFLIGHT_CUT_OUTS__FRONT__NECKLINE__1_0__OF_THE_G") == "Cut-out"
    assert kind_of("NO_SIZE_CHART") == "Size / size chart"
    assert kind_of("SIZE.014") == "Size / size chart"
    assert kind_of("NO_BRAND_OR_SIZE") == "Brand / size missing"
    assert kind_of("WOMEN_RIG_ON_A_MEN_PRODUCT") == "Mannequin / rig"
    assert kind_of("TAX.005") == "Category / taxonomy"
    assert kind_of("SOMETHING_NEW") == "Other"
