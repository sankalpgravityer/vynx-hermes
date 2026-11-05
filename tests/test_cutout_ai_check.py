"""The AI check before a cut-out is returned (3 Nov 2026; `imagery.cutout.ai_check`).

Every candidate the pixel checks accept is shown to the judge beside the photograph.
A flagged candidate hands over to the next model like any other refusal; when every
model is refused the chain answers nothing and vnyx-api turns to its own Gemini
background removal. Hermes never paints: the gate adds no paid strategy.

Synthetic images, a fake judge. Nothing from a tenant, nothing on the network.
"""
from __future__ import annotations

import base64
import io
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout  # noqa: E402
from app.imaging import cutout_integrity as ci  # noqa: E402

H, W = 400, 300


def png(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(rgba, "RGBA").save(buf, format="PNG")
    return buf.getvalue()


def garment(colour=(40, 50, 120)) -> np.ndarray:
    a = np.zeros((H, W, 4), np.uint8)
    a[..., :3] = 255
    a[60:280, 70:230, :3] = colour
    a[60:280, 70:230, 3] = 255
    return a


def scene() -> bytes:
    rgb = np.full((H, W, 3), (206, 206, 200), np.uint8)
    rgb[60:280, 70:230] = (40, 50, 120)
    buf = io.BytesIO()
    Image.fromarray(rgb, "RGB").save(buf, format="PNG")
    return buf.getvalue()


V2 = png(garment((40, 50, 120)))
V1 = png(garment((41, 50, 120)))      # a different byte string, so the judge can tell them apart


def answer(ok: bool, part: str = "strap") -> dict:
    return {"views": [{"view": "x", "ok": ok, "summary": "s",
                       "defects": [] if ok else [{"type": "cloth_broken", "part": part,
                                                  "severity": "clear", "where": "left"}]}]}


@pytest.fixture
def chain(monkeypatch):
    """Both fine-tuned parsers answer, every pixel check passes; the judge is `judge`."""
    monkeypatch.setattr(cutout, "_cloth_seg_ft", lambda data: (V2, None))
    monkeypatch.setattr(cutout, "_cloth_seg_ft_backup", lambda data: (V1, None))
    monkeypatch.setattr(cutout, "_is_cutout", lambda b: (True, "ok"))
    monkeypatch.setattr(cutout, "_kept_stand", lambda p, c=None: (False, ""))
    monkeypatch.setattr(cutout, "_kept_backdrop", lambda s, o, c=None: (True, "ok"))
    monkeypatch.setattr(cutout, "_torn_garment", lambda s, o, c=None: (False, "ok"))
    monkeypatch.setattr(cutout, "_finish_neck", lambda data, out, *a, **k: out)
    monkeypatch.setattr(cutout, "_png_with_icc", lambda out, icc: out)
    monkeypatch.setattr(cutout, "_ft_available", lambda: True)
    asked: list[tuple[bytes, str | None]] = []

    def judge(verdict_for):
        def _judge(raw, cut, *, category=None, model=None, view="cut-out", headwear=False):
            asked.append((cut, category))
            v = verdict_for(cut)
            return (None, "no key") if v is None else (answer(v), "")
        monkeypatch.setattr(ci, "judge_pair", _judge)
        return asked
    return judge


def run(**kw):
    report: dict = {}
    out, err, provider = cutout.remove_background(
        scene(), timeout_s=1, strategies=["cloth-seg-ft", "cloth-seg-ft-backup"],
        garment="T-Shirts & Polos T-Shirt", report=report, **kw)
    return out, err, provider, report


def test_a_cut_out_the_judge_passes_is_returned_with_its_verdict(chain):
    asked = chain(lambda cut: True)
    out, err, provider, report = run()
    assert provider == "cloth-seg-ft" and out == V2, err
    assert len(asked) == 1 and asked[0][1] == "T-Shirts & Polos T-Shirt"
    ai = report["ai_check"]
    assert ai["ran"] and not ai["flagged"]
    assert [v["strategy"] for v in ai["verdicts"]] == ["cloth-seg-ft#1"]
    assert ai["verdicts"][0]["ok"] is True


def test_a_flagged_v2_hands_over_to_v1(chain):
    asked = chain(lambda cut: cut != V2)
    out, err, provider, report = run()
    assert provider == "cloth-seg-ft-backup" and out == V1, err
    assert len(asked) == 2
    ai = report["ai_check"]
    assert ai["flagged"] is True
    assert [(v["strategy"], v["ok"]) for v in ai["verdicts"]] == [
        ("cloth-seg-ft#1", False), ("cloth-seg-ft-backup#1", True)]
    assert "strap" in ai["verdicts"][0]["defects"][0]


def _no_fallback(monkeypatch):
    base = cutout.config()
    monkeypatch.setattr(cutout, "config", lambda pol=None: {**base, "ai_fallback": []})


def test_every_model_flagged_is_no_cut_out_with_every_verdict(chain, monkeypatch):
    _no_fallback(monkeypatch)
    chain(lambda cut: False)
    out, err, provider, report = run()
    assert out is None and provider == "none"
    assert "AI check" in err and "cloth-seg-ft#1" in err and "cloth-seg-ft-backup#1" in err
    ai = report["ai_check"]
    assert ai["flagged"] and [v["ok"] for v in ai["verdicts"]] == [False, False]


GEMINI = png(garment((42, 50, 120)))   # what the Gemini fallback answers


def test_with_the_fallback_off_the_judge_adds_no_paid_strategy(chain, monkeypatch):
    _no_fallback(monkeypatch)
    paid: list[str] = []
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (paid.append("gemini"), (None, "x"))[1])
    monkeypatch.setattr(cutout, "_openai", lambda *a, **k: (paid.append("openai"), (None, "x"))[1])
    chain(lambda cut: False)
    out, err, provider, report = run()
    assert out is None and paid == []


# --------------------------------------------------------------------------- #
# Gemini when the AI check refuses our models (5 Nov 2026, `ai_fallback`)
# --------------------------------------------------------------------------- #

def test_an_ai_refusal_of_our_models_asks_gemini_last(chain, monkeypatch):
    # KLE-000361: v2 cut a hole in the waistband; Gemini's outline kept it.
    paid: list[str] = []
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (paid.append("gemini"), (GEMINI, None))[1])
    seen = []
    # our two models refused, the third candidate (Gemini's, on the photo's own
    # pixels — byte-identical here, so judged by order) passed
    asked = chain(lambda cut: (seen.append(1), len(seen) > 2)[1])
    out, err, provider, report = run()
    assert provider == "gemini-paint" and out is not None, err
    assert paid == ["gemini"]
    # our two models were judged first, Gemini third
    assert [v["strategy"] for v in report["ai_check"]["verdicts"]] == [
        "cloth-seg-ft#1", "cloth-seg-ft-backup#1", "gemini-paint#1"]
    assert len(asked) == 3


def test_a_cut_out_the_judge_passes_never_asks_gemini(chain, monkeypatch):
    paid: list[str] = []
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (paid.append("gemini"), (GEMINI, None))[1])
    chain(lambda cut: True)
    out, err, provider, report = run()
    assert provider == "cloth-seg-ft" and paid == []


def test_a_pixel_refusal_of_every_model_also_asks_gemini(chain, monkeypatch):
    # BOA-006449: every model's mask "tore" the jeans; the old low-resolution
    # cut-out was kept and the product stayed held. Gemini is asked last.
    paid: list[str] = []
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (paid.append("gemini"), (GEMINI, None))[1])
    refuse_ours = {"n": 0}

    def is_cutout(b):
        refuse_ours["n"] += 1
        return (refuse_ours["n"] > 2, "the mask tore 0.3% of the garment out")
    monkeypatch.setattr(cutout, "_is_cutout", is_cutout)
    chain(lambda cut: True)
    out, err, provider, report = run()
    assert paid == ["gemini"] and provider == "gemini-paint", err


def test_the_caller_can_skip_the_fallback(chain, monkeypatch):
    paid: list[str] = []
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (paid.append("gemini"), (GEMINI, None))[1])
    chain(lambda cut: False)
    out, err, provider, report = run(skip=["gemini-paint"])
    assert paid == [] and out is None


def test_the_shipped_policy_falls_back_to_gemini_paint():
    assert cutout.config().get("ai_fallback") == ["gemini-paint"]


def test_an_unavailable_judge_accepts_on_the_pixel_checks(chain):
    chain(lambda cut: None)
    out, err, provider, report = run()
    assert provider == "cloth-seg-ft" and out == V2, err
    ai = report["ai_check"]
    assert ai["ran"] and not ai["flagged"] and ai["verdicts"][0]["ok"] is None


def test_policy_can_refuse_when_the_judge_is_unavailable(chain, monkeypatch):
    chain(lambda cut: None)
    base = cutout.config()
    monkeypatch.setattr(cutout, "config", lambda pol=None: {
        **base, "ai_check": {**cutout.ai_check_config(base), "on_unavailable": "refuse"}})
    out, err, provider, report = run()
    assert out is None and "could not run" in err


def test_the_request_can_turn_the_check_off(chain):
    asked = chain(lambda cut: False)
    out, err, provider, report = run(ai_check=False)
    assert provider == "cloth-seg-ft" and out == V2
    assert asked == [] and report["ai_check"]["ran"] is False


def test_policy_false_turns_the_check_off(chain, monkeypatch):
    asked = chain(lambda cut: False)
    base = cutout.config()
    monkeypatch.setattr(cutout, "config", lambda pol=None: {**base, "ai_check": False})
    out, err, provider, report = run()
    assert provider == "cloth-seg-ft" and asked == []


def test_the_shipped_policy_turns_the_check_on():
    c = cutout.ai_check_config()
    assert c["enabled"] is True and c["on_unavailable"] == "accept"
    assert int(c["max_concurrent"]) >= 1


def test_judge_pair_is_cached_on_the_bytes(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ci, "_ask_bytes",
                        lambda items, category, model, headwear=False: (calls.append(1), answer(True))[1])
    from app.llm import cache
    hits: dict = {}
    monkeypatch.setattr(cache, "through", lambda k, compute, pol=None, complete=None:
                        (hits.setdefault(k, compute()), k in hits and len(calls) < len(hits) + 1))
    a1, _ = ci.judge_pair(b"raw", V2, category="Tops")
    a2, _ = ci.judge_pair(b"raw", V2, category="Tops")
    assert a1 == a2 == answer(True)
    assert len(hits) == 1


# ---- the endpoint carries the verdicts ------------------------------------------

def test_the_endpoint_reports_the_verdicts(chain, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    chain(lambda cut: cut != V2)
    monkeypatch.setattr("app.main._framed", lambda out, frame, canvas=None: (out, None))
    client = TestClient(app)
    r = client.post("/v1/imagery/remove-background", json={
        "image_base64": base64.b64encode(scene()).decode(), "timeout_s": 1,
        "strategies": ["cloth-seg-ft", "cloth-seg-ft-backup"], "garment": "T-Shirts & Polos T-Shirt",
        "frame": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["provider"] == "cloth-seg-ft-backup"
    assert body["ai_check"]["flagged"] is True
    assert [v["strategy"] for v in body["ai_check"]["verdicts"]] == ["cloth-seg-ft#1", "cloth-seg-ft-backup#1"]


def test_the_endpoint_says_why_when_every_model_is_refused(chain, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    chain(lambda cut: False)
    client = TestClient(app)
    r = client.post("/v1/imagery/remove-background", json={
        "image_base64": base64.b64encode(scene()).decode(), "timeout_s": 1,
        "strategies": ["cloth-seg-ft", "cloth-seg-ft-backup"], "ai_check": True, "frame": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False and body["image_base64"] is None
    assert body["ai_check"]["flagged"] is True and len(body["ai_check"]["verdicts"]) == 2


# ---- headwear keeps its display head (3 Nov 2026, TST-000019) --------------------

def test_a_mannequin_head_under_a_cap_holds_nothing():
    head = {"views": [{"view": "x", "ok": False, "summary": "head kept", "defects": [
        {"type": "background", "part": "mannequin head", "severity": "clear", "where": "below the cap"}]}]}
    assert ci.failing(head) and "mannequin head" in ci.failing(head)[0]
    assert ci.failing(head, headwear=True) == []
    assert ci.failing_views(head, headwear=True) == []
    assert any("mannequin head" in n for n in ci.noted(head, headwear=True))


def test_a_stand_below_the_neck_still_holds_for_headwear():
    stand = {"views": [{"view": "x", "ok": False, "summary": "s", "defects": [
        {"type": "background", "part": "stand", "severity": "clear", "where": "below the neck"}]}]}
    assert ci.failing(stand, headwear=True)


def test_the_category_decides_headwear():
    assert ci.is_headwear("Accessories Caps, Beanies & hats")
    assert ci.is_headwear("Hats Bucket Hat")
    assert not ci.is_headwear("T-Shirts & Tops Cap Sleeve Top")
    assert not ci.is_headwear("Backpacks & Bags Bags")
    assert not ci.is_headwear(None)


def test_the_gate_tells_the_judge_about_headwear_and_lets_the_head_through(chain, monkeypatch):
    seen: dict = {}

    def _judge(raw, cut, *, category=None, model=None, view="cut-out", headwear=False):
        seen["headwear"] = headwear
        return {"views": [{"view": view, "ok": False, "summary": "", "defects": [
            {"type": "background", "part": "display head", "severity": "clear", "where": "under the cap"}]}]}, ""
    monkeypatch.setattr(ci, "judge_pair", _judge)
    report: dict = {}
    out, err, provider = cutout.remove_background(
        scene(), timeout_s=1, strategies=["cloth-seg-ft", "cloth-seg-ft-backup"],
        garment="Accessories Caps, Beanies & hats", report=report)
    assert seen["headwear"] is True
    assert out == V2 and provider == "cloth-seg-ft", err
    assert report["ai_check"]["verdicts"][0]["ok"] is True


@pytest.mark.parametrize("iou,accepted", [(0.975, True), (0.5, False)])
def test_gemini_refused_only_by_the_backdrop_check_is_the_third_vote(chain, monkeypatch, iou, accepted):
    # BOA-007642: white leather sleeves read as backdrop, so v2, v1 AND Gemini were
    # refused and the old 896x1195 cut-out was kept. Gemini agreeing with one of our
    # refused parsers means the check misread the garment (0.975: under v2/v1's 0.98,
    # over the fallback's 0.97).
    monkeypatch.setattr(cutout, "_gemini", lambda *a, **k: (GEMINI, None))
    monkeypatch.setattr(cutout, "_kept_backdrop",
                        lambda s, o, c=None: (False, "a single 2% region is its own backdrop colour"))
    monkeypatch.setattr(cutout, "_alpha_agreement", lambda a, b: iou)
    chain(lambda cut: True)
    out, err, provider, report = run()
    assert (provider == "gemini-paint") is accepted, err
