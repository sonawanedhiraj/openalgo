"""
Sector Rotation ETF Strategy — CLI runner (read-only, no order placement).

Usage:
    uv run python -m services.sector_rotation_etf_cli --asof 2026-06-05 \
        --current-positions '{"BANKBEES":100}'

Loads config from strategies/sector_rotation_etf/config_snapshot.json, computes
the recommended rebalance, prints a human-readable summary, and writes the full
result JSON to outputs/sector_rotation_etf/rebalance_<asof_date>.json.

This NEVER places orders. It emits recommended-orders JSON for manual review.
"""

import argparse
import json
import os
from datetime import date, datetime

from services.sector_rotation_etf_service import (
    SectorRotationConfig,
    compute_rebalance,
)
from utils.logging import get_logger

logger = get_logger(__name__)

STRATEGY_DIR = os.path.join("strategies", "sector_rotation_etf")
CONFIG_PATH = os.path.join(STRATEGY_DIR, "config_snapshot.json")
OUTPUT_DIR = os.path.join("outputs", "sector_rotation_etf")


def load_config(config_path: str = CONFIG_PATH) -> SectorRotationConfig:
    """Build a SectorRotationConfig from the canonical config snapshot."""
    with open(config_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return SectorRotationConfig(
        universe=raw["universe"],
        momentum_lookback_days=raw["momentum_lookback_days"],
        lowvol_lookback_days=raw["lowvol_lookback_days"],
        momentum_top_n=raw["momentum_top_n"],
        lowvol_bottom_n=raw["lowvol_bottom_n"],
        weight_method=raw["weight_method"],
        capital_inr=float(raw["capital_inr"]),
        exchange=raw.get("exchange", "NSE"),
    )


def _parse_asof(value: str | None) -> date:
    if not value:
        return date.today()
    return datetime.strptime(value, "%Y-%m-%d").date()


def _print_summary(result: dict) -> None:
    print("=" * 64)
    print(f"Sector Rotation ETF — rebalance as of {result['asof_date']}")
    print("=" * 64)
    print(f"Momentum basket (top-3 6M return): {', '.join(result['momentum_basket']) or '—'}")
    print(f"Low-vol basket (bottom-3 60d vol): {', '.join(result['lowvol_basket']) or '—'}")
    print(
        f"Leg weights — momentum {result['momentum_weight']:.2%} / "
        f"low-vol {result['lowvol_weight']:.2%}"
    )
    print("-" * 64)
    print("Target positions:")
    if result["target_positions"]:
        for sym, t in sorted(result["target_positions"].items()):
            print(
                f"  {sym:<12} qty={t['target_quantity']:<6} "
                f"notional=Rs {t['target_notional']:>12,.0f}  [{t['reason']}]"
            )
    else:
        print("  (none)")
    print("-" * 64)
    print("Recommended orders (REVIEW — NOT placed):")
    if result["rebalance_orders"]:
        for o in result["rebalance_orders"]:
            print(
                f"  {o['side']:<4} {o['symbol']:<12} qty={o['quantity']:<6} "
                f"notional=Rs {o['notional_inr']:>12,.0f}  [{o['reason']}]"
            )
    else:
        print("  (no change — current matches target)")
    missing = result["diagnostics"].get("symbols_missing") or []
    if missing:
        print("-" * 64)
        print(f"WARNING — no daily data for: {', '.join(missing)}")
    print("=" * 64)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sector Rotation ETF rebalance (read-only, no order placement)."
    )
    parser.add_argument("--asof", help="As-of date YYYY-MM-DD (default: today)")
    parser.add_argument(
        "--current-positions",
        default="{}",
        help="JSON dict of current holdings, e.g. '{\"BANKBEES\":100}'",
    )
    parser.add_argument("--config", default=CONFIG_PATH, help="Path to config snapshot")
    parser.add_argument("--db-path", default="db/historify.duckdb", help="historify DuckDB path")
    args = parser.parse_args(argv)

    asof = _parse_asof(args.asof)
    current_positions = {k: int(v) for k, v in json.loads(args.current_positions).items()}
    config = load_config(args.config)

    logger.info("Computing sector_rotation_etf rebalance as of %s", asof)
    result = compute_rebalance(config, asof, current_positions, db_path=args.db_path)

    _print_summary(result)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, f"rebalance_{result['asof_date']}.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print(f"\nSaved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
