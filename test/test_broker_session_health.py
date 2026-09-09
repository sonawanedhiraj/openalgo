"""Tests for ``broker_session_health.probe_token`` (issue #658).

The token-probing core extracted from ``is_live_broker_session`` so the
auto-login watcher's child probe shares the exact same broker verification as
the primary. Fake broker modules are injected via ``sys.modules`` — that is
what ``importlib.import_module`` consults first, so no real broker module (or
network call) is ever touched.
"""

from __future__ import annotations

import sys
import types

import pytest

import services.broker_session_health as health


def _fake_broker(monkeypatch, name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(f"broker.{name}.api.funds")
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, f"broker.{name}.api.funds", mod)
    return mod


def test_probe_token_live_via_test_auth_token(monkeypatch):
    _fake_broker(monkeypatch, "fakeb", test_auth_token=lambda t: (True, None))
    assert health.probe_token("fakeb", "key:tok") is True


def test_probe_token_dead_via_test_auth_token(monkeypatch):
    _fake_broker(monkeypatch, "fakeb", test_auth_token=lambda t: (False, "Token expired"))
    assert health.probe_token("fakeb", "key:tok") is False


def test_probe_token_raising_probe_is_dead(monkeypatch):
    def boom(token):
        raise RuntimeError("socket down")

    _fake_broker(monkeypatch, "fakeb", test_auth_token=boom)
    assert health.probe_token("fakeb", "key:tok") is False


def test_probe_token_fallback_margin_data_success(monkeypatch):
    _fake_broker(monkeypatch, "fakeb", get_margin_data=lambda t: {"availablecash": 100.0})
    assert health.probe_token("fakeb", "key:tok") is True


def test_probe_token_fallback_margin_data_failure_payload(monkeypatch):
    _fake_broker(
        monkeypatch,
        "fakeb",
        get_margin_data=lambda t: {"status": "error", "message": "Invalid token"},
    )
    assert health.probe_token("fakeb", "key:tok") is False


def test_probe_token_empty_inputs_are_dead():
    assert health.probe_token("", "key:tok") is False
    assert health.probe_token("fakeb", "") is False


def test_probe_token_unimportable_broker_is_dead():
    assert health.probe_token("no_such_broker_xyz", "key:tok") is False


def test_is_live_broker_session_routes_through_probe_token(monkeypatch):
    """The primary probe now delegates to the shared core — pin it so the
    primary and child paths cannot drift apart again."""
    import database.auth_db as auth_db

    auth_obj = types.SimpleNamespace(broker="fakeb", auth="ciphertext")
    lookups = []

    class _Query:
        @staticmethod
        def filter_by(**kw):
            lookups.append(kw)
            return types.SimpleNamespace(first=lambda: auth_obj)

    monkeypatch.setattr(auth_db.Auth, "query", _Query(), raising=False)
    monkeypatch.setattr(auth_db, "decrypt_token", lambda v: "key:tok")
    monkeypatch.setattr(health, "primary_username", lambda: "rajandran")

    calls = []

    def fake_probe(broker, token):
        calls.append((broker, token))
        return True

    monkeypatch.setattr(health, "probe_token", fake_probe)
    assert health.is_live_broker_session() is True
    assert calls == [("fakeb", "key:tok")]
    # #719: the PRIMARY's own row, never "the first non-revoked row" — with the
    # primary revoked and a child logged in, first() returned the child.
    assert lookups == [{"name": "rajandran", "is_revoked": False}]


def test_is_live_broker_session_false_without_admin_user(monkeypatch):
    """No admin user → no primary row to probe → dead, never a table scan."""
    import database.auth_db as auth_db

    monkeypatch.setattr(health, "primary_username", lambda: None)
    monkeypatch.setattr(
        auth_db.Auth,
        "query",
        types.SimpleNamespace(filter_by=lambda **kw: pytest.fail("must not scan the table")),
        raising=False,
    )
    assert health.is_live_broker_session() is False


def test_is_live_session_for_probes_named_row(monkeypatch):
    """Child rows (``acct:<id>``) are probed by name through the same core."""
    import database.auth_db as auth_db

    lookups = []
    auth_obj = types.SimpleNamespace(broker="fakeb", auth="ciphertext")

    class _Query:
        @staticmethod
        def filter_by(**kw):
            lookups.append(kw)
            return types.SimpleNamespace(first=lambda: auth_obj)

    monkeypatch.setattr(auth_db.Auth, "query", _Query(), raising=False)
    monkeypatch.setattr(auth_db, "decrypt_token", lambda v: "key:tok")
    monkeypatch.setattr(health, "probe_token", lambda broker, token: False)
    assert health.is_live_session_for("acct:3") is False
    assert lookups == [{"name": "acct:3", "is_revoked": False}]
