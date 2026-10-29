"""Read the PLAYER NAME and NUMBER printed on a sports jersey. Gemini, then OpenAI.

The chain's `player` step (27 Oct 2026). Jerseys generated before the tenant
had `player_name` / `player_number` properties carry neither — and some carry
the name in `model` instead (TST-000078: model "Harden" on a Rockets #13), so
the title format's Player chips stay empty. The bulk extractor is not re-run on
an approved product; this asks the one question with the garment photographs.

Same discipline as app/llm/care_label.py: only what is PRINTED, a confidence
per field, null rather than a guess, and NOTHING written here — the caller
decides. Answers are cached on the photographs (app/llm/cache.py), so a jersey
re-run through the chain is not paid for twice.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from typing import Any

import httpx

from app.config import policy, settings
from app.llm import cache, health
from app.llm.care_label import _fetch

log = logging.getLogger("hermes.player-print")

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_jersey": {"type": "boolean",
                      "description": "A sports jersey or fan shirt with a player's lettering."},
        "player_name": {"type": ["string", "null"],
                        "description": "The player's name as printed, e.g. HARDEN. null if none."},
        "player_name_confidence": {"type": "number"},
        "player_number": {"type": ["string", "null"],
                          "description": "The player's number, digits only, e.g. 13. null if none."},
        "player_number_confidence": {"type": "number"},
        "team": {"type": ["string", "null"]},
    },
    "required": ["is_jersey", "player_name", "player_name_confidence",
                 "player_number", "player_number_confidence", "team"],
}
_OPENAI_SCHEMA = {**SCHEMA, "required": list(SCHEMA["properties"]), "additionalProperties": False}

SYSTEM = (
    "You read the PLAYER lettering on sports jerseys and fan shirts from photographs "
    "of the garment (usually the back and the front).\n"
    "- player_name: the name printed or stitched as lettering, normally across the "
    "upper back. NOT the team or club name (ROCKETS, LAKERS), NOT a sponsor, NOT the "
    "brand logo.\n"
    "- player_number: the large number with it, digits only.\n"
    "Report only what is PRINTED and LEGIBLE — never infer a player from the team or "
    "colours. A team shirt with no name lettering has player_name null. Confidence is "
    "0-100 and is about the characters you read. A wrong answer is far worse than null."
)
PROMPT = ("Read the player name and number from these garment photographs. "
          "Reply with a json object matching the requested schema.")


def _gemini(images: list[tuple[bytes, str]]) -> tuple[dict[str, Any] | None, str]:
    if not images:
        return None, "no_answer"
    key = settings().gemini_api_key
    if not key:
        return None, "not_configured"
    try:
        from google import genai
        from google.genai import types

        from app.llm.gemini import GeminiEvidence
        from app.net import genai_client_args
    except ImportError:  # pragma: no cover
        return None, "not_configured"
    try:
        args = genai_client_args()
        client = genai.Client(api_key=key,
                              http_options=types.HttpOptions(client_args=args) if args else None)
        parts: list[Any] = [types.Part.from_text(text=PROMPT)]
        parts += [types.Part.from_bytes(data=d, mime_type=m) for d, m in images]
        cfg = {"system_instruction": SYSTEM, **GeminiEvidence._schema_config(SCHEMA)}
        resp = client.models.generate_content(model=policy()["llm"]["model_fast"], contents=parts,
                                              config=types.GenerateContentConfig(**cfg))
        parsed = GeminiEvidence._parse(resp.text)
    except Exception as exc:  # noqa: BLE001 - fall through to OpenAI
        log.warning("gemini player read failed: %s", exc)
        return None, ("api_error" if health.record_error(exc, "gemini") else "no_answer")
    health.record_ok("gemini")
    return parsed, "ok"


def _openai(images: list[tuple[bytes, str]]) -> tuple[dict[str, Any] | None, str]:
    if not images:
        return None, "no_answer"
    key = getattr(settings(), "openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")
    if not key:
        return None, "not_configured"
    try:
        content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT}]
        content += [{"type": "image_url",
                     "image_url": {"url": f"data:{m};base64,{base64.b64encode(d).decode()}"}}
                    for d, m in images]
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": policy().get("llm", {}).get("openai_vision_model", "gpt-4o-mini"),
                  "messages": [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": content}],
                  "response_format": {"type": "json_schema", "json_schema": {
                      "name": "player_print", "strict": True, "schema": _OPENAI_SCHEMA}},
                  "max_tokens": 300},
            timeout=90)
        r.raise_for_status()
        parsed = json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception as exc:  # noqa: BLE001
        log.warning("openai player read failed: %s", exc)
        return None, ("api_error" if health.record_error(exc, "openai") else "no_answer")
    health.record_ok("openai")
    return parsed, "ok"


def _model_name(provider: str, pol: dict[str, Any]) -> str:
    llm = pol.get("llm") or {}
    return str(llm.get("openai_vision_model", "gpt-4o-mini") if provider == "openai"
               else llm.get("model_fast") or "")


def _clean_name(value: Any) -> str:
    """'HARDEN' -> 'Harden', "O'NEAL" -> "O'Neal", 'SMITH-JONES' -> 'Smith-Jones';
    mixed case as printed ("McDavid") is kept."""
    text = " ".join(str(value or "").split())
    return " ".join(re.sub(r"[A-Za-z]+", lambda m: m.group(0).capitalize(), w)
                    if w.isupper() or w.islower() else w for w in text.split(" "))


def read(photo_urls: list[str], *, min_confidence: int = 85) -> dict[str, Any]:
    """-> {"player_name", "player_number", "is_jersey", "team", "provider",
           "rejected": {field: score}, "tried": [...], "unavailable": [...], "error"?}

    A field is returned only when read at or above `min_confidence`."""
    out: dict[str, Any] = {"player_name": None, "player_number": None, "is_jersey": None,
                           "team": None, "provider": None, "rejected": {}, "tried": [],
                           "unavailable": [], "cached": []}
    urls = [str(u) for u in photo_urls if u][:2]
    if not urls:
        out["error"] = "no garment photograph to read"
        return out
    pol = policy()
    images: list[tuple[bytes, str]] | None = None
    for name, fn in (("gemini", _gemini), ("openai", _openai)):
        ckey = cache.key(f"player_print.{name}", urls=urls,
                         text=[SYSTEM, PROMPT, _model_name(name, pol)])
        raw = cache.get(ckey, pol)
        if raw is not None:
            status = "ok"
            out["cached"].append(name)
        else:
            if images is None:
                images = _fetch(urls, cap=2)
                if not images:
                    out["error"] = "the garment photographs could not be downloaded"
                    return out
            raw, status = fn(images)
            if status == "not_configured":
                continue
            if status == "ok" and raw and len(images) == len(urls):
                cache.put(ckey, raw, pol)
        out["tried"].append(name)
        if status == "api_error":
            out["unavailable"].append(name)
        if not raw:
            continue
        out["provider"], out["is_jersey"], out["team"] = name, raw.get("is_jersey"), raw.get("team")
        # A slogan tee, a logo hoodie or a numbered fashion print is not a player:
        # nothing is returned unless the model also calls the garment a jersey.
        if raw.get("is_jersey") is not True:
            return out
        for field in ("player_name", "player_number"):
            value = raw.get(field)
            if value is None or not str(value).strip():
                continue
            score = float(raw.get(f"{field}_confidence") or 0)
            if score < min_confidence:
                out["rejected"][field] = max(score, out["rejected"].get(field, 0))
                continue
            if field == "player_number":
                value = "".join(ch for ch in str(value) if ch.isdigit())
                if not value:
                    continue
            else:
                value = _clean_name(value)
            out[field] = value
        return out          # a provider answered; the second opinion is for outages only
    if out["unavailable"]:
        out["error"] = "every vision provider failed: " + ", ".join(out["unavailable"])
    return out
