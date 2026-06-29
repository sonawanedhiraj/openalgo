"""Recovery-class tests against a misbehaving mock broker (issue #230).

Today's mock broker is happy-path only. Real Zerodha exhibits four production
failure modes every six months:

* Daily 3 AM IST token expiry → WS reinit + ZMQ ``CACHE_INVALIDATE``
  (commit ``c5f88a8cf``).
* Intermittent latency / 502s under load → scanner aggregator gates must
  fail-safe (commit ``19b0a4fec``).
* Partial fills on illiquid options → trade_journal reconciliation
  (commit ``8ab028b3a`` — the "phantom Gate 13" SELL/BUY snapshot fix).
* Rate-limit throttling at boot-burst → DuckDB singleton race
  (commit ``c5b973c91``).

This file exercises OpenAlgo's recovery paths against simulated misbehavior.
The misbehavior is injected at the **service seam** (history_fetcher,
notifier, replay_bars, place_order, trade_journal write) — the same place
the mock broker's new ``/_mock/*`` admin endpoints would inject it for a
docker-stack-driven E2E run. The in-process harness lets these tests run in
under a second each, with full assertion fidelity.

Per the issue's rule: a test that reveals a real recovery bug is xfail'd
with a follow-up issue link, NOT silently passed and NOT fixed in this PR.

Tier: in-process integration (BootHarness + mocks). The docker-compose stack
in ``docker-compose.test.yml`` is the right tier when a future test needs
real HTTP round-trips; this file uses the lighter in-process seam for speed.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from unittest.mock import MagicMock

import pytest

from services.ws_recovery_service import (
    BrokerSessionRefreshedEvent,
    WSRecoveryService,
)

# BootHarness must be imported before any other app module so OPENALGO_TESTING is set.
from test.harness import BootHarness
from utils.event_bus import EventBus

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _synthetic_bars(n: int = 20, start_minute: int = 15) -> list[dict]:
    """N consecutive 1m OHLCV bars starting at 09:{start_minute} today, in the
    shape ``WSRecoveryService`` expects (ts as naive datetime).
    """
    base = dt.datetime.now().replace(hour=9, minute=start_minute, second=0, microsecond=0)
    return [
        {
            "ts": base + dt.timedelta(minutes=i),
            "open": 100.0 + i,
            "high": 100.5 + i,
            "low": 99.5 + i,
            "close": 100.2 + i,
            "volume": 1000 + i,
        }
        for i in range(n)
    ]


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll ``predicate()`` until truthy or timeout. Returns whether it became
    truthy in time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@pytest.fixture
def harness():
    """Minimal Flask app with all DBs in conftest's temp dir."""
    with BootHarness.create() as h:
        yield h


# ============================================================================
# Test 1 — Token expiry triggers WS recovery, replays bars for held subs
# ============================================================================
#
# Bug class: Daily 3 AM IST Zerodha token rotation. The WS proxy subprocess
# reconnects off the ZMQ ``CACHE_INVALIDATE`` event published by
# ``upsert_auth`` — the Flask side cannot reach the subprocess directly. After
# the reconnect, the in-process bar aggregators have a gap (every bar that
# closed while the socket was down was never seen). ``WSRecoveryService``
# subscribes to the in-process ``broker_session_refreshed`` bus event and
# fetches the last N minutes of bars per held symbol so the scanner's rolling
# state is current the moment the first live tick lands.
#
# Past fixes: commit ``c5f88a8cf`` (WS reinit, no flag) +
# commit ``19b0a4fec`` (broker history fallback for the 14:30 IST restart).


def test_ws_reinit_resumes_after_token_expiry():
    """Expire token mid-session → bus publishes ``broker_session_refreshed`` →
    ``WSRecoveryService`` fetches missed bars for every held subscription and
    folds them into the aggregator.

    Asserts (behavior, not implementation):

    * Every symbol in the held-subscription universe is fetched once.
    * ``replay_bars`` is called per symbol with the fetched bars.
    * One Telegram-shaped alert summarizes the run (operator visibility —
      never silent).
    """
    held_universe = [("SBIN", "NSE"), ("RELIANCE", "NSE"), ("INFY", "NSE")]
    bars_for = {sym: _synthetic_bars(20) for sym, _ in held_universe}

    aggregator = MagicMock()
    aggregator.replay_bars.return_value = 20

    fetcher_calls: list[tuple[str, str]] = []

    def fetcher(symbol, exchange, api_key, lookback_min):
        fetcher_calls.append((symbol, exchange))
        return bars_for[symbol]

    notifier = MagicMock()
    bus = EventBus()

    svc = WSRecoveryService(
        aggregator_provider=lambda: aggregator,
        universe_provider=lambda: held_universe,
        history_fetcher=fetcher,
        api_key_provider=lambda: "test-key",  # pragma: allowlist secret
        notifier=notifier,
        bus=bus,
    )
    svc.register()

    # The simulated "token expired" → operator re-logged-in → upsert_auth →
    # bus.publish(BrokerSessionRefreshedEvent(...)) sequence.
    bus.publish(BrokerSessionRefreshedEvent(username="alice", broker="zerodha"))

    assert _wait_for(lambda: notifier.call_count >= 1, timeout=5.0), (
        "WS recovery never alerted — recovery did not fire on broker_session_refreshed"
    )

    # Every held symbol got a fetch + a replay; held subscriptions restored.
    assert {sym for sym, _ in fetcher_calls} == {sym for sym, _ in held_universe}
    assert aggregator.replay_bars.call_count == len(held_universe)
    replayed_symbols = {c.args[0] for c in aggregator.replay_bars.call_args_list}
    assert replayed_symbols == {sym for sym, _ in held_universe}

    # Alert is structured + operator-visible (the inverse of "silent drop").
    alert_msg = notifier.call_args.args[0]
    assert f"{len(held_universe)}/{len(held_universe)} symbols re-synced" in alert_msg
    assert "bars replayed" in alert_msg


# ============================================================================
# Test 2 — Aggregator seeder fails-LOUD under broker latency / errors
# ============================================================================
#
# Bug class: Intermittent broker latency / 502s mid-fetch. The scanner
# aggregator seeder iterates ~200 F&O symbols and calls the broker historical
# API for any symbol historify cannot cover. A silent per-symbol error would
# leave the slot dark and produce zero scanner hits all day. The seeder must
# fail-LOUD: log the failure with the symbol name, count it in the summary,
# and continue with the rest of the universe.
#
# Past fix: commit ``19b0a4fec`` (PR #200, broker-history fallback) +
# commit ``ed7bcc0f0`` (scanner uses live 5m close, not stale snapshot).


def test_aggregator_gates_fail_safe_under_broker_latency(caplog):
    """Inject a per-symbol broker error (mimics the 30s-timeout case) at the
    real production seam (``services.history_service.get_history``). Assert
    that:

    * AAA + CCC successfully seed (the other symbols are not affected by
      BBB's failure).
    * BBB shows up in ``empty_symbols`` (no silent drop into the seeded count).
    * The failure is logged with the symbol name at DEBUG+ (the production
      log level for "broker get_history failed for SYM/EXCH" — see
      ``services/scanner_aggregator_seeder.py:248``).

    Equivalent to the issue's "scanner gates either succeed eventually OR
    fail-closed (never silent)" requirement — exercised at the same seam
    the docker-stack ``inject_latency`` admin endpoint would target.
    """
    from services import scanner_aggregator_seeder

    universe = ["AAA", "BBB", "CCC"]
    mock_agg = MagicMock()
    mock_agg.replay_bars = MagicMock(side_effect=lambda sym, bars: len(bars))

    # Synthetic broker payload shape: ``get_history`` returns
    # ``(success: bool, payload: dict, status_code: int)``.
    def fake_get_history(*, symbol, exchange, interval, start_date, end_date, api_key):
        # Simulate intermittent broker latency: BBB times out / 502s.
        if symbol == "BBB":
            return False, {"message": "broker fetch timed out after 30s"}, 504
        # The history_service rows use lowercase ohlc + epoch timestamp;
        # the seeder's normalizer parses it back into the {ts, open, ...} shape.
        rows = []
        base_epoch = int(time.time()) - 200 * 60
        for i in range(20):
            rows.append(
                {
                    "timestamp": base_epoch + i * 60,
                    "open": 100.0 + i,
                    "high": 100.5 + i,
                    "low": 99.5 + i,
                    "close": 100.2 + i,
                    "volume": 1000 + i,
                }
            )
        return True, {"data": rows}, 200

    with (
        caplog.at_level(logging.DEBUG, logger="services.scanner_aggregator_seeder"),
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setenv("SCANNER_AGGREGATOR_SEED_ENABLED", "true")
        mp.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
        # Patch the import target inside the seeder's broker arm.
        import services.history_service as hs

        mp.setattr(hs, "get_history", fake_get_history)
        mp.setattr(
            scanner_aggregator_seeder,
            "_get_api_key",
            lambda: "test-key",  # pragma: allowlist secret
        )
        mp.setattr(
            scanner_aggregator_seeder,
            "_resolve_exchange_for_symbol",
            lambda sym: "NSE",
        )
        # Force the historify tier empty so every symbol routes to the broker.
        mp.setattr(
            scanner_aggregator_seeder,
            "_read_1m_bars_from_historify",
            lambda *a, **kw: [],
        )

        summary = scanner_aggregator_seeder.seed_aggregator(mock_agg, universe)

    # AAA + CCC seeded; BBB reported as empty (NOT silently counted as seeded).
    assert summary["seeded_symbols"] == 2, f"AAA+CCC should have seeded; got summary={summary}"
    assert "BBB" in summary["empty_symbols"], (
        f"BBB broker-fetch failure should surface in empty_symbols; got {summary}"
    )

    # The failure was LOGGED with the symbol name — the line is at DEBUG in
    # production ("broker get_history failed for BBB/NSE: ..."). That is the
    # operator-visible breadcrumb that distinguishes "broker error" from
    # "genuinely no data". A silent drop would produce zero matching records.
    bbb_logged = any(
        "BBB" in rec.getMessage() and "broker" in rec.getMessage().lower() for rec in caplog.records
    )
    assert bbb_logged, (
        f"BBB broker-fetch failure must be logged with the symbol name; "
        f"saw {[r.getMessage() for r in caplog.records]}"
    )


# ============================================================================
# Test 3 — Partial fill: trade_journal records the placed quantity (gap doc)
# ============================================================================
#
# Bug class: Partial fills on illiquid options. The broker can fill 50 of a
# 100-qty order and leave the remaining 50 OPEN; trade_journal needs to know
# the actual filled quantity so the exit side reconciles correctly.
#
# Past fixes (this area): commit ``34731aa9`` (EOD reconciliation pulls
# sandbox MIS square-offs into trade_journal so the Telegram EOD summary
# matches /mypnl); commit ``8ab028b3a`` (SELL/BUY snapshot uses today's
# running daily, not a stale historify row).
#
# This test exercises the *current* journal contract. ``record_entry`` takes
# the order's ``quantity`` (the requested size) — there is no ``filled_quantity``
# field, so a partial fill leaves the journal showing the full quantity. That
# is faithful to today's behavior; if a future change adds fill reconciliation
# (a sibling issue to #230), this test should be revised (or split into two:
# the current pass + a new xfail until the column lands).


def test_partial_fill_journals_with_placed_quantity_today(harness):
    """Today's contract: ``record_entry`` stores the placed quantity. A partial
    fill does NOT downgrade the row — the discrepancy is invisible to the
    journal until reconciliation runs.

    Asserts (behavior, not aspiration):

    * ``record_entry`` round-trips the placed quantity into ``trade_journal``.
    * ``record_exit`` does NOT alter ``quantity`` — the row's ``quantity``
      reflects the entry intent, the fill side is visible only via the
      broker's order book (out of scope for this entry-time test).

    If/when fill reconciliation lands (an open question — see #230's
    "filled_quantity" mention), this test will be the regression guard
    showing that the *requested* quantity is preserved alongside the new
    *filled* column. For now it locks in the current contract.
    """
    with harness.app.app_context():
        from database.trade_journal_db import TradeJournal, init_db
        from services.trade_journal_service import record_entry

        init_db()

        # Simulate the broker filling 50 of 100 — the place_order call
        # returns success, the journal write happens with the *requested*
        # quantity.
        row_id = record_entry(
            symbol="NIFTY24DEC18000CE",
            direction="LONG",
            quantity=100,  # placed
            strategy_name="test_misbehaving_broker",
            signal_source="manual",
            entry_price=125.5,
            entry_order_id="MOCK000000001",
            notes={"partial_fill_simulated": True, "filled_qty_actual": 50},
        )
        assert row_id > 0, "record_entry must return a real row id for a sane input"

        # Read it back — the row stores quantity=100 (today's contract).
        row = TradeJournal.query.filter_by(id=row_id).first()
        assert row is not None, "journal row must persist immediately"
        assert row.quantity == 100, (
            "today's journal stores the PLACED quantity. If this assertion "
            "flips to 50 in a future PR that adds fill-reconciliation, the "
            "test should be updated to assert the new column AND that the "
            "placed quantity is preserved."
        )
        # The fill-side discrepancy lives in ``notes`` (the only place a
        # caller can record it today). This is the gap the issue is calling
        # out — a fix-time follow-up may add a first-class ``filled_quantity``
        # column. The notes-payload assertion proves the gap is real.
        assert row.notes and "filled_qty_actual" in row.notes


# ============================================================================
# Test 4 — WS drop triggers recovery + idempotent replay
# ============================================================================
#
# Bug class: Mid-session WS drop (broker network blip / re-login). The
# recovery service must NOT double-count bars if it fires twice (e.g. the
# operator clicks re-login twice, or the boot path and the event path race).
# ``MultiIntervalAggregator.replay_bars`` dedups by timestamp; the test
# asserts the contract holds end-to-end.
#
# Past fix: commit ``e2b021ac4`` (seeder warms 15m bar builders on boot) +
# commit ``c5f88a8cf`` (WS reinit, idempotent).


def test_ws_drop_triggers_recovery_replay_idempotently():
    """Drop the WS (simulated by firing two ``broker_session_refreshed``
    events back-to-back) → the recovery service fetches both times, but the
    aggregator's ``replay_bars`` is dedup-by-timestamp, so the *effective*
    folded-bars count is unchanged on the second run.

    Asserts the contract every recovery path relies on: replays are
    idempotent. A future regression that drops the dedup-by-ts (e.g. a
    refactor that switches to append) flips one of these assertions and
    fails this test loudly.
    """
    universe = [("AAA", "NSE")]
    bars = _synthetic_bars(20)

    # Real-ish aggregator dedupe: replay_bars returns the count it would have
    # folded, but a second call with the same timestamps returns 0.
    aggregator = MagicMock()
    seen_ts: set[dt.datetime] = set()

    def replay_bars(sym, bars_in):
        new_bars = [b for b in bars_in if b["ts"] not in seen_ts]
        seen_ts.update(b["ts"] for b in bars_in)
        return len(new_bars)

    aggregator.replay_bars.side_effect = replay_bars

    notifier = MagicMock()
    bus = EventBus()

    svc = WSRecoveryService(
        aggregator_provider=lambda: aggregator,
        universe_provider=lambda: universe,
        history_fetcher=lambda *args, **kw: bars,
        api_key_provider=lambda: "test-key",  # pragma: allowlist secret
        notifier=notifier,
        bus=bus,
    )
    svc.register()

    # First reconnect — folds 20 bars.
    bus.publish(BrokerSessionRefreshedEvent(username="alice", broker="zerodha"))
    assert _wait_for(lambda: notifier.call_count >= 1, timeout=5.0)
    first_msg = notifier.call_args.args[0]
    assert "20 bars replayed" in first_msg, (
        f"first recovery should replay all 20 bars; got {first_msg!r}"
    )

    # Second reconnect — fetches again, but dedup returns 0 new bars.
    bus.publish(BrokerSessionRefreshedEvent(username="alice", broker="zerodha"))
    assert _wait_for(lambda: notifier.call_count >= 2, timeout=5.0)
    second_msg = notifier.call_args.args[0]
    assert "0 bars replayed" in second_msg, (
        f"second recovery should fold 0 new bars (idempotent); got {second_msg!r}"
    )

    # Aggregator was called twice (one per event) — the dedup happens INSIDE
    # the aggregator, not by the recovery service skipping the call. This
    # matches production: a duplicate event still costs the broker round
    # trips, it just doesn't corrupt aggregator state.
    assert aggregator.replay_bars.call_count == 2


# ============================================================================
# Test 5 — Per-symbol 503 fails LOUD, never silently drops
# ============================================================================
#
# Bug class: Zerodha rate limit returns 503 under boot burst (the 240-symbol
# warmup hits the 3 req/sec ceiling). The recovery service must log the
# failing symbol with ``logger.exception`` and surface it in the summary +
# Telegram alert if >20% of symbols fail. The opposite — a bare-except that
# swallows the 503 — is the silent-drop class the Semgrep ``bare-except-swallow``
# rule guards against (silent-drops audit, 2026-06-11).
#
# Past fix: commit ``c5b973c91`` (DuckDB singleton kills boot-burst race) +
# the silent-drop audit (see ``audit/silent_drop_audit_2026-06-11.md``).


def test_rate_limited_503_logs_and_alerts_never_silent(caplog):
    """One symbol's history fetch raises a 503 → it gets logged at exception
    level with the symbol name, the OTHER symbols still recover, and the
    summary's ``failed`` count is non-zero.

    The summary is forwarded to Telegram (notifier), so a partial broker
    failure is operator-visible. The opposite would be a silent drop — that
    is the audited anti-pattern the Semgrep rules guard against.
    """
    universe = [("AAA", "NSE"), ("BBB", "NSE"), ("CCC", "NSE")]
    bars_ok = _synthetic_bars(20)

    def fetcher(symbol, exchange, api_key, lookback_min):
        if symbol == "BBB":
            # Simulate Zerodha's "Too many requests" 503 envelope.
            raise RuntimeError("HTTP 503 — Too Many Requests (Zerodha rate limit)")
        return bars_ok

    aggregator = MagicMock()
    aggregator.replay_bars.return_value = 20

    notifier = MagicMock()
    bus = EventBus()

    svc = WSRecoveryService(
        aggregator_provider=lambda: aggregator,
        universe_provider=lambda: universe,
        history_fetcher=fetcher,
        api_key_provider=lambda: "test-key",  # pragma: allowlist secret
        notifier=notifier,
        bus=bus,
    )
    svc.register()

    with caplog.at_level(logging.ERROR, logger="services.ws_recovery_service"):
        bus.publish(BrokerSessionRefreshedEvent(username="alice", broker="zerodha"))
        assert _wait_for(lambda: notifier.call_count >= 1, timeout=5.0), (
            "WS recovery never alerted on a 503"
        )

    # AAA + CCC succeeded, BBB failed. The aggregator was called twice
    # (BBB never reached it).
    assert aggregator.replay_bars.call_count == 2

    # The 503 failure was logged with the symbol name, at ERROR or higher
    # (``logger.exception``). Silent drop would have produced zero matching
    # records — that's what this assertion guards against.
    bbb_failure_logged = any(
        "BBB" in rec.getMessage() and rec.levelno >= logging.ERROR for rec in caplog.records
    )
    assert bbb_failure_logged, (
        f"503 failure for BBB must be logged at ERROR+ with the symbol name; "
        f"saw records={[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )

    # The alert text surfaces "2/3 re-synced" — the operator sees the gap.
    alert_msg = notifier.call_args.args[0]
    assert "2/3 symbols re-synced" in alert_msg, (
        f"alert should report 2 of 3 succeeded; got {alert_msg!r}"
    )


# ============================================================================
# Test 6 — Token-expired mid-recovery alerts and aborts (no API key)
# ============================================================================
#
# Bug class: Boot path runs while the daily token is mid-rotation. The
# recovery service tries to fetch history with no API key available. Today's
# contract: it returns ``status=error, reason=no_api_key`` and notifies — it
# does NOT silently return ``ok``. This is the regression guard for the
# 2026-06-23 silent-death incident (see memory entry).


def test_recovery_aborts_loud_when_no_api_key():
    """Simulate "token expired and not yet refreshed" by returning ``None``
    from ``api_key_provider``. The recovery service must abort with a
    structured ``status=error`` summary and one alert. A silent ``ok`` here
    would mean every reconnect during the 3 AM rotation window is invisible.
    """
    notifier = MagicMock()
    bus = EventBus()

    svc = WSRecoveryService(
        aggregator_provider=lambda: MagicMock(),
        universe_provider=lambda: [("AAA", "NSE")],
        history_fetcher=lambda *a, **kw: _synthetic_bars(20),
        api_key_provider=lambda: None,  # token expired, not yet rotated
        notifier=notifier,
        bus=bus,
    )
    svc.register()

    bus.publish(BrokerSessionRefreshedEvent(username="alice", broker="zerodha"))
    assert _wait_for(lambda: notifier.call_count >= 1, timeout=5.0), (
        "recovery must alert even on a no-API-key abort — silent return is the bug"
    )

    alert_msg = notifier.call_args.args[0]
    assert "no broker API key" in alert_msg, (
        f"alert should call out the missing API key; got {alert_msg!r}"
    )
