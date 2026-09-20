"""Approval-gate rules.

The checks that matter when a product is about to be APPROVED, as opposed to
merely listed in Review with advisories next to it.

Deliberately NOT registered in `rules/__init__.py`'s REGISTRY. Everything here is
HIGH severity, and the default severity floor on /v1/review-queue is `medium` —
so adding these to the shared registry would flip a large slice of the existing
review queue from correct to incorrect overnight, changing an endpoint the UI
already depends on. `app/approval.py` runs them explicitly alongside `run_all`.

Three rules, each answering a question the existing rule set does not:

  DATA.010  a required field is ABSENT.
            check_completeness's DATA.001 fires on a PLACEHOLDER ("n/a", "-"),
            which is a different fault: someone filled it in badly rather than
            never at all. It is also MEDIUM, correct for an advisory list and too
            low for a gate.

  IMG.030   no care label on file.
            Not checked anywhere in Hermes before now, though vnyx-api has been
            sending `careLabelCount` on every feed record all along. VNYX treats a
            missing label as a blocking state of its own (`deriveStage` returns
            MISSING_LABEL rather than PHOTOBOOTH), so approving without one
            contradicts the stage machine.

  SIZE.010  no sizing chart covers this product.
            SIZE.002 validates a product AGAINST its chart and stays silent when
            there is no chart to validate against — the gap this closes. Carries
            the seed for the chart in `detail`, so the repair step does not have
            to re-derive it.
"""

from __future__ import annotations

from typing import Any

from app.models import Finding, ProductSnapshot, Severity

# Fields a product must actually carry before it can be approved.
#
# `price` is absent on purpose: PRICE.010 already reports a missing price as
# CRITICAL with the full assessment attached, and naming it here too produced a
# second, vaguer finding for the same fact — the same reasoning
# check_completeness documents for its own list.
REQUIRED_FIELDS: tuple[str, ...] = (
    "brand",
    "size",
    "eu_size",
    "master_category",
    "category",
    "subcategory",
    "color",
    "material",
    "condition",
    # Absent gender only. A MULTI-valued gender is not this rule's business — see
    # the note on GENDER.001 below.
    "gender",
)

# Guide names that carry no gender signal of their own.
#
# "Defaults" is the one that matters. scripts/backfill-eu-sizes.ts picks its
# letter table with a module constant (`DEFAULTS_TABLE = MEN`), so a WOMEN'S
# product left on it is silently converted against the men's table — S becomes
# 46 instead of 36. That is why sitting on a generic guide is worth reporting
# even though the guide exists and is valid.
_GENERIC_GUIDES = {"defaults", "default", "generic", "kids"}

_MEN_WORDS = ("men", "mens", "man", "male")
_WOMEN_WORDS = ("women", "womens", "woman", "female", "ladies")


def resolve_gender(value: Any) -> str | None:
    """'men' | 'women' | None from any of the shapes VNYX stores.

    Mirrors `resolveGender` in vnyx-ui/src/lib/size-conversion.ts and
    `normalizeGenderToArray` in vnyx-api/src/services/openai.ts, which between
    them accept a string, a comma list, a JSON array string, and a real array.

    Returns None where those return a DEFAULT. The TypeScript pair fall back to
    'women' when nothing matches, which is right for rendering a dropdown and
    wrong here: a rule that cannot tell must stay silent rather than assert a
    gender the record does not carry. None is also returned for genuinely
    ambiguous input, so callers cannot mistake "both" for a decision.
    """
    if value is None:
        return None

    parts: list[str] = []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        text = str(value).strip()
        if not text:
            return None
        if text.startswith("["):
            try:
                import json

                parsed = json.loads(text)
                parts = [str(v) for v in parsed] if isinstance(parsed, list) else [text]
            except ValueError:
                parts = text.split(",")
        else:
            parts = text.split(",")

    joined = " ".join(parts).lower()
    if not joined.strip():
        return None

    found: set[str] = set()
    if "unisex" in joined or "both" in joined:
        found |= {"men", "women"}
    if any(w in joined for w in _WOMEN_WORDS):
        found.add("women")
    # "men" must not match inside "women" — the same trick normalizeGenderToArray
    # uses, blanking every wo/men occurrence before looking for men.
    demasked = joined.replace("women", " ").replace("womens", " ").replace("woman", " ")
    if any(
        w in demasked.split() or f" {w} " in f" {demasked} " for w in _MEN_WORDS
    ):
        found.add("men")

    if len(found) == 1:
        return found.pop()
    return None  # nothing recognised, or both — neither is a decision


def guide_gender(name: str | None) -> str | None:
    """The gender a sizing-guide NAME implies, or None if it implies none.

    Mirrors `isMenName` in scripts/backfill-eu-sizes.ts ("men if it says
    men/male and not women, else women") with one deliberate difference: that
    function has to return a table so it resolves "Defaults" via a constant,
    while this returns None. A guide with no gender in its name genuinely has
    none, and pretending otherwise is how a women's garment ends up sized
    against the men's table.
    """
    if not name:
        return None
    low = name.strip().lower()
    if low in _GENERIC_GUIDES:
        return None
    has_women = any(w in low for w in _WOMEN_WORDS)
    has_men = any(w in low.replace("women", " ") for w in _MEN_WORDS)
    if has_women and not has_men:
        return "women"
    if has_men and not has_women:
        return "men"
    return None


def is_generic_guide(name: str | None) -> bool:
    return bool(name) and name.strip().lower() in _GENERIC_GUIDES

# Human-facing names, so a finding message reads like the edit screen's label
# rather than like a Python attribute.
_LABELS = {
    "eu_size": "EU size",
    "master_category": "master category",
    "subcategory": "sub category",
}


def _absent(value: Any) -> bool:
    """Nothing there at all.

    Narrower than check_completeness's `_is_placeholder`, which also catches
    "unknown" / "n/a" / "-". Those stay DATA.001's business; this is only about a
    field nobody ever wrote.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def expected_guide_name(p: ProductSnapshot) -> str | None:
    """A name for a chart that has to be created from scratch.

    ONLY used when the tenant has no chart covering this product at all, and
    only ever for bottoms — see `seed_chart`, which is the only thing that can
    fill one in.

    Deliberately NOT used to look up an existing chart. Deriving
    "{masterCategory} {category}" and matching on it is wrong, and
    consistency.py:155 records why: the tenant's real guide names are "Men
    Bottoms", "Men Uppers", "Men DressShirts", "Women Uppers", "Defaults",
    "Kids", so the derived form only ever coincides for Bottoms. A men's t-shirt
    sits under `Men > T-Shirts & Polos` and correctly uses "Men Uppers"; matching
    it against "Men T-Shirts & Polos" produced a finding on every top in the
    catalog, which TAX.006 already had to be rewritten to stop doing.
    """
    if _absent(p.master_category) or _absent(p.category):
        return None
    return f"{str(p.master_category).strip()} {str(p.category).strip()}"


def _size_candidates(p: ProductSnapshot) -> list[str]:
    """The size spellings to look for in a chart. Stored data uses several.

    `waist` is only a size when it is NUMERIC. Real records use that column for
    a fit descriptor too — "Mid" turns up as a waist — and feeding it in
    produced the nonsense candidates "Mid" and "WMid", which can never match a
    ladder and so pushed SIZE.013 into reporting a perfectly good product.
    """
    waist = str(p.waist).strip() if p.waist else None
    if waist and not _num(waist):
        waist = None
    return [
        str(v).strip()
        for v in (p.size, p.international_size, waist,
                  f"W{waist}" if waist else None)
        if v and str(v).strip()
    ]


def _num(text: str) -> str | None:
    """The number in a size, or None. "W32" -> "32", "32.0" -> "32"."""
    digits = "".join(ch for ch in str(text) if ch.isdigit() or ch == ".")
    if not digits or digits == ".":
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    return str(int(value)) if value == int(value) else str(value)


def _size_key(text: str) -> tuple[str, str] | None:
    """A size as (family, value), so two spellings of one size compare equal.

    ("num", "32")   from "32", "W32", "w 32", "32.0"
    ("alpha", "m")  from "M", " m ", "M/L" -> "ml"

    The FAMILY is what makes the comparison honest. A letter size and a waist
    number are not the same kind of thing, so "no match" between them says
    nothing about correctness — it says they cannot be compared. Treating that as
    a defect is what made SIZE.013 block a fine product.
    """
    raw = str(text).strip().lower()
    if not raw:
        return None
    number = _num(raw)
    if number is not None:
        return ("num", number)
    letters = "".join(ch for ch in raw if ch.isalpha())
    return ("alpha", letters) if letters else None


def product_gender(p: ProductSnapshot) -> str | None:
    """This product's gender, master category first.

    `masterCategory` is preferred over the `gender` attribute because it is the
    field the tenant's taxonomy and charts are organised by, and the one a
    reviewer sees at the top of the edit screen. The attribute is the fallback,
    and returns None when it holds both — a product still awaiting the gender
    split has not been decided yet.
    """
    return resolve_gender(p.master_category) or resolve_gender(p.gender)


def candidate_guides(p: ProductSnapshot, *,
                     match_gender: bool = True,
                     pol: dict[str, Any] | None = None) -> list[str]:
    """Tenant charts that contain this product's size.

    Membership, not name derivation — the only way to find the right chart
    without knowing the tenant's category-to-guide convention, which is tenant
    knowledge Hermes does not hold.

    `match_gender` then narrows to charts whose NAME agrees with the product's
    gender, which is what separates "Men Uppers" from "Women Uppers" when both
    list "S". Generic charts ("Defaults") are excluded from a gender-matched
    search on purpose: they are a valid fallback but never the best answer, so
    including them would make every lookup ambiguous and escalate instead of
    resolving.
    """
    if not p.catalog:
        return []
    wanted = {s.lower() for s in _size_candidates(p)}
    if not wanted:
        return []

    want_gender = product_gender(p) if match_gender else None

    hits: list[str] = []
    for name, pair in (p.catalog.sizing_guides or {}).items():
        sizes = {str(s).strip().lower() for s in (pair.get("sizes") or [])}
        if not (wanted & sizes):
            continue
        if want_gender is not None:
            if is_generic_guide(name):
                continue
            gg = guide_gender(name)
            # Unknown-gender named charts are kept: a tenant may use a name this
            # module cannot parse, and dropping it would hide the only match.
            if gg is not None and gg != want_gender:
                continue
        hits.append(name)

    # Narrow to the right HALF OF THE BODY when both sides can be read.
    #
    # Without this a women's tee whose size is "S" matched "Women Uppers" AND
    # "Women Bottoms" — a bottoms guide that lists letter sizes is
    # indistinguishable by ladder alone — so the lookup was ambiguous and
    # resolved to nothing. Applied as a FILTER that is dropped when it would
    # empty the list, so it can only ever improve the answer.
    if pol is not None:
        want_side = category_side(p, pol)
        if want_side:
            same_side = [
                n for n in hits
                if (guide_side(n, pol) or want_side) == want_side
            ]
            if same_side:
                hits = same_side

        # Drop charts named for a GARMENT this product is not.
        #
        # Same filter shape as the side narrowing above, and dropped the same way
        # when it would empty the list, so it can only improve the answer.
        #
        # This tenant has "Men Uppers" AND "Men DressShirts", both listing "S".
        # Every men's top on a generic chart therefore matched two charts, and
        # every caller that needs ONE answer — better_guide, the SIZE.012 repair
        # — gave up and escalated. A hoodie is not a dress shirt, and the guide's
        # own name says so.
        #
        # Note which way round this reads. It does NOT derive a guide name from
        # the category, which expected_guide_name explains at length is wrong. It
        # asks whether a guide that has ALREADY matched on gender and ladder
        # names a garment, and if so whether the product is that garment — using
        # the tenant's own category names on one side and the guide's own name on
        # the other. A men's business shirt keeps both candidates and still
        # escalates, which is right: there the ambiguity is real.
        product_garments = _garment_words(
            f"{p.category or ''} {p.subcategory or ''}", pol
        )
        general = [
            n for n in hits
            if not _garment_words(n, pol)
            or (_garment_words(n, pol) & product_garments)
        ]
        if general:
            hits = general
    return sorted(hits)


def resolve_guide(p: ProductSnapshot,
                  pol: dict[str, Any] | None = None) -> str | None:
    """The tenant's OWN chart covering this product, if it can be identified.

    Two ways, in order:

      1. The guide already on the product, if the tenant really has it. Cheapest,
         and the normal case.
      2. Exactly ONE tenant chart contains the product's size. Then the answer is
         unambiguous and pointing the product at it is a lookup, not a guess.

    Returns None when several charts match — "Defaults" and "Men Uppers" may both
    list "S" — because picking one arbitrarily would silently attach the wrong
    size table. That case escalates instead; `candidate_guides` names the options
    for whoever looks.
    """
    if not p.catalog:
        return None
    guides = p.catalog.sizing_guides or {}

    if p.sizing_guide:
        low = str(p.sizing_guide).strip().lower()
        for existing in guides:
            if str(existing).strip().lower() == low:
                return existing

    hits = candidate_guides(p, pol=pol)
    if len(hits) == 1:
        return hits[0]
    # Nothing gender-appropriate. A generic chart is a worse answer than a
    # specific one but a much better answer than none, so it is accepted here
    # rather than escalating a product the tenant HAS configured a chart for.
    if not hits:
        fallback = candidate_guides(p, match_gender=False, pol=pol)
        if len(fallback) == 1:
            return fallback[0]
    return None


def _side_of(text: str | None, pol: dict[str, Any]) -> str | None:
    """'upper' | 'bottom' | None for a category or a guide name.

    Substring tokens from `policy.sizing.sides`, so a tenant's real names resolve
    without an exhaustive list. `bottom` is tested FIRST: "Denim Bottoms" would
    otherwise hit the `denim` end of the upper list and read as an upper.

    None on no match, and that is the point — a name this cannot classify makes
    SIZE.014 stay silent rather than decide on a guess.
    """
    if not text:
        return None
    low = str(text).strip().lower()
    sides = (pol.get("sizing") or {}).get("sides") or {}
    for side in ("bottom", "upper"):
        for token in sides.get(side) or []:
            if str(token).lower() in low:
                return side
    return None


def _garment_words(text: str | None, pol: dict[str, Any]) -> set[str]:
    """The GARMENT tokens in a name, excluding the two body-side words.

    `policy.sizing.sides` lists "upper" and "bottom" first and then the garments
    that imply each side. That split is the difference between a chart named for
    a whole half of the body and one named for a particular garment:

        "Men Uppers"       -> {}          the general men's upper chart
        "Men DressShirts"  -> {"shirt"}   a specialisation of it
        "Men Bottoms"      -> {}

    A general chart sizes anything on its side. A specialised one is only the
    right answer for the garment it names — which is a fact about the guide's
    own name, not an inference about the tenant's conventions.
    """
    low = str(text or "").strip().lower()
    if not low:
        return set()
    sides = (pol.get("sizing") or {}).get("sides") or {}
    return {
        str(token).lower()
        for side in ("bottom", "upper")
        for token in (sides.get(side) or [])
        if str(token).lower() not in ("upper", "bottom")
        and str(token).lower() in low
    }


def category_side(p: ProductSnapshot, pol: dict[str, Any]) -> str | None:
    """Which half of the body this product is, from its taxonomy.

    Sub-category first: it is the more specific name, so a "Shorts" leaf under a
    "Bottoms" branch and a "Denim Jacket" leaf under "Jackets" both resolve
    correctly. Falls back to the category.
    """
    return (
        _side_of(p.subcategory, pol)
        or _side_of(p.category, pol)
    )


def guide_side(name: str | None, pol: dict[str, Any]) -> str | None:
    """Which half of the body a guide measures, from its name.

    Generic guides ("Defaults") deliberately resolve to None — they cover both,
    so comparing them against a category would report a mismatch that is not one.
    """
    if is_generic_guide(name):
        return None
    return _side_of(name, pol)


def guide_contains_size(p: ProductSnapshot, guide: str | None = None) -> bool | None:
    """Does this guide's ladder actually list this product's size?

    THE STRONGEST STATEMENT AVAILABLE, and pure tenant data — no naming
    convention assumed. `ProductSize.sizes[]` is the set of sizes a guide can
    express, so a product whose size is absent from its own guide's ladder
    cannot be sized by it at all: there is no index to read `euSizes[]` at.

    This is what catches a WOMEN'S UPPER sitting on "Women Bottoms". The gender
    agrees, so SIZE.011 stays quiet, and the name says nothing this module should
    trust — but a bottoms ladder holds W28/W30/W32 and the product's size is "M",
    so the mismatch is a fact about the data rather than an inference from a
    label.

    None when it cannot be judged: no catalog, no size on the product, or a guide
    with an empty ladder. Absence of evidence, not a defect.
    """
    name = guide or p.sizing_guide
    if not name or not p.catalog:
        return None
    pair = (p.catalog.sizing_guides or {}).get(name)
    if not pair:
        return None
    ladder = {
        k for k in (_size_key(x) for x in (pair.get("sizes") or [])) if k
    }
    if not ladder:
        return None
    wanted = {k for k in (_size_key(x) for x in _size_candidates(p)) if k}
    if not wanted:
        return None

    if wanted & ladder:
        return True

    # No match — but is that a DEFECT or an incomparable pair?
    #
    # Only a defect when the two are the same KIND of size and the ladder simply
    # does not carry this one (an "XXL" product on a chart that stops at "XL").
    # A letter size against a waist-number ladder is a side/format mismatch, and
    # SIZE.014 answers that properly from the category — so this returns None and
    # defers rather than blocking on a comparison it cannot make.
    families = {f for f, _ in ladder}
    if not any(f in families for f, _ in wanted):
        return None
    return False


def better_guide(p: ProductSnapshot,
                 pol: dict[str, Any] | None = None) -> str | None:
    """A chart that fits this product BETTER than the one it is on.

    Answers the "why is it on Defaults" question. Returns None when the current
    guide is already the best available, which includes the case where the
    tenant has nothing more specific.
    """
    current = p.sizing_guide
    if not current or not p.catalog:
        return None

    hits = candidate_guides(p, pol=pol)  # gender- and side-matched
    if len(hits) != 1:
        return None
    best = hits[0]
    return None if best.strip().lower() == current.strip().lower() else best


def seed_chart(p: ProductSnapshot, pol: dict[str, Any]) -> dict[str, Any] | None:
    """Size/EU pairs to seed a NEW chart with, or None if nothing can seed it.

    THIS IS THE WEAKEST LINK IN THE AUTO-REPAIR CHAIN, and it is worth being
    explicit about why rather than discovering it from wrong sizes later.

    The only mapping available to a service that holds no tenant configuration is
    policy.yaml's generic waist->EU table, and that table is known to disagree
    with a real tenant's chart: it maps W28 to EU 40 where the tenant's own chart
    says 36, and W32 to 48 where the tenant says 40. A chart seeded from it is a
    GUESS, and SIZE.002 will subsequently validate every product against it and
    agree — so the error confirms itself and goes quiet.

    Two consequences are built in rather than left to the caller:

      * `provenance: "hermes_generated"` rides on the return value and is written
        into the chart's name suffix by the repair step, so every product sized
        against a guessed chart stays queryable after the fact. Finding the blast
        radius later must not require reading this docstring.

      * Only bottoms can be seeded at all. policy.yaml has waist->EU tables for
        men's and women's bottoms and nothing else, and an alpha chart
        (XS/S/M/L) carries no EU equivalence to invent. Returning None for
        everything else means SIZE.010 escalates instead, which is the honest
        answer for a category nobody has a mapping for.
    """
    master = (p.master_category or "").strip().lower()
    category = (p.category or "").strip().lower()

    if not category.startswith("bottom"):
        return None

    sz = pol.get("sizing") or {}
    table = sz.get(
        "women_bottoms_waist_in_to_eu"
        if master in ("women", "woman", "female")
        else "men_bottoms_waist_in_to_eu"
    )
    if not table:
        return None

    # Numeric order, not dict order: `sizes` and `euSizes` are index-aligned on
    # the VNYX column, and a chart whose rows run 28, 30, 29 reads as broken in
    # the edit screen's dropdown even though every pair in it is correct.
    waists = sorted(table, key=lambda w: int(w))
    return {
        "sizes": [f"W{w}" for w in waists],
        "euSizes": [str(table[w]) for w in waists],
        "basis": "policy_waist_table",
        "provenance": "hermes_generated",
    }


def check_gate(p: ProductSnapshot, pol: dict[str, Any], *,
               split_on_both_genders: bool = False) -> list[Finding]:
    """Everything that must be true before this product may be approved.

    `split_on_both_genders` is the tenant's `duplicateProductOnBothGenders`
    setting, and it changes who OWNS an unresolved gender — see GENDER.001.
    """
    out: list[Finding] = []

    # ---- DATA.010: required fields present ---------------------------------
    for field in REQUIRED_FIELDS:
        if not _absent(getattr(p, field, None)):
            continue
        label = _LABELS.get(field, field.replace("_", " "))
        out.append(Finding(
            rule_id="DATA.010", severity=Severity.HIGH, fields=[field],
            message=f"Required field '{label}' is empty.",
            detail={"field": field},
            # The evidence layer can fill several of these from the photographs,
            # which is the difference between escalating and repairing.
            needs_evidence=True,
        ))

    # ---- GENDER.001: gender is still undecided ------------------------------
    #
    # WHO OWNS THIS depends on one tenant setting, and the difference matters.
    #
    # With `duplicateProductOnBothGenders` ON, a both-genders product belongs to
    # split-gender.worker.ts: it narrows this row to one gender AND creates the
    # copy for the other, regenerating its title, description and renders.
    # Writing a gender here would pre-empt that — and because the splitter's
    # both-gender guard makes a re-run a no-op once the parent is single-gender,
    # THE SECOND PRODUCT WOULD NEVER BE CREATED. A silently lost listing.
    #
    # With it OFF there is no split to protect. analyze.worker narrows inline
    # (`genders = ['men']`, and `resolvedGender` prefers the master category),
    # so deriving it here is the same answer that path would reach — just
    # earlier. `detail.repairable` tells the planner which case it is.
    if resolve_gender(p.gender) is None and not _absent(p.gender):
        out.append(Finding(
            rule_id="GENDER.001", severity=Severity.HIGH, fields=["gender"],
            message=(
                f"Gender '{p.gender}' is not resolved to a single value."
                + (
                    " The gender split decides this; approving now would "
                    "pre-empt it and the second product would never be created."
                    if split_on_both_genders else
                    " The master category decides it."
                )
            ),
            detail={
                "gender": p.gender,
                "repairable": not split_on_both_genders,
                "owner": (
                    "split-gender-worker" if split_on_both_genders
                    else "approval-gate"
                ),
                "derived": resolve_gender(p.master_category),
            },
        ))

    # ---- SIZE.011 / SIZE.012: is this the RIGHT chart? ----------------------
    #
    # SIZE.010 asks whether a chart covers the product at all. These two ask
    # whether it is the correct one, which SIZE.002 cannot see: that rule
    # validates the euSize AGAINST whatever chart is attached, so a product on
    # the wrong chart with a matching euSize passes it cleanly while carrying a
    # size from the wrong table.
    current = p.sizing_guide
    if current and p.catalog and (p.catalog.sizing_guides or {}):
        want = product_gender(p)
        have = guide_gender(current)

        # SIZE.014 — the guide measures the WRONG HALF OF THE BODY.
        #
        # First, because it is the mismatch the other two cannot see. A Women >
        # T-Shirts & Polos product on "Women Bottoms" agrees on gender, so
        # SIZE.011 stays quiet; and when a tenant's bottoms guide lists letter
        # sizes rather than waist numbers the ladder holds "S" too, so SIZE.013
        # stays quiet as well. Only the category answers it.
        want_side = category_side(p, pol)
        have_side = guide_side(current, pol)
        holds_size = guide_contains_size(p)

        if want_side and have_side and want_side != have_side:
            fits = candidate_guides(p, pol=pol)
            out.append(Finding(
                rule_id="SIZE.014", severity=Severity.HIGH,
                fields=["sizing_guide"],
                message=(
                    f"Sizing guide '{current}' is a {have_side}-body chart but "
                    f"this is a {want_side}-body product "
                    f"({p.master_category} > {p.category})."
                    + (f" '{fits[0]}' is the match." if len(fits) == 1 else "")
                ),
                detail={
                    "sizing_guide": current,
                    "guide_side": have_side,
                    "product_side": want_side,
                    "suggested": fits,
                },
            ))
        elif holds_size is False:
            fits = candidate_guides(p)
            out.append(Finding(
                rule_id="SIZE.013", severity=Severity.HIGH,
                fields=["sizing_guide"],
                message=(
                    f"Sizing guide '{current}' does not list this product's size "
                    f"({', '.join(_size_candidates(p)[:2]) or 'unknown'}), so it "
                    f"cannot size it."
                    + (f" '{fits[0]}' does." if len(fits) == 1 else "")
                ),
                detail={
                    "sizing_guide": current,
                    "product_size": _size_candidates(p)[:3],
                    "ladder": (
                        (p.catalog.sizing_guides or {}).get(current, {})
                    ).get("sizes", [])[:8],
                    "suggested": fits,
                },
            ))
        elif want and have and want != have:
            # The chart contradicts the garment. This is the case that produces
            # wrong sizes: the men's letter table maps S to EU 46, the women's to
            # 36, so a men's product on a women's chart is off by ten EU sizes.
            out.append(Finding(
                rule_id="SIZE.011", severity=Severity.HIGH,
                fields=["sizing_guide"],
                message=(
                    f"Sizing guide '{current}' is a {have}'s chart but this is a "
                    f"{want}'s product."
                ),
                detail={"sizing_guide": current, "guide_gender": have,
                        "product_gender": want,
                        "suggested": candidate_guides(p)},
            ))
        elif is_generic_guide(current) and want:
            # SIZE.012 — on a generic chart while specific ones fit.
            #
            # MEDIUM, so it does NOT block approval. Sitting on "Defaults" is a
            # legitimate configuration and the tenant may intend it; it is worth
            # correcting, not worth halting a catalog for. The hazard is real but
            # latent — DEFAULTS_TABLE in backfill-eu-sizes.ts is a module
            # constant, so a generic chart converts one gender correctly and the
            # other silently wrong.
            #
            # `suggested` is a LIST, matching SIZE.011/013/014. It used to be a
            # single name from better_guide(), which returns None unless EXACTLY
            # one chart fits — and that conflated two different facts:
            #
            #   nothing more specific exists   -> Defaults is right, stay silent
            #   several more specific exist    -> still wrong, a human picks
            #
            # This tenant has both "Men Uppers" and "Men DressShirts" listing
            # "S", so every men's top left on Defaults matched two charts, failed
            # the len==1 test, and was reported as clean. A real men's hoodie
            # (1939df54) is what surfaced it.
            #
            # The planner already reads the list correctly: one candidate becomes
            # a set_column, several become an escalate naming them.
            fits = candidate_guides(p, pol=pol)
            if fits:
                quoted = ", ".join("'" + n + "'" for n in fits)
                named = (
                    f"'{fits[0]}' matches" if len(fits) == 1
                    else f"{len(fits)} charts match ({quoted})"
                )
                out.append(Finding(
                    rule_id="SIZE.012", severity=Severity.MEDIUM,
                    fields=["sizing_guide"],
                    message=(
                        f"'{current}' is a generic chart; {named} this product's "
                        f"gender and size."
                        + ("" if len(fits) == 1 else " Pick one.")
                    ),
                    detail={"sizing_guide": current, "suggested": fits,
                            "product_gender": want},
                ))

    # ---- IMG.030: a care label exists --------------------------------------
    #
    # The MEDIA ROWS decide, not `careLabelCount`.
    #
    # `careLabelCount` is derived from `Product.careLabelImages`, one of the
    # legacy `String[]` columns the ProductMedia refactor replaced —
    # docs/product-media-changes.md lists it under DROPPED, with live `LABEL`
    # rows as its replacement, and docs/product-media-model.md notes it is now
    # DERIVED from `view === LABEL` on the way out. Trusting it reported "no care
    # label" on products whose labels were plainly present in the gallery,
    # because array membership is no longer the pipeline's state.
    #
    # Same reasoning IMG.001 already applies to renders: `generationStatus` lies,
    # so the media rows are read instead.
    label_rows = [
        m for m in p.media
        if (m.view or "").upper() == "LABEL" and m.is_current and not m.deleted_at
    ]
    if p.media:
        has_label = bool(label_rows)
    elif p.care_label_count is not None:
        # No typed rows supplied (a raw webhook payload, a fixture) — fall back to
        # the count, which is all such a caller has.
        has_label = p.care_label_count > 0
    else:
        # Nothing said either way. Silent, rather than reporting every payload
        # that does not mention labels as unlabelled.
        has_label = True

    if not has_label:
        out.append(Finding(
            rule_id="IMG.030", severity=Severity.HIGH, fields=["care_label_images"],
            message="No care label image on file.",
            detail={"label_media_rows": len(label_rows),
                    "care_label_count": p.care_label_count,
                    "basis": "media_rows" if p.media else "care_label_count"},
        ))

    # ---- SIZE.010: a chart covers this product -----------------------------
    #
    # Skipped entirely when there is no catalog: absence of evidence about the
    # tenant's charts is not evidence that the tenant has none, and a raw webhook
    # payload carries no catalog at all.
    if p.catalog is not None and resolve_guide(p, pol) is None:
        ambiguous = candidate_guides(p, pol=pol)
        # A chart is only invented when NOTHING the tenant has covers the size.
        # When several do, the tenant is configured fine and the product just
        # needs the right one chosen — creating another would make it worse.
        name = None if ambiguous else expected_guide_name(p)
        seed = seed_chart(p, pol) if name else None

        if ambiguous:
            message = (
                f"This product's size matches {len(ambiguous)} of the tenant's "
                f"charts ({', '.join(ambiguous)}), so the right one cannot be "
                f"chosen automatically."
            )
        elif name:
            message = f"No sizing chart covers this product; '{name}' is missing."
        else:
            message = (
                "No sizing chart covers this product and its taxonomy is too "
                "incomplete to name one."
            )

        out.append(Finding(
            rule_id="SIZE.010", severity=Severity.HIGH, fields=["sizing_guide"],
            message=message,
            detail={
                "expected_guide": name,
                "on_product": p.sizing_guide,
                "candidates": ambiguous,
                "available": sorted((p.catalog.sizing_guides or {}).keys()),
                # None here is what makes the repair step escalate rather than
                # create a chart it cannot fill in.
                "seed": seed,
            },
        ))

    out.extend(check_column_drift(p))
    return out


# --------------------------------------------------------------------------- #
# DRIFT.001 — the column and the `properties` copy hold different values
# --------------------------------------------------------------------------- #

# (snapshot field, column name, `properties` key) for every value VNYX genuinely
# stores TWICE.
#
# This is not a hypothetical. `Product.internationalSize` is a real column and
# `properties.international_size` is a real key, both written by different paths:
# the analyze worker writes the property, the edit screen writes the column, and
# no code copies one to the other. `PROPERTY_ALIASES` resolves a precedence
# between them for READING, which is what makes the disagreement invisible —
# every resolved view of the product shows the winner and nothing shows that
# there was a contest.
#
# It matters because different consumers read different copies. The product
# export reads the COLUMN. So a product whose column says "Unknown" and whose
# property says "S" passes every size rule — the resolved size is "S" — and then
# publishes with no size on it.
#
# Deliberately narrow. Only pairs where both sides are a plain scalar naming the
# same thing; nothing derived, nothing where one side is a relation id.
COLUMN_PROPERTY_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("international_size", "internationalSize", "international_size"),
    ("master_category", "masterCategory", "mastercategory"),
    ("subcategory", "subCategory", "sub_category"),
)

# Which side wins when they disagree, and why.
#
# The rule proposes the NON-placeholder side. That is the only direction that is
# safe without knowing which write came last: "Unknown" is never the answer
# somebody meant, so copying the real value over it cannot destroy information.
# When BOTH sides hold a real value they simply differ, and no arithmetic settles
# which is right — that escalates.
_UNKNOWN = {"", "unknown", "n/a", "na", "none", "null", "-", "tbd"}


def _meaningless(value: Any) -> bool:
    return value is None or str(value).strip().lower() in _UNKNOWN


def _same(a: Any, b: Any) -> bool:
    """Compare on letters and digits only.

    "Leather Jackets" and "leather  jackets" are the same value written by two
    paths; reporting the difference in spacing as data drift is noise.
    """
    def flat(v: Any) -> str:
        return "".join(ch for ch in str(v or "").lower() if ch.isalnum())
    return flat(a) == flat(b)


# The drift pairs whose value has to exist in the tenant's category tree before
# it may be proposed as a repair. `international_size` is deliberately absent:
# sizes are not taxonomy and have no tree to check against.
_TREE_CHECKED: dict[str, str] = {
    "master_category": "root",
    "subcategory": "subcategory",
}


def _tree_holds(p: ProductSnapshot, field: str, value: Any) -> bool:
    """Is `value` a name this tenant's own category tree actually uses?

    Returns True for anything not under _TREE_CHECKED, and True when the tenant
    supplied no tree — with nothing to check against, refusing every taxonomy
    repair would be worse than the drift it is meant to fix, and the same
    reasoning the catalog rules already use for an absent option list.

    Compared on letters and digits only, so "T-Shirts & Tops" and "tshirts tops"
    are the same name and a difference in punctuation is not reported as a value
    the tenant does not have.
    """
    level = _TREE_CHECKED.get(field)
    if level is None:
        return True
    tree = (p.catalog.categories if p.catalog else None) or {}
    if not tree:
        return True

    def flat(v: Any) -> str:
        return "".join(ch for ch in str(v or "").lower() if ch.isalnum())

    want = flat(value)
    if not want:
        return True
    if level == "root":
        return want in {flat(root) for root in tree}
    return want in {
        flat(sub)
        for branches in tree.values()
        for subs in branches.values()
        for sub in subs
    }


def check_column_drift(p: ProductSnapshot) -> list[Finding]:
    """DRIFT.001 — a column and its `properties` twin hold different values.

    Silent unless the caller supplied `column_values`; see the note on that field
    in models.py. HIGH when one side is empty and the other is not, because the
    consumer reading the empty side ships a product with the field missing.
    MEDIUM when both hold real but different values — still wrong, but a human
    has to pick.
    """
    if not p.column_values:
        return []

    out: list[Finding] = []
    for field, column, prop_key in COLUMN_PROPERTY_PAIRS:
        if column not in p.column_values:
            continue
        col_value = p.column_values[column]
        prop_value = (p.properties_raw or {}).get(prop_key)

        if _same(col_value, prop_value):
            continue
        col_empty, prop_empty = _meaningless(col_value), _meaningless(prop_value)
        if col_empty and prop_empty:
            continue

        if col_empty or prop_empty:
            winner = prop_value if col_empty else col_value
            loser_side = "column" if col_empty else "properties"

            # A TAXONOMY VALUE THE TENANT'S TREE DOES NOT HOLD IS NOT A REPAIR.
            #
            # This rule's whole argument for proposing a value automatically is
            # that one side is empty, so the other must be right. That holds for
            # a size. It does NOT hold for `masterCategory` or `subCategory`,
            # where the `properties` copy is written by the analyze worker from
            # the model's own words and can say anything at all.
            #
            # On 18 Sep 2026 it said "Men's/Unisex" and "hoodie". Both were
            # proposed here, both were written, and both were rejected by
            # TAX.001/TAX.002 in the same run — after which `copy` regenerated
            # the title from them and an Adidas t-shirt was published as a
            # "Deep Burgundy Hoodie". The value was never plausible; nothing
            # asked the tree before planning the write.
            #
            # So: still a finding, because the two copies genuinely disagree and
            # a person should look. But `repair_to: None`, which is this rule's
            # existing way of saying "both are real, the choice is a judgement"
            # — and the executor never auto-writes those.
            if not _tree_holds(p, field, winner):
                out.append(Finding(
                    rule_id="DRIFT.001", severity=Severity.MEDIUM, fields=[field],
                    message=(
                        f"'{column}' and 'properties.{prop_key}' disagree, and the "
                        f"only value on offer ({winner!r}) is not in this tenant's "
                        f"category tree — so it cannot be written. A person has to "
                        f"say what this product is."
                    ),
                    detail={
                        "column": column, "column_value": col_value,
                        "property": prop_key, "property_value": prop_value,
                        "repair_to": None, "rejected_value": winner,
                        "why": "not a value in the tenant's category tree",
                    },
                    needs_evidence=True,
                ))
                continue

            out.append(Finding(
                rule_id="DRIFT.001", severity=Severity.HIGH, fields=[field],
                message=(
                    f"'{column}' and 'properties.{prop_key}' disagree: the "
                    f"{loser_side} holds {col_value if col_empty else prop_value!r} "
                    f"and the other holds {winner!r}. Whichever consumer reads the "
                    f"empty side publishes this product without a {field.replace('_', ' ')}."
                ),
                detail={
                    "column": column, "column_value": col_value,
                    "property": prop_key, "property_value": prop_value,
                    "repair_to": winner, "repair_side": loser_side,
                },
            ))
        else:
            out.append(Finding(
                rule_id="DRIFT.001", severity=Severity.MEDIUM, fields=[field],
                message=(
                    f"'{column}' holds {col_value!r} but "
                    f"'properties.{prop_key}' holds {prop_value!r}. Both are real "
                    f"values, so which is correct is a judgement."
                ),
                detail={
                    "column": column, "column_value": col_value,
                    "property": prop_key, "property_value": prop_value,
                    "repair_to": None,
                },
            ))
    return out
