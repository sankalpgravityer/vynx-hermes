"""Is this listing OUR OWN? (29 Oct 2026)

BOA-006463: the only "same item" the lookup found was BOAS's own eBay and
Marktplaats listings of that very product — seller `boas-savefashionforgood`,
our own photo, our price. Pricing a product against itself is circular, so an
own listing is shown, labelled, and never counted.

Two tests, no model call:
  seller   the eBay seller name is one of the tenant's (policy `own_sellers`);
  photo    the listing's photo IS one of our pictures of the product — the
           raw photos or the cut-outs and renders the channels were given —
           by perceptual hash (dHash, 64 bits): a resized or re-encoded copy
           differs in a handful of bits, another garment in dozens.
"""
from __future__ import annotations

import io
import logging
from typing import Any

log = logging.getLogger("hermes.resale.own")

# dHash bits that may differ for "the same picture". Measured on our own
# cut-outs as the channels show them: 3-4 (BOA-006463), 13-14 (BOA-006950, a
# Lens thumbnail and the page's own image alike); another photo of the same
# model 20+, another product 25-42. Above the first bound, the listing also
# asking exactly OUR price settles it.
OWN_PHOTO_MAX_DISTANCE = 15
OWN_PRICE_MATCH_DISTANCE = 20


def dhash(data: bytes, size: int = 8) -> int | None:
    """A 64-bit difference hash of an image, or None when it cannot be read."""
    try:
        from PIL import Image, ImageOps

        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        if im.mode in ("RGBA", "LA", "P"):          # a cut-out: transparent = white
            bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
            bg.alpha_composite(im.convert("RGBA"))
            im = bg
        im = im.convert("L")
        # Our cut-outs sit on white with margin; trim it (ignoring JPEG noise
        # in the white) so a listing's crop of the same picture hashes alike.
        bbox = im.point(lambda p: 255 if p < 235 else 0).getbbox()
        if bbox:
            im = im.crop(bbox)
        im = im.resize((size + 1, size), Image.Resampling.LANCZOS)
        px = list(im.tobytes())
        bits = 0
        for row in range(size):
            for col in range(size):
                left, right = px[row * (size + 1) + col], px[row * (size + 1) + col + 1]
                bits = (bits << 1) | (1 if left > right else 0)
        return bits
    except Exception:  # noqa: BLE001
        return None


def distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def own_hashes(photos: dict[str, bytes]) -> list[int]:
    return [h for h in (dhash(b) for b in photos.values()) if h is not None]


def own_sellers_for(tenant: str | None, cfg: dict[str, Any]) -> set[str]:
    """policy `own_sellers`: {tenant name: [seller names]} plus '*' for all."""
    m = cfg.get("own_sellers") or {}
    names = list(m.get("*") or [])
    for k, v in m.items():
        if k != "*" and tenant and k.strip().lower() == tenant.strip().lower():
            names += list(v or [])
    return {str(n).strip().lower() for n in names if str(n).strip()}


def mark_own(comps: list[dict], ours: list[int], sellers: set[str],
             listing_photos: dict[str, bytes],
             max_distance: int = OWN_PHOTO_MAX_DISTANCE,
             our_price: float | None = None,
             price_match_distance: int = OWN_PRICE_MATCH_DISTANCE) -> int:
    """Set c["own"] = {"why": ...} on every own listing. -> how many."""
    n = 0
    for c in comps:
        if c.get("own"):
            n += 1
            continue
        seller = str(c.get("seller") or "").strip().lower()
        if seller and seller in sellers:
            c["own"] = {"why": f"our own seller account ({c['seller']})"}
            n += 1
            continue
        b = listing_photos.get(c.get("image") or "")
        if b and ours:
            h = dhash(b)
            if h is not None:
                d = min(distance(h, o) for o in ours)
                c["photo_distance"] = d
                same_price = (our_price is not None and c.get("price_eur")
                              and abs(float(c["price_eur"]) - float(our_price)) < 0.05)
                if d <= max_distance:
                    c["own"] = {"why": f"the listing shows our own photo ({d} bits apart)"}
                    n += 1
                elif d <= price_match_distance and same_price:
                    c["own"] = {"why": f"our own photo ({d} bits apart) at exactly our price"}
                    n += 1
    return n
