"""Engine ↔ trade_journal integration.

Drives the engine through entry and each exit path with a mocked
``trade_journal_service`` (it captures calls into a list) and a mocked
``_dispatch_order`` / ``_wait_for_fill`` so the broker side is fully
stubbed. The tests then assert:

* entry placement writes a ``record_entry`` call with the right metadata
  and an ``update_entry_fill`` call after the fill resolves;
* each exit path (``stop_loss``, ``eod``, ``global_profit_lock_loser``)
  invokes ``record_exit`` with the normalised reason;
* a broken ``trade_journal_service`` (every call raises) does NOT break
  the engine's entry or exit handlers — the journal write is informational
  only.

The engine has no "target hit" exit path (it trails the stop instead) and
no "manual close" path (operator would close via broker UI). Those reason
codes exist in the journal schema for future surfaces but are not driven
from this engine.
"""

from __future__ import annotations

# === Live-DB isolation: rebind trade_journal_db to an in-memory engine.
# Every test here installs a journal stub, so the engine's record_entry calls
# normally never reach the DB. This block is defense-in-depth: it guarantees
# that even an unstubbed path cannot write into the operator's real
# db/openalgo.db trade_journal. We surgically rebind ONLY trade_journal_db (the
# write path); DATABASE_URL is left on the live DB so the engine's read-only
# calls keep working, and reads never pollute. The module engine uses NullPool,
# which drops :memory: tables between operations, so we bind to a private
# default-pool engine whose single connection persists the schema.
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import database.trade_journal_db as _tjdb

_journal_engine = create_engine("sqlite:///:memory:")
_journal_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=_journal_engine)
)
_tjdb.engine = _journal_engine
_tjdb.db_session = _journal_session
_tjdb.Base.query = _journal_session.query_property()
_tjdb.Base.metadata.create_all(_journal_engine)

import datetime as dt
import sys

import pytest

import services
import services.simplified_stock_engine_service as sse_module
from services.simplified_stock_engine_core import (
    DIRECTION_BUY,
    MODE_SANDBOX,
    EntrySignal,
    ExitSignal,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)


def _install_journal_stub(monkeypatch, stub) -> None:
    """Replace ``services.trade_journal_service`` everywhere the engine
    might look it up.

    ``from services import trade_journal_service`` (the lazy import in the
    engine's helper) can resolve via either ``sys.modules`` OR the
    ``services`` package's attribute — and when another test has already
    imported the real module, the package attribute is set. We patch
    both so the stub is what the engine sees regardless of import order.
    """
    monkeypatch.setitem(sys.modules, "services.trade_journal_service", stub)
    monkeypatch.setattr(services, "trade_journal_service", stub, raising=False)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _RecordingJournal:
    """Stub for services.trade_journal_service that captures every call."""

    def __init__(self, next_id: int = 42, open_id_lookup: int | None = 42):
        self.entries: list[dict] = []
        self.fills: list[dict] = []
        self.exits: list[dict] = []
        self.lookups: list[str] = []
        self._next_id = next_id
        self._open_id_lookup = open_id_lookup

    def record_entry(self, **kwargs) -> int:
        self.entries.append(kwargs)
        return self._next_id

    def update_entry_fill(self, journal_id, entry_price=None, entry_fill_at=None):
        self.fills.append(
            {
                "journal_id": journal_id,
                "entry_price": entry_price,
                "entry_fill_at": entry_fill_at,
            }
        )

    def get_open_journal_id_for_symbol(self, symbol):
        self.lookups.append(symbol)
        return self._open_id_lookup

    def record_exit(self, journal_id, **kwargs):
        self.exits.append({"journal_id": journal_id, **kwargs})


class _BrokenJournal:
    """Every call raises — used to assert engine survives journal failure."""

    def record_entry(self, **kwargs) -> int:
        raise RuntimeError("simulated journal outage")

    def update_entry_fill(self, *a, **kw):
        raise RuntimeError("simulated journal outage")

    def get_open_journal_id_for_symbol(self, symbol):
        raise RuntimeError("simulated journal outage")

    def record_exit(self, *a, **kw):
        raise RuntimeError("simulated journal outage")


@pytest.fixture
def service(monkeypatch):
    """A SimplifiedStockEngineService in sandbox mode with the broker side
    stubbed: ``_dispatch_order`` returns success with a fake order id,
    ``_wait_for_fill`` returns a fake fill price, and ``_check_live_funds``
    isn't reached because the mode is sandbox.

    Risk gate is disabled, veto layer is bypassed, and the engine clock
    is anchored to a fixed in-market time.
    """
    # Daily circuit breaker — fail open with "not tripped".
    monkeypatch.setattr(
        "services.risk_service.daily_circuit_breaker_tripped", lambda: (False, "")
    )

    cfg = SimplifiedEngineConfig(mode=MODE_SANDBOX)
    fake_now = dt.datetime(2026, 5, 29, 11, 30)
    engine = SimplifiedStockEngine(config=cfg, now_provider=lambda: fake_now)
    svc = sse_module.SimplifiedStockEngineService(config=cfg, engine=engine)
    svc._strategy_by_symbol["RELIANCE"] = "chartink_FnO_intraday_buy"
    svc._api_key_by_symbol["RELIANCE"] = "test-api-key"

    # Bypass the LLM veto layer (mode='off' returns (True, None)).
    monkeypatch.setattr(
        svc, "_run_pre_order_review", lambda signal, strategy_name: (True, None)
    )
    # Sandbox doesn't hit live funds gate, but stub anyway for safety.
    monkeypatch.setattr(svc, "_check_live_funds", lambda api_key: (True, 1_000_000.0, None))

    # Broker stubs.
    monkeypatch.setattr(
        svc,
        "_dispatch_order",
        lambda payload, api_key, is_entry: (True, {"orderid": "ORD-42"}),
    )
    monkeypatch.setattr(
        svc, "_wait_for_fill", lambda api_key, strategy_name, order_id: 101.5
    )

    return svc, engine


def _make_entry_signal(symbol: str = "RELIANCE") -> EntrySignal:
    return EntrySignal(
        symbol=symbol,
        action=DIRECTION_BUY,
        quantity=10,
        reference_price=101.5,
        stop_loss=99.0,
        risk_per_share=2.5,
        candle_ts=dt.datetime(2026, 5, 29, 11, 30),
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )


def _seed_position(engine: SimplifiedStockEngine, symbol: str = "RELIANCE") -> None:
    """Plant a Position so confirm_exit has something to close."""
    engine.positions[symbol] = Position(
        symbol=symbol,
        entry_price=101.5,
        qty=10,
        stop_loss=99.0,
        entry_time=dt.datetime(2026, 5, 29, 11, 30),
        risk_per_share=2.5,
    )


# ---------------------------------------------------------------------------
# Entry path
# ---------------------------------------------------------------------------


def test_entry_writes_record_entry_and_update_fill(monkeypatch, service):
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    signal = _make_entry_signal()
    engine.pending_entries[signal.symbol] = signal

    svc._place_entry_order(signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    # One entry record, with LONG / qty=10 / strategy=trending_equity_intraday.
    assert len(journal.entries) == 1
    e = journal.entries[0]
    assert e["symbol"] == "RELIANCE"
    assert e["direction"] == "LONG"
    assert e["quantity"] == 10
    assert e["strategy_name"] == "trending_equity_intraday"
    assert e["signal_source"] == "chartink"
    assert e["entry_order_id"] == "ORD-42"
    # decision_id is None because the veto layer was stubbed to off.
    assert e["signal_decision_id"] is None

    # Fill update with the executed price from _wait_for_fill.
    assert len(journal.fills) == 1
    assert journal.fills[0]["journal_id"] == 42
    assert journal.fills[0]["entry_price"] == 101.5

    # Engine confirmed the local position.
    assert "RELIANCE" in engine.positions


def test_entry_with_fill_unconfirmed_still_writes_record(monkeypatch, service):
    """Even when the broker doesn't confirm the fill, the journal entry is
    recorded (the order WAS sent) and the fill update is called with
    entry_price=None so reflection can tell the unfilled case apart.
    """
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    # Override the fill stub to return None (unconfirmed).
    monkeypatch.setattr(
        svc, "_wait_for_fill", lambda api_key, strategy_name, order_id: None
    )

    signal = _make_entry_signal()
    engine.pending_entries[signal.symbol] = signal

    svc._place_entry_order(signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    assert len(journal.entries) == 1
    assert journal.entries[0]["entry_order_id"] == "ORD-42"
    assert len(journal.fills) == 1
    assert journal.fills[0]["entry_price"] is None


def test_entry_does_not_journal_when_broker_rejects(monkeypatch, service):
    """A dispatch failure means no order went out — and so no journal row."""
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    monkeypatch.setattr(
        svc,
        "_dispatch_order",
        lambda payload, api_key, is_entry: (False, {"message": "rejected"}),
    )

    signal = _make_entry_signal()
    engine.pending_entries[signal.symbol] = signal

    svc._place_entry_order(signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    assert journal.entries == []
    assert journal.fills == []


# ---------------------------------------------------------------------------
# Exit paths
# ---------------------------------------------------------------------------


def _build_exit_signal(reason: str) -> ExitSignal:
    return ExitSignal(
        symbol="RELIANCE",
        action="SELL",
        quantity=10,
        reason=reason,
        reference_price=99.0,
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )


def test_exit_stop_loss_writes_record_exit(monkeypatch, service):
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    _seed_position(engine)
    exit_signal = _build_exit_signal("stop_loss")
    engine.pending_exits[exit_signal.symbol] = exit_signal

    svc._place_exit_order(exit_signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    # Looked up the open row, then wrote the exit.
    assert journal.lookups == ["RELIANCE"]
    assert len(journal.exits) == 1
    e = journal.exits[0]
    assert e["journal_id"] == 42
    assert e["exit_reason"] == "stop_loss"
    assert e["exit_order_id"] == "ORD-42"
    assert e["exit_price"] == 101.5


def test_exit_eod_writes_normalized_reason(monkeypatch, service):
    """The engine emits reason='eod'; the journal records 'eod_squareoff'."""
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    _seed_position(engine)
    exit_signal = _build_exit_signal("eod")
    engine.pending_exits[exit_signal.symbol] = exit_signal

    svc._place_exit_order(exit_signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    assert len(journal.exits) == 1
    assert journal.exits[0]["exit_reason"] == "eod_squareoff"


def test_exit_global_profit_lock_writes_reason(monkeypatch, service):
    svc, engine = service
    journal = _RecordingJournal()
    _install_journal_stub(monkeypatch, journal)

    _seed_position(engine)
    exit_signal = _build_exit_signal("global_profit_lock_loser")
    engine.pending_exits[exit_signal.symbol] = exit_signal

    svc._place_exit_order(exit_signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    assert len(journal.exits) == 1
    assert journal.exits[0]["exit_reason"] == "global_profit_lock_loser"


def test_exit_with_no_open_row_does_not_raise(monkeypatch, service):
    """If the journal can't find an open row (e.g. entry was lost), the
    exit handler still completes — it just skips the journal write."""
    svc, engine = service
    journal = _RecordingJournal(open_id_lookup=None)
    _install_journal_stub(monkeypatch, journal)

    _seed_position(engine)
    exit_signal = _build_exit_signal("stop_loss")
    engine.pending_exits[exit_signal.symbol] = exit_signal

    svc._place_exit_order(exit_signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    # Lookup happened but no exit was recorded.
    assert journal.lookups == ["RELIANCE"]
    assert journal.exits == []
    # Position was still closed at the engine layer.
    assert "RELIANCE" not in engine.positions


# ---------------------------------------------------------------------------
# Fail-safety: journal raising must not break the engine
# ---------------------------------------------------------------------------


def test_broken_journal_does_not_break_entry(monkeypatch, service):
    svc, engine = service
    _install_journal_stub(monkeypatch, _BrokenJournal())

    signal = _make_entry_signal()
    engine.pending_entries[signal.symbol] = signal

    # Must complete normally even when every journal call raises.
    svc._place_entry_order(signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    # Engine still confirmed the position despite journal exceptions.
    assert "RELIANCE" in engine.positions


def test_broken_journal_does_not_break_exit(monkeypatch, service):
    svc, engine = service
    _install_journal_stub(monkeypatch, _BrokenJournal())

    _seed_position(engine)
    exit_signal = _build_exit_signal("stop_loss")
    engine.pending_exits[exit_signal.symbol] = exit_signal

    svc._place_exit_order(exit_signal, api_key="test-api-key", strategy_name="chartink_FnO_intraday_buy")

    # Engine still closed the position.
    assert "RELIANCE" not in engine.positions
