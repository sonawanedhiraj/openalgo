"""Time-pinned mid-session scenario regression guards (issue #224).

Why this file exists
--------------------
Most production bugs in the scanner / freshness / bar-aggregator stack happen
at a *specific moment* in the IST trading day with a specific historify +
aggregator + broker state. Generic unit tests that mock everything internally
do not reproduce the moment that triggers the bug. The fixtures in
``test/fixtures/mid_session.py`` pin the IST clock AND set realistic system
state for each known-bad moment; the tests below exercise the *full call path*
through ``BootHarness`` for that moment and assert correct behaviour.

Each test is a **regression guard**: its docstring names the commit + PR + the
bug it catches, and its assertion FAILS if the corresponding fix is reverted.

Bug → test mapping
------------------
* ``test_at_14_30_restart_seeds_full_universe_via_broker_fallback``
  → bug PR #200 / commit ``19b0a4fec`` — sparse historify after 14:30 IST
  restart leaves ~195 scanner-universe symbols dark for the rest of the
  day. Sibling unit tests in ``test/test_scanner_aggregator_seeder.py``
  mock the historify reader symbol-by-symbol with the same canned return,
  so the *asymmetric* "subset present, remainder physically empty" data
  state is never reproduced. The two-universe fixture (issue #223,
  commit ``c0efe0f85``) added the data shape; this test pins the moment
  it occurs.
* ``test_at_15_10_stale_daily_prefers_live_5m_close_over_frozen_snapshot``
  → bug PR #204 / commit ``ed7bcc0f0`` — at 15:10 IST 2026-06-29 the
  ``ScannerHistoryProvider`` cache held a *frozen* boot snapshot of
  today's daily close (2050, ~2% below yesterday) while the live LTP had
  recovered (2103, +0.41%). The SELL rule fired 41 false signals;
  ``derive_today_and_yest`` must prefer the live 5m close. The companion
  unit test at ``test/test_scanner_aggregator_seeder.py:665`` calls
  ``derive_today_and_yest`` directly with hand-rolled frames; this
  integration test pins the clock to 15:10 IST and walks the *full*
  fixture-built call path.
* ``test_at_09_30_cold_start_warms_both_5m_aggregator_and_15m_bars``
  → bug PR #202 / commit ``e2b021ac4`` — the seeder folded 1m bars into
  the 5m aggregator but did NOT seed each symbol's 15m bar deque, so the
  15m RSI(14) gate stayed cold (warm-up < 14 bars) for ~100 minutes
  after a cold-morning restart, silently failing every rule call. The
  fix added the ``bar_15m_history`` parameter and the
  ``_Rolling15mBars.seed_bars`` call. This test pins to 09:30 IST and
  asserts both arms fire.
* ``test_at_10_00_post_relogin_replays_missed_bars_into_aggregator``
  → WS reinit class (post-Zerodha-relogin gap). At 10:00 IST a daily
  Zerodha token refresh triggers WS reinit and the ``WSRecoveryService``
  must fold missed 1m bars into the live scanner aggregator (issue
  #129 / Fix B-prime). The unit tests in
  ``test/test_ws_recovery_service.py`` already cover happy path /
  failures / idempotency at the unit level; this test pins the clock
  to a realistic IST moment and exercises the recovery through a
  ``BootHarness``-managed Flask app with a real ``mock_zerodha_login``
  seam.

Skipped scenarios
-----------------
None. All four target moments are reachable without modifying service code:
``derive_today_and_yest`` and ``WSRecoveryService.recover`` both accept
injectable inputs, and the seeder exposes ``_read_1m_bars_from_broker`` /
``_resolve_exchange_for_symbol`` as patchable seams.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from services import scanner_aggregator_seeder
from test.fixtures.mid_session import pin_clock_in

# Fixtures (``at_14_30_restart`` &c.) are registered as a pytest plugin via
# ``test/integration/conftest.py`` so they appear in this module's fixture
# namespace without an explicit ``from ... import`` (which would trip
# ruff F811 against the test function parameters of the same name).

# ============================================================================
# 14:30 IST mid-session restart — PR #200 (commit 19b0a4fec)
# ============================================================================


def test_at_14_30_restart_seeds_full_universe_via_broker_fallback(at_14_30_restart):
    """Regression guard: PR #200 (commit ``19b0a4fec``).

    At 14:30 IST mid-session, historify has 1m bars for the sector_follow
    subset but is *physically empty* for the ~195 scanner-only F&O names
    (their backfill runs 15:30-17:00 IST only). The broker-history
    fallback is what fills the gap so the scanner aggregator is fully
    warm for the rest of the session.

    This test pins the moment, sets the asymmetric historify state via
    ``seed_historify_partial``, and asserts:
      * every symbol in the universe ends up seeded
      * the broker fetcher was called *only* for the broker-only universe
        (historify subset must NOT trigger the broker call)

    If a future change drops or neutralises the broker-fallback path,
    seeded_symbols will fall back to ``len(historify_subset)`` and the
    assertion fails loudly.
    """
    state = at_14_30_restart
    extras = state.extras

    full_universe = extras["full_universe"]
    historify_subset = set(extras["historify_subset"])
    broker_only = set(extras["broker_only_universe"])
    broker_bars = extras["make_broker_replay_bars"](200, end_at=state.now)

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    with (
        pin_clock_in("services.scanner_aggregator_seeder", state.now),
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
        patch.object(
            scanner_aggregator_seeder,
            "_resolve_exchange_for_symbol",
            return_value="NSE",
        ),
        patch.dict(
            "os.environ",
            {
                "SCANNER_AGGREGATOR_SEED_ENABLED": "true",
                "SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED": "true",
            },
            clear=False,
        ),
    ):
        summary = scanner_aggregator_seeder.seed_aggregator(mock_agg, full_universe)

    assert summary["seeded_symbols"] == len(full_universe), (
        f"Every symbol must be seeded at 14:30 IST restart; "
        f"got {summary['seeded_symbols']}/{len(full_universe)}, "
        f"empty={summary['empty_symbols']}. "
        f"If empty includes the broker-only universe, the broker fallback "
        f"path (PR #200) regressed."
    )
    assert summary["empty_symbols"] == []

    broker_called_for = {call.args[0] for call in broker_fn.call_args_list}
    assert broker_only.issubset(broker_called_for), (
        f"Broker fallback must fire for the broker-only universe at 14:30 IST; "
        f"called for {broker_called_for}, expected superset of {broker_only}"
    )
    assert broker_called_for.isdisjoint(historify_subset), (
        f"Broker fallback must NOT fire for symbols already in historify; "
        f"called for {broker_called_for & historify_subset}"
    )


# ============================================================================
# 15:10 IST stale-daily SELL evaluation — PR #204 (commit ed7bcc0f0)
# ============================================================================


def test_at_15_10_stale_daily_prefers_live_5m_close_over_frozen_snapshot(at_15_10_stale_daily):
    """Regression guard: PR #204 (commit ``ed7bcc0f0``).

    At 15:10 IST 2026-06-29 the cached ``bars_daily`` (refreshed once at
    boot by ``ScannerHistoryProvider``) carried a *frozen* today-dated
    bar whose close was a boot-time snapshot (2050, ~2% below the prior
    settled close of 2094). The live 5m aggregator had recovered to 2103
    (+0.41%). Pre-#204, ``derive_today_and_yest`` trusted the frozen
    historify ``iloc[-1]`` as today_d → 41 false SELL fires that day
    including TCS while its live LTP was UP.

    The fix: ``derive_today_and_yest`` MUST derive today_d from the
    live 5m close whenever today's 5m bars exist (Path B), only falling
    back to the frozen historify ``iloc[-1]`` when 5m is empty (Path C,
    overnight / pre-open).

    If a future change reverts to trusting ``bars_daily.iloc[-1]`` at
    15:10 IST, ``today_d["close"]`` returns 2050 and the assertion fails.
    """
    from services.scan_rules._today_running import derive_today_and_yest

    state = at_15_10_stale_daily
    daily = state.extras["daily"]
    bars_5m = state.extras["bars_5m"]
    frozen = state.extras["frozen_close"]
    live = state.extras["live_close"]

    today_d, yest_d, yest_idx = derive_today_and_yest(daily, bars_5m, state.now)

    assert today_d is not None, (
        "derive_today_and_yest returned None at 15:10 IST despite a "
        "well-formed 5m frame — the helper regressed."
    )
    assert today_d["close"] == live, (
        f"today_d.close must be the live 5m close ({live}) at 15:10 IST, "
        f"NOT the frozen historify daily snapshot ({frozen}). "
        f"Got {today_d['close']}. PR #204 regressed."
    )
    # iloc[-1] is today-dated → yest_d must be iloc[-2] (the SETTLED bar).
    assert yest_d["close"] == 2094
    assert yest_idx == -2


# ============================================================================
# 09:30 IST cold-boot restart — PR #202 (commit e2b021ac4)
# ============================================================================


def test_at_09_30_cold_start_warms_both_5m_aggregator_and_15m_bars(at_09_30_cold_start):
    """Regression guard: PR #202 (commit ``e2b021ac4``).

    A cold-morning restart at 09:30 IST means historify is empty,
    aggregator is empty, and the 15m bar deque on every symbol is also
    empty. PR #202 made the seeder also seed each ``_Rolling15mBars``
    via ``seed_bars`` — without that, the 15m RSI(14) gate stays cold
    (warm-up < 14 bars) for ~100 minutes after restart, and every scan
    rule call rejects at the warm-up guard while looking identical to
    "no setups".

    This test pins to 09:30 IST, supplies a 1m broker-history stream
    long enough to roll into multiple 15m buckets, and asserts both
    arms fire: ``aggregator.replay_bars`` AND
    ``_Rolling15mBars.seed_bars`` (verified via the returned
    ``seeded_15m_bars`` count and the real deque length).

    If a future change drops the ``bar_15m_history`` parameter from
    ``seed_aggregator`` or skips the ``seed_bars`` call,
    ``seeded_15m_bars`` falls to 0 and the assertion fails.
    """
    from services.scanner_service import _Rolling15mBars

    state = at_09_30_cold_start
    universe = state.extras["universe"]
    # Provide 5 full 15m buckets worth of 1m bars (75 bars: 09:15→09:29 +
    # 09:30→09:44 + 09:45→09:59 + 10:00→10:14 + 10:15→10:29).
    broker_bars = state.extras["make_broker_replay_bars"](75, end_at=state.now)

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    # Real ``_Rolling15mBars`` instances — we want the actual deque side-effect,
    # not a MagicMock, so this is a true integration test of PR #202's wiring.
    bar_15m_history = {sym: _Rolling15mBars(sym) for sym in universe}

    with (
        pin_clock_in("services.scanner_aggregator_seeder", state.now),
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_historify",
            return_value=[],  # cold morning — historify is empty
        ),
        patch.object(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_broker",
            return_value=broker_bars,
        ),
        patch.object(
            scanner_aggregator_seeder,
            "_get_api_key",
            return_value="test-key",  # pragma: allowlist secret
        ),
        patch.object(
            scanner_aggregator_seeder,
            "_resolve_exchange_for_symbol",
            return_value="NSE",
        ),
        patch.dict(
            "os.environ",
            {
                "SCANNER_AGGREGATOR_SEED_ENABLED": "true",
                "SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED": "true",
            },
            clear=False,
        ),
    ):
        summary = scanner_aggregator_seeder.seed_aggregator(
            mock_agg, universe, bar_15m_history=bar_15m_history
        )

    # 5m aggregator fed: replay_bars called per symbol.
    assert summary["seeded_symbols"] == len(universe), (
        f"5m aggregator must be seeded for the full universe at 09:30 IST "
        f"cold start; got {summary['seeded_symbols']}/{len(universe)}"
    )
    # 15m bars seeded: PR #202 — the entire bug.
    assert summary["seeded_15m_bars"] > 0, (
        "PR #202 regression: seeder did not feed the 15m bar deques. "
        "The 15m RSI(14) gate will stay cold for ~100 min after restart. "
        f"Summary: {summary}"
    )
    # Real deques carry the seeded bars (not just the MagicMock count).
    for sym in universe:
        bars_15m_df = bar_15m_history[sym].get_recent_bars(50)
        assert not bars_15m_df.empty, (
            f"_Rolling15mBars[{sym}] deque is still empty after seed — "
            f"the 15m warm-up gate will fail on the first close."
        )


# ============================================================================
# 10:00 IST post-broker-relogin — WS reinit class (Fix B-prime)
# ============================================================================


def test_at_10_00_post_relogin_replays_missed_bars_into_aggregator(at_10_00_post_relogin):
    """Regression guard: WS reinit class (issue #129 / Fix B-prime).

    A Zerodha re-login at 10:00 IST emits a ``CACHE_INVALIDATE`` ZMQ
    event that triggers WS reinit; the in-process
    ``WSRecoveryService`` consumes the ``BrokerSessionRefreshedEvent``
    and folds missed 1m bars from the broker historical API into the
    live scanner aggregator. Without it the aggregator silently warms
    up from scratch on every reconnect — the 2026-06-11/12
    tick-starvation collapse class.

    This test pins to 10:00 IST, performs a ``BootHarness.mock_zerodha_login``
    so ``auth_db`` carries a fresh token (the same state the recovery
    service sees in production), wires up ``WSRecoveryService`` with
    a stub history-fetcher and a stub aggregator, and calls
    ``recover()`` directly. Assertion: every symbol in the universe
    saw its bars replayed via ``aggregator.replay_bars`` — i.e. the
    recovery is wired end-to-end.

    If the recovery service stops resolving the aggregator, stops
    iterating the universe, or stops calling ``replay_bars``, this
    test fails.
    """
    from services.ws_recovery_service import WSRecoveryService

    state = at_10_00_post_relogin
    universe = state.extras["universe"]  # list of (symbol, exchange)
    bars_per_symbol = state.extras["make_broker_replay_bars"](20, end_at=state.now)

    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    captured_alerts: list[str] = []

    # Gap-detection gate (issue #258): recovery only runs after a genuine
    # mid-session reconnect. This scenario pins 10:00 IST post-relogin with a
    # live feed that was delivering bars until the drop — model that with a
    # clock at the pinned IST moment and a last-known-good bar during the
    # session (09:30 IST), so the gate resolves to "gap" and recovery runs.
    import datetime as _dt

    pinned = state.now.replace(tzinfo=None)  # naive IST wall-clock the box would read

    def _clock() -> _dt.datetime:
        return pinned

    def _last_known_ts() -> _dt.datetime:
        return pinned.replace(hour=9, minute=30, second=0, microsecond=0)

    svc = WSRecoveryService(
        aggregator_provider=lambda: mock_agg,
        universe_provider=lambda: list(universe),
        history_fetcher=lambda symbol, exchange, api_key, lookback_min: bars_per_symbol,
        api_key_provider=lambda: "test-key",  # pragma: allowlist secret
        notifier=captured_alerts.append,
        lookback_min=20,
        clock=_clock,
        last_known_ts_provider=_last_known_ts,
    )

    # mock_zerodha_login (the fixture already called it) gives the API-key
    # provider something real to point at in production; here we stub the
    # provider directly so the test never depends on the auth_db lookup
    # working in the harness's temp DB.
    summary = svc.recover(username="admin", broker="zerodha")

    assert summary["status"] == "ok", (
        f"WS recovery must complete cleanly at 10:00 IST post-relogin; got {summary!r}"
    )
    assert summary["resynced"] == len(universe), (
        f"Every universe symbol must have its missed bars replayed; "
        f"got resynced={summary['resynced']} of {len(universe)}"
    )
    assert summary["bars_replayed"] == len(universe) * len(bars_per_symbol)

    # Every symbol saw a replay_bars call — the recovery is wired to the
    # aggregator, not silently no-op'd.
    replayed_for = {call.args[0] for call in mock_agg.replay_bars.call_args_list}
    expected = {sym for sym, _ in universe}
    assert replayed_for == expected, (
        f"replay_bars must be called for every universe symbol; "
        f"replayed={replayed_for}, expected={expected}"
    )

    # The recovery service always emits one Telegram-shaped alert.
    assert captured_alerts, "WS recovery must emit one summary alert on completion"


# ============================================================================
# Sanity guards (catch fixture regressions before they mask real failures)
# ============================================================================


def test_each_fixture_pins_a_distinct_ist_moment(
    at_14_30_restart, at_15_10_stale_daily, at_09_30_cold_start, at_10_00_post_relogin
):
    """Belt-and-braces: confirm all four fixtures yield distinct IST moments
    and are tz-aware. If a future refactor sets one to ``datetime.now()`` the
    scenario stops being deterministic and this assertion fails first."""
    moments = {
        "at_14_30_restart": at_14_30_restart.now,
        "at_15_10_stale_daily": at_15_10_stale_daily.now,
        "at_09_30_cold_start": at_09_30_cold_start.now,
        "at_10_00_post_relogin": at_10_00_post_relogin.now,
    }
    for name, m in moments.items():
        assert m.tzinfo is not None, f"{name}.now must be tz-aware (got naive {m!r})"
    assert len({m.replace(microsecond=0) for m in moments.values()}) == len(moments), (
        f"Fixtures must pin distinct moments; got {moments}"
    )
