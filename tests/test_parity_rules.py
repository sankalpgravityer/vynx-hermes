"""The rules ported from vnyx-auto-approve in WP3 — one case per finding, and
the false-positive traps each was written to avoid.

  TEXT.005  material is a sentence        TEXT.006  placeholder title
  TEXT.007  copy says the other gender    DATA.003  inventory is not one
  PRICE.012 retail below the floor        ATTR.004  condition is not a condition
  ATTR.005  supplier not canonical        IMG.023 / IMG.024  the gallery's lead
"""
from __future__ import annotations

import pytest

from app.config import policy
from app.models import MediaAsset, ProductSnapshot, TenantCatalog
from app.rules import catalog as catalog_rules
from app.rules import consistency, imagery, pricing


@pytest.fixture(scope="module")
def pol() -> dict:
    return policy()


def snap(**kw) -> ProductSnapshot:
    base = dict(
        id="p1", tenant_id="t1", sku="BOA-1", title="Vintage Levi's Jeans Men W32",
        description="Classic straight men's jeans in mid blue.",
        master_category="Men", category="Bottoms", subcategory="Jeans",
        gender="men", brand="Levi's", color="Blue", material="Cotton",
        size="32", condition="Good", grade="B", grade_label="Good",
        price=29.99, retail_price=79.99, currency="EUR", inventory=1,
    )
    base.update(kw)
    return ProductSnapshot(**base)


def ids(findings) -> set[str]:
    return {f.rule_id for f in findings}


# ----------------------------------------------------------------- TEXT.005

def test_material_that_is_a_sentence_is_low(pol):
    long = "100% Cotton. Made in Portugal. Machine wash cold, do not tumble dry."
    f = consistency.check_copy(snap(material=long), pol)
    assert "TEXT.005" in ids(f)
    assert all(x.severity.value == "low" for x in f if x.rule_id == "TEXT.005")


def test_a_fibre_is_fine(pol):
    assert "TEXT.005" not in ids(consistency.check_copy(snap(material="Cotton"), pol))


# ----------------------------------------------------------------- TEXT.006

def test_placeholder_title_is_high_and_stops_the_copy_checks(pol):
    f = consistency.check_copy(snap(title="Generating Product..."), pol)
    assert ids(f) == {"TEXT.006"}
    assert f[0].severity.value == "high"


def test_placeholder_match_is_prefix_and_case_insensitive(pol):
    assert "TEXT.006" in ids(consistency.check_copy(snap(title="generating product 12"), pol))
    assert "TEXT.006" not in ids(consistency.check_copy(snap(title="Product generating buzz"), pol))


# ----------------------------------------------------------------- TEXT.007

def test_copy_saying_the_other_gender_is_flagged_per_field(pol):
    f = consistency.check_copy(
        snap(title="Vintage Levi's Jeans Women W32",
             description="Straight women's jeans."), pol)
    hits = [x for x in f if x.rule_id == "TEXT.007"]
    assert {tuple(x.fields) for x in hits} == {("title", "gender"), ("description", "gender")}
    assert all(x.severity.value == "medium" for x in hits)


def test_copy_naming_both_genders_is_not_a_contradiction(pol):
    f = consistency.check_copy(
        snap(description="Unisex fit — sized for men, also worn by women."), pol)
    assert "TEXT.007" not in ids(f)


def test_gender_shapes_the_record_uses_all_resolve(pol):
    # The snapshot field is a string; the JSON-array spelling is how VNYX stores
    # the list form, and to_snapshot hands it through verbatim.
    for stored in ('["men"]', "Men", "men", "men,men"):
        f = consistency.check_copy(snap(gender=stored, title="Ladies Jeans W32"), pol)
        assert "TEXT.007" in ids(f), stored
    assert consistency._gender_side(["women"]) == "women"
    assert consistency._gender_side('["men", "women"]') is None


def test_no_product_gender_means_no_copy_gender_check(pol):
    f = consistency.check_copy(snap(gender=None, title="Women's Jeans W32"), pol)
    assert "TEXT.007" not in ids(f)


# ----------------------------------------------------------------- DATA.003

def test_inventory_other_than_one_is_medium(pol):
    for qty in (0, 3):
        f = consistency.check_completeness(snap(inventory=qty), pol)
        assert "DATA.003" in ids(f), qty
    assert "DATA.003" not in ids(consistency.check_completeness(snap(inventory=1), pol))


def test_negative_inventory_is_data_002_not_003(pol):
    f = consistency.check_completeness(snap(inventory=-1), pol)
    assert "DATA.002" in ids(f) and "DATA.003" not in ids(f)


def test_inventory_rule_can_be_switched_off(pol):
    off = {**pol, "completeness": {"expected_inventory": None}}
    assert "DATA.003" not in ids(consistency.check_completeness(snap(inventory=5), off))


# ---------------------------------------------------------------- PRICE.012

def test_retail_below_the_floor_is_reported(pol):
    f = pricing.check(snap(retail_price=2.0, price=1.0), pol)
    assert "PRICE.012" in ids(f)


def test_retail_above_the_floor_is_not(pol):
    assert "PRICE.012" not in ids(pricing.check(snap(), pol))


# ----------------------------------------------------------------- ATTR.004

def test_free_text_condition_with_no_grade_is_medium(pol):
    f = consistency.check_grading(
        snap(condition="Good condition overall, small mark on hem",
             grade=None, grade_label=None), pol)
    assert "ATTR.004" in ids(f)


def test_known_condition_word_without_a_grade_is_fine(pol):
    f = consistency.check_grading(snap(condition="Good", grade=None, grade_label=None), pol)
    assert "ATTR.004" not in ids(f)


def test_condition_matching_its_grade_label_is_grade_001_territory_not_attr_004(pol):
    # With a grade label present GRADE.001 owns the comparison; ATTR.004 is silent.
    f = consistency.check_grading(snap(condition="Lived In", grade="C", grade_label="Lived In"), pol)
    assert "ATTR.004" not in ids(f)
    f = consistency.check_grading(snap(condition="Excellent+", grade="A", grade_label="As New"), pol)
    assert "GRADE.001" in ids(f) and "ATTR.004" not in ids(f)


# ----------------------------------------------------------------- ATTR.005

def test_supplier_is_judged_only_for_a_configured_tenant(pol):
    cat = TenantCatalog(brands=["Levi's"], colors=["Blue"], materials=["Cotton"])
    p = snap(supplier="Some Other Wholesaler", catalog=cat)
    assert "ATTR.005" not in ids(catalog_rules.check_catalog(p, pol))
    configured = {**pol, "suppliers": {"canonical_by_tenant": {"t1": "Bank & Vogue"}}}
    f = catalog_rules.check_catalog(p, configured)
    assert "ATTR.005" in ids(f)
    # Typography does not count as a different supplier.
    p2 = snap(supplier="bank and vogue".replace("and", "&"), catalog=cat)
    assert "ATTR.005" not in ids(catalog_rules.check_catalog(p2, configured))


# ------------------------------------------------------------ IMG.023 / 024

def media(view, processing="RAW", url=None, position=0):
    return MediaAsset(url=url or f"https://r2/{view.lower()}-{position}.jpg",
                      view=view, processing=processing, position=position)


def test_gallery_leading_with_a_label_is_medium(pol):
    rows = [media("LABEL", url="https://r2/label.jpg", position=0),
            media("FRONT", "BG_REMOVED", url="https://r2/front.png", position=1),
            media("AI_FRONT", "GENERATED", url="https://r2/ai.jpg", position=2)]
    p = snap(media=rows, images=["https://r2/label.jpg", "https://r2/front.png"])
    f = imagery.check_imagery(p, pol)
    assert "IMG.024" in ids(f)


def test_gallery_leading_with_a_raw_photo_while_a_cutout_exists_is_low(pol):
    rows = [media("FRONT", "RAW", url="https://r2/raw.jpg", position=0),
            media("BACK", "BG_REMOVED", url="https://r2/back.png", position=1)]
    p = snap(media=rows, images=["https://r2/raw.jpg", "https://r2/back.png"])
    f = imagery.check_imagery(p, pol)
    assert "IMG.023" in ids(f)
    assert "IMG.024" not in ids(f)


def test_gallery_leading_with_the_cutout_is_fine(pol):
    rows = [media("FRONT", "BG_REMOVED", url="https://r2/front.png", position=0),
            media("LABEL", url="https://r2/label.jpg", position=9)]
    p = snap(media=rows, images=["https://r2/front.png", "https://r2/label.jpg"])
    f = imagery.check_imagery(p, pol)
    assert not {"IMG.023", "IMG.024"} & ids(f)


def test_no_images_cache_means_no_lead_rule(pol):
    rows = [media("LABEL", url="https://r2/label.jpg", position=0)]
    assert not {"IMG.023", "IMG.024"} & ids(imagery.check_imagery(snap(media=rows, images=[]), pol))
