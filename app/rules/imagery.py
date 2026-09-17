"""Are this product's pictures finished?

Two questions, both answered from the typed `ProductMedia` rows:

  1. Are the AI on-model renders there?
  2. Have the garment photographs been through the segmenter?

METADATA ONLY. Everything here is arithmetic over columns the caller already
sent, so it costs nothing and is safe to run on every product of a review-queue
page. The pixel and vision checks that can catch a row *lying* about its own
`processing` state live in `app/imaging/background.py` and run only on the
single-product endpoint.


WHY NOT `generationStatus`

Because it lies. On the measured tenant, 78 of the 89 products with no renders at
all are marked COMPLETE — the pipeline finished, wrote no images, and said it was
done, which is precisely why nobody had noticed. `Product.aiGeneratedImages` is no
better: it is one of the six legacy String[] columns ProductMedia replaced, and it
is empty both on products whose renders are missing and on some that have them.

So generation state is REPORTED (IMG.011, IMG.012) and never trusted. The rows
decide.


THE FALSE-POSITIVE TRAPS

Reporting a correct catalog as broken is the failure mode this rule set was
designed around, because the pricing validator already lived through it. Four
things in the real data will do exactly that if taken literally:

  * 404 products carry FIVE `AI_FRONT` rows and no other view — an older run
    filed every render under one view. They have their pictures; only the labels
    are wrong. IMG.021, and never a regeneration.
  * 904 size charts are filed under view `OTHER`, which `needsBackgroundRemoval`
    includes. Their URL gives them away.
  * The ¾ views postdate most of the catalog, so requiring them makes 955
    products defective for missing something they never had. Advisory instead.
  * Footwear and `isModelGenerationEnabled = false` mean generation was never
    going to run. A reason, not a violation.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from app.imaging import cutouts as cutout_checks
from app.models import (
    AiViewReport, Finding, GenerationPlan, MediaAsset, ProductSnapshot, Severity,
    SourceImage,
)

# Footwear never gets an on-model render: every MannequinType frames the item as
# apparel worn on the torso, so the analyze worker skips generation outright
# (analyze.worker.ts, `isFootwear`). Flagging the shoe catalog as "missing model
# images" would be a false positive on every one of them.
#
# WORD-BOUNDARY, ported verbatim from isFootwearCategory in
# vnyx-api/src/helpers/formatters.ts. The regex is the whole point: a naive
# `'boot' in text` classifies BOOTCUT JEANS as footwear, and substring matching is
# how a "High Top" sneaker came to look like a TOP garment.
_FOOTWEAR_WORDS = [
    "footwear", "shoe", "shoes", "sneaker", "sneakers", "trainer", "trainers",
    "boot", "boots", "sandal", "sandals", "heel", "heels", "loafer", "loafers",
    "pump", "pumps", "mule", "mules", "clog", "clogs", "espadrille",
    "espadrilles", "slipper", "slippers",
]
_FOOTWEAR_RE = re.compile(r"\b(" + "|".join(_FOOTWEAR_WORDS) + r")\b", re.IGNORECASE)


def is_footwear(*values: str | None) -> bool:
    return any(v and _FOOTWEAR_RE.search(v) for v in values)


def _cfg(pol: dict[str, Any]) -> dict[str, Any]:
    return pol.get("imagery") or {}


def is_size_chart(url: str, pol: dict[str, Any]) -> bool:
    """A size chart filed under the wrong view.

    904 of the 926 live OTHER+RAW rows on the measured tenant are size-chart
    images written with view='OTHER'. The size-guide uploader puts them under
    /size-charts/ while garment photography goes under /products/, so the URL is
    the only thing that still distinguishes them.
    """
    low = (url or "").lower()
    markers = _cfg(pol).get("size_chart_url_markers") or []
    return any(m in low for m in markers)


# --------------------------------------------------------------------------- #
# Reading the media rows
# --------------------------------------------------------------------------- #

def live_media(p: ProductSnapshot) -> list[MediaAsset]:
    """Assets actually on the product right now.

    `is_current` is the materialized "nothing derives from this row" flag: a RAW
    upload whose cut-out exists is SUPERSEDED, and counting it would report every
    successfully matted product as still needing work. `deleted_at` is a human
    having removed the asset — a different fact, same effect on the gallery.
    """
    return [m for m in p.media if m.live and m.media_type == "IMAGE"]


def garment_photos(p: ProductSnapshot, pol: dict[str, Any]) -> list[MediaAsset]:
    """The photographs generation is seeded from and matting applies to."""
    views = set(_cfg(pol).get("garment_views") or [])
    return [
        m for m in live_media(p)
        if m.view in views and not is_size_chart(m.url, pol)
    ]


def ai_renders(p: ProductSnapshot) -> list[MediaAsset]:
    return [m for m in live_media(p) if m.is_ai]


def unmatted(p: ProductSnapshot, pol: dict[str, Any]) -> list[MediaAsset]:
    """Garment photographs sitting at `processing == RAW`.

    Every other value means the segmenter has been through, and re-running costs
    a 15-20s provider call AND degrades the picture — matting an already-matted
    image erodes the silhouette further.

    NOTE this is the raw count, not the answer to "does this need the segmenter".
    See `orphan_cutouts`: on one tenant 89% of products have a cut-out that was
    filed under the wrong view, so their originals read RAW while the work is
    already done.
    """
    return [m for m in garment_photos(p, pol) if m.processing == "RAW"]


def orphan_cutouts(p: ProductSnapshot, pol: dict[str, Any]) -> list[MediaAsset]:
    """Background-removed images filed under OTHER rather than FRONT / BACK.

    The single largest false-positive source found in the data, and it is not
    close. On BOAS:

        FRONT   6,514 RAW  vs     35 BG_REMOVED
        BACK    6,236 RAW  vs     21 BG_REMOVED
        OTHER   2,221 RAW  vs 12,477 BG_REMOVED

    12,311 of those OTHER cut-outs are `-processed.png` files under /products/ —
    they are the FRONT and BACK cut-outs, written with the wrong view and no
    derivation edge back to the original (which is why the originals still read
    `isCurrent`). 5,889 of 6,585 products are in this state.

    Taking `processing == RAW` at face value therefore reports almost the entire
    catalog as needing background removal, and acting on it would re-mat twelve
    thousand already-matted images: a provider bill, and a worse picture at the
    end of it.

    NOT matched pairwise. There is no honest way to say WHICH original a given
    cut-out came from — the derivation edge is absent and the filenames share
    only the product folder — so this stays a count, and the finding it produces
    says "relabel", never "these specific two are the same photo".
    """
    return [
        m for m in garment_photos(p, pol)
        if m.view == "OTHER" and m.processing != "RAW"
    ]


def needs_matting_for_generation(
    p: ProductSnapshot, pol: dict[str, Any]
) -> list[MediaAsset]:
    """The originals a REPAIR should matte — only what generation seeds from.

    Narrower than `needs_segmenter` on purpose. That function answers "which
    garment photographs are un-matted?", and IMG.010 reports all of them because
    an un-matted extra angle is a real defect in the gallery.

    This answers the different question a repair asks: what is worth a provider
    call *before generating*. `source_images` seeds the model from FRONT and
    BACK, so matting the extra OTHER angles buys nothing for the render — on one
    product that was four segmenter calls to produce two useful cut-outs.

    Deliberately does NOT inherit `needs_segmenter`'s orphan-cutout exemption.
    That exemption answers "has the segmenter been over this product at all?",
    and for a catalog-quality report it is right — 5,889 products have their
    cut-outs mis-filed under OTHER, and re-matting all of them would be twelve
    thousand pointless provider calls.

    But generation asks something narrower: is there a clean cut-out OF THE
    FRONT and OF THE BACK to seed from? A cut-out filed under OTHER cannot
    answer that — there is no derivation edge and no way to tell which of them
    is the front — so `source_images` falls back to the RAW originals and the
    model gets a garment photographed on a stockroom wall. Observed on
    d29c474f: two OTHER cut-outs, exempt from matting, and both renders seeded
    from un-matted originals.

    So the test here is per-view and literal: a FRONT or BACK that is RAW and
    has no background-removed sibling OF ITS OWN VIEW is matted. One that
    already has one is left alone — use the existing cut-out, never pay to make
    a second.
    """
    views = set(_cfg(pol).get("matte_views") or ["FRONT", "BACK"])
    photos = [m for m in garment_photos(p, pol) if m.view in views]
    matted_views = {m.view for m in photos if m.processing != "RAW"}
    return [
        m for m in photos
        if m.processing == "RAW" and m.view not in matted_views
    ]


def cutout_pairs(
    p: ProductSnapshot, pol: dict[str, Any]
) -> list[tuple[str, MediaAsset, MediaAsset | None]]:
    """Each live FRONT/BACK cut-out with the original it was cut from.

    Readiness phase 3. `derivedFromId` names the original when the cut-out was
    written through `replaceWithDerived` (1,069 of the 2,341 approved cut-outs
    locally); otherwise the RAW of the same view AND ORIGIN stands in — the
    SUPERSEDED one first, since that is what a cut-out replaces, then a live
    RAW beside it. Same origin, because a WEB cut-out next to a PHOTOBOOTH
    and a DECISION original (BLM-000408) is three photographs, and comparing
    the shape of one with another would flag a defect that is not there. None
    when no original is on file — a C-twin carries only its parent's cut-outs —
    and then the backdrop can still be judged, the canvas cannot. Never pairs
    across views, and a cut-out filed under OTHER is not a FRONT (see
    `orphan_cutouts`).
    """
    views = set(_cfg(pol).get("matte_views") or ["FRONT", "BACK"])
    by_id = {m.id: m for m in p.media if m.id}
    cutouts = [m for m in garment_photos(p, pol)
               if m.view in views and m.processing != "RAW"]
    out: list[tuple[str, MediaAsset, MediaAsset | None]] = []
    for cut in cutouts:
        raw = by_id.get(cut.derived_from_id) if cut.derived_from_id else None
        if raw is not None and raw.processing != "RAW":
            # Derived from an earlier cut-out (a re-matte of a re-matte). The
            # loader carries only RAW archive rows, so fall back to the view.
            raw = None
        if raw is None:
            cands = [
                m for m in p.media
                if m.view == cut.view and m.processing == "RAW"
                and m.media_type == "IMAGE" and not m.deleted_at
                and not is_size_chart(m.url, pol)
                and (not cut.origin or not m.origin or m.origin == cut.origin)
            ]
            cands.sort(key=lambda m: (m.is_current, -m.position))
            raw = cands[0] if cands else None
        out.append((cut.view, cut, raw))
    return out


def needs_segmenter(p: ProductSnapshot, pol: dict[str, Any]) -> list[MediaAsset]:
    """Garment photographs whose background genuinely has not been removed.

    A RAW original is only outstanding work if there is no mis-filed cut-out that
    could be its counterpart. With at least as many orphan cut-outs as RAW
    originals, the segmenter has plainly run over this product and the defect is
    the labelling, not the imagery.
    """
    raw = unmatted(p, pol)
    return [] if len(orphan_cutouts(p, pol)) >= len(raw) else raw


def required_views(p: ProductSnapshot, pol: dict[str, Any]) -> list[str]:
    """The AI views whose absence is a defect for THIS product.

    The close-up joins the required set only if the tenant switched it on AND
    policy lists it — otherwise it stays advisory, because a tenant with
    `isCloseUpEnabled = false` is not missing anything by not having one.
    """
    cfg = _cfg(pol)
    req = list(cfg.get("required_views") or [])
    close_up = cfg.get("close_up_view")
    settings = p.imagery_settings
    if close_up and close_up in req and settings and not settings.is_close_up_enabled:
        req.remove(close_up)
    return req


def view_report(p: ProductSnapshot, pol: dict[str, Any]) -> AiViewReport:
    rows = ai_renders(p)
    present = sorted({m.view for m in rows})
    req = required_views(p, pol)
    cfg = _cfg(pol)

    # A tenant with `isCloseUpEnabled = false` does not want close-ups, and the
    # analyze worker honours that (`wantCloseUp` in banana-nano.ts) — so it is
    # absent by choice, not missing. `required_views` already drops it from the
    # required set for the same reason; leaving it in ADVISORY was harmless only
    # while advisory views were never generated. Now that a repair fills them in,
    # keeping it here would spend a call producing a view the tenant switched off
    # and that the worker would never have made.
    settings = p.imagery_settings
    close_up = cfg.get("close_up_view")
    wants_close_up = not settings or settings.is_close_up_enabled

    advisory = [
        v for v in (cfg.get("all_views") or [])
        if v not in req
        and v not in present
        and not (v == close_up and not wants_close_up)
    ]
    return AiViewReport(
        required=req,
        present=present,
        missing=[v for v in req if v not in present],
        advisory_missing=advisory,
        row_count=len(rows),
    )


def is_mislabelled(report: AiViewReport, pol: dict[str, Any]) -> bool:
    """Renders present, all filed under one view.

    404 products on the measured tenant have five `AI_FRONT` rows and nothing
    else. Asking "does AI_BACK exist?" marks every one of them incomplete, and
    regenerating them would spend ~2,000 Nano Banana calls reproducing pictures
    the product already has. The row count is what distinguishes this from a
    product that genuinely only ever got its front view.
    """
    expected = len(_cfg(pol).get("all_views") or []) or 5
    return report.row_count >= expected and len(report.present) == 1


def not_generatable_reason(p: ProductSnapshot, pol: dict[str, Any]) -> str | None:
    """Why generation cannot or should not run. None means it can.

    Established BEFORE any missing-imagery finding, because "no model images" is
    only a defect when model images were supposed to exist. Getting this backwards
    is what turns a verification pass into noise.
    """
    settings = p.imagery_settings
    if settings and not settings.is_model_generation_enabled:
        return "this tenant has model generation switched off"
    # FOOTWEAR IS NO LONGER REFUSED.
    #
    # It was, and the reason was sound at the time: every MannequinType frames
    # the item as apparel worn on the torso, so a pair of sandals rendered as a
    # full-body shot of an invented t-shirt and trousers with a few pixels of
    # product at the bottom edge. 124 such renders reached the catalogue before
    # the skip landed on 2026-08-20.
    #
    # `classify_garment_type` now returns a "footwear" class that reframes the
    # ¾ views to knee-to-floor and the close-up onto the shoes themselves, so
    # the thing that made the render useless is fixed rather than avoided.
    # Refusing here would keep the 28 live footwear products permanently
    # without imagery for a reason that no longer holds.
    #
    # `is_footwear` is kept and still exported: the sheet and the audit script
    # report which products took that framing, and analyze.worker.ts shares the
    # word list.
    if not garment_photos(p, pol):
        return "no garment photograph to generate from"
    return None


def generation_plan(
    p: ProductSnapshot,
    pol: dict[str, Any],
    include_advisory: bool = False,
) -> GenerationPlan:
    """Decide whether to generate, and what.

    THE decision, made here rather than by whoever called. It used to be derived
    by each caller from `ai_views.missing` and `needs_background_removal`, which
    meant the repair button and the post-generation sweep each carried their own
    copy of the same arithmetic — free to drift on the questions that are not
    obvious: does a mislabelled set count as missing (no), is matting alone worth
    a job (yes), does an advisory ¾ view justify spending a call (only if asked).

    Order matters. `not_generatable` is settled first, because "no model images"
    is only a defect when model images were supposed to exist — a footwear
    product is finished, not broken. Then mislabelling, because a product with
    five renders under one view HAS its pictures and needs a relabel, and
    regenerating it would spend real money reproducing what is already there.
    """
    reason = not_generatable_reason(p, pol)
    if reason is not None:
        return GenerationPlan(should_generate=False, reason=reason)

    report = view_report(p, pol)
    # Only FRONT/BACK — see needs_matting_for_generation. The wider un-matted
    # set is still reported by IMG.010; it is just not work this repair pays for.
    matte = [
        SourceImage(url=m.url, view=m.view, processing=m.processing)
        for m in needs_matting_for_generation(p, pol)
    ]

    if is_mislabelled(report, pol):
        # IMG.021. Renders exist; the `view` column is what is wrong.
        return GenerationPlan(
            should_generate=False,
            matte_first=matte,
            reason=(
                f"{report.row_count} renders already exist, all filed under "
                f"{report.present[0]} — this needs relabelling, not regenerating"
            ),
        )

    views = list(report.missing)
    if include_advisory:
        views += [v for v in report.advisory_missing if v not in views]

    if not views and not matte:
        return GenerationPlan(
            should_generate=False,
            reason=(
                f"every required view is present ({', '.join(report.present) or 'none required'}) "
                "and every garment original already has a cut-out"
            ),
        )

    # Matting alone is still work, and still worth a job: an original without a
    # cut-out is a visible defect in the gallery whether or not a render is also
    # missing. Hence `should_generate` is true here with an empty `views`, and
    # why callers must not test `len(views)` to decide.
    if not views:
        return GenerationPlan(
            should_generate=True,
            views=[],
            matte_first=matte,
            reason=(
                f"every required view is present, but {len(matte)} garment "
                f"original(s) still need background removal"
            ),
        )

    return GenerationPlan(
        should_generate=True,
        views=views,
        matte_first=matte,
        reason=(
            f"missing {', '.join(views)}"
            + (f"; {len(matte)} original(s) need matting first" if matte else "")
        ),
    )


def source_images(p: ProductSnapshot, pol: dict[str, Any]) -> list[SourceImage]:
    """Garment photos in the order generation would consume them.

    FRONT first, then BACK, then anything else — and within a view the matted
    derivative ahead of its raw original, since a cut-out is a cleaner seed. The
    TypeScript path recovers this by substring-matching filenames
    (`resolveImagesByView`); the typed `view` column makes that unnecessary.
    """
    order = {"FRONT": 0, "BACK": 1}

    def rank(m: MediaAsset) -> tuple[int, int, int]:
        return (order.get(m.view, 2), 0 if m.processing != "RAW" else 1, m.position)

    return [
        SourceImage(url=m.url, view=m.view, processing=m.processing)
        for m in sorted(garment_photos(p, pol), key=rank)
    ]


# --------------------------------------------------------------------------- #
# The gallery order — readiness phase 5 (docs/READINESS-PLAN.md §4 step 7)
# --------------------------------------------------------------------------- #
#
# `Product.images` is vnyx-api's display order, rebuilt from the rows by
# compareForDisplay: processed before raw; then the BAND — renders, garment
# photography, video, labels, size charts; inside the garment band the ORIGIN
# leads (decision 2: uploads, then the booth's front and back, then the portal's
# front and back), inside every other band the view leads; then position. This
# is that comparator, reproduced so IMG.025 can say whether a cached order is
# the catalog's. The fix is never a position write from Hermes: the cache is
# rebuilt by vnyx-api (`rebuild-media-cache.ts`, the chain's `order` step).

_GALLERY_VIEW_RANK: dict[str, int] = {
    "AI_FRONT_34": 0, "AI_BACK_34": 1, "AI_FRONT": 2, "AI_BACK": 3, "AI_CLOSEUP": 4,
    "FRONT": 10, "BACK": 11, "OTHER": 12, "VIDEO_TURNTABLE": 15, "LABEL": 20, "SIZE_CHART": 30,
}
_GALLERY_BAND: dict[str, int] = {
    "AI_FRONT_34": 0, "AI_BACK_34": 0, "AI_FRONT": 0, "AI_BACK": 0, "AI_CLOSEUP": 0,
    "FRONT": 1, "BACK": 1, "OTHER": 1, "VIDEO_TURNTABLE": 2, "LABEL": 3, "SIZE_CHART": 4,
}
_GARMENT_BAND = 1


def gallery_config(pol: dict[str, Any] | None) -> dict[str, Any]:
    from app import readiness

    return dict(readiness.config(pol).get("gallery") or {})


def gallery_enabled(pol: dict[str, Any] | None) -> bool:
    from app import readiness

    return readiness.enabled(pol) and bool(gallery_config(pol).get("enabled", True))


def gallery_key(m: MediaAsset, pol: dict[str, Any] | None) -> tuple[int, ...]:
    """vnyx-api's compareForDisplay, as a sort key."""
    origins = [str(o).upper() for o in (gallery_config(pol).get("origin_order") or [])]
    origin = str(m.origin or "").upper()
    origin_rank = origins.index(origin) if origin in origins else len(origins)
    band = _GALLERY_BAND.get(m.view, 5)
    view = _GALLERY_VIEW_RANK.get(m.view, 99)
    inner = (origin_rank, view) if band == _GARMENT_BAND else (view, origin_rank)
    return (0 if m.processing != "RAW" else 1, band, *inner, m.position)


def gallery_label(m: MediaAsset) -> str:
    return f"{m.view}/{m.origin or '?'}" + ("/raw" if m.processing == "RAW" else "")


def gallery_order(p: ProductSnapshot, pol: dict[str, Any] | None) -> list[MediaAsset]:
    """The live images in the catalog order."""
    return sorted(live_media(p), key=lambda m: gallery_key(m, pol))


def gallery_divergence(actual_urls: list[str],
                       expected: list[MediaAsset]) -> dict[str, Any] | None:
    """Where the cached order first departs from the catalog order, or None.

    Compared over the urls both sides know: a cache entry with no live row
    (legacy) and a row the cache has not caught up with are not an ORDER
    problem, and saying so would send the fix at the wrong thing.
    """
    by_url = {m.url: m for m in expected}
    seen: list[str] = []
    for u in actual_urls:
        if u in by_url and u not in seen:
            seen.append(u)
    known = set(seen)
    want = [m.url for m in expected if m.url in known]
    if seen == want:
        return None
    index = next((i for i, (a, b) in enumerate(zip(seen, want)) if a != b), 0)
    return {
        "index": index,
        "actual": gallery_label(by_url[seen[index]]),
        "expected": gallery_label(by_url[want[index]]),
        "actual_sequence": [gallery_label(by_url[u]) for u in seen],
        "expected_sequence": [gallery_label(by_url[u]) for u in want],
    }


def _stale(p: ProductSnapshot, pol: dict[str, Any]) -> bool:
    if not p.updated_at:
        return False
    try:
        seen = datetime.fromisoformat(str(p.updated_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    # Prisma maps DateTime to `timestamp without time zone`, so a serialised value
    # often arrives with no offset. It is stored in UTC; say so rather than letting
    # the subtraction raise on a mixed-awareness pair.
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    hours = (datetime.now(timezone.utc) - seen).total_seconds() / 3600
    return hours > float(_cfg(pol).get("stale_generation_hours") or 1)


# --------------------------------------------------------------------------- #
# The rule group
# --------------------------------------------------------------------------- #

def check_imagery(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """IMG.001-021 — on-model renders and background removal.

    Silent when no media rows were supplied. That is a legitimate state — a raw
    webhook payload and the hand-written fixtures both carry only the flat
    `images` cache — and reporting "no AI renders" for a product whose rows simply
    were not sent would be worse than saying nothing.
    """
    if not p.media:
        return []

    out: list[Finding] = []
    report = view_report(p, pol)
    photos = garment_photos(p, pol)
    raw = unmatted(p, pol)
    reason = not_generatable_reason(p, pol)

    # --- can it be generated at all? ------------------------------------------
    if reason and not photos:
        out.append(Finding(
            rule_id="IMG.003", severity=Severity.HIGH, fields=["images"],
            message=(
                "No garment photograph on this product, so no on-model image can "
                "be generated. This needs a photo, not a render."
            ),
            detail={"live_images": len(live_media(p))},
        ))
    elif reason:
        out.append(Finding(
            rule_id="IMG.020", severity=Severity.LOW, fields=["images"],
            message=f"On-model generation does not apply here: {reason}.",
            detail={"reason": reason},
        ))

    # --- the AI set ------------------------------------------------------------
    if is_mislabelled(report, pol):
        # Has its renders; they are filed wrong. Explicitly NOT a missing-imagery
        # finding — regenerating would pay again for pictures already on the row.
        out.append(Finding(
            rule_id="IMG.021", severity=Severity.LOW, fields=["images"],
            message=(
                f"{report.row_count} AI renders are all filed under "
                f"'{report.present[0]}'. The images exist — the view labels are "
                f"wrong. Relabel them; do not regenerate."
            ),
            detail={"row_count": report.row_count, "views": report.present},
        ))
    elif not reason:
        if report.row_count == 0:
            out.append(Finding(
                rule_id="IMG.001", severity=Severity.HIGH, fields=["images"],
                message=(
                    "No AI on-model images at all. "
                    + (f"The pipeline reports generationStatus="
                       f"{p.generation_status}, which is not evidence either way — "
                       f"the media rows are. " if p.generation_status else "")
                    + f"{len(photos)} garment photo(s) available to generate from."
                ),
                detail={
                    "generation_status": p.generation_status,
                    "source_count": len(photos),
                    "required": report.required,
                },
            ))
        elif report.missing:
            out.append(Finding(
                rule_id="IMG.002", severity=Severity.MEDIUM, fields=["images"],
                message=(
                    f"Missing {', '.join(report.missing)}. "
                    f"Present: {', '.join(report.present) or 'none'}."
                ),
                detail={"missing": report.missing, "present": report.present},
            ))

        if report.advisory_missing and report.row_count > 0:
            out.append(Finding(
                rule_id="IMG.005", severity=Severity.LOW, fields=["images"],
                message=(
                    f"{', '.join(report.advisory_missing)} not generated. "
                    f"Advisory only — these views postdate most of the catalog "
                    f"and are not required."
                ),
                detail={"advisory_missing": report.advisory_missing},
            ))

        # One usable photo means the rear of the garment is the model's invention
        # rather than a photograph of the item being sold. Worth knowing before it
        # ships, and the reason the generator says so in its prompt.
        if len(photos) == 1 and (report.missing or report.row_count == 0):
            out.append(Finding(
                rule_id="IMG.004", severity=Severity.LOW, fields=["images"],
                message=(
                    "Only one garment photograph, so any back view would be "
                    "inferred from the front rather than photographed."
                ),
                detail={"source": photos[0].url},
            ))

    # --- backgrounds -----------------------------------------------------------
    #
    # `raw` is the count of originals sitting at processing=RAW; `outstanding` is
    # how many of those actually still need the segmenter. They differ whenever a
    # product's cut-outs were filed under OTHER instead of replacing the original,
    # which on BOAS is 5,889 of 6,585 products. Reporting the first number as work
    # to do would ask for twelve thousand images to be re-matted that already are.
    orphans = orphan_cutouts(p, pol)
    outstanding = needs_segmenter(p, pol)

    if outstanding:
        views = ", ".join(sorted({m.view for m in outstanding}))
        out.append(Finding(
            rule_id="IMG.010", severity=Severity.HIGH, fields=["images"],
            message=(
                f"{len(outstanding)} garment image(s) still have their background "
                f"({views}). These are at processing=RAW and this product has no "
                f"cut-out that could be their counterpart."
            ),
            detail={"urls": [m.url for m in outstanding],
                    "views": sorted({m.view for m in outstanding})},
            # The pixel layer can only ever CONFIRM these; the interesting case it
            # adds is the opposite one, a row claiming BG_REMOVED that still shows
            # a room. Flagged so the single-product endpoint knows to look.
            needs_evidence=True,
        ))
    elif raw and orphans:
        out.append(Finding(
            rule_id="IMG.022", severity=Severity.LOW, fields=["images"],
            message=(
                f"{len(orphans)} background-removed cut-out(s) are filed under "
                f"view OTHER while the {len(raw)} original(s) they replace are "
                f"still the live FRONT/BACK. The segmenter has run — the view "
                f"labels are wrong. Relabel them; do NOT re-run background removal."
            ),
            detail={"raw": [m.url for m in raw],
                    "cutouts": [m.url for m in orphans]},
        ))

    # --- the cut-outs themselves (readiness phase 3) -------------------------
    #
    # IMG.026 canvas and IMG.027 backdrop, from evidence already ON the rows:
    # stored `width/height` for the canvas, and the border measurement
    # app/imaging/cutouts.judge writes back after looking at the file. Silent
    # without either — a rule set that runs on every review-queue page must
    # not fetch — so on the chain they fire after the matte step has measured,
    # and in the audit after the images have been measured. The severity
    # follows `readiness.cutouts.hold`: LOW (a flag) while soft, HIGH once the
    # shadow run has earned the right to block.
    if cutout_checks.enabled(pol):
        cut_cfg = cutout_checks.config(pol)
        hold_sev = Severity.HIGH if cut_cfg.get("hold") == "block" else Severity.LOW
        expected = cutout_checks.expected_backdrop(p.imagery_settings, cut_cfg)
        tolerance = float(cut_cfg.get("canvas_tolerance") or 0.02)
        for view, cut, raw in cutout_pairs(p, pol):
            # IMG.026: the shape. The aspect ratio against the original when
            # both are known, and — from the border measurement — the garment
            # cut to its bounding box. Either is the zoom the requirement
            # forbids; a same-ratio downscale of the whole frame is not.
            frame_problems: list[str] = []
            if cut.width and cut.height and raw is not None and raw.width and raw.height:
                why = cutout_checks.canvas_mismatch(
                    (cut.width, cut.height), (raw.width, raw.height), tolerance)
                if why:
                    frame_problems.append(why)
            if cut.border:
                why = cutout_checks.frame_mismatch(cut.border, cut_cfg)
                if why:
                    frame_problems.append(why)
            if frame_problems:
                out.append(Finding(
                    rule_id="IMG.026", severity=hold_sev, fields=["images"],
                    message=(
                        f"The {view} cut-out is not the photograph's frame: "
                        f"{'; '.join(frame_problems)}. Next to the original it reads as "
                        f"zoomed in. Re-matte from the original on the source canvas."
                    ),
                    detail={"view": view, "url": cut.url,
                            "original": raw.url if raw else None,
                            "canvas": [cut.width, cut.height],
                            "original_canvas": ([raw.width, raw.height]
                                                if raw and raw.width and raw.height else None),
                            "edges_touched": (cut.border or {}).get("edges_touched"),
                            "hold": cut_cfg.get("hold")},
                ))
            if cut.border:
                why = cutout_checks.background_mismatch(cut.border, expected, cut_cfg)
                if why:
                    out.append(Finding(
                        rule_id="IMG.027", severity=hold_sev, fields=["images"],
                        message=(
                            f"The {view} cut-out is not on the tenant's backdrop: {why}. "
                            f"Re-matte on {expected.describe()}."
                        ),
                        detail={"view": view, "url": cut.url, "border": cut.border,
                                "expected": expected.as_dict(),
                                "hold": cut_cfg.get("hold")},
                    ))

    # --- the gallery order (readiness phase 5) --------------------------------
    #
    # IMG.025: `Product.images` against the catalog order the rows imply. Silent
    # for a gallery a person arranged, when the cache is empty, and when the
    # rows were not supplied. LOW while `readiness.gallery.hold` is soft, HIGH
    # once it is block — the fix is a cache rebuild, and the chain runs it.
    g_cfg = gallery_config(pol) if gallery_enabled(pol) else {}
    respected = bool(p.media_manual_order and g_cfg.get("respect_manual", True))
    if p.images and gallery_enabled(pol) and not respected:
        divergence = gallery_divergence(p.images, gallery_order(p, pol))
        if divergence:
            out.append(Finding(
                rule_id="IMG.025",
                severity=Severity.HIGH if g_cfg.get("hold") == "block" else Severity.LOW,
                fields=["images"],
                message=(
                    f"The gallery is out of the catalog order: position "
                    f"{divergence['index'] + 1} shows {divergence['actual']} where "
                    f"{divergence['expected']} belongs. Now: "
                    f"{' > '.join(divergence['actual_sequence'])}. Rebuild the media cache."
                ),
                detail={**divergence, "hold": g_cfg.get("hold")},
            ))

    # --- the gallery's lead image --------------------------------------------
    #
    # `p.images` is vnyx-api's own display order (rebuildMediaCache sorts it:
    # processed before raw, then by view, then position), so images[0] IS what
    # the storefront leads with. Two things should never be first, and both are
    # only reachable through a human arrangement (mediaManualOrder) or a cache
    # the last relabel did not rebuild — which is exactly why they are worth a
    # rule rather than trust.
    #
    # Deliberately NOT a "real photo before render" rule: which of those leads
    # is vnyx-api's VIEW_RANK decision (renders first), and the auditor's
    # reversal of it (2026-08-14) is for the product owner to make, not a port.
    lead_row = next((m for m in live_media(p) if m.url == (p.images or [None])[0]), None)
    if lead_row is not None:
        if lead_row.view in ("LABEL", "SIZE_CHART") or is_size_chart(lead_row.url, pol):
            out.append(Finding(
                rule_id="IMG.024", severity=Severity.MEDIUM, fields=["images"],
                message=(f"The gallery leads with a {lead_row.view.lower().replace('_', ' ')}; "
                         f"a shopper's first picture is a tag, not the garment."),
                detail={"lead_view": lead_row.view, "lead_url": lead_row.url},
            ))
        elif (lead_row in photos and lead_row.processing == "RAW"
              and any(m.processing != "RAW" for m in photos)):
            out.append(Finding(
                rule_id="IMG.023", severity=Severity.LOW, fields=["images"],
                message=("The gallery leads with an unprocessed photograph while "
                         "a cut-out of the garment exists."),
                detail={"lead_view": lead_row.view, "lead_url": lead_row.url},
            ))

    # --- pipeline state --------------------------------------------------------
    if p.generation_status == "FAILED":
        out.append(Finding(
            rule_id="IMG.012", severity=Severity.MEDIUM, fields=["images"],
            message="The generation pipeline recorded FAILED for this product.",
            detail={"generation_status": p.generation_status},
        ))

    if _stale(p, pol) and (p.is_regenerating or p.generation_status == "GENERATING"):
        out.append(Finding(
            rule_id="IMG.011", severity=Severity.LOW, fields=["images"],
            message=(
                "Generation has been in flight since "
                f"{p.updated_at} with nothing to show for it. Recovery is "
                "vnyx-api's generation-reaper's job, not this service's — it has "
                "the queue state needed to tell a stranded product from a busy one."
            ),
            detail={"is_regenerating": p.is_regenerating,
                    "generation_status": p.generation_status,
                    "updated_at": p.updated_at},
        ))

    return out
