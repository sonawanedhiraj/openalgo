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
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
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


def _bar(ts: dt.datetime, o: float, h: float, l: float, c: float, v: int = 1000) -> dict:
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": v}


def _warmup_bars(start: dt.datetime, n: int = 22, price: float = 100.0) -> list[dict]:
    """N flat-volume bars walking forward in 5-minute steps."""
    out = []
    for i in range(n):
        out.append(_bar(start + dt.timedelta(minutes=5 * i), price, price + 0.1, price - 0.1, price))
    return out


def _patch_replay(monkeypatch, bars_by_symbol: dict[str, list[dict]]):
    """Patch the bar pipeline to feed ``bars_by_symbol`` directly to the
    replay loop. Bypasses Historify entirely.
    """
    from services import backtest_service

    def fake_fetch(symbol, exchange, from_date, to_date):
        # Returning non-empty so the empty-history short-circuit doesn't trigger.
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
            backtest_service, "_aggregate_to_interval_ohlc",
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
    without coupling the test to fno_intraday_buy_20's exact thresholds.
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


def test_run_backtest_exits_on_stop_loss(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
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


def test_run_backtest_exits_on_target(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
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


def test_run_backtest_eod_squareoff(
    fresh_backtest_db, monkeypatch, isolated_rule_registry
):
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
    bars.append(
        _bar(dt.datetime(2026, 5, 25, 11, 5), 100.0, 100.2, 99.9, 100.1)
    )
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
        bars.append(
            _bar(start + dt.timedelta(minutes=5 * i), 100.0, 100.5, 99.5, 100.0)
        )
    _patch_replay(monkeypatch, {"X": bars})

    run_id = backtest_service.run_backtest(
        rule_names=["test_buy_repeats"],
        symbols=["X"],
        from_date="2026-05-25",
        to_date="2026-05-25",
        interval="5m",
        eod_time_ist="15:30",
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
