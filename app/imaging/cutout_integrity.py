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

and answers, per view: the defects found — `missing_part`, `broken_edge`,
`fragment` (a hanger, hook or clip only PARTLY removed), `background`, `unclear` —
each `minor` or `clear`. A WHOLE hanger or whole clips kept on the garment are not
a defect (the rule: keep the hanger rather than cut cloth).

`failing()` decides: any `missing_part` or `broken_edge`, whatever its severity —
fabric the garment has and the listing does not is never acceptable — and a
`background` or `unclear` the model calls `clear`. A `fragment` alone never fails:
the user's rule (7 Oct 2026) is to keep the hanger rather than cut cloth, and a
hanger or clip stub is not a defect. It is reported, so a person can still look.

Cached by image URLs + prompt, so a re-run is free. Never raises.
"""
from __future__ import annotations

import io
import logging
from typing import Any

import httpx

from app.config import policy, settings
from app.llm import cache, health

log = logging.getLogger("hermes.cutout-integrity")

SYSTEM = (
    "You are a strict quality inspector for second-hand clothing listings. A cut-out is "
    "the garment photograph with the background removed. You compare each cut-out with "
    "the original photograph, region by region, and report every difference in the "
    "garment itself. You never excuse a defect because the rest of the picture is good."
)

_ASK = """For each view you get four images, in this order:
  A — the ORIGINAL photograph (the garment on a hanger or clips, on the wall)
  B — the CUT-OUT made from it
  C — the TOP of the original, enlarged
  D — the TOP of the cut-out, enlarged

Compare B with A, and D with C. Report in the cut-out:
  missing_part — any part of the GARMENT that is in the original but missing or cut away
                 in the cut-out: collar, neckline, hood, shoulder, sleeve, cuff, waistband,
                 belt loop, strap, hem, pocket, zip pull, sewn-in label. Look hardest where
                 the hanger hook or the clips touch the garment: removing them must not take
                 fabric with them. A shop tag, price ticket, swing tag or anything else that
                 is not the garment itself is NOT a garment part — its absence is no defect.
  broken_edge  — the garment's outline is notched, bitten, jagged, or has gaps or holes
                 the original does not have.
  fragment     — a hanger, hook or clip only PARTLY removed, leaving a cut-off piece.
                 A WHOLE hanger, hook or clip kept intact is NOT a defect.
  background   — wall, floor, stand, tape or anything else of the background left in.
  unclear      — the cut-out is blurry, smeared, or the garment looks distorted.
Severity: `clear` when a shopper would notice it at normal size, else `minor`.
Answer ok=true for a view only when it has no defect at all."""

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
                                    "missing_part", "broken_edge", "fragment", "background", "unclear"]},
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


def failing(answer: dict[str, Any] | None) -> list[str]:
    """The defects that fail a cut-out, as sentences. Fabric the listing lacks
    (missing_part, broken_edge) fails at any severity; background and unclear only
    when clear; a hanger or clip fragment never (see the module note)."""
    out: list[str] = []
    for v in (answer or {}).get("views") or []:
        for d in v.get("defects") or []:
            kind, sev = str(d.get("type") or ""), str(d.get("severity") or "")
            if kind in ("missing_part", "broken_edge") or (kind in ("background", "unclear") and sev == "clear"):
                out.append(_sentence(v.get("view"), d))
    return list(dict.fromkeys(out))


def noted(answer: dict[str, Any] | None) -> list[str]:
    """Everything else the AI saw — reported for a person, failing nothing."""
    fails = set(failing(answer))
    out = [_sentence(v.get("view"), d) for v in (answer or {}).get("views") or []
           for d in v.get("defects") or []]
    return [x for x in dict.fromkeys(out) if x not in fails]
