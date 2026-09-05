"""One-time Console tradebook import for a child account (issue #700).

The file is the only input. The tests pin: trades are matched to our mirror
rows by ``order_id`` (a family member's own trades are ignored), partials are
volume-weighted, a placed row on a covered day with no trade becomes a known
0, a reconcile-demoted row the file shows as traded is a CONFLICT (never
promoted), the touched days are written as ``console_csv`` finals, and
re-running the same file changes nothing.
"""

from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from database import account_orders_db, broker_accounts_db
from services import account_console_import as imp
from services import account_pnl_service as pnl_svc

IST = timedelta(hours=5, minutes=30)
STRAT = "open15_vol_breakout"
DAY = date(2026, 8, 28)


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    broker_accounts_db.init_db()
    account_orders_db.init_db()
    # never a broker call from the import path
    monkeypatch.setattr(pnl_svc, "_charges_module", lambda b: None)
    monkeypatch.setattr(pnl_svc, "_br_symbol", lambda b, s, e: s)
    monkeypatch.setattr(imp, "get_auth_token", lambda name: None)
    yield
    for model in (account_orders_db.AccountOrder, account_orders_db.AccountDailyPnl):
        account_orders_db.db_session.query(model).delete()
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    broker_accounts_db.db_session.query(broker_accounts_db.BrokerAccount).delete()
    broker_accounts_db.db_session.commit()
    broker_accounts_db.db_session.remove()


@pytest.fixture()
def child():
    return broker_accounts_db.add_account(
        display_name="Kid A",
        api_key="key_kid_a_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_a_0123456789",  # pragma: allowlist secret
        capital_inr=100000,
    )


def _mirror(account_id, symbol, action, qty, orderid, day: date, hh, mm, status="placed"):
    row = account_orders_db.record_mirror_attempt(
        account_id=account_id,
        strategy_name=STRAT,
        symbol=symbol,
        exchange="NFO",
        action=action,
        product="MIS",
        parent_qty=qty,
        child_qty=qty,
        status=status,
        broker_orderid=orderid,
    )
    when = datetime(day.year, day.month, day.day, hh, mm) - IST
    account_orders_db.db_session.query(account_orders_db.AccountOrder).filter(
        account_orders_db.AccountOrder.id == row["id"]
    ).update({"created_at": when})
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    return row


CSV_HEADER = (
    "symbol,isin,trade_date,exchange,segment,series,trade_type,auction,"
    "quantity,price,trade_id,order_id,order_execution_time\n"
)


def _csv(tmp_path: Path, lines: list[str]) -> Path:
    p = tmp_path / "Kid A_tradebook.csv"
    p.write_text(CSV_HEADER + "".join(lines), encoding="utf-8")
    return p


def _line(symbol, side, qty, price, trade_id, order_id, ts):
    return f"{symbol},,{DAY.isoformat()},NFO,FO,,{side},false,{qty},{price},{trade_id},{order_id},{ts}\n"


def test_dry_run_plans_and_writes_nothing(tmp_path, child, capsys):
    sym = "TIINDIA26SEP2800CE"
    _mirror(child["id"], sym, "BUY", 200, "O1", DAY, 9, 17)
    _mirror(child["id"], sym, "SELL", 200, "O2", DAY, 9, 30)
    path = _csv(
        tmp_path,
        [
            _line(sym, "buy", 200, 50.0, "T1", "O1", "2026-08-28T09:17:02"),
            _line(sym, "sell", 200, 55.0, "T2", "O2", "2026-08-28T09:30:01"),
            _line("HDFCBANK", "buy", 10, 1600.0, "T9", "MANUAL9", "2026-08-28T10:00:00"),
        ],
    )
    assert imp.main(["--account", str(child["id"]), "--file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out and "NOT ours (ignored)           : 1" in out
    rows = account_orders_db.list_orders(account_id=child["id"])
    assert all(r["fill_qty"] is None for r in rows)
    assert account_orders_db.list_daily_pnl(STRAT) == []


def test_apply_writes_fills_and_a_console_csv_day_row(tmp_path, child):
    sym = "TIINDIA26SEP2800CE"
    _mirror(child["id"], sym, "BUY", 200, "O1", DAY, 9, 17)
    _mirror(child["id"], sym, "SELL", 200, "O2", DAY, 9, 30)
    path = _csv(
        tmp_path,
        [
            # partial fills on the entry, volume-weighted → 51.0
            _line(sym, "buy", 100, 50.0, "T1", "O1", "2026-08-28T09:17:02"),
            _line(sym, "buy", 100, 52.0, "T2", "O1", "2026-08-28T09:17:04"),
            _line(sym, "sell", 200, 55.0, "T3", "O2", "2026-08-28T09:30:01"),
            _line("HDFCBANK", "buy", 10, 1600.0, "T9", "MANUAL9", "2026-08-28T10:00:00"),
        ],
    )
    assert imp.main(["--account", str(child["id"]), "--file", str(path), "--apply"]) == 0
    by_oid = {r["broker_orderid"]: r for r in account_orders_db.list_orders(account_id=child["id"])}
    assert by_oid["O1"]["fill_price"] == 51.0 and by_oid["O1"]["fill_qty"] == 200
    assert by_oid["O2"]["fill_price"] == 55.0
    (row,) = account_orders_db.list_daily_pnl(STRAT, account_id=child["id"])
    assert row["trade_date"] == DAY.isoformat()
    assert row["realized_gross"] == pytest.approx(200 * (55.0 - 51.0))
    assert row["capture_source"] == "console_csv" and row["finalized"] is True
    assert row["charges_source"] == "modelled" and row["charges_inr"] > 0
    assert row["realized_net"] == pytest.approx(800.0 - row["charges_inr"], abs=0.01)


def test_placed_row_absent_from_file_on_a_covered_day_is_known_unfilled(tmp_path, child):
    sym = "X26SEP100CE"
    _mirror(child["id"], sym, "BUY", 100, "O1", DAY, 9, 17)
    _mirror(child["id"], sym, "SELL", 100, "O2", DAY, 9, 30)  # never traded per Console
    path = _csv(tmp_path, [_line(sym, "buy", 100, 10.0, "T1", "O1", "2026-08-28T09:17:02")])
    imp.main(["--account", str(child["id"]), "--file", str(path), "--apply"])
    by_oid = {r["broker_orderid"]: r for r in account_orders_db.list_orders(account_id=child["id"])}
    assert by_oid["O2"]["fill_qty"] == 0
    (row,) = account_orders_db.list_daily_pnl(STRAT, account_id=child["id"])
    assert row["n_open_legs"] == 1 and row["n_round_trips"] == 0


def test_demoted_row_the_file_shows_as_traded_is_a_conflict_not_a_promotion(
    tmp_path, child, capsys
):
    sym = "X26SEP100CE"
    row = _mirror(child["id"], sym, "BUY", 100, "O1", DAY, 9, 17)
    account_orders_db.update_status(row["id"], status="rejected", error_text="RMS")
    path = _csv(tmp_path, [_line(sym, "buy", 100, 10.0, "T1", "O1", "2026-08-28T09:17:02")])
    imp.main(["--account", str(child["id"]), "--file", str(path), "--apply"])
    out = capsys.readouterr().out
    assert "CONFLICTS" in out
    stored = account_orders_db.list_orders(account_id=child["id"])[0]
    assert stored["status"] == "rejected" and stored["fill_qty"] is None
    assert account_orders_db.list_daily_pnl(STRAT) == []


def test_reimport_is_idempotent(tmp_path, child):
    sym = "X26SEP100CE"
    _mirror(child["id"], sym, "BUY", 100, "O1", DAY, 9, 17)
    _mirror(child["id"], sym, "SELL", 100, "O2", DAY, 9, 30)
    path = _csv(
        tmp_path,
        [
            _line(sym, "buy", 100, 10.0, "T1", "O1", "2026-08-28T09:17:02"),
            _line(sym, "sell", 100, 12.0, "T2", "O2", "2026-08-28T09:30:01"),
        ],
    )
    imp.main(["--account", str(child["id"]), "--file", str(path), "--apply"])
    first = account_orders_db.list_daily_pnl(STRAT, account_id=child["id"])
    imp.main(["--account", str(child["id"]), "--file", str(path), "--apply"])
    second = account_orders_db.list_daily_pnl(STRAT, account_id=child["id"])
    assert len(second) == 1
    assert second[0]["realized_net"] == first[0]["realized_net"]


def test_unknown_account_or_missing_file_exit_2(tmp_path, child):
    assert imp.main(["--account", "9999", "--file", "x.csv"]) == 2
    assert imp.main(["--account", str(child["id"]), "--file", str(tmp_path / "nope.csv")]) == 2


def test_header_variants_and_bad_lines_are_tolerated(tmp_path):
    p = tmp_path / "t.csv"
    p.write_text(
        "Symbol,Trade Date,Trade Type,Quantity,Price,Order ID\n"
        "ABC,2026-08-28,buy,10,5.5,O1\n"
        "ABC,2026-08-28,buy,,5.5,O1\n",
        encoding="utf-8",
    )
    by_order, problems = imp.normalise_trades(imp.read_export(p))
    assert list(by_order) == ["O1"] and by_order["O1"][0]["quantity"] == 10
    assert len(problems) == 1
