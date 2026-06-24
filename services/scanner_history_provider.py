"""In-memory daily/weekly OHLCV cache for the in-house Chartink-equivalent screener.

The scanner evaluates BUY/SELL rules on dozens of symbols at every 5-minute bar
close. Several gates need daily bars (~205 rows) and weekly bars (~22 rows) per
symbol. Hitting DuckDB (``database.historify_db.get_ohlcv``) once per symbol per
tick is wasteful, so this module keeps a daily-refreshed in-memory cache.

Design:
- ``ScannerHistoryProvider`` holds two dicts keyed by symbol: daily and weekly
  ``pandas.DataFrame`` frames (columns: timestamp, open, high, low, close,
  volume, oi — exactly what ``get_ohlcv`` returns).
- ``refresh()`` bulk-reloads all configured symbols and atomically swaps the
  cache dicts under a single ``RLock`` so concurrent scanner reads never see a
  half-built cache.
- ``get_daily``/``get_weekly`` return ``None`` (never raise) when data is
  missing. A symbol not yet in the cache is lazy-loaded on first read.

The 16:00-IST scheduler refresh hook is wired in Task 3; this module only
exposes ``refresh()`` for the scheduler to call.
"""

from __future__ import annotations

import os
import threading
import time

import pandas as pd

from database import historify_db
from utils.logging import get_logger

logger = get_logger(__name__)

# Seconds per calendar day; used to size the DuckDB date-range query.
_SECONDS_PER_DAY = 86_400
# Buffer factor over lookback bars to absorb weekends/holidays (trading days
# are ~5/7 of calendar days, so 1.4 comfortably covers the gap).
_CALENDAR_BUFFER = 1.4


class ScannerHistoryProvider:
    """Thread-safe daily/weekly OHLCV cache wrapping ``historify_db.get_ohlcv``."""

    def __init__(
        self,
        symbols: list[str],
        exchange: str = "NSE",
        daily_lookback_bars: int = 205,
        weekly_lookback_bars: int = 22,
    ):
        self.symbols = [s.upper() for s in symbols]
        self.exchange = exchange.upper()
        self.daily_lookback_bars = int(daily_lookback_bars)
        self.weekly_lookback_bars = int(weekly_lookback_bars)

        self._lock = threading.RLock()
        self._daily: dict[str, pd.DataFrame] = {}
        self._weekly: dict[str, pd.DataFrame] = {}
        self._last_refresh_at: float | None = None

    # ------------------------------------------------------------------ reads

    def get_daily(self, symbol: str) -> pd.DataFrame | None:
        """Return the last N daily bars for ``symbol``, or ``None`` if unavailable."""
        return self._get(symbol, "D", self._daily, self.daily_lookback_bars)

    def get_weekly(self, symbol: str) -> pd.DataFrame | None:
        """Return the last N weekly bars for ``symbol``, or ``None`` if unavailable."""
        return self._get(symbol, "W", self._weekly, self.weekly_lookback_bars)

    def _get(
        self,
        symbol: str,
        interval: str,
        cache: dict[str, pd.DataFrame],
        lookback_bars: int,
    ) -> pd.DataFrame | None:
        sym = symbol.upper()
        with self._lock:
            frame = cache.get(sym)
            if frame is not None and not frame.empty:
                return frame
            # Empty sentinel (pd.DataFrame()) or absent — fall through to lazy-load.
            # An empty sentinel means refresh() ran before backfill wrote DuckDB rows.
            # Re-attempting the fetch lets the provider self-heal once data arrives.

        # Lazy-load a symbol that was not pre-configured / not yet cached.
        logger.info(
            f"ScannerHistoryProvider lazy-loading {interval} bars for {sym} ({self.exchange})"
        )
        frame = self._fetch(sym, interval, lookback_bars)
        with self._lock:
            cache[sym] = frame if frame is not None else pd.DataFrame()
        return frame if frame is not None and not frame.empty else None

    # ---------------------------------------------------------------- refresh

    def refresh(self) -> dict:
        """Bulk-reload every configured symbol from DuckDB and atomically swap.

        Returns:
            ``{'symbols_loaded': int, 'errors': [{'symbol': str, 'error': str}]}``
        """
        new_daily: dict[str, pd.DataFrame] = {}
        new_weekly: dict[str, pd.DataFrame] = {}
        errors: list[dict[str, str]] = []
        loaded = 0

        for sym in self.symbols:
            try:
                daily = self._fetch(sym, "D", self.daily_lookback_bars)
                weekly = self._fetch(sym, "W", self.weekly_lookback_bars)
                new_daily[sym] = daily if daily is not None else pd.DataFrame()
                new_weekly[sym] = weekly if weekly is not None else pd.DataFrame()
                loaded += 1
            except Exception as e:  # one bad symbol must not abort the whole refresh
                logger.exception(f"ScannerHistoryProvider refresh failed for {sym}: {e}")
                errors.append({"symbol": sym, "error": str(e)})

        with self._lock:
            self._daily = new_daily
            self._weekly = new_weekly
            self._last_refresh_at = time.time()

        logger.info(
            f"ScannerHistoryProvider refresh complete: {loaded} symbols loaded, "
            f"{len(errors)} errors"
        )
        return {"symbols_loaded": loaded, "errors": errors}

    # ------------------------------------------------------------------ status

    def get_cache_status(self) -> dict:
        """Debug snapshot for ops endpoints."""
        with self._lock:
            daily_rows = sum(len(f) for f in self._daily.values())
            weekly_rows = sum(len(f) for f in self._weekly.values())
            return {
                "last_refresh_at": self._last_refresh_at,
                "symbol_count": len(self._daily),
                "daily_rows_total": daily_rows,
                "weekly_rows_total": weekly_rows,
            }

    # ----------------------------------------------------------------- private

    def _fetch(self, symbol: str, interval: str, lookback_bars: int) -> pd.DataFrame | None:
        """Fetch the last ``lookback_bars`` rows for one symbol/interval.

        Returns the tail frame, or ``None`` when DuckDB has no data. Errors
        propagate to the caller (``refresh`` records them; ``_get`` swallows
        via the empty-frame fallback).
        """
        # Daily-aggregated intervals (W) are rolled from D server-side, so a
        # weekly lookback still spans ``lookback_bars`` weeks of calendar time.
        days_per_bar = 7 if interval == "W" else 1
        span_days = int(lookback_bars * days_per_bar * _CALENDAR_BUFFER) + 1
        end_ts = int(time.time())
        start_ts = end_ts - span_days * _SECONDS_PER_DAY

        df = historify_db.get_ohlcv(
            symbol=symbol,
            exchange=self.exchange,
            interval=interval,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
        )
        if df is None or df.empty:
            return None
        return df.iloc[-lookback_bars:].reset_index(drop=True)


# --------------------------------------------------------------------- singleton

_default_provider: ScannerHistoryProvider | None = None
_default_lock = threading.Lock()


def get_provider() -> ScannerHistoryProvider:
    """Return the lazily-initialized default provider.

    Symbols come from the ``SCANNER_SYMBOLS`` env var (comma-separated). Tests
    should construct their own ``ScannerHistoryProvider`` instances directly.
    """
    global _default_provider
    if _default_provider is None:
        with _default_lock:
            if _default_provider is None:
                raw = os.getenv("SCANNER_SYMBOLS", "")
                symbols = [s.strip() for s in raw.split(",") if s.strip()]
                exchange = os.getenv("SCANNER_EXCHANGE", "NSE")
                _default_provider = ScannerHistoryProvider(symbols, exchange=exchange)
    return _default_provider


def run_boot_warmup() -> dict | None:
    """Boot-time warm-up entry point (Task 3).

    Gated by ``SCANNER_HISTORY_WARMUP_ENABLED`` (default ``true``). Bulk-loads
    the daily/weekly cache via ``get_provider().refresh()`` so the first scan
    does not pay per-symbol lazy-load latency. Designed to run on a daemon
    thread; never raises — DuckDB/network failures are logged and swallowed so
    boot is unaffected. Returns the refresh result dict, or ``None`` when
    disabled or on failure.
    """
    if os.getenv("SCANNER_HISTORY_WARMUP_ENABLED", "true").lower() != "true":
        logger.info("Scanner history warm-up disabled (SCANNER_HISTORY_WARMUP_ENABLED!=true)")
        return None
    try:
        provider = get_provider()
        logger.info(
            "Scanner history warm-up starting: refreshing %d symbols",
            len(provider.symbols),
        )
        result = provider.refresh()
        logger.info(
            "Scanner history warm-up complete: %d symbols loaded, %d errors",
            result.get("symbols_loaded", 0),
            len(result.get("errors", [])),
        )
        return result
    except Exception as e:  # never let a warm-up failure affect boot
        logger.exception(f"Scanner history warm-up failed: {e}")
        return None
