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


def test_every_live_cutout_of_a_view_is_sent_not_only_the_first():
    """MID-000569 had a flat-lay FRONT cut-out (whole) and a booth FRONT cut-out
    (collar gone) — the audit saw the first and called every cut-out whole."""
    booth = {"url": "https://r2/front-booth-cut.png", "view": "FRONT", "processing": "BG_REMOVED",
             "mediaType": "IMAGE", "position": 8}
    images, photos = pa.select_images([*MEDIA, booth], POL)
    urls = [im["url"] for im in images]
    assert photos == 2 and urls[:2] == ["https://r2/front-cut.png", "https://r2/back-cut.png"]
    assert "https://r2/front-booth-cut.png" in urls and urls.index("https://r2/front-booth-cut.png") == 2
    assert images[2]["kind"] == "photo" and "nothing cut away" in images[2]["expect"]
    # And a defect on it names FRONT for the re-cut.
    raw = _raw()
    raw["images"][2] = {"index": 3, "ok": True, "issue": "", "missing_parts": ["collar"]}
    v = pa.decide(raw, images=images, grade_severity="none", grade_label="A", pol=POL)
    assert v.bad_cutouts == ["FRONT"] and v.soft == ["CUTOUT DEFECT — FRONT cut-out: collar missing"]


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


def test_a_cutout_defect_is_re_cut_and_a_render_defect_re_rendered_each_on_its_own_view():
    """Three kinds of picture, three fates: a cut-out the mask spoiled is re-cut
    (a flag meanwhile, the cut-out policy being soft); a render is made again;
    a raw photograph can only be flagged."""
    v = _decide(_raw(bad=[(1, "background not fully removed"), (4, "two models in frame")]))
    assert v.action == "regen" and v.code == "RENDER_DEFECT" and v.blocks
    assert v.soft == ["CUTOUT DEFECT — FRONT cut-out: background not fully removed"]
    assert v.bad_cutouts == ["FRONT"] and pa.rematte_views(v, POL) == ["FRONT"]
    assert v.reasons == ["RENDER DEFECT — AI_BACK render: two models in frame"]
    assert v.bad_views == ["AI_BACK"] and pa.regen_views(v, POL) == ["AI_BACK"]
    assert pa.summary(v).startswith("REFUSED — RENDER DEFECT — AI_BACK render")


RAW_ONLY = [m for m in MEDIA if m["processing"] != "BG_REMOVED"]


def test_gallery_block_holds_a_photograph_defect_with_image_defect():
    pol = {**POL, "photo_audit": {"gallery": {"block": True}}}
    v = _decide(_raw(bad=[(1, "hanger still in frame")]), pol=pol, media=RAW_ONLY)
    assert v.action == "review" and v.code == "IMAGE_DEFECT"
    assert v.reasons == ["IMAGE DEFECT — FRONT photo: hanger still in frame"]
    assert v.bad_views == [] and v.bad_cutouts == []
    assert pa.regen_views(v, POL) == [] and pa.rematte_views(v, POL) == []


# MID-000569 (Midtex, production, 17 Sep 2026): the sweater's ribbed neckband
# was cut away by the background removal on both FRONT cut-outs — plain on the
# edit screen, invisible to the canvas and backdrop measurements.

def test_mid_000569_a_collar_the_mask_ate_is_a_cutout_defect_and_that_view_is_re_cut():
    v = _decide(_raw(bad=[(1, "collar cut away by background removal")]))
    assert v.action == "ok" and v.code is None                       # soft — readiness.cutouts.hold
    assert v.soft == ["CUTOUT DEFECT — FRONT cut-out: collar cut away by background removal"]
    assert v.bad_cutouts == ["FRONT"] and pa.rematte_views(v, POL) == ["FRONT"]
    assert v.bad_views == [] and pa.regen_views(v, POL) == []


def test_a_part_the_model_lists_as_missing_counts_even_when_it_called_the_cutout_ok():
    """The generic 'matches its expectation' let the neckband through; the
    part-by-part list is the answer that is trusted. Renders never carry one."""
    raw = _raw()
    raw["images"][0] = {"index": 1, "ok": True, "issue": "", "missing_parts": ["collar", "neckband"]}
    raw["images"][2] = {"index": 3, "ok": True, "issue": "", "missing_parts": ["sleeve"]}   # a render: ignored
    v = _decide(raw)
    assert v.action == "ok" and v.bad_views == []
    assert v.soft == ["CUTOUT DEFECT — FRONT cut-out: collar, neckband missing"]
    assert v.bad_cutouts == ["FRONT"]
    raw["images"][0] = {"index": 1, "ok": False, "issue": "stand visible", "missing_parts": ["hem"]}
    assert _decide(raw).soft == ["CUTOUT DEFECT — FRONT cut-out: stand visible; hem missing"]


def test_a_render_refused_on_its_own_answer_is_not_refused_twice_for_the_odd_model():
    v = _decide(_raw7(bad=[(7, "shows a different model")], same_model=False, odd=[7]), media=MEDIA_FIVE)
    assert v.reasons == ["RENDER DEFECT — AI_CLOSEUP render: shows a different model"]
    assert v.bad_views == ["AI_CLOSEUP"]


def test_a_cutout_defect_holds_once_the_cutout_policy_is_block():
    pol = {**POL, "readiness": {"cutouts": {"hold": "block"}}}
    v = _decide(_raw(bad=[(1, "collar cut away"), (2, "stand visible at bottom")]), pol=pol)
    assert v.action == "review" and v.code == "CUTOUT_DEFECT" and v.blocks
    assert v.reasons == ["CUTOUT DEFECT — FRONT cut-out: collar cut away",
                         "CUTOUT DEFECT — BACK cut-out: stand visible at bottom"]
    assert v.bad_cutouts == ["FRONT", "BACK"] and pa.rematte_views(v, pol) == ["FRONT", "BACK"]


def test_cutout_defects_can_be_a_hold_or_the_old_soft_flag_by_policy():
    hold = {**POL, "photo_audit": {"gallery": {"cutout_defects": "hold"}}}
    v = _decide(_raw(bad=[(1, "collar cut away")]), pol=hold)
    assert v.action == "review" and v.code == "IMAGE_DEFECT"
    assert v.bad_cutouts == [] and pa.rematte_views(v, hold) == []
    soft = {**POL, "photo_audit": {"gallery": {"cutout_defects": "soft"}}}
    v = _decide(_raw(bad=[(1, "collar cut away")]), pol=soft)
    assert v.action == "ok" and v.soft == ["IMAGE DEFECT — FRONT cut-out: collar cut away"]
    assert v.bad_cutouts == [] and pa.rematte_views(v, soft) == []


def test_rematte_views_never_re_cuts_on_an_audit_that_could_not_run():
    from app.imaging.quality_gate import GateVerdict

    assert pa.rematte_views(GateVerdict("review", "VISION_UNAVAILABLE", ["429"], unavailable=True,
                                        bad_cutouts=["FRONT"]), POL) == []
    assert pa.rematte_views(GateVerdict("ok"), POL) == []


# MID-000253 (Midtex, production, 17 Sep 2026): the lead AI_FRONT passed the
# gate; the AI_FRONT_34 beside it had the model's knees smeared to white.
MEDIA_FIVE = [m for m in MEDIA if m["view"] != "AI_FRONT_34"] + [
    {"url": "https://r2/ai-front-34.jpg", "view": "AI_FRONT_34", "processing": "GENERATED",
     "mediaType": "IMAGE", "position": 6},
    {"url": "https://r2/ai-back-34.jpg", "view": "AI_BACK_34", "processing": "GENERATED",
     "mediaType": "IMAGE", "position": 7},
]


def _raw7(bad=(), wear="none", confidence=0.9, same_model=True, odd=()):
    images = [{"index": i, "ok": i not in dict(bad), "issue": dict(bad).get(i, "")} for i in range(1, 8)]
    return {"wear": wear, "wear_confidence": confidence, "defects": [], "images": images,
            "same_model": same_model, "odd_renders": list(odd)}


# MID-000569 again: the close-up re-rendered beside four renders of a blonde
# woman came back with a dark-haired one. Only a look across the set sees it.

def test_a_render_showing_a_different_person_than_the_rest_is_a_render_defect_of_that_view():
    v = _decide(_raw7(same_model=False, odd=[7]), media=MEDIA_FIVE)           # 7 = AI_CLOSEUP
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.reasons == ["RENDER DEFECT — AI_CLOSEUP render: a different model than the other renders"]
    assert v.bad_views == ["AI_CLOSEUP"] and pa.regen_views(v, POL) == ["AI_CLOSEUP"]


def test_same_model_needs_a_majority_to_compare_with_else_it_is_a_soft_note():
    # No odd render named, or as many odd as not: nobody to re-render against.
    v = _decide(_raw7(same_model=False), media=MEDIA_FIVE)
    assert v.action == "ok" and any("may not all show the same model" in s for s in v.soft)
    v = _decide(_raw7(same_model=False, odd=[3, 4, 5]), media=MEDIA_FIVE)     # 3 of 5
    assert v.action == "ok" and v.bad_views == []
    # Two renders only: no majority — the question is not asked of the answer.
    two = [m for m in MEDIA_FIVE if m["view"] not in ("AI_BACK", "AI_FRONT_34", "AI_BACK_34")]
    v = _decide(_raw7(same_model=False, odd=[4]), media=two)
    assert v.action == "ok" and v.bad_views == []
    # An index that is a photograph, or out of range, is ignored.
    v = _decide(_raw7(same_model=False, odd=[1, 99, "x"]), media=MEDIA_FIVE)
    assert v.action == "ok" and v.bad_views == []


def test_mid_000253_the_smeared_knees_on_a_view_the_gate_never_sees_re_render_that_view_alone():
    images, _ = pa.select_images(MEDIA_FIVE, POL)
    assert [im["view"] for im in images] == ["FRONT", "BACK", "AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"]
    v = _decide(_raw7(bad=[(5, "AI artifact on legs")]), media=MEDIA_FIVE)
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.reasons == ["RENDER DEFECT — AI_FRONT_34 render: AI artifact on legs"]
    assert v.bad_views == ["AI_FRONT_34"]
    # That view only — never the set: the model was judged right on the lead.
    assert pa.regen_views(v, POL) == ["AI_FRONT_34"]


def test_several_defective_renders_are_re_rendered_in_the_renderer_s_order():
    v = _decide(_raw7(bad=[(6, "duplicated sleeve"), (3, "text on the wall")]), media=MEDIA_FIVE)
    assert sorted(v.bad_views) == ["AI_BACK_34", "AI_FRONT"]
    assert pa.regen_views(v, POL) == ["AI_BACK_34", "AI_FRONT"]      # imagery.all_views order


def test_a_hold_for_a_person_names_the_verdict_and_the_render_still_goes():
    """The grade contradicted AND a smeared render: the person's question names
    the code, the re-render happens anyway — regen_views reads bad_views."""
    v = _decide(_raw7(wear="major", bad=[(5, "AI artifact on legs")]), media=MEDIA_FIVE, grade="none")
    assert v.action == "review" and v.code == "GRADE_SUSPECT"
    assert v.reasons[0].startswith("GRADE SUSPECT") and v.reasons[1].startswith("RENDER DEFECT — AI_FRONT_34")
    assert pa.regen_views(v, POL) == ["AI_FRONT_34"]


def test_render_defects_can_be_a_hold_or_the_old_soft_flag_by_policy():
    hold = {**POL, "photo_audit": {"gallery": {"render_defects": "hold"}}}
    v = _decide(_raw7(bad=[(5, "AI artifact on legs")]), media=MEDIA_FIVE, pol=hold)
    assert v.action == "review" and v.code == "IMAGE_DEFECT"
    assert v.reasons == ["IMAGE DEFECT — AI_FRONT_34 render: AI artifact on legs"]
    assert v.bad_views == [] and pa.regen_views(v, hold) == []

    soft = {**POL, "photo_audit": {"gallery": {"render_defects": "soft"}}}
    v = _decide(_raw7(bad=[(5, "AI artifact on legs")]), media=MEDIA_FIVE, pol=soft)
    assert v.action == "ok" and v.soft == ["IMAGE DEFECT — AI_FRONT_34 render: AI artifact on legs"]
    assert v.bad_views == [] and pa.regen_views(v, soft) == []


def test_regen_views_never_re_renders_on_an_audit_that_could_not_run():
    from app.imaging.quality_gate import GateVerdict

    v = GateVerdict("review", "VISION_UNAVAILABLE", ["429"], unavailable=True, bad_views=["AI_FRONT"])
    assert pa.regen_views(v, POL) == []
    assert pa.regen_views(GateVerdict("ok"), POL) == []


def test_wear_outranks_the_gallery_when_both_hold():
    pol = {**POL, "photo_audit": {"gallery": {"block": True, "cutout_defects": "hold"}}}
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
