"""The tenant's title format and description sections, resolved per product.

A port of vnyx-api's title builder (services/title-template.ts,
services/title-template-override.ts, services/product-title.ts) so the copy
rules can say what the title SHOULD be: the format set on Settings → Title &
Description, its category override when one matches (subcategory → category →
master category → Kids, the image-sequence matcher), else the tenant default.

Keep in step with vnyx-api — the expected title here must be the one
`regenerate-copy.ts` writes, or the copy step would fix a title into another
mismatch. The fallback chains below are copied from `resolveTitleVars`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable

from app.rules.shot_sequence import match_override, norm

TITLE_MAX_LENGTH = 255
TITLE_TEMPLATE_MAX_PARTS = 20


def normalize_template(raw: Any) -> dict[str, Any]:
    """`{enabled, parts}`; anything malformed reads as disabled (keeps the AI title)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("parts"), list):
        return {"enabled": False, "parts": []}
    parts: list[dict[str, Any]] = []
    for p in raw["parts"]:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text" and isinstance(p.get("value"), str):
            parts.append({"type": "text", "value": p["value"]})
        elif p.get("type") == "field" and isinstance(p.get("key"), str) and p["key"]:
            part = {"type": "field", "key": p["key"]}
            for k in ("prefix", "suffix"):
                if isinstance(p.get(k), str) and p[k]:
                    part[k] = p[k]
            parts.append(part)
    return {"enabled": raw.get("enabled") is True,
            "parts": parts[:TITLE_TEMPLATE_MAX_PARTS]}


def is_active(template: dict[str, Any]) -> bool:
    return bool(template.get("enabled")) and bool(template.get("parts"))


@dataclass
class TitleOverride:
    """One category's stored format, with the category's path (leaf first)."""

    template: dict[str, Any]
    name: str
    path: list[str]


def override_candidates(rows: Iterable[tuple[Any, str | None, str | None, str | None]]
                        ) -> list[TitleOverride]:
    """`(template, category, parent, grandparent)` rows -> the usable overrides.

    An override is always on (readOverrideTemplate): it exists only while the
    tenant format is, and the default's switch governs both.
    """
    out: list[TitleOverride] = []
    for template, cat, parent, grand in rows:
        name = norm(cat)
        if not name:
            continue
        out.append(TitleOverride(
            template={**normalize_template(template), "enabled": True},
            name=str(cat),
            path=[p for p in (name, norm(parent), norm(grand)) if p],
        ))
    return out


def _str(v: Any) -> str:
    if isinstance(v, bool):
        return ""
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return ""


def _first(*vals: Any) -> str:
    for v in vals:
        s = _str(v)
        if s:
            return s
    return ""


def _gender_text(raw: Any) -> str:
    v = raw
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            import json
            v = json.loads(v)
        except ValueError:
            pass

    def cap(s: str) -> str:
        return s[:1].upper() + s[1:]

    if isinstance(v, list):
        vals = [s for s in (_str(x) for x in v) if s]
        if not vals:
            return ""
        return cap(vals[0]) if len(vals) == 1 else "Unisex"
    return cap(_str(v))


def resolve_vars(props: dict[str, Any], *, brand_relation: Any = None,
                 category: Any = None, sub_category: Any = None,
                 master_category: Any = None, sizing_guide: Any = None,
                 international_size: Any = None, sku: Any = None) -> dict[str, str]:
    """Every title field to a plain string ('' when missing) — `resolveTitleVars`."""
    out: dict[str, str] = {}
    for k, v in props.items():
        s = _gender_text(v) if k == "gender" else _str(v)
        if s:
            out[k] = s
    out.update({
        # Any case (`Brand`, `brand`, `BRAND`) — vnyx-api's resolveTitleVars, 27 Oct 2026.
        "brand": _first(next((v for k, v in props.items() if k.lower() == "brand"), None),
                        brand_relation),
        "color": _first(props.get("color")),
        "gender": _gender_text(props.get("gender")),
        "size": _first(props.get("international_size"), props.get("size"), international_size),
        "euSize": _first(props.get("eu_size"), props.get("euSize")),
        "waistSize": _first(props.get("waist_size"), props.get("waistSize"), props.get("waist")),
        "lengthSize": _first(props.get("length_size"), props.get("lengthSize")),
        "material": _first(props.get("material")),
        "fit": _first(props.get("fit")),
        "model": _first(props.get("model")),
        "playerName": _first(props.get("player_name"), props.get("playerName")),
        "playerNumber": _first(props.get("player_number"), props.get("playerNumber")),
        "condition": _first(props.get("condition")),
        "category": _first(props.get("category"), category),
        "subCategory": _first(props.get("sub_category"), props.get("subCategory"), sub_category),
        "masterCategory": _first(props.get("mastercategory"), props.get("masterCategory"),
                                 master_category),
        "productType": _first(props.get("producttype"), props.get("product_type"),
                              props.get("productType"), sizing_guide),
        "sku": _first(sku),
    })
    return out


def build_title(template: dict[str, Any], vars_: dict[str, str]) -> str | None:
    """`buildTitleFromTemplate`: empty fields drop with their prefix/suffix, cut at 255."""
    pieces: list[str] = []
    for p in template.get("parts") or []:
        if p["type"] == "text":
            pieces.append(p["value"].strip())
            continue
        v = (vars_.get(p["key"]) or "").strip()
        if not v or v.lower() == "unknown":
            continue
        pieces.append(f'{p.get("prefix", "")}{v}{p.get("suffix", "")}')
    title = re.sub(r"\s{2,}", " ", " ".join(x for x in pieces if x)).strip()
    if not title:
        return None
    if len(title) <= TITLE_MAX_LENGTH:
        return title
    cut = title[:TITLE_MAX_LENGTH]
    space = cut.rfind(" ")
    return (cut[:space] if space > TITLE_MAX_LENGTH * 0.6 else cut).strip()


def active_section_names(raw: Any) -> list[str]:
    """The tenant's active description sections, in order — what the copy must carry."""
    if not isinstance(raw, list):
        return []
    rows = [s for s in raw if isinstance(s, dict) and s.get("isActive") is not False
            and isinstance(s.get("name"), str) and s["name"].strip()]
    rows.sort(key=lambda s: s.get("order") if isinstance(s.get("order"), (int, float)) else 0)
    return [s["name"].strip() for s in rows]


def resolve(*, default_template: Any, overrides: list[TitleOverride], sections: Any,
            props: dict[str, Any], brand_relation: Any = None, category: Any = None,
            sub_category: Any = None, master_category: Any = None,
            mannequin_type: Any = None, sizing_guide: Any = None,
            international_size: Any = None, sku: Any = None) -> dict[str, Any]:
    """The product's copy format: expected title (None when the format is off) + sections.

    The columns overlay the taxonomy keys of `properties`, exactly as
    regenerate-copy.ts feeds `buildTemplateTitleForProduct` — the columns are the
    facts once the cascade has settled them.
    """
    default = normalize_template(default_template)
    template, source, matched = default, "default", None
    if default["enabled"]:
        hit = match_override(overrides, category=category, sub_category=sub_category,
                             master_category=master_category, mannequin_type=mannequin_type)
        if hit:
            template, source, matched = hit.template, "category", hit.name

    overlay = dict(props)
    if master_category:
        overlay["mastercategory"] = overlay["masterCategory"] = master_category
    if category:
        overlay["category"] = category
    if sub_category:
        overlay["sub_category"] = overlay["subCategory"] = sub_category
        overlay["product_type"] = sub_category
    if international_size and not _str(props.get("international_size")):
        overlay["international_size"] = international_size

    expected = None
    if is_active(template):
        expected = build_title(template, resolve_vars(
            overlay, brand_relation=brand_relation, category=category,
            sub_category=sub_category, master_category=master_category,
            sizing_guide=sizing_guide, international_size=international_size, sku=sku))
    return {
        "expectedTitle": expected,
        "titleSource": source,
        "matchedCategory": matched,
        "sections": active_section_names(sections),
    }
