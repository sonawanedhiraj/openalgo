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

    # `mode` and `price_fetcher` are service constructor args (not config fields) —
    # strip them from overrides before building the SectorFollowConfig. Default
    # price fetcher returns None so positions price as "unavailable" unless a test
    # injects a real one (keeps non-MTM tests hermetic — no broker call).
    mode = overrides.pop("mode", "scaffold")
    price_fetcher = overrides.pop("price_fetcher", lambda symbol, exchange: None)
    # Optional injections for the data-freshness gate tests. Default checker None
    # (gate skipped — keeps every existing test hermetic).
    notifier = overrides.pop("notifier", lambda msg: None)
    data_health_checker = overrides.pop("data_health_checker", None)
    # Unified daily-intent: default to an injected run/env decision so tests stay
    # hermetic (no real strategy_daily_intent DB read). Tests exercising the
    # intent gate pass their own ``intent_resolver``.
    intent_resolver = overrides.pop("intent_resolver", None)
    if intent_resolver is None:
        from services.mode_service import EffectiveDecision

        intent_resolver = lambda: EffectiveDecision(  # noqa: E731
            mode="sandbox", intent="run", daily_capital_cap=None, source="env"
        )
    cfg = _config(**overrides)
    svc = SectorFollowService(
        config=cfg,
        sector_map={s: "NIFTY" for s in cfg.universe},
        mode=mode,
        metrics_provider=fake_metrics_provider,
        order_placer=fake_placer,
        price_fetcher=price_fetcher,
        notifier=notifier,
        trade_recorder=fake_recorder,
        now=lambda: datetime(2026, 6, 10, 15, 20, tzinfo=_IST),
        intent_resolver=intent_resolver,
        data_health_checker=data_health_checker,
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


# --------------------------------------------------------------------------- #
# Phase 2 — observability + operator controls
# --------------------------------------------------------------------------- #
def test_get_status_returns_required_keys():
    svc = _make_service(metrics={"AAA": _hit()}, mode="sandbox")
    svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    status = svc.get_status()
    required = {
        "mode", "kill_switch_active", "kill_switch_reason", "manual_pause",
        "today_entries", "today_exits", "open_positions", "today_pnl_net",
        "capital_inr", "config",
    }
    assert required <= set(status)
    assert status["mode"] == "sandbox"
    assert status["capital_inr"] == 250000.0
    assert len(status["today_entries"]) == 1
    assert len(status["open_positions"]) == 1
    assert status["open_positions"][0]["symbol"] == "AAA"


def test_manual_pause_halts_new_entries_but_not_exits():
    from services.sector_follow_service import PaperPosition

    svc = _make_service(metrics={"AAA": _hit()}, mode="sandbox")
    svc.pause()
    assert svc.manual_pause is True
    # New entry is blocked while paused.
    blocked = svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    assert blocked is None
    assert svc._test_placed == []
    # A scheduled exit still runs while paused.
    pos = PaperPosition(
        symbol="BBB", quantity=10, entry_price=100.0, entry_date="2026-06-09", vol_ratio=2.0
    )
    exited = svc.place_exit(pos, price=102.0)
    assert exited is not None
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["action"] == "SELL"


def test_resume_clears_kill_switch():
    svc = _make_service()
    svc.kill_switch_active = True
    svc.kill_switch_reason = "forced for test"
    svc.manual_pause = True
    result = svc.resume()
    assert svc.kill_switch_active is False
    assert svc.kill_switch_reason is None
    assert svc.manual_pause is False
    assert result["kill_switch_active"] is False


def test_close_all_squares_open_positions():
    from services.sector_follow_service import PaperPosition

    svc = _make_service(mode="sandbox")
    svc.paper_book = {
        "AAA": PaperPosition("AAA", 10, 100.0, "2026-06-09", 2.0),
        "BBB": PaperPosition("BBB", 5, 200.0, "2026-06-09", 1.5),
    }
    closed = svc.close_all_positions()
    assert len(closed) == 2
    assert {c["symbol"] for c in closed} == {"AAA", "BBB"}
    assert all(c["status"] == "success" for c in closed)
    assert svc.paper_book == {}  # book emptied
    # Both squared off via SELL orders.
    assert len(svc._test_placed) == 2
    assert all(o[1]["action"] == "SELL" for o in svc._test_placed)


def test_eod_summary_formats_telegram_message_correctly():
    from services.sector_follow_service import PaperPosition

    svc = _make_service(mode="sandbox")
    # Two entries today.
    svc.today_entries = [
        {"symbol": "TMPV", "entry_time": "t", "entry_price": 100.0, "qty": 10},
        {"symbol": "BEL", "entry_time": "t", "entry_price": 50.0, "qty": 20},
    ]
    # One exit from a prior session.
    svc.place_exit(
        PaperPosition("HDFCBANK", 10, 1000.0, "2026-06-09", 2.0), price=1004.2
    )
    # One position still open.
    svc.paper_book["TMPV"] = PaperPosition("TMPV", 10, 100.0, "2026-06-10", 2.0)

    msg = svc.build_eod_summary()
    assert "📊 sector_follow_cap5_vol EOD 2026-06-10" in msg
    assert "Mode: sandbox" in msg
    assert "Entries: 2 (TMPV, BEL)" in msg
    assert "Exits: 1" in msg
    assert "entered 06-09" in msg
    assert "HDFCBANK +0.42%" in msg
    assert "Open EOD: 1 (T+1 exit 2026-06-11)" in msg
    assert "Kill switch: inactive" in msg


# --------------------------------------------------------------------------- #
# Phase 3 — live MTM + sector-index feed wiring
# --------------------------------------------------------------------------- #
def test_mtm_compute_returns_gross_and_net():
    from services.sector_follow_service import PaperPosition

    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 110.0)
    pos = PaperPosition("AAA", 100, 100.0, "2026-06-09", 2.0)
    mtm = svc._compute_mtm(pos)
    # gross = (110 - 100) * 100 = 1000
    assert mtm["mtm_pnl_gross"] == 1000.0
    # cost = 0.0857% * 100 * 100 = 8.57 ; net = 1000 - 8.57
    assert mtm["mtm_pnl_net"] == pytest.approx(1000.0 - 8.57)
    assert mtm["current_price"] == 110.0
    assert mtm["mtm_error"] is None


def test_mtm_handles_price_fetch_failure():
    from services.sector_follow_service import PaperPosition

    def boom(symbol, exchange):
        raise RuntimeError("broker quote API down")

    svc = _make_service(mode="sandbox", price_fetcher=boom)
    pos = PaperPosition("AAA", 10, 100.0, "2026-06-09", 2.0)
    mtm = svc._compute_mtm(pos)
    assert mtm["mtm_pnl_gross"] is None
    assert mtm["mtm_pnl_net"] is None
    assert mtm["current_price"] is None
    assert mtm["mtm_error"] is not None


def test_status_includes_mtm_when_positions_open():
    svc = _make_service(
        metrics={"AAA": _hit(price=100.0)}, mode="sandbox",
        price_fetcher=lambda s, e: 105.0,
    )
    svc.place_entry({"symbol": "AAA", "current_price": 100.0, "vol_ratio": 2.0})
    status = svc.get_status()
    op = status["open_positions"][0]
    assert op["symbol"] == "AAA"
    assert op["current_price"] == 105.0
    assert op["mtm_pnl_gross"] is not None
    assert op["mtm_pnl_net"] is not None
    assert op["mtm_pnl"] == op["mtm_pnl_net"]  # legacy alias populated
    # No closed exits -> today_pnl_net is purely the open position's unrealized net.
    assert status["today_pnl_net"] == pytest.approx(op["mtm_pnl_net"])
    assert status["today_pnl_unrealized_net"] == pytest.approx(op["mtm_pnl_net"])


def test_sector_index_subscription_includes_all_mapped():
    import json as _json
    from pathlib import Path

    from services.sector_follow_index_backfill import sector_index_symbols
    from services.sector_follow_service import _DEFAULT_SECTOR_MAP_PATH

    subs = set(sector_index_symbols())
    raw = _json.loads(Path(_DEFAULT_SECTOR_MAP_PATH).read_text(encoding="utf-8"))
    mapped = {entry["index"] for entry in raw["map"].values()}
    # Every index referenced by the live map is kept fresh.
    assert mapped <= subs
    # The two known 1m-missing indices are always attempted defensively, even
    # though the Phase 3 re-map (DIXON, RELIANCE -> NIFTY) no longer references them.
    assert {"NIFTYCONSRDURBL", "NIFTYOILANDGAS"} <= subs
    # NIFTY broad-market fallback is in the set.
    assert "NIFTY" in subs


# --------------------------------------------------------------------------- #
# Phase 5 — EOD markdown report file sink (alongside Telegram summary)
# --------------------------------------------------------------------------- #
def _eod_service_with_activity(mode="sandbox"):
    """A service seeded with one open position + one closed exit for EOD tests."""
    from services.sector_follow_service import PaperPosition

    svc = _make_service(mode=mode)
    svc.today_entries = [
        {
            "symbol": "AAA", "entry_time": "t", "entry_price": 100.0, "qty": 50,
            "vol_ratio": 2.0, "sector": "NIFTY", "sector_ret": 0.018, "stock_ret": 0.009,
        },
    ]
    # A prior-session position squared off today -> populates today_exits.
    svc.place_exit(PaperPosition("BBB", 10, 1000.0, "2026-06-09", 2.0), price=1010.0)
    # A position still open at EOD.
    svc.paper_book["AAA"] = PaperPosition("AAA", 50, 100.0, "2026-06-10", 2.0)
    return svc


def test_format_eod_report_markdown_has_expected_sections():
    svc = _eod_service_with_activity()
    report = svc._format_eod_report_markdown(
        journal_rows=list(svc.today_entries) + list(svc.today_exits),
        positions=svc.open_positions_view(),
        kill_switch_state={"active": False, "reason": None, "daily_pnl": 0.0},
    )
    # Header + mode.
    assert "# sector_follow_cap5_vol — EOD Report 2026-06-10" in report
    assert "- **Mode:** sandbox" in report
    # Required sections.
    for heading in ("## Summary", "## Sector breakdown", "## Positions",
                    "## Kill switch (EOD)", "## Note — expected vs R40 baseline"):
        assert heading in report
    # Summary content.
    assert "Signals fired / positions opened: 1" in report
    assert "Open at EOD: 1 (T+1 exit 2026-06-11)" in report
    assert "Exits today: 1" in report
    # Sector breakdown shows the index + intraday %.
    assert "NIFTY" in report
    assert "+1.80%" in report
    # Per-position table: open row (AAA) and closed row (BBB) with exact realized P&L.
    assert "| AAA | NIFTY |" in report
    assert "OPEN" in report
    assert "CLOSED" in report
    # BBB: (1010-1000)*10 = +100 realized; entry recovered as 1,000.00.
    assert "+100" in report
    assert "1,000.00" in report
    assert "1,010.00" in report
    # Kill switch + baseline note.
    assert "State: inactive" in report
    assert "Sharpe ~2.19" in report


def test_eod_report_file_sink_writes_expected_path_and_content(tmp_path):
    svc = _eod_service_with_activity()
    svc.eod_reports_dir = tmp_path / "eod_reports"
    out_path = svc._write_eod_report()
    expected = tmp_path / "eod_reports" / "2026-06-10.md"
    assert out_path == expected
    assert expected.exists()
    content = expected.read_text(encoding="utf-8")
    assert "# sector_follow_cap5_vol — EOD Report 2026-06-10" in content
    assert "## Positions" in content


def test_run_eod_summary_telegram_failure_does_not_block_file_sink(tmp_path):
    notified = []

    def boom_notifier(msg):
        notified.append(msg)
        raise RuntimeError("telegram down")

    svc = _make_service(mode="sandbox")
    svc._notify = boom_notifier
    svc.eod_reports_dir = tmp_path / "eod_reports"
    # Should not raise despite the Telegram failure.
    svc.run_eod_summary()
    # File still written.
    assert (tmp_path / "eod_reports" / "2026-06-10.md").exists()
    # Telegram was still attempted.
    assert len(notified) == 1


def test_run_eod_summary_file_failure_does_not_block_telegram(tmp_path):
    notified = []
    svc = _make_service(mode="sandbox")
    svc._notify = lambda msg: notified.append(msg)
    # Point the report dir at a path whose parent is a FILE -> mkdir fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")
    svc.eod_reports_dir = blocker / "eod_reports"
    # Should not raise despite the file-sink failure.
    svc.run_eod_summary()
    # Telegram still delivered.
    assert len(notified) == 1
    assert "sector_follow_cap5_vol EOD" in notified[0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


# --------------------------------------------------------------------------- #
# Unified daily-intent gate (pause / halt / daily_capital_cap / mode override)
# --------------------------------------------------------------------------- #
def _decision(mode="sandbox", intent="run", cap=None, source="unified"):
    from services.mode_service import EffectiveDecision

    return EffectiveDecision(
        mode=mode, intent=intent, daily_capital_cap=cap, source=source
    )


def _open_pos(svc, symbol="ZZZ", entry_date="2026-06-09"):
    from services.sector_follow_service import PaperPosition

    svc.paper_book[symbol] = PaperPosition(
        symbol=symbol, quantity=10, entry_price=100.0, entry_date=entry_date, vol_ratio=2.0
    )


def test_intent_pause_blocks_entries():
    svc = _make_service(
        metrics={"AAA": _hit(), "BBB": _hit()},
        mode="sandbox",
        intent_resolver=lambda: _decision(intent="pause"),
    )
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []  # no orders dispatched


def test_intent_pause_still_runs_exits():
    svc = _make_service(mode="sandbox", intent_resolver=lambda: _decision(intent="pause"))
    _open_pos(svc)  # entered yesterday → eligible for T+1 exit
    exited = svc.run_exit()
    assert [e["symbol"] for e in exited] == ["ZZZ"]
    assert any(o[1]["action"] == "SELL" for o in svc._test_placed)


def test_intent_halt_blocks_entries():
    svc = _make_service(
        metrics={"AAA": _hit()},
        mode="sandbox",
        intent_resolver=lambda: _decision(intent="halt"),
    )
    assert svc.run_entry() == []
    assert svc._test_placed == []


def test_intent_halt_blocks_exits():
    svc = _make_service(mode="sandbox", intent_resolver=lambda: _decision(intent="halt"))
    _open_pos(svc)
    assert svc.run_exit() == []
    assert svc._test_placed == []  # halt blocks the T+1 exit too


def test_daily_capital_cap_limits_slots():
    # cap = 50_000 and max_position_inr = 50_000 → exactly 1 slot, so only the
    # top vol_ratio candidate is entered even though two pass the gates.
    svc = _make_service(
        metrics={"AAA": _hit(vol=1.5), "BBB": _hit(vol=3.0)},
        mode="sandbox",
        intent_resolver=lambda: _decision(intent="run", cap=50000.0),
    )
    placed = svc.run_entry()
    assert len(placed) == 1
    assert placed[0]["symbol"] == "BBB"  # higher vol_ratio wins the single slot


def test_unified_mode_override_changes_routing():
    # Service constructed scaffold; a unified row with mode='sandbox' overrides
    # it so the entry actually dispatches an order.
    svc = _make_service(
        metrics={"AAA": _hit()},
        mode="scaffold",
        intent_resolver=lambda: _decision(mode="sandbox", intent="run", source="unified"),
    )
    svc.run_entry()
    assert svc.mode == "sandbox"
    assert any(o[1]["symbol"] == "AAA" for o in svc._test_placed)


def test_env_source_does_not_override_mode():
    # source='env' must NOT mutate self.mode — back-compat fall-through.
    svc = _make_service(
        metrics={"AAA": _hit()},
        mode="scaffold",
        intent_resolver=lambda: _decision(mode="sandbox", intent="run", source="env"),
    )
    svc.run_entry()
    assert svc.mode == "scaffold"  # unchanged
    assert svc._test_placed == []  # scaffold places no orders


# --------------------------------------------------------------------------- #
# Data-freshness gate (run_entry aborts on stale; run_exit still processes)
# --------------------------------------------------------------------------- #
def test_run_entry_aborts_on_stale_data():
    alerts = []
    stale_details = {
        "AAA": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9, "kind": "stock"},
        "NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9, "kind": "index"},
    }
    svc = _make_service(
        metrics={"AAA": _hit(), "BBB": _hit()},
        mode="sandbox",
        intent_resolver=lambda: _decision(mode="sandbox", intent="run"),
        notifier=lambda msg: alerts.append(msg),
        data_health_checker=lambda name, date, index_only=False: (False, stale_details),
    )
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []  # no orders dispatched
    assert any("ABORTED" in a for a in alerts)  # operator alerted


def test_run_entry_proceeds_when_data_fresh():
    svc = _make_service(
        metrics={"AAA": _hit()},
        mode="sandbox",
        intent_resolver=lambda: _decision(mode="sandbox", intent="run"),
        data_health_checker=lambda name, date, index_only=False: (True, {}),
    )
    placed = svc.run_entry()
    assert [p["symbol"] for p in placed] == ["AAA"]


def test_run_exit_proceeds_despite_stale_index_data():
    # Exits must NOT be blocked by a stale index feed — a held T+1 position is
    # riskier than squaring off on a slightly stale read.
    svc = _make_service(
        mode="sandbox",
        intent_resolver=lambda: _decision(intent="run"),
        data_health_checker=lambda name, date, index_only=False: (
            False, {"NIFTY": {"ok": False, "last_date": "2026-05-29",
                              "staleness_days": 9, "kind": "index"}}
        ),
    )
    _open_pos(svc)  # entered yesterday → eligible for T+1 exit
    exited = svc.run_exit()
    assert [e["symbol"] for e in exited] == ["ZZZ"]
    assert any(o[1]["action"] == "SELL" for o in svc._test_placed)
