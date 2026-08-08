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
    signal_evaluator_details = overrides.pop("signal_evaluator_details", None)
    signal_reviewer = overrides.pop("signal_reviewer", None)
    market_context_provider = overrides.pop("market_context_provider", None)
    news_context_provider = overrides.pop("news_context_provider", lambda: "")
    order_placer = overrides.pop("order_placer", fake_placer)

    intent_resolver = overrides.pop("intent_resolver", None)
    now = overrides.pop("now", lambda: datetime(2026, 6, 10, 15, 20, tzinfo=_IST))
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
        signal_evaluator_details=signal_evaluator_details,
        contract_resolver=contract_resolver,
        order_placer=order_placer,
        price_fetcher=price_fetcher,
        notifier=notifier,
        trade_recorder=fake_recorder,
        now=now,
        intent_resolver=intent_resolver,
        data_health_checker=data_health_checker,
        signal_reviewer=signal_reviewer,
        market_context_provider=market_context_provider,
        news_context_provider=news_context_provider,
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
# OPTION_C same-minute@15:25 (issue #406)
# --------------------------------------------------------------------------- #
def test_signal_snapshot_stashes_signals_without_placing():
    svc = _make_service(signals=[_sig("AAA"), _sig("BBB")], mode="sandbox")
    out = svc.run_signal_snapshot()
    assert [s["symbol"] for s in out] == ["AAA", "BBB"]
    assert svc._pending_snapshot is not None
    assert [s["symbol"] for s in svc._pending_snapshot[1]] == ["AAA", "BBB"]
    assert svc._test_placed == []  # snapshot places NO orders


def test_exit_then_entry_exits_first_then_sizes_fresh_cap():
    """OPTION_C core: the T+1 exit runs BEFORE the entry, so the carried lot's
    margin is freed and the new entry sizes against a fresh (empty) book — the
    #405 leak is closed by construction, not by a sizing rule."""
    # 2 lots already held into the session (prior-day) — with legacy sizing they
    # would occupy the whole 50% cap and block today's entries.
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox", price_fetcher=lambda s, e: 24000.0)
    _seed_position(svc, "P1", entry_date="2026-06-09", lots=2)  # exits today
    svc.run_signal_snapshot()
    result = svc.run_exit_then_entry()
    assert len(result["exited"]) == 1  # the prior-day 2-lot cohort squared off
    assert len(result["placed"]) == 1  # today's entry placed (cap was fresh)
    # exactly one held position remains: today's new lot, none of the old
    assert svc.lots_held() == 1
    sides = [o[1]["action"] for o in svc._test_placed]
    assert sides == ["SELL", "BUY"]  # exit first, then entry


def test_exit_then_entry_falls_back_to_fresh_eval_without_snapshot():
    svc = _make_service(signals=[_sig("AAA")], mode="sandbox", price_fetcher=lambda s, e: 24000.0)
    # no run_signal_snapshot() called -> _pending_snapshot is None
    assert svc._pending_snapshot is None
    result = svc.run_exit_then_entry()
    assert len(result["placed"]) == 1  # re-evaluated at 15:25 and placed


def test_signal_snapshot_failure_clears_pending(monkeypatch):
    svc = _make_service(mode="sandbox")
    monkeypatch.setattr(
        svc, "evaluate_signals", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    out = svc.run_signal_snapshot()
    assert out == []
    assert svc._pending_snapshot is None  # cleared on failure (15:25 re-evaluates)


def _open_store_book(symbol="NIFTY30JUN26FUT", qty=75, avg="24000.0"):
    return (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": symbol,
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": qty,
                    "average_price": avg,
                }
            ],
        },
        200,
    )


def test_run_exit_rehydrates_boot_race_position_before_squaring_off():
    """Issue #403 — the 2026-07-14 missed-exit reproduction. The in-memory
    paper_book is EMPTY (boot rehydration skipped because the broker session
    wasn't up yet), but a prior-day leg is still open in the store. run_exit must
    rehydrate-then-exit so the T+1 square-off still fires — pre-fix it squared off
    0 because it only looked at the empty paper_book."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    assert svc.paper_book == {}  # boot-race: nothing in memory
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=_open_store_book(qty=75, avg="24000.0"),
        ),
    ):
        exited = svc.run_exit()
    assert len(exited) == 1  # pre-fix: 0
    assert svc._test_placed[0][1]["action"] == "SELL"
    assert svc.lots_held() == 0


def test_run_eod_watchdog_rehydrates_boot_race_position():
    """The 15:28 watchdog is the second exit path — it must also rehydrate a
    boot-race position the empty paper_book never saw (issue #403)."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    assert svc.paper_book == {}
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch(
            "services.positionbook_service.get_positionbook",
            return_value=_open_store_book(),
        ),
    ):
        flattened = svc.run_eod_watchdog()
    assert len(flattened) == 1
    assert svc.lots_held() == 0


def test_run_exit_rehydrate_before_exit_is_noop_when_store_empty():
    """No open store position -> rehydrate-before-exit is a clean no-op and the
    exit run stays empty (never raises, never fabricates a position)."""
    svc = _make_service(mode="sandbox")
    empty = (True, {"status": "success", "data": []}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=empty),
    ):
        exited = svc.run_exit()
    assert exited == []
    assert svc.lots_held() == 0


# --------------------------------------------------------------------------- #
# #497 — the read path must resolve the SAME book the write path chose
# --------------------------------------------------------------------------- #
def test_rehydrate_passes_strategy_mode_key_to_positionbook():
    """The rehydrate read MUST identify itself with the strategy's mode_key.

    Without it the read falls through to the platform analyze overlay
    (`resolve_effective_mode()`), which returns LIVE whenever Analyze is off —
    so a `sandbox` strategy read the real broker book, got an empty position
    list, and squared off nothing for four trading days. The routing itself is
    covered end-to-end in test/test_positionbook_mode_routing.py; this pins the
    contract at the call site so it cannot silently regress to an untagged read.
    """
    from services.futures_follow_service import STRATEGY_NAME

    svc = _make_service(mode="sandbox")
    empty = (True, {"status": "success", "data": []}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=empty) as store,
    ):
        svc.rehydrate_paper_book_from_store()

    store.assert_called_once()
    assert store.call_args.kwargs.get("mode_key") == STRATEGY_NAME == "futures_follow_cap50"


def test_t1_exit_survives_a_restart_between_entry_and_exit_day():
    """Day-boundary regression (#497): entry day -> process restart (paper_book
    lost) -> exit day still squares off.

    The T+1 overnight hold is this strategy's defining feature, but every prior
    test exercised entry-day and exit-day as independent single-day units with
    `paper_book` handed in pre-built. That is exactly the seam the #440 read/write
    mode split fell through: entries kept working, the restart-rehydrate silently
    returned 0, and nothing failed loudly.
    """
    entry_day = datetime(2026, 7, 27, 15, 20, tzinfo=_IST)
    exit_day = datetime(2026, 7, 28, 15, 25, tzinfo=_IST)
    clock = {"now": entry_day}

    svc = _make_service(
        signals=[_sig("AAA")],
        mode="sandbox",
        now=lambda: clock["now"],
        price_fetcher=lambda s, e: 24106.0,
    )

    # --- Day 1, 15:20: entry lands in the (sandbox) book -------------------- #
    svc.run_entry()
    assert svc.lots_held() == 1
    held = next(iter(svc.paper_book.values()))
    assert held.entry_date == "2026-07-27"

    # --- Overnight restart: the in-memory paper_book is gone ---------------- #
    svc.paper_book.clear()
    assert svc.lots_held() == 0

    # The store still holds the position — this is what the 15:25 exit rehydrates
    # from, and what a misrouted read returned empty.
    store_book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": held.nifty_symbol,
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": held.quantity,
                    "average_price": str(held.entry_price),
                }
            ],
        },
        200,
    )

    # --- Day 2, 15:25: the T+1 exit must find and square off the carry ------ #
    clock["now"] = exit_day
    svc._test_placed.clear()
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.positionbook_service.get_positionbook", return_value=store_book),
    ):
        exited = svc.run_exit()

    assert len(exited) == 1, "the carried position was stranded across the restart"
    assert svc.lots_held() == 0
    assert svc._test_placed[0][1]["action"] == "SELL"


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


def test_rehydrate_stamps_previous_trading_day_not_calendar_yesterday():
    """A Monday restart of a Friday position must stamp the FRIDAY (previous
    trading day), never Sunday (calendar-yesterday) — issue #401. The buggy
    Sunday entry_date rode straight into the T+1 journal exit row, showing an
    impossible entry session on the dashboard."""
    # Monday 2026-07-13; calendar-yesterday is Sunday 2026-07-12 (impossible).
    monday = lambda: datetime(2026, 7, 13, 15, 20, tzinfo=_IST)  # noqa: E731
    svc = _make_service(mode="sandbox", now=monday)
    book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "NIFTY28JUL26FUT",
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": 75,
                    "average_price": "24240.0",
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
    pos = next(iter(svc.paper_book.values()))
    assert pos.entry_date == "2026-07-10"  # Friday, not Sunday 2026-07-12
    # And it's still a prior day, so the T+1 exit predicate (entry_date != today) fires.
    assert pos.entry_date != "2026-07-13"


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


def test_rehydrate_derives_lots_by_ceiling_not_floor(monkeypatch):
    """#353: a store position whose net qty (130) doesn't divide evenly by the
    CURRENTLY CONFIGURED lot size (75) must not silently undercount lots via
    floor division (130 // 75 == 1). This is exactly the 2026-07-06 incident
    shape — two legacy 65-qty BUYs net to a 130-qty store position, and the
    strategy's lot size later became 75 — so ceiling division must derive 2
    lots, and the batched T+1 exit journaled off that rehydrated position must
    record lots=2 (not the hardcoded/undercounted 1)."""
    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    book = (
        True,
        {
            "status": "success",
            "data": [
                {
                    "symbol": "NIFTY30JUN26FUT",
                    "exchange": "NFO",
                    "product": "NRML",
                    "quantity": 130,  # NOT a clean multiple of the 75 lot size
                    "average_price": "24000.0",
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
    pos = next(iter(svc.paper_book.values()))
    assert pos.quantity == 130
    # Ceiling(130 / 75) == 2, not floor(130 / 75) == 1.
    assert pos.lots == 2

    # The subsequent T+1 exit must journal the same corrected lots=2 on its
    # single batched SELL row (quantity still 130 — one order, no re-split).
    match = (True, {"quantity": 130, "status": "success"}, 200)
    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch("services.openposition_service.get_open_position", return_value=match),
    ):
        svc.run_exit()
    sell_rows = [r for r in svc._test_journal if r.get("side") == "SELL"]
    assert len(sell_rows) == 1
    assert sell_rows[0]["quantity"] == 130
    assert sell_rows[0]["lots"] == 2


# --------------------------------------------------------------------------- #
# #507 — the exit reconciliation must route by this strategy's dispatch key
# --------------------------------------------------------------------------- #
def test_exit_reconciliation_passes_canonical_mode_key():
    """Without ``mode_key`` the store read resolves through the analyze overlay
    and a sandbox position reads flat, so the guard suppresses a real exit —
    this strategy lost every T+1 exit from 2026-07-17 to 2026-08-07 that way.
    The full routing behaviour is pinned in
    ``test/test_openposition_mode_routing.py``; this asserts the caller's half."""
    from services import futures_follow_service as ffs
    from services import live_position_reconciliation_service as recon

    svc = _make_service(mode="sandbox", price_fetcher=lambda s, e: 24100.0)
    _seed_position(svc, "P1", entry_date="2026-06-09")

    with (
        patch("services.futures_follow_service._resolve_exit_api_key", return_value="k"),
        patch.object(recon, "reconcile_exit") as rec,
    ):
        rec.return_value = recon.ReconcileDecision(
            broker_qty=75,
            action=recon.ACTION_PROCEED,
            guarded_qty=75,
            reason="ok",
        )
        svc.run_exit()

    assert rec.call_args.kwargs["mode_key"] == ffs.STRATEGY_NAME == "futures_follow_cap50"


# --------------------------------------------------------------------------- #
# #292 — 15:18 pre-entry smoke check for futures_follow_cap50
# --------------------------------------------------------------------------- #


def _make_smoke_service(
    *,
    data_ok: bool = True,
    stale: list[str] | None = None,
    session_ok: bool = True,
    quote_ok: bool = True,
    notifier=None,
    now_dt: datetime | None = None,
) -> FuturesFollowService:
    """Build a FuturesFollowService wired for smoke-check testing.

    data_health_checker is injected with a fake that returns ``(data_ok, details_map)``
    where details_map has one entry per stale symbol (ok=False). broker_session_checker
    is a lambda returning ``session_ok``; quote_probe is a lambda returning
    ``quote_ok`` (issue #332 — keeps the default quotes source hermetic). All
    other effects are no-ops."""
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
        quote_probe=lambda: quote_ok,
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


# --------------------------------------------------------------------------- #
# #332 — quotes-snapshot intraday source: smoke-check quote probe
# --------------------------------------------------------------------------- #


def test_smoke_check_blocks_and_alerts_when_quote_probe_fails(monkeypatch):
    """AC #9: with source=quotes, a failed dry-run quote probe at 15:18 writes
    the same self-expiring pause override + Telegram alert as feed-staleness."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "quotes")
    svc = _make_smoke_service(data_ok=True, session_ok=True, quote_ok=False)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is False
    assert details["quote_probe_ok"] is False
    assert details["intraday_source"] == "quotes"
    # The freshness + session arms were green — the probe alone must block.
    assert details["data_ok"] is True
    assert details["broker_session_ok"] is True

    overrides = sro.list_overrides(include_expired=True)
    pauses = [
        r
        for r in overrides
        if r["override_type"] == "pause" and "smoke_check_failed" in (r.get("reason") or "")
    ]
    assert pauses, f"expected smoke-check pause override; got {overrides}"
    assert any("quote probe" in (r.get("reason") or "") for r in pauses)
    assert any("quote probe" in a for a in svc._test_alerts)


def test_smoke_check_skips_quote_probe_when_source_aggregator(monkeypatch):
    """With FUTURES_FOLLOW_INTRADAY_SOURCE=aggregator the probe never runs —
    a failing probe fake must not block (pre-#332 smoke behavior preserved)."""
    from database import strategy_runtime_override_db as sro

    monkeypatch.setenv("FUTURES_FOLLOW_SMOKE_CHECK_ENABLED", "true")
    monkeypatch.setenv("DATA_FRESHNESS_VALIDATION_ENABLED", "true")
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "aggregator")
    svc = _make_smoke_service(data_ok=True, session_ok=True, quote_ok=False)
    ok, details = svc.assert_data_pipeline_healthy()

    assert ok is True
    assert details["intraday_source"] == "aggregator"
    assert details["quote_probe_ok"] is True  # skipped ⇒ treated as ok
    overrides = sro.list_overrides(include_expired=True)
    smoke_pauses = [r for r in overrides if "smoke_check_failed" in (r.get("reason") or "")]
    assert smoke_pauses == [], f"unexpected smoke-check override: {smoke_pauses}"


def test_futures_intraday_source_defaults_and_validates(monkeypatch):
    """Default is quotes; unknown values fall back to quotes; aggregator honored."""
    from services.futures_follow_service import futures_intraday_source

    monkeypatch.delenv("FUTURES_FOLLOW_INTRADAY_SOURCE", raising=False)
    assert futures_intraday_source() == "quotes"
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "aggregator")
    assert futures_intraday_source() == "aggregator"
    monkeypatch.setenv("FUTURES_FOLLOW_INTRADAY_SOURCE", "bogus")
    assert futures_intraday_source() == "quotes"


# --------------------------------------------------------------------------- #
# #332 follow-up — wall-clock entry-lateness guard (default deadline 15:28 IST)
# --------------------------------------------------------------------------- #


def _make_late_guard_service(fire_at: datetime, signals=None):
    """Service with a pinned clock + counting evaluator/notifier for guard tests."""
    eval_calls = []
    alerts = []

    def counting_evaluator(as_of=None):
        eval_calls.append(as_of)
        return list(signals or [_sig("AAA")])

    svc = _make_service(
        signal_evaluator=counting_evaluator,
        notifier=lambda msg: alerts.append(msg),
        now=lambda: fire_at,
    )
    svc._test_eval_calls = eval_calls
    svc._test_alerts = alerts
    return svc


@pytest.mark.parametrize("fire_hm", [(15, 29), (15, 35)])
def test_entry_skipped_when_fired_after_deadline(monkeypatch, fire_hm):
    """A late-fired entry job (post-15:28 default deadline) places NOTHING:
    no evaluation, no orders, returns [], and Telegrams the operator."""
    monkeypatch.delenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", raising=False)
    h, m = fire_hm
    svc = _make_late_guard_service(datetime(2026, 6, 10, h, m, 5, tzinfo=_IST))
    placed = svc.run_entry()

    assert placed == []
    assert svc._test_eval_calls == [], "evaluator must not run on a late fire"
    assert svc._test_placed == [], "no orders may be placed on a late fire"
    assert any("fired LATE" in a and "no orders placed" in a for a in svc._test_alerts), (
        svc._test_alerts
    )


def test_entry_proceeds_at_scheduled_1520(monkeypatch):
    """The normal 15:20 fire is untouched by the guard (regression)."""
    monkeypatch.delenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", raising=False)
    svc = _make_late_guard_service(datetime(2026, 6, 10, 15, 20, tzinfo=_IST))
    placed = svc.run_entry()
    assert len(placed) == 1
    assert len(svc._test_eval_calls) == 1


def test_entry_deadline_custom_env_respected(monkeypatch):
    """FUTURES_FOLLOW_ENTRY_DEADLINE_IST=15:25 → a 15:26 fire is skipped, a
    15:24 fire proceeds."""
    monkeypatch.setenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", "15:25")
    late = _make_late_guard_service(datetime(2026, 6, 10, 15, 26, tzinfo=_IST))
    assert late.run_entry() == []
    assert late._test_placed == []
    assert any("deadline 15:25" in a for a in late._test_alerts)

    on_time = _make_late_guard_service(datetime(2026, 6, 10, 15, 24, tzinfo=_IST))
    assert len(on_time.run_entry()) == 1


def test_entry_deadline_malformed_env_falls_back_to_default(monkeypatch):
    """A typo'd deadline value must never disable the guard: 'banana' → the
    default 15:28 stays active (15:29 fire skipped, 15:20 fire proceeds)."""
    from services.futures_follow_service import futures_entry_deadline_ist

    monkeypatch.setenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", "banana")
    assert futures_entry_deadline_ist().strftime("%H:%M") == "15:28"

    late = _make_late_guard_service(datetime(2026, 6, 10, 15, 29, tzinfo=_IST))
    assert late.run_entry() == []
    assert late._test_placed == []

    on_time = _make_late_guard_service(datetime(2026, 6, 10, 15, 20, tzinfo=_IST))
    assert len(on_time.run_entry()) == 1


def test_exit_and_watchdog_not_gated_by_entry_deadline(monkeypatch):
    """Repo invariant: exits are NEVER gated. run_exit and run_eod_watchdog
    still square off a held T+1 position at 15:40, well past the deadline."""
    monkeypatch.delenv("FUTURES_FOLLOW_ENTRY_DEADLINE_IST", raising=False)
    fire_at = datetime(2026, 6, 10, 15, 40, tzinfo=_IST)

    svc = _make_service(now=lambda: fire_at)
    _seed_position(svc, "P1", entry_date="2026-06-09")
    exited = svc.run_exit()
    assert len(exited) == 1, "run_exit must not be gated by the entry deadline"

    svc2 = _make_service(now=lambda: fire_at)
    _seed_position(svc2, "P1", entry_date="2026-06-09")
    flattened = svc2.run_eod_watchdog()
    assert len(flattened) == 1, "run_eod_watchdog must not be gated by the entry deadline"


# --------------------------------------------------------------------------- #
# Stage-1 LLM veto gate in run_entry (issue #318)
# --------------------------------------------------------------------------- #


def _fake_reviewer(decisions: dict[str, str], calls: list[dict]):
    """Reviewer stub: decides per signal-symbol; records every call's kwargs."""

    def fake(**kwargs):
        calls.append(kwargs)
        symbol = kwargs.get("symbol")
        return {
            "id": len(calls),
            "decision": decisions.get(symbol, "take"),
            "reasoning": f"reviewed {symbol}",
            "confidence": 0.6,
            "latency_ms": 5,
            "enforcement_mode": "active",
        }

    return fake


def test_run_entry_veto_off_bypasses_reviewer(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "off")
    calls: list[dict] = []
    svc = _make_service(signals=[_sig("RELIANCE")], signal_reviewer=_fake_reviewer({}, calls))
    placed = svc.run_entry()
    assert len(placed) == 1
    assert calls == []  # reviewer never invoked in off mode


def test_run_entry_no_reviewer_injected_skips_veto(monkeypatch):
    """Default construction (signal_reviewer=None) never reviews — mirrors the
    data_health_checker pattern so existing behavior/tests are untouched."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    svc = _make_service(signals=[_sig("RELIANCE")])
    placed = svc.run_entry()
    assert len(placed) == 1


def test_run_entry_active_skip_blocks_lot_and_does_not_consume_cap(monkeypatch):
    """An enforcing 'skip' drops that lot only — the margin cap slot stays free
    for later signals, the skip is journalled, and no phantom position exists."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    calls: list[dict] = []
    # Cap = 50% of ₹10L = ₹5L = 2 lots. Three signals; the FIRST is vetoed.
    # If the veto consumed the cap, only one of B/C would fit — both must fill.
    svc = _make_service(
        signals=[_sig("AAA", vol=3.0), _sig("BBB", vol=2.0), _sig("CCC", vol=1.5)],
        signal_reviewer=_fake_reviewer({"AAA": "skip"}, calls),
    )
    placed = svc.run_entry()

    assert [p["signal_symbol"] for p in placed] == ["BBB", "CCC"]
    assert svc.lots_held() == 2
    assert len(calls) == 3  # every in-cap signal was reviewed
    # The veto skip is journalled with status='veto_skip' and no margin.
    veto_rows = [j for j in svc._test_journal if j.get("status") == "veto_skip"]
    assert len(veto_rows) == 1
    assert veto_rows[0]["signal_id"] == "AAA"
    assert veto_rows[0]["margin_inr"] == 0.0
    assert veto_rows[0]["order_id"] is None
    # The veto_skip row links back to its signal_decision audit row (#358).
    assert veto_rows[0]["decision_id"] == 1
    # Placed entries carry their reviewer's decision id too (reviewer stub
    # returns id=call-ordinal: AAA=1, BBB=2, CCC=3).
    placed_rows = [j for j in svc._test_journal if j.get("status") == "placed"]
    assert sorted(r["decision_id"] for r in placed_rows) == [2, 3]
    # No phantom position for the vetoed signal.
    assert all(p.signal_symbol != "AAA" for p in svc.paper_book.values())
    # And only two orders reached the placer.
    assert len(svc._test_placed) == 2


def test_run_entry_shadow_logs_but_places_anyway(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
    calls: list[dict] = []
    svc = _make_service(
        signals=[_sig("RELIANCE")],
        signal_reviewer=_fake_reviewer({"RELIANCE": "skip"}, calls),
    )
    placed = svc.run_entry()
    assert len(placed) == 1  # skip verdict NOT enforced in shadow
    assert len(calls) == 1  # but the reviewer ran and the decision is recorded
    assert not any(j.get("status") == "veto_skip" for j in svc._test_journal)


def test_run_entry_reviewer_failure_fails_open(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "active")

    def boom(**kwargs):
        raise RuntimeError("reviewer infrastructure down")

    svc = _make_service(signals=[_sig("RELIANCE")], signal_reviewer=boom)
    placed = svc.run_entry()
    assert len(placed) == 1  # failsafe philosophy: any reviewer failure → take


def test_run_entry_budget_zero_places_all_unreviewed(monkeypatch):
    """R3: once the cumulative review budget is exhausted the remaining signals
    are placed UNREVIEWED (fail-open)."""
    import services.futures_follow_service as ffs

    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    monkeypatch.setattr(ffs, "VETO_REVIEW_BUDGET_SECONDS", 0.0)
    calls: list[dict] = []
    svc = _make_service(
        signals=[_sig("AAA"), _sig("BBB")],
        signal_reviewer=_fake_reviewer({"AAA": "skip", "BBB": "skip"}, calls),
    )
    placed = svc.run_entry()
    assert len(placed) == 2  # both placed despite skip verdicts — never reviewed
    assert calls == []


def test_run_entry_budget_exhausts_mid_batch(monkeypatch):
    """R3: reviews that consume the 180s budget stop mid-batch; the rest place
    unreviewed. Fake time makes each review 'cost' 100s."""
    import services.futures_follow_service as ffs

    monkeypatch.setenv("VETO_LAYER_MODE", "active")

    class FakeTime:
        _t = 0.0

        @classmethod
        def monotonic(cls):
            cls._t += 100.0  # each call advances 100s → one review = 100s
            return cls._t

    monkeypatch.setattr(ffs, "time", FakeTime)
    calls: list[dict] = []
    # ₹20L capital → cap ₹10L → 4 lots, so all 3 signals fit under the cap.
    svc = _make_service(
        signals=[_sig("AAA"), _sig("BBB"), _sig("CCC")],
        signal_reviewer=_fake_reviewer({}, calls),
        capital_inr=2_000_000.0,
    )
    placed = svc.run_entry()
    # Review 1 (AAA): elapsed 0 < 180 → reviewed (now 100s).
    # Review 2 (BBB): elapsed 100 < 180 → reviewed (now 200s).
    # Review 3 (CCC): elapsed 200 >= 180 → UNREVIEWED, placed anyway.
    assert len(calls) == 2
    assert [c["symbol"] for c in calls] == ["AAA", "BBB"]
    assert len(placed) == 3


def test_run_entry_marks_actually_taken(monkeypatch):
    """mark_actually_taken records True after placement, False on a veto skip."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")

    marked: list[tuple] = []
    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "mark_actually_taken", lambda did, taken: marked.append((did, taken)))

    calls: list[dict] = []
    svc = _make_service(
        signals=[_sig("AAA", vol=3.0), _sig("BBB", vol=2.0)],
        signal_reviewer=_fake_reviewer({"AAA": "skip"}, calls),
    )
    svc.run_entry()
    # AAA (decision id 1) vetoed → False; BBB (id 2) placed → True.
    assert (1, False) in marked
    assert (2, True) in marked


def test_run_entry_reviewer_passes_strategy_identity_and_context(monkeypatch):
    """The review call carries source/strategy_name='futures_follow_cap50',
    direction='BUY', and a combined context (signal metrics + contract + book)."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    calls: list[dict] = []
    svc = _make_service(
        signals=[_sig("RELIANCE", vol=2.5)],
        signal_reviewer=_fake_reviewer({}, calls),
        market_context_provider=lambda: {"nifty_pct": 0.4, "india_vix": 13.0},
    )
    svc.run_entry()

    assert len(calls) == 1
    kw = calls[0]
    assert kw["symbol"] == "RELIANCE"
    assert kw["source"] == "futures_follow_cap50"
    assert kw["strategy_name"] == "futures_follow_cap50"
    assert kw["direction"] == "BUY"
    ctx = kw["context"]
    assert ctx["vol_ratio"] == 2.5
    assert ctx["stock_ret"] == 0.01
    assert ctx["sector_ret"] == 0.02
    assert ctx["contract_symbol"] == _CONTRACT["symbol"]
    assert ctx["margin_cap_inr"] == 500_000.0
    assert ctx["kill_switch_active"] is False
    assert ctx["nifty_pct"] == 0.4  # injected market context merged in


def test_run_entry_kill_switch_skips_review_entirely(monkeypatch):
    """With the kill switch armed no LLM call is burned (place_entry refuses
    the order anyway)."""
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    calls: list[dict] = []
    svc = _make_service(signals=[_sig("RELIANCE")], signal_reviewer=_fake_reviewer({}, calls))
    svc.kill_switch_active = True
    placed = svc.run_entry()
    assert placed == []
    assert calls == []


# --------------------------------------------------------------------------- #
# #334 — watchdog moved to 15:28 (after the 15:25 primary exit)
# --------------------------------------------------------------------------- #


def test_register_jobs_watchdog_at_1528():
    """The EOD watchdog cron is registered at 15:28 IST (post-primary-exit
    backstop), not the old 15:14 inherited from the simplified engine's MIS
    constraint. The simplified engine's own watchdog is untouched by #334."""
    jobs: dict[str, tuple] = {}

    class FakeScheduler:
        def add_job(self, fn, trigger, id, replace_existing, name):
            jobs[id] = (trigger, name)

    svc = _make_smoke_service()
    svc.strategy_id = 77
    svc.register_jobs(FakeScheduler())

    trigger, name = jobs["futures_follow_eod_watchdog"]
    assert "15:28" in name
    assert "minute='28'" in str(trigger) and "hour='15'" in str(trigger)
    # Ordering sanity: primary exit stays at 15:25, before the watchdog.
    exit_trigger, _exit_name = jobs["futures_follow_exit"]
    assert "minute='25'" in str(exit_trigger)


def test_watchdog_finds_nothing_after_successful_primary_exit():
    """#334 AC-2a: on a normal day the 15:25 primary exit squares off the T+1
    position; the 15:28 watchdog then finds an empty book and does nothing —
    the primary exit is primary again."""
    svc = _make_service()
    _seed_position(svc, "P1", entry_date="2026-06-09")

    exited = svc.run_exit()  # 15:25 primary
    assert len(exited) == 1
    assert svc.paper_book == {}

    flattened = svc.run_eod_watchdog()  # 15:28 backstop
    assert flattened == []


def test_watchdog_flattens_position_left_by_rejected_primary_exit():
    """#334 AC-2b: a REJECTED 15:25 exit keeps the position in the book; the
    15:28 watchdog retries and flattens it before the 15:30 close."""
    sell_attempts: list[dict] = []

    def flaky_placer(mode, order):
        if order["action"] != "SELL":
            return {"status": "success", "orderid": "OID-BUY"}
        sell_attempts.append(order)
        if len(sell_attempts) == 1:
            return {"status": "error", "message": "exchange rejected"}
        return {"status": "success", "orderid": "OID-RETRY"}

    svc = _make_service(order_placer=flaky_placer)
    _seed_position(svc, "P1", entry_date="2026-06-09")

    exited = svc.run_exit()  # 15:25 primary — SELL rejected
    assert len(exited) == 1  # journalled as rejected
    assert len(svc.paper_book) == 1, "rejected exit must stay in book for the retry"

    flattened = svc.run_eod_watchdog()  # 15:28 retry backstop
    assert len(flattened) == 1
    assert svc.paper_book == {}
    assert len(sell_attempts) == 2  # one rejected attempt + one successful retry


# --------------------------------------------------------------------------- #
# Issue #352 — entry-evaluation breakdown snapshot
# --------------------------------------------------------------------------- #
@pytest.fixture
def _isolated_eval_db(monkeypatch):
    """Rebind database.futures_follow_eval_db to a fresh in-memory DB per test so
    snapshot writes never touch the live openalgo.db and never leak across tests."""
    from database import futures_follow_eval_db as eval_db

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(eval_db, "engine", eng)
    monkeypatch.setattr(eval_db, "db_session", sess)
    eval_db.Base.query = sess.query_property()
    eval_db.Base.metadata.create_all(eng)
    yield eval_db
    sess.remove()
    eng.dispose()


def _fake_details(symbols_meta: dict) -> callable:
    """Build a fake ``signal_evaluator_details`` returning a fixed breakdown.

    ``symbols_meta``: {symbol: {sector_ret, stock_ret, vol_ratio, passed,
    fail_reason, intraday_source}}.
    """

    def _details(as_of=None):
        n_by_source = {"quotes": 0, "aggregator": 0, "historify": 0, "none": 0}
        rows = []
        for sym, meta in symbols_meta.items():
            src = meta.get("intraday_source", "quotes")
            if src in n_by_source:
                n_by_source[src] += 1
            rows.append(
                {
                    "symbol": sym,
                    "sector_index": meta.get("sector_index", "NIFTY"),
                    "sector_ret": meta.get("sector_ret"),
                    "stock_ret": meta.get("stock_ret"),
                    "vol_ratio": meta.get("vol_ratio"),
                    "current_price": meta.get("current_price", 100.0),
                    "intraday_source": src,
                    "passed": meta.get("passed", False),
                    "fail_reason": meta.get("fail_reason"),
                }
            )
        return {"n_by_source": n_by_source, "symbols": rows}

    return _details


def test_run_entry_persists_snapshot_with_known_metrics(_isolated_eval_db):
    """run_entry captures + persists a snapshot row with the injected evaluator's
    per-symbol metrics — the WHY-zero-signals breakdown the operator reads."""
    details_fn = _fake_details(
        {
            "RELIANCE": {
                "sector_ret": 0.02,
                "stock_ret": 0.01,
                "vol_ratio": 1.5,
                "passed": True,
            },
            "TCS": {
                "sector_ret": 0.003,
                "stock_ret": 0.01,
                "vol_ratio": 1.5,
                "passed": False,
                "fail_reason": "sector 0.30% <= 1.0%",
            },
            "INFY": {
                "sector_ret": None,
                "stock_ret": None,
                "vol_ratio": None,
                "passed": False,
                "fail_reason": "None data [sector_ret, stock_ret, vol_ratio] (src=none)",
            },
        }
    )
    svc = _make_service(signals=[_sig("RELIANCE")], signal_evaluator_details=details_fn)

    placed = svc.run_entry()
    assert len(placed) == 1  # sanity — entries still place normally

    row = _isolated_eval_db.get_snapshot("futures_follow_cap50", "2026-06-10")
    assert row is not None
    payload = row["payload"]
    assert payload["n_signals"] == 1
    by_symbol = {s["symbol"]: s for s in payload["symbols"]}
    assert by_symbol["RELIANCE"]["outcome"] == "in_cap_placed"
    assert by_symbol["TCS"]["outcome"] == "first_failed_gate"
    assert by_symbol["INFY"]["outcome"] == "missing_data"
    assert payload["per_gate_fail_counts"]["sector"] == 1
    assert payload["per_gate_fail_counts"]["missing_data"] == 1


def test_run_entry_persist_failure_does_not_break_entry_placement(_isolated_eval_db, monkeypatch):
    """A persist failure in the breakdown capture must never affect the entries
    already placed — run_entry wraps the capture in a try/except."""

    def _raising_details(as_of=None):
        raise RuntimeError("boom — details provider exploded")

    svc = _make_service(
        signals=[_sig("RELIANCE")],
        signal_evaluator_details=_raising_details,
    )
    placed = svc.run_entry()
    assert len(placed) == 1  # entry placement unaffected by the capture failure
    assert _isolated_eval_db.get_snapshot("futures_follow_cap50", "2026-06-10") is None


def test_run_entry_no_evaluator_details_injected_is_a_noop(_isolated_eval_db):
    """The default (no signal_evaluator_details injected, as in every other
    existing test) must not attempt any DB write — purely additive capture."""
    svc = _make_service(signals=[_sig("RELIANCE")])
    placed = svc.run_entry()
    assert len(placed) == 1
    assert _isolated_eval_db.get_snapshot("futures_follow_cap50", "2026-06-10") is None


def test_run_entry_snapshot_idempotent_on_rerun(_isolated_eval_db):
    """Re-running run_entry for the same trading day overwrites the row rather
    than creating a duplicate (idempotent upsert per (strategy, date))."""
    details_fn = _fake_details(
        {"RELIANCE": {"sector_ret": 0.02, "stock_ret": 0.01, "vol_ratio": 1.5, "passed": True}}
    )
    svc = _make_service(signals=[_sig("RELIANCE")], signal_evaluator_details=details_fn)
    svc.run_entry()
    svc.run_entry()  # second run same day (e.g. a scheduler retry)

    from database.futures_follow_eval_db import FuturesFollowEvalSnapshot

    rows = _isolated_eval_db.db_session.query(FuturesFollowEvalSnapshot).all()
    assert len(rows) == 1
    _isolated_eval_db.db_session.remove()


def test_entry_breakdown_snapshot_none_when_not_yet_evaluated(_isolated_eval_db):
    """No evaluation recorded yet for a given date -> get_snapshot returns None
    (the endpoint's contract: data=null means 'no evaluation recorded yet')."""
    assert _isolated_eval_db.get_snapshot("futures_follow_cap50", "2026-01-01") is None


# --------------------------------------------------------------------------- #
# Big-loss news-context alert (issue #399) — informational, human-in-the-loop
# --------------------------------------------------------------------------- #
def test_big_loss_alert_fires_with_news_on_large_t1_loss():
    alerts = []
    svc = _make_service(
        mode="sandbox",
        price_fetcher=lambda s, e: 23_400.0,  # -600 pts vs 24000 entry -> ~-45k
        notifier=lambda m: alerts.append(m),
        news_context_provider=lambda: "📰 Recent headlines:\n⚠️ [et] War fears hit markets",
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    svc.run_exit()
    big = [a for a in alerts if "BIG LOSS" in a]
    assert len(big) == 1
    assert "War fears" in big[0]  # news context attached
    assert "no auto-action" in big[0].lower()  # human-in-the-loop framing


def test_big_loss_alert_not_fired_on_small_loss():
    alerts = []
    svc = _make_service(
        price_fetcher=lambda s, e: 23_990.0,  # -10 pts -> tiny loss, below 2%
        notifier=lambda m: alerts.append(m),
        news_context_provider=lambda: "NEWS",
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    svc.run_exit()
    assert not any("BIG LOSS" in a for a in alerts)


def test_big_loss_alert_dedups_per_day_and_resets():
    alerts = []
    svc = _make_service(
        price_fetcher=lambda s, e: 23_400.0,
        notifier=lambda m: alerts.append(m),
        news_context_provider=lambda: "N",
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    svc.run_exit()
    _seed_position(svc, "P2", entry_date="2026-06-09")
    svc.run_exit()  # same day -> deduped
    assert len([a for a in alerts if "BIG LOSS" in a]) == 1
    svc.reset_daily_state()
    _seed_position(svc, "P3", entry_date="2026-06-09")
    svc.run_exit()  # after reset -> fires again
    assert len([a for a in alerts if "BIG LOSS" in a]) == 2


def test_big_loss_alert_places_no_extra_order():
    """The alert is informational — it must NOT place or cancel any order."""
    svc = _make_service(
        price_fetcher=lambda s, e: 23_400.0,
        news_context_provider=lambda: "N",
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    n_before = len(svc._test_placed)
    svc.run_exit()
    assert len(svc._test_placed) == n_before + 1  # only the T+1 SELL, none from the alert


def test_kill_switch_alert_includes_news_context():
    alerts = []
    svc = _make_service(
        notifier=lambda m: alerts.append(m),
        news_context_provider=lambda: "📰 headline XYZ",
    )
    svc.update_daily_pnl(realized_today=-30_001.0, open_mtm=0.0)
    ks = [a for a in alerts if "kill switch" in a]
    assert len(ks) == 1
    assert "headline XYZ" in ks[0]


def test_big_loss_alert_survives_news_provider_failure():
    alerts = []

    def boom():
        raise RuntimeError("feed down")

    svc = _make_service(
        price_fetcher=lambda s, e: 23_400.0,
        notifier=lambda m: alerts.append(m),
        news_context_provider=boom,
    )
    _seed_position(svc, "P1", entry_date="2026-06-09")
    svc.run_exit()  # must not raise
    assert any("BIG LOSS" in a for a in alerts)  # alert still sent, without news


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
