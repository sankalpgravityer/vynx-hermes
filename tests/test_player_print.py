"""The chain's `player` step source (27 Oct 2026): the player lettering read off
a jersey's photographs — printed name and number only, at the confidence floor,
nothing invented for a plain team shirt."""

from app.llm import player_print as pp


def setup(monkeypatch, answer):
    monkeypatch.setattr(pp.cache, "get", lambda *a, **k: None)
    monkeypatch.setattr(pp.cache, "put", lambda *a, **k: None)
    monkeypatch.setattr(pp, "_fetch", lambda urls, cap=2: [(b"x", "image/jpeg") for _ in urls])
    monkeypatch.setattr(pp, "_gemini", lambda images: (answer, "ok"))


def test_reads_and_cleans_name_and_number(monkeypatch):
    setup(monkeypatch, {"is_jersey": True, "player_name": "HARDEN", "player_name_confidence": 99,
                        "player_number": "#13", "player_number_confidence": 98, "team": "Rockets"})
    got = pp.read(["https://x/back.jpg", "https://x/front.jpg"])
    assert (got["player_name"], got["player_number"], got["provider"]) == ("Harden", "13", "gemini")


def test_below_the_floor_is_rejected_not_returned(monkeypatch):
    setup(monkeypatch, {"is_jersey": True, "player_name": "HARDEN", "player_name_confidence": 60,
                        "player_number": "13", "player_number_confidence": 95, "team": None})
    got = pp.read(["https://x/back.jpg"], min_confidence=85)
    assert got["player_name"] is None and got["rejected"] == {"player_name": 60}
    assert got["player_number"] == "13"


def test_team_shirt_without_lettering_returns_nothing(monkeypatch):
    setup(monkeypatch, {"is_jersey": True, "player_name": None, "player_name_confidence": 0,
                        "player_number": None, "player_number_confidence": 0, "team": "Ajax"})
    got = pp.read(["https://x/back.jpg"])
    assert got["player_name"] is None and got["player_number"] is None and got["team"] == "Ajax"


def test_no_photo_is_an_error_not_a_call(monkeypatch):
    monkeypatch.setattr(pp, "_gemini", lambda images: (_ for _ in ()).throw(AssertionError("called")))
    assert pp.read([])["error"]


def test_name_casing():
    assert pp._clean_name("LEBRON  JAMES") == "Lebron James"
    assert pp._clean_name("O'Neal") == "O'Neal"


def test_lettering_on_a_non_jersey_is_ignored(monkeypatch):
    setup(monkeypatch, {"is_jersey": False, "player_name": "BROOKLYN", "player_name_confidence": 95,
                        "player_number": "77", "player_number_confidence": 95, "team": None})
    got = pp.read(["https://x/front.jpg"])
    assert got["player_name"] is None and got["player_number"] is None
