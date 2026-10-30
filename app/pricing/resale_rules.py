"""Verified used listings in, ONE suggested resale price out. Pure logic.

Ported from `pricing_rules.py` in the BOAS handoff (5 Oct 2026), which was
calibrated on 261 of Balazs's own decisions on Klekt batch 1: verified lookups
within ±15% of his price 89% of the time, tier A 97%. The numbers below are his
v2/v3 values, unchanged, with three deliberate differences:

  ROUNDING   The handoff's `r99` rounds to the NEAREST euro and subtracts a cent
             (40.30 -> 39.99). VNYX keeps the euros and sets the cents to .99
             (40.30 -> 40.99) and never rounds down into a discount — that is
             `app.rules.pricing.charm99`, and two rules on one product would
             fight. So this uses VNYX's.
  OUTLIERS   The handoff compared prices by object identity (`x is not c`), which
             only works by accident for floats. Here "the others" are the other
             listings by position.
  BIG PRICES The handoff's price parser read a bare "1200 USD" as 120 (its
             thousands branch matched the first three digits). Fixed in
             `parse_price`.

The only network call is `fx_rates()` (ECB daily reference rates, with a
fallback table), and tests replace it with `set_fx_rates`.
"""
from __future__ import annotations

import re
import statistics as st
import unicodedata
from urllib.parse import urlparse

from app.rules.pricing import charm99

# --------------------------------------------------------------------------- #
# 1. Market / region
# --------------------------------------------------------------------------- #
EU_TLDS = ("de", "nl", "fr", "it", "es", "be", "at", "ie", "pt", "pl", "se", "dk", "fi",
           "cz", "gr", "lu", "hu", "ro", "sk", "si", "lt", "lv", "ee", "hr")
EU_SITES = ("vinted", "vestiairecollective", "marktplaats", "2dehands", "kleinanzeigen",
            "leboncoin", "vide-dressing", "wallapop", "subito", "tise", "sellpy", "zalando")
US_SITES = ("poshmark", "mercari", "therealreal", "ebay.com", "thredup")
GLOBAL_SITES = ("grailed", "depop", "etsy", "stockx")

# How much a listing counts, and the price level it is scaled to (EU = 1).
# v2 levels (23 Sep 2026): v1 stacked asking ×0.85, region ×0.8 and condition
# ×0.85 into ~0.58 of the listing price and cut cheap items far below VNYX.
REGION_WEIGHT = {"EU": 1.0, "UK": 0.8, "GLOBAL": 0.6, "US": 0.4, "?": 0.6}
REGION_LEVEL = {"EU": 1.0, "UK": 1.0, "GLOBAL": 0.95, "US": 0.9, "?": 0.95}

MIN_COMPS_TO_CUT = 3      # lowering VNYX's price needs at least this many verified listings
NO_CUT_BELOW = 25.0       # items VNYX prices under this are only ever raised, never cut
ASKING_FACTOR = 0.85      # a listed (unsold) price is discounted to a sold level
ASKING_WEIGHT = 0.7


def region_of(url: str, location: str = "") -> str:
    loc = (location or "").lower().strip()
    if loc:
        # A bare ISO code (eBay's itemLocation.country: "US", "GB", "DE").
        if loc in ("us", "ca"):
            return "US"
        if loc in ("gb", "uk"):
            return "UK"
        if loc in EU_TLDS:
            return "EU"
        if any(w in loc for w in ("united states", "usa", " us", "canada")):
            return "US"
        if any(w in loc for w in ("united kingdom", " uk", "england", "london", "scotland")):
            return "UK"
        if any(w in loc for w in ("germany", "deutschland", "netherlands", "nederland", "france",
                                  "italy", "spain", "belgium", "austria", "ireland", "poland",
                                  "lithuania", "portugal", "sweden", "denmark", "finland",
                                  "czech", "europe", " eu")):
            return "EU"
    d = (urlparse(url or "").netloc or "").lower().removeprefix("www.")
    if not d:
        return "?"
    if any(s in d for s in US_SITES) or (d.endswith(".com") and d.startswith("ebay.")):
        return "US"
    if d.endswith(".co.uk") or d.endswith(".uk"):
        return "UK"
    if any(s in d for s in EU_SITES) or d.rsplit(".", 1)[-1] in EU_TLDS:
        return "EU"
    if any(s in d for s in GLOBAL_SITES):
        return "GLOBAL"
    return "?"


# --------------------------------------------------------------------------- #
# Currency
# --------------------------------------------------------------------------- #
FX_FALLBACK = {"EUR": 1.0, "USD": 0.86, "GBP": 1.16, "SEK": 0.09, "DKK": 0.134,
               "PLN": 0.235, "CHF": 1.07, "CZK": 0.04}
ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
_fx_cache: dict | None = None


def fx_rates() -> dict:
    """EUR per 1 unit of each currency. ECB daily rates; the fallback table offline."""
    global _fx_cache
    if _fx_cache is not None:
        return _fx_cache
    rates: dict = dict(FX_FALLBACK)
    src = "fallback table"
    try:
        from app.net import make_client

        with make_client(10.0) as http:
            xml = http.get(ECB_URL).text
        for cur, rate in re.findall(r"currency='([A-Z]{3})' rate='([\d.]+)'", xml):
            rates[cur] = 1 / float(rate)           # ECB quotes units per EUR
        day = re.search(r"time='([\d-]+)'", xml)
        src = "ECB " + (day.group(1) if day else "?")
    except Exception:  # noqa: BLE001 — offline means the fallback table, not a failure
        pass
    rates["_source"] = src
    _fx_cache = rates
    return rates


def set_fx_rates(rates: dict | None) -> None:
    """Pin the rates (tests), or None to fetch again on the next call."""
    global _fx_cache
    _fx_cache = rates


SYMBOLS = {"€": "EUR", "£": "GBP", "₽": "RUB", "руб": "RUB", "¥": "JPY", "円": "JPY",
           "₩": "KRW", "zł": "PLN", "chf": "CHF", "a$": "AUD", "au$": "AUD", "c$": "CAD",
           "ca$": "CAD", "hk$": "HKD", "s$": "SGD", "₱": "PHP", "₹": "INR", "kr": "SEK",
           "$": "USD"}
# A rouble price once read as euros produced a €16,690 Haglöfs: anything outside
# this set is dropped rather than converted.
ACCEPTED_CURRENCIES = {"EUR", "USD", "GBP", "SEK", "DKK", "PLN", "CHF", "CZK", "NOK", "AUD", "CAD"}


def parse_price(text: str, default_cur: str = "EUR") -> tuple[float | None, str]:
    """'€1.234,50' / '120 USD' / '£60' -> (amount, ISO currency)."""
    t = (text or "").strip()
    if not t:
        return None, default_cur
    cur = None
    m = re.search(r"\b(EUR|USD|GBP|SEK|DKK|NOK|PLN|CHF|CZK|AUD|CAD|RUB|JPY|KRW|HKD|PHP|INR|US\$)\b",
                  t, re.I)
    if m:
        cur = m.group(1).upper().replace("US$", "USD")
    else:
        for sym, c in SYMBOLS.items():
            if sym in t.lower():
                cur = c
                break
    # A thousands group is REQUIRED by the first branch (`+`, the handoff had `*`):
    # with `*` it matched the first three digits of a bare "1200 USD" and read 120.
    num = re.search(r"(\d{1,3}(?:[.,\s]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)", t)
    if not num:
        return None, cur or default_cur
    s = num.group(1).replace(" ", "")
    if re.search(r"[.,]\d{1,2}$", s):              # the last separator is the decimals
        if s[-3] in ".,":
            s = re.sub(r"[.,](?=\d{3}\b)", "", s[:-3]) + "." + s[-2:]
        else:
            s = s.replace(",", ".")
    else:
        s = s.replace(",", "").replace(".", "")
    try:
        return float(s), cur or default_cur
    except ValueError:
        return None, cur or default_cur


def to_eur(amount: float | None, cur: str) -> float | None:
    """None for a currency we do not price from."""
    code = (cur or "EUR").upper()
    if amount is None or code not in ACCEPTED_CURRENCIES:
        return None
    rate = fx_rates().get(code)
    return round(amount * rate, 2) if rate else None


# --------------------------------------------------------------------------- #
# 2. Condition
# --------------------------------------------------------------------------- #
def cond_score(text: str) -> float | None:
    t = (text or "").lower()
    if not t:
        return None
    if any(w in t for w in ("new with tag", "nwt", "brand new", "neu mit etikett", "nieuw met",
                            "deadstock", "bnwt")):
        return 1.0
    if any(w in t for w in ("like new", "as new", "excellent", "mint", "hervorragend",
                            "zo goed als nieuw", "perfect", "near new", "10/10", "9/10")):
        return 0.9
    if any(w in t for w in ("very good", "sehr gut", "gently", "great")):
        return 0.82
    if any(w in t for w in ("good", "gut", "goed", "used", "pre-owned", "gebraucht", "gebruikt",
                            "worn", "8/10", "7/10")):
        return 0.72
    if any(w in t for w in ("fair", "lived", "flaw", "damage", "stain", "hole", "akzeptabel",
                            "6/10")):
        return 0.55
    return None


# VNYX's own condition words (`properties.condition`), as the handoff scored
# them, plus the values the local catalogue holds that it did not list.
OUR_COND = {"brand new": 1.0, "as new": 0.9, "excellent": 0.9, "very good": 0.82,
            "good": 0.75, "fair": 0.55, "lived in": 0.55, "poor": 0.45, "recycle": 0.35}
# Graded this way, VNYX auto-rejects the item: the lookup price is tier C.
DOUBTFUL_CONDITIONS = {"lived in", "recycle", "poor"}


def our_cond(text: str) -> float:
    return OUR_COND.get((text or "").strip().lower(), cond_score(text) or 0.8)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def wmedian(pairs: list[tuple[float, float]]) -> float | None:
    pairs = sorted((v, w) for v, w in pairs if v and w > 0)
    if not pairs:
        return None
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= total / 2:
            return v
    return pairs[-1][0]


def round99(x: float) -> float:
    """VNYX's shelf-price rule: keep the euros, cents to .99 (never down)."""
    return round(charm99(x), 2)


# --------------------------------------------------------------------------- #
# 3. Per-item price
# --------------------------------------------------------------------------- #
def price_item(comps: list[dict], our_condition: str, vnyx_price: float | None) -> dict:
    """`comps`: the COUNTED listings, each with price_eur, status, condition, url, location.

    Mutates each comp with `region`, `adj_eur`, `weight` (and `outlier` when
    dropped) so the report can show how every listing was used.
    """
    oc = our_cond(our_condition)
    for c in comps:                 # a re-price must not inherit an earlier pass's verdict
        c.pop("outlier", None)
    priced = [c for c in comps if c.get("price_eur")]
    if len(priced) >= 3:
        keep = []
        for i, c in enumerate(priced):
            others = [x["price_eur"] for j, x in enumerate(priced) if j != i]
            m = st.median(others)
            if not (m / 3 <= c["price_eur"] <= m * 3):
                c["outlier"] = True
                continue
            keep.append(c)
        priced = keep
    adj: list[tuple[float, float]] = []
    adj_eu: list[tuple[float, float]] = []
    for c in priced:
        p = c["price_eur"]
        reg = c.get("region") or region_of(c.get("url", ""), c.get("location", ""))
        sold = (c.get("status") or "").startswith("sold")
        cs = cond_score(c.get("condition", "")) or 0.75
        f = max(0.85, min(1.15, oc / cs))
        v = p * f * (1.0 if sold else ASKING_FACTOR) * REGION_LEVEL.get(reg, 0.9)
        w = REGION_WEIGHT.get(reg, 0.6) * (1.0 if sold else ASKING_WEIGHT)
        c["region"], c["adj_eur"], c["weight"] = reg, round(v, 2), round(w, 2)
        adj.append((v, w))
        if reg == "EU":
            adj_eu.append((v, 1.0 if sold else ASKING_WEIGHT))
    wm = wmedian(adj)
    eu = wmedian(adj_eu) if len(adj_eu) >= 2 else None
    plain = st.median([c["price_eur"] for c in priced]) if priced else None
    base = eu if eu is not None else wm
    sug = round99(base) if base else None
    # A cut needs listings that are the SAME item (29 Oct 2026): a listing the
    # same-item judge called "similar" counts for a raise, never for a cut.
    # Listings without a verdict (judge off, or failed) count as before.
    n_same = sum(1 for c in priced
                 if (c.get("match") or {}).get("verdict") in (None, "same", "unchecked"))
    note = ""
    if sug and vnyx_price and sug < vnyx_price:
        if vnyx_price < NO_CUT_BELOW:
            sug, note = vnyx_price, (f"listings ~€{base:.0f}, kept VNYX "
                                     f"(items under €{NO_CUT_BELOW:.0f} are only raised)")
        elif n_same < MIN_COMPS_TO_CUT:
            sug, note = vnyx_price, (f"listings ~€{base:.0f} but only {n_same} confirmed the "
                                     f"same item — too few to cut, kept VNYX")
    flag = ""
    if sug and vnyx_price:
        r = sug / vnyx_price
        if r < 0.5 or r > 3:
            flag = f"{r:.1f}× VNYX — check"
    return {
        "median_all_eur": round(plain, 2) if plain else None,
        "weighted_eur": round(wm, 2) if wm else None,
        "eu_median_eur": round(eu, 2) if eu else None,
        "n_eu": len(adj_eu),
        "n_priced": len(adj),
        "suggested_eur": sug,
        "basis": "EU listings" if eu is not None else ("all listings, weighted" if wm else ""),
        "price_flag": flag,
        "price_note": note,
    }


SHOE_STOCKX_FACTOR = 0.65   # a shoe sells at the StockX price less 35%


def price_shoe(comps: list[dict], vnyx_price: float | None) -> dict:
    """A shoe's price (the user's rule, 23 Oct 2026): the StockX price less 35%.

    `comps`: the COUNTED StockX listings. The last sale is the StockX price
    (the median when several pages sold); with no sale, the lowest ask. No
    condition, region or asking discount and no no-cut guards — the 35% is the
    whole calculation. Same keys as `price_item`.
    """
    priced = [c for c in comps if c.get("price_eur")]
    sold = [c for c in priced if (c.get("status") or "").startswith("sold")]
    use = sold or priced
    base = st.median([c["price_eur"] for c in use]) if use else None
    for c in priced:
        c.pop("outlier", None)
        c["region"] = c.get("region") or region_of(c.get("url", ""), c.get("location", ""))
        c["adj_eur"], c["weight"] = round(c["price_eur"] * SHOE_STOCKX_FACTOR, 2), 1.0
    sug = round99(base * SHOE_STOCKX_FACTOR) if base else None
    flag = ""
    if sug and vnyx_price:
        r = sug / vnyx_price
        if r < 0.5 or r > 3:
            flag = f"{r:.1f}× VNYX — check"
    return {
        "median_all_eur": round(base, 2) if base else None,
        "weighted_eur": None, "eu_median_eur": None, "n_eu": 0, "n_priced": len(priced),
        "suggested_eur": sug,
        "basis": (f"StockX {'last sale' if sold else 'lowest ask'} €{base:.2f} less 35%"
                  if base else ""),
        "price_flag": flag, "price_note": "",
    }


# --------------------------------------------------------------------------- #
# 4. Brands
# --------------------------------------------------------------------------- #
DESIGNER = {  # designer / luxury / hype streetwear — resale well above ordinary used clothing
    "stone island", "cp company", "supreme", "a bathing ape", "bape", "palace", "moncler",
    "the north face", "acne studios", "off white", "patta", "apc", "heliot emil",
    "billionaire boys club", "kenzo", "fear of god", "essentials", "aime leon dore",
    "jacquemus", "palm angels", "canada goose", "a cold wall", "noah", "aries", "awake",
    "awake ny", "paul smith", "snow peak", "nike acg", "arcteryx", "represent", "corteiz",
    "kith", "our legacy", "norse projects", "wood wood", "ami", "ami paris", "maison kitsune",
    "barbour", "burberry", "prada", "gucci", "balenciaga", "vivienne westwood",
    "yohji yamamoto", "issey miyake", "comme des garcons", "neighborhood", "wtaps",
    "human made", "undercover", "cav empt", "needles", "carhartt wip", "stussy", "rhude",
    "amiri", "mastermind", "visvim", "kapital", "sacai", "martine rose", "wales bonner",
    "casablanca", "ralph lauren purple label", "moncler genius", "c p company", "nanamica",
    "and wander", "salomon", "hoka"}


def _norm_brand(b: str) -> str:
    t = unicodedata.normalize("NFKD", (b or "").lower()).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9 ]+", " ", t.replace("'", "").replace(".", ""))
    return re.sub(r"\s+", " ", t).strip()


def brand_group(brand: str) -> str:
    n = _norm_brand(brand)
    return "designer" if n in DESIGNER or any(n.startswith(d + " ") for d in DESIGNER) else "other"


# --------------------------------------------------------------------------- #
# 5. Authenticity
# --------------------------------------------------------------------------- #
AUTH_STRONG = ("counterfeit", "replica", "fake", "bootleg", "not authentic", "inauthentic",
               "suspicious", "inconsistent", "unusual", "anomal", "does not match",
               "doesn't match", "red flag", "fantasy")


def auth_level(text: str) -> str:
    """'hold' when the photos gave a concrete reason to doubt; 'check' for
    generic advice ('verify the Certilogo code'); '' when nothing was said."""
    t = (text or "").strip().lower()
    if not t or t in ("none", "no", "n/a", "none observed", "no concerns", "none noted"):
        return ""
    return "hold" if any(w in t for w in AUTH_STRONG) else "check"


# --------------------------------------------------------------------------- #
# 6. Trust tiers — what may be accepted without a second look
# --------------------------------------------------------------------------- #
def trust_tier(final, vnyx, suggested, confidence, n, auth, cond_flag=False,
               n_same=None) -> tuple[str, str]:
    """A = safe to accept; B = spot-check; C = a person must look.

    v3 thresholds (24 Sep 2026, from 261 of Balazs's decisions): verified raises
    up to 6× were accepted 96-100% of the time; above 6× he lowered 8 of 28.
    """
    if not final:
        return "", ""
    if auth == "hold" or cond_flag:
        return "C", "authenticity or grade doubt"
    r = final / vnyx if vnyx else 1
    if r < 0.5:
        return "C", f"big cut ({r:.1f}× VNYX)"
    if final > 1000 or (r > 8 and (n or 0) <= 1):
        return "C", f"implausible: €{final:.0f} = {r:.1f}× VNYX from {n or 0} listing(s)"
    if not suggested:
        return "B", "estimate — no verified listings"
    n = n or 0
    # Since 29 Oct 2026 the same-item judge says which listings ARE the item:
    # tier A needs two of those, not two "similar" ones (BOA-006463 reached
    # A on three similar jackets and none confirmed).
    if n_same is not None and n_same < 2:
        return "B", (f"only {n_same} listing{'s' if n_same != 1 else ''} confirmed the same item"
                     f" ({n} counted)")
    if confidence in ("high", "medium") and n >= 2 and r <= 6:
        return "A", f"{n} verified listings, {r:.1f}× VNYX"
    if r > 6:
        return "B", f"very big raise ({r:.1f}× VNYX) — 1 in 3 of these were lowered by hand"
    return "B", f"only {n} listing{'s' if n != 1 else ''}" + (f", {r:.1f}× VNYX" if r > 3 else "")


def shoe_tier(final, vnyx, proven, auth, cond_flag=False) -> tuple[str, str]:
    """A shoe has one StockX page, not 2+ listings: tier A needs its price
    `proven` (the page Google returned; for the retail, the style code matches
    too). Doubt, a big cut, an implausible price and a >6× raise still go to a
    person, as in `trust_tier`."""
    if not final:
        return "", ""
    if auth == "hold" or cond_flag:
        return "C", "authenticity or grade doubt"
    r = final / vnyx if vnyx else 1
    if r < 0.5:
        return "C", f"big cut ({r:.1f}× VNYX)"
    if final > 1000:
        return "C", f"implausible: €{final:.0f} = {r:.1f}× VNYX"
    if r > 6:
        return "B", f"very big raise ({r:.1f}× VNYX)"
    if proven:
        return "A", f"StockX price on a page Google returned, {r:.1f}× VNYX"
    return "B", "StockX price not proven (page not returned by Google, or another style code)"
