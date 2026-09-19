"""The copy step must not describe data the rules have already rejected.

18 September 2026, ten Midtex products on production. `reconcile` wrote taxonomy
values that its own blocking rules refused in the same run, and `copy` then
regenerated each title from them:

    Athletic Adidas T-Shirt in Burgundy size M
        -> Vintage Adidas Deep Burgundy Hoodie Women S      (category 'hoodie')
    Vintage New Balance Charcoal T-Shirt Women L
        -> Regular T-Shirt in Charcoal Grey size L          (brand gone)

The title is generated FROM the taxonomy, gender and size. Regenerating it while
those are still blocking repairs nothing and makes the damage far harder to see:
a wrong title reads as a real product, a wrong `subCategory` reads as a bug.

Three changes closed this, and each has its own tests:

  * DRIFT.001 no longer PROPOSES a taxonomy value the tenant's tree does not
    hold — `tests/test_column_drift.py`.
  * vnyx-api's approval-gate executor REFUSES to write the names when the path
    does not resolve, as a backstop for any other planner.
  * `copy` is withheld while the record still misdescribes the garment — here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import policy  # noqa: E402
from app.vnyx_client import to_snapshot  # noqa: E402
from scripts import repair_product as rp  # noqa: E402

POL = policy()

# Both roots, because the gender checks need a women's branch to disagree with.
TREE = {
    "Men": {"Jackets": ["Leather Jacket"], "Tops": ["T-Shirt"]},
    "Women": {"Tops": ["T-Shirt"]},
}
CATALOG = {
    "categories": TREE,
    "sizingGuides": {
        "Men Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["46", "48", "50"]},
        "Women Uppers": {"sizes": ["S", "M", "L"], "euSizes": ["36", "38", "40"]},
    },
    "brands": ["Zara"], "colors": ["Black"], "materials": ["Leather"],
}


def snap(**over):
    raw = {
        "id": "p1", "tenantId": "t1", "sku": "S1", "productCode": "PC1",
        "masterCategory": "Men", "category": "Jackets", "subCategory": "Leather Jacket",
        "size": "S", "euSize": "46", "sizingGuide": "Men Uppers", "brand": "Zara",
        "color": "Black", "material": "Leather", "condition": "As New",
        "gender": ["men"], "careLabelCount": 1,
        # A rig, so TAX.007 stays quiet. It is not this guard's business — see
        # test_a_missing_mannequin_rig_does_not_withhold_the_copy.
        "mannequinType": "Men Top",
        "properties": {}, "columnValues": {},
        **over,
    }
    return to_snapshot(raw, catalog=CATALOG)


def ids(p) -> set[str]:
    return {f.rule_id for f in rp._blocking_taxonomy(p)}


def test_a_clean_record_does_not_withhold_the_copy():
    assert ids(snap()) == set()


def test_a_category_the_tree_does_not_hold_withholds_the_copy():
    """MID-000236's case: `category: 'hoodie'` on a t-shirt."""
    found = ids(snap(category="hoodie", subCategory="hoodie"))
    assert found
    assert any(r.startswith("TAX.") for r in found)


def test_a_gender_that_contradicts_the_master_category_withholds_the_copy():
    """MID-000079's case: master 'Women' with gender 'men', which is what made
    the gate pay for a whole-set re-render as well as producing a wrong title."""
    found = ids(snap(masterCategory="Women", category="Tops",
                     subCategory="T-Shirt", gender=["men"]))
    assert "TAX.004" in found


def test_a_missing_mannequin_rig_does_not_withhold_the_copy():
    """TAX.007 is HIGH and starts with `TAX.`, and the first version of this
    guard matched on that prefix and swept it in. The rig is a rendering choice;
    the title never mentions it. A product with no rig configured would have had
    its copy withheld forever."""
    p = snap(mannequinType=None)
    assert "TAX.007" in {f.rule_id for f in __import__(
        "app.rules", fromlist=["run_all"]).run_all(p, POL)}
    assert ids(p) == set()


def test_a_sizing_guide_for_the_other_gender_withholds_the_copy():
    p = snap(masterCategory="Men", gender=["men"], sizingGuide="Women Uppers")
    assert "SIZE.011" in ids(p) or "TAX.004" in ids(p)


def test_a_price_or_label_problem_does_not_withhold_the_copy():
    """Scoped deliberately. The copy is written from the taxonomy, the gender and
    the size; a missing care label or a price outside its window says nothing
    about whether the title describes the garment, and withholding on those would
    stop the copy repairing titles it can legitimately fix."""
    p = snap(careLabelCount=0, price="999.00", retailPrice="10.00")
    assert ids(p) == set()


def test_only_blocking_findings_count():
    """A LOW or MEDIUM taxonomy finding is a note, not a contradiction. Treating
    every TAX.* finding as a reason to withhold would stop the copy step running
    on most of the catalogue."""
    from app.models import Severity

    for f in rp._blocking_taxonomy(snap(category="hoodie", subCategory="hoodie")):
        assert f.severity in (Severity.HIGH, Severity.CRITICAL)


# --------------------------------------------------------------------------- #
# The step-runner capability probe
#
# Items 6 and 8 of docs/PICTURE-CHECK-FIXES.md are sent by the chain as
# `--bg-strategies` and `--keep-better`, and land on a vnyx-api whose
# backfill-bg-removal.ts does not take them yet. Without a probe the first
# re-matte of every run dies on a 400 — after master, matte and reconcile have
# already written.
# --------------------------------------------------------------------------- #

def _options(monkeypatch, value):
    monkeypatch.setattr(rp, "_REMOTE_OPTIONS", value, raising=False)


def test_a_server_that_lists_the_options_is_believed(monkeypatch):
    _options(monkeypatch, {"replace", "keepBetter", "bgStrategies"})
    assert rp.remote_supports("keepBetter") is True
    assert rp.remote_supports("keepBetter", "bgStrategies") is True


def test_a_server_that_lists_options_without_ours_vetoes(monkeypatch):
    """The only case worth stopping for: this deployment WOULD answer 400."""
    _options(monkeypatch, {"replace", "minConfidence", "views"})
    assert rp.remote_supports("keepBetter") is False
    assert rp.remote_supports("bgStrategies") is False


def test_one_missing_option_is_enough_to_veto(monkeypatch):
    _options(monkeypatch, {"keepBetter"})
    assert rp.remote_supports("keepBetter", "bgStrategies") is False


def test_the_local_transport_is_never_vetoed(monkeypatch):
    """A spawn has no request to reject: an argument the script does not know is
    a flag it does not read, not a 400."""
    _options(monkeypatch, {rp._LOCAL_TRANSPORT})
    assert rp.remote_supports("keepBetter", "bgStrategies") is True


def test_a_ping_that_did_not_answer_is_not_a_veto(monkeypatch):
    """Inventing a skip for an unreachable server would replace a clear
    connection error with a misleading "left the cut-outs alone"."""
    _options(monkeypatch, set())
    assert rp.remote_supports("keepBetter") is True
