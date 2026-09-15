"""Cross-field consistency rules.

Everything here is deterministic string/number comparison. No LLM. These catch
the class of error where each field is individually plausible but the set is
internally contradictory — e.g. a "Women Top" mannequin on a pair of men's jeans.
"""

from __future__ import annotations

import re
from typing import Any

from app.models import Finding, ProductSnapshot, Severity

_NUM = re.compile(r"(\d+)")


def _int(value: str | None) -> int | None:
    if not value:
        return None
    m = _NUM.search(str(value))
    return int(m.group(1)) if m else None


def _norm_code(value: str | None) -> str:
    """Strip decoration so 'ZONE A-02-12-3' and 'A-02-12-3' compare equal."""
    if not value:
        return ""
    v = str(value).upper()
    v = re.sub(r"\b(ZONE|BIN|LOC|LOCATION)\b", "", v)
    return re.sub(r"[^A-Z0-9]", "", v)


def _is_placeholder(value: Any, pol: dict[str, Any]) -> bool:
    return str(value or "").strip().lower() in pol["confidence"]["placeholders"]


def _alnum(value: str) -> str:
    """Letters and digits only, lowercased — 'T-Shirts' -> 'tshirts'.

    Used to compare a stored attribute against free text, where hyphens, spaces
    and case differ freely between the two.
    """
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


# --------------------------------------------------------------------------- #

def _taxonomy(p: ProductSnapshot, pol: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The category tree to validate against, and where it came from.

    The TENANT'S OWN tree whenever the feed supplied one. `pol["taxonomy"]` is a
    fallback for payloads without a catalog, and a poor one — a real tenant's Men
    tree has no `Tops`, `Outerwear` or `Footwear`, which is three of the five
    categories the policy file assumes, so validating a men's t-shirt
    (`T-Shirts & Polos`) against it fires TAX.002 on a perfectly good record.
    """
    # A catalog being PRESENT is what makes it authoritative — including when its
    # category tree is empty. That means "this tenant has configured no
    # categories", which is information, and falling back to a generic tree there
    # would flag every one of their products against a shop they are not.
    # policy.yaml is reached only when no catalog was supplied at all.
    if p.catalog is not None:
        return p.catalog.categories, "tenant"
    return pol["taxonomy"], "policy"


def check_taxonomy(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    tax, basis = _taxonomy(p, pol)

    # No tree to check against at all — say nothing rather than inventing one.
    if not tax:
        return out

    if p.master_category and p.master_category not in tax:
        out.append(Finding(
            rule_id="TAX.001", severity=Severity.HIGH, fields=["master_category"],
            message=f"Unknown master category '{p.master_category}'.",
            detail={"allowed": list(tax), "basis": basis}, needs_evidence=True,
        ))
        return out

    if p.master_category and p.category:
        cats = tax[p.master_category]
        if p.category not in cats:
            out.append(Finding(
                rule_id="TAX.002", severity=Severity.HIGH,
                fields=["master_category", "category"],
                message=f"'{p.category}' is not a valid category under "
                        f"'{p.master_category}'.",
                detail={"allowed": list(cats), "basis": basis},
                needs_evidence=True,
            ))
        # A category with NO configured subcategories cannot invalidate one —
        # `x not in []` is always true, which would flag every product under a
        # leaf category. Absent children means "nothing to check", not "wrong".
        elif p.subcategory and cats[p.category] and \
                p.subcategory not in cats[p.category]:
            out.append(Finding(
                rule_id="TAX.003", severity=Severity.MEDIUM,
                fields=["category", "subcategory"],
                message=f"'{p.subcategory}' is not a valid subcategory under "
                        f"'{p.master_category} > {p.category}'.",
                detail={"allowed": cats[p.category], "basis": basis},
                needs_evidence=True,
            ))

    # TAX.004 — gender must agree with master category.
    #
    # Gender is a SET, not a scalar. VNYX stores it as an array and
    # normalizeGenderToArray expands "unisex" into ['men', 'women'], which the
    # adapter joins to "men, women". Comparing that string to "men" would flag
    # every genuinely unisex product, so it is split back apart here.
    #
    # Agreement means: the master category appears in the set, OR the set covers
    # more than one gender (unisex stock legitimately sits under either), OR the
    # master category is itself Unisex.
    if p.gender and p.master_category:
        genders = {
            g.strip().lower() for g in str(p.gender).split(",") if g.strip()
        }
        master = p.master_category.strip().lower()
        agrees = (
            master in genders
            or len(genders) > 1
            or master == "unisex"
            or "unisex" in genders
        )
        if not agrees:
            out.append(Finding(
                rule_id="TAX.004", severity=Severity.HIGH,
                fields=["gender", "master_category"],
                message=f"Gender '{p.gender}' disagrees with master category "
                        f"'{p.master_category}'.",
                detail={"gender": sorted(genders), "master_category": master},
                needs_evidence=True,
            ))

    # Mannequin rig must match gender+category — this is the "Women Top on
    # men's jeans" defect.
    #
    # TWO PASSES, because the first one covers almost nothing.
    #
    # `mannequin_map` is keyed `{masterCategory}|{category}` and policy.yaml
    # holds seven of them: Men|Tops, Men|Outerwear, Men|Bottoms, Women|Tops,
    # Women|Outerwear, Women|Bottoms, Women|Dresses. A real tenant's categories
    # are Jackets, Sweaters & Hoodies, T-Shirts & Polos, Shirts, Vests,
    # Accessories, Footwear — of which only `Bottoms` is in that list. Measured
    # on production: of 1,762 products in review, 300 have a category the map
    # can key on at all. The other 83% were unjudged, and among them sat 255
    # wrong-GENDER mannequins and 21 wrong-SIDE ones — every one of which blocks
    # approval, because scripts/approve-products.ts checks this properly and
    # Hermes was reporting the products clean.
    #
    # So the map stays as the exact-name check where a tenant does use those
    # names, and the general case is derived the way approve-products.ts derives
    # it: the mannequin's own name carries its gender and its side, and the
    # product's taxonomy carries the side it needs. No category list required,
    # so it works on any tenant's tree.
    mannequin_flagged = False
    if p.mannequin and p.master_category and p.category:
        key = f"{p.master_category}|{p.category}"
        allowed = pol["mannequin_map"].get(key)
        if allowed and p.mannequin.strip() not in allowed:
            mannequin_flagged = True
            out.append(Finding(
                rule_id="TAX.005", severity=Severity.HIGH, fields=["mannequin"],
                message=f"Mannequin '{p.mannequin}' is wrong for {key.replace('|', ' > ')}; "
                        f"expected one of {allowed}.",
                detail={"allowed": allowed, "basis": "policy_map"},
            ))

    if p.mannequin and not mannequin_flagged:
        # Imported here rather than at module scope: gate.py is the approval-only
        # rule set and importing it eagerly from the shared registry would invert
        # the dependency the note at the top of that file describes.
        from app.rules.gate import _side_of, resolve_gender

        want_side = _side_of(p.subcategory, pol) or _side_of(p.category, pol)
        have_side = _side_of(p.mannequin, pol)
        rig_gender = resolve_gender(p.mannequin)
        master_gender = resolve_gender(p.master_category)

        problems: list[str] = []
        if rig_gender and master_gender and rig_gender != master_gender:
            problems.append(
                f"it is a {rig_gender}'s rig on a {master_gender}'s product")
        if want_side and have_side and want_side != have_side:
            problems.append(
                f"it is a {have_side}-body rig and "
                f"'{p.subcategory or p.category}' is a {want_side}")

        if problems:
            # THE RIG IS THE ANCHOR FOR GENDER. The side still comes from the
            # garment.
            #
            # This used to be `gender = master_category or rig_gender`, so a
            # Women Top rig on a product the extractor had filed under Men was
            # "fixed" by rewriting the RIG to Men Top. That is backwards on the
            # evidence:
            #
            #   the rig     an operator physically selected it in the booth and
            #               photographed the garment on it. A human action about
            #               the item in their hands.
            #   masterCategory
            #               the extraction model's choice of category BRANCH,
            #               made from photographs. For a genuinely unisex
            #               garment the prompt even instructs it to treat the
            #               item as the Men branch while reporting gender
            #               "Unisex" — so Men here is frequently an artefact of
            #               that instruction rather than a judgement.
            #
            # Preferring the branch over the rig produced exactly one coherent
            # field and three incoherent ones: CBOA-006175 ended up a Men Top
            # rig, gender women, masterCategory Men, a Women Uppers size chart
            # and female renders — and it APPROVED, because rewriting the rig is
            # what made TAX.005 pass. Four fields describing two different
            # garments, published.
            #
            # So the rig decides the gender, and masterCategory and the gender
            # property are brought to it. The side is a separate question — top
            # vs bottom is about the garment type, so it still comes from the
            # subcategory.
            side = want_side or have_side
            gender = rig_gender or master_gender
            suggested = None
            if side and gender:
                suggested = f"{gender.capitalize()} " + (
                    "Top" if side == "upper" else "Bottom")

            # WHAT THE OTHER TWO FIELDS SHOULD BECOME, and whether the tenant's
            # own taxonomy can actually hold it.
            #
            # Checked against `catalog.categories`, never assumed: a men's
            # category path is not guaranteed to exist under Women. If it does
            # not, the switch is NOT planned — a product with a valid path and a
            # wrong master category is recoverable, one pointing at a branch
            # that does not exist is not, and the finding still reports the
            # disagreement for a human.
            master_should_be = None
            taxonomy_ok = False
            if rig_gender and master_gender and rig_gender != master_gender:
                candidate = rig_gender.capitalize()
                subs = (p.catalog.categories.get(candidate) or {}).get(
                    p.category or ""
                )
                taxonomy_ok = bool(subs) and (
                    p.subcategory is None or p.subcategory in subs
                )
                if taxonomy_ok:
                    master_should_be = candidate

            out.append(Finding(
                rule_id="TAX.005", severity=Severity.HIGH, fields=["mannequin"],
                message=(f"Mannequin '{p.mannequin}' and "
                         f"{p.master_category} > {p.category} disagree: "
                         + "; ".join(problems) + "."
                         + (f" The rig is the anchor, so master category "
                            f"becomes '{master_should_be}'."
                            if master_should_be else "")
                         + (f" '{suggested}' is the matching rig."
                            if suggested and suggested != p.mannequin else "")),
                detail={"mannequin": p.mannequin, "suggested": suggested,
                        "product_side": want_side, "rig_side": have_side,
                        "product_gender": master_gender, "rig_gender": rig_gender,
                        "master_category_should_be": master_should_be,
                        "gender_should_be": rig_gender if master_should_be else None,
                        "taxonomy_supports_switch": taxonomy_ok,
                        "basis": "derived"},
            ))

    # TAX.006 — the sizing guide must be one the tenant actually configured.
    #
    # Checked by MEMBERSHIP in the tenant's guide list, not by deriving a name
    # from master+category. Real guide names are "Men Bottoms", "Men Uppers",
    # "Men DressShirts", "Women Uppers", "Defaults", "Kids" — so the derived form
    # only ever coincides for Bottoms. A men's t-shirt sits under
    # `Men > T-Shirts & Polos` and correctly uses "Men Uppers", which the old
    # rule reported as not matching "Men T-Shirts & Polos": a finding on every
    # top in the catalog.
    #
    # Without a catalog this cannot be checked at all, so it stays silent rather
    # than falling back to the derived guess.
    if p.sizing_guide and p.catalog and p.catalog.sizing_guides:
        known = {g.strip().lower() for g in p.catalog.sizing_guides}
        if p.sizing_guide.strip().lower() not in known:
            out.append(Finding(
                rule_id="TAX.006", severity=Severity.LOW, fields=["sizing_guide"],
                message=f"Sizing guide '{p.sizing_guide}' is not one of this "
                        f"tenant's configured guides.",
                detail={"allowed": sorted(p.catalog.sizing_guides)},
            ))
    return out


def check_sizing(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []
    sz = pol["sizing"]

    waist = _int(p.waist)
    size = _int(p.size)
    intl = _int(p.international_size)
    eu = _int(p.eu_size)

    # SIZE.001 — waist / size / international size must agree numerically.
    numeric = {"waist": waist, "size": size, "international_size": intl}
    present = {k: v for k, v in numeric.items() if v is not None}
    if len(set(present.values())) > 1:
        out.append(Finding(
            rule_id="SIZE.001", severity=Severity.HIGH,
            fields=list(present), message="Size fields disagree with each other.",
            detail=present, needs_evidence=True,
        ))

    # SIZE.002 — the EU size must be the one the product's OWN sizing guide pairs
    # with its size.
    #
    # The tenant's chart first, always. `ProductSize.euSizes` is index-aligned
    # with `sizes` (VNYX documents the alignment on the column), so this is a
    # lookup in the same table the edit screen's dropdown is built from — not a
    # conversion. It also covers the full range the tenant configured: their Men
    # Bottoms chart runs W21-W44, where the policy table has only 28-40 with gaps
    # at 35/37/39, so a W35 product went unchecked entirely.
    #
    # This matters most for women's bottoms, where the two disagree outright: the
    # tenant's chart maps W28 to EU 36 and W32 to EU 40, while the policy table
    # says 40 and 48. Validating a correct women's product against the policy
    # table fires SIZE.002 every time, off by up to 8 EU sizes.
    guide_expected = None
    if p.catalog:
        # Try the raw size first, then the waist restated as "W32" — stored
        # attributes use both spellings for the same value.
        for candidate in (p.size, p.waist, f"W{waist}" if waist else None):
            guide_expected = p.catalog.eu_for_size(p.sizing_guide, candidate)
            if guide_expected:
                break

    if guide_expected is not None and eu is not None:
        expected_int = _int(guide_expected)
        if expected_int and abs(expected_int - eu) > sz["eu_tolerance"]:
            out.append(Finding(
                rule_id="SIZE.002", severity=Severity.HIGH,
                fields=["eu_size", "size"],
                message=f"EU size {eu} does not match the '{p.sizing_guide}' chart, "
                        f"which pairs this size with EU {expected_int}.",
                detail={"eu_size": eu, "expected_eu": expected_int,
                        "sizing_guide": p.sizing_guide, "basis": "tenant_chart"},
            ))
    # Fallback: no catalog (raw webhook payload / fixture). The generic waist→EU
    # table is a guess at the tenant's chart, so it is used only when there is
    # nothing better, and `basis` records that it was.
    elif waist and eu and p.category and p.category.lower().startswith("bottom"):
        table_key = (
            "women_bottoms_waist_in_to_eu"
            if (p.master_category or "").lower() == "women"
            else "men_bottoms_waist_in_to_eu"
        )
        expected = sz[table_key].get(waist)
        if expected and abs(expected - eu) > sz["eu_tolerance"]:
            out.append(Finding(
                rule_id="SIZE.002", severity=Severity.HIGH,
                fields=["eu_size", "waist"],
                message=f"EU size {eu} does not convert from W{waist} "
                        f"(expected ~{expected}).",
                detail={"eu_size": eu, "waist": waist, "expected_eu": expected,
                        "basis": "policy_table"},
            ))

    # SIZE.003 — inseam plausibility.
    length = _int(p.length_size)
    lo, hi = sz["length_range_in"]
    if length is not None and not (lo <= length <= hi):
        out.append(Finding(
            rule_id="SIZE.003", severity=Severity.MEDIUM, fields=["length_size"],
            message=f"Inseam {length} is outside the plausible range {lo}–{hi}.",
            detail={"length_size": length},
        ))
    return out


def check_grading(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    out: list[Finding] = []

    # GRADE.001 — condition must restate the grade.
    #
    # In VNYX this is an EXACT invariant, not a heuristic: updateProduct syncs
    # properties.condition to the grade's own label on ANY grade write
    # (services/products.ts, updateConditionFromGrade), because condition is
    # published as a Shopify metafield and the two must never disagree. So when
    # the feed supplies `grade_label` we compare against that — the tenant's own
    # word for the grade — and a mismatch means the sync did not run (an older
    # row, or a direct DB edit).
    #
    # The condition_to_grade table is only the fallback for payloads without a
    # label. It cannot be authoritative: it is keyed on English condition names,
    # while grade labels are per-tenant free text, so a tenant using
    # "Excellent+" would be flagged on every product by the table alone.
    if p.condition and p.grade_label:
        if p.condition.strip().lower() != p.grade_label.strip().lower():
            out.append(Finding(
                rule_id="GRADE.001", severity=Severity.HIGH,
                fields=["condition", "grade"],
                message=f"Condition '{p.condition}' does not match the label for "
                        f"Grade {p.grade} ('{p.grade_label}'). These are kept in "
                        "sync on every grade write, and condition is published as "
                        "a metafield.",
                detail={"expected": p.grade_label, "actual": p.condition,
                        "grade": p.grade, "basis": "grade_label"},
            ))
    elif p.condition and p.grade:
        mapping = pol["pricing"]["condition_to_grade"]
        expected = mapping.get(p.condition.strip().lower())
        actual = p.grade.strip().upper()[:1]
        if expected and expected != actual:
            out.append(Finding(
                rule_id="GRADE.001", severity=Severity.HIGH,
                fields=["condition", "grade"],
                message=f"Condition '{p.condition}' maps to Grade {expected}, "
                        f"but the record says Grade {actual}.",
                detail={"expected": expected, "actual": actual,
                        "basis": "condition_to_grade"},
            ))

    # ATTR.004 — a condition that is not a condition.
    #
    # GRADE.001 above needs a grade to compare against. With none, a free-text
    # condition ("Good condition overall, small mark on hem") passes every rule
    # while being unusable as the facet it is published as. Checked against the
    # tenant's own grade label first — the record carries only its OWN grade's
    # label, so a match there is definitive — then against the policy's condition
    # vocabulary. MEDIUM: the value needs a reviewer's eye, the product may be fine.
    elif p.condition and not _is_placeholder(p.condition, pol):
        known = {k.strip().lower() for k in pol["pricing"]["condition_to_grade"]}
        if p.grade_label:
            known.add(p.grade_label.strip().lower())
        if p.condition.strip().lower() not in known:
            out.append(Finding(
                rule_id="ATTR.004", severity=Severity.MEDIUM, fields=["condition"],
                message=(f"Condition '{p.condition}' is not one of the known "
                         f"condition labels and there is no grade to derive it "
                         f"from."),
                detail={"value": p.condition, "known": sorted(known)},
                needs_evidence=True,
            ))

    # Defects: the operator's list REPLACES the AI's in VNYX when present
    # (someone holding the garment outranks a model looking at a photo of it), so
    # "no defects recorded" has to consider both or a fully operator-reported
    # product reads as defect-free.
    all_defects = list(p.defects) + [
        d for d in p.operator_defects if d not in p.defects
    ]

    letter = (p.grade or "").strip().upper()[:1]
    if letter == "A" and all_defects:
        out.append(Finding(
            rule_id="GRADE.002", severity=Severity.HIGH, fields=["grade", "defects"],
            message=f"Grade A claims as-new condition but {len(all_defects)} "
                    "defect(s) are recorded.",
            detail={"defects": all_defects}, needs_evidence=True,
        ))
    if letter in {"C", "D"} and not all_defects:
        out.append(Finding(
            rule_id="GRADE.003", severity=Severity.MEDIUM, fields=["grade", "defects"],
            message=f"Grade {letter} implies visible wear but no defects were logged.",
            detail={}, needs_evidence=True,
        ))
    return out


def check_identity(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """Barcode / location integrity. These are physical facts — never auto-fixed."""
    out: list[Finding] = []

    # ID.001 — bin location vs a second copy of the same LOCATION code.
    #
    # Dormant against VNYX, on purpose. VNYX has no second copy of the location
    # code: `binCode` is the location ("A-04-12-2") and the printed barcode
    # encodes `binNumber`, a 10-digit scan id. Those two never match by
    # construction, so mapping bin_barcode to binNumber would fire this CRITICAL
    # rule on every product with a bin. The adapter therefore leaves bin_barcode
    # None and this rule sits idle until a field genuinely holding a duplicate
    # location code exists. ID.004 below is the check that IS grounded in the
    # real schema.
    if p.bin_code and p.bin_barcode:
        if _norm_code(p.bin_code) != _norm_code(p.bin_barcode):
            out.append(Finding(
                rule_id="ID.001", severity=Severity.CRITICAL,
                fields=["bin_code", "bin_barcode"],
                message=(
                    f"Bin location '{p.bin_code}' does not match the printed bin "
                    f"barcode '{p.bin_barcode}'. Pickers will be sent to the wrong shelf."
                ),
                detail={"bin_code": p.bin_code, "bin_barcode": p.bin_barcode},
            ))

    # ID.004 — the bin's location code must sit inside the zone it belongs to.
    #
    # VNYX generates binCode from the zone plus aisle/rack/level, so the zone code
    # is a prefix of it. A product whose binCode says one zone while the bin's
    # own zone row says another means the placement and the location string
    # disagree — a picker following either one may go to the wrong aisle.
    #
    # Prefix rather than equality: binCode carries aisle/rack/level after the zone
    # segment, so the zone is contained in it, not equal to it.
    if p.bin_code and p.bin_zone_code:
        code = _norm_code(p.bin_code)
        zone = _norm_code(p.bin_zone_code)
        if zone and not code.startswith(zone):
            out.append(Finding(
                rule_id="ID.004", severity=Severity.HIGH,
                fields=["bin_code", "bin_zone_code"],
                message=(
                    f"Bin code '{p.bin_code}' does not belong to zone "
                    f"'{p.bin_zone_code}'. The location string and the bin's zone "
                    "disagree about where this product physically is."
                ),
                detail={"bin_code": p.bin_code, "bin_zone_code": p.bin_zone_code,
                        "warehouse": p.bin_warehouse_code},
            ))

    if p.lpn_code and p.sku and _norm_code(p.lpn_code) == _norm_code(p.sku):
        out.append(Finding(
            rule_id="ID.002", severity=Severity.MEDIUM, fields=["lpn_code", "sku"],
            message="LPN and SKU are identical; they should be distinct identifiers.",
            detail={},
        ))

    # ID.003 — a required identifier is missing or a placeholder.
    #
    # `sku` and `product_code` only. `lpn_code` and `bin_code` are deliberately
    # NOT required: a product that has not been put away yet has neither, and in
    # the review queue that is the norm rather than a defect — putaway happens
    # after review. Including them fired two HIGH findings on every un-binned
    # product, which is most of the queue.
    for field in ("sku", "product_code"):
        if _is_placeholder(getattr(p, field), pol):
            out.append(Finding(
                rule_id="ID.003", severity=Severity.HIGH, fields=[field],
                message=f"Identifier '{field}' is empty or a placeholder.",
                detail={},
            ))

    # A bin/LPN code that is PRESENT but holds a placeholder string is still a
    # real problem — that is a written value, not an absent one.
    for field in ("lpn_code", "bin_code"):
        value = getattr(p, field)
        if value is not None and _is_placeholder(value, pol):
            out.append(Finding(
                rule_id="ID.003", severity=Severity.HIGH, fields=[field],
                message=f"Identifier '{field}' holds a placeholder value "
                        f"({value!r}).",
                detail={},
            ))
    return out


def check_copy(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """Title/description agreement with the structured fields (token-level only).

    Deeper semantic contradiction is handled by the LLM layer; this catches the
    cheap, unambiguous cases first so we don't pay for a model call.
    """
    out: list[Finding] = []
    title = (p.title or "").lower()

    if not title:
        out.append(Finding(
            rule_id="TEXT.001", severity=Severity.HIGH, fields=["title"],
            message="Title is empty.", detail={}, needs_evidence=True,
        ))
        return out

    # TEXT.006 — the title the pipeline wrote at creation and never replaced.
    #
    # "Generating Product..." is written when the row is created and overwritten
    # when generation succeeds; still carrying it means nothing was produced. On
    # one 61-product batch that was 35 rows, every one of which would otherwise
    # have shipped under the placeholder. HIGH: this is not copy quality, it is
    # a product with no title.
    prefixes = [str(x).strip().lower()
                for x in (pol.get("copy") or {}).get("placeholder_title_prefixes") or []]
    hit = next((x for x in prefixes if x and title.strip().startswith(x)), None)
    if hit:
        out.append(Finding(
            rule_id="TEXT.006", severity=Severity.HIGH, fields=["title"],
            message=(f"Title still carries the generation placeholder "
                     f"('{p.title}'); the pipeline never replaced it."),
            detail={"prefix": hit},
        ))
        return out

    # TEXT.007 — the copy says the other gender.
    #
    # A men's product whose title or description says "women's" is either a
    # C-twin whose copy was inherited from its parent, or a product filed under
    # the wrong gender; both are worth a look. Only a CONTRADICTION fires: the
    # opposite word present and the product's own gender word absent, so a
    # "Unisex — men / women" description stays quiet.
    gender = _gender_side(p.gender)
    if gender:
        own = _MEN_WORDS if gender == "men" else _WOMEN_WORDS
        other = _WOMEN_WORDS if gender == "men" else _MEN_WORDS
        for field, text in (("title", p.title), ("description", p.description)):
            words = set(re.findall(r"[a-z']+", (text or "").lower()))
            if words & other and not words & own:
                out.append(Finding(
                    rule_id="TEXT.007", severity=Severity.MEDIUM,
                    fields=[field, "gender"],
                    message=(f"The {field} says "
                             f"'{sorted(words & other)[0]}' but the product is "
                             f"listed as {gender}."),
                    detail={"found": sorted(words & other), "gender": gender},
                ))

    # TEXT.002 — the title should name the brand, colour and subcategory.
    #
    # Matched on a normalised STEM, not a raw token. Titles are written in the
    # singular while categories are plural, and both carry punctuation: a
    # "Nike T-Shirt in Blue size L" under subcategory "T-Shirts" is perfectly
    # consistent, but a literal `"t-shirts" in title` test misses it. Stripping
    # non-alphanumerics and a trailing plural makes "T-Shirts" -> "tshirt", which
    # is present in "nike tshirt in blue size l".
    #
    # These are cosmetic copy checks, so a false positive here is expensive
    # relative to its value — it would mark an otherwise perfect record incorrect.
    title_norm = _alnum(title)
    for field in ("brand", "color", "subcategory"):
        value = getattr(p, field)
        if not value or _is_placeholder(value, pol):
            continue
        token = str(value).split()[0]
        stem = _alnum(token)
        if not stem:
            continue
        # Accept the stem, or its singular/plural counterpart.
        variants = {stem, stem.rstrip("s")} | {stem + "s"}
        if not any(v and v in title_norm for v in variants):
            out.append(Finding(
                rule_id="TEXT.002", severity=Severity.LOW, fields=["title", field],
                message=f"Title does not mention {field} '{value}'.",
                detail={"token": token, "stem": stem},
            ))

    size_in_title = _int(title)
    size_field = _int(p.size) or _int(p.waist)
    if size_in_title and size_field and size_in_title != size_field:
        out.append(Finding(
            rule_id="TEXT.003", severity=Severity.HIGH, fields=["title", "size"],
            message=f"Title advertises size {size_in_title} but the size field "
                    f"says {size_field}.",
            detail={},
        ))

    if not (p.description or "").strip():
        out.append(Finding(
            rule_id="TEXT.004", severity=Severity.MEDIUM, fields=["description"],
            message="Description is empty.", detail={}, needs_evidence=True,
        ))

    # TEXT.005 — `material` is a fibre, not a paragraph.
    #
    # The bulk extractor sometimes pastes the whole composition line into the
    # field ("100% Cotton. Made in Portugal. Machine wash cold…"), which the
    # storefront then renders as a facet value. LOW: cosmetic, and the fix is a
    # reviewer trimming it, not a rule guessing which words to keep.
    max_chars = int((pol.get("copy") or {}).get("material_max_chars") or 0)
    if max_chars and p.material and len(p.material.strip()) > max_chars:
        out.append(Finding(
            rule_id="TEXT.005", severity=Severity.LOW, fields=["material"],
            message=(f"Material is {len(p.material.strip())} characters — a "
                     f"sentence, not a fibre (limit {max_chars})."),
            detail={"length": len(p.material.strip()), "limit": max_chars},
        ))
    return out


_MEN_WORDS = {"men", "men's", "mens", "man", "man's", "male", "gents"}
_WOMEN_WORDS = {"women", "women's", "womens", "woman", "woman's", "female",
                "ladies", "ladies'"}


def _gender_side(value: Any) -> str | None:
    """'men' | 'women' | None — a local mirror of rules/gate.py's resolver.

    Local rather than imported: consistency.py is imported by gate.py's
    neighbours and a cross-import between rule modules is the kind of cycle that
    only shows up at startup.
    """
    if value is None:
        return None
    parts: list[str]
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        text = str(value).strip()
        if text.startswith("["):
            parts = [x.strip(" \"'") for x in text.strip("[]").split(",")]
        else:
            parts = text.split(",")
    sides = set()
    for part in parts:
        low = part.strip().lower()
        if low in _WOMEN_WORDS or low.startswith("women") or low.startswith("female"):
            sides.add("women")
        elif low in _MEN_WORDS or low.startswith("men") or low.startswith("male"):
            sides.add("men")
    return sides.pop() if len(sides) == 1 else None


def check_completeness(p: ProductSnapshot, pol: dict[str, Any]) -> list[Finding]:
    """Placeholder values and low-confidence fields."""
    out: list[Finding] = []
    threshold = pol["confidence"]["review_threshold"]

    # `price` is deliberately absent: PRICE.010 already reports a missing price as
    # CRITICAL with the full assessment attached, and listing it here too produced
    # a second, vaguer MEDIUM finding for the same fact.
    required = ["brand", "color", "material", "size", "condition"]
    for field in required:
        if _is_placeholder(getattr(p, field, None), pol):
            out.append(Finding(
                rule_id="DATA.001", severity=Severity.MEDIUM, fields=[field],
                message=f"Required field '{field}' holds a placeholder value.",
                detail={}, needs_evidence=True,
            ))

    for field, score in p.confidence.items():
        if score < threshold and not p.is_locked(field):
            out.append(Finding(
                rule_id="CONF.001", severity=Severity.LOW, fields=[field],
                message=f"Field '{field}' has low extraction confidence "
                        f"({score:.0%} < {threshold:.0%}).",
                detail={"confidence": score}, needs_evidence=True,
            ))

    if p.inventory is not None and p.inventory < 0:
        out.append(Finding(
            rule_id="DATA.002", severity=Severity.HIGH, fields=["inventory"],
            message="Inventory is negative.", detail={"inventory": p.inventory},
        ))

    # DATA.003 — vintage is one-of-one.
    #
    # A quantity other than the configured one on a product heading for approval
    # is a data-entry slip: 0 means there is nothing to sell, 3 means the same
    # garment will oversell twice. MEDIUM so a multi-unit tenant is warned rather
    # than blocked; `completeness.expected_inventory: null` switches it off.
    expected = (pol.get("completeness") or {}).get("expected_inventory")
    if (expected is not None and p.inventory is not None and p.inventory >= 0
            and int(p.inventory) != int(expected)):
        out.append(Finding(
            rule_id="DATA.003", severity=Severity.MEDIUM, fields=["inventory"],
            message=(f"Inventory is {p.inventory}; a one-of-one garment should "
                     f"carry {expected}."),
            detail={"inventory": p.inventory, "expected": expected},
        ))
    return out
