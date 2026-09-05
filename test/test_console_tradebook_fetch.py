"""Headless Console tradebook fetch (issue #702) — browser stubbed at the seam.

Pins: the market-hours refusal (a web login can kill the child's live API
session), fail-loud on a Console shape change (no file written), the CSV
contract the #700 importer reads, and the fetch → import hand-off.
"""

from datetime import date
from pathlib import Path

import pytest

from database import account_orders_db, broker_accounts_db
from services import console_tradebook_fetch as fetch


@pytest.fixture(autouse=True)
def _tables(monkeypatch):
    broker_accounts_db.init_db()
    account_orders_db.init_db()
    monkeypatch.setattr(fetch, "in_market_hours_now", lambda: False)
    monkeypatch.setattr(fetch, "probe_child_api_token", lambda account_id: True)
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
    acct = broker_accounts_db.add_account(
        display_name="Kid A",
        api_key="key_kid_a_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_a_0123456789",  # pragma: allowlist secret
        capital_inr=100000,
        broker_client_id="AB1234",
    )
    broker_accounts_db.update_account(
        acct["id"],
        password="pw-not-real",  # pragma: allowlist secret
        totp_secret="JBSWY3DPEHPK3PXP",  # pragma: allowlist secret
    )
    return acct


ROW = {
    "symbol": "TIINDIA26SEP2800CE",
    "isin": "",
    "trade_date": "2026-08-28",
    "exchange": "NFO",
    "segment": "FO",
    "series": "",
    "trade_type": "buy",
    "auction": False,
    "quantity": 200,
    "price": 50.0,
    "trade_id": "T1",
    "order_id": "O1",
    "order_execution_time": "2026-08-28T09:17:02",
}


def test_envelope_extraction_handles_consoles_shapes():
    rows, pag, err = fetch._extract_result(
        {"status": "success", "data": {"result": [ROW], "pagination": {"total_pages": 1}}}
    )
    assert rows == [ROW] and pag == {"total_pages": 1} and err is None
    rows, _, err = fetch._extract_result({"status": "success", "data": [ROW]})
    assert rows == [ROW] and err is None
    _, _, err = fetch._extract_result({"status": "error", "message": "Invalid session"})
    assert "Invalid session" in err
    _, _, err = fetch._extract_result({"status": "success", "data": {"state": "pending"}})
    assert "not ready" in err


def test_shape_change_is_loud_and_names_the_keys():
    bad = {**ROW}
    del bad["order_id"]
    msg = fetch.validate_rows([ROW, bad])
    assert "row 1 lacks ['order_id']" in msg and "keys seen" in msg
    assert fetch.validate_rows([ROW]) is None


def test_csv_matches_the_importer_contract(tmp_path):
    p = fetch.write_console_csv([{**ROW, "extra_key": "dropped"}], tmp_path / "x.csv")
    text = p.read_text(encoding="utf-8").splitlines()
    assert text[0] == ",".join(fetch.CSV_COLUMNS)
    assert "extra_key" not in text[0]
    from services.account_console_import import normalise_trades, read_export

    by_order, problems = normalise_trades(read_export(p))
    assert list(by_order) == ["O1"] and problems == []


def test_market_hours_refusal(monkeypatch, child, capsys):
    monkeypatch.setattr(fetch, "in_market_hours_now", lambda: True)
    rc = fetch.main(["--account", str(child["id"]), "--from", "2026-08-26", "--to", "2026-09-04"])
    assert rc == 3
    assert "Refusing inside market hours" in capsys.readouterr().err


def test_fetch_failure_writes_no_file_and_exits_1(monkeypatch, child, tmp_path, capsys):
    monkeypatch.setattr(
        fetch, "fetch_console_tradebook", lambda *a, **k: (None, "Kite login error: Invalid TOTP")
    )
    out = tmp_path / "kid.csv"
    rc = fetch.main(
        [
            "--account",
            str(child["id"]),
            "--from",
            "2026-08-26",
            "--to",
            "2026-09-04",
            "--out",
            str(out),
        ]
    )
    assert rc == 1 and not out.exists()
    assert "Invalid TOTP" in capsys.readouterr().err


def test_missing_stored_credentials_is_explained(child, capsys):
    broker_accounts_db.update_account(child["id"], broker_client_id="")
    path, err, _ = fetch.fetch_for_account(child["id"], date(2026, 8, 26), date(2026, 9, 4))
    assert path is None and "no stored Kite user-id" in err


def test_fetch_then_import_hands_the_file_to_the_importer(monkeypatch, child, tmp_path):
    # a mirror row the fetched trade matches
    row = account_orders_db.record_mirror_attempt(
        account_id=child["id"],
        strategy_name="open15_vol_breakout",
        symbol="TIINDIA26SEP2800CE",
        exchange="NFO",
        action="BUY",
        product="MIS",
        parent_qty=200,
        child_qty=200,
        status="placed",
        broker_orderid="O1",
    )
    assert row
    captured = {}

    def fake_fetch(user_id, password, totp_secret, d_from, d_to, segment="FO"):
        captured.update(user_id=user_id, segment=segment, d_from=d_from, d_to=d_to)
        return [
            ROW,
            {
                **ROW,
                "trade_type": "sell",
                "price": 55.0,
                "trade_id": "T2",
                "order_id": "O2",
                "order_execution_time": "2026-08-28T09:30:01",
            },
        ], None

    monkeypatch.setattr(fetch, "fetch_console_tradebook", fake_fetch)
    out = tmp_path / "kid.csv"
    rc = fetch.main(
        [
            "--account",
            str(child["id"]),
            "--from",
            "2026-08-26",
            "--to",
            "2026-09-04",
            "--out",
            str(out),
            "--import",
        ]
    )
    assert rc == 0 and out.exists()
    assert captured["user_id"] == "AB1234" and captured["segment"] == "FO"
    # dry-run import: nothing written yet
    assert account_orders_db.list_orders(account_id=child["id"])[0]["fill_qty"] is None
    rc = fetch.main(
        [
            "--account",
            str(child["id"]),
            "--from",
            "2026-08-26",
            "--to",
            "2026-09-04",
            "--out",
            str(out),
            "--import",
            "--apply",
        ]
    )
    assert rc == 0
    assert account_orders_db.list_orders(account_id=child["id"])[0]["fill_price"] == 50.0


def test_default_output_path_is_gitignored_imports_dir():
    p = fetch.default_output_path("Swapna-zerodha", "FO", date(2026, 8, 26), date(2026, 9, 4))
    assert p.parts[0] == "imports" and p.parts[1] == "console"
    assert p.name == "Swapna-zerodha_tradebook_FO_2026-08-26_2026-09-04.csv"
