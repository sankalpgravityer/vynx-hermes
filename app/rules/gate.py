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
    """The size spellings to look for in a chart. Stored data uses several."""
    waist = str(p.waist).strip() if p.waist else None
    return [
        str(v).strip()
        for v in (p.size, p.international_size, waist,
                  f"W{waist}" if waist else None)
        if v and str(v).strip()
    ]


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
    ladder = {str(s).strip().lower() for s in (pair.get("sizes") or [])}
    if not ladder:
        return None
    wanted = {s.lower() for s in _size_candidates(p)}
    if not wanted:
        return None
    return bool(wanted & ladder)


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
        else:
            better = better_guide(p, pol)
            if better:
                # MEDIUM, so it does NOT block approval. Sitting on "Defaults" is
                # a legitimate configuration and the tenant may intend it; it is
                # worth correcting, not worth halting a catalog for. The hazard is
                # real but latent — DEFAULTS_TABLE in backfill-eu-sizes.ts is a
                # module constant, so a generic chart converts one gender
                # correctly and the other silently wrong.
                out.append(Finding(
                    rule_id="SIZE.012", severity=Severity.MEDIUM,
                    fields=["sizing_guide"],
                    message=(
                        f"'{current}' is a generic chart; '{better}' matches this "
                        f"product's gender and size."
                    ),
                    detail={"sizing_guide": current, "suggested": better,
                            "product_gender": product_gender(p)},
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

    return out
