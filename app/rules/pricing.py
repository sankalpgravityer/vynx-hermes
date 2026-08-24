"""Pricing prediction, verification, and correction.

The whole model in four lines:

    expected     = retail_price * price_factor
    min_allowed  = expected * (1 - tolerance)
    max_allowed  = expected * (1 + tolerance)
    correct      = min_allowed <= price <= max_allowed

`price_factor` is the TENANT'S OWN `Grade.priceFactor`, delivered with each
product by the VNYX feed — not a number in this file. That matters: grade codes
mean different things per tenant (one shop's Grade D is "Good", another's is
"Recycle"), so a policy-file band mis-prices every custom scale. When the feed
supplies no factor, `grade_targets` in policy.yaml is the fallback and the
assessment says so in `rule`.

A price inside the window is left completely alone. A price outside it is
clamped to the *nearest* edge, not pulled to the target — the smallest change
that makes the record valid.

WHY THE WINDOW HAS A TOLERANCE AT ALL, since exact arithmetic is available:
`price = retail_price * priceFactor` is only guaranteed on ONE VNYX code path —
a manual regrade (services/products.ts, updateProduct: it requires a PRIOR grade
and no explicit price in the same request). Products priced at analyze time get
`price` from an eBay used-listing search, which bears no arithmetic relation to
the factor. Exact equality would therefore flag most of the catalog. The
tolerance keeps an analyze-derived price that lands near the tenant's intent
quiet, while a price far from it still surfaces. `exact_factor_match` records
which of the two a given row is.

None of this is delegated to a language model. It is arithmetic over a policy
table, so it is reproducible, auditable, and testable.
"""

from __future__ import annotations

import math
from typing import Any

from app.models import (
    Finding, PriceAssessment, PriceVerdict, ProductSnapshot, Severity,
)


# --------------------------------------------------------------------------- #
# Grade + window resolution
# --------------------------------------------------------------------------- #

def grade_of(p: ProductSnapshot, pol: dict[str, Any]) -> str:
    """Resolve the grade letter from `grade`, falling back to `condition`."""
    targets = pol["pricing"]["grade_targets"]
    if p.grade:
        letter = str(p.grade).strip().upper()[:1]
        if letter in targets:
            return letter
    if p.condition:
        mapped = pol["pricing"]["condition_to_grade"].get(
            str(p.condition).strip().lower()
        )
        if mapped in targets:
            return mapped
    return "C"  # conservative mid-tier default


def window_for(
    subject: ProductSnapshot | str, pol: dict[str, Any]
) -> dict[str, Any]:
    """The allowed price/retail ratio window.

    Accepts either:
      - a ProductSnapshot — uses the tenant's own `Grade.priceFactor` when the
        feed supplied one, which is the case that matters in production; or
      - a bare grade letter ("C") — the policy band for that grade, with no
        tenant context. Useful for inspecting the configured ladder.

    Reports which authority it used in `rule`, so a verdict is always
    attributable to either the tenant's config or this file's fallback.

    The high bound is additionally capped by `hard_max_ratio`, so a generous
    tolerance can never open the door to pricing at or above retail.
    """
    pr = pol["pricing"]

    if isinstance(subject, str):
        grade = subject if subject in pr["grade_targets"] else "C"
        factor = None
    else:
        grade = grade_of(subject, pol)
        factor = subject.price_factor

    if factor is not None and float(factor) > 0:
        target = float(factor)
        rule = "backend_factor"
        tol = float(pr.get("factor_tolerance", pr["tolerance"]))
    else:
        target = float(pr["grade_targets"][grade])
        rule = "policy_band"
        tol = float(pr.get("tolerance_overrides", {}).get(grade, pr["tolerance"]))

    low = target * (1.0 - tol)
    # Cap just BELOW hard_max_ratio, not at it: PRICE.001 fires at `>=`, so a
    # bound of exactly hard_max_ratio would describe a window whose own top edge
    # is a critical violation.
    high = min(target * (1.0 + tol), float(pr["hard_max_ratio"]) - 1e-9)
    return {
        "grade": grade,
        "rule": rule,
        "target": target,
        "tolerance": tol,
        "low": low,
        "high": high,
    }


def band_for(p: ProductSnapshot, pol: dict[str, Any]) -> dict[str, Any]:
    """Ratio band for callers that want it directly (the resolver uses this)."""
    return window_for(p, pol)


# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #

def charm(x: float, endings: list[float], direction: int = 0) -> float:
    """Snap to a psychological price ending.

    direction  0 -> nearest
              +1 -> nearest ending at or ABOVE x  (use at a lower bound)
              -1 -> nearest ending at or BELOW x  (use at an upper bound)

    Rounding inward at a bound is what keeps a charm-rounded price inside the
    window it was just clamped to.
    """
    if x <= 0:
        return 0.0
    base = math.floor(x)
    cands = sorted({
        round(b + e, 2)
        for b in range(base - 2, base + 3)
        for e in endings
        if b + e > 0
    })
    if direction > 0:
        pool = [c for c in cands if c >= x - 1e-9]
        return pool[0] if pool else cands[-1]
    if direction < 0:
        pool = [c for c in cands if c <= x + 1e-9]
        return pool[-1] if pool else cands[0]
    return min(cands, key=lambda c: abs(c - x))


def charm99(x: float) -> float:
    """Keep the whole units, set the cents to .99.

        15.60 -> 15.99      23.40 -> 23.99      19.00 -> 19.99

    Distinct from `charm` above, which snaps to the NEAREST of several endings
    and will happily cross a unit boundary to do it: charm(23.40) returns 22.99,
    a whole unit lower. That is wrong for a shop that wants every shelf price to
    read "<the number you expected>.99".

    Always rounds UP within the unit, so it never quietly discounts. The one
    place that matters is an upper bound, where going up could breach the
    retail ceiling — `charm99_at_most` is for those.
    """
    if x <= 0:
        return 0.0
    return math.floor(x) + 0.99


def charm99_at_most(x: float) -> float:
    """The largest `n.99` that does not exceed `x`.

    Needed wherever a ceiling must hold: charm99(19.00) is 19.99, which on a
    20.00 retail price would push the maximum above the hard_max_ratio the
    ABOVE_RETAIL invariant enforces. Returns 0.0 when nothing fits.
    """
    if x < 0.99:
        return 0.0
    n = math.floor(x)
    candidate = n + 0.99
    if candidate <= x + 1e-9:
        return candidate
    return max(n - 1 + 0.99, 0.0)


def round_price(x: float, pol: dict[str, Any], direction: int = 0) -> float:
    """Apply the configured rounding mode."""
    mode = pol["pricing"].get("round_mode", "exact")
    if mode == "charm99":
        # `direction` is ignored on purpose. It exists for `charm` mode, where
        # rounding to the nearest ending can escape a window whose bounds are
        # raw. Under charm99 the WINDOW ITSELF is charmed (see assess), so
        # rounding up lands exactly ON the bound rather than past it — and every
        # caller that has a hard ceiling caps the result immediately after.
        # Honouring direction here would clamp 78.00 to 47.99 while the
        # assessment reported a 48.99 maximum: two numbers for one edge.
        return charm99(x)
    if mode == "charm":
        return charm(x, pol["pricing"]["charm_endings"], direction)
    return round(x, 2)


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------- #
# Assessment — predict, verify, correct
# --------------------------------------------------------------------------- #

def assess(p: ProductSnapshot, pol: dict[str, Any]) -> PriceAssessment:
    """Predict what this item should sell for and judge what it's actually set to."""
    pr = pol["pricing"]
    w = window_for(p, pol)
    currency = p.currency or pr["default_currency"]

    a = PriceAssessment(
        rule=w["rule"],
        grade=p.grade or w["grade"],
        currency=currency,
        retail_price=p.retail_price,
        actual_price=p.price,
        price_factor=round(w["target"], 4),
        discount_pct=round(1.0 - w["target"], 4),
        tolerance_pct=round(w["tolerance"], 4),
    )

    if not p.retail_price or p.retail_price <= 0:
        a.verdict = PriceVerdict.NO_ANCHOR
        a.explanation = (
            "No retail price to measure against, so the selling price cannot be "
            "verified. Supply the original retail price, or let the evidence layer "
            "look up the RRP."
        )
        return a

    retail = float(p.retail_price)
    # Round the bounds to currency precision BEFORE comparing. 100 * 0.32 is
    # 32.000000000000004 in binary floating point, which would make a price of
    # exactly 32.00 read as too low. Compare against what we would actually write.
    expected = round(retail * w["target"], 2)
    # The bounds come off the ROUNDED expectation, not from retail x target again.
    # applyGradePriceFactor in vnyx-api stores `result.toFixed(2)`, so the rounded
    # figure is the real centre of the window: a 129.99 retail at factor 0.5 gives
    # the backend 65.00, not 64.995. Re-deriving from the raw product put the two
    # verifiers a cent apart — harmless while both wrote exact cents, but charm99
    # rounds a cent into a whole unit (77.99 against 78.99), which is enough for
    # a Verify click to move a price the analyze worker had just set.
    min_allowed = round(max(expected * (1 - w["tolerance"]),
                            float(pr["min_price"])), 2)
    max_allowed = round(expected * (1 + w["tolerance"]), 2)

    # Charm the WINDOW, not just the corrected figure. A window of 15.60-23.40
    # with prices ending .99 would report bounds no price can ever sit on, and
    # clamping to 23.40 would produce a price the shop does not want to display.
    # Charming both ends means a clamped price lands on a real shelf price.
    #
    # `expected` is deliberately left raw: it is the mathematical target used to
    # explain the verdict, not a figure anyone writes.
    if pr.get("round_mode") == "charm99":
        min_allowed = charm99(min_allowed)
        # The ceiling has to survive rounding up. hard_max_ratio is what the
        # ABOVE_RETAIL invariant checks, so the top of the window must stay
        # strictly under it — charm99_at_most picks the highest .99 that does.
        ceiling99 = charm99_at_most(retail * float(pr["hard_max_ratio"]) - 0.01)
        max_allowed = min(charm99(max_allowed), ceiling99)
        # A very cheap item can end up with the floor above the (charmed)
        # ceiling; the existing min > max branch below reports that as
        # NO_ANCHOR rather than silently inverting the window.

    a.expected_price = expected
    a.min_allowed = min_allowed
    a.max_allowed = max_allowed

    if min_allowed > max_allowed:
        a.verdict = PriceVerdict.NO_ANCHOR
        a.explanation = (
            f"The {pr['min_price']:.2f} price floor exceeds the maximum this grade "
            f"allows ({max_allowed:.2f}). The item is not worth listing at Grade "
            f"{a.grade}; regrade it or scrap it."
        )
        return a

    if not p.price or p.price <= 0:
        a.verdict = PriceVerdict.NO_PRICE
        # Clamped into the window: rounding up can push a small target past the
        # (charmed) ceiling, and the min_price floor can sit above the target on
        # a cheap item. In exact mode both bounds are no-ops.
        a.corrected_price = clamp(round_price(expected, pol), min_allowed, max_allowed)
        a.change_required = True
        a.explanation = (
            f"No selling price set. Grade {a.grade} targets {w['target']:.0%} of "
            f"{retail:.2f} {currency}, so {a.corrected_price:.2f} is the expected price."
        )
        return a

    price = round(float(p.price), 2)
    ratio = price / retail
    a.actual_pct_of_retail = round(ratio, 4)
    # Was this price last written BY the factor? To the cent means yes (a manual
    # regrade); anything else came from the analyze-time eBay search.
    a.exact_factor_match = abs(price - expected) <= 0.01

    # Hard invariant first — checked independently of the window, because a used
    # item priced at its own RRP is wrong no matter how the ladder is configured.
    if ratio >= float(pr["hard_max_ratio"]):
        a.verdict = PriceVerdict.ABOVE_RETAIL
        a.corrected_price = round_price(max_allowed, pol, direction=-1)
        a.change_required = True
        a.explanation = (
            f"{price:.2f} is {ratio:.0%} of the {retail:.2f} retail price. A "
            f"second-hand item cannot be priced at or above {pr['hard_max_ratio']:.0%} "
            f"of retail. Clamped down to the Grade {a.grade} maximum of "
            f"{a.corrected_price:.2f} {currency}."
        )
        return a

    if price < min_allowed:
        a.verdict = PriceVerdict.TOO_LOW
        a.corrected_price = round_price(min_allowed, pol, direction=+1)
        a.change_required = True
        a.explanation = (
            f"{price:.2f} is only {ratio:.0%} of retail. Grade {a.grade} expects "
            f"{w['target']:.0%} ({expected:.2f}) with a +/-{w['tolerance']:.0%} "
            f"tolerance, so the window is {min_allowed:.2f}-{max_allowed:.2f}. "
            f"Raised to the minimum, {a.corrected_price:.2f} {currency}. "
            f"We are underselling by {min_allowed - price:.2f}."
        )
        return a

    if price > max_allowed:
        a.verdict = PriceVerdict.TOO_HIGH
        a.corrected_price = round_price(max_allowed, pol, direction=-1)
        a.change_required = True
        a.explanation = (
            f"{price:.2f} is {ratio:.0%} of retail. Grade {a.grade} expects "
            f"{w['target']:.0%} ({expected:.2f}) with a +/-{w['tolerance']:.0%} "
            f"tolerance, so the window is {min_allowed:.2f}-{max_allowed:.2f}. "
            f"Lowered to the maximum, {a.corrected_price:.2f} {currency}. "
            f"It was overpriced by {price - max_allowed:.2f} and unlikely to sell."
        )
        return a

    # In range — but under charm99 that is not yet a price the shop wants on a
    # shelf. 15.60 and 18.40 both sit comfortably inside their window, and they
    # are exactly the endings this mode exists to remove, so the ratio check
    # passing is not on its own a reason to leave the cents alone.
    #
    # Capped at max_allowed: rounding UP must not carry the price out of the
    # window it just passed.
    normalised = round(round_price(price, pol), 2)
    if pr.get("round_mode") == "charm99":
        normalised = round(min(normalised, max_allowed), 2)

    if abs(normalised - price) > 0.001:
        a.verdict = PriceVerdict.ROUND_REQUIRED
        a.corrected_price = normalised
        a.change_required = True
        a.explanation = (
            f"{price:.2f} is {ratio:.0%} of retail, inside the Grade {a.grade} "
            f"window {min_allowed:.2f}-{max_allowed:.2f}, so the ratio is right. "
            f"Rounded to {a.corrected_price:.2f} {currency} for a .99 price ending."
        )
        return a

    a.verdict = PriceVerdict.OK
    a.corrected_price = round(price, 2)
    a.change_required = False
    a.explanation = (
        f"{price:.2f} is {ratio:.0%} of retail, inside the Grade {a.grade} window "
        f"{min_allowed:.2f}-{max_allowed:.2f} ({w['target']:.0%} +/-"
        f"{w['tolerance']:.0%}). Correct as-is; no change."
    )
    return a


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #

def check(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    pr = pol["pricing"]

    if p.price is not None and 0 < p.price < pr["min_price"]:
        out.append(Finding(
            rule_id="PRICE.011", severity=Severity.MEDIUM, fields=["price"],
            message=f"Selling price {p.price:.2f} is below the {pr['min_price']:.2f} "
                    "floor; handling cost would exceed revenue.",
            detail={"price": p.price, "floor": pr["min_price"]},
        ))

    if not p.currency:
        out.append(Finding(
            rule_id="PRICE.030", severity=Severity.MEDIUM, fields=["currency"],
            message="Currency is not set; defaulting is unsafe for a priced listing.",
            detail={"default": pr["default_currency"]},
        ))

    a = assess(p, pol)
    payload = a.model_dump(mode="json")

    if a.verdict is PriceVerdict.NO_PRICE:
        out.append(Finding(
            rule_id="PRICE.010", severity=Severity.CRITICAL, fields=["price"],
            message=a.explanation, detail=payload, needs_evidence=True,
        ))
    elif a.verdict is PriceVerdict.NO_ANCHOR:
        out.append(Finding(
            rule_id="PRICE.020", severity=Severity.HIGH, fields=["retail_price"],
            message=a.explanation, detail=payload, needs_evidence=True,
        ))
    elif a.verdict is PriceVerdict.ABOVE_RETAIL:
        out.append(Finding(
            rule_id="PRICE.001", severity=Severity.CRITICAL,
            fields=["price", "retail_price"],
            message=a.explanation, detail=payload, needs_evidence=True,
        ))
    elif a.verdict is PriceVerdict.TOO_HIGH:
        out.append(Finding(
            rule_id="PRICE.002", severity=Severity.HIGH,
            fields=["price", "retail_price"],
            message=a.explanation, detail=payload, needs_evidence=True,
        ))
    elif a.verdict is PriceVerdict.TOO_LOW:
        out.append(Finding(
            rule_id="PRICE.003", severity=Severity.HIGH,
            fields=["price", "retail_price"],
            message=a.explanation, detail=payload, needs_evidence=False,
        ))
    # ROUND_REQUIRED deliberately emits NO finding. A shelf price of 15.60 is not
    # a data defect: a finding here would mark most of a correct catalog as
    # incorrect, and vnyx-api's verify route lists any finding outside its
    # PRICE_WRITE_RESOLVES set as still outstanding — so a rounding would keep
    # being reported as unresolved immediately after the write that fixed it.
    # The verdict and corrected_price on the assessment carry it instead, and
    # those are what the write is gated on.

    out.extend(check_config(p, pol))
    out.extend(check_mirror(p, pol))
    return out


def check_config(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """Problems with the tenant's pricing CONFIG rather than this product.

    Reported on the product because that is where a reviewer meets them, but the
    fix is in the grade ladder, not the record — so they are deliberately
    separate rule ids. Without this, a missing priceFactor shows up as a silent
    fallback to the policy band and the verdict looks tenant-specific when it
    is not.
    """
    out: list[Finding] = []
    pr = pol["pricing"]

    if p.grade and p.price_factor is None:
        # Which fallback actually applied? grade_of() recognises only the codes
        # present in policy.yaml's grade_targets, so a tenant using its own
        # scale ("E", "1", "EXCELLENT") lands on the mid-tier C default. Naming
        # the borrowed grade matters: reporting a bare "0.4" for grade E reads as
        # though 0.4 were E's configured value, when it is grade C's and has
        # nothing to do with this product.
        targets = pr["grade_targets"]
        letter = grade_of(p, pol)
        recognised = str(p.grade).strip().upper()[:1] in targets
        used = targets.get(letter)
        fix = "Set it in Product settings > Pricing Multiplier."
        if recognised:
            message = (
                f"Grade {p.grade} has no Regrade Factor configured, so this price "
                f"was judged against the policy default for grade {letter} "
                f"({used:.0%}) rather than the tenant's own ladder. {fix}"
            )
        else:
            message = (
                f"Grade {p.grade} has no Regrade Factor configured, and is not in "
                f"the policy fallback table either, so this price was judged "
                f"against a mid-tier grade {letter} default of {used:.0%}. That "
                f"figure has nothing to do with grade {p.grade}. {fix}"
            )
        out.append(Finding(
            rule_id="PRICE.101", severity=Severity.MEDIUM,
            fields=["price_factor", "grade"],
            message=message,
            detail={"grade": p.grade, "fallback_grade": letter,
                    "fallback_target": used, "grade_recognised": recognised},
        ))

    if p.price_factor is not None and float(p.price_factor) >= float(pr["hard_max_ratio"]):
        out.append(Finding(
            rule_id="PRICE.102", severity=Severity.HIGH,
            fields=["price_factor"],
            message=(
                f"Grade {p.grade}'s configured priceFactor "
                f"({float(p.price_factor):.0%}) is at or above the "
                f"{float(pr['hard_max_ratio']):.0%} ceiling for second-hand goods. "
                "Every product on this grade will price at roughly its own RRP."
            ),
            detail={"price_factor": float(p.price_factor),
                    "hard_max_ratio": pr["hard_max_ratio"]},
        ))

    return out


def check_mirror(p: ProductSnapshot, _pol: dict[str, Any]) -> list[Finding]:
    """Product.price vs the ProductVariant.basePrice mirror.

    VNYX forward-mirrors price into ProductVariant/Price, and the marketplace
    sync publishes the MIRROR — so when these disagree the catalog shows one
    figure and the channel sells at another. The mirror is best-effort
    (syncDefaultVariantSafe swallows its failures) and several writers reach
    Product.price through a bare update, so drift is reachable.

    HIGH, not critical: the record is internally wrong but neither figure is
    absurd, and which one is right depends on which write landed last — a human
    has to look.
    """
    drift = p.variant_price_drift
    if drift is None or abs(float(drift)) <= 0.01:
        return []
    return [Finding(
        rule_id="PRICE.110", severity=Severity.HIGH,
        fields=["price", "variant_base_price"],
        message=(
            f"Catalog price {p.price} does not match the published variant price "
            f"{p.variant_base_price} (drift {float(drift):+.2f}). The marketplace "
            "sync reads the variant, so the channel is selling at a different "
            "price from the one shown on the product."
        ),
        detail={"price": p.price, "variant_base_price": p.variant_base_price,
                "drift": float(drift)},
    )]


def verify_invariants(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """Post-repair gate: only the non-negotiable checks.

    The pipeline re-runs this against the patched snapshot. Anything that still
    fires means the repair failed, and the patch is refused rather than written.
    """
    hard = {"PRICE.001", "PRICE.010"}
    return [f for f in check(p, pol) if f.rule_id in hard]
