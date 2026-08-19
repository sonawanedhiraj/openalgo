"""A trigger always produces one event, and the residual cash is spent (#643).

Two defects that shared one symptom on **2026-08-19**. GVT&D triggered a legal
short at 09:24:45 (2.73x volume while beyond the level, against a 1.5x gate) and
vanished: no journal row, no decision-log event, no alert. The /logs page showed
a GREEN (gate-cleared) volume cell beside the outcome text ``no trigger``.

1. ``_sim_context`` still unpacked ``_option_liquidity`` as the pre-#555 3-tuple.
   GVT&D was the day's 3rd trigger against an effective cap of 2, so ``_enter``
   took the ``max_trades_cap`` branch, which prices the miss at 1 lot through
   that function. It raised ``ValueError``, the raise unwound past ``_enter``
   into the ZMQ loop's generic handler, and the trigger produced NOTHING.

2. The cap was 2 because ``clamp_slots_to_funds`` floors ``cash / slot``:
   ``floor(161365.10 / 60000)``. The two fills actually consumed Rs1,21,635, so
   **Rs39,730 sat idle** — two lots of the contract the third signal wanted.

The tests are ordered by how much damage a regression would do:

* an entry that raises must still be journaled and logged (a silent drop is
  invisible to every consumer, which is what made #643 take a day to notice);
* the ledger must be released on all three non-fill outcomes, or every later
  entry silently shrinks;
* residual money must never be spent twice, and must be labelled so the
  research cohort stays separable from full-slot rows;
* with the flag off, everything is byte-identical to pre-#643 behaviour.
"""

import datetime as dt
import json

import pytest

from services.open15_breakout_service import (
    Open15BreakoutService,
    Open15Core,
    clamp_residual_min_lots,
    clamp_residual_reserve_pct,
    clamp_slots_to_funds,
    resolve_day_config,
    resolve_entry_budget,
)

TRADE_DATE = "2026-08-19"


@pytest.fixture(autouse=True)
def _clean_journal():
    """Empty the journal between tests (issue #553's fixture, same reason)."""
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
            "exchange_timestamp": dt.datetime(2026, 8, 19, h, m, s).timestamp(),
        }
    )
    return topic, payload


def _now(h, m, s=0):
    import pytz

    return pytz.timezone("Asia/Kolkata").localize(dt.datetime(2026, 8, 19, h, m, s))


def _mk_service(orders, monkeypatch=None, *, cfg=None, lotsize=125, premium=112.35):
    """Option-mode service with the ATM resolver and the quote pinned."""

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.universe = {"AAA", "BBB", "CCC", "ZZZ"}
    svc.core = Open15Core(
        {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=3
    )
    svc.day_status = "armed"
    svc._log_date = TRADE_DATE
    base = {
        "margin_per_slot": 60000,
        "sizing_mode": "fixed",
        "vol_mult": 1.5,
        "instrument": "atm_option",
        "max_trades": 3,
    }
    base.update(cfg or {})
    svc.day_config = resolve_day_config(base, 0)
    svc._broker_qty = lambda symbol, exchange: 0
    if monkeypatch is not None:
        import services.open15_option_shadow as shadow

        monkeypatch.setattr(
            shadow,
            "resolve_atm_option",
            lambda underlying, side, spot, trade_date: {
                "symbol": f"{underlying}25AUG264100PE",
                "strike": 4100.0,
                "expiry": "25-AUG-26",
                "lotsize": lotsize,
                "ticksize": 0.05,
            },
        )
        svc.quote_snapshot_fn = lambda sym, ex: {
            "ltp": premium,
            "volume": 1000,
            "oi": 500_000,
            "bid": premium - 0.5,
            "ask": premium,
        }
        svc.quote_fn = lambda sym, ex: premium
    return svc


# distinct gaps, so the 09:16 ranking is deterministic and the three symbols the
# tests trigger are the three it selects (top_n=3 per side)
_OPEN_PX = {"AAA": 106.0, "BBB": 105.0, "CCC": 104.0, "ZZZ": 101.0}


def _run_to_selection(svc, symbols=("AAA", "BBB", "CCC", "ZZZ")):
    for sym in symbols:
        px = _OPEN_PX[sym]
        svc._handle_raw(*_frame(sym, px, 1000, 9, 15, 1), _now(9, 15, 1))
        svc._handle_raw(*_frame(sym, px * 1.001, 5000, 9, 15, 50), _now(9, 15, 50))
    # every symbol needs a 09:16 bar before it can trigger: the 09:15 candle is
    # excluded from the volume baseline by default, so a symbol whose only ticks
    # are the opening minute has an EMPTY baseline and can never fire
    for sym in symbols:
        svc._handle_raw(*_frame(sym, _OPEN_PX[sym], 6000, 9, 16, 10), _now(9, 16, 10))


def _trigger(svc, sym, h=9, m=17, s=12):
    level = svc.core.sym[sym]["fc"]["high"]
    svc._handle_raw(*_frame(sym, level + 0.5, 6000 + 9000, h, m, s), _now(h, m, s))


def _rows(symbol=None):
    from database.open15_breakout_db import Open15Trade, db_session

    db_session.expire_all()
    q = db_session.query(Open15Trade)
    if symbol:
        q = q.filter(Open15Trade.symbol == symbol)
    return q.order_by(Open15Trade.id).all()


# --------------------------------------------------------------------------- #
# 1. The crash: a cap-skipped OPTION trigger must be measured, not lost
# --------------------------------------------------------------------------- #
def test_a_cap_skipped_option_trigger_is_priced_instead_of_raising(monkeypatch):
    """The exact 2026-08-19 path.

    ``_sim_context`` is only reached in option mode from the ``max_trades_cap``
    branch, which is why every existing test missed it: the option branch of
    that function had no coverage at all, and the stock branch never touches
    ``_option_liquidity``.
    """
    orders = []
    svc = _mk_service(orders, monkeypatch, cfg={"max_trades": 1})
    _run_to_selection(svc)
    _trigger(svc, "AAA", m=17)
    _trigger(svc, "BBB", m=18)  # the cap is spent -> sim-priced skip

    assert len(orders) == 1, "the cap still bounds real orders"
    row = _rows("BBB")[-1]
    assert row.status == "skipped" and row.reason == "max_trades_cap"
    assert row.fill == "sim", "the miss must be priced, which is what #555 exists for"
    assert row.opt_entry_premium == 112.35 and row.opt_lot_size == 125
    # the #555 columns ride in the same quote response — free, and a sim row
    # without them cannot be compared with a real one
    assert row.opt_entry_bid == 111.85 and row.opt_entry_ask == 112.35
    assert row.opt_tick_size == 0.05
    events = [e for e in svc.day_log if e["event"] == "entry_skipped"]
    assert [e["symbol"] for e in events] == ["BBB"]
    assert not [e for e in svc.day_log if e["event"] == "entry_error"]


# --------------------------------------------------------------------------- #
# 2. A raise inside _enter is journaled, logged and alerted — never silent
# --------------------------------------------------------------------------- #
def test_an_entry_that_raises_becomes_a_terminal_error_row(monkeypatch):
    """Pre-#643 the symbol simply disappeared from every consumer.

    Asserted against the real ``_handle_raw`` pipeline with the failure injected
    at the order placer, because the guard lives at the call site — a test that
    called ``_enter`` directly would prove nothing about the tick path.
    """

    def exploding_placer(mode, order):
        raise RuntimeError("broker client blew up")

    svc = _mk_service([], monkeypatch)
    svc.order_placer = exploding_placer
    _run_to_selection(svc)
    _trigger(svc, "AAA")

    row = _rows("AAA")[-1]
    assert row.status == "error" and row.reason == "entry_error"
    assert row.fill is None, "an error row must not join ANY P&L bucket"
    assert row.quantity == 0 and "RuntimeError" in row.error_message

    ev = [e for e in svc.day_log if e["event"] == "entry_error"]
    assert len(ev) == 1
    assert ev[0]["symbol"] == "AAA" and "broker client blew up" in ev[0]["error"]
    assert ev[0]["slot_released"] is True


def test_a_raising_entry_does_not_stop_the_next_symbol(monkeypatch):
    """One symbol's failure must not cost every other symbol its ticks."""
    orders = []
    state = {"boom": True}

    def flaky(mode, order):
        if state["boom"]:
            state["boom"] = False
            raise RuntimeError("transient")
        orders.append(order)
        return {"status": "success", "orderid": "T-2"}

    svc = _mk_service([], monkeypatch)
    svc.order_placer = flaky
    _run_to_selection(svc)
    _trigger(svc, "AAA", m=17)
    _trigger(svc, "BBB", m=18)

    assert len(orders) == 1, "the second trigger still reached the broker"
    assert [r.status for r in _rows("AAA")] == ["error"]
    assert [r.status for r in _rows("BBB")] == ["open"]


def test_the_error_row_is_not_counted_as_an_entry_by_the_digest():
    """``entered`` counts fills. A trigger that raised placed nothing."""
    from services.open15_log_view import summarize_day

    events = [
        {"event": "armed", "universe": 4},
        {"event": "selection", "selected": {"AAA": "L"}, "gaps_pct": {}},
        {"event": "entry_error", "symbol": "AAA", "error": "RuntimeError: x"},
    ]
    dig = summarize_day(TRADE_DATE, events)
    assert dig["entered"] == 0 and dig["errors"] == 1


def test_the_js_and_python_row_builders_agree_on_an_entry_error():
    """The /logs page has twice gone dark on an event nobody taught it (#615,
    #622). The parity harness runs the page's OWN JS, so a Python-only fix
    cannot pass here."""
    from services.open15_log_view import selection_outcomes
    from test.test_open15_log_view import _run_render_sel

    events = [
        {"ts": "09:10", "event": "armed", "universe": 4},
        {"ts": "09:16", "event": "selection", "selected": {"AAA": "L"}, "gaps_pct": {"AAA": 3.0}},
        {
            "ts": "09:17",
            "event": "entry_error",
            "symbol": "AAA",
            "trigger_price": 103.5,
            "at": "09:17:12",
            "error": "ValueError: too many values to unpack (expected 3)",
        },
    ]
    js = _run_render_sel(events)
    py = {r["symbol"]: r for r in selection_outcomes(TRADE_DATE, events)}
    assert set(js) == set(py)
    assert py["AAA"]["skip_reason"] == "entry_error" and py["AAA"]["entered"] is False
    # the page must SAY it failed rather than leave the selection-time default
    assert "no trigger" not in js["AAA"]["out"]
    assert "entry failed" in js["AAA"]["out"]


def test_the_chips_row_runs_without_a_reference_error():
    """A variable used but never declared is INVISIBLE to ``node --check``.

    Caught in review: the ``errors`` count reached the chips string while its
    ``const`` did not land in the file. The page PARSED, and ``renderChips``
    threw ``ReferenceError: errors is not defined`` at runtime — which on this
    page means the whole day goes blank, the #615/#622 failure mode one more
    time. Parsing is not running, so this EXECUTES the real extracted function.
    """
    import json
    import shutil
    import subprocess

    from blueprints.open15_breakout import _LOGS_PAGE

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    body = _LOGS_PAGE.split("function renderChips(){")[1].split("\nfunction srcBadge(")[0]
    # the split lands after renderChips' own closing brace — drop it, the
    # wrapper below supplies one
    body = body.rstrip().removesuffix("}")
    events = [
        {"event": "armed", "universe": 4, "mode": "live", "vol_mult": 1.5},
        {"event": "selection", "selected": {"AAA": "L"}, "gaps_pct": {}},
        {"event": "entry_error", "symbol": "AAA", "error": "boom"},
        {"event": "summary", "day": "done", "selected": 1},
    ]
    script = (
        "const esc=s=>String(s);\n"
        "const out={};\n"
        "const document={getElementById:()=>({set innerHTML(v){out.html=v;}})};\n"
        f"const curEvents={json.dumps(events)};\n"
        "const curJournal=[];\n"
        "const curDate='2026-08-19';\n"
        "const digests=[{date:'2026-08-19',status:'done',selected:1,entered:0,"
        "paper:0,sim:0,shadow:0,errors:1,pnl:null}];\n"
        f"function renderChips(){{{body}}}\n"
        "renderChips();console.log(JSON.stringify(out));"
    )
    out = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=60, check=False
    )
    assert out.returncode == 0, out.stderr
    assert "1 error" in json.loads(out.stdout)["html"]


# --------------------------------------------------------------------------- #
# 3. resolve_entry_budget — the pure sizing decision
# --------------------------------------------------------------------------- #
def test_the_2026_08_19_residual_buys_two_lots():
    """The incident, in one assertion.

    Rs39,730 left, 3% reserve, the GVT&D 4100 PE at Rs112.35 x 125 = Rs14,043.75
    per lot: 2 lots for Rs28,087.50 — the trade that was dropped.
    """
    budget, basis = resolve_entry_budget(60_000, 39_730, 3.0)

    assert basis == "residual"
    assert int(budget // (112.35 * 125)) == 2
    assert round(budget, 2) == 38_538.10


def test_a_full_slot_is_preferred_whenever_the_cash_is_there():
    """Residual sizing spends leftovers; it never shrinks a funded trade."""
    assert resolve_entry_budget(60_000, 200_000, 3.0) == (60_000, "slot")
    # exactly affordable including the reserve
    assert resolve_entry_budget(60_000, 61_856, 3.0)[1] == "slot"


def test_an_unknown_balance_fails_open_to_the_full_slot():
    """``None`` is not zero. The ledger not knowing must never halve a position
    — the broker is the backstop and a rejection is journaled correctly."""
    assert resolve_entry_budget(60_000, None, 3.0) == (60_000, "slot")


def test_the_reserve_is_held_back_only_from_the_residual():
    """A full slot is a number the operator chose; the residual is our own
    arithmetic against a balance that must also cover charges."""
    assert resolve_entry_budget(60_000, 40_000, 0.0)[0] == 40_000
    assert resolve_entry_budget(60_000, 40_000, 10.0)[0] == 36_000


def test_a_negative_or_spent_balance_buys_nothing():
    assert resolve_entry_budget(60_000, 0, 3.0) == (0.0, "residual")
    assert resolve_entry_budget(60_000, -5_000, 3.0) == (0.0, "residual")


@pytest.mark.parametrize(
    ("value", "expected"),
    [(-1, 0.0), (0, 0.0), (3, 3.0), (25, 25.0), (99, 25.0), ("x", 3.0), (None, 3.0)],
)
def test_reserve_pct_is_clamped_not_rejected(value, expected):
    assert clamp_residual_reserve_pct(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"), [(0, 1), (1, 1), (4, 4), (99, 10), ("x", 1), (None, 1)]
)
def test_min_lots_is_clamped_not_rejected(value, expected):
    assert clamp_residual_min_lots(value) == expected


# --------------------------------------------------------------------------- #
# 4. The ledger: spend once, and give it back on every non-fill
# --------------------------------------------------------------------------- #
def test_the_residual_entry_is_sized_from_what_earlier_fills_left(monkeypatch):
    """End to end: two full slots, then a third trigger on the remainder."""
    orders = []
    svc = _mk_service(
        orders, monkeypatch, cfg={"residual_sizing_enabled": True}, premium=112.35, lotsize=125
    )
    svc._cash_at_arm = 161_365.10
    _run_to_selection(svc)
    # 60,000 // 14,043.75 = 4 lots = 500 qty = Rs56,175 per full-slot entry
    _trigger(svc, "AAA", m=17)
    _trigger(svc, "BBB", m=18)
    _trigger(svc, "CCC", m=19)

    # 4 lots each for the two funded slots; the third gets what is left
    assert [o["quantity"] for o in orders] == [500, 500, 375]
    third = _rows("CCC")[-1]
    assert third.sizing_basis == "residual" and third.quantity == 375
    assert [r.sizing_basis for r in _rows("AAA")] == ["slot"]
    ev = [e for e in svc.day_log if e["event"] == "entry" and e["symbol"] == "CCC"]
    assert ev[0]["sizing_basis"] == "residual"
    # 161,365.10 - 2 x 56,175 = 49,015.10, less the 3% reserve
    assert round(ev[0]["slot_capital_used"], 2) == 47_544.65


def test_the_same_cash_is_never_committed_twice(monkeypatch):
    """Two triggers in the same second are sized against what is LEFT.

    The reservation is taken before the order is placed precisely so the second
    cannot be sized against money the first has already spent.
    """
    orders = []
    # Rs5,000/lot, so the first entry takes exactly the Rs60,000 slot (12 lots)
    # and the Rs10,000 left is still worth one more lot
    svc = _mk_service(orders, monkeypatch, cfg={"residual_sizing_enabled": True}, premium=40.0)
    svc._cash_at_arm = 70_000.0
    _run_to_selection(svc)
    _trigger(svc, "AAA", m=17, s=10)
    _trigger(svc, "BBB", m=17, s=11)

    spent = sum(o["quantity"] * 40.0 for o in orders)
    assert spent <= 70_000, f"the ledger over-spent: Rs{spent:,.0f} of Rs70,000"
    assert [o["quantity"] for o in orders] == [1500, 125], "the leftover bought one lot"


def test_a_rejected_entry_gives_its_cash_back(monkeypatch):
    """Nothing was bought. Without the release every later entry shrinks."""
    rejected = {"n": 0}

    def placer(mode, order):
        rejected["n"] += 1
        if rejected["n"] == 1:
            return {"status": "error", "message": "IP not allowed"}
        return {"status": "success", "orderid": "T-2"}

    svc = _mk_service([], monkeypatch, cfg={"residual_sizing_enabled": True})
    svc.order_placer = placer
    svc._cash_at_arm = 100_000.0
    _run_to_selection(svc)
    _trigger(svc, "AAA", m=17)

    assert svc._cash_remaining() == 100_000.0, "a refused order spends nothing"
    _trigger(svc, "BBB", m=18)
    assert _rows("BBB")[-1].sizing_basis == "slot", "the second entry still gets a full slot"


def test_an_entry_that_raises_gives_its_cash_back(monkeypatch):
    svc = _mk_service([], monkeypatch, cfg={"residual_sizing_enabled": True})
    svc._cash_at_arm = 100_000.0

    def exploding(mode, order):
        raise RuntimeError("boom")

    svc.order_placer = exploding
    _run_to_selection(svc)
    _trigger(svc, "AAA")

    assert svc._cash_remaining() == 100_000.0


def test_a_post_ack_rejection_gives_its_cash_back(monkeypatch):
    """#626's shape: HTTP 200 with an order id, then RMS refuses it."""
    import services.open15_fill_reconcile as fr

    svc = _mk_service([], monkeypatch, cfg={"residual_sizing_enabled": True})
    svc._cash_at_arm = 100_000.0
    _run_to_selection(svc)
    _trigger(svc, "AAA")
    assert svc._cash_remaining() < 100_000.0, "the ACK'd order reserved its cash"

    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: "k", raising=False)
    monkeypatch.setattr(
        fr,
        "fetch_fill",
        lambda order_id, api_key: {
            "order_status": "rejected",
            "price": None,
            "qty": 0,
            "message": "Insufficient funds",
        },
    )
    assert svc.verify_entries() == 1
    assert svc._cash_remaining() == 100_000.0


def test_a_residual_too_small_for_the_minimum_says_which_constraint_bound(monkeypatch):
    """Not a bare "unaffordable": the slot could have paid, the residual could
    not, and those are different facts about the day."""
    orders = []
    svc = _mk_service(
        orders, monkeypatch, cfg={"residual_sizing_enabled": True, "residual_min_lots": 3}
    )
    svc._cash_at_arm = 90_000.0
    _run_to_selection(svc)
    _trigger(svc, "AAA", m=17)  # takes a full slot (4 lots, Rs56,175)
    _trigger(svc, "BBB", m=18)  # Rs33,825 left -> 2 lots, below the 3-lot floor

    assert len(orders) == 1
    row = _rows("BBB")[-1]
    assert row.status == "skipped" and row.reason == "unaffordable_residual"
    assert row.sizing_basis == "residual"


# --------------------------------------------------------------------------- #
# 5. Off by default is byte-identical to pre-#643
# --------------------------------------------------------------------------- #
def test_residual_sizing_is_off_unless_it_is_turned_on(monkeypatch):
    monkeypatch.delenv("OPEN15_RESIDUAL_SIZING", raising=False)
    cfg = resolve_day_config(None, 0.0)
    assert cfg["residual_sizing_enabled"] is False
    assert cfg["residual_reserve_pct"] == 3.0 and cfg["residual_min_lots"] == 1


def test_a_stored_false_beats_a_true_env_seed(monkeypatch):
    monkeypatch.setenv("OPEN15_RESIDUAL_SIZING", "true")
    assert resolve_day_config({"residual_sizing_enabled": False}, 0.0)[
        "residual_sizing_enabled"
    ] is (False)


def test_with_the_flag_off_the_ledger_never_clamps_a_size(monkeypatch):
    """The rollback path: no cash read, no residual, no behaviour change."""
    orders = []
    svc = _mk_service(orders, monkeypatch)  # flag off
    svc._cash_at_arm = 10.0  # would bind hard if it were consulted
    _run_to_selection(svc)
    _trigger(svc, "AAA")

    assert svc._cash_remaining() is None
    assert orders[0]["quantity"] == 500, "full slot sizing, exactly as before #643"
    assert _rows("AAA")[-1].sizing_basis == "slot"


def test_the_funds_clamp_still_shrinks_max_trades_when_residual_is_off():
    """``clamp_slots_to_funds`` itself is untouched — #626's guarantee stands."""
    assert clamp_slots_to_funds(60_000, 3, 161_365.10)[0] == 2
