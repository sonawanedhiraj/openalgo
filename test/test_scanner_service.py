"""Tests for ``services.scanner_service.ScannerService`` (Stage 1.5 item 5,
commit 2).

The scanner glues together five things:

1. A registry of ``@scan_rule``-decorated callables (commit 1).
2. ``MultiIntervalAggregator`` from ``services.bar_aggregator``.
3. The ZMQ tick bus produced by broker adapters.
4. The ``scan_definitions`` / ``scan_results`` tables.
5. The in-process event bus.

These tests focus on the contract of ``_on_bar_close`` — the seam where
all five concerns meet — and the parsing / lifecycle helpers that surround
it. ZMQ is mocked: we never bind a real socket.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from unittest import mock

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# Force scan rules to self-register before the scanner imports the package.
import services.scan_rules  # noqa: F401
from services import scanner_service
from utils.event_bus import Event

# ---------------------------------------------------------------------------
# Test-local reference rules
#
# The scanner-service contract tests below need a rule whose predicate fires on
# simple synthetic seed data (volume surge + price vs EMA20). The production
# ``fno_intraday_*_chartink`` rules require multi-timeframe daily/weekly frames
# and 200+ daily bars, so we register self-contained test rules here rather than
# coupling these service tests to a production rule's predicate.
#
# These rules are registered fresh on every test via the ``_register_test_rules``
# autouse fixture below — NOT at module import time. The rule registry is a
# module-global dict shared across the whole pytest session, and another suite
# (``test/integration/test_phase2_p0.py``) calls
# ``scanner_service._clear_rule_registry_for_tests()`` in its teardown. Under
# ``pytest -n auto`` (CI), if that suite happens to run earlier in the same
# xdist worker our module-import-time registrations get wiped and the rules
# vanish before our tests evaluate, producing the 7 failures originally seen on
# PR #206 (no scan_results row, no PASS log, etc.). Re-registering per-test
# closes that ordering hole — see issue #207.
# ---------------------------------------------------------------------------


def _test_buy_surge_ema(bars, indicators):
    if len(bars) < 21:
        return False
    vol_avg = bars["volume"].rolling(20).mean().iloc[-2]
    if pd.isna(vol_avg) or vol_avg <= 0:
        return False
    if bars["volume"].iloc[-1] / vol_avg < 2.0:
        return False
    ema20 = indicators.get("ema_20")
    if ema20 is None or len(ema20) == 0 or pd.isna(ema20.iloc[-1]):
        return False
    return bool(bars["close"].iloc[-1] > ema20.iloc[-1])


def _test_sell_surge_ema(bars, indicators):
    if len(bars) < 21:
        return False
    vol_avg = bars["volume"].rolling(20).mean().iloc[-2]
    if pd.isna(vol_avg) or vol_avg <= 0:
        return False
    if bars["volume"].iloc[-1] / vol_avg < 2.0:
        return False
    ema20 = indicators.get("ema_20")
    if ema20 is None or len(ema20) == 0 or pd.isna(ema20.iloc[-1]):
        return False
    return bool(bars["close"].iloc[-1] < ema20.iloc[-1])


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_scanner_db(monkeypatch):
    """Point ``database.scanner_db`` at a clean in-memory SQLite for one test.

    Mirrors the fixture in ``test_scanner_db.py`` — kept inline (rather
    than promoted to conftest) so the file stays self-contained.
    """
    from database import scanner_db as sdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)

    scanner_service.init_scanner_db()
    yield sdb

    test_session.remove()
    test_engine.dispose()


@pytest.fixture(autouse=True)
def _register_test_rules():
    """Re-register the two test-only rules before every test.

    The rule registry is a module-global dict (``scanner_service._rule_registry``)
    shared across the whole pytest session. Another suite —
    ``test/integration/test_phase2_p0.py`` — calls
    ``scanner_service._clear_rule_registry_for_tests()`` in its teardown to
    wipe its own ``_p0_always_true`` rule, which under ``pytest -n auto`` also
    wipes the rules a previously-loaded ``test_scanner_service`` module
    registered at import time. Registering per-test makes this file's contract
    robust to that ordering hazard (issue #207).
    """
    scanner_service.scan_rule(
        "_test_buy_surge_ema",
        "buy",
        "test-only: vol surge >=2x AND close above EMA20",
    )(_test_buy_surge_ema)
    scanner_service.scan_rule(
        "_test_sell_surge_ema",
        "sell",
        "test-only: vol surge >=2x AND close below EMA20",
    )(_test_sell_surge_ema)
    yield


@pytest.fixture(autouse=True)
def _pin_market_hours(monkeypatch):
    """Pin the scanner's IST clock to 11:00 (mid-session) for every test.

    Tier-1 Fix #1 added a market-hours gate to ``_evaluate_definitions`` that
    skips evaluation outside [09:15, 15:30] IST. Without this pin, the
    long-standing ``_on_bar_close`` fire/no-fire tests would pass or fail
    depending on the wall-clock time the suite happens to run. The gate-specific
    tests below override ``_now_ist`` to drive the post-close / pre-open paths.
    """
    monkeypatch.setattr(
        scanner_service,
        "_now_ist",
        lambda: scanner_service._IST.localize(dt.datetime(2026, 5, 30, 11, 0)),
    )


class _CapturingBus:
    """Synchronous stand-in for ``EventBus`` so tests can assert on emitted events.

    The real bus dispatches to a thread pool, which would make tests flaky
    without explicit waits. This stub records ``publish`` calls in-order.
    """

    def __init__(self) -> None:
        self.events: list[Event] = []

    def publish(self, event: Event) -> None:
        self.events.append(event)


def _make_bars(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    """Build a small OHLCV frame for direct rule evaluation."""
    assert len(closes) == len(volumes)
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1.0 for c in closes],
            "low": [c - 1.0 for c in closes],
            "close": closes,
            "volume": volumes,
        }
    )


def _seed_history(
    svc: scanner_service.ScannerService,
    symbol: str,
    interval: str,
    closes: list[float],
    volumes: list[float],
) -> None:
    """Pre-populate the scanner's per-symbol rolling frame so the next bar
    close has enough history for the 21-bar minimum the example rules need.
    """
    bars = _make_bars(closes, volumes)
    bars.insert(
        0,
        "ts",
        [dt.datetime(2026, 5, 30, 9, 15) + dt.timedelta(minutes=5 * i) for i in range(len(bars))],
    )
    svc._bar_history[(symbol, interval)] = bars


# ---------------------------------------------------------------------------
# topic / tick parsing
# ---------------------------------------------------------------------------


def test_parse_topic_extracts_exchange_symbol_mode():
    assert scanner_service._parse_topic("NSE_RELIANCE_QUOTE") == ("NSE", "RELIANCE", "QUOTE")
    assert scanner_service._parse_topic("NFO_BANKNIFTY24APR24FUT_LTP") == (
        "NFO",
        "BANKNIFTY24APR24FUT",
        "LTP",
    )


def test_parse_topic_handles_multi_segment_index_exchange():
    assert scanner_service._parse_topic("NSE_INDEX_NIFTY_LTP") == ("NSE_INDEX", "NIFTY", "LTP")
    assert scanner_service._parse_topic("BSE_INDEX_SENSEX_QUOTE") == (
        "BSE_INDEX",
        "SENSEX",
        "QUOTE",
    )


def test_parse_topic_skips_cache_and_account_events():
    assert scanner_service._parse_topic("CACHE_INVALIDATE_user_42") is None
    assert scanner_service._parse_topic("NSE_RELIANCE_orders") is None
    assert scanner_service._parse_topic("NSE_RELIANCE_positions") is None
    assert scanner_service._parse_topic("NSE_RELIANCE_margins") is None
    assert scanner_service._parse_topic("BROKEN") is None
    assert scanner_service._parse_topic("") is None


def test_normalize_tick_extracts_price_volume_and_ms_timestamp():
    out = scanner_service._normalize_tick(
        {
            "ltp": "2451.25",
            "volume": "1500000",
            "timestamp": 1748580900000,  # epoch ms — well above 10^10
        }
    )
    assert out is not None
    assert out["price"] == 2451.25
    assert out["cumulative_volume"] == 1500000
    assert isinstance(out["ts"], dt.datetime)


def test_normalize_tick_falls_back_to_now_when_timestamp_missing():
    out = scanner_service._normalize_tick({"ltp": 100.0})
    assert out is not None
    assert out["cumulative_volume"] == 0  # default
    assert isinstance(out["ts"], dt.datetime)


def test_normalize_tick_returns_none_without_price():
    assert scanner_service._normalize_tick({"volume": 100, "timestamp": 0}) is None
    assert scanner_service._normalize_tick({}) is None


# ---------------------------------------------------------------------------
# _on_bar_close — the main contract
# ---------------------------------------------------------------------------


def _enable_buy_definition(name: str = "_test_buy_surge_ema") -> int:
    """Insert an enabled BUY scan_definition that points at the example rule."""
    return scanner_service.create_scan_definition(
        name=name,
        screener_type="buy",
        expression_json=None,
        rule_module="_test_buy_surge_ema",  # name registered by the rule module
        enabled=True,
    )


def test_on_bar_close_writes_result_when_rule_fires(fresh_scanner_db):
    """A matching bar should produce one scan_results row + one scan_hit event."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)

    def_id = _enable_buy_definition()

    # 20-bar rising history with steady volume — last bar is the "spike" we
    # feed via _on_bar_close. The seeded history is bars[0..19]; _on_bar_close
    # appends bar[20] which is the surge.
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)

    matching_bar = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,  # well above the trailing EMA
        "volume": 5000,  # 5× the 1000 baseline
        "elapsed_pct": 1.0,
    }

    svc._on_bar_close("RELIANCE", "5m", matching_bar)

    rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    assert len(rows) == 1
    assert rows[0]["scan_definition_id"] == def_id
    assert rows[0]["symbols"] == ["RELIANCE"]
    assert rows[0]["source"] == "inhouse"

    assert len(capturing_bus.events) == 1
    event = capturing_bus.events[0]
    assert isinstance(event, scanner_service.ScanHitEvent)
    assert event.topic == "scan_hit"
    assert event.symbol == "RELIANCE"
    assert event.interval == "5m"
    assert event.scan_definition_id == def_id
    assert event.screener_type == "buy"
    assert event.bar["close"] == 150.0


def test_on_bar_close_skips_when_rule_does_not_fire(fresh_scanner_db):
    """A bar that doesn't match the rule should produce no row + no event."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)

    _enable_buy_definition()

    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)

    # Same volume as the trailing baseline ⇒ no surge ⇒ no fire.
    non_matching_bar = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 1000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", non_matching_bar)

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


def test_on_bar_close_evaluates_all_enabled_definitions(fresh_scanner_db):
    """Both enabled definitions should be evaluated; matching ones fire."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)

    buy_id = scanner_service.create_scan_definition(
        name="buy_def",
        screener_type="buy",
        expression_json=None,
        rule_module="_test_buy_surge_ema",
        enabled=True,
    )
    sell_id = scanner_service.create_scan_definition(
        name="sell_def",
        screener_type="sell",
        expression_json=None,
        rule_module="_test_sell_surge_ema",
        enabled=True,
    )

    # Rising history ⇒ the BUY rule will match when we feed a volume-surge
    # bar that closes above the EMA; the SELL rule will not (close > EMA).
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)

    surge_above_ema = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 5000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", surge_above_ema)

    rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    # Only the BUY definition matches — proves both were evaluated, only
    # one produced a hit. (If the SELL rule had not been evaluated at all,
    # we could not tell the difference; the symmetric counter-test below
    # closes that gap.)
    assert {r["scan_definition_id"] for r in rows} == {buy_id}
    assert sell_id not in {r["scan_definition_id"] for r in rows}
    assert len(capturing_bus.events) == 1
    assert capturing_bus.events[0].scan_definition_id == buy_id


def test_on_bar_close_skips_disabled_definitions(fresh_scanner_db):
    """A disabled definition matching the bar should NOT produce a row."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)

    # Disabled BUY definition — would have matched the surge bar if enabled.
    scanner_service.create_scan_definition(
        name="buy_disabled",
        screener_type="buy",
        expression_json=None,
        rule_module="_test_buy_surge_ema",
        enabled=False,
    )

    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    surge = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 5000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", surge)

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


def test_on_bar_close_skips_definition_with_unregistered_rule(fresh_scanner_db):
    """A definition referencing an unknown rule name should be skipped quietly."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)

    scanner_service.create_scan_definition(
        name="phantom",
        screener_type="buy",
        expression_json=None,
        rule_module="does_not_exist_anywhere",
        enabled=True,
    )

    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    svc._on_bar_close(
        "RELIANCE",
        "5m",
        {
            "ts": dt.datetime.now(),
            "open": 1,
            "high": 1,
            "low": 1,
            "close": 1,
            "volume": 1,
            "elapsed_pct": 1.0,
        },
    )

    # No crash, no row, no event.
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


# ---------------------------------------------------------------------------
# _build_indicators
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Stand-in for ScannerHistoryProvider returning canned daily/weekly frames."""

    def __init__(self, daily=None, weekly=None):
        self._daily = daily
        self._weekly = weekly

    def get_daily(self, symbol):  # noqa: ARG002 — fixed canned frame
        return self._daily

    def get_weekly(self, symbol):  # noqa: ARG002
        return self._weekly


def test_indicators_dict_populated_with_expected_keys():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    svc._history_provider = _FakeProvider()  # avoid DuckDB lazy-load in tests
    bars = _make_bars(
        closes=[100.0 + i * 0.5 for i in range(30)],
        volumes=[1000.0] * 30,
    )
    result = svc._build_indicators("RELIANCE", bars)
    # Backward-compat indicator keys, the four Task-4 multi-timeframe keys, plus
    # the ``symbol`` key (Tier-1 Fix #1/#2 — rules name the symbol in their logs).
    # Issue #158 D2 added the ``exchange`` key so F&O rules can skip indices.
    assert set(result.keys()) == {
        "symbol",
        "exchange",
        "ema_20",
        "atr_14",
        "rsi_14",
        "volume_avg_20",
        "bars_5m",
        "bars_15m",
        "bars_daily",
        "bars_weekly",
    }
    assert result["symbol"] == "RELIANCE"
    # The four legacy series remain Series of the same length as bars.
    for name in ("ema_20", "atr_14", "rsi_14", "volume_avg_20"):
        series = result[name]
        assert series is not None, f"{name} unexpectedly None"
        assert len(series) == len(bars)
    # bars_5m is the same frame passed in.
    assert result["bars_5m"] is bars


def test_build_indicators_daily_weekly_none_when_provider_returns_none():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    svc._history_provider = _FakeProvider(daily=None, weekly=None)
    bars = _make_bars(closes=[100.0] * 5, volumes=[1000.0] * 5)
    result = svc._build_indicators("RELIANCE", bars)
    assert result["bars_daily"] is None
    assert result["bars_weekly"] is None


def test_build_indicators_passes_through_daily_weekly_frames():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    daily = _make_bars(closes=[100.0] * 3, volumes=[1.0] * 3)
    weekly = _make_bars(closes=[200.0] * 2, volumes=[2.0] * 2)
    svc._history_provider = _FakeProvider(daily=daily, weekly=weekly)
    bars = _make_bars(closes=[100.0] * 5, volumes=[1000.0] * 5)
    result = svc._build_indicators("RELIANCE", bars)
    assert result["bars_daily"] is daily
    assert result["bars_weekly"] is weekly


def test_build_indicators_bars_15m_empty_when_no_history_yet():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    svc._history_provider = _FakeProvider()
    bars = _make_bars(closes=[100.0] * 5, volumes=[1000.0] * 5)
    result = svc._build_indicators("RELIANCE", bars)
    bars_15m = result["bars_15m"]
    # A tracked symbol with no closed 15m bar yet → empty DataFrame (not None).
    assert isinstance(bars_15m, pd.DataFrame)
    assert bars_15m.empty


def test_15m_bars_built_from_tick_stream():
    """Ticks crossing 15-minute boundaries should close 15m bars that
    ``_build_indicators`` then surfaces as a rolling frame."""
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    svc._history_provider = _FakeProvider()

    base = dt.datetime(2026, 5, 30, 9, 15)
    # Three distinct 15m buckets (09:15, 09:30, 09:45) — two closes expected
    # by the time we reach the third bucket. One tick per 5 minutes.
    cum_vol = 0
    for i in range(7):  # 09:15 .. 09:45 in 5-min steps
        ts = base + dt.timedelta(minutes=5 * i)
        cum_vol += 100
        payload = json.dumps(
            {
                "ltp": 100.0 + i,
                "volume": cum_vol,
                "timestamp": int(ts.timestamp()),
            }
        )
        svc._ingest_message("NSE_RELIANCE_QUOTE", payload)

    bars = _make_bars(closes=[100.0] * 5, volumes=[1000.0] * 5)
    result = svc._build_indicators("RELIANCE", bars)
    bars_15m = result["bars_15m"]
    assert isinstance(bars_15m, pd.DataFrame)
    # 09:15 and 09:30 buckets have closed; 09:45 is still in progress.
    assert len(bars_15m) == 2
    assert {"ts", "open", "high", "low", "close", "volume"}.issubset(bars_15m.columns)


def test_15m_bars_capped_at_50():
    """The per-symbol 15m accumulator must bound memory to ~50 bars."""
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    builder = svc._bar_15m_history["RELIANCE"]
    base = dt.datetime(2026, 5, 30, 9, 15)
    # Feed 120 distinct 15m buckets (one tick each → forces a close on the next).
    for i in range(120):
        ts = base + dt.timedelta(minutes=15 * i)
        builder.on_tick({"price": 100.0 + i, "cumulative_volume": 100 * i, "ts": ts})
    frame = builder.get_recent_bars(50)
    assert len(frame) <= 50


def test_existing_rule_still_evaluates_against_new_indicators_dict():
    """Regression: ``_test_buy_surge_ema`` reads only ``bars`` + ``ema_20`` and
    must still evaluate cleanly against the enriched 8-key indicators dict."""
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    svc._history_provider = _FakeProvider()
    rule = scanner_service.get_rule("_test_buy_surge_ema")
    assert rule is not None

    # 20 baseline bars + a final volume-surge bar above the EMA → should fire.
    closes = [100.0 + i * 0.5 for i in range(20)] + [150.0]
    volumes = [1000.0] * 20 + [5000.0]
    bars = _make_bars(closes, volumes)
    indicators = svc._build_indicators("RELIANCE", bars)
    assert rule(bars, indicators) is True

    # No surge → rule returns False, still no exception.
    flat = _make_bars([100.0] * 21, [1000.0] * 21)
    assert rule(flat, svc._build_indicators("RELIANCE", flat)) is False


def test_history_rolls_off_old_bars():
    """Bar history must cap at ``history_size`` to keep the window small."""
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus(), history_size=5)
    base_ts = dt.datetime(2026, 5, 30, 9, 15)
    for i in range(10):
        svc._append_bar(
            "RELIANCE",
            "5m",
            {
                "ts": base_ts + dt.timedelta(minutes=5 * i),
                "open": float(i),
                "high": float(i + 1),
                "low": float(i - 1),
                "close": float(i),
                "volume": 100 + i,
            },
        )
    frame = svc._bar_history[("RELIANCE", "5m")]
    assert len(frame) == 5
    assert list(frame["close"]) == [5.0, 6.0, 7.0, 8.0, 9.0]


# ---------------------------------------------------------------------------
# _ingest_message
# ---------------------------------------------------------------------------


def test_ingest_message_routes_known_symbol_to_aggregator():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    with mock.patch.object(svc.aggregator, "on_tick") as on_tick:
        svc._ingest_message(
            "NSE_RELIANCE_QUOTE",
            json.dumps({"ltp": 2500.0, "volume": 1000, "timestamp": 1748580900}),
        )
    on_tick.assert_called_once()
    args, _ = on_tick.call_args
    assert args[0] == "RELIANCE"
    assert args[1]["price"] == 2500.0
    assert args[1]["cumulative_volume"] == 1000


def test_ingest_message_ignores_unknown_symbol():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    with mock.patch.object(svc.aggregator, "on_tick") as on_tick:
        svc._ingest_message(
            "NSE_INFY_QUOTE",
            json.dumps({"ltp": 1500.0, "volume": 1000, "timestamp": 1748580900}),
        )
    on_tick.assert_not_called()


def test_ingest_message_swallows_bad_json():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    with mock.patch.object(svc.aggregator, "on_tick") as on_tick:
        svc._ingest_message("NSE_RELIANCE_QUOTE", "{not valid json")
    on_tick.assert_not_called()


# ---------------------------------------------------------------------------
# start / stop
# ---------------------------------------------------------------------------


def test_scanner_lifecycle_start_stop():
    """``start`` spawns the subscriber thread; ``stop`` joins it.

    We replace the real ``_run_subscriber`` with a stub so the test does
    not depend on the loopback ZMQ port being free. The lifecycle bits
    we care about are: flag flipping, thread spawning, clean join.
    """
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())

    started = []

    def _stub_run():
        started.append(True)
        while not svc._stop_event.is_set():
            time.sleep(0.01)

    with mock.patch.object(svc, "_run_subscriber", side_effect=_stub_run):
        assert svc.running() is False
        svc.start()
        try:
            assert svc.running() is True
            # Re-calling start is a no-op (no second thread spawned).
            svc.start()
        finally:
            svc.stop()
        assert svc.running() is False
        assert started == [True]  # exactly one invocation


def test_scanner_start_idempotent_until_stopped():
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())

    def _stub_run():
        while not svc._stop_event.is_set():
            time.sleep(0.01)

    with mock.patch.object(svc, "_run_subscriber", side_effect=_stub_run):
        svc.start()
        first_thread = svc._subscriber_thread
        svc.start()
        assert svc._subscriber_thread is first_thread
        svc.stop()


# ---------------------------------------------------------------------------
# Tier-1 Fix #1 — market-hours gate on evaluation
# ---------------------------------------------------------------------------


def _seed_matching_buy(svc):
    """Seed RELIANCE history + return a surge bar that fires ``_test_buy_surge_ema``."""
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    return {
        "ts": dt.datetime(2026, 5, 30, 16, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 5000,
        "elapsed_pct": 1.0,
    }


def _pin_now(monkeypatch, hour, minute=0):
    monkeypatch.setattr(
        scanner_service,
        "_now_ist",
        lambda: scanner_service._IST.localize(dt.datetime(2026, 5, 30, hour, minute)),
    )


def test_scanner_skips_evaluation_after_market_close(fresh_scanner_db, monkeypatch, caplog):
    """A bar that closes at 16:00 IST must be skipped (post-close) with a DEBUG log.

    The per-bar skip line is DEBUG (issue #256) — it fires per bar per symbol,
    so at INFO a restart floods the log. The skip *behavior* (no row, no event)
    is the load-bearing assertion; the log level is verified at DEBUG.
    """
    import logging

    _pin_now(monkeypatch, 16, 0)
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)

    with caplog.at_level(logging.DEBUG, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", bar)

    # No evaluation happened → no row, no event.
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []
    assert any(
        "scanner evaluation skipped" in r.message and "post-close" in r.message
        for r in caplog.records
    )


def test_scanner_skips_evaluation_before_market_open(fresh_scanner_db, monkeypatch, caplog):
    """A bar that closes at 08:00 IST must be skipped (pre-open) with a DEBUG log."""
    import logging

    _pin_now(monkeypatch, 8, 0)
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)

    with caplog.at_level(logging.DEBUG, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", bar)

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []
    assert any(
        "scanner evaluation skipped" in r.message and "pre-open" in r.message
        for r in caplog.records
    )


def test_scanner_evaluates_during_market_hours(fresh_scanner_db, monkeypatch):
    """Inside [09:15, 15:30] IST the same surge bar fires normally."""
    _pin_now(monkeypatch, 11, 0)
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)

    svc._on_bar_close("RELIANCE", "5m", bar)

    assert len(scanner_service.get_scan_results(hours=24, source="inhouse")) == 1
    assert len(capturing_bus.events) == 1


def test_postclose_gate_disabled_allows_post_close_evaluation(fresh_scanner_db, monkeypatch):
    """With SCANNER_POSTCLOSE_GATE_ENABLED=false the gate is a no-op (legacy path)."""
    _pin_now(monkeypatch, 16, 0)
    monkeypatch.setenv("SCANNER_POSTCLOSE_GATE_ENABLED", "false")
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)

    svc._on_bar_close("RELIANCE", "5m", bar)

    # Gate disabled → evaluation proceeds even post-close.
    assert len(scanner_service.get_scan_results(hours=24, source="inhouse")) == 1


def test_within_market_hours_boundaries():
    """The window is inclusive of 09:15 and 15:30, exclusive outside."""
    mk = lambda h, m: scanner_service._IST.localize(dt.datetime(2026, 5, 30, h, m))  # noqa: E731
    assert scanner_service._within_market_hours(mk(9, 15)) is True
    assert scanner_service._within_market_hours(mk(15, 30)) is True
    assert scanner_service._within_market_hours(mk(9, 14)) is False
    assert scanner_service._within_market_hours(mk(15, 31)) is False
    assert scanner_service._within_market_hours(mk(11, 0)) is True


# ---------------------------------------------------------------------------
# Tier-1 Fix #2 — loud per-symbol PASS/FAIL + missing-input logging
# ---------------------------------------------------------------------------


def test_scanner_logs_pass_for_qualifying_symbol(fresh_scanner_db, caplog):
    """A matching symbol emits an INFO 'scanner PASS <sym>' line."""
    import logging

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)  # default pinned clock is 11:00 (in-hours)

    with caplog.at_level(logging.INFO, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", bar)

    assert any(
        "scanner PASS RELIANCE" in r.message and r.levelno == logging.INFO for r in caplog.records
    )


def test_scanner_logs_fail_with_reason_for_disqualifying_symbol(fresh_scanner_db, caplog):
    """A non-matching symbol emits a DEBUG 'scanner FAIL <sym>' line (kept at DEBUG
    so the per-bar no-match firehose does not flood the log)."""
    import logging

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    non_matching = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 1000,  # no surge → no match
        "elapsed_pct": 1.0,
    }

    with caplog.at_level(logging.DEBUG, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", non_matching)

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert any("scanner FAIL RELIANCE" in r.message for r in caplog.records)


def test_get_today_ohlcv_logs_reason_when_no_bars(caplog):
    """A symbol with no bars today logs a reason instead of a silent (None, None)."""
    import logging

    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    with caplog.at_level(logging.DEBUG, logger="services.scanner_service"):
        close, vol = svc.get_today_ohlcv("RELIANCE", dt.date(2026, 5, 30))

    assert (close, vol) == (None, None)
    assert any("no live bars for RELIANCE" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Tier-1 Fix #3 — decision-input completeness metric
# ---------------------------------------------------------------------------


def _ist(hour, minute=0):
    return scanner_service._IST.localize(dt.datetime(2026, 5, 30, hour, minute))


def test_completeness_metric_emitted_per_cycle(caplog):
    """When the rolling window elapses, n_live/total is logged for the PRIOR
    window (the triggering bar belongs to the next window)."""
    import logging

    svc = scanner_service.ScannerService(symbols=["A", "B", "C", "D"], bus=_CapturingBus())
    t0 = _ist(11, 0)
    svc._record_completeness("A", t0)
    svc._record_completeness("B", t0)  # 2 of 4 live this window
    with caplog.at_level(logging.INFO, logger="services.scanner_service"):
        svc._record_completeness("C", t0 + dt.timedelta(minutes=6))  # rolls the window
    assert any("decision-input completeness: 2/4 (50%)" in r.message for r in caplog.records)


def test_completeness_below_50pct_triggers_warning_telegram():
    """A live fraction under the WARN threshold (default 50%) sends a WARNING."""
    sent = []
    svc = scanner_service.ScannerService(
        symbols=list("ABCDEFGHIJ"), bus=_CapturingBus(), notifier=sent.append
    )
    # 4 of 10 (40%) live → below 50% WARN, above 20% CRIT.
    for s in "ABCD":
        svc._completeness_window_syms.add(s)
    svc._emit_completeness(_ist(11, 0))
    assert len(sent) == 1
    assert "WARNING" in sent[0] and "4/10" in sent[0]


def test_completeness_below_20pct_triggers_critical_telegram():
    """A live fraction under the CRIT threshold (default 20%) sends a CRITICAL."""
    sent = []
    svc = scanner_service.ScannerService(
        symbols=list("ABCDEFGHIJ"), bus=_CapturingBus(), notifier=sent.append
    )
    svc._completeness_window_syms.add("A")  # 1 of 10 (10%) → CRITICAL
    svc._emit_completeness(_ist(11, 0))
    assert len(sent) == 1
    assert "CRITICAL" in sent[0] and "1/10" in sent[0]


def test_completeness_full_coverage_is_quiet():
    """Full coverage (no degradation) logs the metric but sends no alert —
    distinguishes a genuinely quiet market from a starved feed."""
    sent = []
    svc = scanner_service.ScannerService(
        symbols=list("ABCD"), bus=_CapturingBus(), notifier=sent.append
    )
    for s in "ABCD":
        svc._completeness_window_syms.add(s)  # 100% live
    svc._emit_completeness(_ist(11, 0))
    assert sent == []


def test_completeness_dedup_one_alert_per_day():
    """The same severity alerts at most once per day; a new day re-arms it."""
    sent = []
    svc = scanner_service.ScannerService(
        symbols=list("ABCDEFGHIJ"), bus=_CapturingBus(), notifier=sent.append
    )

    def _degraded_emit(at):
        svc._completeness_window_syms = {"A", "B", "C", "D"}  # 40% → WARNING
        svc._emit_completeness(at)

    _degraded_emit(_ist(11, 0))
    _degraded_emit(_ist(11, 30))  # same day, same severity → deduped
    assert len(sent) == 1
    _degraded_emit(_ist(11, 0) + dt.timedelta(days=1))  # next day → re-armed
    assert len(sent) == 2


def test_completeness_disabled_does_not_record(monkeypatch, fresh_scanner_db):
    """SCANNER_COMPLETENESS_ENABLED=false → no window accumulation."""
    monkeypatch.setenv("SCANNER_COMPLETENESS_ENABLED", "false")
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=_CapturingBus())
    _enable_buy_definition()
    bar = _seed_matching_buy(svc)
    svc._on_bar_close("RELIANCE", "5m", bar)
    assert svc._completeness_window_syms == set()


# ---------------------------------------------------------------------------
# Chunk C — rule parameterisation via indicators["parameters"]
# ---------------------------------------------------------------------------


def test_evaluate_definitions_injects_params_into_indicators(fresh_scanner_db):
    """A definition with parameters_json passes a 'parameters' key to the rule."""
    captured = {}

    @scanner_service.scan_rule(
        "_test_capture_params", "buy", "test-only: captures the indicators dict"
    )
    def _capture_params(bars, indicators):
        captured["parameters"] = indicators.get("parameters")
        return False  # never fires a hit; we only care about what is passed in

    scanner_service.create_scan_definition(
        name="parameterised_def",
        screener_type="buy",
        expression_json=None,
        rule_module="_test_capture_params",
        enabled=True,
        parameters_json='{"gap_pct": 1.5}',
    )

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    bar = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 100.0,
        "volume": 1000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", bar)

    assert captured.get("parameters") == {"gap_pct": 1.5}


def test_evaluate_definitions_no_params_key_when_parameters_json_empty(fresh_scanner_db):
    """A definition without parameters_json reuses indicators_dict unchanged (no 'parameters' key)."""
    captured = {}

    @scanner_service.scan_rule(
        "_test_capture_no_params", "buy", "test-only: captures indicators when no params"
    )
    def _capture_no_params(bars, indicators):
        captured["has_parameters"] = "parameters" in indicators
        return False

    scanner_service.create_scan_definition(
        name="unparameterised_def",
        screener_type="buy",
        expression_json=None,
        rule_module="_test_capture_no_params",
        enabled=True,
        parameters_json=None,
    )

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)
    bar = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 100.0,
        "volume": 1000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", bar)

    assert captured.get("has_parameters") is False


# ---------------------------------------------------------------------------
# Issue #256 — replayed bars warm rolling state WITHOUT firing signals
#
# The boot seeder (scanner_aggregator_seeder) and WS-reconnect recovery
# (ws_recovery_service) both fold historical 1m bars into the live aggregator
# via MultiIntervalAggregator.replay_bars to warm RSI/SMA windows. Those bars
# are historical: evaluating them (or firing a ScanHitEvent) is wrong. A
# mid-session restart replays them DURING market hours where the market-hours
# gate would NOT skip them, so the fix must protect the path itself — not lean
# on the gate. Only genuine LIVE bar closes are evaluated.
# ---------------------------------------------------------------------------


def _replay_ohlcv_bars(
    n: int,
    start: dt.datetime = dt.datetime(2026, 5, 30, 9, 15),
) -> list[dict]:
    """N discrete 1m OHLCV bars (the shape replay_bars expects — not ticks)."""
    return [
        {
            "ts": start + dt.timedelta(minutes=i),
            "open": 100.0 + i * 0.5,
            "high": 101.0 + i * 0.5,
            "low": 99.0 + i * 0.5,
            "close": 100.0 + i * 0.5,
            "volume": 1000,
        }
        for i in range(n)
    ]


def test_replay_bars_warms_history_but_does_not_evaluate(fresh_scanner_db):
    """replay_bars folds bars into the rolling window (indicator warm-up) but
    fires NO scan-evaluation: no scan_results row, no ScanHitEvent."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()

    # 40 one-minute bars → 8 closed 5m buckets folded via the real aggregator
    # callback path (aggregator.on_bar_close == svc._on_bar_close).
    folded = svc.aggregator.replay_bars("RELIANCE", _replay_ohlcv_bars(40))
    assert folded == 40

    # The rolling 5m history window warmed (indicators can be computed later).
    warmed = svc._bar_history.get(("RELIANCE", "5m"))
    assert warmed is not None and len(warmed) >= 7

    # But NOTHING was evaluated: no results, no events.
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


def test_live_bar_after_replay_still_evaluates(fresh_scanner_db):
    """The live path is preserved: a genuine live bar close after a replay
    warm-up evaluates exactly as before (fires the rule → row + event)."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    def_id = _enable_buy_definition()

    # Warm the window via replay (no eval); assert the replay itself is silent.
    svc.aggregator.replay_bars("RELIANCE", _replay_ohlcv_bars(40))
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []

    # Seed enough rising history for the rule's 21-bar minimum (the replayed 5m
    # buckets alone are too few) so the live surge below can actually match —
    # this test is about the live path still evaluating, not warm-up depth.
    closes = [100.0 + i * 0.5 for i in range(20)]
    volumes = [1000.0] * 20
    _seed_history(svc, "RELIANCE", "5m", closes, volumes)

    # A live bar carries no is_replay flag (default False) — full evaluation.
    live_surge = {
        "ts": dt.datetime(2026, 5, 30, 11, 0),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,
        "volume": 5000,
        "elapsed_pct": 1.0,
    }
    svc._on_bar_close("RELIANCE", "5m", live_surge)

    rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    assert len(rows) == 1
    assert rows[0]["scan_definition_id"] == def_id
    assert len(capturing_bus.events) == 1
    assert isinstance(capturing_bus.events[0], scanner_service.ScanHitEvent)


def test_replay_during_market_hours_still_does_not_fire(fresh_scanner_db, monkeypatch):
    """A mid-session restart replays historical bars DURING market hours, where
    the _evaluate_definitions market-hours gate does NOT skip. Proves the fix
    (is_replay suppression) — not the gate — protects the mid-session path."""
    # 11:00 IST is squarely inside [09:15, 15:30] — the gate would let a live
    # bar through here.
    _pin_now(monkeypatch, 11, 0)
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()

    # Seed a rising history and replay a would-be surge bar as a HISTORICAL bar.
    # If replayed bars were evaluated, this in-hours surge would fire a hit.
    warm = _replay_ohlcv_bars(100)  # 20 closed 5m buckets — plenty of history
    # Make the final replayed 1m bar a volume+price surge that WOULD match the
    # buy rule if it were ever evaluated as a live bar.
    warm[-1] = {
        "ts": warm[-1]["ts"],
        "open": 150.0,
        "high": 151.0,
        "low": 149.0,
        "close": 200.0,
        "volume": 500000,
    }
    svc.aggregator.replay_bars("RELIANCE", warm)

    # In-hours, but replayed → still no evaluation, no signal.
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


def test_ws_recovery_replay_warms_without_firing(fresh_scanner_db):
    """The ws_recovery path (MultiIntervalAggregator.replay_bars over the live
    scanner aggregator) updates aggregator + history state without firing
    signals — the same guarantee as the boot seeder."""
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()

    # ws_recovery calls aggregator.replay_bars(symbol, bars) exactly like this.
    n = svc.aggregator.replay_bars("RELIANCE", _replay_ohlcv_bars(30))
    assert n == 30

    # Aggregator's in-progress bucket advanced (state updated) and history warmed.
    assert svc.aggregator.current_bar("RELIANCE", "5m") is not None
    assert svc._bar_history.get(("RELIANCE", "5m")) is not None

    # No signals fired.
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []


def test_replay_bars_snapshot_carries_is_replay_flag():
    """Unit-level guarantee: a bar closed via BarBuilder.replay_bars carries
    is_replay=True, while a live tick close carries is_replay=False. This is the
    single flag _on_bar_close keys off to suppress evaluation."""
    from services.bar_aggregator import BarBuilder

    replay_closes: list[dict] = []
    b = BarBuilder("INFY", "1m", on_bar=lambda bar: replay_closes.append(bar))
    # Two 1m bars in different buckets → the first bucket closes on the second.
    b.replay_bars(
        [
            {
                "ts": dt.datetime(2026, 5, 30, 9, 15),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 10,
            },
            {
                "ts": dt.datetime(2026, 5, 30, 9, 16),
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 20,
            },
        ]
    )
    assert replay_closes and all(bar["is_replay"] is True for bar in replay_closes)

    live_closes: list[dict] = []
    lb = BarBuilder("INFY", "1m", on_bar=lambda bar: live_closes.append(bar))
    lb.on_tick({"ts": dt.datetime(2026, 5, 30, 9, 15, 0), "price": 1.0, "cumulative_volume": 10})
    lb.on_tick({"ts": dt.datetime(2026, 5, 30, 9, 16, 0), "price": 2.0, "cumulative_volume": 20})
    closes = [bar for bar in live_closes if bar["elapsed_pct"] >= 1.0]
    assert closes and all(bar["is_replay"] is False for bar in closes)


# ---------------------------------------------------------------------------
# Issue #305 — smoke-fail post-hold: a failed 09:18 smoke check blocks hit
# persistence/posting (PASS still logged); release/expiry restores posting.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_smoke_hold():
    """Keep the module-level post-hold state clean so an armed hold from one
    test (or another test file) never bleeds into the fire/no-fire contracts
    above."""
    from services import scanner_smoke_check_service as smoke

    smoke._reset_hold_for_tests()
    yield
    smoke._reset_hold_for_tests()


# The autouse ``_pin_market_hours`` pins the scanner clock to 2026-05-30 11:00
# IST — the hold must be armed for that same day to be consulted as active.
_PINNED_DAY = dt.date(2026, 5, 30)


def _matching_bar(minute=0):
    return {
        "ts": dt.datetime(2026, 5, 30, 11, minute),
        "open": 110.0,
        "high": 111.5,
        "low": 109.5,
        "close": 150.0,  # well above the trailing EMA
        "volume": 5000,  # 5× the 1000 baseline
        "elapsed_pct": 1.0,
    }


def test_smoke_hold_blocks_persist_and_post_but_logs_pass(fresh_scanner_db, caplog):
    """Hold armed → the rule PASS is still logged, but NO scan_results row is
    written and NO ScanHitEvent is published; a dedup'd 'scanner HELD'
    WARNING names the symbol."""
    import logging

    from services import scanner_smoke_check_service as smoke

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    _seed_history(svc, "RELIANCE", "5m", [100.0 + i * 0.5 for i in range(20)], [1000.0] * 20)

    smoke.set_post_hold(reason="smoke check failed (test)", day=_PINNED_DAY)

    with caplog.at_level(logging.INFO, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", _matching_bar())

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []
    assert any("scanner PASS RELIANCE" in r.getMessage() for r in caplog.records)
    held = [r for r in caplog.records if "scanner HELD RELIANCE" in r.getMessage()]
    assert len(held) == 1
    assert "smoke_check_failed" in held[0].getMessage()


def test_smoke_hold_warning_deduped_per_symbol_day(fresh_scanner_db, caplog):
    """Two held PASSes on the same day → both blocked, but only ONE 'scanner
    HELD' WARNING (per-symbol-per-day dedup)."""
    import logging

    from services import scanner_smoke_check_service as smoke

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    _seed_history(svc, "RELIANCE", "5m", [100.0 + i * 0.5 for i in range(20)], [1000.0] * 20)

    smoke.set_post_hold(reason="smoke check failed (test)", day=_PINNED_DAY)

    with caplog.at_level(logging.WARNING, logger="services.scanner_service"):
        svc._on_bar_close("RELIANCE", "5m", _matching_bar(minute=0))
        svc._on_bar_close("RELIANCE", "5m", _matching_bar(minute=5))

    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []
    held = [r for r in caplog.records if "scanner HELD RELIANCE" in r.getMessage()]
    assert len(held) == 1


def test_smoke_hold_release_restores_posting(fresh_scanner_db):
    """Clearing the hold (a passing re-check) restores normal persistence."""
    from services import scanner_smoke_check_service as smoke

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    _seed_history(svc, "RELIANCE", "5m", [100.0 + i * 0.5 for i in range(20)], [1000.0] * 20)

    smoke.set_post_hold(reason="smoke check failed (test)", day=_PINNED_DAY)
    svc._on_bar_close("RELIANCE", "5m", _matching_bar(minute=0))
    assert scanner_service.get_scan_results(hours=24, source="inhouse") == []

    smoke.clear_post_hold(reason="re-check passed (test)")
    svc._on_bar_close("RELIANCE", "5m", _matching_bar(minute=5))
    rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    assert len(rows) == 1
    assert len(capturing_bus.events) == 1


def test_smoke_hold_prior_day_does_not_block(fresh_scanner_db):
    """A hold armed for a PRIOR day never gates today's hits (day-scoped)."""
    from services import scanner_smoke_check_service as smoke

    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    _seed_history(svc, "RELIANCE", "5m", [100.0 + i * 0.5 for i in range(20)], [1000.0] * 20)

    smoke.set_post_hold(reason="stale hold from yesterday", day=_PINNED_DAY - dt.timedelta(days=1))
    svc._on_bar_close("RELIANCE", "5m", _matching_bar())
    assert len(scanner_service.get_scan_results(hours=24, source="inhouse")) == 1


def test_smoke_hold_flag_off_does_not_block(fresh_scanner_db, monkeypatch):
    """SCANNER_SMOKE_BLOCK_ENABLED=false → the hold is never enforced."""
    from services import scanner_smoke_check_service as smoke

    monkeypatch.setenv("SCANNER_SMOKE_BLOCK_ENABLED", "false")
    capturing_bus = _CapturingBus()
    svc = scanner_service.ScannerService(symbols=["RELIANCE"], bus=capturing_bus)
    _enable_buy_definition()
    _seed_history(svc, "RELIANCE", "5m", [100.0 + i * 0.5 for i in range(20)], [1000.0] * 20)

    smoke.set_post_hold(reason="smoke check failed (test)", day=_PINNED_DAY)
    svc._on_bar_close("RELIANCE", "5m", _matching_bar())
    rows = scanner_service.get_scan_results(hours=24, source="inhouse")
    assert len(rows) == 1
    assert len(capturing_bus.events) == 1
