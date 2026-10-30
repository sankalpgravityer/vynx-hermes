"""The resale sites that let us READ them (29 Oct 2026).

eBay, Grailed, Etsy, Depop, Poshmark, Kleinanzeigen and thredUP refuse a plain
page fetch (403), so their prices only ever come from an API (eBay) or from
what the model read in a search snippet. Marktplaats, 2dehands and Vinted do
not: their listing pages come back with the price in structured data, and
Vinted's search page lists items with prices and photos. That is EU resale
evidence — the kind this lookup has least of — at no cost and with no key.

  page_price(url)        the price on one listing page, or None
  vinted_search(query)   Vinted's own search: listings with price and photo
"""
from __future__ import annotations

import html
import json
import logging
import re
from typing import Any
from urllib.parse import quote, urlparse

from app.net import make_client

log = logging.getLogger("hermes.resale.pages")

BROWSER = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
           "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
           "Accept-Language": "nl,en;q=0.9,de;q=0.8"}

# Sites whose listing pages can be read; the currency their prices are in.
READABLE = {"marktplaats.nl": "EUR", "2dehands.be": "EUR", "2ememain.be": "EUR",
            "vinted.nl": "EUR", "vinted.be": "EUR", "vinted.de": "EUR", "vinted.fr": "EUR",
            "vinted.it": "EUR", "vinted.es": "EUR", "vinted.at": "EUR", "vinted.ie": "EUR",
            "vinted.lt": "EUR", "vinted.pt": "EUR", "vinted.co.uk": "GBP", "vinted.pl": "PLN",
            "vinted.se": "SEK", "vinted.cz": "CZK", "vinted.com": "USD"}


def _domain(u: str) -> str:
    try:
        return urlparse(u or "").netloc.lower().removeprefix("www.")
    except Exception:  # noqa: BLE001
        return ""


def readable(url: str) -> bool:
    return _domain(url) in READABLE


_PRICE_RES = (
    # JSON-LD offers, either order: "priceCurrency":"EUR","price":82 (Vinted)
    re.compile(r'"priceCurrency"\s*:\s*"([A-Z]{3})"[^{}]{0,120}?"price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)'),
    re.compile(r'"price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?[^{}]{0,120}?"priceCurrency"\s*:\s*"([A-Z]{3})"'),
    # Marktplaats: "price":"85.99" with "currency":"EUR" somewhere near
    re.compile(r'"price"\s*:\s*"(\d+(?:[.,]\d{1,2})?)"[^{}]{0,200}?"currency"\s*:\s*"([A-Z]{3})"'),
)


def price_in(html_text: str, default_cur: str) -> tuple[float, str] | None:
    for i, rx in enumerate(_PRICE_RES):
        m = rx.search(html_text)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        cur, amt = (a, b) if i == 0 else (b, a)
        try:
            v = float(amt.replace(",", "."))
        except ValueError:
            continue
        if v > 0:
            return v, cur
    m = re.search(r'"price"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)', html_text)
    if m:
        try:
            v = float(m.group(1).replace(",", "."))
            if v > 0:
                return v, default_cur
        except ValueError:
            pass
    return None


def fetch(url: str, timeout_s: float = 20.0) -> str | None:
    try:
        with make_client(timeout_s) as http:
            r = http.get(url, headers=BROWSER, follow_redirects=True)
        return r.text if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


GONE = ("niet meer beschikbaar", "is verkocht", "no longer available", "this item is sold",
        "nicht mehr verfügbar", "advertentie is verlopen")


def page_price(url: str, timeout_s: float = 20.0) -> dict[str, Any] | None:
    """-> {"price_raw": "82 EUR", "sold": bool, "title": ...} from a readable
    listing page, or None (unreadable site, blocked, no price)."""
    d = _domain(url)
    if d not in READABLE:
        return None
    text = fetch(url, timeout_s)
    if not text:
        return None
    found = price_in(text, READABLE[d])
    if not found:
        return None
    low = text.lower()
    t = re.search(r'<meta property="og:title" content="([^"]*)"', text)
    avail = re.search(r'"availability"\s*:\s*"([^"]*)"', text)
    # Structured data first (Vinted: "InStock" / "SoldOut"); the word list only
    # on a page without it — "verkocht" also appears in a site's menus.
    sold = ("soldout" in avail.group(1).lower()) if avail else any(g in low for g in GONE)
    return {"price_raw": f"{found[0]:.2f} {found[1]}", "sold": sold,
            "title": html.unescape(t.group(1)) if t else ""}


# --------------------------------------------------------------------------- #
# Vinted's own search
# --------------------------------------------------------------------------- #
def vinted_search(query: str, domain: str = "vinted.nl", limit: int = 10,
                  timeout_s: float = 25.0) -> tuple[list[dict], str | None]:
    """Vinted's search results for a query: listings with title, price, photo
    and link, as the page embeds them. -> (comps, error). No key, one GET."""
    cur = READABLE.get(domain, "EUR")
    text = fetch(f"https://www.{domain}/catalog?search_text={quote(query)}", timeout_s)
    if not text:
        return [], f"Vinted ({domain}) did not answer"
    comps: list[dict] = []
    seen: set[str] = set()
    for item in _vinted_items(text):
        if not item.get("id") or item["id"] in seen:
            continue
        seen.add(item["id"])
        url = f"https://www.{domain}{item['path']}" if item.get("path") else f"https://www.{domain}/items/{item['id']}"
        comps.append({
            "title": (f"{item['title']} ({item['brand']})" if item.get("brand") and
                      item["brand"].lower() not in item["title"].lower() else item.get("title") or ""),
            "platform": "Vinted",
            "status": "listed",
            "condition": item.get("condition") or "",
            "colour": "",
            "size": item.get("size") or "",
            "location": domain.rsplit(".", 1)[-1].upper(),
            "price_raw": f"{item['price']} {item.get('currency') or cur}" if item.get("price") else "",
            "url": url,
            "image": item.get("photo") or "",
            "item_id": item["id"],
            "seller": item.get("seller") or "",
            "source": "vinted",
            "cited": [url],
        })
        if len(comps) >= limit:
            break
    return comps, None


_CARD = re.compile(
    r'data-testid="product-item-id-(\d+)"(?P<body>.{0,6000}?)'
    r'<img src="(?P<img>[^"]+)" alt="(?P<alt>[^"]*)"(?P<rest>.{0,3000}?)href="(?P<href>/items/\d+[^"?]*)',
    re.S)
_MONEY = re.compile(r"^(?:(€|£|\$)\s?)?(\d+(?:[.,]\d{2})?)\s?(€|£|\$|EUR|GBP|USD|PLN|SEK|CZK)?$")
_SYMBOL = {"€": "EUR", "£": "GBP", "$": "USD"}


def _vinted_items(text: str) -> list[dict]:
    """The item cards on a Vinted catalog page. Each card's image ALT text
    carries everything, localised: "Patagonia Parka Skyforest W, Merk:
    Patagonia, Staat: Heel goed, Maat: S, 82.00 €, 86.80 €" — the title, then
    "label: value" pairs (brand, condition, size in the site's language), then
    the item price and the price with buyer protection."""
    out: list[dict] = []
    for m in _CARD.finditer(text):
        parts = [html.unescape(x).strip() for x in m.group("alt").split(", ")]
        if not parts:
            continue
        title, fields, prices = parts[0], {}, []
        for part in parts[1:]:
            money = _MONEY.match(part.replace("\u00a0", " "))
            if money and (money.group(1) or money.group(3)):
                sym = money.group(1) or money.group(3)
                prices.append((money.group(2).replace(",", "."), _SYMBOL.get(sym, sym)))
            elif ":" in part:
                k, v = part.split(":", 1)
                fields[k.strip().lower()] = v.strip()
        brand = next((v for k, v in fields.items() if k in ("merk", "marke", "brand", "marque", "marca")), "")
        cond = next((v for k, v in fields.items() if k in ("staat", "zustand", "condition", "état", "etat", "stato", "estado")), "")
        size = next((v for k, v in fields.items() if k in ("maat", "größe", "groesse", "size", "taille", "taglia", "talla")), "")
        out.append({"id": m.group(1), "title": title, "brand": brand, "condition": cond, "size": size,
                    "price": prices[0][0] if prices else None,
                    "currency": prices[0][1] if prices else None,
                    "photo": html.unescape(m.group("img")),
                    "url": None, "path": m.group("href"), "seller": None})
    return out


DEFAULTS: dict[str, Any] = {"vinted": True, "vinted_domains": ["vinted.nl"], "limit": 10}


def direct_search(queries: list[str], cfg: dict[str, Any] | None) -> dict[str, Any]:
    """The readable sites' own search for each query (Vinted today).
    -> {comps, calls, notes}; listings deduplicated by item id."""
    cfg = {**DEFAULTS, **(cfg or {})}
    comps: list[dict] = []
    notes: list[str] = []
    calls = 0
    seen: set[str] = set()
    if cfg["vinted"]:
        for q in [x for x in dict.fromkeys(q.strip() for q in queries) if x]:
            for domain in cfg["vinted_domains"]:
                got, err = vinted_search(q, domain, int(cfg["limit"]))
                calls += 1
                if err:
                    notes.append(err)
                for c in got:
                    if c["item_id"] not in seen:
                        seen.add(c["item_id"])
                        comps.append(c)
    return {"comps": comps, "calls": calls, "notes": notes}


def price_readable_pages(comps: list[dict], max_pages: int = 6) -> int:
    """Lens (or Google) listings on a readable site that came without a
    price: open the page and take the price it shows. -> how many priced."""
    from concurrent.futures import ThreadPoolExecutor

    todo = [c for c in comps if not c.get("price_raw") and readable(c.get("url") or "")
            and "page_price" not in c][:max_pages]
    if not todo:
        return 0
    with ThreadPoolExecutor(max_workers=min(4, len(todo))) as pool:
        results = list(pool.map(lambda c: page_price(c["url"]), todo))
    n = 0
    for c, r in zip(todo, results):
        c["page_price"] = r or {"none": True}
        if r:
            c["price_raw"] = r["price_raw"]
            if r["sold"]:
                c["status"] = "sold"
            if r.get("title") and not c.get("title"):
                c["title"] = r["title"]
            c["source"] = (c.get("source") or "page") + "+page" if c.get("source") else "page"
            c["cited"] = c.get("cited") or [c["url"]]
            n += 1
    return n
