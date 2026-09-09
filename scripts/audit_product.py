#!/usr/bin/env python
"""One product id — every check, the repairs, and a sheet. From the terminal.

    # LOOK FIRST. Writes nothing, spends nothing.
    python scripts/audit_product.py 3fddf03b-09ef-4e77-a517-a83b36e72f5f

    # DO IT.
    python scripts/audit_product.py <uuid> --apply

    # Several, or a named sheet.
    python scripts/audit_product.py <uuid1> <uuid2> --apply
    python scripts/audit_product.py <uuid> --out jacket.xlsx

The connection string comes from DATABASE_URL, or `--db`.

This is the same code path as `POST /v1/product-audit` — it calls
`app.product_audit.audit` directly rather than over HTTP, so it needs no running
service. Every other script in this directory works that way and reaching for one
should not require a server and a token.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import product_audit  # noqa: E402
from app.config import settings  # noqa: E402

# ANSI, but only when stdout is a terminal — a redirect to a file should not
# collect escape codes.
_TTY = sys.stdout.isatty()

# A Windows console is cp1252 unless something has changed the code page, and
# `print("──")` on one raises UnicodeEncodeError rather than degrading. Ask for
# UTF-8 first, then pick the glyph set from what the stream can ACTUALLY encode —
# testing the encoder is the only reliable check, because the reconfigure can
# succeed and still leave a console that renders nothing.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):  # pragma: no cover - not a real stream
    pass


def _encodable(sample: str) -> bool:
    try:
        sample.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return False
    return True


_UNICODE_OK = _encodable("──→·✓")
RULE_CH = "─" if _UNICODE_OK else "-"
ARROW = "→" if _UNICODE_OK else "->"
DOT = "·" if _UNICODE_OK else "|"


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


RED, YELLOW, GREEN, DIM, BOLD = "31", "33", "32", "2", "1"
_SEV_COLOUR = {"critical": RED, "high": RED, "medium": YELLOW, "low": DIM}

# Which rule group a rule id belongs to, for the section headings. Grouped by the
# area a person thinks in rather than by rule prefix, so SIZE.* and DRIFT.001 on
# a size field land together.
_AREAS: list[tuple[str, tuple[str, ...]]] = [
    ("SIZING", ("SIZE.", "DRIFT.")),
    ("TAXONOMY", ("TAX.", "GENDER.")),
    ("IMAGES", ("IMG.",)),
    ("BRAND & ATTRIBUTES", ("ATTR.", "DATA.")),
    ("DESCRIPTION & COPY", ("TEXT.",)),
    ("PRICING", ("PRICE.",)),
    ("CONDITION", ("GRADE.",)),
    ("IDENTITY", ("ID.",)),
    ("CONFIDENCE", ("CONF.",)),
]


def area_of(rules: list[str]) -> str:
    for name, prefixes in _AREAS:
        if any(r.startswith(p) for r in rules for p in prefixes):
            return name
    return "OTHER"


def report(res: dict) -> None:
    print()
    print(paint(res["title"] or "(no title)", BOLD),
          paint(f'{DOT} {res["sku"] or "no sku"} {DOT} {res["stage"]}', DIM))
    print(paint(f'  {res["product_id"]}   tenant {res["tenant"]}', DIM))
    print()

    counts = res["counts"]
    if res["verified_after"]:
        headline = paint("READY", GREEN)
    else:
        headline = paint("NOT READY", RED)
    changed = counts["fixed"] or counts["would_fix"]
    verb = "fixed" if res["applied"] else "fixable"
    print(f'{headline}  {counts["blocking"]} blocking {DOT} '
          f'{counts["advisory"]} advisory {DOT} {changed} {verb} {DOT} '
          f'{counts["needs_a_human"]} needs a human')

    if res.get("message"):
        print(paint(f'  {res["message"]}', YELLOW))

    by_area: dict[str, list[dict]] = {}
    for issue in res["issues"]:
        by_area.setdefault(area_of(issue["rules"]), []).append(issue)

    for name, _ in _AREAS + [("OTHER", ())]:
        rows = by_area.get(name)
        if not rows:
            continue
        print()
        print(paint(f'{RULE_CH * 2} {name} '
                    + RULE_CH * max(0, 58 - len(name)), DIM))
        for issue in rows:
            colour = _SEV_COLOUR.get(issue["severity"], DIM)
            mark = "x" if issue["blocking"] else "!"
            ids = ", ".join(dict.fromkeys(issue["rules"]))
            print(f'{paint(mark, colour)} {paint(ids, BOLD):<28} '
                  f'{issue["severity"]:<8} {issue["field"]}')
            for message in dict.fromkeys(issue["messages"]):
                print(paint(f'    {message}', DIM))
            if issue.get("fix") is not None:
                print(f'    {paint(ARROW + " " + str(issue["fix"]), GREEN)}')

    if res["fixes"]:
        print()
        print(paint(RULE_CH * 2 + ' '
                    + ("APPLIED" if res["applied"] else "WOULD APPLY")
                    + ' ' + RULE_CH * 48, DIM))
        for a in res["fixes"]:
            print(f'  {a.get("field"):<22} {ARROW} '
                  f'{str(a.get("value"))[:44]:<46} '
                  f'{paint(a.get("reason", ""), DIM)}')
            print(paint(f'    {a.get("storage", "")}', DIM))

    if res["not_fixed"]:
        print()
        print(paint(RULE_CH * 2 + ' NEEDS A HUMAN ' + RULE_CH * 45, DIM))
        for a in res["not_fixed"]:
            print(f'  {paint(a.get("field") or "(product)", YELLOW):<24} '
                  f'{a.get("reason", "")}')
            print(paint(f'    {a.get("why", "")}', DIM))

    print()
    if res.get("sheet"):
        print(paint("sheet:", DIM), res["sheet"])
    if not res["applied"] and res["fixes"]:
        print(paint("Re-run with --apply to write the fixes.", DIM))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("product_ids", nargs="+", metavar="PRODUCT_ID")
    ap.add_argument("--apply", action="store_true",
                    help="write the repairs. Off by default.")
    ap.add_argument("--use-llm", action="store_true",
                    help="let the evidence layer spend model calls.")
    ap.add_argument("--db", "--dsn", dest="db",
                    help="connection string. Defaults to DATABASE_URL.")
    ap.add_argument("--out", help="sheet path (single product only).")
    ap.add_argument("--no-sheet", action="store_true")
    args = ap.parse_args()

    dsn = args.db or os.getenv("DATABASE_URL") or settings().database_url
    if not dsn:
        sys.exit("No DATABASE_URL, and no --db passed.")

    if args.out and len(args.product_ids) > 1:
        sys.exit("--out names one file; pass one product id with it.")

    failed = 0
    for product_id in args.product_ids:
        try:
            res = product_audit.audit(
                dsn, product_id,
                apply=args.apply,
                use_llm=args.use_llm,
                write_sheet=not args.no_sheet,
                sheet_path=args.out,
            )
        except product_audit.ProductNotFound:
            print(paint(f"no product {product_id}", RED))
            failed += 1
            continue
        report(res)

    # Non-zero when a product could not be read, so a shell loop notices. NOT
    # when a product merely has findings — "this product has problems" is the
    # normal, successful outcome of an audit and must not read as a crash.
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
