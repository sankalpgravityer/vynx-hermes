"""Material and grade are reported by the rules and ignored by the gate.

Neither is a condition of approval at VNYX — a fibre string the tenant's option
list has not absorbed yet, and the operator's own judgement of a garment they
were holding, which nothing here can overrule. `rules.severity_overrides` in
policy.yaml drops both, and this pins the two halves that matter:

  * the RULES are untouched. GRADE.001-003, ATTR.003 and TEXT.005 still fire
    under `run_all`, so /v1/review-queue and every report that reads the raw
    rule set keeps seeing them. Deleting the rules instead would have thrown
    the evidence away along with the blocking.
  * the GATE never sees them. `_all_findings` is the one place run_gate's three
    passes all go through, so `blocking`, `advisory` and `field_issues` agree.

And the scoping, which is the part that would quietly do damage if it were
wrong: DATA.001 and DATA.010 each cover ten required fields, and only their
`material` findings are dropped. A missing category must still stop a product.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import approval  # noqa: E402
from app.config import policy  # noqa: E402
from app.rules import run_all  # noqa: E402
from app.vnyx_client import to_snapshot  # noqa: E402

from tests.test_review_queue import feed  # noqa: E402

POL = policy()


def gate_ids(p) -> set[str]:
    return {f.rule_id for f in approval._all_findings(p, POL)}


def test_the_grade_rules_still_fire_but_no_longer_reach_the_gate():
    # Condition disagreeing with the tenant's own grade label — GRADE.001's
    # exact-invariant branch, the one test_review_queue pins on `run_all`.
    p = to_snapshot(feed(condition="As New"))
    assert "GRADE.001" in {f.rule_id for f in run_all(p, POL)}
    assert "GRADE.001" not in gate_ids(p)


def test_a_grade_c_with_no_defects_is_reported_and_not_held():
    p = to_snapshot(feed(defects=[], operatorDefects=[]))
    assert "GRADE.003" in {f.rule_id for f in run_all(p, POL)}
    assert "GRADE.003" not in gate_ids(p)


def test_a_material_outside_the_tenants_list_no_longer_reaches_the_gate():
    """ATTR.003 needs the tenant's own `/material-settings` list in hand, which
    the review-queue feed fixture does not carry — so this one is built on the
    catalog-bearing snapshot from test_column_drift."""
    from tests.test_column_drift import snap

    p = snap(material="Ripstop Nylon 70D")
    assert "ATTR.003" in {f.rule_id for f in run_all(p, POL)}
    assert "ATTR.003" not in gate_ids(p)

    # The sibling rules on the same list are untouched — this is a material
    # decision, not a "stop checking the tenant's option lists" decision.
    q = snap(brand="Not A Brand We Stock")
    assert "ATTR.001" in gate_ids(q)


def test_a_material_placeholder_is_dropped_but_a_brand_placeholder_is_not():
    """DATA.001 covers five fields. Only the material one goes."""
    p = to_snapshot(feed(material="n/a", brand="n/a"))
    raw = [f for f in run_all(p, POL) if f.rule_id == "DATA.001"]
    assert {tuple(f.fields) for f in raw} >= {("material",), ("brand",)}

    kept = [f for f in approval._all_findings(p, POL) if f.rule_id == "DATA.001"]
    assert {tuple(f.fields) for f in kept} == {("brand",)}


def test_an_absent_category_still_blocks_when_an_absent_material_does_not():
    """DATA.010 is the gate's own required-field rule, and the same scoping
    applies: material is not a reason to hold, a missing category is."""
    p = to_snapshot(feed(material=None, subCategory=None))
    fields = {tuple(f.fields) for f in approval._all_findings(p, POL)
              if f.rule_id == "DATA.010"}
    assert ("subcategory",) in fields
    assert ("material",) not in fields


def test_a_tenant_that_re_ranks_one_of_these_keeps_its_own_answer():
    """The policy block is the DEFAULT layer. A tenant that has deliberately set
    GRADE.002 back to HIGH must not have it silently dropped underneath them."""
    merged = approval._merged_overrides(POL, {"GRADE.002": "high"})
    assert merged["GRADE.002"] == "high"
    assert merged["ATTR.003"] == "ignore"


def test_no_tenant_setting_means_the_policy_block_stands_alone():
    merged = approval._merged_overrides(POL, None)
    assert merged["GRADE.001"] == "ignore"
    assert merged["DATA.010:material"] == "ignore"


def test_a_missing_material_alone_does_not_put_extract_on_the_plan(tmp_path):
    """The live chain has always skipped the extractor when material is the only
    gap (NOT_WORTH_EXTRACT). The offline report used to promise it anyway, so a
    fixture and a real run disagreed about the same record.

    The shipped fixture is missing brand AND material, which is why it still runs
    extract; filling the brand in leaves material alone and nothing to do.
    """
    import json

    from scripts import repair_product as rp

    src = Path(__file__).resolve().parent / "fixtures" / "repair" / "boa-006114.json"
    data = json.loads(src.read_text(encoding="utf-8"))
    assert rp.run_fixture(src)["state"]["attributes_missing"] == ["brand", "material"]

    data["record"]["brand"] = "Nike"
    patched = tmp_path / "brand-filled.json"
    patched.write_text(json.dumps(data), encoding="utf-8")

    r = rp.run_fixture(patched)
    assert r["state"]["attributes_missing"] == ["material"]
    assert "extract" not in r["would_run"]
