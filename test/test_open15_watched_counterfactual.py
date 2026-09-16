"""Watched-break counterfactual — the untriggered watch list priced at the 09:15
break, 1 lot, from bars, numbers only (issue #728).

Pins:
- the tick-thread break capture in ``Open15Core`` (first beyond-tick only, never
  overwritten, exported by ``watch_snapshot``);
- the bars conventions (``premiums_from_bars`` with a configured exit minute,
  ``marks_from_bars`` MAE/MFE);
- ``enrich_watched``: one ``fill='watched'`` journal row per level-broken
  no-trigger name, ``pnl`` NULL, the number in ``opt_pnl``, never-broke names
  unpriced, ``no_contract`` and ``bars_pending`` shapes, idempotent re-run,
  the pending catch-up, stock-mode days reported not guessed;
- the money locks: ``NON_REAL_FILLS`` carries ``watched`` and
  ``total_realized_pnl`` is unchanged by the rows;
- both row builders (Python ``selection_outcomes`` + the page's own JS) and the
  day digest carry the new fields;
- config resolution + storage roundtrip;
- the backfill CLI: break rebuilt from tick captures, dry-run writes nothing.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
import pytz

from database import open15_breakout_db as o15db
from services import open15_option_shadow as shadow
from services.open15_breakout_service import Open15Core, resolve_day_config

IST = pytz.timezone("Asia/Kolkata")
DATE = "2026-09-15"


@pytest.fixture(autouse=True)
def _fresh():
    o15db.init_db()
    yield
    try:
        o15db.db_session.query(o15db.Open15Trade).delete()
        o15db.db_session.query(o15db.Open15DayLog).delete()
        o15db.db_session.query(o15db.Open15Config).delete()
        o15db.db_session.commit()
    finally:
        o15db.db_session.remove()


def _ts(hh: int, mm: int, ss: int = 0) -> dt.datetime:
    return IST.localize(dt.datetime(2026, 9, 15, hh, mm, ss))


def _bars(rows: dict[str, tuple], date: str = DATE) -> list[dict]:
    """``{"HH:MM": (open, high, low, close)}`` -> broker-shaped 1m bars."""
    y, m, d = (int(x) for x in date.split("-"))
    out = []
    for hhmm, (o, h, lo, c) in rows.items():
        hh, mi = (int(x) for x in hhmm.split(":"))
        ts = IST.localize(dt.datetime(y, m, d, hh, mi))
        out.append({"timestamp": int(ts.timestamp()), "open": o, "high": h, "low": lo, "close": c})
    return out


def _events(with_break: bool = True, instrument: str = "atm_option") -> list[dict]:
    return [
        {
            "ts": "09:10:00.000",
            "event": "armed",
            "instrument": instrument,
            "exit_time": "09:30",
            "no_entry_after": "09:24",
            "mode": "live",
            "vol_mult": 1.5,
        },
        {
            "ts": "09:16:01.000",
            "event": "selection",
            "selected": {"INFY": "L", "POWERGRID": "S"},
            "gaps_pct": {"INFY": 4.56, "POWERGRID": -0.72},
            "candidates": 207,
        },
        {
            "ts": "09:30:00.400",
            "event": "no_entry",
            "symbol": "INFY",
            "side": "L",
            "watch_source": "seed",
            "level_broken": True,
            "max_vol_ratio": 1.11,
            "max_vol_ratio_while_beyond": 1.11,
            "needed": 1.5,
            **({"first_break_at": "09:17:12", "first_break_price": 1636.4} if with_break else {}),
        },
        {
            "ts": "09:30:00.500",
            "event": "no_entry",
            "symbol": "POWERGRID",
            "side": "S",
            "watch_source": "seed",
            "level_broken": False,
            "max_vol_ratio": 0.91,
            "max_vol_ratio_while_beyond": 0.0,
            "needed": 1.5,
            "first_break_at": None,
            "first_break_price": None,
        },
    ]


INFY_BARS = {
    "09:17": (24.0, 24.6, 23.9, 24.4),
    "09:18": (24.3, 25.1, 24.1, 25.0),  # entry = 09:18 open = 24.30
    "09:19": (25.0, 25.4, 23.2, 23.5),  # worst low 23.2
    "09:22": (23.6, 26.5, 23.5, 26.3),  # best high 26.5
    "09:29": (26.0, 26.2, 25.7, 25.9),
    "09:30": (25.85, 26.0, 25.6, 25.7),  # exit = 09:30 open = 25.85
    "09:31": (25.7, 27.0, 25.5, 26.9),  # after the exit — must not count
}


def _contract(symbol, side, spot, date):
    return {"symbol": f"{symbol}29SEP261640CE", "lotsize": 400, "strike": 1640, "ticksize": 0.05}


def _rows():
    try:
        return o15db.db_session.query(o15db.Open15Trade).order_by(o15db.Open15Trade.id).all()
    finally:
        o15db.db_session.remove()


# --------------------------------------------------------------------------- #
# core: the break is captured on the tick thread, once
# --------------------------------------------------------------------------- #
def _core_with_watch(symbol="INFY", side="L", high=101.0, low=99.0) -> Open15Core:
    core = Open15Core(prev_closes={symbol: 98.0}, vol_mult=1.5)
    core.finalized = True
    core.selected[symbol] = side
    core.first_candles[symbol] = {"open": 100.0, "high": high, "low": low}
    core.watch_stats[symbol] = {
        "max_vol_ratio": None,
        "max_vol_ratio_beyond": None,
        "level_broken": False,
    }
    # one completed minute so the baseline exists (the gate needs a mean)
    core.on_tick(symbol, 100.0, 1000, _ts(9, 16, 10))
    core.on_tick(symbol, 100.2, 1500, _ts(9, 17, 5))  # rolls 09:16 -> baseline [1000]
    return core


def test_core_records_the_first_break_only_and_exports_it():
    core = _core_with_watch()
    assert core.watch_stats["INFY"].get("first_break_ts") is None
    core.on_tick("INFY", 100.9, 1600, _ts(9, 17, 20))  # not beyond 101
    assert core.watch_stats["INFY"].get("first_break_ts") is None
    core.on_tick("INFY", 101.2, 1650, _ts(9, 17, 42))  # first beyond tick
    ws = core.watch_stats["INFY"]
    assert ws["first_break_ts"] == _ts(9, 17, 42)
    assert ws["first_break_price"] == 101.2
    core.on_tick("INFY", 102.0, 1700, _ts(9, 18, 3))  # later beyond tick must not overwrite
    assert ws["first_break_ts"] == _ts(9, 17, 42) and ws["first_break_price"] == 101.2
    snap = core.watch_snapshot()["INFY"]
    assert snap["first_break_at"] == "09:17:42" and snap["first_break_price"] == 101.2
    assert "INFY" not in core.entered  # 1650-1500 = 150 < 1.5x1000: no trigger


def test_core_short_side_break_is_below_the_low_and_none_when_never_broken():
    core = _core_with_watch(side="S")
    core.on_tick("INFY", 99.5, 1600, _ts(9, 17, 20))  # above the 99 low: not beyond
    assert core.watch_snapshot()["INFY"]["first_break_at"] is None
    core.on_tick("INFY", 98.7, 1620, _ts(9, 18, 1))
    assert core.watch_snapshot()["INFY"]["first_break_at"] == "09:18:01"


# --------------------------------------------------------------------------- #
# bars conventions
# --------------------------------------------------------------------------- #
def test_premiums_from_bars_honours_the_configured_exit_minute():
    bars = _bars(INFY_BARS)
    entry, exit_p = shadow.premiums_from_bars(bars, "09:17", exit_minute="09:30")
    assert (entry, exit_p) == (24.3, 25.85)
    _, exit_29 = shadow.premiums_from_bars(bars, "09:17", exit_minute="09:29")
    assert exit_29 == 26.0
    _, exit_default = shadow.premiums_from_bars(bars, "09:17")
    assert exit_default == 25.85  # default unchanged (09:30)


def test_marks_from_bars_window_is_entry_inclusive_exit_exclusive():
    bars = _bars(INFY_BARS)
    m = shadow.marks_from_bars(bars, "09:18", "09:30", 24.3, 400, sign=1)
    assert m["mae"] == round((23.2 - 24.3) * 400, 2) and m["mae_minute"] == "09:19"
    assert m["mfe"] == round((26.5 - 24.3) * 400, 2) and m["mfe_minute"] == "09:22"
    # 09:31's 27.0 high must not leak in
    assert m["mfe"] < (27.0 - 24.3) * 400
    assert shadow.marks_from_bars(bars, "09:40", "09:45", 24.3, 400) == {}


# --------------------------------------------------------------------------- #
# enrich_watched — journal row shape, money locks, idempotency, pending
# --------------------------------------------------------------------------- #
def test_enrich_watched_journals_one_row_per_break_and_never_touches_pnl(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    monkeypatch.setattr(
        shadow, "_fetch_1m_bars", lambda sym, date, exchange="NFO": _bars(INFY_BARS)
    )
    res = shadow.enrich_watched(DATE, _events())
    assert res["watched"] == 1 and res["no_break"] == 1 and res["priced"] == 1
    rows = _rows()
    assert len(rows) == 1  # POWERGRID (never broke) gets NO row
    r = rows[0]
    assert r.symbol == "INFY" and r.fill == "watched" and r.reason == "no_trigger"
    assert r.status == "skipped" and r.quantity == 0 and r.sim_quantity == 400
    assert r.trigger_price is None and r.break_at == "09:17:12" and r.break_price == 1636.4
    assert r.opt_symbol == "INFY29SEP261640CE" and r.opt_lot_size == 400
    assert (r.opt_entry_premium, r.opt_exit_premium) == (24.3, 25.85)
    gross = round((25.85 - 24.3) * 400, 2)
    charges = shadow.option_round_trip_charges(24.3 * 400, 25.85 * 400)
    assert r.opt_charges_inr == charges and r.opt_pnl == round(gross - charges, 2)
    assert r.cf_mae == round((23.2 - 24.3) * 400, 2) and r.cf_source == "bars"
    assert r.cf_exit_minute == "09:30" and r.gap_pct == 4.56
    # the money locks
    assert r.pnl is None and r.charges_inr is None
    assert "watched" in o15db.NON_REAL_FILLS
    assert o15db.total_realized_pnl() == 0.0
    assert o15db.watched_net_of_row(r) == r.opt_pnl
    assert o15db.watched_pnl_by_date() == {DATE: r.opt_pnl}
    assert o15db.trades_pnl_by_date() == {} and o15db.sim_pnl_by_date() == {}
    # the event detail the service emits
    d = res["rows"][0]
    assert d["symbol"] == "INFY" and d["pnl"] == r.opt_pnl and d["status"] == "priced"
    assert d["entry_minute"] == "09:18" and d["exit_minute"] == "09:30"
    # re-run: nothing new, nothing re-priced
    again = shadow.enrich_watched(DATE, _events())
    assert again["already"] == 1 and again["priced"] == 0 and len(_rows()) == 1


def test_enrich_watched_pending_then_caught_up(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: None)  # broker lag
    res = shadow.enrich_watched(DATE, _events())
    assert res["pending"] == 1 and res["priced"] == 0 and res["rows"] == []
    r = _rows()[0]
    assert r.fill == "watched" and r.opt_pnl is None and r.opt_symbol
    # bars arrive: the arm-time catch-up reads everything from the row itself
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: _bars(INFY_BARS))
    res2 = shadow.enrich_watched_pending()
    assert res2["priced"] == 1
    assert _rows()[0].opt_entry_premium == 24.3
    assert shadow.enrich_watched_pending()["priced"] == 0  # idempotent


def test_enrich_watched_no_contract_and_no_break_time(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", lambda *a, **k: None)
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: _bars(INFY_BARS))
    res = shadow.enrich_watched(DATE, _events())
    assert res["no_contract"] == 1 and res["priced"] == 0
    r = _rows()[0]
    assert r.fill == "watched" and r.reason == "no_contract" and r.opt_symbol is None
    assert r.opt_pnl is None and r.pnl is None
    # a pre-#728 day: level broken, no break time -> nothing is invented
    o15db.db_session.query(o15db.Open15Trade).delete()
    o15db.db_session.commit()
    o15db.db_session.remove()
    res = shadow.enrich_watched(DATE, _events(with_break=False))
    assert res["no_break_time"] == 1 and _rows() == []


def test_enrich_watched_reports_a_stock_mode_day_instead_of_guessing(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    res = shadow.enrich_watched(DATE, _events(instrument="stock"))
    assert res["unsupported"] == 2 and _rows() == []


def test_breaks_override_supplies_the_moment_for_a_pre_728_log(monkeypatch):
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: _bars(INFY_BARS))
    breaks = {"INFY": {"break_at": "09:17:40", "break_price": 1637.0}}
    res = shadow.enrich_watched(DATE, _events(with_break=False), breaks=breaks)
    assert res["priced"] == 1 and _rows()[0].break_at == "09:17:40"


# --------------------------------------------------------------------------- #
# row builders + digest
# --------------------------------------------------------------------------- #
def _wcf_event() -> dict:
    return {
        "ts": "09:35:01.000",
        "event": "watched_counterfactual",
        "symbol": "INFY",
        "side": "L",
        "watch_source": "seed",
        "break_at": "09:17:12",
        "break_price": 1636.4,
        "contract": "INFY29SEP261640CE",
        "lot_size": 400,
        "entry_minute": "09:18",
        "entry_premium": 24.3,
        "exit_minute": "09:30",
        "exit_premium": 25.85,
        "gross": 620.0,
        "charges": 78.8,
        "pnl": 541.2,
        "mae": -440.0,
        "mfe": 880.0,
        "source": "bars",
        "status": "priced",
    }


def test_selection_outcomes_carry_the_break_and_the_counterfactual():
    from services.open15_log_view import CSV_COLUMNS, render_csv, selection_outcomes

    rows = {r["symbol"]: r for r in selection_outcomes(DATE, [*_events(), _wcf_event()])}
    infy, pg = rows["INFY"], rows["POWERGRID"]
    assert infy["entered"] is False and infy["fill"] == "watched"
    assert infy["break_at"] == "09:17:12" and infy["break_price"] == 1636.4
    assert infy["wcf_contract"] == "INFY29SEP261640CE" and infy["wcf_net"] == 541.2
    assert infy["wcf_entry"] == 24.3 and infy["wcf_exit"] == 25.85
    assert infy["wcf_mae"] == -440.0 and infy["wcf_status"] == "priced"
    assert infy["pnl"] is None  # never a P&L
    assert pg["wcf_status"] == "no_break" and pg["wcf_net"] is None
    assert "wcf_net" in CSV_COLUMNS and "break_at" in CSV_COLUMNS
    header = render_csv([infy]).split("\n")[0]
    assert header.endswith("wcf_status,wcf_source")


def test_journal_overlay_prices_a_sealed_day_without_an_event():
    """The arm-time catch-up cannot append events to yesterday — the journal is
    the only source then, and the Python builder must read it (#557 shape)."""
    from services.open15_log_view import selection_outcomes

    journal = [
        {
            "symbol": "INFY",
            "fill": "watched",
            "reason": "no_trigger",
            "instrument": "option",
            "opt_symbol": "INFY29SEP261640CE",
            "opt_entry_premium": 24.3,
            "opt_exit_premium": 25.85,
            "opt_pnl": 541.2,
            "opt_charges_inr": 78.8,
            "cf_mae": -440.0,
            "cf_mfe": 880.0,
            "break_at": "09:17:12",
            "break_price": 1636.4,
            "quantity": 0,
            "sim_quantity": 400,
            "pnl": None,
        }
    ]
    rows = {r["symbol"]: r for r in selection_outcomes(DATE, _events(), journal=journal)}
    infy = rows["INFY"]
    assert infy["fill"] == "watched" and infy["wcf_net"] == 541.2 and infy["qty"] == 400
    assert infy["wcf_status"] == "priced" and infy["pnl"] is None and infy["entered"] is False
    pending = dict(journal[0], opt_pnl=None, opt_entry_premium=None, opt_exit_premium=None)
    rows = {r["symbol"]: r for r in selection_outcomes(DATE, _events(), journal=[pending])}
    assert rows["INFY"]["wcf_status"] == "bars_pending" and rows["INFY"]["wcf_net"] is None


def test_summarize_day_counts_the_fifth_bucket():
    from services.open15_log_view import summarize_day

    d = summarize_day(DATE, [*_events(), _wcf_event()])
    assert d["watched"] == 1 and d["watched_nobreak"] == 1 and d["watched_priced"] == 1
    assert d["watched_pnl"] == 541.2 and d["pnl"] is None and d["sim_pnl"] is None
    d2 = summarize_day(DATE, _events(), watched_pnl=-12.5)
    assert d2["watched_pnl"] == -12.5 and d2["watched_priced"] == 0


def test_logs_page_js_agrees_with_python_on_watched_rows():
    from test.test_open15_log_view import _run_render_sel

    events = [*_events(), _wcf_event()]
    js = _run_render_sel(events)
    assert js["INFY"]["fill"] == "watched" and js["INFY"]["wcfNet"] == 541.2
    assert js["INFY"]["breakAt"] == "09:17:12" and js["INFY"]["wcfStatus"] == "priced"
    assert js["POWERGRID"]["wcfStatus"] == "no_break"
    assert "09:17:12" in js["INFY"]["out"]
    # the sealed-day path: journal only, no event
    journal = [
        {
            "symbol": "INFY",
            "fill": "watched",
            "reason": "no_trigger",
            "instrument": "option",
            "opt_symbol": "INFY29SEP261640CE",
            "opt_entry_premium": 24.3,
            "opt_exit_premium": 25.85,
            "opt_pnl": 541.2,
            "opt_charges_inr": 78.8,
            "cf_mae": -440.0,
            "cf_mfe": 880.0,
            "break_at": "09:17:12",
            "break_price": 1636.4,
            "quantity": 0,
            "sim_quantity": 400,
            "pnl": None,
        }
    ]
    js2 = _run_render_sel(_events(), journal)
    assert js2["INFY"]["fill"] == "watched" and js2["INFY"]["wcfNet"] == 541.2
    assert js2["INFY"]["wcfGross"] == 620.0 and js2["INFY"].get("net") is None
    from services.open15_log_view import selection_outcomes

    py = {r["symbol"]: r for r in selection_outcomes(DATE, _events(), journal=journal)}
    assert set(js2) == set(py) and py["INFY"]["wcf_net"] == js2["INFY"]["wcfNet"]


def test_logs_page_never_colours_a_watched_number_as_money():
    from blueprints.open15_breakout import _LOGS_PAGE

    fn = _LOGS_PAGE.split("function pnlCell(r){")[1].split("function renderTimeline")[0]
    head = fn.split("if(r.net==null)return dash;")[0]
    assert "r.fill==='watched'" in head and "b-watched" in head
    assert "'pos'" not in head and "'neg'" not in head
    assert "c_watched" in _LOGS_PAGE and "watched_cf_enabled" in _LOGS_PAGE


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_resolution_and_roundtrip(monkeypatch):
    monkeypatch.delenv("OPEN15_WATCHED_CF", raising=False)
    assert resolve_day_config(None, 0.0)["watched_cf_enabled"] is True
    assert resolve_day_config({"watched_cf_enabled": False}, 0.0)["watched_cf_enabled"] is False
    monkeypatch.setenv("OPEN15_WATCHED_CF", "false")
    assert resolve_day_config(None, 0.0)["watched_cf_enabled"] is False
    assert resolve_day_config({"watched_cf_enabled": True}, 0.0)["watched_cf_enabled"] is True
    assert o15db.save_config(60000.0, "fixed", 1.5, watched_cf_enabled=False)
    assert o15db.get_config()["watched_cf_enabled"] is False
    assert o15db.save_config(60000.0, "fixed", 1.5)
    assert o15db.get_config()["watched_cf_enabled"] is None


# --------------------------------------------------------------------------- #
# backfill CLI
# --------------------------------------------------------------------------- #
def _tick_file(tmp_path, recs):
    d = tmp_path / "ticks"
    d.mkdir()
    with open(d / "ticks-20260915-1.jsonl", "w", encoding="utf-8") as fh:
        for ts, sym, ltp in recs:
            fh.write(json.dumps({"ts": ts, "symbol": sym, "ltp": ltp, "volume": 1}) + "\n")
    return str(d)


def test_cli_rebuilds_the_break_from_ticks(tmp_path):
    from services import open15_watched_backfill as cli

    recs = [
        ("2026-09-15T09:15:00.100000", "INFY", 1630.0),
        ("2026-09-15T09:15:30.000000", "INFY", 1636.2),  # 09:15 high
        ("2026-09-15T09:15:59.000000", "INFY", 1604.1),  # 09:15 low
        ("2026-09-15T09:16:10.000000", "INFY", 1636.2),  # AT the level: not beyond
        ("2026-09-15T09:17:12.000000", "INFY", 1636.4),  # first break
        ("2026-09-15T09:18:00.000000", "INFY", 1640.0),
        ("2026-09-15T09:15:10.000000", "POWERGRID", 280.0),
        ("2026-09-15T09:20:00.000000", "POWERGRID", 280.5),  # never below the low
        ("2026-09-15T09:26:00.000000", "LATE", 100.0),
    ]
    tick_dir = _tick_file(tmp_path, recs)
    ticks = cli.load_ticks(DATE, {"INFY", "POWERGRID"}, tick_dir)
    fb = cli.first_break_from_ticks(ticks["INFY"], "L", 9 * 60 + 24)
    assert fb["break_at"] == "09:17:12" and fb["break_price"] == 1636.4 and fb["level"] == 1636.2
    assert cli.first_break_from_ticks(ticks["POWERGRID"], "S", 9 * 60 + 24) is None
    # a break AFTER the entry cutoff does not count
    late = [
        (dt.datetime(2026, 9, 15, 9, 15, 5), 100.0),
        (dt.datetime(2026, 9, 15, 9, 26, 0), 101.0),
    ]
    assert cli.first_break_from_ticks(late, "L", 9 * 60 + 24) is None
    # pre-#728 log (no first_break_at): the break comes from the ticks
    ev = _events(with_break=False)
    ev[2]["level_broken"] = True
    breaks = cli.reconstruct_breaks(DATE, ev, tick_dir)
    assert breaks["INFY"]["source"] == "ticks" and breaks["INFY"]["break_at"] == "09:17:12"
    assert "POWERGRID" not in breaks  # never broke: nothing to rebuild
    # a post-#728 log keeps its own moment
    breaks2 = cli.reconstruct_breaks(DATE, _events(), tick_dir)
    assert breaks2["INFY"]["source"] == "log" and breaks2["INFY"]["break_at"] == "09:17:12"


def test_cli_dry_run_writes_nothing_and_apply_prices(tmp_path, monkeypatch):
    from services import open15_watched_backfill as cli

    ev = _events(with_break=False)
    assert o15db.save_day_log(DATE, ev)
    tick_dir = _tick_file(
        tmp_path,
        [
            ("2026-09-15T09:15:00.100000", "INFY", 1636.2),
            ("2026-09-15T09:15:59.000000", "INFY", 1604.1),
            ("2026-09-15T09:17:12.000000", "INFY", 1636.4),
        ],
    )
    monkeypatch.setattr(shadow, "resolve_atm_option", _contract)
    monkeypatch.setattr(shadow, "_fetch_1m_bars", lambda *a, **k: _bars(INFY_BARS))
    rep = cli.run(DATE, DATE, apply=False, tick_dir=tick_dir)
    assert rep[DATE]["plan"]["INFY"]["contract"] == "INFY29SEP261640CE"
    assert _rows() == []  # dry run
    rep = cli.run(DATE, DATE, apply=True, tick_dir=tick_dir)
    assert rep[DATE]["result"]["priced"] == 1
    r = _rows()[0]
    assert r.fill == "watched" and r.break_at == "09:17:12" and r.opt_pnl is not None
    rep2 = cli.run(DATE, DATE, apply=True, tick_dir=tick_dir)
    assert rep2[DATE]["result"]["already"] == 1 and len(_rows()) == 1
