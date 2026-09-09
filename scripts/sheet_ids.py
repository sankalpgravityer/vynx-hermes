"""Dump the Product ID column of a workbook to a plain id-per-line file.

The approve script takes `--ids-file` rather than reading xlsx itself: the
vnyx-api repo has no spreadsheet library, and adding one to a production
dependency list to read a one-column list would be the wrong trade.

Reads every sheet that has a "Product ID" column, finding the header row by
name because these workbooks carry a title banner and a prose preamble of
varying length above it. Duplicates across sheets are collapsed.
"""

import argparse
import sys
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet", required=True, help="Input .xlsx")
    ap.add_argument("--out", required=True, help="Output .txt, one id per line")
    ap.add_argument("--only-sheet", help="Restrict to one worksheet by name.")
    args = ap.parse_args()

    wb = openpyxl.load_workbook(args.sheet, read_only=True)
    ids: list[str] = []
    for ws in wb.worksheets:
        if args.only_sheet and ws.title != args.only_sheet:
            continue
        rows = list(ws.iter_rows(values_only=True))
        head = next(
            (i for i, r in enumerate(rows)
             if r and any(str(c).strip() == "Product ID"
                          for c in r if c is not None)),
            None,
        )
        if head is None:
            continue
        col = list(rows[head]).index("Product ID")
        found = [str(r[col]).strip() for r in rows[head + 1:] if r and r[col]]
        ids += found
        print(f"  {ws.title}: {len(found)} id(s)")
    wb.close()

    unique = list(dict.fromkeys(ids))
    dropped = len(ids) - len(unique)
    Path(args.out).write_text("\n".join(unique) + "\n", encoding="utf-8")
    print(f"\n{len(unique):,} id(s) -> {args.out}"
          + (f"  ({dropped} duplicate(s) collapsed)" if dropped else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
