"""ONNX Runtime session options for Hermes' segmenters, and the cap on cut-outs at once (6 Oct 2026).

THE SERVER RAN OUT OF MEMORY. 6 Oct 2026, 05:45 UTC: the kernel killed uvicorn at 7.7 GB
resident on the 7.8 GB box (swap full), and every request in flight died with it — vnyx-api
saw "fetch failed" from the gate and "segmenter unreachable or timing out" from the matte.

Two causes, measured on a 3000x4000 photo:

1. ONNX Runtime's CPU MEMORY ARENA keeps the largest working buffer each session has ever
   needed and never gives it back. One cut-out on each route left the process holding
   1.6 GB -> 3.0 GB -> 4.1 GB, the models themselves being ~0.5 GB of it. Several cut-outs
   at once on the same session grow its arena to their sum. `session_options()` turns the
   arena off: buffers are freed after each run, at a few percent of speed.
   HERMES_ORT_ARENA=1 turns it back on.

2. NOTHING LIMITED HOW MANY RAN AT ONCE. vnyx-api sends a product's photos in parallel
   (backfill-bg-removal.ts, Promise.allSettled), FastAPI runs each in its own thread, and
   each full-size cut-out adds ~0.7-0.9 GB on top. `slot()` caps it (policy
   `imagery.cutout.max_concurrent`); a request that waits longer than `max_wait_s` is
   refused as busy rather than queued past vnyx-api's own timeout (420 s).
"""
from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator


def session_options() -> Any:
    """SessionOptions for a segmenter: no CPU memory arena unless HERMES_ORT_ARENA=1."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    if os.getenv("HERMES_ORT_ARENA", "").strip().lower() not in ("1", "true", "yes", "on"):
        opts.enable_cpu_mem_arena = False
    return opts


_lock = threading.Lock()
_slots: dict[int, threading.BoundedSemaphore] = {}


def _semaphore(n: int) -> threading.BoundedSemaphore:
    with _lock:
        if n not in _slots:
            _slots[n] = threading.BoundedSemaphore(n)
        return _slots[n]


class Busy(Exception):
    """No cut-out slot came free within the wait."""


@contextmanager
def slot(max_concurrent: int, max_wait_s: float) -> Iterator[float]:
    """Hold one of `max_concurrent` cut-out slots; yields how long it waited. 0 = no cap."""
    if max_concurrent <= 0:
        yield 0.0
        return
    sem = _semaphore(int(max_concurrent))
    started = time.perf_counter()
    if not sem.acquire(timeout=max(0.0, float(max_wait_s))):
        raise Busy(f"{max_concurrent} cut-out(s) already running; waited {max_wait_s:.0f}s for a slot")
    try:
        yield time.perf_counter() - started
    finally:
        sem.release()
