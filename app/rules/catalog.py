"""Attribute values must exist in the tenant's own option lists.

The review screen renders brand, colour and material as SELECTS, populated from
`/brands`, `/color-settings` and `/material-settings`. A stored value absent from
those lists is therefore a value no reviewer could have chosen — it came from the
AI extraction, or from an import, and it will not round-trip: open the edit page
and the dropdown shows empty, so saving anything else silently replaces it.

That makes this the most literal reading of "is the review data correct" there
is, and it is checkable only with the tenant's lists in hand — which is why the
feed ships them.

Deliberately MEDIUM, not high. The value is very often *right* and merely not yet
in the list ("Levi Strauss & Co." vs a `Levi's` entry), so this reads as "these
need reconciling", not "this product is broken". The fix is usually to add the
option, not to change the product — so nothing here is ever auto-repairable.
"""

from __future__ import annotations

from typing import Any

from app.models import Finding, ProductSnapshot, Severity


def _norm(value: str) -> str:
    """Compare on letters and digits only.

    Case, punctuation and spacing differ freely between an extracted value and a
    catalog entry — "Levi Strauss & Co." vs "levi strauss co", "T-Shirt" vs
    "tshirt". Matching strictly would report a difference in typography as a data
    error, which is noise, not a finding.
    """
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _check_one(
    value: str | None,
    options: list[str],
    field: str,
    rule_id: str,
    pol: dict[str, Any],
) -> list[Finding]:
    if not value or not options:
        return []
    # A placeholder is already reported by DATA.001; saying it twice adds nothing.
    if value.strip().lower() in pol["confidence"]["placeholders"]:
        return []

    if _norm(value) in {_norm(o) for o in options}:
        return []

    # Offer the nearest entries so a reviewer can see whether this is a genuine
    # mismatch or just a spelling the list has not absorbed yet. Substring both
    # ways — "Levi Strauss & Co." should surface a "Levi's" entry.
    needle = _norm(value)
    near = [
        o for o in options
        if needle and (needle in _norm(o) or _norm(o) in needle)
    ][:5]

    return [Finding(
        rule_id=rule_id, severity=Severity.MEDIUM, fields=[field],
        message=(
            f"{field.capitalize()} '{value}' is not in this tenant's "
            f"{field} list, so it cannot be selected on the edit screen"
            + (f". Closest entries: {near}." if near else
               f" ({len(options)} configured).")
        ),
        detail={"value": value, "near": near, "option_count": len(options)},
        needs_evidence=not near,
    )]


def check_catalog(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """ATTR.001-003 — brand / colour / material membership.

    No catalog supplied (a raw webhook payload, or a fixture) means there is
    nothing to check against, and this whole group is silent. It never falls back
    to a hardcoded list: unlike a size chart, there is no plausible default set of
    67 colours to guess at.
    """
    if not p.catalog:
        return []

    out: list[Finding] = []
    out.extend(_check_one(p.brand, p.catalog.brands, "brand", "ATTR.001", pol))
    out.extend(_check_one(p.color, p.catalog.colors, "color", "ATTR.002", pol))
    out.extend(
        _check_one(p.material, p.catalog.materials, "material", "ATTR.003", pol)
    )
    return out
