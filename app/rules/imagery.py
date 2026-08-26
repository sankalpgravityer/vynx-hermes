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

from app.models import (
    AiViewReport, Finding, MediaAsset, ProductSnapshot, Severity, SourceImage,
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
    advisory = [
        v for v in (_cfg(pol).get("all_views") or [])
        if v not in req and v not in present
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
    if is_footwear(p.category, p.subcategory, p.title):
        return (
            "footwear — every mannequin type frames the item as apparel worn on "
            "a torso, so the analyze pipeline skips it too"
        )
    if not garment_photos(p, pol):
        return "no garment photograph to generate from"
    return None


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
