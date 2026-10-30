"""Our own listings (29 Oct 2026): BOA-006463 was "priced" against BOAS's own
eBay and Marktplaats listings of the product. Caught by the eBay seller name
and by the listing's photo being one of ours."""
from __future__ import annotations

import io

from PIL import Image, ImageDraw

from app.pricing import own_listing as OWN


def _png(draw_fn, size=(300, 400)) -> bytes:
    im = Image.new("RGB", size, "white")
    draw_fn(ImageDraw.Draw(im))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _jpeg_thumb(png: bytes, size=(120, 160)) -> bytes:
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.thumbnail(size)
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=60)
    return buf.getvalue()


def jacket(d):      # a navy "jacket": body, hood, a red patch on the left sleeve
    d.rectangle([80, 90, 220, 360], fill=(20, 30, 70))
    d.ellipse([110, 40, 190, 110], fill=(20, 30, 70))
    d.rectangle([40, 100, 85, 300], fill=(20, 30, 70))
    d.rectangle([215, 100, 260, 300], fill=(20, 30, 70))
    d.rectangle([225, 130, 250, 160], fill=(200, 40, 40))


def other(d):       # a grey fleece pullover, no hood, chest pocket
    d.rectangle([70, 80, 230, 340], fill=(150, 150, 150))
    d.rectangle([30, 90, 75, 260], fill=(150, 150, 150))
    d.rectangle([225, 90, 270, 260], fill=(150, 150, 150))
    d.rectangle([150, 120, 210, 170], fill=(40, 40, 90))


def test_a_thumbnail_of_our_photo_is_ours_and_another_garment_is_not():
    ours = _png(jacket)
    hashes = OWN.own_hashes({"https://r2/front.png": ours})
    assert len(hashes) == 1
    same = OWN.dhash(_jpeg_thumb(ours))
    different = OWN.dhash(_png(other))
    assert OWN.distance(same, hashes[0]) <= OWN.OWN_PHOTO_MAX_DISTANCE
    assert OWN.distance(different, hashes[0]) > OWN.OWN_PHOTO_MAX_DISTANCE


def test_mark_own_by_seller_and_by_photo():
    ours = _png(jacket)
    hashes = OWN.own_hashes({"u": ours})
    comps = [
        {"url": "https://www.ebay.de/itm/1", "seller": "boas-savefashionforgood", "image": "https://i/1"},
        {"url": "https://www.marktplaats.nl/v/a1", "image": "https://i/2"},
        {"url": "https://www.ebay.de/itm/3", "seller": "someone_else", "image": "https://i/3"},
    ]
    photos = {"https://i/2": _jpeg_thumb(ours), "https://i/3": _png(other)}
    n = OWN.mark_own(comps, hashes, OWN.own_sellers_for("BOAS", {"own_sellers": {"boas": ["BOAS-SaveFashionForGood"]}}),
                     photos)
    assert n == 2
    assert "seller" in comps[0]["own"]["why"]
    assert "our own photo" in comps[1]["own"]["why"]
    assert "own" not in comps[2] and comps[2]["photo_distance"] > OWN.OWN_PHOTO_MAX_DISTANCE


def test_own_sellers_for_matches_the_tenant_and_the_wildcard():
    cfg = {"own_sellers": {"BOAS": ["a"], "Klekt": ["b"], "*": ["c"]}}
    assert OWN.own_sellers_for("boas", cfg) == {"a", "c"}
    assert OWN.own_sellers_for("Nobody", cfg) == {"c"}
    assert OWN.own_sellers_for(None, {}) == set()


def test_a_looser_match_counts_as_ours_only_at_exactly_our_price():
    comps = [{"url": "https://www.marktplaats.nl/v/a1", "image": "https://i/1", "price_eur": 85.99},
             {"url": "https://www.marktplaats.nl/v/a2", "image": "https://i/2", "price_eur": 60.0}]
    # Both photos 17 bits from ours: only the one at our price is ours.
    import app.pricing.own_listing as O
    monkey_h = {"https://i/1": 0b0, "https://i/2": 0b0}
    ours = [int("1" * 17, 2)]                       # 17 bits set -> distance 17 from 0
    orig = O.dhash
    O.dhash = lambda b: 0
    try:
        n = O.mark_own(comps, ours, set(), {"https://i/1": b"x", "https://i/2": b"y"}, our_price=85.99)
    finally:
        O.dhash = orig
    assert n == 1 and "exactly our price" in comps[0]["own"]["why"] and "own" not in comps[1]
    assert comps[1]["photo_distance"] == 17
