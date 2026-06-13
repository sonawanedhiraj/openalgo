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
# ---------------------------------------------------------------------------


@scanner_service.scan_rule(
    "_test_buy_surge_ema", "buy", "test-only: vol surge >=2x AND close above EMA20"
)
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


@scanner_service.scan_rule(
    "_test_sell_surge_ema", "sell", "test-only: vol surge >=2x AND close below EMA20"
)
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
    # Backward-compat indicator keys plus the four Task-4 multi-timeframe keys.
    assert set(result.keys()) == {
        "ema_20",
        "atr_14",
        "rsi_14",
        "volume_avg_20",
        "bars_5m",
        "bars_15m",
        "bars_daily",
        "bars_weekly",
    }
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
    assert set(["ts", "open", "high", "low", "close", "volume"]).issubset(bars_15m.columns)


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
