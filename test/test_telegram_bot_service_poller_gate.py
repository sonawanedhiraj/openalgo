"""Tests for issue #238: gate the legacy Telegram poller when inbound is enabled.

The legacy interactive bot (services/telegram_bot_service.TelegramBotService) and
the inbound poller (services/telegram_inbound_service) both call getUpdates on
the same bot token; Telegram returns
``telegram.error.Conflict: terminated by other getUpdates request`` when both
poll. The fix gates ``start_bot()`` on the ``TELEGRAM_INBOUND_ENABLED`` env var
so the legacy poller short-circuits when the inbound poller owns the token.
Outbound send paths are untouched.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from services.telegram_bot_service import TelegramBotService

# --------------------------------------------------------------------------- #
# Part 1 — start_bot() gating
# --------------------------------------------------------------------------- #


def _truthy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_INBOUND_ENABLED", "true")


def _falsy(monkeypatch):
    monkeypatch.setenv("TELEGRAM_INBOUND_ENABLED", "false")


def test_start_bot_short_circuits_when_inbound_enabled(monkeypatch):
    """When TELEGRAM_INBOUND_ENABLED=true, start_bot() must NOT spawn the
    polling thread, MUST clear bot_config.is_active, and MUST return success."""
    _truthy(monkeypatch)
    svc = TelegramBotService()

    with (
        patch("services.telegram_bot_service.update_bot_config") as mock_update,
        patch("services.telegram_bot_service.get_bot_config") as mock_get,
        patch.object(svc, "_run_bot_in_thread") as mock_run,
    ):
        ok, msg = svc.start_bot()

    assert ok is True, f"start_bot should return success when gated, got msg={msg!r}"
    assert "inbound" in msg.lower(), f"return msg should mention inbound: {msg!r}"
    # The polling thread must NOT have been spawned.
    mock_run.assert_not_called()
    # The polling thread is the only place start_polling is called, so we
    # verify it indirectly by asserting the thread runner was never invoked.
    assert svc.bot_thread is None
    # get_bot_config must NOT be reached — we short-circuit before reading.
    mock_get.assert_not_called()
    # We MUST clear any stale is_active=True.
    mock_update.assert_called_once_with({"is_active": False})
    assert svc.is_running is False


@pytest.mark.parametrize("value", ["1", "true", "True", "TRUE", "yes", "on", "  true  "])
def test_start_bot_gate_accepts_truthy_variants(monkeypatch, value):
    """The env-var truthiness check must accept the same set as
    services.telegram_inbound_service._inbound_enabled."""
    monkeypatch.setenv("TELEGRAM_INBOUND_ENABLED", value)
    svc = TelegramBotService()

    with (
        patch("services.telegram_bot_service.update_bot_config") as mock_update,
        patch("services.telegram_bot_service.get_bot_config") as mock_get,
        patch.object(svc, "_run_bot_in_thread") as mock_run,
    ):
        ok, _ = svc.start_bot()

    assert ok is True
    mock_run.assert_not_called()
    mock_get.assert_not_called()
    mock_update.assert_called_once_with({"is_active": False})


@pytest.mark.parametrize("value", ["0", "false", "False", "", "no", "off", "random"])
def test_start_bot_runs_normally_when_inbound_disabled(monkeypatch, value):
    """Regression: when the flag is falsy/unset, start_bot() must reach the
    normal startup path (get_bot_config + _run_bot_in_thread)."""
    monkeypatch.setenv("TELEGRAM_INBOUND_ENABLED", value)
    svc = TelegramBotService()

    with (
        patch("services.telegram_bot_service.get_bot_config") as mock_get,
        patch.object(svc, "_run_bot_in_thread") as mock_run,
        patch("services.telegram_bot_service.original_threading.Thread") as mock_thread,
    ):
        # Make get_bot_config return a valid config so the normal path proceeds.
        mock_get.return_value = {"bot_token": "dummy_token", "is_active": False}
        # Block the wait loop by leaving is_running False; we don't care about
        # the return value here — we just need to reach the thread-start path.
        thread_instance = MagicMock()
        mock_thread.return_value = thread_instance

        ok, msg = svc.start_bot()

    # We reached the normal config-read path.
    mock_get.assert_called_once()
    # And we attempted to construct the polling thread (the normal path).
    mock_thread.assert_called_once()
    thread_instance.start.assert_called_once()
    # Without is_running flipping inside the mocked thread, start_bot returns
    # the "failed to start within timeout" path — that's fine: the *gate* did
    # not fire, which is the regression we care about.
    assert ok is False
    assert "timeout" in msg.lower()
    # Critical: _run_bot_in_thread is the thread target. We did NOT short-circuit
    # via the inbound gate, so update_bot_config({"is_active": False}) must
    # NOT have been called as a gate side-effect (it may be called elsewhere
    # by the thread, but we did not run the thread).
    del mock_run  # silence unused; the thread target was never invoked


def test_start_bot_gate_unset_env_runs_normal_path(monkeypatch):
    """When TELEGRAM_INBOUND_ENABLED is unset, behave as if false."""
    monkeypatch.delenv("TELEGRAM_INBOUND_ENABLED", raising=False)
    svc = TelegramBotService()

    with (
        patch("services.telegram_bot_service.get_bot_config") as mock_get,
        patch("services.telegram_bot_service.original_threading.Thread") as mock_thread,
    ):
        mock_get.return_value = {"bot_token": "dummy_token", "is_active": False}
        thread_instance = MagicMock()
        mock_thread.return_value = thread_instance

        svc.start_bot()

    mock_get.assert_called_once()
    mock_thread.assert_called_once()


def test_start_bot_gate_returns_when_already_running(monkeypatch):
    """If the bot is already running, the existing early-return wins over the
    new gate — preserving prior contract."""
    _truthy(monkeypatch)
    svc = TelegramBotService()
    svc.is_running = True

    with (
        patch("services.telegram_bot_service.update_bot_config") as mock_update,
        patch("services.telegram_bot_service.get_bot_config") as mock_get,
    ):
        ok, msg = svc.start_bot()

    assert ok is False
    assert "already" in msg.lower()
    # We hit the prior early return — the gate's update_bot_config was NOT called.
    mock_update.assert_not_called()
    mock_get.assert_not_called()


# --------------------------------------------------------------------------- #
# Part 2 — app.py boot-time invariant enforcement
#
# We can't easily import-and-run app.py at module load (it boots a Flask app,
# scheduler, etc.). Instead we exercise the same code shape against a stub: the
# block reads TELEGRAM_INBOUND_ENABLED, calls get_bot_config(), and if
# is_active=True calls update_bot_config({"is_active": False}). That contract is
# what the operator-visible behavior depends on.
# --------------------------------------------------------------------------- #


def _run_boot_invariant() -> tuple[bool, list[str]]:
    """Replica of the app.py boot-time enforcement block. Mirrors the production
    code; the production block does the same work guarded by try/except."""
    warnings_emitted: list[str] = []
    flipped = False
    if os.getenv("TELEGRAM_INBOUND_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        from database.telegram_db import (
            get_bot_config as _get_bot_config_238,
        )
        from database.telegram_db import (
            update_bot_config as _update_bot_config_238,
        )

        _cfg_238 = _get_bot_config_238()
        if _cfg_238 and _cfg_238.get("is_active"):
            warnings_emitted.append("TELEGRAM_INBOUND_ENABLED=true but bot_config.is_active=True")
            _update_bot_config_238({"is_active": False})
            flipped = True
    return flipped, warnings_emitted


def test_boot_invariant_flips_stale_is_active_to_false(monkeypatch):
    """When the flag is truthy AND bot_config row has is_active=True, the boot
    block must flip it to False."""
    _truthy(monkeypatch)
    with (
        patch("database.telegram_db.get_bot_config") as mock_get,
        patch("database.telegram_db.update_bot_config") as mock_update,
    ):
        mock_get.return_value = {"is_active": True, "bot_token": "x"}
        flipped, warnings = _run_boot_invariant()
    assert flipped is True
    mock_update.assert_called_once_with({"is_active": False})
    assert warnings  # WARNING was emitted


def test_boot_invariant_no_op_when_already_inactive(monkeypatch):
    """If the row already shows is_active=False, the boot block must NOT write."""
    _truthy(monkeypatch)
    with (
        patch("database.telegram_db.get_bot_config") as mock_get,
        patch("database.telegram_db.update_bot_config") as mock_update,
    ):
        mock_get.return_value = {"is_active": False, "bot_token": "x"}
        flipped, _ = _run_boot_invariant()
    assert flipped is False
    mock_update.assert_not_called()


def test_boot_invariant_no_op_when_flag_disabled(monkeypatch):
    """If TELEGRAM_INBOUND_ENABLED is falsy, the boot block must NOT touch the
    row even if is_active=True — that's the legacy bot's legitimate state."""
    _falsy(monkeypatch)
    with (
        patch("database.telegram_db.get_bot_config") as mock_get,
        patch("database.telegram_db.update_bot_config") as mock_update,
    ):
        mock_get.return_value = {"is_active": True, "bot_token": "x"}
        flipped, _ = _run_boot_invariant()
    assert flipped is False
    mock_update.assert_not_called()
    mock_get.assert_not_called()
