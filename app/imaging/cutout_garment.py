"""The garment, read from the product's own background-removed photographs.

THE CUT-OUTS DECIDE THE CATEGORY (22 Oct 2026, the user). The refile step used
to take the garment from the image gate's look at the AI render. A render is a
picture the image model DREW from the category it was given, so a product filed
under the wrong category can come back rendered as that wrong garment — the
render then "agrees" with a wrong record, or disagrees for reasons of its own.
The background-removed FRONT / BACK photographs are the garment itself, so the
answer to "what is this?" is read from them.

The vocabulary is the gate's (`garment_family` / `garment_type`, policy
`garment_families` / `garment_types`), so a cut-out answer and a filed category
are compared exactly the way the gate compared a render with one.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.imaging import quality_gate

VIEWS = ("FRONT", "BACK")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "garment": {
            "type": "string",
            "description": "One or two plain words for the item — 't-shirt', "
                           "'jeans', 'shorts', 'hoodie', 'puffer vest', 'dress', "
                           "'backpack', 'sneakers'. Empty if unclear.",
        },
        "confidence": {"type": "number", "description": "0.0 to 1.0."},
    },
    "required": ["garment", "confidence"],
}

SYSTEM = (
    "You identify second-hand clothing and accessories from product photographs. "
    "You name what the item IS from its shape and construction, not from any "
    "text printed on it, and you say so through the confidence field when unsure."
)

PROMPT = (
    "These are photographs of ONE item for sale, with the background removed "
    "(front, and back when there are two). It may hang on a hanger or sit on a "
    "form — ignore those. What is the item? Answer the json schema."
)


@dataclass
class CutoutGarment:
    garment: str = ""
    confidence: float | None = None
    urls: list[str] = field(default_factory=list)
    cached: bool = False
    unavailable: bool = False     # could not ask — retry, never act on it
    error: str = ""

    def summary(self) -> str:
        if self.unavailable:
            return f"could not read the cut-outs — {self.error}"
        if not self.garment:
            return "the cut-outs do not name a garment"
        conf = f", confidence {self.confidence:.2f}" if self.confidence is not None else ""
        return (f"the cut-outs show {self.garment!r}{conf}"
                + (" (cached)" if self.cached else ""))


# The vision model names a bag precisely ("handbag", "tote"); a tenant's tree
# says "Bags". The refile planner matches WORDS against the tree, so these are
# brought to the word a tree uses before anything is compared or filed.
_CANON = {
    "bag": ("handbag", "tote", "purse", "clutch", "satchel", "crossbody",
            "shoulder bag", "tote bag", "duffel", "pouch"),
    "backpack": ("rucksack", "back pack"),
}


def canonical(garment: str) -> str:
    g = " ".join(str(garment or "").lower().split())
    for word, synonyms in _CANON.items():
        if g in synonyms or any(re.search(rf"\b{re.escape(s)}s?\b", g) for s in synonyms):
            return word
    return g


def photos(media: list[dict[str, Any]]) -> list[str]:
    """The background-removed FRONT then BACK photograph — never an AI render.

    One per view, the lowest position (the gallery's own pick), so a second
    stale cut-out left beside a re-cut does not decide anything.
    """
    out: list[str] = []
    for view in VIEWS:
        rows = [m for m in media
                if m.get("view") == view
                and m.get("processing") == "BG_REMOVED"
                and str(m.get("origin") or "") != "AI"
                and (m.get("mediaType") or "IMAGE") == "IMAGE"
                and m.get("isCurrent", True) and not m.get("deletedAt") and m.get("url")]
        rows.sort(key=lambda m: (m.get("position") or 0, str(m.get("createdAt") or "")))
        if rows:
            out.append(str(rows[0]["url"]))
    return out


def read(media: list[dict[str, Any]], pol: dict[str, Any] | None, *,
         evidence: Any = None, api_key: str | None = None) -> CutoutGarment:
    """Ask the vision model what the cut-outs show. Cached by URL and prompt."""
    urls = photos(media)
    if not urls:
        return CutoutGarment(unavailable=True, error="no background-removed FRONT/BACK photograph")
    gcfg = quality_gate.config(pol)
    model = gcfg.get("model") or (pol or {}).get("llm", {}).get("model_fast") or "gemini-2.5-flash"

    from app.llm import cache

    ckey = cache.key("cutout_garment", urls=urls, text=[model, SYSTEM, PROMPT, str(SCHEMA)])
    raw = cache.get(ckey, pol)
    hit = raw is not None
    if not hit:
        if evidence is None:
            from app.config import settings

            key = api_key or settings().gemini_api_key
            if not key:
                return CutoutGarment(urls=urls, unavailable=True, error="no GEMINI_API_KEY")
            try:
                from app.llm.gemini import GeminiEvidence
            except ImportError as exc:  # pragma: no cover — SDK optional
                return CutoutGarment(urls=urls, unavailable=True,
                                     error=f"google-genai is not installed ({exc})")
            evidence = GeminiEvidence(key, pol or {"llm": {"model_fast": "gemini-2.5-flash"}})
        parts = evidence._fetch_images(urls)
        if not parts:
            return CutoutGarment(urls=urls, unavailable=True, error="cut-outs could not be downloaded")
        raw = evidence._generate(model=model, contents=[*parts, PROMPT], schema=SCHEMA, system=SYSTEM)
        if raw is None:
            last = (getattr(evidence, "errors", None) or ["unreadable answer"])[-1]
            return CutoutGarment(urls=urls, unavailable=True, error=f"vision provider: {str(last)[:160]}")
        cache.put(ckey, raw, pol)
    conf = raw.get("confidence")
    return CutoutGarment(garment=canonical(str(raw.get("garment") or "")),
                         confidence=float(conf) if isinstance(conf, (int, float)) else None,
                         urls=urls, cached=hit)


def mismatch(seen: CutoutGarment, *, category: Any, subcategory: Any,
             pol: dict[str, Any] | None) -> str | None:
    """Why the cut-outs and the filed category disagree, or None.

    The gate's own test, applied to the cut-outs' answer: a different family
    (not an adjacent pair such as tops / outerwear), or — inside one family —
    a different type (shorts vs jeans) when the read is sure enough.
    """
    garment = seen.garment
    if not garment or seen.unavailable:
        return None
    filed = str(subcategory or category or "").strip()
    fam_seen = quality_gate.garment_family(garment, pol)
    fam_filed = quality_gate.garment_family(filed, pol)
    if fam_seen and fam_filed and fam_seen != fam_filed \
            and not quality_gate._adjacent(fam_seen, fam_filed, pol):
        return (f"the cut-outs show {garment} ({fam_seen}) but the product is "
                f"filed under '{filed}' ({fam_filed})")
    if fam_seen and fam_seen == fam_filed:
        type_seen = quality_gate.garment_type(garment, pol)
        type_filed = quality_gate.garment_type(filed, pol)
        sure = seen.confidence is None or seen.confidence >= float(
            quality_gate.config(pol).get("type_min_confidence") or 0.6)
        if type_seen and type_filed and type_seen != type_filed and sure:
            return (f"the cut-outs show {garment} ({type_seen.replace('_', ' ')}) but "
                    f"the product is filed under '{filed}' ({type_filed.replace('_', ' ')})")
    return None
