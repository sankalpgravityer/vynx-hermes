"""Filing the garment from its photographs, against the tenant's own tree.

The gap this closes, in one product. MID-000615 is a tank top filed under
`Women > Dresses > Casual Dress`. That path EXISTS in the tenant's tree, so
TAX.002 and TAX.003 — which read columns — are both silent and correct. The
quality gate saw the garment and raised CATEGORY_IMAGE_MISMATCH, but returns
`review` by design, and `regen_views` returns nothing for a review: the product
blocked on every run with no repair behind the hold.

Cold: no database, no network, no model. The vision answer is handed in as
evidence, which is exactly the shape `gather_evidence` produces.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.models import (  # noqa: E402
    Action, Evidence, ProductSnapshot, TaxonomySuggestion, TenantCatalog, VisionAudit,
)
from app.pipeline import taxonomy_options  # noqa: E402
from app.resolver import resolve_taxonomy  # noqa: E402

POL = policy()

# Midtex-shaped: `Dresses > Casual Dress` is real, which is why nothing caught
# the misfiling, and `Tops > Tank Tops` is where the garment belongs.
TREE = {
    "Women": {
        "Dresses": ["Casual Dress", "Midi", "Maxi"],
        "Tops": ["T-Shirts", "Tank Tops", "Blouses"],
        "Bottoms": ["Jeans", "Trousers"],
    },
    "Men": {"Tops": ["T-Shirts"], "Bottoms": ["Jeans"]},
}


def product(**over) -> ProductSnapshot:
    kw = dict(
        id="23e813ee", tenant_id="t", master_category="Women",
        category="Dresses", subcategory="Casual Dress",
        catalog=TenantCatalog(categories=copy.deepcopy(TREE)),
    )
    kw.update(over)
    return ProductSnapshot(**kw)


def evidence(sug: TaxonomySuggestion | None) -> Evidence:
    ev = Evidence()
    ev.vision = VisionAudit(taxonomy=sug)
    return ev


def run(sug, p=None, pol=POL):
    return resolve_taxonomy(p or product(), evidence(sug), pol, set())


TANK_TOP = TaxonomySuggestion(
    category="Tops", subcategory="Tank Tops", garment="tank top", confidence=0.9,
)


# --------------------------------------------------------------------------- #
# What the model is allowed to choose from
# --------------------------------------------------------------------------- #

def test_options_are_the_tenants_own_tree_under_the_master():
    assert taxonomy_options(product(), POL) == {
        "Dresses": ["Casual Dress", "Midi", "Maxi"],
        "Tops": ["T-Shirts", "Tank Tops", "Blouses"],
        "Bottoms": ["Jeans", "Trousers"],
    }


def test_options_never_cross_a_root():
    """A photograph may move a product WITHIN its root and never between roots —
    `readiness.master.photo_check` is explicit that the render may not rewrite
    the record it is judged by. Offering the Men branch would invite exactly
    that."""
    opts = taxonomy_options(product(), POL)
    assert "Men" not in opts and set(opts) == {"Dresses", "Tops", "Bottoms"}


def test_options_match_the_tenants_spelling_of_the_root():
    assert taxonomy_options(product(master_category="WOMEN"), POL)


@pytest.mark.parametrize("why,p", [
    ("no master to anchor on", product(master_category=None)),
    ("no catalog", product(catalog=None)),
    ("a root the tenant does not have", product(master_category="Kids")),
])
def test_options_are_empty_rather_than_guessed(why, p):
    assert taxonomy_options(p, POL) == {}, why


def test_options_are_empty_when_the_feature_is_off():
    """The prompt then drops the whole section and the call returns to exactly
    its previous shape."""
    pol = copy.deepcopy(POL)
    pol["taxonomy_from_picture"] = {"enabled": False}
    assert taxonomy_options(product(), pol) == {}


# --------------------------------------------------------------------------- #
# The repair
# --------------------------------------------------------------------------- #

def test_the_tank_top_is_refiled():
    """THE REGRESSION TEST for MID-000615."""
    out = run(TANK_TOP)
    assert [(p.field, p.new_value, p.action) for p in out] == [
        ("category", "Tops", Action.APPLY),
        ("subcategory", "Tank Tops", Action.APPLY),
    ]
    assert "tank top" in out[0].reason and out[0].rule_id == "TAX.010"


def test_both_halves_move_together():
    out = run(TANK_TOP)
    assert {p.field for p in out} == {"category", "subcategory"}
    assert len({p.action for p in out}) == 1


def test_spelling_and_plurals_resolve_to_the_tenants_own_strings():
    """A model told to copy a string exactly still drops a hyphen sometimes, and
    'Tank Top' vs 'Tank Tops' is one subcategory. The WRITTEN value is always the
    tenant's spelling, never the model's."""
    out = run(TaxonomySuggestion(category="tops", subcategory="tank top",
                                 garment="tank top", confidence=0.95))
    assert [p.new_value for p in out] == ["Tops", "Tank Tops"]


def test_a_category_outside_the_tree_is_dropped_not_proposed():
    """The 18 Sep 2026 corruption — `category: 'hoodie'` on an Adidas t-shirt,
    republished as a "Deep Burgundy Hoodie" by the copy step. An off-catalog
    value is not a weaker repair, it is not a repair."""
    assert run(TaxonomySuggestion(category="Knitwear", subcategory="Jumpers",
                                  garment="jumper", confidence=0.99)) == []


def test_below_the_floor_it_proposes_rather_than_writes():
    """The gate read this garment at 0.70 and the photo audit at 0.90; the floor
    sits between them on purpose."""
    out = run(TaxonomySuggestion(category="Tops", subcategory="Tank Tops",
                                 garment="tank top", confidence=0.7))
    assert {p.action for p in out} == {Action.PROPOSE}
    assert "below the 0.90 floor" in out[0].reason


def test_half_a_path_escalates_instead_of_writing_the_category_alone():
    """'Casual Dress' is not offered under 'Tops', so moving the category alone
    would leave the product on a leaf its branch does not have."""
    out = run(TaxonomySuggestion(category="Tops", subcategory=None,
                                 garment="tank top", confidence=0.95))
    assert len(out) == 1
    assert out[0].action is Action.ESCALATE and out[0].new_value is None
    assert "Choose the pair" in out[0].reason


def test_the_category_alone_moves_when_the_leaf_survives_the_move():
    """The same fault with a subcategory that exists under BOTH branches is not
    a split path, so the category moves on its own."""
    tree = copy.deepcopy(TREE)
    tree["Women"]["Tops"].append("Casual Dress")
    p = product(catalog=TenantCatalog(categories=tree))
    out = run(TaxonomySuggestion(category="Tops", subcategory=None,
                                 garment="tank top", confidence=0.95), p=p)
    assert [(x.field, x.new_value) for x in out] == [
        ("category", "Tops"), ("subcategory", "Casual Dress")]


def test_agreement_writes_nothing():
    """Most products are filed correctly. A no-op entry on every one of them
    would bury the real repairs."""
    assert run(TaxonomySuggestion(category="Dresses", subcategory="Casual Dress",
                                  garment="casual dress", confidence=0.99)) == []


@pytest.mark.parametrize("why,sug", [
    ("the model declined outright", None),
    ("it named no category", TaxonomySuggestion(garment="tank top", confidence=0.99)),
])
def test_declining_is_a_legitimate_answer(why, sug):
    assert run(sug) == [], why


def test_the_feature_can_be_switched_off_in_policy():
    pol = copy.deepcopy(POL)
    pol["taxonomy_from_picture"] = {"enabled": False}
    assert run(TANK_TOP, pol=pol) == []


def test_the_master_category_is_never_written():
    """The anchor is what the branch was narrowed BY. A picture may not move it."""
    assert all(p.field != "master_category" for p in run(TANK_TOP))


def test_a_locked_field_is_not_overwritten_and_takes_its_partner_with_it():
    """`_gate` demotes a human-locked field on its own — so without the
    both-or-neither check the pair would come back APPLY and ESCALATE, and the
    executor would write the subcategory beside a category a person had fixed by
    hand. Any disagreement demotes both."""
    p = product(locked_fields=["category"])
    out = run(TANK_TOP, p=p)
    assert len(out) == 2
    assert {x.action for x in out} == {Action.PROPOSE}


# --------------------------------------------------------------------------- #
# Reaching the product at all — the gate that made the whole fix a no-op
# --------------------------------------------------------------------------- #

def test_the_question_is_asked_with_no_finding_behind_it():
    """THE ONE THAT MATTERS. `plan()` runs every planner behind `if findings:`
    and `gather_evidence` only calls the vision layer when a rule named a visual
    field. A garment filed under a category that EXISTS raises neither, so the
    repair would never have been reached on the product it was written for."""
    from app.pipeline import wants_taxonomy

    assert wants_taxonomy(product(images=["https://r2/front.png"]), POL)


@pytest.mark.parametrize("why,p", [
    ("no garment photograph — a wash tag cannot file a garment",
     product(images=[], care_label_urls=["https://r2/label.jpg"])),
    ("no tree to choose from", product(catalog=None)),
    ("no master to narrow by", product(master_category=None, images=["https://r2/f.png"])),
])
def test_the_question_is_not_asked_when_it_cannot_be_answered(why, p):
    from app.pipeline import wants_taxonomy

    assert not wants_taxonomy(p, POL), why


def test_always_ask_false_returns_the_previous_cost_profile():
    """The switch is the cost decision, and turning it off has to restore the
    finding-driven behaviour exactly."""
    from app.pipeline import wants_taxonomy

    pol = copy.deepcopy(POL)
    pol["taxonomy_from_picture"]["always_ask"] = False
    assert not wants_taxonomy(product(images=["https://r2/f.png"]), pol)


def test_gather_evidence_makes_the_call_with_no_findings():
    """End to end through the real `gather_evidence`, with a stub model."""
    from app.pipeline import gather_evidence

    seen: dict = {}

    class Stub:
        calls = 1

        def audit_images(self, p, fields, options=None):
            seen["options"] = options
            return VisionAudit(taxonomy=TANK_TOP)

    ev = gather_evidence(product(images=["https://r2/front.png"]), [], POL, Stub())
    assert seen.get("options"), "the tenant's tree must reach the model"
    assert ev.vision.taxonomy is not None
