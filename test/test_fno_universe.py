"""Not in F&O -> not watched. A fact, not a gate (issue #647).

On **2026-08-19** SAMMAANCAP won a rolling watch slot at 09:16:31, triggered at
09:17:38, and died at ``skipped: no_option_contract``. It has no NFO derivatives
at all — today's master contract (downloaded 09:03:36, 214 underlyings) carries
only its NSE/BSE cash rows. EXIDEIND and NUVAMA are in the same state.

The system had already worked this out, twice, and did nothing:

    stage 1 · EXIDEIND / NUVAMA / SAMMAANCAP → no_option_contracts, enforced: FALSE
    stage 2 · SAMMAANCAP                     → no_option_contracts, enforced: FALSE

because the check lived inside ``LiquidityGate``, whose first line is
``if not self.enabled: return None``. Switching off the percentile JUDGEMENT
switched off the existence FACT with it.

Two properties matter more than anything else here, and they pull in opposite
directions:

1. a symbol with no contracts must be dropped **whatever any flag says** — it
   can fill under no variant, so watching it produces no data; and
2. a broken master-contract read must drop **nothing** — a real F&O exclusion is
   1-3 names, a failed instrument dump looks like 200, and darkening a universe
   because a read failed is the #390 shape (3 stale symbols of 216 held the
   whole scanner for a session).
"""

import datetime as dt
import json

import pytest

from services.fno_universe import (
    DEGRADE_IMPLAUSIBLE,
    DEGRADE_READ_FAILED,
    MIN_PLAUSIBLE_UNDERLYINGS,
    filter_to_fno,
)

# the real shape of 2026-08-19: 211 watched, 3 of them without contracts
WATCHED = {"CGPOWER", "MANKIND", "HAL", "SAMMAANCAP", "EXIDEIND", "NUVAMA"}
NO_CONTRACTS = {"SAMMAANCAP", "EXIDEIND", "NUVAMA"}


def _underlyings(extra=(), n=MIN_PLAUSIBLE_UNDERLYINGS + 10):
    """A plausible master contract: the tradeable names plus filler."""
    base = (WATCHED - NO_CONTRACTS) | set(extra)
    return base | {f"FILLER{i}" for i in range(n)}


# --------------------------------------------------------------------------- #
# 1. The fact
# --------------------------------------------------------------------------- #
def test_the_three_names_that_have_no_contracts_are_dropped():
    kept, dropped, degraded = filter_to_fno(WATCHED, _underlyings())

    assert degraded is None
    assert dropped == sorted(NO_CONTRACTS)
    assert kept == {"CGPOWER", "MANKIND", "HAL"}


def test_a_name_with_contracts_is_kept():
    kept, dropped, _ = filter_to_fno({"HAL"}, _underlyings())

    assert kept == {"HAL"} and dropped == []


def test_dropped_is_sorted_so_the_log_line_is_stable_day_to_day():
    _kept, dropped, _ = filter_to_fno({"ZZZZ", "AAAA", "MMMM"}, _underlyings())

    assert dropped == ["AAAA", "MMMM", "ZZZZ"]


def test_re_inclusion_needs_no_code_or_config_change():
    """The half with no coverage before #647: NSE ADDS a name back.

    The master contract is re-downloaded every morning and read fresh at the
    09:10 arm, so the same symbol against tomorrow's dump is simply kept. There
    is no list to edit and no flag to flip.
    """
    today = filter_to_fno({"SAMMAANCAP"}, _underlyings())
    tomorrow = filter_to_fno({"SAMMAANCAP"}, _underlyings(extra=["SAMMAANCAP"]))

    assert today[0] == set() and today[1] == ["SAMMAANCAP"]
    assert tomorrow[0] == {"SAMMAANCAP"} and tomorrow[1] == []


def test_the_reverse_direction_is_never_auto_added():
    """The master contract has 214 underlyings including 6 INDICES (NIFTY,
    BANKNIFTY, ...). This filter only ever REMOVES from the watch list — an
    index has no place in an equity universe, and growing one from the
    instrument dump would silently rewrite what the strategy trades."""
    kept, _dropped, _ = filter_to_fno({"HAL"}, _underlyings(extra=["NIFTY", "BANKNIFTY"]))

    assert kept == {"HAL"}, "nothing outside the watch list may appear"


# --------------------------------------------------------------------------- #
# 2. Fail OPEN — the two cases that could dark a trading day
# --------------------------------------------------------------------------- #
def test_a_raising_master_contract_read_drops_nothing(monkeypatch):
    import services.option_liquidity_service as ols

    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ols, "option_underlyings", boom)
    kept, dropped, degraded = filter_to_fno(WATCHED)

    assert degraded == DEGRADE_READ_FAILED
    assert kept == WATCHED and dropped == []


def test_an_empty_underlying_set_drops_nothing():
    """``option_underlyings`` swallows its own errors and returns an empty set,
    so "the read failed" arrives here disguised as "nothing is in F&O". Trusting
    it would dark the entire universe."""
    kept, dropped, degraded = filter_to_fno(WATCHED, set())

    assert degraded == DEGRADE_IMPLAUSIBLE
    assert kept == WATCHED and dropped == []


def test_an_implausibly_small_underlying_set_drops_nothing():
    """A partial instrument download. NSE stock F&O has held ~180-220 names for
    years (214 on 2026-08-19); 12 means the dump is broken, not that the segment
    shrank overnight."""
    kept, dropped, degraded = filter_to_fno(WATCHED, {f"X{i}" for i in range(12)})

    assert degraded == DEGRADE_IMPLAUSIBLE
    assert kept == WATCHED and dropped == []


@pytest.mark.parametrize("n", [MIN_PLAUSIBLE_UNDERLYINGS - 1, MIN_PLAUSIBLE_UNDERLYINGS])
def test_the_plausibility_floor_is_exact(n):
    unders = {f"X{i}" for i in range(n)}
    _kept, _dropped, degraded = filter_to_fno({"HAL"}, unders)

    assert (degraded is None) == (n >= MIN_PLAUSIBLE_UNDERLYINGS)


def test_a_degraded_answer_is_never_read_as_nothing_to_drop():
    """`dropped == []` is ambiguous on its own — it means both "all good" and
    "we could not tell". The caller must branch on ``degraded``, so this pins
    that a degraded result carries the FULL universe back, not a filtered one."""
    kept, dropped, degraded = filter_to_fno(WATCHED, set())

    assert degraded is not None
    assert kept == WATCHED, "a degraded read returns the input untouched"
    assert dropped == []


# --------------------------------------------------------------------------- #
# 3. The arm — the universe the strategy actually watches
# --------------------------------------------------------------------------- #
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


def _svc(instrument="atm_option"):
    from services.open15_breakout_service import (
        Open15BreakoutService,
        Open15Core,
        resolve_day_config,
    )

    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc.universe = set(WATCHED)
    svc.day_config = resolve_day_config({"instrument": instrument, "vol_mult": 1.5}, 0)
    svc._log_date = "2026-08-19"
    svc.core = Open15Core(dict.fromkeys(WATCHED, 100.0), vol_mult=1.5, top_n=3)
    svc.day_status = "armed"
    return svc


def test_the_arm_drops_them_from_the_watched_universe(monkeypatch):
    import services.fno_universe as fu

    svc = _svc()
    monkeypatch.setattr(
        fu,
        "filter_to_fno",
        lambda u, underlyings=None: (u - NO_CONTRACTS, sorted(NO_CONTRACTS), None),
    )
    prev = dict.fromkeys(WATCHED, 100.0)
    svc._apply_fno_filter(prev)

    assert svc.universe == {"CGPOWER", "MANKIND", "HAL"}
    # the armed event's `universe` and `prev_closes` must describe the same set
    assert set(prev) == svc.universe


def test_the_arm_logs_one_enforced_event_naming_the_symbols(monkeypatch):
    import services.fno_universe as fu

    svc = _svc()
    monkeypatch.setattr(
        fu,
        "filter_to_fno",
        lambda u, underlyings=None: (u - NO_CONTRACTS, sorted(NO_CONTRACTS), None),
    )
    svc._apply_fno_filter(dict.fromkeys(WATCHED, 100.0))

    ev = [e for e in svc.day_log if e["event"] == "universe_excluded"]
    assert len(ev) == 1
    assert ev[0]["stage"] == 0 and ev[0]["reason"] == "not_in_fno"
    # `enforced: true` is the whole point — the pre-#647 verdict said false
    assert ev[0]["enforced"] is True
    assert ev[0]["n_excluded"] == 3 and ev[0]["n_watched"] == 3
    assert [s["symbol"] for s in ev[0]["symbols"]] == sorted(NO_CONTRACTS)


def test_stock_mode_filters_nothing(monkeypatch):
    """In stock mode there is no contract to require, and shrinking the universe
    would silently change what the strategy watches."""
    import services.fno_universe as fu

    called = []
    monkeypatch.setattr(fu, "filter_to_fno", lambda *a, **k: called.append(1) or (set(), [], None))
    svc = _svc(instrument="stock")
    svc._apply_fno_filter({})

    assert svc.universe == WATCHED and not called


def test_a_degraded_read_leaves_the_arm_universe_untouched(monkeypatch):
    import services.fno_universe as fu

    svc = _svc()
    monkeypatch.setattr(
        fu, "filter_to_fno", lambda u, underlyings=None: (set(u), [], DEGRADE_READ_FAILED)
    )
    svc._apply_fno_filter(dict.fromkeys(WATCHED, 100.0))

    assert svc.universe == WATCHED
    assert not [e for e in svc.day_log if e["event"] == "universe_excluded"]


def test_a_raising_filter_never_costs_the_trading_day(monkeypatch):
    import services.fno_universe as fu

    svc = _svc()

    def boom(*a, **k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(fu, "filter_to_fno", boom)
    svc._apply_fno_filter({})

    assert svc.universe == WATCHED


def test_a_dropped_symbol_cannot_be_resurrected_by_a_rolling_add(monkeypatch):
    """The universe filter is total because `_handle_raw` discards ticks for
    symbols outside it — so a dropped name never reaches `core.last_price`, and
    `maybe_rerank` ranks only what it has seen. One check at arm covers both the
    09:16 seed ranking and every rolling addition."""
    import services.fno_universe as fu

    svc = _svc()
    monkeypatch.setattr(
        fu,
        "filter_to_fno",
        lambda u, underlyings=None: (u - NO_CONTRACTS, sorted(NO_CONTRACTS), None),
    )
    svc._apply_fno_filter(dict.fromkeys(WATCHED, 100.0))

    svc._handle_raw(*_frame("SAMMAANCAP", 150.0, 5000, 9, 15, 30), _now(9, 15, 30))
    svc._handle_raw(*_frame("HAL", 101.0, 5000, 9, 15, 30), _now(9, 15, 30))

    assert "SAMMAANCAP" not in svc.core.sym
    assert "SAMMAANCAP" not in svc.core.last_price
    assert "HAL" in svc.core.sym, "the control symbol still flows"


# --------------------------------------------------------------------------- #
# 4. The WIRING — that arm() actually calls it
# --------------------------------------------------------------------------- #
def test_arm_applies_the_filter_and_stamps_the_real_universe(monkeypatch):
    """The hole #643 fell through: a method can be perfect and never called.

    Every test above drives ``_apply_fno_filter`` directly, so deleting the call
    from ``arm()`` leaves them all green. This one runs the real ``arm()`` and
    asserts on what the day was armed WITH — the universe the core holds and the
    counts the ``armed`` event stamps, which is what the /logs page renders.
    """
    import datetime as dtm

    import database.open15_breakout_db as db
    import services.fno_universe as fu
    import services.open15_breakout_service as svc_mod
    from services.open15_breakout_service import IST, Open15BreakoutService

    db.init_db()

    # a realistic 25-name universe: 22 tradeable, the 3 real-world exclusions.
    # >= 20 prev closes or arm() aborts with `skipped_no_prev_closes`.
    watched = {f"SYM{i}" for i in range(22)} | NO_CONTRACTS
    unders = {f"SYM{i}" for i in range(22)} | {f"FILLER{i}" for i in range(60)}

    monkeypatch.setattr(
        Open15BreakoutService,
        "_now_ist",
        staticmethod(lambda: IST.localize(dtm.datetime(2026, 8, 19, 9, 10, 0))),
    )
    monkeypatch.setattr(Open15BreakoutService, "_load_universe", staticmethod(lambda: set(watched)))
    monkeypatch.setattr(
        Open15BreakoutService,
        "_load_prev_closes",
        lambda self, universe, date: dict.fromkeys(universe, 100.0),
    )
    monkeypatch.setattr(svc_mod, "fetch_broker_prev_closes", lambda universe: {})
    monkeypatch.setattr(svc_mod, "verify_prev_closes", lambda prev, date: (prev, {}))
    monkeypatch.setattr(svc_mod, "read_available_cash", lambda: None)
    # arm() REBUILDS day_config from the DB row + env, so the instrument has to
    # come from there — assigning svc.day_config beforehand is overwritten, and
    # in stock mode this filter correctly does nothing
    monkeypatch.setattr(svc_mod, "_instrument_default", lambda: "atm_option")
    monkeypatch.setattr(fu, "option_underlyings", lambda: unders, raising=False)
    monkeypatch.setattr("services.option_liquidity_service.option_underlyings", lambda: unders)
    # keep the liquidity gate and the OI mirror out of it — this test is about
    # the F&O filter, and both of those read the DB
    monkeypatch.setattr(
        Open15BreakoutService, "_apply_liquidity_stage1", lambda self, today: (None, [])
    )
    monkeypatch.setattr(Open15BreakoutService, "_build_oi_filter", lambda self, now: None)
    monkeypatch.setattr(Open15BreakoutService, "_apply_exit_schedule", lambda self: None)

    svc = Open15BreakoutService(order_placer=lambda mode, order: {"status": "success"})
    svc.day_config = svc_mod.resolve_day_config({"instrument": "atm_option"}, 0)
    svc.arm()

    assert svc.day_status == "armed"
    assert svc.universe == {f"SYM{i}" for i in range(22)}
    assert not (svc.universe & NO_CONTRACTS)
    # the core was built from the FILTERED prev closes, so a dropped name has no
    # entry to rank from even if a stray tick arrived
    assert not (set(svc.core.prev_closes) & NO_CONTRACTS)

    armed = next(e for e in svc.day_log if e["event"] == "armed")
    assert armed["universe"] == 22, "the stamp must be what was watched, not what was listed"
    assert armed["prev_closes"] == 22, "universe and prev_closes must describe one set"
    ev = next(e for e in svc.day_log if e["event"] == "universe_excluded")
    assert ev["reason"] == "not_in_fno" and ev["enforced"] is True
