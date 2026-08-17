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
        # a pre-#529 day has no watchlist_add events, so the rolling cohort is 0
        "rolling_added": 0,
        "liq_excluded": 0,
        "liq_excluded_stage1": 0,
        "liq_excluded_stage2": 0,
        "entered": 1,
        # a pre-#548 day has no entry_rejected/exit_paper events, so the paper
        # cohort is empty and paper P&L is absent (not 0 — nothing was simulated)
        "paper": 0,
        "paper_pnl": None,
        # likewise a pre-#555 day has no entry_skipped/exit_sim events, so the
        # sim cohort is empty and its P&L is absent rather than 0 — "nothing was
        # simulated" and "the simulation broke even" must stay distinguishable
        "sim": 0,
        "sim_pnl": None,
        # and a pre-#581 day has no entry_shadow/exit_shadow events — same rule,
        # absent rather than 0, so "the excluded side was never watched" stays
        # distinguishable from "it was watched and broke even"
        "shadow": 0,
        "shadow_pnl": None,
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


def test_selection_outcomes_fills_max_vol_for_entered_symbol():
    """The `watch_stats` event covers EVERY selected symbol (issue #524).

    Pre-#524 the entered row's `max_vol_ratio` was blank — `no_entry` skips
    symbols that traded — which left the UI's `max vol×` column empty for them.
    """
    from services.open15_log_view import selection_outcomes

    # emitted just before the per-symbol no_entry events, as the eod block does
    day = list(TRADED_DAY)
    day.insert(
        -1,
        {
            "ts": "09:30:04.070",
            "event": "watch_stats",
            "needed": 1.5,
            "stats": {
                "OIL": {"max_vol_ratio": 1.95, "level_broken": True, "entered": True},
                "OFSS": {"max_vol_ratio": 1.31, "level_broken": True, "entered": False},
                "DRREDDY": {"max_vol_ratio": 0.99, "level_broken": False, "entered": False},
            },
        },
    )
    rows = {r["symbol"]: r for r in selection_outcomes("2026-07-23", day)}
    assert rows["OIL"]["max_vol_ratio"] == 1.95  # was None before #524
    assert rows["OIL"]["vol_needed"] == 1.5 and rows["OIL"]["entered"] is True
    # the per-symbol no_entry event still wins for non-entered symbols
    assert rows["OFSS"]["max_vol_ratio"] == 1.31
    assert rows["DRREDDY"]["level_broken"] is False


def test_selection_outcomes_tags_seed_rows():
    """A pre-#529 day is all seed — the column is never blank (issue #529)."""
    from services.open15_log_view import selection_outcomes

    rows = selection_outcomes("2026-07-23", TRADED_DAY)
    assert {r["watch_source"] for r in rows} == {"seed"}


def test_selection_outcomes_adds_rolling_rows():
    """A rolling add is a watched symbol and gets its own outcome row (#529)."""
    from services.open15_log_view import selection_outcomes, summarize_day

    day = list(TRADED_DAY)
    day.insert(
        2,
        {
            "ts": "09:18:30.000",
            "event": "watchlist_add",
            "symbol": "JUBLFOOD",
            "side": "L",
            "pct_change": 6.5,
            "rank": 1,
            "watch_size": 4,
            "at": "09:18:30",
        },
    )
    rows = {r["symbol"]: r for r in selection_outcomes("2026-07-23", day)}
    assert set(rows) == {"OIL", "OFSS", "DRREDDY", "JUBLFOOD"}
    jub = rows["JUBLFOOD"]
    assert jub["watch_source"] == "rolling"
    assert jub["side"] == "L" and jub["entered"] is False
    # a rolling add has no 09:15 gap — gap_pct carries its % change AT ADD
    assert jub["gap_pct"] == 6.5
    # the 09:16 seed picks keep their own tag
    assert rows["OIL"]["watch_source"] == "seed"
    assert summarize_day("2026-07-23", day)["rolling_added"] == 1


def test_selection_outcomes_repairs_pre545_polluted_selection_event():
    """A pre-#545 log double-recorded the FIRST re-rank pass (issue #545).

    Before the fix, ``maybe_rerank`` appended to ``core.selected`` on the very
    tick that finalized selection — with its own ``watchlist_add`` logged first
    — so the ``selection`` event that followed carried the rolling adds too.
    The parse must trust the ``watchlist_add`` (a seed pick can never emit one)
    and report the symbol as rolling, carrying its %-at-add rather than the
    09:15 open gap.
    """
    from services.open15_log_view import selection_outcomes

    day = list(TRADED_DAY)
    # ordering is load-bearing: the add precedes the polluted selection event
    day.insert(
        1,
        {
            "ts": "09:16:00.100",
            "event": "watchlist_add",
            "symbol": "PNBHOUSING",
            "side": "L",
            "pct_change": 2.67,
            "rank": 3,
            "watch_size": 5,
            "at": "09:16:00",
        },
    )
    day[2] = {
        "ts": "09:16:00.167",
        "event": "selection",
        # PNBHOUSING leaked in, with its OPEN gap (negative — it gapped down
        # and then rallied inside the 09:15 candle) and a LONG side, which no
        # seed pick can ever have
        "selected": {"OIL": "L", "OFSS": "L", "DRREDDY": "S", "PNBHOUSING": "L"},
        "gaps_pct": {"OIL": 0.52, "OFSS": 4.3, "DRREDDY": -6.67, "PNBHOUSING": -0.91},
        "candidates": 211,
    }
    rows = {r["symbol"]: r for r in selection_outcomes("2026-08-05", day)}
    pnb = rows["PNBHOUSING"]
    assert pnb["watch_source"] == "rolling"
    assert pnb["gap_pct"] == 2.67  # % at add, NOT the -0.91 open gap
    assert pnb["side"] == "L"
    # exactly one row for the symbol, and the genuine seed picks are untouched
    assert len(rows) == 4
    assert rows["OIL"]["watch_source"] == "seed" and rows["OIL"]["gap_pct"] == 0.52


def test_selection_outcomes_unchanged_without_watch_stats_event():
    """Pre-#524 stored days must parse byte-identically (no such event)."""
    from services.open15_log_view import render_csv, selection_outcomes

    rows = selection_outcomes("2026-07-23", TRADED_DAY)
    assert {r["symbol"]: r["max_vol_ratio"] for r in rows} == {
        "OIL": None,
        "OFSS": 1.31,
        "DRREDDY": 0.99,
    }
    assert render_csv(rows) == render_csv(selection_outcomes("2026-07-23", TRADED_DAY))


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


def test_logs_page_colours_max_vol_by_the_beyond_ratio():
    """Green in the `max vol×` column must mean the GATE cleared (#524 x #525).

    The column shows the peak ANYWHERE in the minute, which can sit above the
    threshold on a symbol that correctly never entered (INDIGO 2026-08-03: 1.95x
    peak, 1.27x while beyond, needed 1.5). Colouring that number green would
    re-tell the same lie #525 removed from the outcome sentence.
    """
    from blueprints.open15_breakout import _LOGS_PAGE

    fn = _LOGS_PAGE.split("function fmtVol(")[1].split("function renderSel")[0]
    packed = fn.replace(" ", "")
    assert "beyond!=null&&beyond>=needed" in packed
    assert "v>=needed" not in packed  # never gate the colour on the peak-anywhere value


def _run_render_sel(events, journal=None):
    """Execute the logs page's OWN row-building JS in node (issue #545).

    The page builds its selection-outcomes table client-side, duplicating
    ``selection_outcomes``. That duplication is what let #545 be fixed in Python
    while the page kept showing the wrong thing — so this test runs the real
    extracted source rather than grepping it for a substring.

    ``applyJournal`` (issue #557) is extracted too, because the journal overlay
    IS part of row building — a row whose P&L came only from the timeline is
    stale the moment a reconcile lands.
    """
    import json
    import shutil
    import subprocess

    import pytest

    from blueprints.open15_breakout import _LOGS_PAGE

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    body = _LOGS_PAGE.split("function renderSel(){")[1].split("// mid-window")[0]
    overlay = (
        "function applyJournal(rows){"
        + _LOGS_PAGE.split("function applyJournal(rows){")[1].split("\nconst dash=")[0]
    )
    script = (
        "const esc=s=>String(s);\n"
        f"const curEvents={json.dumps(events)};\n"
        f"const curJournal={json.dumps(journal or [])};\n"
        f"{overlay}\n"
        f"{body}\n"
        "console.log(JSON.stringify(rows));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=True
    )
    return json.loads(out.stdout)


# The real 2026-08-05 shape: the first re-rank pass fired on the same tick that
# finalized selection, so its adds leaked into the `selection` event AND are
# logged earlier than it. PNBHOUSING is the tell — a LONG with a negative gap.
POLLUTED_DAY = [
    {"ts": "09:10:00.190", "event": "armed", "universe": 211, "vol_mult": 1.5},
    {
        "ts": "09:16:00.100",
        "event": "watchlist_add",
        "symbol": "PNBHOUSING",
        "side": "L",
        "pct_change": 2.67,
        "rank": 3,
        "watch_size": 5,
        "at": "09:16:00",
    },
    {
        "ts": "09:16:00.167",
        "event": "selection",
        "selected": {"ICICIGI": "L", "INDIGO": "L", "PNBHOUSING": "L"},
        "gaps_pct": {"ICICIGI": 5.14, "INDIGO": 1.9, "PNBHOUSING": -0.91},
        "candidates": 211,
    },
]


def test_logs_page_js_does_not_relabel_a_rolling_add_as_seed():
    """The page JS must reach the same verdict as `selection_outcomes` (#545)."""
    rows = _run_render_sel(POLLUTED_DAY)
    assert rows["PNBHOUSING"]["src"] == "rolling"
    assert rows["PNBHOUSING"]["gap"] == 2.67  # % at add, NOT the -0.91 open gap
    assert rows["PNBHOUSING"]["side"] == "L"
    assert rows["ICICIGI"]["src"] == "seed" and rows["ICICIGI"]["gap"] == 5.14


def test_logs_page_js_and_python_parsers_agree():
    """The duplicated parsers must not drift — the #545 failure mode."""
    from services.open15_log_view import selection_outcomes

    js = _run_render_sel(POLLUTED_DAY)
    py = {r["symbol"]: r for r in selection_outcomes("2026-08-05", POLLUTED_DAY)}
    assert set(js) == set(py)
    for sym in py:
        assert js[sym]["src"] == py[sym]["watch_source"], sym
        assert js[sym]["gap"] == py[sym]["gap_pct"], sym
        assert js[sym]["side"] == py[sym]["side"], sym


def test_logs_page_js_keeps_a_rolling_row_outcome():
    """The override must not wipe an outcome already recorded for the row."""
    day = [
        *POLLUTED_DAY,
        {
            "ts": "09:20:00.000",
            "event": "entry",
            "symbol": "PNBHOUSING",
            "trigger_price": 478.45,
            "vol_ratio": 1.84,
            "order_status": "success",
        },
    ]
    rows = _run_render_sel(day)
    assert rows["PNBHOUSING"]["src"] == "rolling"
    assert "entered" in rows["PNBHOUSING"]["out"]
    # the entry price moved out of the outcome text into its own column
    # (issue #555) — it must still be on the row, not merely still rendered
    assert rows["PNBHOUSING"]["stockEntry"] == 478.45


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
    # newest first. Assert the ORDER of this test's own dates rather than their
    # absolute positions: the DB is shared across the session, so another test
    # file's persisted day log would otherwise fail this unrelated assertion.
    mine = [d for d in days if d in ("2026-07-22", "2026-07-23")]
    assert mine == ["2026-07-23", "2026-07-22"]
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


# --- issue #552: ONE P&L convention (net) across chip, digest and rows --------
#
# The 2026-08-05 shape, verbatim: three broker-rejected option entries whose
# gross sums to +2108.75 while the per-symbol rows (net of modelled charges)
# sum to +1383.81. Pre-fix the chip read +2109 above rows totalling +1384.
_PAPER_DAY_ROWS = [
    # (symbol, gross pnl, charges, net)
    ("JUBLFOOD", -312.50, 229.22, -541.72),
    ("GODREJPROP", 1803.75, 246.34, 1557.41),
    ("DLF", 617.50, 249.38, 368.12),
]
_PAPER_DAY_NET = round(sum(net for *_, net in _PAPER_DAY_ROWS), 2)  # 1383.81


def _paper_day_events(date: str) -> list[dict]:
    ev: list[dict] = [
        {"event": "armed", "universe": 211, "vol_mult": 1.5, "mode": "live"},
        {
            "event": "selection",
            "selected": {s: "L" for s, *_ in _PAPER_DAY_ROWS},
            "gaps_pct": {s: 2.5 for s, *_ in _PAPER_DAY_ROWS},
        },
    ]
    for sym, gross, charges, net in _PAPER_DAY_ROWS:
        ev.append({"event": "entry_rejected", "symbol": sym, "entry_price": 20.0, "error": "403"})
        ev.append(
            {
                "event": "exit_paper",
                "symbol": sym,
                "instrument": "option",
                "exit_price": 21.0,
                "gross": gross,
                "charges": charges,
                "pnl": net,
                "fill": "paper",
            }
        )
    return ev


def test_paper_day_digest_equals_the_sum_of_its_rows():
    """The chip and the rows below it must be the same number (issue #552).

    Pre-fix the digest summed the events' ``gross`` while the page rendered
    each row's ``pnl`` (net), so the header overstated the day by the whole
    charge bill — 34% of gross on the day this was found.
    """
    from services.open15_log_view import selection_outcomes, summarize_day

    events = _paper_day_events("2026-08-05")
    digest = summarize_day("2026-08-05", events)
    rows_total = round(
        sum(r["pnl"] for r in selection_outcomes("2026-08-05", events) if r.get("pnl") is not None),
        2,
    )
    assert rows_total == _PAPER_DAY_NET
    assert digest["paper_pnl"] == _PAPER_DAY_NET, (
        f"chip {digest['paper_pnl']} != rows {rows_total} — the header and the "
        "table are reporting different P&L conventions again"
    )
    assert digest["pnl"] is None and digest["paper"] == 3


def test_pnl_by_date_and_realized_pnl_are_net_of_charges():
    """``charges_inr`` is real money — never report or compound gross (#552)."""
    from database.open15_breakout_db import (
        db_session,
        init_db,
        insert_trade,
        paper_pnl_by_date,
        total_realized_pnl,
        trades_pnl_by_date,
    )

    init_db()
    before = total_realized_pnl()
    for sym, gross, charges, _net in _PAPER_DAY_ROWS:
        insert_trade(
            trade_date="2026-08-05",
            symbol=sym,
            side="L",
            mode="live",
            pnl=gross,
            charges_inr=charges,
            fill="paper",
        )
    # A real row on a date no other test touches (the DB is shared across the
    # session). Gross 82.25 - 100.07 charges is a NET LOSS, which the pre-fix
    # gross aggregation reported as a +82 gain — the real 2026-07-23 OIL trade.
    insert_trade(
        trade_date="2026-07-23-net552",
        symbol="OIL",
        side="L",
        mode="sandbox",
        pnl=82.25,
        charges_inr=100.07,
    )

    assert paper_pnl_by_date()["2026-08-05"] == _PAPER_DAY_NET
    assert trades_pnl_by_date()["2026-07-23-net552"] == -17.82
    # paper still never reaches compound sizing, and the real row lands net
    assert round(total_realized_pnl() - before, 2) == -17.82
    db_session.remove()


def test_summarize_day_counts_liquidity_exclusions():
    """Stage 1 arrives as ONE event carrying a list of symbols; stage 2 as one event
    per symbol. The digest counts SYMBOLS, so the two stages stay comparable."""
    from services.open15_log_view import summarize_day

    day = [
        {"event": "armed"},
        {
            "event": "universe_excluded",
            "stage": 1,
            "symbols": [{"symbol": "AAA"}, {"symbol": "BBB"}, {"symbol": "CCC"}],
        },
        {"event": "universe_excluded", "stage": 2, "symbol": "DDD", "side": "long"},
        {"event": "summary", "day": "done"},
    ]
    d = summarize_day("2026-08-10", day)
    assert d["liq_excluded"] == 4
    assert d["liq_excluded_stage1"] == 3
    assert d["liq_excluded_stage2"] == 1


# --------------------------------------------------------------------------- #
# A clobbered log still has its journal (issue #612)
# --------------------------------------------------------------------------- #
_CLOBBERED = [{"ts": "14:33:09.391", "event": "skipped_late_boot", "armed_at": "14:33:09"}]
_CLOBBERED_JOURNAL = [
    {
        "symbol": "ASHOKLEY",
        "side": "L",
        "fill": "real",
        "watch_source": "rolling",
        "pnl": 2000.0,
        "charges_inr": 561.84,
        "entry_status": "success",
        "trigger_price": 179.9,
    },
    {
        "symbol": "ZYDUSLIFE",
        "side": "S",
        "fill": "shadow",
        "watch_source": "rolling",
        "pnl": 4455.0,
        "charges_inr": 570.81,
    },
]


def test_a_clobbered_log_still_renders_its_trades():
    """2026-08-13: a late-boot arm overwrote the log; the 8 trades survived.

    The page reported "0 filled / 0 sel" and "no selection this day" directly
    beside a real +Rs1438 — it asserted nothing happened AND showed the money.
    """
    import pytest

    from services.open15_log_view import selection_outcomes

    rows = {
        r["symbol"]: r
        for r in selection_outcomes("2026-08-13", _CLOBBERED, journal=_CLOBBERED_JOURNAL)
    }
    assert set(rows) == {"ASHOKLEY", "ZYDUSLIFE"}
    assert rows["ASHOKLEY"]["from_journal"] is True
    assert rows["ASHOKLEY"]["fill"] == "real"
    # NET, never gross (#552): 2000 - 561.84
    assert rows["ASHOKLEY"]["pnl"] == pytest.approx(1438.16)
    # the decision detail is genuinely gone — left NULL, never guessed
    assert rows["ASHOKLEY"]["gap_pct"] is None
    assert rows["ASHOKLEY"]["max_vol_ratio"] is None


def test_an_intact_log_is_unchanged_by_the_clobber_path():
    """The narrow condition: a stray journal symbol beside a REAL selection is
    still refused (the #557 invariant), because that case is anomalous rather
    than a lost timeline."""
    from services.open15_log_view import selection_outcomes

    day = [{"ts": "09:16", "event": "selection", "selected": {"HAL": "L"}, "gaps_pct": {}}]
    journal = [{"symbol": "STRAY", "pnl": 1.0, "charges_inr": 0.0, "fill": "real"}]
    rows = selection_outcomes("2026-08-06", day, journal=journal)
    assert [r["symbol"] for r in rows] == ["HAL"]


def test_js_and_python_agree_on_a_clobbered_day():
    """Both row builders must seed the same symbols — the parity test compares
    SETS, so one side seeding alone is a silent divergence."""
    from services.open15_log_view import selection_outcomes

    js = _run_render_sel(_CLOBBERED, journal=_CLOBBERED_JOURNAL)
    py = {
        r["symbol"]: r
        for r in selection_outcomes("2026-08-13", _CLOBBERED, journal=_CLOBBERED_JOURNAL)
    }
    assert set(js) == set(py)
