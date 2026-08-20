"""Domain models for Hermes.

The product snapshot is deliberately *flat*, with confidence/provenance/lock
state carried in sidecar maps. That keeps the mapping from the VNYX payload
trivial while still letting the resolver reason about how much to trust each
individual field.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Provenance(str, Enum):
    """Where a field's current value came from. Drives trust arbitration."""

    HUMAN = "human"          # an operator typed/confirmed it — never overwrite
    GROUNDED = "grounded"    # search-grounded, with citable sources
    MARKET = "market"        # market comparable lookup
    DERIVED = "derived"      # computed from another field (e.g. grade multiplier)
    AI = "ai"                # raw model guess
    UNKNOWN = "unknown"


PROVENANCE_RANK: dict[Provenance, int] = {
    Provenance.HUMAN: 100,
    Provenance.GROUNDED: 80,
    Provenance.MARKET: 60,
    Provenance.DERIVED: 40,
    Provenance.AI: 20,
    Provenance.UNKNOWN: 0,
}


class Severity(str, Enum):
    CRITICAL = "critical"   # must not reach a sales channel
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Action(str, Enum):
    APPLY = "apply"         # safe to write back automatically
    PROPOSE = "propose"     # surface in UI, human clicks accept
    ESCALATE = "escalate"   # human must investigate; Hermes will not guess


class TenantCatalog(BaseModel):
    """The option lists a tenant's review screen actually offers.

    These are the five lists the edit page loads to populate its dropdowns
    (`/categories`, `/sizes`, `/brands`, `/color-settings`, `/material-settings`),
    so a stored value absent from them is one no reviewer could have chosen.

    Carrying them per product removes the last place this service was guessing at
    tenant configuration — and the guesses were not close. A real tenant's Men
    tree is `Accessories / Backpacks & Bags / Bottoms / Jackets / Shirts / Shoes /
    Sweaters & Hoodies / T-Shirts & Polos / Vests`: no `Tops`, no `Outerwear`, no
    `Footwear`, which is three of the five categories policy.yaml assumes. Its
    Women Bottoms chart maps W28 to EU 36 where the generic table says 40.
    Validating against the policy file instead would reject most of the catalog.
    """

    # The VNYX feed is camelCase throughout, so `sizingGuides` is accepted by
    # alias while the Python attribute keeps snake_case. populate_by_name lets a
    # hand-written fixture use either spelling.
    model_config = ConfigDict(populate_by_name=True)

    # masterCategory -> category -> subcategories. Same shape as pol["taxonomy"],
    # so it is a drop-in replacement for it.
    categories: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    # Sizing-guide name -> its index-aligned size/EU pairs. VNYX documents the
    # alignment on the column itself: sizes[i] <-> euSizes[i].
    sizing_guides: dict[str, dict[str, list[str]]] = Field(
        default_factory=dict, alias="sizingGuides"
    )
    colors: list[str] = Field(default_factory=list)
    materials: list[str] = Field(default_factory=list)
    brands: list[str] = Field(default_factory=list)

    def eu_for_size(self, guide: str | None, size: str | None) -> str | None:
        """The EU size this guide pairs with `size`, or None if it cannot say.

        Matched case- and whitespace-insensitively because the stored attribute
        ("w32") and the chart entry ("W32") are written by different paths.
        """
        if not guide or not size:
            return None
        pair = self.sizing_guides.get(guide)
        if not pair:
            return None
        sizes, eu = pair.get("sizes") or [], pair.get("euSizes") or []
        want = str(size).strip().lower()
        for i, candidate in enumerate(sizes):
            if str(candidate).strip().lower() == want and i < len(eu):
                return str(eu[i])
        return None


class ProductSnapshot(BaseModel):
    """Normalised view of one VNYX product."""

    id: str
    tenant_id: str | None = None
    # Deep-link to the review screen for this product, built by the VNYX API
    # (it owns the public origin, which differs per environment). Echoed back on
    # every verdict so a caller can act on a finding without a second lookup.
    edit_url: str | None = None

    # identity
    sku: str | None = None
    product_code: str | None = None
    lpn_code: str | None = None
    bin_code: str | None = None
    # The 10-digit scannable "Bin ID" and the location's zone/warehouse codes.
    #
    # NOTE: `bin_barcode` below is NOT bin_number. In VNYX the printed barcode
    # encodes bin_number (a 10-digit number), while bin_code is a location string
    # like "A-04-12-2" — comparing them would fire ID.001 on every product. It is
    # left unmapped until a field genuinely holding a second copy of the LOCATION
    # code exists; see check_identity.
    bin_number: str | None = None
    bin_zone_code: str | None = None
    bin_warehouse_code: str | None = None
    bin_barcode: str | None = None

    # taxonomy
    master_category: str | None = None
    category: str | None = None
    subcategory: str | None = None

    # copy
    title: str | None = None
    description: str | None = None

    # commercial
    currency: str | None = None
    price: float | None = None            # what we sell it for
    retail_price: float | None = None     # original RRP anchor
    inventory: int | None = None

    # ── The tenant's own pricing config, supplied by the VNYX feed ──────────
    #
    # `price_factor` is Grade.priceFactor for THIS product's grade: the fraction
    # of retail_price the tenant has configured this grade to sell at. It is the
    # authority on what the price should be — a policy file can only guess at it,
    # and it is per-tenant (Blackmann's Grade D and BOAS's Grade D mean different
    # things), so a hardcoded band mis-prices custom scales.
    price_factor: float | None = None
    # retail_price x price_factor, as the VNYX backend itself computes it
    # (applyGradePriceFactor). Recomputed here rather than trusted blindly, but
    # carried so a disagreement between the two is visible.
    expected_price: float | None = None
    # Where retail_price came from: 'derived' (grade multiplier — circular
    # evidence for the price derived from it), 'market' (eBay comparables), or
    # 'unknown'. Read off Product.retailPriceBreakdown.source.
    retail_provenance: str | None = None

    # ── Mirror drift (VNYX-internal, invisible from the public API) ─────────
    #
    # ProductVariant.basePrice is forward-mirrored from Product.price, and the
    # marketplace sync publishes the MIRROR. When they diverge the catalog shows
    # one price and the channel sells at another.
    variant_base_price: float | None = None
    variant_price_drift: float | None = None

    # attributes
    gender: str | None = None
    sizing_guide: str | None = None
    international_size: str | None = None
    eu_size: str | None = None
    size: str | None = None
    waist: str | None = None
    length_size: str | None = None
    fit: str | None = None
    brand: str | None = None
    model: str | None = None
    color: str | None = None
    material: str | None = None

    # condition
    condition: str | None = None
    grade: str | None = None
    # The tenant's own human label for `grade` ("Lived In" for C). VNYX syncs
    # properties.condition to this on ANY grade write (see updateConditionFromGrade
    # in services/products.ts), which makes condition == grade_label a real
    # invariant here — checkable exactly, without the guessed condition_to_grade
    # table a policy file would have to fall back on.
    grade_label: str | None = None
    defects: list[str] = Field(default_factory=list)
    # Defects the OPERATOR reported while photographing. In VNYX these REPLACE the
    # AI's findings in qualityGrading.defects when present, so a grade-vs-defects
    # rule must consider both before calling a list empty.
    operator_defects: list[str] = Field(default_factory=list)

    # ops
    mannequin: str | None = None
    hanger: str | None = None
    supplier: str | None = None

    images: list[str] = Field(default_factory=list)

    # sidecars
    confidence: dict[str, float] = Field(default_factory=dict)
    provenance: dict[str, Provenance] = Field(default_factory=dict)
    locked_fields: list[str] = Field(default_factory=list)

    # This tenant's own option lists. None means none were supplied (a raw
    # webhook payload, or a hand-written fixture), in which case the rules fall
    # back to policy.yaml and say so.
    catalog: TenantCatalog | None = None

    def prov(self, field: str) -> Provenance:
        if field in self.locked_fields:
            return Provenance.HUMAN
        return self.provenance.get(field, Provenance.UNKNOWN)

    def trust(self, field: str) -> int:
        return PROVENANCE_RANK[self.prov(field)]

    def conf(self, field: str) -> float:
        return self.confidence.get(field, 0.0)

    def is_locked(self, field: str) -> bool:
        return field in self.locked_fields or self.prov(field) is Provenance.HUMAN


class PriceVerdict(str, Enum):
    """Outcome of the deterministic price assessment."""

    OK = "ok"                      # inside the window the tenant's factor implies
    TOO_HIGH = "too_high"
    TOO_LOW = "too_low"
    ABOVE_RETAIL = "above_retail"  # at or above RRP — arithmetically impossible
    NO_PRICE = "no_price"
    NO_ANCHOR = "no_anchor"        # no retail_price to measure against
    NO_FACTOR = "no_factor"        # grade has no priceFactor configured


class PriceAssessment(BaseModel):
    """What this item should sell for, and what it is actually set to.

    Reported on every product, whether or not it produced a finding — a reviewer
    asking "why is this flagged" and a reviewer asking "is this price right"
    want the same numbers, and withholding them on a clean record just forces a
    second call.
    """

    # Which authority decided the expected price. 'backend_factor' = the tenant's
    # own Grade.priceFactor; 'policy_band' = grade_targets from policy.yaml.
    rule: str = "backend_factor"
    grade: str | None = None
    currency: str = "EUR"

    retail_price: float | None = None
    actual_price: float | None = None

    # The tenant's configured fraction of retail for this grade, and the discount
    # it implies (0.40 -> 60% off).
    price_factor: float | None = None
    discount_pct: float | None = None
    tolerance_pct: float | None = None

    expected_price: float | None = None
    min_allowed: float | None = None
    max_allowed: float | None = None
    actual_pct_of_retail: float | None = None

    corrected_price: float | None = None
    change_required: bool = False

    verdict: PriceVerdict = PriceVerdict.OK
    explanation: str = ""

    # True only when the price matches the factor to the cent — i.e. it was last
    # written BY the factor (a manual regrade). False means the price came from
    # somewhere else, most often the eBay used-listing search at analyze time,
    # which is legitimate and is why the window has a tolerance at all.
    exact_factor_match: bool | None = None


class Finding(BaseModel):
    """A rule violation. Findings describe problems; they never fix them."""

    rule_id: str
    severity: Severity
    fields: list[str]
    message: str
    detail: dict[str, Any] = Field(default_factory=dict)
    # True when the resolver needs external evidence (LLM / search) to repair.
    needs_evidence: bool = False


class Patch(BaseModel):
    field: str
    old_value: Any = None
    new_value: Any = None
    action: Action = Action.PROPOSE
    rule_id: str = ""
    reason: str = ""
    confidence: float = 0.0
    provenance: Provenance = Provenance.DERIVED
    sources: list[str] = Field(default_factory=list)


class RRPEvidence(BaseModel):
    """Grounded original-retail-price lookup result."""

    found: bool = False
    rrp: float | None = None
    currency: str | None = None
    confidence: float = 0.0
    reasoning: str = ""
    sources: list[str] = Field(default_factory=list)


class AttributeVerdict(BaseModel):
    field: str
    verdict: Literal["confirm", "contradict", "uncertain"]
    observed_value: str | None = None
    confidence: float = 0.0
    evidence: str = ""


class VisionAudit(BaseModel):
    verdicts: list[AttributeVerdict] = Field(default_factory=list)
    visible_defects: list[str] = Field(default_factory=list)
    notes: str = ""


class Evidence(BaseModel):
    rrp: RRPEvidence = Field(default_factory=RRPEvidence)
    vision: VisionAudit = Field(default_factory=VisionAudit)
    text_verdicts: list[AttributeVerdict] = Field(default_factory=list)
    llm_calls: int = 0


class ReconcileStatus(str, Enum):
    CLEAN = "clean"                  # nothing wrong
    REPAIRED = "repaired"            # fixed automatically, invariants re-verified
    NEEDS_REVIEW = "needs_review"    # patches proposed, awaiting a human
    BLOCKED = "blocked"              # critical issue Hermes refuses to guess at


class ReconcileResult(BaseModel):
    product_id: str
    status: ReconcileStatus
    findings: list[Finding] = Field(default_factory=list)
    residual_findings: list[Finding] = Field(default_factory=list)
    patches: list[Patch] = Field(default_factory=list)
    applied: list[str] = Field(default_factory=list)
    publishable: bool = True
    llm_calls: int = 0
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Review-queue verification — the read-only sweep
# --------------------------------------------------------------------------- #

class ProductVerdict(BaseModel):
    """Is this product's review data correct, and if not, where do I fix it?

    Report-only by construction: there is no patch list and no applied list. The
    answer to a failed check is `edit_url`, not a write.
    """

    product_id: str
    # The single field a caller branches on: False when a finding at or above the
    # request's severity floor fired (default MEDIUM).
    #
    # Deliberately NOT "any finding at all". CONF.001 fires on every field the AI
    # extracted with low confidence and TEXT.002 on any title that does not
    # restate the colour — real signals, but cosmetic ones. Letting them flip
    # `correct` would hand back an edit URL for almost every product and make the
    # answer useless. Nothing is hidden: below-floor findings still appear in
    # `findings` and are counted in `advisory_count`.
    correct: bool
    status: ReconcileStatus
    publishable: bool
    findings: list[Finding] = Field(default_factory=list)
    # Findings BELOW the floor — present, counted, but not disqualifying.
    advisory_count: int = 0
    # The floor this verdict was judged at, echoed so a caller never has to
    # remember what it asked for.
    min_severity: Severity = Severity.MEDIUM
    # Highest severity present, or null on a clean record — so a list can be
    # sorted or badged without walking every finding.
    worst_severity: Severity | None = None
    price: PriceAssessment | None = None
    # Where a human goes to fix it. Passed through from the feed rather than
    # constructed here: the public origin is environment-specific and belongs to
    # whoever serves the UI.
    edit_url: str | None = None
    llm_calls: int = 0


class ReviewQueueResponse(BaseModel):
    verdicts: list[ProductVerdict] = Field(default_factory=list)
    # Echoed so a caller can tell WHICH rules produced a verdict — a clean result
    # from three rule groups is a different fact from a clean result from seven.
    rule_groups: list[str] = Field(default_factory=list)
    checked: int = 0
    incorrect: int = 0
    duration_ms: int = 0
