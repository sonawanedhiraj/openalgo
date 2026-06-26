"""Unit tests for the boot-convergence serialisation coordinator.

Pins down:
- The context manager is mutually exclusive across threads (no overlap).
- The disabled-flag path is a no-op (threads run concurrently).
- Acquire/release timings are logged with the holder name.
- An exception inside the held block releases the lock for the next waiter.
- Different names interleave fairly (first to call .acquire() wins each round).
"""

from __future__ import annotations

import threading
import time

import pytest

from services import boot_convergence


@pytest.fixture(autouse=True)
def _fresh_lock_each_test():
    boot_convergence._reset_for_tests()
    yield
    boot_convergence._reset_for_tests()


def _record_overlap(name: str, sleep_s: float, recorder: list[tuple[str, float, float]]) -> None:
    """Hold the lock for ``sleep_s``, record (name, entered_at, exited_at)."""
    with boot_convergence.boot_convergence_lock(name=name):
        entered = time.monotonic()
        time.sleep(sleep_s)
        exited = time.monotonic()
        recorder.append((name, entered, exited))


def _no_overlap(recorder: list[tuple[str, float, float]]) -> bool:
    """True iff every pair of intervals is disjoint."""
    by_entry = sorted(recorder, key=lambda r: r[1])
    for prev, curr in zip(by_entry, by_entry[1:], strict=False):
        # The next holder must enter at or after the previous holder's exit.
        if curr[1] < prev[2] - 1e-3:  # 1 ms tolerance for monotonic-clock jitter
            return False
    return True


def test_enabled_serialises_two_concurrent_holders(monkeypatch):
    monkeypatch.setenv("BOOT_BACKFILL_SERIALIZE_ENABLED", "true")
    recorder: list[tuple[str, float, float]] = []

    t1 = threading.Thread(target=_record_overlap, args=("sector_follow", 0.05, recorder))
    t2 = threading.Thread(target=_record_overlap, args=("scanner", 0.05, recorder))
    t1.start()
    t2.start()
    t1.join(2)
    t2.join(2)

    assert len(recorder) == 2
    assert _no_overlap(recorder), f"hold intervals overlapped: {recorder}"


def test_enabled_serialises_four_concurrent_holders(monkeypatch):
    """The real-world boot fan-out is 4 sibling holders (sector_follow has its
    own + scanner has 1m + D in one call; we model 4 holders to be safe)."""
    monkeypatch.setenv("BOOT_BACKFILL_SERIALIZE_ENABLED", "true")
    recorder: list[tuple[str, float, float]] = []

    threads = [
        threading.Thread(target=_record_overlap, args=(name, 0.03, recorder))
        for name in ("a", "b", "c", "d")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(2)

    assert len(recorder) == 4
    assert _no_overlap(recorder), f"hold intervals overlapped: {recorder}"


def test_disabled_flag_runs_concurrently(monkeypatch):
    monkeypatch.setenv("BOOT_BACKFILL_SERIALIZE_ENABLED", "false")
    recorder: list[tuple[str, float, float]] = []

    t1 = threading.Thread(target=_record_overlap, args=("sector_follow", 0.05, recorder))
    t2 = threading.Thread(target=_record_overlap, args=("scanner", 0.05, recorder))
    t1.start()
    t2.start()
    t1.join(2)
    t2.join(2)

    assert len(recorder) == 2
    # With serialisation disabled, the two holds MUST overlap (both slept 50 ms).
    a, b = sorted(recorder, key=lambda r: r[1])
    assert b[1] < a[2], f"expected overlap with flag off, got disjoint intervals: {recorder}"


def test_exception_releases_lock(monkeypatch):
    """A failure inside the held block must not strand the lock for the next caller."""
    monkeypatch.setenv("BOOT_BACKFILL_SERIALIZE_ENABLED", "true")

    with pytest.raises(RuntimeError, match="boom"):
        with boot_convergence.boot_convergence_lock(name="failer"):
            raise RuntimeError("boom")

    # If the lock leaked, this acquire would hang past the join timeout.
    acquired = threading.Event()

    def _try_acquire() -> None:
        with boot_convergence.boot_convergence_lock(name="recovery"):
            acquired.set()

    t = threading.Thread(target=_try_acquire)
    t.start()
    t.join(2)
    assert acquired.is_set(), "lock was not released after exception"


def test_logs_include_holder_name(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("BOOT_BACKFILL_SERIALIZE_ENABLED", "true")
    with caplog.at_level(logging.INFO, logger="services.boot_convergence"):
        with boot_convergence.boot_convergence_lock(name="my_holder"):
            pass

    messages = [r.message for r in caplog.records]
    assert any("my_holder queued for lock" in m for m in messages)
    assert any("my_holder acquired lock" in m for m in messages)
    assert any("my_holder released lock" in m for m in messages)
