"""Tests for the Cowork↔Claude Code bridge guards (2026-06-11 retrospective item #6).

Covers the three counter-measures added after the 2026-06-11 incident:
  1. Market-hours block (409) on /fix-bug and /restart-app during 09:15–15:30
     IST on weekdays; pass-through after the close.
  2. Scoped pytest in the /fix-bug verify step — the test files that cover the
     reported error, or `-m unit`, never the full `pytest test/` suite.
  3. log/bridge_access.jsonl audit trail — one JSON line per request.

The clock is injected via bridge.server._now_ist so the market-hours branch is
deterministic; the claude subprocess and app restart are mocked so no real
process is spawned.
"""

import asyncio
import datetime as dt
import json

import pytest
import pytz
from fastapi.testclient import TestClient

from bridge import server
from bridge.server import FixBugRequest, _is_market_hours, _scoped_pytest_command

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient with the access log redirected to a tmp file."""
    monkeypatch.setattr(server, "ACCESS_LOG", tmp_path / "bridge_access.jsonl")
    monkeypatch.setattr(server, "LOG_DIR", tmp_path)
    # Idle state for every test (restart-app checks BUSY after the hours guard).
    server.state.status = server.BridgeStatus.IDLE
    server.state.current_task = None
    return TestClient(server.app)


def _set_clock(monkeypatch, *, weekday: int, hour: int, minute: int):
    """Pin bridge.server._now_ist to a fixed IST datetime.

    weekday: 0=Mon ... 6=Sun. Uses 2026-06-08 (a Monday) as the week anchor.
    """
    monday = dt.date(2026, 6, 8)  # Monday
    day = monday + dt.timedelta(days=weekday)
    fixed = IST.localize(dt.datetime(day.year, day.month, day.day, hour, minute))
    monkeypatch.setattr(server, "_now_ist", lambda: fixed)


# ---------------------------------------------------------------------------
# 1. Market-hours boundary logic (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "weekday,hour,minute,expected",
    [
        (0, 10, 0, True),  # Mon 10:00 — open
        (2, 9, 15, True),  # Wed 09:15 — exactly open
        (4, 15, 30, True),  # Fri 15:30 — exactly close (inclusive)
        (0, 9, 14, False),  # Mon 09:14 — one minute early
        (0, 15, 31, False),  # Mon 15:31 — one minute late
        (0, 3, 0, False),  # Mon 03:00 — pre-dawn
        (5, 10, 0, False),  # Sat 10:00 — weekend
        (6, 11, 0, False),  # Sun 11:00 — weekend
    ],
)
def test_is_market_hours_boundaries(weekday, hour, minute, expected):
    monday = dt.date(2026, 6, 8)
    day = monday + dt.timedelta(days=weekday)
    now = IST.localize(dt.datetime(day.year, day.month, day.day, hour, minute))
    assert _is_market_hours(now) is expected


# ---------------------------------------------------------------------------
# 2. /fix-bug and /restart-app refused during market hours (409)
# ---------------------------------------------------------------------------


def test_fix_bug_refused_during_market_hours(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=11, minute=0)  # Mon 11:00 IST
    resp = client.post("/fix-bug", json={"error_message": "boom"})
    assert resp.status_code == 409
    assert "market hours" in resp.json()["detail"].lower()


def test_restart_app_refused_during_market_hours(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=11, minute=0)
    resp = client.post("/restart-app", json={"kill_existing": True})
    assert resp.status_code == 409
    assert "market hours" in resp.json()["detail"].lower()


# ---------------------------------------------------------------------------
# 3. Pass-through after the close
# ---------------------------------------------------------------------------


def test_fix_bug_passes_through_after_hours(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=20, minute=0)  # Mon 20:00 IST

    captured = {}

    async def fake_run_claude(prompt, task_name, extra_tools=None):
        captured["prompt"] = prompt
        captured["task"] = task_name
        return {"success": True, "summary": "stub", "task": task_name}

    monkeypatch.setattr(server, "run_claude", fake_run_claude)
    resp = client.post(
        "/fix-bug",
        json={"error_message": "boom", "file_path": "services/notification_service.py"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert captured["task"] == "fix-bug"


def test_restart_app_passes_through_after_hours(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=20, minute=0)

    class _FakeProc:
        returncode = None
        pid = 4321

        async def communicate(self):
            return (b"", b"")

    async def fake_exec(*args, **kwargs):
        return _FakeProc()

    async def fake_sleep(_secs):
        return None

    monkeypatch.setattr(server.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    resp = client.post("/restart-app", json={"kill_existing": False})
    assert resp.status_code == 200
    assert resp.json()["success"] is True


# ---------------------------------------------------------------------------
# 4. Scoped pytest in /fix-bug (never the full suite)
# ---------------------------------------------------------------------------


def test_scoped_pytest_maps_source_to_its_test_file():
    cmd = _scoped_pytest_command(
        FixBugRequest(error_message="x", file_path="services/notification_service.py")
    )
    assert "test/test_notification_service.py" in cmd
    assert "pytest test/ -v" not in cmd
    assert "pytest test/ " not in cmd  # never the whole test/ dir as a target


def test_scoped_pytest_falls_back_to_unit_marker_for_unknown_file():
    cmd = _scoped_pytest_command(
        FixBugRequest(error_message="x", file_path="services/does_not_exist_xyz_qqq.py")
    )
    assert cmd == "uv run pytest -m unit -q --maxfail=5"


def test_scoped_pytest_picks_up_test_files_from_traceback():
    tb = 'File "services/eod_watchdog_service.py", line 42, in run\n    raise ValueError'
    cmd = _scoped_pytest_command(FixBugRequest(error_message="x", traceback=tb))
    assert "test/test_eod_watchdog_service.py" in cmd
    assert "pytest test/ -v" not in cmd


def test_fix_bug_prompt_embeds_scoped_pytest_not_full_suite(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=20, minute=0)  # after hours
    captured = {}

    async def fake_run_claude(prompt, task_name, extra_tools=None):
        captured["prompt"] = prompt
        return {"success": True, "task": task_name}

    monkeypatch.setattr(server, "run_claude", fake_run_claude)
    client.post(
        "/fix-bug",
        json={"error_message": "boom", "file_path": "services/notification_service.py"},
    )
    prompt = captured["prompt"]
    assert "test/test_notification_service.py" in prompt
    assert "uv run pytest test/ -v" not in prompt
    assert "Do NOT run the full" in prompt


# ---------------------------------------------------------------------------
# 5. Access-log audit trail
# ---------------------------------------------------------------------------


def test_access_log_written_per_request(client, monkeypatch):
    # A GET / request (no market-hours concern) should still be audited.
    resp = client.get("/")
    assert resp.status_code == 200

    log_path = server.ACCESS_LOG
    assert log_path.exists(), "bridge_access.jsonl was not created"
    lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    assert lines, "no access-log entries written"
    last = lines[-1]
    assert last["endpoint"] == "/"
    assert last["method"] == "GET"
    assert last["status"] == 200
    assert "ts" in last and "elapsed_ms" in last


def test_access_log_captures_body_summary_and_409(client, monkeypatch):
    _set_clock(monkeypatch, weekday=0, hour=11, minute=0)  # market hours -> 409
    resp = client.post("/fix-bug", json={"error_message": "the-boom", "file_path": "a.py"})
    assert resp.status_code == 409

    lines = [
        json.loads(line) for line in server.ACCESS_LOG.read_text().splitlines() if line.strip()
    ]
    entry = next(e for e in reversed(lines) if e["endpoint"] == "/fix-bug")
    assert entry["status"] == 409
    assert entry["method"] == "POST"
    # body summary carries the request fields (string values truncated).
    assert entry["body_summary"]["error_message"] == "the-boom"
    assert entry["body_summary"]["file_path"] == "a.py"
