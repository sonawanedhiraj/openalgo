"""Child-account open15 verification surface (issue #663).

Covers the three requirements of the /accounts "Child trades — open15" card:
trades taken (journal read), open-position check against the exit time (child's
OWN broker book, flat vs unreadable kept distinct), and the manual square-off's
safety rules — refuse on flat/unreadable book, place the BOOK's quantity,
journal every outcome.

DB isolation comes from test/conftest.py's global redirect; broker I/O is
stubbed at the module seams (`_broker_module`, `get_auth_token`) — never a real
broker call.
"""

from types import SimpleNamespace

import pytest

from database import account_orders_db, broker_accounts_db
from services import account_open15_service as svc


@pytest.fixture(autouse=True)
def _tables():
    broker_accounts_db.init_db()
    account_orders_db.init_db()
    yield
    # Remove rows this test created so cases stay independent.
    account_orders_db.db_session.query(account_orders_db.AccountOrder).delete()
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    broker_accounts_db.db_session.query(broker_accounts_db.BrokerAccount).delete()
    broker_accounts_db.db_session.commit()
    broker_accounts_db.db_session.remove()


@pytest.fixture()
def child():
    account = broker_accounts_db.add_account(
        display_name="Kid A",
        api_key="key_kid_a_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_a_0123456789",  # pragma: allowlist secret
        capital_inr=100000,
    )
    return account


class FakeBrokerModule:
    """Stands in for broker.zerodha.api.order_api."""

    def __init__(self, positions_payload, place_status=200, order_id="ORD1"):
        self.positions_payload = positions_payload
        self.place_status = place_status
        self.order_id = order_id
        self.placed_orders = []

    def get_positions(self, auth):
        return self.positions_payload

    def place_order_api(self, data, auth):
        self.placed_orders.append(dict(data))
        if self.place_status == 200:
            return SimpleNamespace(status=200), {"status": "success"}, self.order_id
        return SimpleNamespace(status=400), {"message": "RMS rejected"}, None


def _wire(monkeypatch, module, token="key:token"):
    monkeypatch.setattr(svc, "_broker_module", lambda broker: module)
    monkeypatch.setattr(svc, "get_auth_token", lambda name: token)
    # Symbol mapping is identity in tests — matching logic is what's under test.
    monkeypatch.setattr(svc, "_br_symbol", lambda broker, symbol, exchange: symbol)


def _book(*positions):
    return {"status": "success", "data": {"net": list(positions)}}


def _pos(symbol, qty, pnl, exchange="NSE", product="MIS"):
    return {
        "tradingsymbol": symbol,
        "exchange": exchange,
        "product": product,
        "quantity": qty,
        "pnl": pnl,
    }


def _journal_trade(account_id, symbol="TIINDIA", status="placed", action="BUY", qty=10):
    return account_orders_db.record_mirror_attempt(
        account_id=account_id,
        strategy_name=svc.STRATEGY_NAME,
        symbol=symbol,
        exchange="NSE",
        action=action,
        product="MIS",
        parent_qty=qty,
        child_qty=qty if status == "placed" else 0,
        status=status,
        broker_orderid="B1" if status == "placed" else None,
    )


# ---------------------------------------------------------------------------
# effective_exit_time
# ---------------------------------------------------------------------------


def test_exit_time_defaults_to_0930(monkeypatch):
    monkeypatch.delenv("OPEN15_EXIT_TIME", raising=False)
    monkeypatch.setattr("database.open15_breakout_db.get_config", lambda: None)
    assert svc.effective_exit_time() == "09:30"


def test_exit_time_prefers_stored_config(monkeypatch):
    monkeypatch.setattr("database.open15_breakout_db.get_config", lambda: {"exit_time": "09:45"})
    assert svc.effective_exit_time() == "09:45"


def test_exit_time_malformed_config_falls_back(monkeypatch):
    monkeypatch.delenv("OPEN15_EXIT_TIME", raising=False)
    monkeypatch.setattr("database.open15_breakout_db.get_config", lambda: {"exit_time": "bogus"})
    assert svc.effective_exit_time() == "09:30"


# ---------------------------------------------------------------------------
# open15_status — trades, positions, P&L
# ---------------------------------------------------------------------------


def test_status_reports_trades_positions_and_pnl(monkeypatch, child):
    _journal_trade(child["id"], "TIINDIA", status="placed")
    _journal_trade(child["id"], "SAIL", status="rejected")
    module = FakeBrokerModule(_book(_pos("TIINDIA", 0, 1210.5), _pos("OTHER", 5, -100.0)))
    _wire(monkeypatch, module)

    payload = svc.open15_status()
    account = next(a for a in payload["accounts"] if a["account_id"] == child["id"])

    assert len(account["trades"]) == 2
    assert account["positions_readable"] is True
    # Only the PLACED trade's symbol is position-checked; it reads flat.
    assert account["positions"] == [
        {"symbol": "TIINDIA", "exchange": "NSE", "product": "MIS", "open_qty": 0, "pnl": 1210.5}
    ]
    assert account["open_after_exit"] is False
    # Day P&L spans the whole child book (requirement 3).
    assert account["day_pnl"] == pytest.approx(1110.5)


def test_status_flags_open_position_after_exit_time(monkeypatch, child):
    _journal_trade(child["id"], "TIINDIA", status="placed")
    module = FakeBrokerModule(_book(_pos("TIINDIA", 10, -388.0)))
    _wire(monkeypatch, module)
    # Force "past the exit time" regardless of wall clock.
    monkeypatch.setattr(
        svc, "_now_ist", lambda: __import__("datetime").datetime(2026, 8, 24, 10, 0)
    )

    payload = svc.open15_status()
    account = next(a for a in payload["accounts"] if a["account_id"] == child["id"])
    assert payload["after_exit_time"] is True
    assert account["positions"][0]["open_qty"] == 10
    assert account["open_after_exit"] is True


def test_status_unreadable_book_is_not_flat(monkeypatch, child):
    """Flat and unreadable are different answers — the card must not show a
    green 'all squared off' on a book it could not read."""
    _journal_trade(child["id"], "TIINDIA", status="placed")
    module = FakeBrokerModule({"status": "error", "message": "down"})
    _wire(monkeypatch, module)

    payload = svc.open15_status()
    account = next(a for a in payload["accounts"] if a["account_id"] == child["id"])
    assert account["positions_readable"] is False
    assert account["positions"] == []
    assert account["day_pnl"] is None


def test_status_no_session_reports_unreadable(monkeypatch, child):
    _journal_trade(child["id"], "TIINDIA", status="placed")
    module = FakeBrokerModule(_book())
    _wire(monkeypatch, module, token=None)

    payload = svc.open15_status()
    account = next(a for a in payload["accounts"] if a["account_id"] == child["id"])
    assert account["positions_readable"] is False
    assert account["day_pnl"] is None


def test_status_one_broken_child_does_not_blank_others(monkeypatch, child):
    other = broker_accounts_db.add_account(
        display_name="Kid B",
        api_key="key_kid_b_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_b_0123456789",  # pragma: allowlist secret
        capital_inr=50000,
    )
    module = FakeBrokerModule(_book())
    _wire(monkeypatch, module)

    original = svc._account_status

    def exploding(account, date_utc, after_exit):
        if account["id"] == child["id"]:
            raise RuntimeError("boom")
        return original(account, date_utc, after_exit)

    monkeypatch.setattr(svc, "_account_status", exploding)
    payload = svc.open15_status()
    ids = {a["account_id"] for a in payload["accounts"]}
    assert {child["id"], other["id"]} <= ids
    broken = next(a for a in payload["accounts"] if a["account_id"] == child["id"])
    assert broken["positions_readable"] is False


# ---------------------------------------------------------------------------
# square_off — safety rules
# ---------------------------------------------------------------------------


def test_squareoff_refuses_unknown_account(monkeypatch):
    ok, payload = svc.square_off(99999, "TIINDIA", "NSE", "MIS")
    assert ok is False
    assert payload["reason"] == "unknown_account"


def test_squareoff_refuses_without_session(monkeypatch, child):
    module = FakeBrokerModule(_book(_pos("TIINDIA", 10, 0)))
    _wire(monkeypatch, module, token=None)
    ok, payload = svc.square_off(child["id"], "TIINDIA", "NSE", "MIS")
    assert ok is False
    assert payload["reason"] == "no_session"
    assert module.placed_orders == []


def test_squareoff_refuses_on_unreadable_book(monkeypatch, child):
    module = FakeBrokerModule({"status": "error"})
    _wire(monkeypatch, module)
    ok, payload = svc.square_off(child["id"], "TIINDIA", "NSE", "MIS")
    assert ok is False
    assert payload["reason"] == "book_unreadable"
    assert module.placed_orders == []


def test_squareoff_refuses_on_flat_book(monkeypatch, child):
    module = FakeBrokerModule(_book(_pos("TIINDIA", 0, 0)))
    _wire(monkeypatch, module)
    ok, payload = svc.square_off(child["id"], "TIINDIA", "NSE", "MIS")
    assert ok is False
    assert payload["reason"] == "no_position"
    assert module.placed_orders == []


def test_squareoff_places_market_opposite_of_book_qty(monkeypatch, child):
    """The BOOK's quantity is what's flattened — never a journal number."""
    _journal_trade(child["id"], "TIINDIA", status="placed", qty=99)
    module = FakeBrokerModule(_book(_pos("TIINDIA", 11, -388.0)))
    _wire(monkeypatch, module)

    ok, payload = svc.square_off(child["id"], "TIINDIA", "NSE", "MIS")
    assert ok is True
    assert payload["broker_orderid"] == "ORD1"
    order = module.placed_orders[0]
    assert order["action"] == "SELL"
    assert order["quantity"] == 11  # book qty, not the journal's 99
    assert order["pricetype"] == "MARKET"

    rows = account_orders_db.list_orders(account_id=child["id"])
    squareoff_rows = [r for r in rows if r["parent_orderid"] == "manual_squareoff"]
    assert len(squareoff_rows) == 1
    assert squareoff_rows[0]["status"] == "placed"
    assert squareoff_rows[0]["child_qty"] == 11


def test_squareoff_short_position_buys_back(monkeypatch, child):
    module = FakeBrokerModule(_book(_pos("SAIL", -50, 120.0)))
    _wire(monkeypatch, module)
    ok, payload = svc.square_off(child["id"], "SAIL", "NSE", "MIS")
    assert ok is True
    order = module.placed_orders[0]
    assert order["action"] == "BUY"
    assert order["quantity"] == 50


def test_squareoff_broker_rejection_is_journaled(monkeypatch, child):
    module = FakeBrokerModule(_book(_pos("TIINDIA", 11, 0)), place_status=400)
    _wire(monkeypatch, module)
    ok, payload = svc.square_off(child["id"], "TIINDIA", "NSE", "MIS")
    assert ok is False
    assert payload["reason"] == "rejected"
    rows = account_orders_db.list_orders(account_id=child["id"])
    assert rows[0]["status"] == "rejected"
    assert "RMS rejected" in rows[0]["error_text"]
