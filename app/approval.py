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
}

# The imagery rules that mean "renders are missing", as opposed to the ones about
# a mislabelled or unmatted image.
_MISSING_RENDER_RULES = {"IMG.001", "IMG.002", "IMG.003", "IMG.004", "IMG.005"}


def _blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if _RANK[f.severity] >= _RANK[_BLOCKING_FLOOR]]


def _all_findings(p: ProductSnapshot, pol: dict[str, Any], *,
                  split_on_both_genders: bool = False) -> list[Finding]:
    """The standard rule set plus the gate-only rules.

    `check_gate` is called explicitly rather than registered in REGISTRY — see
    the note at the top of rules/gate.py. Adding it there would change what
    /v1/review-queue reports for every existing caller.
    """
    return run_all(p, pol) + gate_rules.check_gate(
        p, pol, split_on_both_genders=split_on_both_genders
    )


# --------------------------------------------------------------------------- #
# Plan building
# --------------------------------------------------------------------------- #

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


def _plan_subcategory(p: ProductSnapshot, findings: list[Finding],
                      plan: list[dict[str, Any]]) -> None:
    """Fill in subCategory when the tenant's tree leaves exactly one option.

    Deterministic and free, so it runs before the evidence layer is asked. When
    the tree offers several the vision audit may still resolve it (subcategory is
    in `visual_fields`), and when it offers none this escalates.
    """
    absent = any(
        f.rule_id == "DATA.010" and "subcategory" in f.fields for f in findings
    )
    if not absent or not p.catalog:
        return

    subs = ((p.catalog.categories or {}).get(p.master_category or "") or {}).get(
        p.category or ""
    ) or []
    if len(subs) == 1:
        plan.append({
            "kind": "set_column", "field": "subCategory", "value": subs[0],
            "reason": "DATA.010",
            "detail": (
                f"the only subcategory the tenant lists under "
                f"'{p.master_category} > {p.category}'"
            ),
        })
    # Several or none: left for the evidence layer or a human. No escalate entry
    # here — _plan_escalations already names an unrepaired DATA.010 field, and a
    # second one would double-count it.


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
             split_on_both_genders: bool = False) -> dict[str, Any]:
    """Verify one product and compute the repair for everything blocking it.

    Writes nothing, ever. Call it a second time after executing the plan to get
    the post-repair verdict.
    """
    started = time.perf_counter()
    pol = policy()

    p = to_snapshot(raw, catalog=catalog, imagery_settings=imagery_settings)
    findings = _all_findings(
        p, pol, split_on_both_genders=split_on_both_genders
    )
    blocking = _blocking(findings)

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
                p, pol, split_on_both_genders=split_on_both_genders
            )

        _plan_gender(p, findings, plan)
        _plan_subcategory(p, findings, plan)
        _plan_fields(p, pol, findings, llm, plan)
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
        "field_issues": _field_issues(findings, plan),
        # ---- the detail behind that answer ---------------------------------
        "ready": not blocking,
        "blocking": [f.model_dump(mode="json") for f in blocking],
        "advisory": [f.model_dump(mode="json") for f in findings
                     if _RANK[f.severity] < _RANK[_BLOCKING_FLOOR]],
        "repair_plan": plan,
        # True when executing the plan should clear everything blocking. False
        # means at least one blocker has no automated repair — the caller still
        # applies the free database writes, but defers a paid render rather than
        # spending it on a product a human has to touch anyway.
        "plan_covers_blockers": bool(blocking) and covered,
        "findings": sorted({f.rule_id for f in findings}),
        "price": assess(p, pol).model_dump(mode="json"),
        "edit_url": p.edit_url,
        "duration_ms": int((time.perf_counter() - started) * 1000),
    }
