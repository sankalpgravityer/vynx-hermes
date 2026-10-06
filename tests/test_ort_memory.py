"""Cut-out memory (app/imaging/ort_memory.py), 6 Oct 2026: the kernel killed Hermes at 7.7 GB.

No ONNX Runtime memory arena, and a cap on cut-outs at once. Measured on 4 full-size photos
at once: arena on and no cap peaked at 6.2 GB and held 5.7 GB afterwards; arena off with 2
at a time peaked at 2.7 GB, held 0.7 GB, in the same time.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.imaging import cutout, ort_memory  # noqa: E402


def test_the_arena_is_off_unless_asked_for(monkeypatch):
    monkeypatch.delenv("HERMES_ORT_ARENA", raising=False)
    assert ort_memory.session_options().enable_cpu_mem_arena is False
    monkeypatch.setenv("HERMES_ORT_ARENA", "1")
    assert ort_memory.session_options().enable_cpu_mem_arena is True


def test_no_more_than_the_cap_run_at_once():
    running, most = [0], [0]
    lock = threading.Lock()

    def job():
        with ort_memory.slot(2, 10):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1

    threads = [threading.Thread(target=job) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert most[0] == 2


def test_a_wait_past_the_limit_is_refused_as_busy():
    held = threading.Event()
    release = threading.Event()

    def holder():
        with ort_memory.slot(1, 1):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        with pytest.raises(ort_memory.Busy):
            with ort_memory.slot(1, 0.1):
                pass
    finally:
        release.set()
        t.join()


def test_zero_means_no_cap():
    with ort_memory.slot(0, 0) as waited:
        assert waited == 0.0


def test_a_busy_hermes_says_so_instead_of_cutting(monkeypatch):
    """The photo is not cut this time; the server is not killed."""
    monkeypatch.setattr(cutout, "config", lambda pol=None: {"max_concurrent": 1, "max_wait_s": 0.1})
    ran = []
    monkeypatch.setattr(cutout, "_remove_background", lambda *a, **k: ran.append(1) or (b"png", None, "x"))
    held, release = threading.Event(), threading.Event()

    def holder():
        with ort_memory.slot(1, 1):
            held.set()
            release.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        out, err, provider = cutout.remove_background(b"raw")
    finally:
        release.set()
        t.join()
    assert out is None and provider == "none" and err.startswith("Hermes is busy") and not ran
    # ... and with the slot free again, it cuts.
    assert cutout.remove_background(b"raw") == (b"png", None, "x")


def test_the_shipped_policy_caps_at_two():
    cfg = cutout.config()
    assert cfg["max_concurrent"] == 2 and 0 < cfg["max_wait_s"] < 420
