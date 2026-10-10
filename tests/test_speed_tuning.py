"""The 10 Oct 2026 speed and cost settings (the user's calls), as shipped.

  * the cut-out chains ask only our own methods — IS-Net and the fine-tuned parsers;
    Gemini's and gpt-image's background removal failed every time on the Klekt runs;
  * the gate makes no search-grounded retail-price lookup (85-100 s a call, past
    vnyx-api's 120 s gate timeout) — the agent changes no price beyond the .99 rounding.

The machinery behind both is unchanged; restoring the policy lists / flag brings it back.
"""
from __future__ import annotations

from app.config import policy
from app.imaging import cutout, cutouts
from app.models import Finding, ProductSnapshot, Severity
from app.pipeline import gather_evidence

PAID = {"gemini-paint", "openai-paint", "gemini-mask", "openai-mask"}


def test_no_cut_out_chain_asks_a_paid_method():
    cfg = cutout.config(policy())
    assert not PAID & set(cfg["strategies"])
    assert not PAID & set(cfg["url_strategies"])
    assert cfg["hanger"]["then"] == [] and cfg["object"]["then"] == []
    assert cutouts.config(policy())["recut_strategies"] == ["object-isnet"]
    # Our own methods are all still there.
    assert {"object-isnet", "hanger-isnet", "cloth-seg-ft", "cloth-seg-ft-backup"} <= set(cfg["strategies"])


class _CountingLLM:
    def __init__(self) -> None:
        self.rrp_calls = 0

    def ground_rrp(self, p, currency):  # noqa: ANN001
        self.rrp_calls += 1
        raise AssertionError("the retail-price lookup must not be asked")


def test_a_price_finding_makes_no_retail_price_lookup():
    llm = _CountingLLM()
    p = ProductSnapshot(id="p1", price=95.0, retail_price=100.0)
    findings = [Finding(rule_id="PRICE.001", severity=Severity.CRITICAL,
                        fields=["price", "retail_price"], message="at retail")]
    ev = gather_evidence(p, findings, policy(), llm)
    assert llm.rrp_calls == 0
    assert not ev.rrp.found


def test_the_lookup_comes_back_with_the_flag():
    pol = {**policy(), "llm": {**policy()["llm"], "rrp_lookup": True}}
    calls = []

    class _LLM:
        def ground_rrp(self, p, currency):  # noqa: ANN001
            calls.append(currency)
            from app.models import RRPEvidence
            return RRPEvidence()

    p = ProductSnapshot(id="p1", price=95.0, retail_price=100.0, currency="EUR")
    findings = [Finding(rule_id="PRICE.001", severity=Severity.CRITICAL,
                        fields=["price", "retail_price"], message="at retail")]
    gather_evidence(p, findings, pol, _LLM())
    assert calls == ["EUR"]
