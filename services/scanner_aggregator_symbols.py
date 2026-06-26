"""Compute the full set of symbols the scanner aggregator must track (issue #161).

Why this exists
---------------
On 2026-06-26 15:20 IST sector_follow_cap5_vol was flipped to LIVE. The
strategy's own smoke check at 15:18 had PASSED (30/30 stock coverage). Two
minutes later all 8 mapped sector indices returned ``today_close=None``;
the strategy emitted 0 orders silently.

Root cause: ``services.scanner_service.ScannerService`` initialises
``MultiIntervalAggregator(symbols=SCANNER_SYMBOLS)``. Sector_follow's
mapped indices (NIFTYAUTO, NIFTYFMCG, NIFTYIT, NIFTYMETAL, NIFTYPSUBANK,
NIFTYPVTBANK + NIFTY + BANKNIFTY) are NOT in SCANNER_SYMBOLS — they live
in REGIME_SECTOR_SYMBOLS + sector_follow's ``sector_map.json``. The WS
adapter subscribes them (so ticks flow over ZMQ), but the aggregator's
``on_tick`` silently drops any symbol not in its builder dict. Result: no
intraday bars for the indices → ``sector_ret=None`` → 0 trades in LIVE.

The minimal fix is at boot time: union every source of symbols a
downstream consumer relies on and pass the combined set to ScannerService.
The aggregator then builds bars for all of them and the WS ticks (already
arriving) accumulate cleanly.

Sources unioned
---------------
* ``SCANNER_SYMBOLS`` env (the in-house scanner's F&O universe; existing)
* ``REGIME_SECTOR_SYMBOLS`` env (regime-classification indices; existing
  WS-subscribed via ``regime_pre_subscriber`` but never in the aggregator)
* ``sector_index_symbols()`` (sector_follow's mapped indices from
  ``sector_map.json``)

The function is **read-only** and pure: it doesn't subscribe anything, doesn't
touch DBs, doesn't mutate global state. Boot wiring calls it once to compute
the constructor symbols for ScannerService.

Each source is best-effort: a missing/unreadable source contributes its
empty set and an INFO log; the union of what *was* readable still gets
through. A logger.exception fires only if every source failed (true outage).
"""

from __future__ import annotations

import os

from utils.logging import get_logger

logger = get_logger(__name__)


def _scanner_symbols() -> list[str]:
    raw = os.environ.get("SCANNER_SYMBOLS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _regime_symbols() -> list[str]:
    raw = os.environ.get("REGIME_SECTOR_SYMBOLS", "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _sector_follow_indices() -> list[str]:
    """The 8 mapped sector indices from sector_follow_cap5_vol/sector_map.json.

    Imported lazily so a missing sector_map (e.g. an early-boot smoke test)
    doesn't break the unrelated scanner construction — falls back to [].
    """
    try:
        from services.sector_follow_index_backfill import sector_index_symbols

        return list(sector_index_symbols())
    except Exception:
        logger.exception(
            "scanner_aggregator_symbols: sector_index_symbols failed — "
            "sector_follow indices will be missing from the aggregator"
        )
        return []


def compute_aggregator_symbols() -> list[str]:
    """Return the de-duplicated, sorted union of every symbol source the
    scanner aggregator must track.

    Called by ``app.py`` boot wiring as ``ScannerService(symbols=...)``.
    Logs a structured breakdown so the operator can verify, in the boot log,
    that every required strategy's index set made it into the aggregator.

    Returns an empty list when every source is empty (the existing
    SCANNER_ENABLED=true + SCANNER_SYMBOLS empty path stays exactly as
    today — caller still logs the "scanner will idle" warning).
    """
    scanner = _scanner_symbols()
    regime = _regime_symbols()
    sf_idx = _sector_follow_indices()

    combined = sorted({s.upper() for s in (scanner + regime + sf_idx) if s})
    # Per-source counts let the operator confirm at a glance:
    #   "scanner aggregator universe: 232 symbols
    #     (SCANNER_SYMBOLS=216 + REGIME_SECTOR_SYMBOLS=10 + sector_follow=8;
    #      union=232, dedup-drop=2)"
    total_input = len(scanner) + len(regime) + len(sf_idx)
    dedup_drop = total_input - len(combined)
    logger.info(
        "scanner aggregator universe: %d symbols "
        "(SCANNER_SYMBOLS=%d + REGIME_SECTOR_SYMBOLS=%d + sector_follow=%d; "
        "union=%d, dedup-drop=%d)",
        len(combined),
        len(scanner),
        len(regime),
        len(sf_idx),
        len(combined),
        dedup_drop,
    )
    return combined
