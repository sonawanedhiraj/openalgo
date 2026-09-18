"""Unit + end-to-end tests for services/cas_straddle_service.py (issue #740).

Every external effect (expiry list, strikes, contracts, quotes, orders, fills,
position book, mode, notifications, config, clock) is injected with fakes, so
these run with no broker and no live DB. The end-to-end tests rebind the
journal module to an in-memory SQLite so the journal rows are asserted too.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import restx_api  # noqa: F401  (imported first, as app.py does — place_order_service ↔ restx_api cycle)
from database import cas_straddle_db as db
from services import cas_straddle_service as cs
from services.cas_straddle_service import (
    DEFAULTS,
    STRATEGY_NAME,
    CasStraddleService,
    atm_from_strikes,
    combined_target_reached,
    expiry_to_ddmmmyy,
    is_expiry_today,
    ladder_strikes,
    option_leg_charges,
    parse_expiry,
    resolve_config,
    session_digest,
    validate_config,
)

_IST = timezone(timedelta(hours=5, minutes=30))
TODAY = date(2026, 9, 22)  # a NIFTY Tuesday


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _isolate_runtime_override(monkeypatch):
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


@pytest.fixture
def journal(monkeypatch):
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(db, "engine", eng)
    monkeypatch.setattr(db, "db_session", sess)
    db.Base.query = sess.query_property()
    db.Base.metadata.create_all(eng)
    yield db
    sess.remove()
    eng.dispose()


class Clock:
    def __init__(self, h=15, m=12, s=0, d=TODAY):
        self.dt = datetime(d.year, d.month, d.day, h, m, s, tzinfo=_IST)

    def now(self):
        return self.dt

    def set(self, h, m, s=0):
        self.dt = self.dt.replace(hour=h, minute=m, second=s)


class FakeScheduler:
    def __init__(self):
        self.jobs = {}

    def add_job(self, fn, trigger=None, id=None, replace_existing=False, name=None):
        self.jobs[id] = {"fn": fn, "trigger": trigger, "name": name}


class Harness:
    """All fakes in one place; every test drives the same shape."""

    def __init__(self, *, expiry_today=("NIFTY",), mode="sandbox", config=None, clock=None):
        self.clock = clock or Clock()
        self.mode = mode
        self.config = config or {}
        self.expiries = {
            "NIFTY": ["22-SEP-26", "29-SEP-26", "06-OCT-26"],
            "SENSEX": ["24-SEP-26", "01-OCT-26"],
        }
        if "SENSEX" in expiry_today:
            self.expiries["SENSEX"] = ["22-SEP-26", "24-SEP-26"]
        if "NIFTY" not in expiry_today:
            self.expiries["NIFTY"] = ["29-SEP-26", "06-OCT-26"]
        self.quotes: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.reject_symbols: set[str] = set()
        self.post_ack_reject: set[str] = set()
        self.fill_pending: set[str] = set()
        self.book: list[dict] | None = []
        self.notes: list[str] = []
        self.trading_day = True
        self.session_ok = True
        self.sched = FakeScheduler()
        self._oid = 0

    # --- fakes ---
    def expiry_lister(self, u, exch):
        return list(self.expiries[u])

    def strike_lister(self, u, expiry, side, exch):
        if u == "NIFTY":
            return [float(k) for k in range(22800, 23601, 50)]
        return [float(k) for k in range(73000, 76001, 100)]

    def contract_finder(self, u, expiry, strike, side, exch):
        lot = 65 if u == "NIFTY" else 20
        return {
            "symbol": f"{u}{expiry_to_ddmmmyy(expiry)}{int(strike)}{side}",
            "exchange": exch,
            "strike": float(strike),
            "expiry": expiry,
            "lotsize": lot,
            "tick_size": 0.05,
            "side": side,
        }

    def quote_batch(self, instruments):
        return {s: dict(self.quotes[s]) for s, _ in instruments if s in self.quotes}

    def order_placer(self, order):
        rec = dict(order)
        self.orders.append(rec)
        order = rec
        if order["symbol"] in self.reject_symbols and order["action"] == "BUY":
            return {"status": "error", "message": "RMS:Margin Exceeds"}
        if order["symbol"] in self.reject_symbols and order["action"] == "SELL":
            return {"status": "error", "message": "exit refused"}
        self._oid += 1
        oid = f"O{self._oid}"
        order["_oid"] = oid
        return {"status": "success", "orderid": oid}

    def fill_reader(self, order_id):
        order = next((o for o in self.orders if o.get("_oid") == order_id), None)
        if order is None:
            return None
        if order["symbol"] in self.post_ack_reject and order["action"] == "BUY":
            return {"status": "rejected", "price": None, "qty": 0, "message": "post-ACK RMS reject"}
        if order["symbol"] in self.fill_pending:
            return {"status": "pending", "price": None, "qty": None, "message": None}
        q = self.quotes.get(order["symbol"], {})
        px = q.get("ask") if order["action"] == "BUY" else q.get("bid")
        return {
            "status": "filled",
            "price": float(px),
            "qty": int(order["quantity"]),
            "message": None,
        }

    def book_reader(self):
        return self.book

    def service(self, journal=False):
        return CasStraddleService(
            scheduler=self.sched,
            expiry_lister=self.expiry_lister,
            strike_lister=self.strike_lister,
            contract_finder=self.contract_finder,
            quote_batch=self.quote_batch,
            order_placer=self.order_placer,
            fill_reader=self.fill_reader,
            book_reader=self.book_reader,
            mode_resolver=lambda: self.mode,
            broker_session_checker=lambda: self.session_ok,
            notifier=self.notes.append,
            config_reader=lambda: dict(self.config),
            trading_day_checker=lambda d: self.trading_day,
            now=self.clock.now,
            sleep=lambda s: None,
            journal=journal,
        )

    # --- helpers ---
    def set_spot(self, u, px):
        self.quotes[u] = {"ltp": px, "bid": None, "ask": None, "volume": None, "oi": None}

    def set_opt(self, sym, bid, ask, ltp=None):
        self.quotes[sym] = {"ltp": ltp or (bid + ask) / 2 if bid and ask else ltp,
                            "bid": bid, "ask": ask, "volume": 100, "oi": 5000}  # fmt: skip


def _arm_nifty(h: Harness, svc, spot=23172.35):
    """Arm + a 15:14:58 poll (continuous print) + a 15:15:01 poll (ATM fixed)."""
    h.set_spot("NIFTY", spot)
    h.clock.set(15, 12)
    out = svc.arm()
    assert out["armed"], out
    st = svc.day["u"]["NIFTY"]
    for c in list(st["ladder"].values()):
        h.set_opt(c["symbol"], 90.0, 92.0)
    h.clock.set(15, 14, 58)
    svc.poll_once()
    h.clock.set(15, 15, 1)
    svc.poll_once()
    return st


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def test_resolve_config_defaults_and_clamps():
    assert resolve_config(None) == DEFAULTS | {"trade_nifty": True, "trade_sensex": True}
    eff = resolve_config(
        {"lots_nifty": 99, "target_mult": 0.5, "hard_exit_time": "15:39", "poll_interval_s": 1,
         "trade_sensex": False, "max_premium_inr": 5}
    )  # fmt: skip
    assert eff["lots_nifty"] == 10 and eff["target_mult"] == 1.1
    assert eff["hard_exit_time"] == "15:37:00" and eff["poll_interval_s"] == 2
    assert eff["trade_sensex"] is False and eff["max_premium_inr"] == 1000.0
    # NULL falls through to the code default
    assert resolve_config({"lots_nifty": None})["lots_nifty"] == 1


def test_validate_config_refuses_rather_than_clamps():
    values, errors = validate_config(
        {"lots_nifty": 11, "target_mult": "abc", "hard_exit_time": "15:39:00",
         "poll_interval_s": 1, "trade_nifty": "maybe", "max_premium_inr": 10}
    )  # fmt: skip
    assert values == {}
    assert len(errors) == 6
    values, errors = validate_config(
        {"lots_nifty": "2", "target_mult": 2.5, "hard_exit_time": "15:29", "trade_sensex": 0,
         "poll_interval_s": None}
    )  # fmt: skip
    assert errors == []
    assert values == {
        "lots_nifty": 2, "target_mult": 2.5, "hard_exit_time": "15:29:00",
        "trade_sensex": False, "poll_interval_s": None,
    }  # fmt: skip


def test_expiry_parsing_and_expiry_day_from_master_contract():
    assert parse_expiry("22-SEP-26") == date(2026, 9, 22)
    assert parse_expiry("22-SEP-2026") == date(2026, 9, 22)
    assert parse_expiry("22SEP26") == date(2026, 9, 22)
    assert parse_expiry("garbage") is None
    assert expiry_to_ddmmmyy("22-SEP-26") == "22SEP26"
    # a holiday-shifted MONDAY expiry is an expiry day — no weekday logic
    assert is_expiry_today(["19-OCT-26", "27-OCT-26"], date(2026, 10, 19)) == (True, "19-OCT-26")
    assert is_expiry_today(["29-SEP-26"], TODAY) == (False, "29-SEP-26")
    assert is_expiry_today(["15-SEP-26"], TODAY) == (False, None)  # stale rows ignored
    assert is_expiry_today([], TODAY) == (False, None)


def test_atm_and_ladder_from_actual_strikes():
    strikes = [float(k) for k in range(23000, 23401, 50)]
    assert atm_from_strikes(23172.35, strikes) == 23150.0
    assert atm_from_strikes(23176.0, strikes) == 23200.0
    assert ladder_strikes(23150.0, strikes) == [23050.0, 23100.0, 23150.0, 23200.0, 23250.0]
    assert ladder_strikes(23000.0, strikes) == [23000.0, 23050.0, 23100.0]


def test_option_leg_charges_by_exchange_and_worthless_expiry():
    nfo = option_leg_charges(100 * 65, 200 * 65, "NFO")
    bfo = option_leg_charges(100 * 65, 200 * 65, "BFO")
    assert nfo > bfo  # NSE 0.03553% vs BSE 0.0325% of premium
    # STT 0.15% of the sell side dominates: 13000 × 0.0015 = 19.5
    assert 19.5 < nfo < 80
    # a leg left to expire pays one brokerage and no sell-side charges
    worthless = option_leg_charges(100 * 65, 0.0, "NFO", orders=1)
    assert 20 < worthless < 30  # Rs20 + txn + stamp + GST on brokerage


def test_combined_target_is_net_and_fails_closed_on_a_missing_bid():
    legs = [
        {"entry_price": 100.0, "quantity": 65, "bid": 200.0, "exchange": "NFO"},
        {"entry_price": 80.0, "quantity": 65, "bid": 160.0, "exchange": "NFO"},
    ]
    reached, d = combined_target_reached(legs, 2.0)
    assert not reached  # exactly 2x GROSS is below 2x once charges come off
    assert d["cost"] == 180.0 * 65 and d["combined_bid"] == 360.0 * 65
    legs[0]["bid"] = 210.0
    assert combined_target_reached(legs, 2.0)[0]
    legs[1]["bid"] = None  # unsellable leg contributes nothing
    assert not combined_target_reached(legs, 2.0)[0]
    assert not combined_target_reached([], 2.0)[0]


def test_session_digest_fields():
    def s(h, m, sec, spot, cb, ca, pb, pa):
        return {
            "t": time(h, m, sec),
            "spot": spot,
            "ce_bid": cb,
            "ce_ask": ca,
            "pe_bid": pb,
            "pe_ask": pa,
        }

    samples = [
        s(15, 14, 58, 23172.0, 95, 97, 85, 87),
        s(15, 15, 1, 23172.0, 95, 97, 85, 87),
        s(15, 20, 1, 23342.0, 250, 254, 10, 12),  # first print, entry poll
        s(15, 20, 57, 22852.0, 5, 6, 400, 404),  # low
        s(15, 24, 0, 23100.0, 40, 42, 150, 152),
        s(15, 29, 30, 23118.0, 30, 32, 120, 122),
    ]
    d = session_digest(samples, 23150.0, 2.0)
    assert d["close_1515"] == 23172.0
    assert d["first_print"] == 23342.0 and d["first_print_at"] == "15:20:01"
    assert d["iiv_low"] == 22852.0 and d["iiv_high"] == 23342.0
    assert d["settle"] == 23118.0 and d["settle_intrinsic"] == pytest.approx(32.0)
    assert d["straddle_ask_1520"] == 266 and d["straddle_bid_1520"] == 260
    assert d["max_combined_bid"] == 405 and d["max_combined_bid_at"] == "15:20:57"
    assert d["t_first_target"] is None  # 405 < 2 × 266
    assert d["combined_bid_1525"] == 190 and d["combined_bid_1530"] == 150
    assert d["combined_bid_1535"] == 150
    assert d["n_polls"] == 6 and d["spread_pct_1520"] == pytest.approx(100 * 6 / 263, rel=1e-3)


# --------------------------------------------------------------------------- #
# arm / ladder / ATM
# --------------------------------------------------------------------------- #
def test_non_expiry_day_is_idle():
    h = Harness(expiry_today=())
    svc = h.service()
    out = svc.arm()
    assert out["armed"] is False
    assert out["underlyings"]["NIFTY"] == {"expiry_day": False, "nearest_expiry": "29-SEP-26"}
    assert svc._monitor_thread is None and h.orders == []
    assert "cas_straddle_hard_exit" not in h.sched.jobs
    # status still tells the operator the next expiry per underlying
    st = svc.get_status()
    assert st["underlyings"]["SENSEX"]["nearest_expiry"] == "24-SEP-26"
    assert st["armed"] is False and st["config"]["lots_nifty"] == 1


def test_arm_resolves_ladder_lotsize_and_schedules_hard_exit(monkeypatch):
    h = Harness(config={"hard_exit_time": "15:29:00"})
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    h.set_spot("NIFTY", 23172.35)
    out = svc.arm()
    assert out["armed"] is True
    ni = out["underlyings"]["NIFTY"]
    assert ni["expiry_day"] and ni["expiry"] == "22-SEP-26" and ni["atm"] == 23150.0
    assert len(ni["ladder"]) == 10 and ni["lotsize"] == 65 and ni["trade"] and ni["lots"] == 1
    assert out["underlyings"]["SENSEX"]["expiry_day"] is False
    job = h.sched.jobs["cas_straddle_hard_exit"]
    assert "15:29:00" in job["name"]
    assert any("armed" in n and "hard exit 15:29:00" in n for n in h.notes)


def test_no_broker_session_at_arm_does_not_arm():
    h = Harness()
    h.session_ok = False
    svc = h.service()
    h.set_spot("NIFTY", 23172.35)
    out = svc.arm()
    assert out["armed"] is False and any("NO broker session" in n for n in h.notes)


def test_atm_is_fixed_from_the_1515_print_and_ladder_extends(monkeypatch):
    h = Harness()
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc, spot=23172.35)
    assert st["atm"] == 23150.0 and st["atm_fixed"] and st["close_1515"] == 23172.35
    # a spot that moved between 15:12 and 15:15 re-fixes the ATM and grows the ladder
    h2 = Harness()
    svc2 = h2.service()
    monkeypatch.setattr(svc2, "_ensure_monitor_thread", lambda: None)
    h2.set_spot("NIFTY", 23172.35)
    h2.clock.set(15, 12)
    svc2.arm()
    for c in list(svc2.day["u"]["NIFTY"]["ladder"].values()):
        h2.set_opt(c["symbol"], 90, 92)
    h2.set_spot("NIFTY", 23290.0)
    h2.clock.set(15, 14, 59)
    svc2.poll_once()
    h2.clock.set(15, 15, 2)
    svc2.poll_once()
    st2 = svc2.day["u"]["NIFTY"]
    assert st2["atm"] == 23300.0 and st2["atm_fixed"]
    assert "NIFTY22SEP2623300CE" in st2["ladder"] and "NIFTY22SEP2623050CE" in st2["ladder"]


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #
def test_entry_places_two_nrml_legs_in_sandbox_as_market(monkeypatch):
    h = Harness()
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    out = svc.run_entry()
    assert out["results"]["NIFTY"]["cost"] == pytest.approx((102 + 82) * 65)
    buys = [o for o in h.orders if o["action"] == "BUY"]
    assert {o["symbol"] for o in buys} == {"NIFTY22SEP2623150CE", "NIFTY22SEP2623150PE"}
    for o in buys:
        assert o["product"] == "NRML" and o["quantity"] == 65 and o["pricetype"] == "MARKET"
    assert st["entered"] and {leg["status"] for leg in st["legs"].values()} == {"open"}
    assert st["legs"]["CE"]["entry_price"] == 102.0 and st["legs"]["PE"]["entry_price"] == 82.0
    # idempotent
    assert svc.run_entry()["results"]["NIFTY"] == {"skipped": "already_entered"}
    assert len(h.orders) == 2


def test_entry_in_live_uses_marketable_limit_at_ask_plus_ticks(monkeypatch):
    h = Harness(mode="live")
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    svc.run_entry()
    ce = next(o for o in h.orders if o["symbol"].endswith("CE"))
    assert ce["pricetype"] == "LIMIT" and ce["price"] == pytest.approx(102.25)
    assert svc.day["u"]["NIFTY"]["legs"]["CE"]["mode"] == "live"


def test_entry_refuses_when_premium_exceeds_cap_and_never_trims(monkeypatch):
    h = Harness(config={"lots_nifty": 3, "max_premium_inr": 20000})
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)  # (102+82) × 195 = 35,880 > 20,000
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    out = svc.run_entry()
    assert out["results"]["NIFTY"]["skipped"] == "premium_cap"
    assert h.orders == [] and not st["entered"]
    assert any("REFUSED, not trimmed" in n for n in h.notes)


def test_trade_toggle_off_records_but_never_orders(monkeypatch, journal):
    h = Harness(config={"trade_nifty": False})
    svc = h.service(journal=True)
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.clock.set(15, 20, 0)
    out = svc.run_entry()
    assert out["results"]["NIFTY"] == {"skipped": "trade_off"} and h.orders == []
    assert st["entry_skipped"] == "trade_off"
    polls = journal.polls_for(TODAY.isoformat(), "NIFTY")
    assert len(polls) == 2 * (1 + 10)  # two polls × (spot + 10 ladder contracts)
    assert {p.kind for p in polls} == {"spot", "option"}


def test_pause_holds_entry_but_exits_still_run(monkeypatch):
    h = Harness()
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    _arm_nifty(h, svc)
    svc.pause()
    h.clock.set(15, 20, 0)
    assert svc.run_entry()["results"]["NIFTY"] == {"skipped": "paused"}
    assert h.orders == []
    svc.resume()
    assert not svc._entry_held_by_override()


def test_placement_rejection_makes_a_paper_leg_and_the_other_leg_trades(monkeypatch, journal):
    h = Harness()
    h.reject_symbols.add("NIFTY22SEP2623150PE")
    svc = h.service(journal=True)
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    svc.run_entry()
    assert st["legs"]["PE"]["fill"] == "paper" and st["legs"]["PE"]["status"] == "rejected"
    assert st["legs"]["CE"]["status"] == "open"
    rows = {r.side: r for r in journal.trades_for_date(TODAY.isoformat())}
    assert rows["PE"].fill == "paper" and "RMS" in rows["PE"].error_message
    assert rows["CE"].fill == "real" and rows["CE"].entry_price == 102.0
    # hard exit: the paper leg is priced at the bid for measurement, never ordered
    h.set_opt("NIFTY22SEP2623150CE", 150.0, 152.0)
    h.set_opt("NIFTY22SEP2623150PE", 20.0, 22.0)
    h.clock.set(15, 28, 0)
    svc.run_hard_exit()
    sells = [o for o in h.orders if o["action"] == "SELL"]
    assert [o["symbol"] for o in sells] == ["NIFTY22SEP2623150CE"]
    rows = {r.side: r for r in journal.trades_for_date(TODAY.isoformat())}
    assert rows["PE"].status == "closed" and rows["PE"].exit_price == 20.0
    assert journal.net_pnl_of_row(rows["PE"]) is None  # paper never joins real P&L
    assert journal.net_pnl_of_row(rows["CE"]) == pytest.approx(
        (150 - 102) * 65 - option_leg_charges(102 * 65, 150 * 65, "NFO")
    )
    assert [r.id for r in journal.real_closed_rows()] == [rows["CE"].id]


def test_post_ack_rejection_demotes_to_paper(monkeypatch):
    h = Harness()
    h.post_ack_reject.add("NIFTY22SEP2623150CE")
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    svc.run_entry()
    assert st["legs"]["CE"]["fill"] == "paper" and st["legs"]["CE"]["status"] == "rejected"
    assert st["legs"]["PE"]["status"] == "open"
    assert any("REJECTED the entry after ACK" in n for n in h.notes)


def test_pending_fill_is_re_verified_by_the_next_poll(monkeypatch):
    h = Harness()
    h.fill_pending.add("NIFTY22SEP2623150CE")
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    svc.run_entry()
    assert st["legs"]["CE"]["status"] == "placed" and st["legs"]["PE"]["status"] == "open"
    h.fill_pending.clear()
    h.clock.set(15, 20, 4)
    svc.poll_once()
    assert st["legs"]["CE"]["status"] == "open" and st["legs"]["CE"]["entry_price"] == 102.0


# --------------------------------------------------------------------------- #
# target / hard exit / fallback
# --------------------------------------------------------------------------- #
def _entered(monkeypatch, h: Harness | None = None, journal=False):
    h = h or Harness()
    svc = h.service(journal=journal)
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    st = _arm_nifty(h, svc)
    h.set_opt("NIFTY22SEP2623150CE", 100.0, 102.0)
    h.set_opt("NIFTY22SEP2623150PE", 80.0, 82.0)
    h.clock.set(15, 20, 0)
    svc.run_entry()
    assert st["entered"]
    return h, svc, st


def test_target_needs_two_consecutive_polls_and_reads_net_of_charges(monkeypatch):
    h, svc, st = _entered(monkeypatch)
    # cost = (102+82)×65 = 11,960; 2× = 23,920. Bids 250+130 = 380×65 = 24,700 gross,
    # ≈ 24,600 net → reached.
    h.set_opt("NIFTY22SEP2623150CE", 250.0, 252.0)
    h.set_opt("NIFTY22SEP2623150PE", 130.0, 132.0)
    h.clock.set(15, 23, 0)
    svc.poll_once()
    assert st["target_polls"] == 1 and not st["exit_done"]
    assert [o for o in h.orders if o["action"] == "SELL"] == []
    # one dip below resets the count
    h.set_opt("NIFTY22SEP2623150CE", 200.0, 202.0)
    h.clock.set(15, 23, 2)
    svc.poll_once()
    assert st["target_polls"] == 0
    h.set_opt("NIFTY22SEP2623150CE", 250.0, 252.0)
    h.clock.set(15, 23, 4)
    svc.poll_once()
    h.clock.set(15, 23, 6)
    svc.poll_once()
    sells = [o for o in h.orders if o["action"] == "SELL"]
    assert len(sells) == 2 and all(o["product"] == "NRML" for o in sells)
    assert st["exit_done"]
    assert {leg["exit_reason"] for leg in st["legs"].values()} == {"target"}
    assert st["legs"]["CE"]["exit_price"] == 250.0 and st["legs"]["PE"]["exit_price"] == 130.0
    net = sum(leg["net_pnl"] for leg in st["legs"].values())
    assert net == pytest.approx(
        (250 - 102) * 65 + (130 - 82) * 65
        - option_leg_charges(102 * 65, 250 * 65) - option_leg_charges(82 * 65, 130 * 65)
    )  # fmt: skip
    # a later poll does nothing more
    h.clock.set(15, 23, 8)
    svc.poll_once()
    assert len(h.orders) == 4
    assert any("exit (target)" in n for n in h.notes)


def test_hard_exit_sells_the_leg_with_a_bid_and_expires_the_other(monkeypatch, journal):
    h, svc, st = _entered(monkeypatch, journal=True)
    h.set_opt("NIFTY22SEP2623150CE", 180.0, 182.0)
    h.quotes["NIFTY22SEP2623150PE"] = {"ltp": 0.05, "bid": None, "ask": 0.1, "volume": 1, "oi": 1}
    h.clock.set(15, 28, 0)
    svc.run_hard_exit()
    sells = [o for o in h.orders if o["action"] == "SELL"]
    assert [o["symbol"] for o in sells] == ["NIFTY22SEP2623150CE"]
    assert st["legs"]["PE"]["exit_reason"] == "expired_worthless"
    assert st["legs"]["PE"]["exit_price"] == 0.0 and st["exit_done"]
    rows = {r.side: r for r in journal.trades_for_date(TODAY.isoformat())}
    assert rows["PE"].charges_inr == pytest.approx(
        option_leg_charges(82 * 65, 0.0, "NFO", orders=1)
    )
    assert journal.net_pnl_of_row(rows["PE"]) == pytest.approx(-82 * 65 - rows["PE"].charges_inr)
    assert rows["CE"].exit_reason == "hard_exit" and rows["CE"].exit_price == 180.0
    # the hard exit is idempotent
    svc.run_hard_exit()
    assert len(h.orders) == 3


def test_rejected_exit_is_error_never_paper_and_fallback_retries_from_the_book(monkeypatch):
    h, svc, st = _entered(monkeypatch)
    h.reject_symbols.add("NIFTY22SEP2623150CE")  # SELL refused
    h.set_opt("NIFTY22SEP2623150CE", 180.0, 182.0)
    h.set_opt("NIFTY22SEP2623150PE", 30.0, 32.0)
    h.clock.set(15, 28, 0)
    svc.run_hard_exit()
    assert st["legs"]["CE"]["status"] == "error" and st["legs"]["CE"]["fill"] == "real"
    assert st["legs"]["PE"]["status"] == "closed"
    assert any("exit order NOT accepted" in n for n in h.notes)
    # 15:38: the book still holds the CE → SELL again (now accepted)
    h.reject_symbols.clear()
    h.book = [
        {"symbol": "NIFTY22SEP2623150CE", "exchange": "NFO", "product": "NRML", "quantity": 65}
    ]
    h.clock.set(15, 38, 0)
    out = svc.run_fallback_flatten()
    assert [o["symbol"] for o in out["results"]["NIFTY"]] == ["NIFTY22SEP2623150CE"]
    assert st["legs"]["CE"]["status"] == "closed" and st["legs"]["CE"]["exit_reason"] == "fallback"


def test_fallback_on_unreadable_book_sends_for_confirmed_fill_only(monkeypatch):
    h = Harness()
    h.fill_pending.add("NIFTY22SEP2623150PE")  # PE never confirmed
    h, svc, st = _entered(monkeypatch, h)
    assert st["legs"]["PE"]["status"] == "placed" and st["legs"]["CE"]["status"] == "open"
    h.book = None
    h.set_opt("NIFTY22SEP2623150CE", 150.0, 152.0)
    h.clock.set(15, 38, 0)
    svc.run_fallback_flatten()
    sells = [o for o in h.orders if o["action"] == "SELL"]
    assert [o["symbol"] for o in sells] == ["NIFTY22SEP2623150CE"]  # believed filled → sent
    assert st["legs"]["PE"]["status"] == "placed"  # unverified + unreadable → left alone


def test_fallback_on_affirmatively_flat_book_papers_an_unverified_leg(monkeypatch):
    h = Harness()
    h.fill_pending.add("NIFTY22SEP2623150PE")
    h, svc, st = _entered(monkeypatch, h)
    h.book = []  # flat
    h.set_opt("NIFTY22SEP2623150CE", 150.0, 152.0)
    h.clock.set(15, 38, 0)
    svc.run_fallback_flatten()
    assert st["legs"]["PE"]["fill"] == "paper" and st["legs"]["PE"]["status"] == "rejected"
    # CE was confirmed filled but the book is flat: closed unpriced + alert, no SELL
    assert st["legs"]["CE"]["status"] == "closed" and st["legs"]["CE"].get("exit_price") is None
    assert [o for o in h.orders if o["action"] == "SELL"] == []
    assert any("book flat at 15:38" in n for n in h.notes)


def test_fallback_caps_quantity_to_the_book(monkeypatch):
    h, svc, st = _entered(monkeypatch)
    st["legs"]["CE"]["status"] = "error"  # a rejected exit earlier
    st["legs"]["PE"]["status"] = "closed"
    st["legs"]["PE"]["exit_reason"] = "hard_exit"
    h.book = [
        {"symbol": "NIFTY22SEP2623150CE", "exchange": "NFO", "product": "NRML", "quantity": 30}
    ]
    h.set_opt("NIFTY22SEP2623150CE", 150.0, 152.0)
    h.clock.set(15, 38, 0)
    svc.run_fallback_flatten()
    sell = next(o for o in h.orders if o["action"] == "SELL")
    assert sell["quantity"] == 30


def test_close_all_is_manual_exit(monkeypatch):
    h, svc, st = _entered(monkeypatch)
    h.set_opt("NIFTY22SEP2623150CE", 110.0, 112.0)
    h.set_opt("NIFTY22SEP2623150PE", 70.0, 72.0)
    out = svc.close_all_positions()
    assert len(out) == 2 and {leg["exit_reason"] for leg in st["legs"].values()} == {"manual"}


# --------------------------------------------------------------------------- #
# EOD digest + settlement counterfactual
# --------------------------------------------------------------------------- #
def test_eod_summary_writes_session_and_settlement_counterfactual(monkeypatch, journal):
    h, svc, st = _entered(monkeypatch, journal=True)
    h.set_spot("NIFTY", 23342.0)
    h.set_opt("NIFTY22SEP2623150CE", 200.0, 204.0)
    h.set_opt("NIFTY22SEP2623150PE", 10.0, 12.0)
    h.clock.set(15, 20, 3)
    svc.poll_once()
    h.set_spot("NIFTY", 23118.6)
    h.set_opt("NIFTY22SEP2623150CE", 30.0, 32.0)
    h.set_opt("NIFTY22SEP2623150PE", 60.0, 62.0)
    h.clock.set(15, 28, 0)
    svc.poll_once()
    svc.run_hard_exit()
    h.clock.set(15, 45, 0)
    out = svc.run_eod_summary()
    d = out["digests"]["NIFTY"]
    assert d["close_1515"] == 23172.35 and d["first_print"] == 23342.0
    assert d["settle"] == 23118.6 and d["settle_intrinsic"] == pytest.approx(31.4)
    sess = journal.session_to_dict(journal.sessions()[0])
    assert sess["traded"] is True and sess["straddle_ask_1520"] == pytest.approx(184.0)
    assert sess["config"]["lots_nifty"] == 1 and sess["n_polls"] == 5  # incl. the entry sample
    rows = {r.side: r for r in journal.trades_for_date(TODAY.isoformat())}
    # CE intrinsic 0 at 23118.6 vs K 23150: cf = −102×65 − one brokerage leg
    assert rows["CE"].settlement_cf_pnl == pytest.approx(
        -102 * 65 - option_leg_charges(102 * 65, 0.0, "NFO", orders=1)
    )
    # PE intrinsic 31.4: (31.4 − 82)×65 − exercise STT − entry brokerage
    assert rows["PE"].settlement_cf_pnl == pytest.approx(
        (31.4 - 82) * 65
        - cs.exercise_stt(31.4 * 65)
        - option_leg_charges(82 * 65, 0.0, "NFO", orders=1)
    )
    assert any("EOD" in n and "TRADED 1 lot" in n for n in h.notes)
    assert svc.get_status()["underlyings"]["NIFTY"]["digest"]["settle"] == 23118.6


# --------------------------------------------------------------------------- #
# monitor thread + scheduler registration
# --------------------------------------------------------------------------- #
def test_monitor_loop_polls_inside_the_window_then_marks_done(monkeypatch):
    h = Harness(config={"poll_interval_s": 2})
    svc = h.service()
    monkeypatch.setattr(svc, "_ensure_monitor_thread", lambda: None)
    _arm_nifty(h, svc)
    beats, dones = [], []
    monkeypatch.setattr("services.thread_registry.beat", beats.append)
    monkeypatch.setattr("services.thread_registry.done", dones.append)
    ticks = []
    monkeypatch.setattr(svc, "poll_once", lambda now=None: ticks.append(now))
    monkeypatch.setattr(svc, "_finalize_sessions", lambda: {})
    h.clock.set(15, 40, 58)
    steps = iter([(15, 40, 59), (15, 41, 0)])

    def sleep(_s):
        h.clock.set(*next(steps))

    svc._sleep = sleep
    svc._monitor_loop()
    assert len(ticks) == 2 and beats == ["cas-straddle-monitor"] * 3
    assert dones == ["cas-straddle-monitor"]


def test_register_jobs_catalogued_ids_and_daily_reset():
    h = Harness()
    svc = h.service()
    svc.register_jobs(h.sched)
    assert set(h.sched.jobs) == {
        "cas_straddle_daily_reset",
        "cas_straddle_arm",
        "cas_straddle_entry",
        "cas_straddle_hard_exit",
        "cas_straddle_fallback_flatten",
        "cas_straddle_eod_summary",
    }
    assert cs.get_service() is svc
    svc.day["trade_date"] = "x"
    svc.run_daily_reset()
    assert svc.day["trade_date"] is None


# --------------------------------------------------------------------------- #
# routing: the /strategies toggle is the ONLY thing that takes it live
# --------------------------------------------------------------------------- #
@pytest.fixture
def routing(monkeypatch):
    state = {"analyze": False, "modes": {}}

    def fake_get_mode(strategy_name):
        mode = state["modes"].get(strategy_name)
        return {"mode": mode} if mode else None

    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: state["analyze"])
    monkeypatch.setattr("database.strategy_mode_db.get_mode", fake_get_mode)
    return state


def test_resolve_order_mode_default_denies_to_sandbox(routing):
    from services.mode_service import EffectiveMode, resolve_order_mode

    assert resolve_order_mode(STRATEGY_NAME) is EffectiveMode.SANDBOX  # no row
    routing["modes"][STRATEGY_NAME] = "live"
    assert resolve_order_mode(STRATEGY_NAME) is EffectiveMode.LIVE  # toggle flipped
    routing["analyze"] = True
    assert resolve_order_mode(STRATEGY_NAME) is EffectiveMode.SANDBOX  # Analyze wins
    assert cs.production_mode_resolver() == "sandbox"
    routing["analyze"] = False
    assert cs.production_mode_resolver() == "live"


def test_production_order_placer_routes_by_the_strategy_mode_key():
    seen = {}

    def fake_place_order(payload, api_key=None, mode_key=None):
        seen.update(payload=payload, api_key=api_key, mode_key=mode_key)
        return True, {"status": "success", "orderid": "S1"}, 200

    import database.auth_db as auth_db
    import services.place_order_service as pos

    with (
        patch.object(
            auth_db, "get_first_available_api_key", return_value="KEY"
        ),  # pragma: allowlist secret
        patch.object(pos, "place_order", fake_place_order),
    ):
        resp = cs.production_order_placer(
            {"symbol": "NIFTY22SEP2623150CE", "exchange": "NFO", "action": "BUY",
             "product": "NRML", "quantity": 65, "pricetype": "LIMIT", "price": 102.25}
        )  # fmt: skip
    assert resp["orderid"] == "S1"
    assert (
        seen["mode_key"] == STRATEGY_NAME and seen["api_key"] == "KEY"  # pragma: allowlist secret
    )  # pragma: allowlist secret
    p = seen["payload"]
    assert p["strategy"] == STRATEGY_NAME and p["product"] == "NRML"
    assert p["pricetype"] == "LIMIT" and p["price"] == "102.25" and p["quantity"] == "65"


def test_place_order_with_auth_lands_in_sandbox_then_live_by_toggle(routing):
    """End to end through the REAL dispatch seam: the same payload goes to the
    sandbox book with no row and to the broker once the toggle says live."""
    from services.place_order_service import place_order_with_auth

    calls = {"sandbox": [], "broker": []}

    def fake_sandbox(order_data, api_key, original_data, prefetched_quote=None):
        calls["sandbox"].append(order_data)
        return True, {"status": "success", "orderid": "SB1"}, 200

    class _Broker:
        @staticmethod
        def place_order_api(order_data, auth_token):
            calls["broker"].append(order_data)
            return type("R", (), {"status": 200})(), {"status": "success"}, "BR1"

    payload = {
        "apikey": "KEY", "strategy": STRATEGY_NAME, "symbol": "NIFTY22SEP2623150CE",  # pragma: allowlist secret
        "exchange": "NFO", "action": "BUY", "product": "NRML", "pricetype": "LIMIT",
        "price": "102.25", "quantity": "65",
    }  # fmt: skip
    with (
        patch("services.sandbox_service.sandbox_place_order", fake_sandbox),
        patch("services.place_order_service.import_broker_module", return_value=_Broker),
        patch("services.place_order_service.bus"),
        patch("services.synthetic_market_order_service.ensure_live_safe_pricetype",
              lambda od, *_a: (od, None, None)),
    ):  # fmt: skip
        ok, resp, _ = place_order_with_auth(
            dict(payload),
            "TOKEN",  # pragma: allowlist secret
            "zerodha",
            dict(payload),
            emit_event=False,
            mode_key=STRATEGY_NAME,
        )
        assert ok and resp["orderid"] == "SB1" and calls["broker"] == []
        routing["modes"][STRATEGY_NAME] = "live"
        ok, resp, _ = place_order_with_auth(
            dict(payload),
            "TOKEN",  # pragma: allowlist secret
            "zerodha",
            dict(payload),
            emit_event=False,
            mode_key=STRATEGY_NAME,
        )
    assert ok and len(calls["broker"]) == 1 and calls["broker"][0]["product"] == "NRML"
