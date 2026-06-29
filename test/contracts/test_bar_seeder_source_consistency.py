"""Scanner aggregator seeder cross-source contract — the two-tier reader
(historify → broker fallback) must observably log which source it picked.

What this test exercises that existing tests do NOT
---------------------------------------------------
``test/test_scanner_aggregator_seeder.py`` already drives
``_read_1m_bars_for_symbol`` against many input shapes — empty
historify, epoch conversion, exception swallow, partial historify
triggering broker fallback. Each test asserts the right BARS came out.
None asks: "when the fallback fires, is the source pick LOGGED so an
operator can see in errors.jsonl which symbols are running off broker
data?"

That observability is the cross-source contract. The 2026-06-12
tick-starvation collapse (and the broader class of silent-source-pick
bugs) all hinge on the operator NOT noticing that the service is silently
running off the secondary source. The seeder's ``logger.info`` line on
the broker fallback is the only signal you have without doing
post-incident archeology — pin it.

The contract under test (production code, ``services/scanner_aggregator_seeder.py``)::

    if len(broker_bars) > len(historify_bars):
        logger.info(
            "aggregator_seeder: %s — historify had %d bars (<%d), "
            "broker fallback returned %d",
            symbol, len(historify_bars), min_required, len(broker_bars),
        )
        return broker_bars

If a future refactor neutralises that log line, this test goes red.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from services import scanner_aggregator_seeder
from services.scanner_aggregator_seeder import _read_1m_bars_for_symbol

_IST = timezone(timedelta(hours=5, minutes=30))


def _historify_df(n: int, base_price: float = 100.0) -> pd.DataFrame:
    """Build a historify-shaped 1m DataFrame with ``n`` rows ending ~now."""
    now = datetime.now(_IST).replace(second=0, microsecond=0)
    rows = []
    for i in range(n):
        ts = now - timedelta(minutes=(n - i))
        rows.append(
            {
                "timestamp": int(ts.timestamp()),
                "open": base_price,
                "high": base_price + 0.5,
                "low": base_price - 0.5,
                "close": base_price + 0.1,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


def _broker_bars(n: int, base_price: float = 100.0) -> list[dict]:
    """Build a broker-fallback-shaped list of ``n`` 1m bars ending ~now.

    The shape mirrors ``_read_1m_bars_from_broker``'s output (naive datetime
    ``ts``, NOT epoch — that's the live-tick convention ``replay_bars`` wants).
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


# ----------------------------------------------------------------------- #
# Contract A — divergent source sizes (historify short, broker long) →
# broker wins AND the source pick is logged.
# ----------------------------------------------------------------------- #
def test_short_historify_triggers_broker_fallback_and_logs_source_pick(monkeypatch, caplog):
    """The single observable contract from PR #200 (commit ``19b0a4fec``)
    that the operator depends on: when the two-tier reader falls through to
    the broker, ``logger.info`` records the symbol + both bar counts so the
    operator can grep errors.jsonl / log files and see which symbols are
    running off broker data instead of historify.

    Concrete divergence: historify returns 10 bars (well below the
    ``lookback_min // 3 = 166`` threshold for the default 500-min lookback);
    broker returns 300. Contract: broker wins AND the info-line fires.

    Bug class: a future change that "optimises" the reader to silently
    take the longer source without logging would re-introduce the
    silent-source-pick bug.
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")

    short_hist = _historify_df(10)  # below min_required=166 (lookback 500 // 3)
    long_broker = _broker_bars(300)

    with (
        patch("database.historify_db.get_ohlcv", return_value=short_hist),
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            return_value=long_broker,
        ),
        caplog.at_level(logging.INFO, logger=scanner_aggregator_seeder.logger.name),
    ):
        bars = _read_1m_bars_for_symbol("TESTSYM", "NSE", 500, api_key="dummy")

    # Picked the broker source (the longer one).
    assert len(bars) == 300, (
        f"reader returned {len(bars)} bars — expected 300 from broker. "
        f"Silent-stale-source bug class if historify's short series silently won."
    )

    # And the source pick was logged at INFO with both counts.
    info_lines = [
        rec
        for rec in caplog.records
        if rec.levelno == logging.INFO
        and "aggregator_seeder" in rec.getMessage()
        and "TESTSYM" in rec.getMessage()
        and "broker fallback returned" in rec.getMessage()
    ]
    assert info_lines, (
        f"reader silently fell through to broker without logging the source pick — "
        f"caplog INFO+ records for the seeder: "
        f"{[(r.levelname, r.getMessage()) for r in caplog.records if 'aggregator_seeder' in r.getMessage()]}"
    )


# ----------------------------------------------------------------------- #
# Contract B — historify long enough → historify wins, NO broker call.
# ----------------------------------------------------------------------- #
def test_long_historify_skips_broker_and_does_not_log_fallback(monkeypatch, caplog):
    """When historify has enough bars, the broker reader must NOT be called
    AND the source-pick log line must NOT fire. The reader returns the
    historify source silently. (We can't be both observable AND fast unless
    the log line only fires in the divergent path.)
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")

    long_hist = _historify_df(300)  # well above min_required=166

    # If anything tries to hit the broker, surface as AssertionError.
    with (
        patch("database.historify_db.get_ohlcv", return_value=long_hist),
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            side_effect=AssertionError("broker reader was called when historify was sufficient"),
        ),
        caplog.at_level(logging.INFO, logger=scanner_aggregator_seeder.logger.name),
    ):
        bars = _read_1m_bars_for_symbol("TESTSYM", "NSE", 500, api_key="dummy")

    assert len(bars) == 300

    # No source-pick log line in the happy path.
    fallback_lines = [
        rec for rec in caplog.records if "broker fallback returned" in rec.getMessage()
    ]
    assert not fallback_lines, (
        f"reader logged a broker-fallback line on the historify happy path: "
        f"{[r.getMessage() for r in fallback_lines]}"
    )


# ----------------------------------------------------------------------- #
# Contract C — fallback gate OFF: never picks broker even when historify
# is short. The flag boundary must be respected (PR #199 / #200 regression
# guard).
# ----------------------------------------------------------------------- #
def test_fallback_gate_off_keeps_historify_source_no_broker_call(monkeypatch):
    """Operator-killable: with the broker-fallback flag OFF, the reader
    must NOT call the broker even when historify is short. Otherwise an
    operator who disabled the flag for an emergency would still be paying
    broker rate-limit cost.
    """
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "false")

    short_hist = _historify_df(10)

    with (
        patch("database.historify_db.get_ohlcv", return_value=short_hist),
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            side_effect=AssertionError(
                "broker reader must NOT be called when fallback flag is off"
            ),
        ),
    ):
        bars = _read_1m_bars_for_symbol("TESTSYM", "NSE", 500, api_key="dummy")

    # Reader returned the short historify result rather than calling the broker.
    assert len(bars) == 10
