"""Tests for the open15 broker-OI watch-list filter (issue #595).

Zerodha blocks MIS orders on stock option contracts whose OI is below 500
LOTS — a per-contract, ABSOLUTE rule. On 2026-08-13 it rejected 4 of 5
entries (UNOMINDA 282 / KALYANKJIL 433 / ADANIENSOL 339 / BDL 460 lots)
while the one contract above the floor (ASHOKLEY, 2791) filled. The
percentile gate is structurally blind to this: KALYANKJIL sat at p96.

The filter mirrors the broker's rule at the ONLY moment slots are
allocated — seed selection and rolling additions — and nowhere else: at
entry the broker itself is the authority and a rejection lands in the
#548 paper path (whose ``test_rejection_releases_its_max_trades_slot``
already pins that a rejection frees its ``max_trades`` slot).

Load-bearing rules pinned here:
  - blocked candidates are skipped and the NEXT ranked name is promoted
    (always — an OI-blocked contract can fill under no variant, so there
    is no ``backfill_rank`` debate to have);
  - the rolling path is filtered too, with the same day-cached verdicts,
    so a persistently-thin name is quoted once, not every 30s;
  - SHADOW candidates are filtered identically (operator decision): a
    shadow fill on a contract the broker would block is unrealizable
    P&L, which is exactly what the #581 cohort must not accumulate;
  - fail OPEN three ways — no verdict, unknown OI (0/None, the Zerodha
    mapper's "not available"), and a raising/failing batch call (#390).
"""

import os

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import datetime as dt  # noqa: E402

from services.open15_breakout_service import (  # noqa: E402
    Open15BreakoutService,
    Open15Core,
    clamp_min_oi_lots,
    production_oi_filter,
    resolve_day_config,
)


def t(h, m, s=0):
    return dt.datetime(2026, 8, 13, h, m, s)


FIRST_CANDLES = {
    "GOOD": {"open": 102.0, "high": 103.0, "low": 101.0},
    "NEXT": {"open": 101.5, "high": 102.0, "low": 101.0},
    "THIN": {"open": 103.0, "high": 104.0, "low": 102.0},
    "SHORTY": {"open": 98.0, "high": 99.0, "low": 97.0},
}
PREV = dict.fromkeys(FIRST_CANDLES, 100.0)


def _fn(blocked: dict, calls: list | None = None):
    """Fake batch filter: ``blocked`` maps (symbol, side) -> oi_lots."""

    def fn(candidates):
        if calls is not None:
            calls.append([(c["symbol"], c["side"]) for c in candidates])
        out = {}
        for c in candidates:
            key = (c["symbol"], c["side"])
            out[key] = {
                "blocked": key in blocked,
                "oi_lots": blocked.get(key, 999.0),
                "opt_symbol": f"{c['symbol']}25AUG26XCE",
                "min_lots": 500,
            }
        return out

    return fn


def _core(fn, top_n=1, rolling=False, trade_side="both", shadow_side=None):
    return Open15Core(
        dict(PREV),
        top_n=top_n,
        await_snapshot=True,
        trade_side=trade_side,
        rolling_enabled=rolling,
        rolling_cadence_s=10,
        rolling_top_n=2,
        shadow_side=shadow_side,
        oi_filter_fn=fn,
    )


# ---------------------------------------------------------------------------
# seed path
# ---------------------------------------------------------------------------


def test_seed_skips_the_blocked_name_and_promotes_the_next():
    """THIN gaps hardest (+3%) but its ATM contract is under the floor — the
    slot must go to GOOD (+2%), the 2026-08-13 shape."""
    c = _core(_fn({("THIN", "L"): 282.0}))
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("THIN", 103.0, 1000, t(9, 16, 1))
    assert c.selected.get("GOOD") == "L"
    assert "THIN" not in c.selected
    ex = [e for e in c.liquidity_exclusions if e["reason"] == "oi_below_broker_min"]
    assert len(ex) == 1
    assert ex[0]["symbol"] == "THIN"
    assert ex[0]["watch_source"] == "seed"
    assert ex[0]["oi_lots"] == 282.0
    assert ex[0]["side"] == "long"


def test_no_verdict_fails_open():
    """A candidate the batch call could not judge is seeded — the broker's own
    rejection + the #548 paper path remain the backstop."""
    c = _core(lambda candidates: {})
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("THIN", 103.0, 1000, t(9, 16, 1))
    assert c.selected.get("THIN") == "L"


def test_a_raising_filter_fails_open():
    def boom(candidates):
        raise RuntimeError("broker down")

    c = _core(boom)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("THIN", 103.0, 1000, t(9, 16, 1))
    assert c.selected.get("THIN") == "L", "a broken OI check must never cost a selection"


def test_no_filter_checks_nothing():
    c = _core(None)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("THIN", 103.0, 1000, t(9, 16, 1))
    assert c.selected.get("THIN") == "L"
    assert c.liquidity_exclusions == []


# ---------------------------------------------------------------------------
# rolling path
# ---------------------------------------------------------------------------


def test_rolling_addition_is_filtered_and_verdicts_are_day_cached():
    """THIN rallies mid-window: it must not be added, and repeat passes must
    reuse the cached verdict instead of re-quoting every cadence tick."""
    calls: list = []
    c = _core(_fn({("THIN", "L"): 433.0}, calls), top_n=1, rolling=True)
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("GOOD", 102.0, 1000, t(9, 16, 1))
    assert c.selected.get("GOOD") == "L"
    n_seed_calls = len(calls)
    c.liquidity_exclusions.clear()
    for sym, px in (("THIN", 130.0), ("GOOD", 103.0)):
        c.on_tick(sym, px, 2000, t(9, 20, 0))
    c.maybe_rerank(t(9, 20, 5))
    assert "THIN" not in c.selected
    assert any(
        e["reason"] == "oi_below_broker_min" and e["watch_source"] == "rolling"
        for e in c.liquidity_exclusions
    )
    # second pass: THIN is still the top mover, but its verdict is cached
    c.maybe_rerank(t(9, 20, 20))
    assert len(calls) == n_seed_calls, "a day-cached verdict must not re-quote"


def test_shadow_candidates_are_filtered_identically():
    """long_only + shadow shorts: a thin PE candidate must not become a shadow
    row — unrealizable P&L must not enter the #581 cohort."""
    c = _core(
        _fn({("SHORTY", "S"): 100.0}),
        top_n=1,
        trade_side="long_only",
        shadow_side="S",
    )
    c.apply_first_candles(FIRST_CANDLES)
    c.on_tick("SHORTY", 98.0, 1000, t(9, 16, 1))
    assert "SHORTY" not in c.selected
    assert any(
        e["reason"] == "oi_below_broker_min" and e["side"] == "short"
        for e in c.liquidity_exclusions
    )


# ---------------------------------------------------------------------------
# production_oi_filter — verdicts from contract + batched quote
# ---------------------------------------------------------------------------


def _wire(monkeypatch, contracts: dict, quotes: dict, ok=True, api_key="k"):
    monkeypatch.setattr(
        "services.open15_option_shadow.resolve_atm_option",
        lambda sym, side, spot, td: contracts.get(sym),
    )
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: api_key)
    monkeypatch.setattr(
        "services.quotes_service.get_multiquotes",
        lambda payload, api_key=None: (
            ok,
            {"results": [{"symbol": s, "data": d} for s, d in quotes.items()]},
            200,
        ),
    )


def test_verdicts_from_live_quote_oi(monkeypatch):
    """282 lots blocked, 2791 allowed, exactly at the floor allowed — the rule
    is ``< min_lots``, mirroring the broker's own wording."""
    _wire(
        monkeypatch,
        {
            "UNOMINDA": {"symbol": "UNOCE", "lotsize": 550},
            "ASHOKLEY": {"symbol": "ASHCE", "lotsize": 5000},
            "EDGE": {"symbol": "EDGCE", "lotsize": 100},
        },
        {
            "UNOCE": {"oi": 155100},  # 282 lots — the real 2026-08-13 rejection
            "ASHCE": {"oi": 13955000},  # 2791 lots — the real fill
            "EDGE": {"oi": 50000},  # exactly 500 lots
        },
    )
    out = production_oi_filter(
        [
            {"symbol": "UNOMINDA", "side": "L", "spot": 1250.0},
            {"symbol": "ASHOKLEY", "side": "L", "spot": 180.0},
            {"symbol": "EDGE", "side": "L", "spot": 100.0},
        ],
        500,
        "2026-08-13",
    )
    assert out[("UNOMINDA", "L")]["blocked"] is True
    assert out[("UNOMINDA", "L")]["oi_lots"] == 282.0
    assert out[("ASHOKLEY", "L")]["blocked"] is False
    assert out[("EDGE", "L")]["blocked"] is False, "at the floor is tradeable"


def test_unknown_oi_is_never_thin(monkeypatch):
    """The Zerodha mapper defaults absent fields to 0 — 0/None/missing quote
    all mean "unknown", never "below the floor" (#555)."""
    _wire(
        monkeypatch,
        {
            "ZEROED": {"symbol": "ZCE", "lotsize": 100},
            "ABSENT": {"symbol": "ACE", "lotsize": 100},
        },
        {"ZCE": {"oi": 0}},  # ACE missing from the response entirely
    )
    out = production_oi_filter(
        [
            {"symbol": "ZEROED", "side": "L", "spot": 100.0},
            {"symbol": "ABSENT", "side": "L", "spot": 100.0},
        ],
        500,
        "2026-08-13",
    )
    assert out[("ZEROED", "L")]["blocked"] is False
    assert out[("ZEROED", "L")]["oi_lots"] is None
    assert out[("ABSENT", "L")]["blocked"] is False


def test_unresolved_contract_gets_no_verdict(monkeypatch):
    _wire(monkeypatch, {}, {})
    out = production_oi_filter([{"symbol": "GONE", "side": "L", "spot": 100.0}], 500, "2026-08-13")
    assert out == {}


def test_failed_batch_fails_open(monkeypatch):
    _wire(monkeypatch, {"AAA": {"symbol": "ACE", "lotsize": 100}}, {}, ok=False)
    out = production_oi_filter([{"symbol": "AAA", "side": "L", "spot": 100.0}], 500, "2026-08-13")
    assert out == {}


def test_no_api_key_fails_open(monkeypatch):
    _wire(monkeypatch, {"AAA": {"symbol": "ACE", "lotsize": 100}}, {}, api_key=None)
    out = production_oi_filter([{"symbol": "AAA", "side": "L", "spot": 100.0}], 500, "2026-08-13")
    assert out == {}


# ---------------------------------------------------------------------------
# config + wiring
# ---------------------------------------------------------------------------


def test_clamp_and_default():
    assert clamp_min_oi_lots(500) == 500
    assert clamp_min_oi_lots(-5) == 0
    assert clamp_min_oi_lots(99999) == 5000
    assert clamp_min_oi_lots("junk") == 500
    cfg = resolve_day_config(None, 0.0)
    assert cfg["option_min_oi_lots"] == 500
    assert resolve_day_config({"option_min_oi_lots": 0}, 0.0)["option_min_oi_lots"] == 0
    assert resolve_day_config({"option_min_oi_lots": 550}, 0.0)["option_min_oi_lots"] == 550


def test_build_oi_filter_only_in_option_mode_with_a_floor():
    svc = Open15BreakoutService(order_placer=lambda m, o: {"status": "success"})
    now = dt.datetime(2026, 8, 13, 9, 10)
    svc.day_config = resolve_day_config({"instrument": "stock"}, 0.0)
    assert svc._build_oi_filter(now) is None, "stock mode has nothing to mirror"
    svc.day_config = resolve_day_config({"instrument": "atm_option", "option_min_oi_lots": 0}, 0.0)
    assert svc._build_oi_filter(now) is None, "a floor of 0 is the off switch"
    svc.day_config = resolve_day_config({"instrument": "atm_option"}, 0.0)
    assert callable(svc._build_oi_filter(now))
