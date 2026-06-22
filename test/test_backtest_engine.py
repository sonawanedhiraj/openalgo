"""Tests for the backtest replay loop + simulated execution.

These exercise ``services.backtest_service.run_backtest`` end-to-end with
the DB stubbed to in-memory SQLite. Historical bar fetch is monkeypatched
so each test feeds a deterministic bar stream; that lets us assert the
exact SL/target/EOD/cooldown behaviour without depending on real
Historify data.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_backtest_db(monkeypatch):
    from database import backtest_db as bdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
    monkeypatch.setattr(bdb, "engine", test_engine)
    monkeypatch.setattr(bdb, "db_session", test_session)
    bdb.Base.metadata.create_all(bind=test_engine)
    yield bdb
    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def isolated_rule_registry(monkeypatch):
    """Snapshot the scan_rule registry and restore it after the test.

    Tests that register an inline rule via @scan_rule must not bleed into
    other tests in the suite.
    """
    from services import scanner_service

    saved = dict(scanner_service._rule_registry)
    saved_meta = dict(scanner_service._rule_metadata)
    yield
    scanner_service._rule_registry.clear()
    scanner_service._rule_registry.update(saved)
    scanner_service._rule_metadata.clear()
    scanner_service._rule_metadata.update(saved_meta)


def _bar(ts: dt.datetime, o: float, h: float, lo: float, c: float, v: int = 1000) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": v}


def _warmup_bars(start: dt.datetime, n: int = 22, price: float = 100.0) -> list[dict]:
    """N flat-volume bars walking forward in 5-minute steps."""
    out = []
    for i in range(n):
        out.append(
            _bar(start + dt.timedelta(minutes=5 * i), price, price + 0.1, price - 0.1, price)
        )
    return out


def _zero_slip():
    """A SlippageModel that produces no slippage — keeps fills == bar prices.

    Used by the older tests whose assertions assume the MVP behavior (entry
    at the next bar's open, SL/target/EOD at the exact reference price).
    """
    from services.backtest_service import SlippageModel  # noqa: PLC0415

    return SlippageModel(slippage_bps=0.0, half_spread_bps=0.0)


def _patch_replay(monkeypatch, bars_by_symbol: dict[str, list[dict]]):
    """Patch the bar pipeline to feed ``bars_by_symbol`` directly to the
    replay loop. Bypasses Historify entirely.
    """
    from services import backtest_service

    def fake_fetch(symbol, exchange, from_date, to_date, source="api", cache=None):
        # Returning non-empty so the empty-history short-circuit doesn't trigger.
        # Accepts source/cache kwargs since real _fetch_bars now takes them
        # (Commit 3); tests don't care which source is selected.
        return [{"timestamp": 0, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0}]

    def fake_aggregate(bars_1m, interval):
        # The fake_fetch payload above is a placeholder — pick up the real
        # bars from the closure keyed by the symbol currently being replayed.
        return bars_1m  # placeholder, overridden in _replay_symbol via monkeypatch

    monkeypatch.setattr(backtest_service, "_fetch_bars", fake_fetch)

    # _replay_symbol calls _aggregate_to_interval_ohlc directly. We swap it
    # for a function that returns the pre-baked bars for the symbol the
    # caller passed in. We track that via a frame-walking trick — simpler:
    # patch _replay_symbol itself to accept an injected bar list.
    original = backtest_service._replay_symbol

    def patched_replay(**kwargs):
        sym = kwargs["symbol"]
        injected = list(bars_by_symbol.get(sym, []))
        # Patch aggregate to return our bars regardless of input, just for
        # this call. Use a nested monkeypatch so other tests aren't affected.
        monkeypatch.setattr(
            backtest_service,
            "_aggregate_to_interval_ohlc",
            lambda bars_1m, interval: injected,
        )
        return original(**kwargs)

    monkeypatch.setattr(backtest_service, "_replay_symbol", patched_replay)


# ---------------------------------------------------------------------------
# Test 1: no history → run still completes with 0 trades.
# ---------------------------------------------------------------------------


def test_run_backtest_completes_on_empty_history(fresh_backtest_db, monkeypatch):
    from services import backtest_service

    monkeypatch.setattr(backtest_service, "_fetch_bars", lambda *a, **kw: [])

    run_id = backtest_service.run_backtest(
        symbols=["NODATA"],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
    )
    assert run_id > 0
    row = backtest_service.get_run(run_id)
    assert row["status"] == "completed"
    assert row["total_trades"] == 0
    assert backtest_service.get_run_trades(run_id) == []


# ---------------------------------------------------------------------------
# Test 2: rule match arms entry on next bar's open.
# ---------------------------------------------------------------------------


def test_run_backtest_records_entry_when_rule_matches(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """Use a synthetic always-match rule so we can assert entry mechanics
    without coupling the test to fno_intraday_buy_chartink's exact thresholds.
    """
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_always_buy", "buy", "fires on every bar")
    def _always(bars, indicators):  # noqa: ARG001
        return True

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=25, price=100.0)
    _patch_replay(monkeypatch, {"SBIN": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_always_buy"],
        symbols=["SBIN"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=_zero_slip(),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) >= 1
    t = trades[0]
    assert t["symbol"] == "SBIN"
    assert t["direction"] == "LONG"
    assert t["entry_reason"] == "test_always_buy"
    # Entry happens on the bar AFTER the one where the rule first matched.
    # Rule first becomes evaluable at bar index 21 (need 21 bars of history).
    # So pending_entry is set at bar 21, entry fires at bar 22 open.
    assert t["entry_price"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Test 3: stop-loss hit at exact SL price.
# ---------------------------------------------------------------------------


def test_run_backtest_exits_on_stop_loss(fresh_backtest_db, monkeypatch, isolated_rule_registry):
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_buy_once", "buy", "fires on the 21st bar only")
    def _once(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    # 21 warm-up bars trigger the rule at the end of bar 21.
    # Bar 22: entry at open=100. ATR(14) ≈ 0.2 on flat bars (high-low=0.2).
    # atr_sl_mult=1.5 → risk_per_share ≈ 0.3 → SL ≈ 99.7. Make bar 22 low
    # dip well below that so the SL is unambiguously taken intra-bar.
    bars = _warmup_bars(start, n=21, price=100.0)
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)
    bars.append(_bar(entry_bar_ts, 100.0, 100.2, 95.0, 99.0))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_once"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        atr_sl_mult=1.5,
        slippage_model=_zero_slip(),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "stop_loss"
    assert t["exit_price"] == pytest.approx(t["sl_price"])
    # PnL is negative (stop-loss on a LONG).
    assert t["pnl"] < 0


# ---------------------------------------------------------------------------
# Test 4: target hit at exact target price.
# ---------------------------------------------------------------------------


def test_run_backtest_exits_on_target(fresh_backtest_db, monkeypatch, isolated_rule_registry):
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_buy_once_t", "buy", "fires on the 21st bar")
    def _once(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)
    # Bar 22: bar opens at 100, high pushes well above target (target is
    # at entry + 1.5*risk ≈ 100.45 for risk ~0.3 with rr=1.5), low stays
    # above SL.
    bars.append(_bar(entry_bar_ts, 100.0, 105.0, 99.9, 104.0))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_once_t"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        atr_sl_mult=1.5,
        rr_target=1.5,
        slippage_model=_zero_slip(),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "target"
    assert t["exit_price"] == pytest.approx(t["target_price"])
    assert t["pnl"] > 0


# ---------------------------------------------------------------------------
# Test 5: EOD squareoff after configured cutoff.
# ---------------------------------------------------------------------------


def test_run_backtest_eod_squareoff(fresh_backtest_db, monkeypatch, isolated_rule_registry):
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_buy_eod", "buy", "fires on bar 21")
    def _once(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    # Place bar 21 just before EOD so bar 22's open enters the trade and
    # bar 23 (or later) crosses the EOD cutoff.
    start = dt.datetime(2026, 5, 25, 13, 30)
    bars = _warmup_bars(start, n=21, price=100.0)  # bars 1..21
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)  # 15:15? compute below

    # We want bar 22 to be the entry bar and a subsequent bar to be past
    # EOD. Use eod_time_ist=14:30 so any bar at/after 14:30 forces EOD.
    # With start=13:30 and bars every 5min, bar 21 ts = 15:10, bar 22 ts =
    # 15:15. That's already past 14:30, so entry never arms — pending
    # entries that would arm past EOD are cleared. Adjust: use smaller
    # warm-up or larger gap. Start at 09:15.
    bars = _warmup_bars(dt.datetime(2026, 5, 25, 9, 15), n=21, price=100.0)
    entry_bar_ts = dt.datetime(2026, 5, 25, 9, 15) + dt.timedelta(minutes=5 * 21)
    # Bar 22 stays within target/SL range so it doesn't exit on its own.
    bars.append(_bar(entry_bar_ts, 100.0, 100.2, 99.9, 100.05))
    # Bar 23 lands past the 11:00 EOD cutoff we'll set.
    bars.append(_bar(dt.datetime(2026, 5, 25, 11, 5), 100.0, 100.2, 99.9, 100.1))
    _patch_replay(monkeypatch, {"X": bars})

    # Use 11:05 cutoff so bar 22 (at 11:00) enters and bar 23 (at 11:05)
    # triggers EOD. A cutoff equal to the entry bar's time would force-exit
    # on the entry bar itself — that path is also tested in test 3 implicitly
    # via the stop-loss check, so here we explicitly hold a position across
    # bars to verify the "EOD on a later bar" path.
    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_eod"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        eod_time_ist="11:05",
        slippage_model=_zero_slip(),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    assert t["exit_reason"] == "eod_squareoff"
    # EOD exits at the EOD bar's close.
    assert t["exit_price"] == pytest.approx(100.1)


# ---------------------------------------------------------------------------
# Test 6: same-day stop-out blocks a second entry even if the rule fires.
# ---------------------------------------------------------------------------


def test_run_backtest_same_day_cooldown_blocks_reentry(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_buy_repeats", "buy", "fires from bar 21 onward")
    def _all_after_warmup(bars, indicators):  # noqa: ARG001
        return len(bars) >= 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)
    # Bar 22: entry at open 100, low blows through SL.
    bars.append(_bar(entry_bar_ts, 100.0, 100.2, 90.0, 95.0))
    # Bars 23+ keep firing the rule with healthy room above SL/target.
    for i in range(23, 50):
        bars.append(_bar(start + dt.timedelta(minutes=5 * i), 100.0, 100.5, 99.5, 100.0))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_repeats"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        eod_time_ist="15:30",
        slippage_model=_zero_slip(),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    # Despite the rule firing on bars 23..49, no second trade is recorded
    # because the first one stopped out — same-day block kicks in.
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "stop_loss"


# ---------------------------------------------------------------------------
# Test 7: get_history raising is caught — run marked completed (per-symbol skip).
# ---------------------------------------------------------------------------


def test_run_backtest_handles_history_fetch_failure(fresh_backtest_db, monkeypatch):
    from services import backtest_service

    def boom(*a, **kw):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(backtest_service, "_fetch_bars", boom)

    run_id = backtest_service.run_backtest(
        symbols=["FAIL1", "FAIL2"],
        from_date="2026-01-01",
        to_date="2026-01-01",
        interval="5m",
    )
    assert run_id > 0
    row = backtest_service.get_run(run_id)
    # Orchestrator survives per-symbol exceptions; run marks completed.
    assert row["status"] == "completed"
    assert row["total_trades"] == 0


# ---------------------------------------------------------------------------
# Slippage model tests (Commit 1).
# ---------------------------------------------------------------------------


def test_slippage_model_round_to_tick():
    from services.backtest_service import SlippageModel

    m = SlippageModel(tick_size=0.05, slippage_bps=0.0, half_spread_bps=0.0)
    # 100.03 → nearest tick is 100.05 (delta 0.02 vs 0.03 to 100.00).
    assert m.round_to_tick(100.03) == pytest.approx(100.05)
    # 100.07 → nearest tick is 100.05 (delta 0.02 vs 0.03 to 100.10).
    assert m.round_to_tick(100.07) == pytest.approx(100.05)


def test_slippage_long_entry_fills_above_mid():
    from services.backtest_service import SlippageModel

    m = SlippageModel(tick_size=0.01, slippage_bps=2.0, half_spread_bps=1.5)
    # offset = 100 * (1.5 + 2.0) / 10000 = 0.035 → rounded to 0.01-tick = 0.04
    fill = m.entry_fill(100.0, "LONG")
    assert fill > 100.0
    assert fill == pytest.approx(100.04)


def test_slippage_short_entry_fills_below_mid():
    from services.backtest_service import SlippageModel

    m = SlippageModel(tick_size=0.01, slippage_bps=2.0, half_spread_bps=1.5)
    fill = m.entry_fill(100.0, "SHORT")
    assert fill < 100.0
    assert fill == pytest.approx(99.96)


def test_slippage_long_exit_fills_below_mid():
    from services.backtest_service import SlippageModel

    m = SlippageModel(tick_size=0.01, slippage_bps=2.0, half_spread_bps=1.5)
    fill = m.exit_fill(100.0, "LONG")
    assert fill < 100.0
    assert fill == pytest.approx(99.96)


def test_replay_with_default_slippage_produces_realistic_fills(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """Default slippage drags entries above the next-bar open for LONGs and
    exits below the SL/target reference for LONGs."""
    from services import backtest_service, scanner_service

    @scanner_service.scan_rule("test_buy_default_slip", "buy", "fires on bar 21")
    def _once(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)
    # Bar 22 stays in-range so the trade survives until EOD squareoff.
    bars.append(_bar(entry_bar_ts, 100.0, 100.2, 99.9, 100.05))
    # Bar 23 lands past the 11:00 EOD cutoff.
    bars.append(_bar(dt.datetime(2026, 5, 25, 11, 5), 100.05, 100.2, 99.95, 100.1))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_default_slip"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        eod_time_ist="11:05",
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    # Default total offset = 3.5 bps on price ~100 → 0.035, nearest 0.05-tick
    # rounds the LONG entry to 100.05 (above) and the LONG EOD exit (on a mid
    # of 100.10) to 100.10 - 0.035 → 100.05 nearest tick. Asserting both show
    # the slippage drag.
    assert t["entry_price"] > 100.0  # filled above bar 22's open of 100.0
    assert t["exit_price"] < 100.1  # filled below bar 23's close of 100.1


def test_replay_zero_slippage_matches_mvp_behavior(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """With slippage_bps=0 and half_spread_bps=0 the fills coincide exactly
    with the bar prices the MVP used. Guarantees the default behavior is
    additive — no silent change to the original arithmetic when the model
    is zeroed out.
    """
    from services import backtest_service, scanner_service
    from services.backtest_service import SlippageModel

    @scanner_service.scan_rule("test_buy_zero_slip", "buy", "fires on bar 21")
    def _once(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    entry_bar_ts = start + dt.timedelta(minutes=5 * 21)
    bars.append(_bar(entry_bar_ts, 100.0, 105.0, 99.9, 104.0))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_zero_slip"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=SlippageModel(slippage_bps=0.0, half_spread_bps=0.0),
        scan_cadence_minutes=5,
        entry_confirmation_bars=0,
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    assert t["entry_price"] == pytest.approx(100.0)
    # Hit target on bar 22; exit at exact target price.
    assert t["exit_reason"] == "target"
    assert t["exit_price"] == pytest.approx(t["target_price"])


# ---------------------------------------------------------------------------
# Cadence + watchlist arming tests (Commit 2).
# ---------------------------------------------------------------------------


def test_rule_evaluated_only_at_scan_boundaries(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """A rule that fires only at a non-boundary bar must not arm under
    the default 15-min cadence: it would have armed under the per-bar MVP,
    so a zero-trade outcome is direct evidence the scan was skipped.
    """
    from services import backtest_service, scanner_service

    # Bars start at 09:15. First bar where the history is long enough for
    # rule eval is bar idx=20 (the 21st bar, ts=10:55). That timestamp's
    # minute (655) % 15 = 10, i.e. NOT a scan boundary.
    @scanner_service.scan_rule(
        "test_buy_non_boundary",
        "buy",
        "matches only when history is exactly 21 bars (a non-boundary moment)",
    )
    def _only_at_bar21(bars, indicators):  # noqa: ARG001
        return len(bars) == 21

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=30, price=100.0)
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_non_boundary"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=_zero_slip(),
        # defaults: scan_cadence_minutes=15, entry_confirmation_bars=1.
    )
    trades = backtest_service.get_run_trades(run_id)
    # Rule wanted to fire at bar 21 (non-boundary). With cadence=15m the
    # scan never asks the rule at that bar, so no entry is queued. With
    # cadence=5m the rule would have fired and we'd see one trade.
    assert trades == []


def test_armed_symbol_enters_on_next_confirming_bar(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """Scan at a boundary arms the symbol; an UP bar one step later
    confirms; entry fires on the bar AFTER the confirming bar."""
    from services import backtest_service, scanner_service

    # Bar at ts=11:00 is the first scan boundary post-warmup (history len=22).
    @scanner_service.scan_rule("test_buy_boundary", "buy", "matches at bar 22 (scan boundary)")
    def _at_boundary(bars, indicators):  # noqa: ARG001
        return len(bars) == 22

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    # Bar 22 (idx=21, ts=11:00): flat — irrelevant to the rule which keys
    # off history length.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 21), 100.0, 100.1, 99.9, 100.0))
    # Bar 23 (ts=11:05): clear UP bar — confirms LONG.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 22), 100.0, 100.5, 99.95, 100.3))
    # Bar 24 (ts=11:10): entry fires here at this bar's open.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 23), 100.4, 100.6, 100.3, 100.5))
    # Hold until EOD so the trade closes deterministically.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 24), 100.5, 100.6, 100.4, 100.5))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_boundary"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=_zero_slip(),
        # Defaults: cadence=15, confirmation=1.
        eod_time_ist="11:15",  # bar 25 (ts=11:15) forces EOD exit
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1
    t = trades[0]
    # Entry on bar 24 → ts=11:10, open=100.4.
    assert t["entry_at"].endswith("11:10:00")
    assert t["entry_price"] == pytest.approx(100.4)


def test_armed_symbol_disarms_at_next_scan_if_rule_no_longer_matches(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """A symbol armed at scan T but dropped by the screener at scan T+1
    (cadence step) leaves the watchlist with no entry, even though the
    confirmation window had not yet expired.
    """
    from services import backtest_service, scanner_service

    # Match only at bar 22 (the first scan boundary); after that the
    # screener is silent on this symbol.
    @scanner_service.scan_rule("test_buy_once_then_silent", "buy", "matches exactly at bar 22")
    def _at_22(bars, indicators):  # noqa: ARG001
        return len(bars) == 22

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    # Bar 22 (11:00, scan boundary): rule matches → arm.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 21), 100.0, 100.1, 99.9, 100.0))
    # Bars 23..26: flat-then-down bars that never give a LONG confirmation.
    # Close strictly below open keeps `_confirms_direction` returning False.
    for i in range(22, 27):
        ts = start + dt.timedelta(minutes=5 * i)
        bars.append(_bar(ts, 100.0, 100.05, 99.5, 99.6))
    # Bar 27 (ts=11:30): next 15-min scan boundary. Rule no longer matches
    # (len > 22) so the armed watch should be dropped — no entry possible.
    bars.append(_bar(start + dt.timedelta(minutes=5 * 27), 99.5, 99.7, 99.4, 99.5))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_once_then_silent"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=_zero_slip(),
        # entry_confirmation_bars=10 keeps the symbol armed past the next
        # scan boundary so we can prove the disarm came from the scan
        # re-eval, not from window expiry.
        entry_confirmation_bars=10,
    )
    assert backtest_service.get_run_trades(run_id) == []


def test_already_open_position_not_double_armed(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
    """While a position is open, scan boundaries should not call the rule.

    We assert via a sidechannel call counter rather than via trade count
    because a queued pending_entry during open positions would silently sit
    until eventually voided by EOD — externally indistinguishable.
    """
    from services import backtest_service, scanner_service

    call_log: list[int] = []

    @scanner_service.scan_rule("test_buy_count_calls", "buy", "logs each eval")
    def _count(bars, indicators):  # noqa: ARG001
        call_log.append(len(bars))
        # Match from bar 22 onward — the rule would keep flagging the
        # symbol at every scan if we let it.
        return len(bars) >= 22

    start = dt.datetime(2026, 5, 25, 9, 15)
    bars = _warmup_bars(start, n=21, price=100.0)
    # Bar 22 (11:00, scan boundary): rule matches → arm + immediate pending
    # (conf=0). Bar 23: entry. Subsequent bars hold the position open
    # across the next two scan boundaries (11:15 and 11:30) so we can
    # check that the rule was NOT called at those points.
    for i in range(21, 30):
        ts = start + dt.timedelta(minutes=5 * i)
        # Tight intra-bar range avoids hitting SL/target accidentally.
        bars.append(_bar(ts, 100.0, 100.05, 99.95, 100.0))
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_count_calls"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        slippage_model=_zero_slip(),
        entry_confirmation_bars=0,  # arm → pending → enter next bar
        eod_time_ist="15:30",
    )
    trades = backtest_service.get_run_trades(run_id)
    assert len(trades) == 1  # exactly the bar-23 entry, no re-arming

    # 11:00 is the only scan boundary at which the rule should have been
    # called: at 11:15 and 11:30 the position is still open, so the scan
    # is suppressed. The call at 11:00 records history_len=22.
    assert call_log == [22]


# ---------------------------------------------------------------------------
# Data source + cache tests (Commit 3).
# ---------------------------------------------------------------------------


def _stub_get_history(monkeypatch, payload_fn):
    """Replace ``services.history_service.get_history`` with ``payload_fn``.

    ``payload_fn(**kwargs)`` must return the same 3-tuple shape the real
    function returns: ``(success: bool, payload: dict, status: int)``.
    """
    from services import history_service

    monkeypatch.setattr(history_service, "get_history", payload_fn)


def test_default_data_source_is_api(fresh_backtest_db, monkeypatch):
    from services import backtest_service

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return False, {"data": []}, 404  # no bars → run completes empty

    _stub_get_history(monkeypatch, fake)

    backtest_service.run_backtest(
        symbols=["TEST"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
    )
    assert len(calls) == 1
    assert calls[0]["source"] == "api"


def test_data_source_db_passes_through(fresh_backtest_db, monkeypatch):
    from services import backtest_service

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return False, {"data": []}, 404

    _stub_get_history(monkeypatch, fake)

    backtest_service.run_backtest(
        symbols=["TEST"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        data_source="db",
    )
    assert len(calls) == 1
    assert calls[0]["source"] == "db"


def test_data_source_auto_falls_back_to_api_when_db_empty(fresh_backtest_db, monkeypatch):
    from services import backtest_service

    sources_seen: list[str] = []

    def fake(**kwargs):
        src = kwargs["source"]
        sources_seen.append(src)
        if src == "db":
            return False, {"data": []}, 404
        # source == "api"
        return True, {"data": []}, 200  # success but empty → harmless

    _stub_get_history(monkeypatch, fake)

    backtest_service.run_backtest(
        symbols=["TEST"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        data_source="auto",
    )
    assert sources_seen == ["db", "api"]


def test_in_process_cache_avoids_duplicate_history_calls(fresh_backtest_db, monkeypatch):
    """Symbols can repeat within one run (e.g. duplicates in the input list).

    Without the cache the broker history API would be hit once per
    replay. The in-process cache keys on (symbol, exchange, interval,
    from_date, to_date) so a repeat within the same invocation reuses
    the prior fetch.
    """
    from services import backtest_service

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        return False, {"data": []}, 404

    _stub_get_history(monkeypatch, fake)

    backtest_service.run_backtest(
        symbols=["DUPED", "DUPED", "DUPED"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
    )
    # Three replays of the same symbol/window → exactly one history call.
    assert len(calls) == 1


def test_data_source_invalid_raises(fresh_backtest_db):
    from services import backtest_service

    with pytest.raises(ValueError):
        backtest_service.run_backtest(
            symbols=["TEST"],
            from_date="2026-05-25",
            to_date="2026-05-25",
            interval="5m",
            data_source="not-a-source",
        )
