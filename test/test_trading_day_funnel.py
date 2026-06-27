"""Hermetic E2E for the trading-day funnel diagnostic (issue #159).

Exercises ``services.trading_day_funnel_service.run_funnel_for_date`` end-to-end
against in-memory SQLite copies of the four tables it queries — ``scan_results``
(``scanner_db``), ``signal_decision`` (``signal_decision_db``), ``trade_journal``
(``trade_journal_db``), ``sandbox_orders`` (``sandbox_db``) — plus a mocked
notification service. The global ``test/conftest.py`` guard keeps every engine
off the live DBs regardless of what this file does.

Coverage:

* compute_funnel returns the right shape on an empty day,
* per-source breakdown of scanner hits is correct (inhouse vs chartink, dedup),
* signal_decision actually_taken split and first-dropped sample,
* per-strategy order/fill/open-EOD breakdown in trade_journal,
* sandbox_orders cross-check counts only today's rows,
* _format_telegram renders the empty day, partial day, and DB-error day
  (where individual counters degrade to ``?``) without raising,
* drop-arrow zero-denominator is rendered ``K/0`` not a divide-by-zero,
* run_funnel_for_date dispatches through notification_service.notify with the
  ``trading_day_funnel`` event_type and tolerates dispatch failure,
* _funnel_job respects the per-fire ``TRADING_DAY_FUNNEL_ENABLED`` env flag,
* register_jobs is idempotent (``replace_existing=True``).
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

DATE = "2026-06-26"


def _mk(module):
    """Rebind ``module`` to a fresh in-memory engine and create its tables."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    module.Base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def empty_day(monkeypatch):
    """All four tables exist but contain no rows for ``DATE``."""
    from database import sandbox_db, scanner_db, signal_decision_db, trade_journal_db

    for mod in (scanner_db, signal_decision_db, trade_journal_db, sandbox_db):
        eng, sess = _mk(mod)
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", sess)
    return None


@pytest.fixture
def active_day(monkeypatch):
    """Realistic mixed day: hits from both sources, vetoes, two strategies."""
    from database import sandbox_db, scanner_db, signal_decision_db, trade_journal_db

    bindings = {}
    for mod in (scanner_db, signal_decision_db, trade_journal_db, sandbox_db):
        eng, sess = _mk(mod)
        bindings[mod.__name__] = (eng, sess)
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", sess)

    # --- scan_results: 2 inhouse rows (overlapping symbol) + 1 chartink row.
    scan_sess = bindings[scanner_db.__name__][1]
    scan_sess.add_all(
        [
            scanner_db.ScanResult(
                scan_definition_id=1,
                run_at=f"{DATE}T10:00:00+05:30",
                symbols='["TCS", "INFY"]',
                source="inhouse",
                posted_to_engine=1,
            ),
            scanner_db.ScanResult(
                scan_definition_id=1,
                run_at=f"{DATE}T11:00:00+05:30",
                symbols='["INFY", "WIPRO"]',  # INFY overlaps, must dedup
                source="inhouse",
                posted_to_engine=1,
            ),
            scanner_db.ScanResult(
                scan_definition_id=2,
                run_at=f"{DATE}T12:00:00+05:30",
                symbols='["RELIANCE"]',
                source="chartink",
                posted_to_engine=1,
            ),
            # Yesterday — must be excluded.
            scanner_db.ScanResult(
                scan_definition_id=1,
                run_at="2026-06-25T10:00:00+05:30",
                symbols='["OLD"]',
                source="inhouse",
                posted_to_engine=1,
            ),
        ]
    )
    scan_sess.commit()

    # --- signal_decision: 2 taken + 1 vetoed today, plus yesterday noise.
    sd_sess = bindings[signal_decision_db.__name__][1]
    sd_sess.add_all(
        [
            signal_decision_db.SignalDecision(
                candidate_at=f"{DATE}T12:30:00+05:30",
                symbol="TCS",
                source="inhouse_scanner",
                decision="take",
                enforcement_mode="active",
                actually_taken=1,
                direction="BUY",
            ),
            signal_decision_db.SignalDecision(
                candidate_at=f"{DATE}T12:45:00+05:30",
                symbol="RELIANCE",
                source="chartink_FnO_intraday_buy",
                decision="take",
                enforcement_mode="active",
                actually_taken=1,
                direction="BUY",
            ),
            signal_decision_db.SignalDecision(
                candidate_at=f"{DATE}T13:00:00+05:30",
                symbol="ZYDUSLIFE",
                source="chartink_FnO_intraday_buy",
                decision="skip",
                enforcement_mode="active",
                actually_taken=0,
                reasoning="Sector conflict with NIFTYPHARMA dominance",
                direction="SELL",
            ),
            signal_decision_db.SignalDecision(
                candidate_at="2026-06-25T12:00:00+05:30",
                symbol="OLD",
                source="inhouse_scanner",
                decision="take",
                enforcement_mode="active",
                actually_taken=1,
                direction="BUY",
            ),
        ]
    )
    sd_sess.commit()

    # --- trade_journal: 2 strategies, mixed fill / open / closed states.
    tj_sess = bindings[trade_journal_db.__name__][1]
    tj_sess.add_all(
        [
            # strategy A: 1 filled + closed
            trade_journal_db.TradeJournal(
                placed_at=f"{DATE}T12:31:00+05:30",
                symbol="TCS",
                direction="LONG",
                quantity=10,
                strategy_name="trending_equity_intraday",
                signal_source="inhouse_scanner",
                entry_price=4000.0,
                entry_fill_at=f"{DATE}T12:31:01+05:30",
                exit_price=4050.0,
                exited_at=f"{DATE}T15:00:00+05:30",
                exit_reason="target_hit",
                created_at=f"{DATE}T12:31:00+05:30",
                updated_at=f"{DATE}T15:00:00+05:30",
            ),
            # strategy A: 1 filled, still open at EOD
            trade_journal_db.TradeJournal(
                placed_at=f"{DATE}T12:46:00+05:30",
                symbol="RELIANCE",
                direction="LONG",
                quantity=5,
                strategy_name="trending_equity_intraday",
                signal_source="chartink_FnO_intraday_buy",
                entry_price=2900.0,
                entry_fill_at=f"{DATE}T12:46:01+05:30",
                exit_price=None,
                created_at=f"{DATE}T12:46:00+05:30",
                updated_at=f"{DATE}T12:46:01+05:30",
            ),
            # strategy B: 1 placed but never filled
            trade_journal_db.TradeJournal(
                placed_at=f"{DATE}T15:20:00+05:30",
                symbol="HDFCBANK",
                direction="LONG",
                quantity=20,
                strategy_name="sector_follow_cap5_vol",
                signal_source="sector_follow",
                entry_price=1700.0,
                entry_fill_at=None,
                exit_price=None,
                created_at=f"{DATE}T15:20:00+05:30",
                updated_at=f"{DATE}T15:20:00+05:30",
            ),
            # Yesterday — excluded.
            trade_journal_db.TradeJournal(
                placed_at="2026-06-25T11:00:00+05:30",
                symbol="OLD",
                direction="LONG",
                quantity=1,
                strategy_name="trending_equity_intraday",
                signal_source="inhouse_scanner",
                entry_price=100.0,
                entry_fill_at="2026-06-25T11:00:01+05:30",
                exit_price=110.0,
                created_at="2026-06-25T11:00:00+05:30",
                updated_at="2026-06-25T11:00:01+05:30",
            ),
        ]
    )
    tj_sess.commit()

    # --- sandbox_orders: 3 today (matching trade_journal entries) + 1 yesterday.
    sb_sess = bindings[sandbox_db.__name__][1]
    sb_sess.add_all(
        [
            sandbox_db.SandboxOrders(
                orderid="O1",
                user_id="u",
                strategy="trending_equity_intraday",
                symbol="TCS",
                exchange="NSE",
                action="BUY",
                quantity=10,
                price=4000,
                trigger_price=0,
                price_type="MARKET",
                product="MIS",
                order_status="complete",
                average_price=4000,
                filled_quantity=10,
                pending_quantity=0,
                margin_blocked=0,
                order_timestamp=_dt.datetime(2026, 6, 26, 12, 31, 0),
                update_timestamp=_dt.datetime(2026, 6, 26, 12, 31, 1),
            ),
            sandbox_db.SandboxOrders(
                orderid="O2",
                user_id="u",
                strategy="trending_equity_intraday",
                symbol="RELIANCE",
                exchange="NSE",
                action="BUY",
                quantity=5,
                price=2900,
                trigger_price=0,
                price_type="MARKET",
                product="MIS",
                order_status="complete",
                average_price=2900,
                filled_quantity=5,
                pending_quantity=0,
                margin_blocked=0,
                order_timestamp=_dt.datetime(2026, 6, 26, 12, 46, 0),
                update_timestamp=_dt.datetime(2026, 6, 26, 12, 46, 1),
            ),
            sandbox_db.SandboxOrders(
                orderid="O3",
                user_id="u",
                strategy="sector_follow_cap5_vol",
                symbol="HDFCBANK",
                exchange="NSE",
                action="BUY",
                quantity=20,
                price=1700,
                trigger_price=0,
                price_type="MARKET",
                product="CNC",
                order_status="open",
                average_price=0,
                filled_quantity=0,
                pending_quantity=20,
                margin_blocked=34000,
                order_timestamp=_dt.datetime(2026, 6, 26, 15, 20, 0),
                update_timestamp=_dt.datetime(2026, 6, 26, 15, 20, 0),
            ),
            sandbox_db.SandboxOrders(
                orderid="O0",
                user_id="u",
                strategy="trending_equity_intraday",
                symbol="OLD",
                exchange="NSE",
                action="BUY",
                quantity=1,
                price=100,
                trigger_price=0,
                price_type="MARKET",
                product="MIS",
                order_status="complete",
                average_price=100,
                filled_quantity=1,
                pending_quantity=0,
                margin_blocked=0,
                order_timestamp=_dt.datetime(2026, 6, 25, 11, 0, 0),
                update_timestamp=_dt.datetime(2026, 6, 25, 11, 0, 1),
            ),
        ]
    )
    sb_sess.commit()

    return bindings


# --------------------------------------------------------------------------- #
# compute_funnel — per-layer shape and date filtering
# --------------------------------------------------------------------------- #


def test_compute_funnel_empty_day_returns_zeroes(empty_day):
    from services.trading_day_funnel_service import compute_funnel

    r = compute_funnel(DATE)
    assert r["date"] == DATE
    assert r["hits"] == {"inhouse": 0, "chartink": 0, "total": 0, "by_source": {}}
    assert r["signals"]["total"] == 0
    assert r["signals"]["first_dropped"] is None
    assert r["strategies"] == {}
    assert r["sandbox"] == {"total": 0, "by_strategy": {}}


def test_compute_funnel_active_day_counts_match(active_day):
    from services.trading_day_funnel_service import compute_funnel

    r = compute_funnel(DATE)
    # Hits: 3 distinct inhouse (TCS, INFY, WIPRO — INFY dedup'd) + 1 chartink.
    assert r["hits"]["inhouse"] == 3
    assert r["hits"]["chartink"] == 1
    assert r["hits"]["total"] == 4

    # Signals: 3 today (2 taken, 1 vetoed). Yesterday row excluded.
    assert r["signals"]["total"] == 3
    assert r["signals"]["taken"] == 2
    assert r["signals"]["vetoed"] == 1

    # Strategies: 2 distinct, with the right per-strategy mix.
    s = r["strategies"]
    assert set(s.keys()) == {"trending_equity_intraday", "sector_follow_cap5_vol"}
    tei = s["trending_equity_intraday"]
    assert tei["attempted"] == 2 and tei["filled"] == 2
    assert tei["closed"] == 1 and tei["open"] == 1
    sf = s["sector_follow_cap5_vol"]
    assert sf["attempted"] == 1 and sf["filled"] == 0 and sf["open"] == 1

    # Sandbox: 3 today, 1 yesterday excluded.
    assert r["sandbox"]["total"] == 3
    assert r["sandbox"]["by_strategy"]["trending_equity_intraday"] == 2
    assert r["sandbox"]["by_strategy"]["sector_follow_cap5_vol"] == 1


def test_compute_funnel_first_dropped_names_the_signal(active_day):
    from services.trading_day_funnel_service import compute_funnel

    r = compute_funnel(DATE)
    fd = r["signals"]["first_dropped"]
    assert fd is not None
    assert fd["symbol"] == "ZYDUSLIFE"
    assert fd["source"] == "chartink_FnO_intraday_buy"
    assert "NIFTYPHARMA" in fd["reason"]


def test_compute_funnel_other_date_returns_empty(active_day):
    from services.trading_day_funnel_service import compute_funnel

    # The fixture rows are dated 2026-06-26; query a different date.
    r = compute_funnel("2026-06-15")
    assert r["hits"]["total"] == 0
    assert r["signals"]["total"] == 0
    assert r["strategies"] == {}
    assert r["sandbox"]["total"] == 0


# --------------------------------------------------------------------------- #
# Internal counter resilience
# --------------------------------------------------------------------------- #


def test_parse_symbol_list_tolerates_malformed_json():
    from services.trading_day_funnel_service import _parse_symbol_list

    assert _parse_symbol_list(None) == []
    assert _parse_symbol_list("") == []
    assert _parse_symbol_list("not-json") == []
    assert _parse_symbol_list('{"not": "a list"}') == []
    assert _parse_symbol_list('["A", null, "  b  ", ""]') == ["A", "B"]


def test_count_scanner_hits_degrades_to_none_on_db_error(monkeypatch):
    """A query failure surfaces as None, not a raise. Funnel must keep going."""
    from database import scanner_db
    from services import trading_day_funnel_service as tdf

    # Replace the live ``db_session`` with one whose ``.query()`` raises. The
    # service catches any Exception out of the query and degrades the counter
    # to the None sentinel; this proves the funnel keeps rendering a partial
    # message instead of crashing the whole job.
    class _BoomSession:
        def query(self, *_a, **_kw):
            raise RuntimeError("simulated DB outage")

        def remove(self):  # session contract used by the finally block.
            return None

    monkeypatch.setattr(scanner_db, "db_session", _BoomSession())
    out = tdf._count_scanner_hits(DATE)
    assert out == {"inhouse": None, "chartink": None, "total": None, "by_source": {}}


# --------------------------------------------------------------------------- #
# Telegram formatter
# --------------------------------------------------------------------------- #


def test_format_telegram_empty_day_uses_dash_placeholder(empty_day):
    from services.trading_day_funnel_service import _format_telegram, compute_funnel

    msg = _format_telegram(compute_funnel(DATE))
    assert "Trading day funnel" in msg
    assert DATE in msg
    assert "no strategy activity" in msg
    # Empty drop-off ratios must not divide by zero.
    assert "0/0" in msg


def test_format_telegram_active_day_lists_per_strategy(active_day):
    from services.trading_day_funnel_service import _format_telegram, compute_funnel

    msg = _format_telegram(compute_funnel(DATE))
    assert "trending_equity_intraday: attempted=2" in msg
    assert "sector_follow_cap5_vol: attempted=1" in msg
    # Drop-off line uses sum-of-strategies for the numerator.
    assert "signals_taken→orders: 3/2" in msg
    # First vetoed surfaces with its reason fragment.
    assert "first vetoed: ZYDUSLIFE" in msg


def test_format_drop_arrow_zero_denominator_does_not_divide():
    from services.trading_day_funnel_service import _fmt_drop_arrow

    assert _fmt_drop_arrow(0, 0) == "0/0"
    assert _fmt_drop_arrow(5, 0) == "5/0"
    assert _fmt_drop_arrow(None, 10) == "?/10"
    assert _fmt_drop_arrow(3, 6) == "3/6 (50%)"


# --------------------------------------------------------------------------- #
# run_funnel_for_date — dispatch + flag plumbing
# --------------------------------------------------------------------------- #


def test_run_funnel_dispatches_via_notification_service(active_day, monkeypatch):
    from services import notification_service, trading_day_funnel_service

    fake = MagicMock()
    monkeypatch.setattr(notification_service, "get_notification_service", lambda: fake)
    r = trading_day_funnel_service.run_funnel_for_date(DATE, dispatch_telegram=True)
    assert r["telegram_sent"] is True
    fake.notify.assert_called_once()
    event_type, message = fake.notify.call_args.args
    assert event_type == "trading_day_funnel"
    assert "Trading day funnel" in message
    # The message we round-trip MUST equal the one returned.
    assert r["telegram_message"] == message


def test_run_funnel_skip_dispatch(active_day, monkeypatch):
    from services import notification_service, trading_day_funnel_service

    fake = MagicMock()
    monkeypatch.setattr(notification_service, "get_notification_service", lambda: fake)
    r = trading_day_funnel_service.run_funnel_for_date(DATE, dispatch_telegram=False)
    assert r["telegram_sent"] is False
    fake.notify.assert_not_called()


def test_dispatch_failure_returns_false_does_not_raise(monkeypatch):
    from services import notification_service, trading_day_funnel_service

    def _boom():
        raise RuntimeError("telegram bot down")

    monkeypatch.setattr(notification_service, "get_notification_service", _boom)
    # Must not raise even though the dispatcher exploded.
    assert trading_day_funnel_service._dispatch_telegram("x") is False


def test_funnel_job_respects_flag_off(monkeypatch):
    from services import trading_day_funnel_service as tdf

    called = []
    monkeypatch.setattr(tdf, "run_funnel_for_date", lambda *a, **kw: called.append((a, kw)))
    monkeypatch.setenv("TRADING_DAY_FUNNEL_ENABLED", "false")
    tdf._funnel_job()
    assert called == []


def test_funnel_job_runs_when_flag_on(monkeypatch):
    from services import trading_day_funnel_service as tdf

    called = []
    monkeypatch.setattr(tdf, "run_funnel_for_date", lambda *a, **kw: called.append((a, kw)) or {})
    monkeypatch.setenv("TRADING_DAY_FUNNEL_ENABLED", "true")
    tdf._funnel_job()
    assert len(called) == 1


def test_funnel_job_swallows_run_failure(monkeypatch):
    from services import trading_day_funnel_service as tdf

    def _boom(*a, **kw):
        raise RuntimeError("simulated")

    monkeypatch.setattr(tdf, "run_funnel_for_date", _boom)
    monkeypatch.setenv("TRADING_DAY_FUNNEL_ENABLED", "true")
    # APScheduler must never see an exception bubble from the job body.
    tdf._funnel_job()


# --------------------------------------------------------------------------- #
# Scheduler registration
# --------------------------------------------------------------------------- #


def test_register_jobs_idempotent_and_named():
    from apscheduler.schedulers.background import BackgroundScheduler

    from services.trading_day_funnel_service import register_jobs

    # Start (paused) so jobs land in the jobstore where ``replace_existing``
    # actually deduplicates. On a not-started scheduler, ``add_job`` queues
    # to ``_pending_jobs`` (a list, not a dict) and the second call appends
    # rather than replaces — a hermetic-test gotcha, not a service bug.
    sched = BackgroundScheduler(timezone="UTC")
    sched.start(paused=True)
    try:
        register_jobs(sched)
        register_jobs(sched)  # replace_existing=True
        ids = [j.id for j in sched.get_jobs()]
        assert ids.count("trading_day_funnel") == 1
        job = sched.get_job("trading_day_funnel")
        assert "Trading day funnel" in job.name
        # Default HH:MM is 15:35.
        trig = job.trigger
        assert trig.fields[trig.FIELD_NAMES.index("hour")].expressions[0].first == 15
        assert trig.fields[trig.FIELD_NAMES.index("minute")].expressions[0].first == 35
    finally:
        sched.shutdown(wait=False)


def test_register_jobs_honours_env_time(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    from services.trading_day_funnel_service import register_jobs

    monkeypatch.setenv("TRADING_DAY_FUNNEL_TIME", "16:07")
    sched = BackgroundScheduler(timezone="UTC")
    sched.start(paused=True)
    try:
        register_jobs(sched)
        job = sched.get_job("trading_day_funnel")
        trig = job.trigger
        assert trig.fields[trig.FIELD_NAMES.index("hour")].expressions[0].first == 16
        assert trig.fields[trig.FIELD_NAMES.index("minute")].expressions[0].first == 7
    finally:
        sched.shutdown(wait=False)


def test_register_jobs_falls_back_on_bad_env_time(monkeypatch):
    from apscheduler.schedulers.background import BackgroundScheduler

    from services.trading_day_funnel_service import register_jobs

    monkeypatch.setenv("TRADING_DAY_FUNNEL_TIME", "not-a-time")
    sched = BackgroundScheduler(timezone="UTC")
    sched.start(paused=True)
    try:
        register_jobs(sched)
        job = sched.get_job("trading_day_funnel")
        trig = job.trigger
        # Falls back to default 15:35 — the gate must never crash boot.
        assert trig.fields[trig.FIELD_NAMES.index("hour")].expressions[0].first == 15
        assert trig.fields[trig.FIELD_NAMES.index("minute")].expressions[0].first == 35
    finally:
        sched.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# notification_service event-type registration (#159 must not get dropped)
# --------------------------------------------------------------------------- #


def test_notification_service_registers_trading_day_funnel_event():
    from services.notification_service import _EVENT_TYPES, NotificationService

    assert "trading_day_funnel" in _EVENT_TYPES
    svc = NotificationService()
    assert "trading_day_funnel" in svc.per_event
    # Default ON — the operator wants this diagnostic every day.
    assert svc.per_event["trading_day_funnel"] is True
