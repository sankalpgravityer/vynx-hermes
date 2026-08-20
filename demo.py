"""Run a product payload through Hermes and print a before/after table.

    python demo.py                          # your real Levi's record
    python demo.py samples/02-price-above-retail.json
    python demo.py samples\02-price-above-retail.json --llm

Prints exactly what changed, so you can diff Hermes against the values VNYX
originally generated.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.pipeline import reconcile
from app.vnyx_client import to_snapshot

ROOT = Path(__file__).resolve().parent


def load(path: str | None) -> dict:
    target = Path(path) if path else ROOT / "samples" / "01-levis-real.json"
    with open(target, encoding="utf-8") as fh:
        payload = json.load(fh)
    return payload.get("product", payload)


def make_llm():
    from app.config import policy, settings

    s = settings()
    if not s.gemini_api_key:
        print("!! No GEMINI_API_KEY set - falling back to rules only.\n")
        return None
    from app.llm.gemini import GeminiEvidence

    return GeminiEvidence(s.gemini_api_key, policy())


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    use_llm = "--llm" in sys.argv

    raw = load(args[0] if args else None)
    snapshot = to_snapshot(raw)
    result = reconcile(snapshot, apply=False, llm=make_llm() if use_llm else None)

    bar = "=" * 78
    print(bar)
    print(f" {result.status.value.upper():<14} publishable={result.publishable}   "
          f"llm_calls={result.llm_calls}   {result.duration_ms}ms")
    print(bar)

    if not result.findings:
        print("\nNo findings. This record is internally consistent.\n")
        return 0

    print("\nFINDINGS")
    for f in result.findings:
        print(f"  [{f.severity.value:<8}] {f.rule_id:<11} {f.message}")

    if not result.patches:
        print("\nNo patches proposed.\n")
    else:
        print("\nBEFORE / AFTER")
        print(f"  {'FIELD':<16}{'WAS':<26}{'BECOMES':<26}ACTION")
        print("  " + "-" * 74)
        for p in result.patches:
            print(f"  {p.field:<16}{str(p.old_value)[:24]:<26}"
                  f"{str(p.new_value)[:24]:<26}{p.action.value}")

        print("\nWHY")
        for p in result.patches:
            print(f"  {p.field}: {p.reason}")
            if p.sources:
                print(f"    sources: {', '.join(p.sources[:3])}")

    if result.notes:
        print("\nNOTES")
        for n in result.notes:
            print(f"  - {n}")

    residual = [f for f in result.residual_findings if f.severity.value == "critical"]
    if residual:
        print("\nSTILL BLOCKING AFTER REPAIR (human action required)")
        for f in residual:
            print(f"  {f.rule_id} {f.message}")

    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
