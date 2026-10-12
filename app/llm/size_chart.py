"""Read a tenant's size-chart IMAGE into size -> EU rows (12 Oct 2026).

WHY. The EU size is a lookup in the tenant's chart DATA (`ProductSize.sizes` /
`euSizes`, index-aligned), and the data does not always hold the product's
size. Klekt's "Men Bottoms" lists W21-W40 only, so a pair of shorts sold as
"Small" has no row to read — while the chart IMAGE attached to the same guide
prints S = EU 46, M = 48, L = 50 in plain sight. Its "Women Bottoms" pairs S
with the EU value "S" (a letter, not a size), and the image prints 36 and 38.
124 products were held for "no EU size" with the answer on the picture.

WHAT IT RETURNS. `{"rows": [{"size": "S", "eu": "46"}, ...]}` — every row of
the printed table, in order, the size as printed in its SIZE (or INT) column
and the EU column's number. A size may appear on two rows (the women's charts
print S twice: EU 36 and EU 38); the caller decides between them. None when
there is no key, the image cannot be fetched, or the picture is not a chart.

The model only READS. It never decides a product's size: the caller looks the
product's own size up in what was read, and an EU value without a digit is
dropped here, so a misread can only make the lookup miss, never invent.

Cached by URL in the vision cache, and in-process: a tenant has a handful of
chart images and every product on a guide shares one, so a whole run costs
one call per chart.
"""
from __future__ import annotations

import logging
import re
import threading
from typing import Any

import httpx

from app.config import policy, settings
from app.llm import cache, health

log = logging.getLogger("hermes.size-chart")

SYSTEM = (
    "You read clothing size-chart images. You copy what is printed; you never "
    "estimate, convert or fill a gap."
)

PROMPT = (
    "This image should be a clothing size chart. Copy its size table row by row, "
    "top to bottom. For each row give:\n"
    "- size: the row's size label as printed in the SIZE column (or the INT column "
    "when there is no SIZE column), for example XS, S, M, L, XL, XXL, W32\n"
    "- eu: the value printed in that row's EU column, exactly as printed, or null "
    "when the table has no EU column\n"
    "Keep repeated sizes as separate rows. Set is_size_chart to false and rows to "
    "an empty list if the image is not a size chart. Reply with a json object "
    "matching the requested schema."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "is_size_chart": {"type": "boolean"},
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "size": {"type": "string"},
                    "eu": {"type": ["string", "null"]},
                },
                "required": ["size", "eu"],
            },
        },
    },
    "required": ["is_size_chart", "rows"],
}

_memo: dict[str, dict[str, Any] | None] = {}
_lock = threading.Lock()


def _clean(parsed: dict[str, Any] | None) -> dict[str, Any] | None:
    """Keep the rows that can answer a lookup: a size and an EU with a digit."""
    if not isinstance(parsed, dict) or not parsed.get("is_size_chart"):
        return None
    rows = []
    for r in parsed.get("rows") or []:
        if not isinstance(r, dict):
            continue
        size = re.sub(r"\s+", "", str(r.get("size") or "")).upper()
        eu = str(r.get("eu") or "").strip()
        if size and re.search(r"\d", eu):
            rows.append({"size": size, "eu": eu})
    return {"rows": rows} if rows else None


def _ask(url: str) -> dict[str, Any] | None:
    key = settings().gemini_api_key
    if not key:
        return None
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
        r.raise_for_status()
        mime = r.headers.get("content-type", "image/png").split(";")[0]
        if not mime.startswith("image/"):
            mime = "image/png"
        data = r.content
    except Exception as exc:  # noqa: BLE001 - no picture, no answer
        log.warning("size-chart fetch failed (%s): %s", url[:80], exc)
        return None
    try:
        from google import genai
        from google.genai import types

        from app.llm.gemini import GeminiEvidence
        from app.net import genai_client_args

        args = genai_client_args()
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(client_args=args) if args else None,
        )
        cfg: dict[str, Any] = {"system_instruction": SYSTEM}
        cfg.update(GeminiEvidence._schema_config(SCHEMA))
        resp = client.models.generate_content(
            model=policy()["llm"]["model_fast"],
            contents=[types.Part.from_text(text=PROMPT),
                      types.Part.from_bytes(data=data, mime_type=mime)],
            config=types.GenerateContentConfig(**cfg),
        )
        parsed = GeminiEvidence._parse(resp.text)
    except Exception as exc:  # noqa: BLE001 - a chart we cannot read is a miss
        log.warning("size-chart read failed (%s): %s", url[:80], exc)
        health.record_error(exc, "gemini")
        return None
    health.record_ok("gemini")
    return _clean(parsed)


def read_chart(url: str, pol: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The chart image's rows, or None. Never raises."""
    url = cache.normalise_url(url)
    if not url:
        return None
    with _lock:
        if url in _memo:
            return _memo[url]
    k = cache.key("size-chart", urls=[url], text=[PROMPT, SCHEMA])
    try:
        answer, _hit = cache.through(k, lambda: _ask(url), pol=pol)
    except Exception as exc:  # noqa: BLE001
        log.warning("size-chart read failed (%s): %s", url[:80], exc)
        answer = None
    answer = _clean({"is_size_chart": True, **answer}) if answer else None
    with _lock:
        _memo[url] = answer
    return answer


def eu_options(rows: list[dict[str, Any]], sizes: list[str]) -> list[str]:
    """The EU values the chart prints for the first of `sizes` it lists, in order."""
    for size in sizes:
        want = re.sub(r"\s+", "", str(size or "")).upper()
        if not want:
            continue
        hits = [r["eu"] for r in rows if r["size"] == want]
        if hits:
            return hits
    return []


def reset() -> None:
    """Forget the in-process answers. For tests."""
    with _lock:
        _memo.clear()
