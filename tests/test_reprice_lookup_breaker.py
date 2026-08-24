"""The circuit breaker on the retail lookup.

Without it, a spent SerpApi quota meant every remaining product still made a
SerpApi call AND a Google fallback call, retrying the rate limits: ~1,400
requests for 460 products, all returning the same "quota exhausted", producing
nothing. The point of these tests is that the SECOND failure of that kind stops
the run from spending anything further.

Driven against a local HTTP server so the call count is measured rather than
mocked -- "how many requests did this actually make" is the whole question.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
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


class Handler(BaseHTTPRequestHandler):
    """Answers according to `mode`, counting every request it receives."""

    mode = "ok"
    count = 0
    lock = threading.Lock()

    def do_GET(self):                                      # noqa: N802
        with Handler.lock:
            Handler.count += 1
        mode = Handler.mode
        if mode == "quota200":
            return self._send(200, {"error": "Your account has run out of "
                                             "searches. Please upgrade."})
        if mode == "unauthorised":
            return self._send(401, {"error": "Invalid API key"})
        if mode == "ratelimited":
            return self._send(429, {"error": "Too many requests"})
        if mode == "notfound":
            return self._send(200, {"shopping_results": []})
        return self._send(200, {"shopping_results": [
            {"title": "A", "extracted_price": 100.0},
            {"title": "B", "extracted_price": 120.0},
        ]})

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def base():
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/search.json"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def reset():
    Handler.count = 0
    Handler.mode = "ok"
    yield


def make_lookup(base, retries=0, rate=0):
    """A RetailLookup whose SerpApi backend points at the stub."""
    lk = rr.RetailLookup(serpapi_key="k", retries=retries, rate=rate)

    def _serpapi(query):
        import urllib.parse
        url = f"{base}?{urllib.parse.urlencode({'q': query})}"
        data, error = lk._get_json(url, "serpapi")
        if error:
            return rr.RetailQuote(None, 0, "serpapi", error=error)
        if data.get("error"):
            message = f"serpapi said: {data['error']}"
            lk._note_failure("serpapi", message)
            return rr.RetailQuote(None, 0, "serpapi", error=message)
        results = data.get("shopping_results") or []
        found = [float(r["extracted_price"]) for r in results
                 if r.get("extracted_price")]
        if len(found) < lk.min_samples:
            return rr.RetailQuote(None, len(found), "serpapi",
                                  error="no usable prices for this title")
        return rr.RetailQuote(rr._median(found), len(found), "serpapi")

    lk._serpapi = _serpapi
    return lk


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("message", [
    "HTTP 401",
    "HTTP 403: forbidden",
    "serpapi said: Your account has run out of searches",
    "serpapi said: Your searches quota is exhausted",
    "Invalid API key",
    "daily limit exceeded",
])
def test_a_broken_backend_is_recognised(message):
    assert rr.is_fatal_error(message) is True


@pytest.mark.parametrize("message", [
    "google_shopping returned no shopping results for this title",
    "12 shopping result(s) but only 1 carried a usable price; 2 needed",
    "network error: timed out",
    "HTTP 500",
])
def test_a_single_failed_product_is_not_treated_as_a_broken_backend(message):
    """These are per-product outcomes. Tripping the breaker on them would
    abandon the run over one garment with a vague title."""
    assert rr.is_fatal_error(message) is False


# --------------------------------------------------------------------------- #
# The breaker actually stops the spending
# --------------------------------------------------------------------------- #

def test_an_exhausted_quota_stops_after_the_first_product(base):
    Handler.mode = "quota200"
    lk = make_lookup(base)

    for i in range(50):
        lk._fetch(f"product {i}")

    assert Handler.count == 1, (
        f"made {Handler.count} requests after the quota was reported spent")
    assert "serpapi" in lk.disabled
    assert "run out of searches" in lk.disabled["serpapi"]
    assert lk.skipped_calls == 49


def test_a_dead_key_stops_after_the_first_product(base):
    Handler.mode = "unauthorised"
    lk = make_lookup(base)
    for i in range(20):
        lk._fetch(f"product {i}")
    assert Handler.count == 1
    assert "serpapi" in lk.disabled


def test_the_reason_reaches_every_remaining_product(base):
    """Each row still gets an explanation -- a blank column would leave the
    operator guessing which products were actually checked."""
    Handler.mode = "quota200"
    lk = make_lookup(base)
    lk._fetch("first")
    quote = lk._fetch("second")
    assert quote.retail is None
    assert "disabled for this run" in quote.error
    assert "run out of searches" in quote.error


def test_enough_rate_limits_gives_up_but_not_on_the_first(base):
    """A few 429s are a transient burst worth riding out. Enough of them means
    the ceiling will not move within the run."""
    Handler.mode = "ratelimited"
    lk = make_lookup(base)
    for i in range(rr.RATE_LIMIT_GIVE_UP + 10):
        lk._fetch(f"product {i}")

    assert Handler.count == rr.RATE_LIMIT_GIVE_UP, Handler.count
    assert "serpapi" in lk.disabled
    assert "rate limits (HTTP 429)" in lk.disabled["serpapi"]
    assert lk.skipped_calls == 10


def test_rate_limits_are_counted_in_total_not_as_a_streak(base):
    """The defect this replaced.

    The first version counted CONSECUTIVE 429s and reset on any success. With
    several workers in flight one lucky reply kept resetting the counter, and a
    real run took 1,459 rate limits without ever tripping the breaker. A quota
    ceiling produces 429s interleaved with the occasional success, so the total
    is the signal and the streak is noise.
    """
    lk = make_lookup(base)
    for i in range(rr.RATE_LIMIT_GIVE_UP - 1):
        Handler.mode = "ratelimited"
        lk._fetch(f"limited {i}")
        Handler.mode = "ok"                 # a success between every 429
        lk._fetch(f"fine {i}")

    assert lk.disabled == {}, "not tripped yet -- one short of the threshold"
    Handler.mode = "ratelimited"
    lk._fetch("the last straw")
    assert "serpapi" in lk.disabled, (
        "interleaved successes must not stop the 429s accumulating")


def test_the_threshold_is_configurable(base):
    Handler.mode = "ratelimited"
    lk = rr.RetailLookup(serpapi_key="k", retries=0, rate=0, max_rate_limits=3)
    lk._serpapi = make_lookup(base)._serpapi          # borrow the stub backend
    for i in range(10):
        lk._get_json(f"{base}?q=x{i}", "serpapi")
    assert "serpapi" in lk.disabled
    assert lk.calls == 3


def test_products_that_are_merely_not_found_never_trip_it(base):
    """The common case: a vague title Google has nothing for. The run must
    continue -- this is not evidence the backend is broken."""
    Handler.mode = "notfound"
    lk = make_lookup(base)
    for i in range(15):
        lk._fetch(f"product {i}")
    assert Handler.count == 15
    assert lk.disabled == {}
    assert lk.skipped_calls == 0


def test_a_success_does_not_wipe_the_rate_limit_count(base):
    """Explicitly the opposite of the original behaviour. Kept as its own test
    because the reset looked obviously correct and was the whole bug."""
    lk = make_lookup(base)
    Handler.mode = "ratelimited"
    for i in range(3):
        lk._fetch(f"a{i}")
    Handler.mode = "ok"
    assert lk._fetch("good").retail == 110.0
    assert lk._429_total.get("serpapi") == 3


def test_the_fallback_is_not_disabled_by_the_primary_failing(base):
    """They have separate keys and separate quotas. Killing the fallback because
    SerpApi is spent would waste the one backend still able to answer."""
    Handler.mode = "quota200"
    lk = make_lookup(base)
    lk.google_key, lk.google_cx = "gk", "cx"
    lk._google_cse = lambda q: rr.RetailQuote(99.0, 4, "google_cse")

    assert lk._fetch("first").retail == 99.0
    assert lk._fetch("second").retail == 99.0
    assert "serpapi" in lk.disabled
    assert "google_cse" not in lk.disabled
    assert Handler.count == 1          # the primary was called once, then never


def test_the_saving_is_the_point(base):
    """The concrete number: 460 eligible products against a spent quota used to
    cost ~460 primary calls plus ~460 fallback calls plus retries."""
    Handler.mode = "quota200"
    lk = make_lookup(base, retries=2)
    for i in range(460):
        lk._fetch(f"product {i}")
    assert Handler.count == 1
    assert lk.skipped_calls == 459


# --------------------------------------------------------------------------- #
# The call budget
#
# A separate guard from the breaker: the breaker stops when a backend says it is
# done, the budget stops when the RUN has spent what it was allowed to. A real
# run took 1,715 billable calls for 443 products with neither in place.
# --------------------------------------------------------------------------- #

def test_the_budget_stops_the_spending_dead(base):
    Handler.mode = "notfound"
    lk = make_lookup(base)
    lk.budget = 5
    for i in range(40):
        lk._fetch(f"product {i}")
    assert Handler.count == 5, Handler.count
    assert lk.calls == 5
    assert lk.budget_hit is True
    assert lk.skipped_calls == 35


def test_the_budget_reason_reaches_the_row(base):
    """The sheet has to distinguish "not found" from "we stopped paying"."""
    Handler.mode = "notfound"
    lk = make_lookup(base)
    lk.budget = 1
    lk._fetch("first")
    quote = lk._fetch("second")
    assert "call budget reached (1/1)" in quote.error


def test_no_budget_means_no_cap(base):
    Handler.mode = "notfound"
    lk = make_lookup(base)
    lk.budget = None
    for i in range(12):
        lk._fetch(f"p{i}")
    assert Handler.count == 12
    assert lk.budget_hit is False


def test_the_default_budget_leaves_room_for_fallbacks_but_not_a_runaway():
    """443 eligible products must allow ~488 calls, not 1,715."""
    def auto(eligible):
        return eligible + max(rr.BUDGET_HEADROOM_MIN,
                              -(-eligible * rr.BUDGET_HEADROOM_PCT // 100))

    assert auto(443) == 488
    assert auto(443) < 500                  # the stated ceiling
    assert auto(10) == 35                   # small runs still get headroom
    # And it is nowhere near what an uncapped runaway actually cost.
    assert auto(443) < 1715 / 2


def test_a_budget_exhausted_run_still_writes_the_prices_it_can(base):
    """The lookup is an enhancement, not a precondition: products that never got
    a quote keep their stored retail and are still repriced by the grade window.
    """
    Handler.mode = "notfound"
    lk = make_lookup(base)
    lk.budget = 1
    totals = rr.Totals()
    rows = [rr.price_row(
        {"id": "00000000-0000-4000-8000-%012d" % i, "sku": f"S{i}",
         "title": f"Jacket {i}", "tenantId": "x", "tenant_name": "T",
         "brand": "B", "current_price": "180.99", "retail_price": "159.99",
         "grade": "A", "variant_id": None, "variant_base_price": None},
        totals) for i in range(5)]

    rr.relookup_retail(rows, lk, totals, rr.DEFAULT_TOLERANCE,
                       rr.DEFAULT_MIN_PRICE, 1, "over_retail", "always",
                       rr.MIN_QUOTE_GAIN_PCT, 0)

    assert lk.budget_hit is True
    assert all(r.new_retail is None for r in rows)
    # Still repriced, against the anchor they already had.
    assert all(r.writable and r.new_price == 96.99 for r in rows)
