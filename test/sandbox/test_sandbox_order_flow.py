# test/sandbox/test_sandbox_order_flow.py
"""
P0-T2: Sandbox order placement — margin, fill, position, and MIS square-off.

Covers (issue #94):
  T1 — BUY MARKET → SandboxOrders 'complete', SandboxTrades row, margin held
  T2 — Insufficient margin → 400 error, NO accepted SandboxOrders row written
  T3 — BUY then SELL closes position, P&L computed correctly
  T4 — MIS BUY position present; SquareOffManager at 15:16 IST flattens it

Quote provider is mocked via ``prefetched_quote`` (T1-T3) or
``ExecutionEngine._fetch_quote`` patch (T4) — no broker or historify needed.
The sandbox DB is redirected to a temp dir by test/conftest.py (already active
for every run in the suite).

Refs #94  Closes (not yet — tracked in the PR body).
"""

from __future__ import annotations

import datetime as dt_real
from decimal import Decimal
from unittest.mock import patch

import pytest
import pytz

_IST = pytz.timezone("Asia/Kolkata")

# ──────────────────────────────────────────────────────────────────────────────
# Shared quote stubs
# ──────────────────────────────────────────────────────────────────────────────
_QUOTE_2800 = {"ltp": 2800.0, "bid": 2799.5, "ask": 2800.5}
_QUOTE_2810 = {"ltp": 2810.0, "bid": 2809.5, "ask": 2810.5}

# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session", autouse=True)
def insert_test_symbol():
    """Insert RELIANCE/NSE into the temp symtoken table (session-wide).

    ``get_symbol_info`` checks the in-memory cache first; since no master
    contract is loaded in the test process the cache is empty and every
    lookup falls through to the DB — so a plain INSERT is sufficient.
    """
    from database.symbol import Base, SymToken, db_session

    existing = SymToken.query.filter_by(symbol="RELIANCE", exchange="NSE").first()
    if not existing:
        sym = SymToken(
            symbol="RELIANCE",
            brsymbol="RELIANCE",
            name="RELIANCE INDUSTRIES",
            exchange="NSE",
            brexchange="NSE",
            token="2885",
            lotsize=1,
            instrumenttype="EQ",
            tick_size="0.05",
        )
        db_session.add(sym)
        db_session.commit()
    yield


@pytest.fixture
def fresh_user():
    """One isolated test user per test: API key + ₹1 Cr funds + cleanup."""
    from database.auth_db import upsert_api_key
    from database.sandbox_db import (
        SandboxFunds,
        SandboxOrders,
        SandboxPositions,
        SandboxTrades,
        db_session,
    )
    from sandbox.fund_manager import initialize_user_funds

    uid = "p0_t2_flow_user"
    key = "test_p0_t2_api_key_flow_abc123"

    # Clean slate
    for model in (SandboxOrders, SandboxPositions, SandboxTrades, SandboxFunds):
        model.query.filter_by(user_id=uid).delete()
    db_session.commit()

    upsert_api_key(uid, key)
    initialize_user_funds(uid)

    yield {"user_id": uid, "api_key": key}

    # Teardown
    for model in (SandboxOrders, SandboxPositions, SandboxTrades, SandboxFunds):
        model.query.filter_by(user_id=uid).delete()
    db_session.commit()


# ──────────────────────────────────────────────────────────────────────────────
# T1 — BUY MARKET → order complete + trade row + margin held
# ──────────────────────────────────────────────────────────────────────────────


def test_buy_market_creates_order_trade_and_holds_margin(fresh_user):
    """Sandbox BUY MARKET order with prefetched quote:
    - SandboxOrders row written with status='complete'
    - SandboxTrades row filled at ask price
    - Margin is held in SandboxFunds (used_margin > 0)
    """
    from database.sandbox_db import SandboxOrders, SandboxPositions, SandboxTrades
    from sandbox.fund_manager import get_user_funds
    from services.sandbox_service import sandbox_place_order

    uid = fresh_user["user_id"]
    key = fresh_user["api_key"]

    order_data = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 100,
        "pricetype": "MARKET",
        "product": "CNC",
        "strategy": "test_t1",
    }

    success, response, status_code = sandbox_place_order(
        order_data, key, {}, prefetched_quote=_QUOTE_2800
    )

    assert success is True, f"Expected success but got: {response}"
    assert status_code == 200
    assert "orderid" in response
    orderid = response["orderid"]

    # SandboxOrders: row must exist and be complete (MARKET executes immediately)
    order_row = SandboxOrders.query.filter_by(orderid=orderid).first()
    assert order_row is not None, "No SandboxOrders row written"
    assert order_row.order_status == "complete", (
        f"Expected 'complete', got '{order_row.order_status}'"
    )
    assert order_row.symbol == "RELIANCE"
    assert order_row.action == "BUY"
    assert order_row.quantity == 100

    # SandboxTrades: fill row created at ask price (BUY fills at ask)
    trade_row = SandboxTrades.query.filter_by(orderid=orderid).first()
    assert trade_row is not None, "No SandboxTrades row for the fill"
    assert float(trade_row.price) == pytest.approx(2800.5, abs=0.01), (
        f"Expected fill at ask 2800.5, got {trade_row.price}"
    )
    assert trade_row.action == "BUY"
    assert trade_row.quantity == 100

    # SandboxPositions: position opened
    pos = SandboxPositions.query.filter_by(
        user_id=uid, symbol="RELIANCE", exchange="NSE", product="CNC"
    ).first()
    assert pos is not None, "No SandboxPositions row after BUY"
    assert pos.quantity == 100

    # Funds: CNC 1× leverage → ₹280,000 margin blocked (100 × 2800 / 1)
    funds = get_user_funds(uid)
    assert funds["utiliseddebits"] == pytest.approx(280_000.0, rel=1e-3), (
        f"Expected ~₹280,000 margin held, got {funds['utiliseddebits']}"
    )
    assert funds["availablecash"] == pytest.approx(10_000_000.0 - 280_000.0, rel=1e-3)


# ──────────────────────────────────────────────────────────────────────────────
# T2 — Insufficient margin → 400, no accepted SandboxOrders row
# ──────────────────────────────────────────────────────────────────────────────


def test_buy_market_insufficient_margin_rejected(fresh_user):
    """A BUY that exceeds available capital must be rejected with 400 and must
    NOT write an accepted SandboxOrders row (no 'open' or 'complete' row).
    """
    from database.sandbox_db import SandboxOrders
    from services.sandbox_service import sandbox_place_order

    key = fresh_user["api_key"]

    # 5,000 × ₹2,800 CNC (1×) = ₹14,000,000 > ₹10,000,000 starting capital
    order_data = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        "action": "BUY",
        "quantity": 5000,
        "pricetype": "MARKET",
        "product": "CNC",
        "strategy": "test_t2",
    }

    success, response, status_code = sandbox_place_order(
        order_data, key, {}, prefetched_quote=_QUOTE_2800
    )

    assert success is False, "Expected failure for insufficient margin"
    assert status_code == 400
    assert "error" in response.get("status", "")

    # No accepted order row must exist — margin check fires BEFORE the INSERT
    accepted = SandboxOrders.query.filter(
        SandboxOrders.order_status.in_(["open", "complete"])
    ).all()
    assert len(accepted) == 0, f"Expected 0 accepted rows but found {len(accepted)}: " + str(
        [o.orderid for o in accepted]
    )


# ──────────────────────────────────────────────────────────────────────────────
# T3 — BUY then SELL closes position with correct P&L
# ──────────────────────────────────────────────────────────────────────────────


def test_buy_then_sell_closes_position_with_pnl(fresh_user):
    """Full round-trip: BUY 100 RELIANCE CNC at ₹2800.5, then SELL 100 at ₹2799.5.
    After the SELL: position quantity=0, realized P&L = (2799.5 − 2800.5) × 100 = −₹100.
    """
    from database.sandbox_db import SandboxOrders, SandboxPositions, SandboxTrades
    from sandbox.fund_manager import get_user_funds
    from services.sandbox_service import sandbox_place_order

    uid = fresh_user["user_id"]
    key = fresh_user["api_key"]

    base = {"symbol": "RELIANCE", "exchange": "NSE", "pricetype": "MARKET", "product": "CNC"}

    # --- BUY ---
    buy_ok, buy_resp, buy_code = sandbox_place_order(
        {**base, "action": "BUY", "quantity": 100, "strategy": "test_t3_buy"},
        key,
        {},
        prefetched_quote=_QUOTE_2800,
    )
    assert buy_ok is True, f"BUY failed: {buy_resp}"
    assert buy_code == 200

    pos_after_buy = SandboxPositions.query.filter_by(
        user_id=uid, symbol="RELIANCE", exchange="NSE", product="CNC"
    ).first()
    assert pos_after_buy is not None
    assert pos_after_buy.quantity == 100

    funds_after_buy = get_user_funds(uid)
    assert funds_after_buy["utiliseddebits"] > 0, "Expected margin held after BUY"

    # --- SELL (close the position) ---
    # BUY average_price is 2800.5 (ask). SELL fills at bid=2799.5.
    sell_ok, sell_resp, sell_code = sandbox_place_order(
        {**base, "action": "SELL", "quantity": 100, "strategy": "test_t3_sell"},
        key,
        {},
        prefetched_quote=_QUOTE_2800,
    )
    assert sell_ok is True, f"SELL failed: {sell_resp}"
    assert sell_code == 200

    # Position must be closed (quantity = 0)
    pos_after_sell = SandboxPositions.query.filter_by(
        user_id=uid, symbol="RELIANCE", exchange="NSE", product="CNC"
    ).first()
    assert pos_after_sell is not None, "Position row missing after SELL"
    assert pos_after_sell.quantity == 0, (
        f"Expected closed position (qty=0), got {pos_after_sell.quantity}"
    )

    # Two trades must exist: the BUY fill and the SELL fill
    buy_orderid = buy_resp["orderid"]
    sell_orderid = sell_resp["orderid"]
    buy_trade = SandboxTrades.query.filter_by(orderid=buy_orderid).first()
    sell_trade = SandboxTrades.query.filter_by(orderid=sell_orderid).first()
    assert buy_trade is not None, "No trade row for BUY"
    assert sell_trade is not None, "No trade row for SELL"

    # P&L: SELL fills at bid=2799.5, BUY filled at ask=2800.5 → loss = ₹100
    expected_pnl = (2799.5 - 2800.5) * 100  # −100.0
    actual_pnl = float(pos_after_sell.accumulated_realized_pnl or 0)
    assert actual_pnl == pytest.approx(expected_pnl, abs=1.0), (
        f"Expected P&L ≈ {expected_pnl}, got {actual_pnl}"
    )

    # Margin must be released after closing the position
    funds_after_sell = get_user_funds(uid)
    assert funds_after_sell["utiliseddebits"] == pytest.approx(0.0, abs=1.0), (
        f"Margin still held after close: {funds_after_sell['utiliseddebits']}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# T4 — MIS auto-square-off: open MIS position → SquareOffManager at 15:16 IST
# ──────────────────────────────────────────────────────────────────────────────


def test_mis_squareoff_at_1516_flattens_open_position(fresh_user):
    """SquareOffManager.check_and_square_off() at 15:16 IST closes any open MIS
    position for NSE (square-off time = 15:15 IST).

    The MIS position is planted directly in SandboxPositions + SandboxFunds to
    avoid dependence on clock-time during BUY placement. The close order created
    by SquareOffManager → PositionManager.close_position needs a quote; we patch
    ExecutionEngine._fetch_quote to return a deterministic price.
    """
    from database.sandbox_db import SandboxFunds, SandboxPositions, db_session
    from sandbox.execution_engine import ExecutionEngine
    from sandbox.fund_manager import FundManager
    from sandbox.squareoff_manager import SquareOffManager

    uid = fresh_user["user_id"]

    # MIS 5× leverage: margin for 100 RELIANCE @ 2800 = 100 × 2800 / 5 = ₹56,000
    mis_margin = Decimal("56000.00")

    # Plant an open MIS position (bypasses order_manager time-of-day check)
    pos = SandboxPositions(
        user_id=uid,
        symbol="RELIANCE",
        exchange="NSE",
        product="MIS",
        quantity=100,
        average_price=Decimal("2800.00"),
        ltp=Decimal("2800.00"),
        pnl=Decimal("0.00"),
        pnl_percent=Decimal("0.00"),
        accumulated_realized_pnl=Decimal("0.00"),
        margin_blocked=mis_margin,
    )
    db_session.add(pos)

    # Reflect the blocked margin in SandboxFunds
    fm = FundManager(uid)
    fm.block_margin(mis_margin, "MIS test seed position")

    db_session.commit()

    # Verify setup
    assert pos.quantity == 100
    funds_before = fm.get_funds()
    assert funds_before["utiliseddebits"] == pytest.approx(56_000.0, abs=1.0)

    # Freeze squareoff_manager's view of "now" to 15:16 IST (past 15:15 cutoff)
    _T1516 = dt_real.datetime(2026, 6, 23, 15, 16, 0, tzinfo=_IST)

    class _FakeDT:
        """Minimal datetime stand-in: only .now() is called by SquareOffManager."""

        @staticmethod
        def now(tz=None):
            return _T1516.astimezone(tz) if tz else _T1516

        @staticmethod
        def combine(date, time_obj):
            return dt_real.datetime.combine(date, time_obj)

    with patch("sandbox.squareoff_manager.datetime", _FakeDT):
        # Provide a live-looking quote so the close MARKET order executes inline
        with patch.object(
            ExecutionEngine,
            "_fetch_quote",
            return_value=_QUOTE_2810,
        ):
            som = SquareOffManager()
            som.check_and_square_off()

    # Position must now be closed
    db_session.expire_all()  # force re-read from DB
    closed_pos = SandboxPositions.query.filter_by(
        user_id=uid, symbol="RELIANCE", exchange="NSE", product="MIS"
    ).first()
    assert closed_pos is not None, "Position row disappeared unexpectedly"
    assert closed_pos.quantity == 0, (
        f"Expected quantity=0 after square-off, got {closed_pos.quantity}"
    )

    # Realized P&L: SELL fills at bid=2809.5, avg cost=2800.0 → +₹950
    expected_pnl = (2809.5 - 2800.0) * 100  # 950.0
    actual_pnl = float(closed_pos.accumulated_realized_pnl or 0)
    assert actual_pnl == pytest.approx(expected_pnl, abs=1.0), (
        f"Expected P&L ≈ {expected_pnl}, got {actual_pnl}"
    )

    # Margin fully released after close
    funds_after = fm.get_funds()
    assert funds_after["utiliseddebits"] == pytest.approx(0.0, abs=1.0), (
        f"Margin still held after square-off: {funds_after['utiliseddebits']}"
    )
