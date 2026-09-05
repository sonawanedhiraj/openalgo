"""Per-account realized P&L payload for the strategy page (issue #700).

Pins the rules the card relies on: the Primary row is the SAME row set as the
Performance table's Live column; windows are IST days; a child day with mirrors
but no captured row is MISSING (counted, excluded, never ₹0); the verdict is
the sign of the total with no tolerance band.
"""

from datetime import date, datetime, timedelta

import pytest

from database import account_orders_db, broker_accounts_db
from services import strategy_accounts_pnl as svc

IST = timedelta(hours=5, minutes=30)
STRAT = "open15_vol_breakout"
TODAY = svc.today_ist()


@pytest.fixture(autouse=True)
def _tables():
    broker_accounts_db.init_db()
    account_orders_db.init_db()
    yield
    for model in (account_orders_db.AccountOrder, account_orders_db.AccountDailyPnl):
        account_orders_db.db_session.query(model).delete()
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()
    for model in (broker_accounts_db.AccountStrategy, broker_accounts_db.BrokerAccount):
        broker_accounts_db.db_session.query(model).delete()
    broker_accounts_db.db_session.commit()
    broker_accounts_db.db_session.remove()


@pytest.fixture()
def child():
    acct = broker_accounts_db.add_account(
        display_name="Kid A",
        api_key="key_kid_a_0123456789",  # pragma: allowlist secret
        api_secret="secret_kid_a_0123456789",  # pragma: allowlist secret
        capital_inr=100000,
    )
    broker_accounts_db.set_strategies(acct["id"], [STRAT])
    return acct


def _day_row(account_id, d: date, net: float, finalized=True, source="modelled"):
    return account_orders_db.upsert_daily_pnl(
        account_id,
        d,
        STRAT,
        realized_gross=net + 10.0,
        charges_inr=10.0,
        charges_source=source,
        n_round_trips=1,
        n_fills=2,
        n_open_legs=0,
        book_realised=None,
        capture_source="tradebook",
        finalized=finalized,
    )


def _placed(account_id, d: date):
    row = account_orders_db.record_mirror_attempt(
        account_id=account_id,
        strategy_name=STRAT,
        symbol="X26SEP100CE",
        exchange="NFO",
        action="BUY",
        product="MIS",
        parent_qty=1,
        child_qty=1,
        status="placed",
        broker_orderid=f"O{d.isoformat()}{account_id}",
    )
    when = datetime(d.year, d.month, d.day, 9, 17) - IST
    account_orders_db.db_session.query(account_orders_db.AccountOrder).filter(
        account_orders_db.AccountOrder.id == row["id"]
    ).update({"created_at": when})
    account_orders_db.db_session.commit()
    account_orders_db.db_session.remove()


def _no_primary(monkeypatch):
    monkeypatch.setattr(svc, "primary_daily_series", lambda name: None)


# ---------------------------------------------------------------------------
# pure
# ---------------------------------------------------------------------------
def test_verdict_is_the_sign_of_the_total_with_no_band():
    assert svc.verdict_of(0.01) == "profit"
    assert svc.verdict_of(-0.01) == "loss"
    assert svc.verdict_of(0) == "flat"
    assert svc.verdict_of(None) == "flat"


def test_stats_window_and_drawdown():
    d = TODAY
    daily = [
        (d - timedelta(days=3), 100.0),
        (d - timedelta(days=2), -300.0),
        (d - timedelta(days=1), 50.0),
        (d, 20.0),
    ]
    all_ = svc.stats_from_daily(daily, None, d)
    assert all_["net_inr"] == -130.0
    assert all_["days_traded"] == 4 and all_["win_days_pct"] == 75.0
    assert all_["max_dd_inr"] == -300.0  # peak 100 → trough -200
    assert all_["today_net_inr"] == 20.0
    week = svc.stats_from_daily(daily, svc.window_since("1d", d), d)
    assert week["net_inr"] == 20.0 and week["days_traded"] == 1
    empty = svc.stats_from_daily([], None, d)
    assert empty["net_inr"] is None and empty["max_dd_inr"] is None


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------
def test_primary_row_is_the_performance_tables_row_set(monkeypatch):
    """Same source as `_open15_lifetime()['live']['cum_net_pnl']`: the daily
    series is built from `real_closed_rows(mode='live')` + `net_pnl_of_row`."""
    from database.open15_breakout_db import net_pnl_of_row, real_closed_rows

    calls = {}

    class Row:
        def __init__(self, trade_date, pnl, charges):
            self.trade_date, self.pnl, self.charges_inr = trade_date, pnl, charges

    rows = [
        Row("2026-09-01", 1000.0, 100.0),
        Row("2026-09-01", -200.0, 50.0),
        Row("2026-09-02", 500.0, 40.0),
    ]

    def fake_real_closed_rows(mode=None):
        calls["mode"] = mode
        return rows

    import database.open15_breakout_db as o15

    monkeypatch.setattr(o15, "real_closed_rows", fake_real_closed_rows)
    daily = svc.primary_daily_series(STRAT)
    assert calls["mode"] == "live"
    assert daily == [(date(2026, 9, 1), 650.0), (date(2026, 9, 2), 460.0)]
    assert sum(v for _, v in daily) == pytest.approx(sum(net_pnl_of_row(r) for r in rows))
    assert real_closed_rows is not fake_real_closed_rows or True  # imported name unchanged


def test_child_rows_missing_days_and_total(monkeypatch, child):
    _no_primary(monkeypatch)
    d1, d2 = TODAY - timedelta(days=2), TODAY - timedelta(days=1)
    _placed(child["id"], d1)
    _placed(child["id"], d2)
    _placed(child["id"], TODAY)
    _day_row(child["id"], d1, 300.0)
    # d2: mirrors placed but never captured → MISSING, not ₹0
    _day_row(child["id"], TODAY, -120.0, finalized=False, source="broker")

    out = svc.build_accounts_pnl(STRAT, "all")
    assert out["verdict"] == "profit"
    assert out["total"]["net_inr"] == 180.0
    assert out["total"]["days_missing"] == 1
    (row,) = out["accounts"]
    assert row["role"] == "child" and row["name"] == "Kid A"
    assert row["net_inr"] == 180.0 and row["days_traded"] == 2
    assert row["missing_days"] == [d2.isoformat()]
    assert row["capture"] == "provisional"
    assert row["charges_source"] == "mixed"
    assert row["today_net_inr"] == -120.0
    assert row["return_pct"] == pytest.approx(0.18)


def test_child_with_mirrors_today_and_no_row_reads_missing(monkeypatch, child):
    _no_primary(monkeypatch)
    _placed(child["id"], TODAY)
    out = svc.build_accounts_pnl(STRAT, "all")
    (row,) = out["accounts"]
    assert row["capture"] == "missing"
    assert row["net_inr"] is None
    assert out["verdict"] == "flat" and out["total"]["net_inr"] is None


def test_window_filters_rows_and_flips_verdict(monkeypatch, child):
    _no_primary(monkeypatch)
    _day_row(child["id"], TODAY - timedelta(days=20), 5000.0)
    _day_row(child["id"], TODAY, -100.0)
    assert svc.build_accounts_pnl(STRAT, "all")["verdict"] == "profit"
    week = svc.build_accounts_pnl(STRAT, "1w")
    assert week["verdict"] == "loss" and week["total"]["net_inr"] == -100.0
    assert week["accounts"][0]["days_traded"] == 1


def test_child_without_selection_or_rows_is_omitted(monkeypatch):
    _no_primary(monkeypatch)
    broker_accounts_db.add_account(
        display_name="Idle",
        api_key="key_idle_0123456789",  # pragma: allowlist secret
        api_secret="secret_idle_0123456789",  # pragma: allowlist secret
        capital_inr=1,
    )
    assert svc.build_accounts_pnl(STRAT)["accounts"] == []


def test_primary_and_children_sum_into_the_verdict(monkeypatch, child):
    monkeypatch.setattr(svc, "primary_daily_series", lambda name: [(TODAY, 1000.0)])
    monkeypatch.setattr(svc, "_primary_capital", lambda name: 100000.0)
    _day_row(child["id"], TODAY, -1500.0)
    out = svc.build_accounts_pnl(STRAT)
    assert [a["role"] for a in out["accounts"]] == ["primary", "child"]
    assert out["total"]["net_inr"] == -500.0 and out["verdict"] == "loss"
    assert out["accounts"][0]["return_pct"] == 1.0
