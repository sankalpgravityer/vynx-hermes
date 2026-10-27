"""Configured capture views and their media slots (vnyx-api, 23 Oct 2026).

A category's capture views (Category Settings) now earn their own slots — a
`side` view is `original_decision_side` / `original_photobooth_side` — and any
non-legacy view is a garment photo unless its setting says otherwise. Hermes
orders the gallery the way vnyx-api does, and CHECKS the cut-outs of such views
(IMG.032 / IMG.033) without ever re-cutting them.
"""

from __future__ import annotations

import copy

from app.config import policy
from app.models import ImagerySettings, MediaAsset, ProductSnapshot
from app.rules import shot_sequence as ss
from app.rules.imagery import check_imagery, gallery_order

POL = copy.deepcopy(policy())


def row(view, origin="DECISION", processing="RAW", position=0, **kw):
    return {"id": f"{view}-{origin}-{processing}-{position}", "view": view, "origin": origin,
            "processing": processing, "position": position, **kw}


# ---- slots --------------------------------------------------------------------

def test_slot_for_row_derives_the_slot_from_the_view():
    assert ss.slot_for_row("SIDE", "DECISION") == "original_decision_side"
    assert ss.slot_for_row("SHOEXSIDE", "WEB") == "original_photobooth_shoexside"
    assert ss.slot_for_row("FRONT", "PHOTOBOOTH") == "original_photobooth_front"
    assert ss.slot_for_row("LABEL", "WEB") == "labels"
    for view in ("OTHER", "AI_FRONT", "SIZE_CHART", "VIDEO_TURNTABLE"):
        assert ss.slot_for_row(view, "DECISION") is None
    assert ss.slot_for_row("SIDE", "MANUAL") is None


def test_normalize_keeps_a_category_slot_where_it_stands():
    lines = ss.normalize_sequence([
        {"slot": "original_decision_side", "enabled": False},
        {"shot": "full_front", "count": 1},
        {"slot": "not a slot"},
    ])
    assert lines[0] == {"slot": "original_decision_side", "enabled": False}
    assert [line.get("slot") for line in lines[2:]] == ss.MEDIA_SLOT_KEYS


def test_side_photo_takes_its_slot_position_and_can_be_hidden():
    seq = [{"slot": "original_decision_side"}, {"shot": "full_front", "count": 1},
           {"slot": "original_decision_front"}]
    rows = [row("FRONT", position=0), row("AI_FRONT", "AI", "GENERATED", 1, shotKey="full_front"),
            row("SIDE", position=2)]
    assert [r["view"] for r in ss.display_order(rows, seq)] == ["SIDE", "AI_FRONT", "FRONT"]

    hidden = [{"slot": "original_decision_side", "enabled": False}, *seq[1:]]
    assert [r["view"] for r in ss.display_order(rows, hidden)] == ["AI_FRONT", "FRONT"]


# ---- roles --------------------------------------------------------------------

def test_view_roles_first_setting_wins_and_legacy_is_fixed():
    roles = ss.view_roles([
        {"views": [{"key": "side", "isProductImage": True},
                   {"key": "tag", "isProductImage": False},
                   {"key": "front", "isProductImage": False}]},
        {"views": [{"key": "side", "isProductImage": False}]},
    ])
    assert roles == {"SIDE": "GARMENT", "TAG": "REFERENCE"}
    assert ss.role_of("FRONT", {"FRONT": "REFERENCE"}) == "GARMENT"
    assert ss.role_of("SOLE") == "GARMENT"
    assert ss.is_extra_garment_view("SIDE") and not ss.is_extra_garment_view("TAG", roles)
    assert not ss.is_extra_garment_view("FRONT")


def test_a_configured_garment_view_ranks_with_the_garment_photos():
    side = ss.canonical_rank("SIDE", "DECISION", "RAW")
    assert ss.canonical_rank("BACK", "DECISION", "RAW") < side < ss.canonical_rank("LABEL", "DECISION", "RAW")
    tag = ss.canonical_rank("TAG", "DECISION", "RAW", roles={"TAG": "REFERENCE"})
    assert tag > ss.canonical_rank("VIDEO_TURNTABLE", "DECISION", "RAW")


def test_gallery_order_without_a_sequence_uses_the_role():
    p = ProductSnapshot(id="p", media=[
        MediaAsset(url="l", view="LABEL", origin="DECISION", position=0),
        MediaAsset(url="s", view="SIDE", origin="DECISION", position=1),
        MediaAsset(url="f", view="FRONT", origin="DECISION", position=2),
    ])
    order = [m.url for m in gallery_order(p, POL)]  # no shotSequence: the fixed comparator
    assert order.index("f") < order.index("s") < order.index("l")


# ---- configured views are cut out, judged and re-cut like FRONT/BACK -----------

def snap(media, roles=None):
    return ProductSnapshot(id="p", title="Vintage Nike Air Jordan 1 Sneakers Men 42",
                           master_category="Men", category="Shoes", media=media,
                           view_roles=roles or {}, imagery_settings=ImagerySettings())


def found(p):
    return {f.rule_id: f for f in check_imagery(p, POL)}


def test_side_photo_without_a_cutout_is_img010_for_the_matte_step():
    p = snap([MediaAsset(url="f", view="FRONT", processing="BG_REMOVED"),
              MediaAsset(url="s", view="SIDE", origin="PHOTOBOOTH")])
    assert found(p)["IMG.010"].detail["views"] == ["SIDE"]


def test_side_with_a_cutout_or_a_reference_view_needs_no_matte():
    with_cut = snap([MediaAsset(url="s", view="SIDE", processing="BG_REMOVED"),
                     MediaAsset(url="r", view="SIDE", is_current=False)])
    assert "IMG.010" not in found(with_cut)
    ref = snap([MediaAsset(url="t", view="TAG")], roles={"TAG": "REFERENCE"})
    assert "IMG.010" not in found(ref)


def test_side_cutout_off_its_photo_frame_is_img026_for_the_rematte():
    p = snap([MediaAsset(url="s", id="c", view="SIDE", processing="BG_REMOVED",
                         derived_from_id="r", width=1000, height=1000),
              MediaAsset(url="r", id="r", view="SIDE", is_current=False,
                         width=3000, height=4000)])
    hit = found(p)["IMG.026"]
    assert hit.detail["view"] == "SIDE"


def test_cutout_views_and_pairs_include_the_configured_view():
    from app.rules.imagery import cutout_pairs, cutout_views

    p = snap([MediaAsset(url="s", id="c", view="SIDE", processing="BG_REMOVED", derived_from_id="r"),
              MediaAsset(url="r", id="r", view="SIDE", is_current=False)])
    assert cutout_views(p, POL) == {"FRONT", "BACK", "SIDE"}
    assert [(v, c.id, r.id) for v, c, r in cutout_pairs(p, POL)] == [("SIDE", "c", "r")]
    assert ss.is_cutout_view("SIDE") and not ss.is_cutout_view("OTHER")


def test_cutcheck_looks_at_a_side_cutout():
    from app.imaging import gallery_cleanup as gc

    rows = [{"id": "c", "url": "https://x/side-cut.png", "view": "SIDE", "origin": "PHOTOBOOTH",
             "processing": "BG_REMOVED", "derivedFromId": None, "deletedAt": None, "createdAt": None}]
    assert [r["id"] for r in gc.listing_cutouts(rows, [])] == ["c"]


def test_needs_counts_an_unmatted_side_for_the_matte_step():
    from scripts import repair_product as rp

    loaded = {"record": {"viewRoles": {}}, "catalog": {}, "media": [
        {"view": "FRONT", "processing": "BG_REMOVED", "mediaType": "IMAGE", "url": "f", "origin": "WEB"},
        {"view": "SIDE", "processing": "RAW", "mediaType": "IMAGE", "url": "s", "origin": "WEB"},
    ]}
    n = rp.needs_from(loaded)
    assert n["unmatted"] and "SIDE" in n["unmatted_views"]
