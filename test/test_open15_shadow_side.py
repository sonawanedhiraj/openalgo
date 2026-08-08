"""Shadow-logging the excluded side (issue #581).

The operator trades ONE side with real money (``trade_side='long_only'``) and
wants the other side measured so the two cohorts can be compared. A shadow row
is a priced counterfactual: the trigger is decided identically to a real one,
and no order is ever sent.

These tests exist to pin the properties that make that safe on an install which
is **live**. In order of how much damage a regression would do:

1. **No order is ever placed for a shadow trigger.** Asserted against the
   ``order_placer`` seam driving the real ZMQ pipeline, never against a mocked
   journal — mocking the call that must not happen cannot prove it did not.
2. **Shadow money never becomes real money.** It is excluded from
   ``total_realized_pnl``, which drives compound position sizing, so a
   regression would size tomorrow's REAL orders against money never made.
3. **Shadow triggers never consume the real ``max_trades`` budget.** That cap
   is a real-money budget; spending it on measurement would silently reduce how
   much the strategy actually trades.
4. **The exit path never reads the position book for a shadow row.** Nothing
   was sent, so the book could only surface an unrelated same-symbol position
   and promote a trade we never placed into a live square-off.
5. **Off by default is byte-identical to today.** Merging this must not change
   what the next 09:10 arm does.
"""

import datetime as dt
import json

import pytest

from services.open15_breakout_service import (
    Open15BreakoutService,
    Open15Core,
    _shadow_excluded_side_default,
    _shadow_max_trades_default,
    clamp_shadow_max_trades,
    resolve_day_config,
    shadow_side_for,
)

TRADE_DATE = "2026-08-11"


@pytest.fixture(autouse=True)
def _clean_journal():
    """Empty the journal between tests (same reason as issue #553's fixture)."""
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


def _frame(symbol, price, cumvol, h, m, s):
    topic = f"NSE_{symbol}_LTP"
    payload = json.dumps(
        {
            "ltp": price,
            "volume": cumvol,
            "exchange_timestamp": dt.datetime(2026, 8, 11, h, m, s).timestamp(),
        }
    )
    return topic, payload


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 11, h, m, s))


def _mk_service(orders, *, shadow=True, max_trades=3, shadow_max=3):
    """long_only service, AAA the gainer (traded) and CCC the loser (shadowed)."""

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "CCC"}
    svc.day_config = resolve_day_config(
        {
            "margin_per_slot": 30000,
            "sizing_mode": "fixed",
            "vol_mult": 1.5,
            "instrument": "stock",
            "trade_side": "long_only",
            "max_trades": max_trades,
            "shadow_excluded_side": shadow,
            "shadow_max_trades": shadow_max,
        },
        0,
    )
    svc.core = Open15Core(
        {"AAA": 100.0, "CCC": 100.0},
        vol_mult=1.5,
        top_n=1,
        trade_side="long_only",
        shadow_side=svc.day_config["shadow_side"],
    )
    svc.day_status = "armed"
    svc._log_date = TRADE_DATE
    # a book read is a FAILURE for shadow rows — see property 4
    svc._broker_qty = lambda symbol, exchange: pytest.fail(
        f"position book was read for {symbol} — nothing was ever ordered"
    )
    return svc


def _run_to_selection(svc):
    for sym, px in (("AAA", 103.0), ("CCC", 97.0)):
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    svc._handle_raw(*_frame("AAA", 103.0, 6000, 9, 16, 10), _now(9, 16, 10))
    svc._handle_raw(*_frame("CCC", 96.9, 6000, 9, 16, 15), _now(9, 16, 15))


def _trigger_long(svc, h=9, m=17, s=12):
    level = svc.core.sym["AAA"]["fc"]["high"]
    svc._handle_raw(*_frame("AAA", level + 0.5, 6000 + 9000, h, m, s), _now(h, m, s))
    return level


def _trigger_short(svc, h=9, m=18, s=20):
    level = svc.core.sym["CCC"]["fc"]["low"]
    svc._handle_raw(*_frame("CCC", level - 0.5, 6000 + 9000, h, m, s), _now(h, m, s))
    return level


def _rows():
    from database.open15_breakout_db import Open15Trade, db_session

    try:
        return {r.symbol: r for r in db_session.query(Open15Trade).all()}
    finally:
        db_session.remove()


# ---- derivation ----------------------------------------------------------- #


def test_shadow_side_for_derivation():
    assert shadow_side_for("long_only", True) == "S"
    assert shadow_side_for("short_only", True) == "L"
    # `both` excludes nothing, so there is nothing to shadow however the flag is set
    assert shadow_side_for("both", True) is None
    assert shadow_side_for("long_only", False) is None


def test_core_rejects_a_shadow_side_that_is_actually_traded():
    """Shadowing the side you TRADE would journal real fills as counterfactuals."""
    core = Open15Core({"AAA": 100.0}, trade_side="long_only", shadow_side="L")
    assert core.shadow_side is None
    core = Open15Core({"AAA": 100.0}, trade_side="both", shadow_side="S")
    assert core.shadow_side is None


def test_env_defaults_are_off():
    assert _shadow_excluded_side_default() is False
    assert _shadow_max_trades_default() == 3


def test_clamp_shadow_max_trades():
    assert clamp_shadow_max_trades(3) == 3
    assert clamp_shadow_max_trades(-5) == 0  # 0 is legal: "shadow nothing"
    assert clamp_shadow_max_trades(999) == 10
    assert clamp_shadow_max_trades("junk") == 3


def test_resolve_day_config_defaults_off():
    cfg = resolve_day_config(None, 0.0)
    assert cfg["shadow_excluded_side"] is False
    assert cfg["shadow_side"] is None


def test_resolve_day_config_stored_false_beats_env_true(monkeypatch):
    monkeypatch.setenv("OPEN15_SHADOW_EXCLUDED_SIDE", "true")
    cfg = resolve_day_config({"trade_side": "long_only", "shadow_excluded_side": False}, 0.0)
    assert cfg["shadow_side"] is None
    cfg = resolve_day_config({"trade_side": "long_only"}, 0.0)
    assert cfg["shadow_side"] == "S"


# ---- property 5: OFF is unchanged ----------------------------------------- #


def test_off_by_default_excluded_side_is_not_even_watched():
    """The pre-#581 behaviour, unchanged: long_only watches longs only."""
    orders = []
    svc = _mk_service(orders, shadow=False)
    _run_to_selection(svc)
    assert svc.core.selected == {"AAA": "L"}
    assert svc.core.shadow_side is None


def test_shadow_on_watches_both_sides():
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    assert svc.core.selected == {"AAA": "L", "CCC": "S"}
    assert svc.core.is_shadow("S") and not svc.core.is_shadow("L")


# ---- property 1: NO ORDER ------------------------------------------------- #


def test_shadow_trigger_places_no_order():
    """The contract. Asserted at the order_placer seam, not on a mock journal."""
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_short(svc)

    assert orders == [], f"an order was placed for the shadowed side: {orders}"
    row = _rows()["CCC"]
    assert row.fill == "shadow"
    assert row.status == "skipped"
    assert row.reason == "side_excluded"
    # `quantity` records what was ORDERED, and nothing was
    assert row.quantity == 0
    assert row.entry_order_id in (None, "")


def test_shadow_and_real_coexist_and_only_the_real_one_orders():
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_long(svc)
    _trigger_short(svc)

    assert [o["symbol"] for o in orders] == ["AAA"]
    rows = _rows()
    assert rows["AAA"].fill == "real"
    assert rows["CCC"].fill == "shadow"


def test_flatten_places_no_order_for_a_shadow_row():
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_short(svc)
    svc.flatten()

    assert orders == [], f"the exit placed an order for a shadow row: {orders}"
    row = _rows()["CCC"]
    assert row.exit_status == "not_placed"
    assert row.fill == "shadow"
    assert row.pnl is not None  # it IS priced — that is the whole point
    assert row.charges_inr is not None
    assert row.pnl_source == "quote"
    assert row.fill_reconcile_status == "not_applicable"


# ---- property 4: the book is never read ----------------------------------- #


def test_flatten_never_reads_the_position_book_for_shadow():
    """``_broker_qty`` fails the test if called (see the fixture).

    A shadow row was never sent, so a non-zero book quantity could only be an
    unrelated position — and acting on it would open a real square-off for a
    trade that never existed.
    """
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_short(svc)
    svc.flatten()  # would pytest.fail inside _broker_qty


# ---- property 2: shadow money is never real money ------------------------- #


def test_shadow_pnl_excluded_from_realized_and_reported_apart():
    from database.open15_breakout_db import (
        shadow_pnl_by_date,
        total_realized_pnl,
        trades_pnl_by_date,
    )

    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_short(svc)
    svc.flatten()

    row = _rows()["CCC"]
    net = float(row.pnl) - float(row.charges_inr or 0.0)
    assert net != 0.0  # a zero would make this test vacuous

    assert total_realized_pnl() == 0.0
    assert trades_pnl_by_date().get(TRADE_DATE) is None
    assert shadow_pnl_by_date()[TRADE_DATE] == pytest.approx(round(net, 2), abs=0.01)


def test_shadow_is_in_the_non_real_fill_list():
    """The one edit that keeps shadow out of every P&L sum."""
    from database.open15_breakout_db import NON_REAL_FILLS

    assert "shadow" in NON_REAL_FILLS


# ---- property 3: the real budget is untouched ----------------------------- #


def test_shadow_does_not_consume_max_trades():
    """max_trades=1: the shadow fires first, the real long must still trade."""
    orders = []
    svc = _mk_service(orders, max_trades=1)
    _run_to_selection(svc)
    _trigger_short(svc, m=17)
    _trigger_long(svc, m=18)

    assert [o["symbol"] for o in orders] == ["AAA"]
    rows = _rows()
    assert rows["AAA"].fill == "real"
    assert rows["AAA"].status == "open"
    # and the shadow row was NOT diverted into a max_trades_cap sim row
    assert rows["CCC"].fill == "shadow"
    assert rows["CCC"].reason == "side_excluded"


def test_count_fills_separates_shadow_from_real():
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    _trigger_long(svc)
    _trigger_short(svc)
    assert svc._count_fills() == (1, 0, 0, 1)


def test_shadow_cap_stops_further_rows():
    orders = []
    svc = _mk_service(orders, shadow_max=0)
    _run_to_selection(svc)
    _trigger_short(svc)

    assert orders == []
    row = _rows()["CCC"]
    # journaled unpriced as the documented "deliberately not measured" class
    assert row.fill == "none"
    assert row.reason == "shadow_cap"
    assert row.pnl is None


# ---- sizing --------------------------------------------------------------- #


def test_shadow_is_priced_at_full_slot_not_one_unit():
    """Operator decision: comparable to the traded cohort, so full slot size."""
    orders = []
    svc = _mk_service(orders)
    _run_to_selection(svc)
    level = _trigger_short(svc)

    row = _rows()["CCC"]
    expected = int(svc.day_config["notional"] / (level - 0.5))
    assert row.sim_quantity == expected
    assert expected > 1


# ---- rolling additions ---------------------------------------------------- #


def test_rolling_addition_on_the_shadow_side_is_flagged():
    """Seed AND rolling shorts are shadowed (operator decision)."""
    core = Open15Core(
        {"AAA": 100.0, "CCC": 100.0, "DDD": 100.0},
        vol_mult=1.5,
        top_n=1,
        trade_side="long_only",
        shadow_side="S",
        rolling_enabled=True,
        rolling_cadence_s=10,
        rolling_top_n=1,
    )
    # CCC is the biggest loser at 09:15, so it is the SEED short (top_n=1).
    # DDD only becomes the top loser intraday — which is what the rolling
    # re-rank exists to catch, and the case that must still be shadowed.
    for sym, px in (("AAA", 103.0), ("CCC", 97.0), ("DDD", 98.0)):
        core.on_tick(sym, px, 1000, dt.datetime(2026, 8, 11, 9, 15, 1))
    core.on_tick("AAA", 103.0, 2000, dt.datetime(2026, 8, 11, 9, 16, 5))
    assert core.selected == {"AAA": "L", "CCC": "S"}
    core.on_tick("DDD", 90.0, 2000, dt.datetime(2026, 8, 11, 9, 16, 30))
    adds = core.maybe_rerank(dt.datetime(2026, 8, 11, 9, 17, 0))

    added = {a["symbol"]: a for a in adds}
    assert "DDD" in added, "the shadow side must still be re-ranked"
    assert added["DDD"]["side"] == "S"
    assert added["DDD"]["shadow"] is True


# ---- digest bucket separation --------------------------------------------- #


def test_digest_keeps_shadow_apart_from_real_and_sim():
    from services.open15_log_view import summarize_day

    events = [
        {"event": "armed"},
        {"event": "selection", "selected": {"AAA": "L", "CCC": "S"}},
        {"event": "entry", "symbol": "AAA", "order_status": "success"},
        {"event": "entry_shadow", "symbol": "CCC", "fill": "shadow"},
        {"event": "exit", "symbol": "AAA", "pnl": 500.0},
        {"event": "exit_shadow", "symbol": "CCC", "pnl": -120.0},
    ]
    d = summarize_day("2026-08-11", events)
    assert d["entered"] == 1
    assert d["shadow"] == 1
    assert d["pnl"] == 500.0
    assert d["shadow_pnl"] == -120.0
    assert d["sim_pnl"] is None
    assert d["paper_pnl"] is None


def test_selection_outcomes_marks_the_shadow_row():
    from services.open15_log_view import selection_outcomes

    events = [
        {"event": "selection", "selected": {"CCC": "S"}, "gaps_pct": {"CCC": -3.0}},
        {
            "event": "entry_shadow",
            "symbol": "CCC",
            "qty": 1554,
            "trigger_price": 96.4,
            "reason": "side_excluded",
            "instrument": "stock",
            "vol_ratio": 2.1,
        },
        {
            "event": "exit_shadow",
            "symbol": "CCC",
            "exit_price": 97.0,
            "pnl": -120.0,
            "qty": 1554,
            "fill": "shadow",
        },
    ]
    rows = selection_outcomes("2026-08-11", events)
    row = next(r for r in rows if r["symbol"] == "CCC")
    assert row["fill"] == "shadow"
    assert row["entered"] is False
    assert row["pnl"] == -120.0


# ---- DB + API ------------------------------------------------------------- #


def test_config_db_roundtrip_shadow_fields():
    import database.open15_breakout_db as db

    db.init_db()
    assert db.save_config(
        30000.0, "fixed", 1.5, updated_by="test", shadow_excluded_side=True, shadow_max_trades=5
    )
    cfg = db.get_config()
    assert cfg["shadow_excluded_side"] is True
    assert cfg["shadow_max_trades"] == 5
    # cleared back to NULL = fall through to the env default (OFF)
    assert db.save_config(30000.0, "fixed", 1.5, updated_by="test")
    assert db.get_config()["shadow_excluded_side"] is None


@pytest.fixture
def client(monkeypatch):
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    from flask import Flask

    import blueprints.open15_breakout as bp

    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_config_post_saves_and_clamps_shadow_fields(client, monkeypatch):
    import database.open15_breakout_db as db

    saved = {}

    def fake_save(*args, **kwargs):
        saved.update(kwargs)
        return True

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.setattr(db, "save_config", fake_save)
    r = client.post(
        "/open15_vol_breakout/api/config",
        json={"shadow_excluded_side": True, "shadow_max_trades": 99},
    )
    assert r.status_code == 200
    assert saved["shadow_excluded_side"] is True
    assert saved["shadow_max_trades"] == 10  # clamped, not rejected


def test_config_get_exposes_shadow_env_defaults(client, monkeypatch):
    import database.open15_breakout_db as db

    monkeypatch.setattr(db, "get_config", lambda: None)
    monkeypatch.delenv("OPEN15_SHADOW_EXCLUDED_SIDE", raising=False)
    r = client.get("/open15_vol_breakout/api/config")
    assert r.status_code == 200
    assert r.get_json()["env_defaults"]["shadow_excluded_side"] is False
