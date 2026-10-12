"""Is a cut-out the WHOLE garment? Gemini compares it with the photograph it was cut from.

12 Oct 2026. The photo audit passed BOA-002833's back cut-out — clips half cut away
at the waistband, a broken top edge — and BOA-002039's, the collar cut away around
the hanger hook. It had seen "clip, slight" and "hanger, slight", and policy rightly
ignores a slight leftover; but it judged each cut-out ALONE, never against the
photograph, and its question is about leftovers, not about fabric that went with
them. Removing a hanger or a clip is exactly where cloth gets cut.

So this asks one narrow question per view, with the evidence to answer it:

  A  the original photograph
  B  the cut-out
  C  the top of the original, enlarged   (where the hanger, the hook and the clips are)
  D  the top of the cut-out, enlarged

and answers, per view, the user's three defects (12 Oct 2026) — each `minor` or
`clear`:

  cloth_broken  fabric missing, cut away or torn; a hole; a notched outline
  background    anything that is not the garment left OUTSIDE its outline
  colour        the garment's colour lost or changed against the photograph

and NOT, by name: the dress form's neck seen inside the neck opening (KLE-000398 —
it keeps the garment from looking hollow, and the first prompt called it
"background above the collar"), the back collar above the front neckline, a hanger
or clip kept or left as a stub (keep the hanger rather than cut cloth), a missing
shop tag.

`failing()` decides: `cloth_broken` at any severity — fabric the garment has and
the listing does not is never acceptable — and `background` / `colour` the model
calls `clear`.

Cached by image URLs + prompt, so a re-run is free. Never raises.
"""
from __future__ import annotations

import io
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from app.config import policy, settings
from app.llm import cache, health

log = logging.getLogger("hermes.cutout-integrity")

SYSTEM = (
    "You are a quality inspector for second-hand clothing listings. A cut-out is the "
    "garment photograph with the background removed. You compare each cut-out with the "
    "original photograph and report only what makes the listing look wrong to a shopper: "
    "cloth that is broken, something of the background left in, or colour that is lost. "
    "You do not report what is normal for a garment photographed on a hanger or a dress form."
)

_ASK = """For each view you get four images, in this order:
  A — the ORIGINAL photograph (the garment on a hanger, on clips, or on a dress form)
  B — the CUT-OUT made from it
  C — the TOP of the original, enlarged
  D — the TOP of the cut-out, enlarged

Compare B with A, and D with C. Report ONLY these three kinds of defect:
  cloth_broken — garment fabric that is in the original but missing, cut away or torn in
                 the cut-out: a piece of the collar, neckline, hood, shoulder, sleeve, cuff,
                 waistband, belt loop, strap, hem or pocket gone; a hole the original does
                 not have; the outline notched or bitten (often where a hanger hook or clips
                 touched the garment).
  background   — something that is NOT the garment, left OUTSIDE its outline: wall, floor,
                 ceiling, rail, the dress form's stand, pole or base, the form showing below
                 the hem or beside the garment, tape, a hand, other objects.
  colour       — the garment's colour lost or changed compared with the original: an area
                 washed out, turned white, grey or see-through, a print, patch or logo that
                 lost its colour, a clearly different shade.

These are NOT defects — never report them:
  * the dress form's neck or body seen INSIDE the neck opening, armholes or other openings,
    enclosed by the garment (it keeps the garment from looking hollow);
  * the inside back of the collar or neckline visible above the front neckline;
  * a hanger, hook or clips kept on the garment, whole or as a small stub;
  * a missing shop tag, price ticket or swing tag (not part of the garment);
  * slightly soft edges, or lighting that is a little different but the same colour.

Severity: `clear` when a shopper would notice it at normal size, else `minor`.
Answer ok=true for a view only when it has none of the three defects."""

SCHEMA = {
    "type": "object",
    "properties": {
        "views": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "view": {"type": "string"},
                    "ok": {"type": "boolean"},
                    "defects": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {"type": "string", "enum": [
                                    "cloth_broken", "background", "colour"]},
                                "part": {"type": "string"},
                                "severity": {"type": "string", "enum": ["minor", "clear"]},
                                "where": {"type": "string"},
                            },
                            "required": ["type", "part", "severity", "where"],
                        },
                    },
                    "summary": {"type": "string"},
                },
                "required": ["view", "ok", "defects", "summary"],
            },
        },
    },
    "required": ["views"],
}

# How the images are sent: the long side, and how much of the top is enlarged.
LONG_SIDE = 1536
TOP_SHARE = 0.38


def _prepare(data: bytes) -> tuple[bytes, bytes]:
    """The whole picture and its top, as JPEG — a transparent cut-out on white."""
    from PIL import Image

    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    try:  # the photograph's own orientation
        from PIL import ImageOps
        im = ImageOps.exif_transpose(im)
    except Exception:  # noqa: BLE001
        pass
    whole = im.copy()
    whole.thumbnail((LONG_SIDE, LONG_SIDE))
    w, h = im.size
    top = im.crop((0, 0, w, max(1, int(h * TOP_SHARE))))
    top.thumbnail((LONG_SIDE, LONG_SIDE))

    def jpg(x) -> bytes:
        buf = io.BytesIO()
        x.save(buf, "JPEG", quality=88)
        return buf.getvalue()

    return jpg(whole), jpg(top)


def _ask(pairs: list[dict[str, Any]], category: str | None, model: str) -> dict[str, Any] | None:
    key = settings().gemini_api_key
    if not key:
        return None
    from google import genai
    from google.genai import types

    from app.llm.gemini import GeminiEvidence
    from app.net import genai_client_args

    parts: list[Any] = []
    lines = [_ASK, "", f"The garment is: {category or 'a clothing item'}."]
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        for n, pair in enumerate(pairs, 1):
            cut = client.get(pair["cutout"]).content
            orig = client.get(pair["original"]).content if pair.get("original") else None
            cut_whole, cut_top = _prepare(cut)
            if orig:
                orig_whole, orig_top = _prepare(orig)
                lines.append(f"View {n} = {pair['view']}: images in the order A, B, C, D below.")
                imgs = [orig_whole, cut_whole, orig_top, cut_top]
            else:
                lines.append(f"View {n} = {pair['view']}: NO original on file — images B then D only; "
                             f"judge the cut-out on its own.")
                imgs = [cut_whole, cut_top]
            for b in imgs:
                parts.append(types.Part.from_bytes(data=b, mime_type="image/jpeg"))
    lines.append("Answer one entry per view, in the same order, with `view` set to its name.")
    args = genai_client_args()
    gclient = genai.Client(api_key=key,
                           http_options=types.HttpOptions(client_args=args) if args else None)
    cfg: dict[str, Any] = {"system_instruction": SYSTEM}
    cfg.update(GeminiEvidence._schema_config(SCHEMA))
    resp = gclient.models.generate_content(
        model=model, contents=[types.Part.from_text(text="\n".join(lines)), *parts],
        config=types.GenerateContentConfig(**cfg))
    return GeminiEvidence._parse(resp.text)


def judge(pairs: list[dict[str, Any]], *, category: str | None = None,
          model: str | None = None) -> tuple[dict[str, Any] | None, str]:
    """`pairs`: [{view, cutout, original|None}]. Returns (answer, note); answer is
    {"views": [...]} or None when the AI could not answer (note says why)."""
    if not pairs:
        return None, "no cut-out to look at"
    pol = policy()
    model = model or (pol.get("llm") or {}).get("model_fast") or "gemini-2.5-flash"
    k = cache.key("cutout-integrity", urls=[u for p in pairs for u in (p["cutout"], p.get("original") or "")],
                  text=[model, SYSTEM, _ASK, SCHEMA, category or "", LONG_SIDE, TOP_SHARE])
    try:
        answer, hit = cache.through(k, lambda: _ask(pairs, category, model), pol=pol)
    except Exception as exc:  # noqa: BLE001 — a failed look concludes nothing
        log.warning("cut-out integrity check failed: %s", exc)
        health.record_error(exc, "gemini")
        return None, f"AI check could not run — {type(exc).__name__}: {exc}"[:200]
    if not answer:
        return None, "AI check could not run — no answer"
    health.record_ok("gemini")
    return answer, "cached" if hit else ""


def _sentence(view: Any, d: dict[str, Any]) -> str:
    kind = str(d.get("type") or "")
    part = str(d.get("part") or "").strip()
    where = str(d.get("where") or "").strip()
    return (f"CUTOUT DEFECT — {view} cut-out: {kind.replace('_', ' ')}"
            + (f" ({part})" if part else "") + (f" — {where}" if where else "")
            + f" [{d.get('severity')}]")


def _fails(d: dict[str, Any]) -> bool:
    """The user's three (12 Oct 2026): broken cloth at any severity; background or
    lost colour when clear."""
    kind, sev = str(d.get("type") or ""), str(d.get("severity") or "")
    return kind == "cloth_broken" or (kind in ("background", "colour") and sev == "clear")


def failing(answer: dict[str, Any] | None) -> list[str]:
    """The defects that hold a product, as sentences."""
    out = [_sentence(v.get("view"), d) for v in (answer or {}).get("views") or []
           for d in v.get("defects") or [] if _fails(d)]
    return list(dict.fromkeys(out))


def failing_views(answer: dict[str, Any] | None) -> list[str]:
    """The views (as named in the pairs) with a defect that holds."""
    out = [str(v.get("view") or "") for v in (answer or {}).get("views") or []
           if any(_fails(d) for d in v.get("defects") or [])]
    return list(dict.fromkeys(out))


def noted(answer: dict[str, Any] | None) -> list[str]:
    """Everything else the AI saw — reported for a person, failing nothing."""
    fails = set(failing(answer))
    out = [_sentence(v.get("view"), d) for v in (answer or {}).get("views") or []
           for d in v.get("defects") or []]
    return [x for x in dict.fromkeys(out) if x not in fails]


# --------------------------------------------------------------------------- #
# the repair chain's step (scripts/repair_product.py `cutcheck`)
# --------------------------------------------------------------------------- #
CODE = "CUTOUT_DEFECT"

# `readiness.cutouts.integrity`, with its defaults. Whether a defect HOLDS follows
# `readiness.cutouts.hold`, like every other cut-out finding.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # A FRONT/BACK view that fails is re-cut once from the raw photograph and
    # looked at again; still failing holds.
    "recut": True,
    # The AI could not answer: hold (retried by the runner as VISION_UNAVAILABLE)
    # rather than let a cut-out through unlooked-at.
    "required": True,
    # The photo audit judges each cut-out ALONE; once this check — the cut-out
    # against its photograph, held only for the user's three defects — has
    # answered, the audit's CUTOUT DEFECT findings no longer hold on their own
    # (they still drive the rematte step's re-cut).
    "photo_audit_defers": True,
}


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    from app.imaging import cutouts

    return {**DEFAULTS, **(cutouts.config(pol or {}).get("integrity") or {})}


@dataclass
class IntegrityVerdict:
    action: str                                   # ok | bad | pending | unknown | skipped
    reasons: list[str] = field(default_factory=list)       # the defects that hold
    soft: list[str] = field(default_factory=list)          # seen, holding nothing
    bad_views: list[str] = field(default_factory=list)     # FRONT / BACK / OTHER …
    hold: bool = True                             # readiness.cutouts.hold == block
    required: bool = True
    unavailable: bool = False
    note: str = ""
    code: str = CODE

    @property
    def blocks(self) -> bool:
        if self.action == "bad":
            return self.hold
        return self.action == "unknown" and self.unavailable and self.required

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "blocks": self.blocks}


def check(rows: list[dict[str, Any]], sent: list[str], *, category: str | None,
          pol: dict[str, Any] | None = None, model: str | None = None) -> IntegrityVerdict:
    """Every cut-out the listing shows, against the photograph it was cut from.

    `rows` are the product's ProductMedia rows (id, url, view, origin, processing,
    derivedFromId, deletedAt, createdAt); `sent` is Product.images. Never raises.
    """
    from app.imaging import cutouts
    from app.imaging import gallery_cleanup as gc

    cfg = config(pol)
    hold = str(cutouts.config(pol or {}).get("hold") or "soft").lower() == "block"
    by_id = {r["id"]: r for r in rows}
    cuts = gc.listing_cutouts(rows, sent)
    if not cuts:
        return IntegrityVerdict("skipped", hold=hold, required=bool(cfg["required"]),
                                note="no cut-out to look at")
    seen: dict[str, int] = {}
    pairs = []
    for r in cuts:
        view = str(r.get("view") or "OTHER")
        seen[view] = seen.get(view, 0) + 1
        pairs.append({"view": view if seen[view] == 1 else f"{view} {seen[view]}",
                      "cutout": r["url"],
                      "original": gc.root_of(r, by_id)["url"] if r.get("derivedFromId") else None})
    answer, note = judge(pairs, category=category, model=model)
    if answer is None:
        return IntegrityVerdict("unknown", hold=hold, required=bool(cfg["required"]),
                                unavailable=True, note=note)
    reasons = failing(answer)
    return IntegrityVerdict("bad" if reasons else "ok", reasons=reasons, soft=noted(answer),
                            bad_views=failing_views(answer), hold=hold,
                            required=bool(cfg["required"]), note=note)


def summary(v: IntegrityVerdict) -> str:
    if v.action == "ok":
        return ("every cut-out whole, background clear, colour kept"
                + (f" (also seen: {'; '.join(v.soft)})" if v.soft else ""))
    if v.action == "skipped":
        return v.note
    if v.action == "unknown":
        return f"could not look — {v.note}"
    return "; ".join(v.reasons)
