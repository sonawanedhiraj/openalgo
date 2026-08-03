"""Tests for the investigating agent, its sandbox, and the filing path (#536).

The agent here has more power than any earlier phase: it reads the repository and
its output is published to GitHub. So most of these tests are about what it
*cannot* do.

The three that carry the most weight:

* `test_deny_list_covers_env_and_db` — `.env` holds `API_KEY_PEPPER` and
  `FERNET_SALT`; every encrypted secret in `openalgo.db` is sealed against them.
  Read access plus a publish path is an exfiltration channel.
* `test_publish_guard_blocks_*` — the far end of that same defence, which does not
  care how a credential-shaped string reached the text.
* `test_finding_citing_a_nonexistent_path_is_dropped` — replaces Phase 3's
  fingerprint allow-list. The agent may now propose findings, so evidence is what
  keeps it honest, and a citation is checked against the filesystem rather than
  believed.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from services import postmarket_investigation as inv

DATE = "2026-07-30"
FP = "38bd638a20a9"  # pragma: allowlist secret


def _digest(**overrides):
    base = {
        "date": DATE,
        "is_trading_day": True,
        "sources_failed": [],
        "jobs": {"recorded": 6, "jobs_with_errors": [], "jobs_missed": []},
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


def _contracts(n=1):
    return {
        "violations": [
            {
                "fingerprint": FP,
                "severity": "P0",
                "strategy": "futures_follow_cap50",
                "contract_id": "t1_exit_for_carry",
                "description": "carry is exited T+1",
                "summary": "8 lots open, 0 exits today",
                "observed": {},
            }
        ][:n],
        "counts": {},
        "unknown_contracts": [],
        "evaluated": True,
    }


def _reply(findings, assessment="Assessed."):
    return (
        "prose\n```json\n"
        + json.dumps({"day_assessment": assessment, "findings": findings})
        + "\n```"
    )


def _finding(**over):
    base = {
        "fingerprint": FP,
        "verdict": "confirmed",
        "severity": "P0",
        "title": "futures_follow exit job never fires",
        "summary": "The T+1 exit leg has not run since 2026-07-17.",
        "root_cause": "register_jobs never adds futures_follow_exit.",
        # A path that genuinely exists in this repo.
        "evidence": [
            {"path": "services/postmarket_investigation.py", "line": 1, "note": "seen here"}
        ],
        "validation": ["Confirm the exit job appears in the scheduler."],
        "worth_filing": True,
    }
    base.update(over)
    return base


# --------------------------------------------------------------------------
# The sandbox
# --------------------------------------------------------------------------


def test_deny_list_covers_env_and_db():
    """`.env` and `db/` must be unreadable — they hold the crypto material."""
    from services.llm_agent_client import DEFAULT_DENY_PATHS, build_settings

    settings = json.loads(build_settings(DEFAULT_DENY_PATHS))
    deny = settings["permissions"]["deny"]

    assert any(".env" in rule for rule in deny)
    assert any("db/" in rule for rule in deny)
    assert any(".git/" in rule for rule in deny)
    assert all(rule.startswith("Read(") for rule in deny)


def test_agent_cannot_write_or_run_a_shell():
    from services.llm_agent_client import (
        DEFAULT_ALLOWED_TOOLS,
        DEFAULT_DISALLOWED_TOOLS,
        build_command,
    )

    cmd = build_command("p")

    assert set(DEFAULT_ALLOWED_TOOLS) == {"Read", "Grep", "Glob"}
    for forbidden in ("Bash", "Write", "Edit", "WebFetch"):
        assert forbidden in DEFAULT_DISALLOWED_TOOLS
        assert forbidden in cmd
    assert "--disallowedTools" in cmd
    # No shell involved: argv is a list, so nothing is word-split or expanded.
    assert isinstance(cmd, list)


def test_command_is_argv_not_a_shell_string():
    """Model/log text can never become part of the command shape."""
    from services.llm_agent_client import build_command

    cmd = build_command("rm -rf / ; echo $(whoami) `id`")

    assert cmd[1] == "-p"
    # The whole hostile string stays a single argv element.
    assert cmd[2] == "rm -rf / ; echo $(whoami) `id`"


# --------------------------------------------------------------------------
# The publish guard
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Deliberate fixtures — each is a shape the guard MUST refuse to publish.
        # None is a real credential; the pragmas stop the repo's own scanner from
        # flagging the test that proves the scanner works.
        "API_KEY_PEPPER=" + ("a3f8b21c9d4e5f60" * 4),  # pragma: allowlist secret
        "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIE",  # pragma: allowlist secret
        "access_token: " + "aBcDeFgH12345678IjKlMnOp",  # pragma: allowlist secret
        "FERNET_SALT = " + "9f8e7d6c5b4a3928170",  # pragma: allowlist secret
    ],
)
def test_publish_guard_blocks_credential_shapes(text):
    from services.publish_guard import scan_text

    verdict = scan_text(text, require_scanner=False)

    assert verdict["safe"] is False
    assert verdict["findings"]


def test_publish_guard_allows_ordinary_prose():
    from services.publish_guard import scan_text

    verdict = scan_text(
        "## Problem\nThe exit job did not fire. See services/foo.py:2621.",
        require_scanner=False,
    )

    assert verdict["safe"] is True


def test_publish_guard_fails_closed_when_the_scanner_cannot_run():
    """A scanner that degrades to 'looks fine' is worse than none — it is trusted."""
    from services import publish_guard

    with patch.object(publish_guard, "_detect_secrets_findings", return_value=([], False)):
        verdict = publish_guard.scan_text("perfectly innocent text")

    assert verdict["safe"] is False
    assert any("failing closed" in f for f in verdict["findings"])


def test_gh_write_paths_go_through_the_guard():
    from services import github_cli_client as gh

    leaky = "APP_KEY=" + ("a3f8b21c9d4e5f60" * 4)  # pragma: allowlist secret
    for call in (
        lambda: gh.create_issue("t", leaky, dry_run=False),
        lambda: gh.comment_issue(1, leaky, dry_run=False),
        lambda: gh.update_body(1, leaky, dry_run=False),
    ):
        with patch.object(gh, "_run") as run, pytest.raises(gh.GhError, match="publish guard"):
            call()
        # Nothing reached the CLI.
        run.assert_not_called()


def test_gh_verb_allow_list_rejects_dangerous_operations():
    from services import github_cli_client as gh

    for verb in ("merge", "delete", "transfer"):
        with pytest.raises(gh.GhError, match="allow-list"):
            gh._run(["issue", verb, "1"])


# --------------------------------------------------------------------------
# Evidence requirements
# --------------------------------------------------------------------------


def test_finding_citing_a_nonexistent_path_is_dropped():
    reply = _reply(
        [_finding(evidence=[{"path": "services/not_a_real_file_xyz.py", "line": 1, "note": "n"}])]
    )

    parsed = inv.parse_reply(reply, _digest(), {FP})

    assert parsed["findings"] == []
    assert "no verifiable evidence" in parsed["dropped"]


def test_finding_with_no_evidence_is_dropped():
    parsed = inv.parse_reply(_reply([_finding(evidence=[])]), _digest(), {FP})

    assert parsed["findings"] == []


def test_finding_with_a_real_path_survives():
    parsed = inv.parse_reply(_reply([_finding()]), _digest(), {FP})

    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["evidence"][0]["path"].endswith("postmarket_investigation.py")


def test_unknown_fingerprint_becomes_a_proposed_finding_not_a_verdict():
    """The agent may now find new things — but then it stands on its own evidence."""
    parsed = inv.parse_reply(_reply([_finding(fingerprint="not_a_real_fp")]), _digest(), {FP})

    assert len(parsed["findings"]) == 1
    assert parsed["findings"][0]["proposed"] is True
    assert parsed["findings"][0]["fingerprint"] is None


def test_log_template_citation_is_accepted_when_it_matches_the_digest():
    parsed = inv.parse_reply(
        _reply([_finding(fingerprint=None, evidence=[{"note": "Failed to query <v>"}])]),
        _digest(),
        {FP},
    )

    assert len(parsed["findings"]) == 1


def test_invalid_verdict_and_severity_are_normalised():
    parsed = inv.parse_reply(
        _reply([_finding(verdict="definitely broken", severity="CATASTROPHIC")]), _digest(), {FP}
    )

    finding = parsed["findings"][0]
    assert finding["verdict"] == "unverified"
    assert finding["severity"] == "P2"


# --------------------------------------------------------------------------
# Failure modes
# --------------------------------------------------------------------------


def test_logged_out_cli_reports_its_own_status():
    with patch(
        "services.llm_review_client.probe_claude_health",
        return_value={"reachable": False, "reason": "not_logged_in", "detail": "run claude login"},
    ):
        result = inv.investigate(_digest(), _contracts())

    assert result["status"] == inv.STATUS_NOT_LOGGED_IN
    assert result["findings"] == []


def test_timeout_is_reported():
    with (
        patch(
            "services.llm_review_client.probe_claude_health",
            return_value={"reachable": True, "reason": "ok"},
        ),
        patch("services.llm_agent_client.invoke_claude_agent", side_effect=TimeoutError("slow")),
    ):
        result = inv.investigate(_digest(), _contracts())

    assert result["status"] == inv.STATUS_TIMEOUT


def test_unparseable_reply_degrades():
    with (
        patch(
            "services.llm_review_client.probe_claude_health",
            return_value={"reachable": True, "reason": "ok"},
        ),
        patch("services.llm_agent_client.invoke_claude_agent", return_value=("no json here", "")),
    ):
        result = inv.investigate(_digest(), _contracts())

    assert result["status"] == inv.STATUS_PARSE_FAILED


def test_quiet_day_with_no_violations_and_no_errors_skips():
    digest = _digest(logs={"errors": {"total": 0, "top_templates": []}})
    result = inv.investigate(digest, {"violations": [], "evaluated": True})

    assert result["status"] == inv.STATUS_SKIPPED_NOTHING


def test_disabled_flag_skips(monkeypatch):
    monkeypatch.setenv("POSTMARKET_INVESTIGATION_ENABLED", "false")
    result = inv.investigate(_digest(), _contracts())

    assert result["status"] == inv.STATUS_SKIPPED_DISABLED


# --------------------------------------------------------------------------
# Filing
# --------------------------------------------------------------------------


def test_only_confirmed_findings_are_filed():
    """An issue asserts something IS wrong; a refuted finding must not become one."""
    findings = [
        _finding(verdict="confirmed", title="real"),
        _finding(verdict="refuted", title="false alarm"),
        _finding(verdict="unverified", title="unclear"),
    ]
    with (
        patch.object(inv, "_existing_issue_for", return_value=None),
        patch(
            "services.github_cli_client.create_issue",
            side_effect=lambda t, b, labels=None, dry_run=True: {
                "created": not dry_run,
                "title": t,
            },
        ) as create,
    ):
        result = inv.file_findings(findings, _digest(), dry_run=True, max_issues=5)

    assert [c.kwargs.get("labels") is not None for c in create.call_args_list] == [True]
    assert len(result["filed"]) == 1
    assert {s["verdict"] for s in result["skipped"]} == {"refuted", "unverified"}


def test_recurrence_comments_instead_of_opening_a_second_issue():
    with (
        patch.object(inv, "_existing_issue_for", return_value=511),
        patch("services.github_cli_client.comment_issue", return_value=False) as comment,
        patch("services.github_cli_client.create_issue") as create,
    ):
        result = inv.file_findings([_finding()], _digest(), dry_run=True)

    comment.assert_called_once()
    create.assert_not_called()
    assert result["recurrences"][0]["issue"] == 511


def test_rate_cap_diverts_overflow_to_the_audit_log(tmp_path, monkeypatch):
    monkeypatch.setenv("POSTMARKET_REPO_ROOT", str(tmp_path))
    findings = [_finding(title=f"finding {i}") for i in range(5)]

    with (
        patch.object(inv, "_existing_issue_for", return_value=None),
        patch(
            "services.github_cli_client.create_issue",
            side_effect=lambda t, b, labels=None, dry_run=True: {"created": False, "title": t},
        ),
    ):
        result = inv.file_findings(findings, _digest(), dry_run=True, max_issues=2)

    assert len(result["filed"]) == 2
    assert len(result["overflow"]) == 3
    # Capped findings must still be recoverable, not silently dropped.
    recorded = (tmp_path / "audit" / "proposed_fixes.jsonl").read_text(encoding="utf-8")
    assert recorded.count("rate-capped finding") == 3


def test_guard_block_during_filing_is_recorded_not_swallowed():
    from services.github_cli_client import GhError

    with (
        patch.object(inv, "_existing_issue_for", return_value=None),
        patch(
            "services.github_cli_client.create_issue", side_effect=GhError("publish guard blocked")
        ),
    ):
        result = inv.file_findings([_finding()], _digest(), dry_run=True)

    assert result["filed"] == []
    assert len(result["blocked"]) == 1


def test_issue_body_carries_the_dedupe_marker_and_validation_section():
    body = inv.render_issue_body(_finding(), _digest())

    assert f"{inv._FINGERPRINT_MARKER}: {FP}" in body
    assert "## Validation" in body
    assert "- [ ] Confirm the exit job appears in the scheduler." in body
    assert "services/postmarket_investigation.py" in body
    # States plainly that a human owns the fix.
    assert "never edits, branches, or commits" in body


def test_filing_defaults_to_dry_run(monkeypatch):
    monkeypatch.delenv("POSTMARKET_FILING_MODE", raising=False)

    with (
        patch.object(inv, "_existing_issue_for", return_value=None),
        patch("services.github_cli_client.create_issue", return_value={"created": False}) as create,
    ):
        result = inv.file_findings([_finding()], _digest())

    assert result["dry_run"] is True
    assert create.call_args.kwargs["dry_run"] is True


def test_dedupe_matches_on_the_body_marker():
    issues = [
        {"number": 100, "body": "unrelated"},
        {"number": 200, "body": f"<!-- {inv._FINGERPRINT_MARKER}: {FP} -->\nbody"},
    ]
    with patch("services.github_cli_client.list_issues", return_value=issues):
        assert inv._existing_issue_for(FP) == 200
        assert inv._existing_issue_for("other") is None
        assert inv._existing_issue_for(None) is None
