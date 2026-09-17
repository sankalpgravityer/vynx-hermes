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

A RENDER DEFECT IS DIFFERENT, AND IT IS FIXED, NOT FLAGGED. The gate judges one
render — the lead — and the other four are seen only here. MID-000253 (Midtex,
production, 17 Sep 2026): the AI_FRONT_34 had the model's knees smeared into a
white blur, plain to anyone who opened the edit screen; the gate passed the
AI_FRONT beside it and this audit wrote "IMAGE DEFECT — AI_FRONT_34 render: AI
artifact on legs" as a soft flag nobody acted on. So an AI render the model
calls defective is `RENDER_DEFECT`, carries the view in `bad_views`, and the
chain's regen step re-renders THAT VIEW ONLY — the same model identity, because
the renderer is handed the product's stored personality and its AI_FRONT as the
reference (`gallery.render_defects: regen`; `hold` makes it a person's decision,
`soft` the old flag). A cut-out or photograph that is not what its slot says
stays `IMAGE_DEFECT` under `gallery.block`: nothing re-renders a photograph.
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
                    # Asked part by part, because "does it match its expectation"
                    # let a sweater with its neckband cut away through (MID-000569).
                    "missing_parts": {
                        "type": "array", "items": {"type": "string"},
                        "description": (
                            "For a GARMENT PHOTOGRAPH with the background removed only: "
                            "each part of the garment that is missing or cut off — "
                            "'collar', 'neckband', 'left sleeve', 'cuff', 'hem', 'strap', "
                            "'waistband'. Check the neckline, both sleeves and the hem "
                            "one by one. Empty when the garment is complete; always "
                            "empty for a render or an unprocessed photograph."
                        ),
                    },
                },
                "required": ["index", "ok", "issue", "missing_parts"],
            },
        },
        # Readiness phase 1: the master category is confirmed against the
        # GARMENT photographs — never against the render, which is the thing
        # under judgement. Same call, three more fields.
        "garment_gender": {
            "type": "string",
            "enum": ["men", "women", "unisex", "unknown"],
            "description": (
                "Who the GARMENT itself is cut for, judged from the garment "
                "photographs only (cut, fit, closures, styling): men, women, "
                "unisex when it could honestly be either (a plain tee, a hoodie), "
                "unknown when the photographs cannot show it. Ignore the model "
                "in any AI render."
            ),
        },
        "garment_type": {
            "type": "string",
            "description": "The kind of garment in one or two words: 'denim jacket', "
                           "'midi dress', 'jeans', 'polo shirt'.",
        },
        "garment_confidence": {"type": "number", "description": "0.0 to 1.0 for garment_gender."},
        # 17 Sep 2026, MID-000569: a close-up re-rendered beside four renders of
        # a blonde woman came back with a dark-haired one. Every render is in
        # this one call, so the question costs nothing extra.
        "same_model": {
            "type": "boolean",
            "description": (
                "true when every AI RENDER in the list shows the SAME person: the "
                "same face, hair colour and style, skin tone and build. true when "
                "there are fewer than two renders. false when one or more renders "
                "show a different person than the others."
            ),
        },
        "odd_renders": {
            "type": "array", "items": {"type": "integer"},
            "description": "When same_model is false: the 1-based index(es) of the "
                           "render(s) showing a DIFFERENT person than the majority. "
                           "Empty otherwise.",
        },
    },
    "required": ["wear", "defects", "wear_confidence", "images",
                 "garment_gender", "garment_type", "garment_confidence",
                 "same_model", "odd_renders"],
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
    "background. Wear on the garment is never an image defect.\n"
    "Separately, say who the GARMENT is cut for from the garment photographs "
    "alone — never from the person in a render. Answer unisex whenever a garment "
    "could honestly be worn by either; reserve men or women for a cut, closure or "
    "styling that plainly belongs to one.\n"
    # MID-000569 (17 Sep 2026): a sweater's ribbed neckband was cut away by the
    # background removal on both FRONT cut-outs and nothing said so.
    "For a background-removed cut-out, a PART OF THE GARMENT MISSING is a "
    "defect: a collar or neckband cut away, a sleeve, cuff or hem eaten by the "
    "mask, a strap gone. The garment's own openings (the neck hole, the space "
    "between sleeve and body) are not. A stand, hanger or hand left in the "
    "picture is a defect too.\n"
    "Finally, look across ALL the AI renders together and say whether they show "
    "the same person; when one shows a different person than the rest, name it "
    "in odd_renders."
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
        # Two cut-outs per garment view (flat lay + booth) and five renders.
        "max_images": 10,
        # A cut-out or photograph that is not what its slot says: IMAGE_DEFECT
        # holds when true, else a soft flag.
        "block": False,
        # An AI RENDER the model calls defective (artefacts, a smeared limb,
        # duplicated garments, clutter): `regen` re-renders that view through
        # the chain's regen step (RENDER_DEFECT); `hold` is IMAGE_DEFECT for a
        # person; `soft` records it and nothing follows.
        "render_defects": "regen",
        # A CUT-OUT the model calls defective (a collar the mask ate, a stand
        # left in): `rematte` re-cuts it from the raw archive through the
        # chain's rematte step (CUTOUT_DEFECT — a flag while
        # `readiness.cutouts.hold` is soft, a hold once it is block); `hold` is
        # a person's IMAGE_DEFECT; `soft` records it and nothing follows.
        "cutout_defects": "rematte",
    },
    "model": None,
}

# The five on-model views in the renderer's order — the order re-rendered
# views are named in. Mirrors `imagery.all_views` in policy.yaml.
_ALL_VIEWS = ["AI_FRONT_34", "AI_BACK_34", "AI_FRONT", "AI_BACK", "AI_CLOSEUP"]

# What a correct image in each slot looks like — the sentence the model is
# given for each picture. Keyed by view; processing decides photo vs cut-out.
_EXPECT_CUTOUT = ("a garment photograph with the background removed: the WHOLE "
                  "garment in frame and sharp with nothing cut away — collar or "
                  "neckband, both sleeves and cuffs, hem all present — background "
                  "fully removed, no hands, hangers, stands or props, not a label "
                  "or tag close-up")
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
            if view in _EXPECT_RENDER:
                m = best(view)
                if m is not None:
                    add(m, "render", _EXPECT_RENDER[view] + _RENDER_TAIL)
                continue
            # EVERY live cut-out of a garment view, not the first by position.
            # A product photographed twice — the flat lay and the booth — has
            # two FRONT cut-outs in its gallery, and MID-000569's broken one
            # (the form's neck and the ribbed collar cut away together) was the
            # second: the audit never saw it and called every cut-out whole.
            rows = [m for m in live if m.get("view") == view]
            rows.sort(key=lambda m: (0 if m.get("processing") == "BG_REMOVED" else 1, m.get("position") or 0))
            cutouts = [m for m in rows if m.get("processing") == "BG_REMOVED"] or rows[:1]
            for m in cutouts:
                if len(chosen) >= int(cfg["gallery"]["max_images"]):
                    break
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
           pol: dict[str, Any] | None = None,
           product_gender: Any = None) -> GateVerdict:
    """The model's answer → a verdict, with no call involved so it tests cold.

    Wear first: the photographs at least `disagreement_steps` WORSE than the
    grade is the finding this module exists for. Then the gallery. Soft flags
    never block; which of the two blocks is policy (`wear.block`, `gallery.block`).

    `product_gender` is the gender the MASTER CATEGORY implies ('men' | 'women'),
    or None for a root that implies none. When the garment photographs say the
    other gender with enough confidence, `readiness.master.photo_check` decides
    whether that is a soft flag or a MASTER_CATEGORY_MISMATCH hold.
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
    #
    # Two kinds of picture, two fates. A PHOTOGRAPH or cut-out that is not what
    # its slot says (`issues`) is IMAGE_DEFECT under `gallery.block`: nothing
    # can re-take a photograph. An AI RENDER the model calls defective
    # (`render_issues`) is a picture the chain can make again — RENDER_DEFECT,
    # the view in `bad_views`, that view alone re-rendered (see the module
    # docstring; `gallery.render_defects`).
    gal_cfg = cfg["gallery"]
    bad_views: list[str] = []
    bad_cutouts: list[str] = []
    render_reasons: list[str] = []
    if gal_cfg["enabled"]:
        photo_issues: list[str] = []
        cutout_issues: list[str] = []
        render_issues: list[str] = []
        renders = [i for i, im in enumerate(images, start=1) if im.get("kind") == "render"]
        for entry in raw.get("images") or []:
            if not isinstance(entry, dict):
                continue
            try:
                idx = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            if not 1 <= idx <= len(images):
                continue
            im = images[idx - 1]
            view = str(im.get("view") or "")
            is_cutout = im.get("kind") != "render" and im.get("processing") == "BG_REMOVED"
            # A garment part the mask ate counts on a cut-out even when the
            # model still called the picture "ok" — the part-by-part question
            # is the one it answers honestly.
            missing = ([str(p).strip() for p in (entry.get("missing_parts") or []) if str(p).strip()]
                       if is_cutout else [])
            if entry.get("ok") is not False and not missing:
                continue
            issue = str(entry.get("issue") or "").strip()[:80]
            if missing:
                issue = (f"{issue}; " if issue else "") + f"{', '.join(missing[:3])} missing"
            issue = issue or "does not match its slot"
            if im.get("kind") == "render":
                render_issues.append(f"{view} render: {issue}")
                if view and view not in bad_views:
                    bad_views.append(view)
                continue
            if is_cutout:
                cutout_issues.append(f"{view} cut-out: {issue}")
                if view and view not in bad_cutouts:
                    bad_cutouts.append(view)
                continue
            photo_issues.append(f"{view} photo: {issue}")

        # ONE PERSON ACROSS THE RENDERS. The gate judges the lead alone and
        # cannot see that the close-up shows somebody else (MID-000569). A
        # render that shows a different person than the majority is a render
        # defect of THAT view — re-rendered against the others as reference.
        # Needs a majority to compare with: three renders or more, and the odd
        # ones fewer than the rest.
        if raw.get("same_model") is False and len(renders) >= 3:
            odd: list[int] = []
            for o in raw.get("odd_renders") or []:
                try:
                    o = int(o)
                except (TypeError, ValueError):
                    continue
                if o in renders and o not in odd:
                    odd.append(o)
            if odd and len(odd) * 2 < len(renders):
                for o in odd:
                    view = str(images[o - 1].get("view") or "")
                    if view in bad_views:
                        continue                 # already refused on its own answer
                    render_issues.append(f"{view} render: a different model than the other renders")
                    if view:
                        bad_views.append(view)
            else:
                soft.append("the renders may not all show the same model (which one is unclear)")

        if photo_issues:
            if gal_cfg["block"]:
                code = code or "IMAGE_DEFECT"
                reasons.extend("IMAGE DEFECT — " + i for i in photo_issues)
            else:
                soft.extend("IMAGE DEFECT — " + i for i in photo_issues)

        if cutout_issues:
            # A cut-out the chain can make again: re-cut from the raw archive
            # (the rematte step). Whether it HOLDS meanwhile follows the
            # cut-out policy of phase 3, `readiness.cutouts.hold` — soft until
            # the per-tenant shadow says otherwise.
            mode = str(gal_cfg.get("cutout_defects") or "rematte").lower()
            if mode == "rematte":
                from app.imaging import cutouts as _cutouts

                if str(_cutouts.config(pol).get("hold") or "soft").lower() == "block":
                    code = code or "CUTOUT_DEFECT"
                    reasons.extend("CUTOUT DEFECT — " + i for i in cutout_issues)
                else:
                    soft.extend("CUTOUT DEFECT — " + i for i in cutout_issues)
            elif mode == "hold":
                code = code or "IMAGE_DEFECT"
                reasons.extend("IMAGE DEFECT — " + i for i in cutout_issues)
                bad_cutouts = []
            else:
                soft.extend("IMAGE DEFECT — " + i for i in cutout_issues)
                bad_cutouts = []

        if render_issues:
            mode = str(gal_cfg.get("render_defects") or "regen").lower()
            if mode == "regen":
                # Named LAST (below), after every hold has had its say: a
                # render defect only names the verdict when nothing holds it
                # for a person.
                render_reasons = ["RENDER DEFECT — " + i for i in render_issues]
            elif mode == "hold":
                code = code or "IMAGE_DEFECT"
                reasons.extend("IMAGE DEFECT — " + i for i in render_issues)
                bad_views = []
            else:
                soft.extend("IMAGE DEFECT — " + i for i in render_issues)
                bad_views = []
    head["bad_views"] = bad_views
    head["bad_cutouts"] = bad_cutouts

    # ---- the master category, against the garment photographs ----------------
    #
    # Readiness phase 1. Only a plain contradiction counts: the master says one
    # gender, the photographs say the OTHER (never unisex or unknown), at or
    # above the policy floor, and only from garment photographs — with no photo
    # in the set the model was looking at renders and its answer is ignored.
    # The render never votes on the record it is judged by.
    from app.readiness import config as readiness_config

    m_cfg = readiness_config(pol)["master"]
    seen_gender = str(raw.get("garment_gender") or "unknown").strip().lower()
    try:
        g_conf: float | None = float(raw.get("garment_confidence"))
    except (TypeError, ValueError):
        g_conf = None
    if (photos and product_gender in ("men", "women") and seen_gender in ("men", "women")
            and seen_gender != product_gender and g_conf is not None
            and g_conf >= float(m_cfg.get("photo_min_confidence") or 0.85)):
        kind = str(raw.get("garment_type") or "").strip()
        text = (f"the garment photographs look like a {seen_gender}'s "
                + (f"{kind} " if kind else "garment ")
                + f"but the master category says {product_gender}")
        if str(m_cfg.get("photo_check") or "soft").lower() == "hold":
            code = code or "MASTER_CATEGORY_MISMATCH"
            reasons.append("MASTER CATEGORY — " + text)
        else:
            soft.append("MASTER CATEGORY — " + text)

    # ---- the render defects, last -------------------------------------------
    #
    # A render defect alone is `regen`: the chain re-renders the view and looks
    # again. Beside a hold for a person (the grade, a photograph, the master)
    # the hold names the verdict, the render reasons ride along, and the views
    # still sit in `bad_views` — regen_views() reads those, whatever the action.
    if render_reasons:
        code = code or "RENDER_DEFECT"
        reasons.extend(render_reasons)

    if reasons:
        return GateVerdict("regen" if code == "RENDER_DEFECT" else "review", code, reasons, soft, **head)
    return GateVerdict("ok", None, [], soft, **head)


def regen_views(verdict: GateVerdict, pol: dict[str, Any] | None = None) -> list[str]:
    """The renders this verdict asks the chain to make again — each named view
    on its own, in the renderer's order, never the whole set.

    A render defect is a property of ONE picture: the model, the gender and the
    build were judged fine on the lead by the gate, so the other views stay and
    the renderer is handed the product's stored personality and its AI_FRONT as
    the identity reference. Empty unless policy says `regen`, or when the audit
    could not run.
    """
    if verdict is None or verdict.unavailable or not verdict.bad_views:
        return []
    if str(config(pol)["gallery"].get("render_defects") or "regen").lower() != "regen":
        return []
    all_views = list(((pol or {}).get("imagery") or {}).get("all_views") or _ALL_VIEWS)
    wanted = {str(v) for v in verdict.bad_views}
    return [v for v in all_views if v in wanted] + sorted(v for v in wanted if v not in all_views)


def rematte_views(verdict: GateVerdict, pol: dict[str, Any] | None = None) -> list[str]:
    """The garment views whose cut-out this verdict asks the chain to re-cut.

    A cut-out the model calls defective — the neckband gone, a stand left in —
    is made again from the raw archive by the rematte step, whatever the
    verdict's action (a flag while `readiness.cutouts.hold` is soft). Empty
    unless policy says `rematte`, or when the audit could not run.
    """
    if verdict is None or verdict.unavailable or not verdict.bad_cutouts:
        return []
    if str(config(pol)["gallery"].get("cutout_defects") or "rematte").lower() != "rematte":
        return []
    return list(dict.fromkeys(str(v) for v in verdict.bad_cutouts))


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #
def judge(media: list[dict[str, Any]], *, grade_severity: Any, grade_label: Any = None,
          pol: dict[str, Any] | None = None, evidence: Any = None,
          api_key: str | None = None, product_gender: Any = None) -> GateVerdict:
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
                     grade_label=grade_label, pol=pol, product_gender=product_gender)
    verdict.cached = hit
    return verdict


def summary(v: GateVerdict) -> str:
    """One line for the step note and the run log.

    Not GateVerdict.summary(): there `review` reads "could not decide", which
    is right for a gate that had no answer and wrong here, where `review` is a
    HOLD with a reason — the photographs contradict the grade. `regen` is a
    render the chain makes again (RENDER_DEFECT).
    """
    head = {"ok": "passed", "regen": "REFUSED", "review": "HELD", "skipped": "skipped"}.get(v.action, v.action)
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
