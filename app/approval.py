"""The approval gate — judgment and repair COMPUTATION, no writes.

    verify -> plan

One product, judged against everything that must be true before it may leave
Review. Where /v1/review-queue reports and stops, this also computes the exact
repair for each thing it found, as a typed plan the caller executes.

WHY IT WRITES NOTHING

Hermes holds no VNYX credentials, no database URL and no R2 keys, and this
endpoint does not change that. It is the same contract /v1/review-queue and
/v1/imagery/verify already keep: everything needed arrives in the request body,
so Hermes cannot reach a product the caller was not already authorised to read.

An earlier version of this module DID write, through a VnyxClient carrying a
`VNYX_API_TOKEN`. That failed on contact with reality: vnyx-api authenticates
with short-lived JWT access tokens, so a static token in Hermes' env expires
within the hour and the writes start failing quietly. The alternative — a
database URL — would mean reimplementing tenant scoping, the `properties` merge,
the ProductVariant price mirror, recordStageTransition and the ProductMedia
invariants in Python, as a second copy that drifts from the TypeScript one.

So the division is:

    Hermes computes the VALUE          vnyx-api WRITES it
    ─────────────────────────          ──────────────────
    corrected price (grade factor)     updateProduct
    euSize from the tenant's chart     properties merge
    taxonomy / sizingGuide / grade     updateProduct
    seed rows for a missing chart      prisma.productSize
    model-image BYTES                  R2 upload + addImages
      (via /v1/imagery/generate)

That holds for the generated things too, which is the point: a render is
synthesised HERE, because the prompt logic and the image-model keys live here,
and the bytes travel back for vnyx-api to store. Hermes does the work; the
caller does the writing.

RE-VERIFY is the caller's second call. Ask again after executing the plan and
`ready` is computed from the stored record rather than from the plan — which
matters because a repair can fail silently, and a gate that trusted its own plan
would approve products whose fixes never landed.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from app.config import policy
from app.models import Finding, ProductSnapshot, Severity
from app.rules import run_all
from app.rules import gate as gate_rules
from app.rules import imagery as imagery_rules
from app.rules.pricing import assess, verify_invariants
from app.vnyx_client import to_snapshot

log = logging.getLogger("approval-gate")

# Severity at or above which a finding stops an approval. HIGH, not MEDIUM: the
# cosmetic copy rules (TEXT.*) and the extraction-confidence rules (CONF.*) are
# advisories a reviewer routinely ships, and letting them block would mean the
# gate never approves anything.
_BLOCKING_FLOOR = Severity.HIGH

_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}

# Snapshot fields that live in VNYX's `properties` Json map rather than as
# columns. The caller has to merge these rather than PUT them, so the plan says
# which kind of write each repair needs instead of making the caller guess.
_PROPERTY_FIELDS = {
    "gender", "brand", "color", "material", "size", "eu_size", "waist",
    "length_size", "fit", "condition", "model", "supplier",
}

# Property actions carry the CANONICAL snapshot field name (`eu_size`), not a
# guessed storage key.
#
# There used to be a map turning `eu_size` into `euSize` here. Both spellings are
# live in stored rows, and vnyx-api's PROPERTY_ALIASES gives `eu_size`
# PRECEDENCE when reading — so writing `euSize` created a SECOND key beside the
# original, the reader kept returning the stale value, and SIZE.002 survived its
# own repair. The caller resolves the name against the same alias table it reads
# through, which is the only way the two can agree.
_COLUMN_KEYS = {
    "retail_price": "retailPrice",
    "master_category": "masterCategory",
    "subcategory": "subCategory",
    "sizing_guide": "sizingGuide",
    "mannequin": "mannequinType",
    "description": "summary",
    # `internationalSize` is a real column as well as a `properties` key, and
    # DRIFT.001 can plan a write to either side. Without this entry _apply_plan
    # cannot map the storage name back onto the snapshot, so the shadow re-run
    # sees the un-repaired value.
    "international_size": "internationalSize",
}

# The imagery rules that mean "renders are missing", as opposed to the ones about
# a mislabelled or unmatted image.
_MISSING_RENDER_RULES = {"IMG.001", "IMG.002", "IMG.003", "IMG.004", "IMG.005"}


def _blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if _RANK[f.severity] >= _RANK[_BLOCKING_FLOOR]]


_SEVERITY_BY_NAME: dict[str, Severity] = {s.value: s for s in Severity}

# Values that mean "drop this finding entirely" rather than "re-rank it".
_DROP_TOKENS = frozenset({"ignore", "off", "none", "skip", "false"})


def apply_severity_overrides(
    findings: list[Finding], overrides: dict[str, str] | None
) -> list[Finding]:
    """Re-rank or drop findings per the tenant's Brain configuration.

    WHY THIS EXISTS. `AutoApprovalConfig.severityOverrides` has been carried
    from the Brain screen, through vnyx-api, into the run's frozen
    configSnapshot and on into `RunSettings.severity_overrides` since the
    feature shipped — and then dropped on the floor, because nothing ever read
    it. A setting a user can change that provably does nothing is worse than no
    setting at all.

    TWO KEY FORMS, and the field-scoped one is the point:

        "DATA.010"           every finding from that rule
        "DATA.010:material"  only findings naming that field

    DATA.010 covers ten required fields — brand, size, eu_size, the three
    taxonomy levels, color, material, condition, gender. A tenant who does not
    record fibre composition needs to stop blocking on `material` WITHOUT also
    stopping blocking on a missing category, so a rule-level override is far too
    blunt to express the common case. Most specific wins.

    VALUES are a severity name (`low`/`medium`/`high`/`critical`) or one of
    `ignore`/`off`/`none`/`skip` to remove the finding outright. Downgrading
    below the blocking floor is the normal way to stop something blocking while
    keeping it visible in the report; dropping is for a rule a tenant considers
    genuinely inapplicable.

    UNKNOWN KEYS AND VALUES ARE IGNORED, deliberately. This runs inside the
    verification path for every product, and a typo in a JSON config column must
    not raise there — the cost of a silently ineffective override is one puzzled
    user, and the cost of an exception is a tenant whose queue stops draining.
    """
    if not overrides:
        return findings

    # Normalise once, not per finding: this is called on every product.
    by_rule: dict[str, str] = {}
    by_rule_field: dict[tuple[str, str], str] = {}
    for raw_key, raw_val in overrides.items():
        key = str(raw_key or "").strip()
        val = str(raw_val or "").strip().lower()
        if not key or not val:
            continue
        if ":" in key:
            rule, _, field = key.partition(":")
            by_rule_field[(rule.strip().upper(), field.strip().lower())] = val
        else:
            by_rule[key.upper()] = val

    out: list[Finding] = []
    for f in findings:
        rule = (f.rule_id or "").upper()
        want: str | None = None

        # Field-scoped first — "most specific wins" is the whole reason the
        # two-part key exists.
        for field in f.fields or ():
            hit = by_rule_field.get((rule, str(field).strip().lower()))
            if hit:
                want = hit
                break
        if want is None:
            want = by_rule.get(rule)

        if want is None:
            out.append(f)
            continue
        if want in _DROP_TOKENS:
            continue

        target = _SEVERITY_BY_NAME.get(want)
        if target is None:
            # Unrecognised severity name: leave the finding exactly as the rule
            # produced it rather than guessing what was meant.
            out.append(f)
            continue

        # model_copy, not mutation: `findings` is rebound against a SHADOW
        # product further down run_gate, and a mutated Finding would leak the
        # override into whatever else holds a reference to the same object.
        out.append(f.model_copy(update={"severity": target}))

    return out


def _all_findings(p: ProductSnapshot, pol: dict[str, Any], *,
                  split_on_both_genders: bool = False,
                  severity_overrides: dict[str, str] | None = None,
                  ) -> list[Finding]:
    """The standard rule set plus the gate-only rules.

    `check_gate` is called explicitly rather than registered in REGISTRY — see
    the note at the top of rules/gate.py. Adding it there would change what
    /v1/review-queue reports for every existing caller.

    The overrides are applied HERE rather than at run_gate's top, because
    run_gate calls this three times — once for the stored record, once against
    the shadow product after a guide switch is planned, and once for the
    residual check — and a re-ranking that applied to only the first would make
    `blocking` and `field_issues` disagree about the same finding.
    """
    return apply_severity_overrides(
        run_all(p, pol) + gate_rules.check_gate(
            p, pol, split_on_both_genders=split_on_both_genders
        ),
        severity_overrides,
    )


# --------------------------------------------------------------------------- #
# Plan building
# --------------------------------------------------------------------------- #

def _apply_plan(p: ProductSnapshot,
                plan: list[dict[str, Any]]) -> ProductSnapshot:
    """The product as it WOULD be once the plan is executed.

    Only the field writes — a created chart or a generated render changes nothing
    the attribute rules read. Storage keys are mapped back to snapshot field names
    so a `set_column subCategory` lands on `subcategory`.
    """
    reverse = {v: k for k, v in _COLUMN_KEYS.items()}
    updates: dict[str, Any] = {}
    for a in plan:
        if a["kind"] not in ("set_column", "set_property"):
            continue
        field = a.get("field")
        if not field:
            continue
        name = reverse.get(field, field)
        if hasattr(p, name):
            updates[name] = a.get("value")
    return p.model_copy(update=updates) if updates else p


def _plan_size_chart(p: ProductSnapshot, pol: dict[str, Any],
                     findings: list[Finding],
                     plan: list[dict[str, Any]]) -> None:
    """Point the product at a chart, or ask for one to be created.

    Two cases, and the cheap one is also the only one that cannot be wrong:

      1. The tenant already has a chart covering this product and the product is
         simply not pointed at it. A LOOKUP — always safe.

      2. No chart exists, so one has to be seeded from policy.yaml's waist->EU
         table. That is a GUESS: policy maps W28 to EU 40 where a real tenant's
         chart says 36. The action carries `provenance: hermes_generated` and the
         caller names the chart with an `(auto)` suffix, so every product later
         sized against a guessed chart stays queryable. See
         rules/gate.py:seed_chart.
    """
    finding = next((f for f in findings if f.rule_id == "SIZE.010"), None)
    if finding is None:
        return

    existing = gate_rules.resolve_guide(p, pol)
    if existing:
        plan.append({
            "kind": "set_column", "field": "sizingGuide", "value": existing,
            "reason": "SIZE.010", "basis": "tenant_chart",
        })
        return

    candidates = finding.detail.get("candidates") or []
    if candidates:
        # Several of the tenant's charts list this size ("Defaults" and "Men
        # Uppers" may both carry "S"). The tenant is configured correctly; only a
        # human knows which chart this category belongs to, and attaching the
        # wrong size table is worse than attaching none.
        plan.append({
            "kind": "escalate", "field": "sizingGuide", "reason": "SIZE.010",
            "detail": f"{len(candidates)} charts match this size "
                      f"({', '.join(candidates)}); pick one",
        })
        return

    name = finding.detail.get("expected_guide")
    seed = finding.detail.get("seed")
    if not name or not seed:
        # No taxonomy to name a chart from, or no table that could fill one — an
        # alpha chart (XS/S/M/L) carries no EU equivalence to invent.
        plan.append({
            "kind": "escalate", "field": "sizingGuide", "reason": "SIZE.010",
            "detail": "no chart exists and none can be seeded for this category",
        })
        return

    plan.append({
        "kind": "create_size_chart",
        "name": f"{name} (auto)",
        "sizes": seed["sizes"],
        "euSizes": seed["euSizes"],
        "reason": "SIZE.010",
        "basis": seed["basis"],
        "provenance": seed["provenance"],
    })


# Charts whose NAME marks them as specialised, and the words a garment has to
# carry for one to be the right answer. Anything not listed here is general.
#
# Keyed on a squashed lowercase name so "Men DressShirts", "Men Dress Shirts"
# and "men-dressshirts" all match the same entry -- guide names are tenant text,
# not an enum.
_SPECIALISED_GUIDES: dict[str, tuple[str, ...]] = {
    "dressshirts": ("dress shirt", "business shirt", "formal shirt"),
}


def _squash(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _pick_guide(p: ProductSnapshot, candidates: list[str]) -> str | None:
    """One chart out of several that all fit, or None to leave it to a human.

    WHY THIS EXISTS. SIZE.011/013/014 can name more than one chart whose ladder
    covers the product, and every one of those used to escalate. On a real
    catalogue that is most of the tops: BOAS has both "Men Uppers" and
    "Men DressShirts", both list S-XXL, so every men's t-shirt reported "2 charts
    fit this product; pick one" and waited for a person who would answer "Men
    Uppers" every time.

    THE RULE IS SPECIFIC-BEATS-GENERAL, NOT A HARDCODED NAME. A blanket "always
    prefer Uppers" would file an actual business shirt on the general chart,
    which is the same class of error the escalation was protecting against --
    just silent. So a specialised chart wins only when the garment's own
    taxonomy names its domain; otherwise the general chart does, because that is
    what "general" means.

    Still returns None when the choice is genuinely open -- two specialised
    charts, or two general ones -- because then there is nothing to reason from
    and a guess would be a guess.
    """
    text = f"{p.subcategory or ''} {p.category or ''} {p.title or ''}".lower()

    specialised: list[str] = []
    general: list[str] = []
    for c in candidates:
        squashed = _squash(c)
        words = next(
            (w for key, w in _SPECIALISED_GUIDES.items() if key in squashed),
            None,
        )
        if words is None:
            general.append(c)
        elif any(w in text for w in words):
            # The garment names this chart's domain: it wins outright.
            return c
        else:
            specialised.append(c)

    # No specialised chart claimed it, so the general one is the answer.
    if len(general) == 1:
        return general[0]
    return None


def _plan_guide_switch(p: ProductSnapshot, findings: list[Finding],
                       plan: list[dict[str, Any]]) -> str | None:
    """Move the product onto the chart its taxonomy and size actually call for.

    Handles, in precedence order: SIZE.014 (the chart measures the wrong half of
    the body — a women's top on "Women Bottoms"), SIZE.013 (its ladder does not
    list this size at all), SIZE.011 (it contradicts the garment's gender) and
    SIZE.012 (a generic chart while a specific one fits). Runs BEFORE `_plan_fields` for the
    same reason the chart creation does: SIZE.002 derives euSize from whatever
    guide is attached, so switching afterwards would leave a size computed
    against the chart we just replaced.
    """
    # SIZE.013 first — it is the strongest of the three: the ladder plainly
    # cannot express this product's size, whatever the guide is called.
    finding = next(
        (f for f in findings
         if f.rule_id in ("SIZE.014", "SIZE.013", "SIZE.011", "SIZE.012")), None
    )
    if finding is None:
        return

    suggested = finding.detail.get("suggested")
    # SIZE.011/013/014 carry a LIST (the candidates), SIZE.012 a single name.
    if isinstance(suggested, list):
        if len(suggested) != 1:
            # Try to resolve it before handing it to a person — see _pick_guide.
            chosen = _pick_guide(p, suggested) if suggested else None
            if chosen:
                plan.append({
                    "kind": "set_column", "field": "sizingGuide", "value": chosen,
                    "reason": finding.rule_id,
                    "detail": (
                        f"was '{p.sizing_guide}'; {len(suggested)} charts fit "
                        f"({', '.join(suggested)}) and '{chosen}' is the general "
                        f"one for this garment"
                    ),
                })
                return chosen
            plan.append({
                "kind": "escalate", "field": "sizingGuide",
                "reason": finding.rule_id,
                "detail": (
                    f"{len(suggested)} charts fit this product "
                    f"({', '.join(suggested)}); pick one"
                    if suggested else
                    "no chart matches this product's gender and size"
                ),
            })
            return None
        suggested = suggested[0]

    if not suggested:
        plan.append({
            "kind": "escalate", "field": "sizingGuide",
            "reason": finding.rule_id,
            "detail": "no better chart available",
        })
        return None

    plan.append({
        "kind": "set_column", "field": "sizingGuide", "value": suggested,
        "reason": finding.rule_id,
        "detail": f"was '{p.sizing_guide}'",
    })
    # Returned so the caller can plan the REST of the product against the guide
    # it is about to be on — see the note at the call site.
    return str(suggested)


def _plan_gender(p: ProductSnapshot, findings: list[Finding],
                 plan: list[dict[str, Any]]) -> None:
    """Fill in a gender that was never written, from the master category.

    ABSENT ONLY. A multi-valued gender is GENDER.001's business and is
    deliberately left alone — split-gender.worker.ts owns that decision and
    narrowing the row here would make its both-gender guard skip the second
    product entirely. See the note on GENDER.001.

    Written as a single-element LIST because that is the stored shape:
    `normalizeGenderToArray` returns `string[]`, and the split worker writes
    `gender: [gender]`. A bare string would be a shape the readers tolerate and
    the writers never produce.
    """
    absent = any(
        f.rule_id == "DATA.010" and "gender" in f.fields for f in findings
    )
    unresolved = next(
        (f for f in findings if f.rule_id == "GENDER.001"), None
    )

    # An UNRESOLVED gender is repairable only when no split owns it — the rule
    # settles that from the tenant's setting and says so in `detail.repairable`.
    if unresolved is not None and not unresolved.detail.get("repairable"):
        return
    if not absent and unresolved is None:
        return

    rule = "DATA.010" if absent else "GENDER.001"

    # Master category first, mannequin second — the same precedence
    # analyze.worker's `resolvedGender` uses, so the two paths cannot reach
    # different answers for one product.
    derived = (
        gate_rules.resolve_gender(p.master_category)
        or gate_rules.resolve_gender(p.mannequin)
    )
    if not derived:
        plan.append({
            "kind": "escalate", "field": "gender", "reason": rule,
            "detail": "neither the master category nor the mannequin implies a "
                      "gender",
        })
        return

    plan.append({
        # A single-element LIST: the shape normalizeGenderToArray produces and
        # split-gender.worker writes. A bare string is a shape the readers
        # tolerate and no writer in the codebase creates.
        "kind": "set_property", "field": "gender", "value": [derived],
        "reason": rule,
        "detail": (
            f"derived from master category '{p.master_category}'"
            if gate_rules.resolve_gender(p.master_category)
            else f"derived from mannequin '{p.mannequin}'"
        ),
    })


def _singular(text: str) -> str:
    """Letters and digits only, with a trailing plural 's' dropped.

    Enough to match "Leather Jackets" to a tree entry of "Leather Jacket", which
    is the shape this disagreement actually takes: the extractor writes the
    plural a shopper would type and the tenant configured the singular. Not a
    stemmer — anything cleverer starts matching things that are genuinely
    different, and a wrong subcategory is worse than an escalated one.
    """
    flat = "".join(ch for ch in str(text or "").lower() if ch.isalnum())
    return flat[:-1] if flat.endswith("s") and len(flat) > 3 else flat


def _plan_subcategory(p: ProductSnapshot, findings: list[Finding],
                      plan: list[dict[str, Any]]) -> None:
    """Settle subCategory against the tenant's tree — absent, or not in it.

    Two faults, one repair surface:

      DATA.010  no subcategory at all. Fillable only when the tree leaves exactly
                one option.
      TAX.003   a subcategory that is not on this branch. The dropdown renders
                BLANK for these — the value is stored, it is simply not offered —
                so it reads to an operator as missing while being non-null in the
                database. Repairable when the stored value matches exactly one
                tree entry once case, punctuation and a trailing plural are
                ignored.

    Deterministic and free, so both run before the evidence layer is asked.
    """
    if not p.catalog:
        return

    subs = ((p.catalog.categories or {}).get(p.master_category or "") or {}).get(
        p.category or ""
    ) or []
    if not subs:
        return

    absent = any(
        f.rule_id == "DATA.010" and "subcategory" in f.fields for f in findings
    )
    if absent:
        if len(subs) == 1:
            plan.append({
                "kind": "set_column", "field": "subCategory", "value": subs[0],
                "reason": "DATA.010",
                "detail": (
                    f"the only subcategory the tenant lists under "
                    f"'{p.master_category} > {p.category}'"
                ),
            })
            return

        # THE TITLE, when it names exactly one of the branch's own options.
        #
        # "Relaxed Sweatshirt in Black size M" under Women > Sweaters & Hoodies,
        # whose branch offers Sweatshirts / Fleece Pullover / Hoodies / Sweaters:
        # the title says which one. Matched WORD BY WORD against the tenant's
        # entries rather than by substring, so "Sweatshirt" finds "Sweatshirts"
        # and "Vest" cannot quietly match "Puffer Vests" — and only accepted
        # when exactly one entry matches.
        #
        # This is not "pick something from the category". Measured on
        # production, 28 products have a blank subcategory and their branches
        # offer four to eleven options each; choosing one without evidence would
        # be wrong most of the time and indistinguishable afterwards from a
        # value somebody meant. The title is evidence. Where there is none, this
        # stays silent — it recovers 1 of the 28, and the other 27 genuinely
        # cannot be known from the record.
        # TWO shapes of entry, matched differently on purpose.
        #
        # A one-word entry ("Sweatshirts") is matched against the title's WORDS,
        # so a bare "Vest" in a title cannot pick one of four vest TYPES.
        # A multi-word entry ("Leather Vests") can never equal a single word, so
        # it is matched against the title with the spaces taken out —
        # "leathervest" inside "vintagebrownleathervestwomen". That direction is
        # safe where the reverse is not: the entry has to appear in the title,
        # not the other way round, so "Puffer Vests" does not match a faux-fur
        # one. Restricted to multi-word entries because a three-letter
        # normalised token would start finding itself inside unrelated words.
        words = {_singular(w) for w in re.findall(r"[A-Za-z]+", p.title or "")}
        flat_title = _singular(p.title or "")
        named = [
            s for s in subs
            if (_singular(s) in words
                or (len(s.split()) > 1 and _singular(s) in flat_title))
        ]
        if len(named) == 1:
            plan.append({
                "kind": "set_column", "field": "subCategory", "value": named[0],
                "reason": "DATA.010",
                "detail": (f"the title names it, and '{named[0]}' is the only "
                           f"option on this branch that it matches"),
            })
        # Several or none: left for the evidence layer or a human. No escalate
        # entry here — _plan_escalations already names an unrepaired DATA.010
        # field, and a second one would double-count it.
        return

    off_tree = any(
        f.rule_id == "TAX.003" and "subcategory" in f.fields for f in findings
    )
    if not off_tree or not p.subcategory:
        return

    want = _singular(p.subcategory)
    matches = [s for s in subs if _singular(s) == want]
    # Exactly one, or nothing. Two tree entries that both normalise to the stored
    # value means the tenant configured a distinction this cannot see, and
    # picking either would be a guess.
    if len(matches) == 1 and matches[0] != p.subcategory:
        plan.append({
            "kind": "set_column", "field": "subCategory", "value": matches[0],
            "reason": "TAX.003",
            "detail": (
                f"'{p.subcategory}' is stored but not offered under "
                f"'{p.master_category} > {p.category}', so the dropdown renders "
                f"blank. The tenant's tree spells it '{matches[0]}'."
            ),
        })
        return

    # NOT A DATA PROBLEM — a missing option. Named explicitly because the fix
    # lives somewhere else entirely and a bare "needs a human" sends the wrong
    # person at it.
    #
    # Ten women's blazers on production sit under Women > Jackets with
    # subCategory "Blazers", and every tenant's tree carries "Blazers" under
    # MEN > Jackets and under no Women branch at all. Those products are
    # correctly labelled; the option does not exist for them to point at. No
    # per-product edit can fix that, and substituting "Sports Jackets" would
    # write a garment type nobody chose.
    other_branches = sorted({
        f"{master} > {cat}"
        for master, branch in (p.catalog.categories or {}).items()
        for cat, options in (branch or {}).items()
        if any(_singular(o) == want for o in options or [])
    })
    plan.append({
        "kind": "escalate", "field": "subCategory", "reason": "TAX.003",
        "detail": (
            f"'{p.subcategory}' is not offered under "
            f"'{p.master_category} > {p.category}' in any spelling"
            + (f" — the tenant lists it under {', '.join(other_branches)}. "
               f"Either the master category is wrong, or a tenant admin has to "
               f"add the option to this branch."
               if other_branches else
               f". The branch offers {subs}. A tenant admin has to add it, or a "
               f"reviewer picks one of those.")
        ),
    })


def _plan_eu_size(p: ProductSnapshot, findings: list[Finding],
                  plan: list[dict[str, Any]]) -> None:
    """Fill an absent EU size from the product's own sizing chart.

    A LOOKUP, not a conversion. `ProductSize.euSizes` is index-aligned with
    `sizes` — VNYX documents the alignment on the column — so this reads the
    pair out of the same table the edit screen builds its dropdown from. No
    generic table, no arithmetic, nothing guessed: if the tenant's Men Uppers
    chart says S sits at index 2 and euSizes[2] is "46", the answer is 46.

    Why it needed its own planner: SIZE.002 validates an EU size that is PRESENT
    and wrong, and stays silent when there is none to validate. So a product
    whose size arrived from the care-label pass had a size, a guide and a chart
    that pairs them, and still reported `eu_size` absent forever — and the edit
    screen shows "EU Size is required" in red underneath.

    Runs after the size repairs for the obvious reason: the lookup key is the
    size, and computing it from the old one would pair the new size with the old
    EU number.
    """
    absent = any(
        f.rule_id == "DATA.010" and "eu_size" in (f.fields or [])
        for f in findings
    )
    if not absent or not p.catalog:
        return

    # `_apply_plan` has already folded any planned size into the snapshot for
    # the residual pass, but on the first pass the snapshot still holds the
    # pre-repair size — so a size this run is about to write is preferred.
    planned_size = next(
        (a.get("value") for a in plan
         if a.get("field") in ("size", "international_size", "internationalSize")
         and a["kind"] in ("set_property", "set_column")),
        None,
    )
    for candidate in (planned_size, p.size, p.international_size):
        if not candidate:
            continue
        eu = p.catalog.eu_for_size(p.sizing_guide, candidate)
        if eu:
            plan.append({
                "kind": "set_property", "field": "eu_size", "value": eu,
                "reason": "DATA.010",
                "detail": (f"the '{p.sizing_guide}' chart pairs "
                           f"{candidate!r} with EU {eu}"),
            })
            return


def _plan_mannequin(findings: list[Finding],
                    plan: list[dict[str, Any]]) -> None:
    """TAX.005 — swap the rig for the one the taxonomy calls for.

    Only the DERIVED form, which carries `suggested`: the policy-map form knows
    a list of allowed names and not which of them is right, so it escalates.

    Worth repairing rather than escalating because it is the single commonest
    approval blocker on this catalog — 255 wrong-gender rigs in a 1,762-product
    review queue — and the correct value is arithmetic over two facts the record
    already holds, not a judgement about the garment. It also feeds forward: the
    renderer picks the model from this and the master category, so a product
    approved with a Women rig on a men's coat renders the wrong model.
    """
    for f in findings:
        if f.rule_id != "TAX.005":
            continue

        # ---- the gender fields follow the RIG -----------------------------
        #
        # Planned BEFORE the rig itself, and usually instead of changing it.
        # See the note in rules/consistency.py: the operator's rig selection is
        # a human act about the garment in their hands, while masterCategory is
        # the extractor's branch guess. Bringing the other two fields to the rig
        # is what keeps the mannequin, the gender, the master category, the size
        # chart and the rendered model describing one garment instead of two.
        #
        # `master_category_should_be` is only set when the tenant's own taxonomy
        # holds the same category path under the other gender — the rule checks
        # that against catalog.categories rather than assuming it.
        master = f.detail.get("master_category_should_be")
        gender = f.detail.get("gender_should_be")
        if master:
            plan.append({
                "kind": "set_column", "field": "masterCategory", "value": master,
                "reason": "TAX.005",
                "detail": (f'the {f.detail.get("rig_gender")} rig is the anchor; '
                           f'was {f.detail.get("product_gender")!r}'),
            })
            if gender:
                plan.append({
                    "kind": "set_property", "field": "gender", "value": [gender],
                    "reason": "TAX.005",
                    "detail": f'brought into line with the {gender} rig',
                })

        # ---- and the rig, only if its SIDE is wrong ------------------------
        #
        # `suggested` now carries the rig's own gender, so it differs from the
        # stored rig only when the top/bottom side is wrong — which is a fact
        # about the garment type and still comes from the subcategory. Skipping
        # the write when they match stops the plan reporting a repair that
        # changes nothing.
        suggested = f.detail.get("suggested")
        if suggested and suggested != f.detail.get("mannequin"):
            plan.append({
                "kind": "set_column", "field": "mannequinType", "value": suggested,
                "reason": "TAX.005",
                "detail": f'was {f.detail.get("mannequin")!r}',
            })
        return


def _plan_column_drift(findings: list[Finding],
                       plan: list[dict[str, Any]]) -> None:
    """DRIFT.001 — copy the real value over the empty one.

    Only the HIGH form, where one side is a placeholder. The rule sets
    `repair_to` to None when both sides hold real values, because nothing here
    can decide which is right — that falls through to _plan_escalations.

    The direction matters and is decided by the rule, not here: `repair_side`
    names which copy is empty, so a column-side repair writes the column and a
    properties-side repair writes the key. Writing both would be tempting and
    wrong — it would silently pick a winner in the MEDIUM case too.

    YIELDS TO ANY REPAIR ALREADY PLANNED FOR THE SAME VALUE. Runs after
    _plan_subcategory and _plan_guide_switch, both of which decide the same
    fields from the tenant's own tree — strictly better information than "the two
    copies differ". On a real product the column held 'T-Shirt' and the tree
    spells it 'T-Shirts': TAX.003 planned the column to 'T-Shirts', then this
    planned the property to 'T-Shirt' from the PRE-repair column, and because the
    executor mirrors both sides and applies in order, the drift entry landed last
    and undid the taxonomy fix. Skipping is safe as well as correct — the earlier
    repair writes both copies, which is what closes the drift.
    """
    def flat(s: Any) -> str:
        return "".join(ch for ch in str(s or "").lower() if ch.isalnum())

    claimed = {flat(a.get("field")) for a in plan if a.get("field")}

    for f in findings:
        if f.rule_id != "DRIFT.001":
            continue
        value = f.detail.get("repair_to")
        if value is None:
            continue
        if flat(f.detail.get("column")) in claimed or \
                flat((f.fields or [None])[0]) in claimed:
            continue
        if f.detail.get("repair_side") == "column":
            plan.append({
                "kind": "set_column", "field": f.detail["column"],
                "value": value, "reason": "DRIFT.001",
                "detail": (
                    f"the column holds a placeholder while "
                    f"properties.{f.detail['property']} holds {value!r}"
                ),
            })
        else:
            plan.append({
                "kind": "set_property", "field": (f.fields or [None])[0],
                "value": value, "reason": "DRIFT.001",
                "detail": (
                    f"properties.{f.detail['property']} holds a placeholder "
                    f"while the column holds {value!r}"
                ),
            })


def _plan_fields(p: ProductSnapshot, pol: dict[str, Any],
                 findings: list[Finding], llm: Any | None,
                 plan: list[dict[str, Any]]) -> None:
    """Price, taxonomy and attribute repairs, through the existing resolver.

    Reuses `resolver.resolve` rather than re-deriving anything: it already knows
    the tenant's grade-factor arithmetic, the size-chart lookup and the
    confidence gates, and a second implementation here would drift from the one
    /v1/reconcile uses.

    Only `Action.APPLY` patches become writes. PROPOSE and ESCALATE are precisely
    the cases the resolver is not confident enough to act on unattended, and an
    approval gate is the last place to start overriding that judgement.
    """
    from app.models import Action, Evidence
    from app.pipeline import gather_evidence
    from app.resolver import resolve

    ev = Evidence()
    if llm is not None and any(f.needs_evidence for f in findings):
        ev = gather_evidence(p, findings, pol, llm)

    patches = resolve(p, findings, ev, pol)

    auto = [pt for pt in patches
            if pt.action is Action.APPLY and pt.new_value is not None]

    for pt in patches:
        if pt.action is Action.APPLY:
            continue
        if pt.action is Action.PROPOSE and pt.new_value is not None:
            # PROPOSE is not "no repair exists" — it is "a repair exists and
            # wants a human to say yes". The resolver reaches it when a change is
            # larger than `auto_apply_max_delta_pct`, or when a model-derived
            # value sits below the confidence floor.
            #
            # Collapsing it into `escalate` lost that distinction, and with it
            # the only thing a reviewer could usefully act on: the modal said
            # "needs you" and offered no way to agree. `propose` carries the
            # computed value so the Fix-issues path can apply it on an explicit
            # click, while the unattended path still skips it.
            plan.append({
                "kind": "propose",
                "field": (
                    pt.field if pt.field in _PROPERTY_FIELDS
                    else _COLUMN_KEYS.get(pt.field, pt.field)
                ),
                "target": (
                    "property" if pt.field in _PROPERTY_FIELDS else "column"
                ),
                "value": pt.new_value,
                "from": pt.old_value,
                "reason": pt.rule_id,
                "detail": pt.reason,
            })
        else:
            plan.append({"kind": "escalate", "field": pt.field,
                         "reason": pt.rule_id, "detail": pt.reason})

    if not auto:
        return

    # Shadow re-verify BEFORE proposing the write, the same guarantee
    # pipeline.py provides: if the repaired record would still break a hard
    # pricing invariant, the pricing patches are dropped rather than proposed. A
    # price-above-retail product must never leave here marked as fixed.
    shadow = p.model_copy(deep=True)
    for pt in auto:
        if hasattr(shadow, pt.field):
            setattr(shadow, pt.field, pt.new_value)
    if verify_invariants(shadow, pol):
        for pt in [x for x in auto if x.field in ("price", "retail_price")]:
            plan.append({
                "kind": "escalate", "field": pt.field, "reason": "PRICE.reverify",
                "detail": "repair would still violate a hard price invariant",
            })
        auto = [pt for pt in auto if pt.field not in ("price", "retail_price")]

    for pt in auto:
        if pt.field in _PROPERTY_FIELDS:
            plan.append({
                "kind": "set_property",
                "field": pt.field,
                "value": pt.new_value, "reason": pt.rule_id,
            })
        else:
            plan.append({
                "kind": "set_column",
                "field": _COLUMN_KEYS.get(pt.field, pt.field),
                "value": pt.new_value, "reason": pt.rule_id,
            })


def _plan_imagery(p: ProductSnapshot, pol: dict[str, Any],
                  findings: list[Finding], plan: list[dict[str, Any]]) -> None:
    """Which renders are missing, and whether generating them applies at all.

    The DECISION lives here — `generation_plan` owns it, and the caller obeys.
    It refuses for footwear, for a tenant that switched generation off, and for a
    product with no photograph to work from; those are escalations, never
    retries, because no number of attempts fixes a configuration gap and each one
    would cost a paid render.

    The BYTES do not come from this endpoint. The caller asks
    /v1/imagery/generate for them, which already exists and already hands them
    back inline — one endpoint per job, and a verdict call that stays pure CPU
    instead of blocking on an image model for a minute.

    ADVISORY VIEWS ARE INCLUDED. `required_views` is FRONT and BACK only, so a
    plan built from it leaves the 3/4 pair and the close-up absent — which is
    what "only front and back get generated" looked like from the outside. The
    catalog wants the full set (`all_views`: AI_FRONT, AI_BACK, AI_FRONT_34,
    AI_BACK_34, AI_CLOSEUP), so the plan asks for all of them.
      
    The cost is real and worth stating: five renders per product rather than two.
    The close-up still respects the tenant's own `isCloseUpEnabled` — Hermes
    checks that inside `required_views`/`view_report`, so a tenant that switched
    it off does not get one.
    """
    # BLOCKING findings only. An advisory imagery finding (a missing ¾ view, a
    # mislabelled set) is not stopping the approval, so planning a render for it
    # would spend a paid model call on something no rule requires — and the
    # escalation it produced when there was nothing to generate made the plan
    # look as though it could not clear the blockers it actually could.
    if not any(f.rule_id in _MISSING_RENDER_RULES for f in _blocking(findings)):
        return

    reason = imagery_rules.not_generatable_reason(p, pol)
    if reason:
        plan.append({"kind": "escalate", "field": "images",
                     "reason": "IMG.001", "detail": reason})
        return

    gen = imagery_rules.generation_plan(p, pol, include_advisory=True)
    if not gen.should_generate or not gen.views:
        plan.append({"kind": "escalate", "field": "images",
                     "reason": "IMG.001", "detail": gen.reason})
        return

    plan.append({
        "kind": "generate_images",
        "views": list(gen.views),
        "reason": "IMG.001",
        "detail": gen.reason,
        # Images the segmenter still has to cut out. Reported alongside because a
        # render generated from an unmatted photograph inherits its background.
        "needs_background_removal": [
            m.url for m in imagery_rules.needs_segmenter(p, pol)
        ],
    })


def _plan_escalations(findings: list[Finding],
                      plan: list[dict[str, Any]]) -> None:
    """Blocking findings no repair can address, so a human sees them named.

    Care labels are the clearest case and the reason this exists. A model image
    can be SYNTHESISED from a garment photograph; a care label is a photograph of
    a physical tag sewn into a specific garment. Nothing can generate one — an
    operator has to photograph it — so IMG.030 is always an escalation, never a
    repair, however capable the pipeline gets.
    """
    # Flattened, because a repair names the STORAGE key ("subCategory") while the
    # finding names the snapshot field ("subcategory"). Comparing them raw
    # double-reported every field the plan had just fixed.
    def flat(s: Any) -> str:
        return "".join(ch for ch in str(s or "").lower() if ch.isalnum())

    planned = {flat(a.get("field")) for a in plan if a["kind"] != "escalate"}

    for f in _blocking(findings):
        if f.rule_id == "IMG.030":
            plan.append({
                "kind": "escalate", "field": "careLabelImages",
                "reason": "IMG.030",
                "detail": "a care label is a photograph of a physical tag; it "
                          "cannot be generated and has to be captured",
            })
        elif f.rule_id == "GENDER.001" and "gender" not in planned:
            # Only when the split owns it. When the gate repaired it, `planned`
            # already carries `gender` and this would double-report a fixed field.
            plan.append({
                "kind": "escalate", "field": "gender", "reason": "GENDER.001",
                "detail": "the gender split owns this decision; narrowing it "
                          "here would stop the second product being created",
            })
        elif f.rule_id == "DATA.010" and not (
            {flat(x) for x in f.fields} & planned
        ):
            plan.append({
                "kind": "escalate", "field": (f.fields or [None])[0],
                "reason": "DATA.010", "detail": f.message,
            })


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def _field_issues(findings: list[Finding],
                  plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per FIELD, not per rule — the shape a reviewer reads.

    Several rules can name the same field (a size can be absent, disagree with
    its chart, and sit on the wrong chart at once), and a list of rule ids makes
    the reader do the grouping. Keyed by field, worst severity first, with the
    planned action attached so "wrong" and "being fixed" are one answer rather
    than two lists to cross-reference.
    """
    def flat(s: Any) -> str:
        return "".join(ch for ch in str(s or "").lower() if ch.isalnum())

    # field -> the plan entry that addresses it, if any.
    handled: dict[str, dict[str, Any]] = {}
    for a in plan:
        if a["kind"] == "generate_images":
            handled["images"] = a
        elif a.get("field"):
            handled[flat(a["field"])] = a

    by_field: dict[str, dict[str, Any]] = {}
    for f in findings:
        for field in (f.fields or ["(product)"]):
            key = flat(field)
            entry = by_field.setdefault(key, {
                "field": field,
                "severity": f.severity.value,
                "rules": [],
                "messages": [],
            })
            entry["rules"].append(f.rule_id)
            entry["messages"].append(f.message)
            if _RANK[f.severity] > _RANK[Severity(entry["severity"])]:
                entry["severity"] = f.severity.value

    out = []
    for key, entry in by_field.items():
        action = handled.get(key)
        entry["action"] = action["kind"] if action else "none"
        entry["fix"] = action.get("value") if action else None
        entry["blocking"] = _RANK[Severity(entry["severity"])] >= _RANK[_BLOCKING_FLOOR]
        out.append(entry)

    out.sort(key=lambda e: (-_RANK[Severity(e["severity"])], e["field"]))
    return out


def run_gate(raw: dict[str, Any], *, catalog: dict[str, Any] | None = None,
             imagery_settings: dict[str, Any] | None = None,
             llm: Any | None = None,
             split_on_both_genders: bool = False,
             severity_overrides: dict[str, str] | None = None,
             ) -> dict[str, Any]:
    """Verify one product and compute the repair for everything blocking it.

    Writes nothing, ever. Call it a second time after executing the plan to get
    the post-repair verdict.

    `severity_overrides` is the tenant's Brain setting, keyed `RULE` or
    `RULE:field` — see apply_severity_overrides. Threaded down to every
    `_all_findings` call rather than applied once here, so the three passes
    cannot disagree about the same finding.
    """
    started = time.perf_counter()
    pol = policy()

    p = to_snapshot(raw, catalog=catalog, imagery_settings=imagery_settings)
    findings = _all_findings(
        p, pol, split_on_both_genders=split_on_both_genders,
        severity_overrides=severity_overrides,
    )
    blocking = _blocking(findings)

    # WHAT IS WRONG WITH THE PRODUCT AS STORED, kept for the report.
    #
    # `findings` below is deliberately rebound to a SHADOW product once a guide
    # switch is planned, so the remaining fields are planned against the chart
    # the product is about to be on rather than the one it is leaving. That is
    # right for planning and wrong for reporting: the shadow no longer has the
    # defect, so the finding that JUSTIFIED the repair disappears out of
    # `field_issues` and `advisory` — while `blocking`, computed above, still
    # carries it. A product came back with `sizingGuide -> Men Uppers` in the
    # repair plan and no sizing-guide row anywhere in the issue list.
    #
    # Reported findings are the stored product's. The planner keeps its shadow.
    reported = list(findings)

    plan: list[dict[str, Any]] = []
    # Built whenever ANY finding fired, not only a blocking one.
    #
    # `if blocking:` was wrong and hid a whole class of repair. SIZE.012 (on a
    # generic chart while a specific one fits) is MEDIUM by design so it does not
    # halt a catalog — but that also meant a product whose ONLY defect was the
    # wrong sizing guide got no plan at all, was reported verified, and kept the
    # wrong chart forever. "Advisory" describes whether it blocks approval, not
    # whether it is worth fixing.
    if findings:
        # Order matters for the CALLER, which executes in sequence: the chart
        # comes first because SIZE.002 cannot produce a correct euSize
        # expectation until the product points at one, so a size repair computed
        # before the chart exists would be derived from the wrong chart or none.
        _plan_size_chart(p, pol, findings, plan)
        # Before _plan_fields: SIZE.002 derives euSize from the ATTACHED guide,
        # so a switch decided afterwards would leave a size computed against the
        # chart it just replaced.
        switched = _plan_guide_switch(p, findings, plan)

        # Re-plan the size fields against the NEW guide.
        #
        # SIZE.002 derives euSize from the ladder of whatever guide is on the
        # snapshot, and at this point that is still the OLD one — so a run that
        # moved a women's top from "Women Bottoms" to "Women Uppers" would then
        # compute its EU size from the bottoms ladder it had just abandoned. The
        # two writes would land together and disagree.
        #
        # fix-guide-gender.ts calls this out as the reason all three moves belong
        # in one operation: "the ladders differ by gender: L is EU 50 on Men
        # Uppers and EU 40 on Women Uppers. Switching the guide without this
        # leaves a men's number on a women's chart, which is the same defect
        # wearing a different hat."
        if switched:
            p = p.model_copy(update={"sizing_guide": switched})
            findings = _all_findings(
                p, pol, split_on_both_genders=split_on_both_genders,
                severity_overrides=severity_overrides,
            )

        _plan_gender(p, findings, plan)
        _plan_subcategory(p, findings, plan)
        _plan_mannequin(findings, plan)
        # Before _plan_fields, for the same reason the chart is: a drift repair
        # settles WHICH copy of a value is authoritative, and a field repair
        # planned first would be computed from the copy that is about to lose.
        _plan_column_drift(findings, plan)
        _plan_fields(p, pol, findings, llm, plan)
        # LAST of the field planners: its input is the size, which every planner
        # above may have just changed.
        _plan_eu_size(p, findings, plan)

        # SETTLE THE PAIRED FIELDS.
        #
        # Several fields are only correct RELATIVE to another: euSize to size via
        # the ladder, condition to grade via the grade label. Planning one pass
        # against the original snapshot repaired one half and left the other
        # describing the value it replaced — so a product that came in CLEAN went
        # out with two blockers it did not arrive with:
        #
        #   pass 1  ready=YES blocking=[]        planned=4
        #           wrote price, title, size, condition
        #   pass 2  ready=NO  blocking=[SIZE.002, GRADE.001]
        #
        # Both new findings were consequences of the writes: a new `size` with the
        # old `eu_size`, and a new `condition` against the unchanged grade label.
        #
        # So the plan is applied to a SHADOW and the rules re-run, exactly as
        # pipeline.py does before it writes anything. A second round is enough —
        # the dependents are one level deep — and bounding it is what stops a pair
        # that disagrees in both directions from planning forever.
        for _ in range(2):
            shadow = _apply_plan(p, plan)
            residual = _all_findings(
                shadow, pol, split_on_both_genders=split_on_both_genders,
                severity_overrides=severity_overrides,
            )
            new_blockers = [
                f for f in _blocking(residual)
                if f.rule_id not in {x.rule_id for x in blocking}
            ]
            if not new_blockers:
                break
            _plan_fields(shadow, pol, new_blockers, llm, plan)

        _plan_imagery(p, pol, findings, plan)
        _plan_escalations(findings, plan)

    writes = sum(1 for a in plan if a["kind"] in
                 ("set_column", "set_property", "create_size_chart",
                  "generate_images"))
    escalations = sum(1 for a in plan if a["kind"] == "escalate")

    # Does the plan address every BLOCKING finding?
    #
    # Computed per rule id rather than as "are there any escalations", because an
    # escalation raised for an ADVISORY finding says nothing about whether the
    # blockers can be cleared — and treating it as though it did left a product
    # whose price and size were both repairable looking unfixable.
    # AUTO-appliable kinds only. `propose` is excluded deliberately: it needs a
    # human click, so counting it here would tell the unattended path a blocker
    # was covered when nothing is going to write it — and the product would be
    # re-verified, still blocked, for no reason.
    _AUTO = ("set_column", "set_property", "create_size_chart", "generate_images")
    repaired_rules = {a.get("reason") for a in plan if a["kind"] in _AUTO}
    covered = all(f.rule_id in repaired_rules for f in blocking)

    # Repairs a reviewer can authorise. The Fix-issues button exists only when
    # this is non-empty; otherwise the modal has nothing to offer and should not
    # pretend it does.
    authorizable = [a for a in plan if a["kind"] == "propose"]

    log.info(
        "product=%s ready=%s blocking=[%s] planned=%d escalated=%d (%dms)",
        p.id,
        "YES" if not blocking else "NO",
        ",".join(sorted({f.rule_id for f in blocking})),
        writes, escalations,
        int((time.perf_counter() - started) * 1000),
    )

    return {
        "product_id": p.id,
        # ---- the two fields the backend acts on ----------------------------
        #
        # `verified` is the ONLY cue to approve. It is `ready` under a name that
        # says what it means to the caller, so a reader of the vnyx-api side does
        # not have to know that "ready" was computed from a severity floor.
        "verified": not blocking,
        # Deliberately not just `not verified`: on the FIRST pass a product with
        # a full repair plan is not verified and needs no human either — the
        # writes are about to happen. This is true only when something is left
        # that no repair can reach.
        "human_intervention_needed": bool(blocking) and not covered,
        "reasons": [
            f"{f.rule_id}: {f.message}" for f in blocking
        ],
        # Repairs held back for a human to authorise — a change too large to
        # apply silently, or a model-derived value below the confidence floor.
        # Non-empty is what makes a "Fix issues" button meaningful.
        "authorizable": authorizable,
        # EVERY field any rule took issue with, at any severity, each with what
        # is wrong and whether a repair exists.
        #
        # The severity split (blocking vs advisory) answers "can this be
        # approved"; it does NOT answer "what is wrong with this product", and a
        # reviewer opening the modal is asking the second question. A missing
        # material is MEDIUM, so it sat collapsed behind an advisory disclosure
        # while the field itself read as empty on the form.
        "field_issues": _field_issues(reported, plan),
        # ---- the detail behind that answer ---------------------------------
        "ready": not blocking,
        "blocking": [f.model_dump(mode="json") for f in blocking],
        "advisory": [f.model_dump(mode="json") for f in reported
                     if _RANK[f.severity] < _RANK[_BLOCKING_FLOOR]],
        "repair_plan": plan,
        # True when executing the plan should clear everything blocking. False
        # means at least one blocker has no automated repair — the caller still
        # applies the free database writes, but defers a paid render rather than
        # spending it on a product a human has to touch anyway.
        "plan_covers_blockers": bool(blocking) and covered,
        "findings": sorted({f.rule_id for f in reported}),
        "price": assess(p, pol).model_dump(mode="json"),
        "edit_url": p.edit_url,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
