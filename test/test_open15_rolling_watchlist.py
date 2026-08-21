"""Tests for the open15 rolling additive watch list (issue #529).

The feature re-ranks the universe on live LTP every ``rolling_cadence_s`` inside
the entry window and APPENDS the current top-N movers to the watch list. It
ships OFF by default as a MEASUREMENT instrument, so the tests that matter are
the invariants, not a P&L claim:

  - deploy is a no-op — disabled, the selection is byte-identical to today's;
  - additive only — the 09:16 seed picks are never dropped and ``watch_size``
    never shrinks;
  - ``trade_side`` is honoured (an excluded side is never added);
  - the cadence throttles, and is CLAMPED server-side (the UI number input is
    a hint, never a trust boundary — the operator-facing requirement on #529);
  - an added symbol enters on ITS OWN 09:15 candle level, through the
    unchanged volume gate, and journals ``watch_source='rolling'``;
  - the ``max_trades`` slot cap still binds across both cohorts.
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from services.open15_breakout_service import (  # noqa: E402
    Open15Core,
    _rolling_cadence_default,
    _rolling_enabled_default,
    _rolling_top_n_default,
    clamp_rolling_cadence,
    clamp_rolling_top_n,
    resolve_day_config,
)


def t(h, m, s=0):
    return dt.datetime(2026, 8, 3, h, m, s)


# 09:15 broker candles: AAA gaps up (+2%), ZZZ gaps down (-2%), the rest flat.
# With ``top_n=1`` the 09:16 seed is therefore exactly {AAA: L, ZZZ: S} — the
# other three can only ever join via the rolling re-rank.
FIRST_CANDLES = {
    "AAA": {"open": 102.0, "high": 103.0, "low": 101.0},
    "BBB": {"open": 100.0, "high": 101.0, "low": 99.0},
    "CCC": {"open": 100.0, "high": 101.0, "low": 99.0},
    "DDD": {"open": 100.0, "high": 101.0, "low": 99.0},
    "EEE": {"open": 100.0, "high": 101.0, "low": 99.0},
    "ZZZ": {"open": 98.0, "high": 99.0, "low": 97.0},
}
PREV_CLOSES = dict.fromkeys(FIRST_CANDLES, 100.0)

# intraday prices at 09:16 — BBB/CCC are the day's real gainers, DDD the real
# loser, all three invisible to the 09:16 gap ranking (they opened flat)
INTRADAY = {"AAA": 102.0, "BBB": 110.0, "CCC": 108.0, "DDD": 90.0, "EEE": 100.0, "ZZZ": 98.0}


def build_core(**kw):
    """Armed core with the selection already finalized at 09:16."""
    kw.setdefault("top_n", 1)
    core = Open15Core(dict(PREV_CLOSES), vol_mult=1.5, **kw)
    for sym, candle in FIRST_CANDLES.items():
        core.on_tick(sym, candle["open"], 1000, t(9, 15, 10))
    core.apply_first_candles({s: dict(c) for s, c in FIRST_CANDLES.items()})
    for sym, price in INTRADAY.items():
        core.on_tick(sym, price, 1200, t(9, 16, 10))
    assert core.finalized
    return core


# ---- clamping (the operator-facing UI knob) -------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [(30, 30), (10, 10), (300, 300), (5, 10), (1, 10), (0, 10), (-99, 10), (600, 300), ("45", 45)],
)
def test_clamp_rolling_cadence(value, expected):
    assert clamp_rolling_cadence(value) == expected


@pytest.mark.parametrize("value", [None, "", "abc", object()])
def test_clamp_rolling_cadence_bad_input_is_the_default(value):
    assert clamp_rolling_cadence(value) == 30


@pytest.mark.parametrize(
    ("value", "expected"), [(3, 3), (1, 1), (10, 10), (0, 1), (-2, 1), (25, 10), ("4", 4)]
)
def test_clamp_rolling_top_n(value, expected):
    assert clamp_rolling_top_n(value) == expected


def test_clamp_rolling_top_n_bad_input_is_the_default():
    assert clamp_rolling_top_n("nope") == 3


# ---- env defaults ---------------------------------------------------------- #


def test_rolling_env_defaults(monkeypatch):
    for var in (
        "OPEN15_ROLLING_WATCHLIST_ENABLED",
        "OPEN15_ROLLING_CADENCE_S",
        "OPEN15_ROLLING_TOP_N",
    ):
        monkeypatch.delenv(var, raising=False)
    assert _rolling_enabled_default() is False  # deploy is a no-op
    assert _rolling_cadence_default() == 30
    assert _rolling_top_n_default() == 3
    monkeypatch.setenv("OPEN15_ROLLING_WATCHLIST_ENABLED", "TRUE")
    monkeypatch.setenv("OPEN15_ROLLING_CADENCE_S", "9999")
    monkeypatch.setenv("OPEN15_ROLLING_TOP_N", "0")
    assert _rolling_enabled_default() is True
    assert _rolling_cadence_default() == 300  # env is clamped too
    assert _rolling_top_n_default() == 1


# ---- day-config resolution ------------------------------------------------- #


def test_resolve_day_config_rolling_defaults_off(monkeypatch):
    monkeypatch.delenv("OPEN15_ROLLING_WATCHLIST_ENABLED", raising=False)
    cfg = resolve_day_config(None, 0.0)
    assert cfg["rolling_watchlist_enabled"] is False
    assert cfg["rolling_cadence_s"] == 30
    assert cfg["rolling_top_n"] == 3


def test_resolve_day_config_rolling_from_row():
    cfg = resolve_day_config(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 60, "rolling_top_n": 5}, 0.0
    )
    assert cfg["rolling_watchlist_enabled"] is True
    assert cfg["rolling_cadence_s"] == 60
    assert cfg["rolling_top_n"] == 5


def test_resolve_day_config_rolling_row_clamps():
    """A row written before a clamp change (or by hand) is still clamped."""
    cfg = resolve_day_config({"rolling_cadence_s": 1, "rolling_top_n": 99}, 0.0)
    assert cfg["rolling_cadence_s"] == 10
    assert cfg["rolling_top_n"] == 10


def test_resolve_day_config_explicit_false_beats_env_true(monkeypatch):
    """``is None`` not truthiness — a stored OFF must survive an env of ON."""
    monkeypatch.setenv("OPEN15_ROLLING_WATCHLIST_ENABLED", "true")
    assert (
        resolve_day_config({"rolling_watchlist_enabled": False}, 0.0)["rolling_watchlist_enabled"]
        is False
    )
    assert resolve_day_config(None, 0.0)["rolling_watchlist_enabled"] is True


# ---- disabled = no-op ------------------------------------------------------ #


def test_disabled_core_never_adds():
    core = build_core()  # rolling_enabled defaults to False
    assert core.rolling_enabled is False
    before = dict(core.selected)
    assert core.maybe_rerank(t(9, 20, 0)) == []
    assert core.selected == before == {"AAA": "L", "ZZZ": "S"}
    assert core.rolling_adds == []


def test_disabled_core_marks_every_pick_as_seed():
    core = build_core()
    assert core.watch_source == {"AAA": "seed", "ZZZ": "seed"}
    assert {s["watch_source"] for s in core.watch_snapshot().values()} == {"seed"}


# ---- additive behaviour ---------------------------------------------------- #


def test_rerank_appends_without_dropping_seeds():
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    adds = core.maybe_rerank(t(9, 16, 40))
    added = {a["symbol"]: a["side"] for a in adds}
    assert added == {"BBB": "L", "CCC": "L", "DDD": "S"}
    # the 09:16 seed picks are still watched, unchanged
    assert core.selected["AAA"] == "L"
    assert core.selected["ZZZ"] == "S"
    assert core.watch_source == {
        "AAA": "seed",
        "ZZZ": "seed",
        "BBB": "rolling",
        "CCC": "rolling",
        "DDD": "rolling",
    }


def test_rerank_records_rank_pct_and_watch_size():
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    adds = core.maybe_rerank(t(9, 16, 40))
    bbb = next(a for a in adds if a["symbol"] == "BBB")
    assert bbb["rank"] == 1  # the biggest gainer of the cycle
    assert bbb["pct_change"] == 10.0  # 110 / 100 - 1
    ccc = next(a for a in adds if a["symbol"] == "CCC")
    assert ccc["rank"] == 2
    # watch_size is the running watch-list size, so it never decreases
    sizes = [a["watch_size"] for a in adds]
    assert sizes == sorted(sizes)
    assert sizes[-1] == len(core.selected)


def test_watch_size_is_monotonically_non_decreasing_across_cycles():
    core = build_core(rolling_enabled=True, rolling_cadence_s=10, rolling_top_n=1)
    sizes = [len(core.selected)]
    for i, (sym, price) in enumerate([("BBB", 110.0), ("CCC", 130.0), ("EEE", 150.0)], start=1):
        core.on_tick(sym, price, 1300, t(9, 16 + i, 5))
        core.maybe_rerank(t(9, 16 + i, 30))
        sizes.append(len(core.selected))
    assert sizes == sorted(sizes)
    assert set(core.selected) >= {"AAA", "ZZZ"}  # seeds never removed


def test_already_selected_symbol_is_never_re_sided():
    """A seed SHORT that later tops the gainers must not flip to LONG."""
    core = build_core(rolling_enabled=True, rolling_top_n=5)
    core.on_tick("ZZZ", 200.0, 1300, t(9, 17, 0))  # the day's biggest gainer now
    core.maybe_rerank(t(9, 17, 30))
    assert core.selected["ZZZ"] == "S"
    assert core.watch_source["ZZZ"] == "seed"


def test_symbol_without_a_level_is_not_added():
    """No first candle => no breakout level => watching it is pointless."""
    core = Open15Core(
        {"AAA": 100.0, "NOFC": 100.0}, vol_mult=1.5, top_n=1, rolling_enabled=True, rolling_top_n=3
    )
    core.on_tick("AAA", 102.0, 1000, t(9, 15, 10))
    core.apply_first_candles({"AAA": dict(FIRST_CANDLES["AAA"])})
    core.on_tick("AAA", 102.0, 1200, t(9, 16, 10))
    # NOFC never ticked in the 09:15 minute and the snapshot missed it, so it
    # has neither a broker candle nor a tick-built fallback
    core.last_price["NOFC"] = 150.0
    assert core.first_candle("NOFC") is None
    assert [a["symbol"] for a in core.maybe_rerank(t(9, 16, 40))] == []


# ---- cadence --------------------------------------------------------------- #


def test_cadence_throttles_between_passes():
    core = build_core(rolling_enabled=True, rolling_cadence_s=30, rolling_top_n=2)
    assert core.maybe_rerank(t(9, 16, 40))  # first pass runs
    core.on_tick("EEE", 200.0, 1300, t(9, 16, 50))  # a brand-new top gainer
    assert core.maybe_rerank(t(9, 16, 55)) == []  # 15s < 30s cadence — throttled
    assert "EEE" not in core.selected
    assert [a["symbol"] for a in core.maybe_rerank(t(9, 17, 12))] == ["EEE"]  # 32s — due


def test_cadence_is_clamped_on_the_core_too():
    core = build_core(rolling_enabled=True, rolling_cadence_s=1)
    assert core.rolling_cadence_s == 10
    core = build_core(rolling_enabled=True, rolling_cadence_s=9999, rolling_top_n=99)
    assert core.rolling_cadence_s == 300
    assert core.rolling_top_n == 10


def test_rerank_outside_the_entry_window_is_skipped():
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    assert core.maybe_rerank(t(9, 45, 0)) == []  # past the 09:29 cutoff
    assert core.rolling_adds == []


def test_rerank_before_selection_is_skipped():
    """The seed ranking must land first — a pre-09:16 add has no cohort."""
    core = Open15Core(dict(PREV_CLOSES), rolling_enabled=True, rolling_top_n=3)
    core.on_tick("BBB", 110.0, 1000, t(9, 15, 10))
    assert core.finalized is False
    assert core.maybe_rerank(t(9, 15, 40)) == []


# ---- trade_side gating ----------------------------------------------------- #


def test_long_only_never_adds_a_short():
    core = build_core(trade_side="long_only", rolling_enabled=True, rolling_top_n=3)
    adds = core.maybe_rerank(t(9, 16, 40))
    assert {a["side"] for a in adds} == {"L"}
    assert "DDD" not in core.selected  # the day's biggest loser is never watched
    assert set(core.selected.values()) == {"L"}


def test_short_only_never_adds_a_long():
    core = build_core(trade_side="short_only", rolling_enabled=True, rolling_top_n=3)
    adds = core.maybe_rerank(t(9, 16, 40))
    assert {a["side"] for a in adds} == {"S"}
    assert "BBB" not in core.selected
    assert set(core.selected.values()) == {"S"}


# ---- the entry gate is unchanged ------------------------------------------- #


def test_added_symbol_enters_on_its_own_first_candle_level():
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    core.maybe_rerank(t(9, 16, 40))
    assert core.selected["BBB"] == "L"
    core.on_tick("BBB", 110.0, 1400, t(9, 17, 10))  # builds the volume baseline
    action = core.on_tick("BBB", 105.0, 2000, t(9, 18, 10))
    assert action is not None
    assert action["level"] == FIRST_CANDLES["BBB"]["high"]  # ITS OWN 09:15 high
    assert action["watch_source"] == "rolling"
    assert action["cum_vol_at_trigger"] >= 1.5 * action["baseline_vol"]


def test_added_symbol_below_the_volume_gate_does_not_enter():
    """The gate is untouched: a level break without the surge is still no entry."""
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    core.maybe_rerank(t(9, 16, 40))
    core.on_tick("BBB", 110.0, 1400, t(9, 17, 10))
    assert core.on_tick("BBB", 105.0, 1500, t(9, 18, 10)) is None  # 100 vs 200 baseline
    assert core.entered == {}


def test_seed_entry_still_says_seed():
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    core.maybe_rerank(t(9, 16, 40))
    core.on_tick("AAA", 102.0, 1400, t(9, 17, 10))
    action = core.on_tick("AAA", 104.0, 2000, t(9, 18, 10))
    assert action is not None
    assert action["watch_source"] == "seed"


def test_rolling_symbols_appear_in_the_watch_snapshot():
    """#524 behaviour must cover the rolling cohort, or their max vol x is blank."""
    core = build_core(rolling_enabled=True, rolling_top_n=2)
    core.maybe_rerank(t(9, 16, 40))
    snap = core.watch_snapshot()
    assert set(snap) == {"AAA", "ZZZ", "BBB", "CCC", "DDD"}
    assert snap["BBB"]["watch_source"] == "rolling"
    assert snap["BBB"]["max_vol_ratio"] is None  # no data yet, NOT a 0.0 ratio
    core.on_tick("BBB", 110.0, 1400, t(9, 17, 10))
    assert core.watch_snapshot()["BBB"]["max_vol_ratio"] is not None


# ---- slot cap ------------------------------------------------------------- #


def test_max_trades_cap_binds_across_both_cohorts(monkeypatch):
    """A rolling add competes for the SAME slots — it never widens the cap."""
    import services.open15_breakout_service as svc_mod

    journaled = []
    monkeypatch.setattr(svc_mod, "_mode", lambda: "observe")
    monkeypatch.setattr(
        "database.open15_breakout_db.insert_trade", lambda **kw: journaled.append(kw) or 1
    )
    svc = svc_mod.Open15BreakoutService(order_placer=lambda mode, o: {"status": "success"})
    svc.day_config = svc_mod.resolve_day_config({"max_trades": 1}, 0.0)
    svc.positions = {"AAA": {"status": "open"}}  # the one slot is already used
    svc._enter(
        {
            "symbol": "BBB",
            "side": "L",
            "price": 105.0,
            "level": 101.0,
            "gap_pct": 0.0,
            "baseline_vol": 200.0,
            "cum_vol_at_trigger": 600.0,
            "trigger_minute": "09:18",
            "trigger_second": 10,
            "watch_source": "rolling",
        }
    )
    assert len(journaled) == 1
    assert journaled[0]["status"] == "skipped"
    assert journaled[0]["reason"] == "max_trades_cap"
    assert journaled[0]["watch_source"] == "rolling"


# ---- end-to-end session (the pipeline, not the core) ----------------------- #


def _frame(symbol, price, cumvol, h, m, s):
    """A raw (topic, payload) pair exactly as the WS proxy publishes it."""
    import json

    return (
        f"NSE_{symbol}_LTP",
        json.dumps(
            {
                "ltp": price,
                "volume": cumvol,
                "exchange_timestamp": dt.datetime(2026, 8, 3, h, m, s).timestamp(),
            }
        ),
    )


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 3, h, m, s))


def _mk_service(rolling_cfg, orders):
    """Armed service over a 4-symbol universe, with the order placer mocked."""
    import services.open15_breakout_service as svc_mod

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = svc_mod.Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "BBB", "CCC", "ZZZ"}
    svc.day_config = svc_mod.resolve_day_config(
        {"margin_per_slot": 30000, "sizing_mode": "fixed", "vol_mult": 1.5, **rolling_cfg}, 0
    )
    svc.core = svc_mod.Open15Core(
        dict.fromkeys(("AAA", "BBB", "CCC", "ZZZ"), 100.0),
        vol_mult=1.5,
        top_n=1,
        rolling_enabled=svc.day_config["rolling_watchlist_enabled"],
        rolling_cadence_s=svc.day_config["rolling_cadence_s"],
        rolling_top_n=svc.day_config["rolling_top_n"],
    )
    svc.day_status = "armed"
    svc._log_date = "2026-08-03"
    return svc


def _run_session(svc):
    """09:15 open -> 09:16 selection -> BBB rallies -> BBB breaks out on volume."""
    # 09:15: AAA gaps +3% (top gainer), CCC -3% (top loser), BBB/ZZZ flat
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("ZZZ", 101.0), ("BBB", 100.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    # 09:16 finalizes the seed selection on the first tick past the open
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    # 09:16 onward BBB is the day's real mover — invisible to the gap ranking
    svc._handle_raw(*_frame("BBB", 130.0, 6000, 9, 16, 20), _now(9, 16, 20))
    svc._handle_raw(*_frame("BBB", 130.0, 7000, 9, 17, 10), _now(9, 17, 10))
    svc._handle_raw(*_frame("BBB", 131.0, 10000, 9, 18, 10), _now(9, 18, 10))
    return [e for e in svc.day_log if e["event"] == "watchlist_add"]


def test_session_with_rolling_disabled_produces_no_additions():
    """Deploy is a no-op: the watch list is exactly today's behaviour."""
    from database.open15_breakout_db import init_db

    init_db()
    orders = []
    svc = _mk_service({"rolling_watchlist_enabled": False}, orders)
    assert _run_session(svc) == []
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}  # seeds only
    assert svc.core.rolling_adds == []
    assert orders == []  # BBB was never watched, so its breakout never traded


def test_session_with_rolling_enabled_adds_and_trades_the_mover():
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    orders = []
    svc = _mk_service(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 10, "rolling_top_n": 1}, orders
    )
    adds = _run_session(svc)
    assert [a["symbol"] for a in adds] == ["BBB"]
    assert adds[0]["side"] == "L" and adds[0]["rank"] == 1
    # additive: the 09:16 seed picks are still watched at the cutoff
    assert svc.core.selected["AAA"] == "L" and svc.core.selected["CCC"] == "S"
    assert adds[0]["watch_size"] == 3
    # the added symbol traded through the UNCHANGED entry gate
    assert len(orders) == 1 and orders[0]["symbol"] == "BBB" and orders[0]["action"] == "BUY"
    try:
        row = db_session.query(Open15Trade).filter(Open15Trade.symbol == "BBB").first()
        assert row is not None and row.watch_source == "rolling"
    finally:
        db_session.remove()


def test_session_watch_size_never_shrinks_and_status_reports_config():
    orders = []
    svc = _mk_service(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 10, "rolling_top_n": 2}, orders
    )
    _run_session(svc)
    sizes = [a["watch_size"] for a in svc.day_log if a["event"] == "watchlist_add"]
    assert sizes == sorted(sizes)
    status = svc.get_status()
    assert status["rolling"]["enabled"] is True
    assert status["rolling"]["cadence_s"] == 10
    assert status["rolling"]["top_n"] == 2
    # top-2 per side, so the first cycle also picks up ZZZ (the #2 gainer that
    # the top_n=1 seed ranking left out) before BBB rallies
    assert [a["symbol"] for a in status["rolling"]["adds"]] == ["ZZZ", "BBB"]
    # #524: the rolling cohort's running max vol x is readable too
    assert status["watch_stats"]["BBB"]["watch_source"] == "rolling"
    assert status["watch_stats"]["AAA"]["watch_source"] == "seed"


def _run_intra_candle_rally(svc):
    """The PNBHOUSING shape (issue #545): BBB opens DOWN 1%, then rallies to
    +5% inside the 09:15 candle. It is invisible to the 09:16 gap ranking (a
    negative gap can only ever be a SHORT seed, and CCC's -3% outranks it), but
    it is the #1 LONG on live price the moment selection finalizes — so the
    FIRST re-rank pass adds it on that very tick, before the old code logged
    the selection event.
    """
    for sym, o, last in (
        ("AAA", 103.0, 103.0),  # +3% gap -> seed L
        ("CCC", 97.0, 97.0),  # -3% gap -> seed S
        ("ZZZ", 101.0, 101.0),
        ("BBB", 99.0, 105.0),  # gaps DOWN, rallies inside the candle
    ):
        svc._handle_raw(*_frame(sym, o, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, last, 5000, 9, 15, 50), _now(9, 15, 50))
    # the finalizing tick — seeds AND the first re-rank pass both land here
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))


def test_first_rerank_pass_is_not_recorded_as_a_seed_pick():
    """The #545 regression: the selection event must carry SEED picks only.

    ``maybe_rerank``'s first pass has no cadence to wait for, so it fires on
    the same tick that finalizes selection and appends to ``core.selected``.
    Logging that dict verbatim tagged the added symbol ``seed`` in the decision
    log and the CSV export, and gave it the 09:15 OPEN gap where its %-at-add
    belonged — a long with a negative gap, which no seed pick can be.
    """
    from services.open15_log_view import selection_outcomes

    orders = []
    svc = _mk_service(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 30, "rolling_top_n": 1}, orders
    )
    _run_intra_candle_rally(svc)

    # BBB really was added by the first pass, on the finalizing tick
    adds = [e for e in svc.day_log if e["event"] == "watchlist_add"]
    assert [a["symbol"] for a in adds] == ["BBB"]
    assert adds[0]["side"] == "L" and adds[0]["pct_change"] == 5.0
    assert svc.core.watch_source["BBB"] == "rolling"

    sel = [e for e in svc.day_log if e["event"] == "selection"]
    assert len(sel) == 1
    assert sel[0]["selected"] == {"AAA": "L", "CCC": "S"}  # BBB must NOT be here
    assert "BBB" not in sel[0]["gaps_pct"]

    # ... and the view layer agrees end-to-end
    rows = {r["symbol"]: r for r in selection_outcomes("2026-08-03", svc.day_log)}
    assert rows["BBB"]["watch_source"] == "rolling"
    assert rows["BBB"]["gap_pct"] == 5.0  # % at add, not the -1.0 open gap
    assert rows["AAA"]["watch_source"] == "seed" and rows["AAA"]["gap_pct"] == 3.0


def test_selection_event_is_logged_without_a_tick_writer():
    """It used to be emitted from ``_capture_tick``, which returns early when
    tick capture is off — so the decision log silently lost its selection
    record (issue #545)."""
    orders = []
    svc = _mk_service({"rolling_watchlist_enabled": False}, orders)
    assert svc._tick_writer is None
    _run_session(svc)
    sel = [e for e in svc.day_log if e["event"] == "selection"]
    assert len(sel) == 1 and sel[0]["selected"] == {"AAA": "L", "CCC": "S"}


def test_armed_event_records_the_effective_rolling_config():
    """A day must be replayable from its own log (the config_source pattern)."""
    orders = []
    svc = _mk_service(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 45, "rolling_top_n": 4}, orders
    )
    svc._log_event(
        "armed",
        rolling_watchlist_enabled=svc.day_config["rolling_watchlist_enabled"],
        rolling_cadence_s=svc.day_config["rolling_cadence_s"],
        rolling_top_n=svc.day_config["rolling_top_n"],
    )
    armed = next(e for e in svc.day_log if e["event"] == "armed")
    assert armed["rolling_watchlist_enabled"] is True
    assert armed["rolling_cadence_s"] == 45 and armed["rolling_top_n"] == 4


def test_rerank_failure_never_costs_a_seed_entry(monkeypatch):
    """The rolling watch list is instrumentation — it must fail open."""
    orders = []
    svc = _mk_service(
        {"rolling_watchlist_enabled": True, "rolling_cadence_s": 10, "rolling_top_n": 1}, orders
    )

    def boom(_ts):
        raise RuntimeError("re-rank exploded")

    monkeypatch.setattr(svc.core, "maybe_rerank", boom)
    _run_session(svc)
    # the seed selection still happened and the pipeline stayed alive
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    assert [e["event"] for e in svc.day_log].count("watchlist_add") == 0


# ---- DB round-trip --------------------------------------------------------- #


def test_config_db_roundtrip_rolling():
    import database.open15_breakout_db as db

    db.init_db()
    assert db.save_config(
        30000.0,
        "fixed",
        1.5,
        updated_by="test",
        rolling_watchlist_enabled=True,
        rolling_cadence_s=60,
        rolling_top_n=5,
    )
    row = db.get_config()
    assert row["rolling_watchlist_enabled"] is True
    assert row["rolling_cadence_s"] == 60
    assert row["rolling_top_n"] == 5
    # an explicit OFF is stored as a real False, not as "unset"
    assert db.save_config(30000.0, "fixed", 1.5, rolling_watchlist_enabled=False)
    assert db.get_config()["rolling_watchlist_enabled"] is False
    # cleared back to NULL = fall through to the env default
    assert db.save_config(30000.0, "fixed", 1.5)
    row = db.get_config()
    assert row["rolling_watchlist_enabled"] is None
    assert row["rolling_cadence_s"] is None
    assert row["rolling_top_n"] is None


def test_trade_row_carries_watch_source():
    import database.open15_breakout_db as db

    db.init_db()
    row_id = db.insert_trade(
        trade_date="2026-08-03", symbol="BBB", side="L", mode="observe", watch_source="rolling"
    )
    assert row_id
    try:
        row = db.db_session.query(db.Open15Trade).filter(db.Open15Trade.id == row_id).first()
        assert row.watch_source == "rolling"
    finally:
        db.db_session.remove()


# ---- blueprint (the UI knob) ----------------------------------------------- #


@pytest.fixture
def client(monkeypatch):
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    import blueprints.open15_breakout as bp

    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def saved_config(monkeypatch):
    import database.open15_breakout_db as db

    saved = {}

    def fake_save(*args, **kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setattr(db, "save_config", fake_save)
    return saved


def test_config_post_saves_rolling_fields(client, saved_config):
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"rolling_watchlist_enabled": True, "rolling_cadence_s": 60, "rolling_top_n": 5},
    )
    assert r.status_code == 200
    assert saved_config["rolling_watchlist_enabled"] is True
    assert saved_config["rolling_cadence_s"] == 60
    assert saved_config["rolling_top_n"] == 5


def test_config_post_clamps_out_of_range_cadence(client, saved_config):
    """A hand-crafted POST cannot set a 1-second re-rank."""
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"rolling_cadence_s": 1, "rolling_top_n": 99},
    )
    assert r.status_code == 200
    assert saved_config["rolling_cadence_s"] == 10
    assert saved_config["rolling_top_n"] == 10


def test_config_post_empty_rolling_fields_clear_to_env_default(client, saved_config):
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"rolling_watchlist_enabled": "", "rolling_cadence_s": "", "rolling_top_n": ""},
    )
    assert r.status_code == 200
    assert saved_config["rolling_watchlist_enabled"] is None
    assert saved_config["rolling_cadence_s"] is None
    assert saved_config["rolling_top_n"] is None


def test_config_post_omitting_rolling_fields_leaves_them_unset(client, saved_config):
    r = client.post("/open15_vol_breakout/api/config", json={"max_trades": 3})
    assert r.status_code == 200
    assert saved_config["rolling_watchlist_enabled"] is None
    assert saved_config["rolling_cadence_s"] is None


def test_config_get_exposes_rolling_env_defaults(client, monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setenv("OPEN15_ROLLING_WATCHLIST_ENABLED", "true")
    monkeypatch.setenv("OPEN15_ROLLING_CADENCE_S", "45")
    monkeypatch.setenv("OPEN15_ROLLING_TOP_N", "2")
    d = client.get("/open15_vol_breakout/api/config").get_json()["env_defaults"]
    assert d["rolling_watchlist_enabled"] is True
    assert d["rolling_cadence_s"] == 45
    assert d["rolling_top_n"] == 2


def test_logs_page_exposes_the_cadence_input(client):
    """The 30s cadence must be editable from the UI, not just the env."""
    html = client.get("/open15_vol_breakout/logs").get_data(as_text=True)
    for marker in ("c_rollcad", "c_rolltn", "c_roll", "rolling_cadence_s", "rolling watch-list"):
        assert marker in html
