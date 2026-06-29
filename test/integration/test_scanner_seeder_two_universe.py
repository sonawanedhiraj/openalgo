"""Two-universe data-shape regression test for the scanner aggregator seeder
(issue #223; covers PR #200 / commit ``19b0a4fec``).

What this test exercises that existing tests do NOT
---------------------------------------------------
``test/test_scanner_aggregator_seeder.py`` already has 30+ unit tests for the
seeder, but every one of them mocks ``_read_1m_bars_for_symbol`` /
``_read_1m_bars_from_historify`` to return the *same* canned value for any
symbol. That makes a specific production state invisible to the test suite:

* historify has bars for a *subset* of symbols (e.g. sector_follow's 30 stocks
  that are backfilled continuously)
* historify is *physically empty* for the rest of the scanner universe
  (~195 F&O symbols backfilled only post-close, in the 15:30-17:00 IST
  window)

This is what production looks like at 14:30 IST mid-session restart. PR #200
(commit ``19b0a4fec``) introduced the broker-history fallback so the seeder
can fill the gap. Without that fallback the 195 non-sector symbols stay
empty and the scanner emits no signals for them all day — exactly bug #200.

This test reproduces the asymmetric data state by writing real OHLCV rows
into the temp historify for a chosen subset only (via the new
``BootHarness.seed_historify_partial`` fixture), then asserting:

* with the broker fallback ON: every symbol in the universe is seeded
  (subset from historify + remainder from the broker mock)
* with the broker fallback OFF: only the subset is seeded; the remainder
  shows up in ``empty_symbols`` — reproducing the pre-#200 bug

The fallback-OFF arm is the regression guard: if a future change removes or
neutralises the broker-fallback path, this assertion flips and the test
fails.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from test.harness import BootHarness

_IST = timezone(timedelta(hours=5, minutes=30))

# Subset present in historify ("sector_follow" universe in production).
_HISTORIFY_SUBSET = ["SBIN", "RELIANCE", "INFY"]
# Symbols only available via the broker fallback ("scanner-only" universe).
_BROKER_ONLY = ["TCS", "HDFCBANK", "WIPRO", "ITC", "LT"]
_FULL_UNIVERSE = _HISTORIFY_SUBSET + _BROKER_ONLY


def _make_1m_bars(n: int, *, base_price: float = 100.0) -> list[dict]:
    """Build N recent 1m bars relative to now, in the shape upsert_market_data
    expects (timestamp will be normalized to epoch seconds by the upsert).
    """
    now = datetime.now(_IST).replace(second=0, microsecond=0)
    bars: list[dict] = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i))
        bars.append(
            {
                "timestamp": int(ts.timestamp()),
                "open": base_price,
                "high": base_price + 0.5,
                "low": base_price - 0.5,
                "close": base_price + 0.1,
                "volume": 1000,
            }
        )
    return bars


def _make_replay_bars(n: int, *, base_price: float = 100.0) -> list[dict]:
    """Build N recent 1m bars in the ``replay_bars`` shape the broker reader
    returns (ts is a naive datetime, not epoch).
    """
    now = datetime.now(_IST).replace(second=0, microsecond=0, tzinfo=None)
    return [
        {
            "ts": now - timedelta(minutes=(n - i)),
            "open": base_price,
            "high": base_price + 0.5,
            "low": base_price - 0.5,
            "close": base_price + 0.1,
            "volume": 1000,
        }
        for i in range(n)
    ]


@pytest.fixture
def harness():
    with BootHarness.create() as h:
        yield h


@pytest.fixture
def seeded_historify(harness):
    """Real DuckDB writes for the subset only; the broker-only symbols stay
    physically absent from market_data, mirroring the 14:30 IST mid-session
    production state."""
    bars_per_symbol = _make_1m_bars(200)
    harness.seed_historify_partial(
        dict.fromkeys(_HISTORIFY_SUBSET, bars_per_symbol),
        interval="1m",
        exchange="NSE",
    )
    return harness


def test_partial_historify_fixture_writes_subset_only(seeded_historify):
    """Sanity check the new fixture: ``get_ohlcv`` returns bars for the seeded
    subset and an empty DataFrame for anything else. This is the *data
    shape* the rest of this file's tests rely on.
    """
    from database.historify_db import get_ohlcv

    for sym in _HISTORIFY_SUBSET:
        df = get_ohlcv(sym, "NSE", "1m")
        assert not df.empty, f"{sym} should be seeded but historify returned empty"

    for sym in _BROKER_ONLY:
        df = get_ohlcv(sym, "NSE", "1m")
        assert df.empty, (
            f"{sym} should be physically absent (broker-only universe) "
            f"but historify returned {len(df)} rows"
        )


def test_seeder_with_broker_fallback_covers_full_universe(seeded_historify, monkeypatch):
    """With the broker fallback ON: every symbol in the universe ends up
    seeded — the subset from historify, the remainder from broker history.
    This is the PR #200 behaviour. Equivalent to "no bug": no symbol is
    silently dark for the rest of the day.
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")

    from services import scanner_aggregator_seeder

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    # Stub the broker fetcher to return bars for any symbol — this is the
    # role the real Zerodha history API plays at 14:30 IST when historify
    # has nothing for the symbol.
    broker_bars = _make_replay_bars(200)
    with (
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            return_value=broker_bars,
        ) as broker_fn,
        patch.object(
            scanner_aggregator_seeder,
            "_get_api_key",
            return_value="test-key",  # pragma: allowlist secret
        ),
        # The seeder calls resolve_exchange_for_symbol per name; pin it to NSE
        # so it doesn't try to hit master_contract DB for unknown symbols.
        patch.object(
            scanner_aggregator_seeder,
            "_resolve_exchange_for_symbol",
            return_value="NSE",
        ),
    ):
        summary = scanner_aggregator_seeder.seed_aggregator(mock_agg, _FULL_UNIVERSE)

    assert summary["seeded_symbols"] == len(_FULL_UNIVERSE), (
        f"every symbol should be seeded with broker fallback ON; "
        f"got {summary['seeded_symbols']}/{len(_FULL_UNIVERSE)}, "
        f"empty={summary['empty_symbols']}"
    )
    assert summary["empty_symbols"] == []

    # Broker fetcher was called for the broker-only universe (historify was
    # short for those), but NOT for the seeded subset (historify was
    # sufficient). This is the contract that bug #200 violated.
    broker_called_for = {call.args[0] for call in broker_fn.call_args_list}
    assert set(_BROKER_ONLY).issubset(broker_called_for), (
        f"broker fallback should fire for the broker-only universe; "
        f"called for {broker_called_for}, expected superset of {set(_BROKER_ONLY)}"
    )
    assert broker_called_for.isdisjoint(_HISTORIFY_SUBSET), (
        f"broker fallback must NOT fire for symbols already in historify; "
        f"called for {broker_called_for & set(_HISTORIFY_SUBSET)}"
    )


def test_seeder_without_broker_fallback_leaves_subset_dark(seeded_historify, monkeypatch):
    """Regression guard for PR #200 (commit ``19b0a4fec``).

    With the broker fallback OFF: only the historify subset is seeded; the
    rest of the universe shows up in ``empty_symbols`` and stays dark all
    day. This is the production bug state on 2026-06-29 before PR #200.

    If a future change neutralises the broker-fallback path (e.g. the flag
    is renamed, the call site loses the fallback, ``_read_1m_bars_for_symbol``
    is refactored to drop the second tier), the seeded count flips from
    ``len(subset)`` to ``len(full)`` and this assertion will fail loudly.
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "false")

    from services import scanner_aggregator_seeder

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    with (
        # If anything calls the broker reader despite the flag being off,
        # surface it as a fail rather than a silent return.
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            side_effect=AssertionError(
                "broker reader must not be called when the fallback flag is off"
            ),
        ),
        patch.object(
            scanner_aggregator_seeder,
            "_resolve_exchange_for_symbol",
            return_value="NSE",
        ),
    ):
        summary = scanner_aggregator_seeder.seed_aggregator(mock_agg, _FULL_UNIVERSE)

    assert summary["seeded_symbols"] == len(_HISTORIFY_SUBSET), (
        f"only the historify subset should seed when broker fallback is off; "
        f"got {summary['seeded_symbols']} (expected {len(_HISTORIFY_SUBSET)})"
    )
    assert set(summary["empty_symbols"]) == set(_BROKER_ONLY), (
        f"the broker-only universe should be reported as empty; got {summary['empty_symbols']}"
    )
