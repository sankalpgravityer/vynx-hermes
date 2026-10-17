"""The render's model decides a master nothing else names (17 Oct 2026, the user).

20 Vinted imports had no master, no category and gender "both" ("Vintage Nike Deep
Black T-Shirt Unisex XS") and held as MASTER_CATEGORY_UNRESOLVED. Last of all the
evidence, the on-model render's gender names the root; with no category either, the
garment the same render shows files it under that root.
"""
from __future__ import annotations

from app import approval, readiness
from app.config import policy
from app.vnyx_client import to_snapshot
from tests.test_render_decides import CATALOG, raw


def blank(**over):
    base = dict(masterCategory=None, category=None, subCategory=None, gender=["men", "women"],
                sizingGuide=None, mannequinType=None)
    return raw(**{**base, **over})


def test_unresolved_without_the_render():
    d = readiness.decide_master(to_snapshot(blank(), catalog=CATALOG), policy())
    assert d["action"] == "unresolved"


def test_the_render_names_the_root_last():
    d = readiness.decide_master(to_snapshot(blank(), catalog=CATALOG), policy(), render_gender="women")
    assert (d["action"], d["master"], d["basis"]) == ("set", "Women", "render model")
    d = readiness.decide_master(to_snapshot(blank(), catalog=CATALOG), policy(), render_gender="men")
    assert d["master"] == "Men"


def test_data_still_comes_before_the_render():
    # the gender property names a root: the render is not asked
    d = readiness.decide_master(to_snapshot(blank(gender=["men"]), catalog=CATALOG), policy(), render_gender="women")
    assert (d["master"], d["basis"]) == ("Men", "gender property")


def test_reconcile_writes_master_and_files_the_garment_under_it():
    out = approval.run_gate(blank(), catalog=CATALOG, llm=None,
                            hints={"render_gender": "women", "render_garment": "t-shirt"})
    plan = {a["field"]: a["value"] for a in out["repair_plan"] if a["kind"] in ("set_column", "set_property")}
    assert plan["masterCategory"] == "Women"
    assert (plan["category"], plan["subCategory"]) == ("T-Shirts & Tops", "T-Shirts")


def test_no_render_hint_plans_no_master():
    out = approval.run_gate(blank(), catalog=CATALOG, llm=None)
    plan = {a["field"]: a["value"] for a in out["repair_plan"] if a["kind"] in ("set_column", "set_property")}
    assert "masterCategory" not in plan
