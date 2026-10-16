"""Soft cut-outs on C-copies (15 Oct 2026): the parent's originals in the loader,
the parent re-push in the chain, and the driver that fixes a list."""
from __future__ import annotations

from app import product_audit as pa
from scripts import fix_soft_cutouts as fx
from scripts import repair_product as rp


# MEDIA_SQL-shaped: (id, url, view, origin, processing, mediaType, position,
#                    width, height, derivedFromId, isCurrent, shotKey)
def _row(mid, view, origin, processing, cur=True):
    return (mid, f"r2/{mid}", view, origin, processing, "IMAGE", 0, 896, 1195, None, cur, None)


def test_a_copy_gets_its_parents_originals_only_where_it_has_none():
    own = [_row("c-front", "FRONT", "PHOTOBOOTH", "BG_REMOVED"),
           _row("c-back-raw", "BACK", "PHOTOBOOTH", "RAW", cur=False)]
    parent = [_row("p-front-raw", "FRONT", "PHOTOBOOTH", "RAW", cur=False),
              _row("p-back-raw", "BACK", "PHOTOBOOTH", "RAW", cur=False),
              _row("p-front-dec", "FRONT", "DECISION", "RAW", cur=False)]
    ids = [r[0] for r in pa.with_parent_originals(own, parent)]
    # the copy's own BACK raw wins; the parent fills FRONT, per origin
    assert ids == ["c-front", "c-back-raw", "p-front-raw", "p-front-dec"]


def test_the_batch_query_is_keyed_by_the_copy():
    assert "SELECT c.id::text, pm.id::text" in pa.PARENT_RAWS_BATCH_SQL
    assert "c.id = ANY(%s::uuid[])" in pa.PARENT_RAWS_BATCH_SQL


def test_parents_updated_reads_the_backfill_summary():
    out = ("  dda4d9ef FRONT  -> hermes + #ebebeb  3000x4000  [replace-from-parent]\n"
           "  parent updated   : 17dc3195-083d-475c-acad-cdc7539eaa05 BOA-006506 (2 view(s))\n"
           "  cut-outs written : 2\n")
    assert rp.parents_updated(out) == {"17dc3195-083d-475c-acad-cdc7539eaa05": "BOA-006506"}
    assert rp.parents_updated("") == {} and rp.parents_updated(None) == {}


def test_soft_is_under_2000_on_the_long_side():
    rows = [{"view": "FRONT", "width": 3000, "height": 4000},
            {"view": "BACK", "width": 896, "height": 1195},
            {"view": "BACK", "width": 2440, "height": 3253}]
    assert fx.soft_views(rows) == ["BACK"]
    assert fx.soft_views(rows[:1]) == []


def _driver(monkeypatch, before, after, out):
    calls = iter([before, after])
    monkeypatch.setattr(fx, "cutouts", lambda pid: next(calls))
    monkeypatch.setattr(fx, "_vnyx_api_dir", lambda: "/nowhere")
    sent = {}

    def run_step(vnyx_api, script, args, **kw):
        sent["args"] = args
        return True, out, None

    monkeypatch.setattr(fx.rp, "run_step", run_step)
    return sent


def test_fixed_only_when_measured_sharp_after(monkeypatch):
    soft = [{"view": "FRONT", "origin": "WEB", "url": "a", "width": 896, "height": 1195}]
    sharp = [{"view": "FRONT", "origin": "WEB", "url": "b", "width": 3000, "height": 4000}]
    sent = _driver(monkeypatch, soft, sharp, "  cut-outs written : 1\n")
    r = fx.fix_one({"sku": "BOA-1", "id": "p1", "tenant": "BOAS"}, dsn="x", apply=True,
                   sync=False, provider="hermes", quiet=True)
    assert r["status"] == "fixed" and r["action"] == "re-cut from own photo"
    assert sent["args"][-2:] == ["--views", "FRONT"] and "--keep-better" in sent["args"]

    _driver(monkeypatch, soft, soft, "  KEPT the cut-out on file  — less garment\n")
    r = fx.fix_one({"sku": "BOA-1", "id": "p1", "tenant": "BOAS"}, dsn="x", apply=True,
                   sync=False, provider="hermes", quiet=True)
    assert r["status"] == "still soft: FRONT" and "KEPT" in r["note"]


def test_a_copy_shared_from_its_parent(monkeypatch):
    soft = [{"view": "FRONT", "origin": "PHOTOBOOTH", "url": "a", "width": 896, "height": 1195}]
    sharp = [{"view": "FRONT", "origin": "PHOTOBOOTH", "url": "p", "width": 2624, "height": 3499}]
    _driver(monkeypatch, soft, sharp,
            "  c2b4d42e FRONT  -> parent's cut-out  2624x3499  [reuse-parent]\n"
            "  shared from parent: 1\n")
    r = fx.fix_one({"sku": "CVIN-001265", "id": "c1", "tenant": "Vinted"}, dsn="x", apply=True,
                   sync=False, provider="hermes", quiet=True)
    assert r["status"] == "fixed" and r["action"] == "shared from parent" and r["copy"] is True


def test_an_already_sharp_product_is_not_sent(monkeypatch):
    sharp = [{"view": "FRONT", "origin": "WEB", "url": "b", "width": 3000, "height": 4000}]
    sent = _driver(monkeypatch, sharp, sharp, "")
    r = fx.fix_one({"sku": "KLE-1", "id": "k1", "tenant": "Klekt"}, dsn="x", apply=True,
                   sync=False, provider="hermes", quiet=True)
    assert r["status"] == "already sharp" and "args" not in sent
