"""Find products whose gender signals disagree, or whose sub-category is not
selectable in the tenant's own taxonomy. Read-only.

FIVE INDEPENDENT SIGNALS say what a product is. They are written by different
paths at different times and nothing keeps them in step:

  masterCategory      Men / Women / Kids on the product
  sizingGuide         "Men Uppers" carries a gender in its name
  mannequinType       "Women Top" / "Men Top" likewise
  properties.gender   what the extractor read
  subCategory         which branch of the category tree it belongs to

WHY THE DROPDOWN LOOKS EMPTY. Sub-categories live under a specific
master > category branch. A product filed Women > Jackets carrying the
sub-category "Outdoor Jacket" shows a BLANK dropdown when that name exists only
under Men > Jackets -- the value is stored, it is simply not offered for the
branch the product is in. Reported separately from a genuinely empty
sub-category, because the fix differs: one is a wrong master category, the
other is missing data.

Deliberately conservative on gender: only Men and Women are judged. Kids,
Unisex and unset master categories are never flagged, and a guide or mannequin
without a gender in its name ("Top", "Bottom", "Defaults") is treated as
carrying no opinion.

Usage:
  python scripts/audit_taxonomy_mismatch.py --sheet <xlsx>
  python scripts/audit_taxonomy_mismatch.py --sheet <xlsx> --out report.xlsx
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("openpyxl is required:  pip install openpyxl")
try:
    import psycopg
except ImportError:
    sys.exit("psycopg is required:  pip install psycopg[binary]")

DSN = "postgresql://postgres:Copenhagen%40event1@34.7.102.18:5432/vnyx-prod"
HDR_FILL = PatternFill("solid", fgColor="1F2937")
BAD = PatternFill("solid", fgColor="FDECEA")
WARN = PatternFill("solid", fgColor="FFF8E1")
OK = PatternFill("solid", fgColor="E8F5E9")
BLUE = PatternFill("solid", fgColor="D9E2F3")

PLACEHOLDER = {"", "unknown", "n/a", "na", "none", "null", "-", "tbd"}


def blank(v) -> bool:
    return v is None or str(v).strip().lower() in PLACEHOLDER


def gender_of(text) -> str | None:
    """'Men' | 'Women' | None. Women tested first: 'Women' contains 'men'."""
    s = str(text or "").lower().replace(" ", "")
    if not s:
        return None
    if "women" in s or "woman" in s:
        return "Women"
    if "men" in s or "man" in s:
        return "Men"
    return None


def gender_prop(raw) -> str | None:
    """properties.gender is sometimes a string, sometimes a JSON array."""
    if raw is None:
        return None
    v = raw
    if isinstance(v, str) and v.strip().startswith("["):
        try:
            v = json.loads(v)
        except Exception:
            pass
    if isinstance(v, list):
        v = v[0] if v else None
    return gender_of(v)


PRODUCT_SQL = """
SELECT p.id::text, p.title, t.name, p."tenantId"::text,
       p."masterCategory", p.category, p."subCategory",
       COALESCE(ps.name, p."sizingGuide"), p."mannequinType",
       p.properties->>'gender', p."internationalSize", p.properties->>'eu_size'
FROM "Product" p
LEFT JOIN "Tenant" t ON t.id = p."tenantId"
LEFT JOIN "ProductSize" ps ON ps.id::text = p."sizingGuideId"
WHERE p.id = ANY(%s::uuid[])
"""

# Every master > category > sub path the tenants actually offer.
TAXONOMY_SQL = """
WITH RECURSIVE tree AS (
  SELECT id, "tenantId", name, "parentId", name::text AS path, 1 AS depth
  FROM "Category"
  WHERE "parentId" IS NULL AND "isActive" AND "deletedAt" IS NULL
  UNION ALL
  SELECT ch.id, ch."tenantId", ch.name, ch."parentId",
         t.path || '>' || ch.name, t.depth + 1
  FROM "Category" ch
  JOIN tree t ON ch."parentId" = t.id
  WHERE ch."isActive" AND ch."deletedAt" IS NULL
)
SELECT "tenantId"::text, path, depth FROM tree WHERE depth <= 3
"""

ANNOTATION = [
    "Taxonomy verdict",
    "Gender check",
    "Sizing guide check",
    "Sub-category check",
]

COLUMNS = [
    "Product ID", "Title", "Tenant", "Master category", "Category",
    "Sub-category", "Sizing guide", "Mannequin", "Gender property",
    "Size", "EU size", "Problems", "Edit URL",
]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet", required=True)
    ap.add_argument("--out")
    ap.add_argument("--db", default=DSN)
    args = ap.parse_args()

    src = Path(args.sheet).expanduser()
    out = (Path(args.out).expanduser() if args.out
           else src.with_name(
               f"{src.stem}-checked-{datetime.now():%Y-%m-%d-%H%M}.xlsx"))

    wb_in = openpyxl.load_workbook(src, read_only=True)
    ids: list[str] = []
    for ws in wb_in.worksheets:
        rows = list(ws.iter_rows(values_only=True))
        h = next((i for i, r in enumerate(rows)
                  if r and any(str(c).strip() == "Product ID"
                               for c in r if c is not None)), None)
        if h is None:
            continue
        col = list(rows[h]).index("Product ID")
        ids += [str(r[col]).strip() for r in rows[h + 1:] if r and r[col]]
    wb_in.close()
    ids = list(dict.fromkeys(ids))
    print(f"Products : {len(ids):,}")

    with psycopg.connect(args.db, connect_timeout=40) as cn, cn.cursor() as cur:
        cur.execute(PRODUCT_SQL, (ids,))
        products = cur.fetchall()
        cur.execute(TAXONOMY_SQL)
        paths: dict[str, set] = {}
        for tid, path, depth in cur.fetchall():
            paths.setdefault(tid, set()).add(path.lower())
    print(f"Found    : {len(products):,}\n")

    from collections import Counter
    tally: Counter = Counter()
    flagged = []
    # One verdict per axis, keyed by product, so the source sheet can be
    # annotated column-by-column rather than with one opaque blob.
    verdicts: dict[str, dict] = {}

    for rec in products:
        (pid, title, tenant, tid, master, cat, sub, guide, mannequin,
         gprop, size, eu) = rec
        problems = []

        m = gender_of(master)
        g = gender_of(guide)
        q = gender_of(mannequin)
        pg = gender_prop(gprop)

        guide_check = "ok"
        if m and g and m != g:
            guide_check = f'MISMATCH — "{guide}" is {g}, product is {m}'
            problems.append(f'sizing guide "{guide}" is {g}, product is {m}')
        elif not g:
            guide_check = "not judged — guide carries no gender"

        gender_check = "ok"
        gender_bits = []
        if q and g and q != g:
            gender_bits.append(f'mannequin "{mannequin}" is {q}, guide is {g}')
        if m and q and m != q:
            gender_bits.append(f'mannequin "{mannequin}" is {q}, product is {m}')
        if m and pg and m != pg:
            gender_bits.append(f"gender property is {pg}, product is {m}")
        if gender_bits:
            gender_check = "MISMATCH — " + "; ".join(gender_bits)
            problems += gender_bits
        elif not m:
            gender_check = "not judged — no Men/Women master category"

        # Sub-category: empty, or present but not offered on this branch.
        sub_check = "ok"
        if blank(sub):
            sub_check = "EMPTY — no sub-category on the product"
            problems.append("sub-category is empty")
        elif master and cat:
            want = f"{master}>{cat}>{sub}".lower()
            known = paths.get(tid, set())
            if want not in known:
                flip = "Men" if m == "Women" else "Women"
                other = f"{flip}>{cat}>{sub}".lower()
                if other in known:
                    sub_check = (f'BLANK IN UI — "{sub}" exists only under '
                                 f"{flip} > {cat}")
                    problems.append(
                        f'sub-category "{sub}" exists only under the '
                        f"{flip} branch — the dropdown renders blank")
                else:
                    sub_check = (f'BLANK IN UI — "{sub}" is not under '
                                 f"{master} > {cat} in this tenant's taxonomy")
                    problems.append(
                        f'sub-category "{sub}" is not in the tenant taxonomy '
                        f'under {master} > {cat} — the dropdown renders blank')

        verdicts[pid] = {
            "gender": gender_check,
            "guide": guide_check,
            "sub": sub_check,
            "verdict": "OK" if not problems else "NEEDS REVIEW",
            "count": len(problems),
        }

        if not problems:
            tally["consistent"] += 1
            continue
        for p in problems:
            tally[p.split(" —")[0].split(" is ")[0].strip()[:46]] += 1
        flagged.append([
            pid, title, tenant, master, cat, sub, guide, mannequin,
            str(gprop), size, eu, "; ".join(problems),
            f"https://try.vnyx.ai/product/{pid}/edit?tenantId={tid}",
        ])

    print(f"  consistent : {tally['consistent']:,}")
    print(f"  FLAGGED    : {len(flagged):,}\n")
    print("--- by problem (a product can have several) ---")
    for k, n in tally.most_common():
        if k == "consistent":
            continue
        print(f"  {n:6,}  {k}")

    # ---- annotate the ORIGINAL sheet ---------------------------------------
    # A standalone report only lists the faults, which answers "what is wrong"
    # but not "is this one fine" for the 1,271 that are. The source sheet with
    # four extra columns answers both, and keeps every column already there.
    wb = openpyxl.load_workbook(src)
    for sheet in wb.worksheets:
        rows = list(sheet.iter_rows(values_only=True))
        head = next((i for i, r in enumerate(rows)
                     if r and any(str(c).strip() == "Product ID"
                                  for c in r if c is not None)), None)
        if head is None:
            continue
        hdr = [str(c) if c is not None else "" for c in rows[head]]
        id_col = hdr.index("Product ID") + 1
        start = max(i for i, c in enumerate(rows[head], start=1)
                    if c is not None) + 1

        for off, name in enumerate(ANNOTATION):
            c = sheet.cell(row=head + 1, column=start + off, value=name)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = HDR_FILL
            c.alignment = Alignment(vertical="center", wrap_text=True)
            sheet.column_dimensions[get_column_letter(start + off)].width = (
                14 if name == "Taxonomy verdict" else 54)

        for r in range(head + 2, sheet.max_row + 1):
            raw = sheet.cell(row=r, column=id_col).value
            if not raw:
                continue
            v = verdicts.get(str(raw).strip())
            if v is None:
                values = ["not found", "", "", ""]
                fill = WARN
            else:
                values = [v["verdict"], v["gender"], v["guide"], v["sub"]]
                fill = OK if v["verdict"] == "OK" else BAD
            for off, value in enumerate(values):
                cell = sheet.cell(row=r, column=start + off, value=value)
                cell.alignment = Alignment(vertical="top", wrap_text=False)
                cell.fill = fill

    ws = wb.create_sheet("Mismatches")
    ws.append(COLUMNS)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HDR_FILL
        c.alignment = Alignment(vertical="center", wrap_text=True)
    for row in sorted(flagged, key=lambda r: (r[2] or "", r[11])):
        ws.append(row)
        ws.cell(row=ws.max_row, column=12).fill = BAD
        ws.cell(row=ws.max_row, column=12).alignment = Alignment(wrap_text=True,
                                                                 vertical="top")
    for i, w in enumerate(
        [38, 42, 16, 14, 20, 20, 16, 14, 16, 8, 9, 80, 74], start=1
    ):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{ws.max_row}"

    sm = wb.create_sheet("Summary", 1)
    sm.append(["Gender and taxonomy consistency"])
    sm.cell(row=1, column=1).font = Font(bold=True, size=14)
    sm.append(["Generated", datetime.now().strftime("%Y-%m-%d %H:%M")])
    sm.append(["Products checked", len(products)])
    sm.append(["Consistent", tally["consistent"]])
    sm.append(["Flagged", len(flagged)])
    sm.append([])
    sm.append(["Problem", "Products"])
    for c in sm[sm.max_row]:
        c.font = Font(bold=True)
    for k, n in tally.most_common():
        if k == "consistent":
            continue
        sm.append([k, n])
        sm.cell(row=sm.max_row, column=1).fill = BAD
    sm.append([])
    sm.append(['A blank sub-category dropdown does NOT mean the field is '
               'empty. Sub-categories hang off a master > category branch, so '
               'a value that exists only under the other gender is stored but '
               'never offered, and the control renders blank.'])
    for n, w in ((1, 62), (2, 12)):
        sm.column_dimensions[get_column_letter(n)].width = w
    for row in sm.iter_rows():
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    tmp = out.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, out)
    print(f"\nwritten to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
