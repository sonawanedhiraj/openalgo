"""Unit tests for the canonical "is broker session fresh?" predicate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services import broker_session_state


def test_empty_user_is_never_fresh(monkeypatch):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "true")
    assert broker_session_state.is_broker_session_fresh(None) is False
    assert broker_session_state.is_broker_session_fresh("") is False


def test_gate_off_is_permissive_for_any_user(monkeypatch):
    """Legacy behaviour preserved when the gate flag is off."""
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "false")
    # Even if get_auth_token would return None, the gate returns True when off.
    with patch("database.auth_db.get_auth_token", return_value=None):
        assert broker_session_state.is_broker_session_fresh("dheeraj") is True


def test_gate_on_returns_true_when_token_present(monkeypatch):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "true")
    with patch("database.auth_db.get_auth_token", return_value="fresh-token-abc"):
        assert broker_session_state.is_broker_session_fresh("dheeraj") is True


def test_gate_on_returns_false_when_token_missing(monkeypatch):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "true")
    with patch("database.auth_db.get_auth_token", return_value=None):
        assert broker_session_state.is_broker_session_fresh("dheeraj") is False


def test_gate_on_returns_false_when_token_empty_string(monkeypatch):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "true")
    with patch("database.auth_db.get_auth_token", return_value=""):
        assert broker_session_state.is_broker_session_fresh("dheeraj") is False


def test_gate_on_returns_false_when_auth_db_raises(monkeypatch):
    """An auth_db lookup failure must NOT propagate as a freshness=true crash."""
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", "true")
    with patch("database.auth_db.get_auth_token", side_effect=RuntimeError("db down")):
        assert broker_session_state.is_broker_session_fresh("dheeraj") is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1"])
def test_gate_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", value)
    # Only "true" (case-insensitive) is accepted as a safety measure — verify.
    expected = value.lower() == "true"
    assert broker_session_state.gate_enabled() is expected


@pytest.mark.parametrize("value", ["false", "False", "no", "0", "off"])
def test_gate_disabled_default_and_falsey_values(monkeypatch, value):
    monkeypatch.setenv("BROKER_FRESHNESS_GATE_ENABLED", value)
    assert broker_session_state.gate_enabled() is False


def test_gate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("BROKER_FRESHNESS_GATE_ENABLED", raising=False)
    assert broker_session_state.gate_enabled() is False
