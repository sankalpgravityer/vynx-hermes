"""app/imaging/photo_audit.py — which pictures go, what the answer becomes, and
the call's failure modes. No network, no key: `decide()` is tested on the raw
answer, `judge()` against a fake evidence object.
"""
from __future__ import annotations

from app.imaging import photo_audit as pa

POL = {"llm": {"model_fast": "test-model"}, "photo_audit": {}}

MEDIA = [
    {"url": "https://r2/front-raw.jpg", "view": "FRONT", "processing": "RAW", "mediaType": "IMAGE", "position": 0},
    {"url": "https://r2/front-cut.png", "view": "FRONT", "processing": "BG_REMOVED", "mediaType": "IMAGE", "position": 0},
    {"url": "https://r2/back-cut.png", "view": "BACK", "processing": "BG_REMOVED", "mediaType": "IMAGE", "position": 1},
    {"url": "https://r2/label.jpg", "view": "LABEL", "processing": "RAW", "mediaType": "IMAGE", "position": 2},
    {"url": "https://r2/ai-front.jpg", "view": "AI_FRONT", "processing": "GENERATED", "mediaType": "IMAGE", "position": 3},
    {"url": "https://r2/ai-back.jpg", "view": "AI_BACK", "processing": "GENERATED", "mediaType": "IMAGE", "position": 4},
    {"url": "https://r2/closeup.jpg", "view": "AI_CLOSEUP", "processing": "GENERATED", "mediaType": "IMAGE", "position": 5},
    {"url": "https://r2/gone.jpg", "view": "AI_FRONT_34", "processing": "GENERATED", "mediaType": "IMAGE", "deletedAt": "2026-01-01"},
]


def _raw(wear="none", confidence=0.9, defects=(), bad=()):
    images = [{"index": i, "ok": i not in dict(bad), "issue": dict(bad).get(i, "")} for i in range(1, 7)]
    return {"wear": wear, "wear_confidence": confidence, "defects": list(defects), "images": images}


# ------------------------------------------------------------ select_images

def test_photographs_first_cutouts_preferred_labels_and_deleted_rows_left_out():
    images, photos = pa.select_images(MEDIA, POL)
    assert photos == 2
    assert [im["url"] for im in images] == [
        "https://r2/front-cut.png", "https://r2/back-cut.png",       # the wear read's pictures
        "https://r2/ai-front.jpg", "https://r2/ai-back.jpg", "https://r2/closeup.jpg",
    ]
    assert images[0]["kind"] == "photo" and "background removed" in images[0]["expect"]
    assert images[2]["kind"] == "render" and "from the front" in images[2]["expect"]
    text = pa.prompt_for(images, photos)
    assert "image 1 — GARMENT PHOTOGRAPH (FRONT)" in text and "images 1–2 only" in text


def test_a_raw_photo_stands_in_when_there_is_no_cutout():
    media = [m for m in MEDIA if m["processing"] != "BG_REMOVED"]
    images, photos = pa.select_images(media, POL)
    assert photos == 1 and images[0]["url"] == "https://r2/front-raw.jpg"
    assert "as taken" in images[0]["expect"]


def test_gallery_off_sends_only_the_photographs():
    pol = {**POL, "photo_audit": {"gallery": {"enabled": False}}}
    images, photos = pa.select_images(MEDIA, pol)
    assert photos == 2 and len(images) == 2
    assert "no garment photograph" not in pa.prompt_for(images, photos)


# ------------------------------------------------------------------ decide

def _decide(raw, grade="none", pol=POL, media=MEDIA):
    images, _ = pa.select_images(media, pol)
    return pa.decide(raw, images=images, grade_severity=grade, grade_label="A", pol=pol)


def test_photos_two_steps_worse_than_the_grade_hold_with_the_defects():
    v = _decide(_raw("moderate", defects=["stain on front hem", "pilling on sleeves"]), grade="none")
    assert v.action == "review" and v.code == "GRADE_SUSPECT" and v.blocks
    assert "moderate wear" in v.reasons[0] and "stain on front hem" in v.reasons[0]
    assert "grade A says none" in v.reasons[0]
    assert v.lead_url == "https://r2/front-cut.png" and v.confidence == 0.9


def test_one_step_apart_is_not_a_finding():
    v = _decide(_raw("minor"), grade="none")
    assert v.action == "ok" and v.reasons == [] and v.soft == []


def test_photos_better_than_the_grade_are_a_soft_flag():
    v = _decide(_raw("none"), grade="moderate")
    assert v.action == "ok" and any("conservative" in s for s in v.soft)


def test_unknown_or_unsure_wear_never_holds():
    assert _decide(_raw("unknown"), grade="none").action == "ok"
    v = _decide(_raw("major", confidence=0.3), grade="none")
    assert v.action == "ok" and any("unclear" in s for s in v.soft)


def test_no_grade_to_compare_with_still_records_what_was_seen():
    v = _decide(_raw("major", defects=["hole at left cuff"]), grade=None)
    assert v.action == "ok" and v.soft == ["photos show major wear: hole at left cuff"]


def test_wear_block_off_turns_the_hold_into_a_soft_flag():
    pol = {**POL, "photo_audit": {"wear": {"block": False}}}
    v = _decide(_raw("major"), grade="none", pol=pol)
    assert v.action == "ok" and v.soft[0].startswith("GRADE SUSPECT")


def test_gallery_defects_are_soft_by_default_and_name_the_slot():
    v = _decide(_raw(bad=[(1, "background not fully removed"), (4, "two models in frame")]))
    assert v.action == "ok"
    assert v.soft == ["IMAGE DEFECT — FRONT cut-out: background not fully removed",
                      "IMAGE DEFECT — AI_BACK render: two models in frame"]


def test_gallery_block_holds_with_image_defect():
    pol = {**POL, "photo_audit": {"gallery": {"block": True}}}
    v = _decide(_raw(bad=[(3, "no person in the render")]), pol=pol)
    assert v.action == "review" and v.code == "IMAGE_DEFECT"
    assert v.reasons == ["IMAGE DEFECT — AI_FRONT render: no person in the render"]


def test_wear_outranks_the_gallery_when_both_hold():
    pol = {**POL, "photo_audit": {"gallery": {"block": True}}}
    v = _decide(_raw("major", bad=[(2, "blurred")]), grade="none", pol=pol)
    assert v.code == "GRADE_SUSPECT" and len(v.reasons) == 2


def test_out_of_range_or_malformed_image_entries_are_ignored():
    raw = _raw()
    raw["images"] += [{"index": 99, "ok": False, "issue": "x"}, {"index": "?", "ok": False}, "junk"]
    assert _decide(raw).action == "ok"


def test_summary_reads_as_a_hold_not_an_indecision():
    v = _decide(_raw("major", defects=["hole"]), grade="none")
    assert pa.summary(v).startswith("HELD — GRADE SUSPECT")
    assert "wear seen: major" in pa.summary(v)
    ok = _decide(_raw("minor"), grade="minor")
    assert pa.summary(ok) == "passed (wear seen: minor)"


# ------------------------------------------------------------------- judge

class _Fake:
    def __init__(self, answer, *, drop=0, api_error=False):
        self.answer, self.drop, self.api_error = answer, drop, api_error
        self.calls = 0
        self.errors: list[str] = []
        self.last_error_kind = None

    def _fetch_images(self, urls):
        return ["part"] * (len(urls) - self.drop)

    def _generate(self, **kw):
        self.calls += 1
        if self.api_error:
            self.errors.append("429")
            self.last_error_kind = "api"
            return None
        return self.answer


def test_judge_sends_every_selected_image_once_and_decides():
    ev = _Fake(_raw("major", defects=["tear"]))
    v = pa.judge(MEDIA, grade_severity="none", grade_label="A", pol=POL, evidence=ev)
    assert ev.calls == 1 and v.code == "GRADE_SUSPECT" and v.cached is False


def test_judge_skips_when_disabled_or_nothing_to_look_at():
    off = {**POL, "photo_audit": {"enabled": False}}
    assert pa.judge(MEDIA, grade_severity="none", pol=off, evidence=_Fake(_raw())).action == "skipped"
    v = pa.judge([m for m in MEDIA if m["view"] == "LABEL"], grade_severity="none", pol=POL, evidence=_Fake(_raw()))
    assert v.action == "skipped" and "no garment photograph" in v.reasons[0]


def test_a_missing_download_judges_nothing_rather_than_the_wrong_picture():
    ev = _Fake(_raw("major"), drop=1)
    v = pa.judge(MEDIA, grade_severity="none", pol=POL, evidence=ev)
    assert ev.calls == 0 and v.action == "skipped" and "could not be downloaded" in v.reasons[0]


def test_provider_failure_blocks_only_when_required():
    ev = _Fake(None, api_error=True)
    v = pa.judge(MEDIA, grade_severity="none", pol=POL, evidence=ev)
    assert v.action == "skipped" and not v.blocks and "could not run" in v.reasons[0]
    strict = {**POL, "photo_audit": {"required": True}}
    v = pa.judge(MEDIA, grade_severity="none", pol=strict, evidence=_Fake(None, api_error=True))
    assert v.action == "review" and v.code == "VISION_UNAVAILABLE" and v.unavailable


def test_judge_answers_from_the_cache_on_the_same_pictures(monkeypatch, tmp_path):
    from app.llm import cache

    monkeypatch.delenv("HERMES_VISION_CACHE", raising=False)
    monkeypatch.setenv("HERMES_VISION_CACHE_DIR", str(tmp_path))
    cache.reset()
    ev = _Fake(_raw("major"))
    first = pa.judge(MEDIA, grade_severity="none", pol=POL, evidence=ev)
    second = pa.judge(MEDIA, grade_severity="major", pol=POL, evidence=ev)   # same pictures, other grade
    assert ev.calls == 1
    assert first.code == "GRADE_SUSPECT" and second.action == "ok" and second.cached
    assert pa.summary(second).endswith("(cached)")
