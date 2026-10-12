"""Which cut-outs a product sends to Shopify, and which of them should not be there.

12 Oct 2026. Shopify listings showed more than the product's front/back cut-outs.
Two causes, both found by reading each image's LINEAGE — `derivedFromId` walked up
to the original photograph — never its file name, size or storage folder:

  DECISION   A cut-out made from a decision-portal photograph (KLE-000536, 3rd
             image), or that photograph itself. Internal only. The tenant's Image
             Sequence now has the decision slots switched off, so Product.images
             — what a push sends — no longer holds them; Shopify does until the
             product is pushed again. Nothing here deletes them.

  DUPLICATE  Two cut-outs of the same photograph, both live:
               * the OLD PIPELINE's cut-out left beside a re-cut (BOA-000314).
                 Products imported in July–August carry their cut-outs as OTHER
                 photos with no link to the photo they were cut from —
                 `<ms ts>-<n>-processed.png`, <n> the index of the original upload
                 (`<ms ts>-…-original…`), or `<ms ts>-bg-removed.png` — so a re-cut
                 superseded the photograph and nothing pointed at the old cut-out.
                 Decided PER PHOTO: `-N-processed` is a duplicate when the N-th
                 original upload has a live cut-out made from it; the unnumbered
                 kind only when EVERY original has. A product never re-cut keeps
                 them — they are still its only front/back pictures.
               * two live cut-outs made from the same photograph by the current
                 pipeline: the newest stays.

Rows are the dicts `ProductMedia` gives: id, url, view, origin, processing,
derivedFromId, deletedAt, createdAt. Pure — the scripts and the repair chain
load the rows and write.
"""
from __future__ import annotations

import re
from typing import Any

CUT = ("BG_REMOVED", "COMPOSITED")
GARMENT = ("FRONT", "BACK", "OTHER")

_LEGACY_INDEXED = re.compile(r"/\d{10,}-(\d+)-processed\.png$", re.I)
_LEGACY_PLAIN = re.compile(r"/\d{10,}-bg-removed\.png$", re.I)
_ORIGINAL = re.compile(r"^\d{10,}-.*original")
_ORIGINAL_INDEX = re.compile(r"^\d{10,}-(\d+)-original")
_ORIGINAL_TS = re.compile(r"^(\d{10,})-")


def base(url: str | None) -> str:
    return str(url or "").split("?")[0].rsplit("/", 1)[-1].lower()


def root_of(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The original photograph a row descends from (itself when it has no source)."""
    seen: set[str] = set()
    while row.get("derivedFromId") and row["derivedFromId"] in by_id and row["id"] not in seen:
        seen.add(row["id"])
        row = by_id[row["derivedFromId"]]
    return row


def is_decision(row: dict[str, Any], by_id: dict[str, dict[str, Any]]) -> bool:
    """By origin, by the origin of the photo it was cut from, or by the portal's own
    file name (`…inspection_front_0_….jpg`). Never by storage folder — early
    photobooth cut-outs were written under `inspection/` too (KLE-000888)."""
    root = root_of(row, by_id)
    return (row.get("origin") == "DECISION" or root.get("origin") == "DECISION"
            or "inspection_" in base(row.get("url")) or "inspection_" in base(root.get("url")))


def live(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Not removed by a person, and nothing derived from them — vnyx-api's
    LIVE_MEDIA_WHERE (a deleted child still supersedes its source)."""
    sources = {r["derivedFromId"] for r in rows if r.get("derivedFromId")}
    return [r for r in rows if not r.get("deletedAt") and r["id"] not in sources]


def is_cutout(row: dict[str, Any]) -> bool:
    return row.get("view") in GARMENT and row.get("processing") in CUT


def legacy_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The old pipeline's cut-outs that a re-cut has made redundant."""
    by_id = {r["id"]: r for r in rows}
    alive = live(rows)

    def order(r: dict[str, Any]) -> tuple[int, int]:
        name = base(r.get("url"))
        ix = _ORIGINAL_INDEX.match(name)
        if ix:
            return (0, int(ix.group(1)))
        ts = _ORIGINAL_TS.match(name)
        return (1, int(ts.group(1)) if ts else 0)

    # The old pipeline's own uploads only: a size chart or an operator upload
    # filed as OTHER has no place in its index (BOA-000279).
    originals = sorted(
        (r for r in rows if r.get("processing") == "RAW" and not r.get("deletedAt")
         and r.get("view") in GARMENT and not is_decision(r, by_id)
         and _ORIGINAL.match(base(r.get("url")))),
        key=order)
    cut_from = {root_of(r, by_id)["id"] for r in alive if is_cutout(r) and r.get("derivedFromId")}
    recut = [o["id"] in cut_from for o in originals]

    out = []
    for r in alive:
        if (r.get("view") != "OTHER" or r.get("processing") not in CUT or r.get("derivedFromId")
                or "/size-charts/" in str(r.get("url") or "") or is_decision(r, by_id)):
            continue
        url = str(r.get("url") or "")
        m = _LEGACY_INDEXED.search(url)
        if m:
            n = int(m.group(1))
            if n < len(recut) and recut[n]:
                out.append(r)
        elif _LEGACY_PLAIN.search(url) and recut and all(recut):
            out.append(r)
    return out


def same_photo_duplicates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live cut-outs made from the same photograph as a newer live one."""
    by_id = {r["id"]: r for r in rows}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in live(rows):
        if is_cutout(r) and r.get("derivedFromId") and not is_decision(r, by_id):
            groups.setdefault(root_of(r, by_id)["id"], []).append(r)
    out = []
    for group in groups.values():
        if len(group) > 1:
            group.sort(key=lambda r: str(r.get("createdAt") or ""))
            out.extend(group[:-1])
    return out


def listing_cutouts(rows: list[dict[str, Any]], sent: list[str]) -> list[dict[str, Any]]:
    """The cut-outs the listing shows, for the repair chain's AI look (12 Oct 2026).

    The live, non-decision, non-duplicate cut-outs in the gallery cache — plus a
    live FRONT/BACK cut-out of a view the cache does not cover yet: a re-cut made
    this run supersedes the row the cache still lists until the gallery is rebuilt.
    """
    by_id = {r["id"]: r for r in rows}
    gone = {r["id"] for r in [*legacy_duplicates(rows), *same_photo_duplicates(rows)]}
    keep = [r for r in live(rows) if is_cutout(r) and not is_decision(r, by_id)
            and r["id"] not in gone and "/size-charts/" not in str(r.get("url") or "")]
    in_cache = set(sent or [])
    shown = [r for r in keep if r.get("url") in in_cache]
    covered = {r.get("view") for r in shown}
    extra = [r for r in keep if r not in shown and r.get("view") in ("FRONT", "BACK")
             and r.get("view") not in covered]
    return [*shown, *extra]


def classify(rows: list[dict[str, Any]], sent: list[str]) -> dict[str, Any]:
    """One product: its decision images, its duplicates, and the cut-outs it sends.

    `sent` is Product.images — what a push sends now. The cut-outs it sends AFTER
    the clean-up are the live, non-decision cut-outs in it, minus the duplicates.
    """
    by_id = {r["id"]: r for r in rows}
    alive = live(rows)
    decision = [r for r in alive if r.get("view") in GARMENT
                and "/size-charts/" not in str(r.get("url") or "") and is_decision(r, by_id)]
    legacy = legacy_duplicates(rows)
    same = same_photo_duplicates(rows)
    dupes = [*legacy, *[r for r in same if r not in legacy]]
    gone = {r["id"] for r in dupes}
    in_cache = set(sent or [])
    keep = [r for r in alive if is_cutout(r) and not is_decision(r, by_id)
            and r["id"] not in gone and r.get("url") in in_cache]
    return {
        "decision": decision,
        "legacy_duplicates": legacy,
        "same_photo_duplicates": [r for r in same if r not in legacy],
        "duplicates": dupes,
        "sent_cutouts": keep,
        "decision_still_sent": [r for r in decision if r.get("url") in in_cache],
        "by_id": by_id,
    }
