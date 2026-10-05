"""Garment TYPE within a family — category and subcategory only (1 Oct 2026).

Three Levi's denim SHORTS went live on BOAS as `Bottoms > Jeans` / `Trousers`.
The image gate read "shorts" on every one of them and passed, because shorts,
jeans and trousers are all the `bottoms` family; and the reconcile step WROTE
"Jeans" on two of them from the title ("…511 Light Wash Jeans W34 L34") over a
`properties` copy that said Shorts.

Pinned here: the type lookup, the gate refusing a type mismatch, the picture's
pick written at the single-field floor when the TYPE differs, and the
photographs overruling a subcategory taken from the title's words.
Cold: no database, no network, no model.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import approval  # noqa: E402
from app.config import policy  # noqa: E402
from app.imaging import quality_gate as qg  # noqa: E402
from app.models import (  # noqa: E402
    Action, Evidence, ProductSnapshot, TaxonomySuggestion, TenantCatalog, VisionAudit,
)
from app.resolver import resolve_taxonomy  # noqa: E402

POL = policy()

TREE = {
    "Men": {"Bottoms": ["Chinos", "Jeans", "Joggers", "Shorts", "Trousers"]},
    "Women": {"Bottoms": ["Jeans", "Leather Trousers", "Leggings", "Shorts", "Trousers"],
              "Tops": ["T-Shirts", "Short Sleeve Shirts"]},
}


def product(**over) -> ProductSnapshot:
    kw = dict(id="69d7d32e", tenant_id="t", master_category="Men", category="Bottoms",
              subcategory="Jeans", title="LEVI STRAUSS & CO. 511 Light Wash Jeans W34 L34",
              catalog=TenantCatalog(categories=copy.deepcopy(TREE)))
    kw.update(over)
    return ProductSnapshot(**kw)


def evidence(sug: TaxonomySuggestion | None) -> Evidence:
    ev = Evidence()
    ev.vision = VisionAudit(taxonomy=sug)
    return ev


def shorts(conf: float, garment: str = "denim shorts") -> TaxonomySuggestion:
    return TaxonomySuggestion(category="Bottoms", subcategory="Shorts",
                              garment=garment, confidence=conf)


# --------------------------------------------------------------------------- #
# The type lookup
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("text,want", [
    ("Shorts", "shorts"), ("denim shorts", "shorts"), ("jean shorts", "shorts"),
    ("cargo shorts", "shorts"), ("Jeans", "full_length"), ("Trousers", "full_length"),
    ("Leather Trousers", "full_length"), ("Leggings", "full_length"),
    ("Dungarees", "dungarees"), ("skirt", "skirts"),
    ("Short Sleeve Shirts", None),   # two families: decides nothing
    ("T-Shirts", None), ("Bottoms", None), ("", None),
])
def test_garment_type(text, want):
    assert qg.garment_type(text, POL) == want


# --------------------------------------------------------------------------- #
# Fix 2 — the gate checks the type within the family
# --------------------------------------------------------------------------- #

GOOD = {"model_present": True, "face_ok": True, "gender": "Men", "lead_ok": True,
        "body_coherent": True, "body_issue": "", "view": "front", "confidence": 0.75}


def decide(garment, subcategory, confidence=0.75):
    return qg.decide({**GOOD, "garment": garment, "confidence": confidence},
                     product_gender="men", accessory=False, pol=POL,
                     category="Bottoms", subcategory=subcategory)


def test_shorts_filed_as_jeans_are_a_mismatch():
    """BOA-006863's own render read: 'shorts', filed under Jeans."""
    v = decide("shorts", "Jeans")
    assert v.action == "review" and v.code == "CATEGORY_IMAGE_MISMATCH"
    assert "shorts" in v.reasons[0] and "Jeans" in v.reasons[0]


def test_shorts_filed_as_trousers_are_a_mismatch():
    assert decide("denim shorts", "Trousers").code == "CATEGORY_IMAGE_MISMATCH"


def test_the_same_type_passes():
    assert decide("jeans", "Trousers").action == "ok"      # both full length
    assert decide("denim shorts", "Shorts").action == "ok"


def test_an_unsure_read_decides_nothing():
    assert decide("shorts", "Jeans", confidence=0.4).action == "ok"


# --------------------------------------------------------------------------- #
# Fix 1a — the picture's pick, at the single-field floor when the TYPE differs
# --------------------------------------------------------------------------- #

def test_shorts_filed_as_trousers_are_refiled_at_the_type_floor():
    """CBOA-006863: a valid `Trousers` raises no finding; only the picture can
    move it, and 0.86 used to be a mere proposal under the 0.90 pair floor."""
    out = resolve_taxonomy(product(master_category="Women", subcategory="Trousers"),
                           evidence(shorts(0.86)), POL, set())
    sub = next(pt for pt in out if pt.field == "subcategory")
    assert sub.new_value == "Shorts" and sub.action is Action.APPLY


def test_below_the_type_floor_it_still_only_proposes():
    out = resolve_taxonomy(product(subcategory="Trousers"), evidence(shorts(0.8)), POL, set())
    assert {pt.action for pt in out} == {Action.PROPOSE}


def test_the_same_type_keeps_the_pair_floor():
    """Jeans against Trousers is a judgement the type map does not settle."""
    out = resolve_taxonomy(
        product(subcategory="Trousers"),
        evidence(TaxonomySuggestion(category="Bottoms", subcategory="Jeans",
                                    garment="jeans", confidence=0.86)), POL, set())
    assert {pt.action for pt in out} == {Action.PROPOSE}


def test_a_model_that_names_a_third_type_keeps_the_pair_floor():
    out = resolve_taxonomy(product(subcategory="Trousers"),
                           evidence(shorts(0.86, garment="skirt")), POL, set())
    assert {pt.action for pt in out} == {Action.PROPOSE}


# --------------------------------------------------------------------------- #
# Fix 1b — the photographs overrule a subcategory taken from the title
# --------------------------------------------------------------------------- #

def title_entry(value="Jeans"):
    return {"kind": "set_column", "field": "subCategory", "value": value,
            "reason": "TAX.003", "detail": "the title names it", "source": "title"}


def test_the_photographs_overrule_the_title():
    plan = [title_entry()]
    approval._photo_type_over_title(product(), plan, evidence(shorts(0.9)), POL)
    assert plan[0]["kind"] == "set_column" and plan[0]["value"] == "Shorts"
    assert plan[0]["reason"] == "TAX.003" and plan[0]["source"] == "photo"
    assert "photographs show denim shorts" in plan[0]["detail"]


def test_an_unsure_picture_escalates_rather_than_writing_the_title():
    plan = [title_entry()]
    approval._photo_type_over_title(product(), plan, evidence(shorts(0.6)), POL)
    assert plan[0]["kind"] == "escalate" and plan[0]["value"] is None
    assert "'Jeans' and 'Shorts'" in plan[0]["detail"]


def test_the_same_type_leaves_the_title_alone():
    plan = [title_entry("Jeans")]
    approval._photo_type_over_title(
        product(), plan,
        evidence(TaxonomySuggestion(category="Bottoms", subcategory="Trousers",
                                    garment="trousers", confidence=0.95)), POL)
    assert plan[0]["value"] == "Jeans" and plan[0]["source"] == "title"


def test_only_a_title_derived_entry_is_overruled():
    plan = [{**title_entry(), "source": None}]
    approval._photo_type_over_title(product(), plan, evidence(shorts(0.95)), POL)
    assert plan[0]["value"] == "Jeans"


def test_the_taxonomy_question_sees_the_whole_garment_first():
    """The cut-out of each view, else its photograph; FRONT then BACK; never a
    superseded row. Independent of the gallery cache's order, whose head was
    close-ups when the three shorts were filed as Jeans."""
    from app.models import MediaAsset

    media = [
        MediaAsset(url="https://r2/label.jpg", view="LABEL", processing="RAW"),
        MediaAsset(url="https://r2/front-raw.jpg", view="FRONT", processing="RAW"),
        MediaAsset(url="https://r2/front-cut.png", view="FRONT", processing="BG_REMOVED"),
        MediaAsset(url="https://r2/old-back.png", view="BACK", processing="BG_REMOVED",
                   is_current=False),
        MediaAsset(url="https://r2/back-raw.jpg", view="BACK", processing="RAW"),
    ]
    p = product(media=media, images=["https://r2/closeup.jpg", "https://r2/label.jpg"])
    assert p.garment_view_urls == ["https://r2/front-cut.png", "https://r2/back-raw.jpg"]
    assert product().garment_view_urls == []


def test_end_to_end_the_plan_writes_shorts(monkeypatch):
    """BOA-006863 through _plan_subcategory and _plan_fields: an off-tree
    subcategory, a title that says Jeans, photographs that show shorts."""
    import app.pipeline as pipeline
    from app.models import Finding, Severity

    p = product(subcategory="Jean Shorts")            # off-tree -> TAX.003
    findings = [Finding(rule_id="TAX.003", severity=Severity.HIGH, fields=["subcategory"],
                        message="not on this branch")]
    monkeypatch.setattr(pipeline, "gather_evidence", lambda p, f, pol, llm: evidence(shorts(0.9)))
    monkeypatch.setattr(pipeline, "wants_taxonomy", lambda p, pol: True)
    plan: list[dict] = []
    approval._plan_subcategory(p, findings, plan)
    assert plan[0]["value"] == "Jeans" and plan[0]["source"] == "title"   # what it did before
    approval._plan_fields(p, POL, findings, object(), plan)
    subs = [a for a in plan if a.get("field") == "subCategory"]
    assert len(subs) == 1 and subs[0]["kind"] == "set_column" and subs[0]["value"] == "Shorts"
