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

def _decide(raw, grade="none", pol=POL, media=MEDIA, category=None, subcategory=None):
    images, _ = pa.select_images(media, pol)
    return pa.decide(raw, images=images, grade_severity=grade, grade_label="A", pol=pol,
                     category=category, subcategory=subcategory)


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


# The leftovers, measured. MID-000591 FRONT (18 Sep 2026) reported a hanger on
# a cut-out with no hanger in it, because the prompt asked whether one was
# there and never how much of it. §1.2 of docs/PICTURE-CHECK-FIXES.md.

def test_mid_000591_a_hanger_hook_at_the_neckline_is_slight_and_never_a_defect():
    raw = _raw()
    raw["images"][0] = {"index": 1, "ok": False, "issue": "hanger visible", "missing_parts": [],
                        "leftovers": [{"part": "hanger", "extent": "slight"}]}
    v = _decide(raw)
    assert v.action == "ok" and v.soft == [] and v.reasons == []
    assert v.bad_cutouts == [] and pa.rematte_views(v, POL) == []
    # The measurement outranks the issue line: "no leftovers at all" and
    # "hanger visible" in the same answer is the model contradicting itself,
    # and only one half of it was asked how much.
    raw["images"][0]["leftovers"] = []
    assert _decide(raw).soft == []
    # What is struck out is the leftover, not the rest of the sentence.
    raw["images"][0] = {"index": 1, "ok": False, "issue": "hook at collar, collar cut away",
                        "missing_parts": [], "leftovers": [{"part": "hook", "extent": "slight"}]}
    assert _decide(raw).soft == ["CUTOUT DEFECT — FRONT cut-out: collar cut away"]


def test_mid_000521_a_stand_under_the_garment_is_clear_and_still_a_cutout_defect():
    """The case the threshold has to keep: all four cut-outs stand on a podium,
    and the vision check was the only thing that ever caught it (§1.1)."""
    raw = _raw()
    raw["images"][0] = {"index": 1, "ok": True, "issue": "", "missing_parts": [],
                        "leftovers": [{"part": "stand", "extent": "clear"}]}
    v = _decide(raw)
    assert v.soft == ["CUTOUT DEFECT — FRONT cut-out: stand visible"]
    assert v.bad_cutouts == ["FRONT"] and pa.rematte_views(v, POL) == ["FRONT"]
    # Named in the issue line already: said once, not twice.
    raw["images"][1] = {"index": 2, "ok": False, "issue": "hanger and stand visible", "missing_parts": [],
                        "leftovers": [{"part": "hanger", "extent": "clear"},
                                      {"part": "stand", "extent": "clear"}]}
    assert _decide(raw).soft[1] == "CUTOUT DEFECT — BACK cut-out: hanger and stand visible"
    # A slight one beside a clear one does not water the clear one down.
    raw["images"][1]["leftovers"][0]["extent"] = "slight"
    assert _decide(raw).bad_cutouts == ["FRONT", "BACK"]


def test_leftovers_are_read_on_a_cutout_and_nowhere_else():
    """A render's duplicated hand and a photograph shot on its hanger are other
    questions — the threshold must not quietly answer them too."""
    raw = _raw()
    raw["images"][3] = {"index": 4, "ok": False, "issue": "the model's hand is duplicated",
                        "missing_parts": [], "leftovers": [], "face_visible": True}
    assert _decide(raw).reasons == ["RENDER DEFECT — AI_BACK render: the model's hand is duplicated"]
    pol = {**POL, "photo_audit": {"gallery": {"block": True}}}
    raw = _raw()
    raw["images"][0] = {"index": 1, "ok": False, "issue": "hanger still in frame",
                        "missing_parts": [], "leftovers": []}
    v = _decide(raw, pol=pol, media=RAW_ONLY)
    assert v.reasons == ["IMAGE DEFECT — FRONT photo: hanger still in frame"]


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


def _raw7(bad=(), wear="none", confidence=0.9, same_model=True, odd=(), faces=None):
    """`faces` = the 1-based indexes whose render shows a face, as the model
    answers it. None leaves `face_visible` out entirely — an answer cached
    under the schema before 18 Sep 2026."""
    images = [{"index": i, "ok": i not in dict(bad), "issue": dict(bad).get(i, "")} for i in range(1, 8)]
    if faces is not None:
        for e in images:
            e["face_visible"] = e["index"] in faces
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


# Faces only, 18 Sep 2026. MEDIA_FIVE's renders are images 3–7: AI_FRONT,
# AI_BACK, AI_FRONT_34, AI_BACK_34, AI_CLOSEUP. Three of those can show a face;
# the two back views cannot, and every false positive this question ever made
# was one of them (BOA-006151 → [4, 6], BOA-006153 → [7]). §1.4.

def test_same_model_is_judged_across_the_renders_with_a_face_and_no_others():
    # MID-000569's close-up: a face, a different woman, still caught.
    v = _decide(_raw7(same_model=False, odd=[7], faces=[3, 5, 7]), media=MEDIA_FIVE)
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.bad_views == ["AI_CLOSEUP"] and pa.regen_views(v, POL) == ["AI_CLOSEUP"]
    # Two of the three faces called odd is no majority — and the two faceless
    # renders do not pad the population into one (2 of 5 would have flagged).
    v = _decide(_raw7(same_model=False, odd=[5, 7], faces=[3, 5, 7]), media=MEDIA_FIVE)
    assert v.action == "ok" and v.bad_views == []
    assert any("may not all show the same model" in s for s in v.soft)
    # Only two faces in the whole set: nobody to hold a majority. Under the old
    # rule this was 1 odd of 5 renders and a re-render of AI_FRONT_34.
    v = _decide(_raw7(same_model=False, odd=[5], faces=[3, 5]), media=MEDIA_FIVE)
    assert v.action == "ok" and v.bad_views == [] and v.soft == []


def test_boa_006151_odd_renders_may_not_name_a_render_with_no_face():
    v = _decide(_raw7(same_model=False, odd=[4, 6], faces=[3, 5, 7]), media=MEDIA_FIVE)
    assert v.action == "ok" and v.reasons == [] and v.bad_views == []
    # Not even softly: the note would be the line on the page this exists to
    # clear, and the comparison behind it was made on hair and build.
    assert v.soft == [] and pa.regen_views(v, POL) == []
    # No face anywhere in the set — the question is simply not answered.
    v = _decide(_raw7(same_model=False, odd=[4], faces=[]), media=MEDIA_FIVE)
    assert v.action == "ok" and v.bad_views == [] and v.soft == []


def test_a_cached_answer_with_neither_new_field_still_decides():
    """An answer stored before the 18 Sep schema has no `leftovers` and no
    `face_visible`. It is judged the way it was written: no leftovers to
    threshold, the face unknown, so every render is compared as before."""
    raw = _raw7(bad=[(1, "hanger visible")], same_model=False, odd=[7])
    assert all("leftovers" not in e and "face_visible" not in e for e in raw["images"])
    v = _decide(raw, media=MEDIA_FIVE)
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.soft == ["CUTOUT DEFECT — FRONT cut-out: hanger visible"]
    assert v.reasons == ["RENDER DEFECT — AI_CLOSEUP render: a different model than the other renders"]
    assert v.bad_cutouts == ["FRONT"] and v.bad_views == ["AI_CLOSEUP"]


# 21 Sep 2026. Two defects the audit passed on the Midtex dossier because it
# had never been asked about them: the studio behind the model (MID-000475
# AI_FRONT_34 — a backdrop rig, ceiling beams, a clamp and shelving, `ok: true`)
# and a body that comes apart inside the frame (MID-000382 AI_BACK — the lower
# leg dissolves into the backdrop while the shoe stays). Both are measured on a
# scale, like the leftovers, so the threshold is a value and not an adjective.

def _raw_scene(scene=None, body=None, on=(5,), ok=True):
    """`scene` / `body_flaw` set on the images in `on` (1-based), which are
    renders in MEDIA_FIVE unless a photograph index is passed deliberately."""
    raw = _raw7()
    for e in raw["images"]:
        e["scene"] = "plain"
        e["body_flaw"] = "none"
        if e["index"] in on:
            e["ok"] = ok
            if scene:
                e["scene"] = scene
            if body:
                e["body_flaw"] = body
    return raw


def test_mid_000475_a_studio_behind_the_model_is_a_render_defect_of_that_view():
    v = _decide(_raw_scene(scene="cluttered"), media=MEDIA_FIVE)          # 5 = AI_FRONT_34
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.reasons == ["RENDER DEFECT — AI_FRONT_34 render: background is not clean"]
    assert v.bad_views == ["AI_FRONT_34"] and pa.regen_views(v, POL) == ["AI_FRONT_34"]


def test_mid_000382_a_leg_that_dissolves_inside_the_frame_is_a_render_defect():
    v = _decide(_raw_scene(body="clear", on=(4,)), media=MEDIA_FIVE)      # 4 = AI_BACK
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.reasons == ["RENDER DEFECT — AI_BACK render: the body is not whole"]
    assert v.bad_views == ["AI_BACK"]


def test_the_scale_is_the_threshold_and_only_its_top_is_a_defect():
    """Every render has a soft shadow and a soft edge somewhere. `minor` and
    `slight` are those, and nothing follows from them — the discipline the
    leftovers got after MID-000591, applied before the same mistake is made."""
    assert _decide(_raw_scene(scene="minor"), media=MEDIA_FIVE).action == "ok"
    assert _decide(_raw_scene(body="slight"), media=MEDIA_FIVE).action == "ok"
    both = _decide(_raw_scene(scene="minor", body="slight"), media=MEDIA_FIVE)
    assert both.action == "ok" and both.bad_views == [] and both.soft == []


def test_neither_question_is_asked_of_a_garment_photograph():
    """Images 1–2 of MEDIA_FIVE are cut-outs. A cut-out sits on a white field
    with no person in it; a model answering `cluttered` or `clear` there is
    answering the wrong question, and the cut-out has `leftovers` for the
    things that really are behind it."""
    v = _decide(_raw_scene(scene="cluttered", body="clear", on=(1, 2)), media=MEDIA_FIVE)
    assert v.action == "ok" and v.bad_views == [] and v.bad_cutouts == []


def test_the_model_s_own_words_are_not_repeated_by_the_measurement():
    """The model that answers `cluttered` usually also writes the issue line.
    One row, one sentence — not 'studio equipment visible; background is not
    clean'."""
    raw = _raw_scene(scene="cluttered", ok=False)
    raw["images"][4]["issue"] = "studio equipment visible behind model"
    v = _decide(raw, media=MEDIA_FIVE)
    assert v.reasons == ["RENDER DEFECT — AI_FRONT_34 render: studio equipment visible behind model"]
    # A complaint about something else keeps both halves, in one row.
    raw = _raw_scene(body="clear", ok=False)
    raw["images"][4]["issue"] = "garment colour wrong"
    v = _decide(raw, media=MEDIA_FIVE)
    assert v.reasons == ["RENDER DEFECT — AI_FRONT_34 render: garment colour wrong; the body is not whole"]


# FRAMING, per view (21 Sep 2026). The user's standard, which is also what
# nanobanana.py already asks the renderer for: AI_FRONT and AI_BACK show the
# whole figure; the three-quarter views are knee-up, so a cut THROUGH the lower
# leg is the defect (MID-000425's AI_FRONT_34 ends mid-shin, no feet); the
# close-up is a detail. A bottom needs its hem, footwear and accessories are
# framed around the product and exempt.

# MEDIA_FIVE in order: FRONT, BACK (photographs), then AI_FRONT, AI_BACK,
# AI_FRONT_34, AI_BACK_34, AI_CLOSEUP. Everything not under test is framed the
# way its view wants, so a failure names the image the test is about.
_FRAMED_RIGHT = {1: "detail", 2: "detail", 3: "feet", 4: "feet",
                 5: "thigh", 6: "thigh", 7: "waist_up"}


def _raw_framing(where, on=(5,)):
    raw = _raw7()
    for e in raw["images"]:
        e["framing"] = where if e["index"] in on else _FRAMED_RIGHT[e["index"]]
    return raw


def test_a_three_quarter_view_cut_through_the_lower_leg_is_a_render_defect():
    v = _decide(_raw_framing("lower_leg"), media=MEDIA_FIVE)              # 5 = AI_FRONT_34
    assert v.action == "regen" and v.code == "RENDER_DEFECT"
    assert v.reasons == ["RENDER DEFECT — AI_FRONT_34 render: ends at lower leg — "
                         "the legs are cut through the middle — crop at the knee or show the feet"]
    assert v.bad_views == ["AI_FRONT_34"]
    # Knee-up and thigh-up are what that view is FOR, and full body is not a
    # thing missing from it.
    for ok_where in ("knee", "thigh", "waist_up", "feet"):
        assert _decide(_raw_framing(ok_where), media=MEDIA_FIVE).action == "ok"


def test_the_front_and_back_must_show_the_whole_figure():
    for where in ("lower_leg", "knee", "thigh", "waist_up"):
        v = _decide(_raw_framing(where, on=(4,)), media=MEDIA_FIVE)        # 4 = AI_BACK
        assert v.action == "regen" and v.bad_views == ["AI_BACK"]
        assert "head to feet" in v.reasons[0] and f"ends at {where.replace('_', ' ')}" in v.reasons[0]
    assert _decide(_raw_framing("feet", on=(4,)), media=MEDIA_FIVE).action == "ok"


def test_the_close_up_is_never_judged_on_framing():
    for where in ("lower_leg", "detail", "waist_up"):
        assert _decide(_raw_framing(where, on=(7,)), media=MEDIA_FIVE).action == "ok"   # 7 = AI_CLOSEUP


def test_a_bottom_needs_its_hem_in_frame_on_the_three_quarter_views_too():
    """Jeans cropped at the thigh are a picture of half the product.

    Which names mean "bottom" is the tenant's, read from `sizing.sides` — the
    same table SIZE.014 and the gate's build check read. Without one nothing is
    a bottom and the view table alone applies, which is the honest answer for a
    caller that passed no category at all."""
    sided = {**POL, "sizing": {"sides": {"bottom": ["trouser", "jean", "short"],
                                         "upper": ["shirt", "tee", "polo"]}}}
    v = _decide(_raw_framing("thigh"), media=MEDIA_FIVE, pol=sided, category="Jeans & Trousers")
    assert v.action == "regen" and "head to feet" in v.reasons[0]
    # The same answer on an upper is the view working as designed.
    assert _decide(_raw_framing("thigh"), media=MEDIA_FIVE, pol=sided,
                   category="T-Shirts & Polos").action == "ok"
    # And with no sides table, a bottom cannot be recognised: no defect invented.
    assert _decide(_raw_framing("thigh"), media=MEDIA_FIVE, category="Jeans & Trousers").action == "ok"


def test_footwear_and_accessories_are_framed_around_the_product_and_exempt():
    for cat, sub in (("Shoes", "Sneakers"), ("Accessories", "Cap")):
        v = _decide(_raw_framing("lower_leg", on=(4, 5)), media=MEDIA_FIVE,
                    category=cat, subcategory=sub)
        assert v.action == "ok" and v.bad_views == []


def test_framing_can_be_recorded_without_re_rendering_anything():
    soft = {**POL, "photo_audit": {"gallery": {"framing": "soft"}}}
    v = _decide(_raw_framing("lower_leg"), media=MEDIA_FIVE, pol=soft)
    assert v.action == "ok" and v.bad_views == []
    assert v.soft == ["FRAMING — AI_FRONT_34 render ends at lower leg; the legs are cut "
                      "through the middle — crop at the knee or show the feet"]
    off = {**POL, "photo_audit": {"gallery": {"framing": "off"}}}
    v = _decide(_raw_framing("lower_leg"), media=MEDIA_FIVE, pol=off)
    assert v.action == "ok" and v.soft == []


# THE CLOSE-UP'S SIDE IS THE GARMENT'S BUSINESS (21 Sep 2026). KLE-000030, a
# pair of trousers, was refused for "AI_CLOSEUP render: shows back view instead
# of front" — but the seat and rear pockets ARE the detail shot for a bottom,
# and the renderer has a `closeup_back` for it. The demand belongs to uppers.

SIDED = {**POL, "sizing": {"sides": {"bottom": ["trouser", "jean", "short"],
                                     "upper": ["shirt", "tee", "polo", "jacket"]}}}


def _closeup_expect(category, pol=SIDED):
    images, _ = pa.select_images(MEDIA_FIVE, pol,
                                 pa.garment_side(category, None, pol))
    return next(im["expect"] for im in images if im["view"] == "AI_CLOSEUP")


def test_an_upper_close_up_must_show_the_front():
    expect = _closeup_expect("T-Shirts & Polos")
    assert "FRONT of this garment" in expect
    assert "BACK does not belong in this slot" in expect


def test_a_bottom_close_up_may_show_either_side():
    expect = _closeup_expect("Jeans & Trousers")
    assert "EITHER side is correct" in expect
    assert "BACK does not belong" not in expect
    # The waistband and the seat are both named, so the model is told what the
    # shot IS rather than only what it is not.
    assert "seat" in expect and "waistband" in expect


def test_an_unknown_category_is_not_treated_as_an_upper():
    """None is not 'upper'. A check that invented a defect for every product
    whose category this tenant names differently is the bug, not the fix."""
    assert "EITHER side is correct" in _closeup_expect(None)
    assert "EITHER side is correct" in _closeup_expect("Something Unmapped")
    # And with no sides table at all, nothing can be classified.
    assert "EITHER side is correct" in _closeup_expect("T-Shirts & Polos", pol=POL)


def test_the_other_views_keep_their_side_whatever_the_garment():
    """Only the close-up is ambiguous. A back view filed as AI_FRONT is wrong
    for trousers exactly as it is for a shirt."""
    for category in ("Jeans & Trousers", "T-Shirts & Polos"):
        images, _ = pa.select_images(MEDIA_FIVE, SIDED,
                                     pa.garment_side(category, None, SIDED))
        by_view = {im["view"]: im["expect"] for im in images}
        assert "facing the camera" in by_view["AI_FRONT"]
        assert "turned away" in by_view["AI_BACK"]


def test_an_answer_cached_before_the_scene_questions_is_judged_as_it_was():
    raw = _raw7()
    assert all("scene" not in e and "body_flaw" not in e for e in raw["images"])
    assert _decide(raw, media=MEDIA_FIVE).action == "ok"


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
