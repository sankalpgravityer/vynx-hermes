"""Find listings of an item FROM ITS PHOTO (29 Oct 2026, Phase 2 of the reprice plan).

Two sources, both official or contracted, neither a scrape of a listing site:

  eBay Browse API  `search_by_image`: our photo in, eBay's own visual matches
                   out — title, price, condition, seller country, item photo
                   and link — per marketplace (ebay.de, .co.uk, .com). Free
                   with the app's client credentials (EBAY_PRICE_CLIENT_ID /
                   _SECRET).
  SerpApi          the "google_lens" engine: Google Lens's visual matches for
                   a public image URL — title, source, link, thumbnail, and a
                   price when Google shows one. Paid per search (SERPAPI_KEY).

What they fix: BOA-007078's text search guessed "West Bend half-zip" and
priced it from other Columbia fleeces; eBay's image search returned the
"Lodge Sherpa Pullover, Stone Green/Beach" — the item — eight times.

What they give the rest of the lookup:
  * candidate listings with a PHOTO, so `resale_lookup.judge_matches` can
    compare pictures instead of titles (listing pages themselves refuse us);
  * `hints`: the titles the matches carry, which tell the text search and the
    judge what the item is likely called when the identify call was unsure.

Everything here is read-only and never raises: a source that is not set up,
is down or answers nonsense returns no listings and a note.
"""
from __future__ import annotations

import base64
import collections
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.parse import urlparse

import httpx

from app.net import make_client

log = logging.getLogger("hermes.resale.image")

EBAY_API = "https://api.ebay.com"
SERPAPI = "https://serpapi.com/search.json"

DEFAULTS: dict[str, Any] = {
    "ebay": True,
    "ebay_marketplaces": ["EBAY_DE", "EBAY_GB", "EBAY_US"],
    "ebay_limit": 8,
    "lens": True,
    "lens_country": "nl",
    "lens_max": 12,
    "lens_price_per_search_usd": 0.01,
    "timeout_s": 60,
}

# Lens returns every shop on earth; only resale listings are candidates. The
# others still feed `hints` (a brand's own page names the model best of all).
LENS_RESALE = ("ebay.", "vinted.", "grailed.", "depop.", "vestiairecollective.", "marktplaats.",
               "2dehands.", "kleinanzeigen.", "poshmark.", "mercari.", "thredup.", "etsy.",
               "wallapop.", "subito.", "tise.", "vide-dressing.", "sellpy.", "secondlife",
               "stockx.", "therealreal.")


def _domain(u: str) -> str:
    try:
        return urlparse(u or "").netloc.lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# eBay
# --------------------------------------------------------------------------- #
_token_lock = threading.Lock()
_token: dict[str, Any] = {"value": None, "expires": 0.0}


def ebay_credentials() -> tuple[str, str]:
    return (os.getenv("EBAY_PRICE_CLIENT_ID") or "").strip(), (os.getenv("EBAY_PRICE_CLIENT_SECRET") or "").strip()


def ebay_token(timeout_s: float = 30.0) -> tuple[str | None, str | None]:
    """An application token (client credentials), cached until shortly before
    it expires. -> (token, None) or (None, why)."""
    cid, sec = ebay_credentials()
    if not cid or not sec:
        return None, "no eBay keys: set EBAY_PRICE_CLIENT_ID and EBAY_PRICE_CLIENT_SECRET in .env"
    with _token_lock:
        if _token["value"] and time.time() < _token["expires"]:
            return _token["value"], None
        try:
            with make_client(timeout_s) as http:
                r = http.post(f"{EBAY_API}/identity/v1/oauth2/token",
                              headers={"Authorization": "Basic "
                                       + base64.b64encode(f"{cid}:{sec}".encode()).decode()},
                              data={"grant_type": "client_credentials",
                                    "scope": "https://api.ebay.com/oauth/api_scope"})
            js = r.json() if r.status_code == 200 else {}
        except Exception as exc:  # noqa: BLE001
            return None, f"eBay token: {type(exc).__name__}: {str(exc)[:120]}"
        if not js.get("access_token"):
            return None, f"eBay token refused (HTTP {r.status_code}): {r.text[:160]}"
        _token["value"] = js["access_token"]
        _token["expires"] = time.time() + float(js.get("expires_in") or 7200) - 120
        return _token["value"], None


def _ebay_comp(it: dict, marketplace: str) -> dict | None:
    url = it.get("itemWebUrl") or ""
    if not url:
        return None
    price = it.get("price") or {}
    loc = it.get("itemLocation") or {}
    return {
        "title": (it.get("title") or "").strip(),
        "platform": "eBay",
        "status": "listed",
        "condition": it.get("condition") or "",
        "colour": "",
        "size": "",
        "location": loc.get("country") or "",
        "price_raw": f"{price.get('value', '')} {price.get('currency', '')}".strip(),
        "url": url.split("?")[0],
        "image": (it.get("image") or {}).get("imageUrl") or "",
        "item_id": it.get("legacyItemId") or (it.get("itemId") or "").split("|")[1:2] and
                   (it.get("itemId") or "").split("|")[1],
        "seller": (it.get("seller") or {}).get("username") or "",
        "source": "ebay",
        "marketplace": marketplace,
        "cited": [url.split("?")[0]],
    }


def ebay_search_by_image(image: bytes, marketplace: str, limit: int = 8,
                         timeout_s: float = 60.0) -> tuple[list[dict], str | None]:
    """eBay's visual matches for our photo on one marketplace. -> (comps, error)."""
    token, err = ebay_token()
    if err:
        return [], err
    try:
        with make_client(timeout_s) as http:
            r = http.post(f"{EBAY_API}/buy/browse/v1/item_summary/search_by_image",
                          params={"limit": int(limit)},
                          json={"image": base64.b64encode(image).decode()},
                          headers={"Authorization": f"Bearer {token}",
                                   "X-EBAY-C-MARKETPLACE-ID": marketplace})
    except Exception as exc:  # noqa: BLE001
        return [], f"eBay {marketplace}: {type(exc).__name__}: {str(exc)[:120]}"
    if r.status_code != 200:
        return [], f"eBay {marketplace}: HTTP {r.status_code}: {r.text[:160]}"
    try:
        items = (r.json() or {}).get("itemSummaries") or []
    except Exception:  # noqa: BLE001
        return [], f"eBay {marketplace}: not JSON"
    return [c for c in (_ebay_comp(it, marketplace) for it in items) if c], None


def ebay_item_by_legacy_id(legacy_id: str, marketplace: str = "EBAY_US",
                           timeout_s: float = 30.0) -> dict | None:
    """One eBay listing by the number in its URL — the price, condition and
    photo of a Lens match that Google showed without a price. None if unknown."""
    token, err = ebay_token()
    if err or not legacy_id:
        return None
    try:
        with make_client(timeout_s) as http:
            r = http.get(f"{EBAY_API}/buy/browse/v1/item/get_item_by_legacy_id",
                         params={"legacy_item_id": legacy_id},
                         headers={"Authorization": f"Bearer {token}",
                                  "X-EBAY-C-MARKETPLACE-ID": marketplace})
        if r.status_code != 200:
            return None
        it = r.json() or {}
    except Exception:  # noqa: BLE001
        return None
    c = _ebay_comp(it, marketplace)
    if c:
        c["item_id"] = legacy_id
        c["source"] = "lens+ebay"
    return c


# --------------------------------------------------------------------------- #
# Google Lens, through SerpApi
# --------------------------------------------------------------------------- #
def serpapi_key() -> str:
    return (os.getenv("SERPAPI_KEY") or "").strip()


def _lens_price(m: dict) -> str:
    p = m.get("price")
    if isinstance(p, dict):
        v = str(p.get("value") or "").replace("*", "").strip()
        return v
    return str(p or "").replace("*", "").strip()


def lens_search(image_url: str, country: str = "nl", timeout_s: float = 60.0
                ) -> tuple[list[dict], list[str], str | None]:
    """Google Lens's visual matches for a PUBLIC image URL.
    -> (resale-site matches as comps, every match's title, error)."""
    key = serpapi_key()
    if not key:
        return [], [], "no SerpApi key: set SERPAPI_KEY in .env"
    try:
        with make_client(timeout_s) as http:
            r = http.get(SERPAPI, params={"engine": "google_lens", "url": image_url,
                                          "country": country, "hl": "en", "api_key": key})
        js = r.json()
    except Exception as exc:  # noqa: BLE001
        return [], [], f"Lens: {type(exc).__name__}: {str(exc)[:120]}"
    if js.get("error"):
        return [], [], f"Lens: {js['error']}"[:200]
    matches = js.get("visual_matches") or []
    titles = [str(m.get("title") or "").strip() for m in matches if m.get("title")]
    comps = []
    for m in matches:
        link = m.get("link") or ""
        d = _domain(link)
        if not link or not any(k in d for k in LENS_RESALE):
            continue
        item_id = None
        mm = re.search(r"ebay\.[a-z.]+/itm/(?:[^/]+/)?(\d{9,})", link)
        if mm:
            item_id = mm.group(1)
        comps.append({
            "title": str(m.get("title") or "").strip(),
            "platform": str(m.get("source") or d),
            "status": "listed",
            "condition": "",
            "colour": "",
            "size": "",
            "location": "",
            "price_raw": _lens_price(m),
            "url": link,
            "image": m.get("thumbnail") or m.get("image") or "",
            "item_id": item_id,
            "source": "lens",
            "cited": [link],
        })
    return comps, titles, None


# --------------------------------------------------------------------------- #
# Together
# --------------------------------------------------------------------------- #
STOP = {"the", "a", "and", "for", "with", "in", "of", "on", "to", "size", "sz", "new", "nwt",
        "used", "mens", "men's", "men", "womens", "women's", "women", "ladies", "unisex",
        "pre-owned", "preowned", "vintage", "xl", "xxl", "l", "m", "s", "xs", "2xl", "3xl"}


def hints_from_titles(titles: list[str], brand: str = "", top: int = 3) -> list[str]:
    """The names the matches agree on: the most frequent 2-3 word phrases
    (minus sizes, genders and fillers), e.g. "lodge sherpa pullover"."""
    counts: collections.Counter[str] = collections.Counter()
    b = (brand or "").lower().split()
    for t in titles:
        words = [w for w in re.findall(r"[a-z0-9/'-]+", t.lower())
                 if w not in STOP and w not in b and not re.fullmatch(r"\d+[a-z]*", w)]
        seen: set[str] = set()
        for n in (3, 2):
            for i in range(len(words) - n + 1):
                ph = " ".join(words[i:i + n])
                if ph not in seen:
                    seen.add(ph)
                    counts[ph] += 1
    out: list[str] = []
    for ph, n in counts.most_common(40):
        if n < 2:
            break
        if any(ph in o or o in ph for o in out):
            continue
        out.append(ph)
        if len(out) >= top:
            break
    return out


def image_search(photo: bytes | None, photo_url: str, brand: str, cfg: dict[str, Any]
                 ) -> dict[str, Any]:
    """Every source that is on and set up. -> {comps, hints, ebay_calls,
    lens_calls, notes}. Comps are deduplicated by eBay item id and by URL,
    an EU marketplace's copy of a listing winning over a US one."""
    cfg = {**DEFAULTS, **(cfg or {})}
    comps: list[dict] = []
    titles: list[str] = []
    notes: list[str] = []
    ebay_calls = lens_calls = 0
    timeout = float(cfg["timeout_s"])
    # Every call at once: three eBay marketplaces and Lens take one call's
    # wall time, not four (the user's ask, 29 Oct 2026: 2 minutes a product).
    jobs: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if cfg["ebay"] and photo:
            if ebay_token()[1]:            # no keys / refused: one note, no three
                notes.append(ebay_token()[1])
            else:
                for mk in cfg["ebay_marketplaces"]:
                    jobs[mk] = pool.submit(ebay_search_by_image, photo, mk,
                                           int(cfg["ebay_limit"]), timeout)
        if cfg["lens"] and photo_url:
            jobs["lens"] = pool.submit(lens_search, photo_url, str(cfg["lens_country"]), timeout)
    got_lens: list[dict] = []
    for mk in cfg["ebay_marketplaces"]:
        if mk not in jobs:
            continue
        ebay_calls += 1
        got, err = jobs[mk].result()
        if err:
            notes.append(err)
        comps += got
        titles += [c["title"] for c in got]
    if "lens" in jobs:
        got_lens, lens_titles, err = jobs["lens"].result()
        lens_calls += 1
        if err:
            notes.append(err)
        titles += lens_titles
    if got_lens:
        got = got_lens
        # A Lens eBay match without a price: eBay's own record has it (free).
        for c in got[: int(cfg["lens_max"])]:
            if c["source"] == "lens" and c.get("item_id") and not c["price_raw"] and cfg["ebay"]:
                full = ebay_item_by_legacy_id(c["item_id"], "EBAY_US", float(cfg["timeout_s"]))
                if full:
                    c.update({k: full[k] for k in ("price_raw", "condition", "location", "image")
                              if full.get(k)})
                    c["source"] = "lens+ebay"
            comps.append(c)
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    uniq: list[dict] = []
    for c in comps:                 # eBay DE, GB, US order: the EU copy comes first
        key_id = str(c.get("item_id") or "")
        key_url = (c.get("url") or "").split("?")[0].lower().rstrip("/")
        if (key_id and key_id in seen_ids) or key_url in seen_urls:
            continue
        if key_id:
            seen_ids.add(key_id)
        seen_urls.add(key_url)
        uniq.append(c)
    return {"comps": uniq, "hints": hints_from_titles(titles, brand),
            "ebay_calls": ebay_calls, "lens_calls": lens_calls, "notes": notes,
            "n_titles": len(titles)}
