"""The chain's `shoe model` step source (26 Oct 2026): the resale identifier's
model line for a FOOTWEAR product — brand stripped, written only at the
configured confidence, never asked about anything that is not a shoe."""

from app.pricing import resale_lookup as rl

PAYLOAD = {"record": {"brand": "Nike", "masterCategory": "Men", "category": "Shoes",
                      "subCategory": "Running"},
           "media": [{"view": "FRONT", "url": "https://x/f.jpg", "processing": "RAW",
                      "isCurrent": True}]}


def fake(answer):
    def identify(item, key, usage):
        return answer, None
    return identify


def test_brand_is_stripped_and_high_confidence_is_accepted(monkeypatch):
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    monkeypatch.setattr(rl, "identify", fake({"model_name": "Nike Air Max 90",
                                              "id_confidence": "high", "article_code": "CN8490"}))
    got = rl.shoe_model(PAYLOAD, "high")
    assert (got["model_name"], got["accepted"], got["article_code"]) == ("Air Max 90", True, "CN8490")


def test_below_min_confidence_is_reported_not_accepted(monkeypatch):
    monkeypatch.setattr(rl, "api_key", lambda: "k")
    monkeypatch.setattr(rl, "identify", fake({"model_name": "Dunk Low", "id_confidence": "medium"}))
    assert rl.shoe_model(PAYLOAD, "high")["accepted"] is False
    assert rl.shoe_model(PAYLOAD, "medium")["accepted"] is True


def test_not_footwear_never_calls_the_model(monkeypatch):
    def boom(*a):
        raise AssertionError("identify must not be called")
    monkeypatch.setattr(rl, "identify", boom)
    shirt = {"record": {**PAYLOAD["record"], "category": "Tops", "subCategory": "T-Shirts"},
             "media": PAYLOAD["media"]}
    assert rl.shoe_model(shirt)["error"] == "not footwear"
