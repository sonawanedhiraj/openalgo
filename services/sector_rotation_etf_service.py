"""
Sector Rotation ETF Strategy — signal computation.

Spec: SECTOR_ROTATION_DEPLOYMENT_PLAN_2026-06-06.md
Backtest evidence: outputs/backtest_round26_etf_combined_2026-06-06/

This module ONLY computes signals and recommended orders.
It does NOT place orders. It does NOT subscribe to live feeds.
All database access is strictly read-only.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timezone

import duckdb
import numpy as np

from utils.logging import get_logger

logger = get_logger(__name__)

# ~252 trading days/year — annualization factor for daily realized vol.
TRADING_DAYS_PER_YEAR = 252


@dataclass
class SectorRotationConfig:
    """Strategy configuration. Mirrors config_snapshot.json (scaffold defaults)."""

    universe: list[str]
    momentum_lookback_days: int = 126  # ~6 months trading days
    lowvol_lookback_days: int = 60
    momentum_top_n: int = 3
    lowvol_bottom_n: int = 3
    weight_method: str = "risk_parity_inverse_vol"
    capital_inr: float = 300000.0
    exchange: str = "NSE"


@dataclass
class RebalanceOrder:
    """A single recommended order. Emitted for review — never auto-placed."""

    symbol: str
    exchange: str
    side: str  # "BUY" or "SELL"
    quantity: int
    notional_inr: float
    reason: str  # "momentum", "lowvol", "both", "exit"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "side": self.side,
            "quantity": self.quantity,
            "notional_inr": round(self.notional_inr, 2),
            "reason": self.reason,
        }


def load_daily_closes(
    symbols: list[str],
    end_date: date,
    lookback_days: int,
    db_path: str = "db/historify.duckdb",
) -> dict[str, list[tuple[date, float]]]:
    """Load (date, close) pairs for each symbol ending on end_date.

    Reads daily (`interval='D'`) bars from the historify DuckDB read-only.
    The `timestamp` column is epoch seconds at UTC midnight per trading day, so
    the UTC date equals the trading date (see strategy LEARNINGS.md).

    Args:
        symbols: OpenAlgo symbols to load.
        end_date: Inclusive upper bound on the trading date.
        lookback_days: Keep at most this many of the most recent bars per symbol.
        db_path: Path to the historify DuckDB file.

    Returns:
        dict mapping symbol -> list of (date, close) ordered ascending by date,
        truncated to the last ``lookback_days`` bars. Symbols with no data are
        omitted.
    """
    if not symbols:
        return {}

    result: dict[str, list[tuple[date, float]]] = {}
    con = duckdb.connect(db_path, read_only=True)
    try:
        placeholders = ", ".join(["?"] * len(symbols))
        rows = con.execute(
            f"""
            SELECT symbol, timestamp, close
            FROM market_data
            WHERE symbol IN ({placeholders})
              AND interval = 'D'
            ORDER BY symbol, timestamp ASC
            """,
            symbols,
        ).fetchall()
    finally:
        con.close()

    for symbol, ts, close in rows:
        bar_date = datetime.fromtimestamp(ts, tz=UTC).date()
        if bar_date > end_date:
            continue
        result.setdefault(symbol, []).append((bar_date, float(close)))

    # Truncate each series to the most recent ``lookback_days`` bars.
    for symbol in list(result.keys()):
        series = result[symbol]
        if lookback_days > 0:
            result[symbol] = series[-lookback_days:]
    return result


def compute_momentum_returns(
    closes: dict[str, list[tuple[date, float]]], lookback_days: int
) -> dict[str, float]:
    """Compute trailing total return over ``lookback_days`` bars per symbol.

    Return = last_close / close_lookback_days_ago - 1. Symbols with fewer than
    ``lookback_days + 1`` bars are skipped (insufficient history).
    """
    out: dict[str, float] = {}
    for symbol, series in closes.items():
        if len(series) < lookback_days + 1:
            logger.debug(
                "momentum: skipping %s (have %d bars, need %d)",
                symbol,
                len(series),
                lookback_days + 1,
            )
            continue
        start_close = series[-(lookback_days + 1)][1]
        end_close = series[-1][1]
        if start_close <= 0:
            continue
        out[symbol] = end_close / start_close - 1.0
    return out


def compute_realized_vol(
    closes: dict[str, list[tuple[date, float]]], lookback_days: int
) -> dict[str, float]:
    """Annualized std-dev of daily log returns over the last ``lookback_days`` bars.

    Uses the most recent ``lookback_days`` log returns (requires
    ``lookback_days + 1`` closes). Annualized by sqrt(252).
    """
    out: dict[str, float] = {}
    for symbol, series in closes.items():
        if len(series) < lookback_days + 1:
            logger.debug(
                "vol: skipping %s (have %d bars, need %d)",
                symbol,
                len(series),
                lookback_days + 1,
            )
            continue
        prices = np.array([c for _, c in series[-(lookback_days + 1) :]], dtype=float)
        if np.any(prices <= 0):
            continue
        log_returns = np.diff(np.log(prices))
        daily_std = float(np.std(log_returns, ddof=1))
        out[symbol] = daily_std * np.sqrt(TRADING_DAYS_PER_YEAR)
    return out


def select_momentum_basket(momentum_returns: dict[str, float], top_n: int) -> list[str]:
    """Return the top-N symbols by momentum return (descending).

    Ties broken deterministically by symbol name (ascending) so the result is
    stable run-to-run.
    """
    ranked = sorted(momentum_returns.items(), key=lambda kv: (-kv[1], kv[0]))
    return [sym for sym, _ in ranked[:top_n]]


def select_lowvol_basket(volatilities: dict[str, float], bottom_n: int) -> list[str]:
    """Return the bottom-N symbols by volatility (ascending).

    Ties broken deterministically by symbol name (ascending).
    """
    ranked = sorted(volatilities.items(), key=lambda kv: (kv[1], kv[0]))
    return [sym for sym, _ in ranked[:bottom_n]]


def compute_risk_parity_weights(mom_basket_vol: float, lv_basket_vol: float) -> tuple[float, float]:
    """Inverse-vol weights for (momentum, low-vol) legs, summing to 1.0.

    Each leg's weight is proportional to the inverse of its realized vol — the
    calmer leg receives more capital. Degenerate inputs (non-positive vols) fall
    back to a 50/50 split.
    """
    if mom_basket_vol <= 0 or lv_basket_vol <= 0:
        return 0.5, 0.5
    inv_mom = 1.0 / mom_basket_vol
    inv_lv = 1.0 / lv_basket_vol
    total = inv_mom + inv_lv
    return inv_mom / total, inv_lv / total


def _basket_vol(basket: list[str], volatilities: dict[str, float]) -> float:
    """Mean realized vol of the symbols in a basket (equal-weight proxy)."""
    vols = [volatilities[s] for s in basket if s in volatilities]
    if not vols:
        return 0.0
    return float(np.mean(vols))


def compute_target_positions(
    momentum_basket: list[str],
    lowvol_basket: list[str],
    momentum_weight: float,
    lowvol_weight: float,
    capital_inr: float,
    last_prices: dict[str, float],
) -> dict[str, dict]:
    """Compute target notional/quantity per symbol across both legs.

    Within each leg holdings are equal-weight (1/N of that leg's capital). A
    symbol appearing in both legs has its notionals summed (reason="both").
    Quantity = floor(target_notional / last_price).

    Returns:
        dict mapping symbol -> {"target_notional", "target_quantity", "reason"}.
    """
    targets: dict[str, dict] = {}

    def _add(symbol: str, notional: float, reason: str) -> None:
        if symbol not in last_prices or last_prices[symbol] <= 0:
            logger.warning("no usable last price for %s — skipping target", symbol)
            return
        entry = targets.setdefault(
            symbol, {"target_notional": 0.0, "target_quantity": 0, "reason": reason}
        )
        entry["target_notional"] += notional
        if entry["reason"] != reason:
            entry["reason"] = "both"

    if momentum_basket and momentum_weight > 0:
        per = (capital_inr * momentum_weight) / len(momentum_basket)
        for sym in momentum_basket:
            _add(sym, per, "momentum")

    if lowvol_basket and lowvol_weight > 0:
        per = (capital_inr * lowvol_weight) / len(lowvol_basket)
        for sym in lowvol_basket:
            _add(sym, per, "lowvol")

    for sym, entry in targets.items():
        entry["target_quantity"] = int(entry["target_notional"] // last_prices[sym])

    return targets


def diff_orders(
    current_positions: dict[str, int],
    target_positions: dict[str, dict],
    last_prices: dict[str, float],
) -> list[RebalanceOrder]:
    """Diff current holdings against targets into BUY/SELL recommendations.

    SELLs (positions to reduce or exit) are emitted before BUYs so the operator
    frees capital first. Returns an ordered list (sells, then buys), each sorted
    by symbol for stable output.
    """
    sells: list[RebalanceOrder] = []
    buys: list[RebalanceOrder] = []

    symbols = set(current_positions) | set(target_positions)
    for sym in sorted(symbols):
        cur_qty = int(current_positions.get(sym, 0))
        tgt = target_positions.get(sym)
        tgt_qty = int(tgt["target_quantity"]) if tgt else 0
        delta = tgt_qty - cur_qty
        if delta == 0:
            continue
        price = last_prices.get(sym, 0.0)
        if delta > 0:
            reason = tgt["reason"] if tgt else "momentum"
            buys.append(
                RebalanceOrder(
                    symbol=sym,
                    exchange="NSE",
                    side="BUY",
                    quantity=delta,
                    notional_inr=delta * price,
                    reason=reason,
                )
            )
        else:
            reason = "exit" if tgt_qty == 0 else (tgt["reason"] if tgt else "exit")
            sells.append(
                RebalanceOrder(
                    symbol=sym,
                    exchange="NSE",
                    side="SELL",
                    quantity=-delta,
                    notional_inr=-delta * price,
                    reason=reason,
                )
            )

    return sells + buys


def compute_rebalance(
    config: SectorRotationConfig,
    asof_date: date,
    current_positions: dict[str, int],
    db_path: str = "db/historify.duckdb",
) -> dict:
    """Top-level entry point — compute signals + recommended rebalance orders.

    Strictly read-only on the DB. Places no orders. Returns a JSON-serializable
    summary (RebalanceOrder objects are converted via ``to_dict``).
    """
    lookback = max(config.momentum_lookback_days, config.lowvol_lookback_days) + 5
    closes = load_daily_closes(config.universe, asof_date, lookback, db_path)

    momentum_returns = compute_momentum_returns(closes, config.momentum_lookback_days)
    volatilities = compute_realized_vol(closes, config.lowvol_lookback_days)

    momentum_basket = select_momentum_basket(momentum_returns, config.momentum_top_n)
    lowvol_basket = select_lowvol_basket(volatilities, config.lowvol_bottom_n)

    mom_vol = _basket_vol(momentum_basket, volatilities)
    lv_vol = _basket_vol(lowvol_basket, volatilities)
    momentum_weight, lowvol_weight = compute_risk_parity_weights(mom_vol, lv_vol)

    last_prices = {sym: series[-1][1] for sym, series in closes.items() if series}

    target_positions = compute_target_positions(
        momentum_basket,
        lowvol_basket,
        momentum_weight,
        lowvol_weight,
        config.capital_inr,
        last_prices,
    )

    rebalance_orders = diff_orders(current_positions, target_positions, last_prices)

    missing = [s for s in config.universe if s not in closes]
    if missing:
        logger.warning("no daily data for: %s", ", ".join(missing))

    return {
        "asof_date": asof_date.isoformat(),
        "momentum_basket": momentum_basket,
        "lowvol_basket": lowvol_basket,
        "momentum_weight": round(momentum_weight, 4),
        "lowvol_weight": round(lowvol_weight, 4),
        "target_positions": {
            sym: {
                "target_notional": round(v["target_notional"], 2),
                "target_quantity": v["target_quantity"],
                "reason": v["reason"],
            }
            for sym, v in target_positions.items()
        },
        "rebalance_orders": [o.to_dict() for o in rebalance_orders],
        "diagnostics": {
            "momentum_returns": {k: round(v, 6) for k, v in momentum_returns.items()},
            "volatilities": {k: round(v, 6) for k, v in volatilities.items()},
            "momentum_basket_vol": round(mom_vol, 6),
            "lowvol_basket_vol": round(lv_vol, 6),
            "last_prices": {k: round(v, 4) for k, v in last_prices.items()},
            "symbols_loaded": sorted(closes.keys()),
            "symbols_missing": missing,
            "capital_inr": config.capital_inr,
        },
    }
