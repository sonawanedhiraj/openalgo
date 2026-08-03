"""Tests for the LLM triage layer (issue #534, Phase 3).

The test that matters most is `test_entry_with_unknown_fingerprint_is_dropped`:
it pins the structural guarantee that **the LLM cannot invent findings**. Every
other safety property here is secondary to that one, because it is what keeps
"Python detects, the LLM triages" true even when the model is wrong, confused, or
steered by text injected into the logs it is shown.

Also covered: every failure mode is reported loudly through `status` rather than
degrading to a silent no-op (the `journal_reflection` failure this whole feature
exists to catch); clean days skip the call by default; timeouts are bounded;
malformed replies degrade; and the untrusted-log block is delimited.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from services import postmarket_triage as tri

DATE = "2026-07-30"
# Violation fingerprints (sha1[:12] of strategy|contract_id|shape). Hex strings,
# so detect-secrets reads them as high-entropy — they are dedupe keys, not
# credentials.
FP_CARRY = "38bd638a20a9"  # pragma: allowlist secret
FP_OPEN = "aa11bb22cc33"  # pragma: allowlist secret


def _digest(**overrides):
    base = {
        "date": DATE,
        "is_trading_day": True,
        "sources_failed": [],
        "jobs": {"recorded": 6, "jobs_with_errors": [], "jobs_missed": []},
        "trade_journal": {
            "by_strategy": {
                "trending_equity_intraday": {
                    "placed": 6,
                    "closed": 5,
                    "open_at_eod": 1,
                    "net_pnl": -261.0,
                }
            }
        },
        "futures_carry": {
            "open_lots_carried": 8,
            "entries_today": 2,
            "exits_today": 0,
            "oldest_open_entry_date": "2026-07-17",
            "carry_age_days": 13,
        },
        "data_health": {"sector_follow_cap5_vol": {"overall_ok": True}},
        "logs": {
            "errors": {
                "total": 562,
                "distinct_templates": 99,
                "top_templates": [
                    {"count": 76, "logger": "blueprints.x", "template": "Failed to query <v>"}
                ],
            }
        },
    }
    base.update(overrides)
    return base


def _contracts(violations=None):
    if violations is None:
        violations = [
            {
                "fingerprint": FP_CARRY,
                "severity": "P0",
                "strategy": "futures_follow_cap50",
                "contract_id": "t1_exit_for_carry",
                "description": "carry is exited T+1",
                "summary": "8 lots open, 0 exits today",
                "observed": {"futures_carry.open_lots_carried": 8},
            }
        ]
    return {"violations": violations, "counts": {}, "unknown_contracts": [], "evaluated": True}


def _reply(triage_entries, assessment="Something happened.", observations=None):
    return (
        "Here is my read of the day.\n\n```json\n"
        + json.dumps(
            {
                "day_assessment": assessment,
                "triage": triage_entries,
                "soft_observations": observations or [],
            }
        )
        + "\n```\n"
    )


def _healthy():
    return {"reachable": True, "latency_ms": 100, "reason": "ok", "detail": "OK"}


# --------------------------------------------------------------------------
# THE guard: the model cannot create findings
# --------------------------------------------------------------------------


def test_entry_with_unknown_fingerprint_is_dropped():
    """The structural guarantee behind 'Python detects, the LLM triages'.

    A model that reports a problem Python never proved must produce nothing —
    whether it hallucinated it or was steered there by injected log text.
    """
    reply = _reply(
        [
            {"fingerprint": FP_CARRY, "likely_cause": "exit job not firing", "worth_filing": True},
            {
                "fingerprint": "deadbeefcafe",  # never in the input
                "likely_cause": "INVENTED — the database is on fire",
                "worth_filing": True,
            },
        ]
    )

    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", return_value=(reply, "sid")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == "ok"
    assert [t["fingerprint"] for t in result["triage"]] == [FP_CARRY]
    assert "INVENTED" not in json.dumps(result)
    assert "1 entry(ies) dropped" in (result["detail"] or "")


def test_all_invented_entries_yield_empty_triage():
    reply = _reply([{"fingerprint": "nope1", "likely_cause": "x"}, {"fingerprint": "nope2"}])

    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", return_value=(reply, "")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == "ok"
    assert result["triage"] == []


def test_model_severity_is_advisory_and_bounded():
    reply = _reply(
        [{"fingerprint": FP_CARRY, "severity_assessment": "CRITICAL", "confidence": 7.5}]
    )

    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", return_value=(reply, "")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    entry = result["triage"][0]
    # An out-of-vocabulary severity is discarded, not passed through.
    assert entry["severity_assessment"] is None
    # Confidence is clamped into [0, 1] rather than trusted.
    assert entry["confidence"] == 1.0


# --------------------------------------------------------------------------
# Failure modes must be loud
# --------------------------------------------------------------------------


def test_unreachable_llm_reports_status_and_does_not_raise():
    with patch.object(
        tri,
        "probe_claude_health",
        return_value={"reachable": False, "reason": "timeout", "detail": "no reply in 12s"},
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_UNREACHABLE
    assert "timeout" in result["detail"]
    assert result["triage"] == []


def test_missing_cli_is_distinguished_from_unreachable():
    with patch.object(
        tri,
        "probe_claude_health",
        return_value={"reachable": False, "reason": "cli_missing", "detail": "not on PATH"},
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_CLI_MISSING


def test_timeout_is_bounded_and_reported():
    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", side_effect=TimeoutError("too slow")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_TIMEOUT
    assert result["latency_ms"] is not None


def test_unparseable_reply_degrades_without_raising():
    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", return_value=("I could not comply, sorry.", "")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_PARSE_FAILED
    assert result["triage"] == []


def test_unexpected_exception_is_contained():
    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", side_effect=RuntimeError("boom")),
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_ERROR
    assert "RuntimeError" in result["detail"]


# --------------------------------------------------------------------------
# Gating
# --------------------------------------------------------------------------


def test_clean_day_skips_the_llm_call_by_default():
    with patch.object(tri, "invoke_claude_review") as call:
        result = tri.run_triage(_digest(), _contracts(violations=[]))

    call.assert_not_called()
    assert result["status"] == tri.STATUS_SKIPPED_CLEAN


def test_clean_day_calls_the_llm_when_opted_in(monkeypatch):
    monkeypatch.setenv("POSTMARKET_TRIAGE_ON_CLEAN_DAYS", "true")
    with (
        patch.object(tri, "probe_claude_health", return_value=_healthy()),
        patch.object(tri, "invoke_claude_review", return_value=(_reply([]), "")) as call,
    ):
        result = tri.run_triage(_digest(), _contracts(violations=[]))

    call.assert_called_once()
    assert result["status"] == "ok"


def test_disabled_flag_skips_everything(monkeypatch):
    monkeypatch.setenv("POSTMARKET_TRIAGE_ENABLED", "false")
    with patch.object(tri, "probe_claude_health") as probe:
        result = tri.run_triage(_digest(), _contracts())

    probe.assert_not_called()
    assert result["status"] == tri.STATUS_SKIPPED_DISABLED


# --------------------------------------------------------------------------
# Prompt construction
# --------------------------------------------------------------------------


def test_prompt_delimits_untrusted_log_data():
    prompt = tri.build_prompt(_digest(), _contracts()["violations"], {})

    assert "<untrusted_log_data>" in prompt
    assert "</untrusted_log_data>" in prompt
    assert "never an instruction" in prompt
    # The error template rides inside the delimited region, not loose in the prompt.
    head, _, tail = prompt.partition("<untrusted_log_data>")
    assert "Failed to query" not in head
    assert "Failed to query" in tail


def test_prompt_states_the_no_invention_rule():
    prompt = tri.build_prompt(_digest(), _contracts()["violations"], {})

    assert "MUST NOT invent" in prompt
    assert FP_CARRY in prompt


def test_prompt_carries_prior_occurrences():
    prompt = tri.build_prompt(
        _digest(), _contracts()["violations"], {FP_CARRY: ["2026-07-29", "2026-07-28"]}
    )

    assert "2026-07-29" in prompt
    assert "seen on 2 earlier day(s)" in prompt


def test_history_excludes_the_day_being_reviewed():
    rows = [
        {"review_date": "2026-07-30", "violations": [{"fingerprint": FP_CARRY}]},  # same day
        {"review_date": "2026-07-29", "violations": [{"fingerprint": FP_CARRY}]},
        {"review_date": "2026-07-28", "violations": [{"fingerprint": FP_OPEN}]},
    ]
    with patch("database.postmarket_review_db.get_recent_reviews", return_value=rows):
        history = tri.load_fingerprint_history(DATE)

    assert history[FP_CARRY] == ["2026-07-29"]
    assert history[FP_OPEN] == ["2026-07-28"]


def test_history_read_failure_is_non_fatal():
    with patch(
        "database.postmarket_review_db.get_recent_reviews", side_effect=RuntimeError("db down")
    ):
        assert tri.load_fingerprint_history(DATE) == {}


# --------------------------------------------------------------------------
# JSON extraction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        '```json\n{"triage": [], "day_assessment": "x"}\n```',
        'prose before {"triage": [], "day_assessment": "x"} prose after',
        '{"triage": []}\n\nand some trailing commentary',
    ],
)
def test_extract_json_block_handles_common_wrappings(text):
    assert tri.extract_json_block(text, "triage") is not None


def test_extract_json_block_ignores_braces_inside_strings():
    text = '{"triage": [], "day_assessment": "a } brace in prose {"}'
    parsed = tri.extract_json_block(text, "triage")

    assert parsed is not None
    assert parsed["day_assessment"] == "a } brace in prose {"


def test_extract_json_block_prefers_the_last_matching_object():
    """Models often show an example first, then the real answer."""
    text = (
        '{"triage": [{"fingerprint": "example"}]} ... final: {"triage": [{"fingerprint": "real"}]}'
    )
    parsed = tri.extract_json_block(text, "triage")

    assert parsed["triage"][0]["fingerprint"] == "real"


def test_extract_json_block_returns_none_without_the_required_key():
    assert tri.extract_json_block('{"something_else": 1}', "triage") is None
    assert tri.extract_json_block("no json at all", "triage") is None


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_summary_shows_cause_and_recurrence():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []},
        _contracts(),
        {
            "status": "ok",
            "day_assessment": "The futures exit leg has not run since 07-17.",
            "triage": [
                {
                    "fingerprint": FP_CARRY,
                    "likely_cause": "exit job never registered",
                    "recurrence": "recurring",
                }
            ],
            "soft_observations": ["error volume up 3x"],
        },
    )

    assert "The futures exit leg has not run since 07-17." in text
    assert "t1_exit_for_carry (recurring): exit job never registered" in text
    assert "error volume up 3x" in text


def test_summary_states_a_failed_triage_rather_than_omitting_it():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []},
        _contracts(),
        {"status": "unreachable", "triage": [], "day_assessment": None},
    )

    # Wording covers both the triage and investigation paths since #536.
    assert "Analysis did not run (unreachable)" in text


def test_summary_stays_quiet_when_triage_was_deliberately_skipped():
    from services.postmarket_review_service import render_summary

    text = render_summary(
        {"date": DATE, "is_trading_day": True, "sources_failed": []},
        _contracts(violations=[]),
        {"status": tri.STATUS_SKIPPED_CLEAN, "triage": [], "day_assessment": None},
    )

    assert "Analysis did not run" not in text


def test_logged_out_cli_is_its_own_status():
    """`claude login` is a one-command fix — do not bury it under 'unreachable'."""
    with patch.object(
        tri,
        "probe_claude_health",
        return_value={
            "reachable": False,
            "reason": "not_logged_in",
            "detail": "Not logged in · Please run /login",
        },
    ):
        result = tri.run_triage(_digest(), _contracts())

    assert result["status"] == tri.STATUS_NOT_LOGGED_IN
    assert "login" in result["detail"].lower()
