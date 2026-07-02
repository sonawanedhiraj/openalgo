"""Unit tests for services/futures_follow_service.py.

All external effects (signal evaluation, contract resolution, order placement,
notifications, trade journal, price fetch) are injected with fakes, so these run
with no live broker, no DuckDB, and no DB writes — mirroring
test/test_sector_follow_service.py.

NOTE: written but NOT executed during market hours (pytest pollutes the live
journal). Operator runs `uv run pytest test/test_futures_follow_service.py -v`
post-close to verify before merging to dev.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services.futures_follow_service import (
    FuturesFollowConfig,
    FuturesFollowService,
    FuturesPosition,
    compute_futures_charges,
    compute_lots_to_buy,
)

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _isolate_runtime_override(monkeypatch):
    """pause()/resume()/kill-switch write the shared strategy_runtime_override
    table. Rebind it to a fresh in-memory DB per test so override writes never leak
    between tests."""
    from database import strategy_runtime_override_db as sro

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sro, "engine", eng)
    monkeypatch.setattr(sro, "db_session", sess)
    sro.Base.query = sess.query_property()
    sro.Base.metadata.create_all(eng)
    yield
    sess.remove()
    eng.dispose()


def _config(**overrides) -> FuturesFollowConfig:
    base = {
        "capital_inr": 1_000_000.0,
        "cap_margin_pct": 0.50,
        "nifty_lot_size": 75,
        "nifty_lot_margin_inr": 250_000.0,
        "margin_rate": 0.14,
        "lots_per_signal": 1,
        "max_signals_per_day": 5,
        "daily_loss_kill_pct": 3.0,
        "cost_pct_round_trip": 0.030,
        "underlying": "NIFTY",
        "broker": "zerodha",
        "exchange": "NFO",
        "product": "NRML",
        "strategy_id": 77,
    }
    base.update(overrides)
    return FuturesFollowConfig(**base)


_CONTRACT = {
    "symbol": "NIFTY26JUN24FUT",
    "brsymbol": "NIFTY26JUN24FUT",
    "token": "12345",
    "expiry": "26-JUN-24",
    "lot_size": 75,
}


def _make_service(signals=None, **overrides):
    """Build a service with all side effects stubbed out."""
    placed_orders = []
    journal = []

    def fake_placer(mode, order):
        placed_orders.append((mode, order))
        return {"status": "success", "orderid": f"OID-{order['symbol']}-{len(placed_orders)}"}

    def fake_recorder(**kwargs):
        journal.append(kwargs)
        return len(journal)

    signals = signals if signals is not None else []

    def fake_signal_evaluator(as_of=None):
        return list(signals)

    def fake_contract_resolver(underlying="NIFTY", exchange="NFO", as_of=None):
        return dict(_CONTRACT)

    mode = overrides.pop("mode", "sandbox")
    price_fetcher = overrides.pop("price_fetcher", lambda symbol, exchange: 24000.0)
    notifier = overrides.pop("notifier", lambda msg: None)
    data_health_checker = overrides.pop("data_health_checker", None)
    contract_resolver = overrides.pop("contract_resolver", fake_contract_resolver)
    signal_evaluator = overrides.pop("signal_evaluator", fake_signal_evaluator)

    intent_resolver = overrides.pop("intent_resolver", None)
    if intent_resolver is None:
        from services.mode_service import EffectiveDecision

        intent_resolver = lambda: EffectiveDecision(  # noqa: E731
            mode="sandbox", intent="run", daily_capital_cap=None, source="env"
        )
    cfg = _config(**overrides)
    svc = FuturesFollowService(
        config=cfg,
        mode=mode,
        signal_evaluator=signal_evaluator,
        contract_resolver=contract_resolver,
        order_placer=fake_placer,
        price_fetcher=price_fetcher,
        notifier=notifier,
        trade_recorder=fake_recorder,
        now=lambda: datetime(2026, 6, 10, 15, 20, tzinfo=_IST),
        intent_resolver=intent_resolver,
        data_health_checker=data_health_checker,
    )
    svc._test_placed = placed_orders
    svc._test_journal = journal
    return svc


def _sig(symbol, vol=2.0):
    return {"symbol": symbol, "vol_ratio": vol, "stock_ret": 0.01, "sector_ret": 0.02}


def _seed_position(svc, pos_id, entry_date="2026-06-09", lots=1):
    svc.paper_book[pos_id] = FuturesPosition(
        nifty_symbol="NIFTY26JUN24FUT",
        lots=lots,
        quantity=lots * 75,
        entry_price=24000.0,
        entry_date=entry_date,
        vol_ratio=2.0,
        margin_inr=lots * 250_000.0,
        signal_symbol="OLD",
    )


# --------------------------------------------------------------------------- #
# Pure: position sizing (the 50%-of-capital margin cap)
# --------------------------------------------------------------------------- #
def test_compute_lots_one_lot_when_room():
    # 0 lots filled, ₹10L capital, ₹2.5L/lot margin, 50% cap = ₹5L → room for 2.
    assert compute_lots_to_buy(0, 1_000_000.0, 250_000.0, 0.50) == 1


def test_compute_lots_second_lot_still_fits():
    assert compute_lots_to_buy(1, 1_000_000.0, 250_000.0, 0.50) == 1


def test_compute_lots_third_lot_skipped_at_cap():
    # 2 lots already = ₹5L = the whole 50% cap → 3rd skipped.
    assert compute_lots_to_buy(2, 1_000_000.0, 250_000.0, 0.50) == 0


def test_compute_lots_zero_margin_returns_zero():
    assert compute_lots_to_buy(0, 1_000_000.0, 0.0, 0.50) == 0


# --------------------------------------------------------------------------- #
# Pure: charge model (~₹530/lot round-trip on ~₹18L notional)
# --------------------------------------------------------------------------- #
def test_charges_computed_correctly():
    # 1 NIFTY lot = 75 * 24000 = ₹18,00,000 notional each leg.
    notional = 75 * 24000.0
    charges = compute_futures_charges(notional, notional)
    # Per the documented model:
    #   brokerage 40 + STT 0.0002*18L=360 + exch 0.000019*36L=68.4
    #   + SEBI 0.000001*36L=3.6 + stamp 0.00002*18L=36 + GST 0.18*(40+68.4+3.6)=20.16
    #   = 528.16
    assert charges == pytest.approx(528.16, abs=0.5)
    # ~0.03% of notional.
    assert charges / notional == pytest.approx(0.00029, abs=0.0001)


# --------------------------------------------------------------------------- #
# Signal eval → lots
# --------------------------------------------------------------------------- #
def test_signal_eval_buys_one_lot_per_signal():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    placed = svc.run_entry()
    assert len(placed) == 1
    assert placed[0]["lots"] == 1
    assert placed[0]["quantity"] == 75  # 1 lot * lot_size
    assert placed[0]["nifty_symbol"] == "NIFTY26JUN24FUT"
    assert placed[0]["signal_symbol"] == "AAA"
    # one BUY order routed
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["action"] == "BUY"
    assert svc._test_placed[0][1]["product"] == "NRML"
    assert svc._test_placed[0][1]["exchange"] == "NFO"


# --------------------------------------------------------------------------- #
# Cap-50% enforcement (the core risk control)
# --------------------------------------------------------------------------- #
def test_cap_50_enforcement_greedy_three_signals_two_filled():
    # ₹10L capital, ₹2.5L/lot margin, 50% cap = ₹5L → exactly 2 lots fit.
    # 3 signals fire → first 2 (vol-ratio order) placed, 3rd skipped.
    svc = _make_service(
        signals=[_sig("HIGH", 3.0), _sig("MID", 2.0), _sig("LOW", 1.0)],
        mode="sandbox",
    )
    placed = svc.run_entry()
    assert len(placed) == 2  # capped at 2 lots
    assert [p["signal_symbol"] for p in placed] == ["HIGH", "MID"]
    assert svc.lots_held() == 2
    assert svc.margin_used() == 500_000.0  # exactly the 50% cap


def test_cap_50_enforcement_third_signal_skipped_when_two_already_held():
    # Two lots already open (entered prior session, still consuming overnight
    # margin at 15:20) → a fresh signal is skipped at the cap.
    svc = _make_service(signals=[_sig("NEW")], mode="sandbox")
    _seed_position(svc, "P1")
    _seed_position(svc, "P2")
    assert svc.lots_held() == 2
    placed = svc.run_entry()
    assert placed == []  # cap already hit
    assert svc._test_placed == []  # no new order routed


# --------------------------------------------------------------------------- #
# Mode-aware order placement (sandbox is the structural default — no scaffold)
# --------------------------------------------------------------------------- #
def test_default_mode_is_sandbox_and_places_orders(monkeypatch):
    # With mode=None (constructor reads the env) and no env override, the service
    # defaults to ACTIVE sandbox trading — it routes a real order, not a logged-only
    # signal.
    monkeypatch.delenv("FUTURES_FOLLOW_MODE", raising=False)
    svc = _make_service(signals=[_sig("AAA")], mode=None)  # mode=None → env default
    assert svc.mode == "sandbox"
    placed = svc.run_entry()
    assert len(placed) == 1
    assert len(svc._test_placed) == 1  # an order WAS routed
    assert svc._test_placed[0][0] == "sandbox"
    assert svc._test_journal[0]["status"] == "placed"


def test_unknown_mode_falls_back_to_sandbox():
    svc = _make_service(signals=[_sig("AAA")], mode="bogus")
    assert svc.mode == "sandbox"


def test_sandbox_mode_routes_to_sandbox_book():
    # Verify the order actually flows to the (mocked) sandbox placer — not just
    # signal logging.
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc.run_entry()
    assert len(svc._test_placed) == 1
    mode, order = svc._test_placed[0]
    assert mode == "sandbox"
    assert order["action"] == "BUY"
    assert order["symbol"] == "NIFTY26JUN24FUT"
    assert order["product"] == "NRML"
    assert order["quantity"] == 75
    assert svc._test_journal[0]["status"] == "placed"
    assert svc.lots_held() == 1


def test_entry_rejection_journaled_no_phantom_position():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc._order_placer = lambda mode, order: {"status": "error", "message": "insufficient margin"}
    placed = svc.run_entry()
    assert placed == []
    assert svc.lots_held() == 0  # no phantom position
    assert svc.today_entries == []
    row = svc._test_journal[0]
    assert row["status"] == "rejected"
    assert "insufficient margin" in row["error_message"]


def test_entry_exception_journaled_and_batch_continues():
    svc = _make_service(signals=[_sig("AAA", 3.0), _sig("BBB", 2.0)], mode="sandbox")
    calls = {"n": 0}

    def flaky(mode, order):
        # First signal (AAA, higher vol-ratio) raises; the second (BBB) must still
        # place. Both map to the SAME NIFTY contract, so distinguish by call order.
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("broker timeout")
        return {"status": "success", "orderid": "OID-BBB"}

    svc._order_placer = flaky
    placed = svc.run_entry()
    # AAA raised → not placed; BBB placed.
    assert [p["signal_symbol"] for p in placed] == ["BBB"]
    assert svc.lots_held() == 1
    statuses = {r.get("status") for r in svc._test_journal}
    assert "exception" in statuses
    assert "placed" in statuses


# --------------------------------------------------------------------------- #
# T+1 exit at 15:25
# --------------------------------------------------------------------------- #
def test_run_exit_squares_off_prior_day_positions():
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    exited = svc.run_exit()
    assert len(exited) == 1
    assert svc.lots_held() == 0
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["action"] == "SELL"
    # gross = (24100-24000)*75 = 7500; net = gross - charges
    ex = svc.today_exits[0]
    assert ex["gross_pnl"] == pytest.approx(100 * 75)
    assert ex["net_pnl"] < ex["gross_pnl"]  # charges subtracted


def test_run_exit_skips_same_day_positions():
    # A position entered TODAY is not eligible for the T+1 exit.
    svc = _make_service(mode="sandbox")
    _seed_position(svc, "P_TODAY", entry_date="2026-06-10")
    exited = svc.run_exit()
    assert exited == []
    assert svc.lots_held() == 1


# --------------------------------------------------------------------------- #
# Kill switch
# --------------------------------------------------------------------------- #
def test_kill_switch_fires_at_3pct_loss():
    svc = _make_service()
    # -3% of 1,000,000 = -30,000. A -30,001 loss trips it.
    active = svc.update_daily_pnl(realized_today=-30_001.0, open_mtm=0.0)
    assert active is True
    assert svc.kill_switch_active is True


def test_kill_switch_does_not_fire_above_threshold():
    svc = _make_service()
    assert svc.update_daily_pnl(realized_today=-29_000.0, open_mtm=0.0) is False


def test_kill_switch_blocks_new_entries():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc.kill_switch_active = True
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []


def test_kill_switch_does_not_block_exits():
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24050.0)
    svc.kill_switch_active = True
    _seed_position(svc, "P1", entry_date="2026-06-09")
    exited = svc.run_exit()
    assert len(exited) == 1
    assert svc._test_placed[0][1]["action"] == "SELL"


def test_daily_reset_clears_kill_switch_and_journals():
    svc = _make_service()
    svc.kill_switch_active = True
    svc.daily_pnl = -99_999.0
    svc.today_entries = [{"x": 1}]
    svc.today_exits = [{"y": 2}]
    svc.run_daily_reset()
    assert svc.kill_switch_active is False
    assert svc.daily_pnl == 0.0
    assert svc.today_entries == []
    assert svc.today_exits == []


# --------------------------------------------------------------------------- #
# Runtime override (pause) — blocks entry, allows exit
# --------------------------------------------------------------------------- #
def test_runtime_override_blocks_entries():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    with patch(
        "database.strategy_runtime_override_db.is_entry_blocked",
        return_value=(True, {"override_type": "pause", "reason": "x", "expires_at": "x"}),
    ):
        placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []


def test_runtime_override_does_not_block_exits():
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24010.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    with patch(
        "database.strategy_runtime_override_db.is_entry_blocked",
        return_value=(True, {"override_type": "kill_switch", "reason": "loss", "expires_at": "x"}),
    ):
        exited = svc.run_exit()
    assert len(exited) == 1
    assert any(o[1]["action"] == "SELL" for o in svc._test_placed)


def test_pause_blocks_entry_resume_clears():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc.pause()
    assert svc.manual_pause is True
    blocked = svc.run_entry()
    assert blocked == []
    assert svc._test_placed == []
    svc.resume()
    assert svc.manual_pause is False
    placed = svc.run_entry()
    assert len(placed) == 1


def test_pause_writes_runtime_override():
    from database import strategy_runtime_override_db as sro

    svc = _make_service()
    svc.pause()
    active = sro.get_active_overrides("futures_follow_cap50", now=svc._utc_naive(svc._now()))
    assert [o["override_type"] for o in active] == ["pause"]


# --------------------------------------------------------------------------- #
# Data-freshness gate
# --------------------------------------------------------------------------- #
def test_run_entry_aborts_on_stale_data():
    alerts = []
    stale = {"NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9}}
    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        notifier=lambda msg: alerts.append(msg),
        data_health_checker=lambda name, date, index_only=False: (False, stale),
    )
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []
    assert any("ABORTED" in a for a in alerts)


def test_run_entry_proceeds_when_data_fresh():
    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        data_health_checker=lambda name, date, index_only=False: (True, {}),
    )
    placed = svc.run_entry()
    assert len(placed) == 1


def test_run_exit_proceeds_despite_stale_index_data():
    svc = _make_service(
        mode="sandbox",
        price_fetcher=lambda s, e: 24020.0,
        data_health_checker=lambda name, date, index_only=False: (
            False,
            {"NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9}},
        ),
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    exited = svc.run_exit()
    assert len(exited) == 1


# --------------------------------------------------------------------------- #
# Contract resolution failure fails closed
# --------------------------------------------------------------------------- #
def test_run_entry_aborts_when_contract_unresolved():
    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        contract_resolver=lambda underlying="NIFTY", exchange="NFO", as_of=None: None,
    )
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []


def test_run_entry_aborts_when_no_price():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox", price_fetcher=lambda s, e: None)
    placed = svc.run_entry()
    assert placed == []
    assert svc._test_placed == []


# --------------------------------------------------------------------------- #
# Mode override via persistent strategy_mode row
# --------------------------------------------------------------------------- #
def _decision(mode="sandbox", cap=None, source="strategy_mode"):
    from services.mode_service import EffectiveDecision

    return EffectiveDecision(mode=mode, intent="run", daily_capital_cap=cap, source=source)


def test_strategy_mode_row_escalates_sandbox_to_live():
    # A persistent strategy_mode row with mode='live' escalates the default
    # sandbox routing to live.
    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        intent_resolver=lambda: _decision(mode="live", source="strategy_mode"),
    )
    svc.run_entry()
    assert svc.mode == "live"
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][0] == "live"


def test_env_source_cannot_escalate_to_live():
    # Safety: a non-strategy_mode decision (env/default) must NOT escalate the
    # active sandbox book to live — only a strategy_mode row can flip live.
    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        intent_resolver=lambda: _decision(mode="live", source="env"),
    )
    svc.run_entry()
    assert svc.mode == "sandbox"  # unchanged — stays sandbox
    assert svc._test_placed[0][0] == "sandbox"  # still routes to sandbox


def test_daily_capital_cap_tightens_margin_cap():
    # cap = ₹250k → only 1 lot fits even though 2 signals pass and base cap is ₹5L.
    svc = _make_service(
        signals=[_sig("AAA", 3.0), _sig("BBB", 2.0)],
        mode="sandbox",
        intent_resolver=lambda: _decision(cap=250_000.0),
    )
    placed = svc.run_entry()
    assert len(placed) == 1
    assert placed[0]["signal_symbol"] == "AAA"


# --------------------------------------------------------------------------- #
# EOD watchdog (tick-independent flatten backstop)
# --------------------------------------------------------------------------- #
def test_eod_watchdog_flattens_open_prior_day_positions():
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24030.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    flattened = svc.run_eod_watchdog()
    assert len(flattened) == 1
    assert svc.lots_held() == 0
    assert svc._test_placed[0][1]["action"] == "SELL"


def test_eod_watchdog_noop_when_nothing_open():
    svc = _make_service(mode="sandbox")
    assert svc.run_eod_watchdog() == []


# --------------------------------------------------------------------------- #
# Observability + EOD summary
# --------------------------------------------------------------------------- #
def test_get_status_returns_required_keys():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    svc.run_entry()
    status = svc.get_status()
    required = {
        "mode",
        "kill_switch_active",
        "manual_pause",
        "lots_held",
        "margin_used_inr",
        "margin_cap_inr",
        "today_entries",
        "today_exits",
        "open_positions",
        "today_pnl_net",
        "capital_inr",
        "config",
    }
    assert required <= set(status)
    assert status["mode"] == "sandbox"
    assert status["lots_held"] == 1
    assert status["margin_used_inr"] == 250_000.0
    assert status["margin_cap_inr"] == 500_000.0


def test_close_all_squares_open_positions():
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24040.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    _seed_position(svc, "P2", entry_date="2026-06-09")
    closed = svc.close_all_positions()
    assert len(closed) == 2
    assert all(c["status"] == "success" for c in closed)
    assert svc.paper_book == {}
    assert all(o[1]["action"] == "SELL" for o in svc._test_placed)


def test_eod_summary_formats_message():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc.run_entry()
    msg = svc.build_eod_summary()
    assert "📊 futures_follow_cap50 EOD 2026-06-10" in msg
    assert "Mode: sandbox" in msg
    assert "Lots bought: 1" in msg
    assert "Kill switch: inactive" in msg


def test_eod_report_file_sink_writes_path(tmp_path):
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox")
    svc.run_entry()
    svc.eod_reports_dir = tmp_path / "eod_reports"
    out_path = svc._write_eod_report()
    expected = tmp_path / "eod_reports" / "2026-06-10.md"
    assert out_path == expected
    content = expected.read_text(encoding="utf-8")
    assert "# futures_follow_cap50 — EOD Report 2026-06-10" in content
    assert "leveraged beta, not alpha" in content


def test_run_eod_summary_telegram_failure_does_not_block_file_sink(tmp_path):
    notified = []

    def boom(msg):
        notified.append(msg)
        raise RuntimeError("telegram down")

    svc = _make_service(mode="sandbox")
    svc._notify = boom
    svc.eod_reports_dir = tmp_path / "eod_reports"
    svc.run_eod_summary()
    assert (tmp_path / "eod_reports" / "2026-06-10.md").exists()
    assert len(notified) == 1


# --------------------------------------------------------------------------- #
# Contract resolver pure logic (near-month selection + expiry parse)
# --------------------------------------------------------------------------- #
def test_parse_expiry_handles_two_and_four_digit_year():
    from services.futures_follow_service import _parse_expiry

    assert _parse_expiry("26-JUN-24") == date(2024, 6, 26)
    assert _parse_expiry("26-JUN-2024") == date(2024, 6, 26)
    assert _parse_expiry("garbage") is None
    assert _parse_expiry("") is None


def test_contract_resolver_picks_nearest_non_expired(monkeypatch):
    from services import futures_follow_service as ffs

    rows = [
        {
            "symbol": "NIFTY26JUN24FUT",
            "name": "NIFTY",
            "expiry": "26-JUN-24",
            "lotsize": 75,
            "brsymbol": "x",
            "token": "1",
        },
        {
            "symbol": "NIFTY31JUL24FUT",
            "name": "NIFTY",
            "expiry": "31-JUL-24",
            "lotsize": 75,
            "brsymbol": "y",
            "token": "2",
        },
        {
            "symbol": "NIFTY29MAY24FUT",
            "name": "NIFTY",
            "expiry": "29-MAY-24",
            "lotsize": 75,
            "brsymbol": "z",
            "token": "3",
        },  # already expired
    ]
    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: rows)
    as_of = datetime(2024, 6, 10, 15, 20, tzinfo=_IST)
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    # 29-MAY expired, 26-JUN is the nearest non-expired.
    assert c["symbol"] == "NIFTY26JUN24FUT"


# --------------------------------------------------------------------------- #
# Expiry-day safety: the strategy holds T+1 overnight, so a contract that won't
# survive until tomorrow's 15:25 exit must be skipped (skip expiries <= today+1).
#
# Dates below are the REAL NIFTY monthly FUT expiries verified against the live
# master contract (db/openalgo.db symtoken) on 2026-06-15:
#   30-JUN-26 (Tue), 28-JUL-26 (Tue), 25-AUG-26 (Tue)  — pattern: LAST TUESDAY of
# the month (NSE moved NIFTY expiry off Thursday). The resolver gate is pure
# calendar arithmetic on the `expiry` field, so the weekday is incidental — but
# the test data mirrors reality so it documents the true expiry cadence.
# --------------------------------------------------------------------------- #
def _two_month_rows():
    """Current-month (30-JUN-26 Tue) and next-month (28-JUL-26 Tue) NIFTY FUT rows."""
    return [
        {
            "symbol": "NIFTY30JUN26FUT",
            "name": "NIFTY",
            "expiry": "30-JUN-26",
            "lotsize": 75,
            "brsymbol": "cur",
            "token": "10",
        },
        {
            "symbol": "NIFTY28JUL26FUT",
            "name": "NIFTY",
            "expiry": "28-JUL-26",
            "lotsize": 75,
            "brsymbol": "next",
            "token": "11",
        },
    ]


def test_resolver_picks_current_month_on_normal_day(monkeypatch):
    """Normal day (Mon 2026-06-15, today): current-month 30-JUN-26 is far enough out."""
    from services import futures_follow_service as ffs

    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: _two_month_rows())
    as_of = datetime(2026, 6, 15, 15, 20, tzinfo=_IST)  # Monday (verified today)
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    assert c["symbol"] == "NIFTY30JUN26FUT"


def test_resolver_picks_next_month_on_expiry_day(monkeypatch):
    """Expiry Tuesday (2026-06-30): current contract expires today → next month."""
    from services import futures_follow_service as ffs

    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: _two_month_rows())
    as_of = datetime(2026, 6, 30, 15, 20, tzinfo=_IST)  # real NIFTY expiry Tuesday
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    assert c["symbol"] == "NIFTY28JUL26FUT"


def test_resolver_picks_next_month_one_day_before_expiry(monkeypatch):
    """Mon 2026-06-29: current contract expires tomorrow (Tue 06-30), cannot survive T+1."""
    from services import futures_follow_service as ffs

    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: _two_month_rows())
    as_of = datetime(2026, 6, 29, 15, 20, tzinfo=_IST)  # Monday, day before expiry
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    assert c["symbol"] == "NIFTY28JUL26FUT"


def test_resolver_picks_current_month_two_days_before_expiry(monkeypatch):
    """2026-06-28: current contract survives T+1 (as_of+1=06-29 < 06-30 expiry)."""
    from services import futures_follow_service as ffs

    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: _two_month_rows())
    as_of = datetime(2026, 6, 28, 15, 20, tzinfo=_IST)  # two days before Tue 06-30 expiry
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    assert c["symbol"] == "NIFTY30JUN26FUT"


def test_resolver_returns_none_when_all_expire_within_one_day(monkeypatch):
    """Only contracts expiring today and tomorrow available → fail closed (None)."""
    from services import futures_follow_service as ffs

    rows = [
        {
            "symbol": "NIFTY30JUN26FUT",
            "name": "NIFTY",
            "expiry": "30-JUN-26",  # today
            "lotsize": 75,
            "brsymbol": "cur",
            "token": "10",
        },
        {
            "symbol": "NIFTY01JUL26FUT",
            "name": "NIFTY",
            "expiry": "01-JUL-26",  # tomorrow
            "lotsize": 75,
            "brsymbol": "tom",
            "token": "11",
        },
    ]
    monkeypatch.setattr("database.symbol.fno_search_symbols_db", lambda **kw: rows)
    as_of = datetime(2026, 6, 30, 15, 20, tzinfo=_IST)  # today == 30-JUN expiry
    c = ffs.production_contract_resolver("NIFTY", "NFO", as_of)
    assert c is None


# --------------------------------------------------------------------------- #
# #265 — position-store reconciliation at exit (BOTH modes, mode-aware store)
# --------------------------------------------------------------------------- #
def test_sandbox_exit_consults_store_and_suppresses_phantom():
    """SANDBOX: the guard DOES consult the mode-aware position source (sandbox.db
    via get_open_position). A phantom (store flat) → SUPPRESS the SELL entirely —
    the same guarded behaviour as live, but against the sandbox store."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    flat = (True, {"quantity": 0, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=flat) as store,
    ):
        exited = svc.run_exit()
    store.assert_called()  # the sandbox store IS consulted now
    # Phantom in the sandbox store → no SELL placed, position dropped.
    assert svc._test_placed == []
    assert exited == []
    assert svc.lots_held() == 0


def test_sandbox_exit_partial_store_clamps_qty():
    """SANDBOX: journal 2 lots (150), sandbox store holds 1 lot (75) → SELL only 75.
    The clamp is against the sandbox.db book, routed via the mode-aware source."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09", lots=2)  # quantity=150
    partial = (True, {"quantity": 75, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=partial) as store,
    ):
        exited = svc.run_exit()
    store.assert_called()
    assert svc._test_placed[0][0] == "sandbox"  # routed to the sandbox book
    assert svc._test_placed[0][1]["action"] == "SELL"
    assert svc._test_placed[0][1]["quantity"] == 75  # clamped to the sandbox store
    assert len(exited) == 1


def test_sandbox_exit_consistent_store_proceeds_full_qty():
    """SANDBOX: sandbox store matches journal → SELL the full journalled qty."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")  # quantity=75
    match = (True, {"quantity": 75, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=match),
    ):
        svc.run_exit()
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["quantity"] == 75


def test_sandbox_exit_store_fetch_failure_fails_closed():
    """SANDBOX: sandbox store fetch fails → still SELL, but NEVER more than journaled
    (fail-closed for reverse-risk is preserved in sandbox too)."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09", lots=2)  # quantity=150
    failed = (False, {"status": "error", "message": "down"}, 500)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=failed),
    ):
        svc.run_exit()
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["quantity"] == 150  # journalled, not more


def test_live_exit_phantom_broker_flat_is_suppressed():
    """LIVE: broker reports flat (net 0) → SUPPRESS the SELL entirely."""
    svc = _make_service(mode="live", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    flat = (True, {"quantity": 0, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=flat),
    ):
        exited = svc.run_exit()
    # No SELL placed, position dropped.
    assert svc._test_placed == []
    assert exited == []
    assert svc.lots_held() == 0


def test_live_exit_partial_broker_clamps_qty():
    """LIVE: journal 2 lots (150), broker holds 1 lot (75) → SELL only 75."""
    svc = _make_service(mode="live", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09", lots=2)  # quantity=150
    partial = (True, {"quantity": 75, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=partial),
    ):
        exited = svc.run_exit()
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["action"] == "SELL"
    assert svc._test_placed[0][1]["quantity"] == 75  # clamped to broker
    assert len(exited) == 1
    # P&L journalled on the clamped qty, not the journalled 150.
    ex = svc.today_exits[0]
    assert ex["qty"] == 75
    assert ex["gross_pnl"] == pytest.approx(100 * 75)


def test_live_exit_consistent_broker_proceeds_full_qty():
    """LIVE: broker matches journal → SELL the full journalled qty."""
    svc = _make_service(mode="live", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")  # quantity=75
    match = (True, {"quantity": 75, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=match),
    ):
        svc.run_exit()
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["quantity"] == 75


def test_live_exit_broker_fetch_failure_fails_closed():
    """LIVE: broker fetch fails → still SELL, but NEVER more than journaled."""
    svc = _make_service(mode="live", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09", lots=2)  # quantity=150
    failed = (False, {"status": "error", "message": "down"}, 500)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=failed),
    ):
        svc.run_exit()
    assert len(svc._test_placed) == 1
    assert svc._test_placed[0][1]["quantity"] == 150  # journalled, not more


# --------------------------------------------------------------------------- #
# #265 — boot rehydrate of paper_book from the mode-appropriate store (both modes)
# --------------------------------------------------------------------------- #
def test_rehydrate_rebuilds_paper_book_in_sandbox():
    """SANDBOX: a restart-lost paper_book is rebuilt from the sandbox store
    (sandbox.db, read via the mode-aware get_positionbook) so a T+1 exit is still
    scheduled — the sandbox book can strand a paper leg exactly like live."""
    svc = _make_service(mode="sandbox")
    book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "NIFTY30JUN26FUT",
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": 150,  # 2 lots
                    "average_price": "24000.0",
                },
            ],
        },
        200,
    )
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=book) as store,
    ):
        n = svc.rehydrate_paper_book_from_store()
    store.assert_called()  # the sandbox store IS consulted now
    assert n == 1
    assert svc.lots_held() == 2
    pos = next(iter(svc.paper_book.values()))
    assert pos.nifty_symbol == "NIFTY30JUN26FUT"
    assert pos.quantity == 150


def test_rehydrate_rebuilds_paper_book_in_live():
    svc = _make_service(mode="live")
    book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "NIFTY30JUN26FUT",
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": 150,  # 2 lots
                    "average_price": "24000.0",
                },
                # Non-NIFTY / option leg — must be ignored.
                {
                    "symbol": "RELIANCE",
                    "exchange": "NSE",
                    "product": "MIS",
                    "quantity": 10,
                },
            ],
        },
        200,
    )
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=book),
    ):
        n = svc.rehydrate_paper_book_from_store()
    assert n == 1
    assert svc.lots_held() == 2
    pos = next(iter(svc.paper_book.values()))
    assert pos.nifty_symbol == "NIFTY30JUN26FUT"
    assert pos.quantity == 150
    # Stamped prior-day so today's T+1 exit jobs act on it.
    assert pos.entry_date != "2026-06-10"


def test_rehydrate_skips_already_known_symbols():
    svc = _make_service(mode="live")
    _seed_position(svc, "P1", entry_date="2026-06-09")  # NIFTY26JUN24FUT already held
    book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "NIFTY26JUN24FUT",
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": 75,
                }
            ],
        },
        200,
    )
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=book),
    ):
        n = svc.rehydrate_paper_book_from_store()
    assert n == 0  # already known, not double-counted
    assert svc.lots_held() == 1


# --------------------------------------------------------------------------- #
# #292 — 15:18 pre-entry smoke check for futures_follow_cap50
# --------------------------------------------------------------------------- #


def _make_smoke_service(
    *,
    data_ok: bool = True,
    stale: list[str] | None = None,
    session_ok: bool = True,
    notifier=None,
    now_dt: datetime | None = None,
) -> FuturesFollowService:
    """Build a FuturesFollowService wired for smoke-check testing.

    data_health_checker is injected with a fake that returns ``(data_ok, details_map)``
    where details_map has one entry per stale symbol (ok=False). broker_session_checker
    is a lambda returning ``session_ok``. All other effects are no-ops."""
    stale = stale or []

    def fake_health_checker(strategy_name, date_str=None, index_only=False):
        details_map = {}
        if not data_ok:
            for sym in stale or ["NIFTY"]:
                details_map[sym] = {"ok": False}
        return data_ok, details_map

    alerts = []

    def _notifier(msg):
        alerts.append(msg)
        if notifier:
            notifier(msg)

    svc = FuturesFollowService(
        config=_config(),
        mode="sandbox",
        signal_evaluator=lambda as_of=None: [],
        contract_resolver=lambda u="NIFTY", e="NFO", as_of=None: dict(_CONTRACT),
        order_placer=lambda mode, order: {"status": "success", "orderid": "X"},
        price_fetcher=lambda s, e: 24000.0,
        notifier=_notifier,
        trade_recorder=lambda **kw: 1,
        now=lambda: now_dt or datetime(2026, 7, 2, 15, 18, tzinfo=_IST),
        intent_resolver=None,
        data_health_checker=fake_health_checker,
        broker_session_checker=lambda: session_ok,
    )
    svc._test_alerts = alerts
    return svc


def test_smoke_check_passes_when_data_fresh_and_session_live(monkeypatch):
    """All checks green → ok=True, no override written."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    svc = _make_smoke_service(data_ok=True, session_ok=True)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is True
    assert details["data_ok"] is True
    assert details["broker_session_ok"] is True
    # No override should have been written.
    overrides = sro.list_overrides(include_expired=True)
    smoke_pauses = [r for r in overrides if "smoke_check_failed" in (r.get("reason") or "")]
    assert smoke_pauses == [], f"unexpected smoke-check override: {smoke_pauses}"


def test_smoke_check_blocks_and_alerts_when_data_stale(monkeypatch):
    """Stale feed → ok=False, pause override written, Telegram alert sent."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    svc = _make_smoke_service(data_ok=False, stale=["NIFTYBANK", "NIFTYAUTO"], session_ok=True)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is False
    assert details["data_ok"] is False
    assert "NIFTYBANK" in details["stale_symbols"] or "NIFTYAUTO" in details["stale_symbols"]

    # A pause override must be written so the entry gate blocks.
    overrides = sro.list_overrides(include_expired=True)
    pauses = [
        r
        for r in overrides
        if r["override_type"] == "pause" and "smoke_check_failed" in (r.get("reason") or "")
    ]
    assert pauses, f"expected smoke-check pause override; got {overrides}"

    # The entry gate must honor the override. Check against the SAME simulated
    # clock the service used (15:18 IST on the pinned date), not real wall-clock:
    # the override self-expires at 15:30 IST that day, so a real-time check would
    # spuriously report "expired" whenever the suite runs after 15:30 IST (#303).
    from database.strategy_runtime_override_db import is_entry_blocked

    svc_now_utc = datetime(2026, 7, 2, 15, 18, tzinfo=_IST).astimezone(UTC).replace(tzinfo=None)
    blocked, _ov = is_entry_blocked("futures_follow_cap50", now=svc_now_utc)
    assert blocked is True

    # Telegram alert must mention the strategy name and failure.
    assert any("SMOKE CHECK FAILED" in a for a in svc._test_alerts), svc._test_alerts


def test_smoke_check_blocks_and_alerts_when_broker_session_down(monkeypatch):
    """No broker session → ok=False, pause override written, alert sent."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    svc = _make_smoke_service(data_ok=True, session_ok=False)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is False
    assert details["broker_session_ok"] is False

    overrides = sro.list_overrides(include_expired=True)
    pauses = [
        r
        for r in overrides
        if r["override_type"] == "pause" and "smoke_check_failed" in (r.get("reason") or "")
    ]
    assert pauses, f"expected smoke-check pause override; got {overrides}"
    assert any("broker session not live" in a for a in svc._test_alerts), svc._test_alerts


def test_smoke_check_skipped_when_flag_off(monkeypatch):
    """Flag off → ok=True, no override written, no alert."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "false")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    svc = _make_smoke_service(data_ok=False, session_ok=False)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is True
    assert details.get("skipped") is True
    overrides = sro.list_overrides(include_expired=True)
    assert overrides == [], f"unexpected overrides when flag off: {overrides}"
    assert svc._test_alerts == []


def test_smoke_check_skips_freshness_when_master_flag_off(monkeypatch):
    """DATA_FRESHNESS_VALIDATION_ENABLED=false → freshness arm skipped (data_ok=True)
    but broker check still runs."""
    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "false")
    # data_health_checker would return False but the flag bypasses it.
    svc = _make_smoke_service(data_ok=False, stale=["NIFTY"], session_ok=True)
    ok, details = svc.assert_data_pipeline_healthy()

    # Fresh flag is off so data arm is treated as OK; broker is live → overall pass.
    assert ok is True
    assert details["data_ok"] is True


def test_smoke_check_job_body_calls_method_and_swallows_exceptions():
    """The _smoke_check_job module function calls the singleton's method and
    must not propagate exceptions from a buggy smoke check."""
    from services.futures_follow_service import _smoke_check_job, get_service

    # With no singleton, the job is a no-op.
    _smoke_check_job()  # must not raise

    # With a singleton whose smoke check raises, the job still must not raise.
    svc = _make_smoke_service()

    def _exploding_check():
        raise RuntimeError("deliberate boom")

    svc.assert_data_pipeline_healthy = _exploding_check  # type: ignore[assignment]

    import services.futures_follow_service as _ffs_mod

    original_singleton = _ffs_mod._SINGLETON
    try:
        _ffs_mod._SINGLETON = svc
        _smoke_check_job()  # must not raise despite the boom
    finally:
        _ffs_mod._SINGLETON = original_singleton


def test_register_jobs_includes_smoke_check_when_enabled(monkeypatch):
    """When FUTURES_FOLLOW_SMOKE_CHECK_ENABLED=true the scheduler gets the
    futures_follow_smoke_check job registered."""
    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")

    job_ids: list[str] = []

    class FakeScheduler:
        def add_job(self, fn, trigger, id, replace_existing, name):
            job_ids.append(id)

    svc = _make_smoke_service()
    svc.strategy_id = 77  # avoid seed_strategy DB call
    svc.register_jobs(FakeScheduler())

    assert "futures_follow_smoke_check" in job_ids


def test_register_jobs_skips_smoke_check_when_flag_off(monkeypatch):
    """When FUTURES_FOLLOW_SMOKE_CHECK_ENABLED=false the smoke-check job is NOT
    registered (no 15:18 job ID in the scheduler)."""
    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "false")

    job_ids: list[str] = []

    class FakeScheduler:
        def add_job(self, fn, trigger, id, replace_existing, name):
            job_ids.append(id)

    svc = _make_smoke_service()
    svc.strategy_id = 77
    svc.register_jobs(FakeScheduler())

    assert "futures_follow_smoke_check" not in job_ids


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
