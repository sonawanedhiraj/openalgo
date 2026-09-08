"""Settings Outlook engine + endpoint (issue #711).

The engine is pure over row dicts and minute paths, so every rule is tested
without a broker or a journal. The endpoint test proves the card can never
receive an empty box: a raising engine still answers HTTP 200 with a labelled
``status``.
"""

from __future__ import annotations

import pytest
from flask import Flask

from services import open15_settings_outlook as so


def _row(
    rid,
    date,
    symbol,
    side,
    net,
    charges=500.0,
    entry="09:18",
    exit_ts=None,
    qty=100,
    tick=0.05,
    prem=50.0,
    fill="real",
    reason="eod_0930",
):
    exit_ts = exit_ts or f"{date}T09:30:01+05:30"
    return {
        "id": rid,
        "date": date,
        "symbol": symbol,
        "side": side,
        "fill": fill,
        "net": net,
        "gross": net + charges,
        "charges": charges,
        "qty": qty,
        "tick": tick,
        "premium_paid": prem * qty,
        "ret_pct": net / (prem * qty) * 100.0,
        "entry_min": so._hhmm_to_min(entry),
        "exit_min": so._exit_min(exit_ts),
        "reason": reason,
    }


def _path(series: dict[str, float], available: bool = True):
    return {"available": available, "series": {so._hhmm_to_min(k): v for k, v in series.items()}}


# ---- statistics -----------------------------------------------------------
def test_wilson_and_breakeven():
    lo, hi = so.wilson(19, 41)
    assert 0.31 < lo < 0.33 and 0.60 < hi < 0.62
    assert so.breakeven_wr(7426.0, 4106.0) == pytest.approx(0.356, abs=0.001)
    assert so.breakeven_wr(None, 100.0) is None
    assert so.wilson(0, 0) == (None, None)


def test_describe_ex_best_and_recent():
    rows = [
        _row(i, "2026-09-01", f"S{i}", "long", v) for i, v in enumerate([100, -50, 900, -60, 40])
    ]
    st = so.describe(rows)
    assert st["n"] == 5 and st["wins"] == 3
    assert st["best"] == 900 and st["total_ex_best"] == pytest.approx(30)
    assert st["breakeven_wr_ex_best"] > st["breakeven_wr"]  # the outlier flattered breakeven
    assert st["recent"]["n"] == 5 and st["charges"] == 2500.0


def test_side_split_matches_dashboard_shape():
    rows = [
        _row(1, "d", "A", "long", 10),
        _row(2, "d", "B", "short", -5),
        _row(3, "d", "C", "long", -1),
    ]
    s = so.side_split(rows)
    assert s["long"]["n"] == 2 and s["long"]["wins"] == 1 and s["long"]["win_rate"] == 0.5
    assert s["short"]["n"] == 1 and s["short"]["total"] == -5


def test_pct_unit_for_mixed_scope():
    rows = [
        _row(1, "d", "A", "long", 1000, prem=50, qty=100),
        _row(2, "d", "B", "long", -250, prem=10, qty=500, fill="sim"),
    ]
    st = so.describe(rows, unit="pct")
    assert st["unit"] == "pct" and st["n"] == 2
    assert st["avg_win"] == pytest.approx(20.0) and st["avg_loss"] == pytest.approx(-5.0)
    assert "gross" not in st  # rupees never reported in the size-free unit


# ---- replay ---------------------------------------------------------------
def test_stop_kills_a_winner_that_dipped_through_it():
    # TCS shape: -3172 at 09:22, finishes +6561
    r = _row(1, "2026-08-28", "TCS", "long", 6561.0)
    paths = {
        "TCS": _path({"09:19": -800, "09:20": -1500, "09:22": -3172, "09:25": 2000, "09:29": 7000})
    }
    res = so.replay_day([r], paths, {"stop": 2500, "target": 0, "trail": 0, "max_trades": 3})
    assert res["stops"] == 1
    assert res["trades"][0]["reason"] == "stop_loss"
    assert res["trades"][0]["net"] == pytest.approx(-3172 - 500)
    wide = so.replay_day([r], paths, {"stop": 3500, "target": 0, "trail": 0, "max_trades": 3})
    assert wide["stops"] == 0 and wide["net"] == pytest.approx(6561.0)


def test_closed_form_row_truncates_final_loss_and_is_labelled():
    r = _row(1, "2026-08-14", "CUMMINSIND", "long", -4446.0, charges=446.0)
    res = so.replay_day([r], {}, {"stop": 2500, "target": 0, "trail": 0, "max_trades": 3})
    t = res["trades"][0]
    assert t["closed_form"] is True and t["reason"] == "stop_loss_cf"
    assert t["net"] == pytest.approx(-(2500 + 446))
    # a closed-form WINNER cannot be stopped (no marks) — the known optimism, kept visible
    w = _row(2, "2026-08-14", "X", "long", 3000.0)
    res2 = so.replay_day([w], {}, {"stop": 2500, "target": 0, "trail": 0, "max_trades": 3})
    assert res2["net"] == 3000.0 and res2["stops"] == 0


def test_lock_skips_later_entries_and_trail_flattens():
    a = _row(1, "2026-08-20", "PFC", "short", 17912.0, entry="09:17")
    b = _row(2, "2026-08-20", "HINDALCO", "short", -5329.0, entry="09:24")
    paths = {
        "PFC": _path(
            {"09:18": 3000, "09:20": 9000, "09:22": 12000, "09:26": 10000, "09:29": 18500}
        ),
        "HINDALCO": _path({"09:25": -1000, "09:29": -4800}),
    }
    res = so.replay_day([a, b], paths, {"stop": 0, "target": 8000, "trail": 0, "max_trades": 3})
    assert res["locked"] is True
    assert [t.get("skipped") for t in res["trades"] if t["symbol"] == "HINDALCO"] == ["lock"]
    assert res["net"] == pytest.approx(17912.0)  # only PFC, held to its exit
    # trail: peak 12000-500 at 09:22, 09:26 mark 10000-500 -> give-back 2000 >= 1500 flattens there
    tr = so.replay_day([a, b], paths, {"stop": 0, "target": 8000, "trail": 1500, "max_trades": 3})
    assert tr["trail"] is True
    assert tr["net"] == pytest.approx(10000 - 500)


def test_slot_cap_skips_the_fourth_entry():
    rows = [_row(i, "d", f"S{i}", "long", 100.0, entry=f"09:{16 + i}") for i in range(1, 5)]
    res = so.replay_day(rows, {}, {"stop": 0, "target": 0, "trail": 0, "max_trades": 3})
    assert sum(1 for t in res["trades"] if t.get("skipped") == "cap") == 1
    assert res["net"] == 300.0


def test_replay_totals_and_wrong_stop_count():
    win = _row(1, "2026-09-01", "W", "long", 4000.0)
    loss = _row(2, "2026-09-01", "L", "long", -6000.0)
    paths = {
        "W": _path({"09:20": -2600, "09:29": 4500}),
        "L": _path({"09:20": -2600, "09:29": -6500}),
    }
    out = so.replay(
        [win, loss], {"2026-09-01": paths}, {"stop": 2500, "target": 0, "trail": 0, "max_trades": 3}
    )
    assert out["stops"] == 2 and out["wrong_stops"] == 1
    assert out["net"] == pytest.approx(2 * (-2600 - 500))


# ---- projections + verdict ------------------------------------------------
def test_bootstrap_is_deterministic_and_ordered():
    a = so.bootstrap([1000.0, -500.0, 2000.0, -1500.0, 300.0])
    b = so.bootstrap([1000.0, -500.0, 2000.0, -1500.0, 300.0])
    assert a == b and a["p10"] <= a["p50"] <= a["p90"]
    assert so.bootstrap([1.0, 2.0]) is None


def test_verdict_three_checks():
    rows = [_row(i, "d", f"S{i}", "long", 5000.0 if i % 2 else -3000.0) for i in range(60)]
    st = so.describe(rows)
    v = so.verdict(st)
    assert v["status"] == "not_proven" or v["status"] == "edge_confirmed"
    assert len(v["checks"]) == 3 and v["checkpoint"]["target"] == so.CHECKPOINT_FILLS
    losing = so.describe(
        [_row(i, "d", f"S{i}", "long", 100.0 if i % 4 == 0 else -300.0) for i in range(40)]
    )
    assert so.verdict(losing)["status"] == "losing"
    assert so.verdict({"n": 0})["status"] == "no_data"


def test_sensitivity_line_crosses_zero_at_breakeven():
    st = so.describe(
        [
            _row(i, "d", f"S{i}", "long", v)
            for i, v in enumerate([6000, -4000, 6000, -4000, 6000, -4000])
        ]
    )
    sens = so.sensitivity(st, 2.0)
    be = st["breakeven_wr"]
    below = [v for wr, v in sens["line_all"] if wr < be - 0.02]
    above = [v for wr, v in sens["line_all"] if wr > be + 0.02]
    assert all(v < 0 for v in below) and all(v > 0 for v in above)


# ---- recommendations ------------------------------------------------------
def test_side_rule_excludes_a_below_breakeven_flat_side():
    longs = [_row(i, "d", f"L{i}", "long", 3000.0 if i % 2 else -2000.0) for i in range(20)]
    shorts = [
        _row(100 + i, "d", f"S{i}", "short", 30000.0 if i == 0 else -2000.0) for i in range(8)
    ]
    rows = longs + shorts
    st = so.describe(rows)
    recs = so.recommendations(
        rows,
        {},
        {"trade_side": "both", "stop_loss_enabled": False, "profit_lock_enabled": False},
        st,
        so.side_split(rows),
    )
    side = [r for r in recs if r["rule"] == "side"]
    assert side and side[0]["to"] == "long_only" and side[0]["status"] == "recommend"


def test_stop_rule_is_judged_on_the_traded_side():
    # longs: a stop only hurts (winners dip first); shorts: a stop only helps
    longs = [_row(i, "d", f"L{i}", "long", 3000.0, entry="09:18") for i in range(6)]
    shorts = [_row(50 + i, "d", f"S{i}", "short", -12000.0, entry="09:18") for i in range(6)]
    paths = {}
    for r in longs:
        paths[r["symbol"]] = _path({"09:20": -2600, "09:29": 3500})
    for r in shorts:
        paths[r["symbol"]] = _path({"09:20": -2600, "09:29": -12500})
    rows = longs + shorts
    st = so.describe(rows)
    saved = {
        "trade_side": "long_only",
        "stop_loss_enabled": True,
        "stop_loss_inr": 2500,
        "profit_lock_enabled": False,
    }
    recs = so.recommendations(rows, {"d": paths}, saved, st, so.side_split(rows))
    stop = next(r for r in recs if r["rule"] == "stop")
    assert stop["to"] == 0  # no stop is best for the traded (long) side
    saved_both = dict(saved, trade_side="both")
    stop_both = next(
        r
        for r in so.recommendations(rows, {"d": paths}, saved_both, st, so.side_split(rows))
        if r["rule"] == "stop"
    )
    assert stop_both["to"] > 0  # on both sides the stop pays (short losers run)


def test_trail_rule_uses_one_tick_of_the_largest_lot():
    rows = [
        _row(1, "d", "IDEA", "long", 100.0, qty=71475, tick=0.05),
        _row(2, "d", "HAL", "long", 100.0, qty=150, tick=0.05),
    ]
    st = so.describe(rows)
    recs = so.recommendations(
        rows,
        {},
        {"trail_giveback_inr": 1500, "stop_loss_enabled": False, "profit_lock_enabled": False},
        st,
        so.side_split(rows),
    )
    trail = next(r for r in recs if r["rule"] == "trail")
    assert trail["to"] == 4000.0 and trail["status"] == "recommend"  # 3573.75 rounded up to 500


def test_size_rule_holds_while_ci_straddles_breakeven():
    rows = [_row(i, "d", f"S{i}", "long", 5000.0 if i % 2 else -4000.0) for i in range(10)]
    st = so.describe(rows)
    recs = so.recommendations(
        rows,
        {},
        {"margin_per_slot": 60000, "stop_loss_enabled": False, "profit_lock_enabled": False},
        st,
        so.side_split(rows),
    )
    assert next(r for r in recs if r["rule"] == "size")["status"] == "hold"


# ---- endpoint -------------------------------------------------------------
@pytest.fixture
def client(monkeypatch):
    import utils.session as sess

    monkeypatch.setattr(sess, "is_session_valid", lambda: True)
    import blueprints.open15_breakout as bp

    app = Flask(__name__)
    app.register_blueprint(bp.open15_bp)
    app.config["TESTING"] = True
    return app.test_client()


def test_endpoint_passes_draft_and_never_500s(client, monkeypatch):
    captured = {}

    def fake(draft=None, scope="real", sample="all"):
        captured.update(draft=draft, scope=scope, sample=sample)
        return {"status": "ok"}

    monkeypatch.setattr(so, "compute_outlook", fake)
    r = client.get(
        "/open15_vol_breakout/api/settings_outlook?stop_loss_inr=3500&profit_target_inr=8000"
        "&trail_giveback_inr=3000&max_trades=2&stop_loss_enabled=1&profit_lock_enabled=0&scope=real_sim&sample=longs"
    )
    assert r.status_code == 200 and r.get_json()["status"] == "ok"
    assert captured["draft"]["stop_loss_inr"] == 3500.0 and captured["draft"]["max_trades"] == 2
    assert (
        captured["draft"]["stop_loss_enabled"] is True
        and captured["draft"]["profit_lock_enabled"] is False
    )
    assert captured["scope"] == "real_sim" and captured["sample"] == "longs"

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(so, "compute_outlook", boom)
    r = client.get("/open15_vol_breakout/api/settings_outlook")
    assert r.status_code == 200 and r.get_json()["status"] == "error"


def test_endpoint_rejects_unknown_scope_and_sample_to_defaults(client, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        so,
        "compute_outlook",
        lambda draft=None, scope="real", sample="all": captured.update(scope=scope, sample=sample)
        or {"status": "ok"},
    )
    client.get("/open15_vol_breakout/api/settings_outlook?scope=paper&sample=everything")
    assert captured == {"scope": "real", "sample": "all"}


def test_compute_outlook_contains_a_raise():
    import services.open15_settings_outlook as mod

    orig = mod._compute
    mod._compute = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        assert mod.compute_outlook()["status"] == "error"
    finally:
        mod._compute = orig
