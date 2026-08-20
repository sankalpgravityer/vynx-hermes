"""VNYX read/write adapter.

The ONLY file that knows the shape of the upstream API. Everything else in
Hermes depends on ProductSnapshot alone, so this is the single file to touch when
the VNYX schema moves.

Two payload shapes are accepted, deliberately:

  1. The VERIFICATION FEED — `GET /review-verification/feed` on vnyx-api. Already
     flattened for this purpose: `properties` resolved to scalars, the tenant's
     grade row joined, the price expectation and variant-mirror drift computed.
     This is the shape the /v1/review-queue endpoint receives, and the one to
     prefer: the flattening lives next to the schema it depends on, so a renamed
     property key is fixed in one TypeScript file rather than here.

  2. A RAW product payload — `GET /products/{id}`, or the hand-written fixtures
     in samples/. Kept working because the tests and demo.py use it, and because
     a raw payload is what a webhook delivers.

`_first` is what lets one mapping serve both: each snapshot field lists its
candidate keys in precedence order. VNYX genuinely stores several of these under
more than one spelling — `eu_size` and `euSize` are both live in production (see
the fallback chains in services/openai.ts) — so single-key lookups silently read
None, which a rule engine cannot distinguish from a violation.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import settings
from app.models import Provenance, ProductSnapshot, TenantCatalog

log = logging.getLogger("hermes.vnyx")


def _f(value: Any) -> float | None:
    """Parse a money value, tolerating the currency symbols VNYX stores inline.

    Product.price is a String column holding both "38.99" and "€38.99" — two
    writers disagreed on the format and ~2,851 rows carry the symbol. Numbers
    pass through untouched so the feed's pre-parsed `priceAmount` costs nothing.
    """
    if value in (None, "", "-"):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = (
            str(value)
            .replace("€", "")
            .replace("$", "")
            .replace("£", "")
            .replace(",", "")
            .strip()
        )
        return float(cleaned)
    except ValueError:
        return None


def _pct(value: Any) -> float | None:
    """VNYX surfaces confidence as an integer percentage (e.g. 90)."""
    if value is None:
        return None
    try:
        v = float(value)
        return v / 100.0 if v > 1 else v
    except (TypeError, ValueError):
        return None


def _first(raw: dict[str, Any], *keys: str) -> Any:
    """First key present with a non-empty value. Order is precedence."""
    for key in keys:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            continue
        if isinstance(value, str) and value.strip() == "":
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def _str(value: Any) -> str | None:
    """Collapse to a scalar string.

    Lists are joined rather than dropped: `gender` is stored in VNYX as an ARRAY
    (`['men']` — see normalizeGenderToArray in services/openai.ts), and a rule
    comparing it to masterCategory 'Men' needs a string, not "['men']".
    """
    if value is None:
        return None
    if isinstance(value, list):
        parts = [_str(v) for v in value]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    if isinstance(value, dict):
        return None
    s = str(value).strip()
    return s or None


def _nested(raw: dict[str, Any], outer: str, inner: str) -> Any:
    """Read `raw[outer][inner]`, tolerating an absent or non-dict outer."""
    block = raw.get(outer)
    if isinstance(block, dict):
        return block.get(inner)
    return None


def to_snapshot(
    raw: dict[str, Any], catalog: dict[str, Any] | None = None
) -> ProductSnapshot:
    """Map a VNYX payload — feed record or raw product — onto the snapshot.

    `catalog` is this product's TENANT option lists (categories, sizing guides,
    colours, materials, brands). It arrives once per request rather than per
    product, so the caller resolves it by tenant id and passes it in. None means
    the rules fall back to policy.yaml where they can, and stay silent where they
    cannot.
    """

    # Confidence: the feed sends `propertyConfidence` (VNYX's own column name);
    # the fixtures send `confidence`.
    conf_in: dict[str, Any] = (
        _first(raw, "propertyConfidence", "confidence", "field_confidence") or {}
    )
    confidence = {
        k: v
        for k, v in ((k, _pct(v)) for k, v in conf_in.items())
        if v is not None
    }

    provenance: dict[str, Provenance] = {}
    for field, source in (raw.get("provenance") or {}).items():
        try:
            provenance[field] = Provenance(source)
        except ValueError:
            provenance[field] = Provenance.AI

    # Retail provenance comes from the audit trail VNYX already keeps in
    # Product.retailPriceBreakdown.source, surfaced by the feed as a plain word:
    #   'derived' -> GRADE_MULTIPLIER (retail was computed FROM price, so it is
    #                circular evidence for that price)
    #   'market'  -> EBAY_NEW comparables
    # Real data beats the guessed default below, so it is applied first.
    retail_prov = _str(raw.get("retailProvenance"))
    if retail_prov:
        try:
            provenance["retail_price"] = Provenance(retail_prov)
        except ValueError:
            pass

    # Defaults matching the VNYX UI labels, for payloads that carry no
    # provenance at all: "Powered by Google" price -> market comparable,
    # "Derived from grade multiplier" retail -> derived.
    provenance.setdefault("price", Provenance.MARKET)
    provenance.setdefault("retail_price", Provenance.DERIVED)

    # Grade: the feed puts the tenant's grade CODE at the top level; a raw
    # product payload nests it under qualityGrading.
    grade = _str(_first(raw, "grade")) or _str(_nested(raw, "qualityGrading", "grade"))

    defects_raw = (
        _first(raw, "defects")
        or _nested(raw, "qualityGrading", "defects")
        or []
    )
    defects = [
        d if isinstance(d, str) else (d.get("label", "") if isinstance(d, dict) else str(d))
        for d in defects_raw
    ]

    return ProductSnapshot(
        id=str(_first(raw, "id", "product_id") or ""),
        tenant_id=_str(_first(raw, "tenantId", "tenant_id")),
        edit_url=_str(_first(raw, "editUrl", "edit_url")),

        # identity
        sku=_str(raw.get("sku")),
        product_code=_str(_first(raw, "productCode", "product_code")),
        lpn_code=_str(_first(raw, "lpnCode", "lpn_code")),
        bin_code=_str(_first(raw, "binCode", "bin_code")),
        bin_number=_str(_first(raw, "binNumber", "bin_number")),
        bin_zone_code=_str(_first(raw, "binZoneCode", "bin_zone_code")),
        bin_warehouse_code=_str(_first(raw, "binWarehouseCode", "bin_warehouse_code")),
        # Only ever set from a field that really holds a second copy of the
        # LOCATION code. Deliberately NOT binNumber — see the note in models.py.
        bin_barcode=_str(
            _first(raw, "binBarcode", "bin_barcode") or _nested(raw, "barcodes", "bin")
        ),

        # taxonomy
        master_category=_str(_first(raw, "masterCategory", "master_category")),
        category=_str(raw.get("category")),
        subcategory=_str(_first(raw, "subCategory", "subcategory")),

        # copy — VNYX calls the long description `summary`
        title=_str(raw.get("title")),
        description=_str(_first(raw, "description", "summary")),

        # commercial. The feed pre-parses the money strings into *Amount fields;
        # prefer those so the two sides cannot parse the same string differently.
        currency=_str(raw.get("currency")),
        price=_f(_first(raw, "priceAmount", "price")),
        retail_price=_f(_first(raw, "retailPriceAmount", "retailPrice", "retail_price")),
        inventory=_first(raw, "inventoryQuantity", "inventory"),
        retail_provenance=retail_prov,

        # the tenant's own pricing config, joined by the feed
        price_factor=_f(
            _nested(raw, "priceExpectation", "priceFactor")
            or raw.get("priceFactor")
        ),
        expected_price=_f(
            _nested(raw, "priceExpectation", "expectedPrice")
            or raw.get("expectedPrice")
        ),

        # mirror drift, computed by the feed (needs both tables, so only the
        # backend can see it)
        variant_base_price=_f(_nested(raw, "variantPriceDrift", "variantBasePrice")),
        variant_price_drift=_f(_nested(raw, "variantPriceDrift", "drift")),

        # attributes
        gender=_str(raw.get("gender")),
        sizing_guide=_str(_first(raw, "sizingGuide", "productSizingGuide", "sizing_guide")),
        international_size=_str(_first(raw, "internationalSize", "international_size")),
        eu_size=_str(_first(raw, "euSize", "eu_size")),
        size=_str(raw.get("size")),
        waist=_str(raw.get("waist")),
        length_size=_str(_first(raw, "lengthSize", "length_size")),
        fit=_str(raw.get("fit")),
        brand=_str(_first(raw, "brand", "brandRelation")),
        model=_str(raw.get("model")),
        color=_str(_first(raw, "color", "colour")),
        material=_str(raw.get("material")),

        # condition
        condition=_str(raw.get("condition")),
        grade=grade,
        grade_label=_str(_first(raw, "gradeLabel", "grade_label")),
        defects=[d for d in defects if d],
        operator_defects=[
            d for d in (_first(raw, "operatorDefects", "operator_defects") or []) if d
        ],

        # ops
        mannequin=_str(_first(raw, "mannequinType", "mannequin")),
        hanger=_str(raw.get("hanger")),
        supplier=_str(raw.get("supplier")),

        images=[
            img if isinstance(img, str) else img.get("url", "")
            for img in (raw.get("images") or [])
        ],

        confidence=confidence,
        provenance=provenance,
        locked_fields=_first(raw, "lockedFields", "locked_fields") or [],
        # Accept it inline on the record too, so a single product can be posted
        # to /v1/validate with its own catalog attached.
        catalog=TenantCatalog(**(catalog or raw.get("catalog") or {}))
        if (catalog or raw.get("catalog"))
        else None,
    )


# Hermes field name -> VNYX payload key, for the write path.
_FIELD_MAP = {
    "price": "price",
    "retail_price": "retailPrice",
    "eu_size": "euSize",
    "length_size": "lengthSize",
    "international_size": "internationalSize",
    "master_category": "masterCategory",
    "sizing_guide": "sizingGuide",
    "mannequin": "mannequinType",
    "description": "summary",
    "subcategory": "subCategory",
}

# Snapshot fields that live inside the `properties` Json map rather than as
# columns, so they CANNOT be written field-by-field.
#
# PUT /products/:id takes `properties` as a whole record and REPLACES it
# wholesale (services/products.ts is explicit: "This function replaces
# `properties` wholesale, so applying the label to the stored copy would silently
# discard every other property edit"). Sending `{properties: {color: "Blue"}}`
# would therefore delete gender, size, waist, material, fit and everything else
# on the product.
#
# Writing one of these safely means read-merge-write against the current row,
# with no guard against a concurrent edit in between. That is a lost-update race
# on the single Json column holding most of a product's attributes — not
# something to run unattended. So these are REFUSED here and reported instead.
_PROPERTY_FIELDS = {
    "gender", "brand", "color", "material", "size", "eu_size", "waist",
    "length_size", "fit", "condition", "model", "supplier",
}


class VnyxClient:
    def __init__(self) -> None:
        s = settings()
        self.base = s.vnyx_base_url.rstrip("/")
        self.client = httpx.Client(
            timeout=s.vnyx_timeout_s,
            headers={"Authorization": f"Bearer {s.vnyx_token}",
                     "Content-Type": "application/json"},
        )

    def fetch_product(self, product_id: str, tenant_id: str | None = None) -> ProductSnapshot:
        """One product, via the real detail endpoint.

        `/products/{id}` — plural. The previous singular `/product/{id}` matched
        no route in vnyx-api and 404'd on every call.
        """
        params = {"tenantId": tenant_id} if tenant_id else None
        r = self.client.get(f"{self.base}/products/{product_id}", params=params)
        r.raise_for_status()
        return to_snapshot(r.json())

    def fetch_review_feed(
        self,
        tenant_id: str | None = None,
        status: str = "pending",
        skip: int = 0,
        take: int = 50,
    ) -> tuple[list[ProductSnapshot], int]:
        """A page of the review queue, pre-flattened for verification.

        One request for the whole page. Fetching the queue and then a detail call
        per product would be N+1 over the network for data the feed already
        joins — including the grade row and the variant mirror, which the detail
        endpoint does not expose at all.

        Returns (snapshots, total) so a caller can page without a second count.
        """
        params: dict[str, Any] = {"status": status, "skip": skip, "take": take}
        if tenant_id:
            params["tenantId"] = tenant_id
        r = self.client.get(f"{self.base}/review-verification/feed", params=params)
        r.raise_for_status()
        body = r.json()
        products = body.get("products") or []
        return [to_snapshot(p) for p in products], int(body.get("total") or 0)

    def patch_product(self, product_id: str, payload: dict[str, Any],
                      tenant_id: str | None = None) -> bool:
        """Write back a repair.

        `PUT`, not `PATCH`: vnyx-api exposes PUT /products/:id and no PATCH route,
        so the previous implementation could not have written anything. The route
        already treats a partial body as a partial update (productUpdateSchema is
        `productCreateSchema.partial()`), so PUT is safe to use this way.

        UNUSED on the review-queue path, which is report-only by design — a failed
        check produces `edit_url` for a human, not a write. It stays here for the
        /v1/reconcile flow, and stays correct so enabling that flow later is a
        config change rather than a debugging session.
        """
        body: dict[str, Any] = {}
        refused: list[str] = []
        for key, value in payload.items():
            if key in _PROPERTY_FIELDS:
                refused.append(key)
                continue
            body[_FIELD_MAP.get(key, key)] = value

        if refused:
            # Loud, not silent: the caller believes it repaired these fields, and
            # an unreported no-op would leave the audit log claiming a write that
            # never happened.
            log.warning(
                "refusing to write %s on %s — these live in the `properties` Json "
                "map, which PUT /products/:id replaces wholesale; a partial write "
                "would delete every other attribute. Escalate to a human instead.",
                ", ".join(sorted(refused)), product_id,
            )

        if not body:
            return False

        params = {"tenantId": tenant_id} if tenant_id else None
        try:
            r = self.client.put(f"{self.base}/products/{product_id}",
                                json=body, params=params)
            r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.error("write-back failed for %s: %s", product_id, exc)
            return False

    def close(self) -> None:
        self.client.close()
