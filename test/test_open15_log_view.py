"""Tests for the open15 decision-log history layer (issue #444).

Covers the pure view helpers (`services/open15_log_view.py`), the DB listing
and per-date P&L helpers, and the per-event day-log persistence that makes a
mid-window crash lossless.
"""

from __future__ import annotations

# Real-shaped events (trimmed copies of the 2026-07-23 production log).
TRADED_DAY = [
    {"ts": "09:10:00.190", "event": "armed", "universe": 211, "vol_mult": 1.5, "mode": "sandbox"},
    {
        "ts": "09:16:00.167",
        "event": "selection",
        "selected": {"OIL": "L", "OFSS": "L", "DRREDDY": "S"},
        "gaps_pct": {"OIL": 0.52, "OFSS": 4.3, "DRREDDY": -6.67},
        "candidates": 211,
    },
    {
        "ts": "09:25:36.168",
        "event": "entry",
        "symbol": "OIL",
        "side": "BUY",
        "qty": 329,
        "trigger_price": 455.75,
        "level": 452.95,
        "vol_ratio": 1.95,
        "order_status": "success",
    },
    {
        "ts": "09:30:04.076",
        "event": "exit",
        "symbol": "OIL",
        "action": "SELL",
        "qty": 329,
        "exit_price": 456.0,
        "pnl": 82.0,
        "order_status": "success",
        "reason": "eod_0930",
    },
    {
        "ts": "09:30:04.084",
        "event": "no_entry",
        "symbol": "OFSS",
        "side": "L",
        "level_broken": True,
        "max_vol_ratio": 1.31,
        "max_vol_ratio_while_beyond": 0.18,
        "needed": 1.5,
    },
    {
        "ts": "09:30:04.092",
        "event": "no_entry",
        "symbol": "DRREDDY",
        "side": "S",
        "level_broken": False,
        "max_vol_ratio": 0.99,
        "max_vol_ratio_while_beyond": 0.0,
        "needed": 1.5,
    },
    {"ts": "09:35:00.014", "event": "summary", "selected": 3, "entered": 1, "day": "done"},
]

SKIPPED_DAY = [
    {"ts": "09:16:02.000", "event": "skipped_late_boot", "armed_at": "09:16:02"},
]


def test_summarize_day_traded():
    from services.open15_log_view import summarize_day

    d = summarize_day("2026-07-23", TRADED_DAY)
    assert d == {
        "date": "2026-07-23",
        "status": "done",
        "selected": 3,
        "entered": 1,
        "pnl": 82.0,
        "events": len(TRADED_DAY),
    }


def test_summarize_day_trades_pnl_overrides_events():
    from services.open15_log_view import summarize_day

    assert summarize_day("2026-07-23", TRADED_DAY, trades_pnl=59.5)["pnl"] == 59.5


def test_summarize_day_skipped():
    from services.open15_log_view import summarize_day

    d = summarize_day("2026-07-21", SKIPPED_DAY)
    assert d["status"] == "skipped_late_boot"
    assert d["selected"] == 0 and d["entered"] == 0 and d["pnl"] is None


def test_selection_outcomes_rows():
    from services.open15_log_view import selection_outcomes

    rows = {r["symbol"]: r for r in selection_outcomes("2026-07-23", TRADED_DAY)}
    assert set(rows) == {"OIL", "OFSS", "DRREDDY"}
    oil = rows["OIL"]
    assert oil["entered"] is True
    assert oil["trigger_price"] == 455.75 and oil["level"] == 452.95
    assert oil["exit_price"] == 456.0 and oil["pnl"] == 82.0
    ofss = rows["OFSS"]
    assert ofss["entered"] is False
    assert ofss["level_broken"] is True and ofss["max_vol_ratio"] == 1.31
    assert ofss["vol_needed"] == 1.5 and ofss["gap_pct"] == 4.3
    assert rows["DRREDDY"]["level_broken"] is False


def test_logs_page_outcome_quotes_the_beyond_ratio():
    """The `vol X < needed` sentence must quote the gate's own number (#525).

    `on_tick` enters on `beyond and cum_in_min >= vol_mult*baseline`, so the
    ratio being compared is the peak measured WHILE beyond the level. Quoting
    the peak-anywhere `max_vol_ratio` there produced self-contradicting rows
    like "level broken - vol 1.95x < 1.5" (INDIGO, 2026-08-03: peak 1.95x
    inside the candle, only 1.27x while actually beyond).
    """
    from blueprints.open15_breakout import _LOGS_PAGE

    branch = _LOGS_PAGE.split("e.event==='no_entry'")[-1].split("entry_skipped")[0]
    assert "max_vol_ratio_while_beyond" in branch
    assert "while beyond" in branch


def test_selection_outcomes_empty_for_skipped_day():
    from services.open15_log_view import selection_outcomes

    assert selection_outcomes("2026-07-21", SKIPPED_DAY) == []


def test_render_csv_header_and_rows():
    from services.open15_log_view import CSV_COLUMNS, render_csv, selection_outcomes

    csv_text = render_csv(selection_outcomes("2026-07-23", TRADED_DAY))
    lines = csv_text.strip().split("\n")
    assert lines[0] == ",".join(CSV_COLUMNS)
    assert len(lines) == 4  # header + 3 selected symbols
    oil_line = next(ln for ln in lines if ln.startswith("2026-07-23,OIL"))
    assert "455.75" in oil_line and "82.0" in oil_line


def test_db_list_day_logs_and_pnl_by_date():
    from database.open15_breakout_db import (
        init_db,
        insert_trade,
        list_day_logs,
        save_day_log,
        trades_pnl_by_date,
    )

    init_db()
    assert save_day_log("2026-07-22", [{"event": "armed"}])
    assert save_day_log("2026-07-23", TRADED_DAY)
    days = dict(list_day_logs())
    assert list(days)[:2] == ["2026-07-23", "2026-07-22"]  # newest first
    assert days["2026-07-23"][0]["event"] == "armed"
    insert_trade(trade_date="2026-07-23", symbol="OIL", side="L", mode="sandbox", pnl=82.0)
    insert_trade(trade_date="2026-07-23", symbol="OFSS", side="L", mode="sandbox", pnl=-22.5)
    assert trades_pnl_by_date()["2026-07-23"] == 59.5


def test_log_event_persists_immediately(monkeypatch):
    """A mid-window crash must not lose the day: every event upserts the row."""
    from database.open15_breakout_db import get_day_log, init_db
    from services.open15_breakout_service import Open15BreakoutService

    init_db()
    svc = Open15BreakoutService(order_placer=lambda *a, **k: {"status": "success"})
    svc._log_date = "2026-01-05"
    svc._log_event("selection", selected={"OIL": "L"}, gaps_pct={"OIL": 0.5}, candidates=10)
    persisted = get_day_log("2026-01-05")
    assert persisted is not None and persisted[-1]["event"] == "selection"
    svc._log_event("entry", symbol="OIL", order_status="success")
    assert get_day_log("2026-01-05")[-1]["event"] == "entry"
