"""Grade scorecard + grade backfill (issue #748).

Pins:

- ``describe_grades`` is built from the grader's own constants;
- ``net_of_row_under``: both flags on == ``net_pnl_of_row``; an undone stop /
  trail uses the held-to-exit counterfactual; an unpriced one is None (pending);
- ``max_drawdown`` on a hand-computed sequence;
- ``build_scorecard``: per-grade overall / long / short win rates, the two
  cohorts kept apart, the toggles move only real rows, windows, tape x side;
- the DB row set (real closed + full-slot paper/shadow, never sim/watched);
- the endpoint and the card's presence on /logs;
- the backfill grades through the SAME grader, only NULL-rating rows, and
  refuses a thin capture.
"""

import json

import pytest

from database import open15_breakout_db as o15db
from services import open15_rating as rt
from services.open15_grade_scorecard import build_scorecard, max_drawdown, tape_bucket


@pytest.fixture(autouse=True)
def _clean_journal():
    o15db.init_db()
    o15db.db_session.query(o15db.Open15Trade).delete()
    o15db.db_session.commit()
    o15db.db_session.remove()
    yield
    o15db.db_session.query(o15db.Open15Trade).delete()
    o15db.db_session.commit()
    o15db.db_session.remove()


def _row(**kw):
    base = {
        "trade_date": "2026-09-16",
        "side": "L",
        "fill": "real",
        "cohort": "real",
        "reason": "eod_0930",
        "pnl": 0.0,
        "charges_inr": 0.0,
        "cf_pnl": None,
        "cf_charges_inr": None,
        "rating": "A",
        "rating_source": "live",
        "rating_univ_median_pct": 0.1,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# descriptions
# --------------------------------------------------------------------------- #
def test_descriptions_track_the_rules():
    d = rt.describe_grades()
    assert set(d) == {"A", "B", "C"}
    assert "09:22" in d["A"]["text"] and f"{rt.CLEAN_VOL_MAX:g}x" in d["A"]["text"]
    assert f"{rt.MARKET_MIN_PCT:+.2f}%" in d["A"]["text"]
    assert "09:24" in d["C"]["text"] and f"{rt.MARKET_MIN_PCT:+.2f}%" in d["C"]["text"]
    assert "09:24" in d["B"]["text"] and "09:22" in d["B"]["text"]


def test_rating_kw_stamps_live_source():
    kw = rt.rating_kw({"rating": "B", "rating_inputs": {"univ_median_pct": 0.1, "vol_ratio": 1.6}})
    assert kw["rating_source"] == rt.RATING_SOURCE_LIVE
    assert rt.rating_kw({}) == {}


# --------------------------------------------------------------------------- #
# net under toggles
# --------------------------------------------------------------------------- #
class TestNetUnder:
    def test_as_traded_equals_net(self):
        r = _row(reason="stop_loss", pnl=-2500.0, charges_inr=100.0, cf_pnl=800.0)
        assert o15db.net_of_row_under(r, True, True) == pytest.approx(-2600.0)

    def test_undo_stop(self):
        r = _row(reason="stop_loss", pnl=-2500.0, charges_inr=100.0, cf_pnl=800.0,
                 cf_charges_inr=90.0)  # fmt: skip
        assert o15db.net_of_row_under(r, False, True) == pytest.approx(710.0)
        # the trail flag does not touch a stop row
        assert o15db.net_of_row_under(r, True, False) == pytest.approx(-2600.0)

    def test_undo_trail(self):
        r = _row(reason="profit_trail", pnl=5000.0, charges_inr=100.0, cf_pnl=3000.0,
                 cf_charges_inr=100.0)  # fmt: skip
        assert o15db.net_of_row_under(r, True, False) == pytest.approx(2900.0)
        assert o15db.net_of_row_under(r, False, True) == pytest.approx(4900.0)

    def test_unpriced_counterfactual_is_pending(self):
        r = _row(reason="stop_loss", pnl=-2500.0, cf_pnl=None)
        assert o15db.net_of_row_under(r, False, False) is None

    def test_timed_exit_unmoved(self):
        r = _row(reason="eod_0930", pnl=700.0, charges_inr=50.0)
        assert o15db.net_of_row_under(r, False, False) == pytest.approx(650.0)


# --------------------------------------------------------------------------- #
# drawdown + buckets
# --------------------------------------------------------------------------- #
def test_max_drawdown_hand_computed():
    # cum: 100, 300, 50, -150, 250, 100 -> peak 300 (d2) trough -150 (d4) = -450
    pts = [("d1", 100), ("d2", 200), ("d3", -250), ("d4", -200), ("d5", 400), ("d6", -150)]
    assert max_drawdown(pts) == {"dd": -450.0, "peak_date": "d2", "trough_date": "d4"}


def test_max_drawdown_first_loss_counts_from_zero():
    assert max_drawdown([("d1", -100), ("d2", 50)]) == {
        "dd": -100.0,
        "peak_date": None,
        "trough_date": "d1",
    }
    assert max_drawdown([])["dd"] == 0.0


def test_tape_bucket_edges():
    assert tape_bucket(-0.30) == "down" and tape_bucket(-0.29) == "flat"
    assert tape_bucket(0.30) == "up" and tape_bucket(None) == "unknown"


# --------------------------------------------------------------------------- #
# build_scorecard (pure)
# --------------------------------------------------------------------------- #
def _sample():
    return [
        _row(trade_date="2026-09-01", rating="A", side="L", pnl=1000.0, rating_source="backfill"),
        _row(
            trade_date="2026-09-02",
            rating="A",
            side="S",
            reason="stop_loss",
            pnl=-2000.0,
            cf_pnl=500.0,
        ),  # fmt: skip
        _row(
            trade_date="2026-09-16", rating="C", side="S", pnl=-300.0, rating_univ_median_pct=-0.5
        ),  # fmt: skip
        _row(
            trade_date="2026-09-17",
            rating="C",
            side="L",
            fill="shadow",
            cohort="paper",
            pnl=-900.0,
            rating_univ_median_pct=-0.4,
        ),  # fmt: skip
        _row(
            trade_date="2026-09-18", rating=None, side="L", pnl=200.0, rating_univ_median_pct=None
        ),  # fmt: skip
    ]


class TestBuild:
    def test_as_traded(self):
        j = build_scorecard(_sample())
        real = j["cohorts"]["real"]
        a = real["grades"]["A"]
        assert a["n"] == 2 and a["wins"] == 1 and a["net"] == -1000.0
        assert a["long"] == {"n": 1, "wins": 1, "win_rate": 1.0, "net": 1000.0}
        assert a["short"]["n"] == 1 and a["short"]["win_rate"] == 0.0
        assert a["max_dd"]["dd"] == -2000.0
        assert real["grades"]["C"]["n"] == 1  # the shadow row is NOT real
        assert real["grades"]["U"]["n"] == 1
        assert real["all"]["n"] == 4 and real["all"]["net"] == -1100.0
        full = j["cohorts"]["full_slot"]
        assert full["grades"]["C"]["n"] == 2 and full["grades"]["C"]["net"] == -1200.0
        assert full["all"]["n"] == 5
        assert j["n_backfilled"] == 1
        assert set(j["descriptions"]) == {"A", "B", "C"}

    def test_real_all_net_equals_sum_of_net_pnl(self):
        rows = _sample()
        real_rows = [r for r in rows if r["cohort"] == "real"]
        j = build_scorecard(rows, True, True)
        assert j["cohorts"]["real"]["all"]["net"] == pytest.approx(
            sum(o15db.net_pnl_of_row(r) for r in real_rows)
        )

    def test_undo_stop_moves_only_the_stop_row(self):
        j = build_scorecard(_sample(), apply_sl=False)
        a = j["cohorts"]["real"]["grades"]["A"]
        assert a["net"] == 1500.0 and a["short"]["win_rate"] == 1.0
        assert j["cohorts"]["full_slot"]["grades"]["C"]["net"] == -1200.0  # untouched

    def test_pending_excluded_and_counted(self):
        rows = _sample()
        rows[1]["cf_pnl"] = None
        j = build_scorecard(rows, apply_sl=False)
        assert j["pending"] == {"real": 1, "full_slot": 1}
        assert j["cohorts"]["real"]["grades"]["A"]["n"] == 1

    def test_windows(self):
        assert build_scorecard(_sample(), window="r63")["cohorts"]["full_slot"]["all"]["n"] == 2
        assert build_scorecard(_sample(), window="live")["cohorts"]["full_slot"]["all"]["n"] == 3
        assert build_scorecard(_sample(), window="bogus")["window"] == "all"

    def test_tape_side(self):
        t = build_scorecard(_sample())["cohorts"]["full_slot"]["tape_side"]
        assert t["down"]["short"]["n"] == 1 and t["down"]["long"]["n"] == 1
        assert t["flat"]["long"]["n"] == 1 and t["unknown"]["long"]["n"] == 1

    def test_empty(self):
        j = build_scorecard([])
        assert j["status"] == "ok" and j["cohorts"]["real"]["all"]["n"] == 0
        assert j["cohorts"]["real"]["all"]["win_rate"] is None


# --------------------------------------------------------------------------- #
# DB row set
# --------------------------------------------------------------------------- #
def test_grade_scorecard_rows_cohorts():
    m = "t748"
    ins = o15db.insert_trade
    ins(trade_date="2026-09-20", symbol="R1", side="L", mode=m, fill="real", status="closed",
        pnl=100.0, rating="A", trigger_minute="09:20", trigger_second=5)  # fmt: skip
    ins(trade_date="2026-09-20", symbol="R0", side="L", mode=m, fill="real", status="closed",
        pnl=50.0, rating="B", trigger_minute="09:18", trigger_second=0)  # fmt: skip
    ins(trade_date="2026-09-20", symbol="P1", side="S", mode=m, fill="paper", status="rejected",
        pnl=-50.0)  # fmt: skip
    ins(trade_date="2026-09-20", symbol="H1", side="S", mode=m, fill="shadow", status="skipped",
        pnl=20.0, reason="rating_excluded", rating="C")  # fmt: skip
    ins(trade_date="2026-09-20", symbol="S1", side="S", mode=m, fill="sim", status="skipped",
        pnl=30.0)  # fmt: skip
    ins(trade_date="2026-09-20", symbol="W1", side="L", mode=m, fill="watched", opt_pnl=10.0)
    ins(trade_date="2026-09-20", symbol="O1", side="L", mode=m, fill="real", status="open",
        pnl=5.0)  # fmt: skip
    rows = o15db.grade_scorecard_rows(m)
    assert [r["symbol"] for r in rows if r["cohort"] == "real"] == ["R0", "R1"]  # trigger order
    assert sorted(r["symbol"] for r in rows if r["cohort"] == "paper") == ["H1", "P1"]


# --------------------------------------------------------------------------- #
# endpoint + page
# --------------------------------------------------------------------------- #
def test_endpoint(monkeypatch):
    from flask import Flask

    import blueprints.open15_breakout as bp
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    seen = {}

    def fake(apply_sl, apply_trail, window, mode):
        seen.update(apply_sl=apply_sl, apply_trail=apply_trail, window=window, mode=mode)
        return {"status": "ok"}

    monkeypatch.setattr("services.open15_grade_scorecard.scorecard", fake)
    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    c = app.test_client()
    r = c.get("/open15_vol_breakout/api/grade_scorecard?apply_sl=0&window=live&mode=bad")
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert seen == {"apply_sl": False, "apply_trail": True, "window": "live", "mode": None}


def test_card_on_logs_page():
    from blueprints.open15_breakout import _LOGS_PAGE

    assert 'id="gscard"' in _LOGS_PAGE
    assert "loadGradeCard()" in _LOGS_PAGE and "api/grade_scorecard" in _LOGS_PAGE
    assert "Apply stop loss" in _LOGS_PAGE and "Apply profit-trail exit" in _LOGS_PAGE
    assert "gsAutoRefresh()" in _LOGS_PAGE and "60000" in _LOGS_PAGE  # keeps up with the day


# --------------------------------------------------------------------------- #
# backfill
# --------------------------------------------------------------------------- #
def _write_capture(tmp_path, day, n_syms, trig_at="09:20:30", down=False):
    d = tmp_path / "ticks"
    d.mkdir(exist_ok=True)
    lines = []
    for i in range(n_syms):
        sym = f"S{i:03d}"
        lines.append({"ts": f"{day}T09:15:00.500000", "symbol": sym, "ltp": 100.0, "volume": 1})
        px = 99.0 if down else 100.1
        lines.append({"ts": f"{day}T09:20:00.000000", "symbol": sym, "ltp": px, "volume": 2})
        # after the trigger — must NOT be seen by the grader
        lines.append({"ts": f"{day}T09:25:00.000000", "symbol": sym, "ltp": 90.0, "volume": 3})
    f = d / f"ticks-{day.replace('-', '')}-1.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n", encoding="utf-8")
    return str(d)


class TestBackfill:
    def test_grades_only_null_rows_through_the_same_grader(self, tmp_path):
        from services import open15_rating_backfill as bf

        day = "2026-08-20"
        tick_dir = _write_capture(tmp_path, day, 60)
        ins = o15db.insert_trade
        a_id = ins(trade_date=day, symbol="BF1", side="L", fill="real", status="closed",
                   trigger_minute="09:21", trigger_second=10, trigger_price=100.0,
                   baseline_vol=1000.0, cum_vol_at_trigger=1520.0, pnl=10.0)  # fmt: skip
        b_id = ins(trade_date=day, symbol="BF2", side="S", fill="real", status="closed",
                   trigger_minute="09:21", trigger_second=40, trigger_price=100.0,
                   baseline_vol=1000.0, cum_vol_at_trigger=1700.0, pnl=10.0)  # fmt: skip
        keep_id = ins(trade_date=day, symbol="BF3", side="L", fill="real", status="closed",
                      trigger_minute="09:21", trigger_second=0, trigger_price=100.0,
                      baseline_vol=1000.0, cum_vol_at_trigger=1500.0, pnl=10.0,
                      rating="C", rating_source="live")  # fmt: skip

        dry = bf.run(day, apply=False, tick_dir=tick_dir, opens_provider=_no_broker)
        assert dry["graded"] == 2 and dry["written"] == 0
        rep = bf.run(day, apply=True, tick_dir=tick_dir, opens_provider=_no_broker)
        assert rep["written"] == 2 and rep["by_grade"] == {"A": 1, "B": 1}

        got = {r["id"]: r for r in _rows_by_id([a_id, b_id, keep_id])}
        assert got[a_id]["rating"] == "A" and got[a_id]["rating_source"] == "backfill"
        assert got[a_id]["rating_univ_median_pct"] == pytest.approx(0.1)
        assert got[a_id]["rating_vol_ratio"] == pytest.approx(1.52)
        assert got[b_id]["rating"] == "B"  # 1.7x — not clean
        assert got[keep_id]["rating"] == "C" and got[keep_id]["rating_source"] == "live"
        # idempotent — nothing left to grade on this day
        assert bf.run(day, apply=True, tick_dir=tick_dir, opens_provider=_no_broker)["graded"] == 0

    def test_falling_tape_is_c(self, tmp_path):
        from services import open15_rating_backfill as bf

        day = "2026-08-21"
        tick_dir = _write_capture(tmp_path, day, 60, down=True)
        rid = o15db.insert_trade(trade_date=day, symbol="BF4", side="S", fill="real",
                                 status="closed", trigger_minute="09:20", trigger_second=30,
                                 trigger_price=99.0, baseline_vol=1000.0,
                                 cum_vol_at_trigger=1510.0, pnl=10.0)  # fmt: skip
        bf.run(day, apply=True, tick_dir=tick_dir, opens_provider=_no_broker)
        assert _rows_by_id([rid])[0]["rating"] == "C"

    def test_thin_capture_left_ungraded(self, tmp_path):
        from services import open15_rating_backfill as bf

        day = "2026-07-23"
        tick_dir = _write_capture(tmp_path, day, 5)
        rid = o15db.insert_trade(trade_date=day, symbol="BF5", side="L", fill="real",
                                 status="closed", trigger_minute="09:20", trigger_second=30,
                                 trigger_price=100.0, baseline_vol=1000.0,
                                 cum_vol_at_trigger=1510.0, pnl=10.0)  # fmt: skip
        rep = bf.run(day, apply=True, tick_dir=tick_dir, opens_provider=_no_broker)
        assert rep["skipped"] == {"thin_universe": 1} and rep["written"] == 0
        assert _rows_by_id([rid])[0]["rating"] is None


def _no_broker(_day):
    return {}


def test_broker_open_wins_over_first_tick(tmp_path):
    """The historify 09:15 open is the anchor; the first tick only fills gaps."""
    from services import open15_rating_backfill as bf

    day = "2026-08-26"
    tick_dir = _write_capture(tmp_path, day, 60)
    rid = o15db.insert_trade(trade_date=day, symbol="BF6", side="L", fill="real",
                             status="closed", trigger_minute="09:21", trigger_second=0,
                             trigger_price=100.0, baseline_vol=1000.0,
                             cum_vol_at_trigger=1510.0, pnl=10.0)  # fmt: skip
    # first ticks say +0.1 % (an A); the broker open of 100.5 makes it -0.40 % (a C)
    broker = {f"S{i:03d}": 100.5 for i in range(60)}
    rep = bf.run(day, apply=True, tick_dir=tick_dir, opens_provider=lambda _d: broker)
    assert rep["open_source"][day] == {"broker": 60, "first_tick": 0}
    assert _rows_by_id([rid])[0]["rating"] == "C"


def test_merge_opens_falls_back_per_symbol():
    from services.open15_rating_backfill import merge_opens

    opens, n = merge_opens({"X": 10.0, "Y": 20.0}, {"X": 11.0, "Z": 5.0})
    assert opens == {"X": 11.0, "Y": 20.0} and n == 1


def _rows_by_id(ids):
    try:
        rows = o15db.db_session.query(o15db.Open15Trade).filter(o15db.Open15Trade.id.in_(ids))
        return [
            {
                c: getattr(r, c)
                for c in (
                    "id",
                    "rating",
                    "rating_source",
                    "rating_univ_median_pct",
                    "rating_vol_ratio",
                )
            }  # fmt: skip
            for r in rows
        ]
    finally:
        o15db.db_session.remove()
