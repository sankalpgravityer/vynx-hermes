"""Which products a sheet may name — `--include-approved`.

THE BACKLOG IS IN THE APPROVED TAB. 356 Klekt and 145 Midtex products passed
review months ago and fail checks added since (a studio rig behind the model, a
close-up showing the back). They need the repair chain and must never be
re-approved, so selecting a product and approving one came apart on
21 Sep 2026 and these tests hold them apart.

`resolve()` builds its SQL by string interpolation from a closed set, so the
clause text is what is asserted here — no database is touched.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import run_from_sheet as rfs  # noqa: E402


def clauses(stages, retry=False):
    """The clause list `resolve` would run, as {name: sql}."""
    captured = {}

    def fake_fetch_one(sql, params=None):
        captured["sql"] = sql
        return None

    real = rfs.db.fetch_one
    rfs.db.fetch_one = fake_fetch_one
    try:
        rfs.resolve({"id": "0f1e2d3c-4b5a-4978-8877-665544332211", "sku": ""},
                    include_failed=False, retry=retry, stages=stages)
    finally:
        rfs.db.fetch_one = real
    return captured["sql"]


def test_the_default_still_means_review_alone():
    sql = clauses(["REVIEW"])
    assert "\"currentStage\" = 'REVIEW'" in sql
    assert "APPROVED" not in sql


def test_an_approved_product_is_selectable_on_both_columns():
    """Widening the stage alone is not enough, and that is the bug this fixes:
    `reviewStatus` is ACCEPTED on an approved product, so the row still failed
    'review status pending' — which reads as a different problem and is not
    one. ProductStage.APPROVED is DEFINED as reviewStatus=ACCEPTED."""
    sql = clauses(["REVIEW", "APPROVED"])
    assert "'REVIEW','APPROVED'" in sql.replace(" ", "")
    assert "'ACCEPTED'" in sql


def test_review_status_is_not_widened_without_the_approved_stage():
    """--include-label must not quietly admit accepted products too."""
    sql = clauses(["REVIEW", "LABEL"])
    assert "'ACCEPTED'" not in sql


def test_approved_is_selectable_but_never_approvable():
    """THE SEPARATION. `allow_stage` is validated against APPROVABLE_STAGES,
    which mirrors vnyx-api's APPROVABLE_FROM — so a widened sheet cannot widen
    what the approval pre-flight will move a product out of."""
    assert "APPROVED" in rfs.SELECTABLE_STAGES
    assert "APPROVED" not in rfs.APPROVABLE_STAGES
    assert set(rfs.APPROVABLE_STAGES) < set(rfs.SELECTABLE_STAGES)


def test_the_claim_widens_review_status_with_the_stage():
    """THE SECOND HALF OF THE SAME BUG. The eligibility predicate and the claim
    both test `reviewStatus`, and the claim's copy was hardcoded — so an
    APPROVED product was admitted, enqueued, and then refused by the claim,
    which returns a bare None the caller can only report as "another worker
    holds this tenant". Deriving both from the stages is what keeps them from
    drifting again."""
    from app.services.auto_approval import claim

    assert claim.review_statuses(None) == ["PENDING", "PENDING_DECISON"]
    assert claim.review_statuses(["REVIEW"]) == ["PENDING", "PENDING_DECISON"]
    assert claim.review_statuses(["REVIEW", "LABEL"]) == ["PENDING", "PENDING_DECISON"]
    assert "ACCEPTED" in claim.review_statuses(["REVIEW", "APPROVED"])
    # The clause must read the parameter, not a literal, or none of the above
    # reaches the database.
    assert '%(review)s::"ProductReviewStatus"[]' in claim._CLAIM_SQL
    assert "'PENDING','PENDING_DECISON'" not in claim._CLAIM_SQL


def test_a_stage_outside_the_closed_set_is_refused():
    """The names reach SQL by interpolation, so they are checked, not trusted."""
    with pytest.raises(ValueError, match="unknown stage"):
        clauses(["REVIEW", "DELETED"])
    with pytest.raises(ValueError, match="unknown stage"):
        clauses(["'; DROP TABLE \"Product\"; --"])
