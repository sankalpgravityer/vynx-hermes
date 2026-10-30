"""The readable resale sites (29 Oct 2026): Vinted's own search and the
price on a Marktplaats / Vinted listing page."""
from __future__ import annotations

from app.pricing import resale_lookup as RL
from app.pricing import site_pages as SP

CARD = ('<div data-testid="product-item-id-10024695390"><div class="x"><img src="https://images1.vinted.net/t/06/57010551.webp?s=1" '
        'alt="Patagonia Parka Skyforest W, Merk: Patagonia, Staat: Heel goed, Maat: S, 82.00 €, 86.80 €" class="c"/></div>'
        '<a href="/items/10024695390-patagonia-parka-skyforest-w?referrer=catalog" class="o">x</a></div>'
        '<div data-testid="product-item-id-9099039049"><img src="https://images1.vinted.net/t/05/2.webp" '
        'alt="Patagonia Skyforest M Windbreaker &amp; Jacke, Marke: Patagonia, Zustand: Gut, Größe: M / 38, 125,00 €, 131,00 €"/>'
        '<a href="/items/9099039049-patagonia-skyforest-m">x</a></div>')


def test_vinted_cards_are_read_from_their_alt_text(monkeypatch):
    monkeypatch.setattr(SP, "fetch", lambda url, timeout_s=20.0: CARD)
    comps, err = SP.vinted_search("patagonia skyforest", "vinted.nl")
    assert err is None and len(comps) == 2
    a, b = comps
    assert a["title"] == "Patagonia Parka Skyforest W" and a["price_raw"] == "82.00 EUR"
    assert (a["condition"], a["size"], a["source"], a["platform"]) == ("Heel goed", "S", "vinted", "Vinted")
    assert a["url"] == "https://www.vinted.nl/items/10024695390-patagonia-parka-skyforest-w"
    assert a["image"].startswith("https://images1.vinted.net/")
    assert b["price_raw"] == "125.00 EUR" and b["size"] == "M / 38" and "&" in b["title"]   # German card


def test_page_price_reads_structured_data_and_sold_state(monkeypatch):
    vinted = '{"@type":"Offer","priceCurrency":"EUR","price":82,"availability":"https://schema.org/InStock"} is verkocht'
    mp = '"price":"85.99","priceType":"FIXED","currency":"EUR"'
    pages = {"https://www.vinted.nl/items/1-a": vinted, "https://www.marktplaats.nl/v/a/b/a1-x": mp,
             "https://www.vinted.nl/items/2-b": vinted.replace("InStock", "SoldOut")}
    monkeypatch.setattr(SP, "fetch", lambda url, timeout_s=20.0: pages.get(url))
    assert SP.page_price("https://www.vinted.nl/items/1-a") == {"price_raw": "82.00 EUR", "sold": False, "title": ""}
    assert SP.page_price("https://www.marktplaats.nl/v/a/b/a1-x")["price_raw"] == "85.99 EUR"
    assert SP.page_price("https://www.vinted.nl/items/2-b")["sold"] is True
    assert SP.page_price("https://www.ebay.de/itm/1") is None                   # not readable


def test_a_lens_match_on_a_readable_site_gets_its_page_price(monkeypatch):
    monkeypatch.setattr(SP, "page_price", lambda url, timeout_s=20.0: {"price_raw": "85.99 EUR", "sold": False, "title": "t"})
    comps = [{"url": "https://www.marktplaats.nl/v/a/b/a1-x", "source": "lens", "price_raw": "", "title": "x"},
             {"url": "https://www.ebay.de/itm/237123499945", "source": "lens", "price_raw": ""}]
    assert SP.price_readable_pages(comps) == 1
    assert comps[0]["price_raw"] == "85.99 EUR" and comps[0]["source"] == "lens+page"
    assert "page_price" not in comps[1]


def test_vinted_listings_count_as_verified_in_the_lookup(monkeypatch, tmp_path):
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: (
        {"brand": "Patagonia", "model": "Skyforest Parka", "id_confidence": "high"}, None))
    vinted = [{"url": f"https://www.vinted.nl/items/{i}-x", "title": "Patagonia Skyforest Parka", "price_raw": "82.00 EUR",
               "status": "listed", "source": "vinted", "item_id": str(i), "cited": [], "location": "NL",
               "image": f"https://images1.vinted.net/{i}.webp"} for i in (1, 2, 3)]
    asked: list[list[str]] = []
    monkeypatch.setattr(SP, "direct_search", lambda queries, cfg: (asked.append(queries) or
                                                                   {"comps": vinted, "calls": 1, "notes": []}))
    monkeypatch.setattr(RL, "search_round", lambda *a: __import__("pytest").fail("Google must not run"))
    monkeypatch.setattr(RL, "mark_own_listings", lambda item, comps: 0)

    def judge(ident, item, cs, key, usage):
        for c in cs:
            c["match"] = {"verdict": "same", "reason": "photo"}
        return len(cs)

    monkeypatch.setattr(RL, "judge_matches", judge)
    out = RL.lookup({"sku": "BOA-006950", "images": ["https://r2/a.jpg"], "_photos": {"https://r2/a.jpg": b"x"},
                     "vnyx_price": 85.99, "condition": "As New"}, fresh=True, image=False)
    assert asked == [["Patagonia Skyforest Parka"]]
    assert out["direct_search"]["vinted"] == 3 and out["text_search"]["ran"] is False
    assert out["n_comps"] == 3 and all(c["verified"] == "vinted" for c in out["comps"])
    assert out["n_eu"] == 3 and out["basis"] == "EU listings"
