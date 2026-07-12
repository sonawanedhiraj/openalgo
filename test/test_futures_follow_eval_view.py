"""Unit tests for services/futures_follow_eval_view.summarize_payload (issue #395).

Pure function — no DB, no service, no clock. The fixtures below are trimmed from
real ``futures_follow_eval_snapshots`` rows (2026-07-07, a 1-signal day, and
2026-07-08, a 0-signal day).
"""

import pytest

from services.futures_follow_eval_view import summarize_payload


def _snapshot(payload: dict, eval_date: str = "2026-07-07") -> dict:
    return {
        "eval_date": eval_date,
        "eval_at": f"{eval_date}T15:20:03+05:30",
        "payload": payload,
    }


SIGNAL_DAY = {
    "eval_at": "2026-07-07T15:20:03+05:30",
    "mode": "sandbox",
    "n_signals": 1,
    "intraday_source_counts": {"quotes": 30, "aggregator": 0, "historify": 0, "none": 0},
    "cap_skipped": 0,
    "vetoed": 0,
    "per_gate_fail_counts": {"sector": 28, "stock": 24, "vol": 19, "missing_data": 0},
    "symbols": [
        {"symbol": "INFY", "outcome": "in_cap_placed"},
        {"symbol": "TCS", "outcome": "first_failed_gate"},
        *[{"symbol": f"SYM{i}", "outcome": "first_failed_gate"} for i in range(28)],
    ],
}


def test_signal_day_counts():
    s = summarize_payload(_snapshot(SIGNAL_DAY))
    assert s["eval_date"] == "2026-07-07"
    assert s["mode"] == "sandbox"
    assert s["n_signals"] == 1
    assert s["placed"] == 1
    assert s["total_symbols"] == 30
    assert s["evaluated_symbols"] == 30
    assert s["passed_symbols"] == [{"symbol": "INFY", "outcome": "in_cap_placed"}]


def test_gates_passed_inverts_stored_fail_counts():
    """The card shows passed-of-N; the stored payload counts failures."""
    s = summarize_payload(_snapshot(SIGNAL_DAY))
    assert s["gates_passed"] == {"sector": 2, "stock": 6, "vol": 11}
    # Raw counts survive for the tooltip / log-line reconciliation.
    assert s["gates_failed"] == {"sector": 28, "stock": 24, "vol": 19}


def test_missing_data_symbols_excluded_from_gate_denominator():
    """A symbol with no data never reaches a gate, so it must not count as a
    gate pass — otherwise a dead feed inflates every 'passed' number."""
    payload = {
        **SIGNAL_DAY,
        "per_gate_fail_counts": {"sector": 10, "stock": 10, "vol": 10, "missing_data": 20},
    }
    s = summarize_payload(_snapshot(payload))
    assert s["missing_data"] == 20
    assert s["evaluated_symbols"] == 10
    assert s["gates_passed"] == {"sector": 0, "stock": 0, "vol": 0}


def test_gates_passed_never_negative_on_inconsistent_payload():
    payload = {
        **SIGNAL_DAY,
        "per_gate_fail_counts": {"sector": 99, "stock": 0, "vol": 0, "missing_data": 0},
    }
    assert summarize_payload(_snapshot(payload))["gates_passed"]["sector"] == 0


@pytest.mark.parametrize(
    "sources,expected",
    [
        ({"quotes": 30, "aggregator": 0, "historify": 0, "none": 0}, "quotes"),
        ({"quotes": 0, "aggregator": 4, "historify": 26, "none": 0}, "historify"),
        ({"quotes": 0, "aggregator": 0, "historify": 0, "none": 30}, "none"),
        ({"quotes": 0, "aggregator": 0, "historify": 0, "none": 0}, "none"),
        ({}, "none"),
    ],
)
def test_dominant_source(sources, expected):
    s = summarize_payload(_snapshot({**SIGNAL_DAY, "intraday_source_counts": sources}))
    assert s["dominant_source"] == expected


def test_live_source_count_is_quotes_plus_aggregator():
    payload = {
        **SIGNAL_DAY,
        "intraday_source_counts": {"quotes": 18, "aggregator": 6, "historify": 6, "none": 0},
    }
    assert summarize_payload(_snapshot(payload))["live_source_count"] == 24


def test_passed_symbols_includes_every_post_gate_outcome():
    """cap_skipped / vetoed / placement_failed / not_selected all cleared the
    gates — only a gate failure or missing data keeps a symbol out."""
    payload = {
        **SIGNAL_DAY,
        "symbols": [
            {"symbol": "A", "outcome": "in_cap_placed"},
            {"symbol": "B", "outcome": "cap_skipped"},
            {"symbol": "C", "outcome": "vetoed"},
            {"symbol": "D", "outcome": "placement_failed"},
            {"symbol": "E", "outcome": "not_selected"},
            {"symbol": "F", "outcome": "first_failed_gate"},
            {"symbol": "G", "outcome": "missing_data"},
        ],
    }
    s = summarize_payload(_snapshot(payload))
    assert [r["symbol"] for r in s["passed_symbols"]] == ["A", "B", "C", "D", "E"]
    assert s["placed"] == 1
    assert s["placement_failed"] == 1


def test_empty_payload_yields_zeros_not_an_exception():
    """A malformed or schema-drifted row must degrade to zeros — one bad day
    cannot blank the whole history table."""
    s = summarize_payload({"eval_date": "2026-07-07", "eval_at": None, "payload": {}})
    assert s["n_signals"] == 0
    assert s["total_symbols"] == 0
    assert s["gates_passed"] == {"sector": 0, "stock": 0, "vol": 0}
    assert s["dominant_source"] == "none"
    assert s["passed_symbols"] == []


def test_missing_payload_key_yields_zeros():
    assert summarize_payload({"eval_date": "2026-07-07"})["total_symbols"] == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
