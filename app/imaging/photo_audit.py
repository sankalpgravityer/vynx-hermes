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

TWO OF THESE QUESTIONS CARRY A THRESHOLD RATHER THAN A YES/NO (18 Sep 2026,
items 4 and 5 of docs/PICTURE-CHECK-FIXES.md). Both were yes/no questions the
model could only answer by guessing, and both produced flags a person then had
to go and disprove:

  LEFTOVERS   "a stand, hanger or hand left in the picture is a defect too"
              asked nothing about how much, so MID-000591 FRONT reported a
              hanger on a cut-out with none in it. Now measured per image —
              `leftovers: [{part, extent}]`, extent none|slight|clear — and
              only `clear` is a defect. MID-000521's podium is what `clear`
              means; a hook tip at a collar is `slight` and rides in the
              record without a flag.
  THE MODEL   `same_model` was asked across every render, so a back view was
              compared on hair and build. Every false positive (BOA-006151's
              AI_BACK and AI_BACK_34, BOA-006153's AI_CLOSEUP) was a faceless
              render. Now `face_visible` is asked per render and the
              comparison is drawn across those alone — MID-000569's close-up,
              which had a face and a different woman in it, is still caught.

Both are one schema revision because each one invalidates the photo-audit
cache for every product (~$0.009 a head). `decide()` still reads an answer
cached under the older schema: no `leftovers` key means no leftovers, no
`face_visible` means the face is unknown, and each falls back to the behaviour
that answer was written under rather than failing.
"""
from __future__ import annotations

import logging
import re
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
                    # 18 Sep 2026, MID-000591 FRONT: "hanger visible" on a
                    # cut-out with no hanger in it. The prompt said "a stand,
                    # hanger or hand left in the picture is a defect too" and
                    # set no threshold, so a hook tip at the neckline — or a
                    # shadow read as one — satisfied the sentence. So the
                    # leftover is now MEASURED, not merely named: only `clear`
                    # is a defect, `slight` is recorded and nothing follows.
                    # The bar is MID-000521, whose four cut-outs stand on a
                    # podium nobody could call ambiguous (§1.1) — that is what
                    # `clear` has to keep catching.
                    "leftovers": {
                        "type": "array",
                        "description": (
                            "For a GARMENT PHOTOGRAPH with the background removed only: "
                            "each thing in the picture that is NOT the garment — a "
                            "hanger, a hanger hook, a stand or podium, a mannequin part, "
                            "a hand, a clip — with how much of it is visible. Empty when "
                            "there is nothing but garment; always empty for a render or "
                            "an unprocessed photograph."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "part": {
                                    "type": "string",
                                    "description": "What it is, in one or two words: 'hanger', "
                                                   "'hook', 'stand', 'podium', 'hand', 'clip'.",
                                },
                                "extent": {
                                    "type": "string",
                                    "enum": ["none", "slight", "clear"],
                                    "description": (
                                        "How much of it is in the picture. none = not there "
                                        "at all. slight = a trace you would have to look for: "
                                        "the tip of a hanger hook above the collar, a sliver "
                                        "at an edge, a shadow that might be one. clear = "
                                        "plainly and wholly there: a whole hanger in frame, a "
                                        "podium or stand the garment is standing on, a hand "
                                        "holding it up. When you are unsure, answer slight."
                                    ),
                                },
                            },
                            "required": ["part", "extent"],
                        },
                    },
                    # 21 Sep 2026, MID-000475 AI_FRONT_34: the model stands in
                    # front of a backdrop sheet with the studio's ceiling beams,
                    # a clamp and warehouse shelving plainly behind it, and the
                    # audit answered ok = true. It had never been asked about the
                    # background: `_RENDER_TAIL` said "no text or artefacts", and
                    # a photographic studio is neither. Measured like the
                    # leftovers above, for the same reason — "is the background
                    # clean" with no threshold would flag every soft shadow.
                    "scene": {
                        "type": "string",
                        "enum": ["plain", "minor", "cluttered"],
                        "description": (
                            "For an AI RENDER: what is BEHIND the person. plain = a plain "
                            "studio backdrop or wall of any colour, including a soft "
                            "gradient, the model's own shadow, or a seamless sweep. minor = "
                            "a faint seam, join or scuff you would have to look for. "
                            "cluttered = anything a shopper would see is not a studio "
                            "backdrop: photographic equipment (a backdrop frame or rig, a "
                            "stand, clamp, peg, light, tripod, cable), a ceiling, beams or "
                            "pipes, shelving, furniture, a doorway, another person, a "
                            "second garment floating in the scene, or any recognisable "
                            "object. When you are unsure, answer minor. Always plain for a "
                            "garment photograph."
                        ),
                    },
                    # 21 Sep 2026, MID-000425 AI_FRONT_34: the trousers run down
                    # and the picture ends mid-shin, no feet — "legs missing in
                    # the middle". The GATE already owns this rule
                    # (`full_body_views`) but judges the LEAD render only, so
                    # AI_BACK's framing and the three-quarter views' were never
                    # looked at. Asked here of every render.
                    "framing": {
                        "type": "string",
                        "enum": ["feet", "lower_leg", "knee", "thigh", "waist_up", "detail"],
                        "description": (
                            "For an AI RENDER: follow the body DOWN to the BOTTOM EDGE of "
                            "the picture and say what the edge cuts through. Do not reason "
                            "from the kind of shot it looks like — read the edge. "
                            "feet = shoes or bare feet are visible in the picture. "
                            "lower_leg = the legs run below the knee and the picture ends at "
                            "the calf, shin, ankle or trouser hem, with NO shoes or feet "
                            "shown. knee = the edge is at the knee or within a hand's width "
                            "of it. thigh = the edge is between the hip and above the knee. "
                            "waist_up = the edge is at the waist or higher. detail = a crop "
                            "of the garment with no figure to speak of. Always detail for a "
                            "garment photograph."
                        ),
                    },
                    # 21 Sep 2026, MID-000382 AI_BACK: the model's lower leg
                    # dissolves into the backdrop between the shorts and the
                    # shoe. "no duplicated limbs" was in the expectation; a limb
                    # going MISSING was not, and the picture passed.
                    "body_flaw": {
                        "type": "string",
                        "enum": ["none", "slight", "clear"],
                        "description": (
                            "For an AI RENDER: whether the person's body is broken WITHIN "
                            "the frame — a leg or arm that fades, dissolves or stops before "
                            "it should, a hand or foot melted or missing, a limb duplicated "
                            "or bent the wrong way, a head or torso that does not join up. "
                            "A body part simply CROPPED by the edge of the picture is NOT a "
                            "flaw: a close-up ends at the chest and a three-quarter view may "
                            "end at the thigh, both correct. none = the body in frame is "
                            "whole. slight = a soft or indistinct edge you would have to "
                            "look for. clear = plainly wrong: a leg that vanishes mid-calf "
                            "with the shoe still there, a missing or melted hand. When you "
                            "are unsure, answer slight. Always none for a garment photograph."
                        ),
                    },
                    # 18 Sep 2026, BOA-006151 and BOA-006153: every render ever
                    # named in `odd_renders` falsely was one with no face in it
                    # (AI_BACK, AI_BACK_34, AI_CLOSEUP). Asked for "the same
                    # face, hair colour and style, skin tone and build", the
                    # model has no face on a back view and falls back to hair
                    # and build, where a back view of the SAME person
                    # legitimately looks unlike the front. So the face is asked
                    # for per render and the comparison is drawn only across
                    # the renders that have one (§1.4).
                    "face_visible": {
                        "type": "boolean",
                        "description": (
                            "For an AI RENDER: true when the model's FACE is visible and "
                            "identifiable in THIS picture — eyes, nose and mouth in frame. "
                            "false for a back view, a head cropped out of frame, a face "
                            "turned away, or a detail crop of the garment. Always false "
                            "for a garment photograph."
                        ),
                    },
                },
                "required": ["index", "ok", "issue", "missing_parts", "leftovers",
                             "scene", "body_flaw", "framing", "face_visible"],
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
        #
        # 18 Sep 2026: the question is now asked ONLY of the renders with a
        # face in them (`face_visible` above). MID-000569 was a close-up WITH a
        # face, so it survives; BOA-006151's two back views and BOA-006153's
        # close-up, which had none, stop being answered at all.
        "same_model": {
            "type": "boolean",
            "description": (
                "true when every AI RENDER IN WHICH A FACE IS VISIBLE shows the SAME "
                "person: the same face, and with it the same hair colour and style, "
                "skin tone and build. Renders with NO face visible (a back view, a "
                "detail crop) are NOT compared and never make this false. true when "
                "fewer than two renders show a face. false only when one or more "
                "renders WITH A FACE show a different person than the others."
            ),
        },
        "odd_renders": {
            "type": "array", "items": {"type": "integer"},
            "description": "When same_model is false: the 1-based index(es) of the "
                           "render(s) showing a DIFFERENT person than the majority. "
                           "Only a render whose face_visible is true may be named "
                           "here — never a back view or a detail crop. Empty otherwise.",
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
    "between sleeve and body) are not.\n"
    # MID-000591 FRONT (18 Sep 2026) came back "hanger visible" on a cut-out
    # with no hanger anywhere in it. The sentence that used to stand here — "a
    # stand, hanger or hand left in the picture is a defect too" — asked HOW
    # MUCH of nothing, so a hook tip at the neckline, or a shadow the model
    # read as one, satisfied it and became a CUTOUT DEFECT a person had to
    # read. The threshold is set where the real case sits: MID-000521's four
    # cut-outs stand on a podium (§1.1), and nobody would call that slight.
    "Anything left in a cut-out that is not the garment — a hanger, a hanger "
    "hook, a stand or podium, a mannequin part, a hand, a clip — goes in "
    "`leftovers` with HOW MUCH of it you can see. The tip of a hanger hook "
    "showing above the collar, a sliver at an edge, or a shadow you think might "
    "be one, is `slight`. A WHOLE HANGER in the frame, a podium or stand the "
    "garment is standing on, or a hand holding the garment up, is `clear`. "
    "ONLY `clear` is a defect. When the most you can see is `slight`, record it "
    "in `leftovers` and do NOT mention it in `issue` — answer ok = true unless "
    "something else is wrong with the picture. When you are unsure how much is "
    "visible, it is `slight`.\n"
    # BOA-006151 (odd_renders [4, 6] → AI_BACK, AI_BACK_34) and BOA-006153
    # (odd_renders [7] → AI_CLOSEUP), 18 Sep 2026: every false positive this
    # question has ever produced was a render with NO FACE in it. Asked for the
    # same face, hair, skin tone and build, the model has no face to compare on
    # a back view, falls back to hair and build, and guesses — and a back view
    # of the same person legitimately looks unlike the front. MID-000569, the
    # one true case, was a close-up WITH a visible face, so faces alone keep it.
    # 21 Sep 2026. Both of these are measured with an extent, like the leftovers
    # above, because the conservatism this prompt asks for ("call one only when
    # the problem is plain") is what let them through: with no scale to answer
    # on, a studio rig behind the model was not "plain" enough to be an artefact.
    "For EVERY AI render, two more things, each answered on its own scale and "
    "never left to the `issue` line alone.\n"
    "`scene` — what is BEHIND the person. These renders are studio pictures: a "
    "plain backdrop, any colour, with the model's shadow or a soft gradient, is "
    "`plain` and correct. A photographic studio SEEN AS ONE is not: a backdrop "
    "frame or sheet with its rig showing, a clamp or peg, a light or tripod, a "
    "ceiling, beams, pipes, shelving, furniture, a doorway, another person, or a "
    "second garment floating behind the model is `cluttered`, and a defect. A "
    "faint seam or scuff is `minor` and is not.\n"
    "`framing` — follow the body DOWN to the BOTTOM EDGE of the picture and "
    "report what that edge cuts through: the feet are shown, or the edge is "
    "through the lower leg (below the knee, no shoes in frame), at the knee, at "
    "the thigh, at the waist or above, or it is a detail crop with no figure. "
    "Read the edge itself — not the kind of shot it resembles. A three-quarter "
    "view is not automatically a thigh crop; if the trousers run down to the "
    "ankles and the shoes are missing, that is the lower leg. Whether the place "
    "it ends is the RIGHT place for this view is not your judgement to make.\n"
    "`body_flaw` — whether the person is WHOLE within the frame. A leg that "
    "fades out mid-calf while the shoe is still there, an arm ending in nothing, "
    "a melted hand, a limb bent the wrong way: `clear`, and a defect. What the "
    "EDGE of the picture cuts off is never a flaw — a close-up ends at the "
    "chest, a three-quarter view may end at the thigh, and both are correct "
    "framing. Unsure is `slight`, and `slight` is not a defect.\n"
    "Finally, the person in the renders. For EVERY AI render say whether the "
    "model's FACE is visible in it (`face_visible`): a back view, a detail crop "
    "of the garment, a head out of frame or a face turned away is false. Then "
    "compare ONLY the renders where a face is visible and say in same_model "
    "whether they show the same person, naming any that does not in "
    "odd_renders. A render with no face is NEVER compared and is NEVER odd — a "
    "back view or a detail crop is not judged on hair, build or clothing. When "
    "fewer than two renders show a face there is nothing to compare: answer "
    "same_model = true and leave odd_renders empty."
)

# The words a leftover goes by, for the one job of recognising that a free-text
# `issue` is about a leftover the model measured as less than `clear` — so the
# threshold above cannot be walked around by writing "hanger visible" in the
# issue line instead (MID-000591's exact shape). Matched whole-word, so
# "standing" and "handle" do not count as a stand or a hand.
_LEFTOVER_RE = re.compile(
    r"\b(hangers?|hooks?|stands?|podiums?|plinths?|mannequins?|dress forms?|"
    r"hands?|clips?|pegs?|props?|tripods?|rails?|racks?)\b")

# The same job for the scene and the body (21 Sep 2026): what the model has
# already said in its own `issue` line, so the measurement does not say it
# twice. Whole-word for the reason above — a substring test reads "garment" as
# an arm and "allege" as a leg, and the row then loses the sentence instead.
_SCENE_SAID_RE = re.compile(
    r"\b(backgrounds?|backdrops?|clutter\w*|studios?|scenes?|ceilings?|beams?|"
    r"shelv\w*|equipment|furniture|rooms?|walls?)\b")
_BODY_SAID_RE = re.compile(
    r"\b(legs?|arms?|hands?|limbs?|feet|foot|knees?|bod(?:y|ies)|torsos?|"
    r"missing|dissolv\w*|melt\w*|smear\w*|deformed|distorted)\b")


def _without_leftovers(issue: str) -> str:
    """`issue` with the clauses that are about a leftover struck out.

    The model answers twice about the same thing: once as a measurement
    (`leftovers`, with an extent) and once as free text (`issue`). When the two
    disagree the MEASUREMENT wins, because it is the only one of the two that
    was asked how much. Called only for a cut-out whose measured leftovers are
    all below `clear`; the clauses that survive (a collar cut away, a blurred
    picture) are untouched.
    """
    kept = [c.strip() for c in re.split(r"[;,]", issue)
            if c.strip() and not _LEFTOVER_RE.search(c.lower())]
    return "; ".join(kept)

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
        # 21 Sep 2026 — WHERE THE FRAME MAY CUT THE MODEL, per view. The gate
        # owns this rule for the one render it judges (`quality_gate`'s
        # `full_body_views`); these are the same words applied to all five.
        #
        #   AI_FRONT / AI_BACK    the whole figure, feet in frame
        #   the three-quarter views  knee-up by design (nanobanana.py asks for
        #                         exactly that) — so a cut THROUGH the lower leg
        #                         is the defect: the legs stop part-way and the
        #                         feet are gone. A thigh or knee crop is right.
        #   AI_CLOSEUP            a detail; never judged on framing
        #
        # Footwear and accessories are exempt whatever the view, as in the gate:
        # they are framed around the product on purpose. A BOTTOM is the other
        # way — a three-quarter view cropped at the thigh hides half the garment
        # — so every body view of one must show the feet.
        "framing": "defect",              # defect | soft | off
        "full_body_views": ["AI_FRONT", "AI_BACK"],
        "half_body_views": ["AI_FRONT_34", "AI_BACK_34"],
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
# 21 Sep 2026, MID-000421: a re-rendered close-up came back showing the model's
# BACK, and this table let it pass — "this garment's detail on the model" says
# nothing about which side, so the answer was honest. Which SIDE each slot holds
# is now spelled out, because a picture of the back filed as a close-up is the
# same fault as one filed as AI_FRONT, and only the slot's own sentence can say
# so. (The renderer's own prompt had the identical hole — see nanobanana.py.)
_EXPECT_RENDER = {
    "AI_FRONT": "an AI render of a model wearing this garment, facing the camera, seen from the front",
    "AI_BACK": "an AI render of a model wearing this garment, turned away, seen from behind",
    "AI_FRONT_34": ("an AI render of a model wearing this garment, facing the camera, cropped "
                    "around the knee — a three-quarter LENGTH shot, not an angled view"),
    "AI_BACK_34": ("an AI render of a model wearing this garment, turned away, cropped around "
                   "the knee — a three-quarter LENGTH shot, not an angled view"),
    # Replaced per product — see `_closeup_expectation`. The entry is kept so
    # the table still answers for every view.
    "AI_CLOSEUP": "an AI close-up render of this product's own detail on the model",
}

# WHICH SIDE A CLOSE-UP MUST SHOW DEPENDS ON THE GARMENT, and 21 Sep 2026 is
# when that stopped being an afterthought. The sentence written for an upper —
# "a picture of the model's BACK does not belong in this slot" — was applied to
# everything, and KLE-000030, a pair of trousers, was refused for a close-up of
# its seat. For a bottom that is the right picture: the renderer has a
# `closeup_back` shot for precisely it, and front-versus-back on a cropped
# trouser crop is a call the model should not be asked to make.
#
# So the demand is made ONLY where it is meaningful: an upper garment, whose
# close-up is the neckline and chest. Unknown category behaves like "not an
# upper" — the check that produced the false positive is the one that has to
# earn its place.
_EXPECT_CLOSEUP_UPPER = (
    "an AI close-up render of the FRONT of this garment on the model — the neckline, "
    "chest and the garment's own detail, with the model facing the camera. A picture "
    "of the model's BACK does not belong in this slot")
_EXPECT_CLOSEUP_OTHER = (
    "an AI close-up render of this product's own detail as worn — for a bottom the "
    "waistband, pockets, fly or seat; for footwear the shoes; for an accessory the item "
    "itself. EITHER side is correct for this product, so do not report the side")


def _closeup_expectation(side: str | None) -> str:
    """The AI_CLOSEUP sentence for a garment on this side of the body."""
    return _EXPECT_CLOSEUP_UPPER if side == "upper" else _EXPECT_CLOSEUP_OTHER


def garment_side(category: Any, subcategory: Any, pol: dict[str, Any] | None) -> str | None:
    """'upper' | 'bottom' | None, from the TENANT's own `sizing.sides` table.

    The same table SIZE.014 and the gate's build check read, so a tenant whose
    categories are named their way resolves the same everywhere. None when
    nothing was passed or nothing matched — and None is not "upper": the
    close-up's front requirement is made only where it is known to apply.
    """
    if not (category or subcategory):
        return None
    from app.rules.gate import _side_of

    return (_side_of(str(subcategory or ""), pol or {})
            or _side_of(str(category or ""), pol or {}))
_RENDER_TAIL = ("; a correct one shows one coherent person, whole within the frame, "
                "wearing the same garment as the photographs, against a plain studio "
                "backdrop — no duplicated or missing limbs, no second garment, no "
                "studio equipment, ceiling or furniture behind them, no text or artefacts")


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    over = (pol or {}).get("photo_audit") or {}
    cfg = {**_DEFAULTS, **{k: v for k, v in over.items() if v is not None and k not in ("wear", "gallery")}}
    cfg["wear"] = {**_DEFAULTS["wear"], **{k: v for k, v in (over.get("wear") or {}).items() if v is not None}}
    cfg["gallery"] = {**_DEFAULTS["gallery"], **{k: v for k, v in (over.get("gallery") or {}).items() if v is not None}}
    return cfg


# Where a render may end, by view. `None` = not judged (the close-up, a view
# nobody named, footwear and accessories). Pure, so the whole table is testable
# without a model call.
_FULL_BODY = frozenset({"feet"})
_HALF_BODY = frozenset({"feet", "thigh", "knee", "waist_up"})   # anything but a cut lower leg


def framing_rule(view: str, *, side: str | None, exempt: bool,
                 cfg: dict[str, Any]) -> tuple[frozenset[str], str] | None:
    """What this view's framing must be, and how to say it when it is not.

    `side` is 'bottom' | 'upper' | None from the category; `exempt` is footwear
    or an accessory, which the renderer frames around the product on purpose
    (knee-to-floor, head-and-shoulders) and which the gate exempts too.
    """
    if exempt or str(cfg.get("framing") or "defect") == "off":
        return None
    full = set(cfg.get("full_body_views") or [])
    half = set(cfg.get("half_body_views") or [])
    if view in full or (side == "bottom" and view in half):
        # A bottom on a three-quarter view lands here: the hem is at the ankle,
        # and a picture that stops at the thigh is a picture of half the product.
        return _FULL_BODY, "this view must show the model head to feet"
    if view in half:
        return _HALF_BODY, "the legs are cut through the middle — crop at the knee or show the feet"
    return None


def severity_index(value: Any) -> int | None:
    text = str(value or "").strip().lower()
    return SEVERITY_SCALE.index(text) if text in SEVERITY_SCALE else None


# --------------------------------------------------------------------------- #
# Which pictures, and what each is supposed to be
# --------------------------------------------------------------------------- #
def select_images(media: list[dict[str, Any]], pol: dict[str, Any] | None,
                  side: str | None = None,
                  ) -> tuple[list[dict[str, Any]], int]:
    """The images to send, in order, each with `expect` and `kind`; and how many
    of them (at the head) are garment photographs the wear read may use.

    Garment photographs first — a cut-out per view where one exists, else the
    raw photo — then the renders, so "the first N images" in the prompt is a
    stable phrase. Deduplicated by URL.

    `side` is 'upper' | 'bottom' | None from the category, and changes ONE
    sentence: whether the close-up is required to show the front. A caller that
    does not know the category gets the neutral wording, which is the safe
    default — see `_closeup_expectation`.
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
                    expect = (_closeup_expectation(side) if view == "AI_CLOSEUP"
                              else _EXPECT_RENDER[view])
                    add(m, "render", expect + _RENDER_TAIL)
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
    elif any(im["kind"] == "photo" for im in images):
        # Wear is switched off (`wear.enabled: false`) but garment photographs
        # are still in the call for the gallery check. The old wording here —
        # "there is no garment photograph" — contradicted the per-image lines
        # above, which name several, and a prompt that argues with itself is a
        # worse instruction than either half alone.
        lines.append("Do NOT judge wear on this listing; answer wear = unknown.")
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
           product_gender: Any = None,
           category: Any = None, subcategory: Any = None) -> GateVerdict:
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
    # What the framing is judged against — the same two exemptions the gate
    # makes (`quality_gate.judge`): footwear renders knee-to-floor and an
    # accessory head-and-shoulders, both on purpose. With no category given,
    # nothing is exempt and a bottom cannot be recognised: the view table alone
    # then applies, which is the pre-21-September behaviour for every caller
    # that has not been taught to pass one.
    frame_mode = str(gal_cfg.get("framing") or "defect")
    side = garment_side(category, subcategory, pol)
    framing_exempt = False
    if category or subcategory:
        from app.imaging.quality_gate import is_accessory
        from app.rules.imagery import is_footwear

        framing_exempt = (is_footwear(str(category or ""), str(subcategory or ""))
                          or is_accessory(subcategory, category, pol=pol))
    if gal_cfg["enabled"]:
        photo_issues: list[str] = []
        cutout_issues: list[str] = []
        render_issues: list[str] = []
        renders = [i for i, im in enumerate(images, start=1) if im.get("kind") == "render"]
        # Which renders show a face, as the model answered it. Empty when the
        # answer predates `face_visible` — an older cached one — and the
        # comparison below then falls back to every render, which is what this
        # module did until 18 Sep 2026.
        faces: dict[int, bool] = {}
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
            # Collected before any of the `continue`s below: a render that is
            # perfectly fine on its own still votes on who is in the picture.
            if im.get("kind") == "render" and "face_visible" in entry:
                faces[idx] = bool(entry.get("face_visible"))
            # A garment part the mask ate counts on a cut-out even when the
            # model still called the picture "ok" — the part-by-part question
            # is the one it answers honestly.
            missing = ([str(p).strip() for p in (entry.get("missing_parts") or []) if str(p).strip()]
                       if is_cutout else [])
            # THE LEFTOVERS, MEASURED (18 Sep 2026). Only `clear` is a defect —
            # a whole hanger, or the podium under all four of MID-000521's
            # cut-outs. `slight` is the hook tip at a neckline that made
            # MID-000591 FRONT report a hanger that was not there: it is kept
            # in the stored answer and nothing follows from it.
            #
            # Read on a CUT-OUT only, and only when the model answered the
            # field: a render's "hand duplicated" and a raw photograph shot on
            # its hanger are other questions, and an answer cached under the
            # older schema has no `leftovers` key at all and is judged exactly
            # as it was before.
            has_leftovers = is_cutout and isinstance(entry.get("leftovers"), list)
            clear_left: list[str] = []
            if has_leftovers:
                for row in entry["leftovers"]:
                    if not isinstance(row, dict):
                        continue
                    part = str(row.get("part") or "").strip()
                    if part and str(row.get("extent") or "").strip().lower() == "clear" \
                            and part not in clear_left:
                        clear_left.append(part)
            # THE SCENE AND THE BODY, MEASURED (21 Sep 2026). Renders only, and
            # only the top of each scale: `cluttered` is a studio rig or a room
            # behind the model (MID-000475), `clear` a limb that dissolves
            # inside the frame (MID-000382). `minor` and `slight` are the soft
            # shadow and the indistinct edge every render has, and nothing
            # follows from them — the same threshold discipline the leftovers
            # got after MID-000591. An answer cached under the older schema has
            # neither key and is judged exactly as it was before.
            is_render = im.get("kind") == "render"
            scene_bad = is_render and str(entry.get("scene") or "").strip().lower() == "cluttered"
            body_bad = is_render and str(entry.get("body_flaw") or "").strip().lower() == "clear"

            # THE FRAMING, against what this view is for (21 Sep 2026).
            frame_bad = ""
            frame = str(entry.get("framing") or "").strip().lower()
            rule = framing_rule(view, side=side, exempt=framing_exempt,
                                cfg=gal_cfg) if is_render and frame else None
            if rule is not None and frame not in rule[0]:
                frame_bad = rule[1]
                if frame_mode == "soft":
                    # Recorded, never re-rendered: a tenant deciding what its
                    # framing standard is wants the count before the bill.
                    soft.append(f"FRAMING — {view} render ends at {frame.replace('_', ' ')}; {frame_bad}")
                    frame_bad = ""

            raw_issue = str(entry.get("issue") or "").strip()[:80]
            issue = raw_issue
            # The measurement outranks the free text (see `_without_leftovers`):
            # nothing above `slight` was seen, so an issue line that says
            # "hanger visible" is the model contradicting its own answer.
            if has_leftovers and not clear_left and issue:
                issue = _without_leftovers(issue)
            if not missing and not clear_left and not scene_bad and not body_bad and not frame_bad:
                if entry.get("ok") is not False:
                    continue
                if raw_issue and not issue:
                    continue      # the only complaint was a leftover below the bar
            if clear_left:
                # Only what the issue line does not already say: MID-000521's
                # BACK answer reads "hanger and stand visible" and measures
                # both as clear, and "hanger and stand visible; hanger, stand
                # visible" helps nobody.
                low = issue.lower()
                fresh = [p for p in clear_left if p.lower() not in low]
                if fresh:
                    issue = (f"{issue}; " if issue else "") + f"{', '.join(fresh[:3])} visible"
            if missing:
                issue = (f"{issue}; " if issue else "") + f"{', '.join(missing[:3])} missing"
            # Said in the words a person would use to decide what to do about
            # it, and only when the free text has not already said it — the
            # model that answers `cluttered` usually also writes "studio
            # equipment visible", and the row should not say it twice.
            for flagged, phrase, said in ((scene_bad, "background is not clean", _SCENE_SAID_RE),
                                          (body_bad, "the body is not whole", _BODY_SAID_RE)):
                if flagged and not said.search(issue.lower()):
                    issue = (f"{issue}; " if issue else "") + phrase
            if frame_bad:
                # Always spelled out, unlike the two above: "cropped" in the
                # model's own words does not say WHERE it ends or where it
                # should, and that is the whole instruction to the renderer.
                issue = ((f"{issue}; " if issue else "")
                         + f"ends at {frame.replace('_', ' ')} — {frame_bad}")
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

        # ONE PERSON ACROSS THE RENDERS, JUDGED ON FACES ALONE. The gate judges
        # the lead alone and cannot see that the close-up shows somebody else
        # (MID-000569). A render that shows a different person than the
        # majority is a render defect of THAT view — re-rendered against the
        # others as reference. Needs a majority to compare with: three renders
        # or more, and the odd ones fewer than the rest.
        #
        # 18 Sep 2026: the population is the renders WITH A FACE, not all of
        # them. BOA-006151 named AI_BACK and AI_BACK_34, BOA-006153 named
        # AI_CLOSEUP — every false positive was a faceless render, where the
        # only evidence left is hair and build and a back view of the same
        # person honestly looks unlike the front (§1.4). MID-000569's close-up
        # HAD a face, so it is still judged and still caught.
        #
        # `faces` empty means the answer predates the field (a cached one):
        # fall back to every render, the behaviour that answer was written
        # under. With the field present, all-false is a real answer — nobody's
        # face is visible, so nobody is compared.
        judged = [i for i in renders if faces.get(i)] if faces else renders
        if raw.get("same_model") is False and len(judged) >= 3:
            named: list[int] = []
            for o in raw.get("odd_renders") or []:
                try:
                    o = int(o)
                except (TypeError, ValueError):
                    continue
                if o in renders and o not in named:
                    named.append(o)
            odd = [o for o in named if o in judged]
            if odd and len(odd) * 2 < len(judged):
                for o in odd:
                    view = str(images[o - 1].get("view") or "")
                    if view in bad_views:
                        continue                 # already refused on its own answer
                    render_issues.append(f"{view} render: a different model than the other renders")
                    if view:
                        bad_views.append(view)
            elif named and not odd:
                # Every render it named is one with no face in it. That is not
                # a weaker finding to note softly — it is the comparison the
                # prompt forbids, made anyway, and a soft flag would put the
                # BOA-006151 line back on the page this change exists to clear.
                # The answer stays in the record (`head["raw"]`) and nothing
                # else happens.
                pass
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
          api_key: str | None = None, product_gender: Any = None,
          category: Any = None, subcategory: Any = None) -> GateVerdict:
    """Judge the product's photographs and gallery. Never raises.

    Same shape as quality_gate.judge on purpose: the chain enforces both the
    same way, and the cache is asked first — a set of pictures already judged
    under this prompt costs nothing.

    `category` / `subcategory` do two different things, and the difference
    matters for the cache. The FRAMING rules are applied to the answer, after
    the call: they change what the answer is held to, not what was asked. The
    CLOSE-UP's side is part of the question — an upper's close-up must show the
    front, a bottom's may show the seat — so it goes into the prompt and into
    the cache key. A caller that omits them gets the view table and the neutral
    close-up wording.
    """
    cfg = config(pol)
    if not cfg["enabled"]:
        return GateVerdict("skipped", reasons=["disabled in policy (photo_audit.enabled)"])
    # The side is needed BEFORE the call, not after: it decides one sentence of
    # the prompt (whether a close-up must show the front), so it is part of the
    # question and therefore part of the cache key — unlike the framing rules,
    # which are applied to the answer.
    images, photos = select_images(media, pol, garment_side(category, subcategory, pol))
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
                     grade_label=grade_label, pol=pol, product_gender=product_gender,
                     category=category, subcategory=subcategory)
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
