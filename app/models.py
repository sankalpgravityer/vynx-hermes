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


class MediaAsset(BaseModel):
    """One row of VNYX's `ProductMedia`, the source of truth for a product's assets.

    `ProductSnapshot.images` is a flat list of URLs — VNYX's own denormalized
    `Product.images` cache — which is enough for the vision layer but says nothing
    about what any picture IS. Every imagery question needs the typed row instead:
    a URL cannot tell you whether it is a care label the segmenter must never
    touch, an AI render, or a raw upload still waiting to be cut out.
    """

    url: str
    # FRONT | BACK | LABEL | SIZE_CHART | AI_FRONT | AI_BACK | AI_FRONT_34 |
    # AI_BACK_34 | AI_CLOSEUP | VIDEO_TURNTABLE | OTHER
    view: str
    # DECISION | PHOTOBOOTH | WEB | AI | SIZE_GUIDE | MANUAL
    origin: str | None = None
    # RAW | BG_REMOVED | COMPOSITED | GENERATED | TRANSCODED.
    # Anything other than RAW has already been through the segmenter.
    processing: str = "RAW"
    media_type: str = "IMAGE"
    # "Nothing derives from this row." A RAW upload whose cut-out exists is
    # superseded, not live — counting it would report every successfully matted
    # product as still needing work.
    is_current: bool = True
    # A human removed it. A DIFFERENT fact from being superseded, and the two have
    # opposite meanings for whether the asset should ever come back.
    deleted_at: str | None = None
    position: int = 0

    @property
    def live(self) -> bool:
        return self.is_current and not self.deleted_at

    @property
    def is_ai(self) -> bool:
        return self.view.startswith("AI_")


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

    # The typed `ProductMedia` rows behind `images` above, when the caller sent
    # them. `images` is VNYX's own denormalized cache of the same assets and stays
    # the input to the vision layer, which only needs URLs; the imagery rules need
    # to know what each picture IS and read this instead.
    #
    # Empty is a legitimate state meaning "not supplied" — a raw webhook payload
    # or a hand-written fixture carries no media rows — so the imagery rules stay
    # silent rather than reporting a product with pictures as having none.
    media: list[MediaAsset] = Field(default_factory=list)

    # The generation pipeline's own view of this product.
    #
    # IDLE | GENERATING | COMPLETE | FAILED. Reported, never trusted: on the
    # measured tenant 78 of the 89 products with no renders at all are marked
    # COMPLETE, which is exactly why nobody had noticed. The media rows decide.
    generation_status: str | None = None
    is_regenerating: bool = False
    # ISO-8601. Only used to tell a live generation from a stranded one.
    updated_at: str | None = None
    # This tenant's ImageGenerationSettings row. None means it was not supplied,
    # and the imagery rules fall back to permissive defaults rather than reporting
    # a configuration they cannot see.
    imagery_settings: "ImagerySettings | None" = None

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

    OK = "ok"                      # inside the window, and already a shelf price
    # Inside the window, wrong cents. Its own verdict rather than an OK with a
    # flag set, because the consumers that decide whether to write a price branch
    # on this field and would treat an "ok" as nothing to do — vnyx-api's
    # /review-verification/:id/verify gates its write on `verdict !== 'ok'`.
    # A rounding is not a data defect, so it stays out of the findings list and
    # leaves `correct` true; this field is where it is reported.
    ROUND_REQUIRED = "round_required"
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


# --------------------------------------------------------------------------- #
# Imagery — are the on-model renders there, and are the backgrounds gone?
# --------------------------------------------------------------------------- #

class ImagerySettings(BaseModel):
    """The tenant's `ImageGenerationSettings` row.

    Load-bearing, not decoration. `isModelGenerationEnabled` decides whether a
    product with no renders is a defect or a configuration choice, and getting
    that backwards is what turns a verification pass into noise — the single
    largest source of false positives in the pricing validator.
    """

    model_config = ConfigDict(populate_by_name=True)

    is_model_generation_enabled: bool = Field(True, alias="isModelGenerationEnabled")
    is_close_up_enabled: bool = Field(True, alias="isCloseUpEnabled")
    is_remove_bg_enabled: bool = Field(True, alias="isRemoveBgEnabled")
    bg_removal_provider: str | None = Field(None, alias="bgRemovalProvider")
    # "transparent", a #rrggbb, or a /backgrounds/*.png path. When a backdrop is
    # configured, an opaque uniform background on a cut-out is CORRECT rather than
    # a failure — the segmenter ran and the tenant's backdrop went on top.
    background: str | None = None
    auto_apply_background: bool = Field(False, alias="autoApplyBackground")

    # Model appearance — passed straight through to the prompt builder.
    gender: str | None = None
    age: str | None = None
    ethnicity: str | None = None
    body_type: str | None = Field(None, alias="bodyType")
    custom_prompt: str | None = Field(None, alias="customPrompt")
    brand_color: str | None = Field(None, alias="brandColor")
    theme: str | None = None
    aspect_ratio: str | None = Field(None, alias="aspectRatio")
    resolution: str | None = None

    # Prompt add-ons the regenerate modal offers, applied on top of the
    # structured description rather than through it.
    lighting_texture: str | None = Field(None, alias="lightingTexture")
    realistic_skin_details: bool = Field(False, alias="realisticSkinDetails")

    # --- the tenant's cast of models -----------------------------------------
    #
    # A tenant defines named personalities — "Emma Smith, 24, fair, long sleek
    # dark brown hair" — and the pipeline picks one per product so the catalog
    # does not look like one person wearing everything. BOAS has 20.
    #
    # The traits below are the SELECTED personality's, flattened onto the
    # settings the prompt builder reads. That mirrors the TypeScript, where
    # analyze.worker.ts chooses the personality and hands
    # `generateClothOnModel` a settings object with the traits already resolved.
    personalities_enabled: bool = Field(False, alias="personalitiesEnabled")
    personalities: list[dict[str, Any]] = Field(default_factory=list)

    skin_tone: str | None = Field(None, alias="skinTone")
    hair_color: str | None = Field(None, alias="hairColor")
    hair_style: str | None = Field(None, alias="hairStyle")
    tattoos: str | None = None
    piercings: str | None = None
    personality_notes: str | None = Field(None, alias="personalityNotes")
    # Which personality supplied the traits, echoed so the caller can store it
    # and a later gap-fill can reuse the same face instead of drawing again.
    personality_name: str | None = Field(None, alias="personalityName")


class BackgroundVerdict(str, Enum):
    """What the pixels say is behind the garment."""

    TRANSPARENT = "transparent"      # alpha channel, cut out
    BACKDROP = "backdrop"            # opaque but uniform — a studio sweep or the
                                     # tenant's own configured backdrop
    SCENE = "scene"                  # a real background is still there
    UNKNOWN = "unknown"              # could not be fetched or decoded


class BackgroundCheck(BaseModel):
    """One image's background, and what decided it."""

    url: str
    view: str
    processing: str
    verdict: BackgroundVerdict
    # Which layer produced the verdict. A reviewer disputing a finding needs to
    # know whether a column, a pixel sample or a model said so.
    basis: Literal["metadata", "pixels", "vision"] = "metadata"
    confidence: float = 0.0
    detail: str = ""


class AiViewReport(BaseModel):
    """Which on-model renders exist, against which ones are required."""

    required: list[str] = Field(default_factory=list)
    present: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)
    # The ¾ and close-up views when they are not in `required`. Reported so the UI
    # can offer to fill them in without calling their absence a defect.
    advisory_missing: list[str] = Field(default_factory=list)
    # Rows, not distinct views. A product with five AI rows across ONE view has
    # its pictures and needs relabelling, not regeneration — see IMG.021.
    row_count: int = 0


class SourceImage(BaseModel):
    """A garment photograph the generator can be seeded from."""

    url: str
    view: str
    processing: str


class GenerationPlan(BaseModel):
    """THE DECISION: generate, or leave it alone.

    Hermes owns this, not the caller. The verdict used to report only facts —
    which views are missing, which originals need the segmenter — and every
    caller then re-derived "so should I generate?" from them. Two callers had
    already written that arithmetic twice (the repair button and the sweep after
    generation), which is two chances to disagree about whether an advisory ¾
    view counts, or whether matting alone is worth a job. The rules live in one
    place and speak once; the caller obeys.

    `should_generate` is the whole answer. `views` is what to generate when it is
    true, and is empty when the only outstanding work is background removal —
    so a caller must check `should_generate`, never `len(views)`.
    """

    should_generate: bool = False
    # AI views to render, as VNYX ProductMediaView names.
    views: list[str] = Field(default_factory=list)
    # Garment originals to matte FIRST. Generating from an un-matted photograph
    # seeds the model with a stockroom wall, so this happens before `views`.
    matte_first: list[SourceImage] = Field(default_factory=list)
    # Why, in words, for the log line and the operator. Always set.
    reason: str = ""


class ImageryVerdict(BaseModel):
    """Report-only answer to "are this product's pictures finished?"."""

    product_id: str
    correct: bool
    status: ReconcileStatus
    findings: list[Finding] = Field(default_factory=list)
    advisory_count: int = 0
    worst_severity: Severity | None = None

    ai_views: AiViewReport = Field(default_factory=AiViewReport)
    backgrounds: list[BackgroundCheck] = Field(default_factory=list)

    # Garment photographs that genuinely still need the segmenter, ready to act
    # on. The same set `IMG.010` reports, lifted out of the finding's `detail` so
    # a caller does not have to parse a message to find the work.
    #
    # Excludes the two look-alikes that are NOT work: a size chart mis-filed
    # under OTHER, and an original whose cut-out exists but was written under the
    # wrong view (IMG.022) — re-matting either costs a provider call and degrades
    # the picture.
    needs_background_removal: list[SourceImage] = Field(default_factory=list)

    # What to DO about all of the above. See GenerationPlan: this is the field a
    # caller acts on, and the rest of the verdict is the evidence behind it.
    generation_plan: GenerationPlan = Field(default_factory=GenerationPlan)

    # Can the missing renders actually be produced right now? False carries a
    # reason — footwear, the tenant setting, or no photograph to work from — and
    # the caller should offer no Generate button when it is.
    generatable: bool = True
    not_generatable_reason: str | None = None
    # The photographs generation would be seeded from, in the order it would use
    # them. Empty when nothing usable is on the product.
    source_images: list[SourceImage] = Field(default_factory=list)

    edit_url: str | None = None
    llm_calls: int = 0
    duration_ms: int = 0


class GeneratedView(BaseModel):
    """One render, handed back for the caller to store.

    Hermes holds no VNYX or R2 credentials and writes nothing: the bytes come back
    inline and vnyx-api persists them through its own upload + `addImages` path, so
    `position`, `isCurrent`, `derivedFromId` and the `Product.images` cache rebuild
    stay in the one file that owns those invariants.
    """

    # AI_FRONT | AI_BACK | AI_FRONT_34 | AI_BACK_34 | AI_CLOSEUP — the VNYX
    # `ProductMediaView` name, so the caller writes the row without a lookup table.
    view: str
    # base64, no data: prefix.
    image_base64: str | None = None
    mime_type: str = "image/jpeg"
    bytes: int = 0
    ok: bool = True
    # Which model actually produced it. Not always the configured primary: a
    # garment the primary refuses can still be rendered by a fallback, and the
    # two do not look identical, so an operator comparing renders needs to know.
    model: str | None = None
    # Why this one view failed, while others may have succeeded. Per-view rather
    # than per-request because a quota rejection on the close-up must not discard
    # four good renders.
    error: str | None = None


class ImageryGenerateResponse(BaseModel):
    product_id: str
    views: list[GeneratedView] = Field(default_factory=list)
    # Views asked for that produced nothing at all.
    failed: list[str] = Field(default_factory=list)
    # True when the back render was inferred from the front because no back
    # photograph exists. The caller should surface it: the rear of that garment is
    # the model's invention, not a photograph of the item being sold.
    back_inferred: bool = False
    model: str = ""
    llm_calls: int = 0
    duration_ms: int = 0
    notes: list[str] = Field(default_factory=list)
    # The personality traits these renders were produced with, in the shape
    # `Product.imageSettings` stores.
    #
    # Handed back so the caller can persist them: a later gap-fill has to put the
    # SAME face beside these images, and a freshly drawn personality would
    # describe a different one. This is the same record the regenerate worker
    # keeps for the same reason.
    image_settings: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Readability — is the text in this photograph legible?
#
# Unlike everything above, this judges ONE uploaded image and knows nothing about
# a product. It exists because the capture screen needs an answer at shutter press
# — keep this frame, or ask for another — and the operator needs to be told what
# to change.
# --------------------------------------------------------------------------- #

class LegibilityReason(str, Enum):
    """Why a frame was rejected. Ordered roughly by how actionable it is.

    A verdict carries a LIST of these: a photograph taken in a dark stockroom at
    arm's length is both underexposed and too far away, and reporting only the
    first would send the operator to fix the wrong thing.
    """

    # Nothing to read — settled before OCR runs.
    BLANK_FRAME = "blank_frame"
    # OCR ran and found no text at all.
    NO_TEXT_FOUND = "no_text_found"
    # Found text, but less than the caller said to expect.
    NOT_ENOUGH_TEXT = "not_enough_text"
    # Found enough text, but the recognizer is not confident about it.
    LOW_CONFIDENCE = "low_confidence"
    # Some lines read cleanly, too many others did not.
    PARTIALLY_LEGIBLE = "partially_legible"

    # Contributing causes. Never sufficient on their own — see the policy note on
    # why no pixel statistic is allowed to decide.
    OUT_OF_FOCUS = "out_of_focus"
    TOO_DARK = "too_dark"
    OVEREXPOSED = "overexposed"
    GLARE = "glare"
    WASHED_OUT = "washed_out"

    # The upload itself was the problem.
    DECODE_FAILED = "decode_failed"


class TextLine(BaseModel):
    """One line the recognizer returned, with the score that decided its fate."""

    text: str
    confidence: float
    # Whether this line counted toward the verdict, i.e. scored at or above
    # `line_score_min`. Weak lines are returned rather than dropped so a caller
    # tuning the thresholds can see what it is rejecting.
    kept: bool = True
    # Axis-aligned box at the WORKING resolution, as [x, y, width, height].
    # Working rather than original resolution because that is the space the
    # geometry was measured in, and rescaling it would imply a precision the
    # detector does not have.
    box: list[int] = Field(default_factory=list)


class FrameStats(BaseModel):
    """What the pixels say, independent of any text.

    Reported on every response, pass or fail. These are the numbers behind the
    explanation, and a caller calibrating its own capture UI needs them even when
    the answer was yes.
    """

    width: int = 0
    height: int = 0
    # What OCR actually saw, after the downscale.
    working_width: int = 0
    working_height: int = 0
    # Variance of the Laplacian — the standard focus measure. High is sharp.
    # Scale is arbitrary and content-dependent; compare it against other frames
    # of the same subject, never against an absolute.
    sharpness: float = 0.0
    # Mean luma, 0-255.
    brightness: float = 0.0
    # Luma standard deviation, 0-255.
    contrast: float = 0.0
    # Fraction of pixels at 250+ and at 8-, i.e. blown highlights and crushed
    # shadows.
    clipped_fraction: float = 0.0
    dark_fraction: float = 0.0


class LegibilityVerdict(BaseModel):
    """The answer the capture screen acts on.

    `readable` and `message` are the whole contract for a simple client: keep the
    frame, or show the message and ask for another. Everything else is evidence.
    """

    readable: bool
    # One sentence addressed to the person holding the phone, in the imperative
    # where there is something to do about it. Always set, including on success.
    message: str
    reasons: list[LegibilityReason] = Field(default_factory=list)

    # Mean and worst recognition score across the KEPT lines. Both 0.0 when
    # nothing was kept.
    confidence: float = 0.0
    min_confidence: float = 0.0
    # Kept lines and the characters in them — the coverage half of the decision.
    line_count: int = 0
    char_count: int = 0
    # Lines the detector found, including the weak ones. A frame with many
    # detections and few keeps is the signature of clutter or of degraded text.
    detected_count: int = 0

    # Median height of the KEPT lines, in working-resolution pixels. 0 when
    # nothing was kept.
    #
    # This is how "the label only has two lines" is told apart from "we missed
    # four of them", which the line count alone cannot do. Detected box height
    # bottoms out around 13px because of the detector's vertical padding, so text
    # comfortably above that was resolved properly and a low line count means the
    # frame genuinely holds little text — an adidas neck label reads "adidas" and
    # "S" and that is all there is.
    text_height_px: int = 0

    # What was read, newline-joined over the kept lines. Returned because a caller
    # that has gone to the trouble of uploading the frame usually wants the text
    # too, and a second round trip to get it would cost another 1.5s.
    text: str = ""
    lines: list[TextLine] = Field(default_factory=list)

    frame: FrameStats = Field(default_factory=FrameStats)

    # True when the blank-frame gate answered without running OCR. Surfaced
    # because it explains an unusually fast response, and because a caller seeing
    # it often should look at its capture pipeline rather than at Hermes.
    ocr_skipped: bool = False
    # Split out so a deployment can see whether it is missing the budget on
    # inference or on decoding a needlessly large upload.
    decode_ms: int = 0
    ocr_ms: int = 0
    duration_ms: int = 0


# `ProductSnapshot.imagery_settings` forward-references a class defined below it.
# ProductSnapshot has to stay where it is — it is the type every rule signature
# names — and ImagerySettings reads better next to the rest of the imagery models
# than wedged in above it, so the reference is resolved here instead.
ProductSnapshot.model_rebuild()
