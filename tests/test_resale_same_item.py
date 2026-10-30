"""The same-item judge (29 Oct 2026, Phase 1 of the reprice plan).

BOA-007078: four "verified" listings, each a different Columbia fleece, priced
the item at €9.99. A listing now counts only when the judge does not call it
a different item, and only "same" listings can cut the price.
"""
from __future__ import annotations

import json

import pytest

from app.pricing import resale_lookup as RL
from app.pricing import resale_rules as R

IDENT = {"brand": "Columbia", "model": "West Bend Sherpa Half Zip", "article_code": "",
         "id_confidence": "medium", "garment_type": "sherpa fleece half-zip pullover",
         "colourway": "beige sherpa yoke, olive fleece body, navy chest pocket",
         "features": ["half zip", "navy nylon chest pocket", "colour-blocked sherpa"]}
ITEM = {"sku": "BOA-007078", "condition": "As New", "vnyx_price": 36.99, "gender": "Men",
        "size": "L", "images": ["https://r2/front.png"]}


def comp(url: str, price: str, title: str, **extra: object) -> dict:
    return {"url": url, "price_raw": price, "status": "listed", "title": title,
            "cited": [url], **extra}


def _comps(prices: tuple[str, str, str] = ("€10", "€45", "$17")) -> list[dict]:
    return [
        comp("https://www.kleinanzeigen.de/s-anzeige/columbia-teddy-pullover-gr-s/2866946639-154-2895",
             prices[0], "Columbia Teddy Pullover Gr. S", colour="cream", size="S"),
        comp("https://www.kleinanzeigen.de/s-anzeige/columbia-west-bend-gr-xl/3493451509-154-3847",
             prices[1], "Columbia Teddyfleece Jacke West Bend Gr. XL", colour="beige/green", size="XL"),
        comp("https://www.ebay.com/itm/186905775479", prices[2], "Columbia fleece pullover mens"),
    ]


GROUNDING = {"queries": ["columbia west bend sherpa"], "domains": [], "sources": []}


@pytest.fixture(autouse=True)
def _fx():
    R.set_fx_rates({"EUR": 1.0, "USD": 0.86, "GBP": 1.16})
    yield
    R.set_fx_rates(None)


@pytest.fixture
def cache_dir(tmp_path):
    return tmp_path


def test_the_judge_reads_one_verdict_per_listing(monkeypatch):
    sent: dict = {}

    def fake_post(url, body, key, timeout_s, tries=3):
        sent["prompt"] = body["contents"][0]["parts"][0]["text"]
        reply = [{"n": 1, "verdict": "different", "reason": "another model and colourway"},
                 {"n": 2, "verdict": "same", "reason": "names West Bend, colours match"},
                 {"n": 3, "verdict": "similar", "reason": "only the brand and garment type"}]
        return {"candidates": [{"content": {"parts": [{"text": json.dumps(reply)}]}}],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5}}, None

    monkeypatch.setattr(RL, "_post", fake_post)
    res = {"comps": _comps()}
    RL.summarise(ITEM, res, GROUNDING)                       # marks the candidates
    usage = RL.Usage()
    assert RL.judge_matches(IDENT, ITEM, res["comps"], "k", usage) == 3
    assert [c["match"]["verdict"] for c in res["comps"]] == ["different", "same", "similar"]
    assert usage.tokens_in == 10
    # Our photos' evidence and each listing's details went to the judge.
    assert "navy chest pocket" in sent["prompt"] and "Gr. S" in sent["prompt"]
    assert "1. title: Columbia Teddy Pullover" in sent["prompt"]


def test_a_failed_judge_leaves_the_listings_counted(monkeypatch):
    monkeypatch.setattr(RL, "_post", lambda *a, **k: (None, "HTTP 503"))
    res = {"comps": _comps()}
    RL.summarise(ITEM, res, GROUNDING)
    assert RL.judge_matches(IDENT, ITEM, res["comps"], "k", RL.Usage()) == 0
    s = RL.summarise(ITEM, res, GROUNDING)
    assert s["n_comps"] == 3 and s["n_same"] == 3          # unchecked = as before


def test_different_is_dropped_and_similar_cannot_cut():
    # Two "similar" asking prices well under €36.99 — a cut, were they the item.
    res = {"comps": _comps(("€10", "€20", "$24")), "estimate_confidence": "high"}
    res["comps"][0]["match"] = {"verdict": "different", "reason": "another model"}
    res["comps"][1]["match"] = {"verdict": "similar", "reason": "colourway not confirmed"}
    res["comps"][2]["match"] = {"verdict": "similar", "reason": "only brand and type"}
    s = RL.summarise(ITEM, res, GROUNDING)
    assert [c["counted"] for c in res["comps"]] == [False, True, True]
    assert (s["n_comps"], s["n_same"], s["n_different"]) == (2, 0, 1)
    # €20 and $24 asking -> below €36.99, but nothing confirmed the item: kept.
    assert s["suggested_eur"] == 36.99
    assert "0 confirmed the same item" in s["price_note"]


def test_three_same_listings_still_cut_and_confidence_high_needs_two_same():
    # Three SOLD at €28-30: the same item sells for less than €36.99.
    res = {"comps": _comps(("€30", "€28", "$32")), "estimate_confidence": "high"}
    for c in res["comps"]:
        c["status"] = "sold"
        c["match"] = {"verdict": "same", "reason": "names the model and colourway"}
    s = RL.summarise(ITEM, res, GROUNDING)
    assert s["n_same"] == 3 and s["confidence"] == "high"
    assert s["suggested_eur"] < 36.99                       # a real cut, three same items
    res["comps"][1]["match"]["verdict"] = res["comps"][2]["match"]["verdict"] = "similar"
    s = RL.summarise(ITEM, res, GROUNDING)
    assert s["n_same"] == 1 and s["confidence"] == "medium"
    assert s["suggested_eur"] == 36.99


def test_the_lookup_judges_candidates_and_the_flag_turns_it_off(cache_dir, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (dict(IDENT), None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        {"comps": _comps(), "estimate_confidence": "high"},
        {**GROUNDING, "search_count": 4, "raw_answer": ""}, None))

    def fake_judge(ident, item, comps, key, usage):
        calls.append("judged")
        for c in comps:
            c["match"] = {"verdict": "different", "reason": "another Columbia model"}
        return len(comps)

    monkeypatch.setattr(RL, "judge_matches", fake_judge)
    out = RL.lookup(dict(ITEM), fresh=True)
    assert calls and out["same_item_check"] is True
    assert out["n_comps"] == 0 and out["n_different"] == 3
    assert out["new_price"] is None                          # nothing left to price from
    assert out["decision"].startswith("No new price")

    calls.clear()
    out = RL.lookup(dict(ITEM), fresh=True, same_item=False)
    assert not calls and out["same_item_check"] is False
    assert out["n_comps"] == 3 and out["n_same"] is None


def test_a_shoe_is_not_judged(cache_dir, monkeypatch):
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (
        {"brand": "Nike", "model": "Dunk Low", "article_code": "DD1391-100", "id_confidence": "high"}, None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        {"comps": []}, {**GROUNDING, "search_count": 2, "raw_answer": ""}, None))
    monkeypatch.setattr(RL, "judge_matches", lambda *a: pytest.fail("a shoe must not be judged"))
    out = RL.lookup({**ITEM, "footwear": True}, fresh=True)
    assert out["same_item_check"] is False


def test_thredup_is_named_in_both_rounds_and_searched_outright_for_womens_and_kids(cache_dir, monkeypatch):
    prompts: list[str] = []
    monkeypatch.setattr(RL, "config", lambda: {**RL.DEFAULTS, "cache_dir": str(cache_dir),
                                               "page_check": False, "confirm_prices": False})
    monkeypatch.setattr(RL, "api_key", lambda: "k")
    monkeypatch.setattr(RL, "identify", lambda item, key, usage: (dict(IDENT), None))
    monkeypatch.setattr(RL, "search_round", lambda p, key, usage: (
        prompts.append(p) or {"comps": []},
        {**GROUNDING, "search_count": 2, "raw_answer": ""}, None))
    RL.lookup({**ITEM, "gender": '["women"]'}, fresh=True, image=False)
    assert len(prompts) == 2 and all("thredUP" in p for p in prompts)         # both rounds
    assert all("site:thredup.com" in p for p in prompts)                       # women's: outright
    prompts.clear()
    RL.lookup({**ITEM, "gender": "Men"}, fresh=True, image=False)
    assert all("thredUP" in p and "site:thredup.com" not in p for p in prompts)
    assert RL.thredup_target({"gender": '["men", "women"]'}) and RL.thredup_target({"gender": "Girls"})
    assert not RL.thredup_target({"gender": "men"}) and not RL.thredup_target({})


def test_invented_urls_are_not_verified_and_zalando_counts_only_pre_owned():
    # BOA-006489: four eBay links, all "/175000000000", on a site Google cited.
    fake = [comp(f"https://www.ebay.com/itm/Levis-Hoodie-{i}/175000000000", "€10", f"Levis hoodie {i}")
            for i in range(3)]
    real = comp("https://www.ebay.com/itm/196241212706", "€14", "Levis hoodie real")
    zal_new = comp("https://www.zalando.nl/levis-graphic-hoodie-black-le222s0ab-q11.html", "€59.95",
                   "Levi's Graphic Hoodie - Zalando")
    zal_used = comp("https://www.zalando.nl/pre-owned/levis-graphic-hoodie-black-123.html", "€25",
                    "Levi's hoodie - Pre-owned - Zalando")
    for c in fake + [real, zal_new, zal_used]:      # Google cited eBay's category page, as it did
        c["cited"] = ["https://www.ebay.com/b/Levis-Hoodies-for-Men/155183/bn_55874229"] \
            if "ebay" in c["url"] else [c["url"]]
    res = {"comps": fake + [real, zal_new, zal_used]}
    s = RL.summarise(ITEM, res, GROUNDING)
    assert [c["verified"] for c in fake] == ["none"] * 3 and all(c["invented"] for c in fake)
    assert real["verified"] == "site" and real["unconfirmed"] and not real["counted"]   # since 29 Oct
    assert zal_new["resale_site"] is False and not zal_new["counted"]      # new stock: retail only
    assert zal_used["resale_site"] is True and zal_used["counted"]
    assert s["n_comps"] == 1
    assert RL._looks_invented("https://www.ebay.de/itm/111111111111", []) is True
    assert RL._looks_invented("https://www.ebay.de/itm/237123499945", []) is False


def test_one_cited_listing_stands_in_for_one_mistyped_url_only():
    cited = "https://www.ebay.com/itm/196241212706"
    # As in the real run: three "listings", one path for all (/175000000000).
    comps = [comp(f"https://www.ebay.com/itm/Levis-Hoodie-{i}/175000000000", "€10", f"hoodie {i}",
                  cited=[cited]) for i in range(3)]
    s = RL.summarise(ITEM, {"comps": comps}, GROUNDING)
    assert [c["verified"] for c in comps] == ["exact", "none", "none"]
    assert comps[0]["url"] == cited and s["n_comps"] == 1


def test_ebay_links_need_a_real_item_number_and_site_only_does_not_count():
    # BOA-006950: a slug URL rebuilt from a result title ("...-Blau-156810"), a
    # Google redirect token in an eBay path, and the one real, cited listing.
    dead = comp("https://www.ebay.de/itm/Patagonia-Jacke-Skyforest-Parka-Damen-Gr-M-DE-38-Blau-156810",
                "€69.99", "Patagonia Jacke Skyforest Parka Damen Gr. M", cited=["https://www.ebay.de/b/Patagonia/1"])
    token = comp("https://www.ebay.co.uk/itm/AUZIYQFyPu84UhaXLe7avI_MaK5-sKcdN8hKk6ZNC7Rg8uDVBkw2RuAIiqqMk",
                 "€59.81", "Patagonia Skyforest", cited=["https://www.ebay.co.uk/b/Patagonia/1"])
    site_only = comp("https://www.ebay.co.uk/itm/175629052833", "€50", "Patagonia Skyforest Parka",
                     cited=["https://www.ebay.co.uk/b/Patagonia/1"])
    real = comp("https://www.ebay.co.uk/itm/175629052832", "£73", "Patagonia Skyforest Parka Womens M",
                cited=["https://www.ebay.co.uk/itm/175629052832"])
    res = {"comps": [dead, token, site_only, real]}
    s = RL.summarise(ITEM, res, GROUNDING)
    assert dead["invented"] and token["invented"] and dead["verified"] == "none"
    assert site_only["verified"] == "site" and site_only["unconfirmed"] and not site_only["counted"]
    assert real["verified"] == "exact" and real["counted"]
    assert s["n_comps"] == 1
    # The judge's wording: another colour of the same model is similar, not different.
    assert "NOT merely another colour" in RL.MATCH_PROMPT
