"""Prompt parity with vnyx-api's `services/banana-nano.ts`.

Renders from this service and from the analyze worker land in the SAME product
gallery. If the two prompts drift, a backfilled product stops looking like the
rest of the shop — a different kind of model, different framing, a different
backdrop — and a customer sees it long before a test would.

So the clauses are pinned. A failure here is not necessarily a bug: it means the
prompt changed, and the question to answer is whether banana-nano.ts changed with
it. Update both together, or neither.

Nothing here calls the model. The failure modes that need a real call — a content
refusal, a transient 504 — are classification, and tested as such at the bottom.
"""

from __future__ import annotations

import io
import random

import pytest

from app.config import policy
from app.imaging.nanobanana import (
    NanoBanana, PromptContext, VIEW_ORDER, VIEW_TO_MEDIA, _is_refusal,
    _is_transient, apply_personality, background_image_prompt, build_prompt,
    choose_personality, classify_garment_type, resolve_aspect_ratio,
    resolve_image_size,
)
from app.models import ImagerySettings


@pytest.fixture(autouse=True)
def no_live_vendor_calls(monkeypatch: pytest.MonkeyPatch):
    """Keep the OpenAI fallback off unless a test explicitly turns it on.

    Not hygiene — a correctness guard. The escalation chain ends in a real HTTP
    call to another vendor, and without this any test that exhausts the Gemini
    models runs it: `test_every_model_refusing_explains_why` did exactly that and
    came back with a live `openai 400`. A test suite that spends money and needs
    the network is a test suite that fails on a plane.

    Tests that DO exercise the fallback override this by patching
    `openai_image.available` themselves — a later monkeypatch wins.
    """
    from app.imaging import openai_image

    monkeypatch.setattr(openai_image, "available", lambda: False)


def ctx(**kw) -> PromptContext:
    base = dict(
        settings=ImagerySettings.model_validate({
            "background": "#ffffff", "gender": "female", "age": "young adult",
            "bodyType": "m", "aspectRatio": "auto", "resolution": "2K",
        }),
        category="T-Shirts & Polos", sub_category="Tank Tops",
        mannequin_type="Top", gender="male",
    )
    base.update(kw)
    return PromptContext(**base)


# --------------------------------------------------------------------------- #
# Garment classification — the signal that drives framing and styling
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("category,sub,expected", [
    ("T-Shirts & Polos", "Tank Tops", "top"),
    ("Bottoms", "Jeans", "bottom"),
    ("Shirts", None, "top"),
    ("Dresses", "Maxi", None),
    # Bottom wins when both match — a "jogger top" is framed as a bottom.
    ("Bottoms", "Top", "bottom"),
])
def test_garment_class(category, sub, expected):
    assert classify_garment_type(category, sub) == expected


def test_category_beats_mannequin_type():
    """mannequinType carries the MODEL type, not the garment: the decision flow
    reports "Men Top" for a pair of jeans. Framing a bottom as a shirt is the
    bug this ordering prevents."""
    prompt = build_prompt("front34", ctx(category="Bottoms", sub_category="Jeans",
                                         mannequin_type="Men Top"), False)
    assert "LOWER-BODY three-quarter shot" in prompt
    assert "knee-up" not in prompt


# --------------------------------------------------------------------------- #
# The pinned clauses
# --------------------------------------------------------------------------- #

def test_front_view_asks_for_head_to_feet():
    prompt = build_prompt("front", ctx(), False)
    assert prompt.startswith("Professional studio photography")
    assert "FULL-LENGTH, full-body shot" in prompt
    assert "do NOT crop out the head or the feet" in prompt
    assert prompt.endswith("Return ONLY the generated image.")


def test_back_view_forces_a_true_180_degree_turn():
    """Handing the model a front reference makes it echo the front-facing pose,
    so the camera position has to be stated in the strongest terms."""
    prompt = build_prompt("back", ctx(), True)
    assert "CRITICAL — REAR (BACK) VIEW" in prompt
    assert "facing directly AWAY from the camera" in prompt
    assert "true 180° rear view" in prompt


def test_three_quarter_crops_the_feet_for_a_top():
    prompt = build_prompt("front34", ctx(), True)
    assert "THREE-QUARTER-LENGTH (knee-up)" in prompt
    assert "FEET are deliberately CROPPED OUT" in prompt


def test_close_up_is_not_a_macro():
    prompt = build_prompt("closeup", ctx(), True)
    assert "medium CLOSE-UP" in prompt
    assert "NOT an extreme macro" in prompt


def test_model_is_always_fully_dressed():
    """Without this the model is rendered in the featured garment alone — a top
    with bare legs."""
    prompt = build_prompt("front", ctx(), False)
    assert "IMPORTANT STYLING" in prompt
    assert "never partially undressed" in prompt
    assert "pair it with simple, plain, neutral-colored bottoms" in prompt


def test_bottoms_get_a_complementary_top_instead():
    prompt = build_prompt("front", ctx(category="Bottoms", sub_category="Jeans"), False)
    assert "pair it with a simple, plain, neutral-colored top" in prompt


def test_product_gender_overrides_the_tenant_default():
    """The tenant default here is female; the product is Men's. A catalog whose
    model contradicts the listing is worse than no render."""
    assert "male fashion model" in build_prompt("front", ctx(gender="male"), False)
    assert "female fashion model" in build_prompt("front", ctx(gender="female"), False)


def test_kids_suppresses_gender_and_traits():
    prompt = build_prompt("front", ctx(mannequin_type="Kids"), False)
    assert "child fashion model" in prompt
    assert "male fashion model" not in prompt


# --------------------------------------------------------------------------- #
# The tenant's cast of models
#
# BOAS configures 20 named personalities. Before these were ported, every render
# came out as a generic model and the whole configuration had no effect —
# a catalog that looks like one person wearing the entire shop.
# --------------------------------------------------------------------------- #

CAST = [
    {"name": "Emma Smith", "gender": "female", "age": "24", "skinTone": "fair",
     "hairColor": "dark brown", "hairStyle": "long sleek straight hair",
     "enabled": True},
    {"name": "Liam Jones", "gender": "male", "age": "27", "skinTone": "warm brown",
     "hairColor": "black", "hairStyle": "short neat curls", "enabled": True},
    {"name": "Retired Ray", "gender": "male", "age": "40", "enabled": False},
    {"name": "Legacy Lee", "age": "30", "skinTone": "olive"},  # no gender set
]


def cast_settings(**kw) -> ImagerySettings:
    base = {
        "background": "#ebebeb", "bodyType": "m", "ethnicity": "any",
        "personalitiesEnabled": True, "personalities": CAST,
    }
    base.update(kw)
    return ImagerySettings.model_validate(base)


def test_traits_reach_the_prompt():
    s = apply_personality(cast_settings(), CAST[0])
    prompt = build_prompt("front", ctx(settings=s, gender="female"), False)
    assert "fair skin tone" in prompt
    assert "dark brown long sleek straight hair hair" in prompt
    # The personality's own age beats the tenant default — a named model's "24"
    # is the point of having one.
    assert "24-year-old" in prompt


def test_a_womens_product_never_draws_a_male_model():
    """Otherwise the catalog shows a man modelling a women's blouse."""
    picks = {
        choose_personality(cast_settings(), "women", random.Random(seed))["name"]
        for seed in range(40)
    }
    assert "Liam Jones" not in picks
    assert picks <= {"Emma Smith", "Legacy Lee"}


def test_a_mens_product_never_draws_a_female_model():
    picks = {
        choose_personality(cast_settings(), "Men", random.Random(seed))["name"]
        for seed in range(40)
    }
    assert "Emma Smith" not in picks
    assert picks <= {"Liam Jones", "Legacy Lee"}


def test_disabled_personalities_are_never_drawn():
    picks = {
        choose_personality(cast_settings(), "men", random.Random(seed))["name"]
        for seed in range(40)
    }
    assert "Retired Ray" not in picks


def test_a_personality_with_no_gender_stays_eligible_for_anything():
    """Legacy entries predate the gender field; excluding them would silently
    shrink the cast."""
    for gender in ("men", "women", None):
        picks = {
            choose_personality(cast_settings(), gender, random.Random(seed))["name"]
            for seed in range(40)
        }
        assert "Legacy Lee" in picks


def test_the_cast_is_ignored_when_the_tenant_switched_it_off():
    off = cast_settings(personalitiesEnabled=False)
    assert choose_personality(off, "women") is None


def test_any_is_never_sent_as_a_literal_trait():
    """'any' is the UI's "no preference". "any skin tone" is an instruction, and
    a worse one than saying nothing."""
    s = apply_personality(cast_settings(), {"skinTone": "any", "hairColor": "  "})
    prompt = build_prompt("front", ctx(settings=s, gender="female"), False)
    assert "any skin tone" not in prompt
    assert "hair" not in prompt.split("IMPORTANT STYLING")[0]


def test_kids_get_no_personality_traits():
    s = apply_personality(cast_settings(), CAST[0])
    prompt = build_prompt("front", ctx(settings=s, mannequin_type="Kids"), False)
    assert "child fashion model" in prompt
    assert "fair skin tone" not in prompt


def test_lighting_and_skin_add_ons():
    on = build_prompt("front", ctx(settings=cast_settings(
        lightingTexture="soft rim light", realisticSkinDetails=True)), False)
    assert "Lighting style: soft rim light." in on
    assert "freckles, pores" in on

    # 'none' is the UI default and must not become "Lighting style: none."
    off = build_prompt("front", ctx(settings=cast_settings(
        lightingTexture="none", realisticSkinDetails=False)), False)
    assert "Lighting style" not in off
    assert "freckles" not in off


def test_the_tenant_custom_prompt_leads():
    """BOAS's is 2,825 characters and expects to lead, exactly as
    banana-nano.ts composes it — putting it last would make renders from this
    path differ from the ones already in the gallery."""
    s = cast_settings(customPrompt="RULES FIRST.")
    prompt = build_prompt("front", ctx(settings=s), False)
    assert prompt.startswith("RULES FIRST. Professional studio photography")


def test_body_type_is_non_negotiable():
    prompt = build_prompt("front", ctx(), False)
    assert "average, balanced proportions with a standard frame (size M)" in prompt
    assert "non-negotiable" in prompt


def test_front_reference_changes_the_identity_clause():
    with_ref = build_prompt("front34", ctx(), True)
    without = build_prompt("front34", ctx(), False)
    assert "EXACT SAME model as shown in the front reference image" in with_ref
    assert "Remember the model's characteristics" in without


# --------------------------------------------------------------------------- #
# The clause with no counterpart in the TypeScript
# --------------------------------------------------------------------------- #

def test_inferred_back_says_the_rear_was_never_photographed():
    """With one photo the references show the front TWICE, and the model will
    read the second as the rear and reproduce a chest print between the shoulder
    blades. Saying the rear is unknown gets a plain back instead of a
    confidently wrong one."""
    prompt = build_prompt("back", ctx(back_inferred=True), True)
    assert "no photograph of the garment's reverse exists" in prompt
    assert "Do NOT copy front-facing detail onto the back" in prompt


def test_the_clause_is_absent_when_a_back_photo_exists():
    assert "reverse exists" not in build_prompt("back", ctx(back_inferred=False), True)


def test_front_views_never_carry_the_inferred_note():
    """It is about the rear. On a front view it is noise the model may act on."""
    assert "reverse exists" not in build_prompt("front", ctx(back_inferred=True), False)


# --------------------------------------------------------------------------- #
# Output controls and ordering
# --------------------------------------------------------------------------- #

def test_background_prompt_mapping():
    assert background_image_prompt("#ffffff") == "solid #ffffff color background"
    assert background_image_prompt("white") == "clean white background"
    assert background_image_prompt("bg_white").startswith("white square")
    assert background_image_prompt(None) == ""


def test_aspect_ratio_resolution():
    supported = policy()["imagery"]["generation"]["supported_aspect_ratios"]
    assert resolve_aspect_ratio("auto", supported) is None
    assert resolve_aspect_ratio(None, supported) is None
    assert resolve_aspect_ratio("3:4", supported) == "3:4"
    # Offered in the UI to mirror the image editor; no native Gemini equivalent.
    assert resolve_aspect_ratio("5:7", supported) == "3:4"
    assert resolve_aspect_ratio("7:11", supported) is None


def test_image_size_defaults_to_2k():
    assert resolve_image_size(None) == "2K"
    assert resolve_image_size("nonsense") == "2K"
    assert resolve_image_size("4k") == "4K"


def test_gallery_order_matches_the_workers():
    """rebuildMediaCache ranks AI views in this sequence; the ¾ views lead so
    they become the product's primary images."""
    assert VIEW_ORDER == ["front34", "back34", "front", "back", "closeup"]
    assert VIEW_TO_MEDIA["front34"] == "AI_FRONT_34"


def test_generate_with_no_recognised_views_is_a_no_op():
    nb = NanoBanana("unused-key", policy())
    assert nb.generate(views=["NOT_A_VIEW"], front=b"", back=None, ctx=ctx()) == []
    assert nb.calls == 0


# --------------------------------------------------------------------------- #
# The generation key
#
# GOOGLE_NANO_BANANA_API_KEY, never GEMINI_API_KEY: separate quotas, separate
# costs, and an image backfill must not be able to exhaust the budget the
# verification path runs on. Comma-separated and rotated, matching
# vnyx-api/src/utils/rate-limiter.ts.
# --------------------------------------------------------------------------- #

def test_keys_rotate_round_robin():
    nb = NanoBanana(["key-a", "key-b", "key-c"], policy())
    assert [nb._next_key() for _ in range(7)] == [
        "key-a", "key-b", "key-c", "key-a", "key-b", "key-c", "key-a",
    ]


def test_a_single_key_is_reused():
    nb = NanoBanana(["only"], policy())
    assert {nb._next_key() for _ in range(5)} == {"only"}


def test_a_bare_string_is_accepted_as_one_key():
    assert NanoBanana("solo", policy()).api_keys == ["solo"]


def test_rotation_is_safe_across_threads():
    """Views generate on a thread pool. An unguarded read-modify-write hands the
    same key to two concurrent calls and skips another — precisely the per-key
    pressure the rotation exists to spread."""
    import collections
    import concurrent.futures

    nb = NanoBanana([f"key-{i}" for i in range(4)], policy())
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        drawn = list(pool.map(lambda _: nb._next_key(), range(400)))

    counts = collections.Counter(drawn)
    assert len(counts) == 4
    # 400 draws over 4 keys, perfectly even under a correct lock.
    assert set(counts.values()) == {100}


def test_the_config_splits_and_trims_a_comma_separated_list(monkeypatch):
    from app.config import settings as settings_fn

    monkeypatch.setenv("GOOGLE_NANO_BANANA_API_KEY", " one , two ,, three ")
    settings_fn.cache_clear()
    try:
        assert settings_fn().nano_banana_keys == ["one", "two", "three"]
    finally:
        settings_fn.cache_clear()


def test_an_unset_generation_key_yields_no_keys(monkeypatch):
    """Generation then answers 503; verification is unaffected."""
    from app.config import settings as settings_fn

    monkeypatch.delenv("GOOGLE_NANO_BANANA_API_KEY", raising=False)
    settings_fn.cache_clear()
    try:
        assert settings_fn().nano_banana_keys == []
    finally:
        settings_fn.cache_clear()


# --------------------------------------------------------------------------- #
# Refusal vs fault — the distinction the caller acts on
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("error,refusal,transient", [
    # A real, measured refusal: a Raptors jersey with a player's name on the back.
    ("FinishReason.IMAGE_OTHER", True, False),
    ("FinishReason.IMAGE_SAFETY", True, False),
    ("PROHIBITED_CONTENT", True, False),
    # A real, measured fault: 504 four seconds into a 25-second request.
    ("ServerError: 504 DEADLINE_EXCEEDED", False, True),
    ("ClientError: 429 RESOURCE_EXHAUSTED", False, True),
    ("ConnectError: connection refused", False, True),
    ("ValueError: bad argument", False, False),
    (None, False, False),
])
def test_error_classification(error, refusal, transient):
    assert _is_refusal(error) is refusal
    assert _is_transient(error) is transient


def test_a_refusal_is_never_retried_as_transient():
    """Repeating an identical refused request gets an identical answer while
    doubling the time the operator waits."""
    assert not _is_transient("FinishReason.IMAGE_OTHER")


# --------------------------------------------------------------------------- #
# Source handling
# --------------------------------------------------------------------------- #

def test_fetch_is_keyed_by_url_so_a_failure_cannot_renumber_the_rest(monkeypatch):
    """The regression this file exists for as much as the prompts.

    A 6 MB front photo truncated mid-download, `fetch` dropped it from a
    positional list, and `buffers[0]` became the BACK photograph — which was then
    sent as the front. Nothing raised and the render looked plausible.

    A dict cannot renumber. The front's absence has to be visible to the caller.
    """
    import app.imaging.nanobanana as nb

    front, back, extra = "https://r2/front.jpg", "https://r2/back.jpg", "https://r2/x.jpg"
    monkeypatch.setattr(
        nb, "fetch_all",
        lambda urls, timeout_s=0, deadline_s=0, client=None: {
            front: None,          # truncated, exactly as observed
            back: b"back-bytes",
            extra: b"extra-bytes",
        },
    )
    monkeypatch.setattr(nb, "compress_source", lambda d: d)

    got = NanoBanana("unused-key", policy()).fetch([front, back, extra])
    assert front not in got
    assert got[back] == b"back-bytes"
    # The back must NOT have slid into the front's place.
    assert got.get(front) != b"back-bytes"


def test_sources_are_compressed_before_upload():
    """6 MB camera originals are what got truncated, and the upload is the
    slowest part of a call."""
    from PIL import Image

    from app.imaging.nanobanana import compress_source

    big = Image.new("RGB", (4000, 3000), (120, 90, 60))
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=100)
    out = compress_source(buf.getvalue())

    assert len(out) < len(buf.getvalue())
    assert max(Image.open(io.BytesIO(out)).size) <= 1536


def test_transparent_source_is_flattened_onto_white():
    """Every part is declared image/jpeg. Sending an alpha channel under that
    label leaves the model to interpret a background that is not there."""
    from PIL import Image

    from app.imaging.nanobanana import compress_source

    img = Image.new("RGBA", (800, 600), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = Image.open(io.BytesIO(compress_source(buf.getvalue())))
    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_undecodable_source_passes_through_untouched():
    from app.imaging.nanobanana import compress_source

    assert compress_source(b"not an image") == b"not an image"


# --------------------------------------------------------------------------- #
# Escalating past a refusal — over the SET, never per view
#
# This used to escalate one view at a time, which is how a DeRozan jersey ended
# up with a Gemini front and a gpt-image back: two different men in one product
# gallery. Worse than a missing image, because it ships looking deliberate.
#
# So a model either renders every requested view or the next one gets a turn.
# --------------------------------------------------------------------------- #

# The SHIPPED chain is deliberately empty — one Gemini attempt, then a different
# vendor (see policy.yaml). The escalation mechanism is unchanged and still has
# to work, so these tests supply their own fallback list rather than depending on
# the configured one: they cover the machinery, and
# `test_shipped_chain_is_one_gemini_then_openai` below covers the configuration.
FALLBACKS = ["gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"]


def chain_policy() -> dict:
    """`policy()` with a two-model fallback chain, for the mechanism tests."""
    import copy

    pol = copy.deepcopy(policy())
    pol["imagery"]["generation"]["fallback_models"] = list(FALLBACKS)
    return pol


def test_shipped_chain_is_one_gemini_then_gpt_image():
    """What we actually ship: ONE Gemini model, then gpt-image for its refusals.

    No intermediate Gemini fallbacks — a refusal is a decision the classifier
    makes about the REQUEST, so every Gemini model gives the same answer and
    trying a second one only spends wall-clock. Changing vendor is the only
    escalation that changes the outcome.

    Safety thresholds are set explicitly instead of left to the API default,
    which is stricter than Google's own consumer surface on exactly the
    garments this catalogue is full of."""
    gen = policy()["imagery"]["generation"]
    assert gen["model"] == "gemini-3-pro-image-preview"
    assert gen["fallback_models"] == []
    assert gen.get("openai_fallback") is True
    assert gen.get("safety_relaxed") is True


def test_shipped_chain_escalates_to_openai_only_with_a_key(monkeypatch):
    """`available()` checks the key exists, so the chain length follows it.

    Without a key the run must stay Gemini-only rather than appending a step
    that can only fail — the deployment that has no OPENAI_API_KEY is a valid
    one, not a misconfiguration to be papered over.
    """
    from app.imaging import openai_image

    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-1.5")

    # Overriding the suite-wide `available -> False` guard, per its docstring.
    monkeypatch.setattr(openai_image, "available", lambda: True)
    assert NanoBanana("k", policy())._model_chain(None) == [
        "gemini-3-pro-image-preview",
        "gpt-image-1.5",
    ]

    monkeypatch.setattr(openai_image, "available", lambda: False)
    assert NanoBanana("k", policy())._model_chain(None) == [
        "gemini-3-pro-image-preview"
    ]


def test_shipped_openai_quality_is_set_explicitly():
    """Unset means "auto", and auto resolves towards the top tier.

    At 1024x1536 that is ~6.2k output tokens a view against ~1.6k for `medium`,
    on the path taken by every garment the primary refused. The tier is a
    budget decision, so it is stated rather than inherited."""
    gen = policy()["imagery"]["generation"]
    assert gen.get("openai_quality") in {"low", "medium", "high"}



def _rig(monkeypatch, behaviour):
    """Stub `_render` with a per-(model, view) behaviour map. Records the calls.

    behaviour: {(model, view): (data, error)} — a missing entry succeeds.
    Anything reachable through `_render` is covered, including the OpenAI vendor.
    """
    calls = []

    def fake(self, view, front, back, ctx_, additional, front_reference, model):
        calls.append({"view": view, "model": model, "had_back": back is not None,
                      "had_reference": front_reference is not None})
        return behaviour.get((model, view), (b"img-" + view.encode(), None))

    monkeypatch.setattr(NanoBanana, "_render", fake)
    from app.imaging import openai_image

    monkeypatch.setattr(openai_image, "available", lambda: False)
    return calls


def _models_used(views):
    return {v.model for v in views if v.ok}


def test_one_model_renders_the_whole_set():
    nb = NanoBanana("k", chain_policy())
    # No stub: every view succeeds on the primary.
    import unittest.mock as mock

    with mock.patch.object(
        NanoBanana, "_render",
        lambda self, v, f, b, c, a, r, model: (b"img", None),
    ):
        out = nb.generate(["AI_FRONT", "AI_BACK"], b"f", b"b", ctx())
    assert [v.view for v in out] == ["AI_FRONT", "AI_BACK"]
    assert _models_used(out) == {nb.model}, "one model for the set"


def test_a_model_that_cannot_do_every_view_hands_the_whole_set_over(monkeypatch):
    """THE REGRESSION. The primary renders the front but refuses the back, so the
    ENTIRE set moves to the fallback — rather than shipping a primary front next
    to a fallback back."""
    primary = chain_policy()["imagery"]["generation"]["model"]
    calls = _rig(monkeypatch, {
        (primary, "back"): (None, "FinishReason.IMAGE_OTHER"),
    })

    out = NanoBanana("k", chain_policy()).generate(
        ["AI_FRONT", "AI_BACK"], b"f", b"b", ctx()
    )
    assert all(v.ok for v in out)
    used = _models_used(out)
    assert len(used) == 1, f"the set must share one model, got {used}"
    assert used == {FALLBACKS[0]}
    # The primary was tried and abandoned; nothing from it was kept.
    assert any(c["model"] == primary for c in calls)


def test_a_front_only_set_recovers_on_the_primary_by_dropping_the_back_photo(
    monkeypatch,
):
    """The cheap recovery, kept — but now inside one model so it cannot split the
    set. A Raptors front renders once the `DEROZAN` back photo leaves the
    request."""
    primary = chain_policy()["imagery"]["generation"]["model"]
    calls = _rig(monkeypatch, {})

    def fake(self, view, front, back, ctx_, additional, front_reference, model):
        calls.append({"view": view, "model": model, "had_back": back is not None})
        if back is not None:
            return None, "FinishReason.IMAGE_OTHER"
        return b"img", None

    monkeypatch.setattr(NanoBanana, "_render", fake)
    out = NanoBanana("k", chain_policy()).generate(["AI_FRONT"], b"f", b"b", ctx())

    assert [v.ok for v in out] == [True]
    assert _models_used(out) == {primary}, "recovered without changing model"
    assert [c["had_back"] for c in calls] == [True, False]


def test_a_back_view_never_drops_its_back_photo(monkeypatch):
    """Dropping it there does not route around the refusal — it invents the
    reverse of a licensed garment."""
    calls = _rig(monkeypatch, {
        (m, "back"): (None, "FinishReason.IMAGE_OTHER")
        for m in [policy()["imagery"]["generation"]["model"], *FALLBACKS]
    })
    NanoBanana("k", chain_policy()).generate(["AI_BACK"], b"f", b"b", ctx())
    assert all(c["had_back"] for c in calls if c["view"] == "back")


def test_views_are_salvaged_across_models_when_none_can_do_the_whole_set(
    monkeypatch,
):
    """A John Cena tee: gemini-2.5-flash rendered the front, another model the
    back, and NEITHER could do both. Keeping one partial reported "could not
    generate the front" while a perfectly good front sat in memory."""
    primary = chain_policy()["imagery"]["generation"]["model"]
    # The primary can do neither; the first fallback only the back; the second
    # only the front.
    _rig(monkeypatch, {
        (primary, "front"): (None, "FinishReason.IMAGE_OTHER"),
        (primary, "back"): (None, "FinishReason.IMAGE_OTHER"),
        (FALLBACKS[0], "front"): (None, "FinishReason.IMAGE_OTHER"),
        (FALLBACKS[1], "back"): (None, "FinishReason.IMAGE_OTHER"),
    })

    nb = NanoBanana("k", chain_policy())
    out = nb.generate(["AI_FRONT", "AI_BACK"], b"f", b"b", ctx())

    assert all(v.ok for v in out), "both views exist across the two models"
    assert {v.model for v in out} == {FALLBACKS[0], FALLBACKS[1]}
    assert any("MIXED MODELS" in e for e in nb.errors), "the mix must be flagged"


def test_consistency_can_be_chosen_over_completeness(monkeypatch):
    """With allow_mixed_models off, a matching set beats a complete one."""
    primary = chain_policy()["imagery"]["generation"]["model"]
    _rig(monkeypatch, {
        (primary, "front"): (None, "FinishReason.IMAGE_OTHER"),
        (primary, "back"): (None, "FinishReason.IMAGE_OTHER"),
        (FALLBACKS[0], "front"): (None, "FinishReason.IMAGE_OTHER"),
        (FALLBACKS[1], "back"): (None, "FinishReason.IMAGE_OTHER"),
    })

    nb = NanoBanana("k", chain_policy())
    nb.cfg = {**nb.cfg, "allow_mixed_models": False}
    out = nb.generate(["AI_FRONT", "AI_BACK"], b"f", b"b", ctx())

    produced = {v.model for v in out if v.ok}
    assert len(produced) == 1, "never more than one model in the set"
    assert sum(1 for v in out if v.ok) == 1, "and therefore incomplete"


def test_a_consistent_complete_set_always_wins(monkeypatch):
    """Salvage is the LAST resort — a model that can do everything is preferred
    even when an earlier one covered some views."""
    primary = chain_policy()["imagery"]["generation"]["model"]
    _rig(monkeypatch, {(primary, "back"): (None, "FinishReason.IMAGE_OTHER")})

    nb = NanoBanana("k", chain_policy())
    out = nb.generate(["AI_FRONT", "AI_BACK"], b"f", b"b", ctx())
    assert {v.model for v in out if v.ok} == {FALLBACKS[0]}
    assert not any("MIXED MODELS" in e for e in nb.errors)


def test_the_most_complete_attempt_wins_when_nothing_can_do_it_all(monkeypatch):
    """Every model refuses the back. The set cannot be consistent AND complete,
    so the best partial is returned rather than nothing."""
    every = [policy()["imagery"]["generation"]["model"], *FALLBACKS]
    _rig(monkeypatch, {(m, "back"): (None, "FinishReason.IMAGE_OTHER")
                       for m in every})

    out = NanoBanana("k", chain_policy()).generate(
        ["AI_FRONT", "AI_BACK"], b"f", b"b", ctx()
    )
    by_view = {v.view: v for v in out}
    assert by_view["AI_FRONT"].ok
    assert not by_view["AI_BACK"].ok
    assert "IMAGE_OTHER" in (by_view["AI_BACK"].error or "")


def test_a_preferred_model_is_tried_first(monkeypatch):
    """A gap-fill keeps whatever made the renders already in the gallery."""
    calls = _rig(monkeypatch, {})
    NanoBanana("k", chain_policy()).generate(
        ["AI_BACK"], b"f", b"b", ctx(), preferred_model=FALLBACKS[0]
    )
    assert calls[0]["model"] == FALLBACKS[0]


def test_the_openai_vendor_is_last_and_only_when_configured(monkeypatch):
    from app.imaging import openai_image

    monkeypatch.setattr(openai_image, "available", lambda: True)
    monkeypatch.setenv("OPENAI_IMAGE_MODEL", "gpt-image-1")
    pol = chain_policy()
    pol["imagery"]["generation"]["openai_fallback"] = True
    chain = NanoBanana("k", pol)._model_chain(None)
    assert chain[0] == policy()["imagery"]["generation"]["model"]
    assert chain[-1] == "gpt-image-1"

    monkeypatch.setattr(openai_image, "available", lambda: False)
    assert "gpt-image-1" not in NanoBanana("k", pol)._model_chain(None)


def test_the_chain_never_repeats_a_model(monkeypatch):
    from app.imaging import openai_image

    monkeypatch.setattr(openai_image, "available", lambda: False)
    chain = NanoBanana("k", chain_policy())._model_chain(FALLBACKS[0])
    assert len(chain) == len(set(chain))
    assert chain[0] == FALLBACKS[0]


def test_the_openai_path_receives_the_front_reference(monkeypatch):
    """Omitting it is what let a gpt-image back view invent a different person
    from the Gemini front standing beside it."""
    from app.imaging import openai_image

    seen = {}

    def fake_generate(prompt, images, aspect_ratio=None, timeout_s=0, quality=None):
        seen["images"] = len(images)
        seen["prompt"] = prompt
        return b"img", None

    monkeypatch.setattr(openai_image, "generate", fake_generate)
    nb = NanoBanana("k", chain_policy())
    data, error = nb._render(
        "back", b"front", b"back", ctx(), [], b"the-front-render", "gpt-image-1"
    )
    assert data == b"img" and error is None
    # front + back + the AI front render.
    assert seen["images"] == 3
    assert "EXACT SAME model as in the front reference image" in seen["prompt"]


@pytest.mark.parametrize("model,is_openai", [
    ("gpt-image-1", True),
    ("gpt-image-1.5", True),
    ("dall-e-3", True),
    ("gemini-3-pro-image-preview", False),
    ("gemini-2.5-flash-image", False),
])
def test_vendor_is_recognised_from_the_model_name(model, is_openai):
    from app.imaging.nanobanana import _is_openai_model

    assert _is_openai_model(model) is is_openai


@pytest.mark.parametrize("ratio,expected", [
    ("5:7", "1024x1536"),     # what BOAS configures
    ("3:4", "1024x1536"),
    ("16:9", "1536x1024"),
    ("1:1", "1024x1024"),
    ("auto", "1024x1536"),
    (None, "1024x1536"),
    ("nonsense", "1024x1536"),
])
def test_aspect_ratio_maps_onto_a_supported_canvas(ratio, expected):
    """gpt-image-1 takes fixed canvases, not arbitrary ratios."""
    from app.imaging.openai_image import resolve_size

    assert resolve_size(ratio) == expected


def test_a_transient_fault_is_not_treated_as_a_refusal(monkeypatch):
    """A 504 says nothing about whether the garment is acceptable, and the
    drop-the-back-photo retry must not burn a call on one."""
    calls = _rig(monkeypatch, {
        (policy()["imagery"]["generation"]["model"], "front"):
            (None, "ServerError: 504 DEADLINE_EXCEEDED"),
    })
    NanoBanana("k", chain_policy()).generate(["AI_FRONT"], b"f", b"b", ctx())
    primary_front = [
        c for c in calls
        if c["view"] == "front"
        and c["model"] == policy()["imagery"]["generation"]["model"]
    ]
    assert len(primary_front) == 1, "no back-photo retry on a transient fault"


# --------------------------------------------------------------------------- #
# A dead socket is not a refusal.
# --------------------------------------------------------------------------- #

def test_a_transport_fault_is_transient_not_a_refusal():
    """THE 10038 BUG, both halves.

    Four views run concurrently. They shared one `httpx.HTTPTransport` — cached
    on the instance instead of built per call — so when the first client closed
    it, the other three lost their sockets and raised

        ReadError: [WinError 10038] An operation was attempted on something
        that is not a socket

    `READTIMEOUT` was in the marker list but `READERROR` was not, so those three
    were classified as REFUSALS. A broken connection therefore read as "the
    model declined this garment", and the caller stopped rather than retrying.
    """
    from app.imaging.nanobanana import _is_transient

    dead_socket = ("ReadError: [WinError 10038] An operation was attempted on "
                   "something that is not a socket")
    assert _is_transient(dead_socket)
    for fault in ("WriteError: connection lost", "PoolTimeout",
                  "ConnectError: refused", "ReadTimeout", "429 RESOURCE_EXHAUSTED"):
        assert _is_transient(fault), fault

    # …and the genuine refusals must NOT become retryable in the process.
    for refusal in ("FinishReason.IMAGE_OTHER", "prompt blocked: BlockedReason.OTHER",
                    "FinishReason.IMAGE_SAFETY"):
        assert not _is_transient(refusal), refusal


def test_every_call_gets_its_own_transport():
    """The fix. A cached HttpOptions handed the same connection pool to every
    concurrent client; the pool closed under whichever threads were still
    reading."""
    nb = NanoBanana("k", policy())
    a, b = nb._http_options(), nb._http_options()
    assert a is not b, "HttpOptions must be built per call, not cached"
    ca, cb = (a.client_args or {}), (b.client_args or {})
    if "transport" in ca:          # only when HERMES_IMAGE_FETCH_IPV4 is on
        assert ca["transport"] is not cb["transport"], "transport must not be shared"
