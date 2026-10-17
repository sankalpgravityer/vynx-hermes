"""A WEB photo's cut-out may keep its garment at 1/2.0 of the photo (17 Oct 2026);
photobooth and decision photos keep 1/1.6. Framing is unchanged."""
from app.config import policy
from app.imaging import cutouts


def cfg():
    return {**cutouts.DEFAULTS, **((policy().get("readiness") or {}).get("cutouts") or {})}


M = {"registered": {"scale": 1.0}}


def test_web_passes_at_1_8_where_photobooth_holds():
    c = cfg()
    assert c["resolution_max"] == 1.6 and c["resolution_max_by_origin"]["WEB"] == 2.0
    dims, photo = (1737, 2316), (3000, 4000)          # VIN-000554 FRONT: 1/1.73
    assert cutouts.resolution_problem(M, dims, photo, c, origin="WEB") is None
    assert cutouts.resolution_problem(M, dims, photo, c, origin="PHOTOBOOTH")
    assert cutouts.resolution_problem(M, dims, photo, c)                     # no origin: the strict limit


def test_web_still_holds_the_old_small_cutouts():
    assert cutouts.resolution_problem(M, (896, 1195), (3000, 4000), cfg(), origin="web")   # 1/3.3
