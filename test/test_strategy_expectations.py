"""Tests for the deterministic expectation contracts (issue #532, Phase 2).

These pin the behaviours that decide whether the post-market report is worth
reading. The two that matter most:

* **The #497 contract fires on day one.** `futures_follow_cap50` T+1 exits were
  silently dead for four trading days while open NRML lots reached 110% of book.
  `t1_exit_for_carry` must fail on the FIRST such day, and produce the SAME
  fingerprint on all four so Phase 4 files one issue rather than four.
* **Missing input is `unknown`, never `fail`.** A degraded digest section means
  our collection broke, not that a strategy misbehaved. Reporting it as a
  violation would train the operator to ignore the report — the exact outcome
  this feature exists to prevent.

Coverage: healthy day is silent; stale carry fails; same-day entry does not fail;
fingerprints are stable across days and independent of observed values; degraded
sections and pre-audit dates resolve to unknown; a raising predicate is contained;
non-trading days evaluate nothing; per-contract and master flags work; severity
ordering.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.strategy_expectations import (
    EXPECTATIONS,
    Expect,
    dig,
    evaluate_expectations,
)

DATE = "2026-07-30"


def _digest(**overrides):
    """A minimal healthy trading-day digest; override sections per test."""
    base = {
        "date": DATE,
        "is_trading_day": True,
        "sources_failed": [],
        "jobs": {
            "recorded": 6,
            "audit_earliest_date": "2026-07-01",
            "jobs": {
                "futures_follow_entry": {"ran": 1, "ok": 1, "error": 0, "missed": 0},
                "futures_follow_exit": {"ran": 1, "ok": 1, "error": 0, "missed": 0},
                "sector_follow_entry": {"ran": 1, "ok": 1, "error": 0, "missed": 0},
                "sector_follow_exit": {"ran": 1, "ok": 1, "error": 0, "missed": 0},
            },
            "jobs_with_errors": [],
            "jobs_missed": [],
        },
        "futures_carry": {
            "entries_today": 1,
            "exits_today": 1,
            "open_lots_carried": 1,
            "oldest_open_entry_date": DATE,
            "carry_age_days": 0,
        },
        "trade_journal": {
            "total_rows": 3,
            "by_strategy": {
                "trending_equity_intraday": {
                    "placed": 3,
                    "closed": 3,
                    "open_at_eod": 0,
                    "unpriced_exits": 0,
                    "net_pnl": 120.0,
                }
            },
        },
        "data_health": {
            "sector_follow_cap5_vol": {"overall_ok": True, "n_stale": 0},
            "scanner_universe_1m": {"overall_ok": True, "n_stale": 0},
        },
    }
    base.update(overrides)
    return base


def _ids(result):
    return {f"{v['strategy']}:{v['contract_id']}" for v in result["violations"]}


def _by_id(result, contract_id):
    return next(v for v in result["violations"] if v["contract_id"] == contract_id)


# --------------------------------------------------------------------------
# The headline behaviour
# --------------------------------------------------------------------------


def test_healthy_day_produces_no_violations():
    result = evaluate_expectations(_digest())

    assert result["violations"] == []
    assert result["counts"]["fail"] == 0
    assert result["evaluated"] is True


def test_carry_from_a_previous_session_without_an_exit_fails_p0():
    """The #497 shape, caught on day one instead of day four."""
    result = evaluate_expectations(
        _digest(
            futures_carry={
                "entries_today": 1,
                "exits_today": 0,
                "open_lots_carried": 2,
                "oldest_open_entry_date": "2026-07-17",
                "carry_age_days": 13,
            }
        )
    )

    assert "futures_follow_cap50:t1_exit_for_carry" in _ids(result)
    violation = _by_id(result, "t1_exit_for_carry")
    assert violation["severity"] == "P0"
    assert violation["observed"]["futures_carry.open_lots_carried"] == 2
    assert "0 exits today" in violation["summary"]


def test_position_entered_today_is_not_a_violation():
    """A T+1 strategy owes no exit on the day it entered."""
    result = evaluate_expectations(
        _digest(
            futures_carry={
                "entries_today": 2,
                "exits_today": 0,
                "open_lots_carried": 2,
                "oldest_open_entry_date": DATE,
                "carry_age_days": 0,
            }
        )
    )

    assert "futures_follow_cap50:t1_exit_for_carry" not in _ids(result)


def test_fingerprint_is_stable_across_days_and_magnitudes():
    """Four broken days must dedupe to ONE issue in Phase 4, not four."""
    fingerprints = set()
    for day, lots, age in (
        ("2026-07-27", 2, 10),
        ("2026-07-28", 4, 11),
        ("2026-07-29", 6, 12),
        ("2026-07-30", 8, 13),
    ):
        result = evaluate_expectations(
            _digest(
                date=day,
                futures_carry={
                    "entries_today": 2,
                    "exits_today": 0,
                    "open_lots_carried": lots,
                    "oldest_open_entry_date": "2026-07-17",
                    "carry_age_days": age,
                },
            )
        )
        fingerprints.add(_by_id(result, "t1_exit_for_carry")["fingerprint"])

    assert len(fingerprints) == 1, "same problem across days must share one fingerprint"


# --------------------------------------------------------------------------
# Missing input must never become a failure
# --------------------------------------------------------------------------


def test_degraded_section_is_unknown_not_failure():
    result = evaluate_expectations(_digest(futures_carry=None))

    assert "futures_follow_cap50:t1_exit_for_carry" not in _ids(result)
    assert any("t1_exit_for_carry" in u for u in result["unknown_contracts"])
    assert result["counts"]["unknown"] >= 1


def test_every_section_degraded_yields_zero_violations():
    """A totally broken collector must accuse nobody."""
    result = evaluate_expectations(
        {
            "date": DATE,
            "is_trading_day": True,
            "jobs": None,
            "futures_carry": None,
            "trade_journal": None,
            "data_health": None,
        }
    )

    assert result["violations"] == []
    assert result["counts"]["fail"] == 0
    assert result["counts"]["unknown"] == len(EXPECTATIONS)


def test_date_before_the_audit_existed_is_unknown_not_failure():
    """Replaying history must not report a phantom 'no jobs fired'."""
    result = evaluate_expectations(
        _digest(
            date="2026-06-01",
            jobs={
                "recorded": 0,
                "audit_earliest_date": "2026-08-03",
                "jobs": {},
                "jobs_with_errors": [],
                "jobs_missed": [],
            },
        )
    )

    assert "platform:scheduler_audit_recording" not in _ids(result)
    assert "futures_follow_cap50:exit_job_fired" not in _ids(result)


def test_audit_live_but_recorded_nothing_is_a_real_failure():
    """The contrast case — the audit was running and saw nothing fire."""
    result = evaluate_expectations(
        _digest(
            jobs={
                "recorded": 0,
                "audit_earliest_date": "2026-07-01",
                "jobs": {},
                "jobs_with_errors": [],
                "jobs_missed": [],
            }
        )
    )

    assert "platform:scheduler_audit_recording" in _ids(result)


# --------------------------------------------------------------------------
# Other contracts
# --------------------------------------------------------------------------


def test_missing_exit_job_fires_p0():
    jobs = _digest()["jobs"]
    jobs["jobs"].pop("futures_follow_exit")
    result = evaluate_expectations(_digest(jobs=jobs))

    assert "futures_follow_cap50:exit_job_fired" in _ids(result)
    assert _by_id(result, "exit_job_fired")["severity"] == "P0"


def test_errored_and_missed_jobs_are_reported():
    jobs = _digest()["jobs"]
    jobs["jobs_with_errors"] = ["sector_follow_entry"]
    jobs["jobs_missed"] = ["scanner_comparison_eod"]
    result = evaluate_expectations(_digest(jobs=jobs))

    assert "platform:no_errored_jobs" in _ids(result)
    assert "platform:no_missed_jobs" in _ids(result)
    assert "sector_follow_entry" in _by_id(result, "no_errored_jobs")["summary"]


def test_open_position_and_unpriced_exit_are_reported():
    result = evaluate_expectations(
        _digest(
            trade_journal={
                "total_rows": 3,
                "by_strategy": {
                    "trending_equity_intraday": {
                        "placed": 3,
                        "closed": 2,
                        "open_at_eod": 1,
                        "unpriced_exits": 1,
                        "net_pnl": 0.0,
                    }
                },
            }
        )
    )

    assert "simplified_engine:flat_at_eod" in _ids(result)
    assert "simplified_engine:no_unpriced_exits" in _ids(result)


def test_stale_feed_is_reported():
    result = evaluate_expectations(
        _digest(data_health={"sector_follow_cap5_vol": {"overall_ok": False, "n_stale": 4}})
    )

    assert "platform:feed_fresh" in _ids(result)


# --------------------------------------------------------------------------
# Machinery
# --------------------------------------------------------------------------


def test_non_trading_day_evaluates_nothing():
    result = evaluate_expectations(_digest(is_trading_day=False))

    assert result["evaluated"] is False
    assert result["violations"] == []
    assert result["counts"]["skipped"] == len(EXPECTATIONS)


def test_master_flag_disables_all_contracts(monkeypatch):
    monkeypatch.setenv("POSTMARKET_CONTRACTS_ENABLED", "false")
    result = evaluate_expectations(
        _digest(
            futures_carry={
                "exits_today": 0,
                "oldest_open_entry_date": "2026-07-17",
                "open_lots_carried": 8,
                "carry_age_days": 13,
                "entries_today": 0,
            }
        )
    )

    assert result["evaluated"] is False
    assert result["violations"] == []


@pytest.mark.parametrize(
    "disabled", ["t1_exit_for_carry", "futures_follow_cap50:t1_exit_for_carry"]
)
def test_per_contract_flag_silences_exactly_one(monkeypatch, disabled):
    broken = _digest(
        futures_carry={
            "entries_today": 0,
            "exits_today": 0,
            "open_lots_carried": 8,
            "oldest_open_entry_date": "2026-07-17",
            "carry_age_days": 13,
        },
        data_health={"sector_follow_cap5_vol": {"overall_ok": False, "n_stale": 4}},
    )
    before = _ids(evaluate_expectations(broken))
    monkeypatch.setenv("POSTMARKET_CONTRACTS_DISABLED", disabled)
    after = _ids(evaluate_expectations(broken))

    assert "futures_follow_cap50:t1_exit_for_carry" in before
    assert before - after == {"futures_follow_cap50:t1_exit_for_carry"}
    # The unrelated failure survives — disabling is surgical, not a blanket mute.
    assert "platform:feed_fresh" in after


def test_a_raising_predicate_is_contained_as_unknown():
    """One broken rule must not take down the report it is part of."""
    exploding = Expect(
        contract_id="boom",
        strategy="platform",
        description="always raises",
        predicate=lambda d: 1 / 0,
    )
    with patch("services.strategy_expectations.EXPECTATIONS", (exploding,)):
        result = evaluate_expectations(_digest())

    assert result["violations"] == []
    assert result["counts"]["unknown"] == 1
    assert any("predicate raised" in u for u in result["unknown_contracts"])


def test_violations_sort_p0_first():
    result = evaluate_expectations(
        _digest(
            futures_carry={
                "entries_today": 0,
                "exits_today": 0,
                "open_lots_carried": 8,
                "oldest_open_entry_date": "2026-07-17",
                "carry_age_days": 13,
            },
            data_health={"sector_follow_cap5_vol": {"overall_ok": False, "n_stale": 4}},
        )
    )

    severities = [v["severity"] for v in result["violations"]]
    assert severities == sorted(severities)
    assert severities[0] == "P0"


def test_contract_ids_are_unique_per_strategy():
    """Fingerprints key off (strategy, contract_id) — collisions would merge findings."""
    seen = [(c.strategy, c.contract_id) for c in EXPECTATIONS]
    assert len(seen) == len(set(seen))


def test_every_contract_declares_a_valid_severity():
    assert all(c.severity in ("P0", "P1", "P2") for c in EXPECTATIONS)


@pytest.mark.parametrize(
    "path,expected",
    [
        ("futures_carry.exits_today", 1),
        ("futures_carry.missing_key", None),
        ("nope.at.all", None),
        ("jobs.recorded", 6),
    ],
)
def test_dig_tolerates_missing_nodes(path, expected):
    assert dig(_digest(), path) == expected


def test_dig_treats_none_node_as_missing():
    assert dig(_digest(futures_carry=None), "futures_carry.exits_today") is None
