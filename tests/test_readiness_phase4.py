"""Readiness phase 4 — the renders.

docs/READINESS-PLAN.md §4 step 5, cold: the size → build band, the gate's new
`model_build` judgement and its exemptions, which views a refusal re-renders,
the chain's one paid regeneration with every I/O edge replaced, the two
unfixable codes, and the runner's rejection guards. No database, no network,
no subprocess, no model.
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
from app.services.auto_approval import runner  # noqa: E402
from app.services.auto_approval.outcome import Verdict, classify  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

POL = policy()
ALL_VIEWS = list(POL["imagery"]["all_views"])

GOOD = {"model_present": True, "face_ok": True, "gender": "Men", "model_build": "average",
        "lead_ok": True, "body_coherent": True, "body_issue": "", "view": "front",
        "garment": "jacket", "confidence": 0.9}


# --------------------------------------------------------------------------- #
# The size → build band
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("size,band", [
    ("XS", "slim"), ("s", "slim"), ("XXS", "slim"), ("M", "average"), ("L", "average"),
    ("XL", "plus"), ("2XL", "plus"), ("XXXL", "plus"), ("x l", "plus"),
])
def test_letter_sizes_map_to_a_band(size, band):
    assert qg.size_band(size, pol=POL) == band


def test_nothing_follows_from_an_unknown_or_numeric_top_size():
    for size in (None, "", "Unknown", "unknown", "48", "12", "One Size"):
        assert qg.size_band(size, pol=POL) is None, size


def test_a_waist_is_a_band_only_on_bottoms_with_a_gender():
    assert qg.size_band("W32", pol=POL) is None                                  # not bottoms
    assert qg.size_band("W32", bottoms=True, pol=POL) is None                    # no gender
    assert qg.size_band("W32", gender="men", bottoms=True, pol=POL) == "average"
    assert qg.size_band("W32", gender="women", bottoms=True, pol=POL) == "plus"
    assert qg.size_band("34/32", gender="men", bottoms=True, pol=POL) == "average"  # l
    assert qg.size_band("28", gender="women", bottoms=True, pol=POL) == "average"
    assert qg.size_band("44", gender="men", bottoms=True, pol=POL) == "plus"


# --------------------------------------------------------------------------- #
# decide(): the build against the size
# --------------------------------------------------------------------------- #

# THE SHIPPED `block_on` NO LONGER CARRIES `body_size_mismatch` (19 Sep 2026):
# it re-rendered the whole set on one band of judgement and was 24.8% of a
# measured run. The question is still ASKED and still recorded in `soft`.
#
# So the mechanism tests below run against a policy that still funds it —
# otherwise they would be testing the price rather than the behaviour — and
# `test_the_shipped_gate_does_not_regenerate_on_the_build` pins what ships.
POL_BUILD_BLOCKS = copy.deepcopy(POL)
POL_BUILD_BLOCKS["quality_gate"]["block_on"] = [
    *POL["quality_gate"]["block_on"], "body_size_mismatch"]


def _decide(raw_over: dict, *, build: str | None, size: Any = None, gender="men",
            pol=POL_BUILD_BLOCKS):
    return qg.decide({**GOOD, **raw_over}, product_gender=gender, accessory=False, pol=pol,
                     product_build=build, product_size=size)


def test_two_bands_apart_regenerates_and_names_the_size():
    v = _decide({"model_build": "slim"}, build="plus", size="XL")
    assert v.action == "regen" and v.code == "BODY_SIZE_MISMATCH" and v.blocks
    assert "slim" in v.reasons[0] and "size XL" in v.reasons[0] and "plus" in v.reasons[0]
    assert (v.build_seen, v.build_expected) == ("slim", "plus")


def test_plus_on_a_slim_garment_regenerates_too():
    v = _decide({"model_build": "plus"}, build="slim", size="XS")
    assert v.code == "BODY_SIZE_MISMATCH"


def test_one_band_apart_is_a_soft_flag():
    v = _decide({"model_build": "average"}, build="plus", size="XL")
    assert v.action == "ok" and not v.blocks
    assert any("one band off" in s for s in v.soft)


def test_low_confidence_turns_a_two_band_gap_into_a_flag():
    v = _decide({"model_build": "slim", "confidence": 0.5}, build="plus", size="XL")
    assert v.action == "ok"
    assert any("one band off" in s and "0.50" in s for s in v.soft)


def test_no_expectation_or_no_reading_means_no_judgement():
    assert _decide({"model_build": "slim"}, build=None).action == "ok"
    v = _decide({"model_build": "unknown"}, build="plus", size="XL")
    assert v.action == "ok" and not any("band" in s for s in v.soft)
    assert v.build_seen is None and v.build_expected == "plus"


def test_gender_is_judged_before_the_build():
    """One re-render fixes both; the code should name the model, not the size."""
    v = _decide({"gender": "Women", "model_build": "slim"}, build="plus", size="XL")
    assert v.code == "MODEL_GENDER_MISMATCH"


def test_the_build_check_has_two_switches():
    off = copy.deepcopy(POL)
    off["readiness"]["body_size"]["enabled"] = False
    assert _decide({"model_build": "slim"}, build="plus", size="XL", pol=off).action == "ok"
    soft_only = copy.deepcopy(POL)
    soft_only["quality_gate"]["block_on"] = [
        b for b in soft_only["quality_gate"]["block_on"] if b != "body_size_mismatch"]
    v = _decide({"model_build": "slim"}, build="plus", size="XL", pol=soft_only)
    assert v.action == "ok" and any("one band off" in s for s in v.soft)


def test_the_shipped_gate_does_not_regenerate_on_the_build():
    """WHAT SHIPS (19 Sep 2026). The build question is still asked and still
    recorded; it just stops costing a whole re-rendered set.

    Measured over 18 products: the gate is 6s each (2.3% of the run) and the
    re-render it asks for is 156s (24.8%), the largest line — and a build read
    off a render is one band of judgement, where a gender or a garment family
    is a fact about the picture."""
    v = _decide({"model_build": "slim"}, build="plus", size="XL", pol=POL)
    assert v.action == "ok" and v.code is None and not v.blocks
    # Asked, answered, and on the row — just not blocking.
    assert v.build_seen == "slim" and v.build_expected == "plus"
    assert any("band off" in s for s in v.soft)


def test_the_shipped_gate_still_refuses_the_two_that_matter():
    """`gender_mismatch` and `category_mismatch` are untouched — a woman
    modelling a men's shirt, and a tank top filed as a dress, are still worth a
    regeneration and a hold respectively."""
    wrong_model = _decide({"gender": "Women"}, build=None, gender="men", pol=POL)
    assert wrong_model.action == "regen" and wrong_model.code == "MODEL_GENDER_MISMATCH"

    # The gender is judged BEFORE the category, so it has to agree here or it
    # would be the one answering — GOOD reads "Men".
    wrong_shelf = qg.decide({**GOOD, "garment": "tank top"}, product_gender="men",
                            accessory=False, pol=POL, category="Dresses",
                            subcategory="Casual Dress")
    assert wrong_shelf.code == "CATEGORY_IMAGE_MISMATCH"


def test_a_model_cut_at_the_knees_on_a_full_body_view_is_re_rendered():
    """MID-000247: the lead render ended mid-thigh and passed every check —
    anatomically coherent, right gender, right build. Framing is its own question."""
    v = qg.decide({**GOOD, "framing": "cropped_legs"}, product_gender="men", accessory=False,
                  pol=POL, expect_full_body=True)
    assert v.action == "regen" and v.code == "MODEL_CROPPED"
    assert "legs are cut" in v.reasons[0] and "head to feet" in v.reasons[0]
    assert v.framing_seen == "cropped_legs"
    for framing in ("upper_body", "head_and_shoulders", "close_up"):
        assert qg.decide({**GOOD, "framing": framing}, product_gender="men", accessory=False,
                         pol=POL, expect_full_body=True).code == "MODEL_CROPPED"


def test_framing_is_judged_only_where_a_full_body_is_expected():
    # Not expected (a close-up view, footwear, an accessory — the caller decides): nothing.
    v = qg.decide({**GOOD, "framing": "cropped_legs"}, product_gender="men", accessory=False,
                  pol=POL, expect_full_body=False)
    assert v.action == "ok" and not v.soft
    # A full figure passes; an older answer without the field says nothing.
    assert qg.decide({**GOOD, "framing": "full_body"}, product_gender="men", accessory=False,
                     pol=POL, expect_full_body=True).action == "ok"
    assert qg.decide(GOOD, product_gender="men", accessory=False, pol=POL,
                     expect_full_body=True).action == "ok"
    # Low confidence is a flag; the policy switch turns it into one too.
    v = qg.decide({**GOOD, "framing": "cropped_legs", "confidence": 0.5}, product_gender="men",
                  accessory=False, pol=POL, expect_full_body=True)
    assert v.action == "ok" and any("framing cropped_legs" in s for s in v.soft)
    soft_only = copy.deepcopy(POL)
    soft_only["quality_gate"]["block_on"] = [b for b in soft_only["quality_gate"]["block_on"] if b != "cropped_model"]
    v = qg.decide({**GOOD, "framing": "cropped_legs"}, product_gender="men", accessory=False,
                  pol=soft_only, expect_full_body=True)
    assert v.action == "ok" and any("framing" in s for s in v.soft)


def test_judge_expects_a_full_body_on_the_body_views_but_not_for_footwear_or_accessories():
    cropped = {"framing": "cropped_legs"}
    assert _judge(cropped, size="M", category="Jackets").code == "MODEL_CROPPED"          # lead AI_FRONT
    assert _judge({**cropped, "garment": "sneakers"}, size="42", category="Footwear",
                  subcategory="Sneakers").action == "ok"
    assert _judge({**cropped, "garment": "cap"}, size="M", subcategory="Caps").action == "ok"
    # The three-quarter view is knee-up BY DESIGN (the prompt asks for it) and
    # leads the gallery — MID-000247's "half image" was exactly this. Not a defect.
    tq_media = [{"url": "https://x/front34.png", "view": "AI_FRONT_34", "mediaType": "IMAGE", "position": 1}]
    v = qg.judge(tq_media, gender="men", category="Jackets", pol=POL,
                 evidence=FakeEvidence({**GOOD, "framing": "cropped_legs"}))
    assert v.lead_view == "AI_FRONT_34" and v.action == "ok" and not v.soft
    # The close-up view is a detail by design.
    closeup_media = [{"url": "https://x/closeup.png", "view": "AI_CLOSEUP", "mediaType": "IMAGE", "position": 1}]
    pol = copy.deepcopy(POL)
    pol["quality_gate"]["lead_views"] = ["AI_CLOSEUP"]
    v = qg.judge(closeup_media, gender="men", category="Jackets", pol=pol,
                 evidence=FakeEvidence({**GOOD, "framing": "close_up"}))
    assert v.action == "ok"
    # And the fix is the lead view alone — one picture's framing, not the model's identity.
    assert qg.regen_views(GateVerdict("regen", "MODEL_CROPPED", ["x"], lead_view="AI_FRONT"), POL) == ["AI_FRONT"]


def test_no_model_is_not_also_a_build_problem():
    v = _decide({"model_present": False, "gender": "Unknown", "model_build": "unknown"},
                build="plus", size="XL")
    assert v.code == "IMAGE_QUALITY" and v.reasons[0].startswith("NO MODEL")


# --------------------------------------------------------------------------- #
# judge(): the exemptions, from the record
# --------------------------------------------------------------------------- #

class FakeEvidence:
    def __init__(self, raw):
        self.raw, self.last_error_kind, self.errors = raw, None, []

    def _fetch_images(self, urls):
        return ["part"]

    def _generate(self, **kw):
        return self.raw


MEDIA = [{"url": "https://x/front.png", "view": "AI_FRONT", "mediaType": "IMAGE", "position": 1}]


def _judge(raw_over: dict, **kw):
    return qg.judge(MEDIA, gender=kw.pop("gender", "men"),
                    pol=kw.pop("pol", POL_BUILD_BLOCKS),
                    evidence=FakeEvidence({**GOOD, **raw_over}), **kw)


def test_judge_holds_an_xl_garment_to_a_plus_build():
    v = _judge({"model_build": "slim"}, size="XL", category="Jackets")
    assert v.code == "BODY_SIZE_MISMATCH" and v.build_expected == "plus"


def test_judge_expects_no_build_for_kids_footwear_accessories_or_no_size():
    # The picture's garment word matches the category in each case, so the
    # category-vs-picture check stays quiet and only the build is under test.
    assert _judge({"model_build": "slim"}, size="XL", kids=True, category="Jackets").action == "ok"
    assert _judge({"model_build": "slim", "garment": "sneakers"}, size="XL",
                  category="Footwear", subcategory="Sneakers").action == "ok"
    assert _judge({"model_build": "slim", "garment": "cap"}, size="XL",
                  subcategory="Caps").action == "ok"
    assert _judge({"model_build": "slim"}, size=None, category="Jackets").action == "ok"
    assert _judge({"model_build": "slim"}, size="Unknown", category="Jackets").action == "ok"


def test_judge_does_not_hold_a_kids_product_on_the_models_gender():
    """BLM-001350 on the shadow: a child in a boys' hoodie read as 'women'. A
    flag, not a refusal — the defects still block, the gender does not."""
    v = _judge({"gender": "Women", "model_build": "slim"}, size="S", kids=True, category="Tops")
    assert v.action == "ok" and v.code is None
    assert any(s.startswith("Kids") and "not judged" in s for s in v.soft)
    v = _judge({"gender": "Women", "face_ok": False}, size="S", kids=True, category="Tops")
    assert v.code == "IMAGE_QUALITY"


def test_judge_reads_a_waist_through_the_gender_and_the_side():
    # A men's W32 is average: plus is one band off, a flag.
    v = _judge({"model_build": "plus", "garment": "jeans"}, size="W32",
               category="Bottoms", subcategory="Jeans")
    assert v.action == "ok" and any("one band off" in s for s in v.soft)
    # A women's W34 is plus: slim is two bands off.
    v = _judge({"model_build": "slim", "garment": "jeans", "gender": "Women"}, size="W34",
               gender="women", category="Bottoms", subcategory="Jeans")
    assert v.code == "BODY_SIZE_MISMATCH"
    # The same waist on a top is not a waist.
    assert _judge({"model_build": "slim", "garment": "shirt", "gender": "Women"}, size="W34",
                  gender="women", category="Tops").action == "ok"


# --------------------------------------------------------------------------- #
# Which views a refusal re-renders
# --------------------------------------------------------------------------- #

def test_gender_and_build_re_render_the_set_a_defect_only_the_lead():
    for code in ("MODEL_GENDER_MISMATCH", "BODY_SIZE_MISMATCH"):
        assert qg.regen_views(GateVerdict("regen", code, ["x"], lead_view="AI_FRONT"), POL) == ALL_VIEWS
    assert qg.regen_views(GateVerdict("regen", "IMAGE_QUALITY", ["BAD FACE"], lead_view="AI_FRONT_34"),
                          POL) == ["AI_FRONT_34"]
    assert qg.regen_views(GateVerdict("review", "CATEGORY_IMAGE_MISMATCH", ["x"], lead_view="AI_FRONT"),
                          POL) == []
    assert qg.regen_views(GateVerdict("ok"), POL) == []


# --------------------------------------------------------------------------- #
# The chain: one paid regeneration, then the gate again
# --------------------------------------------------------------------------- #

DSN = "postgresql://test"
PID = "00000000-0000-0000-0000-000000000001"


def _state(**over: Any) -> dict[str, Any]:
    base = {
        "loaded": {
            "record": {"gender": ["men"], "masterCategory": "Men", "category": "Jackets",
                       "subCategory": "Puffer Jackets", "size": "XL", "sizingGuide": None,
                       "updatedAt": None},
            "media": [{"url": "https://x/f.png", "view": "AI_FRONT", "mediaType": "IMAGE",
                       "position": 1}],
            "catalog": {}, "imagery_settings": None,
        },
        "title": "Test puffer", "tenant": "T", "sku": "T-1", "stage": "REVIEW",
        "review_status": "PENDING", "edit_url": None,
        "description_missing": False, "description_chars": 100,
        "care_label": 1, "unmatted": 0, "unmatted_views": [], "leftover_raw": 0,
        "cutout_views": [], "garment_photos": 2, "master": "Men", "mannequin": "Men Top",
        "size": "XL",
        "renders": 5, "render_rows": 5, "renders_missing": 0,
        "generation_status": "COMPLETE", "is_regenerating": False,
        "attributes_missing": [],
    }
    base.update(over)
    return base


BUILD_BAD = GateVerdict("regen", "BODY_SIZE_MISMATCH",
                        ["the model's build reads as slim but the garment is size XL (plus)"],
                        lead_view="AI_FRONT", build_seen="slim", build_expected="plus")
FACE_BAD = GateVerdict("regen", "IMAGE_QUALITY", ["BAD FACE — the model's face is AI-corrupted"],
                       lead_view="AI_FRONT")
OK = GateVerdict("ok", lead_view="AI_FRONT", build_seen="plus", build_expected="plus")
RENDER_OUT = "[1/1] p  done in 400s — matted 0, wrote [AI_FRONT_34, AI_BACK_34, AI_FRONT, AI_BACK, AI_CLOSEUP]"


@pytest.fixture
def wired(monkeypatch):
    calls: dict[str, Any] = {"approve": [], "imagery": [], "judge": 0}
    states: list[dict[str, Any]] = [_state()]
    verdicts: list[Any] = [OK, OK]
    imagery_out: dict[str, str] = {"text": RENDER_OUT}

    def fake_needs(dsn, pid):
        return states[0]

    def fake_run_step(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "backfill-imagery.ts":
            calls["imagery"].append(list(args))
            return True, imagery_out["text"], None
        if script == "verify-and-repair.ts":
            return True, "", {"ok": True, "applied": [], "failed": []}
        if script == "fix-selling-price.ts":
            return True, "", {"results": []}
        return True, "", None

    approve_outcome: dict[str, Any] = {"outcome": "would_approve", "problems": []}

    def fake_approve_check(vnyx_api, dsn, pid, *, apply, skip_bin, quiet, allow_stage=None, publish=True):
        calls["approve"].append({"apply": apply})
        return dict(approve_outcome)

    def fake_judge(media, **kw):
        i = min(calls["judge"], len(verdicts) - 1)
        calls["judge"] += 1
        return verdicts[i]

    # Pinned for the reason test_readiness_phase3.py's fixture spells out: the
    # option probe is a live HTTP call to VNYX_API_URL, and an unpinned one
    # makes these tests pass or fail on whether a server is running locally.
    monkeypatch.setattr(rp, "remote_supports", lambda *names: True)
    monkeypatch.setattr(rp, "needs", fake_needs)
    monkeypatch.setattr(rp, "run_step", fake_run_step)
    monkeypatch.setattr(rp, "approve_check", fake_approve_check)
    monkeypatch.setattr(qg, "judge", fake_judge)
    monkeypatch.setattr(pa, "judge", lambda media, **kw: GateVerdict("ok"))
    monkeypatch.setattr(cutouts, "judge", lambda snap, pol, **kw: cutouts.CutoutVerdict("skipped"))
    monkeypatch.setattr(
        rp.product_audit, "audit",
        lambda *a, **k: {"verified_after": True, "remaining": [], "counts": {"issues": 0}},
    )
    return {"calls": calls, "states": states, "verdicts": verdicts,
            "approve": approve_outcome, "imagery_out": imagery_out}


def _repair(apply: bool = False, approve: bool = False, skip_render: bool = False) -> dict[str, Any]:
    return rp.repair(DSN, PID, apply=apply, vnyx_api=Path("."), infer=False,
                     min_confidence=70, skip_render=skip_render, approve=approve,
                     skip_bin=True, quiet=True, silent=True)


def step(r: dict[str, Any], name: str) -> dict[str, Any]:
    return next(s for s in r["steps"] if s["step"] == name)


def test_a_passed_gate_never_regenerates(wired):
    r = _repair(apply=True)
    assert step(r, "regen")["ran"] is False
    assert "did not ask" in step(r, "regen")["why"]
    assert wired["calls"]["imagery"] == [] and wired["calls"]["judge"] == 1
    assert r["regeneration"]["attempted"] is False and r["unfixable"] is None


def test_a_wrong_build_re_renders_the_whole_set_and_passes_the_second_look(wired):
    wired["verdicts"][:] = [BUILD_BAD, OK]
    r = _repair(apply=True, approve=True)
    args = wired["calls"]["imagery"]
    assert len(args) == 1 and "--apply" in args[0]
    assert args[0][args[0].index("--views") + 1] == ",".join(ALL_VIEWS)
    note = step(r, "regen")["note"]
    assert note.startswith("regenerated the whole set (BODY_SIZE_MISMATCH") and "gate again: passed" in note
    assert r["regeneration"]["attempted"] and r["regeneration"]["code"] == "BODY_SIZE_MISMATCH"
    assert r["regeneration"]["views"] == ALL_VIEWS
    assert r["regeneration"]["after"]["action"] == "ok"
    assert r["gate"]["action"] == "ok"                       # the second verdict is the one that counts
    assert r["approval"]["outcome"] == "would_approve"
    assert wired["calls"]["approve"] == [{"apply": True}]    # the move was allowed
    assert r["generation"]["render_ran"] is True              # the canary knows the chain rendered
    assert wired["calls"]["judge"] == 2


def test_a_broken_face_re_renders_only_the_lead(wired):
    wired["verdicts"][:] = [FACE_BAD, OK]
    r = _repair(apply=True)
    args = wired["calls"]["imagery"][0]
    assert args[args.index("--views") + 1] == "AI_FRONT"
    assert step(r, "regen")["note"].startswith("regenerated AI_FRONT (IMAGE_QUALITY")


def test_refused_twice_holds_with_the_second_verdict_and_spends_the_budget_once(wired):
    wired["verdicts"][:] = [BUILD_BAD, BUILD_BAD]
    r = _repair(apply=True, approve=True)
    assert len(wired["calls"]["imagery"]) == 1                # one paid retry, never a loop
    assert "REFUSED AGAIN" in step(r, "regen")["note"]
    assert r["approval"]["outcome"] == "gate_blocked"
    assert r["approval"]["gate_code"] == "BODY_SIZE_MISMATCH"
    assert wired["calls"]["approve"] == [{"apply": False}]
    v = classify(r)
    assert (v.status, v.outcome) == ("HELD_FOR_HUMAN", "BODY_SIZE_MISMATCH")


def test_a_dry_run_says_what_it_would_re_render_and_keeps_the_first_verdict(wired):
    wired["verdicts"][:] = [BUILD_BAD, OK]
    r = _repair(apply=False)
    args = wired["calls"]["imagery"][0]
    assert "--apply" not in args and "--views" in args
    assert step(r, "regen")["note"].startswith("would regenerate the whole set")
    assert r["regeneration"]["after"] is None
    assert r["gate"]["code"] == "BODY_SIZE_MISMATCH"          # the gate's refusal is reported, as before
    assert r["approval"]["outcome"] == "gate_blocked"
    assert wired["calls"]["judge"] == 1


def test_the_regen_step_respects_no_render_and_the_budget(wired, monkeypatch):
    wired["verdicts"][:] = [BUILD_BAD, OK]
    r = _repair(apply=True, skip_render=True)
    assert step(r, "regen")["why"] == "--no-render" and wired["calls"]["imagery"] == []

    spent = copy.deepcopy(POL)
    spent["readiness"]["max_regenerations_per_run"] = 0
    monkeypatch.setattr(rp, "policy", lambda: spent)
    wired["calls"]["judge"] = 0                              # the gate refuses again
    r = _repair(apply=True)
    assert "max_regenerations_per_run" in step(r, "regen")["why"]
    assert wired["calls"]["imagery"] == []
    assert r["approval"]["outcome"] == "gate_blocked"


def test_a_review_verdict_or_a_refusal_with_no_lead_regenerates_nothing(wired):
    # `review` is a person's decision (the category or the picture is wrong),
    # not a render defect: the gate did not ask for a re-render.
    wired["verdicts"][:] = [GateVerdict("review", "CATEGORY_IMAGE_MISMATCH", ["gown on a vest"],
                                        lead_view="AI_FRONT")]
    r = _repair(apply=True)
    assert "did not ask" in step(r, "regen")["why"]
    assert r["approval"]["outcome"] == "gate_blocked"
    # A refusal that names no lead view has nothing to point the render at.
    wired["verdicts"][:] = [GateVerdict("regen", "IMAGE_QUALITY", ["NO MODEL"])]
    wired["calls"]["judge"] = 0
    r = _repair(apply=True)
    assert "nothing to regenerate" in step(r, "regen")["why"]


def test_a_second_look_the_provider_failed_lands_on_vision_unavailable(wired):
    wired["verdicts"][:] = [BUILD_BAD, GateVerdict("review", "VISION_UNAVAILABLE", ["429"],
                                                    unavailable=True)]
    r = _repair(apply=True, approve=True)
    assert step(r, "regen")["ok"] is False
    assert "gate" in r["vision_unavailable"]
    assert r["approval"]["outcome"] == "gate_unavailable"


# --------------------------------------------------------------------------- #
# The photo audit asks too: a render the gate never sees (MID-000253)
# --------------------------------------------------------------------------- #
#
# The gate judges the lead. MID-000253's lead AI_FRONT passed; the AI_FRONT_34
# beside it had the model's knees smeared to white, and only the photo audit,
# which sees every render, said so — as a soft flag nobody acted on. Now that
# verdict is RENDER_DEFECT and the regen step re-renders the one view.

PHOTO_BAD = GateVerdict("regen", "RENDER_DEFECT",
                        ["RENDER DEFECT — AI_FRONT_34 render: AI artifact on legs"],
                        bad_views=["AI_FRONT_34"], lead_view="FRONT")
PHOTO_OK = GateVerdict("ok", lead_view="FRONT")


def _photo_sequence(monkeypatch, *verdicts: Any) -> dict[str, int]:
    seq, calls = list(verdicts), {"n": 0}

    def fake(media, **kw):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return seq[i]

    monkeypatch.setattr(pa, "judge", fake)
    return calls


def _order(r: dict[str, Any]) -> list[str]:
    return [s["step"] for s in r["steps"]]


def test_a_render_the_photo_audit_calls_defective_is_re_rendered_alone_and_judged_again(wired, monkeypatch):
    calls = _photo_sequence(monkeypatch, PHOTO_BAD, PHOTO_OK)
    r = _repair(apply=True, approve=True)
    # The photo audit runs BEFORE the regen, so its render defects can feed it.
    assert _order(r).index("gate") < _order(r).index("photos") < _order(r).index("regen")
    args = wired["calls"]["imagery"]
    assert len(args) == 1 and args[0][args[0].index("--views") + 1] == "AI_FRONT_34"   # that view only
    note = step(r, "regen")["note"]
    assert note.startswith("regenerated AI_FRONT_34 (RENDER_DEFECT: RENDER DEFECT — AI_FRONT_34 render: AI artifact on legs)")
    assert "gate again: passed" in note and "photo audit again: passed" in note
    assert r["regeneration"]["asked_by"] == ["photos"] and r["regeneration"]["code"] == "RENDER_DEFECT"
    assert r["regeneration"]["photos_before"]["code"] == "RENDER_DEFECT"
    assert r["regeneration"]["photos_after"]["action"] == "ok"
    assert r["photos"]["action"] == "ok"                      # the second verdict is the one that counts
    assert r["approval"]["outcome"] == "would_approve"
    assert calls["n"] == 2 and wired["calls"]["judge"] == 2   # both checks looked again, once


def test_a_render_defect_refused_twice_holds_under_its_own_code(wired, monkeypatch):
    _photo_sequence(monkeypatch, PHOTO_BAD, PHOTO_BAD)
    r = _repair(apply=True, approve=True)
    assert len(wired["calls"]["imagery"]) == 1                # one paid retry, never a loop
    note = step(r, "regen")["note"]
    assert "photo audit REFUSED AGAIN" in note and "the budget is spent" in note
    assert r["approval"]["outcome"] == "gate_blocked" and r["approval"]["gate_code"] == "RENDER_DEFECT"
    assert wired["calls"]["approve"] == [{"apply": False}]
    assert classify(r).outcome == "RENDER_DEFECT"


def test_a_dry_run_names_the_defective_view_it_would_re_render(wired, monkeypatch):
    _photo_sequence(monkeypatch, PHOTO_BAD)
    r = _repair(apply=False)
    args = wired["calls"]["imagery"][0]
    assert "--apply" not in args and args[args.index("--views") + 1] == "AI_FRONT_34"
    assert step(r, "regen")["note"].startswith("would regenerate AI_FRONT_34 (RENDER_DEFECT")
    assert r["photos"]["code"] == "RENDER_DEFECT" and r["regeneration"]["photos_after"] is None
    assert r["approval"]["outcome"] == "gate_blocked"


def test_the_gate_s_lead_and_the_photo_audit_s_view_go_together_in_the_renderer_s_order(wired, monkeypatch):
    wired["verdicts"][:] = [FACE_BAD, OK]
    _photo_sequence(monkeypatch,
                    GateVerdict("regen", "RENDER_DEFECT", ["RENDER DEFECT — AI_BACK_34 render: clutter"],
                                bad_views=["AI_BACK_34"]),
                    PHOTO_OK)
    r = _repair(apply=True)
    args = wired["calls"]["imagery"][0]
    assert args[args.index("--views") + 1] == "AI_BACK_34,AI_FRONT"
    assert r["regeneration"]["asked_by"] == ["gate", "photos"]
    assert r["regeneration"]["code"] == "IMAGE_QUALITY"       # the gate's code names the fix
    assert step(r, "regen")["note"].startswith("regenerated AI_BACK_34, AI_FRONT (IMAGE_QUALITY: BAD FACE")


CUTOUT_BAD = GateVerdict("ok", soft=["CUTOUT DEFECT — FRONT cut-out: collar cut away by background removal"],
                         bad_cutouts=["FRONT"], lead_view="FRONT")
BG_OUT = "--- summary ---\n  cut-outs written : 2\n  failed           : 0\n"


def _record_bg_removal(monkeypatch, wired) -> list[list[str]]:
    """Wrap the fixture's fake run_step so the re-cut's arguments are kept."""
    calls: list[list[str]] = []
    inner = rp.run_step

    def outer(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "backfill-bg-removal.ts":
            calls.append(list(args))
            return True, BG_OUT, None
        return inner(vnyx_api, script, args, timeout_s=timeout_s, quiet=quiet, results_name=results_name)

    monkeypatch.setattr(rp, "run_step", outer)
    return calls


def test_mid_000569_a_collar_the_mask_ate_is_re_cut_and_the_audit_looks_again(wired, monkeypatch):
    calls = _photo_sequence(monkeypatch, CUTOUT_BAD, PHOTO_OK)
    bg = _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=True, approve=True)
    assert _order(r).index("photos") < _order(r).index("rematte") < _order(r).index("regen")
    assert len(bg) == 1 and "--replace" in bg[0] and "--provider" in bg[0]
    note = step(r, "rematte")["note"]
    assert note.startswith("re-cut FRONT from the raw archive (CUTOUT DEFECT — FRONT cut-out: collar cut away")
    assert "photo audit again: passed" in note
    assert r["rematte"]["attempted"] and r["rematte"]["views"] == ["FRONT"] and r["rematte"]["written"] == 2
    assert r["rematte"]["after"]["action"] == "ok" and r["photos"]["action"] == "ok"
    assert "did not ask" in step(r, "regen")["why"] and wired["calls"]["imagery"] == []
    assert r["approval"]["outcome"] == "would_approve" and calls["n"] == 2


def test_a_cutout_still_spoiled_after_the_re_cut_stays_a_flag_while_the_policy_is_soft(wired, monkeypatch):
    _photo_sequence(monkeypatch, CUTOUT_BAD, CUTOUT_BAD)
    _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=True, approve=True)
    note = step(r, "rematte")["note"]
    assert "STILL flagged after the re-cut" in note and "soft — readiness.cutouts.hold" in note
    assert r["approval"]["outcome"] == "would_approve"          # a flag, not a hold


def test_a_dry_run_names_the_cut_out_it_would_re_cut(wired, monkeypatch):
    _photo_sequence(monkeypatch, CUTOUT_BAD)
    bg = _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=False)
    assert bg == []                                              # nothing runs
    assert step(r, "rematte")["note"].startswith("would re-cut FRONT from the raw archive (CUTOUT DEFECT")
    assert r["rematte"]["after"] is None


def test_a_whole_gallery_skips_the_re_cut(wired):
    r = _repair(apply=True)
    assert step(r, "rematte")["ran"] is False and "every cut-out whole" in step(r, "rematte")["why"]


# --------------------------------------------------------------------------- #
# Items 6 and 8 of docs/PICTURE-CHECK-FIXES.md, where the chain sends them
# --------------------------------------------------------------------------- #

# MID-000521's own words, on all four of its cut-outs (§1.1). The re-matte for
# this one has to ask a DIFFERENT segmenter: cloth-seg is a clothing parser,
# the podium is directly beneath the clothing, so re-running it returns the
# same podium and the same verification passes it again.
STAND_LEFT = GateVerdict("ok", soft=["CUTOUT DEFECT — FRONT cut-out: stand visible at bottom"],
                         bad_cutouts=["FRONT"], lead_view="FRONT")
BG_KEPT = "--- summary ---\n  cut-outs written : 0\n  originals kept   : 1\n  failed           : 0\n"


def test_mid_000521_a_stand_left_in_is_re_cut_by_a_different_segmenter(
        wired, monkeypatch):
    """The mechanism, against a policy that funds the mask strategies.

    Both of them are paid calls and the shipped default no longer lists any
    (19 Sep 2026, `imagery.cutout.strategies`), so this asks the question of a
    config that does — otherwise it would be testing the price, not the
    behaviour. `test_a_stand_is_left_alone_when_nothing_can_remove_it` covers
    what actually ships."""
    # `cutout.config()` reads app.config.policy directly, not the chain's, so
    # the module it actually consults is the one to patch.
    from app.imaging import cutout as _cutout

    funded = {**_cutout.config(None),
              "leftover_strategies": ["gemini-mask", "openai-mask"]}
    monkeypatch.setattr(_cutout, "config", lambda pol=None: funded)
    _photo_sequence(monkeypatch, STAND_LEFT, PHOTO_OK)
    bg = _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=True, approve=True)
    args = bg[0]
    assert args[args.index("--bg-strategies") + 1] == "gemini-mask,openai-mask"
    assert "--keep-better" in args and "--replace" in args
    note = step(r, "rematte")["note"]
    assert "with gemini-mask, openai-mask — the same segmenter would return the same cut" in note
    assert r["rematte"]["strategies"] == ["gemini-mask", "openai-mask"]


def test_a_stand_is_left_alone_when_nothing_can_remove_it(wired, monkeypatch):
    """WHAT SHIPS. No paid strategy is funded, so the stand stays and the step
    says why — rather than re-cutting with cloth-seg, which is the segmenter
    that left the stand in and would hand back the same picture."""
    _photo_sequence(monkeypatch, STAND_LEFT, PHOTO_OK)
    bg = _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=True, approve=True)
    assert bg == [], "no segmenter call, paid or free"
    note = step(r, "rematte")["note"]
    assert "left the existing cut-outs alone" in note
    assert "leftover_strategies" in note
    assert r["rematte"]["skipped"] == "no leftover strategy configured"


def test_a_collar_the_mask_ate_keeps_the_default_chain(wired, monkeypatch):
    """A second segmenter is not the answer to a missing garment part."""
    _photo_sequence(monkeypatch, CUTOUT_BAD, PHOTO_OK)
    bg = _record_bg_removal(monkeypatch, wired)
    r = _repair(apply=True, approve=True)
    assert "--bg-strategies" not in bg[0] and "--keep-better" in bg[0]
    assert r["rematte"]["strategies"] is None


def test_kil_001625_a_refused_replacement_says_the_original_was_kept(wired, monkeypatch):
    """§2.1: the damaged replacement is not stored, and the row says so.

    The cut-out on the page is the one that was already there, so the audit
    flags the same defect again — and without this sentence that reads as a
    re-cut which did not help, and the product comes back next run to be cut
    exactly the same way.
    """
    _photo_sequence(monkeypatch, CUTOUT_BAD, CUTOUT_BAD)
    calls: list[list[str]] = []
    inner = rp.run_step

    def outer(vnyx_api, script, args, *, timeout_s, quiet, results_name=None):
        if script == "backfill-bg-removal.ts":
            calls.append(list(args))
            return True, BG_KEPT, None
        return inner(vnyx_api, script, args, timeout_s=timeout_s, quiet=quiet,
                     results_name=results_name)

    monkeypatch.setattr(rp, "run_step", outer)
    r = _repair(apply=True, approve=True)
    note = step(r, "rematte")["note"]
    assert r["rematte"]["kept"] == 1 and r["rematte"]["written"] == 0
    assert "ORIGINAL WAS KEPT" in note
    assert "a further re-cut will not clear it" in note
    assert "STILL flagged after the re-cut" in note
    assert r["approval"]["outcome"] == "would_approve"          # soft, as before


def test_render_defects_switched_to_soft_never_re_render(wired, monkeypatch):
    soft = copy.deepcopy(POL)
    soft["photo_audit"]["gallery"]["render_defects"] = "soft"
    monkeypatch.setattr(rp, "policy", lambda: soft)
    _photo_sequence(monkeypatch, GateVerdict("ok", soft=["IMAGE DEFECT — AI_FRONT_34 render: artifact"],
                                             bad_views=["AI_FRONT_34"]))
    r = _repair(apply=True)
    assert "did not ask" in step(r, "regen")["why"] and wired["calls"]["imagery"] == []
    assert r["approval"]["outcome"] == "would_approve"


# --------------------------------------------------------------------------- #
# Unfixable: no garment photograph, every model declines
# --------------------------------------------------------------------------- #

def test_no_garment_photograph_is_named_from_the_final_audit(wired, monkeypatch):
    monkeypatch.setattr(
        rp.product_audit, "audit",
        lambda *a, **k: {"verified_after": False,
                         "remaining": [{"rule_id": "IMG.003", "fields": ["images"]}],
                         "counts": {"issues": 1}},
    )
    r = _repair(apply=True)
    assert r["unfixable"]["code"] == "NO_GARMENT_PHOTO"
    assert "reshoot" in r["unfixable"]["reason"]


def test_a_print_every_model_declines_is_named_from_the_render_step(wired):
    wired["states"][0] = _state(renders=0, render_rows=0, renders_missing=5)
    wired["verdicts"][:] = [GateVerdict("skipped", reasons=["no on-model render to judge"])]
    wired["imagery_out"]["text"] = (
        "[1/1] p  PARTIAL in 30s — matted 0, wrote [nothing], could not produce "
        "[AI_FRONT_34, AI_BACK_34, AI_FRONT, AI_BACK, AI_CLOSEUP]\n"
        "[1/1]    note: AI_FRONT: IMAGE_OTHER — the model declined to render this kit"
    )
    r = _repair(apply=True)
    assert step(r, "render")["note"].endswith("(the image model declined the print)")
    assert r["render_report"]["written"] == [] and r["render_report"]["refused"] is True
    assert r["unfixable"]["code"] == "RENDER_REFUSED"


def test_a_partial_set_that_was_not_declined_is_not_unfixable(wired):
    wired["states"][0] = _state(renders=3, render_rows=3, renders_missing=2)
    wired["imagery_out"]["text"] = ("[1/1] p  PARTIAL in 30s — matted 0, wrote [AI_FRONT], "
                                    "could not produce [AI_BACK]")
    r = _repair(apply=True)
    assert r["render_report"]["written"] == ["AI_FRONT"] and r["render_report"]["failed"] == ["AI_BACK"]
    assert r["render_report"]["refused"] is False
    assert r["unfixable"] is None


def test_refusal_words_and_finish_reasons_are_recognised():
    assert rp._refusal_text("note: IMAGE_SAFETY — blocked")
    assert rp._refusal_text("note: the model declined this garment")
    assert not rp._refusal_text("note: MIXED MODELS — two personalities")
    assert not rp._refusal_text("note: ReadError: socket died")


def test_kids_products_are_rendered_unless_policy_says_hold(wired, monkeypatch):
    wired["states"][0] = _state(master="Kids", mannequin="Kids", renders=0, render_rows=0,
                                renders_missing=5)
    wired["verdicts"][:] = [GateVerdict("skipped")]
    r = _repair(apply=True)
    assert step(r, "render")["ran"] is True                  # decision 4: generate
    hold = copy.deepcopy(POL)
    hold["readiness"]["kids_renders"] = "hold"
    monkeypatch.setattr(rp, "policy", lambda: hold)
    wired["calls"]["imagery"].clear()
    wired["calls"]["judge"] = 0
    r = _repair(apply=True)
    assert step(r, "render")["ran"] is False and "Kids" in step(r, "render")["why"]


# --------------------------------------------------------------------------- #
# The runner's rejection guards, and the remote option
# --------------------------------------------------------------------------- #

HELD = Verdict("HELD_FOR_HUMAN", "IMG.003", "IMG.003", False, False)
RESULT = {"unfixable": {"code": "NO_GARMENT_PHOTO", "reason": "NO_GARMENT_PHOTO — no garment photograph"}}


def test_the_runner_rejects_only_a_held_product_under_apply_with_the_judge_present():
    assert runner.unfixable_rejection(RESULT, HELD, [], apply=True, pol=POL) == (
        "NO_GARMENT_PHOTO", "NO_GARMENT_PHOTO — no garment photograph")
    assert runner.unfixable_rejection(RESULT, HELD, [], apply=False, pol=POL) is None
    assert runner.unfixable_rejection(RESULT, HELD, ["gate"], apply=True, pol=POL) is None
    verified = Verdict("VERIFIED", "READY_NOT_MOVED", "ok", False, False)
    assert runner.unfixable_rejection(RESULT, verified, [], apply=True, pol=POL) is None
    assert runner.unfixable_rejection({"unfixable": None}, HELD, [], apply=True, pol=POL) is None


def test_the_runner_holds_instead_when_policy_says_hold():
    hold = copy.deepcopy(POL)
    hold["readiness"]["unfixable"] = "hold"
    assert runner.unfixable_rejection(RESULT, HELD, [], apply=True, pol=hold) is None


def test_run_remote_sends_views_as_a_typed_list(monkeypatch):
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
    rp.run_remote("backfill-imagery.ts",
                  ["--db", DSN, "--product", PID, "--apply", "--views", "AI_FRONT,AI_BACK"],
                  timeout_s=10, quiet=True)
    assert sent["step"] == "render" and sent["options"] == {"views": ["AI_FRONT", "AI_BACK"]}
