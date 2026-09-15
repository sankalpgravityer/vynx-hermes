"""app/imaging/quality_gate.py — the decision, cold, and the call's failure modes.

`decide()` is tested against the seven-field answer directly; `judge()` against
a fake evidence object, so the lead selection, the unavailable/bad-answer split
and the gender resolution are exercised with no network and no key.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.imaging import quality_gate as qg

POL = {"llm": {"model_fast": "test-model"}, "quality_gate": {}}

GOOD = {"model_present": True, "face_ok": True, "gender": "Men", "lead_ok": True,
        "body_coherent": True, "body_issue": "", "view": "front", "confidence": 0.9}


def _decide(raw_over: dict, *, gender="men", accessory=False, pol=POL):
    return qg.decide({**GOOD, **raw_over}, product_gender=gender,
                     accessory=accessory, pol=pol)


# ----------------------------------------------------------------- decide()

def test_a_clean_render_passes():
    v = _decide({})
    assert v.action == "ok" and not v.blocks and v.code is None
    assert v.gender_seen == "men" and v.confidence == 0.9


def test_no_model_blocks_as_image_quality():
    v = _decide({"model_present": False, "gender": "Unknown"})
    assert v.blocks and v.action == "regen" and v.code == "IMAGE_QUALITY"
    assert v.reasons[0].startswith("NO MODEL")


def test_no_model_is_soft_on_an_accessory():
    v = _decide({"model_present": False, "gender": "Unknown"}, accessory=True)
    assert v.action == "ok"
    assert any("accessory" in s for s in v.soft)


def test_model_gender_contradicting_the_record_blocks():
    v = _decide({"gender": "Women"}, gender="men")
    assert v.action == "regen" and v.code == "MODEL_GENDER_MISMATCH"
    assert "women" in v.reasons[0] and "men" in v.reasons[0]


def test_unknown_model_gender_is_a_soft_flag_not_a_block():
    """The auditor's calibration: GENDERLESS? is a hint. Only a CONTRADICTION
    blocks — an androgynous model on a men's shirt is not a wrong render."""
    v = _decide({"gender": "Unknown"}, gender="men")
    assert v.action == "ok"
    assert any("gender unclear" in s for s in v.soft)


def test_no_product_gender_means_no_gender_check():
    v = _decide({"gender": "Women"}, gender=None)
    assert v.action == "ok"


def test_bad_face_blocks():
    v = _decide({"face_ok": False})
    assert v.action == "regen" and v.code == "IMAGE_QUALITY"
    assert "BAD FACE" in v.reasons[0]


def test_broken_body_names_the_break():
    v = _decide({"body_coherent": False, "body_issue": "feet distorted and blurred"})
    assert v.action == "regen"
    assert "feet distorted" in v.reasons[0]


def test_back_view_and_bad_lead_are_soft():
    v = _decide({"view": "back", "lead_ok": False})
    assert v.action == "ok"
    assert any("BAD LEAD" in s for s in v.soft)
    assert any("back" in s for s in v.soft)


def test_gender_check_runs_before_face_and_body():
    """Order matters for the recorded cause: a wrong-gender render with a bad
    face is a gender problem first — regenerating for the right gender fixes
    both, and the code should say which fix."""
    v = _decide({"gender": "Women", "face_ok": False}, gender="men")
    assert v.code == "MODEL_GENDER_MISMATCH"


def test_block_on_can_switch_a_check_to_soft():
    pol = {**POL, "quality_gate": {"block_on": ["no_model", "broken_body"]}}
    v = _decide({"face_ok": False}, pol=pol)
    assert v.action == "ok"


def test_summary_is_one_line():
    v = _decide({"body_coherent": False, "body_issue": "floating leg"})
    v.lead_view = "AI_FRONT"
    line = v.summary()
    assert line.startswith("REFUSED") and "floating leg" in line and "AI_FRONT" in line


# ------------------------------------------------------------------ judge()

class FakeEvidence:
    def __init__(self, raw=None, kind=None, fetch_ok=True):
        self.raw, self.last_error_kind, self.fetch_ok = raw, kind, fetch_ok
        self.errors = ["test-model: ClientError: 429 RESOURCE_EXHAUSTED"]
        self.asked: list[str] = []

    def _fetch_images(self, urls):
        self.asked.extend(urls)
        return ["part"] if self.fetch_ok else []

    def _generate(self, **kw):
        return self.raw


MEDIA = [
    {"url": "https://x/label.jpg", "view": "LABEL", "mediaType": "IMAGE", "position": 0},
    {"url": "https://x/front34.png", "view": "AI_FRONT_34", "mediaType": "IMAGE", "position": 1},
    {"url": "https://x/front.png", "view": "AI_FRONT", "mediaType": "IMAGE", "position": 3},
    {"url": "https://x/old-front.png", "view": "AI_FRONT", "mediaType": "IMAGE",
     "position": 2, "isCurrent": False},
]


def test_judge_picks_the_live_ai_front_over_the_three_quarter_and_the_label():
    ev = FakeEvidence(GOOD)
    v = qg.judge(MEDIA, gender=["men"], pol=POL, evidence=ev)
    assert v.lead_view == "AI_FRONT" and v.lead_url == "https://x/front.png"
    assert ev.asked == ["https://x/front.png"]
    assert v.action == "ok"


def test_judge_falls_back_to_the_three_quarter_view():
    media = [m for m in MEDIA if m["view"] != "AI_FRONT"]
    v = qg.judge(media, gender="men", pol=POL, evidence=FakeEvidence(GOOD))
    assert v.lead_view == "AI_FRONT_34"


def test_judge_skips_when_there_is_no_render_to_judge():
    media = [{"url": "https://x/f.jpg", "view": "FRONT", "mediaType": "IMAGE"},
             {"url": "https://x/l.jpg", "view": "LABEL", "mediaType": "IMAGE"}]
    ev = FakeEvidence(GOOD)
    v = qg.judge(media, gender="men", pol=POL, evidence=ev)
    assert v.action == "skipped" and not v.blocks
    assert ev.asked == []  # no call spent


def test_judge_provider_failure_is_unavailable_not_a_refusal():
    v = qg.judge(MEDIA, gender="men", pol=POL, evidence=FakeEvidence(None, "api"))
    assert v.action == "review" and v.blocks
    assert v.unavailable is True and v.code == "VISION_UNAVAILABLE"
    assert "429" in v.reasons[0]


def test_judge_bad_answer_is_review_but_not_unavailable():
    v = qg.judge(MEDIA, gender="men", pol=POL, evidence=FakeEvidence(None, "answer"))
    assert v.action == "review" and v.blocks
    assert v.unavailable is False and v.code == "IMAGE_QUALITY"


def test_judge_unreachable_image_is_unavailable():
    v = qg.judge(MEDIA, gender="men", pol=POL, evidence=FakeEvidence(GOOD, fetch_ok=False))
    assert v.unavailable and v.code == "VISION_UNAVAILABLE"
    assert "downloaded" in v.reasons[0]


def test_judge_honours_the_policy_switch():
    pol = {**POL, "quality_gate": {"enabled": False}}
    v = qg.judge(MEDIA, gender="men", pol=pol, evidence=FakeEvidence(GOOD))
    assert v.action == "skipped"


def test_judge_resolves_every_gender_shape_the_record_uses():
    """The same resolver GENDER.001 uses, so a JSON-string list and a bare word
    are the same answer — and a contradiction is caught in either shape."""
    for stored in ('["women"]', ["women"], "Women", "women"):
        v = qg.judge(MEDIA, gender=stored, pol=POL, evidence=FakeEvidence(GOOD))
        assert v.code == "MODEL_GENDER_MISMATCH", stored


def test_judge_with_no_key_blocks_when_required(monkeypatch):
    from app import config

    monkeypatch.setattr(config, "settings", lambda: SimpleNamespace(gemini_api_key=""))
    v = qg.judge(MEDIA, gender="men", pol=POL)
    assert v.unavailable and "GEMINI_API_KEY" in v.reasons[0]

    pol = {**POL, "quality_gate": {"required": False}}
    v = qg.judge(MEDIA, gender="men", pol=pol)
    assert v.action == "skipped"


def test_accessory_terms_match_subcategory_or_category():
    assert qg.is_accessory("Caps", None, pol=POL)
    assert qg.is_accessory(None, "Bags", pol=POL)
    assert qg.is_accessory("Baseball cap", pol=POL)
    assert not qg.is_accessory("T-Shirt", "Men", pol=POL)
