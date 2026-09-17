"""app/imaging/composition.py — does the figure fill the frame? — and how the
gate folds it in. Synthesised renders on a flat backdrop, a badge in the corner,
no network."""
from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.imaging import composition, quality_gate as qg  # noqa: E402
from app.imaging.quality_gate import GateVerdict  # noqa: E402

POL = policy()
CFG = composition.config(POL)
BACKDROP = (243, 243, 243)


def render(w: int = 600, h: int = 800, *, top: float = 0.02, bottom: float = 0.02,
           badge: bool = True) -> bytes:
    """A figure (a dark column) from `top` to `1-bottom` of the frame, the vnyx badge in the corner."""
    img = Image.new("RGB", (w, h), BACKDROP)
    d = ImageDraw.Draw(img)
    d.rectangle((int(w * 0.35), int(h * top), int(w * 0.65), int(h * (1 - bottom))), fill=(60, 40, 40))
    if badge:
        d.rectangle((w - 90, h - 30, w - 8, h - 8), fill=(30, 160, 120))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    return buf.getvalue()


def test_a_figure_reaching_the_bottom_edge_fills_the_frame():
    m = composition.measure(render(), CFG)
    assert m["bottom_margin"] <= 0.03 and m["height_fill"] >= 0.95
    assert composition.problem(m, CFG) is None


def test_an_empty_band_below_the_figure_is_the_defect():
    """MID-000247's lead: 22% empty below, the figure 76% of the height."""
    m = composition.measure(render(bottom=0.22, top=0.02), CFG)
    assert 0.20 <= m["bottom_margin"] <= 0.24 and 0.74 <= m["height_fill"] <= 0.78
    why = composition.problem(m, CFG)
    assert why and "empty band of 2" in why and "76%" in why


def test_the_badge_in_the_corner_does_not_pull_the_box_to_the_bottom():
    with_badge = composition.measure(render(bottom=0.22), CFG)
    without = composition.measure(render(bottom=0.22, badge=False), CFG)
    assert abs(with_badge["bottom_margin"] - without["bottom_margin"]) < 0.01


def test_a_blank_render_has_no_figure():
    img = Image.new("RGB", (600, 800), BACKDROP)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    m = composition.measure(buf.getvalue(), CFG)
    assert m["empty"] and composition.problem(m, CFG) == "no figure found against the backdrop"


MEDIA = [
    {"url": "https://x/f34.jpg", "view": "AI_FRONT_34", "mediaType": "IMAGE", "position": 0},
    {"url": "https://x/b34.jpg", "view": "AI_BACK_34", "mediaType": "IMAGE", "position": 1},
    {"url": "https://x/f.jpg", "view": "AI_FRONT", "mediaType": "IMAGE", "position": 2},
    {"url": "https://x/label.jpg", "view": "LABEL", "mediaType": "IMAGE", "position": 3},
]


def fake_fetch(files: dict[str, bytes | None]):
    return lambda urls, **kw: {u: files.get(u) for u in urls}


def test_check_measures_every_render_and_names_the_bad_view():
    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    v = composition.check(MEDIA, POL, fetch=fetch)
    assert [c.view for c in v.checks] == ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT"]   # not the label
    assert v.bad_views == ["AI_FRONT_34"] and not v.unavailable
    assert v.reasons[0].startswith("AI_FRONT_34: an empty band")


def test_check_is_unavailable_when_nothing_downloads():
    v = composition.check(MEDIA, POL, fetch=fake_fetch({}))
    assert v.unavailable and v.bad_views == []


# --------------------------------------------------------------------------- #
# Folded into the gate
# --------------------------------------------------------------------------- #

class FakeEvidence:
    def __init__(self, raw):
        self.raw, self.last_error_kind, self.errors = raw, None, []

    def _fetch_images(self, urls):
        return ["part"]

    def _generate(self, **kw):
        return self.raw


GOOD = {"model_present": True, "face_ok": True, "gender": "Men", "model_build": "average",
        "framing": "full_body", "lead_ok": True, "body_coherent": True, "body_issue": "",
        "view": "front", "garment": "t-shirt", "confidence": 0.9}


def test_a_passing_lead_with_one_badly_framed_view_re_renders_that_view_only():
    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL, evidence=FakeEvidence(GOOD), frames=fetch)
    assert v.action == "regen" and v.code == "IMAGE_COMPOSITION"
    assert v.bad_views == ["AI_FRONT_34"] and v.reasons[0].startswith("FRAME — AI_FRONT_34")
    assert qg.regen_views(v, POL) == ["AI_FRONT_34"]                       # not the lead
    assert v.composition["checks"][0]["problem"]


def test_a_set_refusal_stands_and_the_frame_findings_ride_along_as_data():
    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    v = qg.judge(MEDIA, gender="women", category="Tops", pol=POL, evidence=FakeEvidence(GOOD), frames=fetch)
    assert v.code == "MODEL_GENDER_MISMATCH" and v.bad_views == []
    assert qg.regen_views(v, POL) == POL["imagery"]["all_views"]


def test_a_lead_defect_and_a_frame_defect_re_render_both_views():
    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL,
                 evidence=FakeEvidence({**GOOD, "face_ok": False}), frames=fetch)
    assert v.code == "IMAGE_QUALITY" and v.bad_views == ["AI_FRONT_34"]
    assert qg.regen_views(v, POL) == ["AI_FRONT_34", "AI_FRONT"]


def test_the_frame_test_is_soft_when_switched_off_and_skipped_for_a_vision_double_without_a_fetcher():
    soft = copy.deepcopy(POL)
    soft["quality_gate"]["block_on"] = [b for b in soft["quality_gate"]["block_on"] if b != "bad_composition"]
    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=soft, evidence=FakeEvidence(GOOD), frames=fetch)
    assert v.action == "ok" and any(s.startswith("frame: AI_FRONT_34") for s in v.soft)
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL, evidence=FakeEvidence(GOOD))
    assert v.action == "ok" and v.composition is None


def test_the_frame_test_runs_on_the_real_path_whether_the_answer_was_cached_or_not(monkeypatch):
    """The first live run skipped it: on a cache MISS the real provider is
    assigned to the same name a test double would use, and the skip rule read
    that as 'a double, no fetcher'. Both paths must reach the frame test."""
    from app.llm import cache

    fetch = fake_fetch({"https://x/f34.jpg": render(bottom=0.22), "https://x/b34.jpg": render(),
                        "https://x/f.jpg": render()})
    # A cache hit: no provider is built at all.
    monkeypatch.setattr(cache, "get", lambda key, pol: dict(GOOD))
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL, frames=fetch)
    assert v.cached and v.code == "IMAGE_COMPOSITION" and v.bad_views == ["AI_FRONT_34"]

    # A cache miss: the provider is built inside judge() and must not count as injected.
    class Provider:
        errors: list[str] = []
        last_error_kind = None

        def __init__(self, *a, **k):
            pass

        def _fetch_images(self, urls):
            return ["part"]

        def _generate(self, **kw):
            return dict(GOOD)

    import types

    monkeypatch.setattr(cache, "get", lambda key, pol: None)
    monkeypatch.setattr(cache, "put", lambda key, raw, pol: None)
    monkeypatch.setitem(sys.modules, "app.llm.gemini", types.SimpleNamespace(GeminiEvidence=Provider))
    from app import config as app_config
    from types import SimpleNamespace

    monkeypatch.setattr(app_config, "settings", lambda: SimpleNamespace(gemini_api_key="k"))
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL, frames=fetch)
    assert not v.cached and v.code == "IMAGE_COMPOSITION" and v.bad_views == ["AI_FRONT_34"]


def test_no_download_is_a_soft_note_never_a_hold():
    v = qg.judge(MEDIA, gender="men", category="Tops", pol=POL, evidence=FakeEvidence(GOOD),
                 frames=fake_fetch({}))
    assert v.action == "ok" and any("frame test" in s for s in v.soft)
    assert qg.regen_views(GateVerdict("regen", "IMAGE_COMPOSITION", ["x"], bad_views=["AI_BACK"],
                                      lead_view="AI_FRONT"), POL) == ["AI_BACK"]
