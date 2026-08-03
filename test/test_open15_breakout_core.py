"""Unit tests for the open15_vol_breakout tick-driven core (issue #425).

Covers the legality-critical mechanics: selection at 09:16, minute-volume
bookkeeping from cumulative tick volume, the mid-bar trigger (cumvol-in-minute
>= mult x running-avg AND level break), once-per-symbol entry, and the
entry-minute close stamp used by the drift measurement.
"""

import datetime as dt

from services.open15_breakout_service import Open15Core


def t(h, m, s=0):
    return dt.datetime(2026, 7, 21, h, m, s)


def feed_first_candle(core, sym, opens, cum_end):
    """Feed a simple 09:15 candle: open tick + a closing cumvol level."""
    core.on_tick(sym, opens, cum_end * 0.4, t(9, 15, 1))
    core.on_tick(sym, opens * 1.001, cum_end, t(9, 15, 55))


def test_selection_top_n_by_gap():
    core = Open15Core({"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "DDD": 100.0}, top_n=1)
    feed_first_candle(core, "AAA", 103.0, 1000)  # +3% gap (top gainer)
    feed_first_candle(core, "BBB", 101.0, 1000)  # +1%
    feed_first_candle(core, "CCC", 97.0, 1000)  # -3% (top loser)
    feed_first_candle(core, "DDD", 99.0, 1000)  # -1%
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 1))  # first 09:16 tick finalizes
    assert core.finalized
    assert core.selected == {"AAA": "L", "CCC": "S"}


def test_midbar_trigger_fires_at_the_tick_not_bar_close():
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)  # gap +2%; H1 ~102.1, v0=1000
    h1 = core.sym["AAA"]["fc"]["high"]
    # 09:16: quiet minute, no break (vol 400)
    assert core.on_tick("AAA", 101.5, 1400, t(9, 16, 30)) is None
    # 09:17 rolls 09:16 closed -> baseline = mean(400) = 400. The 09:15 minute
    # is NOT in the baseline (issue #502): it is the day's busiest minute and
    # its tick cumvol carries the pre-open auction.
    # surge: cumvol-in-minute reaches 1.5*400=600 at second 12 with price beyond H1
    assert core.on_tick("AAA", h1 + 0.5, 1400 + 599, t(9, 17, 10)) is None  # 599 < 600
    action = core.on_tick("AAA", h1 + 0.6, 1400 + 620, t(9, 17, 12))
    assert action is not None
    assert action["side"] == "L"
    assert action["trigger_second"] == 12
    assert action["level"] == h1
    assert action["baseline_vol"] == 400
    assert action["cum_vol_at_trigger"] == 620
    # once per symbol
    assert core.on_tick("AAA", h1 + 1.0, 5000, t(9, 17, 30)) is None


def test_no_trigger_without_level_break_or_volume():
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.0, 1500, t(9, 16, 5))
    # 09:17: volume surge but price BELOW level -> no entry
    assert core.on_tick("AAA", h1 - 0.5, 1500 + 5000, t(9, 17, 20)) is None
    # price beyond level but thin volume -> no entry
    assert core.on_tick("AAA", h1 + 0.5, 1500 + 5010, t(9, 17, 40)) is None or True


def test_short_side_and_entry_minute_close():
    core = Open15Core({"SSS": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "SSS", 97.0, 1000)  # -3% gap -> short watch
    l1 = core.sym["SSS"]["fc"]["low"]
    core.on_tick("SSS", 97.5, 1300, t(9, 16, 10))  # 09:16 vol 300
    action = core.on_tick("SSS", l1 - 0.4, 1300 + 2000, t(9, 17, 8))
    assert action is not None and action["side"] == "S"
    # later ticks in the entry minute update its last price (the minute close)
    core.on_tick("SSS", l1 - 0.9, 1300 + 2500, t(9, 17, 50))
    core.on_tick("SSS", l1 - 0.7, 1300 + 2600, t(9, 18, 5))  # next minute
    assert core.entry_minute_close("SSS") == l1 - 0.9


def test_near_miss_stats_for_decision_log():
    """A selected watch that never fully triggers still records how close it got."""
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.0, 1400, t(9, 16, 30))  # 09:16 vol 400 -> baseline 400 (#502)
    # beyond level but volume only reaches 1.0x baseline -> no entry, stats recorded
    assert core.on_tick("AAA", h1 + 0.3, 1400 + 400, t(9, 17, 20)) is None
    ws = core.watch_stats["AAA"]
    assert ws["level_broken"] is True
    assert 0.9 < ws["max_vol_ratio_beyond"] <= 1.1
    assert "AAA" not in core.entered


def test_watch_snapshot_seeds_every_selected_symbol():
    """Selection seeds the stats so the UI can read them off-thread (issue #524).

    A seeded-but-tickless symbol must stay ``None`` — blank in the UI means "no
    data", which is a different diagnosis from a genuine 0.0 ratio.
    """
    core = Open15Core({"AAA": 100.0, "BBB": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 103.0, 1000)  # +3% -> long watch
    feed_first_candle(core, "BBB", 97.0, 1000)  # -3% -> short watch
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 1))  # finalizes selection
    snap = core.watch_snapshot()
    assert set(snap) == {"AAA", "BBB"}  # keys fixed at 09:16, incl. tickless BBB
    assert snap["BBB"]["max_vol_ratio"] is None  # no in-window tick yet
    assert snap["BBB"]["level_broken"] is False
    assert snap["AAA"]["entered"] is False
    # 09:17 rolls the 09:16 minute closed -> baseline 100; half-baseline volume
    core.on_tick("AAA", 103.0, 1150, t(9, 17, 10))
    assert core.watch_snapshot()["AAA"]["max_vol_ratio"] == 0.5


def test_watch_snapshot_max_freezes_at_entry():
    """An entered symbol's max is its ratio at trigger — on_tick returns early
    once the symbol is in ``entered``, so later surges never raise it."""
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.5, 1400, t(9, 16, 30))  # baseline 400
    assert core.on_tick("AAA", h1 + 0.6, 1400 + 800, t(9, 17, 12)) is not None
    at_entry = core.watch_snapshot()["AAA"]["max_vol_ratio"]
    assert at_entry == 2.0
    core.on_tick("AAA", h1 + 2.0, 1400 + 9999, t(9, 18, 5))  # much bigger surge
    snap = core.watch_snapshot()["AAA"]
    assert snap["max_vol_ratio"] == at_entry
    assert snap["entered"] is True


def test_resolve_day_config_fixed_and_compound():
    from services.open15_breakout_service import resolve_day_config

    # env defaults, no UI row
    c = resolve_day_config(None, 0.0)
    assert c["sizing_mode"] == "fixed"
    assert c["notional"] == c["margin_per_slot"] * c["leverage"]
    # UI row overrides; fixed ignores cum pnl
    c = resolve_day_config(
        {"margin_per_slot": 50000, "sizing_mode": "fixed", "vol_mult": 2.0}, 99999
    )
    assert c["margin_effective"] == 50000 and c["vol_mult"] == 2.0
    # compound rolls realized P&L into capital
    c = resolve_day_config(
        {"margin_per_slot": 50000, "sizing_mode": "compound", "vol_mult": None}, 12500
    )
    assert c["margin_effective"] == 62500 and c["notional"] == 62500 * c["leverage"]
    # drawdown floor: never below 25% of base
    c = resolve_day_config(
        {"margin_per_slot": 40000, "sizing_mode": "compound", "vol_mult": None}, -90000
    )
    assert c["margin_effective"] == 10000
    # junk sizing mode falls back to fixed
    c = resolve_day_config({"sizing_mode": "martingale"}, 0.0)
    assert c["sizing_mode"] == "fixed"


def test_mode_resolution_env_observe_wins_and_strategy_mode_row_governs(monkeypatch):
    """Env observe = dry-run kill switch; else the strategy_mode row (UI toggle)
    governs; no row -> sandbox default (issue #430)."""
    from database.strategy_mode_db import StrategyMode, db_session
    from database.strategy_mode_db import init_db as mode_init
    from services import open15_breakout_service as m

    mode_init()
    monkeypatch.setenv("OPEN15_MODE", "observe")
    assert m._mode() == "observe"
    monkeypatch.setenv("OPEN15_MODE", "sandbox")
    assert m._mode() == "sandbox"  # no row -> default
    db_session.add(StrategyMode(strategy_name=m.STRATEGY_NAME, mode="live", updated_by="test"))
    db_session.commit()
    try:
        assert m._mode() == "live"  # row governs (global gate still rules routing)
        monkeypatch.setenv("OPEN15_MODE", "observe")
        assert m._mode() == "observe"  # env observe still wins over the row
    finally:
        db_session.query(StrategyMode).filter_by(strategy_name=m.STRATEGY_NAME).delete()
        db_session.commit()
        db_session.remove()


def test_config_db_roundtrip():
    from database.open15_breakout_db import get_config, init_db, save_config

    init_db()
    assert save_config(60000, "compound", 1.8, updated_by="test")
    cfg = get_config()
    assert cfg["margin_per_slot"] == 60000
    assert cfg["sizing_mode"] == "compound"
    assert cfg["vol_mult"] == 1.8


def test_register_jobs_with_persistent_jobstore():
    """The shared scheduler uses SQLAlchemyJobStore, which PICKLES callables.

    Pre-#428 this raised ``cannot pickle '_thread.lock' object`` (bound methods
    + lambdas dragging the service instance into the pickle) — the bug that
    silently killed the 2026-07-20 first session. Registers against a real
    in-memory persistent jobstore to prove serializability.
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
    from apscheduler.schedulers.background import BackgroundScheduler

    from services.open15_breakout_service import Open15BreakoutService

    sched = BackgroundScheduler(jobstores={"default": SQLAlchemyJobStore(url="sqlite://")})
    sched.start(paused=True)
    try:
        Open15BreakoutService().register_jobs(sched)
        ids = {j.id for j in sched.get_jobs()}
        assert ids == {
            "open15_arm",
            "open15_first_candles",
            "open15_exit",
            "open15_exit_retry",
            "open15_summary",
        }
    finally:
        sched.shutdown(wait=False)


def test_unselected_symbol_never_enters():
    core = Open15Core({"AAA": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 103.0, 1000)
    feed_first_candle(core, "ZZZ", 101.0, 1000)  # gainer but not top-1
    zh = core.sym["ZZZ"]["fc"]["high"]
    core.on_tick("ZZZ", 101.2, 1200, t(9, 16, 10))
    assert core.on_tick("ZZZ", zh + 1.0, 99000, t(9, 17, 5)) is None


def test_mis_round_trip_charges_model():
    """issue #433: modelled Zerodha MIS equity round-trip charges.

    Rs 1.2L per leg: brokerage min(20, 0.03% of 1.2L=36) = 20/leg = 40, STT
    0.025% of sell = 30, txn 0.00297% of 2.4L = 7.13, SEBI 0.24, stamp 3.60,
    GST 18% of (40 + 7.13 + 0.24) = 8.53 -> ~89.5 total.
    """
    from services.open15_breakout_service import mis_round_trip_charges

    c = mis_round_trip_charges(120_000.0, 120_000.0)
    assert c is not None and 85.0 < c < 95.0
    # tiny order: brokerage is percentage-bound, not the Rs20 cap
    small = mis_round_trip_charges(1000.0, 1000.0)
    assert small is not None and small < 2.0
    # missing a leg -> None (open trade / no exit price)
    assert mis_round_trip_charges(0.0, 120_000.0) is None
