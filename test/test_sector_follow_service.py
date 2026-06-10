"""Unit tests for services/sector_follow_service.py.

All external effects (market-data metrics, order placement, notifications, trade
journal) are injected with fakes, so these run with no live broker, no DuckDB, and
no DB writes — mirroring the policy-injection pattern in
test/test_scanner_ws_watchdog.py / services/scanner_ws_watchdog.py.

NOTE: written but NOT executed during market hours (pytest pollutes the live
journal). Operator runs `uv run pytest test/test_sector_follow_service.py -v`
post-close to verify before merging to dev.
"""

from datetime import datetime, timedelta, timezone

import pytest

from services.sector_follow_service import (
    SectorFollowConfig,
    SectorFollowService,
    compute_qty,
    passes_gates,
    select_entries,
)

_IST = timezone(timedelta(hours=5, minutes=30))


def _config(**overrides) -> SectorFollowConfig:
    base = dict(
        capital_inr=250000.0,
        max_position_inr=50000.0,
        max_concurrent_positions=5,
        gate_sector_pct=1.0,
        gate_stock_pct=0.5,
        gate_vol_mult=1.0,
        daily_loss_kill_pct=3.0,
        cost_pct_round_trip=0.0857,
        vol_avg_lookback_days=20,
        broker="zerodha",
        exchange="NSE",
        product="CNC",
        universe=["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"],
        strategy_id=99,
    )
    base.update(overrides)
    return SectorFollowConfig(**base)


def _make_service(metrics=None, **overrides):
    """Build a service with all side effects stubbed out."""
    placed_orders = []
    journal = []

    def fake_placer(mode, order):
        placed_orders.append((mode, order))
        return {"status": "success", "orderid": f"OID-{order['symbol']}"}

    def fake_recorder(**kwargs):
        journal.append(kwargs)
        return len(journal)

    metrics = metrics or {}

    def fake_metrics_provider(as_of, universe, sector_map, config):
        return {s: metrics.get(s, _miss()) for s in universe}

    svc = SectorFollowService(
        config=_config(**overrides),
        sector_map={s: "NIFTY" for s in _config(**overrides).universe},
        mode=overrides.get("mode", "scaffold"),
        metrics_provider=fake_metrics_provider,
        order_placer=fake_placer,
        notifier=lambda msg: None,
        trade_recorder=fake_recorder,
        now=lambda: datetime(2026, 6, 10, 15, 20, tzinfo=_IST),
    )
    svc._test_placed = placed_orders
    svc._test_journal = journal
    return svc


def _miss():
    return {"sector_ret": None, "stock_ret": None, "vol_ratio": None, "current_price": None}


def _hit(sector=0.02, stock=0.01, vol=1.5, price=100.0):
    return {"sector_ret": sector, "stock_ret": stock, "vol_ratio": vol, "current_price": price}


# --------------------------------------------------------------------------- #
# Signal evaluator
# --------------------------------------------------------------------------- #
def test_signal_evaluator_passes_when_all_gates_met():
    svc = _make_service(metrics={"AAA": _hit()})
    cands = svc.evaluate_candidates()
    syms = {c["symbol"] for c in cands}
    assert "AAA" in syms


def test_signal_evaluator_rejects_when_sector_gate_misses():
    # sector +0.5% < 1% gate
    svc = _make_service(metrics={"AAA": _hit(sector=0.005)})
    assert all(c["symbol"] != "AAA" for c in svc.evaluate_candidates())


def test_signal_evaluator_rejects_when_stock_gate_misses():
    # stock +0.2% < 0.5% gate
    svc = _make_service(metrics={"AAA": _hit(stock=0.002)})
    assert all(c["symbol"] != "AAA" for c in svc.evaluate_candidates())


def test_signal_evaluator_rejects_when_vol_gate_misses():
    # vol_ratio 0.8 < 1.0 gate
    svc = _make_service(metrics={"AAA": _hit(vol=0.8)})
    assert all(c["symbol"] != "AAA" for c in svc.evaluate_candidates())


def test_passes_gates_fails_closed_on_none():
    cfg = _config()
    assert passes_gates({"sector_ret": None, "stock_ret": 0.01, "vol_ratio": 2.0}, cfg) is False


# --------------------------------------------------------------------------- #
# Position selector
# --------------------------------------------------------------------------- #
def test_position_selector_caps_at_5():
    cands = [{"symbol": f"S{i}", "vol_ratio": float(i), "current_price": 100.0} for i in range(8)]
    picked = select_entries(cands, set(), max_concurrent=5)
    assert len(picked) == 5


def test_position_selector_tiebreaker_is_vol_ratio_desc():
    cands = [
        {"symbol": "LOW", "vol_ratio": 1.1, "current_price": 100.0},
        {"symbol": "HIGH", "vol_ratio": 3.0, "current_price": 100.0},
        {"symbol": "MID", "vol_ratio": 2.0, "current_price": 100.0},
    ]
    picked = select_entries(cands, set(), max_concurrent=2)
    assert [c["symbol"] for c in picked] == ["HIGH", "MID"]


def test_position_selector_skips_already_open():
    cands = [
        {"symbol": "OPEN", "vol_ratio": 5.0, "current_price": 100.0},
        {"symbol": "NEW", "vol_ratio": 2.0, "current_price": 100.0},
    ]
    picked = select_entries(cands, {"OPEN"}, max_concurrent=5)
    assert [c["symbol"] for c in picked] == ["NEW"]


def test_position_selector_respects_remaining_slots():
    cands = [{"symbol": f"S{i}", "vol_ratio": float(i), "current_price": 100.0} for i in range(8)]
    # 3 already open -> only 2 slots left.
    picked = select_entries(cands, {"O1", "O2", "O3"}, max_concurrent=5)
    assert len(picked) == 2


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_fires_at_3pct_loss():
    svc = _make_service()
    # -3% of 250000 = -7500. A -7501 loss trips it.
    active = svc.update_daily_pnl(realized_today=-7501.0, open_mtm=0.0)
    assert active is True
    assert svc.kill_switch_active is True


def test_kill_switch_does_not_fire_above_threshold():
    svc = _make_service()
    active = svc.update_daily_pnl(realized_today=-7000.0, open_mtm=0.0)
    assert active is False


def test_kill_switch_blocks_new_entries():
    svc = _make_service(metrics={"AAA": _hit()}, mode="sandbox")
    svc.kill_switch_active = True
    result = svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    assert result is None
    assert svc._test_placed == []  # no order routed


def test_kill_switch_does_not_block_scheduled_exits():
    from services.sector_follow_service import PaperPosition

    svc = _make_service(mode="sandbox")
    svc.kill_switch_active = True
    pos = PaperPosition(
        symbol="AAA", quantity=10, entry_price=100.0, entry_date="2026-06-09", vol_ratio=2.0
    )
    result = svc.place_exit(pos, price=101.0)
    assert result is not None
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["action"] == "SELL"


def test_daily_reset_clears_kill_switch():
    svc = _make_service()
    svc.kill_switch_active = True
    svc.daily_pnl = -9999.0
    svc.reset_daily_state()
    assert svc.kill_switch_active is False
    assert svc.daily_pnl == 0.0


# --------------------------------------------------------------------------- #
# Mode-aware order placement
# --------------------------------------------------------------------------- #
def test_scaffold_mode_does_not_place_orders():
    svc = _make_service(metrics={"AAA": _hit()}, mode="scaffold")
    result = svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    assert result is not None  # paper-recorded
    assert svc._test_placed == []  # but NO order routed
    assert "AAA" in svc.paper_book
    assert len(svc._test_journal) == 1  # journal row still written


def test_sandbox_mode_routes_to_sandbox_db():
    svc = _make_service(metrics={"AAA": _hit()}, mode="sandbox")
    svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    assert len(svc._test_placed) == 1
    mode, order = svc._test_placed[0]
    assert mode == "sandbox"
    assert order["action"] == "BUY"
    assert order["symbol"] == "AAA"


# --------------------------------------------------------------------------- #
# Quantity sizing
# --------------------------------------------------------------------------- #
def test_qty_sizing_floors_to_integer_shares():
    # 50000 / 333.33 = 150.0009 -> 150
    assert compute_qty(50000.0, 333.33) == 150
    # 50000 / 100 = 500
    assert compute_qty(50000.0, 100.0) == 500
    # non-positive price -> 0 (skip)
    assert compute_qty(50000.0, 0.0) == 0


def test_entry_qty_uses_max_position_inr():
    svc = _make_service(metrics={"AAA": _hit(price=250.0)}, mode="sandbox")
    svc.place_entry({"symbol": "AAA", "current_price": 250.0, "vol_ratio": 2.0})
    # 50000 / 250 = 200 shares
    assert svc.paper_book["AAA"].quantity == 200


# --------------------------------------------------------------------------- #
# End-to-end entry job
# --------------------------------------------------------------------------- #
def test_run_entry_caps_and_records():
    metrics = {s: _hit(vol=float(i + 1)) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"])}
    svc = _make_service(metrics=metrics, mode="scaffold")
    placed = svc.run_entry()
    assert len(placed) == 5  # capped at max_concurrent
    # highest vol_ratio names selected (FFF=6, EEE=5, DDD=4, CCC=3, BBB=2)
    assert {p["symbol"] for p in placed} == {"FFF", "EEE", "DDD", "CCC", "BBB"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
