"""Tests for ``services.strategy_preflight`` (issue #162 — S1).

The preflight layer is the system-enforced gate that replaces operator
memory. Sandbox flips are always allowed; LIVE flips must clear every default
gate plus any strategy-specific gates declared in
``strategies/<name>/preflight.py``.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from services import strategy_preflight
from services.strategy_preflight import (
    PreflightCheck,
    PreflightResult,
    check_broker_session_live,
    check_no_orphan_trades,
    check_recent_duckdb_errors,
    default_preflight,
    run_preflight,
)

# --------------------------------------------------------------------------- #
# Shape / utility classes
# --------------------------------------------------------------------------- #


def test_preflight_result_from_checks_aggregates_correctly():
    checks = [
        PreflightCheck(name="a", passed=True),
        PreflightCheck(name="b", passed=False, blocker_message="b failed"),
        PreflightCheck(name="c", passed=True, warning_message="c is fishy"),
    ]
    r = PreflightResult.from_checks(checks, snapshot={"k": 1})
    assert r.can_flip is False
    assert r.blockers == ["b failed"]
    assert r.warnings == ["c is fishy"]
    assert r.snapshot == {"k": 1}


def test_preflight_result_allow_is_unconditional_pass():
    r = PreflightResult.allow(snapshot={"x": 2})
    assert r.can_flip is True
    assert r.blockers == []
    assert r.warnings == []
    assert r.snapshot == {"x": 2}


def test_preflight_result_merge_combines_two_results():
    a = PreflightResult(can_flip=True, blockers=[], warnings=["w1"], snapshot={"a": 1})
    b = PreflightResult(can_flip=False, blockers=["b1"], warnings=[], snapshot={"b": 2})
    m = a.merge(b)
    assert m.can_flip is False
    assert m.blockers == ["b1"]
    assert m.warnings == ["w1"]
    assert m.snapshot == {"a": 1, "b": 2}


# --------------------------------------------------------------------------- #
# Sandbox always allowed
# --------------------------------------------------------------------------- #


def test_sandbox_flip_always_allowed_no_checks_run():
    """The whole point of the gates is to protect LIVE — sandbox is the
    safe default state and must never be blocked."""
    with patch.object(strategy_preflight, "check_broker_session_live") as broker:
        result = run_preflight("anything", "sandbox")
    assert result.can_flip is True
    assert result.blockers == []
    broker.assert_not_called()


def test_invalid_target_mode_is_blocked():
    result = run_preflight("anything", "yolo")
    assert result.can_flip is False
    assert any("target_mode" in b for b in result.blockers)


# --------------------------------------------------------------------------- #
# Individual default gates
# --------------------------------------------------------------------------- #


def test_check_broker_session_live_passes_when_live():
    with patch("services.broker_session_health.is_live_broker_session", return_value=True):
        check = check_broker_session_live()
    assert check.passed is True
    assert check.blocker_message is None


def test_check_broker_session_live_blocks_when_not_live():
    with patch("services.broker_session_health.is_live_broker_session", return_value=False):
        check = check_broker_session_live()
    assert check.passed is False
    assert "Broker session" in (check.blocker_message or "")


def test_check_broker_session_live_fails_closed_on_exception():
    """An exception in the probe must NOT allow a flip — fail closed."""
    with patch(
        "services.broker_session_health.is_live_broker_session",
        side_effect=RuntimeError("boom"),
    ):
        check = check_broker_session_live()
    assert check.passed is False
    assert "probe failed" in (check.blocker_message or "").lower()


# --------------------------------------------------------------------------- #
# Default preflight composition
# --------------------------------------------------------------------------- #


def test_default_preflight_sandbox_returns_allow_without_running_checks():
    with (
        patch.object(strategy_preflight, "check_broker_session_live") as broker,
        patch.object(strategy_preflight, "check_no_orphan_trades") as orphan,
        patch.object(strategy_preflight, "check_recent_duckdb_errors") as dberr,
    ):
        result = default_preflight("some_strategy", "sandbox")
    assert result.can_flip is True
    broker.assert_not_called()
    orphan.assert_not_called()
    dberr.assert_not_called()


def test_default_preflight_live_runs_all_default_checks():
    with (
        patch.object(
            strategy_preflight,
            "check_broker_session_live",
            return_value=PreflightCheck(name="broker_session_live", passed=True),
        ) as broker,
        patch.object(
            strategy_preflight,
            "check_no_orphan_trades",
            return_value=PreflightCheck(name="no_orphan_trades", passed=True),
        ) as orphan,
        patch.object(
            strategy_preflight,
            "check_recent_duckdb_errors",
            return_value=PreflightCheck(name="recent_duckdb_errors", passed=True),
        ) as dberr,
    ):
        result = default_preflight("strat_x", "live")

    assert result.can_flip is True
    broker.assert_called_once()
    orphan.assert_called_once_with("strat_x")
    dberr.assert_called_once()


def test_default_preflight_blocks_when_any_default_check_fails():
    with (
        patch.object(
            strategy_preflight,
            "check_broker_session_live",
            return_value=PreflightCheck(
                name="broker_session_live",
                passed=False,
                blocker_message="broker down",
            ),
        ),
        patch.object(
            strategy_preflight,
            "check_no_orphan_trades",
            return_value=PreflightCheck(name="no_orphan_trades", passed=True),
        ),
        patch.object(
            strategy_preflight,
            "check_recent_duckdb_errors",
            return_value=PreflightCheck(name="recent_duckdb_errors", passed=True),
        ),
    ):
        result = default_preflight("strat_x", "live")
    assert result.can_flip is False
    assert "broker down" in result.blockers


# --------------------------------------------------------------------------- #
# Strategy-specific preflight discovery + fallback
# --------------------------------------------------------------------------- #


def test_run_preflight_uses_default_when_no_custom_module(monkeypatch):
    """A strategy without strategies/<name>/preflight.py uses the default."""
    sentinel = PreflightResult(
        can_flip=True, blockers=[], warnings=[], snapshot={"path": "default"}
    )
    monkeypatch.setattr(strategy_preflight, "default_preflight", lambda s, m: sentinel)
    result = run_preflight("does_not_exist_strategy_xyz", "live")
    assert result is sentinel


def test_run_preflight_dispatches_to_custom_module(monkeypatch):
    """When strategies/<name>/preflight.py exists, run_preflight calls its
    check_can_go_live and returns its result (with snapshot annotated)."""
    fake_module_name = "strategies.fake_strat_162.preflight"
    parent_name = "strategies.fake_strat_162"
    parent = types.ModuleType(parent_name)
    child = types.ModuleType(fake_module_name)

    custom_result = PreflightResult(
        can_flip=True,
        blockers=[],
        warnings=["custom warning"],
        snapshot={"custom_key": "v"},
    )

    def custom_check(target_mode: str) -> PreflightResult:
        assert target_mode == "live"
        return custom_result

    child.check_can_go_live = custom_check
    monkeypatch.setitem(sys.modules, parent_name, parent)
    monkeypatch.setitem(sys.modules, fake_module_name, child)

    result = run_preflight("fake_strat_162", "live")
    assert result.can_flip is True
    assert result.warnings == ["custom warning"]
    # Snapshot annotated with the path that ran.
    assert result.snapshot["preflight_path"] == fake_module_name
    assert result.snapshot["strategy_name"] == "fake_strat_162"
    assert result.snapshot["target_mode"] == "live"


def test_run_preflight_treats_custom_exception_as_blocker(monkeypatch):
    """A custom preflight that raises must NOT bypass safety — refuse the flip."""
    fake_module_name = "strategies.fake_strat_162b.preflight"
    parent_name = "strategies.fake_strat_162b"
    parent = types.ModuleType(parent_name)
    child = types.ModuleType(fake_module_name)

    def raising_check(target_mode):
        raise RuntimeError("custom preflight is broken")

    child.check_can_go_live = raising_check
    monkeypatch.setitem(sys.modules, parent_name, parent)
    monkeypatch.setitem(sys.modules, fake_module_name, child)

    result = run_preflight("fake_strat_162b", "live")
    assert result.can_flip is False
    assert any("raised" in b for b in result.blockers)


def test_run_preflight_rejects_non_preflightresult_return(monkeypatch):
    """A custom preflight that returns the wrong type is refused."""
    fake_module_name = "strategies.fake_strat_162c.preflight"
    parent_name = "strategies.fake_strat_162c"
    parent = types.ModuleType(parent_name)
    child = types.ModuleType(fake_module_name)
    child.check_can_go_live = lambda target_mode: {"can_flip": True}  # wrong shape
    monkeypatch.setitem(sys.modules, parent_name, parent)
    monkeypatch.setitem(sys.modules, fake_module_name, child)

    result = run_preflight("fake_strat_162c", "live")
    assert result.can_flip is False
    assert any("PreflightResult" in b for b in result.blockers)


# --------------------------------------------------------------------------- #
# Recent DuckDB errors gate — uses errors.jsonl
# --------------------------------------------------------------------------- #


def test_check_recent_duckdb_errors_passes_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # log/errors.jsonl doesn't exist here
    check = check_recent_duckdb_errors()
    assert check.passed is True


def test_check_recent_duckdb_errors_blocks_when_over_threshold(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATEGY_PREFLIGHT_RECENT_ERROR_THRESHOLD", "2")
    monkeypatch.setenv("STRATEGY_PREFLIGHT_RECENT_ERROR_WINDOW_MIN", "60")

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    errors_jsonl = log_dir / "errors.jsonl"

    ist = _tz(timedelta(hours=5, minutes=30))
    now = datetime.now(ist)
    recent_ts = now.strftime("%Y-%m-%d %H:%M:%S")
    old_ts = (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        # 5 recent in-window lock errors — exceeds threshold of 2
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "different configuration"}}\n',
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "different configuration"}}\n',
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "Failed to connect to DuckDB after 3 attempts"}}\n',
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "different configuration"}}\n',
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "being used by another process"}}\n',
        # Older lock error — outside window, should not count
        f'{{"ts": "{old_ts}", "level": "ERROR", "message": "different configuration"}}\n',
        # Unrelated error — should never count
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "some unrelated thing"}}\n',
    ]
    errors_jsonl.write_text("".join(lines), encoding="utf-8")

    check = check_recent_duckdb_errors()
    assert check.passed is False
    assert "5" in (check.blocker_message or "")  # the count is in the message


def test_check_recent_duckdb_errors_passes_when_under_threshold(tmp_path, monkeypatch):
    from datetime import datetime, timedelta
    from datetime import timezone as _tz

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STRATEGY_PREFLIGHT_RECENT_ERROR_THRESHOLD", "5")
    monkeypatch.setenv("STRATEGY_PREFLIGHT_RECENT_ERROR_WINDOW_MIN", "60")

    log_dir = tmp_path / "log"
    log_dir.mkdir()
    errors_jsonl = log_dir / "errors.jsonl"
    ist = _tz(timedelta(hours=5, minutes=30))
    recent_ts = datetime.now(ist).strftime("%Y-%m-%d %H:%M:%S")
    # Just 2 recent lock errors — under threshold
    errors_jsonl.write_text(
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "different configuration"}}\n'
        f'{{"ts": "{recent_ts}", "level": "ERROR", "message": "different configuration"}}\n',
        encoding="utf-8",
    )
    check = check_recent_duckdb_errors()
    assert check.passed is True
