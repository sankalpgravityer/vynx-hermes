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
  * nothing here writes to VNYX;
  * a shoe (vnyx-api's footwear test) is searched and priced from StockX only
    (21 Oct 2026, the user's call): its own prompts, and a listing anywhere
    else is never counted. Since 23 Oct the Retail Price StockX shows is its
    retail, and its selling price is that less 35% (`judge_shoe_retail`,
    `resale_rules.shoe_tier`); with no StockX retail, the StockX last sale /
    lowest ask less 35% (`resale_rules.price_shoe`).

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
from app.pricing import image_search as IS
from app.pricing import own_listing as OWN
from app.pricing import resale_rules as R
from app.pricing import site_pages as SP

log = logging.getLogger("hermes.resale")

API = "https://generativelanguage.googleapis.com/v1beta"

DEFAULTS: dict[str, Any] = {
    "identify_model": "gemini-3.8-flash",
    "search_model": "gemini-3.8-flash",
    "max_searches": 5,
    "rescue_searches": 4,
    "page_check": True,
    "confirm_prices": True,
    "same_item_check": True,
    "match_model": None,            # None: identify_model
    "image_search": {},             # app/pricing/image_search.py DEFAULTS
    "direct_search": {},            # app/pricing/site_pages.py DEFAULTS (Vinted's own search)
    # The Google text search runs only when the image search found fewer
    # same-item listings than this (29 Oct 2026: time and cost).
    "enough_from_images": 3,
    "own_sellers": {},              # {tenant: [eBay seller names]} — never counted
    "own_photo_max_distance": 15,
    "own_price_match_distance": 20,  # this far apart still ours when the price is exactly ours
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
                "kleinanzeigen", "etsy", "poshmark", "vide-dressing", "wallapop", "subito", "tise",
                "thredup", "mercari", "sellpy", "zalando")   # thredup, zalando: the user's asks, 29 Oct 2026
# Zalando sells NEW: a zalando.* page is a resale listing only when it is a
# Zalando Pre-owned one; otherwise it is where the RETAIL NEW price comes from.
NEW_ONLY_UNLESS = {"zalando": ("pre-owned", "preowned", "second hand", "secondhand", "tweedehands")}


def _looks_invented(url: str, others: list[str]) -> bool:
    """A URL the model made up rather than read: an item number of round
    zeros (BOA-006489: four eBay links all ending /175000000000), or the same
    path given for several different listings."""
    p = norm_url(url)
    if not p:
        return True
    if sum(1 for o in others if norm_url(o) == p) > 1:
        return True
    # A Google redirect token pasted into a listing path (BOA-006950:
    # "ebay.co.uk/itm/AUZIYQFyPu84UhaXLe7...").
    if re.search(r"/[A-Za-z0-9_-]{40,}", p):
        return True
    # An eBay listing is /itm/<12 digits> (or /itm/<slug>/<12 digits>): a slug
    # with a short or no number is the model's reconstruction of a search
    # result title, not a page (BOA-006950: ".../Patagonia-Jacke-...-Blau-156810").
    if re.search(r"(^|\.)ebay\.[a-z.]+/itm/", p) and not re.search(r"/itm/(?:[^/]+/)?\d{12}(?:[^\d]|$)", p):
        return True
    m = re.search(r"(\d{9,})(?:[^\d]|$)", p)
    return bool(m and (m.group(1).endswith("00000") or len(set(m.group(1))) <= 2))
# Shoes are priced from StockX ONLY (the user's call, 21 Oct 2026): a shoe's
# eBay / Grailed / Vinted listing is never counted, whatever the model reports.
SHOE_SITES = ("stockx",)

# --------------------------------------------------------------------------- #
# Prompts — the handoff's, verbatim apart from line wrapping
# --------------------------------------------------------------------------- #
ID_PROMPT = """You are identifying a SECOND-HAND clothing item from its photos and brand/care label.
Catalogue record (may be vague or wrong; trust the photos and label):
{rec}
Labels often carry the model name, collection or an ARTICLE CODE (e.g. Stone Island 7515..., C.P.
Company 13CMSS...). Reply with ONLY this JSON (no markdown):
{{"brand": "", "model": "", "article_code": "", "era": "", "id_confidence": "high|medium|low",
 "garment_type": "what it is, e.g. sherpa fleece half-zip pullover",
 "colourway": "the colours and where they sit, e.g. beige sherpa yoke, olive fleece body, navy chest pocket",
 "features": ["3-6 short details that tell THIS item from similar ones by the same brand: closure,
   pockets, logo type and position, pattern, trims, fit"],
 "search_terms": ["2-4 short queries a reseller would type to find this exact item used"],
 "resale_interest": "high|medium|low", "interest_reason": "",
 "authenticity_concerns": "", "catalogue_corrections": ""}}
resale_interest: would this item resell for clearly MORE than ordinary used clothing of its type
(true vintage e.g. 80s/90s, a sought-after model, collab, rare colourway, designer)? high = yes,
clearly; medium = maybe; low = ordinary used item.{extra}"""

ID_SHOE_EXTRA = """
This item is FOOTWEAR. Its article_code is the STYLE CODE printed on the size label inside the
tongue or on the box (e.g. Nike DD1391-100, adidas GY7658, New Balance M990GL6). model = the
shoe's full name as StockX lists it (e.g. "Dunk Low Retro White Black"). search_terms: the
StockX product name, and the style code.
Also add "model_name": the model line for a product title — the model and any collaboration,
WITHOUT the brand and WITHOUT the colourway (e.g. "Dunk Low Retro", "Air Max 90",
"Air Zoom Spiridon Kukini Cage 2 Stussy", "Samba OG"). Identify it from the silhouette,
the design and any collaboration branding as well as from labels; "" when you cannot tell."""

_BLOCK_FORMAT = """Answer in EXACTLY this format, one block per listing (up to 6), then the summary lines:

### <listing title>
* Platform: <site>
* Status: sold | listed
* Condition: <as stated>
* Colour: <the colours the listing names or shows, or unknown>
* Size: <as stated, or unknown>
* Location: <country of the seller, or unknown>
* Price: <amount AND currency exactly as shown, e.g. 120 USD, €85, £60>
* URL: <URL of the individual listing>

ESTIMATE: €<realistic resale list price for our item>
CONFIDENCE: high | medium | low
RETAIL NEW: <the item's original retail price when sold new, amount AND currency as shown> | <URL of the brand's or a retailer's page that shows it — Zalando (zalando.nl / .de) or the brand's own shop first>
  (only a figure you saw on a page in your search results; do not spend extra searches on it; else: RETAIL NEW: unknown)
REASON: <one or two sentences>"""

SEARCH_PROMPT = """Search Google for USED / second-hand listings of this clothing item and report prices.

Item: {ident}
Our item: condition {cond}, size {size}. We sell in the Netherlands / EU.
Suggested searches: {terms}{hints}

Look on resale platforms: Vinted, Grailed, Vestiaire Collective, eBay, Depop, Marktplaats,
Kleinanzeigen, thredUP, Zalando Pre-owned. Prefer EU listings and SOLD prices. Use at most {max_s} searches in total — be targeted.{thredup}
A listing's URL must be the real one from the results — the exact item number — never a made-up or rounded one.
Only report listings you actually found in search results. Never invent a price or URL.
Fewer real listings beat more guessed ones.

""" + _BLOCK_FORMAT

RESCUE_PROMPT = """Second, EU-focused search for USED listings of this clothing item.

Item: {ident}
Our item: condition {cond}, size {size}.
The first search found too few verifiable listings. Now search ONLY European resale sites:
eBay.de / .nl / .fr / .it / .es, Vestiaire Collective, Vinted, Marktplaats, Kleinanzeigen, 2dehands,
Zalando Pre-owned — and thredUP (thredup.com).{hints}
Search by article code / model name, and in local language where useful (e.g. "gebraucht", "gebruikt").
Use at most {max_s} searches. Only report listings you actually found; never invent a price or URL.{thredup}

""" + _BLOCK_FORMAT

# A shoe is priced from the RETAIL price StockX shows (the user's rule, 23 Oct
# 2026): that is our retail, and the selling price is it less 35%. The style
# code the page shows is asked for so a wrong colourway's page cannot price it.
_SHOE_BLOCK_FORMAT = "\n".join(
    ("RETAIL NEW: <the Retail Price shown on this shoe's StockX page, amount AND currency as shown> "
     "| <that StockX page URL> | <the Style code shown on that page>\n"
     "  (only a figure you saw on stockx.com in your search results; else: RETAIL NEW: unknown)")
    if ln.startswith("RETAIL NEW:") else ln
    for ln in _BLOCK_FORMAT.splitlines() if not ln.lstrip().startswith("(only a figure"))

SHOE_SEARCH_PROMPT = """Search Google for this shoe on StockX (stockx.com) and report its prices there.

Item: {ident}
Our item: condition {cond}, size {size}. We sell in the Netherlands / EU.
Suggested searches: {terms}

Use ONLY stockx.com — add "site:stockx.com" to every search. Do not report eBay, Grailed, Vinted,
GOAT, Klekt or any other site. Find the StockX product page of this exact shoe (same style code
and colourway). Its Retail Price matters most: report it on the RETAIL NEW line. Also report
its prices as listings, for our size when the page shows it. Its last sale is Status: sold, its lowest ask
Status: listed. Condition: deadstock for a new pair, or as StockX states it for a used one.
Use at most {max_s} searches in total. Only report pages you actually found in search results.
Never invent a price or URL. Fewer real listings beat more guessed ones.

""" + _SHOE_BLOCK_FORMAT

SHOE_RESCUE_PROMPT = """Second StockX search for this shoe.

Item: {ident}
Our item: condition {cond}, size {size}.
The first search did not find this shoe's StockX Retail Price. Search stockx.com ONLY ("site:stockx.com"),
by the style code and by the model name with its colourway; a regional StockX page
(stockx.com/de-de/..., /en-gb/...) is fine. Never report another site.
Use at most {max_s} searches. Only report pages you actually found; never invent a price or URL.

""" + _SHOE_BLOCK_FORMAT

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
    # A Zalando product page is <slug>-<article code>.html; its category and
    # search pages have no .html (29 Oct 2026, the user's ask).
    if "zalando." in pu.netloc and path.endswith(".html") and not any(h in full for h in SEARCH_HINTS):
        return "listing"
    # A StockX product page is one slug, no id: stockx.com/nike-dunk-low-retro-white-black
    # (or under a region, /de-de/<slug>). One short word is a category (/sneakers).
    if pu.netloc.lower().removeprefix("www.").endswith("stockx.com"):
        segs = [s for s in path.split("/") if s]
        if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0]):
            segs = segs[1:]
        if (len(segs) == 1 and segs[0].count("-") >= 3 and not pu.query
                and not any(h in full for h in SEARCH_HINTS)):
            return "listing"
        return "search"
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
    from app.rules.imagery import is_footwear

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
        # vnyx-api's isFootwearCategory test: a shoe is priced from StockX only.
        "footwear": is_footwear(rec.get("masterCategory"), rec.get("category"),
                                rec.get("subCategory")),
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
        # The gallery (cut-outs, renders): what the channels show, so an own
        # listing's photo is one of these (app/pricing/own_listing.py).
        "gallery": [u for u in (rec.get("images") or []) if u][:6],
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


def cache_key(ident: dict, footwear: bool = False) -> str | None:
    """The same model in another size has the same listings. Specific ids only.
    A shoe's key is apart (`stockx|...`): an entry cached before shoes went
    StockX-only holds eBay and Grailed listings."""
    b = (ident.get("brand") or "").strip().lower()
    pre = "stockx|" if footwear else ""
    code = re.sub(r"[^a-z0-9]", "", (ident.get("article_code") or "").lower())
    if b and len(code) >= 5:
        return f"{pre}{b}|code|{code}"
    model = re.sub(r"\s+", " ", (ident.get("model") or "").strip().lower())
    if (b and ident.get("id_confidence") == "high" and model not in GENERIC_MODELS
            and len(model.split()) >= 3):
        return f"{pre}{b}|model|{model}"
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
                                            "retail_raw", "retail_url", "retail_cited",
                                            "retail_style")},
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
        self.lens_searches = 0      # SerpApi; eBay image calls are free

    def cost_usd(self, cfg: dict) -> float:
        lens = {**IS.DEFAULTS, **(cfg.get("image_search") or {})}["lens_price_per_search_usd"]
        return (self.tokens_in / 1e6 * cfg["token_price_in_per_1m_usd"]
                + self.tokens_out / 1e6 * cfg["token_price_out_per_1m_usd"]
                + self.searches / 1000 * cfg["price_per_1k_searches_usd"]
                + self.lens_searches * float(lens))


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
    parts = [_image_part(got[u], max_px=640) for u in urls if got.get(u)]
    if not parts:
        return None, "identify: no photo could be downloaded"
    item["_photos"] = {u: b for u, b in got.items() if b}     # reused by image search + judge
    photos_sent = len(parts)
    rec = {k: item.get(k) for k in ("title", "brand", "category", "sub_category", "gender",
                                    "size", "color", "material", "condition")}
    parts.append({"text": ID_PROMPT.format(rec=json.dumps(rec, ensure_ascii=False),
                                           extra=ID_SHOE_EXTRA if item.get("footwear") else "")})
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
    for k in ("brand", "model", "model_name", "article_code", "era", "id_confidence", "resale_interest",
              "interest_reason", "authenticity_concerns", "catalogue_corrections",
              "garment_type", "colourway"):
        if k in ident:
            ident[k] = _as_text(ident[k])
    for k in ("search_terms", "features"):
        if not isinstance(ident.get(k), list):
            ident[k] = [t for t in [_as_text(ident.get(k))] if t]
        ident[k] = [_as_text(t) for t in ident[k] if _as_text(t)]
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
            "colour": field("Colour"),
            "size": field("Size"),
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
    out = {"retail_new_eur": None, "retail_raw": "", "retail_url": "", "retail_style": "",
           "_retail_cite_raw": []}
    if not m:
        return out
    line = m.group(1).strip()
    if line.lower().startswith("unknown"):
        return out
    price_part, _, url_part = line.partition("|")
    url_part, _, style_part = url_part.partition("|")     # a shoe's line adds | <style code>
    out["retail_style"] = style_part.strip(" *.`")
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
    """Run confirm_price on every counted listing not yet confirmed. -> how many changed.
    A listing from an API (eBay image search, Lens) carries the platform's own
    price: nothing to confirm, and its page would refuse us anyway."""
    todo = [c for c in comps if c.get("counted") and "price_confirm" not in c
            and not c.get("source")][:10]
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
# Is the listing the SAME item? (29 Oct 2026, Phase 1 of the reprice plan)
#
# BOA-007078: the four "verified" listings were a different Columbia fleece
# each (another model, another colourway, a size S), and the lookup priced the
# item at €9.99 from them. "Verified" only ever meant "the page exists and
# Google returned it". This asks the question that was missing.
#
# FROM TEXT, NOT PHOTOS. eBay, Grailed, Etsy and Kleinanzeigen refuse a page
# fetch from us (HTTP 403) AND from Google's own url_context fetcher, so a
# listing's photo cannot be compared. What the search does return is the
# listing's title, condition, colour and size as the model read them in the
# results; the judge compares those with what the identify call saw in OUR
# photos: model, colourway, distinguishing features. One text call for all the
# listings of a product — no tools, ~$0.005.
#
#   same       counts in full
#   similar    counts for a raise, never for a cut (price_item), and never
#              makes the confidence "high" — it is the same model but the
#              colourway or a detail could not be confirmed
#   different  dropped: another model, colourway, gender, or garment
# --------------------------------------------------------------------------- #
MATCH_PROMPT = """You are checking whether second-hand listings are the SAME ITEM as ours, for pricing.

OUR ITEM (identified from our own photos and label):
{ours}

LISTINGS (the seller's title and details as read from search results; where a listing's photo was
available it follows this text, labelled with the listing's number — judge by the photo FIRST):
{listings}

For each listing answer: is it the same product — same brand AND same model/style AND the same
colourway — in any size and condition?
  same      the photo shows the same garment and colourway, or the title/details name the same
            model (or article code) and nothing contradicts the colourway or the features
  similar   the same model in ANOTHER COLOURWAY, or the same brand and garment type and plausibly
            the same model with the colourway or a distinguishing feature not confirmed by the
            photo or the title/details
  different another model, another gender's or a kids' cut, a different garment type, or a
            bundle / a part / a counterfeit — NOT merely another colour of the same model
A size that differs from ours is NOT a reason to say different. A listing whose title only names
the brand and the garment type ("Columbia fleece pullover") and has no photo is "similar" at best.
Our identification may be wrong about the model NAME: when a listing's photo shows the same garment
under another name, it is "same", and say the name.
Reply with ONLY a JSON array, one object per listing, in order:
[{{"n": 1, "verdict": "same|similar|different", "reason": "one short sentence"}}, ...]"""


def _ours_for_match(ident: dict, item: dict) -> str:
    lines = [f"brand: {ident.get('brand') or item.get('brand') or '?'}",
             f"model: {ident.get('model') or '?'}",
             f"article code: {ident.get('article_code') or 'none seen'}",
             f"garment: {ident.get('garment_type') or item.get('sub_category') or '?'}",
             f"colourway: {ident.get('colourway') or item.get('color') or '?'}",
             f"distinguishing features: {'; '.join(ident.get('features') or []) or '?'}",
             f"gender/department: {item.get('gender') or '?'}",
             f"our size: {item.get('size') or '?'}",
             f"identification confidence: {ident.get('id_confidence') or '?'}"]
    if item.get("_hints"):
        lines.append("image search (eBay / Google Lens) most often calls this item: "
                     + "; ".join(item["_hints"]))
    return "\n".join(lines)


def _listing_photos(comps: list[dict], item: dict | None = None) -> dict[str, bytes]:
    """The listing photos (eBay item images, Lens thumbnails) that could be
    downloaded, by URL — fetched once per lookup (`item["_listing_photos"]`).
    Small and quick; a miss is a text-only judgement."""
    cache: dict[str, bytes | None] = item.setdefault("_listing_photos", {}) if item is not None else {}
    urls = [c["image"] for c in comps if c.get("image") and c["image"] not in cache]
    if urls:
        got = fetch_all(urls, timeout_s=15.0, deadline_s=40.0)
        cache.update(got)
    return {c["image"]: cache[c["image"]] for c in comps
            if c.get("image") and cache.get(c["image"])}


def mark_own_listings(item: dict, comps: list[dict]) -> int:
    """Our own listings of this product (app/pricing/own_listing.py), by the
    eBay seller name and by our own photos. -> how many. Never raises."""
    cfg = config()
    try:
        ours = dict(item.get("_photos") or {})
        # The first gallery pictures are the cut-outs the channels were given
        # (BOA-006463's own listings matched the first one, 3 bits apart).
        gallery = [u for u in (item.get("gallery") or []) if u not in ours][:3]
        if gallery:
            ours.update({u: b for u, b in fetch_all(gallery, timeout_s=15.0, deadline_s=30.0).items() if b})
        hashes = item.get("_own_hashes")
        if hashes is None:
            hashes = item["_own_hashes"] = OWN.own_hashes(ours)
        return OWN.mark_own(comps, hashes, OWN.own_sellers_for(item.get("tenant"), cfg),
                            _listing_photos(comps, item), int(cfg["own_photo_max_distance"]),
                            our_price=item.get("vnyx_price"),
                            price_match_distance=int(cfg["own_price_match_distance"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("own-listing check failed: %s", exc)
        return 0


def _listing_for_match(n: int, c: dict) -> str:
    bits = [f"{n}. title: {c.get('title') or '?'}"]
    for k in ("platform", "condition", "colour", "size", "location", "price_raw"):
        v = (c.get(k) or "").strip()
        if v and v.lower() not in ("unknown", "?", "-", "—"):
            bits.append(f"   {k.replace('_raw', '')}: {v}")
    return "\n".join(bits)


JUDGE_BATCH = 6          # small batches, judged at once: ~20 photo candidates in one call's time


def judge_matches(ident: dict, item: dict, comps: list[dict], key: str, usage: Usage) -> int:
    """Every candidate listing without a verdict, JUDGE_BATCH a call (the
    image search can bring 20+). Sets c["match"] = {"verdict", "reason"}.
    -> how many were judged. Never raises: a failed call leaves its listings
    unjudged, which `summarise` treats as before."""
    todo = [c for c in comps if c.get("candidate") and not c.get("match")]
    if not todo:
        return 0
    _listing_photos(todo, item)                 # one download for every batch
    batches = [todo[i:i + JUDGE_BATCH] for i in range(0, len(todo), JUDGE_BATCH)]
    if len(batches) == 1:
        return _judge_batch(ident, item, batches[0], key, usage)
    with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:   # batches at once
        return sum(pool.map(lambda b: _judge_batch(ident, item, b, key, usage), batches))


def _judge_batch(ident: dict, item: dict, todo: list[dict], key: str, usage: Usage) -> int:
    cfg = config()
    prompt = MATCH_PROMPT.format(
        ours=_ours_for_match(ident, item),
        listings="\n".join(_listing_for_match(i + 1, c) for i, c in enumerate(todo)))
    parts: list[dict] = [{"text": prompt}]
    # Our front photo, then each listing's photo that could be fetched
    # (29 Oct 2026, Phase 2): the judge compares pictures where it can.
    ours = item.get("_photos") or {}
    front = next((ours[u] for u in (item.get("images") or []) if ours.get(u)), None)
    photos = _listing_photos(todo, item)
    if front and photos:
        parts.append({"text": "OUR ITEM, photo:"})
        parts.append(_image_part(front, max_px=512))
        for i, c in enumerate(todo):
            b = photos.get(c.get("image") or "")
            if b:
                c["photo_judged"] = True
                parts.append({"text": f"Listing {i + 1}, photo:"})
                parts.append(_image_part(b, max_px=384))
    js, err = _post(f"{API}/models/{cfg.get('match_model') or cfg['identify_model']}:generateContent",
                    {"contents": [{"parts": parts}],
                     "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096,
                                          "responseMimeType": "application/json"}},
                    key, float(cfg["identify_timeout_s"]), tries=2)
    if err:
        log.warning("same-item check failed: %s", err)
        for c in todo:
            c["match"] = {"verdict": "unchecked", "reason": f"check failed: {err[:120]}"}
        return 0
    um = js.get("usageMetadata") or {}
    usage.tokens_in += um.get("promptTokenCount", 0) or 0
    usage.tokens_out += (um.get("candidatesTokenCount", 0) or 0) + (um.get("thoughtsTokenCount", 0) or 0)
    cand = (js.get("candidates") or [{}])[0]
    text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", [])
                   if isinstance(p, dict))
    try:
        m = re.search(r"\[.*\]", re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M), re.S)
        rows = json.loads(m.group(0)) if m else []
    except Exception:  # noqa: BLE001
        rows = []
    by_n = {int(r.get("n", 0)): r for r in rows if isinstance(r, dict)}
    judged = 0
    for i, c in enumerate(todo):
        r = by_n.get(i + 1)
        v = str((r or {}).get("verdict") or "").strip().lower()
        if v in ("same", "similar", "different"):
            c["match"] = {"verdict": v, "reason": _as_text((r or {}).get("reason"))[:300]}
            judged += 1
        else:
            c["match"] = {"verdict": "unchecked", "reason": "no verdict in the reply"}
    return judged


# --------------------------------------------------------------------------- #
# Which listings count
# --------------------------------------------------------------------------- #
def summarise(item: dict, res: dict, grounding: dict) -> dict:
    """Our own numbers from the listings — never just the model's estimate.

    A listing counts only if a search really ran, its price is above zero, it is
    on a resale site (for a shoe: StockX, and nothing else), its URL is an
    individual listing, and it is grounded: the exact page Google cited
    (`exact`), a listing on a cited site (`site`), or a page we opened that
    shows the price (`page`). That makes it a `candidate`; since 29 Oct 2026
    it also needs the same-item judge not to have called it `different`.
    """
    sites = SHOE_SITES if item.get("footwear") else RESALE_SITES
    comps = [c for c in (res.get("comps") or []) if isinstance(c, dict)]
    good = []
    searched = (bool(grounding.get("queries")) or bool(grounding.get("image_searches"))
                or bool(grounding.get("direct_searches")))
    src = grounding.get("sources") or []
    src_urls = {norm_url(x["url"]) for x in src if x.get("url")}
    gdomains = {d.lower().removeprefix("www.") for d in grounding.get("domains", [])}
    gdomains |= {domain(x["url"]) for x in src if x.get("url")}
    claimed: set[str] = set()
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
        c["resale_site"] = any(k in d for k in sites)
        for site, words in NEW_ONLY_UNLESS.items():
            if site in d:
                text = (u + " " + str(c.get("title") or "") + " " + str(c.get("condition") or "")).lower()
                c["resale_site"] = any(w in text for w in words)
        c["kind"] = url_kind(u)
        site_ok = bool(d) and any(d == g or d.endswith("." + g) or g.endswith("." + d)
                                  for g in gdomains if g)
        cited = [x for x in (c.get("cited") or []) if x]
        if c.get("source") in ("ebay", "lens", "lens+ebay", "vinted", "lens+page", "page"):
            # From an API, a site's own search, or the listing page itself —
            # not from the model's reading of a results page: the listing
            # exists, with the price the platform showed.
            c["verified"] = c["source"]
        elif "cited" in c:
            if norm_url(u) in {norm_url(x) for x in cited}:
                c["verified"] = "exact"
            else:
                same = [x for x in cited if domain(x) == d and url_kind(x) == "listing"
                        and norm_url(x) not in claimed]
                if same:                    # the model typed the URL wrong; the cited one is real
                    c["url_as_written"], u = u, same[0]
                    c["url"], c["kind"] = u, url_kind(u)
                    c["verified"] = "exact"
                    claimed.add(norm_url(u))    # one cited page stands in for ONE listing
                else:
                    c["verified"] = "site" if any(domain(x) == d for x in cited) else "none"
        else:
            c["verified"] = "exact" if norm_url(u) in src_urls else ("site" if site_ok else "none")
        if c["verified"] == "site" and _looks_invented(u, [str(x.get("url") or "") for x in comps]):
            # On a cited site, but the page itself is the model's invention.
            c["verified"], c["invented"] = "none", True
        if c["verified"] == "none" and (c.get("page_check") or {}).get("ok"):
            c["verified"] = "page"
        c["grounded"] = c["verified"] != "none"
        # "site" (the site was in the results, the page itself was not) is
        # not enough to count since 29 Oct 2026: its price is only what the
        # model read in a snippet, and its link may not open (BOA-006950).
        # Such a listing is shown as unconfirmed; it counts once a page check
        # shows the price (`page`).
        c["unconfirmed"] = c["verified"] == "site"
        c["candidate"] = bool(searched and p > 0 and c["resale_site"] and c["kind"] == "listing"
                              and c["grounded"] and not c["unconfirmed"]
                              and (c.get("price_confirm") or {}).get("ok") is not False)
        verdict = (c.get("match") or {}).get("verdict")
        if c.get("own"):                # our own listing of this product: never evidence
            c["candidate"] = False
        c["counted"] = c["candidate"] and verdict != "different"
        if c["counted"]:
            good.append((p, c.get("status") == "sold", c["verified"] in ("exact", "ebay", "lens", "lens+ebay", "vinted", "lens+page", "page"),
                         verdict in ("same", None, "unchecked")))
    sold = [p for p, s, *_ in good if s]
    listed = [p for p, s, *_ in good if not s]
    pts = sold + [p * R.ASKING_FACTOR for p in listed]
    n_exact = sum(1 for _, _, e, _ in good if e)
    n_same = sum(1 for *_, m in good if m)
    n_different = sum(1 for c in comps
                      if c.get("candidate") and (c.get("match") or {}).get("verdict") == "different")
    n_own = sum(1 for c in comps if c.get("own"))
    judged = any(c.get("match") for c in comps)
    if not searched:
        conf = "no search"           # answered from memory: nothing here is evidence
    elif len(good) >= 3 and res.get("estimate_confidence") != "low":
        # "high" needs the listings to be the item, not only to exist.
        conf = "high" if (len(sold) >= 1 or n_exact >= 2) and n_same >= 2 else "medium"
    elif len(good) >= 2:
        conf = "medium"
    elif len(good) == 1:
        conf = "low"
    else:
        conf = "no listings"
    counted = [c for c in comps if c.get("counted")]
    priced = (R.price_shoe(counted, item.get("vnyx_price")) if item.get("footwear")
              else R.price_item(counted, item.get("condition", ""), item.get("vnyx_price")))
    return {"comp_median_eur": round(st.median(pts), 2) if pts else None,
            "n_comps": len(good), "n_sold": len(sold), "n_exact": n_exact,
            "n_same": n_same if judged else None, "n_different": n_different, "n_own": n_own,
            "n_offered": len(comps), "confidence": conf, **priced}


# --------------------------------------------------------------------------- #
# One item, end to end
# --------------------------------------------------------------------------- #
def thredup_target(item: dict) -> bool:
    """A women's or kids' item (VNYX stores gender as "Women", '["women"]',
    '["men", "women"]' ...): where thredUP has stock worth a search."""
    g = str(item.get("gender") or "").lower()
    return any(w in g for w in ("women", "girl", "boy", "kid", "child", "baby"))


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
           identify_only: bool = False, same_item: bool | None = None,
           image: bool | None = None) -> dict[str, Any]:
    """Identify, search, verify, price. Never raises; an `error` key says why.

    `same_item` / `image` override the policy's same_item_check and the image
    search (the CLI's flags, to compare a lookup with and without them)."""
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

    timings: dict[str, float] = out.setdefault("timings", {})

    def timed(name: str, fn, *a, **k):
        t0 = time.perf_counter()
        try:
            return fn(*a, **k)
        finally:
            timings[name] = round(timings.get(name, 0.0) + time.perf_counter() - t0, 1)

    ident, err = timed("identify", identify, item, key, usage)
    if err:
        return done(error=err)
    out["identified"] = {k: ident.get(k, "") for k in ("brand", "model", "article_code", "era",
                                                         "id_confidence", "garment_type",
                                                         "colourway", "features")}
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
    shoe = bool(item.get("footwear"))
    out["price_source"] = "StockX only (footwear)" if shoe else "resale sites"
    search_prompt, rescue_prompt = ((SHOE_SEARCH_PROMPT, SHOE_RESCUE_PROMPT) if shoe
                                    else (SEARCH_PROMPT, RESCUE_PROMPT))
    # The same-item judge: not for a shoe, whose StockX page is matched by its
    # style code already.
    judge = (cfg["same_item_check"] if same_item is None else same_item) and not shoe
    out["same_item_check"] = bool(judge)

    def judge_now(res: dict, grounding: dict) -> None:
        summarise(item, res, grounding)               # sets each listing's `candidate`
        timed("own_check", mark_own_listings, item, res["comps"])
        if judge:
            summarise(item, res, grounding)
            timed("judge", judge_matches, ident, item, res["comps"], key, usage)

    # The image search (Phase 2): not for a shoe (StockX only), not on a cache
    # hit (the cached listings already include it, judged).
    # thredUP (the user's ask, 29 Oct 2026): named in both search rounds, and
    # for a women's or kids' item one search is spent on it outright — that is
    # most of what thredUP sells.
    thredup_txt = ("\nThis is a women's or kids' item: run ONE of the searches as "
                   "site:thredup.com <brand> <item>, and report what it shows."
                   if thredup_target(item) else "")
    icfg = {**IS.DEFAULTS, **(cfg.get("image_search") or {})}
    # Only with a key in .env: without EBAY_PRICE_CLIENT_ID/_SECRET and
    # SERPAPI_KEY the text search is the whole lookup, as before 29 Oct, and
    # no "no eBay keys" note is written on every product.
    have_keys = (icfg["ebay"] and all(IS.ebay_credentials())) or (icfg["lens"] and bool(IS.serpapi_key()))
    use_image = (have_keys if image is None else image) and not shoe
    hints_txt = ""

    ckey = cache_key(ident, footwear=shoe)
    cached = None if fresh else cache_get(ckey)
    if cached:
        res = copy.deepcopy(cached["res"])
        grounding = dict(cached["grounding"], search_count=0, cached_from=cached["sku"])
        if cfg["confirm_prices"]:               # listings cached before 15 Oct were never confirmed
            summarise(item, res, grounding)
            confirm_counted_prices(res["comps"])
        judge_now(res, grounding)                 # listings cached before 29 Oct were never judged
    else:
        # ORDER (29 Oct 2026): the photo sources first — eBay's image search
        # is free and Lens a cent, together ~10 s — and the Google text search
        # (the slow, paid part: ~$0.07-0.13 and 30-60 s) only when they found
        # fewer than `enough_from_images` same-item listings.
        img: dict[str, Any] = {"comps": [], "hints": [], "ebay_calls": 0, "lens_calls": 0,
                               "notes": []}
        if use_image:
            photos = item.get("_photos") or {}
            front_url = next((u for u in (item.get("images") or []) if photos.get(u)), "")
            img = timed("image_search", IS.image_search, photos.get(front_url), front_url,
                        ident.get("brand") or item.get("brand") or "", icfg)
            usage.lens_searches += img["lens_calls"]
            item["_hints"] = img["hints"]
            if img["hints"]:
                hints_txt = ("\nImage search (eBay / Google Lens) most often calls this item: "
                             + "; ".join(img["hints"]) + " — search those names too.")
        out["image_search"] = {
            "on": bool(use_image), "ebay": sum(1 for c in img["comps"] if c["source"] == "ebay"),
            "lens": sum(1 for c in img["comps"] if c["source"].startswith("lens")),
            "hints": img["hints"], "notes": img["notes"]}
        # The readable sites' OWN search (29 Oct 2026, Vinted): no key, one
        # GET a query, EU listings with price and photo — the evidence this
        # lookup had least of. Queries: brand + model, and brand + what the
        # image search calls it.
        dcfg = {**SP.DEFAULTS, **(cfg.get("direct_search") or {})}
        brand_q = ident.get("brand") or item.get("brand") or ""
        queries = [f"{brand_q} {ident.get('model') or ident.get('garment_type') or ''}".strip()]
        if img["hints"]:
            queries.append(f"{brand_q} {img['hints'][0]}".strip())
        direct = (timed("direct_search", SP.direct_search, queries, dcfg)
                  if dcfg["vinted"] else {"comps": [], "calls": 0, "notes": []})
        out["direct_search"] = {"on": bool(dcfg["vinted"]), "queries": queries,
                                "vinted": len(direct["comps"]), "notes": direct["notes"]}
        seen_direct = {norm_url(c["url"]) for c in direct["comps"]}
        res = {"comps": direct["comps"] + [c for c in img["comps"]
                                           if norm_url(c["url"]) not in seen_direct],
               "estimate_confidence": "low"}
        grounding: dict[str, Any] = {"queries": [], "domains": [], "n_sources": 0,
                                     "search_count": 0, "sources": [], "raw_answer": ""}
        if use_image:
            grounding["image_searches"] = {"ebay": img["ebay_calls"], "lens": img["lens_calls"]}
            # A Lens match on Marktplaats or Vinted came without a price: the
            # page shows it.
            timed("page_prices", SP.price_readable_pages, res["comps"])
        if direct["calls"]:
            grounding["direct_searches"] = {"vinted": direct["calls"]}
        if use_image or direct["calls"]:
            judge_now(res, grounding)
        got = summarise(item, res, grounding)
        n_from_images = got["n_same"] if got["n_same"] is not None else got["n_comps"]
        need_text = n_from_images < int(cfg["enough_from_images"])
        out["text_search"] = {"ran": need_text, "same_from_images": n_from_images,
                              "enough": int(cfg["enough_from_images"])}

        def merge_round(r2: dict, g2: dict, label: str) -> None:
            seen = {norm_url(c.get("url")) for c in res["comps"]}
            res["comps"] += [c for c in r2["comps"] if norm_url(c.get("url")) not in seen]
            for k in ("resale_estimate_eur", "estimate_confidence", "reasoning"):
                if r2.get(k) and (not res.get(k) or res.get(k) == "low"):
                    res[k] = r2[k]
            if not res.get("retail_new_eur") and r2.get("retail_new_eur"):
                res.update({k: r2.get(k) for k in ("retail_new_eur", "retail_raw", "retail_url",
                                                   "retail_cited", "retail_style")})
            grounding["queries"] += g2["queries"]
            grounding["search_count"] += g2["search_count"]
            grounding["sources"] += g2["sources"]
            grounding["n_sources"] = len(grounding["sources"])
            grounding["domains"] = sorted(set(grounding["domains"]) | set(g2["domains"]))
            grounding["raw_answer"] += (f"\n\n--- {label} ---\n" if grounding["raw_answer"] else "") \
                + g2["raw_answer"]

        if need_text:
            r1, g1, err = timed("text_search", search_round,
                                search_prompt.format(ident=desc, cond=item.get("condition"),
                                                     size=item.get("size"),
                                                     terms="; ".join(terms[:4]),
                                                     max_s=cfg["max_searches"], hints=hints_txt,
                                                     thredup=thredup_txt),
                                key, usage)
            if err and not res["comps"]:
                return done(error="search: " + err)
            if err:
                out["search_error"] = err
            else:
                merge_round(r1, g1, "Google round 1")
                timed("page_prices", SP.price_readable_pages, res["comps"])
                if page_checks:
                    summarise(item, res, grounding)
                    timed("page_checks", run_page_checks, res["comps"])
                judge_now(res, grounding)
            first = summarise(item, res, grounding)
            # Too few verified: one EU-only round, merged in — but only when
            # NOTHING was found yet; with the image search on, the EU round
            # rarely adds a verifiable listing (its pages refuse us) and costs
            # ~10 s and ~4 searches. A shoe: a 2nd StockX round when the
            # first found no StockX retail price.
            rescue_at = 1 if (img["ebay_calls"] or img["lens_calls"]) else 2
            if (not res.get("retail_new_eur")) if shoe else first["n_comps"] < rescue_at:
                r2, g2, err2 = timed("rescue_search", search_round,
                                     rescue_prompt.format(ident=desc, cond=item.get("condition"),
                                                          size=item.get("size"),
                                                          max_s=cfg["rescue_searches"],
                                                          hints=hints_txt, thredup=thredup_txt),
                                     key, usage)
                if not err2:
                    merge_round(r2, g2, "StockX round 2" if shoe else "EU round")
                    grounding["rescue"] = True
                    if page_checks:
                        summarise(item, res, grounding)
                        timed("page_checks", run_page_checks, res["comps"])
                    judge_now(res, grounding)         # the new round's listings
        if cfg["confirm_prices"]:
            summarise(item, res, grounding)
            timed("confirm_prices", confirm_counted_prices, res["comps"])
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
    if shoe:
        retail = judge_shoe_retail(res, grounding, ident)
        market_proven = bool(final) and s["n_exact"] >= 1
        if retail["retail_new_eur"] and (retail["new_retail"] or not market_proven):
            # The user's rule: StockX retail (to .99) is our retail; selling = it less 35%.
            base = R.round99(retail["retail_new_eur"])
            final = R.round99(base * R.SHOE_STOCKX_FACTOR)
            proven = bool(retail["new_retail"])
            s.update(suggested_eur=final, price_flag="", price_note="",
                     basis=f"StockX retail €{base:.2f} less 35%")
        else:                       # no StockX retail: its last sale / lowest ask less 35%
            proven = market_proven
        tier, tier_reason = R.shoe_tier(final, item.get("vnyx_price"), proven,
                                        out["auth_level"], cond_flag)
    else:
        tier, tier_reason = R.trust_tier(final, item.get("vnyx_price"), s["suggested_eur"],
                                         s["confidence"], s["n_comps"], out["auth_level"], cond_flag,
                                         n_same=s.get("n_same"))
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


def _code(x: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (x or "").lower())


def judge_shoe_retail(res: dict, grounding: dict, ident: dict) -> dict[str, Any]:
    """A shoe's retail from the StockX page, and whether to trust it.

    Proven (proposed as the NEW retail, and tier A possible) when the page is
    on stockx.com, Google returned that exact page (cited, or among the search
    sources — StockX refuses our own page check with a 403), and the style
    code the page shows is the one identified from the label. A wrong
    colourway's page (KLE-002781: an "Ash Pearl" page for a Dusty Pink
    FY2024) fails the style check.
    """
    eur, url = res.get("retail_new_eur"), res.get("retail_url") or ""
    out: dict[str, Any] = {"retail_new_eur": eur, "retail_raw": res.get("retail_raw") or "",
                           "retail_url": url, "retail_verified": "none", "new_retail": None,
                           "retail_note": ""}
    if not eur:
        out["retail_note"] = "no StockX retail price found — keep the current one"
        return out
    returned = {norm_url(x) for x in (res.get("retail_cited") or [])}
    returned |= {norm_url(x.get("url")) for x in grounding.get("sources") or [] if x.get("url")}
    style, ours = _code(res.get("retail_style")), _code(ident.get("article_code"))
    if not domain(url).endswith("stockx.com"):
        out["retail_note"] = "not from stockx.com — shown, not proposed"
    elif norm_url(url) not in returned:
        out["retail_note"] = "the StockX page was not among Google's results — shown, not proposed"
    elif not (style and ours and style == ours):
        out["retail_note"] = (f"the StockX page shows style {res.get('retail_style') or '?'}, the label "
                              f"says {ident.get('article_code') or '?'} — shown, not proposed")
    else:
        out["retail_verified"] = "cited"
        out["new_retail"] = R.round99(eur)          # retail ends in .99 too (the user's call)
    return out


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


# --------------------------------------------------------------------------- #
# The shoe's model name, for the catalogue (26 Oct 2026)
# --------------------------------------------------------------------------- #
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def shoe_model(payload: dict[str, Any], min_confidence: str = "high") -> dict[str, Any]:
    """The model line of a FOOTWEAR product from its photos — the identify call alone,
    no search, nothing cached or charged to the search ledger.

    -> {"model_name", "confidence", "accepted", "article_code", "error", "cost_usd"}.
    `accepted` only when the identifier's own confidence reaches `min_confidence`;
    a name below it is reported, never written.
    """
    item = item_from_payload(payload)
    out: dict[str, Any] = {"model_name": "", "confidence": "", "accepted": False,
                           "article_code": "", "error": None, "cost_usd": 0.0}
    if not item.get("footwear"):
        out["error"] = "not footwear"
        return out
    key = api_key()
    if not key:
        out["error"] = "no Gemini key (GEMINI_API_KEY / HERMES_RESALE_GEMINI_API_KEY)"
        return out
    usage = Usage()
    ident, err = identify(item, key, usage)
    out["cost_usd"] = round(usage.cost_usd(config()), 4)
    if err or not ident:
        out["error"] = err or "no answer"
        return out
    name = _as_text(ident.get("model_name")).strip()
    brand = str(item.get("brand") or "").strip()
    # Never the brand twice in a title: "Nike Air Max 90" -> "Air Max 90".
    if brand and name.lower().startswith(brand.lower() + " "):
        name = name[len(brand):].strip()
    conf = str(ident.get("id_confidence") or "").lower()
    out.update(model_name=name, confidence=conf,
               article_code=_as_text(ident.get("article_code")),
               accepted=bool(name) and _CONFIDENCE_RANK.get(conf, -1)
               >= _CONFIDENCE_RANK.get(min_confidence, 2))
    return out
