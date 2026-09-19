"""scripts/repair_product.py --fixture — the rule engine with no database.

The shipped fixture is BOA-006114 as the local database held it on 15 Sep 2026,
option lists trimmed. Its verdict is pinned here: a change to a rule that moves
one of these findings shows up as a failing test rather than as a surprise on
the next live run.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import repair_product as rp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "repair"
BOA_006114 = FIXTURES / "boa-006114.json"


def test_the_shipped_fixture_is_judged_without_a_database():
    r = rp.run_fixture(BOA_006114)
    assert r["sku"] == "BOA-006114" and r["tenant"] == "BOAS" and r["stage"] == "REVIEW"
    assert r["verified"] is False
    blocking = {f["rule_id"] for f in r["blocking"]}
    # The five blocking defects the record carries; each a different rule family.
    assert {"PRICE.003", "TAX.004", "TAX.005", "SIZE.011", "IMG.030"} <= blocking
    # This fixture's only missing required field is `material`, and material stops
    # products no longer (`rules.severity_overrides` in policy.yaml). The field is
    # still reported as absent — the state block below is read from the record, not
    # from the findings — it simply no longer holds the product.
    assert "DATA.010" not in {f["rule_id"] for f in r["blocking"] + r["advisory"]}
    assert r["state"]["care_label"] == 0
    assert r["state"]["renders"] == 5
    assert "material" in r["state"]["attributes_missing"]
    assert r["would_run"] == ["extract", "reconcile"]
    kinds = {(a["kind"], a.get("field")) for a in r["repair_plan"]}
    assert ("set_column", "mannequinType") in kinds
    assert ("escalate", "careLabelImages") in kinds
    assert r["dumped"]["product"] == "BOA-006114"


def test_a_label_photo_brings_the_care_label_step_in(tmp_path):
    data = json.loads(BOA_006114.read_text(encoding="utf-8"))
    data["media"].append({"url": "https://r2/care-0.jpg", "view": "LABEL", "origin": "PHOTOBOOTH",
                         "processing": "RAW", "mediaType": "IMAGE", "isCurrent": True,
                         "deletedAt": None, "position": 9})
    data["record"]["careLabelCount"] = 1
    p = tmp_path / "with-label.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    r = rp.run_fixture(p)
    assert "care label" in r["would_run"]        # brand is missing and a label exists
    assert "IMG.030" not in {f["rule_id"] for f in r["blocking"]}


def test_cli_runs_a_directory_and_exits_zero_on_a_blocked_product(capsys, monkeypatch, tmp_path):
    out = tmp_path / "verdicts.json"
    monkeypatch.setattr(sys, "argv", ["repair_product.py", "--fixture", str(FIXTURES),
                                      "--out", str(out)])
    assert rp.main() == 0
    text = capsys.readouterr().out
    assert "OFFLINE" in text and "BOA-006114" in text
    assert "1 fixture(s) ran" in text and "1 blocked" in text
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["fixtures"][0]["sku"] == "BOA-006114"


def test_a_file_that_is_not_a_fixture_is_reported_not_raised(capsys, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
    assert rp.run_fixtures([bad]) == 1
    text = capsys.readouterr().out
    assert "not a fixture" in text and "could not run" in text


def test_directory_expansion_is_sorted_and_files_pass_through(tmp_path):
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("", encoding="utf-8")
    paths = rp._fixture_paths([str(tmp_path), str(BOA_006114)])
    assert [p.name for p in paths] == ["a.json", "b.json", "boa-006114.json"]


def test_dump_fixture_writes_what_load_returned_plus_provenance(tmp_path, monkeypatch):
    data = json.loads(BOA_006114.read_text(encoding="utf-8"))
    monkeypatch.setattr(rp.product_audit, "load", lambda dsn, pid: data)
    out = rp.dump_fixture("postgresql://u:secret@localhost:5433/db", "fdb8a034-0000", tmp_path / "x" / "f.json")
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert set(rp.FIXTURE_KEYS) <= set(doc)
    assert doc["_fixture"]["sku"] == "BOA-006114"
    assert "secret" not in doc["_fixture"]["database"]
    # and it round-trips through the judge
    assert rp.run_fixture(out)["sku"] == "BOA-006114"


@pytest.mark.parametrize("flag", ["--fixture"])
def test_fixture_mode_needs_no_database(monkeypatch, capsys, flag):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(sys, "argv", ["repair_product.py", flag, str(BOA_006114)])
    assert rp.main() == 0
    assert "BOA-006114" in capsys.readouterr().out
