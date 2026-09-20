"""Readiness, phase 1 — the master category is the anchor.

docs/READINESS-PLAN.md §4 steps 1 and 2. Everything a product's other fields
hang off is settled FROM the master category, in this order:

    master category  ->  gender  ->  category  ->  subcategory
                     ->  sizing guide (gender + side + ladder)  ->  EU size
                     ->  mannequin rig (derived, never read)

This module holds the pure decisions: which root the product belongs to, what
a root implies for the gender property, what rig a product should carry. The
planners in app/approval.py turn them into plan entries; the rules in
app/rules/consistency.py use them to say what is wrong; the chain in
scripts/repair_product.py prints them at the head of the run. Nothing here
reads a database or calls a model, so every decision is testable cold.

WHY THE MASTER, AND NOT THE RIG. Until this phase TAX.005 made the mannequin
rig the anchor and rewrote masterCategory and gender to match it. On the local
clone the rig disagrees with the master on hundreds of approved products —
`Women Top` on 52 men's products, `WomenTop` on 119 unisex ones — and the
requirement is explicit: the mannequin is never an input. So the rig is now
DERIVED from the master and the garment's side, and written, never read.

WHAT A ROOT IMPLIES. `Men` and `Women` imply a gender. `Unisex` and `Kids` are
valid roots (341 and 98 approved products locally) that imply none: the gender
property stands on its own there, and TAX.004 stays silent. Which roots imply
what is policy (`readiness.master.gender_by_root`), because a tenant may spell
them differently.

THE RENDER NEVER VOTES. The photo audit now reports the gender the GARMENT
photographs suggest; a contradiction with the master is a soft flag until the
shadow run has measured it (`readiness.master.photo_check`). The AI render is
the thing under judgement and can never rewrite the record it is judged by.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.imaging import cutouts as _cutouts
from app.models import ProductSnapshot
from app.rules.gate import _side_of, resolve_gender

_DEFAULTS: dict[str, Any] = {
    # Phase 3, the cut-outs: canvas and backdrop checks, the re-matte, and
    # whether a cut-out still wrong afterwards holds. The defaults live with
    # the code that reads them (app/imaging/cutouts.py); merged here so
    # `config(pol)["cutouts"]` is complete too.
    "cutouts": deepcopy(_cutouts.DEFAULTS),
    "enabled": True,
    "master": {
        # root -> the gender property it implies. Roots not listed imply none.
        "gender_by_root": {"Men": "men", "Women": "women"},
        # Roots that legitimately hold either gender; TAX.004 is silent on them
        # and the gender property is the source for renders and guides.
        "genderless_roots": ["Unisex", "Kids"],
        # What a garment photograph that says the OTHER gender does: `soft` is
        # a flag on the photo audit, `hold` is MASTER_CATEGORY_MISMATCH.
        "photo_check": "soft",
        "photo_min_confidence": 0.85,
    },
    "unisex_gender_source": "property",
    "mannequin": {
        # The rig vocabulary the edit screen offers (vnyx-ui, formatters.ts
        # MannequinType). A derived rig MUST be one of these, or the dropdown
        # renders blank for a value that is stored.
        "upper": {"men": "Men Top", "women": "Women Top", "any": "Top"},
        "bottom": "Bottom",
        "kids": "Kids",
    },
    # A house choice (a category, a guide) wins a tie only when the tenant uses
    # it at least this many times more often than the runner-up.
    "usage_tiebreak_ratio": 2,

    # --- phase 4: the renders (docs/READINESS-PLAN.md §4 step 5) ---------------
    # One paid regeneration per product per run. A second refusal holds.
    "max_regenerations_per_run": 1,
    # Decision 4: Kids products are rendered (with a child model, as the prompt
    # builder already does for the Kids rig), never held for being Kids.
    # `hold` leaves their renders to a person.
    "kids_renders": "generate",
    # Decision 8: a product nothing can repair — no garment photograph, every
    # image model declining the print — is REJECTED with its reason rather
    # than held. `hold` keeps the old behaviour.
    "unfixable": "reject",
    # Roots that mean a child model. Judged against no adult build.
    "kids_roots": ["Kids", "Kid", "Children", "Child"],

    # --- phase 5: the gallery order and the copy (§4 steps 7 and 8) -----------
    "gallery": {
        "enabled": True,
        # `soft`: IMG.025 is a flag; `block`: an out-of-order gallery holds.
        # The chain rebuilds the cache either way.
        "hold": "soft",
        # Decisions 2 and 3, mirrored from vnyx-api ORIGIN_RANK. Inside the
        # garment band the origin leads; the bands and views are the schema's.
        "origin_order": ["AI", "MANUAL", "WEB", "PHOTOBOOTH", "DECISION", "SIZE_GUIDE"],
        # Leave a gallery flagged `mediaManualOrder` as it is. True by default
        # (the flag's intent is an operator's arrangement); false when the flag
        # is not trusted — on production 1,538 of 4,877 approved products carry
        # it, in the machine's own default order (MID-000247), which says the
        # gallery was saved, not that a person chose the sequence.
        "respect_manual": True,
    },
    "copy": {
        "enabled": True,
        # A change to any of these this run means the title template's inputs
        # moved; the title and description are regenerated from the record.
        "trigger_fields": ["gender", "masterCategory", "category", "subCategory",
                           "size", "color", "brand"],
        # Or the copy already disagrees with the record.
        "trigger_rules": ["TEXT.002", "TEXT.003", "TEXT.006", "TEXT.007", "TEXT.008"],
    },
    # The model's build against the garment's size (the gate's `model_build`).
    "body_size": {
        "enabled": True,
        "min_confidence": 0.7,
        # Two bands apart regenerates (slim on an XL); one band is a soft flag.
        "block_on_band_gap": 2,
        "bands": {"xs": "slim", "s": "slim", "m": "average", "l": "average",
                  "xl": "plus", "xxl": "plus"},
        # Decision 9, as shipped in vnyx-api services/body-type.ts: waist inches
        # → letter, per gender, continuous from 20 to 60.
        "waist_bands": {
            "men": {"xs": [20, 29], "s": [30, 31], "m": [32, 33],
                    "l": [34, 37], "xl": [38, 41], "xxl": [42, 60]},
            "women": {"xs": [20, 25], "s": [26, 27], "m": [28, 29],
                      "l": [30, 31], "xl": [32, 33], "xxl": [34, 60]},
        },
    },
}


def _merge(base: dict[str, Any], over: dict[str, Any] | None) -> dict[str, Any]:
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    """The `readiness` policy block with defaults filled in."""
    return _merge(_DEFAULTS, (pol or {}).get("readiness") or {})


def enabled(pol: dict[str, Any] | None) -> bool:
    return bool(config(pol).get("enabled", True))


def flat(value: Any) -> str:
    """Letters and digits only, lowercased — 'T-Shirts' -> 'tshirts'."""
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _singular(value: Any) -> str:
    """`flat` with a trailing plural 's' dropped: 'Leather Jackets' matches
    'Leather Jacket'. Not a stemmer — see approval._singular for why."""
    s = flat(value)
    return s[:-1] if s.endswith("s") and len(s) > 3 else s


# --------------------------------------------------------------------------- #
# What a root implies
# --------------------------------------------------------------------------- #

def root_gender(master: Any, pol: dict[str, Any] | None) -> str | None:
    """'men' | 'women' | None — what this master category implies.

    From `readiness.master.gender_by_root`, matched case-insensitively, so a
    tenant whose root is spelled 'MEN' still resolves. None for a root that
    implies nothing (Unisex, Kids) and for anything unknown.
    """
    if not master:
        return None
    table = config(pol)["master"].get("gender_by_root") or {}
    want = flat(master)
    for root, gender in table.items():
        if flat(root) == want:
            return str(gender).strip().lower() or None
    return None


def is_kids(master: Any, mannequin: Any = None, pol: dict[str, Any] | None = None) -> bool:
    """A child model's product: a Kids root, or the Kids rig already on it.

    Readiness phase 4. The prompt builder renders a child for the `Kids` rig
    (nanobanana.py) and phase 1 derives that rig from a Kids root, so either
    says the same thing; the gate uses it to expect no adult build.
    """
    roots = {flat(r) for r in (config(pol).get("kids_roots") or [])}
    return flat(master) in roots or flat(mannequin) == "kids"


def is_genderless_root(master: Any, pol: dict[str, Any] | None) -> bool:
    if not master:
        return False
    want = flat(master)
    return any(flat(r) == want for r in config(pol)["master"].get("genderless_roots") or [])


def product_gender(p: ProductSnapshot, pol: dict[str, Any] | None) -> str | None:
    """The gender the product should be treated as: the master's, else the
    property's (Unisex/Kids), else None. Never the mannequin's."""
    return root_gender(p.master_category, pol) or resolve_gender(p.gender)


# --------------------------------------------------------------------------- #
# Which root the product belongs to
# --------------------------------------------------------------------------- #

def match_root(master: Any, roots: list[str]) -> str | None:
    """The tenant's own spelling of `master`, or None if no root matches."""
    if not master:
        return None
    want = flat(master)
    return next((r for r in roots if flat(r) == want), None)


def roots_for_category(category: Any, categories: dict[str, dict[str, list[str]]],
                       subcategory: Any = None) -> list[str]:
    """Roots whose tree holds this category (and subcategory, when given).

    Case, punctuation and a trailing plural are ignored on both sides. Used
    when the master is blank or unknown: a category that exists under exactly
    one root names the root.
    """
    if not category:
        return []
    want_cat = _singular(category)
    want_sub = _singular(subcategory) if subcategory else None
    hits: list[str] = []
    for root, branch in (categories or {}).items():
        for cat, subs in (branch or {}).items():
            if _singular(cat) != want_cat:
                continue
            if want_sub and subs and not any(_singular(s) == want_sub for s in subs):
                continue
            hits.append(root)
            break
    return hits


def decide_master(p: ProductSnapshot, pol: dict[str, Any] | None) -> dict[str, Any]:
    """Which master category this product should carry, and why.

    Returns {action, master, basis, detail, rule}:

        keep        the column is a tenant root — the anchor stands
        set         a root was found; `master` is the tenant's spelling of it,
                    `rule` the finding it repairs (TAX.001 for a wrong value,
                    DATA.010 for a blank one)
        unresolved  no root fits; the product must hold
        skip        no catalog to judge against, or readiness is off

    The order of evidence is deliberate and data-only. The tenant's spelling of
    the same word first (an exact-tree miss on 'men' vs 'Men' is a repair, not
    a hold). Then the gender property, because a product filed under no root
    with gender 'women' belongs under the tenant's Women root if it has one.
    Then the category path: a category that exists under exactly one root
    ('Dresses' only under Women) names the root; several roots are narrowed by
    the subcategory. The mannequin rig is never consulted, and neither is the
    render — see the module docstring.
    """
    if not enabled(pol) or not p.catalog or not p.catalog.categories:
        return {"action": "skip", "master": p.master_category, "basis": "no catalog",
                "detail": "no tenant tree to anchor on", "rule": None}

    roots = list(p.catalog.categories)
    current = (p.master_category or "").strip()
    hit = match_root(current, roots)
    if hit is not None:
        if hit == current:
            return {"action": "keep", "master": hit, "basis": "column",
                    "detail": f"'{hit}' is a tenant root", "rule": None}
        return {"action": "set", "master": hit, "basis": "tenant spelling",
                "detail": f"'{current}' is the tenant's '{hit}'", "rule": "TAX.001"}

    rule = "TAX.001" if current else "DATA.010"

    # The gender property names a root.
    gender = resolve_gender(p.gender)
    if gender:
        for root in roots:
            if root_gender(root, pol) == gender:
                return {"action": "set", "master": root, "basis": "gender property",
                        "detail": f"gender {p.gender!r} names the '{root}' root", "rule": rule}

    # The category path exists under exactly one root.
    hits = roots_for_category(p.category, p.catalog.categories)
    if len(hits) > 1 and p.subcategory:
        narrowed = roots_for_category(p.category, p.catalog.categories, p.subcategory)
        if narrowed:
            hits = narrowed
    if len(hits) == 1:
        return {"action": "set", "master": hits[0], "basis": "category path",
                "detail": (f"'{p.category}'"
                           + (f" > '{p.subcategory}'" if p.subcategory and hits else "")
                           + f" exists only under '{hits[0]}'"),
                "rule": rule}

    return {
        "action": "unresolved", "master": None, "basis": "none",
        "detail": (
            (f"'{current}' is not one of this tenant's roots ({', '.join(roots)})"
             if current else "no master category")
            + "; the gender property "
            + (f"({p.gender!r}) names no root" if p.gender else "is empty")
            + (f" and '{p.category}' sits under {len(hits)} roots" if len(hits) > 1
               else f" and '{p.category}' is under no root" if p.category
               else " and there is no category")
        ),
        "rule": rule,
    }


# --------------------------------------------------------------------------- #
# The rig, derived
# --------------------------------------------------------------------------- #

def derive_rig(master: Any, gender: Any, category: Any, subcategory: Any,
               pol: dict[str, Any] | None) -> str | None:
    """The mannequin rig the product should carry, from the master and the side.

    Mirrors `inferMannequinType` in vnyx-api helpers/formatters.ts, so the
    value written here is one the edit screen's dropdown offers:

        Kids root                       -> 'Kids'
        a bottom-body garment           -> 'Bottom'   (no gender word)
        otherwise, men / women / other  -> 'Men Top' / 'Women Top' / 'Top'

    The gender comes from the master (Men/Women) or, for a genderless root,
    from the gender property. Never from the rig that is being replaced.
    None only when there is nothing to derive from at all.
    """
    cfg = config(pol)["mannequin"]
    if master and flat(master) in {flat(r) for r in ("kids", "kid", "children")}:
        return str(cfg.get("kids") or "Kids")
    side = _side_of(subcategory, pol or {}) or _side_of(category, pol or {})
    if side == "bottom":
        return str(cfg.get("bottom") or "Bottom")
    g = root_gender(master, pol) or resolve_gender(gender)
    upper = cfg.get("upper") or {}
    if g and upper.get(g):
        return str(upper[g])
    if master or category or subcategory:
        return str(upper.get("any") or "Top")
    return None


# --------------------------------------------------------------------------- #
# House choices
# --------------------------------------------------------------------------- #

def usage_pick(counts: dict[str, int], candidates: list[str],
               pol: dict[str, Any] | None) -> str | None:
    """The candidate the tenant uses most, when it clearly dominates.

    `counts` is a usage table keyed by candidate name (any casing). Wins only
    at >= `usage_tiebreak_ratio` times the runner-up, so a near-tie is still a
    human's — the same rule `_plan_subcategory` applied to its title matches.
    """
    if not candidates:
        return None
    ratio = float(config(pol).get("usage_tiebreak_ratio") or 2)
    by_flat = {}
    for name, n in (counts or {}).items():
        by_flat[flat(name)] = by_flat.get(flat(name), 0) + int(n or 0)
    ranked = sorted(candidates, key=lambda c: by_flat.get(flat(c), 0), reverse=True)
    top = by_flat.get(flat(ranked[0]), 0)
    second = by_flat.get(flat(ranked[1]), 0) if len(ranked) > 1 else 0
    if top > 0 and top >= ratio * max(second, 1):
        return ranked[0]
    return None


def branch_counts(catalog_usage: dict[str, int], master: str, *,
                  subcategory: Any = None, category: Any = None) -> dict[str, int]:
    """Slice the tenant's `branch_usage` ("Master>Category>Sub" -> n).

    With `subcategory`: category -> n, for categories under `master` that file
    this subcategory. With `category`: subcategory -> n under that branch.
    """
    out: dict[str, int] = {}
    want_master = flat(master)
    want_sub = _singular(subcategory) if subcategory else None
    want_cat = _singular(category) if category else None
    for key, n in (catalog_usage or {}).items():
        parts = str(key).split(">")
        if len(parts) != 3 or flat(parts[0]) != want_master:
            continue
        if want_sub is not None and _singular(parts[2]) == want_sub:
            out[parts[1]] = out.get(parts[1], 0) + int(n or 0)
        elif want_cat is not None and _singular(parts[1]) == want_cat:
            out[parts[2]] = out.get(parts[2], 0) + int(n or 0)
    return out


def guide_counts(catalog_usage: dict[str, int], master: Any, category: Any) -> dict[str, int]:
    """Slice `guide_usage` ("Master>Category>Guide" -> n) to guide -> n for one branch."""
    out: dict[str, int] = {}
    want_master, want_cat = flat(master), _singular(category)
    for key, n in (catalog_usage or {}).items():
        parts = str(key).split(">")
        if len(parts) == 3 and flat(parts[0]) == want_master and _singular(parts[1]) == want_cat:
            out[parts[2]] = out.get(parts[2], 0) + int(n or 0)
    return out
