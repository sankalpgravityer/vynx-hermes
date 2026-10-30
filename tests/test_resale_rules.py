"""The resale lookup's pure parts: pricing maths, answer parsing, verification.

No network: FX rates are pinned, and nothing here calls Gemini or Google.
Figures come from the BOAS handoff (5 Oct 2026) where it gave them.
"""
from __future__ import annotations

import copy

import pytest

from app.pricing import resale_lookup as RL
from app.pricing import resale_rules as R

FX = {"EUR": 1.0, "USD": 0.86, "GBP": 1.16, "SEK": 0.09, "_source": "pinned"}


@pytest.fixture(autouse=True)
def _pinned_fx():
    R.set_fx_rates(dict(FX))
    yield
    R.set_fx_rates(None)


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path)})
    return tmp_path


# --------------------------------------------------------------------------- #
# Prices and currencies
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, amount, cur", [
    ("€85", 85.0, "EUR"),
    ("120 USD", 120.0, "USD"),
    ("£60", 60.0, "GBP"),
    ("€1.234,50", 1234.5, "EUR"),
    ("$1,234.50", 1234.5, "USD"),
    ("38,99 €", 38.99, "EUR"),
    ("16690 RUB", 16690.0, "RUB"),
    ("450 kr", 450.0, "SEK"),
    ("1200 USD", 1200.0, "USD"),        # the handoff read this as 120
    ("€1250", 1250.0, "EUR"),
    ("1 234 SEK", 1234.0, "SEK"),
    ("€1,20", 1.2, "EUR"),
])
def test_parse_price(raw, amount, cur):
    assert R.parse_price(raw) == (amount, cur)


def test_a_rouble_price_is_dropped_not_read_as_euros():
    # The handoff's €16,690 Haglöfs.
    assert R.to_eur(16690.0, "RUB") is None
    assert R.to_eur(100.0, "USD") == 86.0


@pytest.mark.parametrize("url, loc, region", [
    ("https://www.vinted.nl/items/123456789-hoodie", "", "EU"),
    ("https://www.ebay.co.uk/itm/123456789", "", "UK"),
    ("https://www.ebay.com/itm/123456789", "", "US"),
    ("https://www.grailed.com/listings/40669088-x", "", "GLOBAL"),
    ("https://www.grailed.com/listings/40669088-x", "Germany", "EU"),
    ("https://www.ebay.de/itm/123456789", "", "EU"),
])
def test_region_of(url, loc, region):
    assert R.region_of(url, loc) == region


# --------------------------------------------------------------------------- #
# price_item
# --------------------------------------------------------------------------- #
def comp(price, *, status="sold", cond="very good", url="https://www.vinted.nl/items/1234567-x",
         loc=""):
    return {"price_eur": price, "status": status, "condition": cond, "url": url, "location": loc}


def test_rounds_the_vnyx_way_never_down():
    # The handoff's r99 made 40.30 -> 39.99. VNYX keeps the euros: 40.99.
    assert R.round99(40.30) == 40.99
    assert R.round99(40.60) == 40.99
    assert R.round99(19.00) == 19.99


def test_eu_median_wins_when_two_eu_listings():
    comps = [comp(100, url="https://www.vinted.nl/items/1111111-a"),
             comp(110, url="https://www.vinted.de/items/2222222-b"),
             comp(300, url="https://www.grailed.com/listings/3333333-c")]
    out = R.price_item(comps, "Very good", 50)
    assert out["basis"] == "EU listings"
    assert out["n_eu"] == 2
    assert out["suggested_eur"] == 100.99           # EU weighted median 100 -> .99


def test_an_outlier_is_dropped_by_position_not_identity():
    comps = [comp(100.0), comp(100.0), comp(105.0), comp(900.0)]
    R.price_item(comps, "Very good", 50)
    assert [bool(c.get("outlier")) for c in comps] == [False, False, False, True]


def test_items_under_25_are_never_cut():
    comps = [comp(10), comp(11), comp(12)]
    out = R.price_item(comps, "Very good", 20.0)
    assert out["suggested_eur"] == 20.0
    assert "only raised" in out["price_note"]


def test_a_cut_needs_three_verified_listings():
    comps = [comp(40), comp(42)]
    out = R.price_item(comps, "Very good", 80.0)
    assert out["suggested_eur"] == 80.0
    assert "too few to cut" in out["price_note"]


def test_a_cut_with_three_listings_goes_through_and_is_flagged():
    comps = [comp(30), comp(32), comp(31)]
    out = R.price_item(comps, "Very good", 80.0)
    assert out["suggested_eur"] == 31.99
    assert "check" in out["price_flag"]             # under 0.5x VNYX


def test_asking_prices_are_discounted():
    sold = R.price_item([comp(100, status="sold")], "Very good", None)["suggested_eur"]
    listed = R.price_item([comp(100, status="listed")], "Very good", None)["suggested_eur"]
    assert sold == 100.99 and listed == 85.99


def test_a_re_price_forgets_an_earlier_outlier_verdict():
    c = comp(500.0)
    c["outlier"] = True
    R.price_item([c], "Very good", None)
    assert "outlier" not in c


# --------------------------------------------------------------------------- #
# Tiers and authenticity
# --------------------------------------------------------------------------- #
def test_trust_tiers():
    assert R.trust_tier(None, 50, None, "no listings", 0, "") == ("", "")
    assert R.trust_tier(80, 50, 80, "high", 3, "")[0] == "A"
    assert R.trust_tier(80, 50, 80, "low", 1, "")[0] == "B"
    assert R.trust_tier(80, 50, 80, "high", 3, "hold")[0] == "C"
    assert R.trust_tier(80, 50, 80, "high", 3, "", cond_flag=True)[0] == "C"
    assert R.trust_tier(20, 50, 20, "high", 3, "")[0] == "C"         # cut below 0.5x
    assert R.trust_tier(1200, 500, 1200, "high", 3, "")[0] == "C"    # over €1,000
    assert R.trust_tier(450, 50, 450, "high", 1, "")[0] == "C"       # over 8x from one listing
    assert R.trust_tier(350, 50, 350, "high", 3, "")[0] == "B"       # raise over 6x
    # Judged: tier A needs two listings confirmed the same item, not two similar ones.
    assert R.trust_tier(80, 50, 80, "high", 3, "", n_same=0)[0] == "B"
    assert "0 listings confirmed" in R.trust_tier(80, 50, 80, "high", 3, "", n_same=0)[1]
    assert R.trust_tier(80, 50, 80, "high", 3, "", n_same=2)[0] == "A"


def test_auth_level():
    assert R.auth_level("") == ""
    assert R.auth_level("None observed") == ""
    assert R.auth_level("Verify the Certilogo code") == "check"
    assert R.auth_level("Patch strongly points to a counterfeit replica") == "hold"


def test_brand_group():
    assert R.brand_group("Stone Island") == "designer"
    assert R.brand_group("C.P. Company") == "designer"
    assert R.brand_group("Primark") == "other"


# --------------------------------------------------------------------------- #
# Reading the search answer
# --------------------------------------------------------------------------- #
ANSWER = """Here is what I found.

### Stone Island crewneck navy
* Platform: Vinted
* Status: sold
* Condition: Very good
* Location: Netherlands
* Price: €120
* URL: https://www.vinted.nl/items/123456789-stone-island

### Stone Island crewneck
* Platform: Grailed
* Status: listed
* Condition: Used
* Location: United States
* Price: 150 USD
* URL: [link](https://www.grailed.com/listings/40669088-stone-island)

ESTIMATE: €110
CONFIDENCE: medium
RETAIL NEW: €290
REASON: Two comparable listings.
"""


def test_parse_search_answer_maps_citations_to_blocks():
    first = ANSWER.index("### Stone Island crewneck navy")
    second = ANSWER.index("### Stone Island crewneck\n")
    notes = [{"url": "https://r/1", "start_index": first + 5, "end_index": first + 20},
             {"url": "https://r/2", "start_index": second + 5, "end_index": second + 20}]
    res = RL.parse_search_answer(ANSWER, notes)
    a, b = res["comps"]
    assert (a["price_eur"], a["currency"], a["status"]) == (120.0, "EUR", "sold")
    assert (b["price_eur"], b["currency"], b["status"]) == (129.0, "USD", "listed")
    assert b["url"] == "https://www.grailed.com/listings/40669088-stone-island"
    assert a["_cite_raw"] == ["https://r/1"] and b["_cite_raw"] == ["https://r/2"]
    assert res["resale_estimate_eur"] == 110.0
    assert res["estimate_confidence"] == "medium"
    assert res["retail_new_eur"] == 290.0


@pytest.mark.parametrize("url, kind", [
    ("https://www.vinted.nl/items/123456789-x", "listing"),
    ("https://www.ebay.de/itm/123456789012", "listing"),
    ("https://www.ebay.de/sch/i.html?_nkw=stone+island", "search"),
    ("https://www.kleinanzeigen.de/s-anzeige/supreme-hoodie/2870020119-160-2049", "listing"),
    ("https://www.kleinanzeigen.de/s-stone-island/k0", "search"),
    ("https://www.vinted.nl/", "search"),
    ("https://images.vinted.net/t/abc.jpg", "other"),
])
def test_url_kind(url, kind):
    assert RL.url_kind(url) == kind


def test_summarise_counts_only_grounded_listings_on_resale_sites():
    item = {"condition": "Very good", "vnyx_price": 60.0}
    res = {"estimate_confidence": "medium", "comps": [
        {"price_raw": "€120", "status": "sold", "condition": "Very good",
         "url": "https://www.vinted.nl/items/123456789-a",
         "cited": ["https://www.vinted.nl/items/123456789-a"]},                 # exact
        {"price_raw": "€130", "status": "sold", "condition": "Very good",
         "url": "https://www.vinted.nl/items/987654321-typo",
         "cited": ["https://www.vinted.nl/items/987654322-real"]},              # cited one wins
        {"price_raw": "€90", "status": "sold", "condition": "Very good",
         "url": "https://www.depop.com/products/abcdef-1234567", "cited": []},  # not verified
        {"price_raw": "€95", "status": "sold", "condition": "Very good",
         "url": "https://www.depop.com/products/ghijk-7654321", "cited": [],
         "page_check": {"ok": True, "why": "page shows 95"}},                   # opened by us
        {"price_raw": "€99", "status": "sold", "condition": "Very good",
         "url": "https://www.someshop.com/products/1234567", "cited": []},      # not a resale site
    ]}
    grounding = {"queries": ["stone island crewneck used"], "domains": [], "sources": []}
    s = RL.summarise(item, res, grounding)
    verified = [c["verified"] for c in res["comps"]]
    assert verified == ["exact", "exact", "none", "page", "none"]
    assert res["comps"][1]["url"] == "https://www.vinted.nl/items/987654322-real"
    assert s["n_comps"] == 3 and s["n_sold"] == 3
    assert s["confidence"] == "high"


@pytest.mark.parametrize("url, kind", [
    ("https://stockx.com/nike-dunk-low-retro-white-black-2021", "listing"),
    ("https://stockx.com/de-de/nike-dunk-low-retro-white-black-2021", "listing"),
    ("https://stockx.com/sneakers", "search"),
    ("https://stockx.com/brands/nike", "search"),
    ("https://stockx.com/search?s=dunk+low", "search"),
    ("https://images.stockx.com/images/Nike-Dunk-Low.jpg", "other"),
])
def test_url_kind_stockx(url, kind):
    assert RL.url_kind(url) == kind


def test_a_shoe_counts_stockx_listings_only():
    item = {"condition": "Very good", "vnyx_price": 90.0, "footwear": True}
    stockx = ["https://stockx.com/nike-dunk-low-retro-white-black-2021",
              "https://stockx.com/nike-dunk-low-retro-white-black-gs"]
    res = {"estimate_confidence": "medium", "comps": [
        {"price_raw": "€110", "status": "sold", "condition": "deadstock",
         "url": stockx[0], "cited": [stockx[0]]},
        {"price_raw": "€120", "status": "listed", "condition": "deadstock",
         "url": stockx[1], "cited": [stockx[1]]},
        {"price_raw": "€70", "status": "sold", "condition": "Very good",
         "url": "https://www.ebay.de/itm/123456789012", "cited": ["https://www.ebay.de/itm/123456789012"]},
        {"price_raw": "€75", "status": "sold", "condition": "Very good",
         "url": "https://www.grailed.com/listings/12345678-nike-dunk",
         "cited": ["https://www.grailed.com/listings/12345678-nike-dunk"]},
    ]}
    grounding = {"queries": ["site:stockx.com nike dunk low"], "domains": [], "sources": []}
    s = RL.summarise(item, res, grounding)
    assert [c["counted"] for c in res["comps"]] == [True, True, False, False]
    assert s["n_comps"] == 2
    # The same listings for a jacket: StockX is not a site it prices from.
    s2 = RL.summarise({**item, "footwear": False}, res, grounding)
    assert [c["counted"] for c in res["comps"]] == [False, False, True, True]
    assert s2["n_comps"] == 2


def test_a_shoe_searches_stockx_and_keeps_its_own_cache_key(cache_dir, monkeypatch):
    ident = {"brand": "Nike", "model": "Dunk Low Retro White Black", "article_code": "DD1391-100",
             "id_confidence": "high"}
    assert RL.cache_key(ident, footwear=True) == "stockx|nike|code|dd1391100"
    assert RL.cache_key(ident) == "nike|code|dd1391100"
    prompts = []
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (ident, None))

    def search(p, key, usage):
        prompts.append(p)
        return ({"comps": []}, {"queries": ["q"], "domains": [], "sources": [], "search_count": 2,
                                "raw_answer": ""}, None)

    monkeypatch.setattr(RL, "search_round", search)
    out = RL.lookup({"sku": "BOA-1", "images": ["https://r2/a.jpg"], "footwear": True})
    assert out["price_source"] == "StockX only (footwear)"
    assert len(prompts) == 2 and all("stockx.com" in p for p in prompts)
    assert prompts[0].startswith("Search Google for this shoe on StockX")
    assert prompts[1].startswith("Second StockX search")


def test_a_shoe_is_the_stockx_price_less_35_percent():
    # Last sale wins over the lowest ask; no condition or asking discount.
    comps = [{"price_eur": 165.0, "status": "sold", "condition": "deadstock",
              "url": "https://stockx.com/adidas-continental-80-recon-pack"},
             {"price_eur": 191.0, "status": "listed", "condition": "deadstock",
              "url": "https://stockx.com/adidas-continental-80-recon-pack"}]
    p = R.price_shoe(comps, 40.99)
    assert p["suggested_eur"] == 107.99            # 165 × 0.65 = 107.25 -> .99
    assert p["basis"] == "StockX last sale €165.00 less 35%"
    p = R.price_shoe(comps[1:], 40.99)
    assert p["suggested_eur"] == 124.99            # 191 × 0.65 = 124.15
    assert "lowest ask" in p["basis"]
    # A cut goes through too: no 3-listing / under-€25 guard for shoes.
    assert R.price_shoe(comps[:1], 150.0)["suggested_eur"] == 107.99


def test_shoe_tiers():
    assert R.shoe_tier(107.99, 40.99, 1, "")[0] == "A"
    assert R.shoe_tier(107.99, 40.99, 0, "")[0] == "B"          # page not cited
    assert R.shoe_tier(107.99, 40.99, 1, "", cond_flag=True)[0] == "C"   # Lived In
    assert R.shoe_tier(107.99, 15.0, 1, "")[0] == "B"           # > 6x
    assert R.shoe_tier(20.0, 50.0, 1, "")[0] == "C"             # big cut


def _shoe_lookup(cache_dir, monkeypatch, res, sources=()):
    ident = {"brand": "adidas", "model": "Ozweego Dusty Pink", "article_code": "FY2024",
             "id_confidence": "high"}
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (ident, None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        copy.deepcopy(res),
        {"queries": ["site:stockx.com FY2024"], "domains": [], "search_count": 2,
         "sources": [{"url": u} for u in sources], "raw_answer": ""}, None))
    return RL.lookup({"sku": "KLE-002781", "images": ["https://r2/a.jpg"], "footwear": True,
                      "condition": "As New", "vnyx_price": 40.99, "retail_price": 96.99})


def test_a_shoe_sells_at_the_stockx_retail_less_35_percent(cache_dir, monkeypatch):
    url = "https://stockx.com/adidas-ozweego-dusty-pink"
    out = _shoe_lookup(cache_dir, monkeypatch, {
        "comps": [], "retail_new_eur": 94.6, "retail_raw": "US$110", "retail_url": url,
        "retail_style": "FY2024", "retail_cited": []}, sources=[url])
    assert out["new_retail"] == 94.99                   # our retail = StockX retail, to .99
    assert out["new_price"] == 61.99                    # 94.99 × 0.65 = 61.74 -> .99
    assert out["basis"] == "StockX retail €94.99 less 35%"
    assert out["tier"] == "A" and "less 35%" in out["basis"]


def test_a_shoe_retail_from_another_style_code_is_held(cache_dir, monkeypatch):
    # KLE-002781, 23 Oct 2026: an "Ash Pearl" page for a Dusty Pink FY2024.
    url = "https://stockx.com/adidas-ozweego-ash-pearl"
    out = _shoe_lookup(cache_dir, monkeypatch, {
        "comps": [], "retail_new_eur": 94.6, "retail_raw": "US$110", "retail_url": url,
        "retail_style": "EE6999", "retail_cited": [url]})
    assert out["new_retail"] is None and out["tier"] == "B"
    assert "style EE6999" in out["retail_note"]


def test_a_shoe_retail_page_google_did_not_return_is_held(cache_dir, monkeypatch):
    url = "https://stockx.com/adidas-ozweego-dusty-pink"
    out = _shoe_lookup(cache_dir, monkeypatch, {
        "comps": [], "retail_new_eur": 94.6, "retail_raw": "US$110", "retail_url": url,
        "retail_style": "FY2024", "retail_cited": []})
    assert out["new_retail"] is None and out["tier"] == "B"


def test_the_shoe_retail_line_carries_the_style_code():
    text = ("ESTIMATE: €55\nCONFIDENCE: low\n"
            "RETAIL NEW: US$110 | https://stockx.com/adidas-ozweego-dusty-pink | FY2024\nREASON: x")
    res = RL.parse_search_answer(text, [])
    assert (res["retail_raw"], res["retail_style"]) == ("US$110", "FY2024")
    assert res["retail_url"] == "https://stockx.com/adidas-ozweego-dusty-pink"
    assert "Style code" in RL.SHOE_SEARCH_PROMPT and "Style code" not in RL.SEARCH_PROMPT


def test_item_from_payload_flags_footwear():
    rec = {"id": "p", "sku": "S", "category": "Sneakers", "subCategory": "Low Top"}
    assert RL.item_from_payload({"record": rec, "media": []})["footwear"] is True
    rec = {"id": "p", "sku": "S", "category": "Jeans", "subCategory": "Bootcut"}
    assert RL.item_from_payload({"record": rec, "media": []})["footwear"] is False


def test_summarise_without_a_search_is_not_evidence():
    s = RL.summarise({"condition": "Good"}, {"comps": [
        {"price_raw": "€50", "status": "sold", "url": "https://www.vinted.nl/items/1234567-a",
         "cited": ["https://www.vinted.nl/items/1234567-a"]}]}, {"queries": []})
    assert s["confidence"] == "no search" and s["n_comps"] == 0


# --------------------------------------------------------------------------- #
# Cache, ledger, product mapping
# --------------------------------------------------------------------------- #
def test_cache_key_only_for_specific_identifications():
    assert RL.cache_key({"brand": "Stone Island", "article_code": "7515-63051"}) == \
        "stone island|code|751563051"
    assert RL.cache_key({"brand": "Stone Island", "model": "Hoodie", "id_confidence": "high"}) is None
    assert RL.cache_key({"brand": "Stone Island", "model": "Ghost Piece Overshirt",
                         "id_confidence": "high"}) == "stone island|model|ghost piece overshirt"
    assert RL.cache_key({"brand": "Stone Island", "model": "Ghost Piece Overshirt",
                         "id_confidence": "medium"}) is None


def test_cache_round_trip_and_ledger(cache_dir):
    RL.cache_put("k", "KLE-1", {"comps": [{"price_eur": 1}]}, {"queries": ["q"], "search_count": 7})
    got = RL.cache_get("k")
    assert got["sku"] == "KLE-1" and got["res"]["comps"] == [{"price_eur": 1}]
    assert RL.cache_get(None) is None and RL.cache_get("other") is None
    RL.ledger_add(7)
    RL.ledger_add(3)
    assert RL.searches_this_month() == 10


def test_a_cached_entry_with_no_listings_is_a_miss(cache_dir):
    RL.cache_put("k", "KLE-001114", {"comps": []}, {"queries": ["q"], "search_count": 9})
    assert RL.cache_get("k") is None


def test_a_search_that_found_nothing_is_not_cached(cache_dir, monkeypatch):
    # KLE-001114, 15 Oct 2026: run 2 identified "Winter Edition 2011", found no
    # listings and cached that, so run 3 answered "no verified listings" from it.
    ident = {"brand": "Marni", "model": "Winter Edition 2011 Raw Denim Jeans",
             "id_confidence": "high"}
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (ident, None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        {"comps": []},
        {"queries": ["marni jeans"], "domains": [], "sources": [], "search_count": 6,
         "raw_answer": ""},
        None))
    out = RL.lookup({"sku": "KLE-001114", "images": ["https://r2/a.jpg"], "vnyx_price": 72.99})
    assert out["n_comps"] == 0
    assert RL.cache_key(ident) not in RL._read_json(RL._cache_dir() / "comp_cache.json")


def test_item_from_payload_prefers_the_raw_photograph():
    payload = {
        "record": {"id": "p1", "sku": "KLE-000356", "tenantName": "Klekt", "title": "Tee",
                   "brand": "A Bathing Ape", "condition": "As New", "priceAmount": 94.99,
                   "retailPriceAmount": 24.99, "images": ["https://r2/gallery.jpg"]},
        "media": [
            {"url": "https://r2/front-cut.png", "view": "FRONT", "processing": "BG_REMOVED", "isCurrent": True},
            {"url": "https://r2/front-raw.jpg", "view": "FRONT", "processing": "RAW", "isCurrent": False},
            {"url": "https://r2/back-cut.png", "view": "BACK", "processing": "BG_REMOVED", "isCurrent": True},
            {"url": "https://r2/label.jpg", "view": "LABEL", "processing": "RAW", "isCurrent": True},
            {"url": "https://r2/old-label.jpg", "view": "LABEL", "processing": "RAW", "isCurrent": False},
        ],
    }
    item = RL.item_from_payload(payload)
    assert item["images"] == ["https://r2/front-raw.jpg", "https://r2/back-cut.png"]
    assert item["label_images"] == ["https://r2/label.jpg"]
    assert (item["vnyx_price"], item["condition"], item["brand"]) == (94.99, "As New", "A Bathing Ape")


def test_lookup_without_a_key_says_so(monkeypatch):
    monkeypatch.setattr(RL, "api_key", lambda: "")
    out = RL.lookup({"sku": "X-1", "images": ["https://r2/a.jpg"]})
    assert "no Gemini key" in out["error"]


# --------------------------------------------------------------------------- #
# Confirming a counted listing's price on its page (15 Oct 2026)
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url


def _fake_page(monkeypatch, status, text):
    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, u): return _Resp(status, text, u)
    monkeypatch.setattr(RL.httpx, "Client", _Client)


EBAY = "https://www.ebay.it/itm/397975036614"


def test_page_price_reads_structured_data():
    assert RL.page_price('{"@type":"Offer","price":"402.43","priceCurrency":"EUR"}') == (402.43, "EUR")
    assert RL.page_price('{"priceCurrency":"USD","price":448.99}') == (448.99, "USD")
    assert RL.page_price("<html>no price here</html>") is None


def test_the_page_price_replaces_a_misread_one(monkeypatch):
    # KLE-000346: the model said €480, the page says €402.43.
    _fake_page(monkeypatch, 200, '<script>{"price":"402.43","priceCurrency":"EUR"}</script>')
    c = {"url": EBAY, "price_raw": "€480", "counted": True}
    assert RL.confirm_counted_prices([c]) == 1
    assert c["price_raw"] == "402.43 EUR" and c["price_from_model"] == "€480"
    assert c["price_confirm"]["ok"] is True


def test_a_page_that_shows_the_models_price_is_kept_as_is(monkeypatch):
    _fake_page(monkeypatch, 200, "<span>EUR 480,00</span>")
    c = {"url": EBAY, "price_raw": "€480", "counted": True}
    assert RL.confirm_counted_prices([c]) == 0
    assert c["price_raw"] == "€480" and c["price_confirm"]["ok"] is True


def test_a_page_with_no_price_drops_the_listing(monkeypatch):
    _fake_page(monkeypatch, 200, "<html>Dieses Angebot wurde beendet</html>")
    c = {"url": EBAY, "price_raw": "€480", "counted": True}
    RL.confirm_counted_prices([c])
    assert c["price_confirm"]["ok"] is False
    res = {"estimate_confidence": "medium",
           "comps": [{**c, "status": "listed", "cited": [EBAY]}]}
    s = RL.summarise({"condition": "As New"}, res, {"queries": ["q"]})
    assert s["n_comps"] == 0


def test_a_site_that_blocks_us_keeps_the_listing(monkeypatch):
    _fake_page(monkeypatch, 403, "Forbidden")
    c = {"url": "https://www.grailed.com/listings/123456789-x", "price_raw": "€260", "counted": True}
    RL.confirm_counted_prices([c])
    assert c["price_confirm"]["ok"] is None and c["price_raw"] == "€260"


# --------------------------------------------------------------------------- #
# Retail (new) price, from the same search answer (15 Oct 2026)
# --------------------------------------------------------------------------- #
RETAIL_ANSWER = ANSWER.replace(
    "RETAIL NEW: €290",
    "RETAIL NEW: 298 USD | https://www.supreme.com/products/stone-island-hooded-sweatshirt")


def test_the_retail_line_gives_amount_currency_url_and_its_citation():
    line_at = RETAIL_ANSWER.index("RETAIL NEW:")
    res = RL.parse_search_answer(RETAIL_ANSWER, [
        {"url": "https://r/retail", "start_index": line_at + 3, "end_index": line_at + 20}])
    assert res["retail_new_eur"] == round(298 * 0.86, 2)
    assert res["retail_raw"] == "298 USD"
    assert res["retail_url"] == "https://www.supreme.com/products/stone-island-hooded-sweatshirt"
    assert res["_retail_cite_raw"] == ["https://r/retail"]


def test_retail_unknown_proposes_nothing():
    res = RL.parse_search_answer(ANSWER.replace("RETAIL NEW: €290", "RETAIL NEW: unknown"), [])
    assert res["retail_new_eur"] is None
    out = RL.judge_retail(res, {}, 300.0)
    assert out["new_retail"] is None and "keep the current one" in out["retail_note"]


def test_a_cited_retail_is_proposed_and_flags_the_95_percent_rule():
    url = "https://www.supreme.com/products/x"
    out = RL.judge_retail({"retail_new_eur": 256.28, "retail_raw": "298 USD", "retail_url": url,
                           "retail_cited": [url]}, {}, 367.99)
    assert out["retail_verified"] == "cited" and out["new_retail"] == 256.28
    assert "PRICE.001" in out["retail_note"]


def test_an_unproven_retail_is_shown_not_proposed(monkeypatch):
    monkeypatch.setattr(RL, "confirm_price", lambda c: {"ok": None, "why": "blocked"})
    out = RL.judge_retail({"retail_new_eur": 256.28, "retail_url": "https://x.com/p/1",
                           "retail_cited": []}, {}, 200.0)
    assert out["new_retail"] is None and "not proven" in out["retail_note"]
