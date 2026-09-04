"""Intra-hold P&L curve + live P&L (issue #692).

The fixture numbers are 2026-09-01's real DIVISLAB fill, so the math here is
pinned to a day that actually happened: entry fill 183.52 x 300, minute closes
09:19-09:29, broker exit fill => journal gross +2940 / charges 552.54.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytz

from database import open15_breakout_db as o15db
from services import open15_pnl_curve as curve

IST = pytz.timezone("Asia/Kolkata")

DATE = "2026-09-01"
LATER = "2026-09-02"  # a day after DATE, so DATE reads as a settled past day


@pytest.fixture(autouse=True)
def _fresh():
    o15db.init_db()
    curve.clear_caches()
    yield
    try:
        o15db.db_session.query(o15db.Open15Trade).delete()
        o15db.db_session.commit()
    finally:
        o15db.db_session.remove()
    curve.clear_caches()


def _row(**kw):
    defaults = {
        "trade_date": DATE,
        "symbol": "DIVISLAB",
        "side": "S",
        "mode": "live",
        "instrument": "option",
        "opt_symbol": "DIVISLAB29SEP269200PE",
        "opt_lot_size": 100,
        "quantity": 300,
        "entry_fill_price": 183.52,
        "entry_fill_qty": 300,
        "trigger_minute": "09:18",
        "trigger_second": 38,
        "trigger_price": 9173.0,
        "opt_entry_premium": 182.7,
        "exit_ts": f"{DATE}T09:30:01+05:30",
        "status": "closed",
        "pnl": 2940.0,
        "charges_inr": 552.54,
        "fill": "real",
    }
    defaults.update(kw)
    r = o15db.Open15Trade(**defaults)
    o15db.db_session.add(r)
    o15db.db_session.commit()
    o15db.db_session.remove()
    return defaults


def _bars(closes: dict[str, float], date: str = DATE) -> list[dict]:
    y, m, d = (int(x) for x in date.split("-"))
    out = []
    for hhmm, close in closes.items():
        h, mi = (int(x) for x in hhmm.split(":"))
        ts = IST.localize(dt.datetime(y, m, d, h, mi))
        out.append({"timestamp": int(ts.timestamp()), "close": close})
    return out


DIVIS_CLOSES = {
    "09:19": 183.35,
    "09:20": 195.6,
    "09:21": 195.85,
    "09:22": 195.55,
    "09:23": 195.0,
    "09:24": 195.5,
    "09:25": 199.4,
    "09:26": 199.35,
    "09:27": 199.25,
    "09:28": 193.75,
    "09:29": 193.0,
    # bars past the exit exist on the broker side; the curve must ignore them
    "09:30": 195.0,
    "09:31": 194.0,
}


def _patch_bars(monkeypatch, closes_by_contract):
    calls = []

    def fake(symbol, trade_date, exchange="NFO"):
        calls.append((symbol, trade_date, exchange))
        v = closes_by_contract.get(symbol)
        return None if v is None else _bars(v, trade_date)

    monkeypatch.setattr(curve, "_fetch_bars", fake)
    return calls


def _as_past_day(monkeypatch):
    monkeypatch.setattr(curve, "_today_ist", lambda: LATER)


# ---------------------------------------------------------------------------
# curve math
# ---------------------------------------------------------------------------


def test_curve_matches_journal_and_minute_closes(monkeypatch):
    _row()
    _as_past_day(monkeypatch)
    _patch_bars(monkeypatch, {"DIVISLAB29SEP269200PE": DIVIS_CLOSES})

    j = curve.build_pnl_curve(DATE)
    assert j["status"] == "ok"
    (t,) = j["trades"]
    marks = dict(t["series"])
    # option rows are LONG premium whatever the stock side: MTM=(close-fill)*qty
    assert marks["09:19"] == pytest.approx((183.35 - 183.52) * 300, abs=0.01)
    assert marks["09:20"] == pytest.approx(3624.0, abs=0.01)
    assert marks["09:29"] == pytest.approx(2844.0, abs=0.01)
    # marks stop at the last full bar before the exit — never past it
    assert "09:30" not in marks and "09:31" not in marks
    # the final point is the journal's own gross pnl, not a re-derivation
    assert t["final"] == ["09:30", 2940.0]
    # net routes through net_pnl_of_row (#552)
    assert t["net_pnl"] == pytest.approx(2940.0 - 552.54)
    assert j["portfolio_final"] == ["09:30", 2940.0]
    assert j["net_total"] == pytest.approx(2387.46)


def test_real_only_and_error_rows_excluded(monkeypatch):
    _row()
    _row(symbol="SIMCO", fill="sim", quantity=0, sim_quantity=100)
    _row(symbol="SHADOWCO", fill="shadow")
    _row(symbol="PAPERCO", fill="paper")
    _row(symbol="ERRCO", status="error", fill=None)
    _as_past_day(monkeypatch)
    _patch_bars(monkeypatch, {"DIVISLAB29SEP269200PE": DIVIS_CLOSES})

    j = curve.build_pnl_curve(DATE)
    assert [t["symbol"] for t in j["trades"]] == ["DIVISLAB"]
    assert j["n_real"] == 1


def test_stock_short_sign(monkeypatch):
    _row(
        symbol="SBIN",
        instrument=None,  # pre-#437 rows read as stock
        opt_symbol=None,
        side="S",
        entry_fill_price=100.0,
        pnl=300.0,
        charges_inr=50.0,
    )
    _as_past_day(monkeypatch)
    calls = _patch_bars(monkeypatch, {"SBIN": {"09:19": 99.0}})

    j = curve.build_pnl_curve(DATE)
    (t,) = j["trades"]
    # short stock gains when price falls
    assert dict(t["series"])["09:19"] == pytest.approx((100.0 - 99.0) * 300)
    # stock rows fetch NSE bars, not NFO
    assert calls == [("SBIN", DATE, "NSE")]


def test_unavailable_bars_degrade_not_raise(monkeypatch):
    _row()
    _as_past_day(monkeypatch)
    _patch_bars(monkeypatch, {"DIVISLAB29SEP269200PE": None})

    j = curve.build_pnl_curve(DATE)
    assert j["status"] == "ok"
    (t,) = j["trades"]
    assert t["unavailable"] is True and t["reason"]
    assert j["n_unavailable"] == 1


def test_settled_day_cached_open_day_ttl_cached(monkeypatch):
    _row()
    _as_past_day(monkeypatch)
    calls = _patch_bars(monkeypatch, {"DIVISLAB29SEP269200PE": DIVIS_CLOSES})

    curve.build_pnl_curve(DATE)
    curve.build_pnl_curve(DATE)
    assert len(calls) == 1  # settled past day: permanent cache

    # an OPEN day is never frozen permanently, but the 5s page refresh must
    # not multiply broker history calls either — short TTL
    curve.clear_caches()
    o15db.db_session.query(o15db.Open15Trade).update({"status": "open", "exit_ts": None})
    o15db.db_session.commit()
    o15db.db_session.remove()
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    calls.clear()
    curve.build_pnl_curve(DATE)
    curve.build_pnl_curve(DATE)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# poll-interval config
# ---------------------------------------------------------------------------


def test_clamp_live_poll_interval():
    # floor is 2 s since issue #698 (was 3)
    assert curve.clamp_live_poll_interval(1) == 2
    assert curve.clamp_live_poll_interval(2) == 2
    assert curve.clamp_live_poll_interval(999) == 60
    assert curve.clamp_live_poll_interval("10") == 10
    assert curve.clamp_live_poll_interval("abc") == 5
    assert curve.clamp_live_poll_interval(None) == 5


def test_live_poll_clamp_report_describes_every_moved_value():
    # issue #698: the endpoint returns this so the page can SAY it clamped
    assert curve.live_poll_clamp_report(2) is None
    assert curve.live_poll_clamp_report("10") is None
    below = curve.live_poll_clamp_report(1)
    assert below == {"requested": 1, "applied": 2, "min": 2, "max": 60, "reason": "below_min"}
    above = curve.live_poll_clamp_report("999")
    assert above["applied"] == 60 and above["reason"] == "above_max"
    bad = curve.live_poll_clamp_report("abc")
    assert bad["applied"] == 5 and bad["reason"] == "not_a_number"


def test_resolve_live_poll_interval_db_row_beats_env(monkeypatch):
    monkeypatch.setenv("OPEN15_LIVE_POLL_S", "7")
    assert curve.resolve_live_poll_interval() == 7
    assert o15db.save_config(None, None, None, live_poll_interval_s=12)
    assert curve.resolve_live_poll_interval() == 12


# ---------------------------------------------------------------------------
# live P&L
# ---------------------------------------------------------------------------


def _patch_quotes(monkeypatch, ltps):
    calls = []

    def fake(contracts):
        calls.append(list(contracts))
        return ltps

    monkeypatch.setattr(curve, "_batched_ltp", fake)
    return calls


def test_live_pnl_marks_open_trades_one_batched_call(monkeypatch):
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    _row(status="open", exit_ts=None, pnl=None, charges_inr=None)
    _row(
        symbol="HEROMOTOCO",
        side="L",
        opt_symbol="HEROMOTOCO29SEP265600CE",
        entry_fill_price=184.03,
        status="open",
        exit_ts=None,
        pnl=None,
        charges_inr=None,
    )
    calls = _patch_quotes(
        monkeypatch,
        {"DIVISLAB29SEP269200PE": 199.35, "HEROMOTOCO29SEP265600CE": 159.0},
    )

    j = curve.live_pnl()
    assert j["status"] == "live"
    by_sym = {t["symbol"]: t for t in j["trades"]}
    assert by_sym["DIVISLAB"]["mtm"] == pytest.approx((199.35 - 183.52) * 300)
    assert by_sym["HEROMOTOCO"]["mtm"] == pytest.approx((159.0 - 184.03) * 300)
    assert j["portfolio_mtm"] == pytest.approx(4749.0 - 7509.0)
    assert j["poll_interval_s"] == curve.resolve_live_poll_interval()

    # a second call inside the TTL is served from cache — one broker call total
    j2 = curve.live_pnl()
    assert j2 is j or j2 == j
    assert len(calls) == 1


def test_live_pnl_closed_and_idle(monkeypatch):
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    assert curve.live_pnl()["status"] == "idle"
    curve.clear_caches()
    _row()  # closed real row
    assert curve.live_pnl()["status"] == "closed"


def test_live_pnl_unreadable_quotes_degrade(monkeypatch):
    monkeypatch.setattr(curve, "_today_ist", lambda: DATE)
    _row(status="open", exit_ts=None, pnl=None, charges_inr=None)
    monkeypatch.setattr(curve, "_batched_ltp", lambda contracts: None)

    j = curve.live_pnl()
    assert j["status"] == "live"
    assert j["quotes_ok"] is False
    assert j["trades"][0]["mtm"] is None
