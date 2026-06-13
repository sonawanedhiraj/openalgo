"""Tests for the boot-time singleton guard (utils/singleton_guard.py)."""

import socket

import pytest

from utils import singleton_guard


def test_aborts_when_port_5000_is_taken(monkeypatch):
    """If the port-bind probe raises (port already owned), the guard exits 1."""

    class _FakeSock:
        def __init__(self, *a, **k):
            pass

        def bind(self, addr):
            raise OSError("port already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _FakeSock)

    with pytest.raises(SystemExit) as exc:
        singleton_guard.check_singleton_or_abort()
    assert exc.value.code == 1


def test_passes_when_port_free_and_no_other_writer(monkeypatch):
    """Bind succeeds and no foreign process holds the DB → returns cleanly."""

    class _FakeSock:
        def __init__(self, *a, **k):
            self.closed = False

        def bind(self, addr):
            return None  # port is free

        def close(self):
            self.closed = True

    monkeypatch.setattr(socket, "socket", _FakeSock)
    # Force the psutil branch to be skipped so the test is hermetic and never
    # scans the real process table.
    monkeypatch.setitem(__import__("sys").modules, "psutil", None)

    # Should not raise SystemExit.
    singleton_guard.check_singleton_or_abort()
