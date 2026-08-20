"""Gemini connectivity check. Run this BEFORE debugging Hermes.

    python scripts\\check_gemini.py

Tests five things in order, from cheapest to most demanding. If one fails, the
ones below it will too, so fix them top-down:

  1. SDK installed and importable
  2. API key valid  (lists models available to your key)
  3. Plain text generation on the configured models
  4. Structured output  (JSON Schema enforcement)
  5. Structured output + Google Search grounding  <- what ground_rrp needs
  6. The real ground_rrp call on your Levi's record

Nothing here touches Hermes' pipeline, so a green run means any remaining
problem is in Hermes, not in your key or model access.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK, BAD, WARN = "[ OK ]", "[FAIL]", "[WARN]"


def main() -> int:
    # --- 1. SDK ----------------------------------------------------------- #
    try:
        from google import genai

        version = getattr(genai, "__version__", "unknown")
        print(f"{OK} google-genai imported (version {version})")
        if version != "unknown":
            major = int(version.split(".")[0])
            if major < 2:
                print(f"{WARN} Structured output + search grounding needs "
                      f"google-genai >= 2.3.0. Run: pip install -U google-genai")
    except ImportError as exc:
        print(f"{BAD} google-genai not installed: {exc}")
        print("       pip install -U google-genai")
        return 1

    # --- 2. Key ----------------------------------------------------------- #
    from app.config import policy, settings

    s = settings()
    if not s.gemini_api_key:
        print(f"{BAD} GEMINI_API_KEY is empty.")
        print("       Add it to .env in the project root, then re-run.")
        print("       Get a key at https://aistudio.google.com/apikey")
        return 1
    key = s.gemini_api_key
    print(f"{OK} GEMINI_API_KEY loaded ({key[:6]}...{key[-4:]}, {len(key)} chars)")
    if not s.llm_enabled:
        print(f"{WARN} HERMES_LLM_ENABLED is false. This script ignores that flag, "
              f"but Hermes itself will skip all model calls until you set it true.")

    cfg = policy()["llm"]
    fast, reasoning = cfg["model_fast"], cfg["model_reasoning"]
    print(f"       policy.yaml models: fast={fast}  reasoning={reasoning}")

    client = genai.Client(api_key=key)

    try:
        available = sorted(
            m.name.replace("models/", "") for m in client.models.list()
        )
        print(f"{OK} Key is valid — {len(available)} models visible")
        for name in (fast, reasoning):
            mark = OK if name in available else BAD
            print(f"       {mark} {name}")
        missing = [n for n in (fast, reasoning) if n not in available]
        if missing:
            gem = [m for m in available if m.startswith("gemini-")][-12:]
            print(f"{WARN} Not available to your key: {missing}")
            print(f"       Pick a replacement from these and update "
                  f"config/policy.yaml -> llm:")
            for m in gem:
                print(f"         {m}")
    except Exception as exc:
        print(f"{BAD} Could not list models: {type(exc).__name__}: {exc}")
        print("       Usually an invalid or revoked key, or a proxy blocking "
              "generativelanguage.googleapis.com")
        return 1

    # --- 3. Plain generation ---------------------------------------------- #
    for label, model in (("fast", fast), ("reasoning", reasoning)):
        try:
            r = client.models.generate_content(
                model=model, contents="Reply with the single word: ready")
            print(f"{OK} {label} model responds ({model}): {r.text.strip()[:40]}")
        except Exception as exc:
            print(f"{BAD} {label} model failed ({model}): {type(exc).__name__}: {exc}")
            return 1

    # --- 4. Structured output --------------------------------------------- #
    schema = {
        "type": "object",
        "properties": {"currency": {"type": "string"}, "amount": {"type": "number"}},
        "required": ["currency", "amount"],
    }
    try:
        r = client.models.generate_content(
            model=fast,
            contents="Report the number forty two point five in euros.",
            config={"response_mime_type": "application/json",
                    "response_json_schema": schema},
        )
        parsed = json.loads(r.text)
        print(f"{OK} Structured output works: {parsed}")
    except Exception as exc:
        print(f"{BAD} Structured output failed: {type(exc).__name__}: {exc}")
        from google.genai import types as _t
        avail = [k for k in _t.GenerateContentConfig.model_fields
                 if "response" in k and ("schema" in k or "format" in k)]
        print(f"       Schema fields your SDK exposes: {avail or 'none'}")
        print("       Hermes probes for these automatically in "
              "app/llm/gemini.py::_schema_config")
        return 1

    # --- 5. Structured output + search grounding -------------------------- #
    rrp_schema = {
        "type": "object",
        "properties": {
            "found": {"type": "boolean"},
            "rrp": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "confidence": {"type": "number"},
            "reasoning": {"type": "string"},
            "sources": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["found", "rrp", "currency", "confidence", "reasoning", "sources"],
    }
    try:
        r = client.models.generate_content(
            model=reasoning,
            contents="What is the current full retail price of Levi's 501 "
                     "men's jeans on levi.com in EUR?",
            config={
                "tools": [{"google_search": {}}],
                "response_mime_type": "application/json",
                "response_json_schema": rrp_schema,
            },
        )
        parsed = json.loads(r.text)
        print(f"{OK} Grounded structured output works:")
        print(f"       rrp={parsed.get('rrp')} {parsed.get('currency')}  "
              f"confidence={parsed.get('confidence')}")
        print(f"       sources={parsed.get('sources', [])[:2]}")
    except Exception as exc:
        print(f"{BAD} Grounded structured output failed: {type(exc).__name__}: {exc}")
        print("       Combining google_search with a response schema needs a")
        print("       Gemini 3 series model. Check the model list above and set")
        print("       llm.model_reasoning in config/policy.yaml accordingly.")
        return 1

    # --- 6. The real Hermes call ------------------------------------------ #
    try:
        from app.llm.gemini import GeminiEvidence
        from app.vnyx_client import to_snapshot

        raw = json.load(open(Path(__file__).parent.parent / "samples" /
                             "01-levis-real.json", encoding="utf-8"))["product"]
        llm = GeminiEvidence(key, policy())
        ev = llm.ground_rrp(to_snapshot(raw), "EUR")
        print(f"{OK} Hermes ground_rrp() on your Levi's record:")
        print(f"       found={ev.found} rrp={ev.rrp} {ev.currency} "
              f"confidence={ev.confidence:.2f}")
        print(f"       {ev.reasoning[:150]}")
        for src in ev.sources[:3]:
            print(f"       source: {src}")
        if llm.errors:
            print(f"{WARN} errors recorded: {llm.errors}")
    except Exception as exc:
        print(f"{BAD} Hermes ground_rrp failed: {type(exc).__name__}: {exc}")
        return 1

    print("\nAll checks passed. Set HERMES_LLM_ENABLED=true in .env, restart "
          "uvicorn,\nand /healthz will report llm_enabled: true.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())