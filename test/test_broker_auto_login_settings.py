"""Tests for the DB-backed auto-login master switch + per-child opt-in (issue #654).

Covers ``database/broker_auto_login_settings_db`` (env-seed fallback, set/get,
fail-open) and the watcher's child-target filter honouring ``auto_login_enabled``.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def settings_db(monkeypatch):
    import database.broker_auto_login_settings_db as db

    db.init_db()
    yield db
    try:
        db.db_session.query(db.BrokerAutoLoginSettings).delete()
        db.db_session.commit()
    finally:
        db.db_session.remove()


def test_defaults_to_env_seed_when_unset(settings_db, monkeypatch):
    monkeypatch.setenv("BROKER_AUTO_LOGIN_ENABLED", "true")
    assert settings_db.get_enabled() is True
    monkeypatch.setenv("BROKER_AUTO_LOGIN_ENABLED", "false")
    assert settings_db.get_enabled() is False


def test_db_row_wins_over_env(settings_db, monkeypatch):
    monkeypatch.setenv("BROKER_AUTO_LOGIN_ENABLED", "false")
    assert settings_db.set_enabled(True, updated_by="user:tester") is True
    assert settings_db.get_enabled() is True  # DB row overrides the env seed

    settings_db.set_enabled(False)
    assert settings_db.get_enabled() is False


def test_watcher_enabled_reads_db(settings_db, monkeypatch):
    import services.broker_auto_login_watcher as w

    monkeypatch.setenv("BROKER_AUTO_LOGIN_ENABLED", "false")
    settings_db.set_enabled(True)
    assert w.watcher_enabled() is True
    settings_db.set_enabled(False)
    assert w.watcher_enabled() is False


def test_child_targets_respect_auto_login_flag(monkeypatch):
    import services.broker_auto_login_watcher as w

    accounts = [
        {
            "id": 1,
            "display_name": "on",
            "is_enabled": True,
            "has_password": True,
            "auto_login_enabled": True,
        },
        {
            "id": 2,
            "display_name": "off",
            "is_enabled": True,
            "has_password": True,
            "auto_login_enabled": False,
        },
        {
            "id": 3,
            "display_name": "nopw",
            "is_enabled": True,
            "has_password": False,
            "auto_login_enabled": True,
        },
    ]
    import database.broker_accounts_db as adb

    monkeypatch.setattr(adb, "list_accounts", lambda: accounts)
    targets = w._list_child_targets()
    assert targets == [(1, "on")]  # only enabled + password + auto_login_enabled
