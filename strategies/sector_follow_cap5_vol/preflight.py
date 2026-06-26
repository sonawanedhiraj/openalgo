"""Strategy-specific preflight for sector_follow_cap5_vol (issue #162 S4).

Why this file exists
--------------------
On 2026-06-26 15:20:00 IST, sector_follow_cap5_vol was flipped sandbox→live
via raw SQL UPDATE on the ``strategy_mode`` table. Two minutes earlier the
strategy's own 15:18 IST smoke check had PASSED with
``aggregator_coverage='30/30', historify_ok=True, broker_session_ok=True``.
At 15:20:01 the strategy logged:

* ``sector_follow index NIFTYAUTO: sector_ret is None (today_close=None)``
* (same for NIFTYFMCG, NIFTYIT, NIFTYMETAL, NIFTYPSUBANK, NIFTYPVTBANK)
* ``sector_follow eval: 0/30 candidates passed gates``
* ``sector_follow entry job placed 0 order(s) [mode=live]``

The smoke check only verified the **30 stock universe**'s aggregator. It did
NOT verify the **8 mapped sector indices**' aggregator — and those were
empty (NSE_INDEX symbols are subscribed for WS ticks but not folded into
the scanner's ``MultiIntervalAggregator``; see issue #161).

This module is the strategy-specific preflight that
``services.strategy_mode_service.flip_mode`` runs before accepting a
sandbox→live flip. It composes with the default gates and **adds the
sector-follow-specific gates that today's 15:20 flip needed**:

1. **Index intraday-aggregator coverage** — every mapped sector index in
   ``sector_index_symbols()`` must have a non-None ``today_close`` from the
   ``intraday_provider``. This is the gate that would have refused today's
   flip.
2. **Stock intraday-aggregator coverage** — at least
   ``SECTOR_FOLLOW_PREFLIGHT_MIN_STOCK_COVERAGE`` (default 0.9) of the
   30-stock universe must have a non-None ``today_close``.
3. **Master contract ready for the broker** — without it, signal symbols
   resolve to nothing.

Sandbox flips bypass everything (the gates exist to protect LIVE).

The default gates from ``services.strategy_preflight.run_default_checks``
(broker session live, no orphan trades, recent DuckDB errors low) are
included by composing via ``PreflightResult.merge``.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from services.strategy_preflight import (
    PreflightCheck,
    PreflightResult,
    run_default_checks,
)
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Minimum fraction of stock universe that must have today's intraday bars
# before LIVE flip is allowed. Default 0.9 (27/30). Lower than 1.0 because
# a single broker-WS hiccup losing one stock should not block the flip.
_DEFAULT_MIN_STOCK_COVERAGE = 0.9


def _min_stock_coverage() -> float:
    raw = os.environ.get(
        "SECTOR_FOLLOW_PREFLIGHT_MIN_STOCK_COVERAGE",
        str(_DEFAULT_MIN_STOCK_COVERAGE),
    )
    try:
        v = float(raw)
        if 0.0 <= v <= 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return _DEFAULT_MIN_STOCK_COVERAGE


def _probe_coverage(symbols: list[str], intraday_provider, as_of: datetime) -> tuple[int, list[str]]:
    """Return (count_with_today_bar, list_of_missing) for ``symbols``.

    A symbol is considered "covered" iff ``intraday_provider(sym, as_of)``
    returns ``(close, _vol)`` with ``close is not None``. Mirrors the existing
    sector_follow smoke-check logic in
    ``services.sector_follow_service.assert_data_pipeline_healthy``.
    """
    have = 0
    missing: list[str] = []
    for sym in symbols:
        try:
            close, _vol = intraday_provider(sym, as_of)
        except Exception:
            close = None
        if close is None:
            missing.append(sym)
        else:
            have += 1
    return have, missing


def check_index_aggregator_coverage(intraday_provider, as_of: datetime) -> PreflightCheck:
    """Gate: every mapped sector index must have a non-None today_close.

    This is the load-bearing gate. Today's 15:20 LIVE flip would have been
    refused by this check — the strategy's smoke check verified STOCK
    coverage (30/30) but did NOT verify INDEX coverage (0/8). Result was
    0 orders in LIVE mode.
    """
    try:
        from services.sector_follow_index_backfill import sector_index_symbols

        indices = sector_index_symbols()
    except Exception as e:
        logger.exception("preflight: sector_index_symbols import failed")
        return PreflightCheck(
            name="sf_index_aggregator_coverage",
            passed=False,
            blocker_message=f"sector_index_symbols import failed: {e!r}",
        )

    if not indices:
        return PreflightCheck(
            name="sf_index_aggregator_coverage",
            passed=False,
            blocker_message="No mapped sector indices found — sector_map.json missing or empty",
        )

    have, missing = _probe_coverage(indices, intraday_provider, as_of)
    if not missing:
        return PreflightCheck(name="sf_index_aggregator_coverage", passed=True)
    return PreflightCheck(
        name="sf_index_aggregator_coverage",
        passed=False,
        blocker_message=(
            f"Index intraday aggregator empty for {len(missing)}/{len(indices)}: "
            f"{missing} — sector_ret will be None → 0 candidates at 15:20. "
            "Likely cause: NSE_INDEX symbols not in scanner aggregator (issue #161). "
            "Wait for the aggregator to accumulate ticks or fix the subscription."
        ),
    )


def check_stock_aggregator_coverage(
    intraday_provider, as_of: datetime, universe: list[str]
) -> PreflightCheck:
    """Gate: at least the configured fraction of the stock universe must have
    today's intraday bars."""
    if not universe:
        return PreflightCheck(
            name="sf_stock_aggregator_coverage",
            passed=False,
            blocker_message="Stock universe is empty — config_snapshot.json missing or unloaded",
        )

    have, missing = _probe_coverage(universe, intraday_provider, as_of)
    frac = have / len(universe)
    threshold = _min_stock_coverage()
    if frac >= threshold:
        return PreflightCheck(
            name="sf_stock_aggregator_coverage",
            passed=True,
            warning_message=(
                f"Stock intraday coverage {have}/{len(universe)} missing {missing}"
                if missing
                else None
            ),
        )
    return PreflightCheck(
        name="sf_stock_aggregator_coverage",
        passed=False,
        blocker_message=(
            f"Stock intraday aggregator coverage {have}/{len(universe)} "
            f"({frac:.0%} < {threshold:.0%}). Missing: {missing}. "
            "Wait for the aggregator to fill or check broker WS health."
        ),
    )


def check_master_contract_ready(broker: str = "zerodha") -> PreflightCheck:
    """Gate: broker's master contract must be ``is_ready=True``.

    Without it, signal symbol→token resolution fails — `get_history` and
    order placement both 404 on every symbol.
    """
    try:
        from database.master_contract_status_db import get_status

        status = get_status(broker)
    except Exception as e:
        logger.exception("preflight: master_contract_status.get_status raised")
        return PreflightCheck(
            name="sf_master_contract_ready",
            passed=False,
            blocker_message=f"master_contract status query failed: {e!r}",
        )

    if not status:
        return PreflightCheck(
            name="sf_master_contract_ready",
            passed=False,
            blocker_message=f"No master_contract status row for {broker}",
        )
    if status.get("is_ready"):
        return PreflightCheck(name="sf_master_contract_ready", passed=True)
    return PreflightCheck(
        name="sf_master_contract_ready",
        passed=False,
        blocker_message=(
            f"Master contract for {broker} not ready (status={status.get('status')!r}). "
            "Wait for download to complete."
        ),
    )


def _resolve_provider_and_universe() -> tuple[Any, list[str]]:
    """Pull the intraday provider + stock universe from the live sector_follow
    service singleton. Falls back to the production provider + an empty
    universe (which blocks at the stock-coverage gate) if the service hasn't
    been initialised yet (e.g. during a very early boot or in tests)."""
    try:
        from services.sector_follow_service import get_service

        service = get_service()
        if service is not None:
            return service._intraday_provider, list(service.config.universe)
    except Exception:
        logger.exception("preflight: get_sector_follow_service raised")

    # Fallback path — production provider but no universe means the stock-
    # coverage gate will block, giving the operator a clear "universe empty"
    # message rather than a silent fail-open.
    try:
        from services.sector_follow_service import production_intraday_provider

        return production_intraday_provider, []
    except Exception:
        logger.exception("preflight: production_intraday_provider import failed")
        # Last resort — return a None-yielding provider so the coverage gate
        # blocks with a clear message rather than crashing.
        return lambda sym, as_of: (None, None), []


def check_can_go_live(target_mode: str) -> PreflightResult:
    """The entry point ``run_preflight`` discovers and calls.

    Composes the system-wide default gates with the four sector_follow-specific
    gates. Sandbox flips bypass entirely.
    """
    strategy_name = "sector_follow_cap5_vol"

    if target_mode == "sandbox":
        return PreflightResult.allow(
            snapshot={
                "preflight_path": "strategies.sector_follow_cap5_vol.preflight",
                "strategy_name": strategy_name,
                "target_mode": target_mode,
                "reason": "sandbox-always-allowed",
            }
        )

    if target_mode != "live":
        return PreflightResult(
            can_flip=False,
            blockers=[f"target_mode must be 'live' or 'sandbox', got {target_mode!r}"],
            warnings=[],
            snapshot={"preflight_path": "strategies.sector_follow_cap5_vol.preflight"},
        )

    # 1. Run the system-wide default gates first.
    default_result = run_default_checks(strategy_name)

    # 2. Add the sector_follow-specific gates.
    as_of = datetime.now(_IST)
    intraday_provider, universe = _resolve_provider_and_universe()

    custom_checks = [
        check_index_aggregator_coverage(intraday_provider, as_of),
        check_stock_aggregator_coverage(intraday_provider, as_of, universe),
        check_master_contract_ready("zerodha"),
    ]
    custom_snapshot = {
        "custom_checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "blocker": c.blocker_message,
                "warning": c.warning_message,
            }
            for c in custom_checks
        ],
        "as_of_ist": as_of.isoformat(),
        "universe_size": len(universe),
    }
    custom_result = PreflightResult.from_checks(custom_checks, snapshot=custom_snapshot)

    # 3. Compose.
    combined = default_result.merge(custom_result)
    combined.snapshot.setdefault(
        "preflight_path", "strategies.sector_follow_cap5_vol.preflight"
    )
    combined.snapshot.setdefault("strategy_name", strategy_name)
    combined.snapshot.setdefault("target_mode", target_mode)
    return combined
