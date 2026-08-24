"""The Approved-tab entry point, and the stage parameter it rides on.

Two things worth pinning. First that `approved` really is `ACCEPTED` -- getting
that mapping wrong would silently reprice the wrong 3,000 products. Second that
the wrapper is a wrapper: it must not acquire its own copy of the pricing rules,
because two copies drift apart on the first fix applied to either.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, SCRIPTS / name)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


rr = load("reprice_review_db.py", "reprice_review_db")
approved = load("reprice_approved_db.py", "reprice_approved_db_entry")


# --------------------------------------------------------------------------- #
# The stage mapping
# --------------------------------------------------------------------------- #

def test_approved_is_the_accepted_review_status():
    """From reviewStatusForTab in services/review-verification.ts, cross-checked
    against the per-tab counts in services/products.ts."""
    assert rr.STAGES["approved"] == "ACCEPTED"


def test_the_ui_label_and_the_url_name_agree():
    """The tab reads "Approved" while its URL says ?tab=uploaded. Both have to
    resolve to the same rows or the two names would reprice different sets."""
    assert rr.STAGES["approved"] == rr.STAGES["uploaded"] == "ACCEPTED"


def test_review_is_still_pending_and_still_the_default():
    assert rr.STAGES["review"] == rr.STAGES["pending"] == "PENDING"
    assert rr.parse_args(["--dsn", "x"]).stage == "review"


def test_the_misspelled_enum_value_is_preserved():
    """PENDING_DECISON is misspelled in the database enum. Correcting it here
    would match nothing at all."""
    assert rr.STAGES["decision"] == "PENDING_DECISON"


def test_every_stage_maps_to_a_distinct_known_status():
    assert set(rr.STAGES.values()) == {
        "PENDING", "ACCEPTED", "REJECTED", "PENDING_PHOTOBOOTH",
        "PENDING_DECISON"}


def test_an_unknown_stage_is_refused_rather_than_guessed():
    with pytest.raises(SystemExit):
        rr.parse_args(["--dsn", "x", "--stage", "archived"])


# --------------------------------------------------------------------------- #
# The stage reaches the SQL as a parameter
# --------------------------------------------------------------------------- #

def test_the_status_is_a_bind_parameter_not_a_literal():
    """Baked in as a literal it could not be switched per run, and every query
    has to agree on which rows it is talking about."""
    for sql in (rr.SELECT_SQL, rr.COUNT_SQL, rr.STATS_SQL,
                rr.TENANT_BREAKDOWN_SQL):
        assert "%(review_status)s" in sql
    assert "'PENDING'" not in rr.STAGE_WHERE


def test_the_comparison_is_made_on_text():
    """reviewStatus is an enum column. Casting the PARAMETER to that enum would
    mean naming the type here; casting the column avoids it."""
    assert 'p."reviewStatus"::text = %(review_status)s' in rr.STAGE_WHERE


def test_the_stage_clause_still_excludes_archived_and_deleted():
    assert 'p."isArchived" = false' in rr.STAGE_WHERE
    assert 'p."isDeleted"  = false' in rr.STAGE_WHERE


@pytest.mark.parametrize("name", ["STATS_SQL", "COUNT_SQL", "SELECT_SQL",
                                  "TENANT_BREAKDOWN_SQL"])
def test_the_parameterised_sql_is_still_valid_postgres(name):
    pglast = pytest.importorskip("pglast", reason="pip install pglast")
    sql = (getattr(rr, name)
           .replace("%(tenants)s", "'{}'")
           .replace("%(review_status)s", "'ACCEPTED'")
           .replace("{over_retail}", "")
           .replace("{limit}", "LIMIT 10"))
    assert len(pglast.parse_sql(sql)) == 1


# --------------------------------------------------------------------------- #
# The wrapper
# --------------------------------------------------------------------------- #

def test_the_wrapper_injects_the_approved_stage(monkeypatch):
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(approved._impl, "main",
                        lambda argv: seen.setdefault("argv", argv) and 0 or 0)
    approved.main(["--dsn", "x", "--limit", "5"])
    assert seen["argv"][:2] == ["--stage", "approved"]
    assert "--limit" in seen["argv"]


@pytest.mark.parametrize("explicit", [["--stage", "rejected"],
                                      ["--stage=rejected"]])
def test_an_explicit_stage_still_wins(monkeypatch, explicit):
    """Someone reaching for another tab from this entry point should get the tab
    they asked for, not a silent override."""
    seen: dict[str, list[str]] = {}
    monkeypatch.setattr(approved._impl, "main",
                        lambda argv: seen.setdefault("argv", argv) and 0 or 0)
    approved.main(["--dsn", "x", *explicit])
    assert seen["argv"].count("--stage") + \
        sum(a.startswith("--stage=") for a in seen["argv"]) == 1
    assert "approved" not in seen["argv"]


def test_the_wrapper_shares_the_implementation_rather_than_copying_it():
    """One implementation, two entry points. A copy would drift on the first fix
    applied to either file."""
    assert approved._impl.GRADE_PCT is rr.GRADE_PCT or \
        approved._impl.GRADE_PCT == rr.GRADE_PCT
    assert approved._impl.charm99(15.60) == rr.charm99(15.60) == 15.99
    # And it is genuinely the same file on disk.
    assert approved._TARGET.name == "reprice_review_db.py"
    assert approved._TARGET.exists()


def test_the_wrapper_defines_no_pricing_logic_of_its_own():
    source = (SCRIPTS / "reprice_approved_db.py").read_text(encoding="utf-8")
    for forbidden in ("charm99", "GRADE_PCT", "UPDATE ", "SELECT ", "psycopg"):
        assert forbidden not in source, (
            f"{forbidden!r} in the wrapper -- it should delegate, not reimplement")


def test_both_entry_points_expose_the_same_options():
    """--lookup-retail, --revert-from and the rest have to work identically from
    either file, or the docs on one of them are lying."""
    shared = rr.parse_args(["--dsn", "x", "--stage", "approved"])
    for option in ("lookup_retail", "revert_from", "lookup_budget", "tenant_id",
                   "max_quote_ratio", "undo_file", "apply", "all"):
        assert hasattr(shared, option), option
