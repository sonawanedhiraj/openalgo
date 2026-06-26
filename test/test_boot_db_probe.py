"""Unit tests for the boot-time orphan-process guard on historify.duckdb.

The guard probes the file once at boot and aborts with the holding PID parsed
from DuckDB's error message. These tests pin down the truth table:

- flag off → no-op
- file missing → no-op
- probe succeeds → no-op
- transient lock + foreign holder → SystemExit(1)
- transient lock + self holder → no-op (in-process fallback case)
- transient lock + unparseable PID → log + return (don't false-positive)
- non-transient error → log + return (don't false-positive)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import duckdb
import pytest

from services import boot_db_probe


@pytest.fixture
def real_duckdb_file(tmp_path):
    """A real (empty) DuckDB file the probe can open."""
    path = tmp_path / "historify_test.duckdb"
    conn = duckdb.connect(str(path))
    conn.close()
    return str(path)


def test_flag_off_is_noop(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "false")
    # Even if the file is wedged, the guard returns without probing.
    with patch.object(duckdb, "connect", side_effect=AssertionError("should not be called")):
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)


def test_missing_file_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    missing = str(tmp_path / "does_not_exist.duckdb")
    # No exception, no SystemExit.
    boot_db_probe.assert_historify_unlocked(missing)


def test_clean_file_passes(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    boot_db_probe.assert_historify_unlocked(real_duckdb_file)


def test_aborts_when_foreign_pid_holds_file(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    foreign_pid = os.getpid() + 1  # any non-self pid
    err = duckdb.IOException(
        f'IO Error: Cannot open file "{real_duckdb_file}": being used by another process. '
        f"File is already open in python.exe (PID {foreign_pid})"
    )

    service_mock = MagicMock()
    with (
        patch.object(duckdb, "connect", side_effect=err),
        patch("services.notification_service.get_notification_service", return_value=service_mock),
        pytest.raises(SystemExit) as excinfo,
    ):
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)

    assert excinfo.value.code == 1
    service_mock.notify.assert_called_once()
    # Holder PID + path are reported in the alert kwargs.
    call = service_mock.notify.call_args
    assert call.args[0] == "boot_db_probe"
    assert call.kwargs["holder_pid"] == foreign_pid
    assert call.kwargs["self_pid"] == os.getpid()
    assert call.kwargs["severity"] == "critical"
    assert str(foreign_pid) in call.args[1]


def test_self_pid_holder_does_not_abort(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    err = duckdb.IOException(
        f'IO Error: Cannot open file "{real_duckdb_file}": being used by another process. '
        f"File is already open in python.exe (PID {os.getpid()})"
    )
    with patch.object(duckdb, "connect", side_effect=err):
        # Should return cleanly, not exit.
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)


def test_unparseable_pid_does_not_abort(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    # Lock-pattern message but no "(PID nnn)" — we err on the side of not
    # crashing the operator's boot on a malformed error.
    err = duckdb.IOException(
        'IO Error: Cannot open file "/tmp/x.duckdb": being used by another process.'
    )
    with patch.object(duckdb, "connect", side_effect=err):
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)


def test_non_transient_error_does_not_abort(monkeypatch, real_duckdb_file):
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    # An error that is_transient_lock_error() does NOT recognise → we ignore.
    err = duckdb.IOException("IO Error: Some unrelated disk failure")
    with patch.object(duckdb, "connect", side_effect=err):
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)


def test_pid_regex_extracts_decimal_pid():
    assert boot_db_probe._parse_holding_pid("python.exe (PID 20124)") == 20124
    assert boot_db_probe._parse_holding_pid("python.exe (pid 7)") == 7
    assert boot_db_probe._parse_holding_pid("no pid here") is None


def test_config_mismatch_is_recognised_transient(monkeypatch, real_duckdb_file):
    """The in-process config-mismatch error is also a transient-lock signal but
    typically comes from THIS process — we recognise it and don't abort."""
    monkeypatch.setenv("OPENALGO_BOOT_DB_PROBE_ENABLED", "true")
    err = duckdb.ConnectionException(
        f"Can't open a connection to same database file with a different configuration "
        f"than existing connections (PID {os.getpid()})"
    )
    with patch.object(duckdb, "connect", side_effect=err):
        boot_db_probe.assert_historify_unlocked(real_duckdb_file)
