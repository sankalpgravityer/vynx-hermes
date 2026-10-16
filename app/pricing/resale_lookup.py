"""Find what one item actually resells for, used — from its photos.

Ported from `price_lookup.py` + `verify_pages.py` in the BOAS handoff of
5 Oct 2026. The calls and prompts are the ones that ran on 3,800 Klekt, Kilo and
Midtex items; what changed is the plumbing around them:

  * the item comes from a VNYX product payload (`item_from_payload`), not a
    JSON export;
  * the listing cache and the monthly search count are JSON files guarded by a
    thread lock, not `fcntl` (Unix-only — the handoff did not start on Windows);
  * the uncited-listing page check runs inline, before the EU rescue round is
    decided, so a listing it confirms can save that round's searches;
  * nothing here writes to VNYX.

TWO CALLS, ON PURPOSE. (1) Vision, no tools: identify brand, model and article
code from the photos and the care label. (2) Text only, Google Search tool:
find used listings of that item. With images and search in one call the model
skipped searching and invented links (handoff pilot, 23 Sep 2026).

THE INTERACTIONS API, NOT generateContent, for the search. generateContent with
the google_search tool stopped running searches (handoff probe, 23 Sep 2026),
and JSON output carries no citations — so the model answers in a fixed prose
block format and each block keeps the `url_citation` annotations whose text
span overlaps it. That citation is what ties a price to a page Google returned.

THE MODEL NEVER SETS THE PRICE. Its own estimate is recorded and shown; the
price comes from `resale_rules.price_item` over the listings that verified.
"""
from __future__ import annotations

import base64
import copy
import datetime as dt
import io
import json
import logging
import os
import re
import statistics as st
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import ROOT, policy, settings
from app.net import fetch_all, make_client
from app.pricing import resale_rules as R

log = logging.getLogger("hermes.resale")

API = "https://generativelanguage.googleapis.com/v1beta"

DEFAULTS: dict[str, Any] = {
    "identify_model": "gemini-3.8-flash",
    "search_model": "gemini-3.8-flash",
    "max_searches": 5,
    "rescue_searches": 4,
    "page_check": True,
    "confirm_prices": True,
    "cache_days": 21,
    "cache_dir": ".cache/resale",
    "identify_timeout_s": 120,
    "search_timeout_s": 180,
    "price_per_1k_searches_usd": 14.0,
    "free_searches_per_month": 5000,
    "token_price_in_per_1m_usd": 0.75,
    "token_price_out_per_1m_usd": 3.75,
}


def config() -> dict[str, Any]:
    return {**DEFAULTS, **(policy().get("resale_lookup") or {})}


def api_key() -> str:
    """Its own key when one is set, so a search-quota problem cannot reach the
    vision checks that run on GEMINI_API_KEY; that key otherwise."""
    return os.getenv("HERMES_RESALE_GEMINI_API_KEY") or settings().gemini_api_key


RESALE_SITES = ("vinted", "grailed", "vestiaire", "ebay", "depop", "marktplaats", "2dehands",
                "kleinanzeigen", "etsy", "poshmark", "vide-dressing", "wallapop", "subito", "tise")

# --------------------------------------------------------------------------- #
# Prompts — the handoff's, verbatim apart from line wrapping
# --------------------------------------------------------------------------- #
ID_PROMPT = """You are identifying a SECOND-HAND clothing item from its photos and brand/care label.
Catalogue record (may be vague or wrong; trust the photos and label):
{rec}
Labels often carry the model name, collection or an ARTICLE CODE (e.g. Stone Island 7515..., C.P.
Company 13CMSS...). Reply with ONLY this JSON (no markdown):
{{"brand": "", "model": "", "article_code": "", "era": "", "id_confidence": "high|medium|low",
 "search_terms": ["2-4 short queries a reseller would type to find this exact item used"],
 "resale_interest": "high|medium|low", "interest_reason": "",
 "authenticity_concerns": "", "catalogue_corrections": ""}}
resale_interest: would this item resell for clearly MORE than ordinary used clothing of its type
(true vintage e.g. 80s/90s, a sought-after model, collab, rare colourway, designer)? high = yes,
clearly; medium = maybe; low = ordinary used item."""

_BLOCK_FORMAT = """Answer in EXACTLY this format, one block per listing (up to 6), then the summary lines:

### <listing title>
* Platform: <site>
* Status: sold | listed
* Condition: <as stated>
* Location: <country of the seller, or unknown>
* Price: <amount AND currency exactly as shown, e.g. 120 USD, €85, £60>
* URL: <URL of the individual listing>

ESTIMATE: €<realistic resale list price for our item>
CONFIDENCE: high | medium | low
RETAIL NEW: <the item's original retail price when sold new, amount AND currency as shown> | <URL of the brand or retailer page that shows it>
  (only a figure you saw on a page in your search results; do not spend extra searches on it; else: RETAIL NEW: unknown)
REASON: <one or two sentences>"""

SEARCH_PROMPT = """Search Google for USED / second-hand listings of this clothing item and report prices.

Item: {ident}
Our item: condition {cond}, size {size}. We sell in the Netherlands / EU.
Suggested searches: {terms}

Look on resale platforms: Vinted, Grailed, Vestiaire Collective, eBay, Depop, Marktplaats,
Kleinanzeigen. Prefer EU listings and SOLD prices. Use at most {max_s} searches in total — be targeted.
Only report listings you actually found in search results. Never invent a price or URL.
Fewer real listings beat more guessed ones.

""" + _BLOCK_FORMAT

RESCUE_PROMPT = """Second, EU-focused search for USED listings of this clothing item.

Item: {ident}
Our item: condition {cond}, size {size}.
The first search found too few verifiable listings. Now search ONLY European resale sites:
eBay.de / .nl / .fr / .it / .es, Vestiaire Collective, Vinted, Marktplaats, Kleinanzeigen, 2dehands.
Search by article code / model name, and in local language where useful (e.g. "gebraucht", "gebruikt").
Use at most {max_s} searches. Only report listings you actually found; never invent a price or URL.

""" + _BLOCK_FORMAT

PROBE_PROMPT = ("Find 2 used Stone Island crewneck sweatshirts listed on eBay in Europe, "
                "with price and listing URL. Use at most 2 searches.")


# --------------------------------------------------------------------------- #
# URLs
# --------------------------------------------------------------------------- #
LISTING_HINTS = ("/items/", "/itm/", "/listings/", "/products/", "/p/", "/s-anzeige/", "/v/",
                 "/a/", "/product/", "/artikel/", "/listing/", "/item/", "/goods/")
SEARCH_HINTS = ("/b/", "/s-", "/search", "/browse", "/categories", "/catalog", "/brand/",
                "/designers/", "/sch/", "/q/", "/l/", "?q=", "?search")


def domain(u: str) -> str:
    try:
        return urlparse(u or "").netloc.lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def url_kind(u: str) -> str:
    """listing | search | other — a search or category page's 'price' is not a listing."""
    try:
        pu = urlparse(u or "")
    except Exception:  # noqa: BLE001
        return "other"
    path = (pu.path or "/").lower()
    full = (path + ("?" + pu.query if pu.query else "")).lower()
    if (any(x in pu.netloc for x in ("cdn", "media-assets", "images", "img."))
            or re.search(r"\.(jpe?g|png|webp)$", path)):
        return "other"
    # A Kleinanzeigen listing lives under /s-anzeige/, which the "/s-" search
    # hint would otherwise claim (KLE-000346, 15 Oct 2026: a real €549 listing
    # dropped as a search page). Checked first.
    if "/s-anzeige/" in path:
        return "listing"
    if path in ("", "/") or any(h in full for h in SEARCH_HINTS):
        return "search"
    if any(h in path for h in LISTING_HINTS) or re.search(r"\d{6,}", path):
        return "listing"
    return "other"


def norm_url(u: str) -> str:
    try:
        pu = urlparse(u or "")
        return (pu.netloc.lower().removeprefix("www.") + pu.path.rstrip("/")).lower()
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# The product
# --------------------------------------------------------------------------- #
def _media_label(m: dict) -> str:
    """'FRONT · original photo (RAW)' — what a picked picture is, for the log."""
    proc = (m.get("processing") or "").upper()
    kind = {"RAW": "original photo", "BG_REMOVED": "cut-out"}.get(proc, proc.lower() or "?")
    return (f"{(m.get('view') or '?').upper()} · {kind} ({proc or '?'})"
            + ("" if m.get("isCurrent") else " · superseded, not in the gallery"))


def _pick_photos_labelled(media: list[dict], gallery: list[str]) -> list[tuple[str, str]]:
    """One FRONT and one BACK: the photograph first (RAW, even when a cut-out
    superseded it — it is the most faithful picture), else the cut-out, else a
    plain OTHER photo, else the gallery. -> [(url, label)]"""
    out: list[tuple[str, str]] = []
    for view in ("FRONT", "BACK"):
        rows = [m for m in media if (m.get("view") or "").upper() == view and m.get("url")]
        rows.sort(key=lambda m: (
            0 if (m.get("processing") or "").upper() == "RAW" else 1,
            0 if m.get("isCurrent") else 1,
        ))
        if rows:
            out.append((rows[0]["url"], _media_label(rows[0])))
    if not out:
        out = [(m["url"], _media_label(m)) for m in media
               if (m.get("view") or "").upper() == "OTHER" and m.get("isCurrent")
               and (m.get("processing") or "").upper() == "RAW" and m.get("url")][:2]
    return out or [(u, "gallery image (last resort; may be an AI render)")
                   for u in gallery if u][:2]


def _pick_photos(media: list[dict], gallery: list[str]) -> list[str]:
    return [u for u, _ in _pick_photos_labelled(media, gallery)]


def item_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """The lookup's input from `product_audit.load(...)`'s payload."""
    rec = payload["record"]
    media = payload.get("media") or []
    labels = [m["url"] for m in media
              if (m.get("view") or "").upper() == "LABEL" and m.get("isCurrent") and m.get("url")]
    brand = rec.get("brand") or rec.get("brandRelation") or ""
    picked = _pick_photos_labelled(media, rec.get("images") or [])
    photo_labels = {u: lbl for u, lbl in picked}
    photo_labels.update({u: "LABEL · care label photo" for u in labels[:2]})
    return {
        "id": rec.get("id"),
        "sku": rec.get("sku"),
        "tenant": rec.get("tenantName"),
        "review_status": rec.get("reviewStatus"),
        "title": rec.get("title"),
        "brand": "" if str(brand).strip().lower() in ("no brand", "unknown") else brand,
        "category": rec.get("category"),
        "sub_category": rec.get("subCategory"),
        "gender": rec.get("gender"),
        "size": rec.get("size") or rec.get("internationalSize"),
        "color": rec.get("color"),
        "material": rec.get("material"),
        "condition": rec.get("condition") or rec.get("gradeLabel") or "",
        "grade": rec.get("grade"),
        "vnyx_price": rec.get("priceAmount"),
        "retail_price": rec.get("retailPriceAmount"),
        "images": [u for u, _ in picked],
        "label_images": labels[:2],
        "photo_labels": photo_labels,
        "edit_url": rec.get("editUrl"),
    }


# --------------------------------------------------------------------------- #
# Cache and monthly ledger — JSON files, one process
# --------------------------------------------------------------------------- #
_files_lock = threading.Lock()


def _cache_dir() -> Path:
    d = Path(config()["cache_dir"])
    return d if d.is_absolute() else ROOT / d


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or damaged means empty
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


GENERIC_MODELS = {"", "unknown", "hoodie", "sweatshirt", "t-shirt", "tee", "jacket", "sweater",
                  "trousers", "jeans", "shirt", "crewneck", "crewneck sweatshirt",
                  "hooded sweatshirt", "knit", "shorts", "dress", "top"}


def cache_key(ident: dict) -> str | None:
    """The same model in another size has the same listings. Specific ids only."""
    b = (ident.get("brand") or "").strip().lower()
    code = re.sub(r"[^a-z0-9]", "", (ident.get("article_code") or "").lower())
    if b and len(code) >= 5:
        return f"{b}|code|{code}"
    model = re.sub(r"\s+", " ", (ident.get("model") or "").strip().lower())
    if (b and ident.get("id_confidence") == "high" and model not in GENERIC_MODELS
            and len(model.split()) >= 3):
        return f"{b}|model|{model}"
    return None


def cache_get(key: str | None) -> dict | None:
    if not key:
        return None
    with _files_lock:
        entry = _read_json(_cache_dir() / "comp_cache.json").get(key)
    # An entry with no listings is a miss that an older build cached: serving it
    # would repeat "no verified listings" for cache_days without searching again.
    if not entry or not (entry.get("res") or {}).get("comps"):
        return None
    age = dt.datetime.now() - dt.datetime.fromisoformat(entry["at"])
    return entry if age <= dt.timedelta(days=int(config()["cache_days"])) else None


def cache_put(key: str | None, sku: str, res: dict, grounding: dict) -> None:
    if not key:
        return
    path = _cache_dir() / "comp_cache.json"
    with _files_lock:
        data = _read_json(path)
        data[key] = {
            "at": dt.datetime.now().isoformat(timespec="seconds"), "sku": sku,
            "res": {k: res.get(k) for k in ("comps", "resale_estimate_eur",
                                            "estimate_confidence", "retail_new_eur", "reasoning",
                                            "retail_raw", "retail_url", "retail_cited")},
            "grounding": {k: grounding.get(k) for k in ("queries", "domains", "n_sources",
                                                        "sources", "search_count")},
        }
        _write_json(path, data)


def _month() -> str:
    return dt.date.today().strftime("%Y-%m")


def searches_this_month() -> int:
    with _files_lock:
        return int(_read_json(_cache_dir() / "search_ledger.json").get(_month(), 0))


def ledger_add(n: int) -> None:
    if not n:
        return
    path = _cache_dir() / "search_ledger.json"
    with _files_lock:
        data = _read_json(path)
        data[_month()] = int(data.get(_month(), 0)) + int(n)
        _write_json(path, data)


# --------------------------------------------------------------------------- #
# Gemini over REST
# --------------------------------------------------------------------------- #
class Usage:
    def __init__(self) -> None:
        self.tokens_in = 0
        self.tokens_out = 0
        self.searches = 0

    def cost_usd(self, cfg: dict) -> float:
        return (self.tokens_in / 1e6 * cfg["token_price_in_per_1m_usd"]
                + self.tokens_out / 1e6 * cfg["token_price_out_per_1m_usd"]
                + self.searches / 1000 * cfg["price_per_1k_searches_usd"])


def _post(url: str, body: dict, key: str, timeout_s: float,
          tries: int = 3) -> tuple[dict | None, str | None]:
    """POST with retries on 429 and 5xx. (json, None) or (None, error)."""
    last = None
    body = dict(body)
    for attempt in range(tries):
        try:
            with make_client(timeout_s) as http:
                r = http.post(url, json=body, headers={"x-goog-api-key": key})
            if r.status_code == 429:
                last = "HTTP 429 (rate limited)"
                time.sleep(min(30 * (attempt + 1), 90))
                continue
            if r.status_code in (500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 400 and "generation_config" in body:
                body.pop("generation_config")       # field names refused -> the defaults
                continue
            if r.status_code != 200:
                return None, f"HTTP {r.status_code}: {r.text[:300]}"
            return r.json(), None
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:160]}"
            time.sleep(3 * (attempt + 1))
    return None, last or "failed"


def _image_part(data: bytes, max_px: int = 900) -> dict:
    mime = "image/jpeg"
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        if max(im.size) > max_px:
            im.thumbnail((max_px, max_px))
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "JPEG", quality=85)
        data = buf.getvalue()
    except Exception:  # noqa: BLE001 — send it as it came
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            mime = "image/png"
        elif data[8:12] == b"WEBP":
            mime = "image/webp"
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}}


def _as_text(v: Any) -> str:
    """The model sometimes answers an object where a sentence was asked for."""
    if v is None:
        return ""
    if isinstance(v, dict):
        return "; ".join(f"{k}: {_as_text(x)}" for k, x in v.items() if x not in (None, "", [], {}))
    if isinstance(v, (list, tuple)):
        return "; ".join(_as_text(x) for x in v if x not in (None, "", [], {}))
    return str(v).strip()


def _parse_json(txt: str) -> dict | None:
    txt = re.sub(r"^```(?:json)?|```$", "", (txt or "").strip(), flags=re.M).strip()
    m = re.search(r"\{.*\}", txt, re.S)
    return json.loads(m.group(0)) if m else None


def identify(item: dict, key: str, usage: Usage) -> tuple[dict | None, str | None]:
    """Vision call, no tools: (ident, None) or (None, error)."""
    cfg = config()
    urls = (item.get("images") or [])[:2] + (item.get("label_images") or [])[:2]
    got = fetch_all(urls, timeout_s=20.0, deadline_s=60.0)
    labels = item.get("photo_labels") or {}
    # Which pictures went to the model, and which could not be downloaded.
    item["_photo_log"] = [{"url": u, "label": labels.get(u, "?"), "sent": bool(got.get(u))}
                          for u in urls]
    parts = [_image_part(got[u]) for u in urls if got.get(u)]
    if not parts:
        return None, "identify: no photo could be downloaded"
    photos_sent = len(parts)
    rec = {k: item.get(k) for k in ("title", "brand", "category", "sub_category", "gender",
                                    "size", "color", "material", "condition")}
    parts.append({"text": ID_PROMPT.format(rec=json.dumps(rec, ensure_ascii=False))})
    js, err = _post(f"{API}/models/{cfg['identify_model']}:generateContent",
                    {"contents": [{"parts": parts}],
                     "generationConfig": {"temperature": 0.1, "maxOutputTokens": 8192,
                                          "responseMimeType": "application/json"}},
                    key, float(cfg["identify_timeout_s"]))
    if err:
        return None, "identify: " + err
    um = js.get("usageMetadata") or {}
    usage.tokens_in += um.get("promptTokenCount", 0) or 0
    usage.tokens_out += (um.get("candidatesTokenCount", 0) or 0) + (um.get("thoughtsTokenCount", 0) or 0)
    cand = (js.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
                   if isinstance(p, dict))
    try:
        ident = _parse_json(text) or {}
    except Exception:  # noqa: BLE001
        ident = {}
    if not ident:
        return None, f"identify: no JSON in the reply (finish={cand.get('finishReason')})"
    for k in ("brand", "model", "article_code", "era", "id_confidence", "resale_interest",
              "interest_reason", "authenticity_concerns", "catalogue_corrections"):
        if k in ident:
            ident[k] = _as_text(ident[k])
    if not isinstance(ident.get("search_terms"), list):
        ident["search_terms"] = [t for t in [_as_text(ident.get("search_terms"))] if t]
    ident["photos_sent"] = photos_sent
    return ident, None


def interact(prompt: str, key: str, usage: Usage,
             model: str | None = None) -> tuple[str, list[str], list[dict], int, str | None]:
    """One Interactions API call with Google Search.

    -> (answer text, queries run, url citations with spans, searches billed, error)
    """
    cfg = config()
    body = {"model": model or cfg["search_model"], "input": prompt,
            "tools": [{"type": "google_search"}],
            "generation_config": {"temperature": 0.2, "thinking_level": "low"},
            "store": False}
    js, err = _post(f"{API}/interactions", body, key, float(cfg["search_timeout_s"]))
    if err:
        return "", [], [], 0, err
    text, queries, cites = "", [], []
    for step in js.get("steps") or []:
        kind = step.get("type")
        if kind == "google_search_call":
            queries += (step.get("arguments") or {}).get("queries") or []
        elif kind == "model_output":
            for c in step.get("content") or []:
                if c.get("type") != "text":
                    continue
                offset = len(text)
                text += c.get("text", "")
                for a in c.get("annotations") or []:
                    if a.get("type") == "url_citation" and a.get("url"):
                        cites.append({"url": a["url"], "title": a.get("title", ""),
                                      "start_index": offset + (a.get("start_index", 0) or 0),
                                      "end_index": offset + (a.get("end_index", 0) or 0)})
    u = js.get("usage") or {}
    usage.tokens_in += u.get("total_input_tokens", 0) or 0
    usage.tokens_out += (u.get("total_output_tokens", 0) or 0) + (u.get("total_thought_tokens", 0) or 0)
    n = sum(g.get("search_query_count", g.get("count", 0)) or 0
            for g in (u.get("grounding_tool_count") or []))
    n = n or len(queries)
    usage.searches += n
    return text, queries, cites, n, None


def _num(x: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d{1,2})?)", (x or "").replace(",", "."))
    return float(m.group(1)) if m else None


def parse_search_answer(text: str, annotations: list[dict]) -> dict:
    """Listing blocks -> comps. Each comp keeps the citation URLs whose span
    overlaps its block: the link between a price and a page Google returned."""
    comps = []
    heads = [m.start() for m in re.finditer(r"(?m)^#{2,4}\s", text)]
    tail = re.search(r"(?m)^ESTIMATE:", text)
    end_all = tail.start() if tail else len(text)
    for i, start in enumerate(heads):
        end = heads[i + 1] if i + 1 < len(heads) else end_all
        blk = text[start:end]

        def field(k: str, blk: str = blk) -> str:
            m = re.search(rf"(?im)^\W*{k}\W*:\s*\**\s*(.+)$", blk)
            return m.group(1).strip(" *") if m else ""

        url = field("URL")
        m = re.search(r"\((https?://[^)\s]+)\)", url) or re.search(r"(https?://\S+)", url)
        url = m.group(1).rstrip(".,)") if m else ""
        cited = [a.get("url") for a in annotations
                 if a.get("start_index", 0) < end and a.get("end_index", 0) > start]
        amt, cur = R.parse_price(field("Price"))
        status = field("Status")
        comps.append({
            "title": re.sub(r"^#+\s*(\d+\.\s*)?", "", blk.splitlines()[0]).strip(" *"),
            "platform": field("Platform"),
            "status": status.lower().split()[0] if status else "",
            "condition": field("Condition"),
            "location": field("Location"),
            "price_raw": field("Price"),
            "currency": cur,
            "price_eur": R.to_eur(amt, cur),
            "url": url,
            "_cite_raw": [c for c in cited if c],
        })
    t = text[end_all:]

    def g(k: str) -> str:
        m = re.search(rf"(?im)^{k}:\s*(.+)$", t)
        return m.group(1).strip() if m else ""

    conf = (g("CONFIDENCE").lower().split() or ["low"])[0]
    retail = parse_retail_line(text, end_all, annotations)
    return {"comps": comps, "resale_estimate_eur": _num(g("ESTIMATE")),
            "estimate_confidence": conf, "reasoning": g("REASON"), **retail}


def parse_retail_line(text: str, start: int, annotations: list[dict]) -> dict:
    """'RETAIL NEW: 298 USD | https://supreme.com/...' -> retail fields.

    The citations whose span overlaps the line are kept, so the retail figure
    is checked the same way a listing is: was its page really in the results?
    """
    m = re.search(r"(?im)^RETAIL NEW:\s*(.+)$", text[start:])
    out = {"retail_new_eur": None, "retail_raw": "", "retail_url": "", "_retail_cite_raw": []}
    if not m:
        return out
    line = m.group(1).strip()
    if line.lower().startswith("unknown"):
        return out
    price_part, _, url_part = line.partition("|")
    um = re.search(r"\((https?://[^)\s]+)\)", url_part) or re.search(r"(https?://\S+)", url_part or line)
    url = um.group(1).rstrip(".,)") if um else ""
    amt, cur = R.parse_price(price_part)
    lo, hi = start + m.start(), start + m.end()
    out.update({
        "retail_raw": price_part.strip(), "retail_url": url,
        "retail_new_eur": R.to_eur(amt, cur),
        "_retail_cite_raw": [a.get("url") for a in annotations
                             if a.get("start_index", 0) < hi and a.get("end_index", 0) > lo
                             and a.get("url")],
    })
    return out


def resolve_redirects(urls: list[str]) -> dict[str, str | None]:
    """Citation links are Google redirects: follow each one hop to the real page."""
    def one(u: str) -> str | None:
        try:
            with httpx.Client(follow_redirects=False, timeout=10.0) as http:
                r = http.get(u)
            return r.headers.get("location") or (u if r.status_code == 200 else None)
        except Exception:  # noqa: BLE001
            return None

    uniq = list(dict.fromkeys(u for u in urls if u))[:15]
    if not uniq:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(uniq))) as pool:
        return dict(zip(uniq, pool.map(one, uniq)))


def search_round(prompt: str, key: str, usage: Usage) -> tuple[dict | None, dict | None, str | None]:
    text, queries, cites, n_search, err = interact(prompt, key, usage)
    if err:
        return None, None, err
    res = parse_search_answer(text, cites)
    resolved = resolve_redirects([c["url"] for c in cites])
    for c in res["comps"]:
        c["cited"] = [resolved[u] for u in c.pop("_cite_raw") if resolved.get(u)]
    res["retail_cited"] = [resolved[u] for u in res.pop("_retail_cite_raw", []) if resolved.get(u)]
    grounding = {
        "queries": queries,
        "domains": sorted({c["title"] for c in cites if c.get("title")}),
        "n_sources": len(resolved),
        "search_count": n_search,
        "sources": [{"title": "", "url": v} for v in dict.fromkeys(resolved.values()) if v],
        "raw_answer": text[:6000],
    }
    return res, grounding, None


# --------------------------------------------------------------------------- #
# Opening an uncited listing ourselves (verify_pages.py, inline)
# --------------------------------------------------------------------------- #
BROWSER = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/126 Safari/537.36"),
           "Accept-Language": "en-GB,en;q=0.9,nl;q=0.8,de;q=0.7"}
GONE = ("this listing was ended", "no longer available", "listing has been removed",
        "page not found", "item not found", "dit artikel is niet meer beschikbaar",
        "diese anzeige ist nicht mehr verfügbar")


def _amount_variants(raw: str) -> list[str]:
    amt, _ = R.parse_price(raw or "")
    if not amt:
        return []
    whole = int(amt)
    cents = round((amt - whole) * 100)
    v = {f"{amt:.2f}", f"{amt:.2f}".replace(".", ","), f"{whole:,}".replace(",", "."), f"{whole:,}"}
    if cents == 0:
        v.add(str(whole))
    return [x for x in v if x]


def page_check(comp: dict) -> dict:
    """Keep a listing Google did not cite only if its page still exists on the
    same site, is not marked ended, and shows the same amount."""
    u = comp.get("url") or ""
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=BROWSER) as http:
            r = http.get(u)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"fetch failed: {type(exc).__name__}"}
    final = str(r.url) or u
    if r.status_code >= 400:
        return {"ok": False, "why": f"HTTP {r.status_code}"}
    if domain(final).split(".")[-2:] != domain(u).split(".")[-2:] or url_kind(final) != "listing":
        return {"ok": False, "why": "redirected away from the listing"}
    page = r.text[:600_000]
    if "sold" not in (comp.get("status") or "") and any(g in page.lower() for g in GONE):
        return {"ok": False, "why": "listing ended or removed"}
    for a in _amount_variants(comp.get("price_raw") or f"{comp.get('price_eur')}"):
        if re.search(r"(?<![\d.,])" + re.escape(a) + r"(?![\d])", page):
            return {"ok": True, "why": f"page shows {a}"}
    return {"ok": False, "why": "page exists but the price is not on it"}


def run_page_checks(comps: list[dict]) -> int:
    """Page-check the listings that summarise() could not tie to a citation."""
    todo = [c for c in comps
            if "page_check" not in c and c.get("verified") == "none" and c.get("resale_site")
            and c.get("kind") == "listing" and c.get("price_eur")][:8]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
        for c, res in zip(todo, pool.map(page_check, todo)):
            c["page_check"] = res
    return sum(1 for c in todo if c["page_check"]["ok"])


# --------------------------------------------------------------------------- #
# The price on a COUNTED listing's page (15 Oct 2026, the user's request)
#
# A citation proves Google returned the page, not that the model read its price
# right. KLE-000346: the model wrote €480 for an ebay.it listing whose page said
# €402.43 (shipping, or an older price), and the lookup priced from €480. So a
# counted listing's page is opened too — a plain HTTP GET, no Gemini or Google
# call, so it costs nothing:
#   the page shows the model's amount       -> kept
#   the page shows another price             -> the PAGE's price is used
#   the page shows no price at all           -> dropped
#   the site blocks us (403) / fetch fails   -> kept as before (cannot check)
# --------------------------------------------------------------------------- #
_PRICE_PATTERNS = (
    # JSON-LD / microdata offers: "price": "402.43", "priceCurrency": "EUR"
    re.compile(r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?[^{}]{0,200}?"priceCurrency"\s*:\s*"([A-Z]{3})"', re.S),
    re.compile(r'"priceCurrency"\s*:\s*"([A-Z]{3})"[^{}]{0,200}?"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?', re.S),
    # <meta property="product:price:amount" content="402.43">
    re.compile(r'product:price:amount"\s+content="(\d+(?:\.\d{1,2})?)"[^>]*>\s*<meta[^>]+product:price:currency"\s+content="([A-Z]{3})"', re.S),
    # itemprop="price" content="402.43" ... itemprop="priceCurrency" content="EUR"
    re.compile(r'itemprop="price"[^>]*content="(\d+(?:\.\d{1,2})?)".{0,400}?itemprop="priceCurrency"[^>]*content="([A-Z]{3})"', re.S),
)


def page_price(html: str) -> tuple[float, str] | None:
    """The listing's own price from its structured data, or None."""
    for i, pat in enumerate(_PRICE_PATTERNS):
        m = pat.search(html)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        amount, cur = (b, a) if i == 1 else (a, b)
        try:
            v = float(amount)
        except ValueError:
            continue
        if v > 0:
            return v, cur
    return None


def confirm_price(comp: dict) -> dict:
    """Open a counted listing and check its price. Never raises.

    -> {"ok": True|False|None, "why": ..., "page_price": "402.43 EUR"?}
    ok None = could not check (blocked, gone, redirected): the listing stays.
    """
    u = comp.get("url") or ""
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=BROWSER) as http:
            r = http.get(u)
    except Exception as exc:  # noqa: BLE001
        return {"ok": None, "why": f"could not open ({type(exc).__name__})"}
    if r.status_code >= 400:
        return {"ok": None, "why": f"site blocked the check (HTTP {r.status_code})"}
    final = str(r.url) or u
    if domain(final).split(".")[-2:] != domain(u).split(".")[-2:]:
        return {"ok": None, "why": "redirected off the site"}
    page = r.text[:1_500_000]
    for a in _amount_variants(comp.get("price_raw") or f"{comp.get('price_eur')}"):
        if re.search(r"(?<![\d.,])" + re.escape(a) + r"(?![\d])", page):
            return {"ok": True, "why": f"page shows {a}"}
    found = page_price(page)
    if found:
        amount, cur = found
        return {"ok": True, "why": f"page price {amount:.2f} {cur} replaces the model's "
                                   f"{comp.get('price_raw') or comp.get('price_eur')}",
                "page_price": f"{amount:.2f} {cur}"}
    return {"ok": False, "why": "the page does not show the model's price, or any price"}


def confirm_counted_prices(comps: list[dict]) -> int:
    """Run confirm_price on every counted listing not yet confirmed. -> how many changed."""
    todo = [c for c in comps if c.get("counted") and "price_confirm" not in c][:10]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=min(6, len(todo))) as pool:
        results = list(pool.map(confirm_price, todo))
    changed = 0
    for c, res in zip(todo, results):
        c["price_confirm"] = res
        if res.get("page_price"):
            c["price_from_model"] = c.get("price_raw") or c.get("price_eur")
            c["price_raw"] = res["page_price"]          # summarise re-parses this
            changed += 1
    return changed


# --------------------------------------------------------------------------- #
# Which listings count
# --------------------------------------------------------------------------- #
def summarise(item: dict, res: dict, grounding: dict) -> dict:
    """Our own numbers from the listings — never just the model's estimate.

    A listing counts only if a search really ran, its price is above zero, it is
    on a resale site, its URL is an individual listing, and it is grounded: the
    exact page Google cited (`exact`), a listing on a cited site (`site`), or a
    page we opened that shows the price (`page`).
    """
    comps = [c for c in (res.get("comps") or []) if isinstance(c, dict)]
    good = []
    searched = bool(grounding.get("queries"))
    src = grounding.get("sources") or []
    src_urls = {norm_url(x["url"]) for x in src if x.get("url")}
    gdomains = {d.lower().removeprefix("www.") for d in grounding.get("domains", [])}
    gdomains |= {domain(x["url"]) for x in src if x.get("url")}
    for c in comps:
        if c.get("price_raw"):
            amt, cur = R.parse_price(c["price_raw"])
            c["currency"], c["price_eur"] = cur, R.to_eur(amt, cur)
        try:
            p = float(c.get("price_eur") or 0)
        except (TypeError, ValueError):
            p = 0.0
        u = str(c.get("url") or "")
        d = domain(u)
        c["domain"] = d
        c["resale_site"] = any(k in d for k in RESALE_SITES)
        c["kind"] = url_kind(u)
        site_ok = bool(d) and any(d == g or d.endswith("." + g) or g.endswith("." + d)
                                  for g in gdomains if g)
        cited = [x for x in (c.get("cited") or []) if x]
        if "cited" in c:
            if norm_url(u) in {norm_url(x) for x in cited}:
                c["verified"] = "exact"
            else:
                same = [x for x in cited if domain(x) == d and url_kind(x) == "listing"]
                if same:                    # the model typed the URL wrong; the cited one is real
                    c["url_as_written"], u = u, same[0]
                    c["url"], c["kind"] = u, url_kind(u)
                    c["verified"] = "exact"
                else:
                    c["verified"] = "site" if any(domain(x) == d for x in cited) else "none"
        else:
            c["verified"] = "exact" if norm_url(u) in src_urls else ("site" if site_ok else "none")
        if c["verified"] == "none" and (c.get("page_check") or {}).get("ok"):
            c["verified"] = "page"
        c["grounded"] = c["verified"] != "none"
        c["counted"] = bool(searched and p > 0 and c["resale_site"] and c["kind"] == "listing"
                            and c["grounded"]
                            and (c.get("price_confirm") or {}).get("ok") is not False)
        if c["counted"]:
            good.append((p, c.get("status") == "sold", c["verified"] == "exact"))
    sold = [p for p, s, _ in good if s]
    listed = [p for p, s, _ in good if not s]
    pts = sold + [p * R.ASKING_FACTOR for p in listed]
    n_exact = sum(1 for *_, e in good if e)
    if not searched:
        conf = "no search"           # answered from memory: nothing here is evidence
    elif len(good) >= 3 and res.get("estimate_confidence") != "low":
        conf = "high" if (len(sold) >= 1 or n_exact >= 2) else "medium"
    elif len(good) >= 2:
        conf = "medium"
    elif len(good) == 1:
        conf = "low"
    else:
        conf = "no listings"
    priced = R.price_item([c for c in comps if c.get("counted")],
                          item.get("condition", ""), item.get("vnyx_price"))
    return {"comp_median_eur": round(st.median(pts), 2) if pts else None,
            "n_comps": len(good), "n_sold": len(sold), "n_exact": n_exact,
            "n_offered": len(comps), "confidence": conf, **priced}


# --------------------------------------------------------------------------- #
# One item, end to end
# --------------------------------------------------------------------------- #
def _decision(vnyx: float | None, final: float | None, tier: str, tier_reason: str) -> str:
    if not final:
        return "No new price: no verified listings found. Keep the VNYX price."
    if tier == "C":
        return f"A person must decide ({tier_reason}). The lookup says €{final:.2f}."
    if vnyx and abs(final - vnyx) < 0.005:
        return f"Keep €{vnyx:.2f}: the lookup agrees, or a guard kept it."
    if vnyx:
        pct = (final - vnyx) / vnyx * 100
        return f"Change €{vnyx:.2f} -> €{final:.2f} ({pct:+.0f}%)."
    return f"Set €{final:.2f} (the product has no price today)."


def lookup(item: dict, *, fresh: bool = False, check_pages: bool | None = None,
           identify_only: bool = False) -> dict[str, Any]:
    """Identify, search, verify, price. Never raises; an `error` key says why."""
    cfg = config()
    started = time.perf_counter()
    usage = Usage()
    out: dict[str, Any] = {
        "sku": item.get("sku"), "title": item.get("title"), "tenant": item.get("tenant"),
        "review_status": item.get("review_status"), "condition": item.get("condition"),
        "vnyx_price": item.get("vnyx_price"), "retail_price": item.get("retail_price"),
        "edit_url": item.get("edit_url"), "photos": item.get("images"),
        "label_photos": item.get("label_images"),
        "models": f"{cfg['identify_model']} (identify) + {cfg['search_model']} (search)",
    }

    def done(**extra: Any) -> dict[str, Any]:
        out.update(extra)
        out["photos_sent"] = item.get("_photo_log") or []
        out.update({"searches": usage.searches, "tokens_in": usage.tokens_in,
                    "tokens_out": usage.tokens_out, "cost_usd": round(usage.cost_usd(cfg), 4),
                    "seconds": round(time.perf_counter() - started, 1)})
        return out

    key = api_key()
    if not key:
        return done(error="no Gemini key: set GEMINI_API_KEY (or HERMES_RESALE_GEMINI_API_KEY) in .env")
    if not item.get("images") and not item.get("label_images"):
        return done(error="the product has no photos to identify it from")

    ident, err = identify(item, key, usage)
    if err:
        return done(error=err)
    out["identified"] = {k: ident.get(k, "") for k in ("brand", "model", "article_code", "era",
                                                         "id_confidence")}
    out.update({k: ident.get(k, "") for k in ("resale_interest", "interest_reason",
                                              "authenticity_concerns", "catalogue_corrections")})
    out["search_terms"] = ident.get("search_terms") or []
    out["auth_level"] = R.auth_level(ident.get("authenticity_concerns"))
    if identify_only:
        return done(decision="Identified only (no search run).")

    desc = " ".join(x for x in (ident.get("brand") or item.get("brand"), ident.get("model"),
                                ident.get("article_code"), ident.get("era"), item.get("color"),
                                item.get("sub_category")) if x)
    terms = out["search_terms"] or [desc]
    page_checks = cfg["page_check"] if check_pages is None else check_pages
    ckey = cache_key(ident)
    cached = None if fresh else cache_get(ckey)
    if cached:
        res = copy.deepcopy(cached["res"])
        grounding = dict(cached["grounding"], search_count=0, cached_from=cached["sku"])
        if cfg["confirm_prices"]:               # listings cached before 15 Oct were never confirmed
            summarise(item, res, grounding)
            confirm_counted_prices(res["comps"])
    else:
        res, grounding, err = search_round(
            SEARCH_PROMPT.format(ident=desc, cond=item.get("condition"), size=item.get("size"),
                                 terms="; ".join(terms[:4]), max_s=cfg["max_searches"]),
            key, usage)
        if err:
            return done(error="search: " + err)
        first = summarise(item, res, grounding)
        if page_checks:
            run_page_checks(res["comps"])
            first = summarise(item, res, grounding)
        if first["n_comps"] < 2:            # too few verified: one EU-only round, merged in
            r2, g2, err2 = search_round(
                RESCUE_PROMPT.format(ident=desc, cond=item.get("condition"),
                                     size=item.get("size"), max_s=cfg["rescue_searches"]),
                key, usage)
            if not err2:
                seen = {norm_url(c.get("url")) for c in res["comps"]}
                res["comps"] += [c for c in r2["comps"] if norm_url(c.get("url")) not in seen]
                grounding["queries"] += g2["queries"]
                grounding["search_count"] += g2["search_count"]
                grounding["sources"] += g2["sources"]
                grounding["domains"] = sorted(set(grounding["domains"]) | set(g2["domains"]))
                grounding["raw_answer"] += "\n\n--- EU round ---\n" + g2["raw_answer"]
                grounding["rescue"] = True
                if not res.get("retail_new_eur") and r2.get("retail_new_eur"):
                    res.update({k: r2.get(k) for k in ("retail_new_eur", "retail_raw",
                                                       "retail_url", "retail_cited")})
                if not res.get("resale_estimate_eur"):
                    res.update({k: r2.get(k) for k in ("resale_estimate_eur", "estimate_confidence",
                                                       "retail_new_eur", "reasoning")})
                if page_checks:
                    summarise(item, res, grounding)
                    run_page_checks(res["comps"])
        if cfg["confirm_prices"]:
            summarise(item, res, grounding)
            confirm_counted_prices(res["comps"])
        # Only a search that found something is worth reusing. An empty one is
        # often a too-specific identification ("Winter Edition 2011") that the
        # next identify of the same photos may not repeat — caching it pinned
        # every later run of the model to "no verified listings".
        if summarise(item, res, grounding)["n_comps"] > 0:
            cache_put(ckey, item.get("sku") or "", res, grounding)
        ledger_add(grounding.get("search_count", 0))

    s = summarise(item, res, grounding)
    final = s["suggested_eur"]
    cond_flag = (item.get("condition") or "").strip().lower() in R.DOUBTFUL_CONDITIONS
    tier, tier_reason = R.trust_tier(final, item.get("vnyx_price"), s["suggested_eur"],
                                     s["confidence"], s["n_comps"], out["auth_level"], cond_flag)
    retail = judge_retail(res, grounding, final)
    return done(
        **retail,
        new_price=final, tier=tier, tier_reason=tier_reason,
        decision=_decision(item.get("vnyx_price"), final, tier, tier_reason),
        model_estimate_eur=res.get("resale_estimate_eur"),
        model_confidence=res.get("estimate_confidence"),
        reasoning=res.get("reasoning"),
        comps=res.get("comps") or [], queries=grounding.get("queries") or [],
        cached_from=grounding.get("cached_from"), rescue=bool(grounding.get("rescue")),
        raw_answer=grounding.get("raw_answer", ""), fx=R.fx_rates().get("_source"),
        **s,
    )


def judge_retail(res: dict, grounding: dict, new_selling: float | None) -> dict[str, Any]:
    """The retail (new) price the search answer gave, and whether to trust it.

    Proposed as the NEW retail only when its page is proven: Google cited that
    exact page (`cited`), or we opened it and it shows the amount (`page`).
    Otherwise it is shown but not proposed — the model's word is not evidence.
    Costs nothing extra: it comes from the same search call as the listings.
    """
    eur, url = res.get("retail_new_eur"), res.get("retail_url") or ""
    out: dict[str, Any] = {"retail_new_eur": eur, "retail_raw": res.get("retail_raw") or "",
                           "retail_url": url, "retail_verified": "none", "new_retail": None,
                           "retail_note": ""}
    if not eur:
        out["retail_note"] = "no retail price found in the search results — keep the current one"
        return out
    cited = res.get("retail_cited") or []
    if url and norm_url(url) in {norm_url(x) for x in cited}:
        out["retail_verified"] = "cited"
    elif url:
        chk = confirm_price({"url": url, "price_raw": res.get("retail_raw") or f"{eur}"})
        if chk.get("ok") and not chk.get("page_price"):
            out["retail_verified"] = "page"
        out["retail_check"] = chk.get("why")
    if out["retail_verified"] == "none":
        out["retail_note"] = ("not proven: the page it came from was not cited and could not be "
                              "checked — shown, not proposed")
        return out
    out["new_retail"] = round(eur, 2)
    if new_selling and new_selling >= 0.95 * eur:
        out["retail_note"] = (f"the new selling price is {new_selling / eur:.0%} of retail — VNYX's "
                              f"rule PRICE.001 flags a price at or above 95% of retail "
                              f"(normal for sought-after resale, but it will be held)")
    return out


def probe() -> dict[str, Any]:
    """Does this key run Google Search through the Interactions API? One small call."""
    key = api_key()
    if not key:
        return {"ok": False, "error": "no Gemini key in .env"}
    usage = Usage()
    text, queries, cites, n, err = interact(PROBE_PROMPT, key, usage)
    return {"ok": not err and bool(queries), "error": err, "model": config()["search_model"],
            "queries": queries[:5], "searches": n, "citations": len(cites),
            "reply": text[:300].replace("\n", " ")}
