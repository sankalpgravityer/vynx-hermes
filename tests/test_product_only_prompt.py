"""Product-focused on-model views for accessories and footwear (28 Oct 2026).

Every view keeps the AI model, but the camera zooms in on the product, and
"back" means the back of the PRODUCT faces the camera (VIN-001600 backpack,
BOA-007894 hat). Same words as vnyx-api's productFocusFraming.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.imaging import nanobanana as nb  # noqa: E402
from app.imaging.nanobanana import PromptContext  # noqa: E402
from app.models import ImagerySettings  # noqa: E402

BACKPACK = ("Backpacks & Bags", "Backpacks")
BAG = ("Backpacks & Bags", "Bag")
HAT = ("Accessories", "Hats")
SHOES = ("Shoes", "Lifestyle")
TEE = ("T-Shirts & Polos", "T-Shirt")
VIEWS = ("front", "back", "front34", "back34", "closeup")


def _prompt(view, cat, sub, ref=True):
    ctx = PromptContext(settings=ImagerySettings.model_validate({"background": "#ffffff", "body_type": "l"}),
                        category=cat, sub_category=sub)
    return nb.build_prompt(view, ctx, has_front_reference=ref)


def test_every_view_keeps_the_model_framed_on_the_product():
    for cat, sub in (BACKPACK, BAG, HAT, SHOES):
        for view in VIEWS:
            p = _prompt(view, cat, sub)
            assert "worn or held by the model" in p, (sub, view)
            assert "EXACT SAME model as the reference render" in p, (sub, view)
            assert "NO MODEL" not in p and "FULL-LENGTH" not in p, (sub, view)


def test_garments_are_untouched():
    assert not nb.is_product_focus(*TEE)
    p = _prompt("back", *TEE)
    assert "REAR (BACK) VIEW" in p and "PRODUCT-FOCUSED" not in p


def test_a_hat_zooms_in_a_clear_ladder_and_its_back_is_the_closure():
    # the cap's ¾ and close-up used to come back the same
    assert "from the top of the hat down to the waist" in _prompt("front", *HAT)
    assert "head and shoulders, the hat large" in _prompt("front34", *HAT)
    assert "from just above the crown down to the nose" in _prompt("closeup", *HAT)
    back = _prompt("back34", *HAT)
    assert "BACK OF THE HAT" in back and "snapback" in back and "THREE-QUARTER ANGLE" in back


def test_the_zoom_ladder_and_a_different_pose_per_view():
    import re
    for cat, sub in (BACKPACK, BAG, HAT, SHOES):
        assert "the WIDEST of this product" in _prompt("front", cat, sub)
        assert "ZOOM: MEDIUM" in _prompt("front34", cat, sub)
        assert "the TIGHTEST of this product" in _prompt("closeup", cat, sub)
        poses = [re.search(r" POSE: ([^.]*)\.", _prompt(v, cat, sub)).group(1) for v in VIEWS]
        assert len(set(poses)) == len(VIEWS), sub


def test_a_backpack_back_is_the_strap_side_worn_and_its_back34_is_held_up():
    p = _prompt("back", *BACKPACK)
    assert "the STRAP SIDE as it looks worn" in p
    assert "NEVER worn on the chest" in p
    held = _prompt("back34", *BACKPACK)
    assert "HOLDS the backpack up by its top grab handle" in held
    assert "from the chest down to the knees, the held backpack" in held
    assert "OUTER FRONT PANEL faces the camera" in _prompt("front", *BACKPACK)


def test_a_handbag_is_not_a_backpack():
    p = _prompt("back", *BAG)
    assert "REAR PANEL" in p and "shoulder straps" not in p


def test_shoes_are_waist_down_and_their_back_is_the_heels():
    assert "from the WAIST down to the floor" in _prompt("front", *SHOES)
    back = _prompt("back", *SHOES)
    assert "the HEELS of the shoes face the camera" in back
    assert "MUST have a" not in back          # no body-size paragraph on a shoe crop
