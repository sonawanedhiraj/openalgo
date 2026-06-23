"""P0-T5: EOD three-layer defense integration test.

Three independent EOD safety layers tested in isolation and (conceptually) in sequence:

  Layer 1 — tick-driven: engine.check_eod_exits after eod_exit_time
  Layer 2 — APScheduler watchdog (15:14 IST): flatten_strategy_positions
  Layer 3 — Reconciliation (15:30 IST): reconcile_engine_journal stamps sandbox closures

Plus: load-bearing constraint that the watchdog cap is exactly 15:14 (not 15:15),
because sandbox rejects MIS orders placed at/after 15:15.

All hermetic — no live DB, no broker, no network.

Refs #94
"""

from __future__ import annotations

import datetime as dt
import sys
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.orm import Session, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

import restx_api  # noqa: F401 — pre-resolve circular import
import services  # noqa: F401
import services.place_order_service  # noqa: F401
from services.eod_watchdog_service import _WATCHDOG_CAP_TIME, _parse_hhmm
from services.simplified_stock_engine_core import (
    Candle,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)
from services.simplified_stock_engine_service import (
    SimplifiedStockEngineService,
    flatten_strategy_positions,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------


class FixedClock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


class _RecordingJournal:
    """Stub for services.trade_journal_service — records every call, no DB."""

    def __init__(self, open_rows: list[dict] | None = None, next_id: int = 10):
        self.entries: list[dict] = []
        self.exits: list[dict] = []
        self.open_rows: list[dict] = list(open_rows or [])
        self._next_id = next_id

    def record_entry(self, **kw):
        self.entries.append(kw)
        return self._next_id

    def update_entry_fill(self, journal_id, entry_price=None, entry_fill_at=None):
        pass

    def get_open_journal_id_for_symbol(self, symbol):
        return self._next_id

    def record_exit(self, journal_id, **kw):
        self.exits.append({"journal_id": journal_id, **kw})

    def get_trades_for_symbol(self, symbol, days=1):
        return []

    def get_today_summary(self):
        return {"count": len(self.exits), "total_pnl": 0.0}

    def get_open_trades_today(self, strategy_name=None):
        return list(self.open_rows)

    def get_open_trades_for_date(self, date_iso, strategy_name=None):
        return list(self.open_rows)


def _install_journal(monkeypatch, journal: _RecordingJournal) -> None:
    monkeypatch.setitem(sys.modules, "services.trade_journal_service", journal)
    monkeypatch.setattr(services, "trade_journal_service", journal, raising=False)


def _open_row(
    symbol: str = "RELIANCE",
    direction: str = "LONG",
    qty: int = 10,
    entry_price: float = 101.0,
    journal_id: int = 10,
) -> dict:
    return {
        "id": journal_id,
        "symbol": symbol,
        "direction": direction,
        "quantity": qty,
        "entry_price": entry_price,
        "strategy_name": "trending_equity_intraday",
        "placed_at": "2026-04-29T10:20:00",
    }


# ---------------------------------------------------------------------------
# Layer 1 — tick-driven EOD via engine.check_eod_exits
# ---------------------------------------------------------------------------


class TestEodLayerOneTick:
    """Layer 1: engine.check_eod_exits fires after eod_exit_time on a price tick."""

    def _engine_with_position(
        self,
        eod_time: dt.time,
        now: dt.datetime,
        symbol: str = "RELIANCE",
    ) -> SimplifiedStockEngine:
        cfg = SimplifiedEngineConfig(
            no_new_openings_time=dt.time(15, 10),
            reference_candle_expiry_seconds=20 * 60,
            eod_exit_time=eod_time,
        )
        eng = SimplifiedStockEngine(config=cfg, now_provider=FixedClock(now))
        eng.positions[symbol] = Position(
            symbol=symbol,
            entry_price=101.0,
            qty=10,
            stop_loss=99.0,
            entry_time=dt.datetime(2026, 4, 29, 10, 20),
            risk_per_share=2.0,
        )
        return eng

    def test_tick_after_eod_time_emits_eod_exit_signal(self):
        """A tick arriving after eod_exit_time triggers an 'eod' exit signal."""
        now = dt.datetime(2026, 4, 29, 10, 25)
        eng = self._engine_with_position(eod_time=dt.time(10, 20), now=now)

        exits = eng.on_price_update("RELIANCE", 102.0)
        eod_exits = [e for e in exits if e.reason == "eod"]

        assert len(eod_exits) == 1
        assert eod_exits[0].symbol == "RELIANCE"
        assert eod_exits[0].action == "SELL"  # BUY position closes with SELL

    def test_tick_before_eod_time_emits_no_eod_signal(self):
        """A tick before eod_exit_time must not trigger an eod exit."""
        now = dt.datetime(2026, 4, 29, 10, 25)
        eng = self._engine_with_position(eod_time=dt.time(15, 20), now=now)

        exits = eng.on_price_update("RELIANCE", 102.0)
        eod_exits = [e for e in exits if e.reason == "eod"]
        assert len(eod_exits) == 0

    def test_check_eod_exits_is_idempotent_same_day(self):
        """check_eod_exits fires once per day — second call on the same date is a no-op."""
        now = dt.datetime(2026, 4, 29, 10, 25)
        eng = self._engine_with_position(eod_time=dt.time(10, 20), now=now)

        first = eng.check_eod_exits()
        assert len(first) == 1

        # Inject a new position to make sure the idempotency is from eod_done_date, not empty positions
        eng.positions["INFY"] = Position(
            symbol="INFY",
            entry_price=1500.0,
            qty=5,
            stop_loss=1480.0,
            entry_time=now,
            risk_per_share=20.0,
        )
        second = eng.check_eod_exits()
        assert len(second) == 0, "Second call on same day must be a no-op"


# ---------------------------------------------------------------------------
# Layer 2 — APScheduler watchdog: flatten_strategy_positions
# ---------------------------------------------------------------------------


class TestEodLayerTwoWatchdog:
    """Layer 2: flatten_strategy_positions flattens open journal rows via place_order."""

    def _setup_flatten(self, monkeypatch, journal: _RecordingJournal) -> None:
        """Wire journal + api_key resolver + engine service mock + place_order mock."""
        _install_journal(monkeypatch, journal)

        # Patch _resolve_api_key_for_flatten to return a key without hitting auth_db
        monkeypatch.setattr(
            "services.simplified_stock_engine_service._resolve_api_key_for_flatten",
            lambda: "k",
        )

        # Patch get_simplified_stock_engine_service to return a mock with the right config
        mock_svc = MagicMock()
        mock_svc.config.exchange = "NSE"
        mock_svc.config.product = "MIS"
        mock_svc.engine.positions = {}
        mock_svc._lock = __import__("threading").Lock()
        monkeypatch.setattr(
            "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
            lambda: mock_svc,
        )

        # Suppress notification helpers that would try to import Telegram
        for fn in (
            "_notify_watchdog_no_api_key",
            "_notify_watchdog_exit_failure",
            "_publish_summary",
        ):
            monkeypatch.setattr(
                f"services.simplified_stock_engine_service.{fn}",
                lambda *a, **kw: None,
                raising=False,
            )

    def test_watchdog_flattens_long_position_and_stamps_journal(self, monkeypatch):
        """Watchdog calls place_order for each open row, stamps journal exit."""
        journal = _RecordingJournal(open_rows=[_open_row(direction="LONG")])
        self._setup_flatten(monkeypatch, journal)

        placed: list[dict] = []

        def fake_place_order(payload, api_key=None):
            placed.append(payload)
            return (True, {"orderid": "wdg-1", "status": "success"}, 200)

        with patch("services.place_order_service.place_order", side_effect=fake_place_order):
            result = flatten_strategy_positions("trending_equity_intraday", reason="eod_watchdog")

        assert result["attempted"] == 1
        assert result["succeeded"] == 1
        assert len(placed) == 1
        # LONG position closes with SELL
        assert placed[0]["action"] == "SELL"
        assert placed[0]["symbol"] == "RELIANCE"
        assert placed[0]["pricetype"] == "MARKET"

        # Journal exit row written with 'eod_watchdog' reason
        assert len(journal.exits) == 1
        assert journal.exits[0]["exit_reason"] == "eod_watchdog"

    def test_watchdog_flattens_short_position_with_buy(self, monkeypatch):
        """SHORT positions close with a BUY order."""
        journal = _RecordingJournal(open_rows=[_open_row(direction="SHORT")])
        self._setup_flatten(monkeypatch, journal)

        placed: list[dict] = []

        def fake_place_order(payload, api_key=None):
            placed.append(payload)
            return (True, {"orderid": "wdg-2", "status": "success"}, 200)

        with patch("services.place_order_service.place_order", side_effect=fake_place_order):
            result = flatten_strategy_positions("trending_equity_intraday", reason="eod_watchdog")

        assert result["succeeded"] == 1
        assert placed[0]["action"] == "BUY"

    def test_watchdog_no_open_rows_returns_zero_attempted(self, monkeypatch):
        """When no positions are open the watchdog does nothing."""
        journal = _RecordingJournal(open_rows=[])
        self._setup_flatten(monkeypatch, journal)

        with patch("services.place_order_service.place_order") as m_po:
            result = flatten_strategy_positions("trending_equity_intraday")

        m_po.assert_not_called()
        assert result["attempted"] == 0

    def test_watchdog_cap_is_exactly_15_14(self):
        """Load-bearing: watchdog cap is 15:14, NOT 15:15.

        Sandbox rejects MIS orders placed at/after 15:15 (NSE/BSE MIS square-off).
        A cap of 15:15 re-creates the 2026-06-10 OIL/HINDZINC/TATAELXSI orphan class.
        """
        assert _WATCHDOG_CAP_TIME == "15:14", (
            f"Cap must be '15:14' (before sandbox 15:15 MIS close); got {_WATCHDOG_CAP_TIME!r}"
        )
        parsed = _parse_hhmm(_WATCHDOG_CAP_TIME)
        assert parsed == (15, 14)
        # Explicitly confirm the cap is BEFORE 15:15
        assert parsed < (15, 15), "Watchdog cap must fire before sandbox MIS square-off at 15:15"


# ---------------------------------------------------------------------------
# Layer 3 — Reconciliation: reconcile_engine_journal
# ---------------------------------------------------------------------------


def _temp_sandbox_db(tmp_path, monkeypatch):
    """Bind database.sandbox_db to a fresh temp SQLite and create all tables."""
    import database.sandbox_db as smod
    from database.sandbox_db import Base as SandboxBase

    db_file = str(tmp_path / "sandbox_test.db")
    eng = sa_create_engine(
        f"sqlite:///{db_file}",
        poolclass=NullPool,
        connect_args={"check_same_thread": False},
    )
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(smod, "engine", eng)
    monkeypatch.setattr(smod, "db_session", sess)
    SandboxBase.query = sess.query_property()
    SandboxBase.metadata.create_all(bind=eng)
    return eng, sess


class TestEodLayerThreeReconciliation:
    """Layer 3: reconcile_engine_journal stamps 'sandbox_eod_squareoff' exit rows."""

    def test_open_row_with_closing_fill_stamped_as_sandbox_eod(self, monkeypatch, tmp_path):
        """Open journal row + flat position + closing fill → exit_reason='sandbox_eod_squareoff'."""
        from database.sandbox_db import SandboxTrades
        from services.engine_eod_reconciliation_service import reconcile_engine_journal

        eng_db, _ = _temp_sandbox_db(tmp_path, monkeypatch)

        # Seed a SELL fill (closing a LONG) in sandbox_trades
        with Session(eng_db) as s:
            s.add(
                SandboxTrades(
                    tradeid="t-eod-1",
                    orderid="o-eod-1",
                    user_id="u1",
                    symbol="RELIANCE",
                    exchange="NSE",
                    action="SELL",  # closing action for direction=LONG
                    quantity=10,
                    price=105.0,
                    product="MIS",
                    strategy="trending_equity_intraday",
                    trade_timestamp=dt.datetime(2026, 4, 29, 15, 15),
                )
            )
            s.commit()

        # No SandboxPositions row → net_qty is None (treated as flat by reconcile)

        journal = _RecordingJournal(open_rows=[_open_row()])
        _install_journal(monkeypatch, journal)

        result = reconcile_engine_journal(
            dt.date(2026, 4, 29), strategy_name="trending_equity_intraday"
        )

        assert result.entries_checked == 1
        assert result.exits_added == 1
        assert len(journal.exits) == 1
        assert journal.exits[0]["exit_reason"] == "sandbox_eod_squareoff"

    def test_still_open_position_is_skipped(self, monkeypatch, tmp_path):
        """A non-flat sandbox position → reconcile skips it (mid-day safe)."""
        from database.sandbox_db import SandboxPositions
        from services.engine_eod_reconciliation_service import reconcile_engine_journal

        eng_db, _ = _temp_sandbox_db(tmp_path, monkeypatch)

        # Position is still open (qty=10)
        with Session(eng_db) as s:
            s.add(
                SandboxPositions(
                    user_id="u1",
                    symbol="RELIANCE",
                    exchange="NSE",
                    product="MIS",
                    quantity=10,
                    average_price=101.0,
                )
            )
            s.commit()

        journal = _RecordingJournal(open_rows=[_open_row()])
        _install_journal(monkeypatch, journal)

        result = reconcile_engine_journal(
            dt.date(2026, 4, 29), strategy_name="trending_equity_intraday"
        )

        assert result.entries_checked == 1
        assert result.exits_added == 0
        reasons = [s["reason"] for s in result.skipped]
        assert "still_open" in reasons

    def test_reconciliation_is_idempotent(self, monkeypatch, tmp_path):
        """Running reconcile twice produces exactly one exit row (idempotent via exited_at IS NULL)."""
        from database.sandbox_db import SandboxTrades
        from services.engine_eod_reconciliation_service import reconcile_engine_journal

        eng_db, _ = _temp_sandbox_db(tmp_path, monkeypatch)

        with Session(eng_db) as s:
            s.add(
                SandboxTrades(
                    tradeid="t-idem-1",
                    orderid="o-idem-1",
                    user_id="u1",
                    symbol="INFY",
                    exchange="NSE",
                    action="SELL",
                    quantity=5,
                    price=1505.0,
                    product="MIS",
                    strategy="trending_equity_intraday",
                    trade_timestamp=dt.datetime(2026, 4, 29, 15, 15),
                )
            )
            s.commit()

        journal = _RecordingJournal(open_rows=[_open_row(symbol="INFY", qty=5, entry_price=1500.0)])
        _install_journal(monkeypatch, journal)

        # First run — should stamp one exit
        r1 = reconcile_engine_journal(
            dt.date(2026, 4, 29), strategy_name="trending_equity_intraday"
        )
        assert r1.exits_added == 1
        assert len(journal.exits) == 1

        # Second run — journal.open_rows is now empty (row was "closed" by first run,
        # so get_open_trades_for_date returns nothing on second call)
        journal.open_rows = []  # simulate the row now being closed in the real DB
        r2 = reconcile_engine_journal(
            dt.date(2026, 4, 29), strategy_name="trending_equity_intraday"
        )
        assert r2.exits_added == 0
        assert len(journal.exits) == 1  # no new exits on second run

    def test_no_covering_fill_leaves_row_open(self, monkeypatch, tmp_path):
        """If sandbox has no closing fill for the symbol, reconcile leaves the row open."""
        from services.engine_eod_reconciliation_service import reconcile_engine_journal

        eng_db, _ = _temp_sandbox_db(tmp_path, monkeypatch)
        # No SandboxTrades seeded → no fill to price the exit from

        journal = _RecordingJournal(open_rows=[_open_row()])
        _install_journal(monkeypatch, journal)

        result = reconcile_engine_journal(
            dt.date(2026, 4, 29), strategy_name="trending_equity_intraday"
        )

        assert result.exits_added == 0
        reasons = [s["reason"] for s in result.skipped]
        assert "no_covering_close_fill" in reasons
        assert len(journal.exits) == 0
