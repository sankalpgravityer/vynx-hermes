"""The image quality gate: is the on-model render fit to be published?

Every other check in the chain reads columns. None of them looks at the picture
a customer will see. A render can carry a melted face, a floating leg, no model
at all, or a model of the wrong gender — and pass every rule, because the rules
ask whether AI_FRONT exists, not whether it is any good. The auditor this was
ported from caught BOA-003977 (good face, broken lower body) and BOA-005792
(feet distorted and blurred) this way, both of which had cleared every data
check.

ONE Gemini call on the lead render, asked seven things at once — model present,
face intact, model gender, usable lead, body coherent, what is wrong with it,
front or back — rather than four separate calls. That is the auditor's own
optimisation (cut its gate spend ~75%) and it matters here for the same reason:
the gate runs on every product, so its cost is the chain's cost.

THE VERDICT IS AN ACTION, so every caller applies the same policy:

    ok       safe to approve
    regen    the content is wrong — no model, bad face, broken body, wrong
             gender. Hold for a human; a re-render is the likely fix.
    review   the gate could not decide — no answer, image unreachable,
             provider down. `unavailable` says which. Not a judgement about
             the product: the runner retries it instead of holding it.
    skipped  nothing to judge (no render), or the gate is off in policy.

Soft flags — BAD LEAD, an androgynous model — are recorded in `soft` and never
block. They are review hints, and the auditor's calibration is that blocking on
them produced more false holds than catches.

THIS IS ALSO THE FIRST TIME A PHOTOGRAPH CONFIRMS THE PRODUCT'S GENDER. Hermes
derives gender from the master category or the mannequin (approval._plan_gender)
and the renderer picks the model from that derivation. Nothing checked the
result. A gender the picture contradicts is `regen`; a gender the picture cannot
confirm because the call failed is `review` — never approve gender-blind.
"""
from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("hermes.quality-gate")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "model_present": {
            "type": "boolean",
            "description": "A real human is wearing the item — not a flat lay, "
                           "a mannequin or an empty garment.",
        },
        "face_ok": {
            "type": "boolean",
            "description": "false ONLY when a face is shown AND it is corrupted "
                           "(melted, warped, smeared, half-formed). true when the "
                           "face looks natural, and true when no face is shown.",
        },
        "gender": {"type": "string", "enum": ["Men", "Women", "Unknown"],
                   "description": "How the model presents. Unknown if no model "
                                  "or androgynous."},
        "lead_ok": {
            "type": "boolean",
            "description": "A usable primary image. false ONLY for a tag or label "
                           "close-up, a blank or broken image, or clearly not the "
                           "product.",
        },
        "body_coherent": {
            "type": "boolean",
            "description": "false if the human body is anatomically broken: "
                           "missing, garbled, duplicated or floating limbs; "
                           "disconnected, melted or cut-off legs, feet or hands; "
                           "trousers or sleeves ending in nothing; shoes not "
                           "attached to legs; any unnatural distortion. true when "
                           "no model is present.",
        },
        "body_issue": {"type": "string",
                       "description": "At most six words naming the break, else "
                                      "empty."},
        "view": {"type": "string", "enum": ["front", "back"],
                 "description": "front unless the model clearly faces away."},
        "garment": {
            "type": "string",
            "description": "One or two plain words for the main garment being "
                           "sold in this image — 'jeans', 'puffer jacket', "
                           "'t-shirt', 'dress'. Empty if unclear.",
        },
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
    },
    "required": ["model_present", "face_ok", "gender", "lead_ok",
                 "body_coherent", "body_issue", "view", "garment", "confidence"],
}

SYSTEM = (
    "You inspect AI-generated fashion product photography before it is "
    "published to a storefront. You report only what is visibly there. You are "
    "conservative about DEFECTS: call a face or body broken only when the "
    "corruption is plain, and say so through the confidence field when unsure."
)

PROMPT = (
    "This is the LEAD image of an online clothing listing, generated to show a "
    "model wearing the garment. Judge it as a shopper would see it. Answer every "
    "field of the requested json schema."
)

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "required": True,
    "lead_views": ["AI_FRONT", "AI_FRONT_34"],
    "block_on": ["no_model", "bad_face", "broken_body", "gender_mismatch",
                 "category_mismatch"],
    "accessory_terms": [
        "cap", "caps", "beanie", "beanies", "hat", "hats", "gloves", "belt",
        "belts", "scarf", "scarves", "bag", "bags", "backpack", "backpacks",
        "accessories", "accessory", "wallet", "wallets", "socks", "sunglasses",
    ],
    "model": None,
}


@dataclass
class GateVerdict:
    action: str                          # ok | regen | review | skipped
    code: str | None = None              # IMAGE_QUALITY | MODEL_GENDER_MISMATCH | VISION_UNAVAILABLE
    reasons: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    gender_seen: str | None = None       # men | women | None
    view_seen: str | None = None
    lead_url: str | None = None
    lead_view: str | None = None
    unavailable: bool = False            # the gate could not run; retry, do not judge
    confidence: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        return self.action in ("regen", "review")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        """One line for the step note and the run log."""
        head = {
            "ok": "passed",
            "regen": "REFUSED",
            "review": "could not decide",
            "skipped": "skipped",
        }.get(self.action, self.action)
        parts = [head]
        if self.reasons:
            parts.append("; ".join(self.reasons))
        if self.soft:
            parts.append("soft: " + ", ".join(self.soft))
        if self.lead_view:
            parts.append(f"on {self.lead_view}")
        return " — ".join(parts[:2]) + (f" ({'; '.join(parts[2:])})" if len(parts) > 2 else "")


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    over = (pol or {}).get("quality_gate") or {}
    return {**_DEFAULTS, **{k: v for k, v in over.items() if v is not None}}


def pick_lead(media: list[dict[str, Any]], pol: dict[str, Any] | None) -> dict[str, Any] | None:
    """The render a shopper meets first, from the typed media rows.

    ONLY AN AI RENDER. The auditor judged `images[0]`, whatever it was, because
    it had URLs and nothing else. Hermes has `ProductMedia.view`, so the gate
    judges the on-model render by name and never mistakes a flat cut-out or a
    care label for a model shot. A product with no render is `skipped` here —
    the approval pre-flight already refuses it ("missing AI_FRONT"), so the gate
    has nothing to add and no call to spend.
    """
    live = [
        m for m in media
        if (m.get("mediaType") or "IMAGE") == "IMAGE"
        and m.get("isCurrent", True)
        and not m.get("deletedAt")
        and m.get("url")
    ]
    for view in config(pol)["lead_views"]:
        rows = sorted((m for m in live if m.get("view") == view),
                      key=lambda m: m.get("position") or 0)
        if rows:
            return rows[0]
    return None


def is_accessory(*values: Any, pol: dict[str, Any] | None = None) -> bool:
    """Caps, belts and bags are shot flat. NO MODEL is not a defect on them."""
    terms = {str(t).strip().lower() for t in config(pol)["accessory_terms"]}
    for value in values:
        if not value:
            continue
        text = str(value).strip().lower()
        if text in terms or any(text.endswith(" " + t) or text.startswith(t + " ") for t in terms):
            return True
    return False


def _gender_word(value: Any) -> str | None:
    """'men' | 'women' | None from what the model reported."""
    text = str(value or "").strip().lower()
    if text.startswith("women") or text.startswith("female"):
        return "women"
    if text.startswith("men") or text.startswith("male"):
        return "men"
    return None


def garment_family(text: Any, pol: dict[str, Any] | None) -> str | None:
    """Which family — tops, bottoms, dresses, outerwear, footwear, accessories —
    a garment word or a category name belongs to, per `policy.yaml:
    garment_families`. None when no family's tokens appear, or when more than
    one does: this decides nothing on a guess.

    Token match on word stems, longest token first, so 'puffer jacket' lands on
    outerwear before 'jacket' could be read as anything else and 'sweatshirt'
    is not matched by 'shirt' (the whole word is compared, with a plural
    stripped).
    """
    if not text:
        return None
    families = ((pol or {}).get("garment_families") or {})
    words = {w.rstrip("s") for w in re.findall(r"[a-z\-]+", str(text).lower())}
    words |= {w.replace("-", "") for w in words}
    hits: set[str] = set()
    for family, tokens in families.items():
        if family == "adjacent":
            continue
        for tok in tokens or []:
            t = str(tok).lower().rstrip("s")
            if t in words or t.replace("-", "") in words:
                hits.add(family)
                break
    return hits.pop() if len(hits) == 1 else None


def _adjacent(a: str, b: str, pol: dict[str, Any] | None) -> bool:
    pairs = ((pol or {}).get("garment_families") or {}).get("adjacent") or []
    return any({a, b} == {str(x) for x in pair} for pair in pairs if len(pair) == 2)


def decide(raw: dict[str, Any], *, product_gender: str | None,
           accessory: bool, pol: dict[str, Any] | None,
           category: Any = None, subcategory: Any = None) -> GateVerdict:
    """The pure decision, separated from the call so it can be tested cold.

    Order matters and follows the auditor's: NO MODEL first (nothing else can
    be judged without one), then the gender the picture shows against the
    gender the record claims, then face, then body — the render defects —
    then the one DATA question the picture can answer: is this the kind of
    garment the category says? Soft flags never block.
    """
    block_on = set(config(pol)["block_on"])
    seen = _gender_word(raw.get("gender"))
    soft: list[str] = []
    try:
        confidence: float | None = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    view = str(raw.get("view") or "").lower() or None

    def verdict(action: str, code: str | None, reasons: list[str]) -> GateVerdict:
        return GateVerdict(action, code, reasons, soft, seen, view,
                           confidence=confidence, raw=raw)

    if raw.get("model_present") is False:
        if accessory:
            soft.append("no model (accessory, shot flat)")
        elif "no_model" in block_on:
            return verdict("regen", "IMAGE_QUALITY",
                           ["NO MODEL — the lead render shows no person wearing the garment"])
        else:
            soft.append("no model")

    if (product_gender and seen and seen != product_gender
            and "gender_mismatch" in block_on):
        return verdict("regen", "MODEL_GENDER_MISMATCH",
                       [f"the model presents as {seen} but the product is listed "
                        f"as {product_gender}"])

    if raw.get("face_ok") is False and "bad_face" in block_on:
        return verdict("regen", "IMAGE_QUALITY",
                       ["BAD FACE — the model's face is AI-corrupted"])

    if raw.get("body_coherent") is False and "broken_body" in block_on:
        issue = str(raw.get("body_issue") or "").strip()[:60]
        return verdict("regen", "IMAGE_QUALITY",
                       [f"BROKEN BODY — {issue or 'the body is anatomically broken'}"])

    # CATEGORY vs PICTURE (the auditor's CATEGORY_IMAGE_MISMATCH). Not a render
    # defect — the render may be perfect — so it is `review`, not `regen`: a
    # human decides whether the category or the photograph is wrong. Fires only
    # when both sides map to a family, the families differ, and the pair is not
    # one the model routinely conflates (tops/outerwear).
    garment = str(raw.get("garment") or "").strip()
    if garment and "category_mismatch" in block_on:
        fam_seen = garment_family(garment, pol)
        filed_under = subcategory or category
        fam_filed = garment_family(filed_under, pol)
        if (fam_seen and fam_filed and fam_seen != fam_filed
                and not _adjacent(fam_seen, fam_filed, pol)):
            return verdict("review", "CATEGORY_IMAGE_MISMATCH",
                           [f"the render shows {garment} ({fam_seen}) but the "
                            f"product is filed under '{filed_under}' ({fam_filed})"])

    if raw.get("lead_ok") is False:
        soft.append("BAD LEAD — not a usable primary image")
    if raw.get("model_present") is not False and seen is None:
        soft.append("model gender unclear")
    if view == "back":
        # An order problem, not a content problem. WP3's gallery-order repair
        # owns it; recorded so the run log shows how often it happens.
        soft.append("lead shows the back")

    return verdict("ok", None, [])


def judge(media: list[dict[str, Any]], *, gender: Any,
          category: Any = None, subcategory: Any = None,
          pol: dict[str, Any] | None = None,
          evidence: Any = None, api_key: str | None = None) -> GateVerdict:
    """Judge the product's lead render. Never raises.

    `gender` is whatever the record holds — a string, a list, a JSON string —
    and is resolved through the same function the rules use, so the gate cannot
    disagree with GENDER.001 about what the record says.
    """
    cfg = config(pol)
    if not cfg["enabled"]:
        return GateVerdict("skipped", reasons=["disabled in policy (quality_gate.enabled)"])

    lead = pick_lead(media, pol)
    if lead is None:
        return GateVerdict("skipped", reasons=["no on-model render to judge"])
    lead_url, lead_view = str(lead["url"]), str(lead.get("view") or "")

    def unavailable(why: str) -> GateVerdict:
        return GateVerdict("review", "VISION_UNAVAILABLE", [why],
                           lead_url=lead_url, lead_view=lead_view, unavailable=True)

    if evidence is None:
        from app.config import settings

        key = api_key or settings().gemini_api_key
        if not key:
            if cfg["required"]:
                return unavailable("no GEMINI_API_KEY — the gate cannot run, and "
                                   "policy says approval must not proceed without it")
            return GateVerdict("skipped", reasons=["no GEMINI_API_KEY"],
                               lead_url=lead_url, lead_view=lead_view)
        try:
            from app.llm.gemini import GeminiEvidence
        except ImportError as exc:  # pragma: no cover — SDK optional
            return unavailable(f"google-genai is not installed ({exc})")
        evidence = GeminiEvidence(key, pol or {"llm": {"model_fast": "gemini-2.5-flash"}})

    parts = evidence._fetch_images([lead_url])
    if not parts:
        return unavailable(f"lead image could not be downloaded ({lead_url[:80]})")

    model = cfg.get("model") or (pol or {}).get("llm", {}).get("model_fast") or "gemini-2.5-flash"
    raw = evidence._generate(model=model, contents=[*parts, PROMPT],
                             schema=SCHEMA, system=SYSTEM)
    if raw is None:
        if getattr(evidence, "last_error_kind", None) == "api":
            last = (evidence.errors or ["provider error"])[-1]
            return unavailable(f"vision provider failed ({last[:160]})")
        # The provider answered and the answer was unusable. Not an outage, and
        # not a pass either: hold it, and say why.
        return GateVerdict("review", "IMAGE_QUALITY",
                           ["the model returned an unreadable answer"],
                           lead_url=lead_url, lead_view=lead_view)

    from app.rules.gate import resolve_gender

    verdict = decide(raw, product_gender=resolve_gender(gender),
                     accessory=is_accessory(subcategory, category, pol=pol), pol=pol,
                     category=category, subcategory=subcategory)
    verdict.lead_url, verdict.lead_view = lead_url, lead_view
    return verdict
