"""The photo audit: what the garment PHOTOGRAPHS say, as opposed to the render.

The quality gate (quality_gate.py) judges the lead render — the AI picture of
a model wearing the garment. It cannot see wear: a render is generated from the
cut-out and paints the cloth clean. Two of the auditor's checks looked at the
photographs instead, and both were deferred in the parity port on cost:

  GRADE_SUSPECT   the photographs show more wear than the grade admits
                  (`detect_condition_from_images`, `--check-grading`)
  IMAGE_DEFECT    an image in the gallery is not what its slot says it is
                  (`audit_gallery`, `--check-images`, one call PER IMAGE)

This is both, in ONE call per product. The auditor paid a call per image
because it had URLs and nothing else; Hermes has typed media rows, so every
picture can be sent with a sentence saying what it is supposed to be, and the
model answers for all of them at once — the same optimisation the gate made
(seven questions, one call).

WEAR IS READ ON THE SHARED SCALE, NOT THE TENANT'S LADDER. Every tenant's
`Grade` rows carry `severity` in {none, minor, moderate, major}; the ladders
differ in length (BOAS A–D, Bleckmann A–F) but not in that column. So the model
is asked for wear on that four-step scale and the answer is compared with the
record's grade severity: no ladder in the prompt, one cached answer per set of
pictures whatever the tenant, and "two steps worse" means the same thing on
every ladder. The auditor's calibration is kept: flag at ≥ 2 steps, because one
step is the honest disagreement between two people looking at the same jeans.

FLAG, NEVER WRITE. A grade is a commercial decision the tenant made with the
garment in hand; the photographs are evidence against it, not a replacement.
`GRADE_SUSPECT` holds the product for a person (policy `wear.block`), and the
defects the model saw ride along so the person knows where to look. Photos
that look BETTER than the grade are a soft flag — under-grading costs margin,
not trust.

The gallery check is a soft flag by default (`gallery.block: false`): a
background not fully removed or a close-up filed as a front is worth knowing
and not, on its own, worth stopping an approval the pre-flight already passed.
"""
from __future__ import annotations

import logging
from typing import Any

from app.imaging.quality_gate import GateVerdict

log = logging.getLogger("hermes.photo-audit")

# The four steps every tenant's Grade.severity uses, best first.
SEVERITY_SCALE = ["none", "minor", "moderate", "major"]

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "wear": {
            "type": "string",
            "enum": ["none", "minor", "moderate", "major", "unknown"],
            "description": (
                "Overall wear visible on the GARMENT photographs only (the images "
                "described as garment photographs). none = as new, no visible wear; "
                "minor = light wear, no damage; moderate = clear wear or a small "
                "flaw such as a faint mark or slight pilling; major = damage or heavy "
                "wear: holes, tears, stains, missing parts. unknown if the "
                "photographs cannot show it (too small, no garment photograph)."
            ),
        },
        "defects": {
            "type": "array", "items": {"type": "string"},
            "description": "Each visible flaw on the garment in at most six words, "
                           "naming where: 'stain on front hem', 'hole at left cuff', "
                           "'pilling on sleeves', 'fading at shoulders'. Empty if none.",
        },
        "wear_confidence": {"type": "number", "description": "0.0 to 1.0 for the wear judgement."},
        "images": {
            "type": "array",
            "description": "One entry per image, in the order given, judged against the "
                           "expectation stated for it. Wear on the garment is NOT an "
                           "image defect.",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "1-based position in the list."},
                    "ok": {"type": "boolean"},
                    "issue": {"type": "string",
                              "description": "At most eight words when not ok, else empty."},
                },
                "required": ["index", "ok", "issue"],
            },
        },
    },
    "required": ["wear", "defects", "wear_confidence", "images"],
}

SYSTEM = (
    "You inspect second-hand fashion listings before publication. You are given "
    "the product's images, each introduced by a sentence saying what kind of "
    "image it is and what a correct one looks like. Report only what is visibly "
    "there. Be conservative about DEFECTS in an image: call one only when the "
    "problem is plain. Judge WEAR from the garment photographs alone, never from "
    "an AI render, which is generated and shows no wear.\n"
    # Measured on the first calibration run: 11 of 20 products were flagged for
    # 'text overlay' or 'watermark' on renders — the platform's own small badge
    # in a corner, present on every render by design. Not a defect.
    "A small logo badge or watermark in a corner of a render is placed there by "
    "the platform on purpose and is NOT a defect. Text is a defect only when it "
    "is generated into the scene itself — on the garment, the body or the "
    "background. Wear on the garment is never an image defect."
)

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Unlike the gate, a provider failure here does not hold approval by
    # default: the audit adds evidence, it is not the definition of "may this
    # leave Review". true makes it behave like the gate.
    "required": False,
    "wear": {
        "enabled": True,
        "views": ["FRONT", "BACK"],
        "max_images": 2,
        "disagreement_steps": 2,
        "min_confidence": 0.6,
        "block": True,
    },
    "gallery": {
        "enabled": True,
        "views": ["FRONT", "BACK", "AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"],
        "max_images": 8,
        "block": False,
    },
    "model": None,
}

# What a correct image in each slot looks like — the sentence the model is
# given for each picture. Keyed by view; processing decides photo vs cut-out.
_EXPECT_CUTOUT = ("a garment photograph with the background removed: the whole "
                  "garment in frame and sharp, background fully removed, no hands, "
                  "hangers or props, not a label or tag close-up")
_EXPECT_PHOTO = ("a garment photograph as taken: the whole garment in frame and "
                 "sharp, not a label or tag close-up")
_EXPECT_RENDER = {
    "AI_FRONT": "an AI render of a model wearing this garment, seen from the front",
    "AI_BACK": "an AI render of a model wearing this garment, seen from the back",
    "AI_FRONT_34": "an AI render of a model wearing this garment, three-quarter view from the front",
    "AI_BACK_34": "an AI render of a model wearing this garment, three-quarter view from the back",
    "AI_CLOSEUP": "an AI close-up render of this garment's detail on the model",
}
_RENDER_TAIL = ("; a correct one shows one coherent person, the same garment as the "
                "photographs, no duplicated limbs or garments, no text or artefacts")


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    over = (pol or {}).get("photo_audit") or {}
    cfg = {**_DEFAULTS, **{k: v for k, v in over.items() if v is not None and k not in ("wear", "gallery")}}
    cfg["wear"] = {**_DEFAULTS["wear"], **{k: v for k, v in (over.get("wear") or {}).items() if v is not None}}
    cfg["gallery"] = {**_DEFAULTS["gallery"], **{k: v for k, v in (over.get("gallery") or {}).items() if v is not None}}
    return cfg


def severity_index(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    return SEVERITY_SCALE.index(text) if text in SEVERITY_SCALE else None


# --------------------------------------------------------------------------- #
# Which pictures, and what each is supposed to be
# --------------------------------------------------------------------------- #
def select_images(media: list[dict[str, Any]], pol: dict[str, Any] | None
                  ) -> tuple[list[dict[str, Any]], int]:
    """The images to send, in order, each with `expect` and `kind`; and how many
    of them (at the head) are garment photographs the wear read may use.

    Garment photographs first — a cut-out per view where one exists, else the
    raw photo — then the renders, so "the first N images" in the prompt is a
    stable phrase. Deduplicated by URL.
    """
    cfg = config(pol)
    live = [m for m in media
            if (m.get("mediaType") or "IMAGE") == "IMAGE" and m.get("isCurrent", True)
            and not m.get("deletedAt") and m.get("url")]

    def best(view: str) -> dict[str, Any] | None:
        rows = [m for m in live if m.get("view") == view]
        rows.sort(key=lambda m: (0 if m.get("processing") == "BG_REMOVED" else 1, m.get("position") or 0))
        return rows[0] if rows else None

    chosen: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(m: dict[str, Any], kind: str, expect: str) -> None:
        if m["url"] in seen:
            return
        seen.add(m["url"])
        chosen.append({"url": str(m["url"]), "view": str(m.get("view") or ""),
                       "processing": str(m.get("processing") or ""), "kind": kind, "expect": expect})

    photos = 0
    if cfg["wear"]["enabled"]:
        for view in cfg["wear"]["views"]:
            m = best(view)
            if m is None or len(chosen) >= int(cfg["wear"]["max_images"]):
                continue
            add(m, "photo", _EXPECT_CUTOUT if m.get("processing") == "BG_REMOVED" else _EXPECT_PHOTO)
        photos = len(chosen)

    if cfg["gallery"]["enabled"]:
        for view in cfg["gallery"]["views"]:
            if len(chosen) >= int(cfg["gallery"]["max_images"]):
                break
            m = best(view)
            if m is None:
                continue
            if view in _EXPECT_RENDER:
                add(m, "render", _EXPECT_RENDER[view] + _RENDER_TAIL)
            else:
                add(m, "photo", _EXPECT_CUTOUT if m.get("processing") == "BG_REMOVED" else _EXPECT_PHOTO)
    return chosen, photos


def prompt_for(images: list[dict[str, Any]], photos: int) -> str:
    lines = [f"This listing has {len(images)} image(s), described in order:"]
    for i, im in enumerate(images, start=1):
        tag = "GARMENT PHOTOGRAPH" if im["kind"] == "photo" else "AI RENDER"
        lines.append(f"  image {i} — {tag} ({im['view']}): expected to be {im['expect']}.")
    if photos:
        lines.append(f"Judge WEAR from images 1–{photos} only (the garment photographs).")
    else:
        lines.append("There is no garment photograph; answer wear = unknown.")
    lines.append("For every image say whether it matches its expectation, and name the "
                 "problem in a few words when it does not. Answer every field of the "
                 "requested json schema.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The decision, cold
# --------------------------------------------------------------------------- #
def decide(raw: dict[str, Any], *, images: list[dict[str, Any]],
           grade_severity: Any, grade_label: Any = None,
           pol: dict[str, Any] | None = None) -> GateVerdict:
    """The model's answer → a verdict, with no call involved so it tests cold.

    Wear first: the photographs at least `disagreement_steps` WORSE than the
    grade is the finding this module exists for. Then the gallery. Soft flags
    never block; which of the two blocks is policy (`wear.block`, `gallery.block`).
    """
    cfg = config(pol)
    soft: list[str] = []
    reasons: list[str] = []
    code: str | None = None
    try:
        confidence: float | None = float(raw.get("wear_confidence"))
    except (TypeError, ValueError):
        confidence = None

    lead = images[0] if images else {}
    head = {"lead_url": lead.get("url"), "lead_view": lead.get("view"),
            "confidence": confidence, "raw": raw}

    # ---- wear vs grade -------------------------------------------------------
    seen = str(raw.get("wear") or "unknown").strip().lower()
    seen_idx = severity_index(seen)
    grade_idx = severity_index(grade_severity)
    defects = [str(d).strip() for d in (raw.get("defects") or []) if str(d).strip()]
    grade_name = f"grade {grade_label}" if grade_label else "the record's grade"
    wear_cfg = cfg["wear"]
    photos = sum(1 for im in images if im.get("kind") == "photo")

    if wear_cfg["enabled"] and photos:
        if seen_idx is None or confidence is None or confidence < float(wear_cfg["min_confidence"]):
            soft.append("wear unclear from the photographs")
        elif grade_idx is None:
            # No grade to compare with; the defects are still worth a line.
            if defects:
                soft.append(f"photos show {seen} wear: {', '.join(defects[:4])}")
        else:
            steps = int(wear_cfg["disagreement_steps"])
            if seen_idx - grade_idx >= steps:
                text = (f"the photographs show {seen} wear"
                        + (f" ({', '.join(defects[:4])})" if defects else "")
                        + f" but {grade_name} says {SEVERITY_SCALE[grade_idx]}")
                if wear_cfg["block"]:
                    code = "GRADE_SUSPECT"
                    reasons.append("GRADE SUSPECT — " + text)
                else:
                    soft.append("GRADE SUSPECT — " + text)
            elif grade_idx - seen_idx >= steps:
                soft.append(f"photos look better than {grade_name} "
                            f"({seen} wear seen, {SEVERITY_SCALE[grade_idx]} graded) — conservative")

    # ---- the gallery -------------------------------------------------------
    gal_cfg = cfg["gallery"]
    if gal_cfg["enabled"]:
        issues: list[str] = []
        for entry in raw.get("images") or []:
            if not isinstance(entry, dict) or entry.get("ok") is not False:
                continue
            try:
                idx = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            if not 1 <= idx <= len(images):
                continue
            im = images[idx - 1]
            what = ("cut-out" if im.get("processing") == "BG_REMOVED" else
                    "render" if im.get("kind") == "render" else "photo")
            issue = str(entry.get("issue") or "does not match its slot").strip()[:80]
            issues.append(f"{im.get('view')} {what}: {issue}")
        if issues:
            if gal_cfg["block"]:
                code = code or "IMAGE_DEFECT"
                reasons.extend("IMAGE DEFECT — " + i for i in issues)
            else:
                soft.extend("IMAGE DEFECT — " + i for i in issues)

    if reasons:
        return GateVerdict("review", code, reasons, soft, **head)
    return GateVerdict("ok", None, [], soft, **head)


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
def judge(media: list[dict[str, Any]], *, grade_severity: Any, grade_label: Any = None,
          pol: dict[str, Any] | None = None, evidence: Any = None,
          api_key: str | None = None) -> GateVerdict:
    """Judge the product's photographs and gallery. Never raises.

    Same shape as quality_gate.judge on purpose: the chain enforces both the
    same way, and the cache is asked first — a set of pictures already judged
    under this prompt costs nothing.
    """
    cfg = config(pol)
    if not cfg["enabled"]:
        return GateVerdict("skipped", reasons=["disabled in policy (photo_audit.enabled)"])
    images, photos = select_images(media, pol)
    if not images:
        return GateVerdict("skipped", reasons=["no garment photograph or render to judge"])
    lead_url, lead_view = images[0]["url"], images[0]["view"]

    def unavailable(why: str) -> GateVerdict:
        if cfg["required"]:
            return GateVerdict("review", "VISION_UNAVAILABLE", [why],
                               lead_url=lead_url, lead_view=lead_view, unavailable=True)
        # Not required: say so, block nothing. The run's health guard still
        # counts the failure; a real outage stops the run through the gate.
        return GateVerdict("skipped", reasons=[f"could not run — {why}"],
                           lead_url=lead_url, lead_view=lead_view)

    model = cfg.get("model") or (pol or {}).get("llm", {}).get("model_fast") or "gemini-2.5-flash"
    prompt = prompt_for(images, photos)

    from app.llm import cache

    ckey = cache.key("photo_audit", urls=[im["url"] for im in images],
                     text=[model, SYSTEM, prompt, SCHEMA])
    raw = cache.get(ckey, pol)
    hit = raw is not None

    if not hit:
        if evidence is None:
            from app.config import settings

            key = api_key or settings().gemini_api_key
            if not key:
                return unavailable("no GEMINI_API_KEY")
            try:
                from app.llm.gemini import GeminiEvidence
            except ImportError as exc:  # pragma: no cover — SDK optional
                return unavailable(f"google-genai is not installed ({exc})")
            evidence = GeminiEvidence(key, pol or {"llm": {"model_fast": model}})

        urls = [im["url"] for im in images]
        parts = evidence._fetch_images(urls)
        if len(parts) != len(urls):
            # The prompt numbers the images; a missing one would shift every
            # judgement onto the wrong picture. Judge nothing rather than that.
            return unavailable(f"{len(urls) - len(parts)} of {len(urls)} images could not be downloaded")
        raw = evidence._generate(model=model, contents=[*parts, prompt], schema=SCHEMA, system=SYSTEM)
        if raw is None:
            if getattr(evidence, "last_error_kind", None) == "api":
                last = (evidence.errors or ["provider error"])[-1]
                return unavailable(f"vision provider failed ({last[:160]})")
            return GateVerdict("skipped", reasons=["the model returned an unreadable answer"],
                               lead_url=lead_url, lead_view=lead_view)
        cache.put(ckey, raw, pol)

    verdict = decide(raw, images=images, grade_severity=grade_severity,
                     grade_label=grade_label, pol=pol)
    verdict.cached = hit
    return verdict


def summary(v: GateVerdict) -> str:
    """One line for the step note and the run log.

    Not GateVerdict.summary(): there `review` reads "could not decide", which
    is right for a gate that had no answer and wrong here, where `review` is a
    HOLD with a reason — the photographs contradict the grade.
    """
    head = {"ok": "passed", "review": "HELD", "skipped": "skipped"}.get(v.action, v.action)
    line = head + (" — " + "; ".join(v.reasons) if v.reasons else "")
    extras: list[str] = []
    if v.soft:
        extras.append("soft: " + "; ".join(v.soft))
    wear = str((v.raw or {}).get("wear") or "").strip()
    if wear and wear != "unknown" and v.action != "skipped":
        extras.append(f"wear seen: {wear}")
    if extras:
        line += f" ({'; '.join(extras)})"
    return line + (" (cached)" if v.cached else "")
