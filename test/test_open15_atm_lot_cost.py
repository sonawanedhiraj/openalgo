"""Tests for the ATM lot-cost coverage ladder (issue #591).

Regressions in the repo's style: each test pins a decision that could silently
regress — the worst-side rule, the priced-only denominator, the fail-open
``None`` on a missing sweep, and the clamp semantics of the new config knob.
"""

import datetime as dt

import pytest

from services import open15_atm_lot_cost as alc


def _row(cost, strike=None, lot=None, expiry="2026-08-25", as_of="2026-08-08"):
    return {
        "atm_lot_cost_inr": cost,
        "atm_strike": strike if strike is not None else (cost / 100 if cost else None),
        "atm_lot_size": lot if lot is not None else 100,
        "expiry_used": expiry,
        "as_of_date": as_of,
    }


def _scores(pairs):
    """{sym: (ce_cost, pe_cost)} -> the get_latest_scores shape."""
    out = {}
    for sym, (ce, pe) in pairs.items():
        out[(sym, "CE")] = _row(ce)
        out[(sym, "PE")] = _row(pe)
    return out


# ---------------------------------------------------------------------------
# worst_side_costs
# ---------------------------------------------------------------------------


def test_worst_side_is_max_of_ce_pe():
    """At arm time the side is unknown until the 09:16 seed, so affordability
    must hold for EITHER side — bucketing on the cheaper side would count a
    name affordable that the day's actual (worse) side cannot buy."""
    scores = _scores({"AAA": (30_000.0, 33_200.0)})
    costs, unresolved = alc.worst_side_costs(scores, {"AAA"})
    assert costs[0]["w"] == 33_200.0
    assert costs[0]["ce"] == 30_000.0 and costs[0]["pe"] == 33_200.0
    assert unresolved["n"] == 0


def test_one_sided_quote_uses_the_present_side():
    scores = {("AAA", "CE"): _row(25_000.0)}  # PE row absent entirely
    costs, unresolved = alc.worst_side_costs(scores, {"AAA"})
    assert costs[0]["w"] == 25_000.0
    assert unresolved["n"] == 0


def test_unresolved_reasons_stay_distinct():
    """A name the sweep has rows for but no ATM cost is ``no_quote``; a name
    with no rows at all is ``not_scored``. Blending them would hide which
    operator response is called for (#583's reason discipline)."""
    scores = {("QUOTELESS", "CE"): _row(None), ("QUOTELESS", "PE"): _row(None)}
    costs, unresolved = alc.worst_side_costs(scores, {"QUOTELESS", "NOTSCORED"})
    assert costs == []
    assert unresolved["n"] == 2
    assert unresolved["no_quote"] == ["QUOTELESS"]
    assert unresolved["not_scored"] == ["NOTSCORED"]


def test_costs_sorted_ascending():
    scores = _scores({"C": (90.0, 80.0), "A": (10.0, 15.0), "B": (50.0, 40.0)})
    costs, _ = alc.worst_side_costs(scores, {"A", "B", "C"})
    assert [c["s"] for c in costs] == ["A", "B", "C"]
    assert [c["w"] for c in costs] == [15.0, 50.0, 90.0]


# ---------------------------------------------------------------------------
# build_ladder
# ---------------------------------------------------------------------------


def _ten_costs():
    return [
        {
            "s": f"S{i}",
            "k": 100.0 * i,
            "lot": 100,
            "ce": i * 10_000.0,
            "pe": None,
            "w": i * 10_000.0,
        }
        for i in range(1, 11)
    ]


def test_ladder_percentile_math():
    """capital at p% = cost of the ceil(p% x N)-th cheapest name — the MINIMUM
    capital/slot at which >= p% of priced names are affordable."""
    out = alc.build_ladder(_ten_costs(), capital_per_slot=35_000, target_pct=90)
    by_pct = {r["pct"]: r for r in out["ladder"] if r.get("marker") != "current_slot"}
    assert by_pct[50] == {"pct": 50, "names": 5, "capital": 50_000.0}
    assert by_pct[90]["capital"] == 90_000.0 and by_pct[90]["marker"] == "target"
    assert by_pct[100]["capital"] == 100_000.0 and by_pct[100]["costliest"] == "S10"


def test_current_slot_row_counts_affordable_names():
    """The current-slot row must use the strategy's own affordability rule
    (cost <= capital), not a percentile — it answers "what can I buy TODAY"."""
    out = alc.build_ladder(_ten_costs(), capital_per_slot=35_000, target_pct=90)
    cur = next(r for r in out["ladder"] if r.get("marker") == "current_slot")
    assert cur["names"] == 3  # 10k, 20k, 30k
    assert cur["pct"] == 30.0
    assert cur["capital"] == 35_000.0


def test_drop_top_hints():
    """cover-all is usually dominated by a few very expensive names; the hint
    is what covering all-but-K costs. K=10 is absent when N <= 10."""
    out = alc.build_ladder(_ten_costs(), capital_per_slot=35_000, target_pct=90)
    assert out["drop_top"] == {"5": 50_000.0}


def test_ladder_sorted_by_pct():
    out = alc.build_ladder(_ten_costs(), capital_per_slot=35_000, target_pct=75)
    pcts = [r["pct"] for r in out["ladder"]]
    assert pcts == sorted(pcts)


# ---------------------------------------------------------------------------
# compute_event
# ---------------------------------------------------------------------------


def test_compute_event_none_when_no_sweep(monkeypatch):
    """No sweep (or too stale) -> None = "skip the event", NEVER an event that
    reads as "the whole universe is unaffordable" — the #390 fail-open rule."""
    import database.option_liquidity_db as db

    monkeypatch.setattr(db, "get_latest_scores", lambda **kw: {})
    assert alc.compute_event({"AAA"}, 25_000, 90) is None


def test_compute_event_payload(monkeypatch):
    import database.option_liquidity_db as db

    scores = _scores({"AAA": (20_000.0, 22_000.0), "BBB": (60_000.0, 55_000.0)})
    scores[("CCC", "CE")] = _row(None)
    scores[("CCC", "PE")] = _row(None)
    monkeypatch.setattr(db, "get_latest_scores", lambda **kw: scores)
    ev = alc.compute_event({"AAA", "BBB", "CCC", "DDD"}, 25_000, 90, today=dt.date(2026, 8, 11))
    assert ev["priced"] == 2 and ev["universe_n"] == 4
    assert ev["unresolved"]["n"] == 2
    assert ev["as_of"] == "2026-08-08"
    assert ev["expiry"] == "2026-08-25" and ev["dte"] == 14
    assert ev["capital_per_slot"] == 25_000.0 and ev["target_pct"] == 90
    # the sorted distribution rides along for the page's drill-down
    assert [c["s"] for c in ev["costs"]] == ["AAA", "BBB"]
    cur = next(r for r in ev["ladder"] if r.get("marker") == "current_slot")
    assert cur["names"] == 1  # only AAA (worst 22k) fits 25k


def test_compute_event_flags_blocked_expiry_today(monkeypatch):
    """A pre-roll sweep consumed on a broker-blocked day (issue #669): the
    ladder's lot costs price a contract the strategy cannot buy today (entries
    roll to next month, whose lots cost more) — the event must carry the
    ``expiry_blocked_today`` flag so the card says coverage is overstated."""
    import database.option_liquidity_db as db

    scores = _scores({"AAA": (20_000.0, 22_000.0)})
    monkeypatch.setattr(db, "get_latest_scores", lambda **kw: scores)
    # Monday 2026-08-24, the day before the 25-AUG expiry — the golden incident
    ev = alc.compute_event({"AAA"}, 25_000, 90, today=dt.date(2026, 8, 24))
    assert ev["expiry_blocked_today"] is True
    # expiry day itself is also blocked
    ev = alc.compute_event({"AAA"}, 25_000, 90, today=dt.date(2026, 8, 25))
    assert ev["expiry_blocked_today"] is True
    # mid-cycle: no flag at all (absence, not False — the payload stays lean)
    ev = alc.compute_event({"AAA"}, 25_000, 90, today=dt.date(2026, 8, 11))
    assert "expiry_blocked_today" not in ev


def test_compute_event_denominator_is_priced_names_only(monkeypatch):
    """An unresolved name must never be counted as "covered" — the 100% row
    covers all PRICED names and says so via priced/universe_n."""
    import database.option_liquidity_db as db

    scores = _scores({"AAA": (10_000.0, 11_000.0)})
    monkeypatch.setattr(db, "get_latest_scores", lambda **kw: scores)
    ev = alc.compute_event({"AAA", "GHOST"}, 25_000, 90)
    top = next(r for r in ev["ladder"] if r["pct"] == 100)
    assert top["names"] == 1 == ev["priced"]
    assert ev["universe_n"] == 2


# ---------------------------------------------------------------------------
# config plumbing
# ---------------------------------------------------------------------------


def test_clamp_coverage_target():
    from services.open15_breakout_service import clamp_coverage_target

    assert clamp_coverage_target(75) == 75
    assert clamp_coverage_target("85") == 85
    assert clamp_coverage_target(49) == 50
    assert clamp_coverage_target(101) == 100
    assert clamp_coverage_target("junk") == 90
    assert clamp_coverage_target(None) == 90


def test_resolve_day_config_coverage_target_default_and_clamp(monkeypatch):
    from services.open15_breakout_service import resolve_day_config

    monkeypatch.delenv("OPEN15_COVERAGE_TARGET_PCT", raising=False)
    assert resolve_day_config(None, 0.0)["coverage_target_pct"] == 90
    # a stored value wins over the env default and is clamped
    assert resolve_day_config({"coverage_target_pct": 200}, 0.0)["coverage_target_pct"] == 100
    assert resolve_day_config({"coverage_target_pct": 75}, 0.0)["coverage_target_pct"] == 75
    monkeypatch.setenv("OPEN15_COVERAGE_TARGET_PCT", "80")
    assert resolve_day_config(None, 0.0)["coverage_target_pct"] == 80


def test_config_roundtrip_coverage_target():
    from database.open15_breakout_db import get_config, init_db, save_config

    init_db()
    assert save_config(30_000, "fixed", 1.5, coverage_target_pct=85)
    assert get_config()["coverage_target_pct"] == 85
    # NULL stays NULL so the env default keeps supplying it
    assert save_config(30_000, "fixed", 1.5, coverage_target_pct=None)
    assert get_config()["coverage_target_pct"] is None


# ---------------------------------------------------------------------------
# sweep + page wiring pins
# ---------------------------------------------------------------------------


def test_score_band_records_atm_contract():
    """The ATM contract is contracts[0] (resolve_band sorts nearest-first); its
    LTP x lot size is the lot cost. A missing/zero ATM quote stays None — the
    lot is not free."""
    from services import option_liquidity_service as ols

    contracts = [
        {
            "symbol": "A",
            "strike": 100.0,
            "expiry": dt.date(2026, 8, 25),
            "lotsize": 400,
            "ticksize": 0.05,
        },
        {
            "symbol": "B",
            "strike": 105.0,
            "expiry": dt.date(2026, 8, 25),
            "lotsize": 400,
            "ticksize": 0.05,
        },
    ]
    quotes = {
        ("A", "NFO"): {"ltp": 83.0, "volume": 1000, "oi": 5000},
        ("B", "NFO"): {"ltp": 60.0, "volume": 500, "oi": 900},
    }
    out = ols.score_band(contracts, quotes)
    assert out["atm_strike"] == 100.0
    assert out["atm_ltp"] == 83.0
    assert out["atm_lot_size"] == 400
    assert out["atm_lot_cost_inr"] == pytest.approx(83.0 * 400)
    # ATM quote missing -> None, even though the band still measured B
    out2 = ols.score_band(contracts, {("B", "NFO"): {"ltp": 60.0, "volume": 500, "oi": 900}})
    assert out2["atm_lot_cost_inr"] is None and out2["atm_strike"] is None
    assert out2["band_strikes"] == 1


def test_logs_page_renders_the_ladder_card():
    """Page-structure pin: the card, its render fn, and the config knob exist.
    The card must read the `atm_lot_cost` event and a day without one renders
    no card (history predates #591)."""
    from blueprints.open15_breakout import _LOGS_PAGE

    assert 'id="atmcard"' in _LOGS_PAGE
    assert "renderAtmLadder" in _LOGS_PAGE
    assert "atm_lot_cost" in _LOGS_PAGE
    assert 'id="c_covtgt"' in _LOGS_PAGE
    # a day without the event hides the card entirely
    fn = _LOGS_PAGE.split("function renderAtmLadder()")[1].split("function renderAtmDetail")[0]
    assert "display='none'" in fn.replace('"', "'")
