"""The product's image sequence (3 Oct 2026): which renders it gets, how many,
and the gallery order — app/rules/shot_sequence.py and its use in the imagery
rules and the repair chain.

The sequences here are the tenants' real ones (production, read only):

    BOAS default        originals first, then 3/4 front, 3/4 back, full front,
                        full back, close-up, full front AGAIN — six renders
    BOAS Men > Shoes    close-up, back close-up, detail macro
    Magic Body default  close-up, back close-up, 3/4 front, 3/4 back
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import policy  # noqa: E402
from app.models import ImagerySettings, MediaAsset, ProductSnapshot  # noqa: E402
from app.rules import imagery, shot_sequence as ss  # noqa: E402

ORIGINALS = [{"slot": s, "enabled": True} for s in (
    "original_decision_front", "original_decision_back",
    "original_photobooth_front", "original_photobooth_back")]
BOAS_DEFAULT = ORIGINALS + [{"shot": s, "count": 1, "enabled": True} for s in (
    "three_quarter_front", "three_quarter_back", "full_front", "full_back", "closeup",
    "full_front")] + [{"slot": "labels", "enabled": True}]
BOAS_SHOES = [{"shot": s, "count": 1, "enabled": True}
              for s in ("closeup", "closeup_back", "detail_macro")] + ORIGINALS + [
    {"slot": "labels", "enabled": True}]
MAGIC_BODY = [{"shot": s, "count": 1, "enabled": True}
              for s in ("closeup", "closeup_back", "three_quarter_front", "three_quarter_back")]


@pytest.fixture(scope="module")
def pol() -> dict:
    return policy()


def overrides() -> list[ss.Override]:
    return ss.override_candidates([
        (BOAS_SHOES, "Shoes", "Men", None),
        (MAGIC_BODY, "Shoes", "Women", None),
    ])


# --------------------------------------------------------------------------- #
# Which sequence
# --------------------------------------------------------------------------- #

def test_the_category_override_beats_the_tenant_default():
    r = ss.resolve(overrides=overrides(), default_shots=BOAS_DEFAULT,
                   master_category="Men", category="Shoes", sub_category="Sneakers")
    assert (r.source, r.matched_category) == ("category", "Shoes")
    assert [i.label for i in r.expanded] == ["closeup", "closeup_back", "detail_macro"]


def test_a_known_ancestor_that_differs_rules_an_override_out():
    # "Shoes < Women" must not dress a men's shoe, and vice versa.
    r = ss.resolve(overrides=overrides(), default_shots=BOAS_DEFAULT,
                   master_category="Women", category="Shoes")
    assert [i.shot for i in r.expanded][:2] == ["closeup", "closeup_back"]
    assert "detail_macro" not in [i.shot for i in r.expanded]


def test_names_match_loosely_and_plurals_do_not_matter():
    r = ss.resolve(overrides=overrides(), default_shots=None,
                   master_category="Men's", category="shoe")
    assert r.source == "category"


def test_no_override_is_the_tenant_default_and_counts_repeats():
    r = ss.resolve(overrides=overrides(), default_shots=BOAS_DEFAULT,
                   master_category="Men", category="Jeans")
    assert r.source == "tenant-default"
    assert [i.label for i in r.expanded] == [
        "three_quarter_front", "three_quarter_back", "full_front", "full_back", "closeup",
        "full_front#2"]
    second = r.expanded[-1]
    assert (second.position, second.variation, second.variation_count) == (5, 2, 2)


def test_nothing_configured_is_the_built_in_five_and_only_it_obeys_the_close_up_switch():
    r = ss.resolve(overrides=[], default_shots=None, is_close_up_enabled=False)
    assert r.source == "built-in"
    assert "closeup" not in [i.shot for i in r.expanded]
    # A configured sequence IS the instruction: the old switch does not subtract.
    r = ss.resolve(overrides=[], default_shots=BOAS_DEFAULT, is_close_up_enabled=False)
    assert "closeup" in [i.shot for i in r.expanded]


def test_a_sequence_that_renders_nothing_is_no_configuration():
    assert ss.parse_sequence([{"shot": "closeup", "enabled": False}]) is None
    r = ss.resolve(overrides=[], default_shots=[{"slot": "labels"}])
    assert r.source == "built-in"


def test_what_vnyx_api_sends_reads_back():
    sent = ss.resolve(overrides=overrides(), default_shots=BOAS_DEFAULT,
                      master_category="Men", category="Shoes").as_dict()
    back = ss.from_payload(sent)
    assert back is not None and back.describe() == "the Shoes sequence"
    assert ss.from_payload(None) is None and ss.from_payload({"shots": []}) is None


# --------------------------------------------------------------------------- #
# The renders against it
# --------------------------------------------------------------------------- #

def ai(view: str, shot: str | None, position: int, url: str | None = None) -> MediaAsset:
    return MediaAsset(url=url or f"https://r2/x-{view}-{position}.jpg", view=view,
                      origin="AI", processing="GENERATED", position=position, shot_key=shot)


BOAS_FIVE = [ai("AI_FRONT_34", "three_quarter_front", 0), ai("AI_BACK_34", "three_quarter_back", 1),
             ai("AI_FRONT", "full_front", 2), ai("AI_BACK", "full_back", 3),
             ai("AI_CLOSEUP", "closeup", 4)]


def test_boas_five_renders_are_one_short_of_its_six():
    r = ss.resolve(overrides=[], default_shots=BOAS_DEFAULT)
    p = ss.plan(r, BOAS_FIVE)
    assert [i.label for i in p.missing] == ["full_front#2"]
    assert p.extra == []


def test_two_shots_on_one_view_are_told_apart():
    r = ss.resolve(overrides=[], default_shots=MAGIC_BODY)
    p = ss.plan(r, [ai("AI_CLOSEUP", "closeup_back", 0)])
    assert [i.label for i in p.missing] == ["closeup", "three_quarter_front", "three_quarter_back"]


def test_a_render_without_a_shot_key_is_read_from_its_url_then_its_view():
    assert ss.shot_of({"url": "https://r2/a-gen-closeup-back-9f.jpg", "view": "AI_CLOSEUP"}) == "closeup_back"
    assert ss.shot_of({"url": "https://r2/a-gen-back-34-9f.jpg", "view": "AI_BACK_34"}) == "three_quarter_back"
    assert ss.shot_of({"url": "https://r2/legacy.jpg", "view": "AI_BACK"}) == "full_back"


def test_renders_outside_the_sequence_are_extra_never_paired():
    r = ss.resolve(overrides=overrides(), default_shots=None, master_category="Men", category="Shoes")
    p = ss.plan(r, [ai("AI_FRONT", "full_front", 0), ai("AI_CLOSEUP", "closeup", 1)])
    assert [shot for _, shot in p.extra] == ["full_front"]
    assert [i.label for i in p.missing] == ["closeup_back", "detail_macro"]


def test_mislabelled_is_decided_per_shot():
    old_five = [ai("AI_FRONT", None, i, f"https://r2/old-{i}.jpg") for i in range(5)]
    r = ss.resolve(overrides=[], default_shots=BOAS_DEFAULT)
    assert ss.looks_mislabelled(ss.plan(r, old_five), old_five)
    shoes = ss.resolve(overrides=overrides(), default_shots=None, master_category="Men", category="Shoes")
    three = [ai("AI_CLOSEUP", None, 0, "https://r2/a-gen-closeup-1.jpg"),
             ai("AI_CLOSEUP", None, 1, "https://r2/a-gen-closeup-back-1.jpg"),
             ai("AI_CLOSEUP", None, 2, "https://r2/a-gen-detail-1.jpg")]
    assert not ss.looks_mislabelled(ss.plan(shoes, three), three)


# --------------------------------------------------------------------------- #
# The gallery order (rebuildMediaCache's)
# --------------------------------------------------------------------------- #

def photo(view: str, origin: str, processing: str = "BG_REMOVED", position: int = 0) -> MediaAsset:
    return MediaAsset(url=f"https://r2/{origin}-{view}-{position}.png", view=view, origin=origin,
                      processing=processing, position=position)


def test_boas_shows_its_originals_first_then_the_renders_then_the_labels():
    rows = [*BOAS_FIVE, ai("AI_FRONT", "full_front", 9, "https://r2/second-front.jpg"),
            photo("FRONT", "PHOTOBOOTH"), photo("BACK", "DECISION"), photo("FRONT", "DECISION"),
            photo("LABEL", "DECISION", "RAW")]
    order = [m.url for m in ss.display_order(rows, BOAS_DEFAULT)]
    assert order[:3] == ["https://r2/DECISION-FRONT-0.png", "https://r2/DECISION-BACK-0.png",
                         "https://r2/PHOTOBOOTH-FRONT-0.png"]
    assert order[3:5] == ["https://r2/x-AI_FRONT_34-0.jpg", "https://r2/x-AI_BACK_34-1.jpg"]
    assert order[-2:] == ["https://r2/second-front.jpg", "https://r2/DECISION-LABEL-0.png"]


def test_a_disabled_slot_is_left_out_of_the_gallery():
    lines = [{"slot": "labels", "enabled": False}, {"shot": "full_front"}]
    rows = [ai("AI_FRONT", "full_front", 0), photo("LABEL", "DECISION", "RAW")]
    assert [m.view for m in ss.display_order(rows, lines)] == ["AI_FRONT"]


# --------------------------------------------------------------------------- #
# The imagery rules with a sequence
# --------------------------------------------------------------------------- #

def snap(seq: list[dict], media: list[MediaAsset], **kw) -> ProductSnapshot:
    r = ss.resolve(overrides=overrides(), default_shots=seq, master_category=kw.get("master_category"),
                   category=kw.get("category"))
    base = dict(id="p1", title="t", master_category="Men", category="Jeans",
                generation_status="COMPLETE", imagery_settings=ImagerySettings(),
                media=[photo("FRONT", "PHOTOBOOTH"), photo("BACK", "PHOTOBOOTH", position=1), *media],
                shot_sequence=r.as_dict())
    base.update(kw)
    return ProductSnapshot(**base)


def test_a_shoes_sequence_is_not_missing_a_back_render(pol):
    p = snap(BOAS_DEFAULT, [ai("AI_CLOSEUP", "closeup", 0)], category="Shoes")
    report = imagery.view_report(p, pol)
    assert report.required == [] and report.missing == []
    assert report.missing_shots == ["closeup_back", "detail_macro"]
    ids = [f.rule_id for f in imagery.check_imagery(p, pol)]
    assert "IMG.002" not in ids and "IMG.005" in ids


def test_boas_second_full_front_is_counted_and_left_to_the_sequence_render(pol):
    p = snap(BOAS_DEFAULT, list(BOAS_FIVE))
    report = imagery.view_report(p, pol)
    assert report.missing_shots == ["full_front#2"] and report.missing == []
    plan = imagery.generation_plan(p, pol, include_advisory=True)
    # The VIEW generator would retire the full front on file to make it: not offered.
    assert plan.views == [] and plan.shots == ["full_front#2"]
    finding = next(f for f in imagery.check_imagery(p, pol) if f.rule_id == "IMG.005")
    assert "full_front#2" in finding.message and "6 render(s)" in finding.message


def test_the_view_generator_gets_only_views_it_can_fill_safely(pol):
    p = snap(MAGIC_BODY, [ai("AI_FRONT_34", "three_quarter_front", 0)])
    plan = imagery.generation_plan(p, pol, include_advisory=True)
    # AI_CLOSEUP is two shots and empty: the classic front close-up is one of them,
    # nothing on the view to retire. AI_BACK_34 is one shot. Nothing torso-length.
    assert plan.views == ["AI_BACK_34", "AI_CLOSEUP"]
    assert plan.shots == ["closeup", "closeup_back", "three_quarter_back"]


def test_the_gallery_check_uses_the_sequence_order(pol):
    rows = [*BOAS_FIVE, photo("FRONT", "DECISION")]
    p = snap(BOAS_DEFAULT, rows[:-1])
    p = p.model_copy(update={"media": [*p.media, rows[-1]],
                             "images": [m.url for m in ss.display_order(
                                 [m for m in [*p.media, rows[-1]] if m.live], BOAS_DEFAULT)]})
    assert "IMG.025" not in [f.rule_id for f in imagery.check_imagery(p, pol)]
    # The fixed order (renders first) would have called it out of order — and the
    # rebuild the chain then ran would have written this same order back.
    fixed = sorted(imagery.live_media(p), key=lambda m: imagery.gallery_key(m, pol))
    assert imagery.gallery_divergence(p.images, fixed) is not None


def test_policy_can_switch_the_sequence_off(pol):
    off = {**pol, "imagery": {**pol["imagery"], "sequence": {"enabled": False}}}
    p = snap(BOAS_DEFAULT, [ai("AI_CLOSEUP", "closeup", 0)], category="Shoes")
    assert imagery.view_report(p, off).required == ["AI_FRONT", "AI_BACK"]


# --------------------------------------------------------------------------- #
# The repair chain
# --------------------------------------------------------------------------- #

def test_the_chain_counts_against_the_sequence_and_asks_for_it():
    import scripts.repair_product as rp

    record = {"title": "t", "summary": "s", "shotSequence": ss.resolve(
        overrides=[], default_shots=BOAS_DEFAULT).as_dict()}
    media = [{"url": m.url, "view": m.view, "origin": "AI", "processing": "GENERATED",
              "mediaType": "IMAGE", "isCurrent": True, "position": m.position,
              "shotKey": m.shot_key} for m in BOAS_FIVE]
    state = rp.needs_from({"record": record, "media": media})
    assert (state["renders"], state["renders_expected"], state["renders_missing"]) == (5, 6, 1)
    assert state["missing_shots"] == ["full_front#2"] and not state["mislabelled"]
    assert rp._renders_of(state) == "5/6"
    assert rp._sequence_views(state) == ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"]


def test_the_step_runner_carries_sequence_and_shots(monkeypatch):
    import scripts.repair_product as rp

    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "output": ""}

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.update(json)
        return Resp()

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(rp, "vnyx_api_url", lambda: "http://api")
    monkeypatch.setattr(rp, "remote_async", lambda: False)
    monkeypatch.setenv("AUTO_APPROVAL_INTERNAL_SECRET", "x")
    rp.run_remote("backfill-imagery.ts",
                  ["--product", "6f1d2c3b-4a5e-4f60-9b1c-2d3e4f5a6b7c", "--apply", "--views", "AI_CLOSEUP",
                   "--sequence", "--replace", "--shots", "closeup_back"], timeout_s=10, quiet=True)
    assert sent["step"] == "render"
    assert sent["options"] == {"views": ["AI_CLOSEUP"], "sequence": True, "replace": True,
                               "shots": ["closeup_back"]}
