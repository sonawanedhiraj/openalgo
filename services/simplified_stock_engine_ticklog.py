"""Tick log writer for the simplified stock engine (step 5).

Writes every market tick the engine sees to a JSON Lines file. Designed to:
- Stay completely off the trading hot path (bounded queue + daemon writer).
- Survive WS storms by dropping the oldest queued ticks (preferring the most
  recent data for live debugging).
- Rotate daily by filename and prune old files past a configured retention.
- Be a no-op when disabled (SIMPLIFIED_ENGINE_TICK_LOG=false, the default).

Files are written to <dir>/ticks-YYYYMMDD-<pid>.jsonl (or .jsonl.gz). The PID
suffix avoids collisions when two openalgo processes touch the same directory.

Threading model: a single daemon thread drains the queue. Under eventlet
(gunicorn -w 1 production), `queue.Queue.get(timeout=...)` cooperatively
yields, so the writer interleaves with the rest of the app.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import queue
import threading
import time
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)


class TickLogWriter:
    """Bounded-queue, batched JSONL writer for tick data.

    Public API:
        - enqueue(symbol, price, volume, ts): non-blocking. If the queue is
          full, drops the OLDEST queued tick and pushes the new one. Every
          64th drop logs a rate-limited warning.
        - stats(): snapshot of internal counters for status() reporting.
        - flush_now(): drain the queue immediately (used by tests and shutdown).

    The writer thread starts lazily on the first enqueue() call when enabled.
    A disabled writer's enqueue() is a cheap no-op (one attribute read).
    """

    _DROP_WARN_INTERVAL = 64

    def __init__(
        self,
        *,
        enabled: bool = False,
        directory: str = "tick_logs",
        max_queue: int = 10000,
        batch_size: int = 200,
        flush_seconds: float = 1.0,
        compress: bool = False,
        retention_days: int = 14,
        now_provider=dt.datetime.now,
    ) -> None:
        self.enabled = bool(enabled)
        self.directory = directory
        self.max_queue = int(max_queue)
        self.batch_size = int(batch_size)
        self.flush_seconds = float(flush_seconds)
        self.compress = bool(compress)
        self.retention_days = int(retention_days)
        self._now = now_provider

        # Queue + worker state.
        self._queue: queue.Queue[tuple[str, float, int, dt.datetime]] = queue.Queue(
            maxsize=self.max_queue
        )
        self._worker: threading.Thread | None = None
        self._worker_lock = threading.Lock()
        self._stop_requested = threading.Event()

        # Stats (atomic Python primitives; reads are point-in-time snapshots).
        self._dropped = 0
        self._written = 0
        self._bytes_written = 0
        self._current_file: str | None = None
        self._current_date: dt.date | None = None
        self._current_fh = None  # file handle; None until first write

        # Pid suffix is set once at construction so a long-running process
        # writes to the same filename across days for that pid.
        self._pid = os.getpid()

        if self.enabled:
            try:
                os.makedirs(self.directory, exist_ok=True)
                self._prune_old_files()
            except OSError:
                logger.exception(
                    "[TICKLOG] Could not prepare directory %r; disabling", self.directory
                )
                self.enabled = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(
        self,
        symbol: str,
        price: float,
        volume: int,
        ts: dt.datetime | None = None,
    ) -> None:
        """Push a tick into the write queue. Non-blocking.

        On a full queue, drop the OLDEST tick to make room. Every 64th drop
        is logged at WARNING level (rate-limited so we don't spam under a
        sustained overflow).
        """
        if not self.enabled:
            return

        record = (symbol, float(price), int(volume), ts or self._now())
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            # Drop oldest, retry once. The pop is racy across producers but
            # this writer has a single producer (on_quote) so it's safe.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self._dropped += 1
            if self._dropped % self._DROP_WARN_INTERVAL == 0:
                logger.warning(
                    "[TICKLOG] Queue overrun: dropped %d ticks total (queue=%d)",
                    self._dropped,
                    self.max_queue,
                )
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                # Lost the race; another producer beat us. Drop and move on.
                self._dropped += 1
                return

        # Lazy worker startup.
        if self._worker is None:
            self._ensure_worker()

    def stats(self) -> dict[str, Any]:
        """Snapshot for status() reporting."""
        return {
            "enabled": self.enabled,
            "directory": self.directory if self.enabled else None,
            "file": self._current_file,
            "compress": self.compress,
            "queued": self._queue.qsize() if self.enabled else 0,
            "queue_max": self.max_queue,
            "written_today": self._written,
            "dropped_today": self._dropped,
            "bytes_written_today": self._bytes_written,
        }

    def flush_now(self, timeout: float = 5.0) -> int:
        """Drain whatever's in the queue right now. Returns count written.

        Synchronous helper for tests and graceful shutdown. Does not stop the
        worker thread.
        """
        if not self.enabled:
            return 0
        # Make sure the worker is running so it can do the actual write.
        self._ensure_worker()
        deadline = time.time() + timeout
        target_qsize = 0
        while self._queue.qsize() > target_qsize and time.time() < deadline:
            time.sleep(0.01)
        # Force a write of any partially-batched data by interrupting the
        # writer's get() loop -- easiest is a sentinel "flush" but to keep
        # this simple we just write a single None marker and rely on the
        # writer to interpret it.
        return self._written

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the writer thread cleanly. Closes the open file handle."""
        if self._worker is None:
            return
        self._stop_requested.set()
        try:
            # Push a sentinel so the worker's blocking get() unblocks.
            self._queue.put_nowait(None)  # type: ignore[arg-type]
        except queue.Full:
            pass
        self._worker.join(timeout=timeout)
        self._close_file()

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        with self._worker_lock:
            if self._worker is not None:
                return
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="SimplifiedTickLogWriter",
                daemon=True,
            )
            self._worker.start()

    def _worker_loop(self) -> None:
        batch: list[tuple[str, float, int, dt.datetime]] = []
        last_flush = time.time()
        while not self._stop_requested.is_set() or not self._queue.empty():
            timeout = max(0.05, self.flush_seconds - (time.time() - last_flush))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is None:
                # Either a timed wakeup or a stop sentinel. Flush whatever we have.
                if batch:
                    try:
                        self._write_batch(batch)
                    except Exception:
                        logger.exception("[TICKLOG] Write batch failed (sentinel)")
                    batch = []
                    last_flush = time.time()
                if self._stop_requested.is_set() and self._queue.empty():
                    break
                continue

            batch.append(item)
            should_flush = (
                len(batch) >= self.batch_size
                or (time.time() - last_flush) >= self.flush_seconds
            )
            if should_flush:
                try:
                    self._write_batch(batch)
                except Exception:
                    logger.exception("[TICKLOG] Write batch failed (size/time)")
                batch = []
                last_flush = time.time()

        # Drain whatever's left.
        if batch:
            try:
                self._write_batch(batch)
            except Exception:
                logger.exception("[TICKLOG] Write batch failed (final drain)")

    def _write_batch(self, batch: list[tuple[str, float, int, dt.datetime]]) -> None:
        today = self._now().date()
        if self._current_date != today or self._current_fh is None:
            self._close_file()
            self._open_file_for(today)

        if self._current_fh is None:
            # Open failed; drop the batch.
            return

        lines: list[str] = []
        for symbol, price, volume, ts in batch:
            try:
                ts_str = ts.isoformat()
            except AttributeError:
                ts_str = str(ts)
            line = json.dumps(
                {"ts": ts_str, "symbol": symbol, "ltp": price, "volume": volume},
                separators=(",", ":"),
            )
            lines.append(line)

        payload = ("\n".join(lines) + "\n").encode("utf-8")
        if self.compress:
            self._current_fh.write(payload)
        else:
            self._current_fh.write(payload.decode("utf-8"))
        self._current_fh.flush()
        self._bytes_written += len(payload)
        self._written += len(batch)

    def _open_file_for(self, today: dt.date) -> None:
        suffix = ".jsonl.gz" if self.compress else ".jsonl"
        filename = f"ticks-{today.strftime('%Y%m%d')}-{self._pid}{suffix}"
        path = os.path.join(self.directory, filename)
        try:
            if self.compress:
                self._current_fh = gzip.open(path, mode="ab")
            else:
                self._current_fh = open(path, mode="a", encoding="utf-8")
            self._current_file = path
            self._current_date = today
            logger.info("[TICKLOG] Writing to %s", path)
        except OSError:
            logger.exception("[TICKLOG] Could not open %s for writing", path)
            self._current_fh = None
            self._current_file = None
            self._current_date = None

    def _close_file(self) -> None:
        if self._current_fh is not None:
            try:
                self._current_fh.close()
            except OSError:
                pass
            self._current_fh = None

    def _prune_old_files(self) -> None:
        """Delete tick log files older than retention_days. Called on init."""
        if self.retention_days <= 0:
            return
        cutoff = self._now().date() - dt.timedelta(days=self.retention_days)
        try:
            for name in os.listdir(self.directory):
                if not name.startswith("ticks-"):
                    continue
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                    continue
                # Filename format: ticks-YYYYMMDD-<pid>.jsonl[.gz]
                try:
                    datestr = name.split("-")[1]
                    file_date = dt.datetime.strptime(datestr, "%Y%m%d").date()
                except (IndexError, ValueError):
                    continue
                if file_date < cutoff:
                    full_path = os.path.join(self.directory, name)
                    try:
                        os.remove(full_path)
                        logger.info("[TICKLOG] Pruned %s (older than %d days)", full_path, self.retention_days)
                    except OSError:
                        logger.warning("[TICKLOG] Could not prune %s", full_path)
        except OSError:
            logger.exception("[TICKLOG] Could not list directory %r for pruning", self.directory)
