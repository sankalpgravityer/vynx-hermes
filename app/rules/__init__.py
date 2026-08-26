"""Rule engine entry point."""

from __future__ import annotations

from typing import Any, Callable

from app.models import Finding, ProductSnapshot
from app.rules import catalog, consistency, imagery, pricing

RuleFn = Callable[[ProductSnapshot, dict[str, Any]], list[Finding]]

REGISTRY: dict[str, RuleFn] = {
    "pricing": pricing.check,
    "taxonomy": consistency.check_taxonomy,
    "sizing": consistency.check_sizing,
    "grading": consistency.check_grading,
    "identity": consistency.check_identity,
    "copy": consistency.check_copy,
    "completeness": consistency.check_completeness,
    # Brand/colour/material must exist in the tenant's own option lists — the
    # ones the edit screen's dropdowns are built from.
    "catalog": catalog.check_catalog,
    # On-model renders and background removal. Metadata only, so it costs nothing
    # to run here; the pixel and vision checks that can catch a row lying about
    # its own `processing` state are on /v1/imagery/verify instead. Silent unless
    # the caller sent typed media rows.
    "imagery": imagery.check_imagery,
}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def run_all(p: ProductSnapshot, pol: dict[str, Any],
            only: list[str] | None = None) -> list[Finding]:
    findings: list[Finding] = []
    for name, fn in REGISTRY.items():
        if only and name not in only:
            continue
        findings.extend(fn(p, pol))
    findings.sort(key=lambda f: SEVERITY_ORDER[f.severity.value])
    return findings
