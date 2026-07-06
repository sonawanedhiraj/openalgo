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


def test_skipped_when_env_flag_set(monkeypatch):
    """``OPENALGO_SKIP_SINGLETON_GUARD=1`` returns early without probing.

    Load-bearing: start.sh sets this flag before invoking gunicorn so that
    each worker's app.py import does NOT re-run the bind probe (which would
    fail because the gunicorn master has already bound :5000 → worker exits
    1 → container crashloops). Without this bypass the Docker boot fails
    (cd-docker-e2e issue #183 / #186).
    """

    # If the guard actually probed, this fake socket would force a SystemExit
    # because every bind raises. Reaching the end without raising proves the
    # bypass was taken.
    class _AlwaysFails:
        def __init__(self, *a, **k):
            pass

        def bind(self, addr):
            raise OSError("would-be conflict")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _AlwaysFails)
    monkeypatch.setenv("OPENALGO_SKIP_SINGLETON_GUARD", "1")

    # Should NOT raise — the bypass returns before the bind probe.
    singleton_guard.check_singleton_or_abort()


def test_env_flag_only_skips_when_exactly_one(monkeypatch):
    """Anything other than literal ``1`` (empty, ``true``, ``0``) still
    runs the guard. Belt-and-braces so a typo doesn't silently disable it."""

    class _AlwaysFails:
        def __init__(self, *a, **k):
            pass

        def bind(self, addr):
            raise OSError("port already in use")

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", _AlwaysFails)

    for unsafe in ("", "0", "true", "yes", "TRUE"):
        monkeypatch.setenv("OPENALGO_SKIP_SINGLETON_GUARD", unsafe)
        with pytest.raises(SystemExit) as exc:
            singleton_guard.check_singleton_or_abort()
        assert exc.value.code == 1, f"value={unsafe!r} should NOT bypass the guard"
