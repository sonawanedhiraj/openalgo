"""Tests for the one-off #624 replay-repair CLI.

It writes to the trade journal, so the properties that matter are: it finds exactly the rows
the bug corrupted, a dry run writes nothing, an apply writes the FILL prices (not a guess),
and a row whose fills cannot be read is left alone rather than half-repaired.
"""

import datetime as dt

import services.intraday_pullback_replay_repair as repair_mod

D = dt.date(2026, 1, 22)


def _journal():
    from database import intraday_pullback_db as journal

    journal.init_db()
    return journal


def _corrupt_row(symbol="ZZZ"):
    """A row shaped exactly like the 2026-08-17 incident: SL exit stamped before the entry."""
    journal = _journal()
    tid = journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="S",
        symbol=symbol,
        trade_date=D.isoformat(),
        quantity=45,
        entry_time=dt.datetime.combine(D, dt.time(10, 25)),
        entry_price=3301.9,
        stop_price=3314.0,
        entry_order_id="E1",
        status="open",
        gate={},
    )
    journal.close_trade(
        tid,
        exit_time=dt.datetime.combine(D, dt.time(9, 15)),  # before the entry — the signature
        exit_price=3314.0,
        exit_reason="SL",
        gross_pnl=-544.5,
        charges_inr=101.29,
        net_pnl=-645.79,
        exit_order_id="X1",
        status="closed",
    )
    return tid


def _healthy_row(symbol="YYY"):
    journal = _journal()
    tid = journal.record_entry(
        strategy_id=None,
        mode="sandbox",
        side="L",
        symbol=symbol,
        trade_date=D.isoformat(),
        quantity=10,
        entry_time=dt.datetime.combine(D, dt.time(10, 25)),
        entry_price=100.0,
        stop_price=99.0,
        entry_order_id="E2",
        status="open",
        gate={},
    )
    journal.close_trade(
        tid,
        exit_time=dt.datetime.combine(D, dt.time(11, 0)),
        exit_price=101.0,
        exit_reason="EOD",
        gross_pnl=10.0,
        charges_inr=1.0,
        net_pnl=9.0,
        exit_order_id="X2",
        status="closed",
    )
    return tid


def _row(tid):
    from database.intraday_pullback_db import IntradayPullbackTrade
    from database.intraday_pullback_db import db_session as session

    try:
        return session.query(IntradayPullbackTrade).get(tid)
    finally:
        session.remove()


def _fills(monkeypatch, mapping):
    monkeypatch.setattr(
        "database.auth_db.get_first_available_api_key", lambda *a, **k: "KEY", raising=False
    )
    monkeypatch.setattr(repair_mod, "fetch_fill", lambda oid, key: mapping.get(oid))


def test_recompute_uses_the_services_own_charge_formula():
    from services.intraday_pullback_service import _charges

    gross, ch, net = repair_mod.recompute("S", 45, 3305.9, 3305.5)
    assert gross == 18.0  # short: (entry - exit) * qty
    assert ch == round(_charges(3305.5 * 45, 3305.9 * 45), 2)
    assert net == round(18.0 - ch, 2)


def test_finds_only_rows_whose_exit_precedes_their_entry(monkeypatch):
    bad = _corrupt_row("AAB")
    good = _healthy_row("AAC")
    found = {r["id"] for r in repair_mod.find_corrupt_rows(trade_date=D.isoformat())}
    assert bad in found and good not in found


def test_dry_run_reports_but_writes_nothing(monkeypatch):
    tid = _corrupt_row("AAD")
    _fills(
        monkeypatch,
        {
            "E1": {"price": 3305.9, "ts": dt.datetime.combine(D, dt.time(10, 35, 1))},
            "X1": {"price": 3305.5, "ts": dt.datetime.combine(D, dt.time(11, 45, 2))},
        },
    )
    out = repair_mod.repair(trade_date=D.isoformat(), apply=False)

    assert out["status"] == "success" and out["applied"] is False
    assert any(r["id"] == tid and r["action"] == "repair" for r in out["rows"])
    row = _row(tid)
    assert row.entry_price == 3301.9 and row.exit_price == 3314.0  # untouched
    assert row.exit_time < row.entry_time
    assert row.note is None


def test_apply_writes_the_fill_prices_times_and_pnl(monkeypatch):
    tid = _corrupt_row("AAE")
    _fills(
        monkeypatch,
        {
            "E1": {"price": 3305.9, "ts": dt.datetime.combine(D, dt.time(10, 35, 1))},
            "X1": {"price": 3305.5, "ts": dt.datetime.combine(D, dt.time(11, 45, 2))},
        },
    )
    repair_mod.repair(trade_date=D.isoformat(), apply=True)

    row = _row(tid)
    assert row.entry_price == 3305.9 and row.exit_price == 3305.5
    assert row.entry_time == dt.datetime.combine(D, dt.time(10, 35, 1))
    assert row.exit_time == dt.datetime.combine(D, dt.time(11, 45, 2))
    assert row.exit_time > row.entry_time  # the invariant the bug violated
    gross, ch, net = repair_mod.recompute("S", 45, 3305.9, 3305.5)
    assert (row.gross_pnl, row.charges_inr, row.net_pnl) == (gross, ch, net)
    assert row.note == repair_mod._NOTE  # the incident stays visible in the record

    # idempotent: the repaired row no longer matches the corruption signature
    assert tid not in {r["id"] for r in repair_mod.find_corrupt_rows(trade_date=D.isoformat())}


def test_unreadable_fill_leaves_the_row_completely_alone(monkeypatch):
    tid = _corrupt_row("AAF")
    # entry readable, exit not -> must NOT half-repair
    _fills(
        monkeypatch,
        {"E1": {"price": 3305.9, "ts": dt.datetime.combine(D, dt.time(10, 35, 1))}, "X1": None},
    )
    out = repair_mod.repair(trade_date=D.isoformat(), apply=True)

    assert any(r["id"] == tid and r["action"] == "skipped_unreadable_fills" for r in out["rows"])
    row = _row(tid)
    assert row.entry_price == 3301.9 and row.exit_price == 3314.0 and row.net_pnl == -645.79
    assert row.note is None


def test_no_api_key_refuses_rather_than_guessing(monkeypatch):
    tid = _corrupt_row("AAG")
    monkeypatch.setattr(
        "database.auth_db.get_first_available_api_key", lambda *a, **k: None, raising=False
    )
    out = repair_mod.repair(trade_date=D.isoformat(), apply=True)

    assert out["status"] == "error"
    assert _row(tid).entry_price == 3301.9
