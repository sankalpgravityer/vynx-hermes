#!/usr/bin/env python
"""Reprice the products on the APPROVED tab, straight against Postgres.

    # 1. LOOK FIRST. 10 products, read-only, sheet only.
    python scripts/reprice_approved_db.py --dsn "postgresql://USER:PASS@HOST:5432/DB"

    # 2. Write those 10.
    python scripts/reprice_approved_db.py --dsn "..." --limit 10 --apply

    # 3. Every approved product.
    python scripts/reprice_approved_db.py --dsn "..." --all --apply

Every option of reprice_review_db.py works here unchanged -- --lookup-retail,
--tenant-id, --lookup-budget, --revert-from, all of it. Run with --help to see
them.


WHY THIS IS A WRAPPER AND NOT A COPY

It is the same job against a different `reviewStatus`, so it runs the same code
with `--stage approved` already applied. Copying the module would have doubled
roughly a thousand lines of pricing rules, SQL and API handling -- and the copy
would have started drifting from the original with the first fix applied to
either one. Two files, one implementation.

Approved is `reviewStatus = 'ACCEPTED'`. The UI labels that tab "Approved" while
its URL says `?tab=uploaded`; both names are accepted by --stage.


WHAT IS DIFFERENT ABOUT APPROVED STOCK -- READ THIS FIRST

These products have been through a human decision and, unlike Review-stage rows,
many are already published to sales channels. Two consequences:

  * A price change here can alter a LIVE listing. `ProductVariant.basePrice` is
    what the marketplace sync publishes, and this script updates it. Whether the
    channel picks that up depends on when the sync next runs -- so a repricing
    can appear on Etsy or eBay without anyone approving it a second time.

  * An approved price is more likely to be DELIBERATE. Somebody looked at this
    garment and accepted it. A grade-window correction that is obviously right
    for an unreviewed row may be overriding a human decision here.

Neither is a reason not to do it -- an approved product priced above its own
retail is still wrong -- but it is a reason to read the sheet before --apply
rather than after, and to keep the undo file it writes.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_TARGET = Path(__file__).resolve().parent / "reprice_review_db.py"

_SPEC = importlib.util.spec_from_file_location("reprice_review_db", _TARGET)
if _SPEC is None or _SPEC.loader is None:            # pragma: no cover
    sys.exit(f"Cannot load {_TARGET}")
_impl = importlib.util.module_from_spec(_SPEC)
# Registered before exec so the module's @dataclass declarations can resolve
# their own annotations.
sys.modules["reprice_review_db"] = _impl
_SPEC.loader.exec_module(_impl)


def main(argv: list[str] | None = None) -> int:
    """Run the shared implementation with --stage approved already set.

    Injected rather than defaulted so an explicit `--stage` on the command line
    still wins -- someone reaching for a different tab from this entry point
    should get the tab they asked for, not a silent override.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not any(a == "--stage" or a.startswith("--stage=") for a in args):
        args = ["--stage", "approved", *args]
    return _impl.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
