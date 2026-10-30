"""Search by photo (29 Oct 2026, Phase 2): eBay search_by_image and Google
Lens via SerpApi, and how their listings enter the lookup."""
from __future__ import annotations

import json
import re

import pytest

from app.pricing import image_search as IS
from app.pricing import resale_lookup as RL
from app.pricing import resale_rules as R


class FakeResp:
    def __init__(self, status: int, body: dict | str):
        self.status_code = status
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body


class FakeHttp:
    """A make_client() stand-in that answers by URL."""
    routes: dict[str, FakeResp] = {}
    calls: list[tuple[str, dict]] = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _hit(self, url, **kw):
        FakeHttp.calls.append((url, kw))
        for k, v in FakeHttp.routes.items():
            if k in url:
                return v
        return FakeResp(404, "no route")

    def post(self, url, **kw):
        return self._hit(url, **kw)

    def get(self, url, **kw):
        return self._hit(url, **kw)


EBAY_ITEM = {
    "itemId": "v1|326956899129|0", "legacyItemId": "326956899129",
    "title": "Columbia Lodge Sherpa Pullover, Snap Button, Stone Green/ Beach, S",
    "price": {"value": "18.00", "currency": "USD"}, "condition": "Pre-owned - Excellent",
    "itemLocation": {"country": "US"},
    "itemWebUrl": "https://www.ebay.com/itm/326956899129?hash=item4c",
    "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l225.jpg"},
}
LENS = {
    "visual_matches": [
        {"title": "Columbia Lodge Sherpa Pullover in Stone Green Beach NWT", "source": "Pinterest",
         "link": "https://es.pinterest.com/pin/1", "thumbnail": "https://encrypted-tbn0.gstatic.com/a"},
        {"title": "Vintage Columbia Women's 2XL Green 1/4 Snap Fleece Pullover", "source": "Grailed",
         "link": "https://www.grailed.com/listings/98592836-vintage-columbia", "thumbnail": "https://t/b"},
        {"title": "NWT Columbia Lodge Sherpa Pullover, Snap Button, Stone Green", "source": "eBay",
         "link": "https://www.ebay.com/itm/167093907232", "thumbnail": "https://t/c",
         "price": {"value": "€80*", "extracted_value": 80.0, "currency": "€"}},
        {"title": "Columbia Lodge Sherpa Pullover Stone Green", "source": "Columbia", "link": "https://columbia.com/p/1",
         "thumbnail": "https://t/d"},
    ]
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("EBAY_PRICE_CLIENT_ID", "id")
    monkeypatch.setenv("EBAY_PRICE_CLIENT_SECRET", "secret")
    monkeypatch.setenv("SERPAPI_KEY", "serp")
    monkeypatch.setattr(IS, "make_client", FakeHttp)
    IS._token.update(value=None, expires=0.0)
    FakeHttp.calls = []
    FakeHttp.routes = {
        "oauth2/token": FakeResp(200, {"access_token": "tok", "expires_in": 7200}),
        "search_by_image": FakeResp(200, {"itemSummaries": [EBAY_ITEM]}),
        "serpapi.com": FakeResp(200, LENS),
        "get_item_by_legacy_id": FakeResp(200, {**EBAY_ITEM, "legacyItemId": "167093907232",
                                                 "itemWebUrl": "https://www.ebay.com/itm/167093907232"}),
    }
    R.set_fx_rates({"EUR": 1.0, "USD": 0.86, "GBP": 1.16})
    yield
    R.set_fx_rates(None)


def test_ebay_search_by_image_returns_listings_with_photos():
    comps, err = IS.ebay_search_by_image(b"jpg", "EBAY_US")
    assert err is None and len(comps) == 1
    c = comps[0]
    assert c["url"] == "https://www.ebay.com/itm/326956899129"     # tracking stripped
    assert (c["price_raw"], c["location"], c["source"]) == ("18.00 USD", "US", "ebay")
    assert c["image"].startswith("https://i.ebayimg.com/") and c["item_id"] == "326956899129"
    # The token was fetched once and sent.
    assert sum(1 for u, _ in FakeHttp.calls if "oauth2/token" in u) == 1
    hdr = next(kw for u, kw in FakeHttp.calls if "search_by_image" in u)["headers"]
    assert hdr["Authorization"] == "Bearer tok" and hdr["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_missing_ebay_keys_is_a_note_not_an_error(monkeypatch):
    monkeypatch.delenv("EBAY_PRICE_CLIENT_ID")
    comps, err = IS.ebay_search_by_image(b"jpg", "EBAY_DE")
    assert comps == [] and "no eBay keys" in err


def test_lens_keeps_resale_sites_and_every_title():
    comps, titles, err = IS.lens_search("https://r2/front.png")
    assert err is None and len(titles) == 4
    assert [c["platform"] for c in comps] == ["Grailed", "eBay"]     # Pinterest, Columbia out
    assert comps[1]["price_raw"] == "€80" and comps[1]["item_id"] == "167093907232"


def test_hints_are_the_names_the_matches_agree_on():
    hints = IS.hints_from_titles([m["title"] for m in LENS["visual_matches"]] + [EBAY_ITEM["title"]],
                                 brand="Columbia")
    assert hints[0] == "lodge sherpa pullover"


def test_image_search_merges_and_dedupes(monkeypatch):
    out = IS.image_search(b"jpg", "https://r2/front.png", "Columbia",
                          {"ebay_marketplaces": ["EBAY_DE", "EBAY_US"]})
    assert out["ebay_calls"] == 2 and out["lens_calls"] == 1
    urls = [c["url"] for c in out["comps"]]
    assert urls.count("https://www.ebay.com/itm/326956899129") == 1    # DE and US: one copy
    assert "https://www.grailed.com/listings/98592836-vintage-columbia" in urls
    assert out["hints"][0] == "lodge sherpa pullover"
    assert out["notes"] == []


def test_api_listings_count_as_verified_and_the_judge_sees_their_photos(monkeypatch, tmp_path):
    ident = {"brand": "Columbia", "model": "West Bend Half Zip", "id_confidence": "medium",
             "colourway": "olive, cream yoke, navy pocket", "features": ["half zip"]}
    item = {"sku": "BOA-007078", "images": ["https://r2/front.png"], "condition": "As New",
            "vnyx_price": 36.99, "_photos": {"https://r2/front.png": b"ourjpg"}}
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: (dict(ident), None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        {"comps": [], "estimate_confidence": "low"},
        {"queries": ["columbia west bend"], "domains": [], "sources": [], "search_count": 3,
         "raw_answer": ""}, None))
    monkeypatch.setattr(RL, "fetch_all", lambda urls, **k: {u: b"listingjpg" for u in urls})
    sent: dict = {}

    def fake_post(url, body, key, timeout_s, tries=3):
        parts = body["contents"][0]["parts"]
        sent["text"] = parts[0]["text"]
        sent["images"] = sum(1 for p in parts if "inline_data" in p)
        n = sent["text"].count("\n1. title") + sent["text"].count("\n2. title") + sent["text"].count("\n3. title")
        reply = [{"n": i + 1, "verdict": "same", "reason": "photo shows the Lodge Sherpa"} for i in range(n)]
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}],
                "usageMetadata": {}}, None

    monkeypatch.setattr(RL, "_post", fake_post)
    out = RL.lookup(item, fresh=True)
    # The search prompt and the judge both got the image search's name for it.
    assert "lodge sherpa pullover" in sent["text"]
    assert sent["images"] >= 2                                        # ours + listing photos
    assert out["image_search"]["on"] and out["image_search"]["ebay"] == 1
    verified = {c["verified"] for c in out["comps"] if c.get("counted")}
    assert verified <= {"ebay", "lens", "lens+ebay"} and verified
    assert out["n_same"] == out["n_comps"] >= 2
    assert out["price_source"] == "resale sites"


def test_the_image_search_flag_turns_it_off(monkeypatch, tmp_path):
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: ({"brand": "X", "id_confidence": "low"}, None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        {"comps": []}, {"queries": ["x"], "domains": [], "sources": [], "search_count": 1,
                        "raw_answer": ""}, None))
    monkeypatch.setattr(IS, "image_search", lambda *a, **k: pytest.fail("image search must be off"))
    out = RL.lookup({"sku": "S", "images": ["https://r2/a.jpg"], "_photos": {"https://r2/a.jpg": b"x"}},
                    fresh=True, image=False)
    assert out["image_search"]["on"] is False


def test_region_from_ebay_country_codes():
    assert R.region_of("https://www.ebay.co.uk/itm/1", "US") == "US"     # a US seller on ebay.co.uk
    assert R.region_of("https://www.ebay.com/itm/1", "GB") == "UK"
    assert R.region_of("https://www.ebay.com/itm/1", "DE") == "EU"
    assert R.region_of("https://www.ebay.de/itm/1", "") == "EU"


def test_the_judge_works_through_every_candidate_in_batches(monkeypatch):
    calls: list[int] = []

    def fake_post(url, body, key, timeout_s, tries=3):
        text = body["contents"][0]["parts"][0]["text"]
        n = len(re.findall(r"(?m)^\d+\. title:", text))
        calls.append(n)
        reply = [{"n": i + 1, "verdict": "same", "reason": "ok"} for i in range(n)]
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}],
                "usageMetadata": {}}, None

    monkeypatch.setattr(RL, "_post", fake_post)
    comps = [{"url": f"https://www.ebay.com/itm/{100000000 + i}", "title": f"t{i}", "price_raw": "€10",
              "status": "listed", "source": "ebay", "cited": []} for i in range(23)]
    RL.summarise({"condition": "Good"}, {"comps": comps},
                 {"queries": [], "image_searches": {"ebay": 1}, "domains": [], "sources": []})
    assert RL.judge_matches({"brand": "X"}, {}, comps, "k", RL.Usage()) == 23
    assert sorted(calls) == [5, 6, 6, 6]          # batches of JUDGE_BATCH, run at once
    assert all(c["match"]["verdict"] == "same" for c in comps)


def test_google_is_skipped_when_the_image_search_found_enough(monkeypatch, tmp_path):
    """Three same-item listings from the photo sources: no text search (time, cost)."""
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: ({"brand": "Columbia", "id_confidence": "medium"}, None))
    comps = [{"url": f"https://www.ebay.com/itm/{300000000 + i}", "title": "Columbia Lodge Sherpa",
              "price_raw": "€30", "status": "listed", "source": "ebay", "cited": [], "location": "US"}
             for i in range(3)]
    monkeypatch.setattr(IS, "image_search", lambda *a, **k: {"comps": comps, "hints": ["lodge sherpa"],
                                                              "ebay_calls": 3, "lens_calls": 1, "notes": []})
    monkeypatch.setattr(RL, "search_round", lambda *a: pytest.fail("Google must not run"))
    monkeypatch.setattr(RL, "mark_own_listings", lambda item, comps: 0)

    def fake_judge(ident, item, cs, key, usage):
        for c in cs:
            c["match"] = {"verdict": "same", "reason": "photo"}
        return len(cs)

    monkeypatch.setattr(RL, "judge_matches", fake_judge)
    out = RL.lookup({"sku": "S", "images": ["https://r2/a.jpg"], "_photos": {"https://r2/a.jpg": b"x"},
                     "vnyx_price": 36.99, "condition": "Good"}, fresh=True)
    assert out["text_search"] == {"ran": False, "same_from_images": 3, "enough": 3}
    assert out["n_comps"] == 3 and out["searches"] == 0
    assert set(out["timings"]) >= {"identify", "image_search", "judge"}


def test_google_runs_when_the_image_search_came_up_short(monkeypatch, tmp_path):
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: ({"brand": "Columbia", "id_confidence": "medium"}, None))
    own = {"url": "https://www.ebay.de/itm/1", "title": "ours", "price_raw": "€85.99", "status": "listed",
           "source": "ebay", "cited": [], "seller": "boas-savefashionforgood", "location": "NL"}
    monkeypatch.setattr(IS, "image_search", lambda *a, **k: {"comps": [own], "hints": [], "ebay_calls": 3,
                                                              "lens_calls": 1, "notes": []})
    ran: list[str] = []

    def search(prompt, key, usage):
        ran.append(prompt[:20])
        usage.searches += 4
        return ({"comps": [{"url": "https://www.vinted.nl/items/1234567-a", "price_raw": "€60", "status": "sold",
                            "title": "Helly Hansen parka", "cited": ["https://www.vinted.nl/items/1234567-a"]}]},
                {"queries": ["hh parka"], "domains": [], "sources": [], "search_count": 4, "raw_answer": ""}, None)

    monkeypatch.setattr(RL, "search_round", search)
    monkeypatch.setattr(RL, "judge_matches", lambda ident, item, cs, key, usage: 0)
    monkeypatch.setattr(RL, "fetch_all", lambda urls, **k: {u: None for u in urls})
    item = {"sku": "BOA-006463", "tenant": "BOAS", "images": ["https://r2/a.jpg"],
            "_photos": {"https://r2/a.jpg": b"x"}, "vnyx_price": 85.20, "condition": "As New"}
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path), "page_check": False,
                                               "confirm_prices": False,
                                               "own_sellers": {"BOAS": ["boas-savefashionforgood"]}})
    out = RL.lookup(item, fresh=True)
    assert out["n_own"] == 1                                   # our listing: set aside
    assert out["text_search"]["ran"] is True and out["text_search"]["same_from_images"] == 0
    assert len(ran) >= 1 and out["searches"] >= 4
    assert [c["url"] for c in out["comps"] if c.get("counted")] == ["https://www.vinted.nl/items/1234567-a"]
    assert "timings" in out and "text_search" in out["timings"]


def test_without_keys_the_text_search_is_the_whole_lookup(monkeypatch, tmp_path):
    """The user removes EBAY_PRICE_* and SERPAPI_KEY from .env: no image search,
    no note about it, the Google search runs as before 29 Oct."""
    for k in ("EBAY_PRICE_CLIENT_ID", "EBAY_PRICE_CLIENT_SECRET", "SERPAPI_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(tmp_path),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda it, key, usage: ({"brand": "adidas", "id_confidence": "medium"}, None))
    monkeypatch.setattr(IS, "image_search", lambda *a, **k: pytest.fail("no keys: no image search"))
    monkeypatch.setattr(RL, "judge_matches", lambda ident, item, cs, key, usage: 0)
    monkeypatch.setattr(RL, "mark_own_listings", lambda item, comps: 0)
    ran: list[str] = []

    def search(prompt, key, usage):
        ran.append("google")
        usage.searches += 5
        u = "https://www.kleinanzeigen.de/s-anzeige/adidas-hoodie/2870020119-160-2049"
        return ({"comps": [{"url": u, "price_raw": "€20", "status": "listed", "title": "adidas hoodie",
                            "cited": [u]}]},
                {"queries": ["adidas hoodie"], "domains": [], "sources": [], "search_count": 5,
                 "raw_answer": ""}, None)

    monkeypatch.setattr(RL, "search_round", search)
    out = RL.lookup({"sku": "S", "images": ["https://r2/a.jpg"], "_photos": {"https://r2/a.jpg": b"x"},
                     "vnyx_price": 27.99, "condition": "Good"}, fresh=True)
    assert out["image_search"]["on"] is False and out["image_search"]["notes"] == []
    assert ran == ["google", "google"]            # round 1, then the EU round (1 listing < 2)
    assert out["n_comps"] == 1 and out["text_search"]["ran"] is True
