"""Read the BRAND and the SIZE off a care label. Gemini first, OpenAI second.

A last resort, and deliberately narrow.

vnyx-api's `getStructuredProductJSON` already reads the label as part of a
seventeen-field extraction, and it is the right call for the general case — same
prompt, same schema, same tenant settings as the analyze worker. But it asks for
everything at once, and on a label whose brand is legible and whose size is a
single character printed away from the logo it comes back with brand at 98% and
size at 0%. Observed on a Columbia puffer whose label plainly reads "Columbia
Sportswear Company", "MADE IN CHINA" and "S": the brand landed, the size did not,
and `no size` then blocked approval on a product whose size is in the photograph.

So this asks ONE question about TWO fields, with the label images and nothing
else. A narrow question is a different question, not a louder one — there is no
seventeen-field schema competing for the model's attention and no garment photo
to read a brand off.

WHY TWO PROVIDERS, IN THIS ORDER

Gemini first because it is the tenant default and the key is already configured
for the evidence layer. OpenAI second because a size tag is often a few
characters at an angle under a fold, and the two models fail on different
photographs — a second opinion on a hard crop is worth more here than anywhere
else in the pipeline, since the alternative is a warehouse re-shoot.

Both are asked for a confidence, and both are held to the same floor as the bulk
extractor. A guessed size is worse than an absent one: absent blocks approval
and gets fixed, wrong ships.

NOTHING HERE IS WRITTEN. The caller decides. This module fetches images and
returns a reading.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sys
from typing import Any

import httpx

from app.config import policy, settings
from app.llm import health

log = logging.getLogger("hermes.care-label")

# What one provider read attempt came back as. `read()` needs to tell the
# difference between "asked and got nothing" and "could not ask": a provider
# that returned an API error has said nothing about the label, and a product
# must not be judged on that silence.
#
#   ok              a parsed answer
#   no_answer       the provider spoke, but unusably (parse error, empty)
#   api_error       the provider failed — counted by app/llm/health.py
#   not_configured  no key or no SDK; never attempted
ReadStatus = str

# Only these two. The bulk extractor owns the other fifteen fields and reading
# them again here would give two sources of truth for the same value.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "brand": {
            "type": ["string", "null"],
            "description": "Brand name exactly as printed. null if not legible.",
        },
        "brand_confidence": {"type": "number"},
        "size": {
            "type": ["string", "null"],
            "description": (
                "Size as printed — S, M, L, XL, 42, W32, EU 40. The letter or "
                "number ALONE, without surrounding words. null if not legible."
            ),
        },
        "size_confidence": {"type": "number"},
        "notes": {"type": "string"},
    },
    "required": ["brand", "brand_confidence", "size", "size_confidence"],
}

# The same schema in the shape OpenAI's strict mode demands: EVERY property in
# `required`, and `additionalProperties: false`. Derived rather than retyped so
# the two providers cannot drift into answering different questions.
_OPENAI_SCHEMA: dict[str, Any] = {
    **SCHEMA,
    "required": list(SCHEMA["properties"]),
    "additionalProperties": False,
}

SYSTEM = (
    "You read garment care labels and size tags. You are given photographs of "
    "the LABELS ONLY — no garment shot is included, and you must not infer "
    "anything from what the garment looks like.\n\n"
    "Report only what is PRINTED and LEGIBLE.\n"
    "- brand: the maker's name on the main label. Not the retailer, not a "
    "licensor, not a care-symbol vendor.\n"
    "- size: the size marking. It is often a single character on its own line, "
    "on a separate small tag, or printed below the composition. Look for it "
    "there. Return the marking alone: 'S', not 'Size S'.\n\n"
    "Confidence is 0-100 and means how sure you are of the CHARACTERS you read, "
    "not how plausible the value seems. If a field is not legible return null "
    "with confidence 0. A wrong answer is far worse than null."
)

PROMPT = (
    "Read the brand and the size from these care-label photographs. "
    "If the size appears on a separate tag from the brand, that is normal — "
    "check every image. "
    # The word "json" is LOAD-BEARING for OpenAI, not decoration: with
    # `response_format: json_object` its API returns 400 unless some message
    # contains it — "'messages' must contain the word 'json' in some form".
    # Gemini takes its schema through config and does not care either way, so
    # one prompt serves both.
    "Reply with a json object matching the requested schema."
)


def _fetch(urls: list[str], cap: int = 4) -> list[tuple[bytes, str]]:
    """Download the label images. Skips what it cannot get rather than failing.

    Capped: a product with eight label shots is eight uploads for a question two
    answer, and the pipeline already pays for a vision call per product.
    """
    out: list[tuple[bytes, str]] = []
    for url in urls[:cap]:
        try:
            r = httpx.get(url, timeout=20, follow_redirects=True)
            r.raise_for_status()
            mime = r.headers.get("content-type", "image/jpeg").split(";")[0]
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            out.append((r.content, mime))
        except Exception as exc:  # noqa: BLE001 - a missing image is not fatal
            log.warning("care-label fetch failed (%s): %s", url[:60], exc)
    return out


def _read_gemini(images: list[tuple[bytes, str]]) -> tuple[dict[str, Any] | None, ReadStatus]:
    # NEVER call a vision model with no images. Asked to read a care label and
    # given none, Gemini returned brand "JOE FRESH" and size "XL" at confidence
    # 100 — a complete fabrication that the confidence floor waves straight
    # through, because the floor measures how sure the model is, not whether it
    # had anything to look at. `read()` guards this too; both guard it, because
    # either one being the only check is one refactor away from silent invention.
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
    except ImportError as exc:  # pragma: no cover
        # Names the interpreter, because the cause is almost always that the
        # script was run with a Python that is not the venv. `psycopg` and
        # `openpyxl` are commonly installed system-wide and `google-genai` is
        # not, so everything else works and only this fails — which reads as a
        # broken feature rather than a wrong interpreter.
        log.warning(
            "gemini unavailable (%s). Running %s — if that is not "
            "hermes/.venv, use .venv/Scripts/python.exe, or "
            "`pip install google-genai` for this interpreter.",
            exc, sys.executable,
        )
        return None, "not_configured"

    try:
        args = genai_client_args()
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(client_args=args) if args else None,
        )
        parts: list[Any] = [types.Part.from_text(text=PROMPT)]
        for data, mime in images:
            parts.append(types.Part.from_bytes(data=data, mime_type=mime))

        cfg = {"system_instruction": SYSTEM}
        cfg.update(GeminiEvidence._schema_config(SCHEMA))
        resp = client.models.generate_content(
            model=policy()["llm"]["model_fast"],
            contents=parts,
            config=types.GenerateContentConfig(**cfg),
        )
        parsed = GeminiEvidence._parse(resp.text)
    except Exception as exc:  # noqa: BLE001 - fall through to OpenAI
        log.warning("gemini care-label read failed: %s", exc)
        # A provider failure is counted towards the outage guard and reported
        # as such; a parse failure is an answer nobody could use.
        return None, ("api_error" if health.record_error(exc, "gemini") else "no_answer")
    health.record_ok("gemini")
    return parsed, "ok"


def _read_openai(images: list[tuple[bytes, str]]) -> tuple[dict[str, Any] | None, ReadStatus]:
    """The second opinion. Same question, same schema, different eyes."""
    if not images:  # see the note in _read_gemini
        return None, "no_answer"
    key = getattr(settings(), "openai_api_key", "") or os.getenv(
        "OPENAI_API_KEY", "")
    if not key:
        log.warning("no OPENAI_API_KEY — no fallback for the care-label read")
        return None, "not_configured"
    try:
        content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT}]
        for data, mime in images:
            b64 = base64.b64encode(data).decode()
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"},
            })
        r = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": policy().get("llm", {}).get(
                    "openai_vision_model", "gpt-4o-mini"),
                "messages": [
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": content},
                ],
                # json_SCHEMA, not json_object.
                #
                # `json_object` only promises valid JSON — it does not enforce
                # the shape. Asked for it, OpenAI returned
                # {"brand": "Columbia Sportswear Company", "size": "S"} and
                # omitted both confidence fields, which `read()` scores as 0 and
                # rejects. A correct reading thrown away by the floor is the
                # worst of both: the call was paid for and the answer discarded.
                # Strict mode makes the fields mandatory.
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "care_label",
                        "strict": True,
                        "schema": _OPENAI_SCHEMA,
                    },
                },
                "max_tokens": 400,
            },
            timeout=90,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        parsed = json.loads(text)
    except httpx.HTTPStatusError as exc:
        # The body carries the actual reason and the bare status does not — a
        # 400 here was "'messages' must contain the word 'json'", which the
        # status alone reads as an outage.
        detail = ""
        try:
            detail = exc.response.json()["error"]["message"]
        except Exception:  # noqa: BLE001
            detail = (exc.response.text or "")[:200]
        log.warning("openai care-label read failed (%s): %s",
                    exc.response.status_code, detail)
        health.record_error(exc, "openai")
        return None, "api_error"
    except Exception as exc:  # noqa: BLE001
        log.warning("openai care-label read failed: %s", exc)
        return None, ("api_error" if health.record_error(exc, "openai") else "no_answer")
    health.record_ok("openai")
    return parsed, "ok"


def _size_ok(value: str, ladder: list[str] | None) -> bool:
    """Is this a size the product's own sizing guide can actually express?

    THE CHECK THE CONFIDENCE FLOOR CANNOT DO. On the Columbia label — which
    plainly reads "S" — OpenAI returned the DIGIT "5" at 85%. The model was
    confident and wrong about a character, which is the one failure a
    self-reported score can never catch: it grades certainty, not accuracy.

    `ProductSize.sizes` is the set of sizes the attached guide lists, so a
    reading absent from it cannot be right whatever the model thinks. "5" is not
    on the Men Uppers ladder (XXS-XXXL); "S" is.

    No ladder means no opinion — a product with no guide attached is judged on
    confidence alone, as before.
    """
    if not ladder:
        return True
    want = "".join(ch for ch in str(value).lower() if ch.isalnum())
    return any(
        "".join(ch for ch in str(s).lower() if ch.isalnum()) == want
        for s in ladder
    )


def read(label_urls: list[str], *, want: tuple[str, ...] = ("brand", "size"),
         min_confidence: int = 70,
         size_ladder: list[str] | None = None) -> dict[str, Any]:
    """Read the wanted fields off the label. Returns what cleared the floor.

    `{"brand": "Columbia Sportswear", "size": "S", "provider": "gemini",
      "rejected": {"size": 40}, "tried": ["gemini"]}`

    A field is returned only when a provider read it AND scored it at or above
    the floor. Rejections are reported rather than dropped: "the model saw a
    size and was 40% sure" is a different fact from "no size on the label", and
    the second is a re-shoot while the first is a better photograph of the same
    tag.
    """
    result: dict[str, Any] = {"tried": [], "rejected": {}, "provider": None,
                              "unavailable": []}
    if not label_urls:
        result["error"] = "no care-label photograph on the product"
        return result

    images = _fetch(label_urls)
    if not images:
        result["error"] = "care-label images could not be downloaded"
        return result

    # Providers actually asked, as opposed to listed. A provider with no key
    # was never a chance the label had, so it is neither "tried" nor "down".
    attempted: list[str] = []
    for name, fn in (("gemini", _read_gemini), ("openai", _read_openai)):
        raw, status = fn(images)
        if status == "not_configured":
            continue
        result["tried"].append(name)
        attempted.append(name)
        if status == "api_error":
            result["unavailable"].append(name)
        if not raw:
            continue

        got: dict[str, Any] = {}
        for field in want:
            value = raw.get(field)
            score = float(raw.get(f"{field}_confidence") or 0)
            if value is None or not str(value).strip():
                continue
            if score < min_confidence:
                # Kept at the best score seen across providers, so a field the
                # second model was surer about is not hidden by the first.
                result["rejected"][field] = max(
                    score, float(result["rejected"].get(field, 0)))
                continue
            if field == "size" and not _size_ok(value, size_ladder):
                result["rejected"]["size"] = score
                result.setdefault("notes_rejected", []).append(
                    f'{name} read size {value!r} at {score:.0f}%, but the '
                    f"product's sizing guide does not list it")
                continue
            got[field] = str(value).strip()

        if got:
            result.update(got)
            result["provider"] = name
            result["notes"] = raw.get("notes", "")
            # Everything asked for, from one provider — no reason to pay for a
            # second opinion. A partial answer falls through, so the other model
            # gets a chance at the field this one could not read.
            if all(f in got for f in want):
                return result

    # NOTHING WAS READ, AND NOBODY LOOKED. Every provider that could be asked
    # failed at the API — or there was none to ask. That is not "the label is
    # illegible"; it is "the reader was down", and the two must not share the
    # verdict "no size". `api_failed` is what repair_product.py turns into a
    # retry instead of a rejection. See app/llm/health.py for the incident.
    if not any(f in result for f in want):
        if not attempted:
            result["api_failed"] = True
            result["error"] = ("no vision provider is configured — set "
                               "GEMINI_API_KEY or OPENAI_API_KEY; the label was "
                               "never read")
        elif len(result["unavailable"]) == len(attempted):
            result["api_failed"] = True
            result["error"] = (
                f'vision providers unavailable — {", ".join(attempted)} returned '
                f"API errors rather than a reading; nothing about this label is known"
            )

    return result
