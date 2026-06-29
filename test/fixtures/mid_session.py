"""Time-pinned mid-session scenario fixtures (issue #224).

Why this file exists
--------------------
Most production bugs in the scanner / freshness / bar-aggregator stack happen
at a *specific moment* in the IST trading day with specific system state.
General-purpose test frames that mock everything internally never reproduce
the moment that triggers the bug. These fixtures pin the IST clock AND set
historify / aggregator / broker-mock state realistic for that moment, so the
integration test in ``test/integration/test_mid_session_moments.py`` exercises
the *exact* preconditions that caused the bug in production.

The codebase deliberately avoids ``freezegun`` (see
``test/e2e/conftest.py:111-120``: *"Avoids a hard freezegun dependency — the
services under test all accept an injected ``now``."*). These fixtures follow
the same pattern: time is delivered as a tz-aware IST ``datetime`` that the
test passes into the function under test, never patched globally.

Each fixture returns a ``MidSessionState`` namespace carrying:

* ``now`` — the pinned tz-aware IST ``datetime``
* ``harness`` — a ``BootHarness`` already booted with the realistic state
* additional per-scenario knobs (universe lists, synthetic frames, etc.)

The four moments
----------------
* :func:`at_14_30_restart`     — historify has bars for the sector_follow
  subset, is physically empty for the rest of the scanner universe. The
  broker-history fallback is the only thing keeping the remainder from going
  dark for the rest of the day. Bug PR #200 (commit ``19b0a4fec``).
* :func:`at_15_10_stale_daily` — historify's daily-D bar for today is a
  *frozen* boot snapshot (the cached ``bars_daily.iloc[-1]``), while live
  5m bars show a recovery. ``derive_today_and_yest`` must prefer the live
  5m close, not the frozen daily. Bug PR #204 (commit ``ed7bcc0f0``).
* :func:`at_09_30_cold_start`  — historify is empty (cold morning),
  aggregator empty, no warm-up state. The seeder must also seed each
  symbol's 15m bar deque so the rule's ``len(bars_15m) < 15`` warm-up
  guard clears on the first close after restart. Bug PR #202 (commit
  ``e2b021ac4``).
* :func:`at_10_00_post_relogin` — daily Zerodha token refresh has just
  fired; the WS proxy reconnects via ZMQ ``CACHE_INVALIDATE`` and the
  in-process ``WSRecoveryService`` replays missed 1m bars into the
  aggregator. The aggregator must NOT silently warm up from scratch.
  WS reinit class.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import pandas as pd
import pytest
import pytz

from test.harness import BootHarness

_IST = pytz.timezone("Asia/Kolkata")


# --------------------------------------------------------------------------- #
# Result namespaces
# --------------------------------------------------------------------------- #


@dataclass
class MidSessionState:
    """Shared shape returned by every fixture in this module.

    ``now`` is the pinned IST moment the scenario simulates. Tests must pass
    this into any function under test that accepts an injected ``now``
    parameter rather than reading ``datetime.now()`` from the system clock.

    ``extras`` carries scenario-specific knobs (universe lists, synthetic
    daily/5m frames, broker mocks). Keeping it loose keeps each fixture's
    surface area small.
    """

    now: datetime
    harness: BootHarness
    extras: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Bar-builder helpers (shared)
# --------------------------------------------------------------------------- #


def _make_historify_1m_bars(n: int, *, end_at: datetime, base_price: float = 100.0) -> list[dict]:
    """Build ``n`` recent 1m bars ending at ``end_at``, in the
    ``upsert_market_data`` shape (epoch-seconds timestamp).
    """
    bars: list[dict] = []
    for i in range(n):
        ts = end_at - timedelta(minutes=(n - i))
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


def _make_broker_replay_bars(n: int, *, end_at: datetime, base_price: float = 100.0) -> list[dict]:
    """Build ``n`` recent 1m bars in the ``aggregator.replay_bars`` shape
    (naive ``datetime`` ``ts`` field — matches the live tick path).
    """
    end_naive = end_at.replace(tzinfo=None)
    return [
        {
            "ts": end_naive - timedelta(minutes=(n - i)),
            "open": base_price,
            "high": base_price + 0.5,
            "low": base_price - 0.5,
            "close": base_price + 0.1,
            "volume": 1000,
        }
        for i in range(n)
    ]


def _pinned_datetime_class(pinned_now: datetime) -> type[datetime]:
    """Build a ``datetime`` subclass whose ``.now(tz=None)`` returns
    ``pinned_now`` (converted to ``tz`` when supplied).

    Used to patch the ``datetime`` symbol inside a service module so its
    internal ``datetime.now(...)`` calls return a deterministic IST moment.
    The codebase deliberately avoids ``freezegun`` (see module docstring);
    this subclass is the lightest-weight replacement and only affects the
    one module it's patched into.
    """

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D401
            if tz is None:
                # Strip tz so tests of naive-now paths still work.
                return pinned_now.astimezone(None).replace(tzinfo=None)
            return pinned_now.astimezone(tz)

    return _Pinned


@contextlib.contextmanager
def pin_clock_in(module_path: str, pinned_now: datetime):
    """Patch ``<module_path>.datetime`` with a class whose ``.now()`` returns
    ``pinned_now``. Yields the patched class; reverts on exit.

    Example::

        with pin_clock_in("services.scanner_aggregator_seeder", state.now):
            scanner_aggregator_seeder.seed_aggregator(mock_agg, universe)
    """
    PinnedDT = _pinned_datetime_class(pinned_now)
    with patch(f"{module_path}.datetime", PinnedDT):
        yield PinnedDT


# --------------------------------------------------------------------------- #
# Fixture 1 — 14:30 IST mid-session restart (PR #200 / commit 19b0a4fec)
# --------------------------------------------------------------------------- #

# Subset present in historify ("sector_follow" universe in production).
HISTORIFY_SUBSET = ["SBIN", "RELIANCE", "INFY"]
# Symbols only available via the broker fallback (the ~195 scanner-only F&O
# names whose 1m backfill runs in the 15:30-17:00 IST window only).
BROKER_ONLY_UNIVERSE = ["TCS", "HDFCBANK", "WIPRO", "ITC", "LT"]
FULL_SCANNER_UNIVERSE = HISTORIFY_SUBSET + BROKER_ONLY_UNIVERSE


@pytest.fixture
def at_14_30_restart() -> Any:
    """Pin to 14:30 IST and simulate the asymmetric historify state.

    The sector_follow subset (~30 stocks in production) is continuously
    backfilled and so has recent 1m bars in historify. The scanner-only
    F&O universe (~195 names) is backfilled post-close only — at 14:30 IST
    it is *physically empty* in ``market_data`` for those symbols. PR #200
    (commit ``19b0a4fec``) added the broker-history fallback so the seeder
    can still warm up the aggregator for those names.

    ``extras``:
      * ``historify_subset`` — names with real DuckDB rows
      * ``broker_only_universe`` — names that must rely on broker fallback
      * ``full_universe`` — the union the seeder is called with
    """
    pinned_now = _IST.localize(datetime(2026, 6, 29, 14, 30))

    with BootHarness.create() as harness:
        bars = _make_historify_1m_bars(200, end_at=pinned_now)
        harness.seed_historify_partial(
            dict.fromkeys(HISTORIFY_SUBSET, bars),
            interval="1m",
            exchange="NSE",
        )
        yield MidSessionState(
            now=pinned_now,
            harness=harness,
            extras={
                "historify_subset": list(HISTORIFY_SUBSET),
                "broker_only_universe": list(BROKER_ONLY_UNIVERSE),
                "full_universe": list(FULL_SCANNER_UNIVERSE),
                "make_broker_replay_bars": _make_broker_replay_bars,
            },
        )


# --------------------------------------------------------------------------- #
# Fixture 2 — 15:10 IST stale-daily evaluation (PR #204 / commit ed7bcc0f0)
# --------------------------------------------------------------------------- #


def _make_stale_daily_frame(today_ist: datetime) -> pd.DataFrame:
    """Build a 2-row daily DataFrame mimicking ``ScannerHistoryProvider``'s
    boot-time cache: yesterday's settled bar AND a today-dated bar whose
    ``close`` is a *frozen* boot snapshot (2% below yesterday). The frozen
    snapshot is what produced the 41 false SELL fires on 2026-06-29.
    """
    yest = today_ist - timedelta(days=3)  # 2026-06-26 (Friday before the Monday)
    today_ts = int(today_ist.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
    yest_ts = int(yest.replace(hour=9, minute=15, second=0, microsecond=0).timestamp())
    return pd.DataFrame(
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
                "close": 2050,  # FROZEN boot snapshot, ~2% below yesterday
                "volume": 2_500_000,
            },
        ]
    )


def _make_live_5m_recovery_frame(
    today_ist: datetime, *, last_close: float = 2103.0
) -> pd.DataFrame:
    """Build a 5m DataFrame for 09:15→13:00 IST with a final ``close ==
    last_close`` — the live recovered LTP that the SELL rule must see, not
    the frozen daily close.
    """
    start = today_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    rows = []
    for i in range(46):  # 09:15 → 13:00, 5min steps
        ts = int((start + timedelta(minutes=5 * i)).timestamp())
        rows.append(
            {
                "timestamp": ts,
                "open": 2080 + i * 0.5,
                "high": 2085 + i * 0.5,
                "low": 2078 + i * 0.5,
                "close": 2080 + i * 0.5,
                "volume": 10_000,
            }
        )
    df = pd.DataFrame(rows)
    df.loc[df.index[-1], "close"] = last_close
    return df


@pytest.fixture
def at_15_10_stale_daily() -> Any:
    """Pin to 15:10 IST 2026-06-29 — the moment the 41 false SELL fires
    landed in production (issue #203 / PR #204).

    Provides a daily DataFrame whose ``iloc[-1]`` is dated today but carries
    a *frozen* boot snapshot close (2% below yesterday), and a 5m DataFrame
    whose last close shows the live recovered LTP. The helper under test
    (``derive_today_and_yest``) must return the live 5m close as ``today_d``,
    not the frozen historify daily.

    ``extras``:
      * ``daily`` — the stale-daily DataFrame
      * ``bars_5m`` — the live-5m DataFrame
      * ``frozen_close`` — the value that ``derive_today_and_yest`` MUST NOT
        return (2050)
      * ``live_close`` — the value it MUST return (2103)
    """
    pinned_now = _IST.localize(datetime(2026, 6, 29, 15, 10))

    with BootHarness.create() as harness:
        yield MidSessionState(
            now=pinned_now,
            harness=harness,
            extras={
                "daily": _make_stale_daily_frame(pinned_now),
                "bars_5m": _make_live_5m_recovery_frame(pinned_now, last_close=2103.0),
                "frozen_close": 2050.0,
                "live_close": 2103.0,
            },
        )


# --------------------------------------------------------------------------- #
# Fixture 3 — 09:30 IST cold-boot restart (PR #202 / commit e2b021ac4)
# --------------------------------------------------------------------------- #


@pytest.fixture
def at_09_30_cold_start() -> Any:
    """Pin to 09:30 IST and simulate a cold-morning restart.

    Historify is empty (overnight gap, no fresh bars), the aggregator is
    empty, no warm-up state. The seeder must call ``_Rolling15mBars.seed_bars``
    on each symbol's 15m deque in addition to ``aggregator.replay_bars`` —
    otherwise the 15m RSI(14) gate stays cold for ~100 minutes (PR #202).

    ``extras``:
      * ``universe`` — the symbol set the seeder is called with
      * ``make_broker_replay_bars`` — helper to build a broker-mock return
    """
    pinned_now = _IST.localize(datetime(2026, 6, 29, 9, 30))

    with BootHarness.create() as harness:
        # No historify seed — cold morning state.
        yield MidSessionState(
            now=pinned_now,
            harness=harness,
            extras={
                "universe": list(HISTORIFY_SUBSET),
                "make_broker_replay_bars": _make_broker_replay_bars,
            },
        )


# --------------------------------------------------------------------------- #
# Fixture 4 — 10:00 IST post-broker-relogin (WS reinit class)
# --------------------------------------------------------------------------- #


@pytest.fixture
def at_10_00_post_relogin() -> Any:
    """Pin to 10:00 IST and simulate state immediately after a Zerodha
    re-login: ``auth_db`` carries a fresh token (via
    ``BootHarness.mock_zerodha_login``) and a ``BrokerSessionRefreshedEvent``
    is about to fire on the in-process event bus, which
    ``WSRecoveryService`` consumes to fold missed 1m bars into the
    aggregator. The aggregator MUST NOT silently warm up from scratch on
    a reconnect (the 2026-06-11/12 tick-starvation collapse class).

    ``extras``:
      * ``universe`` — list of ``(symbol, exchange)`` tuples used by the
        recovery service's universe provider
      * ``make_broker_replay_bars`` — helper to build a broker-mock return
    """
    pinned_now = _IST.localize(datetime(2026, 6, 29, 10, 0))

    with BootHarness.create() as harness:
        # A fresh login has just happened — its CACHE_INVALIDATE / event-bus
        # signal is what triggers WSRecoveryService.recover() in production.
        harness.mock_zerodha_login(token="post_relogin_token_xyz")
        yield MidSessionState(
            now=pinned_now,
            harness=harness,
            extras={
                "universe": [(sym, "NSE") for sym in HISTORIFY_SUBSET],
                "make_broker_replay_bars": _make_broker_replay_bars,
            },
        )


__all__ = [
    "BROKER_ONLY_UNIVERSE",
    "FULL_SCANNER_UNIVERSE",
    "HISTORIFY_SUBSET",
    "MidSessionState",
    "at_09_30_cold_start",
    "at_10_00_post_relogin",
    "at_14_30_restart",
    "at_15_10_stale_daily",
    "pin_clock_in",
]
