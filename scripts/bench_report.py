"""Turn one or more bench runs' usage.jsonl into the tables the write-up needs.

Cost is RECOMPUTED here from the recorded token counts rather than read from the
`cost_usd` the run wrote. That is the point of storing raw tokens: a price table
that turns out to be wrong — and one did, gpt-image-1.5 bills output at $32/M
where the docs were written against gpt-image-1's $40/M — is a one-line fix and a
re-run of this, not a re-run of the renders.

    python scripts/bench_report.py                      # every run found
    python scripts/bench_report.py --product "jeans for women"
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from bench_images import OUT_ROOT, PRICES, cost_of  # noqa: E402


def load(product: str | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in sorted(OUT_ROOT.glob("*/*/usage.jsonl")):
        if product and path.parent.parent.name != product:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    return rows


def table(title: str, headers: list[str], body: list[list[str]]) -> None:
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in body)) if body else len(headers[i])
        for i in range(len(headers))
    ]
    print(f"\n{title}")
    print("  " + "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  " + "  ".join("-" * widths[i] for i in range(len(headers))))
    for row in body:
        print("  " + "  ".join(row[i].ljust(widths[i]) for i in range(len(headers))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--product")
    args = ap.parse_args()

    rows = load(args.product)
    if not rows:
        print("no runs found under", OUT_ROOT)
        return

    for row in rows:
        row["cost"] = cost_of(row["model"], row.get("usage") or {})

    # ── per arm ──────────────────────────────────────────────────────────────
    by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_arm[row["arm"]].append(row)

    body = []
    for arm, group in by_arm.items():
        ok = [r for r in group if r["ok"]]
        costs = [r["cost"] for r in group if r["cost"] is not None]
        out_tok = [
            r["usage"].get("output_tokens") for r in ok
            if (r.get("usage") or {}).get("output_tokens")
        ]
        in_tok = [
            r["usage"].get("input_tokens") for r in ok
            if (r.get("usage") or {}).get("input_tokens")
        ]
        body.append([
            arm,
            group[0]["model"] + (f" ({group[0]['quality']})" if group[0]["quality"] else ""),
            f"{len(ok)}/{len(group)}",
            f"{sum(r['seconds'] for r in ok) / len(ok):.1f}s" if ok else "-",
            f"{sum(in_tok) // len(in_tok):,}" if in_tok else "-",
            f"{sum(out_tok) // len(out_tok):,}" if out_tok else "-",
            f"${sum(costs) / len(costs):.4f}" if costs else "-",
            f"${sum(costs):.4f}" if costs else "-",
            # What five renders of one product would cost on this arm — the
            # number the model decision is actually made on.
            f"${sum(costs) / len(costs) * 5:.2f}" if costs else "-",
        ])
    body.sort(key=lambda r: r[0])
    table(
        "PER ARM  (mean per render, and what a 5-render product would cost)",
        ["arm", "model", "ok", "mean s", "in tok", "out tok", "$/render",
         "$ run", "$/product"],
        body,
    )

    # ── per prompt ───────────────────────────────────────────────────────────
    by_prompt: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_prompt[row["prompt"]].append(row)
    body = []
    for name, group in sorted(by_prompt.items()):
        ok = [r for r in group if r["ok"]]
        costs = [r["cost"] for r in group if r["cost"] is not None]
        chars = [r["prompt_chars"] for r in group]
        in_tok = [
            (r.get("usage") or {}).get("input_tokens") for r in ok
            if (r.get("usage") or {}).get("input_tokens")
        ]
        body.append([
            name,
            f"{len(ok)}/{len(group)}",
            f"{sum(chars) // len(chars):,}",
            f"{sum(in_tok) // len(in_tok):,}" if in_tok else "-",
            f"{sum(r['seconds'] for r in ok) / len(ok):.1f}s" if ok else "-",
            f"${sum(costs):.4f}" if costs else "-",
        ])
    table(
        "PER PROMPT  (the same 4 arms each, so any difference is the prompt)",
        ["prompt", "ok", "mean chars", "mean in tok", "mean s", "$ total"],
        body,
    )

    # ── failures ─────────────────────────────────────────────────────────────
    bad = [r for r in rows if not r["ok"]]
    if bad:
        table(
            "FAILURES",
            ["prompt", "arm", "view", "error"],
            [[r["prompt"], r["arm"], r["view"], (r["error"] or "")[:90]] for r in bad],
        )
    else:
        print("\nno failures")

    known = [r["cost"] for r in rows if r["cost"] is not None]
    print(f"\n{len(rows)} renders, {sum(1 for r in rows if r['ok'])} ok, "
          f"total ${sum(known):.4f}")
    print("prices used: " + ", ".join(
        f"{m} out ${p['out']}/M" for m, p in PRICES.items()
        if m in {r['model'] for r in rows}
    ))


if __name__ == "__main__":
    main()
