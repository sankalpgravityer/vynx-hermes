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
from typing import Any, Callable

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
        "model_build": {
            "type": "string", "enum": ["slim", "average", "plus", "unknown"],
            "description": "The model's body build as visibly shown: slim (lean or "
                           "petite frame), average (standard frame), plus (full-"
                           "figured or plus-size). unknown if no model is present or "
                           "the build cannot be told.",
        },
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
        "framing": {
            "type": "string",
            "enum": ["full_body", "cropped_legs", "upper_body", "head_and_shoulders",
                     "close_up", "unknown"],
            "description": "How much of the model the frame holds. full_body: the "
                           "model is shown from head to feet, shoes or feet visible "
                           "at the bottom edge. cropped_legs: the frame cuts the legs "
                           "anywhere above the ankles (knees, thighs). upper_body: the "
                           "frame ends around the waist or hips. head_and_shoulders: a "
                           "portrait crop. close_up: a garment detail with no figure. "
                           "unknown: no model.",
        },
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
    "required": ["model_present", "face_ok", "gender", "model_build", "lead_ok",
                 "body_coherent", "body_issue", "framing", "view", "garment", "confidence"],
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
                 "category_mismatch", "body_size_mismatch", "cropped_model",
                 "bad_composition"],
    # The views that must show the WHOLE model, head to feet. A model cut at
    # the knees on one of these is a framing defect (MODEL_CROPPED) and the
    # view is re-rendered. NOT the three-quarter views: nanobanana.py asks for
    # them as "three-quarter-length (knee-up) — feet cropped out of frame, NOT
    # a full-body shot", so a knee crop there is the design (MID-000247's lead
    # is its AI_FRONT_34, knee-up by intent, while its AI_FRONT is full body).
    # The close-up is a detail; footwear and accessories are framed
    # differently on purpose and are exempt whatever the view.
    "full_body_views": ["AI_FRONT", "AI_BACK"],
    "framing_min_confidence": 0.7,
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
    code: str | None = None              # IMAGE_QUALITY | MODEL_GENDER_MISMATCH | BODY_SIZE_MISMATCH | VISION_UNAVAILABLE
    reasons: list[str] = field(default_factory=list)
    soft: list[str] = field(default_factory=list)
    gender_seen: str | None = None       # men | women | None
    view_seen: str | None = None
    # The model's build as the picture shows it, and the band the garment's
    # size calls for (readiness phase 4). None when not judged.
    build_seen: str | None = None        # slim | average | plus | None
    build_expected: str | None = None
    # How much of the model the frame holds, as the picture shows it.
    framing_seen: str | None = None      # full_body | cropped_legs | upper_body | … | None
    # The renders whose FIGURE does not fill the frame (app/imaging/composition):
    # a pixel test over every view, not only the lead. These are re-rendered
    # one by one; `composition` carries the measurements.
    bad_views: list[str] = field(default_factory=list)
    composition: dict[str, Any] | None = None
    # The CUT-OUTS the photo audit calls defective — a collar the mask ate, a
    # stand left in — re-cut by the chain's rematte step. Photo audit only.
    bad_cutouts: list[str] = field(default_factory=list)
    lead_url: str | None = None
    lead_view: str | None = None
    unavailable: bool = False            # the gate could not run; retry, do not judge
    confidence: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    cached: bool = False                 # answered from app/llm/cache.py, no call made

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
        line = " — ".join(parts[:2]) + (f" ({'; '.join(parts[2:])})" if len(parts) > 2 else "")
        return line + (" (cached)" if self.cached else "")


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


# --------------------------------------------------------------------------- #
# The model's build against the garment's size — readiness phase 4
# --------------------------------------------------------------------------- #
#
# The requirement is plain: an XL garment cannot be shown on a small model.
# Phase 2 made every generation path ask for the size-matched build; this is
# the check that the picture actually came back with it. The gate's one call
# gains one field — `model_build` — and `decide()` compares the band the
# picture shows with the band the size implies. Two bands apart (slim on an XL,
# plus on an XS) is a wrong render and regenerates the set with the right
# build; one band apart is a soft flag, because "average" against "plus" is a
# judgement no two people make the same way. Unknown size, Kids, footwear and
# accessories are never judged: there is no build to expect.

_BUILD_ORDER = ["slim", "average", "plus"]

# Letter sizes onto the six-build scale the prompt knows; wider labels collapse
# onto the ends. Mirrors LETTER_TO_BODY_TYPE in vnyx-api services/body-type.ts.
_LETTER_ALIASES: dict[str, str] = {
    "xxxs": "xs", "xxs": "xs", "xs": "xs", "s": "s", "m": "m", "l": "l",
    "xl": "xl", "xxl": "xxl", "2xl": "xxl", "xxxl": "xxl", "3xl": "xxl", "4xl": "xxl",
}

_WAIST_RE = re.compile(r"^w?\s*(\d{2})(?:\.\d+)?(?:\s*[/x]\s*\d{2})?$", re.IGNORECASE)


def body_size_config(pol: dict[str, Any] | None) -> dict[str, Any]:
    """`readiness.body_size` with its defaults (app/readiness.py owns them)."""
    from app import readiness

    return dict(readiness.config(pol).get("body_size") or {})


def size_band(size: Any, *, gender: str | None = None, bottoms: bool = False,
              pol: dict[str, Any] | None = None) -> str | None:
    """'slim' | 'average' | 'plus' for a garment size, or None when nothing follows.

    A letter size maps through `readiness.body_size.bands`. A waist in inches
    maps through the per-gender waist table — bottoms only, and only with a
    gender, because W32 is a medium man and a plus-size woman. Anything else
    (a bare EU/US number, `Unknown`, empty) is None: the tenant default was
    used to render it and there is nothing to hold it against.
    """
    cfg = body_size_config(pol)
    bands = {str(k).lower(): str(v).lower() for k, v in (cfg.get("bands") or {}).items()}
    text = str(size or "").strip().lower()
    if not text or text == "unknown":
        return None
    letter = _LETTER_ALIASES.get(text.replace(" ", "").replace("-", ""))
    if letter:
        band = bands.get(letter)
        return band if band in _BUILD_ORDER else None
    if bottoms and gender in ("men", "women"):
        m = _WAIST_RE.match(text)
        if m:
            inches = int(m.group(1))
            table = (cfg.get("waist_bands") or {}).get(gender) or {}
            for letter, span in table.items():
                try:
                    lo, hi = int(span[0]), int(span[1])
                except (TypeError, ValueError, IndexError):
                    continue
                if lo <= inches <= hi:
                    band = bands.get(str(letter).lower())
                    return band if band in _BUILD_ORDER else None
    return None


def _build_word(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if text in _BUILD_ORDER else None


def regen_views(verdict: GateVerdict, pol: dict[str, Any] | None) -> list[str]:
    """Which views a refusal should re-render.

    Gender and build are properties of the MODEL, and one model carries the
    whole set — regenerating one view would put a different person in it. So
    those two re-render every view. A defect on the lead (no model, a broken
    face or body) is a property of that picture, and only it is redone. A
    `review` verdict — the gate could not decide, or the category disagrees
    with the picture — regenerates nothing: a person decides.
    """
    if verdict.action != "regen":
        return []
    all_views = list(((pol or {}).get("imagery") or {}).get("all_views")
                     or ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"])
    if verdict.code in ("MODEL_GENDER_MISMATCH", "BODY_SIZE_MISMATCH"):
        return all_views
    # A defect of one picture: the lead the vision gate refused, and every view
    # whose figure does not fill its frame — each re-rendered on its own. The
    # two codes that name OTHER views (the frame test; the photo audit's render
    # defect, whose `lead` is a cut-out) never drag the lead in with them.
    wanted = set(verdict.bad_views)
    if verdict.lead_view and verdict.code not in ("IMAGE_COMPOSITION", "RENDER_DEFECT"):
        wanted.add(verdict.lead_view)
    return [v for v in all_views if v in wanted] + sorted(v for v in wanted if v not in all_views)


def with_composition(verdict: GateVerdict, comp: Any, pol: dict[str, Any] | None) -> GateVerdict:
    """Fold the frame test's findings into the vision verdict.

    A set refusal (gender, build) already re-renders every view and stands as
    it is. Otherwise a view whose figure does not fill the frame is a defect
    of that picture: it joins the refusal — or becomes one, `IMAGE_COMPOSITION`
    — and `regen_views` re-renders it. Behind `bad_composition` in block_on;
    off, it is a soft flag. A frame test that could not download anything says
    so softly and never holds: the vision gate has its own unavailability.
    """
    if comp is None:
        return verdict
    verdict.composition = comp.as_dict()
    if getattr(comp, "unavailable", False):
        verdict.soft.append("frame test: no render could be downloaded")
        return verdict
    bad = list(comp.bad_views)
    if not bad:
        return verdict
    if "bad_composition" not in set(config(pol)["block_on"]):
        verdict.soft.extend(f"frame: {r}" for r in comp.reasons)
        return verdict
    if verdict.code in ("MODEL_GENDER_MISMATCH", "BODY_SIZE_MISMATCH"):
        return verdict
    verdict.bad_views = bad
    if verdict.action == "regen":
        verdict.reasons.extend(f"FRAME — {r}" for r in comp.reasons)
    else:
        verdict.action, verdict.code = "regen", "IMAGE_COMPOSITION"
        verdict.reasons = [f"FRAME — {r}" for r in comp.reasons]
    return verdict


_CROPPED = {
    "cropped_legs": "the legs are cut by the frame",
    "upper_body": "the frame ends at the waist",
    "head_and_shoulders": "only the head and shoulders are in frame",
    "close_up": "it is a close-up, not a figure",
}


def decide(raw: dict[str, Any], *, product_gender: str | None,
           accessory: bool, pol: dict[str, Any] | None,
           category: Any = None, subcategory: Any = None,
           product_build: str | None = None, product_size: Any = None,
           expect_full_body: bool = False) -> GateVerdict:
    """The pure decision, separated from the call so it can be tested cold.

    Order matters and follows the auditor's: NO MODEL first (nothing else can
    be judged without one), then the gender the picture shows against the
    gender the record claims, then the build against the size (readiness
    phase 4), then face, then body — the render defects — then the framing
    (a model cut at the knees on a view that must show the whole figure), then
    the one DATA question the picture can answer: is this the kind of garment
    the category says? Soft flags never block.
    """
    cfg = config(pol)
    block_on = set(cfg["block_on"])
    seen = _gender_word(raw.get("gender"))
    build_seen = _build_word(raw.get("model_build"))
    framing = str(raw.get("framing") or "").strip().lower() or None
    soft: list[str] = []
    try:
        confidence: float | None = float(raw.get("confidence"))
    except (TypeError, ValueError):
        confidence = None
    view = str(raw.get("view") or "").lower() or None

    def verdict(action: str, code: str | None, reasons: list[str]) -> GateVerdict:
        return GateVerdict(action, code, reasons, soft, seen, view,
                           build_seen=build_seen, build_expected=product_build,
                           framing_seen=framing,
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

    # THE BUILD AGAINST THE SIZE (readiness phase 4). Before the face and body
    # defects for the same reason the gender is: a wrong build re-renders the
    # whole set and fixes a bad face with it, so the code should name the fix.
    body_cfg = body_size_config(pol)
    if (product_build and build_seen and body_cfg.get("enabled", True)
            and raw.get("model_present") is not False):
        gap = abs(_BUILD_ORDER.index(build_seen) - _BUILD_ORDER.index(product_build))
        sure = confidence is None or confidence >= float(body_cfg.get("min_confidence") or 0.7)
        size_text = f"size {product_size}" if product_size else "its size"
        if (gap >= int(body_cfg.get("block_on_band_gap") or 2) and sure
                and "body_size_mismatch" in block_on):
            return verdict("regen", "BODY_SIZE_MISMATCH",
                           [f"the model's build reads as {build_seen} but the garment is "
                            f"{size_text} ({product_build})"])
        if gap >= 1:
            soft.append(f"build one band off — model {build_seen}, garment "
                        f"{size_text} ({product_build})"
                        + ("" if sure else f", confidence {confidence:.2f}"))

    if raw.get("face_ok") is False and "bad_face" in block_on:
        return verdict("regen", "IMAGE_QUALITY",
                       ["BAD FACE — the model's face is AI-corrupted"])

    if raw.get("body_coherent") is False and "broken_body" in block_on:
        issue = str(raw.get("body_issue") or "").strip()[:60]
        return verdict("regen", "IMAGE_QUALITY",
                       [f"BROKEN BODY — {issue or 'the body is anatomically broken'}"])

    # THE FRAMING. A model cleanly cut at the knees is anatomically coherent,
    # so nothing above catches it — and on a view that is meant to show the
    # whole figure it is a defect a shopper sees at once (MID-000247: the lead
    # render ends mid-thigh). Only where a full body is expected: the close-up
    # is a detail by design, footwear is framed knee-to-floor, accessories
    # head-and-shoulders — the caller says which, this only judges.
    if (expect_full_body and framing in _CROPPED and raw.get("model_present") is not False):
        sure = confidence is None or confidence >= float(cfg.get("framing_min_confidence") or 0.7)
        where = _CROPPED[framing]
        if sure and "cropped_model" in block_on:
            return verdict("regen", "MODEL_CROPPED",
                           [f"CROPPED — {where}; this view must show the model head to feet"])
        soft.append(f"framing {framing} — {where}"
                    + ("" if sure else f", confidence {confidence:.2f}"))

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
          evidence: Any = None, api_key: str | None = None,
          size: Any = None, kids: bool = False,
          frames: Callable[..., dict[str, bytes | None]] | None = None) -> GateVerdict:
    """Judge the product's lead render — and the frame of every render. Never raises.

    `gender` is whatever the record holds — a string, a list, a JSON string —
    and is resolved through the same function the rules use, so the gate cannot
    disagree with GENDER.001 about what the record says.

    `size` is the garment's final size (readiness phase 4): it becomes the
    build the model should have, unless the product is Kids, footwear or an
    accessory, or the size implies no build. `kids` says the master category
    is a Kids root — a child model has no adult build to be held against.

    `frames` fetches the renders for the frame test (app/imaging/composition);
    None means the network. When a vision double is injected (`evidence`) and
    no fetcher is, the frame test is skipped — a test owns its I/O.
    """
    cfg = config(pol)
    if not cfg["enabled"]:
        return GateVerdict("skipped", reasons=["disabled in policy (quality_gate.enabled)"])
    # Remembered NOW: on a cache miss the real provider is assigned to the same
    # name below, and the frame test must not mistake it for a test double.
    injected = evidence is not None

    lead = pick_lead(media, pol)
    if lead is None:
        return GateVerdict("skipped", reasons=["no on-model render to judge"])
    lead_url, lead_view = str(lead["url"]), str(lead.get("view") or "")

    def unavailable(why: str) -> GateVerdict:
        return GateVerdict("review", "VISION_UNAVAILABLE", [why],
                           lead_url=lead_url, lead_view=lead_view, unavailable=True)

    model = cfg.get("model") or (pol or {}).get("llm", {}).get("model_fast") or "gemini-2.5-flash"

    # THE CACHE IS ASKED FIRST — before a client is built, a key is required or
    # a byte is downloaded. A render already judged under this prompt needs
    # none of those, and the download is where the gate's 8–14 s go. Only the
    # model's raw answer is remembered; `decide()` still runs on every call, so
    # a policy change (what blocks, which families are adjacent) applies to
    # cached answers too.
    from app.llm import cache

    ckey = cache.key("quality_gate", urls=[lead_url], text=[model, SYSTEM, PROMPT, SCHEMA])
    raw = cache.get(ckey, pol)
    hit = raw is not None

    if not hit:
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
        # A parsed answer, and nothing else, is worth remembering: the two
        # returns above are the failures the cache must never serve.
        cache.put(ckey, raw, pol)

    from app.rules.gate import _side_of, resolve_gender
    from app.rules.imagery import is_footwear

    product_gender = resolve_gender(gender)
    accessory = is_accessory(subcategory, category, pol=pol)
    # The build the size calls for — or None, and then nothing is expected.
    product_build: str | None = None
    if not kids and not accessory and not is_footwear(str(category or ""), str(subcategory or "")):
        # (footwear is tested again below for the framing; cheap, and keeps the
        # two exemptions readable on their own.)
        bottoms = (_side_of(str(subcategory or ""), pol or {})
                   or _side_of(str(category or ""), pol or {})) == "bottom"
        product_build = size_band(size, gender=product_gender, bottoms=bottoms, pol=pol)

    # KIDS: a child model, and the model's "Men / Women" reading of a child is
    # not a fact to hold a product on (a girl in a boys' hoodie read as
    # "women" on the phase 4 shadow). Decision 4 says Kids are never held for
    # being Kids, so the gender is recorded as a flag rather than judged; the
    # render defects (no model, face, body) still apply.
    judged_gender = None if kids else product_gender
    # A full figure is expected on the four body views, for garments worn on
    # the body. Footwear renders knee-to-floor and accessories head-and-
    # shoulders on purpose (nanobanana.py framing); the close-up is a detail.
    footwear = is_footwear(str(category or ""), str(subcategory or ""))
    expect_full_body = (lead_view in set(cfg.get("full_body_views") or [])
                        and not accessory and not footwear)
    verdict = decide(raw, product_gender=judged_gender,
                     accessory=accessory, pol=pol,
                     category=category, subcategory=subcategory,
                     product_build=product_build, product_size=size,
                     expect_full_body=expect_full_body)
    if kids and product_gender and verdict.gender_seen and verdict.gender_seen != product_gender:
        verdict.soft.append(f"Kids — model reads as {verdict.gender_seen}, record says "
                            f"{product_gender}; not judged on a child")
    verdict.lead_url, verdict.lead_view = lead_url, lead_view
    verdict.cached = hit

    # THE FRAME OF EVERY RENDER, from the pixels. The vision call above looks
    # at one picture; MID-000247's defect was on the view it never looks at.
    # Runs on the real path always (cache hit or miss) and for a test double
    # only when the test hands over a fetcher.
    if frames is not None or not injected:
        from app.imaging import composition

        if composition.config(pol).get("enabled", True):
            verdict = with_composition(verdict, composition.check(media, pol, fetch=frames), pol)
    return verdict
