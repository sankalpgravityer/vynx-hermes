"""The run report's "Listings" for a reprice (23 Oct 2026).

A shoe is priced from the StockX page's retail price, not from listings, and
the run row showed no link at all for one (KLE-002750). The page the price
was picked from now leads the list; the UI prints it unchanged.
"""
from __future__ import annotations

from app.services.auto_approval import reprice as RP

STOCKX = "https://stockx.com/vans-sk8-hi-navy"


def test_a_shoe_lists_the_stockx_page_its_price_came_from():
    res = {"price_source": "StockX only (footwear)", "retail_url": STOCKX,
           "retail_new_eur": 85.92, "retail_raw": "$100", "new_retail": 85.99,
           "retail_verified": "cited",
           "comps": [{"url": STOCKX, "counted": False, "verified": "exact"}]}
    got = RP.summary(res)["listings"]
    assert got == [{"url": STOCKX, "priceEur": 85.92, "status": "StockX retail $100",
                    "verified": "cited"}]          # the same page is listed once


def test_an_unproven_retail_says_so_and_unpriced_pages_are_still_linked():
    other = "https://stockx.com/vans-sk8-hi-black"
    res = {"price_source": "StockX only (footwear)", "retail_url": STOCKX,
           "retail_new_eur": 85.92, "retail_raw": "$100", "new_retail": None,
           "comps": [{"url": other, "counted": False, "verified": "none"}]}
    got = RP.summary(res)["listings"]
    assert got[0]["status"] == "StockX retail $100 (not proven)"
    assert got[1] == {"url": other, "priceEur": None, "status": "StockX page, no price shown",
                      "verified": "none"}


def test_clothing_lists_only_its_counted_listings_as_before():
    res = {"price_source": "resale sites", "retail_url": "https://brand.com/p",
           "retail_new_eur": 200.0,
           "comps": [{"url": "https://www.vinted.nl/items/1234567-a", "price_eur": 80.0,
                      "status": "sold", "verified": "exact", "counted": True},
                     {"url": "https://www.ebay.de/itm/123456789012", "counted": False}]}
    assert RP.summary(res)["listings"] == [
        {"url": "https://www.vinted.nl/items/1234567-a", "priceEur": 80.0, "status": "sold",
         "verified": "exact"}]
