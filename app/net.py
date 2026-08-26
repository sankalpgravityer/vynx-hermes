"""Outbound HTTP: one client policy, and a parallel fetcher with a deadline.

Shared by the image layer and the Gemini layer because they hit the same two
problems against the same network.


1. THE FIRST CONNECTION CAN STALL FOR 40-60 SECONDS

Measured on the dev host, same object, three runs to the tenth of a second:

    R2 image          43.1s default, 1.2s forced onto IPv4
    Gemini API        64.4s default, 0.3s forced onto IPv4

Not bandwidth — the AAAA record is tried first and times out before the IPv4
fallback. This machine already needs `127.0.0.1` rather than `localhost` for
service-to-service calls for exactly the same reason.

Binding the socket to `0.0.0.0` forces IPv4. That is the RIGHT fix for this host
and the WRONG default everywhere else — an IPv6-only network would break outright
— so it is an env switch, off by default, rather than a constant.


2. ONE SLOW OBJECT MUST NOT COST THE WHOLE REQUEST

A verify runs behind a button. Fetching serially means the slowest image sets the
latency, and a public `pub-*.r2.dev` URL is rate-limited by Cloudflare, so a slow
one is not rare. Fetching in parallel under a single wall-clock deadline means the
answer arrives on time with the stragglers marked unknown, which is a far better
outcome than a spinner that never stops.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import time

import httpx

log = logging.getLogger("hermes.imaging.http")


def prefer_ipv4() -> bool:
    """Env: HERMES_IMAGE_FETCH_IPV4. See the note above — host-specific, so not
    a policy value and not a constant."""
    return os.getenv("HERMES_IMAGE_FETCH_IPV4", "false").lower() == "true"


def genai_client_args() -> dict[str, object]:
    """`http_options.client_args` for a google-genai Client.

    The SDK builds its own httpx client, so the transport has to be handed to it
    rather than configured around it. Empty when the switch is off, which leaves
    the SDK's defaults completely untouched — the switch should be invisible to
    anyone who does not need it.
    """
    if not prefer_ipv4():
        return {}
    return {"transport": httpx.HTTPTransport(local_address="0.0.0.0", retries=1)}


def make_client(timeout_s: float) -> httpx.Client:
    """A client with a per-phase timeout, not one budget for the whole request.

    `httpx.Client(timeout=15)` applies 15s to each phase separately and the read
    timer restarts on every chunk, so a slow trickle can run far past it — which
    is how a 15s setting produced a 43s fetch. Connect is capped tight because a
    connection that has not been established in a few seconds is the stall case,
    not a slow one.
    """
    transport = (
        httpx.HTTPTransport(local_address="0.0.0.0", retries=1)
        if prefer_ipv4() else httpx.HTTPTransport(retries=1)
    )
    return httpx.Client(
        timeout=httpx.Timeout(connect=6.0, read=timeout_s, write=10.0, pool=5.0),
        follow_redirects=True,
        transport=transport,
        # Every image on a product comes from the same host, so one pooled
        # connection serves them all — and the expensive first handshake is paid
        # once rather than per image.
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
    )


def fetch_all(
    urls: list[str],
    timeout_s: float = 15.0,
    deadline_s: float = 30.0,
    client: httpx.Client | None = None,
) -> dict[str, bytes | None]:
    """Download every URL concurrently. Never raises.

    Returns url -> bytes, or url -> None for anything that failed or did not
    arrive before `deadline_s`. Order is not preserved; callers index by URL.
    """
    if not urls:
        return {}

    own = client is None
    http = client or make_client(timeout_s)
    out: dict[str, bytes | None] = {u: None for u in urls}
    started = time.perf_counter()

    def one(url: str) -> bytes | None:
        # Retried because the failure this exists for is a TRUNCATED body, not a
        # refused request: "peer closed connection without sending complete
        # message body (received 2108352 bytes, expected 6157219)" on a 6 MB
        # object. The rate-limited pub-*.r2.dev host does this under load, and the
        # first attempt often gets most of the way there — so a second attempt
        # usually succeeds where giving up would drop the image entirely.
        last: Exception | None = None
        for attempt in range(3):
            try:
                resp = http.get(url)
                resp.raise_for_status()
                mime = resp.headers.get("content-type", "").split(";")[0]
                if mime and not mime.startswith("image/"):
                    # Not transient — a size chart served as text/html will never
                    # become an image, so stop rather than burn two more attempts.
                    raise ValueError(f"not an image ({mime})")
                return resp.content
            except ValueError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt < 2:
                    log.info("refetching %s after %s", url[:70], type(exc).__name__)
                    time.sleep(0.5 * (attempt + 1))
        raise last if last else RuntimeError("fetch failed")

    try:
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(8, len(urls))
        ) as pool:
            futures = {pool.submit(one, u): u for u in urls}
            done, pending = concurrent.futures.wait(futures, timeout=deadline_s)
            for future in done:
                url = futures[future]
                try:
                    out[url] = future.result()
                except Exception as exc:  # noqa: BLE001
                    log.warning("image fetch failed (%s): %s", url[:70], exc)
            for future in pending:
                # Cancel what has not started; a request already in flight runs to
                # completion on its thread and its result is simply dropped. The
                # alternative — waiting for it — is the stall this exists to avoid.
                future.cancel()
                log.warning(
                    "image fetch did not finish inside %.0fs: %s",
                    deadline_s, futures[future][:70],
                )
    finally:
        if own:
            http.close()

    got = sum(1 for v in out.values() if v is not None)
    log.info("fetched %d/%d images in %.1fs", got, len(urls),
             time.perf_counter() - started)
    return out
