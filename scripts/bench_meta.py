"""Write a meta.json into every product folder of the render benchmark.

The folder NAME carries the gender ("jeans for women"), so gender is derived
rather than typed. Everything else comes from TAXONOMY below, which is the
realistic filing for each item.

TWO CATEGORIES PER PRODUCT, ON PURPOSE
--------------------------------------
`category`/`subCategory` is how a tenant would really file the item. Under that
filing vnyx-api and Hermes usually AGREE, because vnyx-api's substring test
matches the literal word "Accessories" or "Footwear" in the category.

`gapCategory`/`gapSubCategory` is the same item filed under a broad gendered
category, which is also how products really arrive. That filing is what exposes
vnyx-api's missing keywords — `purse`, `tote`, `mule` are in no list, so the item
falls through to the torso default. Running both filings is the difference
between "the two prompts look similar" and knowing exactly when they diverge.

`mannequinType` is set to the value the mobile and decision flows actually write
("Women Top" / "Men Top") for EVERY product regardless of what it is. That is not
a mistake in the fixture — it is the input that makes vnyx-api tell the model a
shoe is a top, and a benchmark that quietly used a correct mannequinType would
never reproduce the defect.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(r"C:\Users\Mahesh\Desktop\products test items")

# folder name -> (category, subCategory, gapCategory, gapSubCategory, group)
#
# group: A = the classifiers disagree, B = footwear, C = accessories,
#        D = apparel control.
TAXONOMY: dict[str, tuple[str, str, str, str, str]] = {
    "belt for men":            ("Accessories", "Belts",         "Men",   "Belts",         "C"),
    "boots for women":         ("Footwear",    "Boots",         "Women", "Boots",         "B"),
    "cap for men":             ("Accessories", "Caps",          "Men",   "Caps",          "C"),
    "capri for women":         ("Bottomwear",  "Capris",        "Women", "Capris",        "A"),
    "denim jackets for women": ("Outerwear",   "Denim Jackets", "Women", "Denim Jackets", "A"),
    "hat for women":           ("Accessories", "Hats",          "Women", "Hats",          "C"),
    "jeans for women":         ("Bottomwear",  "Jeans",         "Women", "Jeans",         "D"),
    "purse for women":         ("Accessories", "Purses",        "Women", "Purses",        "A"),
    "shoes for men":           ("Footwear",    "Shoes",         "Men",   "Shoes",         "B"),
    "slippers for women":      ("Footwear",    "Slippers",      "Women", "Slippers",      "B"),
    "sunglasses for women":    ("Accessories", "Sunglasses",    "Women", "Sunglasses",    "C"),
    "tshirt for men":          ("Topwear",     "T-Shirts",      "Men",   "T-Shirts",      "D"),
    "watch for men":           ("Accessories", "Watches",       "Men",   "Watches",       "C"),
}

# Folders whose files are not named front/back. Everything else is detected.
# `slippers` has no front shot at all — `top` is the nearest thing to one, and
# naming that here is better than letting the detector pick alphabetically and
# silently render a sole as if it were the upper.
OVERRIDES: dict[str, dict[str, str | None]] = {
    "cap for men":        {"front": "frontwebp.webp", "back": "bac.webp"},
    "slippers for women": {"front": "top.webp", "back": "back.webp"},
    "belt for men":       {"front": "front.webp", "back": "round.webp"},
}

IMAGE_EXTS = {".webp", ".jpg", ".jpeg", ".png"}


def gender_of(folder: str) -> str:
    """'female' | 'male', from the trailing "for women" / "for men"."""
    low = folder.lower()
    if re.search(r"\bfor\s+women\b", low):
        return "female"
    if re.search(r"\bfor\s+men\b", low):
        return "male"
    raise ValueError(f"no gender in folder name: {folder}")


def pick(files: list[str], *words: str) -> str | None:
    """First file whose stem contains any of `words`."""
    for word in words:
        for name in files:
            if word in Path(name).stem.lower():
                return name
    return None


def main() -> None:
    for folder in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        if folder.name.startswith("_"):
            continue  # output directories
        if folder.name not in TAXONOMY:
            print(f"SKIP {folder.name}: not in TAXONOMY")
            continue

        files = sorted(
            f.name for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS
        )
        over = OVERRIDES.get(folder.name, {})
        front = over.get("front") or pick(files, "front")
        back = over.get("back") or pick(files, "back")
        additional = [f for f in files if f not in (front, back)]

        if not front:
            print(f"WARN {folder.name}: no front image found in {files}")

        gender = gender_of(folder.name)
        category, sub, gap_cat, gap_sub, group = TAXONOMY[folder.name]

        meta = {
            "product": folder.name,
            "group": group,
            "gender": gender,
            "category": category,
            "subCategory": sub,
            # The same item filed the way that exposes vnyx-api's keyword gaps.
            "gapCategory": gap_cat,
            "gapSubCategory": gap_sub,
            # What the mobile / decision flow really writes — see the module note.
            "mannequinType": "Women Top" if gender == "female" else "Men Top",
            "bodyType": "m",
            "age": "27",
            "background": "bg_white",
            "images": {"front": front, "back": back, "additional": additional},
        }
        (folder / "meta.json").write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8"
        )
        print(
            f"{folder.name:26} {group}  {gender:6} {category}/{sub:14} "
            f"front={front} back={back}"
            + (f" +{len(additional)}" if additional else "")
        )


if __name__ == "__main__":
    main()
