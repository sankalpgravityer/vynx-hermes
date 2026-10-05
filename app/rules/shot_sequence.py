"""The product's IMAGE SEQUENCE: which on-model shots it gets, how many, and in
what order its gallery is shown (3 Oct 2026).

Each tenant has a default sequence (`ImageGenerationSettings.defaultShots`), and
any category or subcategory can carry its own (`ImageShotSequence`). Measured on
production (read only, 3 Oct 2026):

    BOAS default        3/4 front, 3/4 back, full front, full back, close-up,
                        full front AGAIN — six renders, the originals first
    BOAS Men > Shoes    close-up, back close-up, detail macro — three, no torso
    Magic Body default  close-up, back close-up, 3/4 front, 3/4 back

So "five views, one each" — what Hermes counted against — is wrong for all
three: BOAS's second full front was never counted, the shoes were asked for
torso shots they must not have, and a back close-up sits on AI_CLOSEUP beside
the close-up where a VIEW cannot tell them apart. Counting is per SHOT INSTANCE
(a shot and its variation), from the row's `shotKey`.

PORTED from vnyx-api — keep in step with:

    constants/shot-types.ts          SHOT_CATALOG, DEFAULT_SEQUENCE, parseSequence,
                                     normalizeSequence, expandSequence,
                                     buildDisplayPlan, aiShotFromUrl
    services/image-shot-sequence.ts  norm, matchOverride, resolveShotSequence
    services/product-media.ts        slotForRow, applyDisplayPlan, canonicalRank
    services/sequence-plan.ts        planFromRows, looksMislabelled

vnyx-api renders from the same rules (backfill-imagery.ts --sequence) and orders
the gallery cache by them (rebuildMediaCache), so what Hermes counts and checks
is what vnyx-api makes and shows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# shot -> (view, url suffix, conditioned on the front reference)
SHOT_CATALOG: dict[str, tuple[str, str, bool]] = {
    "full_front": ("AI_FRONT", "front", False),
    "full_back": ("AI_BACK", "back", True),
    "full_side": ("AI_FRONT_34", "side", True),
    "three_quarter_front": ("AI_FRONT_34", "front-34", True),
    "three_quarter_back": ("AI_BACK_34", "back-34", True),
    "three_quarter_side": ("AI_FRONT_34", "side-34", True),
    "closeup": ("AI_CLOSEUP", "closeup", True),
    "closeup_back": ("AI_CLOSEUP", "closeup-back", True),
    "detail_macro": ("AI_CLOSEUP", "detail", False),
    "on_foot": ("AI_FRONT_34", "on-foot", True),
    "in_hand": ("AI_CLOSEUP", "in-hand", True),
    "flat_lay": ("AI_FRONT", "flat-lay", False),
}
ANCHOR_SHOT = "full_front"
MAX_COUNT_PER_SHOT = 5
MAX_SHOTS_PER_SEQUENCE = 12

# The historical five, when a tenant configured nothing.
DEFAULT_SEQUENCE: list[dict[str, Any]] = [
    {"shot": "three_quarter_front", "count": 1, "enabled": True},
    {"shot": "three_quarter_back", "count": 1, "enabled": True},
    {"shot": "full_front", "count": 1, "enabled": True},
    {"shot": "full_back", "count": 1, "enabled": True},
    {"shot": "closeup", "count": 1, "enabled": True},
]

# The fixed media slots, in catalogue (= default) order.
MEDIA_SLOT_KEYS = [
    "original_decision_front", "original_decision_back",
    "original_photobooth_front", "original_photobooth_back", "labels",
]

# A render with no shot key and no recognised url suffix: its view's shot.
LEGACY_VIEW_TO_SHOT = {
    "AI_FRONT": "full_front", "AI_BACK": "full_back", "AI_FRONT_34": "three_quarter_front",
    "AI_BACK_34": "three_quarter_back", "AI_CLOSEUP": "closeup",
}
AI_VIEWS = ("AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP")
GARMENT_VIEWS = ("FRONT", "BACK", "OTHER")


def is_shot(value: Any) -> bool:
    return isinstance(value, str) and value in SHOT_CATALOG


def _clamp(value: Any) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        n = 1
    return min(max(n or 1, 1), MAX_COUNT_PER_SHOT)


def parse_sequence(value: Any) -> list[dict[str, Any]] | None:
    """The shot entries of a stored sequence, or None when nothing in it renders."""
    if not isinstance(value, list):
        return None
    entries = [
        {"shot": e["shot"], "count": _clamp(e.get("count", 1)),
         "promptNote": e.get("promptNote") if isinstance(e.get("promptNote"), str) else None,
         "enabled": e.get("enabled") is not False}
        for e in value if isinstance(e, dict) and is_shot(e.get("shot"))
    ]
    return entries if any(e["enabled"] for e in entries) else None


def normalize_sequence(value: Any) -> list[dict[str, Any]]:
    """Every slot exactly once (first position wins, missing ones appended enabled)."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in value if isinstance(value, list) else []:
        if not isinstance(e, dict):
            continue
        if e.get("slot") in MEDIA_SLOT_KEYS:
            if e["slot"] in seen:
                continue
            seen.add(e["slot"])
            out.append({"slot": e["slot"], "enabled": e.get("enabled") is not False})
        elif is_shot(e.get("shot")):
            out.append({"shot": e["shot"], "count": _clamp(e.get("count", 1)),
                        "promptNote": e.get("promptNote") if isinstance(e.get("promptNote"), str) else None,
                        "enabled": e.get("enabled") is not False})
    out.extend({"slot": s, "enabled": True} for s in MEDIA_SLOT_KEYS if s not in seen)
    return out


@dataclass(frozen=True)
class Instance:
    """One render the sequence asks for: a shot and its variation."""

    shot: str
    position: int
    variation: int
    variation_count: int

    @property
    def view(self) -> str:
        return SHOT_CATALOG[self.shot][0]

    @property
    def label(self) -> str:
        return f"{self.shot}#{self.variation}" if self.variation > 1 else self.shot


def expand_sequence(entries: Iterable[dict[str, Any]]) -> list[Instance]:
    """Counts applied, repeats numbered per shot across the whole sequence."""
    flat: list[str] = []
    for e in entries or []:
        if not isinstance(e, dict) or e.get("enabled") is False or not is_shot(e.get("shot")):
            continue
        for _ in range(_clamp(e.get("count", 1))):
            if len(flat) >= MAX_SHOTS_PER_SEQUENCE:
                break
            flat.append(e["shot"])
        if len(flat) >= MAX_SHOTS_PER_SEQUENCE:
            break
    totals = {s: flat.count(s) for s in set(flat)}
    seen: dict[str, int] = {}
    out: list[Instance] = []
    for position, shot in enumerate(flat):
        seen[shot] = seen.get(shot, 0) + 1
        out.append(Instance(shot, position, seen[shot], totals[shot]))
    return out


# --------------------------------------------------------------------------- #
# Which sequence a product gets
# --------------------------------------------------------------------------- #

def norm(value: Any) -> str | None:
    """Letters and digits, lower case, a trailing plural dropped; None for blank / Unknown."""
    if not isinstance(value, str):
        return None
    flat = re.sub(r"[^a-z0-9]", "", value.lower())
    if not flat or flat == "unknown":
        return None
    return flat[:-1] if flat.endswith("s") and len(flat) > 3 else flat


@dataclass
class Override:
    """One category's stored sequence, with the category's path (leaf first)."""

    shots: Any
    name: str
    path: list[str]


def override_candidates(rows: Iterable[tuple[Any, str | None, str | None, str | None]]) -> list[Override]:
    """`(shots, category, parent, grandparent)` rows -> the usable overrides."""
    out: list[Override] = []
    for shots, cat, parent, grand in rows:
        name = norm(cat)
        if not name or not parse_sequence(shots):
            continue
        out.append(Override(shots=shots, name=str(cat),
                            path=[p for p in (name, norm(parent), norm(grand)) if p]))
    return out


def match_override(candidates: list[Override], *, category: Any = None, sub_category: Any = None,
                   master_category: Any = None, mannequin_type: Any = None) -> Override | None:
    """subCategory -> category -> masterCategory -> Kids; a known ancestor that differs rules a
    candidate out, a missing one does not; the most confirmed ancestors win."""
    levels = [norm(sub_category), norm(category), norm(master_category)]
    tries = [(name, levels[i + 1:]) for i, name in enumerate(levels) if name]
    if mannequin_type == "Kids":
        tries.append(("kid", []))
    for name, ancestors in tries:
        best, best_score = None, -1
        for c in candidates:
            if c.path[0] != name:
                continue
            score, contradicted = 0, False
            for j, expected in enumerate(ancestors):
                actual = c.path[1 + j] if len(c.path) > 1 + j else None
                if not expected or not actual:
                    continue
                if expected == actual:
                    score += 1
                else:
                    contradicted = True
            if not contradicted and score > best_score:
                best, best_score = c, score
        if best:
            return best
    return None


@dataclass
class Resolved:
    """The product's sequence: the AI shots, the whole line-up, and where it came from."""

    shots: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    source: str                      # category | tenant-default | built-in
    matched_category: str | None = None
    expanded: list[Instance] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.expanded:
            self.expanded = expand_sequence(self.shots)

    def describe(self) -> str:
        if self.source == "category":
            return f"the {self.matched_category} sequence"
        return "the tenant's default sequence" if self.source == "tenant-default" else "the built-in sequence"

    def as_dict(self) -> dict[str, Any]:
        return {"source": self.source, "matchedCategory": self.matched_category,
                "shots": self.shots, "lines": self.lines}


def resolve(*, overrides: list[Override], default_shots: Any, is_close_up_enabled: Any = True,
            category: Any = None, sub_category: Any = None, master_category: Any = None,
            mannequin_type: Any = None) -> Resolved:
    """resolveShotSequence: the matching override, else the tenant default, else the built-in
    five (minus the close-ups when the tenant's legacy close-up switch is off)."""
    hit = match_override(overrides, category=category, sub_category=sub_category,
                         master_category=master_category, mannequin_type=mannequin_type)
    if hit:
        return Resolved(parse_sequence(hit.shots) or [], normalize_sequence(hit.shots),
                        "category", hit.name)
    tenant = parse_sequence(default_shots)
    if tenant:
        return Resolved(tenant, normalize_sequence(default_shots), "tenant-default")
    shots = [dict(s) for s in DEFAULT_SEQUENCE]
    if is_close_up_enabled is False:
        shots = [s for s in shots if s["shot"] not in ("closeup", "detail_macro")]
    return Resolved(shots, normalize_sequence(shots), "built-in")


def from_payload(value: Any) -> Resolved | None:
    """The sequence vnyx-api resolved and sent (`product.shotSequence`)."""
    if not isinstance(value, dict):
        return None
    shots = parse_sequence(value.get("shots"))
    if not shots:
        return None
    lines = value.get("lines")
    return Resolved(shots, normalize_sequence(lines if isinstance(lines, list) else shots),
                    str(value.get("source") or "tenant-default"),
                    value.get("matchedCategory") or None)


# --------------------------------------------------------------------------- #
# The product's renders against it
# --------------------------------------------------------------------------- #

# Longest suffix first: `-gen-back-34` before `-gen-back` (aiShotFromUrl).
_SUFFIXES = sorted(((f"-gen-{suffix}", shot) for shot, (_v, suffix, _r) in SHOT_CATALOG.items()),
                   key=lambda t: -len(t[0]))


def _get(row: Any, *names: str) -> Any:
    for n in names:
        v = row.get(n) if isinstance(row, dict) else getattr(row, n, None)
        if v is not None:
            return v
    return None


def shot_of(row: Any) -> str | None:
    """A render's shot: its shotKey, else its url's `-gen-` suffix, else its view's."""
    key = _get(row, "shot_key", "shotKey")
    if is_shot(key):
        return key
    url = str(_get(row, "url") or "")
    for needle, shot in _SUFFIXES:
        if needle in url:
            return shot
    return LEGACY_VIEW_TO_SHOT.get(str(_get(row, "view") or ""))


@dataclass
class Plan:
    resolved: Resolved
    expected: list[Instance]
    present: list[tuple[Instance, Any]]
    missing: list[Instance]
    extra: list[tuple[Any, str | None]]

    @property
    def expected_views(self) -> set[str]:
        return {i.view for i in self.expected}

    def summary(self) -> dict[str, Any]:
        return {"source": self.resolved.source, "matched_category": self.resolved.matched_category,
                "expected": [i.label for i in self.expected],
                "present": [i.label for i, _ in self.present],
                "missing": [i.label for i in self.missing],
                "extra": [str(_get(r, "url") or "") for r, _ in self.extra]}


def plan(resolved: Resolved, ai_rows: Iterable[Any]) -> Plan:
    """Pair the expected instances with the live AI rows: the nth render of a shot (by
    position) is its nth instance; what is left over is outside the sequence."""
    by_shot: dict[str, list[Any]] = {}
    extra: list[tuple[Any, str | None]] = []
    for row in ai_rows:
        shot = shot_of(row)
        if shot is None:
            extra.append((row, None))
        else:
            by_shot.setdefault(shot, []).append(row)
    for rows in by_shot.values():
        rows.sort(key=lambda r: int(_get(r, "position") or 0))
    present, missing = [], []
    for inst in resolved.expanded:
        rows = by_shot.get(inst.shot) or []
        if len(rows) >= inst.variation:
            present.append((inst, rows[inst.variation - 1]))
        else:
            missing.append(inst)
    for shot, rows in by_shot.items():
        wanted = sum(1 for i in resolved.expanded if i.shot == shot)
        extra.extend((r, shot) for r in rows[wanted:])
    return Plan(resolved, list(resolved.expanded), present, missing, extra)


def looks_mislabelled(p: Plan, ai_rows: list[Any]) -> bool:
    """Old renders with no shot key, all under one view, where the sequence expects several
    views — a relabel (IMG.021), not a gap. Three close-ups on a close-up-only sequence are
    not that."""
    views = {str(_get(r, "view")) for r in ai_rows}
    return (len(ai_rows) >= 2 and len(views) == 1 and len(p.expected_views) > 1
            and not any(is_shot(_get(r, "shot_key", "shotKey")) for r in ai_rows))


def single_shot_views(p: Plan) -> dict[str, Instance]:
    """Views the sequence fills with exactly one LEGACY shot instance.

    The only views Hermes' own view generator (/v1/imagery/generate) can make for this
    product without making the wrong shot or replacing a neighbour: AI_CLOSEUP on a sequence
    with a close-up AND a back close-up is two renders, and asking the view generator for
    "AI_CLOSEUP" would produce one front close-up and retire both."""
    by_view: dict[str, list[Instance]] = {}
    for inst in p.expected:
        by_view.setdefault(inst.view, []).append(inst)
    return {v: insts[0] for v, insts in by_view.items()
            if len(insts) == 1 and LEGACY_VIEW_TO_SHOT.get(v) == insts[0].shot}


# --------------------------------------------------------------------------- #
# The gallery order (applyDisplayPlan)
# --------------------------------------------------------------------------- #

_VIEW_RANK = {"AI_FRONT_34": 0, "AI_BACK_34": 1, "AI_FRONT": 2, "AI_BACK": 3, "AI_CLOSEUP": 4,
              "FRONT": 10, "BACK": 11, "OTHER": 12, "VIDEO_TURNTABLE": 15, "LABEL": 20,
              "SIZE_CHART": 30}
_BAND_RANK = {"AI_FRONT_34": 0, "AI_BACK_34": 0, "AI_FRONT": 0, "AI_BACK": 0, "AI_CLOSEUP": 0,
              "FRONT": 1, "BACK": 1, "OTHER": 1, "VIDEO_TURNTABLE": 2, "LABEL": 3, "SIZE_CHART": 4}
_PROCESSING_RANK = {"GENERATED": 0, "COMPOSITED": 1, "BG_REMOVED": 1, "TRANSCODED": 1, "RAW": 2}
ORIGIN_ORDER = ["AI", "MANUAL", "WEB", "PHOTOBOOTH", "DECISION", "SIZE_GUIDE"]


@dataclass
class DisplayPlan:
    shot_ordinals: dict[str, list[float]]
    slot_ordinal: dict[str, float]
    hidden_slots: set[str]
    first_shot_ordinal: float
    after_originals_ordinal: float
    end_ordinal: float


def build_display_plan(lines: Any) -> DisplayPlan:
    shot_ordinals: dict[str, list[float]] = {}
    slot_ordinal: dict[str, float] = {}
    hidden: set[str] = set()
    ordinal, first, last_original = 0, -1, -1
    for line in normalize_sequence(lines):
        if "slot" in line:
            slot_ordinal[line["slot"]] = ordinal
            if not line["enabled"]:
                hidden.add(line["slot"])
            if line["slot"] != "labels":
                last_original = ordinal
            ordinal += 1
            continue
        if not line["enabled"]:
            continue
        for _ in range(_clamp(line.get("count", 1))):
            if first < 0:
                first = ordinal
            shot_ordinals.setdefault(line["shot"], []).append(ordinal)
            ordinal += 1
    return DisplayPlan(shot_ordinals, slot_ordinal, hidden, first if first >= 0 else 0,
                       last_original + 0.5, ordinal)


def slot_for_row(view: str, origin: str | None) -> str | None:
    if view == "LABEL":
        return "labels"
    origin = (origin or "").upper()
    if origin == "DECISION" and view in ("FRONT", "BACK"):
        return f"original_decision_{view.lower()}"
    if origin in ("PHOTOBOOTH", "WEB") and view in ("FRONT", "BACK"):
        return f"original_photobooth_{view.lower()}"
    return None


def canonical_rank(view: str, origin: str | None, processing: str | None,
                   origin_order: list[str] | None = None) -> int:
    order = [o.upper() for o in (origin_order or ORIGIN_ORDER)]
    o = (origin or "").upper()
    o_rank = order.index(o) if o in order else len(order)
    v_rank = _VIEW_RANK.get(view, 99)
    band = _BAND_RANK.get(view, 5)
    inner = o_rank * 1000 + v_rank * 10 if band == 1 else v_rank * 1000 + o_rank * 10
    return _PROCESSING_RANK.get(processing or "RAW", 2) * 1_000_000 + band * 100_000 + inner


def display_order(rows: list[Any], lines: Any, origin_order: list[str] | None = None) -> list[Any]:
    """The live rows in the order rebuildMediaCache writes `Product.images`: rows of a
    disabled slot dropped; the nth render of a shot (by position) at that shot's nth
    ordinal; then canonical rank, then position."""
    dp = build_display_plan(lines)

    def hidden(r: Any) -> bool:
        slot = slot_for_row(str(_get(r, "view")), _get(r, "origin"))
        return bool(slot and slot in dp.hidden_slots)

    visible = [r for r in rows if not hidden(r)]
    shot_ord: dict[int, float] = {}
    by_shot: dict[str, list[Any]] = {}
    for r in visible:
        key = _get(r, "shot_key", "shotKey")
        if key and str(_get(r, "view")) in AI_VIEWS:
            by_shot.setdefault(str(key), []).append(r)
    for key, rs in by_shot.items():
        ords = dp.shot_ordinals.get(key) or []
        last = ords[-1] if ords else dp.first_shot_ordinal
        for n, r in enumerate(sorted(rs, key=lambda x: int(_get(x, "position") or 0))):
            shot_ord[id(r)] = ords[n] if n < len(ords) else last + 0.25

    def ordinal(r: Any) -> float:
        if id(r) in shot_ord:
            return shot_ord[id(r)]
        view = str(_get(r, "view"))
        if view in AI_VIEWS:
            return dp.first_shot_ordinal
        slot = slot_for_row(view, _get(r, "origin"))
        if slot:
            return dp.slot_ordinal.get(slot, dp.end_ordinal)
        if view in GARMENT_VIEWS:
            return dp.after_originals_ordinal
        if view == "VIDEO_TURNTABLE":
            return dp.end_ordinal
        return dp.end_ordinal + 1

    return sorted(visible, key=lambda r: (
        ordinal(r),
        canonical_rank(str(_get(r, "view")), _get(r, "origin"), _get(r, "processing"), origin_order),
        int(_get(r, "position") or 0),
    ))
