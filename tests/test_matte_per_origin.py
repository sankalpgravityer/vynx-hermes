"""The matte step counts originals per view AND origin (7 Oct 2026).

MID-000447..460 (Midtex): each had a DECISION front, a WEB front and a WEB back. The
two WEB photos were matted; the DECISION front never was, because a FRONT cut-out of
any origin made the whole view read as done — so it never reached the Edited gallery,
and IMG.010 kept blocking. backfill-bg-removal.ts (vnyx-api, cutout-coverage.ts) now
covers a RAW only with a cut-out of its own view and origin; the chain counts the same.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _row(view, origin, processing, n):
    return {"url": f"https://x/{n}.jpg", "view": view, "origin": origin, "processing": processing,
            "mediaType": "IMAGE", "isCurrent": True, "position": n}


def _state(media):
    import scripts.repair_product as rp

    return rp.needs_from({"record": {"title": "t", "summary": "s"}, "media": media})


def test_a_decision_front_beside_a_matted_web_front_is_unmatted():
    st = _state([_row("FRONT", "DECISION", "RAW", 1), _row("FRONT", "WEB", "BG_REMOVED", 2),
                 _row("BACK", "WEB", "BG_REMOVED", 3)])
    assert st["unmatted"] == 1 and st["unmatted_views"] == ["FRONT (DECISION)"]
    assert st["leftover_raw"] == 0


def test_an_original_with_a_cut_out_of_its_own_origin_is_done():
    st = _state([_row("FRONT", "DECISION", "RAW", 1), _row("FRONT", "DECISION", "BG_REMOVED", 2),
                 _row("FRONT", "PHOTOBOOTH", "BG_REMOVED", 3)])
    assert st["unmatted"] == 0
    # A second DECISION original beside the DECISION cut-out is still IMG.010's, not ours.
    assert st["leftover_raw"] == 1


def test_a_view_with_no_cut_out_is_named_plainly():
    st = _state([_row("BACK", "WEB", "RAW", 1), _row("FRONT", "WEB", "BG_REMOVED", 2)])
    assert st["unmatted_views"] == ["BACK"]


def test_rows_without_an_origin_count_per_view_as_before():
    st = _state([_row("FRONT", None, "RAW", 1), _row("FRONT", "WEB", "BG_REMOVED", 2)])
    assert st["unmatted"] == 0 and st["leftover_raw"] == 1
