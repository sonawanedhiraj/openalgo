"""Tests for services/intraday_pullback_eval_view.summarize_payload (issue #422).

Pure function — no DB, no service, no clock.
"""

from services.intraday_pullback_eval_view import summarize_payload


def _pick(symbol, position="none", **diag):
    base = {
        "candles": 0,
        "ref_formed": 0,
        "breakouts": 0,
        "gate_blocked": 0,
        "no_slot": 0,
        "entries": 0,
        "exits": 0,
    }
    base.update(diag)
    return {
        "symbol": symbol,
        "sector": "IDX1",
        "gain_930_pct": 1.5,
        "sector_930_pct": 0.4,
        "diag": base,
        "reason": "no reference formed",
        "position": position,
    }


def _row(**over):
    row = {
        "eval_date": "2026-07-15",
        "eval_at": "2026-07-15T10:05:00",
        "mode": "sandbox",
        "side_today": "L",
        "nifty_930_pct": 0.5,
        "selected": True,
        "picks": ["AAA", "BBB"],
        "n_trades_today": 0,
        "evaluation": [_pick("AAA"), _pick("BBB")],
    }
    row.update(over)
    return row


def test_carries_the_days_headline_fields_through():
    s = summarize_payload(_row())
    assert s["eval_date"] == "2026-07-15"
    assert s["eval_at"] == "2026-07-15T10:05:00"
    assert s["mode"] == "sandbox"
    assert s["side_today"] == "L"
    assert s["nifty_930_pct"] == 0.5
    assert s["selected"] is True


def test_counts_picks_trades_and_open_positions():
    s = summarize_payload(
        _row(
            n_trades_today=2,
            evaluation=[_pick("AAA", "open"), _pick("BBB", "closed"), _pick("CCC", "none")],
        )
    )
    assert s["n_picks"] == 3
    assert s["n_trades"] == 2
    assert s["n_open"] == 1


def test_diag_totals_sum_across_picks():
    s = summarize_payload(
        _row(
            evaluation=[
                _pick("AAA", ref_formed=1, breakouts=2, gate_blocked=1),
                _pick("BBB", ref_formed=1, breakouts=3, no_slot=2, entries=1, exits=1),
            ]
        )
    )
    assert s["diag"] == {
        "ref_formed": 2,
        "breakouts": 5,
        "gate_blocked": 1,
        "no_slot": 2,
        "entries": 1,
        "exits": 1,
    }


def test_candles_is_not_aggregated():
    """A summed per-symbol bar count is meaningless as a day total — it stays out of the digest."""
    s = summarize_payload(_row(evaluation=[_pick("AAA", candles=40), _pick("BBB", candles=40)]))
    assert "candles" not in s["diag"]


def test_picks_list_pairs_symbol_with_position():
    s = summarize_payload(_row(evaluation=[_pick("AAA", "open"), _pick("BBB", "none")]))
    assert s["picks"] == [
        {"symbol": "AAA", "position": "open"},
        {"symbol": "BBB", "position": "none"},
    ]


def test_zero_pick_day_summarises_without_raising():
    s = summarize_payload(_row(picks=[], evaluation=[]))
    assert s["n_picks"] == 0
    assert s["n_trades"] == 0
    assert s["diag"]["breakouts"] == 0


def test_empty_or_partial_payload_yields_zeros_not_an_exception():
    """One malformed day must not blank the whole history table."""
    s = summarize_payload({})
    assert s["eval_date"] is None
    assert s["selected"] is False
    assert s["n_picks"] == 0
    assert s["diag"] == dict.fromkeys(
        ("ref_formed", "breakouts", "gate_blocked", "no_slot", "entries", "exits"), 0
    )


def test_null_and_non_numeric_diag_values_are_tolerated():
    ev = [{"symbol": "AAA", "diag": None, "position": "none"}, {"symbol": "BBB", "diag": {}}]
    ev.append({"symbol": "CCC", "diag": {"breakouts": None, "ref_formed": "x"}})
    s = summarize_payload(_row(evaluation=ev))
    assert s["n_picks"] == 3
    assert s["diag"]["breakouts"] == 0
    assert s["diag"]["ref_formed"] == 0
