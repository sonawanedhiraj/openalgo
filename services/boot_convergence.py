"""Cross-scheduler serialisation for the boot-time historify backfill burst.

Why this exists
---------------
At boot, four backfill jobs fire in parallel via daemon threads (verified in
``log/openalgo_2026-06-26.log`` 09:11:05-09:11:08):

* ``sector_follow_index_backfill`` — 10 indices
* ``sector_follow_stock_backfill`` — 30 stocks
* ``scanner_universe_backfill`` (1m) — 216 symbols
* ``scanner_universe_backfill`` (D) — 216 symbols

= 472 symbol-downloads against a single DuckDB file (``db/historify.duckdb``).
Earlier PRs serialised in-process writes per call (PR #125 ``@_synchronized_write``)
and broadened the read-only fallback exceptions (PR #118 / #142), but four jobs
that all *want* the file still queue and produce 100s of retry log lines.

Parallelism gains nothing here — Zerodha's broker API rate-limits at 3 req/s, so
the upstream bottleneck dominates the wall-clock regardless of how many local
threads are pushing.

What this gives the schedulers
------------------------------
A single module-level :class:`threading.Lock` (deliberately not RLock — we want
strict cross-scheduler ordering, not reentrancy) plus a context-manager helper
the boot workers can wrap their convergence calls in::

    from services.boot_convergence import boot_convergence_lock

    with boot_convergence_lock(name="sector_follow"):
        run_boot_backfill_checks()

The periodic refresh loop (15:30..17:00 IST window, different cron offsets) is
not affected — it never produces the burst pattern and runs through this
coordinator only at boot.

Gating
------
``BOOT_BACKFILL_SERIALIZE_ENABLED`` (default ``true``). When ``false`` the
context manager is a no-op and the legacy parallel behaviour is preserved (for
diagnostics or to A/B-compare boot times).
"""

from __future__ import annotations

import contextlib
import os
import threading
import time
from collections.abc import Iterator

from utils.logging import get_logger

logger = get_logger(__name__)

_boot_lock = threading.Lock()


def _enabled() -> bool:
    return os.environ.get("BOOT_BACKFILL_SERIALIZE_ENABLED", "true").lower() == "true"


@contextlib.contextmanager
def boot_convergence_lock(name: str) -> Iterator[None]:
    """Serialise boot-time DuckDB-writing convergence work across schedulers.

    Acquires the shared boot lock for the duration of the ``with`` block, so
    sibling schedulers wait their turn rather than colliding on
    ``historify.duckdb``. Acquire + release timings are logged with ``name`` so
    a slow holder is identifiable in the text log.

    When disabled via env flag, the context manager is a no-op.

    Args:
        name: Short identifier for the holder (e.g. ``"sector_follow"``,
            ``"scanner"``). Surfaces in the acquire/release log lines.
    """
    if not _enabled():
        logger.info("boot_convergence: serialisation disabled — %s runs unserialised", name)
        yield
        return

    queued_at = time.monotonic()
    logger.info("boot_convergence: %s queued for lock", name)
    with _boot_lock:
        waited = time.monotonic() - queued_at
        logger.info("boot_convergence: %s acquired lock (waited %.2fs)", name, waited)
        held_at = time.monotonic()
        try:
            yield
        finally:
            held = time.monotonic() - held_at
            logger.info("boot_convergence: %s released lock (held %.2fs)", name, held)


def _reset_for_tests() -> None:
    """Replace the module lock with a fresh one — tests only.

    A Lock left in a held state by a previous test would deadlock subsequent
    tests in the same process. Tests that exercise the holder/queuer pattern
    call this in their teardown.
    """
    global _boot_lock
    _boot_lock = threading.Lock()
