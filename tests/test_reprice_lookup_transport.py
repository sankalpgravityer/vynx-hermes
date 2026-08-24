"""The HTTP side of the retail lookup: throttling, retries, call accounting and
failure reporting.

Exercised against a real local HTTP server rather than a mocked urlopen, because
the things that matter here are transport behaviours -- a 429 costing a call and
returning nothing, a fallback billing twice for one product, a throttle holding a
rate across threads. A mock that returns dicts cannot show any of those.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "reprice_review_db",
    Path(__file__).resolve().parents[1] / "scripts" / "reprice_review_db.py",
)
assert _SPEC and _SPEC.loader
rr = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = rr
_SPEC.loader.exec_module(rr)


# --------------------------------------------------------------------------- #
# A stand-in for SerpApi / Google, driven by the query string
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    hits: list[float] = []
    lock = threading.Lock()

    def do_GET(self):                                     # noqa: N802
        query = parse_qs(urlparse(self.path).query).get("q", [""])[0]
        with Handler.lock:
            Handler.hits.append(time.monotonic())

        if query == "boom429":
            return self._send(429, {"error": "Your account has run out of searches"})
        if query == "boom500":
            return self._send(500, {"error": "internal"})
        if query == "boom401":
            return self._send(401, {"error": "Invalid API key"})
        if query == "quota200":
            # SerpApi reports quota problems in a 200 body.
            return self._send(200, {"error": "Your searches quota is exhausted"})
        if query == "empty":
            return self._send(200, {"shopping_results": []})
        if query == "nopricey":
            return self._send(200, {"shopping_results": [
                {"title": "A jacket", "link": "x"},
                {"title": "Another jacket", "link": "y"},
            ]})
        if query == "thin":
            return self._send(200, {"shopping_results": [
                {"title": "A jacket", "extracted_price": 100.0},
            ]})
        return self._send(200, {"shopping_results": [
            {"title": "Jacket A", "extracted_price": 100.0},
            {"title": "Jacket B", "extracted_price": 120.0},
            {"title": "Jacket C", "extracted_price": 140.0},
        ]})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):                            # silence
        pass


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}/search.json"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clear_hits():
    Handler.hits.clear()
    yield


def lookup_at(base, **kw):
    """A RetailLookup pointed at the stub instead of serpapi.com."""
    lk = rr.RetailLookup(serpapi_key="k", rate=kw.pop("rate", 0),
                         retries=kw.pop("retries", 0), **kw)
    lk._base = base

    def _serpapi(query, _base=base, _lk=lk):
        import urllib.parse
        url = f"{_base}?{urllib.parse.urlencode({'q': query})}"
        data, error = _lk._get_json(url, "serpapi")
        if error:
            return rr.RetailQuote(None, 0, "serpapi", error=error)
        if data.get("error"):
            return rr.RetailQuote(None, 0, "serpapi",
                                  error=f"serpapi said: {data['error']}")
        results = data.get("shopping_results")
        if not results:
            return rr.RetailQuote(None, 0, "serpapi",
                                  error="google_shopping returned no shopping "
                                        "results for this title")
        found = [float(r["extracted_price"]) for r in results
                 if r.get("extracted_price")]
        if len(found) < _lk.min_samples:
            return rr.RetailQuote(
                None, len(found), "serpapi",
                error=f"{len(results)} shopping result(s) but only {len(found)} "
                      f"carried a usable price; {_lk.min_samples} needed")
        return rr.RetailQuote(rr._median(found), len(found), "serpapi",
                              {"query": query})

    lk._serpapi = _serpapi
    return lk


# --------------------------------------------------------------------------- #
# Call accounting
# --------------------------------------------------------------------------- #

def test_a_successful_lookup_is_one_billable_call(server):
    lk = lookup_at(server)
    quote = lk._fetch("jacket")
    assert quote.retail == 120.0
    assert (lk.calls, lk.serpapi_calls, lk.fallback_calls) == (1, 1, 0)


def test_a_fallback_bills_twice_for_one_product(server):
    """This is why `paid API calls made` can exceed `products eligible` -- the
    number that looked like a bug in a real run."""
    lk = lookup_at(server, google_key="gk", google_cx="cx")
    lk._google_cse = lambda q: rr.RetailQuote(None, 0, "google_cse",
                                              error="no results")
    lk.fallback_calls = 0
    quote = lk._fetch("empty")
    assert lk.serpapi_calls == 1
    assert quote.retail is None
    # Both failures are reported, not just the last one.
    assert "serpapi:" in quote.error and "fallback google_cse:" in quote.error


def test_the_cache_prevents_a_second_call_for_the_same_title(server):
    lk = lookup_at(server)

    class Row:
        title, brand = "Jacket", "Brand"

    row = Row()
    assert lk.lookup(row).retail == 120.0
    assert lk.lookup(row).retail == 120.0
    assert lk.calls == 1
    assert lk.cache_hits == 1


# --------------------------------------------------------------------------- #
# Failures, and saying why
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("query,expected", [
    ("boom429", "HTTP 429 rate limited"),
    ("boom401", "HTTP 401"),
    ("quota200", "searches quota is exhausted"),
    ("empty", "no shopping results"),
    ("nopricey", "only 0 carried a usable price"),
    ("thin", "only 1 carried a usable price"),
])
def test_every_failure_mode_reports_a_usable_reason(server, query, expected):
    """A blank retail column with no explanation is the thing being fixed here.
    Each of these is a genuinely different problem with a different remedy."""
    lk = lookup_at(server)
    quote = lk._fetch(query)
    assert quote.retail is None
    assert expected in quote.error, quote.error


def test_a_429_is_counted_so_the_rate_can_be_lowered(server):
    lk = lookup_at(server)
    lk._fetch("boom429")
    assert lk.rate_limited == 1


def test_a_429_is_retried_and_a_401_is_not(server):
    """A rate limit is transient; a bad key fails identically every time, and
    retrying it just spends more quota."""
    retried = lookup_at(server, retries=2)
    retried._fetch("boom429")
    assert retried.calls == 3          # the original plus two retries

    not_retried = lookup_at(server, retries=2)
    not_retried._fetch("boom401")
    assert not_retried.calls == 1


def test_a_500_is_retried_too(server):
    lk = lookup_at(server, retries=1)
    lk._fetch("boom500")
    assert lk.calls == 2


# --------------------------------------------------------------------------- #
# Throttling
# --------------------------------------------------------------------------- #

def test_the_throttle_holds_the_rate_across_threads(server):
    """--lookup-workers alone is not a rate limit: four threads each finishing in
    200ms is 20 requests a second. The throttle has to be shared, not per-thread.
    """
    from concurrent.futures import ThreadPoolExecutor

    lk = lookup_at(server, rate=20.0)          # 50ms apart
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: lk._fetch(f"jacket{i}"), range(8)))
    elapsed = time.monotonic() - started

    assert lk.calls == 8
    # 8 calls at 20/s cannot finish in under ~350ms however many threads run.
    assert elapsed >= 0.30, elapsed
    gaps = [b - a for a, b in zip(sorted(Handler.hits), sorted(Handler.hits)[1:])]
    assert gaps, "no requests recorded"
    assert max(gaps) < 1.0          # and it does not stall


def test_a_zero_rate_disables_the_throttle(server):
    lk = lookup_at(server, rate=0)
    assert lk._interval == 0
    started = time.monotonic()
    for i in range(5):
        lk._fetch(f"jacket{i}")
    assert time.monotonic() - started < 2.0


def test_the_default_rate_is_conservative():
    """A 429 costs the call and returns nothing, so running under the ceiling is
    cheaper than finding it."""
    assert 0 < rr.DEFAULT_LOOKUP_RATE <= 5
    assert rr.parse_args(["--dsn", "x"]).lookup_rate == rr.DEFAULT_LOOKUP_RATE
