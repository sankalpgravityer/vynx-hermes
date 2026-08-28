"""Imagery rules — one fixture per finding, plus the four false-positive traps.

The traps are the point of this file. Every one of them was found in the real
dev-tenant data, and each would report a large slice of a CORRECT catalog as
broken:

    404 products  five AI_FRONT rows and no other view   -> IMG.021, not IMG.001
    904 rows      size charts filed under view OTHER     -> not IMG.010
    955 products  no ¾ views (they postdate the catalog) -> IMG.005, not IMG.002
    the shoe rack  footwear never gets an on-model shot  -> IMG.020, not IMG.001

A rule set that gets the findings right and the traps wrong is worse than no
rule set, because a reviewer stops reading it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import policy
from app.models import ImagerySettings, MediaAsset, ProductSnapshot
from app.rules.imagery import (
    check_imagery, garment_photos, generation_plan, is_footwear, is_mislabelled,
    is_size_chart, needs_matting_for_generation, needs_segmenter,
    not_generatable_reason, orphan_cutouts, source_images, unmatted, view_report,
)


@pytest.fixture(scope="module")
def pol() -> dict:
    return policy()


def media(view: str, processing: str = "RAW", *, current: bool = True,
          url: str | None = None, deleted: str | None = None,
          media_type: str = "IMAGE", position: int = 0) -> MediaAsset:
    return MediaAsset(
        url=url or f"https://r2.dev/products/{view.lower()}-{position}.jpg",
        view=view, processing=processing, is_current=current,
        deleted_at=deleted, media_type=media_type, position=position,
    )


def product(**kw) -> ProductSnapshot:
    base = dict(
        id="p1", title="Vintage Champion Red Tank Top Men S",
        category="T-Shirts & Polos", subcategory="Tank Tops",
        master_category="Men", generation_status="COMPLETE",
        imagery_settings=ImagerySettings(),
    )
    base.update(kw)
    return ProductSnapshot(**base)


def ids(p: ProductSnapshot, pol: dict) -> list[str]:
    return [f.rule_id for f in check_imagery(p, pol)]


FULL_SET = [media(v, "GENERATED") for v in
            ("AI_FRONT", "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP")]
MATTED = [media("FRONT", "BG_REMOVED"), media("BACK", "BG_REMOVED", position=1)]


# --------------------------------------------------------------------------- #
# The four traps
# --------------------------------------------------------------------------- #

def test_five_rows_under_one_view_is_mislabelled_not_missing(pol):
    """404 real products look like this. Regenerating them would pay twice for
    pictures they already have."""
    p = product(media=[media("AI_FRONT", "GENERATED", url=f"https://r2.dev/g{i}.jpg",
                             position=i) for i in range(5)] + MATTED)
    found = ids(p, pol)
    assert "IMG.021" in found
    assert "IMG.001" not in found and "IMG.002" not in found


def test_two_rows_under_one_view_is_genuinely_incomplete(pol):
    """The mislabel test must not swallow a product that really only got its
    front — the row COUNT is what separates the two."""
    p = product(media=[media("AI_FRONT", "GENERATED", url=f"https://r2.dev/g{i}.jpg",
                             position=i) for i in range(2)] + MATTED)
    found = ids(p, pol)
    assert "IMG.002" in found
    assert "IMG.021" not in found


def test_size_chart_under_other_view_is_not_unmatted_garment(pol):
    """904 real rows. `needsBackgroundRemoval` includes OTHER, so only the URL
    tells a mis-filed size chart from an extra angle."""
    p = product(media=FULL_SET + MATTED + [
        media("OTHER", "RAW", url="https://r2.dev/size-charts/x.webp", position=9),
    ])
    assert "IMG.010" not in ids(p, pol)


def test_extra_angle_under_other_view_is_still_checked(pol):
    """The URL exclusion must not become a blanket exemption for view OTHER."""
    p = product(media=FULL_SET + MATTED + [
        media("OTHER", "RAW", url="https://r2.dev/products/side.jpg", position=9),
    ])
    assert "IMG.010" in ids(p, pol)


def test_cutouts_filed_under_other_are_a_labelling_defect_not_missing_work(pol):
    """The largest false positive in the data by a wide margin.

    On BOAS: FRONT 6,514 RAW vs 35 BG_REMOVED, BACK 6,236 vs 21, and OTHER
    12,477 BG_REMOVED — the cut-outs exist, filed under the wrong view, on 5,889
    of 6,585 products. Reading `processing == RAW` literally asks for twelve
    thousand already-matted images to be matted again: a provider bill, and a
    worse picture at the end of it.
    """
    p = product(media=FULL_SET + [
        media("FRONT", "RAW"),
        media("BACK", "RAW", position=1),
        media("OTHER", "BG_REMOVED", position=2,
              url="https://r2.dev/products/1-0-processed.png"),
        media("OTHER", "BG_REMOVED", position=3,
              url="https://r2.dev/products/1-1-processed.png"),
    ])
    found = ids(p, pol)
    assert "IMG.022" in found
    assert "IMG.010" not in found
    assert needs_segmenter(p, pol) == []


def test_a_genuinely_unmatted_photo_is_still_reported(pol):
    """The counterpart to the test above: with no orphan cut-out to account for
    it, a RAW original really is outstanding work."""
    p = product(media=FULL_SET + [
        media("FRONT", "BG_REMOVED"), media("BACK", "RAW", position=1),
    ])
    found = ids(p, pol)
    assert "IMG.010" in found
    assert "IMG.022" not in found
    assert [m.view for m in needs_segmenter(p, pol)] == ["BACK"]


def test_fewer_cutouts_than_raw_originals_still_reports_work(pol):
    """Two raw photos and one stray cut-out does not account for both."""
    p = product(media=FULL_SET + [
        media("FRONT", "RAW"),
        media("BACK", "RAW", position=1),
        media("OTHER", "BG_REMOVED", position=2,
              url="https://r2.dev/products/1-0-processed.png"),
    ])
    assert "IMG.010" in ids(p, pol)


def test_three_quarter_views_are_advisory_not_defects(pol):
    """955 real products. Requiring these turns two-thirds of the queue red."""
    p = product(media=[media("AI_FRONT", "GENERATED"),
                       media("AI_BACK", "GENERATED", position=1)] + MATTED)
    found = ids(p, pol)
    assert "IMG.005" in found
    assert "IMG.002" not in found


def test_footwear_is_a_reason_not_a_violation(pol):
    p = product(category="Shoes", subcategory="Sneakers", media=MATTED)
    found = ids(p, pol)
    assert "IMG.020" in found
    assert "IMG.001" not in found


def test_bootcut_jeans_are_not_footwear(pol):
    """The word-boundary regex earns its keep here: `'boot' in text` would
    classify these as shoes and skip generation for every pair."""
    assert not is_footwear("Bottoms", "Bootcut Jeans")
    p = product(category="Bottoms", subcategory="Bootcut Jeans", media=MATTED)
    assert "IMG.001" in ids(p, pol)


# --------------------------------------------------------------------------- #
# The findings
# --------------------------------------------------------------------------- #

def test_no_renders_at_all(pol):
    p = product(media=MATTED)
    assert "IMG.001" in ids(p, pol)


def test_generation_status_complete_does_not_suppress_the_finding(pol):
    """78 of the 89 affected products claim COMPLETE. If that silenced the rule
    the feature would report nothing at all."""
    p = product(generation_status="COMPLETE", media=MATTED)
    assert "IMG.001" in ids(p, pol)


def test_no_garment_photo_cannot_generate(pol):
    p = product(media=[media("LABEL"), media("SIZE_CHART", position=1)])
    found = ids(p, pol)
    assert "IMG.003" in found
    assert "IMG.001" not in found


def test_single_source_flags_an_inferred_back(pol):
    p = product(media=[media("FRONT", "BG_REMOVED"), media("LABEL", position=1)])
    found = ids(p, pol)
    assert "IMG.001" in found and "IMG.004" in found


def test_unmatted_garment_photo(pol):
    p = product(media=FULL_SET + [media("FRONT", "BG_REMOVED"),
                                  media("BACK", "RAW", position=1)])
    assert "IMG.010" in ids(p, pol)


def test_care_label_and_size_chart_are_never_matted(pol):
    """A wash-tag macro is all fabric — the segmenter has no foreground and
    mangles it. Same exclusion vnyx-api's needsBackgroundRemoval makes."""
    p = product(media=FULL_SET + MATTED + [
        media("LABEL", "RAW", position=8), media("SIZE_CHART", "RAW", position=9),
    ])
    assert "IMG.010" not in ids(p, pol)


def test_generation_failed_is_reported(pol):
    p = product(generation_status="FAILED", media=FULL_SET + MATTED)
    assert "IMG.012" in ids(p, pol)


def test_stale_generation_is_reported(pol):
    old = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    p = product(generation_status="GENERATING", is_regenerating=True,
                updated_at=old, media=FULL_SET + MATTED)
    assert "IMG.011" in ids(p, pol)


def test_a_generation_that_just_started_is_not_stale(pol):
    fresh = datetime.now(timezone.utc).isoformat()
    p = product(generation_status="GENERATING", is_regenerating=True,
                updated_at=fresh, media=FULL_SET + MATTED)
    assert "IMG.011" not in ids(p, pol)


def test_naive_timestamp_is_treated_as_utc(pol):
    """Prisma stores DateTime without a timezone, so a serialised value often
    arrives with no offset. Comparing it raw raises."""
    naive = (datetime.now(timezone.utc) - timedelta(hours=6)).replace(
        tzinfo=None).isoformat()
    p = product(generation_status="GENERATING", is_regenerating=True,
                updated_at=naive, media=FULL_SET + MATTED)
    assert "IMG.011" in ids(p, pol)


def test_complete_product_is_clean(pol):
    p = product(media=FULL_SET + MATTED)
    assert ids(p, pol) == []


def test_tenant_with_generation_disabled(pol):
    p = product(media=MATTED,
                imagery_settings=ImagerySettings(isModelGenerationEnabled=False))
    found = ids(p, pol)
    assert "IMG.020" in found
    assert "IMG.001" not in found


def test_close_up_not_required_when_tenant_disabled_it(pol):
    p = product(media=FULL_SET[:4] + MATTED,
                imagery_settings=ImagerySettings(isCloseUpEnabled=False))
    report = view_report(p, pol)
    assert "AI_CLOSEUP" not in report.required


def test_silent_without_media_rows(pol):
    """A raw webhook payload and the older fixtures carry only the flat `images`
    cache. Reporting "no renders" for rows that were never sent is worse than
    saying nothing."""
    assert check_imagery(product(media=[]), pol) == []


# --------------------------------------------------------------------------- #
# Liveness, ordering and helpers
# --------------------------------------------------------------------------- #

def test_superseded_raw_original_is_not_reported_as_unmatted(pol):
    """The whole point of `isCurrent`: a RAW upload whose cut-out exists is
    archived, not outstanding. Counting it reports every successfully matted
    product as still needing work."""
    p = product(media=FULL_SET + [
        media("FRONT", "BG_REMOVED"),
        media("FRONT", "RAW", current=False, url="https://r2.dev/products/orig.jpg"),
        media("BACK", "BG_REMOVED", position=1),
    ])
    assert unmatted(p, pol) == []
    assert "IMG.010" not in ids(p, pol)


def test_deleted_media_is_ignored(pol):
    p = product(media=FULL_SET + MATTED + [
        media("BACK", "RAW", deleted="2026-01-01T00:00:00Z", position=7),
    ])
    assert "IMG.010" not in ids(p, pol)


def test_video_is_never_treated_as_an_unmatted_photo(pol):
    p = product(media=FULL_SET + MATTED + [
        media("VIDEO_TURNTABLE", "RAW", media_type="VIDEO", position=9),
    ])
    assert "IMG.010" not in ids(p, pol)


def test_sources_prefer_the_cut_out_and_put_front_first(pol):
    p = product(media=[
        media("OTHER", "RAW", url="https://r2.dev/products/side.jpg", position=5),
        media("BACK", "RAW", position=1),
        media("FRONT", "BG_REMOVED", position=0),
    ])
    got = [(s.view, s.processing) for s in source_images(p, pol)]
    assert got[0] == ("FRONT", "BG_REMOVED")
    assert got[1] == ("BACK", "RAW")


def test_size_chart_url_detection(pol):
    assert is_size_chart("https://r2.dev/size-charts/a.webp", pol)
    assert not is_size_chart("https://r2.dev/products/1-back-original.jpg", pol)


def test_the_real_product_shape(pol):
    """2b40ac19 on the dev tenant, exactly as the rows read: the front was
    matted, the back original was left RAW, and no render was ever written —
    while generationStatus says COMPLETE."""
    p = product(
        id="2b40ac19-4dfe-4a6b-b0ee-916e8732f33d",
        title="Vintage adidas Black Tank Top Men S",
        media=[
            media("FRONT", "BG_REMOVED", url="https://r2.dev/products/1-0-processed.png"),
            media("FRONT", "RAW", current=False,
                  url="https://r2.dev/products/1-front-original.jpg"),
            media("BACK", "RAW", url="https://r2.dev/products/1-back-original.jpg",
                  position=1),
            media("LABEL", "RAW", position=2),
            media("SIZE_CHART", "RAW", position=3),
        ],
    )
    assert sorted(ids(p, pol)) == ["IMG.001", "IMG.010"]
    assert not_generatable_reason(p, pol) is None
    assert [s.view for s in source_images(p, pol)] == ["FRONT", "BACK"]
    assert len(garment_photos(p, pol)) == 2
    assert not is_mislabelled(view_report(p, pol), pol)


# --------------------------------------------------------------------------- #
# The decision itself.
#
# This is what callers act on, so these tests are the contract: the repair
# button, the sweep after generation and the backfill script all obey whatever
# `generation_plan` says, and none of them are allowed a second opinion.
# --------------------------------------------------------------------------- #


def test_plan_asks_for_the_one_missing_view(pol):
    """The common case, and the one the user described: front render present,
    back never made, both photographs matted and ready."""
    p = product(media=[*MATTED, media("AI_FRONT", "GENERATED", position=2)])
    plan = generation_plan(p, pol)
    assert plan.should_generate
    assert plan.views == ["AI_BACK"]
    assert plan.matte_first == []
    assert "AI_BACK" in plan.reason


def test_plan_declines_a_finished_product(pol):
    p = product(media=[*MATTED, *FULL_SET])
    plan = generation_plan(p, pol)
    assert not plan.should_generate
    assert plan.views == []
    # The reason is shown to an operator who pressed a button and got nothing,
    # so it has to say more than "no".
    assert plan.reason


def test_plan_declines_footwear_and_says_why(pol):
    p = product(category="Shoes", subcategory="Sneakers",
                title="Vintage Nike Sneakers Men 9", media=[*MATTED])
    plan = generation_plan(p, pol)
    assert not plan.should_generate
    assert "footwear" in plan.reason


def test_plan_declines_when_the_tenant_switched_generation_off(pol):
    p = product(
        imagery_settings=ImagerySettings(is_model_generation_enabled=False),
        media=[*MATTED],
    )
    plan = generation_plan(p, pol)
    assert not plan.should_generate
    assert "switched off" in plan.reason


def test_plan_never_regenerates_a_mislabelled_set(pol):
    """404 products carry five AI_FRONT rows and nothing else. Treating that as
    'AI_BACK missing' would spend ~2,000 calls reproducing pictures that exist."""
    p = product(media=[*MATTED] + [media("AI_FRONT", "GENERATED", position=i)
                                   for i in range(5)])
    plan = generation_plan(p, pol)
    assert not plan.should_generate
    assert "relabelling" in plan.reason


def test_plan_treats_matting_alone_as_work(pol):
    """Every render present, but an original the segmenter never touched. There
    is no view to generate and there IS work — which is why callers must test
    `should_generate` and not `len(views)`."""
    p = product(media=[
        media("FRONT", "BG_REMOVED"),
        media("BACK", "RAW", url="https://r2.dev/products/1-back-original.jpg",
              position=1),
        *FULL_SET,
    ])
    plan = generation_plan(p, pol)
    assert plan.should_generate
    assert plan.views == []
    assert [m.view for m in plan.matte_first] == ["BACK"]


def test_plan_reports_matting_alongside_a_missing_view(pol):
    p = product(media=[
        media("FRONT", "BG_REMOVED"),
        media("BACK", "RAW", url="https://r2.dev/products/1-back-original.jpg",
              position=1),
        media("AI_FRONT", "GENERATED", position=2),
    ])
    plan = generation_plan(p, pol)
    assert plan.should_generate
    assert plan.views == ["AI_BACK"]
    assert [m.view for m in plan.matte_first] == ["BACK"]


def test_advisory_views_are_work_only_when_asked_for(pol):
    p = product(media=[*MATTED, media("AI_FRONT", "GENERATED", position=2),
                       media("AI_BACK", "GENERATED", position=3)])
    assert not generation_plan(p, pol).should_generate

    opted_in = generation_plan(p, pol, include_advisory=True)
    assert opted_in.should_generate
    assert set(opted_in.views) == {"AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"}


def test_plan_declines_with_no_photograph_to_work_from(pol):
    p = product(media=[media("LABEL", "RAW"), media("SIZE_CHART", "RAW", position=1)])
    plan = generation_plan(p, pol)
    assert not plan.should_generate
    assert "no garment photograph" in plan.reason


def test_plan_on_the_real_product_shape(pol):
    """2b40ac19 again: matte the RAW back first, then make both renders."""
    p = product(
        id="2b40ac19-4dfe-4a6b-b0ee-916e8732f33d",
        media=[
            media("FRONT", "BG_REMOVED", url="https://r2.dev/products/1-0-processed.png"),
            media("BACK", "RAW", url="https://r2.dev/products/1-back-original.jpg",
                  position=1),
            media("LABEL", "RAW", position=2),
            media("SIZE_CHART", "RAW", position=3),
        ],
    )
    plan = generation_plan(p, pol)
    assert plan.should_generate
    assert plan.views == ["AI_FRONT", "AI_BACK"]
    # The size chart is NOT matting work, however RAW it looks.
    assert [m.view for m in plan.matte_first] == ["BACK"]


def test_close_up_is_not_advisory_when_the_tenant_switched_it_off(pol):
    """A tenant with isCloseUpEnabled=false does not want close-ups, and the
    analyze worker never makes one. Listing it as advisory was harmless while
    advisory views were never generated; now that a repair fills them in, it
    would spend a call on a view the tenant switched off."""
    off = product(
        imagery_settings=ImagerySettings(is_close_up_enabled=False),
        media=[*MATTED, media("AI_FRONT", "GENERATED", position=2),
               media("AI_BACK", "GENERATED", position=3)],
    )
    off_plan = generation_plan(off, pol, include_advisory=True)
    assert "AI_CLOSEUP" not in view_report(off, pol).advisory_missing
    assert "AI_CLOSEUP" not in off_plan.views
    # The ¾ views are NOT tenant-gated, so they are still proposed.
    assert set(off_plan.views) == {"AI_FRONT_34", "AI_BACK_34"}

    on = product(
        imagery_settings=ImagerySettings(is_close_up_enabled=True),
        media=[*MATTED, media("AI_FRONT", "GENERATED", position=2),
               media("AI_BACK", "GENERATED", position=3)],
    )
    assert "AI_CLOSEUP" in view_report(on, pol).advisory_missing
    assert "AI_CLOSEUP" in generation_plan(on, pol, include_advisory=True).views


def test_advisory_plan_covers_the_three_quarter_views(pol):
    """The worker attempts all five; a repair asked for advisory must fill the
    same set, not just the required pair."""
    p = product(media=[*MATTED, media("AI_FRONT", "GENERATED", position=2)])
    plan = generation_plan(p, pol, include_advisory=True)
    assert set(plan.views) == {
        "AI_BACK", "AI_FRONT_34", "AI_BACK_34", "AI_CLOSEUP"
    }


# --------------------------------------------------------------------------- #
# What a repair pays to matte.
#
# Generation seeds from FRONT and BACK. Matting the extra angles costs a
# provider call each and changes nothing about the render.
# --------------------------------------------------------------------------- #

def test_only_front_and_back_are_matted_not_every_angle(pol):
    p = product(media=[
        media("FRONT", "RAW"),
        media("BACK", "RAW", position=1),
        media("OTHER", "RAW", position=2),
        media("OTHER", "RAW", position=3),
        media("LABEL", "RAW", position=4),
    ])
    plan = generation_plan(p, pol)
    # Two calls, not four — and never the care label.
    assert sorted(m.view for m in plan.matte_first) == ["BACK", "FRONT"]


def test_a_view_that_already_has_a_cutout_is_not_re_matted(pol):
    """Screenshot 2 / 21ebf282: FRONT and BACK are already BG_REMOVED, with the
    superseded RAW uploads still on the row. Use what exists; never pay to make
    a second cut-out of the same view."""
    p = product(media=[
        media("FRONT", "BG_REMOVED"),
        media("BACK", "BG_REMOVED", position=1),
        media("FRONT", "RAW", position=2),
        media("BACK", "RAW", position=3),
        media("AI_FRONT", "GENERATED", position=4),
        media("AI_BACK", "GENERATED", position=5),
    ])
    assert generation_plan(p, pol).matte_first == []


def test_mis_filed_cutouts_do_not_exempt_front_and_back_from_matting(pol):
    """THE d29c474f CASE. Two cut-outs filed under OTHER, FRONT and BACK both
    RAW. `needs_segmenter` exempts the product — right for a catalog report,
    since re-matting 5,889 such products would be pointless — but generation
    cannot seed from a cut-out it cannot identify as the front, so it fell back
    to the RAW originals and rendered a garment on a stockroom wall.

    A repair therefore mattes FRONT and BACK regardless of what sits under
    OTHER."""
    p = product(media=[
        media("FRONT", "RAW"),
        media("BACK", "RAW", position=1),
        media("OTHER", "BG_REMOVED", position=2),
        media("OTHER", "BG_REMOVED", position=3),
    ])
    # The report still exempts it...
    assert needs_segmenter(p, pol) == []
    # ...and the repair still fixes the two views it actually generates from.
    assert sorted(m.view for m in generation_plan(p, pol).matte_first) == [
        "BACK", "FRONT",
    ]


def test_matting_only_front_and_back_is_still_work_worth_a_job(pol):
    """Every render present, FRONT still RAW: `should_generate` stays true with
    an empty `views`, so callers must not test `len(views)`."""
    p = product(media=[
        media("FRONT", "RAW"),
        media("BACK", "BG_REMOVED", position=1),
        *FULL_SET,
    ])
    plan = generation_plan(p, pol)
    assert plan.should_generate
    assert plan.views == []
    assert [m.view for m in plan.matte_first] == ["FRONT"]
