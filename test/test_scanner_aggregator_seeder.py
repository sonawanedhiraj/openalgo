"""Tests for ``services.scanner_aggregator_seeder`` (issue #156 Phase 2 / R3).

The boot-time helper that seeds the scanner aggregator's rolling state
from historify. Closes the ~25k indicator warmup warnings per restart +
the 100-min silent warmup window after every restart.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from services import scanner_aggregator_seeder
from services.scanner_aggregator_seeder import (
    _read_1m_bars_for_symbol,
    seed_aggregator,
)

_IST = timezone(timedelta(hours=5, minutes=30))


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

    seed_fn.assert_called_once_with(mock_agg, ["RELIANCE", "SBIN"])
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
    """If historify has >=lookback/3 bars, no broker call is made."""
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
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
