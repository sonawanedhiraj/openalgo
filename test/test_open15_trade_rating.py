"""R63 trade rating — grade at the trigger, grade filter, paper for the rest (issue #726).

Pins:

- the pure grader (``services/open15_rating.py``): boundaries at 09:22:00 /
  09:24:00 / -0.30 % / 1.55x, fail-open on unknown inputs, the 48 real R63
  fills reproduce the report's per-trade grades exactly;
- config resolution (``trade_grades`` env seed, stored subset, clamp);
- the ``_enter`` gate: a grade outside ``trade_grades`` is journaled as a
  full-slot SHADOW row with ``reason='rating_excluded'`` — no order, no
  ``max_trades`` slot; an included grade enters normally; ``ABC`` is
  byte-identical to the pre-#726 path; a raising grader never blocks an entry;
- every cohort carries the grade (sim ceiling skip included);
- the live status block and the config storage roundtrip;
- the /logs Python twin carries ``rating`` / ``rating_provisional``.
"""

import pytest

from services.open15_breakout_service import Open15BreakoutService, resolve_day_config
from services.open15_rating import (
    CLEAN_VOL_MAX,
    MARKET_MIN_PCT,
    grade_trigger,
    normalize_trade_grades,
    phase_label,
    sec_of_day,
    universe_median_pct,
)

DATE = "2026-09-15"
S0922 = 9 * 3600 + 22 * 60
S0924 = 9 * 3600 + 24 * 60

# (date, symbol, trigger_sec, univ_median_pct, vol_ratio, grade) — the 48 real
# fills from the R63 report §7, grades as published. If this table and the
# grader ever disagree, the report is wrong or the rule changed — either way
# the pre-registered decision on #726 has to be re-read before shipping.
R63_REAL_FILLS = [
    ("2026-08-06", "MUTHOOTFIN", 33633, -0.135, 1.62, "B"),
    ("2026-08-06", "HAL", 33719, -0.133, 1.515, "A"),
    ("2026-08-13", "ASHOKLEY", 33993, -0.037, 1.907, "C"),
    ("2026-08-14", "CUMMINSIND", 33717, -0.102, 1.668, "B"),
    ("2026-08-18", "DIXON", 33470, -0.098, 1.541, "A"),
    ("2026-08-18", "MOTHERSON", 33585, -0.095, 2.344, "B"),
    ("2026-08-19", "CGPOWER", 33517, -0.191, 1.505, "A"),
    ("2026-08-19", "MANKIND", 33857, -0.205, 1.649, "C"),
    ("2026-08-20", "PFC", 33531, -0.109, 1.533, "A"),
    ("2026-08-20", "HINDALCO", 33812, -0.104, 1.776, "B"),
    ("2026-08-21", "VEDL", 33597, -0.015, 1.621, "B"),
    ("2026-08-21", "BRITANNIA", 33644, -0.106, 1.505, "A"),
    ("2026-08-21", "PREMIERENE", 33704, -0.157, 1.647, "B"),
    ("2026-08-26", "HINDZINC", 33447, -0.071, 1.522, "A"),
    ("2026-08-26", "MAXHEALTH", 33517, -0.085, 2.618, "B"),
    ("2026-08-26", "LICHSGFIN", 33993, -0.004, 3.954, "C"),
    ("2026-08-27", "LICHSGFIN", 33631, 0.012, 2.074, "B"),
    ("2026-08-27", "TVSMOTOR", 33632, 0.009, 1.625, "B"),
    ("2026-08-27", "CGPOWER", 33759, -0.024, 1.548, "B"),
    ("2026-08-28", "WIPRO", 33465, -0.239, 1.534, "A"),
    ("2026-08-28", "TCS", 33476, -0.242, 1.531, "A"),
    ("2026-08-28", "INFY", 33526, -0.278, 1.53, "A"),
    ("2026-08-31", "LTF", 33509, -0.604, 1.86, "C"),
    ("2026-08-31", "HDFCBANK", 33579, -0.609, 1.525, "C"),
    ("2026-09-01", "DIVISLAB", 33518, -0.231, 1.503, "A"),
    ("2026-09-01", "HEROMOTOCO", 33631, -0.2, 1.511, "A"),
    ("2026-09-01", "POLYCAB", 33771, -0.238, 1.625, "B"),
    ("2026-09-02", "DLF", 33771, -0.567, 1.516, "C"),
    ("2026-09-02", "SUNPHARMA", 34060, -0.327, 1.671, "C"),
    ("2026-09-02", "IDEA", 34196, -0.423, 1.609, "C"),
    ("2026-09-03", "SBICARD", 33526, -0.149, 1.513, "A"),
    ("2026-09-03", "BANDHANBNK", 33632, -0.126, 1.725, "B"),
    ("2026-09-03", "ADANIPORTS", 33945, -0.314, 1.602, "C"),
    ("2026-09-04", "ANGELONE", 33648, 0.022, 1.5, "A"),
    ("2026-09-04", "UPL", 34031, 0.031, 1.614, "C"),
    ("2026-09-07", "BAJAJ-AUTO", 33646, -0.311, 1.514, "C"),
    ("2026-09-07", "IDEA", 33709, -0.377, 1.514, "C"),
    ("2026-09-07", "MCX", 33776, -0.358, 1.514, "C"),
    ("2026-09-08", "UNIONBANK", 33539, -0.102, 1.918, "B"),
    ("2026-09-08", "NATIONALUM", 33632, -0.084, 1.527, "A"),
    ("2026-09-08", "LODHA", 33694, -0.121, 1.645, "B"),
    ("2026-09-09", "MCX", 33651, -0.083, 1.504, "A"),
    ("2026-09-09", "BIOCON", 33655, -0.088, 1.565, "B"),
    ("2026-09-10", "VBL", 33599, -0.035, 1.505, "A"),
    ("2026-09-10", "CANBK", 33718, -0.045, 1.537, "A"),
    ("2026-09-10", "MCX", 33766, -0.203, 1.507, "B"),
    ("2026-09-11", "ONGC", 33651, -0.396, 1.566, "C"),
    ("2026-09-11", "TECHM", 33751, -0.437, 1.549, "C"),
]


# --------------------------------------------------------------------------- #
# pure grader
# --------------------------------------------------------------------------- #
class TestGrader:
    def test_boundaries(self):
        # 09:22:00 is still early; one second later is not
        assert grade_trigger(S0922, -0.1, 1.5)["grade"] == "A"
        assert grade_trigger(S0922 + 1, -0.1, 1.5)["grade"] == "B"
        # 09:24:00 is still B-eligible; one second later is closed -> C
        assert grade_trigger(S0924, -0.1, 1.5)["grade"] == "B"
        g = grade_trigger(S0924 + 1, -0.1, 1.5)
        assert g["grade"] == "C" and "closed" in g["fails"]
        # market: strictly above -0.30
        assert grade_trigger(S0922, MARKET_MIN_PCT + 0.001, 1.5)["grade"] == "A"
        g = grade_trigger(S0922, MARKET_MIN_PCT, 1.5)
        assert g["grade"] == "C" and "market_ok" in g["fails"]
        # volume: strictly below 1.55
        assert grade_trigger(S0922, -0.1, CLEAN_VOL_MAX - 0.001)["grade"] == "A"
        g = grade_trigger(S0922, -0.1, CLEAN_VOL_MAX)
        assert g["grade"] == "B" and g["fails"] == ["clean_vol"]

    def test_market_veto_beats_everything(self):
        # a perfect early clean trigger into a falling tape is still C
        assert grade_trigger(S0922 - 300, -0.5, 1.5)["grade"] == "C"

    def test_unknown_inputs_fail_open_and_are_named(self):
        g = grade_trigger(None, None, None)
        assert g["grade"] == "A"
        assert "time_unknown" in g["fails"] and "market_unknown" in g["fails"]
        # provisional (no ratio yet) cannot fail clean_vol
        assert grade_trigger(S0922, -0.1, None)["grade"] == "A"

    def test_r63_real_fills_reproduce_the_report(self):
        for _d, sym, sec, med, vr, expected in R63_REAL_FILLS:
            assert grade_trigger(sec, med, vr)["grade"] == expected, sym
        # and the headline counts the report publishes
        grades = [grade_trigger(s, m, v)["grade"] for _, _, s, m, v, _ in R63_REAL_FILLS]
        assert (grades.count("A"), grades.count("B"), grades.count("C")) == (17, 16, 15)

    def test_sec_of_day(self):
        assert sec_of_day("09:21", 33) == 9 * 3600 + 21 * 60 + 33
        assert sec_of_day("09:21", None) == 9 * 3600 + 21 * 60
        assert sec_of_day(None, 5) is None
        assert sec_of_day("garbage", 5) is None

    def test_universe_median(self):
        opens = {"A": 100.0, "B": 200.0, "C": 50.0, "D": 0.0}
        ltps = {"A": 101.0, "B": 198.0, "C": 50.0, "D": 10.0, "E": 5.0}
        med, n = universe_median_pct(opens, ltps)
        assert n == 3  # D has no open, E is not in opens
        assert med == 0.0
        assert universe_median_pct({}, {}) == (None, 0)

    def test_phase_label(self):
        assert phase_label(S0922 - 1, -0.1).startswith("A-eligible")
        assert phase_label(S0922 + 1, -0.1).startswith("B at best")
        assert phase_label(S0924 + 1, -0.1).startswith("C")
        assert phase_label(S0922 - 100, -0.31).startswith("C")


class TestNormalize:
    def test_subsets_and_garbage(self):
        assert normalize_trade_grades(None) == "ABC"
        assert normalize_trade_grades("") == "ABC"
        assert normalize_trade_grades("xyz") == "ABC"
        assert normalize_trade_grades("ba") == "AB"
        assert normalize_trade_grades(["C", "A"]) == "AC"
        assert normalize_trade_grades("a,b,c") == "ABC"
        assert normalize_trade_grades("C") == "C"

    def test_day_config_env_seed_and_stored_row(self, monkeypatch):
        monkeypatch.delenv("OPEN15_TRADE_GRADES", raising=False)
        assert resolve_day_config({}, 0.0)["trade_grades"] == "ABC"
        monkeypatch.setenv("OPEN15_TRADE_GRADES", "AB")
        assert resolve_day_config({}, 0.0)["trade_grades"] == "AB"
        assert resolve_day_config({"trade_grades": "A"}, 0.0)["trade_grades"] == "A"
        # an empty stored value falls through to the env seed, garbage to ABC
        assert resolve_day_config({"trade_grades": ""}, 0.0)["trade_grades"] == "AB"
        assert resolve_day_config({"trade_grades": "zz"}, 0.0)["trade_grades"] == "ABC"


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _clean_journal():
    from database.open15_breakout_db import Open15Trade, db_session, init_db

    init_db()
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()
    yield
    db_session.query(Open15Trade).delete()
    db_session.commit()
    db_session.remove()


def _mk_service(cfg=None, median=-0.1):
    orders = []

    def placer(mode, order):
        orders.append({"mode": mode, **order})
        return {"status": "success", "orderid": f"T-{len(orders)}"}

    svc = Open15BreakoutService(order_placer=placer)
    svc.day_status = "armed"
    svc._log_date = DATE
    svc.day_config = resolve_day_config({"instrument": "stock", **(cfg or {})}, 0.0)
    svc.positions = {}
    svc._entry_never_filled = lambda _s, _p: False
    svc._reconcile_and_log = lambda **_kw: None
    svc._persist_day_log = lambda: None
    # no core in these tests — pin the tape directly
    svc.universe_median = lambda: (median, 150)
    return svc, orders


def _action(symbol, ratio=1.5, minute="09:18", side="L"):
    return {
        "symbol": symbol,
        "side": side,
        "price": 100.0,
        "gap_pct": 2.0 if side == "L" else -2.0,
        "level": 99.0 if side == "L" else 101.0,
        "baseline_vol": 1000.0,
        "cum_vol_at_trigger": 1000.0 * ratio,
        "trigger_minute": minute,
        "trigger_second": 5,
        "watch_source": "seed",
        "shadow": False,
    }


def _events(svc, kind):
    return [e for e in svc.day_log if e.get("event") == kind]


def _rows():
    from database.open15_breakout_db import Open15Trade, db_session

    rows = [
        {
            "symbol": r.symbol,
            "status": r.status,
            "reason": r.reason,
            "fill": r.fill,
            "quantity": r.quantity,
            "sim_quantity": r.sim_quantity,
            "rating": r.rating,
            "rating_univ_median_pct": r.rating_univ_median_pct,
            "rating_vol_ratio": r.rating_vol_ratio,
        }
        for r in db_session.query(Open15Trade).all()
    ]
    db_session.remove()
    return rows


class TestEntryGate:
    def test_excluded_grade_is_paper_traded_as_shadow_no_order_no_slot(self):
        svc, orders = _mk_service({"trade_grades": "AB"})
        # 09:25 trigger -> closed -> C
        svc._enter(_action("CCC", minute="09:25"))
        assert orders == []
        ev = _events(svc, "entry_shadow")
        assert len(ev) == 1
        assert ev[0]["reason"] == "rating_excluded"
        assert ev[0]["rating"] == "C" and ev[0]["rating_excluded"] is True
        assert ev[0]["qty"] > 0  # full slot sizing, not the 1-lot sim convention
        rows = _rows()
        assert len(rows) == 1
        assert rows[0]["fill"] == "shadow" and rows[0]["reason"] == "rating_excluded"
        assert rows[0]["quantity"] == 0 and rows[0]["sim_quantity"] > 0
        assert rows[0]["rating"] == "C"
        assert rows[0]["rating_univ_median_pct"] == -0.1
        assert rows[0]["rating_vol_ratio"] == 1.5
        # no real slot consumed
        n_real, _p, _s, n_shadow = svc._count_fills()
        assert (n_real, n_shadow) == (0, 1)

    def test_included_grade_enters_normally_with_the_grade_on_the_row(self):
        svc, orders = _mk_service({"trade_grades": "AB"})
        svc._enter(_action("AAA", minute="09:18", ratio=1.5))
        assert len(orders) == 1
        ev = _events(svc, "entry")
        assert ev and ev[0]["rating"] == "A"
        rows = _rows()
        assert rows[0]["fill"] == "real" and rows[0]["rating"] == "A"
        assert svc.positions["AAA"]["rating"] == "A"

    def test_market_veto_grades_c_and_excludes(self):
        svc, orders = _mk_service({"trade_grades": "AB"}, median=-0.45)
        svc._enter(_action("MKT", minute="09:17", ratio=1.5))
        assert orders == []
        ev = _events(svc, "entry_shadow")
        assert ev and ev[0]["rating"] == "C"

    def test_abc_is_byte_identical_to_no_filter(self):
        svc, orders = _mk_service({"trade_grades": "ABC"})
        svc._enter(_action("CCC", minute="09:25"))
        assert len(orders) == 1
        assert _events(svc, "entry_shadow") == []
        assert _events(svc, "entry")[0]["rating"] == "C"

    def test_default_config_trades_everything(self, monkeypatch):
        monkeypatch.delenv("OPEN15_TRADE_GRADES", raising=False)
        svc, orders = _mk_service({})
        svc._enter(_action("CCC", minute="09:25"))
        assert len(orders) == 1

    def test_raising_grader_never_blocks_an_entry(self, monkeypatch):
        import services.open15_breakout_service as mod

        def boom(*_a, **_k):
            raise RuntimeError("grader exploded")

        monkeypatch.setattr(mod, "grade_trigger", boom)
        svc, orders = _mk_service({"trade_grades": "A"})
        svc._enter(_action("UNG", minute="09:25"))  # would be C -> excluded, if graded
        assert len(orders) == 1  # ungraded trades as before
        assert _events(svc, "entry")[0]["rating"] is None
        assert _rows()[0]["rating"] is None

    def test_ceiling_skip_still_carries_the_grade(self, monkeypatch):
        monkeypatch.setenv("OPEN15_SIM_SKIPPED_ENABLED", "true")
        svc, orders = _mk_service({"trade_grades": "AB", "max_vol_ratio": 1.7})
        svc._enter(_action("CAP", minute="09:18", ratio=2.6))
        assert orders == []
        sk = _events(svc, "entry_skipped")
        assert sk and sk[0]["reason"] == "vol_ratio_cap" and sk[0]["rating"] == "B"
        assert _rows()[0]["rating"] == "B"

    def test_shadow_side_row_is_graded_too(self):
        svc, orders = _mk_service({"trade_grades": "AB"})
        a = _action("SHD", minute="09:18")
        a["shadow"] = True
        svc._enter(a)
        assert orders == []
        ev = _events(svc, "entry_shadow")
        assert ev[0]["rating"] == "A" and ev[0]["rating_excluded"] is False
        assert ev[0]["reason"] != "rating_excluded"

    def test_status_block(self):
        svc, _orders = _mk_service({"trade_grades": "AB"})
        st = svc.rating_status()
        assert st["trade_grades"] == "AB"
        assert st["univ_median_pct"] == -0.1
        assert st["rules"]["clean_vol_max"] == CLEAN_VOL_MAX
        assert st["provisional"] == {}  # no core, nothing watched
        assert "rating" in svc.get_status()


class TestConfigStorage:
    def test_roundtrip(self):
        from database.open15_breakout_db import get_config, save_config

        assert save_config(60000.0, "fixed", 1.5, trade_grades="AB")
        assert get_config()["trade_grades"] == "AB"
        assert save_config(60000.0, "fixed", 1.5, trade_grades=None)
        assert get_config()["trade_grades"] is None

    def test_grade_breakdown(self):
        from database.open15_breakout_db import grade_breakdown, insert_trade

        insert_trade(
            trade_date=DATE,
            symbol="A1",
            side="L",
            fill="real",
            rating="A",
            pnl=500.0,
            charges_inr=50.0,
        )
        insert_trade(
            trade_date=DATE,
            symbol="A2",
            side="L",
            fill="real",
            rating="A",
            pnl=-100.0,
            charges_inr=50.0,
        )
        insert_trade(
            trade_date=DATE,
            symbol="C1",
            side="S",
            fill="shadow",
            rating="C",
            pnl=-800.0,
            charges_inr=40.0,
            reason="rating_excluded",
        )
        insert_trade(trade_date=DATE, symbol="C2", side="S", fill="shadow", rating="C", pnl=None)
        bg = grade_breakdown(DATE)
        assert bg["A"]["real"] == {"n": 2, "wins": 1, "net": 300.0}
        assert bg["A"]["paper"]["n"] == 0
        assert bg["C"]["paper"] == {"n": 1, "wins": 0, "net": -840.0}
        assert bg["C"]["unpriced"] == 1
        assert "B" not in bg


# --------------------------------------------------------------------------- #
# /logs Python twin
# --------------------------------------------------------------------------- #
def test_selection_outcomes_carry_the_grades():
    from services.open15_log_view import CSV_COLUMNS, selection_outcomes

    day = [
        {"ts": "09:10:00.000", "event": "armed", "trade_grades": "AB"},
        {
            "ts": "09:16:02.000",
            "event": "selection",
            "selected": {"AAA": "L", "CCC": "S"},
            "gaps_pct": {"AAA": 1.2, "CCC": -1.1},
            "rating_provisional": {
                "AAA": {"grade": "A", "fails": []},
                "CCC": {"grade": "A", "fails": []},
            },
        },
        {
            "ts": "09:19:00.000",
            "event": "watchlist_add",
            "symbol": "RRR",
            "side": "L",
            "pct_change": 0.9,
            "rating_provisional": {"RRR": {"grade": "B", "fails": ["early"]}},
        },
        {
            "ts": "09:18:00.000",
            "event": "entry",
            "symbol": "AAA",
            "rating": "A",
            "order_status": "success",
            "trigger_price": 100.0,
            "vol_ratio": 1.5,
        },
        {
            "ts": "09:25:00.000",
            "event": "entry_shadow",
            "symbol": "CCC",
            "rating": "C",
            "rating_excluded": True,
            "reason": "rating_excluded",
            "qty": 10,
            "trigger_price": 50.0,
        },
    ]
    rows = {r["symbol"]: r for r in selection_outcomes(DATE, day)}
    assert "rating" in CSV_COLUMNS and "rating_provisional" in CSV_COLUMNS
    assert rows["AAA"]["rating"] == "A" and rows["AAA"]["rating_provisional"] == "A"
    assert rows["CCC"]["rating"] == "C" and rows["CCC"]["skip_reason"] == "rating_excluded"
    assert rows["CCC"]["fill"] == "shadow"
    assert rows["RRR"]["rating"] is None and rows["RRR"]["rating_provisional"] == "B"


def test_logs_page_js_grade_cell_exists():
    """The chip must be in the JS builder too (the #615/#622 rule)."""
    import blueprints.open15_breakout as bp

    src = open(bp.__file__, encoding="utf-8").read()
    assert "function gradeCell(r)" in src
    assert "rating_excluded" in src
    assert "c_grA" in src and "trade_grades" in src
