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
import struct
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


# --------------------------------------------------------------------------- #
# Image dimensions from the file header — readiness phase 3
# --------------------------------------------------------------------------- #
#
# The canvas check (IMG.026) compares a cut-out with the photograph it was cut
# from, and the photograph is a 3-6 MB JPEG on a rate-limited host. Its width
# and height sit in the first few hundred bytes, so a ranged read of the head
# of the file answers the question at ~1% of the cost of a download — which is
# what lets the check run on every product of a batch rather than on one.
#
# `ProductMedia.width/height` are stored on 1% of rows today; the matte path
# now fills them as it works (phase 2), so over time this reader is the
# fallback rather than the rule.

_HEADER_BYTES = 65_536
# A JPEG with a large embedded ICC profile or EXIF thumbnail can push its SOF
# marker past 64 KB. Rare; one wider read before giving up.
_HEADER_BYTES_MAX = 1_048_576

# Every JPEG "start of frame" marker: baseline, progressive, lossless, the
# arithmetic-coded variants. The frame header carries the dimensions.
_JPEG_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
             0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def dims_from_header(data: bytes) -> tuple[int, int] | None:
    """Width and height from the head of a PNG, JPEG, WebP or GIF file.

    None when the bytes are not one of those, are too short to say, or the
    JPEG frame header lies beyond what was read. Never raises — a garbled file
    is "unknown", the same answer as "not fetched".
    """
    if not data or len(data) < 10:
        return None

    # PNG: the IHDR chunk is always first, at offset 8; width/height big-endian.
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) >= 24 and data[12:16] == b"IHDR":
            w, h = struct.unpack(">II", data[16:24])
            return (w, h) if w and h else None
        return None

    # GIF: logical screen size straight after the signature, little-endian.
    if data[:6] in (b"GIF87a", b"GIF89a"):
        w, h = struct.unpack("<HH", data[6:10])
        return (w, h) if w and h else None

    # WebP: RIFF container, then one of three bitstream chunks.
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            # Extended: 24-bit canvas width-1 / height-1 after a flags byte and
            # three reserved bytes.
            w = 1 + int.from_bytes(data[24:27], "little")
            h = 1 + int.from_bytes(data[27:30], "little")
            return (w, h)
        if chunk == b"VP8 ":
            # Lossy: 3-byte frame tag, the start code 9d 01 2a, then 14-bit
            # width and height (the top two bits of each are a scale factor).
            if data[23:26] != b"\x9d\x01\x2a":
                return None
            w = int.from_bytes(data[26:28], "little") & 0x3FFF
            h = int.from_bytes(data[28:30], "little") & 0x3FFF
            return (w, h) if w and h else None
        if chunk == b"VP8L":
            # Lossless: signature byte 0x2f, then 14 bits width-1, 14 bits
            # height-1 packed little-endian.
            if data[20] != 0x2F:
                return None
            bits = int.from_bytes(data[21:25], "little")
            return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
        return None

    # JPEG: walk the marker segments until a start-of-frame.
    if data[:2] == b"\xff\xd8":
        i = 2
        n = len(data)
        while i + 4 <= n:
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xFF:          # fill byte
                i += 1
                continue
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2                  # standalone markers carry no length
                continue
            if marker in (0xD9, 0xDA):  # end of image / start of scan: no SOF seen
                return None
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if seg_len < 2:
                return None
            if marker in _JPEG_SOF:
                if i + 9 > n:
                    return None         # the frame header was cut off
                h = int.from_bytes(data[i + 5:i + 7], "big")
                w = int.from_bytes(data[i + 7:i + 9], "big")
                return (w, h) if w and h else None
            i += 2 + seg_len
        return None

    return None


def _read_prefix(http: httpx.Client, url: str, limit: int) -> bytes | None:
    """The first `limit` bytes of a URL, via a Range request; None on any failure.

    A host that ignores `Range` sends the whole file; the stream is closed as
    soon as enough has arrived, so the cost is bounded either way.
    """
    try:
        with http.stream("GET", url, headers={"Range": f"bytes=0-{limit - 1}"}) as resp:
            if resp.status_code >= 400:
                return None
            buf = bytearray()
            for chunk in resp.iter_bytes():
                buf += chunk
                if len(buf) >= limit:
                    break
            return bytes(buf[:limit])
    except Exception as exc:  # noqa: BLE001 — "unknown" is the honest answer
        log.info("header read failed for %s: %s", url[:70], type(exc).__name__)
        return None


def image_dims(url: str, client: httpx.Client | None = None,
               timeout_s: float = 10.0) -> tuple[int, int] | None:
    """Width and height of a remote image from its header. Never raises."""
    own = client is None
    http = client or make_client(timeout_s)
    try:
        for limit in (_HEADER_BYTES, _HEADER_BYTES_MAX):
            head = _read_prefix(http, url, limit)
            if head is None:
                return None
            dims = dims_from_header(head)
            if dims:
                return dims
            if len(head) < limit:
                return None             # the whole file was read and still nothing
        return None
    finally:
        if own:
            http.close()


def image_dims_all(urls: list[str], timeout_s: float = 10.0,
                   deadline_s: float = 30.0,
                   client: httpx.Client | None = None
                   ) -> dict[str, tuple[int, int] | None]:
    """`image_dims` for many URLs at once, under one deadline. Never raises."""
    if not urls:
        return {}
    own = client is None
    http = client or make_client(timeout_s)
    out: dict[str, tuple[int, int] | None] = {u: None for u in urls}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            futures = {pool.submit(image_dims, u, http): u for u in urls}
            done, pending = concurrent.futures.wait(futures, timeout=deadline_s)
            for future in done:
                try:
                    out[futures[future]] = future.result()
                except Exception:  # noqa: BLE001
                    pass
            for future in pending:
                future.cancel()
    finally:
        if own:
            http.close()
    return out
