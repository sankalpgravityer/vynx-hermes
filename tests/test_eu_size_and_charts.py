"""The EU size and the size chart, 12 Oct 2026 (held-for-human fix plan).

124 products were held for "no EU size" and 124 for the size chart. The causes,
each pinned here:

  * vnyx-api's feed sends the EU size as stored — the literal "Unknown" — and
    DATA.010 counted it as present, so nothing was planned while the approval
    pre-flight refused it as blank;
  * sizes written as words ("Small", "X-Large") or with their length ("W28 L30")
    matched no chart;
  * a size the chart DATA lacks is printed on the chart IMAGE (Klekt's Men
    Bottoms data is W21-W40; its image prints S = 46, M = 48);
  * Klekt's "Accesories" chart lists S/M/L, so every men's top in a letter size
    matched it and "Men Uppers", and SIZE.010 called the choice ambiguous;
  * "Men Bottoms (auto)" has no chart image, so the product could never show one;
  * a guide "switch" to the guide already attached repaired nothing, forever.
"""
from __future__ import annotations

import pytest

from app import approval
from app.config import policy
from app.llm import size_chart
from app.rules import gate
from app.vnyx_client import to_snapshot

IMG_MB = "https://r2.example/size-charts/men-bottoms.png"
IMG_WB = "https://r2.example/size-charts/women-bottoms.png"

# Klekt's charts as stored on prod (trimmed), images as the catalog now sends them.
GUIDES = {
    "Accesories": {"sizes": ["S", "M", "L", "One Size"],
                   "euSizes": ["36", "38", "40", "One Size"], "images": []},
    "Men Bottoms": {"sizes": ["W28", "W30", "W32", "W34"],
                    "euSizes": ["36", "38", "40", "44"], "images": [IMG_MB]},
    "Men Bottoms (auto)": {"sizes": ["W28", "W30", "W32", "W34"],
                           "euSizes": ["44", "46", "48", "50"], "images": []},
    "Men Uppers": {"sizes": ["XS", "S", "M", "L", "XL"],
                   "euSizes": ["44", "46", "48", "50", "52"], "images": ["u.png"]},
    "Women Bottoms": {"sizes": ["W26", "W28", "S", "M", "L"],
                      "euSizes": ["34", "36", "S", "M", "L"], "images": [IMG_WB]},
    "Women Uppers": {"sizes": ["XS", "S", "M", "L"],
                     "euSizes": ["34", "36", "38", "40"], "images": ["wu.png"]},
}
TREE = {
    "Men": {"Bottoms": ["Shorts", "Joggers", "Jeans"], "Sweaters & Hoodies": ["Hoodie"],
            "T-Shirts & Polos": ["T-Shirt"]},
    "Women": {"Bottoms": ["Joggers"], "Skirts": ["Maxi Skirt"]},
}
CATALOG = {"categories": TREE, "sizingGuides": GUIDES, "brands": ["Nike"],
           "colors": ["Black"], "materials": ["Cotton"]}

# What the two chart images print (read once by app/llm/size_chart.py).
PRINTED = {
    IMG_MB: {"rows": [{"size": "XS", "eu": "44"}, {"size": "S", "eu": "46"},
                      {"size": "M", "eu": "48"}, {"size": "L", "eu": "50"}]},
    IMG_WB: {"rows": [{"size": "S", "eu": "36"}, {"size": "S", "eu": "38"},
                      {"size": "M", "eu": "40"}, {"size": "M", "eu": "42"}]},
}


@pytest.fixture
def images(monkeypatch):
    asked: list[str] = []

    def read(url, pol=None):
        asked.append(url)
        return PRINTED.get(url)

    monkeypatch.setattr(size_chart, "read_chart", read)
    return asked


def raw(**over):
    return {
        "id": "p1", "tenantId": "t1", "sku": "KLE-1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Bottoms", "subCategory": "Shorts",
        "size": "Small", "internationalSize": "Small", "euSize": "Unknown",
        "sizingGuide": "Men Bottoms", "mannequinType": "Men Bottom",
        "brand": "Nike", "color": "Black", "material": "Cotton", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1, "title": "Nike Shorts",
        **over,
    }


def eu_writes(gate_result):
    return [a for a in gate_result["repair_plan"]
            if a.get("field") == "eu_size" and a["kind"] == "set_property"]


# --------------------------------------------------------------------------- #
# spellings
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value,expected", [
    ("Small", ["Small", "S"]),
    ("X-Large", ["X-Large", "XL"]),
    ("2XL", ["2XL", "XXL"]),
    ("Medium", ["Medium", "M"]),
    ("M", ["M"]),
    ("W28 L30", ["W28 L30", "W28", "28"]),
    ("28/30", ["28/30", "W28", "28"]),
    ("32W 34L", ["32W 34L", "W32", "32"]),
    ("11-12", ["11-12"]),          # a kids' range is not a waist and a length
    ("", []),
])
def test_a_size_is_also_looked_up_in_its_chart_spelling(value, expected):
    assert gate.size_aliases(value) == expected


def test_a_word_size_and_its_letter_compare_equal():
    assert gate._size_key("Small") == gate._size_key("S")
    assert gate._size_key("2XL") == gate._size_key("XXL")


# --------------------------------------------------------------------------- #
# "Unknown" is no EU size
# --------------------------------------------------------------------------- #

def test_an_eu_size_stored_as_unknown_is_reported_empty():
    p = to_snapshot(raw(size="S", internationalSize="S", sizingGuide="Men Uppers",
                        category="T-Shirts & Polos", subCategory="T-Shirt"),
                    catalog=CATALOG)
    hits = [f for f in gate.check_gate(p, policy())
            if f.rule_id == "DATA.010" and "eu_size" in (f.fields or [])]
    assert hits and "Unknown" in hits[0].message


def test_another_field_holding_unknown_stays_data_001s_business():
    p = to_snapshot(raw(material="Unknown"), catalog=CATALOG)
    assert not [f for f in gate.check_gate(p, policy())
                if f.rule_id == "DATA.010" and "material" in (f.fields or [])]


def test_the_chart_data_answers_first(images):
    g = approval.run_gate(raw(size="S", internationalSize="S", sizingGuide="Men Uppers",
                              category="T-Shirts & Polos", subCategory="T-Shirt",
                              mannequinType="Men Top"), catalog=CATALOG)
    assert [a["value"] for a in eu_writes(g)] == ["46"]
    assert images == []  # the picture is not read when the data answers


# --------------------------------------------------------------------------- #
# the chart image
# --------------------------------------------------------------------------- #

def test_a_size_the_data_lacks_is_read_off_the_chart_image(images):
    g = approval.run_gate(raw(), catalog=CATALOG)
    writes = eu_writes(g)
    assert [a["value"] for a in writes] == ["46"]
    assert "chart image" in writes[0]["detail"]
    assert images == [IMG_MB]


def test_a_size_printed_twice_takes_the_value_another_chart_agrees_with(images):
    g = approval.run_gate(raw(masterCategory="Women", gender=["women"], subCategory="Joggers",
                              size="S", internationalSize="S", sizingGuide="Women Bottoms",
                              mannequinType="Bottom"), catalog=CATALOG)
    writes = eu_writes(g)
    assert [a["value"] for a in writes] == ["36"]
    assert "Women Uppers" in writes[0]["detail"]


def test_without_the_image_another_chart_of_the_gender_answers(monkeypatch):
    monkeypatch.setattr(size_chart, "read_chart", lambda url, pol=None: None)
    g = approval.run_gate(raw(size="M", internationalSize="M"), catalog=CATALOG)
    writes = eu_writes(g)
    assert [a["value"] for a in writes] == ["48"]
    assert "Men Uppers" in writes[0]["detail"]


def test_with_nothing_numeric_the_charts_own_letter_still_answers(monkeypatch):
    monkeypatch.setattr(size_chart, "read_chart", lambda url, pol=None: None)
    guides = {k: v for k, v in GUIDES.items() if k != "Women Uppers"}
    g = approval.run_gate(raw(masterCategory="Women", gender=["women"], subCategory="Joggers",
                              size="S", internationalSize="S", sizingGuide="Women Bottoms",
                              mannequinType="Bottom"),
                          catalog={**CATALOG, "sizingGuides": guides})
    assert [a["value"] for a in eu_writes(g)] == ["S"]


def test_a_size_no_chart_knows_is_left_to_a_person(images):
    g = approval.run_gate(raw(size="12YR", internationalSize="12YR"), catalog=CATALOG)
    assert not eu_writes(g)
    assert any(a["kind"] == "escalate" and a.get("field") == "eu_size"
               for a in g["repair_plan"])


def test_the_switch_can_be_turned_off(monkeypatch, images):
    pol = policy()
    monkeypatch.setitem(pol["sizing"], "eu_from_chart_image", False)
    g = approval.run_gate(raw(), catalog=CATALOG)
    # The image is not read; Men Uppers' data answers instead.
    assert images == []
    assert [a["value"] for a in eu_writes(g)] == ["46"]


def test_the_reader_keeps_only_rows_that_can_answer():
    cleaned = size_chart._clean({"is_size_chart": True, "rows": [
        {"size": " s ", "eu": "46"}, {"size": "M", "eu": None}, {"size": "", "eu": "48"},
        {"size": "L", "eu": "L"}]})
    assert cleaned == {"rows": [{"size": "S", "eu": "46"}]}
    assert size_chart._clean({"is_size_chart": False, "rows": [{"size": "S", "eu": "46"}]}) is None
    assert size_chart.eu_options(PRINTED[IMG_WB]["rows"], ["Small", "S"]) == ["36", "38"]


# --------------------------------------------------------------------------- #
# which chart
# --------------------------------------------------------------------------- #

def test_a_chart_named_for_the_gender_beats_one_named_for_neither():
    p = to_snapshot(raw(category="Sweaters & Hoodies", subCategory="Hoodie", size="L",
                        internationalSize="L", euSize="50", sizingGuide=None,
                        mannequinType="Men Top"), catalog=CATALOG)
    assert gate.candidate_guides(p, pol=policy()) == ["Men Uppers"]
    found = {f.rule_id: f for f in gate.check_gate(p, policy())}
    assert "SIZE.010" not in found
    assert found["SIZE.015"].detail["suggested"] == ["Men Uppers"]


def test_a_guide_with_no_chart_image_moves_to_the_one_that_answers_through_it():
    g = approval.run_gate(raw(size="32", internationalSize="32", euSize="48",
                              sizingGuide="Men Bottoms (auto)"), catalog=CATALOG)
    switch = [a for a in g["repair_plan"] if a.get("field") == "sizingGuide"]
    assert switch and switch[0]["value"] == "Men Bottoms" and switch[0]["reason"] == "SIZE.016"
    # Men Bottoms is checked against "(auto)" first (sizing.chart_preference), so
    # the EU size it carries stays right and nothing rewrites it.
    assert not eu_writes(g)


def test_no_image_rule_stays_silent_when_the_catalog_says_nothing_about_images():
    bare = {k: {"sizes": v["sizes"], "euSizes": v["euSizes"]} for k, v in GUIDES.items()}
    p = to_snapshot(raw(size="32", internationalSize="32", euSize="48",
                        sizingGuide="Men Bottoms (auto)"),
                    catalog={**CATALOG, "sizingGuides": bare})
    assert "SIZE.016" not in {f.rule_id for f in gate.check_gate(p, policy())}


def test_a_switch_to_the_guide_already_attached_escalates_instead():
    g = approval.run_gate(raw(masterCategory="Women", gender=["women"], category="Skirts",
                              subCategory="Maxi Skirt", size="XXXL", internationalSize="XXXL",
                              euSize="46", sizingGuide="Women Uppers", mannequinType="Bottom"),
                          catalog=CATALOG)
    guide = [a for a in g["repair_plan"] if a.get("field") == "sizingGuide"]
    assert guide and all(a["kind"] == "escalate" for a in guide)
