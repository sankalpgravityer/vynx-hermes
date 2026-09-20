"""Resolver — turns findings + evidence into concrete patches.

The arbitration principle: when two fields disagree, trust the one with better
provenance. A human-entered value always wins. A search-grounded value beats a
value that was merely derived from a multiplier. Nothing beats a human.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import (
    Action, Evidence, Finding, Patch, PROVENANCE_RANK, PriceVerdict, Provenance,
    ProductSnapshot,
)
from app.rules.pricing import assess, band_for, clamp, grade_of, round_price

log = logging.getLogger("hermes.resolver")


def _delta_pct(old: float | None, new: float) -> float:
    if not old:
        return 100.0
    return abs(new - old) / old * 100.0


def _gate(patch: Patch, pol: dict[str, Any], snapshot: ProductSnapshot) -> Patch:
    """Apply the universal safety gates to any candidate patch."""
    if snapshot.is_locked(patch.field):
        patch.action = Action.ESCALATE
        patch.reason += " (field is human-locked; Hermes will not overwrite it)"
        return patch

    if patch.field in pol["guardrails"]["escalate_only_fields"]:
        patch.action = Action.ESCALATE
        return patch

    if isinstance(patch.old_value, (int, float)) and isinstance(patch.new_value, (int, float)):
        if _delta_pct(patch.old_value, patch.new_value) > pol["pricing"]["auto_apply_max_delta_pct"]:
            patch.action = Action.PROPOSE
            patch.reason += " (change too large to apply silently)"

    if patch.confidence < pol["confidence"]["llm_apply_threshold"] and patch.action is Action.APPLY:
        patch.action = Action.PROPOSE
    return patch


# --------------------------------------------------------------------------- #
# Pricing repair
# --------------------------------------------------------------------------- #

def resolve_pricing(p: ProductSnapshot, findings: list[Finding],
                    ev: Evidence, pol: dict[str, Any]) -> list[Patch]:
    """Repair the price/retail relationship.

    Order of operations matters:
      1. If we have grounded RRP evidence and the existing retail anchor is weak,
         replace the anchor first. Fixing the anchor often fixes the ratio for free.
      2. Then decide which side of the ratio to move, based on provenance.
      3. Snap to the grade's target multiplier and a charm ending.
    """
    ids = {f.rule_id for f in findings}
    # PRICE.003 (too LOW) belongs here as much as PRICE.002 (too high).
    #
    # It was missing, and the early return below then treated a too-low price as
    # "no bad ratio to repair" — its own comment asserts the price "is inside its
    # window", which is exactly what PRICE.003 says it is not. So an underpriced
    # product was reported forever and never corrected, while an overpriced one
    # was fixed on the first pass.
    #
    # The machinery downstream already handles the low side: `clamp` raises a
    # price below `band["low"]`, and `direction = +1` rounds it inward rather
    # than back out of the window. Only this set needed the entry.
    RATIO_RULES = {"PRICE.001", "PRICE.002", "PRICE.003", "PRICE.020", "PRICE.010"}

    if not ids & RATIO_RULES:
        # No bad ratio to repair — but the cents may still need normalising, and
        # that case is keyed on the ASSESSMENT rather than a finding, because it
        # deliberately raises none (see the note in rules/pricing.check).
        # Everything below this point is machinery for a bad price/retail ratio,
        # which this price does not have: it is inside its window, exactly the
        # condition the in-band early return further down uses to do nothing.
        a = assess(p, pol)
        if a.verdict is not PriceVerdict.ROUND_REQUIRED or a.corrected_price is None:
            return []
        return [_gate(Patch(
            field="price", old_value=p.price, new_value=a.corrected_price,
            action=Action.APPLY, rule_id="PRICE.004", reason=a.explanation,
            confidence=0.99, provenance=Provenance.DERIVED,
        ), pol, p)]

    patches: list[Patch] = []
    pr = pol["pricing"]
    currency = p.currency or pr["default_currency"]
    price, retail = p.price, p.retail_price

    # --- 1. Strengthen the retail anchor ---------------------------------- #
    retail_trust = p.trust("retail_price")
    if ev.rrp.found and ev.rrp.rrp and ev.rrp.confidence >= 0.6:
        grounded_beats_current = PROVENANCE_RANK[Provenance.GROUNDED] > retail_trust
        if (retail is None or retail <= 0 or grounded_beats_current):
            new_retail = round(float(ev.rrp.rrp), 2)
            patches.append(_gate(Patch(
                field="retail_price", old_value=retail, new_value=new_retail,
                action=Action.APPLY, rule_id="PRICE.020",
                reason=f"Replaced a {p.prov('retail_price').value}-provenance retail "
                       f"anchor with a search-grounded RRP. {ev.rrp.reasoning}",
                confidence=ev.rrp.confidence, provenance=Provenance.GROUNDED,
                sources=ev.rrp.sources,
            ), pol, p))
            retail = new_retail
            retail_trust = PROVENANCE_RANK[Provenance.GROUNDED]

    if not retail or retail <= 0:
        # No anchor and no evidence — refuse to invent one.
        patches.append(Patch(
            field="retail_price", old_value=p.retail_price, new_value=None,
            action=Action.ESCALATE, rule_id="PRICE.020",
            reason="No retail anchor available and RRP lookup was inconclusive. "
                   "A human must supply the original retail price.",
            confidence=0.0,
        ))
        return patches

    if not price or price <= 0:
        band = band_for(p, pol)
        # round_price, not charm: this must respect `round_mode`, and charm() is
        # only reachable through it.
        target = round_price(retail * band["target"], pol)
        patches.append(_gate(Patch(
            field="price", old_value=price, new_value=target,
            action=Action.APPLY, rule_id="PRICE.010",
            reason=f"Price was missing. Set to Grade {grade_of(p, pol)} target "
                   f"({band['target']:.0%}) of retail {retail:.2f} {currency}.",
            confidence=0.9, provenance=Provenance.DERIVED,
        ), pol, p))
        return patches

    ratio = price / retail
    band = band_for(p, pol)
    grade = grade_of(p, pol)

    # "IN BAND" MUST MEAN WHAT THE RULE MEANS BY IT, and it did not.
    #
    # `band_for` returns the RAW multipliers — a grade-A factor of 0.5 with a
    # +/-20% tolerance gives 0.40-0.60. `rules/pricing.assess` rounds those
    # bounds to the tenant's charm ending BEFORE judging, so on a 49.99 retail
    # its window is 20.99-30.99, not 19.996-29.994.
    #
    # The two therefore disagreed about a thin sliver just inside each raw
    # bound, and the disagreement was unrecoverable: `assess` returned TOO_LOW,
    # rules/pricing raised PRICE.003 at HIGH — above the blocking floor — and
    # this early return then decided there was nothing to repair, so no patch
    # was ever produced. A product in that sliver blocked approval on every
    # pass, forever, and no amount of re-running could fix it.
    #
    # Seen on BOA-006169: price 20.00, retail 49.99, ratio 0.400080. The rule's
    # floor was 20.99, this comparison's was 0.40. Blocked permanently over 99
    # cents, with `corrected_price: 20.99` sitting in the assessment unused.
    #
    # So gate on `assess`, which is the function the RULE blocks on. Repairing
    # against a second, differently-rounded opinion of the same question is
    # exactly how the two drift apart. Only OK returns: ROUND_REQUIRED means the
    # cents still need normalising, and the clamp-and-round machinery below
    # produces the same shelf price for it that `assess` already computed.
    assessment = assess(p, pol)
    if assessment.verdict is PriceVerdict.OK and ratio < pr["hard_max_ratio"]:
        return patches  # anchor fix alone resolved it

    price_trust = p.trust("price")

    # --- 2. Decide which side of the disagreement moves -------------------- #
    #
    # `pricing.anchor` in policy.yaml selects the strategy. It was documented
    # there from the start but never read — this function always did provenance
    # arbitration, so `anchor: retail` (the shipped default) had no effect and the
    # selling price was never the side that moved.
    #
    #   retail      -> retail_price is the anchor; the SELLING PRICE is corrected.
    #                  The right default while `price` provenance is not yet
    #                  trustworthy: it defaults to MARKET for every product
    #                  regardless of how the figure was actually obtained.
    #   provenance  -> whichever field has weaker provenance moves.
    #
    # A HUMAN-locked field is excluded either way — _gate escalates it below, so
    # anchor: retail cannot overwrite a price an operator typed.
    anchor = pr.get("anchor", "retail")
    move_price = True if anchor == "retail" else retail_trust >= price_trust

    if move_price:
        # Retail is the reliable anchor → recompute the selling price.
        # For a hard violation, snap to target. For band drift, clamp to the
        # NEAREST edge — the smallest change that makes the record valid.
        if ratio >= pr["hard_max_ratio"]:
            raw = retail * band["target"]
            direction = 0
        else:
            raw = clamp(price, retail * band["low"], retail * band["high"])
            # Round INWARD at whichever bound we landed on, so the rounding step
            # cannot push the price back out of the window it was just clamped
            # into. Straight `charm()` here ignored round_mode entirely and turned
            # an exact 48.00 upper bound into 47.99.
            direction = -1 if ratio > band["high"] else +1
        new_price = round_price(raw, pol, direction=direction)
        # Re-clamp after rounding, then enforce the hard invariant.
        new_price = min(new_price, round(retail * pr["hard_max_ratio"] - 0.01, 2))
        new_price = max(new_price, pr["min_price"])

        # State the REASON the selling price is the side that moved. Under
        # anchor: retail that is policy, not a trust comparison — the previous
        # wording claimed "retail is more trustworthy than price" unconditionally,
        # which is plainly false in the common case (price defaults to MARKET,
        # retail to DERIVED, so retail is the LESS trusted of the two).
        why_this_side = (
            "`pricing.anchor` is `retail`, so the retail price is the anchor and "
            "the selling price is the side that moves"
            if anchor == "retail"
            else f"the retail anchor ({p.prov('retail_price').value}) has stronger "
                 f"provenance than the price ({p.prov('price').value})"
        )
        # Report the rule that ACTUALLY fired. The old two-way ternary labelled
        # every non-PRICE.001 correction "PRICE.002" (too high), so a too-low
        # repair was filed against the wrong rule — which matters downstream,
        # where callers decide what a write resolved by rule id.
        fired = next(
            (r for r in ("PRICE.001", "PRICE.003", "PRICE.002") if r in ids),
            "PRICE.002",
        )
        patches.append(_gate(Patch(
            field="price", old_value=price, new_value=new_price,
            action=Action.APPLY, rule_id=fired,
            reason=(
                f"Ratio was {ratio:.2f}; Grade {grade} allows "
                f"{band['low']:.2f}–{band['high']:.2f} ({band['rule']}). "
                f"Recomputed the selling price to {new_price:.2f} {currency} "
                f"({new_price / retail:.0%} of retail) because {why_this_side}."
            ),
            confidence=0.92, provenance=Provenance.DERIVED,
        ), pol, p))
    else:
        # Price came from real market comparables → back out the implied retail.
        new_retail = round_price(price / band["target"], pol)
        floor = round(price / pr["hard_max_ratio"] + 0.01, 2)
        new_retail = max(new_retail, floor)

        patches.append(_gate(Patch(
            field="retail_price", old_value=retail, new_value=new_retail,
            action=Action.PROPOSE, rule_id="PRICE.001" if "PRICE.001" in ids else "PRICE.002",
            reason=(
                f"Ratio was {ratio:.2f}. The selling price has stronger provenance "
                f"({p.prov('price').value}) than the retail anchor "
                f"({p.prov('retail_price').value}), so the anchor was back-solved "
                f"from the Grade {grade} target multiplier."
            ),
            confidence=0.75, provenance=Provenance.DERIVED,
        ), pol, p))

    return patches


# --------------------------------------------------------------------------- #
# Attribute repair
# --------------------------------------------------------------------------- #

_DERIVABLE = {
    "TAX.005": "mannequin",
    "GRADE.001": "grade",
    "SIZE.002": "eu_size",
}


def resolve_attributes(p: ProductSnapshot, findings: list[Finding],
                       ev: Evidence, pol: dict[str, Any]) -> list[Patch]:
    """Repair attribute-level findings, deterministically where possible."""
    patches: list[Patch] = []
    handled: set[str] = set()

    for f in findings:
        # -- deterministic repairs (no model needed) ----------------------- #
        if f.rule_id == "TAX.005" and f.detail.get("allowed"):
            patches.append(_gate(Patch(
                field="mannequin", old_value=p.mannequin,
                new_value=f.detail["allowed"][0], action=Action.APPLY,
                rule_id=f.rule_id,
                reason=f"Mannequin rig corrected to match "
                       f"{p.master_category} > {p.category}.",
                confidence=0.99, provenance=Provenance.DERIVED,
            ), pol, p))
            handled.add("mannequin")

        elif f.rule_id == "GRADE.001":
            # WHICH SIDE MOVES depends on which branch fired, and getting it
            # backwards produced a repair that could never land.
            #
            # `basis: grade_label` — the tenant's own Grade row carries the label
            # ("As New") and `condition` disagrees with it. `expected` is that
            # LABEL, so the fix is to write CONDITION. Writing it into `grade`
            # put a condition label where a grade CODE belongs: services/
            # products.ts rejected it ("code not in the tenant's grade scale"),
            # nothing changed, and the finding re-fired on every run — a repair
            # that was planned forever and never converged.
            #
            # `basis: condition_to_grade` — the generic table maps a condition to
            # a grade letter, so `expected` IS a code and `grade` is the side
            # that moves.
            #
            # The rule's own message says which is authoritative: "These are kept
            # in sync on every grade write" — the grade drives the condition.
            if f.detail.get("basis") == "grade_label":
                patches.append(_gate(Patch(
                    field="condition", old_value=p.condition,
                    new_value=f.detail["expected"],
                    action=Action.APPLY, rule_id=f.rule_id,
                    reason=f"Condition realigned to the label for Grade "
                           f"{p.grade} ('{f.detail['expected']}').",
                    confidence=0.97, provenance=Provenance.DERIVED,
                ), pol, p))
                handled.add("condition")
            else:
                patches.append(_gate(Patch(
                    field="grade", old_value=p.grade,
                    new_value=f.detail["expected"],
                    action=Action.APPLY, rule_id=f.rule_id,
                    reason=f"Grade realigned to the condition '{p.condition}' per "
                           "the grading policy.",
                    confidence=0.97, provenance=Provenance.DERIVED,
                ), pol, p))
                handled.add("grade")

        elif f.rule_id == "SIZE.002":
            # `detail` differs by WHICH branch of check_sizing fired, and the two
            # do not carry the same keys. The tenant-chart branch has no `waist`
            # at all, so reading it unconditionally raised KeyError and took the
            # whole resolver down — on the PRIMARY path, the one that uses the
            # tenant's own chart. Nothing caught it until a caller ran the
            # resolver over real feed data: /v1/review-queue never calls it, and
            # /v1/reconcile is unwired.
            basis = f.detail.get("basis")
            tenant_chart = basis == "tenant_chart"
            if tenant_chart:
                source = f"the tenant's '{f.detail.get('sizing_guide')}' chart"
            else:
                waist = f.detail.get("waist")
                source = (
                    f"the generic W{waist} conversion table" if waist
                    else "the generic conversion table"
                )
            patches.append(_gate(Patch(
                field="eu_size", old_value=p.eu_size,
                new_value=str(f.detail["expected_eu"]), action=Action.APPLY,
                rule_id=f.rule_id,
                reason=f"EU size derived from {source}.",
                # The tenant's chart is authoritative; the policy table is a
                # documented guess AT that chart, and the two disagree outright —
                # it maps W28 to EU 40 where a real tenant's chart says 36. So a
                # policy-derived value sits below `llm_apply_threshold` and _gate
                # downgrades it to PROPOSE, which surfaces it for a human instead
                # of writing a size that is probably wrong.
                confidence=0.95 if tenant_chart else 0.80,
                provenance=Provenance.DERIVED,
            ), pol, p))
            handled.add("eu_size")

        elif f.rule_id == "GRADE.003" and ev.vision.visible_defects:
            patches.append(_gate(Patch(
                field="defects", old_value=p.defects,
                new_value=ev.vision.visible_defects, action=Action.PROPOSE,
                rule_id=f.rule_id,
                reason="Defects observed in the product photos but absent from the "
                       "record.",
                confidence=0.7, provenance=Provenance.AI,
            ), pol, p))
            handled.add("defects")

    # -- where the PICTURE says the garment belongs ------------------------- #
    patches.extend(resolve_taxonomy(p, ev, pol, handled))

    # -- evidence-backed repairs from the vision + copy audits -------------- #
    blocked = set(pol["guardrails"]["llm_forbidden_fields"])
    for v in list(ev.vision.verdicts) + list(ev.text_verdicts):
        if v.verdict != "contradict" or v.field in blocked or v.field in handled:
            continue
        if not v.observed_value:
            continue

        observed = str(v.observed_value).strip()

        # A PLACEHOLDER is the model declining to answer, not an answer.
        #
        # "Unknown" got written straight onto a product as its brand — the field
        # went from a real (if wrong) value to the literal word Unknown, which is
        # strictly worse and reads as a successful repair. `placeholders` is
        # already the list check_completeness treats as absent, so trusting it
        # here keeps one definition of "no value".
        #
        # vnyx-api's backfill-product-data.ts takes the same line from the other
        # direction: it treats "the literal 'Unknown' the AI writes in place of
        # an answer" as a hole to fill, never as content.
        if observed.lower() in pol["confidence"]["placeholders"]:
            continue

        # A value the tenant's own dropdown cannot offer is not selectable.
        #
        # The vision layer reads brands off garment prints — a tee printed with a
        # film title came back as brand "Boyz N The Hood" — and writing that
        # produces the blank-dropdown defect fix-subcategory-naming.ts describes:
        # "the value is stored and is not wrong in spirit, it is simply not one
        # of the options, so the control cannot select it and an operator reads
        # the field as empty".
        #
        # PROPOSE rather than skip: the model may well have read the label
        # correctly and the tenant may be missing a brand. That is worth a
        # reviewer's attention, not silent discard.
        catalog_fields = {"brand": "brands", "color": "colors",
                          "material": "materials"}
        off_catalog = False
        if v.field in catalog_fields and p.catalog:
            allowed = getattr(p.catalog, catalog_fields[v.field], None) or []
            if allowed and observed.lower() not in {
                str(a).strip().lower() for a in allowed
            }:
                off_catalog = True

        current = getattr(p, v.field, None)
        if str(current).strip().lower() == observed.lower():
            continue
        confident = v.confidence >= pol["confidence"]["llm_apply_threshold"]
        patches.append(_gate(Patch(
            field=v.field, old_value=current, new_value=v.observed_value,
            action=(
                Action.APPLY if confident and not off_catalog
                else Action.PROPOSE
            ),
            rule_id="LLM.001",
            reason=(
                f"{v.evidence} (NOT in this tenant's {v.field} list — add it "
                f"there first, or correct the reading)"
                if off_catalog else v.evidence
            ),
            confidence=v.confidence, provenance=Provenance.AI,
        ), pol, p))
        handled.add(v.field)

    return patches


# --------------------------------------------------------------------------- #
# The taxonomy the picture supports
# --------------------------------------------------------------------------- #

def _tenant_branch(p: ProductSnapshot) -> dict[str, list[str]]:
    """The `{category: [subcategory, ...]}` under this product's master, or {}."""
    from app.readiness import flat

    if not (p.catalog and p.catalog.categories and p.master_category):
        return {}
    want = flat(p.master_category)
    for root, branch in p.catalog.categories.items():
        if flat(root) == want:
            return {str(c): [str(s) for s in (subs or [])]
                    for c, subs in (branch or {}).items()}
    return {}


def _match(name: Any, options: list[str]) -> str | None:
    """The tenant's own spelling of `name`, or None.

    Case, punctuation and a trailing plural are ignored on both sides, which is
    the same comparison `_plan_category` makes — 'T Shirts' and 'T-Shirts' are
    one category and a model asked to copy a string exactly will still
    occasionally drop a hyphen.
    """
    from app.readiness import _singular

    if not name:
        return None
    want = _singular(name)
    return next((o for o in options if _singular(o) == want), None)


def resolve_taxonomy(p: ProductSnapshot, ev: Evidence, pol: dict[str, Any],
                     handled: set[str]) -> list[Patch]:
    """Category and subcategory from the garment photographs, chosen from the
    tenant's own tree.

    THE GAP THIS CLOSES. Every other taxonomy repair reads COLUMNS: TAX.002 asks
    whether the category sits under the master, TAX.003 whether the subcategory
    sits under the category. Both pass on a path that is structurally perfect and
    describes the wrong garment — MID-000615 is a tank top filed under
    `Women > Dresses > Casual Dress`, and the tree says that path exists, so
    nothing fired. The quality gate DID notice (CATEGORY_IMAGE_MISMATCH) but
    returns `review` by design: "a human decides whether the category or the
    photograph is wrong". Nothing proposed a category, so the product blocked on
    every run with no way out.

    The picture is the only evidence that can settle it, and the model was
    already looking at the pictures and already naming the garment — `tank top`
    at 0.90 confidence, read once to phrase a gender sentence and then dropped.

    FOUR THINGS MAKE THIS SAFE TO WRITE.

      1. The tenant's own list was sent WITH the request, so the answer is a
         tenant value, not a plausible invention. A reply that is not in the
         tree after all is dropped here, not written and not proposed: this is
         the one field where an off-catalog value has already caused damage
         (18 Sep 2026 — `category: 'hoodie'` on an Adidas t-shirt, published as
         a "Deep Burgundy Hoodie" by the copy step).

      2. The two move TOGETHER or not at all. A category written without a
         subcategory that sits under it is how `Women > Dresses > Men's Shirts`
         happens: two halves each defensible alone, describing nothing together.

      3. The master category is never touched. It is the anchor the branch was
         narrowed by, and `readiness.master.photo_check` is explicit that a
         render may not rewrite the record it is judged by. A photograph may
         move a product within its root; it may not move it between roots.

      4. Its own floor, ABOVE the generic one. `llm_apply_threshold` is 0.85 for
         a single field; this writes two at once and the cost of getting it
         wrong is a re-render as well as a mis-filed product, because
         `classify_garment_type` picks the render prompt's framing off the
         category. `min_confidence` defaults to 0.9 and is a separate knob so it
         can be raised without moving every other AI write with it.
    """
    cfg = pol.get("taxonomy_from_picture") or {}
    if not cfg.get("enabled", True):
        return []

    sug = getattr(ev.vision, "taxonomy", None)
    if sug is None or not sug.category:
        return []

    branch = _tenant_branch(p)
    if not branch:
        return []

    category = _match(sug.category, list(branch))
    if category is None:
        log.info(
            "taxonomy from picture: %s is not a category under %r — dropped "
            "(saw %r)", sug.category, p.master_category, sug.garment,
        )
        return []

    subs = branch.get(category) or []
    subcategory = _match(sug.subcategory, subs) if sug.subcategory else None
    if subcategory is None and subs:
        # The category resolved and the subcategory did not. The current one is
        # only usable if it already sits under the NEW category; otherwise this
        # is half a path and a person has to choose the leaf.
        subcategory = _match(p.subcategory, subs)
        if subcategory is None:
            return [Patch(
                field="category", old_value=p.category, new_value=None,
                action=Action.ESCALATE, rule_id="TAX.010",
                reason=(
                    f"The photographs show {sug.garment or 'a different garment'}, "
                    f"which belongs under '{category}' rather than '{p.category}' "
                    f"— but no subcategory there was identified, and moving the "
                    f"category alone would leave '{p.subcategory}' under a branch "
                    f"that does not offer it. Choose the pair."
                ),
                confidence=sug.confidence, provenance=Provenance.AI,
            )]

    same_cat = _match(p.category, [category]) is not None
    same_sub = subcategory is None or _match(p.subcategory, [subcategory]) is not None
    if same_cat and same_sub:
        # The picture agrees with the record. Nothing to write — and worth
        # nothing in the plan either, or every clean product would carry a
        # no-op entry.
        handled.update({"category", "subcategory"})
        return []

    floor = float(cfg.get("min_confidence") or 0.9)
    confident = sug.confidence >= floor
    reason = (
        f"The photographs show {sug.garment or 'this garment'}"
        + (f"; {sug.reasoning}" if sug.reasoning else "")
        + f" (confidence {sug.confidence:.2f}"
        + ("" if confident else f", below the {floor:.2f} floor")
        + ")"
    )
    action = Action.APPLY if confident else Action.PROPOSE

    out = [_gate(Patch(
        field="category", old_value=p.category, new_value=category,
        action=action, rule_id="TAX.010", reason=reason,
        confidence=sug.confidence, provenance=Provenance.AI,
    ), pol, p)]
    if subcategory is not None:
        out.append(_gate(Patch(
            field="subcategory", old_value=p.subcategory, new_value=subcategory,
            action=action, rule_id="TAX.010", reason=reason,
            confidence=sug.confidence, provenance=Provenance.AI,
        ), pol, p))

    # BOTH OR NEITHER, and `_gate` is why this check exists rather than being
    # implied by the code above. It downgrades a patch on a human-locked or
    # low-confidence field independently, so one half can come back APPLY and
    # the other PROPOSE — which would write exactly the split path this function
    # exists to prevent. Any disagreement demotes the pair.
    actions = {pt.action for pt in out}
    if len(actions) > 1:
        for pt in out:
            pt.action = Action.PROPOSE

    handled.update({"category", "subcategory"})
    return out


def resolve(p: ProductSnapshot, findings: list[Finding], ev: Evidence,
            pol: dict[str, Any]) -> list[Patch]:
    return resolve_pricing(p, findings, ev, pol) + resolve_attributes(p, findings, ev, pol)
