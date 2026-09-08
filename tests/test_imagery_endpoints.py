"""The two imagery endpoints, exercised through the app.

WHY THIS FILE EXISTS SEPARATELY FROM THE UNIT TESTS

A local named `settings` shadowed the module-level config accessor of the same
name, so `s = settings()` at the top of the generate handler resolved to a local
that had not been assigned yet. Every unit test passed — none of them called the
handler — and it only surfaced as an `UnboundLocalError` against a real product.

So these drive the routes end to end with the model call stubbed: no network, no
credits, but every line of request handling actually runs.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from app.imaging import nanobanana
from app.main import app
from app.models import GeneratedView

PIXEL = base64.b64encode(b"not-a-real-jpeg").decode()

CAST = [
    {"name": "Emma Smith", "gender": "female", "age": "24", "skinTone": "fair",
     "hairColor": "dark brown", "hairStyle": "long sleek straight hair",
     "enabled": True},
    {"name": "Liam Jones", "gender": "male", "age": "27", "skinTone": "warm brown",
     "hairColor": "black", "hairStyle": "short neat curls", "enabled": True},
]

SETTINGS = {
    "isModelGenerationEnabled": True,
    "isCloseUpEnabled": True,
    "background": "#ebebeb",
    "gender": "female",
    "age": "young adult",
    "bodyType": "m",
    "aspectRatio": "5:7",
    "resolution": "2K",
    "customPrompt": "TENANT RULES.",
    "personalitiesEnabled": True,
    "personalities": CAST,
    "realisticSkinDetails": True,
}

PRODUCT = {
    "id": "11111111-2222-3333-4444-555555555555",
    "tenantId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "title": "Vintage Hanes White T-Shirt Women L",
    "masterCategory": "Women",
    "category": "T-Shirts & Tops",
    "subCategory": "T-Shirts",
    "mannequinType": "Top",
    "generationStatus": "COMPLETE",
}

MEDIA = [
    {"url": "https://r2.dev/products/a-front-original.jpg", "view": "FRONT",
     "processing": "RAW", "mediaType": "IMAGE", "isCurrent": True, "position": 0},
    {"url": "https://r2.dev/products/a-back-original.jpg", "view": "BACK",
     "processing": "RAW", "mediaType": "IMAGE", "isCurrent": True, "position": 1},
]


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def stub_generation(monkeypatch: pytest.MonkeyPatch):
    """Replace the network entirely: source fetch and the model call.

    `seen` captures the PromptContext the generator was handed, so a test can
    assert which model the tenant's cast supplied without inspecting a prompt.
    """
    seen: dict[str, object] = {}

    def fake_fetch(self, urls, timeout=30.0):
        return {u: b"bytes" for u in urls}

    def fake_generate(self, views, front, back, ctx, additional=None,
                      front_reference=None, preferred_model=None):
        seen["ctx"] = ctx
        seen["preferred_model"] = preferred_model
        # One model for the whole set, as the real generator now guarantees.
        model = preferred_model or "stub-model"
        return [
            GeneratedView(view=v, image_base64=PIXEL, bytes=15, ok=True,
                          model=model)
            for v in views
        ]

    monkeypatch.setattr(nanobanana.NanoBanana, "fetch", fake_fetch)
    monkeypatch.setattr(nanobanana.NanoBanana, "generate", fake_generate)
    # GOOGLE_NANO_BANANA_API_KEY, not GEMINI_API_KEY — generation reads only the
    # former, and setting the wrong one here would make these tests pass against
    # a handler that had regressed to borrowing the evidence layer's key.
    monkeypatch.setenv("GOOGLE_NANO_BANANA_API_KEY", "test-key-a,test-key-b")
    from app.config import settings as settings_fn

    settings_fn.cache_clear()
    yield seen
    settings_fn.cache_clear()


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #

def test_verify_reports_missing_renders(client: TestClient):
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "check_pixels": False,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ai_views"]["missing"] == ["AI_FRONT", "AI_BACK"]
    assert body["generatable"] is True
    assert "IMG.001" in [f["rule_id"] for f in body["findings"]]
    assert [s["view"] for s in body["source_images"]] == ["FRONT", "BACK"]


def test_verify_writes_nothing_and_needs_no_key(client: TestClient):
    """Report-only, and usable with the LLM layer entirely absent."""
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "check_pixels": False, "use_llm": False,
    })
    assert r.status_code == 200
    assert r.json()["llm_calls"] == 0


def test_verify_names_the_originals_that_need_matting(client: TestClient):
    """The exact shape of `c1f34fa6`: the FRONT was matted and its raw upload
    superseded, the BACK never went through the segmenter. The Original toggle
    shows two photographs, the Edited view shows one — and generation would
    otherwise seed from a garment hanging on a stockroom wall."""
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT,
        "media": [
            {"url": "https://r2.dev/products/1-0-processed.png", "view": "FRONT",
             "processing": "BG_REMOVED", "isCurrent": True, "position": 0},
            {"url": "https://r2.dev/products/1-front-original.jpg", "view": "FRONT",
             "processing": "RAW", "isCurrent": False, "position": 0},
            {"url": "https://r2.dev/products/1-back-original.jpg", "view": "BACK",
             "processing": "RAW", "isCurrent": True, "position": 1},
            {"url": "https://r2.dev/products/1-care.jpg", "view": "LABEL",
             "processing": "RAW", "isCurrent": True, "position": 2},
            {"url": "https://r2.dev/size-charts/x.webp", "view": "SIZE_CHART",
             "processing": "RAW", "isCurrent": True, "position": 3},
        ],
        "settings": SETTINGS, "check_pixels": False,
    })
    assert r.status_code == 200
    todo = r.json()["needs_background_removal"]
    # The BACK only. The care label and the size chart are never matted, and the
    # superseded FRONT original already has its counterpart.
    assert [x["view"] for x in todo] == ["BACK"]
    assert "IMG.010" in [f["rule_id"] for f in r.json()["findings"]]


def test_nothing_to_matte_when_every_original_has_a_cutout(client: TestClient):
    """The skip condition: don't pay a provider call to redo finished work."""
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT,
        "media": [
            {"url": "https://r2.dev/products/f.png", "view": "FRONT",
             "processing": "BG_REMOVED", "isCurrent": True, "position": 0},
            {"url": "https://r2.dev/products/b.png", "view": "BACK",
             "processing": "BG_REMOVED", "isCurrent": True, "position": 1},
        ],
        "settings": SETTINGS, "check_pixels": False,
    })
    assert r.status_code == 200
    assert r.json()["needs_background_removal"] == []


def test_a_misfiled_cutout_is_not_offered_for_matting(client: TestClient):
    """5,889 products look like this. Re-matting them costs a provider call and
    erodes a silhouette that was already cut out."""
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT,
        "media": [
            {"url": "https://r2.dev/products/f-original.jpg", "view": "FRONT",
             "processing": "RAW", "isCurrent": True, "position": 0},
            {"url": "https://r2.dev/products/b-original.jpg", "view": "BACK",
             "processing": "RAW", "isCurrent": True, "position": 1},
            {"url": "https://r2.dev/products/0-processed.png", "view": "OTHER",
             "processing": "BG_REMOVED", "isCurrent": True, "position": 2},
            {"url": "https://r2.dev/products/1-processed.png", "view": "OTHER",
             "processing": "BG_REMOVED", "isCurrent": True, "position": 3},
        ],
        "settings": SETTINGS, "check_pixels": False,
    })
    assert r.status_code == 200
    assert r.json()["needs_background_removal"] == []
    assert "IMG.022" in [f["rule_id"] for f in r.json()["findings"]]


def test_verify_is_silent_without_media(client: TestClient):
    r = client.post("/v1/imagery/verify", json={"product": PRODUCT, "media": []})
    assert r.status_code == 200
    assert r.json()["findings"] == []


# --------------------------------------------------------------------------- #
# generate
# --------------------------------------------------------------------------- #

def test_generate_runs_end_to_end(client: TestClient, stub_generation):
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "views": ["AI_FRONT", "AI_BACK"],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [v["view"] for v in body["views"]] == ["AI_FRONT", "AI_BACK"]
    assert all(v["ok"] for v in body["views"])
    assert body["failed"] == []


def test_generate_picks_a_personality_matching_the_product(
    client: TestClient, stub_generation
):
    """The product is Women's, so the male entry in the cast must never win."""
    for _ in range(8):
        r = client.post("/v1/imagery/generate", json={
            "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
            "views": ["AI_FRONT"],
        })
        assert r.status_code == 200
        assert r.json()["image_settings"]["personalityName"] == "Emma Smith"

    ctx = stub_generation["ctx"]
    assert ctx.settings.skin_tone == "fair"
    assert ctx.gender == "female"


def test_generate_reuses_the_model_already_on_the_product(
    client: TestClient, stub_generation
):
    """A view filled in later must show the SAME face as the renders beside it —
    a freshly drawn personality would describe a different one while the
    reference image shows the original."""
    stored = {
        "personalityName": "Liam Jones", "age": "27", "skinTone": "warm brown",
        "hairColor": "black", "hairStyle": "short neat curls",
    }
    r = client.post("/v1/imagery/generate", json={
        "product": {**PRODUCT, "imageSettings": stored},
        "media": MEDIA, "settings": SETTINGS, "views": ["AI_BACK"],
    })
    assert r.status_code == 200
    assert r.json()["image_settings"]["personalityName"] == "Liam Jones"
    assert stub_generation["ctx"].settings.skin_tone == "warm brown"


def test_generate_records_nothing_when_every_view_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stub_generation
):
    """Pinning a model to a product with no renders would attribute a face to
    images that do not exist."""
    monkeypatch.setattr(
        nanobanana.NanoBanana, "generate",
        lambda self, views, front, back, ctx, additional=None,
        front_reference=None, preferred_model=None: [
            GeneratedView(view=v, ok=False, error="FinishReason.IMAGE_OTHER")
            for v in views
        ],
    )
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "views": ["AI_FRONT"],
    })
    assert r.status_code == 200
    assert r.json()["image_settings"] is None
    assert r.json()["failed"] == ["AI_FRONT"]


def test_generate_accepts_footwear_now_that_it_has_its_own_framing(
    client: TestClient, stub_generation
):
    """Was a 409. Footwear is generated again, because the reason it was
    refused — every framing putting the product at the bottom edge of a
    full-body shot — is what the "footwear" garment class now fixes."""
    r = client.post("/v1/imagery/generate", json={
        "product": {**PRODUCT, "category": "Shoes", "subCategory": "Sneakers"},
        "media": MEDIA, "settings": SETTINGS,
    })
    assert r.status_code == 200, r.text


def test_generate_refuses_when_the_tenant_switched_it_off(
    client: TestClient, stub_generation
):
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": MEDIA,
        "settings": {**SETTINGS, "isModelGenerationEnabled": False},
    })
    assert r.status_code == 409
    assert "switched off" in r.json()["detail"]


def test_generate_says_so_when_nothing_is_missing(client: TestClient, stub_generation):
    complete = MEDIA + [
        {"url": f"https://r2.dev/products/{v}.jpg", "view": v,
         "processing": "GENERATED", "mediaType": "IMAGE", "isCurrent": True,
         "position": 5 + i}
        for i, v in enumerate(["AI_FRONT", "AI_BACK"])
    ]
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": complete, "settings": SETTINGS,
    })
    assert r.status_code == 200
    assert r.json()["views"] == []
    assert "Nothing missing" in r.json()["notes"][0]


def test_generate_needs_the_generation_key_not_the_evidence_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Only GOOGLE_NANO_BANANA_API_KEY enables generation.

    GEMINI_API_KEY drives the text and vision evidence layer, which has its own
    quota and its own cost — an image backfill borrowing it could exhaust the
    budget the verification path depends on. With a Gemini key present and the
    generation key absent, this must still refuse.
    """
    from app.config import settings as settings_fn

    monkeypatch.delenv("GOOGLE_NANO_BANANA_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "evidence-layer-key")
    settings_fn.cache_clear()
    try:
        r = client.post("/v1/imagery/generate", json={
            "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
            "views": ["AI_FRONT"],
        })
        assert r.status_code == 503
        assert "GOOGLE_NANO_BANANA_API_KEY" in r.json()["detail"]
    finally:
        settings_fn.cache_clear()


def test_verify_still_works_without_a_generation_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Verification is free and must not depend on the image budget."""
    from app.config import settings as settings_fn

    monkeypatch.delenv("GOOGLE_NANO_BANANA_API_KEY", raising=False)
    settings_fn.cache_clear()
    try:
        r = client.post("/v1/imagery/verify", json={
            "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
            "check_pixels": False,
        })
        assert r.status_code == 200
        assert r.json()["ai_views"]["missing"] == ["AI_FRONT", "AI_BACK"]
    finally:
        settings_fn.cache_clear()


def test_every_view_comes_from_one_model(client: TestClient, stub_generation):
    """A gallery mixing a Gemini front with a gpt-image back shows two different
    people. The set shares a model, and which one is recorded."""
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "views": ["AI_FRONT", "AI_BACK"],
    })
    assert r.status_code == 200
    body = r.json()
    assert len({v["model"] for v in body["views"] if v["ok"]}) == 1
    assert body["image_settings"]["generatedWith"] == "stub-model"


def test_a_gap_fill_prefers_the_model_already_in_the_gallery(
    client: TestClient, stub_generation
):
    """Adding a back view months later must not introduce a second model."""
    r = client.post("/v1/imagery/generate", json={
        "product": {**PRODUCT, "imageSettings": {"generatedWith": "gpt-image-1"}},
        "media": MEDIA, "settings": SETTINGS, "views": ["AI_BACK"],
    })
    assert r.status_code == 200
    assert stub_generation["preferred_model"] == "gpt-image-1"
    assert r.json()["image_settings"]["generatedWith"] == "gpt-image-1"


def test_generate_fails_loudly_when_the_front_cannot_be_downloaded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, stub_generation
):
    """Substituting another view for a missing front is the positional-index bug
    this guards against — the model would be asked for a front view of the
    garment's reverse, and nothing about the output would say so."""
    monkeypatch.setattr(
        nanobanana.NanoBanana, "fetch",
        lambda self, urls, timeout=30.0: {
            u: b"bytes" for u in urls if "front" not in u
        },
    )
    r = client.post("/v1/imagery/generate", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "views": ["AI_FRONT"],
    })
    assert r.status_code == 502
    assert "front photograph" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# The plan, over the wire.
#
# The rules are unit-tested; these assert the endpoint actually SERVES the
# decision and logs it. A plan computed and then dropped on the floor would pass
# every test in test_imagery_rules.py.
# --------------------------------------------------------------------------- #

def test_verify_serves_the_generation_plan(client: TestClient):
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
        "check_pixels": False,
    })
    assert r.status_code == 200
    plan = r.json()["generation_plan"]
    assert plan["should_generate"] is True
    assert plan["views"] == ["AI_FRONT", "AI_BACK"]
    # Both originals are RAW, so they are matted before anything is generated.
    assert [m["view"] for m in plan["matte_first"]] == ["FRONT", "BACK"]
    assert plan["reason"]


def test_verify_plan_declines_a_finished_product(client: TestClient):
    done = MEDIA + [
        {"url": f"https://r2.dev/products/a-{v}.jpg", "view": v,
         "processing": "GENERATED", "mediaType": "IMAGE", "isCurrent": True,
         "position": 5 + i}
        for i, v in enumerate(("AI_FRONT", "AI_BACK"))
    ]
    r = client.post("/v1/imagery/verify", json={
        "product": PRODUCT, "media": done, "settings": SETTINGS,
        "check_pixels": False,
    })
    plan = r.json()["generation_plan"]
    # Renders are done; the RAW originals are still work, so the plan says so
    # rather than reporting the product finished.
    assert plan["views"] == []
    assert plan["should_generate"] is True
    assert [m["view"] for m in plan["matte_first"]] == ["FRONT", "BACK"]


def test_verify_plan_honours_include_advisory(client: TestClient):
    complete = [
        {"url": "https://r2.dev/products/a-front.png", "view": "FRONT",
         "processing": "BG_REMOVED", "mediaType": "IMAGE", "isCurrent": True,
         "position": 0},
        {"url": "https://r2.dev/products/a-back.png", "view": "BACK",
         "processing": "BG_REMOVED", "mediaType": "IMAGE", "isCurrent": True,
         "position": 1},
    ] + [
        {"url": f"https://r2.dev/products/a-{v}.jpg", "view": v,
         "processing": "GENERATED", "mediaType": "IMAGE", "isCurrent": True,
         "position": 5 + i}
        for i, v in enumerate(("AI_FRONT", "AI_BACK"))
    ]
    body = {"product": PRODUCT, "media": complete, "settings": SETTINGS,
            "check_pixels": False}

    assert client.post("/v1/imagery/verify", json=body
                       ).json()["generation_plan"]["should_generate"] is False

    opted = client.post("/v1/imagery/verify",
                        json={**body, "include_advisory": True}).json()
    assert opted["generation_plan"]["should_generate"] is True
    assert set(opted["generation_plan"]["views"]) == {
        "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"
    }


def test_verify_logs_the_decision_under_model_image_verifier(
    client: TestClient, caplog: pytest.LogCaptureFixture
):
    """A verifier that only speaks when something is wrong cannot be told apart
    from one that is not running. Both outcomes log, at INFO, under a name that
    says what it is."""
    with caplog.at_level("INFO", logger="model-image-verifier"):
        client.post("/v1/imagery/verify", json={
            "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
            "check_pixels": False,
        })

    lines = [r for r in caplog.records if r.name == "model-image-verifier"]
    assert len(lines) == 1
    msg = lines[0].getMessage()
    assert PRODUCT["id"] in msg
    assert "GENERATE" in msg
    assert "AI_FRONT,AI_BACK" in msg


# --------------------------------------------------------------------------- #
# The cast, in the log.
#
# "Is it using the models from settings/image-generation?" was unanswerable by
# looking: the name went into `notes` for the caller and nowhere else.
# --------------------------------------------------------------------------- #

def test_generate_logs_which_cast_member_is_wearing_it(
    client: TestClient, stub_generation, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("INFO", logger="model-image-generator"):
        r = client.post("/v1/imagery/generate", json={
            "product": PRODUCT, "media": MEDIA, "settings": SETTINGS,
            "views": ["AI_FRONT"],
        })
    assert r.status_code == 200

    lines = [x.getMessage() for x in caplog.records
             if x.name == "model-image-generator"]
    assert len(lines) == 2, lines

    # The tenant's cast is female-only for this product's gender resolution;
    # either way the NAME has to appear, and it must be one of SETTINGS' cast.
    picked = r.json()["image_settings"]["personalityName"]
    assert picked in {p["name"] for p in CAST}
    assert picked in lines[0]
    assert "CAST pick" in lines[0]
    assert f"from {len(CAST)} enabled personality(ies)" in lines[0]

    # …and the second line names the image model that actually rendered.
    assert "rendered 1/1 view(s)" in lines[1]
    assert picked in lines[1]


def test_generate_logs_a_reused_model_as_reused(
    client: TestClient, stub_generation, caplog: pytest.LogCaptureFixture
):
    """Filling a view beside an existing render must show the SAME face, so the
    product's stored traits win over a fresh draw — and the log says so."""
    with caplog.at_level("INFO", logger="model-image-generator"):
        client.post("/v1/imagery/generate", json={
            "product": {
                **PRODUCT,
                "imageSettings": {
                    "personalityName": "Emma Smith",
                    "skinTone": "fair",
                    "hairColor": "dark brown",
                    "hairStyle": "long sleek straight hair",
                },
            },
            "media": MEDIA, "settings": SETTINGS, "views": ["AI_BACK"],
        })
    first = [x.getMessage() for x in caplog.records
             if x.name == "model-image-generator"][0]
    assert "REUSED from the product (Emma Smith)" in first


def test_generate_says_so_when_the_tenant_has_no_cast(
    client: TestClient, stub_generation, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("INFO", logger="model-image-generator"):
        client.post("/v1/imagery/generate", json={
            "product": PRODUCT,
            "media": MEDIA,
            "settings": {**SETTINGS, "personalitiesEnabled": False},
            "views": ["AI_FRONT"],
        })
    first = [x.getMessage() for x in caplog.records
             if x.name == "model-image-generator"][0]
    assert "NO CAST" in first and "personalities disabled" in first
