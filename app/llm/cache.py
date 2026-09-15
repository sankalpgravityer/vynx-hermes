"""Vision response cache — the same picture is never sent to the model twice.

WHAT IT IS FOR. Every vision call in Hermes is a question about images that do
not change: a care label, a render, a cut-out. The answer is a function of the
picture and the prompt, so asking again is paid twice for the same JSON. Within
one run Hermes already reads each image once; the win is on the re-runs — a
`--retry` after an outage, a re-verification after a repair, a calibration
sweep over products already judged, a developer iterating on a rule with the
gate switched on. The auditor's `gemini_cache.py` measured that at most of its
runtime on a second pass, and the gate here is fetch-bound at 8–14 s a product.

WHAT THE KEY IS. A namespace (which question), the prompt and schema text (so
editing a prompt invalidates every answer given to the old one — there is no
version number to forget to bump), the model, and the images: URLs as given,
or a digest of the bytes when the caller only has bytes. URLs are normalised
lightly — whitespace and the `#fragment` go, THE QUERY STAYS. Shopify bumps
`?v=` when the file behind a URL changes, and the auditor stripping it on
2026-07-29 served stale answers for new photographs. vnyx's own R2 URLs carry a
timestamp in the filename, so for them the point is moot, but the rule costs
nothing and is the one that was learnt the hard way.

WHAT IS NEVER CACHED. A failure — None, an empty answer, a provider error — is
not an answer about the picture and must not be remembered as one; the callers
only `put` a parsed dict. Nor is an answer computed from fewer images than were
asked for: when a fetch dropped one, the model judged a different question,
and the next run may well fetch all of them.

WHERE IT LIVES. Redis when `llm.cache.redis_url` (or HERMES_VISION_CACHE_REDIS)
is set — the Celery Redis is right there, pick a spare database — otherwise one
JSON file per key under `llm.cache.dir`, written atomically so parallel workers
(`--workers`) cannot read a half-written file. A Redis that stops answering
demotes the process to files for its lifetime and says so once; a cache must
never be the reason a verification fails.

Process-local counters (`snapshot()`) are how the CLI reports hits and misses;
`HERMES_VISION_CACHE=0` switches the whole thing off for one run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger("hermes.vision-cache")

# Bumped only when the STORED SHAPE changes, never for prompt edits — the prompt
# text is part of every key already.
KEY_VERSION = "1"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "ttl_hours": 168,            # a week: long enough for a retry cycle, short
                                 # enough that a re-shot label under a reused
                                 # URL cannot haunt a product for a season
    "dir": ".cache/vision",
    "redis_url": None,
}

_OFF = {"0", "off", "false", "no"}


def config(pol: dict[str, Any] | None) -> dict[str, Any]:
    """Policy block with defaults, then the environment on top.

    The environment is the operator's switch for ONE process — a test run, a
    one-off script — without editing the policy the worker reads. It can turn
    the cache off, point it elsewhere, or at a Redis; it cannot force it on
    over a policy that says off, because the policy is the deployment's word.
    """
    cfg = {**DEFAULTS, **(((pol or {}).get("llm") or {}).get("cache") or {})}
    env = os.getenv("HERMES_VISION_CACHE")
    if env is not None and env.strip().lower() in _OFF:
        cfg["enabled"] = False
    if os.getenv("HERMES_VISION_CACHE_DIR"):
        cfg["dir"] = os.environ["HERMES_VISION_CACHE_DIR"]
    if os.getenv("HERMES_VISION_CACHE_REDIS"):
        cfg["redis_url"] = os.environ["HERMES_VISION_CACHE_REDIS"]
    return cfg


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #
def normalise_url(url: str) -> str:
    """Whitespace and fragment off; scheme, host, path AND query kept as-is."""
    u = str(url or "").strip()
    if "#" in u:
        u = u.split("#", 1)[0]
    return u


def key(namespace: str, *, urls: Iterable[str] = (), blobs: Iterable[bytes] = (),
        text: Iterable[Any] = ()) -> str:
    """`namespace:<hash>` — stable across processes and machines.

    Order matters and is kept: the images go to the model in this order and a
    prompt that says "the first image is the label" is a different question
    when they are swapped.
    """
    h = hashlib.sha256()
    h.update(KEY_VERSION.encode())
    h.update(b"\x00" + namespace.encode())
    for u in urls:
        h.update(b"\x01" + normalise_url(u).encode())
    for b in blobs:
        h.update(b"\x02" + hashlib.sha256(b).digest())
    for t in text:
        s = t if isinstance(t, str) else json.dumps(t, sort_keys=True, default=str)
        h.update(b"\x03" + s.encode())
    return f"{namespace}:{h.hexdigest()[:32]}"


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #
class _FileBackend:
    name = "file"

    def __init__(self, root: str | Path, ttl_s: float) -> None:
        self.root = Path(root)
        self.ttl_s = ttl_s

    def _path(self, k: str) -> Path:
        ns, digest = k.split(":", 1)
        return self.root / ns / f"{digest}.json"

    def get(self, k: str) -> dict[str, Any] | None:
        p = self._path(k)
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            # A torn or foreign file is a miss, and removed so it stays one
            # rather than costing a parse on every read.
            log.warning("vision cache: unreadable entry %s (%s)", p.name, exc)
            _unlink(p)
            return None
        if time.time() - float(doc.get("at") or 0) > self.ttl_s:
            _unlink(p)
            return None
        value = doc.get("value")
        return value if isinstance(value, dict) else None

    def put(self, k: str, value: dict[str, Any]) -> None:
        p = self._path(k)
        p.parent.mkdir(parents=True, exist_ok=True)
        doc = {"at": time.time(), "key": k, "value": value}
        # Write-then-rename: a reader sees the old file or the new one, never
        # a prefix of the new one. os.replace is atomic on both POSIX and NTFS.
        fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, default=str)
            os.replace(tmp, p)
        except BaseException:
            _unlink(Path(tmp))
            raise


class _RedisBackend:
    name = "redis"

    def __init__(self, url: str, ttl_s: float) -> None:
        import redis  # the Celery extra ships it; imported here so the file

        # backend needs nothing installed
        self.client = redis.Redis.from_url(url, socket_timeout=2.0,
                                           socket_connect_timeout=2.0)
        self.ttl_s = int(ttl_s)

    @staticmethod
    def _rkey(k: str) -> str:
        return f"hermes:vision:{KEY_VERSION}:{k}"

    def get(self, k: str) -> dict[str, Any] | None:
        raw = self.client.get(self._rkey(k))
        if raw is None:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None

    def put(self, k: str, value: dict[str, Any]) -> None:
        self.client.setex(self._rkey(k), self.ttl_s, json.dumps(value, default=str))


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# The process-wide front
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_backend: Any = None
_backend_cfg: tuple[Any, ...] | None = None
_stats: dict[str, Any] = {"hits": 0, "misses": 0, "puts": 0, "errors": 0,
                          "backend": None}


def _select(cfg: dict[str, Any]) -> Any:
    """The backend for this configuration, built once and reused.

    Rebuilt when the effective configuration changes (tests do this; a long
    worker never does), and demoted to files for the rest of the process the
    first time Redis fails — see the module docstring.
    """
    global _backend, _backend_cfg
    ttl_s = float(cfg["ttl_hours"]) * 3600.0
    sig = (cfg.get("redis_url"), str(cfg["dir"]), ttl_s)
    with _lock:
        if _backend is not None and _backend_cfg == sig:
            return _backend
        backend: Any = None
        if cfg.get("redis_url"):
            try:
                backend = _RedisBackend(str(cfg["redis_url"]), ttl_s)
                backend.client.ping()
            except Exception as exc:  # noqa: BLE001 — a cache never fails a run
                log.warning("vision cache: redis unavailable (%s) — using files under %s",
                            str(exc)[:120], cfg["dir"])
                backend = None
        if backend is None:
            backend = _FileBackend(cfg["dir"], ttl_s)
        _backend, _backend_cfg = backend, sig
        _stats["backend"] = backend.name
        return backend


def _demote(cfg: dict[str, Any], exc: Exception) -> Any:
    """Redis misbehaved mid-run: files from here on, said once."""
    global _backend, _backend_cfg
    _stats["errors"] += 1
    log.warning("vision cache: redis error (%s) — files from now on", str(exc)[:120])
    with _lock:
        _backend = _FileBackend(cfg["dir"], float(cfg["ttl_hours"]) * 3600.0)
        _backend_cfg = (cfg.get("redis_url"), str(cfg["dir"]), float(cfg["ttl_hours"]) * 3600.0)
        _stats["backend"] = _backend.name
    return _backend


def get(k: str, pol: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The remembered answer, or None. Never raises."""
    cfg = config(pol)
    if not cfg["enabled"]:
        return None
    backend = _select(cfg)
    try:
        value = backend.get(k)
    except Exception as exc:  # noqa: BLE001
        if backend.name == "redis":
            backend = _demote(cfg, exc)
            try:
                value = backend.get(k)
            except Exception:  # noqa: BLE001
                value = None
        else:
            _stats["errors"] += 1
            log.warning("vision cache: read failed for %s (%s)", k, str(exc)[:120])
            value = None
    if value is None:
        _stats["misses"] += 1
        return None
    _stats["hits"] += 1
    log.debug("vision cache hit %s", k)
    return value


def put(k: str, value: Any, pol: dict[str, Any] | None = None) -> bool:
    """Remember an ANSWER. Refuses None, non-dicts and empty dicts — a failure
    remembered is a failure repeated. Returns whether it was stored."""
    if not isinstance(value, dict) or not value:
        return False
    cfg = config(pol)
    if not cfg["enabled"]:
        return False
    backend = _select(cfg)
    try:
        backend.put(k, value)
    except Exception as exc:  # noqa: BLE001
        if backend.name == "redis":
            backend = _demote(cfg, exc)
            try:
                backend.put(k, value)
            except Exception:  # noqa: BLE001
                return False
        else:
            _stats["errors"] += 1
            log.warning("vision cache: write failed for %s (%s)", k, str(exc)[:120])
            return False
    _stats["puts"] += 1
    return True


def through(k: str, compute: Callable[[], dict[str, Any] | None], *,
            pol: dict[str, Any] | None = None,
            complete: Callable[[], bool] | None = None,
            ) -> tuple[dict[str, Any] | None, bool]:
    """get → compute → put, in one call. Returns `(answer, hit)`.

    `complete` is asked AFTER compute and before put: the caller's word that the
    model saw everything it was meant to see (every image fetched). False means
    the answer is used this once and not remembered.
    """
    cached = get(k, pol)
    if cached is not None:
        return cached, True
    value = compute()
    if value and (complete is None or complete()):
        put(k, value, pol)
    return value, False


def snapshot() -> dict[str, Any]:
    return dict(_stats)


def summary() -> str | None:
    """One line for a CLI footer, or None when the cache was never consulted."""
    s = _stats
    if not (s["hits"] or s["misses"]):
        return None
    total = s["hits"] + s["misses"]
    line = (f'vision cache: {s["hits"]} hit{"" if s["hits"] == 1 else "s"} · '
            f'{s["misses"]} miss{"" if s["misses"] == 1 else "es"} '
            f'({s["hits"] * 100 // total}%) · {s["backend"] or "off"}')
    if s["errors"]:
        line += f' · {s["errors"]} error{"" if s["errors"] == 1 else "s"}'
    return line


def reset() -> None:
    """Forget the backend and the counters. For tests and for `--no-cache`."""
    global _backend, _backend_cfg
    with _lock:
        _backend, _backend_cfg = None, None
        for k in ("hits", "misses", "puts", "errors"):
            _stats[k] = 0
        _stats["backend"] = None
