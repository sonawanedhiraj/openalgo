"""End-to-end critical-flow tests for the simplified FnO engine + LLM veto layer.

Companion to ``test_critical_flows.py`` (which is sector_follow-centric). This
module exercises the simplified-engine seams the operator depends on daily,
hermetically: temp/in-memory SQLite, a mocked broker (``sandbox_place_order``)
and a mocked veto reviewer (``signal_review_service.review_signal``), an injected
engine clock, and NO network. Each test is deterministic and sub-second.

It deliberately drives the *engine core* (``on_new_candle`` / ``on_price_update`` /
``check_eod_exits``) for the mechanical flows — breakout, ATR stop, RR trailing,
cooldown, slot/trade-limit, EOD square-off — and the *service* seam
(``_place_entry_order`` / ``_place_exit_order``) for the order-placement, journal
and veto flows. The full eventlet HTTP boot + live quote feed is intentionally
NOT re-implemented here (same trade-off the sector_follow e2e doc records).

Two tests are regression anchors for the 2026-06-10 investigation
(``outputs/fno_eod_veto_investigation_2026-06-10/REPORT.md``):

* ``test_sell_signal_veto_is_not_framed_as_buy`` (xfail) — the TATAELXSI bug: the
  veto receives only ``source='chartink_FnO_intraday_buy'`` and no direction, so a
  SELL signal is reviewed as a BUY. Marked ``xfail(strict=False)`` so it documents
  the bug today and auto-passes once the surfaced fix lands.
* ``test_eod_summary_is_gross_realized_closed_only`` /
  ``..._excludes_open_positions`` — lock the Telegram EOD summary semantics
  (gross, realized, closed-only) so a silent scope change is caught.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

# Pre-resolve the restx_api / services.place_order_service circular import before
# importing the engine service (see test_engine_veto_shadow.py for the rationale).
import restx_api  # noqa: F401, E402
import services  # noqa: E402
import services.place_order_service  # noqa: F401 — eager bind for mock.patch
import services.sandbox_service  # noqa: F401 — eager bind for mock.patch
from services.simplified_stock_engine_core import (  # noqa: E402
    DIRECTION_BUY,
    DIRECTION_SELL,
    MODE_SANDBOX,
    Candle,
    EntrySignal,
    Position,
    SimplifiedEngineConfig,
    SimplifiedStockEngine,
)
from services.simplified_stock_engine_service import (  # noqa: E402
    SimplifiedStockEngineService,
)


# --------------------------------------------------------------------------- #
# Clock + candle fixtures (mirror test_simplified_stock_engine_core.py so the
# synthetic breakouts here actually fire through the real gate logic).
# --------------------------------------------------------------------------- #
class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _buy_history():
    start = dt.datetime(2026, 4, 29, 9, 30)
    candles = [
        Candle(
            ts=start + dt.timedelta(minutes=5 * i),
            open=100 + (i % 2),
            high=102 + (i % 2),
            low=99 + (i % 2),
            close=101 + (i % 2),
            volume=600,
            elapsed_pct=1.0,
        )
        for i in range(11)
    ]
    candles[-3] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 10),
        open=100,
        high=101,
        low=98,
        close=99,
        volume=100,
        elapsed_pct=1.0,
    )
    candles[-2] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 15),
        open=101,
        high=102,
        low=100,
        close=102,
        volume=800,
        elapsed_pct=1.0,
    )
    candles[-1] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=102,
        high=103,
        low=100,
        close=101,
        volume=200,
        elapsed_pct=1.0,
    )
    return candles


def _sell_history():
    start = dt.datetime(2026, 4, 29, 9, 30)
    candles = [
        Candle(
            ts=start + dt.timedelta(minutes=5 * i),
            open=100 + (i % 2),
            high=102 + (i % 2),
            low=99 + (i % 2),
            close=101 + (i % 2),
            volume=600,
            elapsed_pct=1.0,
        )
        for i in range(11)
    ]
    candles[-3] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 10),
        open=100,
        high=103,
        low=99,
        close=102,
        volume=900,
        elapsed_pct=1.0,
    )
    candles[-2] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 15),
        open=101,
        high=102,
        low=98,
        close=99,
        volume=800,
        elapsed_pct=1.0,
    )
    candles[-1] = Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=99,
        high=101,
        low=98,
        close=100,
        volume=200,
        elapsed_pct=1.0,
    )
    return candles


def _buy_engine(now=dt.datetime(2026, 4, 29, 10, 24)):
    cfg = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10), reference_candle_expiry_seconds=20 * 60
    )
    eng = SimplifiedStockEngine(config=cfg, now_provider=FixedClock(now))
    eng.activate_buy_symbol("RELIANCE")
    eng.load_historical_candles("RELIANCE", _buy_history())
    return eng


def _sell_engine(now=dt.datetime(2026, 4, 29, 10, 24)):
    cfg = SimplifiedEngineConfig(
        no_new_openings_time=dt.time(15, 10), reference_candle_expiry_seconds=20 * 60
    )
    eng = SimplifiedStockEngine(config=cfg, now_provider=FixedClock(now))
    eng.activate_sell_symbol("RELIANCE")
    eng.load_historical_candles("RELIANCE", _sell_history())
    return eng


def _buy_breakout_candle():
    return Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=100,
        high=102,
        low=99,
        close=101.5,
        volume=300,
        elapsed_pct=0.75,
    )


def _sell_breakdown_candle():
    return Candle(
        ts=dt.datetime(2026, 4, 29, 10, 20),
        open=99,
        high=99.5,
        low=96,
        close=97.5,
        volume=600,
        elapsed_pct=0.75,
    )


# --------------------------------------------------------------------------- #
# Service builder with broker + veto stubbed (sandbox mode).
# --------------------------------------------------------------------------- #
def _service(engine, monkeypatch, *, veto_off=True):
    cfg = engine.config
    svc = SimplifiedStockEngineService(config=cfg, engine=engine)
    svc.mode = MODE_SANDBOX
    svc._strategy_by_symbol["RELIANCE"] = "chartink_FnO_intraday_buy"
    svc._api_key_by_symbol["RELIANCE"] = "test-api-key"
    # Circuit breaker fail-open (not tripped) unless a test overrides it.
    monkeypatch.setattr("services.risk_service.daily_circuit_breaker_tripped", lambda: (False, ""))
    if veto_off:
        monkeypatch.setattr(
            svc, "_run_pre_order_review", lambda signal, strategy_name: (True, None)
        )
    return svc


def _sandbox_ok():
    return (True, {"orderid": "sbx-1", "status": "success", "mode": "analyze"}, 200)


def _sell_entry_signal():
    return EntrySignal(
        symbol="RELIANCE",
        action=DIRECTION_SELL,
        quantity=10,
        reference_price=97.5,
        stop_loss=99.0,
        risk_per_share=1.5,
        candle_ts=dt.datetime(2026, 4, 29, 10, 20),
        exchange="NSE",
        product="MIS",
        pricetype="MARKET",
    )


# =========================================================================== #
# Flows 1 & 2 — BUY / SELL breakout → sandbox order
# =========================================================================== #
class TestBuySellBreakoutToOrder:
    def test_buy_breakout_emits_entry_signal(self):
        sig = _buy_engine().on_new_candle("RELIANCE", _buy_breakout_candle())
        assert sig is not None and sig.action == "BUY" and sig.quantity > 0

    def test_sell_breakout_emits_entry_signal(self):
        sig = _sell_engine().on_new_candle("RELIANCE", _sell_breakdown_candle())
        assert sig is not None and sig.action == "SELL" and sig.quantity > 0

    def test_buy_full_cycle_places_sandbox_order(self, monkeypatch):
        eng = _buy_engine()
        svc = _service(eng, monkeypatch)
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        assert sig is not None
        from unittest.mock import patch

        with (
            patch(
                "services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()
            ) as m_sbx,
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=101.7),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")
        m_sbx.assert_called_once()
        # Order carried the BUY action + the breakout qty into the sandbox call.
        assert eng.positions["RELIANCE"].qty == sig.quantity  # long
        assert eng.positions["RELIANCE"].entry_price == 101.7

    def test_sell_full_cycle_creates_short_position(self, monkeypatch):
        eng = _sell_engine()
        svc = _service(eng, monkeypatch)
        sig = eng.on_new_candle("RELIANCE", _sell_breakdown_candle())
        assert sig is not None and sig.action == "SELL"
        from unittest.mock import patch

        with (
            patch(
                "services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()
            ) as m_sbx,
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=97.4),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")
        m_sbx.assert_called_once()
        assert eng.positions["RELIANCE"].qty < 0  # short


# =========================================================================== #
# Flow 12 — journal pairing (one entry + one exit, no orphan)
# =========================================================================== #
class _RecordingJournal:
    def __init__(self, next_id=7, open_id=7):
        self.entries, self.fills, self.exits, self.lookups = [], [], [], []
        self._next_id, self._open_id = next_id, open_id

    def record_entry(self, **kw):
        self.entries.append(kw)
        return self._next_id

    def update_entry_fill(self, journal_id, entry_price=None, entry_fill_at=None):
        self.fills.append({"journal_id": journal_id, "entry_price": entry_price})

    def get_open_journal_id_for_symbol(self, symbol):
        self.lookups.append(symbol)
        return self._open_id

    def record_exit(self, journal_id, **kw):
        self.exits.append({"journal_id": journal_id, **kw})


def _install_journal_stub(monkeypatch, stub):
    monkeypatch.setitem(sys.modules, "services.trade_journal_service", stub)
    monkeypatch.setattr(services, "trade_journal_service", stub, raising=False)


class TestJournalPairing:
    def test_entry_then_exit_pairs_one_to_one(self, monkeypatch):
        eng = _buy_engine()
        svc = _service(eng, monkeypatch)
        journal = _RecordingJournal()
        _install_journal_stub(monkeypatch, journal)
        from unittest.mock import patch

        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        with (
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=101.7),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
            # Adverse tick → stop fires → exit order.
            pos = eng.positions["RELIANCE"]
            exits = eng.on_price_update("RELIANCE", pos.stop_loss - 0.05)
            assert len(exits) == 1
            svc._place_exit_order(exits[0], api_key="k", strategy_name="s")

        assert len(journal.entries) == 1
        assert journal.entries[0]["direction"] == "LONG"
        assert len(journal.exits) == 1  # exactly one exit, no orphan
        assert journal.exits[0]["exit_reason"] == "stop_loss"
        assert "RELIANCE" not in eng.positions

    def test_broker_reject_leaves_no_position_and_no_journal(self, monkeypatch):
        eng = _buy_engine()
        svc = _service(eng, monkeypatch)
        journal = _RecordingJournal()
        _install_journal_stub(monkeypatch, journal)
        from unittest.mock import patch

        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        with patch(
            "services.sandbox_service.sandbox_place_order",
            return_value=(False, {"status": "error", "message": "rejected"}, 400),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
        # Rejected order → no position, no orphan journal entry.
        assert "RELIANCE" not in eng.positions
        assert journal.entries == [] and journal.exits == []


# =========================================================================== #
# Flows 3 & 4 — veto SHADOW (never blocks) / ACTIVE (blocks on skip)
# =========================================================================== #
def _stub_review(decision, decision_id=42, mode="shadow"):
    return {
        "id": decision_id,
        "decision": decision,
        "reasoning": "stub",
        "confidence": 0.7,
        "latency_ms": 10,
        "claude_session_id": "sid",
        "raw_output": "",
        "enforcement_mode": mode,
        "cache_hit": False,
    }


@pytest.fixture(autouse=True)
def _wipe_review_cache():
    from services import signal_review_service as srs

    srs.clear_review_cache()
    yield
    srs.clear_review_cache()


class TestVetoShadowActive:
    def test_shadow_skip_still_places_order(self, monkeypatch):
        monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
        eng = _buy_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal", return_value=_stub_review("skip")
            ),
            patch("services.signal_review_service.mark_actually_taken"),
            patch(
                "services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()
            ) as m_sbx,
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=101.7),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
        m_sbx.assert_called_once()  # shadow: skip recorded but order placed

    def test_active_skip_blocks_order(self, monkeypatch):
        monkeypatch.setenv("VETO_LAYER_MODE", "active")
        eng = _buy_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal",
                return_value=_stub_review("skip", mode="active"),
            ),
            patch("services.signal_review_service.mark_actually_taken") as m_mark,
            patch("services.sandbox_service.sandbox_place_order") as m_sbx,
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
        m_sbx.assert_not_called()
        m_mark.assert_called_with(42, False)
        assert "RELIANCE" not in eng.positions

    def test_active_take_places_order(self, monkeypatch):
        monkeypatch.setenv("VETO_LAYER_MODE", "active")
        eng = _buy_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal",
                return_value=_stub_review("take", mode="active"),
            ),
            patch("services.signal_review_service.mark_actually_taken"),
            patch(
                "services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()
            ) as m_sbx,
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=101.7),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
        m_sbx.assert_called_once()


# =========================================================================== #
# Flow 5 — veto direction consistency (the TATAELXSI bug)
# =========================================================================== #
class TestVetoDirectionConsistency:
    def test_buy_signal_reaches_reviewer(self, monkeypatch):
        monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
        eng = _buy_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal", return_value=_stub_review("take")
            ) as m_rev,
            patch("services.signal_review_service.mark_actually_taken"),
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=101.7),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")
        m_rev.assert_called_once()
        assert m_rev.call_args.kwargs["symbol"] == "RELIANCE"

    def test_veto_receives_explicit_sell_direction(self, monkeypatch):
        """The fixed contract (TATAELXSI fix): a SELL signal is reviewed with the
        source string STILL carrying "buy" (one webhook for both legs), but the
        actual side now rides an explicit direction='SELL' kwarg so the reviewer
        never has to infer it from the misleading source."""
        monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
        eng = _sell_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = _sell_entry_signal()
        eng.pending_entries["RELIANCE"] = sig
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal", return_value=_stub_review("take")
            ) as m_rev,
            patch("services.signal_review_service.mark_actually_taken"),
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=97.4),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")
        kw = m_rev.call_args.kwargs
        assert kw["source"] == "chartink_FnO_intraday_buy"  # still carries "buy"
        assert kw["direction"] == "SELL"  # but the true side is now explicit

    def test_sell_signal_veto_is_not_framed_as_buy(self, monkeypatch):
        monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
        eng = _sell_engine()
        svc = _service(eng, monkeypatch, veto_off=False)
        sig = _sell_entry_signal()
        eng.pending_entries["RELIANCE"] = sig
        from unittest.mock import patch

        with (
            patch(
                "services.signal_review_service.review_signal", return_value=_stub_review("take")
            ) as m_rev,
            patch("services.signal_review_service.mark_actually_taken"),
            patch("services.sandbox_service.sandbox_place_order", return_value=_sandbox_ok()),
            patch.object(SimplifiedStockEngineService, "_wait_for_fill", return_value=97.4),
        ):
            svc._place_entry_order(sig, api_key="k", strategy_name="chartink_FnO_intraday_buy")
        kw = m_rev.call_args.kwargs
        # The reviewer MUST be able to tell this is a SELL/short, either via an
        # explicit direction kwarg or a sell-tagged source. Today neither holds.
        discernible = (
            str(kw.get("direction", "")).upper() in ("SELL", "SHORT")
            or "sell" in kw.get("source", "").lower()
            or "short" in kw.get("source", "").lower()
        )
        assert discernible


# =========================================================================== #
# Flow 6 — ATR stop loss  |  Flow 7 — RR trailing stop
# =========================================================================== #
class TestStopAndTrailing:
    def test_atr_stop_loss_fires_exit(self):
        eng = _buy_engine()
        eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        pos = eng.confirm_entry("RELIANCE", executed_price=101.5)
        assert pos is not None
        exits = eng.on_price_update("RELIANCE", pos.stop_loss - 0.05)
        assert len(exits) == 1
        assert exits[0].action == "SELL" and exits[0].reason == "stop_loss"

    def test_rr_trailing_raises_stop_then_exits_at_trail(self):
        eng = _buy_engine()
        eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        pos = eng.confirm_entry("RELIANCE", executed_price=101.5)
        original_sl = pos.stop_loss
        # Drive price well into profit (≥ rr_trail_start_r × risk) so the trail
        # ratchets the stop up above the original.
        eng.on_price_update("RELIANCE", 101.5 + 20 * pos.risk_per_share)
        assert eng.positions["RELIANCE"].stop_loss > original_sl
        # A pullback to the new (higher) trailed stop exits.
        trailed = eng.positions["RELIANCE"].stop_loss
        exits = eng.on_price_update("RELIANCE", trailed - 0.05)
        assert len(exits) == 1 and exits[0].reason == "stop_loss"


# =========================================================================== #
# Flow 8 — daily kill switch  |  Flow 9 — trade/slot limit  |  Flow 10 — cooldown
# =========================================================================== #
class TestRiskGates:
    def test_kill_switch_blocks_entry(self, monkeypatch):
        eng = _buy_engine()
        svc = _service(eng, monkeypatch)
        monkeypatch.setattr(
            "services.risk_service.daily_circuit_breaker_tripped",
            lambda: (True, "daily loss limit hit"),
        )
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        assert sig is not None
        from unittest.mock import patch

        with patch("services.sandbox_service.sandbox_place_order") as m_sbx:
            svc._place_entry_order(sig, api_key="k", strategy_name="s")
        m_sbx.assert_not_called()
        assert "RELIANCE" not in eng.positions

    def test_trade_limit_blocks_new_entry(self):
        eng = _buy_engine()
        eng.trades_today = eng.config.max_trades_per_day  # at the cap
        sig = eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        assert sig is None  # entry window closed by the daily trade limit

    def test_cooldown_blocks_reentry_after_stop(self):
        eng = _buy_engine()
        eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        eng.confirm_entry("RELIANCE", executed_price=101.5)
        # Stop out → confirm_exit installs the same-symbol cooldown.
        exits = eng.on_price_update("RELIANCE", eng.positions["RELIANCE"].stop_loss - 0.05)
        eng.confirm_exit("RELIANCE", exit_price=exits[0].reference_price, reason="stop_loss")
        assert "RELIANCE" not in eng.positions
        # Re-arm + a fresh breakout one candle later is refused by cooldown.
        eng.activate_buy_symbol("RELIANCE")
        eng.load_historical_candles("RELIANCE", _buy_history())
        nxt = _buy_breakout_candle()
        nxt = Candle(
            ts=nxt.ts + dt.timedelta(minutes=5),
            open=nxt.open,
            high=nxt.high,
            low=nxt.low,
            close=nxt.close,
            volume=nxt.volume,
            elapsed_pct=0.75,
        )
        assert eng.on_new_candle("RELIANCE", nxt) is None


# =========================================================================== #
# Flow 11 — EOD square-off
# =========================================================================== #
class TestEodSquareoff:
    def test_eod_squareoff_emits_exit_for_open_positions(self):
        eng = _buy_engine()
        eng.on_new_candle("RELIANCE", _buy_breakout_candle())
        eng.confirm_entry("RELIANCE", executed_price=101.5)
        # Advance the clock past the EOD exit time.
        eod_now = dt.datetime.combine(
            dt.date(2026, 4, 29),
            (
                dt.datetime.min
                + dt.timedelta(
                    hours=eng.config.eod_exit_time.hour, minutes=eng.config.eod_exit_time.minute + 1
                )
            ).time(),
        )
        eng.now_provider = FixedClock(eod_now)
        exits = eng.check_eod_exits()
        assert len(exits) == 1 and exits[0].reason == "eod"


# =========================================================================== #
# Flow 13 / A1 — EOD telegram-summary semantics (gross, realized, closed-only)
# =========================================================================== #
@pytest.fixture
def journal_db(monkeypatch):
    """Rebind trade_journal_db to a fresh temp SQLite (never the live DB).

    ``trade_journal_service._session()`` resolves ``db_session`` dynamically, so
    the rebind is transparent to the service helpers under test."""
    import database.trade_journal_db as tjdb

    with tempfile.TemporaryDirectory() as d:
        eng = create_engine(
            f"sqlite:///{os.path.join(d, 'tj.db')}",
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
        monkeypatch.setattr(tjdb, "engine", eng)
        monkeypatch.setattr(tjdb, "db_session", sess)
        tjdb.Base.query = sess.query_property()
        tjdb.Base.metadata.create_all(bind=eng)
        yield tjdb


class TestEodSummarySemantics:
    def test_eod_summary_is_gross_realized_closed_only(self, journal_db):
        import services.trade_journal_service as tj

        jid = tj.record_entry(
            symbol="JINDALSTEL",
            direction="SHORT",
            quantity=88,
            strategy_name="trending_equity_intraday",
            signal_source="chartink",
            entry_price=1129.0,
        )
        tj.update_entry_fill(jid, entry_price=1129.0)
        # Gross +₹352 = (1129 - 1125) × 88. No charges deducted.
        tj.record_exit(jid, exit_price=1125.0, exit_reason="stop_loss", pnl=352.0)

        summ = tj.get_today_summary()
        assert summ["count"] == 1
        assert summ["total_pnl"] == 352.0  # GROSS — matches Telegram, not /mypnl net
        assert summ["winners"] == 1 and summ["losers"] == 0
        assert summ["by_strategy"]["trending_equity_intraday"]["count"] == 1

    def test_eod_summary_excludes_open_positions(self, journal_db):
        import services.trade_journal_service as tj

        # One closed + one still-open (no record_exit) → only the closed counts.
        jid = tj.record_entry(
            symbol="SBIN",
            direction="LONG",
            quantity=10,
            strategy_name="trending_equity_intraday",
            signal_source="chartink",
            entry_price=800.0,
        )
        tj.update_entry_fill(jid, entry_price=800.0)
        tj.record_exit(jid, exit_price=810.0, exit_reason="target", pnl=100.0)
        open_jid = tj.record_entry(
            symbol="INFY",
            direction="LONG",
            quantity=5,
            strategy_name="trending_equity_intraday",
            signal_source="chartink",
            entry_price=1500.0,
        )
        tj.update_entry_fill(open_jid, entry_price=1500.0)  # never exited

        summ = tj.get_today_summary()
        assert summ["count"] == 1  # open INFY excluded (no exited_at)
        assert summ["total_pnl"] == 100.0

    def test_eod_summary_groups_by_exit_reason(self, journal_db):
        import services.trade_journal_service as tj

        for sym, reason, pnl in [
            ("AAA", "stop_loss", -50.0),
            ("BBB", "stop_loss", -30.0),
            ("CCC", "eod_squareoff", 200.0),
        ]:
            jid = tj.record_entry(
                symbol=sym,
                direction="LONG",
                quantity=1,
                strategy_name="trending_equity_intraday",
                signal_source="chartink",
                entry_price=100.0,
            )
            tj.update_entry_fill(jid, entry_price=100.0)
            tj.record_exit(jid, exit_price=100.0, exit_reason=reason, pnl=pnl)

        summ = tj.get_today_summary()
        assert summ["count"] == 3
        assert summ["by_exit_reason"]["stop_loss"]["count"] == 2
        assert summ["by_exit_reason"]["stop_loss"]["pnl"] == -80.0
        assert summ["by_exit_reason"]["eod_squareoff"]["pnl"] == 200.0
        assert summ["winners"] == 1 and summ["losers"] == 2
