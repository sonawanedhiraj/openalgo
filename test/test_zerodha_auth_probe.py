"""Zerodha quiet auth probe (issue #464).

On any boot with a dead daily token (weekend/holiday/pre-login morning) the
boot waiters poll ``broker_session_health.is_live_broker_session()`` every
15s. Zerodha exposed no ``test_auth_token``, so every poll fell through to
``get_margin_data`` — which logs each failure at ERROR — flooding
errors.jsonl (241 lines in the 2026-07-26 14:05→14:25 pre-login window).

These tests pin the fix's two guarantees:

1. ``broker.zerodha.api.funds.test_auth_token`` exists, returns the
   ``(is_valid, error_message)`` convention, and NEVER logs at ERROR on the
   expected dead-token path.
2. ``is_live_broker_session()`` prefers ``test_auth_token`` and does not
   touch ``get_margin_data`` when the probe is present.
"""

import logging
import types
from unittest.mock import MagicMock, patch

from broker.zerodha.api import funds as zerodha_funds


def _probe_errors(caplog):
    """ERROR+ records emitted BY THE PROBE.

    Scoped to the module under test on purpose: `caplog.records` collects every
    record propagating to root, including ones emitted from background threads
    other tests left running (an unrelated `database.auth_db` failure on the
    same xdist worker was enough to fail these). The guarantee #464 pins is
    "``test_auth_token`` does not log at ERROR", not "nothing anywhere does".
    """
    return [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and r.name.startswith("broker.zerodha")
    ]


def _client_returning(status_code, payload):
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    client.get.return_value = response
    return client


class TestZerodhaTestAuthToken:
    def test_valid_token(self):
        client = _client_returning(200, {"status": "success", "data": {"user_id": "AB1234"}})
        with patch("broker.zerodha.api.funds.get_httpx_client", return_value=client):
            is_valid, message = zerodha_funds.test_auth_token("key:tok")
        assert is_valid is True
        assert message is None
        # Probe hits the lightweight profile endpoint, not margins/positions.
        assert "/user/profile" in client.get.call_args[0][0]

    def test_dead_token_returns_reason_without_error_log(self, caplog):
        client = _client_returning(
            403,
            {"status": "error", "message": "Incorrect `api_key` or `access_token`."},
        )
        with (
            patch("broker.zerodha.api.funds.get_httpx_client", return_value=client),
            caplog.at_level(logging.DEBUG, logger="broker.zerodha.api.funds"),
        ):
            is_valid, message = zerodha_funds.test_auth_token("key:dead")
        assert is_valid is False
        assert "Incorrect" in message
        assert not _probe_errors(caplog)

    def test_malformed_token_no_error_log(self, caplog):
        # The 2026-07-26 spam variant: header without api_key:access_token shape.
        client = _client_returning(
            403,
            {
                "status": "error",
                "message": "authorization value should atleast be `api_key`:`access_token`",
            },
        )
        with (
            patch("broker.zerodha.api.funds.get_httpx_client", return_value=client),
            caplog.at_level(logging.DEBUG, logger="broker.zerodha.api.funds"),
        ):
            is_valid, message = zerodha_funds.test_auth_token("None")
        assert is_valid is False
        assert "api_key" in message
        assert not _probe_errors(caplog)

    def test_network_exception_no_error_log(self, caplog):
        client = MagicMock()
        client.get.side_effect = ConnectionError("connection refused")
        with (
            patch("broker.zerodha.api.funds.get_httpx_client", return_value=client),
            caplog.at_level(logging.DEBUG, logger="broker.zerodha.api.funds"),
        ):
            is_valid, message = zerodha_funds.test_auth_token("key:tok")
        assert is_valid is False
        assert "connection refused" in message
        assert not _probe_errors(caplog)

    def test_non_json_response_no_error_log(self, caplog):
        client = MagicMock()
        response = MagicMock()
        response.status_code = 502
        response.json.side_effect = ValueError("not json")
        client.get.return_value = response
        with (
            patch("broker.zerodha.api.funds.get_httpx_client", return_value=client),
            caplog.at_level(logging.DEBUG, logger="broker.zerodha.api.funds"),
        ):
            is_valid, message = zerodha_funds.test_auth_token("key:tok")
        assert is_valid is False
        assert message
        assert not _probe_errors(caplog)


class TestHealthProbePrefersTestAuthToken:
    def _run_probe(self, monkeypatch, probe_module):
        """Drive is_live_broker_session with a stubbed auth row + broker module."""
        from services import broker_session_health

        row = types.SimpleNamespace(broker="zerodha", auth="cipher")

        class _FakeQuery:
            def filter_by(self, **kwargs):
                return self

            def first(self):
                return row

        class _FakeAuth:
            query = _FakeQuery()

        monkeypatch.setattr("database.auth_db.Auth", _FakeAuth)
        monkeypatch.setattr("database.auth_db.decrypt_token", lambda cipher: "key:tok")

        fake_importlib = types.SimpleNamespace(import_module=lambda name: probe_module)
        monkeypatch.setattr(broker_session_health, "importlib", fake_importlib)

        return broker_session_health.is_live_broker_session()

    def test_dead_token_uses_probe_not_margin_call(self, monkeypatch):
        margin_calls = []
        probe_module = types.SimpleNamespace(
            test_auth_token=lambda tok: (False, "Incorrect `api_key` or `access_token`."),
            get_margin_data=lambda tok: margin_calls.append(tok) or {},
        )
        assert self._run_probe(monkeypatch, probe_module) is False
        assert margin_calls == []

    def test_live_token_uses_probe_not_margin_call(self, monkeypatch):
        margin_calls = []
        probe_module = types.SimpleNamespace(
            test_auth_token=lambda tok: (True, None),
            get_margin_data=lambda tok: margin_calls.append(tok) or {},
        )
        assert self._run_probe(monkeypatch, probe_module) is True
        assert margin_calls == []
