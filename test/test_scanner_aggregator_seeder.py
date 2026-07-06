"""Tests for ``services.scanner_aggregator_seeder`` (issue #156 Phase 2 / R3).

The boot-time helper that seeds the scanner aggregator's rolling state
from historify. Closes the ~25k indicator warmup warnings per restart +
the 100-min silent warmup window after every restart.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from services import scanner_aggregator_seeder
from services.scanner_aggregator_seeder import (
    _calendar_days_for_lookback,
    _read_1m_bars_for_symbol,
    seed_aggregator,
    session_aware_start_ts,
)

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _reset_reference_registry():
    """Issue #305: the broker-fallback arm of ``_read_1m_bars_for_symbol`` now
    records a broker prev-close into the module-global reference registry as a
    side effect. Reset it around every test so a recording here (e.g. for
    RELIANCE) cannot leak into rule-evaluation tests running later on the same
    xdist worker (the CI-only ``test_scanner_chartink_integration`` failure)."""
    from services import scanner_reference_data as refdata

    refdata.reset_for_tests()
    yield
    refdata.reset_for_tests()


# --------------------------------------------------------------------------- #
# Bar reader
# --------------------------------------------------------------------------- #


def test_read_1m_bars_returns_empty_on_missing_data():
    """historify returns an empty DataFrame → seeder gets [] for that symbol
    (the per-symbol slot stays empty, same as today's pre-seeding state)."""
    with patch("database.historify_db.get_ohlcv", return_value=pd.DataFrame()):
        bars = _read_1m_bars_for_symbol("UNKNOWN", "NSE", 500)
    assert bars == []


def test_read_1m_bars_converts_epoch_to_naive_datetime():
    """historify stores epoch seconds; replay_bars expects naive datetime
    in IST. The reader must convert."""
    now = datetime.now(_IST)
    df = pd.DataFrame(
        [
            {
                "timestamp": int((now - timedelta(minutes=2)).timestamp()),
                "open": 100.0,
                "high": 100.5,
                "low": 99.8,
                "close": 100.2,
                "volume": 1000,
            },
            {
                "timestamp": int((now - timedelta(minutes=1)).timestamp()),
                "open": 100.2,
                "high": 100.6,
                "low": 100.0,
                "close": 100.5,
                "volume": 1100,
            },
        ]
    )
    with patch("database.historify_db.get_ohlcv", return_value=df):
        bars = _read_1m_bars_for_symbol("RELIANCE", "NSE", 500)

    assert len(bars) == 2
    for bar in bars:
        assert isinstance(bar["ts"], datetime)
        assert bar["ts"].tzinfo is None  # naive (matches live tick path)
        assert bar["open"] is not None
        assert bar["close"] is not None
        assert bar["volume"] == int(bar["volume"])


def test_read_1m_bars_swallows_get_ohlcv_exception():
    """A historify read failure must NOT propagate — that symbol's slot just
    stays empty (= today's behaviour without the seeder)."""
    with patch("database.historify_db.get_ohlcv", side_effect=RuntimeError("duckdb locked")):
        bars = _read_1m_bars_for_symbol("RELIANCE", "NSE", 500)
    assert bars == []


def test_read_1m_bars_skips_rows_with_unparseable_timestamp():
    """One bad row doesn't poison the batch — only the broken row is skipped."""
    df = pd.DataFrame(
        [
            {
                "timestamp": "not-a-timestamp",
                "open": 100.0,
                "high": 100.5,
                "low": 99.8,
                "close": 100.2,
                "volume": 1000,
            },
            {
                "timestamp": int(datetime.now(_IST).timestamp()),
                "open": 100.2,
                "high": 100.6,
                "low": 100.0,
                "close": 100.5,
                "volume": 1100,
            },
        ]
    )
    with patch("database.historify_db.get_ohlcv", return_value=df):
        bars = _read_1m_bars_for_symbol("X", "NSE", 500)
    assert len(bars) == 1


# --------------------------------------------------------------------------- #
# seed_aggregator — fold into aggregator
# --------------------------------------------------------------------------- #


def test_seed_aggregator_empty_inputs_return_zeroes():
    summary = seed_aggregator(None, [])
    assert summary["seeded_symbols"] == 0
    assert summary["total_bars"] == 0


def test_seed_aggregator_skips_symbols_with_empty_history():
    """A symbol whose historify read returns [] is reported in empty_symbols
    and contributes 0 bars — aggregator slot stays empty."""
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(return_value=0)

    with (
        patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=[]),
    ):
        summary = seed_aggregator(mock_agg, ["RELIANCE", "SBIN"])

    assert summary["seeded_symbols"] == 0
    assert set(summary["empty_symbols"]) == {"RELIANCE", "SBIN"}
    assert summary["total_bars"] == 0
    # replay_bars never called — no bars to fold.
    mock_agg.replay_bars.assert_not_called()


def test_seed_aggregator_folds_bars_for_each_symbol():
    """Happy path: every symbol has bars → aggregator.replay_bars called once
    per symbol with the right argument shape."""
    fake_bars = [
        {
            "ts": datetime(2026, 6, 26, 14, 50),
            "open": 100.0,
            "high": 100.5,
            "low": 99.8,
            "close": 100.2,
            "volume": 1000,
        },
        {
            "ts": datetime(2026, 6, 26, 14, 51),
            "open": 100.2,
            "high": 100.6,
            "low": 100.0,
            "close": 100.5,
            "volume": 1100,
        },
    ]
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    with patch.object(
        scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=fake_bars
    ):
        summary = seed_aggregator(mock_agg, ["RELIANCE", "SBIN"])

    assert summary["seeded_symbols"] == 2
    assert summary["total_bars"] == 4
    assert summary["avg_bars_per_symbol"] == 2.0
    assert summary["empty_symbols"] == []
    assert summary["errors"] == 0
    assert mock_agg.replay_bars.call_count == 2


def test_seed_aggregator_counts_replay_exceptions_as_errors():
    """A replay_bars exception is logged + counted; other symbols still proceed."""
    fake_bars = [
        {
            "ts": datetime(2026, 6, 26, 14, 50),
            "open": 100.0,
            "high": 100.5,
            "low": 99.8,
            "close": 100.2,
            "volume": 1000,
        }
    ]
    mock_agg = MagicMock()

    def replay(sym, bars):
        if sym == "BROKEN":
            raise RuntimeError("replay failed")
        return len(bars)

    mock_agg.replay_bars = MagicMock(side_effect=replay)

    with patch.object(
        scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=fake_bars
    ):
        summary = seed_aggregator(mock_agg, ["GOOD", "BROKEN", "ALSO_GOOD"])

    assert summary["seeded_symbols"] == 2
    assert summary["errors"] == 1


def test_seed_aggregator_handles_none_aggregator():
    """An uninitialised aggregator (e.g. scanner disabled mid-init) returns
    zeroes — no crash."""
    summary = seed_aggregator(None, ["RELIANCE"])
    assert summary["seeded_symbols"] == 0


def test_seed_aggregator_mixed_results_summary_shape():
    """End-to-end shape: some seeded, some empty, some errored — summary is
    accurate."""

    def read(sym, exch, lookback, api_key=None):
        if sym == "EMPTY":
            return []
        if sym == "BROKEN":
            return [
                {
                    "ts": datetime(2026, 6, 26, 14, 50),
                    "open": 1,
                    "high": 1,
                    "low": 1,
                    "close": 1,
                    "volume": 1,
                }
            ]
        return [
            {
                "ts": datetime(2026, 6, 26, 14, mi),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
            }
            for mi in (50, 51, 52)
        ]

    def replay(sym, bars):
        if sym == "BROKEN":
            raise RuntimeError("boom")
        return len(bars)

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=replay)

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", side_effect=read):
        summary = seed_aggregator(mock_agg, ["RELIANCE", "EMPTY", "BROKEN", "SBIN"])

    assert summary["seeded_symbols"] == 2
    assert summary["empty_symbols"] == ["EMPTY"]
    assert summary["errors"] == 1
    assert summary["total_bars"] == 6  # 3 each from RELIANCE + SBIN
    assert summary["avg_bars_per_symbol"] == 3.0


# --------------------------------------------------------------------------- #
# Env-flag gating
# --------------------------------------------------------------------------- #


def test_boot_worker_skipped_when_flag_off(monkeypatch):
    """SCANNER_AGGREGATOR_SEED_ENABLED=false → seed_aggregator never called."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "false")
    mock_agg = MagicMock()

    with (
        patch.object(scanner_aggregator_seeder, "_wait_for_broker_session") as wait_fn,
        patch.object(scanner_aggregator_seeder, "seed_aggregator") as seed_fn,
    ):
        scanner_aggregator_seeder._boot_worker(mock_agg, ["RELIANCE"])

    wait_fn.assert_not_called()
    seed_fn.assert_not_called()


def test_boot_worker_skipped_when_broker_session_never_comes_up(monkeypatch):
    """No broker session within timeout → exit without seeding (warns)."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_TIMEOUT_SEC", "10")
    mock_agg = MagicMock()

    with (
        patch.object(scanner_aggregator_seeder, "_wait_for_broker_session", return_value=False),
        patch.object(scanner_aggregator_seeder, "seed_aggregator") as seed_fn,
    ):
        scanner_aggregator_seeder._boot_worker(mock_agg, ["RELIANCE"])

    seed_fn.assert_not_called()


def test_boot_worker_runs_seed_when_session_up(monkeypatch):
    """Happy path: broker session up → seed runs, summary notify fires."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    mock_agg = MagicMock()

    with (
        patch.object(scanner_aggregator_seeder, "_wait_for_broker_session", return_value=True),
        patch.object(
            scanner_aggregator_seeder,
            "seed_aggregator",
            return_value={
                "seeded_symbols": 2,
                "empty_symbols": [],
                "total_bars": 4,
                "avg_bars_per_symbol": 2.0,
                "errors": 0,
            },
        ) as seed_fn,
        patch.object(scanner_aggregator_seeder, "_notify") as notify_fn,
    ):
        scanner_aggregator_seeder._boot_worker(mock_agg, ["RELIANCE", "SBIN"])

    seed_fn.assert_called_once_with(mock_agg, ["RELIANCE", "SBIN"], bar_15m_history=None)
    notify_fn.assert_called_once()
    # Telegram message names the per-symbol counts.
    assert "2/2" in notify_fn.call_args.args[0]


def test_boot_worker_empty_symbols_is_noop(monkeypatch):
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    with (
        patch.object(scanner_aggregator_seeder, "_wait_for_broker_session") as wait_fn,
        patch.object(scanner_aggregator_seeder, "seed_aggregator") as seed_fn,
    ):
        scanner_aggregator_seeder._boot_worker(MagicMock(), [])
    wait_fn.assert_not_called()
    seed_fn.assert_not_called()


# --------------------------------------------------------------------------- #
# Broker fallback (issue #199)
# --------------------------------------------------------------------------- #
def _broker_bar(mi: int, ts_base: datetime | None = None) -> dict:
    """Synth a single 1m broker bar dict — epoch-seconds timestamp."""
    base = ts_base or datetime(2026, 6, 26, 9, 15)
    ts = (base + timedelta(minutes=mi)).replace(tzinfo=_IST)
    return {
        "timestamp": int(ts.timestamp()),
        "open": 100.0,
        "high": 100.1,
        "low": 99.9,
        "close": 100.05,
        "volume": 1000,
    }


def test_read_1m_bars_falls_back_to_broker_when_historify_short(monkeypatch):
    """If historify returns <lookback/3 bars AND fallback enabled AND api key
    available, the seeder uses broker history instead."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    # Empty historify
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=[],
        ) as hist_fn,
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
            return_value=[
                {
                    "ts": datetime(2026, 6, 26, 9, 15) + timedelta(minutes=mi),
                    "open": 100,
                    "high": 100,
                    "low": 100,
                    "close": 100,
                    "volume": 1000,
                }
                for mi in range(300)
            ],
        ) as broker_fn,
    ):
        out = scanner_aggregator_seeder._read_1m_bars_for_symbol(
            "RELIANCE",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )
    hist_fn.assert_called_once()
    broker_fn.assert_called_once()
    # Broker bars used (300) since historify returned 0.
    assert len(out) == 300


def test_read_1m_bars_skips_broker_when_historify_has_enough(monkeypatch):
    """If historify has >=lookback/3 bars AND covers today's session, no
    broker call is made. (#344: the clock is pinned mid-session on the bars'
    own date so the today-coverage check passes deterministically — the bars
    reach 14:14, well past the pinned 12:00.)"""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(
        scanner_aggregator_seeder, "_now_naive_ist", lambda: datetime(2026, 6, 26, 12, 0)
    )
    plenty = [
        {
            "ts": datetime(2026, 6, 26, 9, 15) + timedelta(minutes=mi),
            "open": 100,
            "high": 100,
            "low": 100,
            "close": 100,
            "volume": 1,
        }
        for mi in range(300)  # >= 500/3 = 167
    ]
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=plenty,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
        ) as broker_fn,
    ):
        out = scanner_aggregator_seeder._read_1m_bars_for_symbol(
            "RELIANCE",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )
    broker_fn.assert_not_called()
    assert len(out) == 300


def test_read_1m_bars_no_broker_fallback_when_flag_off(monkeypatch):
    """SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED=false → broker never
    called even when historify is empty (pre-#199 behaviour preserved)."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "false")
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=[],
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
        ) as broker_fn,
    ):
        out = scanner_aggregator_seeder._read_1m_bars_for_symbol(
            "RELIANCE",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )
    broker_fn.assert_not_called()
    assert out == []


def test_read_1m_bars_no_broker_when_no_api_key(monkeypatch):
    """If we can't resolve an API key, broker arm is silently skipped."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=[],
        ),
        patch(
            "services.scanner_aggregator_seeder._get_api_key",
            return_value=None,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
        ) as broker_fn,
    ):
        out = scanner_aggregator_seeder._read_1m_bars_for_symbol("RELIANCE", "NSE", 500)
    broker_fn.assert_not_called()
    assert out == []


def test_broker_fetcher_returns_empty_on_failed_call(monkeypatch):
    """A broker get_history that returns success=False yields []. No exception
    propagates."""
    fake_get_history = MagicMock(return_value=(False, {"message": "token miss"}, 502))
    with patch("services.history_service.get_history", fake_get_history):
        out = scanner_aggregator_seeder._read_1m_bars_from_broker(
            "RELIANCE",
            "NSE",
            500,
            "test-key",  # pragma: allowlist secret
        )
    assert out == []


def test_broker_fetcher_parses_epoch_timestamps():
    """Broker rows with epoch-seconds timestamps are converted to naive-IST
    datetimes, sorted, and trimmed to lookback_min."""
    rows = [_broker_bar(mi) for mi in range(0, 600, 1)]  # 600 bars
    fake_payload = {"data": rows}
    fake_get_history = MagicMock(return_value=(True, fake_payload, 200))
    with patch("services.history_service.get_history", fake_get_history):
        out = scanner_aggregator_seeder._read_1m_bars_from_broker(
            "RELIANCE",
            "NSE",
            500,
            "test-key",  # pragma: allowlist secret
        )
    assert len(out) == 500
    # Sorted ascending.
    assert all(out[i]["ts"] <= out[i + 1]["ts"] for i in range(len(out) - 1))
    # ts is naive datetime.
    assert out[0]["ts"].tzinfo is None


# --------------------------------------------------------------------------- #
# 15m bar aggregation (issue #201)
# --------------------------------------------------------------------------- #
def _make_1m_bar(ts: datetime, base: float = 100.0, vol: int = 100) -> dict:
    return {
        "ts": ts,
        "open": base,
        "high": base + 0.5,
        "low": base - 0.5,
        "close": base + 0.1,
        "volume": vol,
    }


def test_aggregate_1m_to_15m_buckets_by_quarter_hour():
    """15 1m bars covering 09:15 → 09:29 should produce a single 15m bar at 09:15."""
    bars_1m = [_make_1m_bar(datetime(2026, 6, 26, 9, 15) + timedelta(minutes=i)) for i in range(15)]
    bars_15m = scanner_aggregator_seeder._aggregate_1m_to_15m(bars_1m)
    assert len(bars_15m) == 1
    b = bars_15m[0]
    assert b["ts"] == datetime(2026, 6, 26, 9, 15)
    # OHLCV semantics: volume aggregated, high/low extrema, open from first, close from last.
    assert b["volume"] == 100 * 15
    assert b["open"] == 100.0
    assert b["close"] == 100.1


def test_aggregate_1m_to_15m_trims_partial_closing_bucket():
    """30 1m bars covering 09:15 → 09:44 (two full 15m buckets) followed by 5
    partial 1m bars at 09:45 → produce 2 closed 15m bars, not 3."""
    bars_1m = [
        _make_1m_bar(datetime(2026, 6, 26, 9, 15) + timedelta(minutes=i)) for i in range(30 + 5)
    ]
    bars_15m = scanner_aggregator_seeder._aggregate_1m_to_15m(bars_1m)
    # 09:15 and 09:30 buckets are full (15 each); 09:45 has only 5 — dropped.
    assert len(bars_15m) == 2
    assert bars_15m[0]["ts"] == datetime(2026, 6, 26, 9, 15)
    assert bars_15m[1]["ts"] == datetime(2026, 6, 26, 9, 30)


def test_aggregate_1m_to_15m_empty():
    assert scanner_aggregator_seeder._aggregate_1m_to_15m([]) == []


def test_seed_aggregator_also_seeds_15m_when_history_passed():
    """When ``bar_15m_history`` is provided, every symbol with sufficient 1m
    bars also seeds its 15m roller via ``seed_bars``."""
    bars_1m = [_make_1m_bar(datetime(2026, 6, 26, 9, 15) + timedelta(minutes=i)) for i in range(30)]
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(return_value=len(bars_1m))

    roll15 = MagicMock()
    roll15.seed_bars = MagicMock(return_value=2)
    bar_15m_history = {"RELIANCE": roll15}

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=bars_1m):
        summary = scanner_aggregator_seeder.seed_aggregator(
            mock_agg, ["RELIANCE"], bar_15m_history=bar_15m_history
        )

    roll15.seed_bars.assert_called_once()
    seeded_15m = roll15.seed_bars.call_args.args[0]
    assert len(seeded_15m) == 2  # two full buckets
    assert summary["seeded_15m_bars"] == 2


def test_seed_aggregator_does_not_seed_15m_when_no_history_passed():
    """If ``bar_15m_history`` is not passed, only the 5m aggregator is seeded
    (the legacy code path stays unchanged)."""
    bars_1m = [_make_1m_bar(datetime(2026, 6, 26, 9, 15) + timedelta(minutes=i)) for i in range(30)]
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(return_value=len(bars_1m))

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=bars_1m):
        summary = scanner_aggregator_seeder.seed_aggregator(mock_agg, ["RELIANCE"])

    assert summary["seeded_15m_bars"] == 0


def test_seed_aggregator_15m_seed_failure_does_not_block_5m():
    """A raise in 15m seeding is caught and reported; the 5m seed still succeeds."""
    bars_1m = [_make_1m_bar(datetime(2026, 6, 26, 9, 15) + timedelta(minutes=i)) for i in range(30)]
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(return_value=30)

    roll15 = MagicMock()
    roll15.seed_bars = MagicMock(side_effect=RuntimeError("disk full"))
    bar_15m_history = {"RELIANCE": roll15}

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=bars_1m):
        summary = scanner_aggregator_seeder.seed_aggregator(
            mock_agg, ["RELIANCE"], bar_15m_history=bar_15m_history
        )

    # 5m seed succeeded; 15m seed crashed silently.
    assert summary["seeded_symbols"] == 1
    assert summary["total_bars"] == 30
    assert summary["seeded_15m_bars"] == 0


# --------------------------------------------------------------------------- #
# _Rolling15mBars.seed_bars (issue #201)
# --------------------------------------------------------------------------- #
def test_rolling_15m_seed_bars_appends_to_deque():
    from services.scanner_service import _Rolling15mBars

    roll = _Rolling15mBars("RELIANCE")
    bars = [
        {
            "ts": datetime(2026, 6, 26, 9, 15) + timedelta(minutes=15 * i),
            "open": 100.0,
            "high": 101.0,
            "low": 99.0,
            "close": 100.5,
            "volume": 1500,
        }
        for i in range(20)
    ]
    n = roll.seed_bars(bars)
    assert n == 20
    out = roll.get_recent_bars(50)
    assert len(out) == 20


def test_rolling_15m_seed_bars_dedups_by_timestamp():
    """A repeated timestamp REPLACES the prior bar rather than double-counting."""
    from services.scanner_service import _Rolling15mBars

    roll = _Rolling15mBars("RELIANCE")
    ts = datetime(2026, 6, 26, 9, 15)
    roll.seed_bars(
        [
            {
                "ts": ts,
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 1500,
            }
        ]
    )
    # Re-seed same ts with different values.
    added = roll.seed_bars(
        [
            {
                "ts": ts,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 9999,
            }
        ]
    )
    # No NEW bar added — but the existing row's values are updated.
    assert added == 0
    out = roll.get_recent_bars(50)
    assert len(out) == 1
    assert out.iloc[0]["close"] == 104.0
    assert out.iloc[0]["volume"] == 9999


# --------------------------------------------------------------------------- #
# Stale-historify regression (issue #203)
# --------------------------------------------------------------------------- #
def test_today_d_prefers_5m_close_over_stale_historify():
    """When bars_daily.iloc[-1] is dated today (post-#199 seeder behaviour —
    historify has today's D bar) AND bars_5m has live timestamps, the helper
    MUST derive today_d from 5m, not trust the frozen daily snapshot.

    Regression: scanner_history_provider refreshes once at boot and caches
    bars_daily. iloc[-1].close becomes a frozen LTP from boot time. The
    pre-#203 helper trusted that, causing 34/41 false-positive SELL fires
    on 2026-06-29 — including TCS (live +0.41%) firing because its boot
    snapshot was -2%.
    """
    import pytz

    from services.scan_rules._today_running import derive_today_and_yest

    ist = pytz.timezone("Asia/Kolkata")
    now = ist.localize(datetime(2026, 6, 29, 15, 10))

    # Daily frame with iloc[-1] dated TODAY but FROZEN close (boot snapshot):
    # close=2050 (~2% below yest 2094), open=2080.
    today_ts = int(ist.localize(datetime(2026, 6, 29, 9, 15)).timestamp())
    yest_ts = int(ist.localize(datetime(2026, 6, 26, 9, 15)).timestamp())
    daily = pd.DataFrame(
        [
            {
                "timestamp": yest_ts,
                "open": 2094,
                "high": 2110,
                "low": 2080,
                "close": 2094,
                "volume": 1_000_000,
            },
            {
                "timestamp": today_ts,
                "open": 2080,
                "high": 2110,
                "low": 2040,
                "close": 2050,
                "volume": 2_500_000,
            },  # frozen boot snapshot
        ]
    )

    # Live 5m frame with timestamps + a recovered close (2103 — UP from prev).
    bars_5m = pd.DataFrame(
        [
            {
                "timestamp": int(
                    ist.localize(
                        datetime(2026, 6, 29, 9, 15) + timedelta(minutes=5 * i)
                    ).timestamp()
                ),
                "open": 2080 + i * 0.5,
                "high": 2085 + i * 0.5,
                "low": 2078 + i * 0.5,
                "close": 2080 + i * 0.5,  # rising back; last close = 2103
                "volume": 10_000,
            }
            for i in range(46)  # 09:15 → 13:00, last close 2080 + 45*0.5 = 2102.5
        ]
    )
    # Make the last 5m bar's close 2103 (matches the broker LTP).
    bars_5m.loc[bars_5m.index[-1], "close"] = 2103

    today_d, yest_d, yest_idx = derive_today_and_yest(daily, bars_5m, now)

    assert today_d is not None
    # today_d.close MUST be 2103 (the live 5m close), NOT 2050 (the frozen
    # historify daily). This is the #203 fix.
    assert today_d["close"] == 2103.0
    # yest_d is the previous settled bar (iloc[-2] since iloc[-1] is dated today).
    assert yest_d["close"] == 2094
    assert yest_idx == -2


def test_today_d_falls_back_to_historify_when_5m_lacks_timestamp_column():
    """Synthetic-test path preserved: if bars_5m has no `timestamp` column
    (the existing test fixtures), trust bars_daily.iloc[-1] as today_d so
    the unit tests in test_fno_intraday_{buy,sell}_chartink.py keep working
    against synthetic frames."""
    import pytz

    from services.scan_rules._today_running import derive_today_and_yest

    ist = pytz.timezone("Asia/Kolkata")
    now = ist.localize(datetime(2026, 6, 29, 15, 10))

    today_ts = int(ist.localize(datetime(2026, 6, 29, 9, 15)).timestamp())
    yest_ts = int(ist.localize(datetime(2026, 6, 26, 9, 15)).timestamp())
    daily = pd.DataFrame(
        [
            {
                "timestamp": yest_ts,
                "open": 2094,
                "high": 2110,
                "low": 2080,
                "close": 2094,
                "volume": 1_000_000,
            },
            {
                "timestamp": today_ts,
                "open": 2080,
                "high": 2110,
                "low": 2040,
                "close": 2050,
                "volume": 2_500_000,
            },
        ]
    )
    # Synthetic 5m frame — no `timestamp` column.
    bars_5m = pd.DataFrame(
        [
            {"open": 2080, "high": 2085, "low": 2078, "close": 2050, "volume": 10_000}
            for _ in range(20)
        ]
    )

    today_d, yest_d, yest_idx = derive_today_and_yest(daily, bars_5m, now)

    assert today_d is not None
    # No 5m timestamps → trust historify iloc[-1]. close=2050 (the daily value).
    assert today_d["close"] == 2050
    assert yest_d["close"] == 2094
    assert yest_idx == -2


# --------------------------------------------------------------------------- #
# session_aware_start_ts (issue #340)
# --------------------------------------------------------------------------- #
# Reproduces the 2026-07-06 incident: boot 08:26 IST (pre-market), default
# lookback 500min. The old wall-clock computation gave start_ts = now - 500min
# = ~00:06 IST — a window with ZERO trading bars, which is why the seeder
# recorded 0/227 symbols seeded and the 15m warm-up gate then rejected the
# whole universe until ~13:00 IST (n_rows=7 at 10:35 vs required=15).


def test_session_aware_start_ts_premarket_boot_walks_back_to_prior_session():
    """A pre-market boot (before 09:15 IST) must NOT anchor the window inside
    the empty overnight gap — it should walk back into the prior trading
    session(s), same as if the boot had happened right at yesterday's close."""
    # Monday 2026-07-06, 08:26 IST — the exact incident boot time.
    now = datetime(2026, 7, 6, 8, 26)
    start_ts = session_aware_start_ts(now, 500)
    start_dt = datetime.fromtimestamp(start_ts, _IST)

    # Must land on a WEEKDAY, inside a session window [09:15, 15:30], and
    # strictly before `now` (so the fetch window is non-degenerate).
    assert start_dt.weekday() < 5
    assert (9, 15) <= (start_dt.hour, start_dt.minute) <= (15, 30)
    assert start_dt.replace(tzinfo=None) < now

    # Pre-fix behaviour would have been ~00:06 IST same-day — assert we do NOT
    # land inside the dead overnight window.
    dead_zone_start = datetime(2026, 7, 6, 0, 0, tzinfo=_IST)
    dead_zone_end = datetime(2026, 7, 6, 9, 15, tzinfo=_IST)
    assert not (dead_zone_start <= start_dt < dead_zone_end)

    # 500 trading minutes back from a Monday pre-market boot must reach back
    # past the weekend into Thursday/Friday of the prior week (walks back
    # Fri 375min + Thu remainder = 500min total).
    assert start_dt.date() == date(2026, 7, 2)  # Thursday


def test_session_aware_start_ts_midsession_restart_uses_today_plus_prior_day():
    """A mid-session restart should pull today's already-elapsed session bars
    FIRST, then reach back into the prior day only for the remainder — so the
    window is contiguous ('today's earlier bars + prior day'), not two
    disjoint chunks."""
    # Monday 2026-07-06, 10:35 IST — today has elapsed 80 minutes (09:15-10:35).
    now = datetime(2026, 7, 6, 10, 35)
    start_ts = session_aware_start_ts(now, 500)
    start_dt = datetime.fromtimestamp(start_ts, _IST)

    # 500 - 80 = 420 minutes still needed; walks back through the weekend to
    # Thursday (Friday's 375 covers 375, remaining 45 min end at 14:45 Thu).
    assert start_dt.date() == date(2026, 7, 2)
    assert (start_dt.hour, start_dt.minute) == (14, 45)


def test_session_aware_start_ts_small_lookback_stays_within_today():
    """When the elapsed session already covers the lookback, the window must
    stay entirely inside today — no unnecessary reach into prior days."""
    # Monday 2026-07-06, 12:00 IST — 165 minutes elapsed since 09:15.
    now = datetime(2026, 7, 6, 12, 0)
    start_ts = session_aware_start_ts(now, 60)
    start_dt = datetime.fromtimestamp(start_ts, _IST)

    assert start_dt.date() == date(2026, 7, 6)
    assert (start_dt.hour, start_dt.minute) == (11, 0)


def test_session_aware_start_ts_weekend_boot_walks_back_to_friday():
    """A boot on a weekend (e.g. a Sunday maintenance restart) must skip
    Saturday/Sunday entirely — today contributes 0 trading minutes."""
    # Sunday 2026-07-05, any time of day.
    now = datetime(2026, 7, 5, 10, 0)
    start_ts = session_aware_start_ts(now, 100)
    start_dt = datetime.fromtimestamp(start_ts, _IST)

    assert start_dt.weekday() == 4  # Friday
    assert start_dt.date() == date(2026, 7, 3)


def test_session_aware_start_ts_never_raises_on_degenerate_lookback():
    """lookback_min <= 0 must not raise — falls back to a safe wall-clock
    computation rather than looping or crashing."""
    now = datetime(2026, 7, 6, 8, 26)
    start_ts = session_aware_start_ts(now, 0)
    assert isinstance(start_ts, int)
    assert start_ts < int(now.timestamp())


# --------------------------------------------------------------------------- #
# _calendar_days_for_lookback (issue #340 — broker-arm trim fix)
# --------------------------------------------------------------------------- #


def test_calendar_days_for_lookback_covers_requested_trading_minutes():
    """The broker arm's fetch window must span enough CALENDAR days to
    guarantee `lookback_min` TRADING minutes are actually present to trim —
    the old fixed 2-day window silently under-covered anything bigger than
    the original ~210min 15m-warmup use case."""
    # 500 trading minutes needs ceil(500/375) = 2 trading days; padded for
    # weekends + slack. Must be strictly more than the old fixed "2".
    days = _calendar_days_for_lookback(500)
    assert days > 2
    # A much larger lookback must ask for correspondingly more days.
    assert _calendar_days_for_lookback(3000) > _calendar_days_for_lookback(500)


def test_calendar_days_for_lookback_small_request_still_gets_a_useful_window():
    days = _calendar_days_for_lookback(60)
    assert days >= 3  # at least "yesterday + today" plus slack


# --------------------------------------------------------------------------- #
# Historify arm — session-aware window (issue #340)
# --------------------------------------------------------------------------- #


class _FixedDatetime(datetime):
    """``datetime`` subclass whose ``.now(tz)`` returns a fixed instant.

    Used to pin ``scanner_aggregator_seeder``'s ``datetime.now(_IST)`` calls
    during a test without disturbing any other ``datetime`` usage (this
    subclass inherits everything else — ``fromtimestamp``, arithmetic, etc.
    — unchanged), unlike patching the whole module with a bare ``MagicMock``.
    """

    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):
        fixed = cls._fixed
        if fixed is None:
            return super().now(tz)
        return fixed if tz is None else fixed.astimezone(tz)


def _pin_seeder_now(monkeypatch, fixed_ist: datetime) -> None:
    """Pin ``scanner_aggregator_seeder.datetime.now(_IST)`` to ``fixed_ist``
    (a tz-aware IST datetime) for the duration of a test."""
    pinned = type("_Pinned", (_FixedDatetime,), {"_fixed": fixed_ist})
    monkeypatch.setattr(scanner_aggregator_seeder, "datetime", pinned)


def test_historify_arm_uses_session_aware_window_on_premarket_boot(monkeypatch):
    """The historify read must ask for a session-aware start_ts, not a raw
    wall-clock one — verified by capturing the actual start_timestamp/
    end_timestamp passed to get_ohlcv during a simulated pre-market boot."""
    captured = {}

    def fake_get_ohlcv(symbol, exchange, interval, start_timestamp=None, end_timestamp=None):
        captured["start_timestamp"] = start_timestamp
        captured["end_timestamp"] = end_timestamp
        return pd.DataFrame()

    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 8, 26, tzinfo=_IST))
    with patch("database.historify_db.get_ohlcv", side_effect=fake_get_ohlcv):
        scanner_aggregator_seeder._read_1m_bars_from_historify("GODREJCP", "NSE", 500)

    assert "start_timestamp" in captured
    start_dt = datetime.fromtimestamp(captured["start_timestamp"], _IST)
    # Must NOT be the pre-fix ~00:06 IST same-day dead zone.
    dead_zone_start = datetime(2026, 7, 6, 0, 0, tzinfo=_IST)
    dead_zone_end = datetime(2026, 7, 6, 9, 15, tzinfo=_IST)
    assert not (dead_zone_start <= start_dt < dead_zone_end)
    assert start_dt.weekday() < 5


# --------------------------------------------------------------------------- #
# End-to-end: pre-market-boot seeding clears the 15m warm-up gate
# (issue #340 acceptance criteria — drives the REAL _Rolling15mBars path)
# --------------------------------------------------------------------------- #


def _historify_1m_bars_for_session(day: date, n_minutes: int = 375) -> pd.DataFrame:
    """Production-shaped historify 1m frame: `timestamp` (epoch seconds) +
    OHLCV, one row per trading minute of `day`'s session (09:15 start),
    capped at n_minutes rows. Mirrors `database.historify_db.get_ohlcv`'s
    documented return shape."""
    base = datetime(day.year, day.month, day.day, 9, 15, tzinfo=_IST)
    rows = []
    close = 1090.0
    for i in range(n_minutes):
        ts = base + timedelta(minutes=i)
        close += 0.05
        rows.append(
            {
                "timestamp": int(ts.timestamp()),
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 500,
            }
        )
    return pd.DataFrame(rows)


def test_premarket_boot_seed_clears_15m_warmup_immediately(monkeypatch):
    """Full acceptance scenario (issue #340): seed with prior-day data at a
    pre-market boot → the 15m frame holds >=15 bars IMMEDIATELY after
    seeding (no live ticks needed) — the exact condition that clears the
    `15m_warmup` gate (`n_rows >= required=15`) that rejected GODREJCP at
    10:35 IST on 2026-07-06.

    Drives the real `_Rolling15mBars` via `seed_aggregator`'s
    `bar_15m_history` param — not a synthetic shortcut — plus the real
    `MultiIntervalAggregator.replay_bars` fan-out for the 5m side.
    """
    from services.bar_aggregator import MultiIntervalAggregator
    from services.scanner_service import _Rolling15mBars

    # Prior trading day (Friday 2026-07-03) has a full session of 1m bars.
    prior_day = date(2026, 7, 3)
    historify_frame = _historify_1m_bars_for_session(prior_day, n_minutes=375)

    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 8, 26, tzinfo=_IST))  # Monday pre-market

    with patch("database.historify_db.get_ohlcv", return_value=historify_frame):
        bars = scanner_aggregator_seeder._read_1m_bars_for_symbol("GODREJCP", "NSE", 500)

    # The session-aware read must actually find bars (pre-fix: 0 — the
    # 2026-07-06 incident: "seeded 0/227 symbols").
    assert len(bars) > 0

    agg = MultiIntervalAggregator(symbols=["GODREJCP"], intervals=["5m"])
    roll15 = _Rolling15mBars("GODREJCP")
    bar_15m_history = {"GODREJCP": roll15}

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=bars):
        summary = scanner_aggregator_seeder.seed_aggregator(
            agg, ["GODREJCP"], bar_15m_history=bar_15m_history
        )

    assert summary["seeded_symbols"] == 1
    # >=15 closed 15m bars available IMMEDIATELY post-seed — the acceptance
    # bar the 15m_warmup gate checks (`len(bars_15m) < 15` in
    # services/scan_rules/fno_intraday_{buy,sell}_chartink.py).
    recent_15m = roll15.get_recent_bars(50)
    assert len(recent_15m) >= 15


def test_midsession_restart_seed_spans_today_and_prior_day_no_double_count(monkeypatch):
    """Mid-session-restart scenario: seed spans today's earlier bars + prior
    day, >=15 bars, and re-seeding (simulating a duplicate boot-worker fire)
    does NOT double-count — BarBuilder/seed_bars timestamp dedup holds."""
    from services.scanner_service import _Rolling15mBars

    prior_day = date(2026, 7, 3)  # Friday
    today = date(2026, 7, 6)  # Monday

    prior_frame = _historify_1m_bars_for_session(prior_day, n_minutes=375)
    today_frame = _historify_1m_bars_for_session(today, n_minutes=80)  # 09:15-10:35
    combined = pd.concat([prior_frame, today_frame], ignore_index=True)

    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 10, 35, tzinfo=_IST))  # mid-session

    with patch("database.historify_db.get_ohlcv", return_value=combined):
        bars = scanner_aggregator_seeder._read_1m_bars_for_symbol("GODREJCP", "NSE", 500)

    assert len(bars) > 0

    roll15 = _Rolling15mBars("GODREJCP")
    bars_15m = scanner_aggregator_seeder._aggregate_1m_to_15m(bars)
    first_seed = roll15.seed_bars(bars_15m)
    assert first_seed >= 15

    # Re-seed with the SAME bars (simulating a duplicate boot fire / retry) —
    # must add ZERO new bars (dedup by timestamp holds).
    second_seed = roll15.seed_bars(bars_15m)
    assert second_seed == 0
    assert len(roll15.get_recent_bars(50)) == first_seed


# --------------------------------------------------------------------------- #
# #257 regression — seeded/replayed bars still never fire evaluations
# --------------------------------------------------------------------------- #


def test_seeded_bars_do_not_trigger_evaluation_end_to_end(monkeypatch):
    """End-to-end guarantee (issue #257, re-verified after the #340 change):
    folding session-aware-windowed historical bars into the LIVE scanner via
    `seed_aggregator` (both the 5m `aggregator.replay_bars` fan-out and the
    15m `_Rolling15mBars.seed_bars` direct-append) must fire NO evaluation —
    only a genuine live tick/bar-close does. This exercises the real
    `ScannerService` + `MultiIntervalAggregator`, not a mock."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    from database import scanner_db as sdb
    from services import scan_rules  # noqa: F401 — self-registers example rules
    from services import scanner_service as ss

    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)
    ss.init_scanner_db()

    class _CapturingBus:
        def __init__(self) -> None:
            self.events: list = []

        def publish(self, event) -> None:
            self.events.append(event)

    capturing_bus = _CapturingBus()
    svc = ss.ScannerService(symbols=["GODREJCP"], bus=capturing_bus)

    def_id = ss.create_scan_definition(
        name="_seeder_test_buy",
        screener_type="buy",
        expression_json=None,
        rule_module="fno_intraday_buy_chartink",
        enabled=True,
    )

    prior_day = date(2026, 7, 3)
    historify_frame = _historify_1m_bars_for_session(prior_day, n_minutes=375)

    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 8, 26, tzinfo=_IST))
    with patch("database.historify_db.get_ohlcv", return_value=historify_frame):
        bars = scanner_aggregator_seeder._read_1m_bars_for_symbol("GODREJCP", "NSE", 500)

    assert len(bars) > 0

    with patch.object(scanner_aggregator_seeder, "_read_1m_bars_for_symbol", return_value=bars):
        summary = scanner_aggregator_seeder.seed_aggregator(
            svc.aggregator, ["GODREJCP"], bar_15m_history=svc._bar_15m_history
        )

    assert summary["seeded_symbols"] == 1
    assert summary["seeded_15m_bars"] >= 15
    # The whole point of #257: warming state via replay/seed fires NOTHING.
    assert ss.get_scan_results(hours=24, source="inhouse") == []
    assert capturing_bus.events == []
    assert def_id is not None

    test_session.remove()
    test_engine.dispose()


# --------------------------------------------------------------------------- #
# Issue #344 — mid-session restart: the seed must contain TODAY's session,
# and the rule-facing 5m frame must aggregate it (volume/open) correctly.
# --------------------------------------------------------------------------- #
#
# Root cause of the 2026-07-06 12:30/12:52 incident: the two-tier source
# selection accepted historify on raw BAR COUNT (>= lookback/3), which a
# mid-session historify always satisfies with PRIOR-day bars while holding
# (almost) none of today's (the scanner-side 1m backfill only runs post-close).
# The broker fallback — the only source that has today's bars mid-session —
# was skipped for every stock, so the seed reaching the aggregator/15m
# builders simply did not contain today's session, and the rules'
# today-aggregation (derive_today_and_yest Path B) starved.


def _mk_1m(day: date, hh: int, mm: int, *, minutes: int, volume: int = 1000) -> list[dict]:
    """``minutes`` consecutive 1m bars starting at day hh:mm (naive IST),
    constant OHLC (open 1060 / close 1060.5) and per-bar ``volume``."""
    base = datetime(day.year, day.month, day.day, hh, mm)
    return [
        {
            "ts": base + timedelta(minutes=i),
            "open": 1060.0,
            "high": 1061.0,
            "low": 1059.0,
            "close": 1060.5,
            "volume": volume,
        }
        for i in range(minutes)
    ]


_MONDAY = date(2026, 7, 6)
_FRIDAY = date(2026, 7, 3)


def test_read_1m_bars_broker_fallback_when_historify_count_rich_but_today_stale(monkeypatch):
    """THE #344 unit case: historify is count-sufficient (prior-day rich) but
    covers only the first 2 minutes of today's session at a 12:52 restart →
    the broker fallback MUST run and its today-covering series MUST win.

    Pre-fix behaviour (verified via git stash): historify passes the raw
    count check, the broker is never called, and the seed contains 2 minutes
    of today — this test FAILS on the pre-fix tree.
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 12, 52, tzinfo=_IST))  # Monday mid-session

    # ~300 Friday bars + ONLY the first 2 Monday bars (the 09:16 backfill) —
    # the exact 2026-07-06 12:52 historify state for LODHA.
    historify = _mk_1m(_FRIDAY, 10, 30, minutes=300) + _mk_1m(_MONDAY, 9, 15, minutes=2, volume=800)
    broker = _mk_1m(_MONDAY, 9, 15, minutes=210)  # 09:15 → 12:44 (broker lag)

    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=historify,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
            return_value=broker,
        ) as broker_fn,
    ):
        out = _read_1m_bars_for_symbol(
            "LODHA",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )

    broker_fn.assert_called_once()
    assert out == broker  # today-covering source wins even though it's SHORTER
    # Sanity: the chosen seed actually contains today's full elapsed session.
    today_vol = sum(b["volume"] for b in out if b["ts"].date() == _MONDAY)
    assert today_vol == 210 * 1000


def test_read_1m_bars_premarket_prior_day_only_historify_still_wins(monkeypatch):
    """#340 pre-market behaviour preserved: before open there is no today
    session to cover, so a prior-days-only historify series stays sufficient
    and NO broker call is made."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 8, 26, tzinfo=_IST))  # Monday pre-market

    historify = _mk_1m(_FRIDAY, 10, 30, minutes=300)
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=historify,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
        ) as broker_fn,
    ):
        out = _read_1m_bars_for_symbol(
            "LODHA",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )

    broker_fn.assert_not_called()
    assert out == historify


def test_read_1m_bars_historify_covering_today_needs_no_broker(monkeypatch):
    """Mid-session, a historify series that DOES cover today's elapsed session
    (within the broker-lag grace) is accepted without a broker call."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 12, 52, tzinfo=_IST))

    # Friday tail + Monday 09:15 → 12:45 (211 bars) — covers elapsed-15.
    historify = _mk_1m(_FRIDAY, 13, 0, minutes=150) + _mk_1m(_MONDAY, 9, 15, minutes=211)
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=historify,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
        ) as broker_fn,
    ):
        out = _read_1m_bars_for_symbol(
            "LODHA",
            "NSE",
            500,
            api_key="test-key",  # pragma: allowlist secret
        )

    broker_fn.assert_not_called()
    assert out == historify


def test_today_coverage_helpers():
    """Unit coverage for the #344 helpers: grace window, pre-open, weekend."""
    f = scanner_aggregator_seeder._has_sufficient_today_coverage

    # Pre-open Monday: trivially sufficient with no today bars.
    assert f([], datetime(2026, 7, 6, 8, 26)) is True
    # First minutes after open (elapsed <= grace): still sufficient.
    assert f([], datetime(2026, 7, 6, 9, 25)) is True
    # Weekend: no session, trivially sufficient.
    assert f([], datetime(2026, 7, 5, 12, 0)) is True  # Sunday
    # Mid-session with no today bars: NOT sufficient.
    assert f([], datetime(2026, 7, 6, 12, 52)) is False
    # Mid-session with bars through (now - grace): sufficient.
    bars = _mk_1m(_MONDAY, 9, 15, minutes=210)  # through 12:44
    assert f(bars, datetime(2026, 7, 6, 12, 52)) is True
    # Post-close boot (16:00) with a full-session series: sufficient.
    full = _mk_1m(_MONDAY, 9, 15, minutes=375)
    assert f(full, datetime(2026, 7, 6, 16, 0)) is True
    # Post-close boot with only the morning: not sufficient.
    morning = _mk_1m(_MONDAY, 9, 15, minutes=60)
    assert f(morning, datetime(2026, 7, 6, 16, 0)) is False


# --------------------------------------------------------------------------- #
# Issue #344 acceptance — mid-session restart, REAL ScannerService objects:
# seeded (broker) bars + live ticks → rule-facing frame's today-aggregation
# yields the full-session volume and the true 09:15 open.
# --------------------------------------------------------------------------- #


class _NullBus:
    def publish(self, event):
        pass

    def subscribe(self, *a, **k):
        pass


def _tick(price: float, cum_vol: int, ts: datetime) -> dict:
    return {"price": price, "cumulative_volume": cum_vol, "ts": ts}


def test_midsession_restart_seeded_bars_reach_rule_facing_today_aggregation(monkeypatch):
    """The #344 acceptance scenario, end-to-end on real objects:

    12:52 restart → live ticks land first (12:50 bucket) → the boot seeder
    runs (historify prior-day-rich/today-stale, broker has 09:15→12:44) →
    a live 5m close at 12:55. The rule-facing frame
    (``ScannerService._bar_history`` — what ``indicators['bars_5m']`` is) must
    then let ``derive_today_and_yest`` Path B compute:

    * ``today_d.volume`` = FULL session sum (seeded 210×1000 + live 20,000)
    * ``today_d.open``   = the real 09:15 open (1060.0), NOT the first
      post-boot bar's open (1063.0)
    * ``today_d.close``  = the latest live close (1063.5)

    Pre-fix (git stash) this fails twice over: historify wins the source
    pick (today volume = live-only + the 2 backfill minutes), and the frame
    is append-ordered so today's open reads from the 12:50 live bar.
    """
    from services.scan_rules._today_running import derive_today_and_yest
    from services.scanner_service import ScannerService
    from test.fixtures.frame_factory import make_historify_daily_frame

    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    _pin_seeder_now(monkeypatch, datetime(2026, 7, 6, 12, 52, 30, tzinfo=_IST))

    svc = ScannerService(symbols=["LODHA"], bus=_NullBus())

    # 1. Restart at 12:52 — live ticks arrive BEFORE the seeder finishes
    #    (production ordering: ticks at boot, seed summary ~15s later).
    svc.aggregator.on_tick("LODHA", _tick(1063.0, 5_000_000, datetime(2026, 7, 6, 12, 52, 13)))
    svc.aggregator.on_tick("LODHA", _tick(1063.5, 5_020_000, datetime(2026, 7, 6, 12, 53, 40)))

    # 2. Boot seed. historify = Friday-rich + first 2 Monday minutes (the
    #    incident state); broker = Monday 09:15 → 12:44 (210 bars × 1000).
    historify = _mk_1m(_FRIDAY, 10, 30, minutes=300) + _mk_1m(_MONDAY, 9, 15, minutes=2, volume=800)
    broker = _mk_1m(_MONDAY, 9, 15, minutes=210)
    with (
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_historify",
            return_value=historify,
        ),
        patch(
            "services.scanner_aggregator_seeder._read_1m_bars_from_broker",
            return_value=broker,
        ),
        patch(
            "services.scanner_aggregator_seeder._get_api_key",
            return_value="test-key",  # pragma: allowlist secret
        ),
        patch(
            "services.scanner_aggregator_seeder._resolve_exchange_for_symbol",
            return_value="NSE",
        ),
    ):
        summary = seed_aggregator(svc.aggregator, ["LODHA"], bar_15m_history=svc._bar_15m_history)
    assert summary["seeded_symbols"] == 1
    assert summary["seeded_15m_bars"] > 0  # 15m warm-up still served (no regression)

    # 3. Live 5m close at 12:55 (closes the 12:50 bucket, vol 20,000 delta).
    svc.aggregator.on_tick("LODHA", _tick(1064.5, 5_030_000, datetime(2026, 7, 6, 12, 55, 1)))

    frame = svc._bar_history.get(("LODHA", "5m"))
    assert frame is not None and not frame.empty

    # The frame is the rules' bars_5m — run the rules' own Path B on it.
    bars_daily = make_historify_daily_frame([1050.0, 1057.0], _FRIDAY)
    now_ist = datetime(2026, 7, 6, 12, 55, 5, tzinfo=_IST)
    today_d, yest_d, yest_idx = derive_today_and_yest(bars_daily, frame, now_ist=now_ist)

    assert today_d is not None
    # FULL-session volume: 210 seeded 1m bars × 1000 + the live 12:50 bucket's
    # 20,000 cum-vol delta. Pre-fix: 22,000 (2 backfill minutes + live).
    assert today_d["volume"] == 210 * 1000 + 20_000
    # True 09:15 open — pre-fix this read 1063.0 (the first post-boot bar,
    # append-ordered at iloc[0]).
    assert today_d["open"] == 1060.0
    # Latest live close.
    assert today_d["close"] == 1063.5
    # yest is Friday's settled bar.
    assert yest_idx == -1
    assert yest_d["close"] == 1057.0


def test_live_bar_for_already_seeded_bucket_counted_once():
    """Double-count guard: a live tick landing in the bucket the seed left
    open (the trailing partial) folds INTO that bucket — one frame row, and
    today's volume counts the seeded portion exactly once."""
    from services.scanner_service import ScannerService

    svc = ScannerService(symbols=["LODHA"], bus=_NullBus())

    # Seed first (no live ticks yet): 09:15 → 12:49 = 215 bars × 1000.
    # 42 full 5m buckets (09:15..12:40) close during replay; the 12:45 bucket
    # (5 bars, 5000 vol) stays open as the trailing partial.
    n = svc.aggregator.replay_bars("LODHA", _mk_1m(_MONDAY, 9, 15, minutes=215))
    assert n == 215

    # Live ticks INSIDE the already-seeded 12:45 bucket: first tick sets the
    # cum-vol baseline (delta 0), second adds 3,000 on top of the seeded 5,000.
    svc.aggregator.on_tick("LODHA", _tick(1061.0, 1_000_000, datetime(2026, 7, 6, 12, 49, 30)))
    svc.aggregator.on_tick("LODHA", _tick(1061.5, 1_003_000, datetime(2026, 7, 6, 12, 49, 50)))
    # Bucket rolls to 12:50 → the 12:45 bucket closes (5,000 seeded + 3,000 live).
    svc.aggregator.on_tick("LODHA", _tick(1062.0, 1_004_000, datetime(2026, 7, 6, 12, 50, 5)))

    frame = svc._bar_history.get(("LODHA", "5m"))
    assert frame is not None
    today_rows = frame[frame["ts"].apply(lambda x: x.date() == _MONDAY)]

    # Exactly ONE 12:45 row — the live continuation did not duplicate it.
    rows_1245 = today_rows[today_rows["ts"] == datetime(2026, 7, 6, 12, 45)]
    assert len(rows_1245) == 1
    assert rows_1245["volume"].iloc[0] == 5_000 + 3_000
    # Total = 42 replay-closed buckets × 5000 + the merged 12:45 bucket.
    assert today_rows["volume"].sum() == 42 * 5_000 + 8_000

    # Idempotency (existing BarBuilder dedup): re-replaying the same bars
    # adds nothing.
    assert svc.aggregator.replay_bars("LODHA", _mk_1m(_MONDAY, 9, 15, minutes=215)) == 0
    frame2 = svc._bar_history.get(("LODHA", "5m"))
    today2 = frame2[frame2["ts"].apply(lambda x: x.date() == _MONDAY)]
    assert today2["volume"].sum() == 42 * 5_000 + 8_000


def test_preopen_boot_live_only_accumulation_unchanged():
    """Pre-open-boot scenario: with no seed at all, live-only accumulation
    still builds the frame exactly as before (#344 must not regress it)."""
    from services.scanner_service import ScannerService

    svc = ScannerService(symbols=["LODHA"], bus=_NullBus())

    svc.aggregator.on_tick("LODHA", _tick(1060.0, 10_000, datetime(2026, 7, 6, 9, 15, 1)))
    svc.aggregator.on_tick("LODHA", _tick(1061.0, 25_000, datetime(2026, 7, 6, 9, 17, 0)))
    svc.aggregator.on_tick("LODHA", _tick(1062.0, 40_000, datetime(2026, 7, 6, 9, 20, 2)))

    frame = svc._bar_history.get(("LODHA", "5m"))
    assert frame is not None and len(frame) == 1
    row = frame.iloc[0]
    assert row["ts"] == datetime(2026, 7, 6, 9, 15)
    assert row["open"] == 1060.0
    assert row["close"] == 1061.0
    assert row["volume"] == 15_000  # delta from the first tick's baseline
