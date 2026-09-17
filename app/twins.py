"""C-twins: the same garment listed twice, and what the copy may inherit.

A twin (`CBOA-000123`) is the SAME physical garment as its parent (`BOA-000123`),
listed a second time under the opposite gender so unisex stock is discoverable
in both departments. VNYX's duplication copies the images and not the
measurements, so twins arrive with blank brand / size and are rejected for it —
9 of 9 in the auditor's 2026-07-31 rehearsal. Measured against this week's
production runs it is a small number (5 of 337 rejections had a parent carrying
the missing fields), which is why this is a late item and a small module.

WHAT MAY BE COPIED. Gender-neutral facts only — brand, the size marking, waist
and inseam, material, fit, condition, colour. They describe the cloth, and the
cloth is the same. Everything gender-dependent — gender itself, master category,
mannequin, sizing guide, EU size, title, description — is the whole point of the
twin being a separate listing and is recomputed for the twin's own gender by the
normal chain. `policy.yaml: twins` carries both lists; `never_inherit` is
documentation the tests check this module against.

NEVER OVERWRITES. A field the twin already holds is left alone whatever the
parent says. This fills blanks; it does not reconcile disagreements.
"""
from __future__ import annotations

from typing import Any

from app.product_audit import PLACEHOLDERS, connect

# policy `inherit_properties` name -> (record key on the loaded product,
# plan field name apply_plan understands). `international_size` is planned as
# `size` because that is _PROPERTY_KEYS' name for it, and it mirrors into the
# `internationalSize` column on write.
#
# EVERY COPY OF A VALUE IS CHECKED, never a bare column. `record["size"]` is the
# `properties` copy (international_size / size through the alias chain);
# `record["internationalSize"]` is the column. A field is blank only when every
# copy is blank. Keyed on the column alone, a twin whose property already said
# 'L' under an 'Unknown' column was planned for inheritance — and the parent's
# size would have been written over the twin's own, against the rule this
# module exists to keep. Measured on CBOA-006107, 15 Sep 2026. Two copies that
# disagree are DRIFT.001's repair, not this module's.
_FIELDS: dict[str, tuple[tuple[str, ...], str]] = {
    "brand": (("brand",), "brand"),
    "international_size": (("size", "internationalSize"), "size"),
    "waist": (("waist",), "waist"),
    "length_size": (("lengthSize",), "length_size"),
    "material": (("material",), "material"),
    "fit": (("fit",), "fit"),
    "condition": (("condition",), "condition"),
    "color": (("color",), "color"),
}


def _cfg(pol: dict[str, Any]) -> dict[str, Any]:
    return pol.get("twins") or {}


def parent_sku(sku: str | None, pol: dict[str, Any]) -> str | None:
    """`CBOA-000123` -> `BOA-000123`; None when the SKU is not a twin."""
    s = (sku or "").strip().upper()
    for prefix in _cfg(pol).get("prefixes") or []:
        pre = str(prefix).strip().upper()
        if pre and s.startswith(pre) and len(s) > len(pre):
            return s[1:]
    return None


def _blank(value: Any) -> bool:
    return value is None or str(value).strip().lower() in PLACEHOLDERS


def _held(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    """The first non-blank copy of the field on this record, or None."""
    for key in keys:
        value = record.get(key)
        if not _blank(value):
            return value
    return None


def inheritance_plan(twin: dict[str, Any], parent: dict[str, Any],
                     pol: dict[str, Any]) -> list[dict[str, Any]]:
    """The set_property actions that fill the twin's blanks from the parent.

    Both arguments are `record` dicts as product_audit.load builds them, so the
    alias chain has already resolved `Brand` vs `brand` on each side. Returns
    the actions in policy order; empty when the parent has nothing the twin
    lacks.
    """
    parent_sku_ = str(parent.get("sku") or "?")
    out: list[dict[str, Any]] = []
    for name in _cfg(pol).get("inherit_properties") or []:
        spec = _FIELDS.get(str(name))
        if spec is None:
            continue
        record_keys, plan_field = spec
        if _held(twin, record_keys) is not None:
            continue                       # the twin has its own — never overwrite
        value = _held(parent, record_keys)
        if value is None:
            continue
        out.append({
            "kind": "set_property",
            "field": plan_field,
            "value": str(value).strip(),
            "reason": "TWIN",
            "detail": f"inherited from parent {parent_sku_}",
        })
    return out


def load_parent(dsn: str, sku: str, tenant_id: str) -> dict[str, Any] | None:
    """The parent's loaded record, or None when this tenant has no such SKU.

    Same tenant only — a SKU is unique per tenant, not globally, and inheriting
    across tenants would copy one merchant's data onto another's product.
    """
    from app import product_audit

    with connect(dsn, read_only=True) as conn, conn.cursor() as cur:
        cur.execute(
            'SELECT id::text FROM "Product" '
            'WHERE upper(sku) = %s AND "tenantId" = %s::uuid AND "isDeleted" = false '
            'ORDER BY "createdAt" LIMIT 1',
            (sku.upper(), tenant_id),
        )
        row = cur.fetchone()
    if not row:
        return None
    return product_audit.load(dsn, row[0])["record"]
