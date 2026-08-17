"""Replay of a missed open15 session (issue #600).

Two things are being defended here, and they are different in kind:

1. **Fidelity** — the reconstruction must reproduce what the live strategy
   actually selected on a day it really ran. The fixture is 2026-08-14's own
   decision log, so these tests fail if the engine drifts from live behaviour.
2. **Production isolation** — replay must be incapable of touching the real
   money this strategy trades. Those tests assert the guards, not the maths.

The fixture carries only each symbol's 09:15 bar, which is everything selection
needs (gap = 09:15 open / prev close) and keeps the file small. Trigger overlap
is deliberately NOT asserted for equality: it is the ~60%-fidelity part of the
reconstruction, so pinning it would encode noise as a contract.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from services import open15_replay as R

FIXTURE = Path(__file__).parent / "fixtures" / "open15_replay_2026-08-14.json"


@pytest.fixture(scope="module")
def day():
    return json.loads(FIXTURE.read_text())


def _config(day, **over):
    """2026-08-14's armed config: long_only + shadow shorts, OI filter at 500."""
    cfg = {
        "vol_mult": 1.5,
        "top_n": 3,
        "trade_side": "long_only",
        "shadow_side": "S",
        "shadow_max_trades": 3,
        "max_trades": 3,
        "margin_effective": 60000.0,
        "instrument": "atm_option",
        "rolling_enabled": False,  # isolates SEED selection from the rolling add-ons
        "rolling_cadence_s": 30,
        "rolling_top_n": 3,
        "no_entry_after": "09:29",
        "exit_time": "09:30",
        "option_min_oi_lots": 500,
        "excluded": [],
        "config_source": "test",
    }
    cfg.update(over)
    return cfg


def _contracts(day):
    """Contract table in the shape ``resolve_contracts_and_oi`` returns."""
    return {
        key: {
            "symbol": day["opt_symbols"][key],
            "lotsize": 1,
            "oi_lots": lots,
            "bars": {},
        }
        for key, lots in day["oi_lots"].items()
    }


# --------------------------------------------------------------------------- #
# Fidelity
# --------------------------------------------------------------------------- #
def test_seed_selection_matches_the_live_decision_log(day):
    """The whole fidelity claim: same picks, same sides, same gaps as live."""
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], _contracts(day))

    assert run["selected"] == day["live_selection"]
    for sym, gap in day["live_gaps_pct"].items():
        assert run["gaps"][sym] == pytest.approx(gap, abs=0.011)


def test_oi_verdicts_match_the_live_stage3_exclusions(day):
    """The #595 filter must block exactly the names live blocked, at the same lots."""
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], _contracts(day))

    got = sorted({(e["symbol"], e["side"], e["oi_lots"]) for e in run["oi_exclusions"]})
    # live also blocked MAXHEALTH, but only as a ROLLING addition — the rolling
    # list is off here, so compare on the seed-reachable subset.
    seed_blocks = [tuple(b) for b in day["live_oi_blocks"] if b[0] != "MAXHEALTH"]
    assert got == sorted(seed_blocks)


def test_oi_read_from_the_0915_bar_not_0916(day):
    """The single most breakable detail in the feature (issue #600).

    A bar's ``oi`` is stamped at the END of its minute, so the 09:15 bar is what
    the live 09:16:02 quote sees. MFSL is the canary: 418 lots on the 09:15 bar
    (blocked, as live did) versus 791 on the 09:16 bar (would clear the floor).
    Reading the wrong bar let NMDC through on 2026-08-17 and manufactured a
    phantom +Rs17,924 on a Rs1.67 put.
    """
    series = {
        "MFSL25AUG261560CE": {
            "09:15": {"open": 42.6, "close": 43.0, "volume": 1, "oi": 418},
            "09:16": {"open": 43.0, "close": 43.2, "volume": 1, "oi": 791},
        }
    }
    captured = {}

    def fake_fetch(sym, date):
        captured["sym"] = sym
        return [{"timestamp": ts, **bar} for ts, bar in _epochs(series[sym], date)]

    import services.open15_option_shadow as shadow

    orig_fetch, orig_resolve = shadow._fetch_1m_bars, shadow.resolve_atm_option
    shadow._fetch_1m_bars = fake_fetch
    shadow.resolve_atm_option = lambda *a, **k: {"symbol": "MFSL25AUG261560CE", "lotsize": 1}
    try:
        out = R.resolve_contracts_and_oi(
            "2026-08-14", [("MFSL", "L")], {"MFSL": {"09:15": {"close": 1563.3}}}
        )
    finally:
        shadow._fetch_1m_bars, shadow.resolve_atm_option = orig_fetch, orig_resolve

    assert out["MFSL|L"]["oi_lots"] == 418.0, "OI must come from the 09:15 bar"


def _epochs(bars_by_minute, date):
    """(epoch, bar) pairs for HH:MM keys on ``date``, IST -> UTC."""
    d = dt.date.fromisoformat(date)
    for hhmm, bar in bars_by_minute.items():
        h, m = (int(x) for x in hhmm.split(":"))
        naive = dt.datetime.combine(d, dt.time(h, m)) - R._IST_OFFSET
        yield int(naive.replace(tzinfo=dt.UTC).timestamp()), bar


def test_unknown_oi_fails_open(day):
    """No verdict must never mean 'thin' — the live filter fails open (#390/#555)."""
    contracts = _contracts(day)
    for v in contracts.values():
        v["oi_lots"] = None
    run = R.run_core(day["date"], _config(day), day["bars"], day["prev_closes"], contracts)

    assert run["oi_exclusions"] == []
    # with nothing blocked, the pre-promotion picks come back
    assert "MFSL" in run["selected"]


def test_oi_filter_disabled_when_floor_is_zero(day):
    assert R.make_oi_filter(_contracts(day), 0) is None


# --------------------------------------------------------------------------- #
# Production isolation (G1-G7)
# --------------------------------------------------------------------------- #
def test_g1_replay_imports_no_order_path():
    """Replay must be structurally incapable of placing an order."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "place_order",
        "order_placer",
        "place_smart_order",
        "close_position",
        "order_router",
    ):
        assert forbidden not in src, f"replay must not reference {forbidden}"


def test_g3_not_imported_by_anything_that_boots():
    """Replay must add no boot-time cost, no scheduler job and no thread.

    Importing the live open15 service and its blueprint — everything the app
    wires up for this strategy — must not drag the replay engine in with them.
    """
    import subprocess
    import sys

    probe = (
        "import sys;"
        "import services.open15_breakout_service, blueprints.open15_breakout;"
        "print('services.open15_replay' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=180)
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr


def test_g3_registers_no_scheduler_job_or_thread():
    """Code patterns, not prose — the docstring is allowed to SAY 'scheduler'."""
    src = Path(R.__file__).read_text(encoding="utf-8")
    for forbidden in ("add_job(", "Thread(", "BackgroundScheduler", "start_background"):
        assert forbidden not in src, f"replay must not use {forbidden}"


def test_control_harness_never_writes():
    """The control harness scores TRADED days — it must be read-only.

    It deliberately bypasses ``check_eligibility`` (every control day would be
    refused as ``day_was_traded``), so the thing standing between it and a
    destructive rewrite is that it never calls a writer at all.
    """
    from services import open15_replay_control as C

    src = Path(C.__file__).read_text(encoding="utf-8")
    # call syntax, not prose — the docstring is allowed to NAME these
    for forbidden in (
        "insert_trade(",
        "update_trade(",
        "delete_replay_rows(",
        "save_day_log(",
        "R.persist(",
        "R.replay_session(",
        ".commit(",
        ".delete(",
    ):
        assert forbidden not in src, f"control harness must not call {forbidden}"


def test_g2_replay_pnl_is_excluded_from_real_aggregates():
    """Without this, replay money compounds into tomorrow's real position size."""
    from database.open15_breakout_db import NON_REAL_FILLS

    assert "replay" in NON_REAL_FILLS


def test_g2_total_realized_pnl_ignores_replay_rows(tmp_path, monkeypatch):
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    db.insert_trade(
        trade_date="2026-08-11",
        symbol="A",
        side="L",
        status="closed",
        fill="real",
        pnl=1000.0,
        charges_inr=100.0,
    )
    before = db.total_realized_pnl()
    db.insert_trade(
        trade_date="2026-08-12",
        symbol="B",
        side="L",
        status="closed",
        fill="replay",
        pnl=50000.0,
        charges_inr=100.0,
    )

    assert db.total_realized_pnl() == before == 900.0
    assert db.replay_pnl_by_date() == {"2026-08-12": 49900.0}
    assert "2026-08-12" not in db.trades_pnl_by_date()


def test_g4_replay_refuses_to_overwrite_a_traded_day(tmp_path, monkeypatch):
    """The #597 clobber class — the guard runs again inside the writer."""
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    monkeypatch.setattr(R, "_early_net", lambda row: None)
    db.insert_trade(
        trade_date="2026-08-13", symbol="REAL", side="L", status="closed", fill="real", pnl=1.0
    )

    rows = [{"status": "closed", "symbol": "X", "side": "L", "pnl": 5.0, "charges_inr": 0.0}]
    with pytest.raises(R.ReplayIneligible) as e:
        R.persist("2026-08-13", {"instrument": "atm_option"}, rows, [])

    assert e.value.reason == "day_was_traded"
    assert _count(db, "2026-08-13") == 1  # nothing was written


def test_g4_has_real_fill_fails_closed(monkeypatch):
    """An unreadable journal must BLOCK the rewrite, never wave it through."""
    import database.open15_breakout_db as db

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.db_session, "query", boom)
    assert db.has_real_fill("2026-08-12") is True


def test_g6_pre_existing_rows_still_classify_as_real(tmp_path, monkeypatch):
    """Adding a fill class must not reinterpret a single existing row."""
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    db.insert_trade(
        trade_date="2026-08-10",
        symbol="OLD",
        side="L",
        status="closed",
        pnl=200.0,
        charges_inr=20.0,
    )  # fill IS NULL, pre-dates the column

    assert db.trades_pnl_by_date() == {"2026-08-10": 180.0}
    assert db.total_realized_pnl() == 180.0


def test_delete_replay_rows_cannot_touch_other_buckets(tmp_path, monkeypatch):
    import database.open15_breakout_db as db

    _rebind(db, tmp_path, monkeypatch)
    for fill in ("real", "paper", "sim", "shadow", "replay", None):
        db.insert_trade(
            trade_date="2026-08-12", symbol=str(fill), side="L", status="closed", fill=fill, pnl=1.0
        )

    assert db.delete_replay_rows("2026-08-12") == 1
    assert _count(db, "2026-08-12") == 5


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("events", "real_fill", "expect_ok", "reason"),
    [
        ([{"event": "skipped_late_boot"}], False, True, "skipped_late_boot"),
        ([{"event": "no_ticks_received"}], False, True, "no_ticks_received"),
        ([], False, True, "no_day_log"),
        ([{"event": "armed"}, {"event": "summary"}], False, False, "day_ran_normally"),
        ([{"event": "skipped_late_boot"}], True, False, "day_was_traded"),
        ([{"event": "replay_meta"}], False, False, "already_replayed"),
    ],
)
def test_eligibility(monkeypatch, events, real_fill, expect_ok, reason):
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: events)
    monkeypatch.setattr(db, "has_real_fill", lambda d: real_fill)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    # a Sunday evening: a trading DATE is being replayed, outside market hours
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 16, 20, 0))

    out = R.check_eligibility("2026-08-12")
    assert out["eligible"] is expect_ok
    assert out["reason"] == reason


def test_market_hours_warns_but_does_not_block(monkeypatch):
    """Operator decision (2026-08-17): market hours is a COST, not a defect.

    A replay is ~250 historical calls against the live strategy's 3 req/s
    budget. That is worth stating, but it is the operator's call to accept —
    so it rides the result as ``warning`` and the day stays eligible. Contrast
    ``day_was_traded`` below, which is about the ANSWER being wrong and stays
    a hard block.
    """
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: [{"event": "skipped_late_boot"}])
    monkeypatch.setattr(db, "has_real_fill", lambda d: False)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 17, 10, 30))

    out = R.check_eligibility("2026-08-12")
    assert out["eligible"] is True
    assert out["reason"] == "skipped_late_boot"
    assert "market hours" in (out["warning"] or "")


def test_a_traded_day_is_still_blocked_during_market_hours(monkeypatch):
    """The cost guard relaxing must not relax the integrity guard with it."""
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: [{"event": "skipped_late_boot"}])
    monkeypatch.setattr(db, "has_real_fill", lambda d: True)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 17, 10, 30))

    out = R.check_eligibility("2026-08-13")
    assert out["eligible"] is False
    assert out["reason"] == "day_was_traded"


def test_same_day_before_history_catches_up_is_still_blocked(monkeypatch):
    """``too_early`` is a CORRECTNESS block — it would rebuild a truncated
    session and report it as complete. Not relaxed with market hours."""
    import database.open15_breakout_db as db
    import services.data_freshness_service as fresh

    monkeypatch.setattr(db, "get_day_log", lambda d: [{"event": "skipped_late_boot"}])
    monkeypatch.setattr(db, "has_real_fill", lambda d: False)
    monkeypatch.setattr(fresh, "is_trading_day", lambda d, exchange=None: True)
    monkeypatch.setattr(R, "_now_ist", lambda: dt.datetime(2026, 8, 17, 9, 30))

    out = R.check_eligibility("2026-08-17")
    assert out["eligible"] is False
    assert out["reason"] == "too_early"


def _rebind(db, tmp_path, monkeypatch):
    """Point the module's engine at a throwaway DB for this test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'open15.db'}")
    session = scoped_session(sessionmaker(bind=engine))
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "db_session", session)
    db.Base.metadata.create_all(bind=engine)


def _count(db, date: str) -> int:
    return db.db_session.query(db.Open15Trade).filter(db.Open15Trade.trade_date == date).count()


def test_ensure_columns_map_matches_the_orm_models():
    """Every migration column must exist on the table it is filed under (#602).

    ``_ensure_columns`` is the ONLY path that adds a column to a table that
    already exists — i.e. the only path that runs on a real install. Tests build
    their tables with ``Base.metadata.create_all()`` straight from the ORM
    model, so a column filed under the WRONG table passes every test and then
    fails in production with ``no such column`` on the first insert.

    That is exactly what happened to ``opt_entry_premium_early``: it shipped in
    the ``open15_config`` block (its anchor, ``coverage_target_pct``, is a
    config column that reads like a trades one), so ``open15_trades`` never got
    it. This test fails against that tree.
    """
    import inspect

    import database.open15_breakout_db as db

    src = inspect.getsource(db._ensure_columns)
    ns: dict = {}
    start = src.index("wanted_by_table = {")
    end = src.index("    try:", start)
    exec(src[start:end].replace("wanted_by_table", "wanted"), ns)  # noqa: S102
    wanted = ns["wanted"]

    by_table = {m.__tablename__: m for m in (db.Open15Trade, db.Open15Config, db.Open15DayLog)}
    for table, cols in wanted.items():
        model = by_table.get(table)
        assert model is not None, f"{table} has no ORM model in this check"
        declared = set(model.__table__.columns.keys())
        stray = sorted(set(cols) - declared)
        assert not stray, f"{table} migration lists columns not on its model: {stray}"


def test_replay_early_premium_is_migrated_onto_the_trades_table():
    """The specific #602 regression, named so a failure is self-explaining."""
    import inspect

    import database.open15_breakout_db as db

    src = inspect.getsource(db._ensure_columns)
    trades = src[src.index('"open15_trades": {') : src.index('"open15_config": {')]
    assert "opt_entry_premium_early" in trades


# --------------------------------------------------------------------------- #
# The sidebar must not hammer the DB (issue #606)
# --------------------------------------------------------------------------- #
def test_sidebar_does_not_fetch_eligibility_per_card():
    """The affordance must cost ZERO extra requests.

    The first cut fetched eligibility per day card, and the sidebar re-renders
    every 5 s: 140 requests in 18 s, each running a real-fill query AND a full
    day-log JSON parse against the live DB while the strategy was trading.
    Eligibility now rides the days digest the sidebar already fetches.
    """
    import blueprints.open15_breakout as bp

    src = Path(bp.__file__).read_text(encoding="utf-8")
    start = src.index("function maybeAddReplayBtn(")
    body = src[start : src.index("function startReplay(", start)]
    assert "fetch(" not in body, "the day-card renderer must not issue a request"
    assert "async" not in body.split("{", 1)[0], "it must be synchronous"


def test_bulk_real_fill_scan_fails_closed(monkeypatch):
    """Unknown must never read as 'no day ever traded'.

    An empty set would offer a replay — i.e. a destructive rewrite — of every
    date in the sidebar. ``None`` is the sentinel the caller turns into
    'treat every day as traded'.
    """
    import database.open15_breakout_db as db

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(db.db_session, "query", boom)
    assert db.real_fill_dates() is None


def test_unknown_traded_set_offers_no_replay():
    """The caller's half of the fail-closed contract."""
    from services.open15_replay import eligibility_or_reason

    traded = None  # scan failed
    out = eligibility_or_reason(
        "2026-08-12", [{"event": "skipped_late_boot"}], True if traded is None else False
    )
    assert out["eligible"] is False
    assert out["reason"] == "day_was_traded"


def test_pure_eligibility_touches_no_database(monkeypatch):
    """``eligibility_from`` is the hot path — it must stay DB-free."""
    import database.open15_breakout_db as db

    def boom(*a, **k):
        raise AssertionError("eligibility_from must not query the DB")

    monkeypatch.setattr(db, "has_real_fill", boom)
    monkeypatch.setattr(db, "get_day_log", boom)

    out = R.eligibility_from(
        "2026-08-12", [{"event": "skipped_late_boot"}], False, dt.datetime(2026, 8, 16, 20, 0)
    )
    assert out["eligible"] is True


def test_a_day_that_ran_is_not_replayable_even_with_a_skip_event():
    """2026-08-11's real log: a full session PLUS 3 late-boot skip events.

    A late-boot restart later in the day appends ``skipped_late_boot`` to a log
    that already recorded a complete session. Judging on the skip event alone
    offered a replay of a day that genuinely ran — which would mix
    reconstruction rows into real observations. A finalized ``selection`` is the
    proof the session was live, and it outranks any skip event beside it.
    """
    events = [
        {"event": "skipped_late_boot"},
        {"event": "armed"},
        {"event": "selection", "selected": {"X": "L"}},
        {"event": "entry_shadow", "symbol": "X"},
        {"event": "summary", "day": "done"},
        {"event": "skipped_late_boot"},
    ]
    out = R.eligibility_or_reason("2026-08-11", events, False, dt.datetime(2026, 8, 16, 20, 0))
    assert out["eligible"] is False
    assert out["reason"] == "day_ran_normally"


def test_zero_tick_day_is_replayable_despite_arming_and_summarising():
    """2026-08-12's real shape: it armed and summarised but never SELECTED,
    because the feed delivered no ticks. That is the canonical missed day."""
    events = [
        {"event": "armed"},
        {"event": "first_candles", "covered": 0},
        {"event": "no_ticks_received"},
        {"event": "summary", "selected": 0},
    ]
    out = R.eligibility_or_reason("2026-08-12", events, False, dt.datetime(2026, 8, 16, 20, 0))
    assert out["eligible"] is True
    assert out["reason"] == "no_ticks_received"


def test_a_replayed_day_can_be_replayed_again():
    """``replay_meta`` marks a reconstruction, and its own ``selection`` event
    must not then read as 'this session ran for real'."""
    events = [
        {"event": "replay_meta"},
        {"event": "armed"},
        {"event": "selection", "selected": {"X": "L"}},
    ]
    out = R.eligibility_or_reason("2026-08-12", events, False, dt.datetime(2026, 8, 16, 20, 0))
    assert out["eligible"] is True
    assert out["reason"] == "re_replay"


# --------------------------------------------------------------------------- #
# Both config paths must satisfy run_core's contract (issue #617)
# --------------------------------------------------------------------------- #
# Every key run_core / price_legs reads off the day config. Asserted as a SET so
# the next key added to either is caught here, rather than by a KeyError in a
# background worker two weeks later.
_CONFIG_CONTRACT = {
    "vol_mult",
    "top_n",
    "trade_side",
    "shadow_side",
    "shadow_max_trades",
    "max_trades",
    "margin_effective",
    "instrument",
    "rolling_enabled",
    "rolling_cadence_s",
    "rolling_top_n",
    "no_entry_after",
    "exit_time",
    "option_min_oi_lots",
    "excluded",
    "config_source",
}

_ARMED_LOG = [
    {
        "event": "armed",
        "vol_mult": 1.5,
        "top_n": 3,
        "trade_side": "long_only",
        "shadow_side": "S",
        "shadow_max_trades": 3,
        "max_trades": 3,
        "margin_effective": 60000.0,
        "instrument": "atm_option",
        "rolling_watchlist_enabled": True,
        "rolling_cadence_s": 30,
        "rolling_top_n": 3,
        "no_entry_after": "09:29",
        "exit_time": "09:30",
        "option_min_oi_lots": 500,
    },
]
# a day that NEVER armed — the skipped_late_boot case the feature exists for
_NEVER_ARMED_LOG = [{"event": "skipped_late_boot", "armed_at": "09:17:09"}]


@pytest.mark.parametrize(
    ("log", "expect_source"),
    [
        (_ARMED_LOG, "armed_event"),
        (_NEVER_ARMED_LOG, "open15_config_row"),
        ([], "open15_config_row"),
    ],
)
def test_both_config_paths_satisfy_the_run_core_contract(monkeypatch, log, expect_source):
    """2026-08-17 crashed with KeyError 'top_n' (#617).

    ``resolve_day_config`` does not return ``top_n`` — the service reads it
    separately from ``OPEN15_TOP_N`` — so the fallback path died on every day
    that never armed, i.e. precisely the case replay is for. 2026-08-12 worked
    only because it armed at 09:10 before its feed went silent.
    """
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_day_log", lambda d: log)
    cfg = R.resolve_replay_config("2026-08-17")

    assert cfg["config_source"] == expect_source
    missing = _CONFIG_CONTRACT - set(cfg)
    assert not missing, f"config from {expect_source} is missing {sorted(missing)}"
    # and the values must be USABLE, not just present
    assert isinstance(cfg["top_n"], int) and cfg["top_n"] >= 1
    assert cfg["vol_mult"] > 0
    assert cfg["exit_time"] and cfg["no_entry_after"]


def test_a_never_armed_day_replays_through_run_core(day, monkeypatch):
    """The end-to-end shape of #617: config from the fallback path must drive
    the core without raising."""
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_day_log", lambda d: _NEVER_ARMED_LOG)
    cfg = R.resolve_replay_config("2026-08-14")
    cfg["excluded"] = []
    run = R.run_core("2026-08-14", cfg, day["bars"], day["prev_closes"], {})

    assert run["universe_n"] > 0
    assert isinstance(run["selected"], dict)
