"""app/llm/cache.py — the keys, the file backend, the never-remember-a-failure
rule, and the four call sites through fakes.

The suite-wide conftest switches the cache OFF; the `on` fixture here turns it
back on under a temporary directory, so nothing these tests write outlives them.
"""
from __future__ import annotations

import time

import pytest

from app.llm import cache

POL = {"llm": {"model_fast": "test-model", "max_images": 4,
               "cache": {"ttl_hours": 1}},
       "guardrails": {"llm_forbidden_fields": []},
       "quality_gate": {}}

GOOD_GATE = {"model_present": True, "face_ok": True, "gender": "Men", "lead_ok": True,
             "body_coherent": True, "body_issue": "", "view": "front",
             "garment": "t-shirt", "confidence": 0.9}


@pytest.fixture
def on(tmp_path, monkeypatch):
    # conftest sets the kill switch; lifting it lets the policy default (on) apply.
    monkeypatch.delenv("HERMES_VISION_CACHE", raising=False)
    monkeypatch.setenv("HERMES_VISION_CACHE_DIR", str(tmp_path))
    cache.reset()
    yield tmp_path
    cache.reset()


# -------------------------------------------------------------------- keys

def test_key_keeps_the_query_and_drops_the_fragment():
    """`?v=` is how Shopify says the file changed; stripping it served stale
    answers on 2026-07-29. A fragment never reaches the server, so it goes."""
    a = cache.key("ns", urls=["https://x/a.jpg?v=1"])
    b = cache.key("ns", urls=["https://x/a.jpg?v=2"])
    c = cache.key("ns", urls=[" https://x/a.jpg?v=1#frag "])
    assert a != b
    assert a == c


def test_key_depends_on_namespace_prompt_model_and_image_order():
    base = cache.key("ns", urls=["u1", "u2"], text=["m", "prompt"])
    assert cache.key("ns", urls=["u1", "u2"], text=["m", "prompt"]) == base
    assert cache.key("ns", urls=["u2", "u1"], text=["m", "prompt"]) != base
    assert cache.key("ns", urls=["u1", "u2"], text=["m", "prompt 2"]) != base
    assert cache.key("ns", urls=["u1", "u2"], text=["m2", "prompt"]) != base
    assert cache.key("other", urls=["u1", "u2"], text=["m", "prompt"]) != base
    assert base.startswith("ns:")


def test_bytes_are_keyed_by_content_and_schemas_by_value():
    assert cache.key("bg", blobs=[b"abc"]) == cache.key("bg", blobs=[b"abc"])
    assert cache.key("bg", blobs=[b"abc"]) != cache.key("bg", blobs=[b"abd"])
    assert (cache.key("g", text=[{"b": 1, "a": 2}])
            == cache.key("g", text=[{"a": 2, "b": 1}]))


# ------------------------------------------------------------ file backend

def test_round_trip_counts_and_lands_in_the_configured_directory(on):
    k = cache.key("ns", urls=["u"])
    assert cache.get(k, POL) is None
    assert cache.put(k, {"a": 1}, POL) is True
    assert cache.get(k, POL) == {"a": 1}
    s = cache.snapshot()
    assert (s["hits"], s["misses"], s["puts"], s["backend"]) == (1, 1, 1, "file")
    files = list(on.rglob("*.json"))
    assert len(files) == 1 and files[0].parent.name == "ns"
    assert "1 hit" in cache.summary() and "1 miss" in cache.summary()


def test_failures_are_never_stored(on):
    k = cache.key("ns", urls=["u"])
    assert cache.put(k, None, POL) is False
    assert cache.put(k, {}, POL) is False
    assert cache.put(k, "not a dict", POL) is False
    assert cache.get(k, POL) is None
    assert not list(on.rglob("*.json"))


def test_expired_entries_are_misses(on, monkeypatch):
    k = cache.key("ns", urls=["u"])
    cache.put(k, {"a": 1}, POL)
    real = time.time
    monkeypatch.setattr(cache.time, "time", lambda: real() + 2 * 3600)
    assert cache.get(k, POL) is None
    assert not list(on.rglob("*.json"))     # swept on read


def test_disabled_by_policy_or_environment(on, monkeypatch):
    k = cache.key("ns", urls=["u"])
    assert cache.put(k, {"a": 1}, {"llm": {"cache": {"enabled": False}}}) is False
    assert cache.put(k, {"a": 1}, POL) is True
    monkeypatch.setenv("HERMES_VISION_CACHE", "off")
    assert cache.get(k, POL) is None
    assert cache.summary() is None          # never consulted while off


def test_a_torn_file_is_a_miss_and_is_removed(on):
    k = cache.key("ns", urls=["u"])
    cache.put(k, {"a": 1}, POL)
    p = next(on.rglob("*.json"))
    p.write_text("{not json", encoding="utf-8")
    assert cache.get(k, POL) is None
    assert not p.exists()


def test_through_computes_once_and_honours_complete(on):
    calls: list[int] = []

    def compute():
        calls.append(1)
        return {"v": len(calls)}

    k = cache.key("ns", urls=["u"])
    value, hit = cache.through(k, compute, pol=POL, complete=lambda: False)
    assert (value, hit) == ({"v": 1}, False)
    value, hit = cache.through(k, compute, pol=POL)     # not stored above
    assert (value, hit) == ({"v": 2}, False)
    value, hit = cache.through(k, compute, pol=POL)
    assert (value, hit) == ({"v": 2}, True)
    assert len(calls) == 2


def test_an_unreachable_redis_demotes_to_files(on):
    pol = {"llm": {"cache": {"redis_url": "redis://127.0.0.1:1/0", "ttl_hours": 1}}}
    k = cache.key("ns", urls=["u"])
    assert cache.put(k, {"a": 1}, pol) is True
    assert cache.get(k, pol) == {"a": 1}
    assert cache.snapshot()["backend"] == "file"


# -------------------------------------------------------------- call sites

class _FakeEvidence:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = 0
        self.errors: list[str] = []
        self.last_error_kind = None

    def _fetch_images(self, urls):
        return ["part"] * len(urls)

    def _generate(self, **kw):
        self.calls += 1
        answer = self.answers.pop(0)
        if answer is None:
            self.errors.append("boom")
            self.last_error_kind = "api"
        return answer


def test_gate_answers_from_the_cache_and_says_so(on):
    from app.imaging import quality_gate as qg

    media = [{"url": "https://r2/x/front.jpg", "view": "AI_FRONT"}]
    ev = _FakeEvidence([GOOD_GATE, {**GOOD_GATE, "gender": "Women"}])
    first = qg.judge(media, gender="men", pol=POL, evidence=ev)
    second = qg.judge(media, gender="men", pol=POL, evidence=ev)
    assert ev.calls == 1                       # the second answer was never asked for
    assert first.action == second.action == "ok"
    assert first.cached is False and second.cached is True
    assert second.summary().endswith("(cached)")
    assert second.as_dict()["cached"] is True


def test_gate_never_remembers_a_failed_call(on):
    from app.imaging import quality_gate as qg

    media = [{"url": "https://r2/x/front.jpg", "view": "AI_FRONT"}]
    ev = _FakeEvidence([None, GOOD_GATE])
    first = qg.judge(media, gender="men", pol=POL, evidence=ev)
    assert first.unavailable and first.code == "VISION_UNAVAILABLE"
    second = qg.judge(media, gender="men", pol=POL, evidence=ev)
    assert ev.calls == 2 and second.action == "ok" and not second.cached


def test_gate_decides_afresh_on_a_cached_answer(on):
    """Only the model's words are stored. The policy applied to them is live."""
    from app.imaging import quality_gate as qg

    media = [{"url": "https://r2/x/front.jpg", "view": "AI_FRONT"}]
    ev = _FakeEvidence([{**GOOD_GATE, "gender": "Women"}])
    assert qg.judge(media, gender="women", pol=POL, evidence=ev).action == "ok"
    held = qg.judge(media, gender="men", pol=POL, evidence=ev)
    assert held.cached and held.code == "MODEL_GENDER_MISMATCH"
    assert ev.calls == 1


def test_care_label_read_is_answered_without_a_download(on, monkeypatch):
    from app.llm import care_label as cl

    fetches: list[int] = []
    reads: list[int] = []
    answer = {"brand": "Nike", "brand_confidence": 95, "size": "M",
              "size_confidence": 90, "notes": ""}

    def fake_fetch(urls, cap=cl._FETCH_CAP):
        fetches.append(1)
        return [(b"img", "image/jpeg")]

    def fake_gemini(images):
        reads.append(1)
        return answer, "ok"

    monkeypatch.setattr(cl, "_fetch", fake_fetch)
    monkeypatch.setattr(cl, "_read_gemini", fake_gemini)
    monkeypatch.setattr(cl, "_read_openai", lambda images: (None, "not_configured"))

    urls = ["https://r2/label.jpg"]
    first = cl.read(urls)
    second = cl.read(urls)
    assert first["brand"] == second["brand"] == "Nike" and second["size"] == "M"
    assert len(reads) == 1 and len(fetches) == 1
    assert first["cached"] == [] and second["cached"] == ["gemini"]
    assert second["provider"] == "gemini" and second["tried"] == ["gemini"]


def test_care_label_failure_is_not_remembered_and_partial_fetch_is_not_stored(on, monkeypatch):
    from app.llm import care_label as cl

    reads: list[str] = []
    answer = {"brand": "Nike", "brand_confidence": 95, "size": None,
              "size_confidence": 0, "notes": ""}
    outcomes = [(None, "api_error"), (answer, "ok"), (answer, "ok")]

    def fake_gemini(images):
        reads.append("gemini")
        return outcomes.pop(0)

    monkeypatch.setattr(cl, "_fetch", lambda urls, cap=cl._FETCH_CAP: [(b"img", "image/jpeg")])
    monkeypatch.setattr(cl, "_read_gemini", fake_gemini)
    monkeypatch.setattr(cl, "_read_openai", lambda images: (None, "not_configured"))

    # Two label URLs asked for, one downloaded: the answer is used, not stored.
    urls = ["https://r2/label-1.jpg", "https://r2/label-2.jpg"]
    assert cl.read(urls).get("api_failed") is True
    assert cl.read(urls)["brand"] == "Nike"
    assert cl.read(urls)["brand"] == "Nike"
    assert reads == ["gemini", "gemini", "gemini"]


def test_classify_background_is_cached_by_bytes(on, monkeypatch):
    from app.llm.gemini import GeminiEvidence

    ev = GeminiEvidence("fake-key", POL)
    calls: list[int] = []

    def fake_generate(**kw):
        calls.append(1)
        return {"background": "real_scene", "confidence": 0.8, "reasoning": "floor line"}

    monkeypatch.setattr(ev, "_generate", fake_generate)
    assert ev.classify_background(b"picture-a")[0] == "real_scene"
    assert ev.classify_background(b"picture-a")[0] == "real_scene"
    assert len(calls) == 1
    ev.classify_background(b"picture-b")
    assert len(calls) == 2


def test_audit_images_is_cached_only_when_every_image_arrived(on, monkeypatch):
    from app.llm.gemini import GeminiEvidence
    from app.models import MediaAsset, ProductSnapshot

    ev = GeminiEvidence("fake-key", POL)
    p = ProductSnapshot(id="p1", images=["https://r2/f.jpg", "https://r2/b.jpg"],
                        media=[MediaAsset(url="https://r2/label.jpg", view="LABEL")])
    assert p.care_label_urls == ["https://r2/label.jpg"]
    calls: list[int] = []
    drop = {"one": True}

    def fake_fetch(urls):
        got = ["part"] * len(urls)
        return got[:-1] if drop["one"] and got else got

    def fake_generate(**kw):
        calls.append(1)
        return {"verdicts": [], "visible_defects": ["stain"], "notes": ""}

    monkeypatch.setattr(ev, "_fetch_images", fake_fetch)
    monkeypatch.setattr(ev, "_generate", fake_generate)

    assert ev.audit_images(p, {"brand": "Nike"}).visible_defects == ["stain"]   # short one image
    drop["one"] = False
    ev.audit_images(p, {"brand": "Nike"})                                        # complete → stored
    ev.audit_images(p, {"brand": "Nike"})                                        # hit
    assert len(calls) == 2
    ev.audit_images(p, {"brand": "Adidas"})                                      # other claims
    assert len(calls) == 3
