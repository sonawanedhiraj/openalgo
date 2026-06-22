"""Tests for GET /health/ws_proxy.

Uses a minimal Flask app with only health_bp registered to avoid the singleton
guard in app.py (which tries to bind port 5000, already held by live OpenAlgo).
"""

import os
import time
from unittest.mock import MagicMock, patch

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402


@pytest.fixture()
def client():
    from blueprints.health import health_bp

    app = Flask(__name__)
    app.register_blueprint(health_bp)
    app.config["TESTING"] = True
    return app.test_client()


def _closed_socket(monkeypatch):
    """Monkeypatch socket.socket so connect_ex returns non-zero (port closed)."""
    mock_sock = MagicMock()
    mock_sock.connect_ex.return_value = 1
    monkeypatch.setattr("blueprints.health.socket.socket", lambda *a, **k: mock_sock)


def _open_socket(monkeypatch):
    """Monkeypatch socket.socket so connect_ex returns 0 (port open)."""
    mock_sock = MagicMock()
    mock_sock.connect_ex.return_value = 0
    monkeypatch.setattr("blueprints.health.socket.socket", lambda *a, **k: mock_sock)


# ---------------------------------------------------------------------------
# Port closed → down
# ---------------------------------------------------------------------------


def test_down_when_port_closed(client, monkeypatch):
    _closed_socket(monkeypatch)
    resp = client.get("/health/ws_proxy")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "down"
    assert body["last_tick_age_sec"] is None
    assert body["subscribed_symbols"] is None


# ---------------------------------------------------------------------------
# Port open, recent tick → healthy
# ---------------------------------------------------------------------------


def test_healthy_with_recent_tick(client, monkeypatch):
    _open_socket(monkeypatch)

    proxy = MagicMock()
    proxy.last_message_time = {("NIFTY", "NSE_INDEX", "live"): time.time() - 5}
    proxy.subscription_index = {"NIFTY": True, "BANKNIFTY": True}

    with patch("websocket_proxy.app_integration._websocket_proxy_instance", proxy):
        resp = client.get("/health/ws_proxy")

    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["last_tick_age_sec"] is not None
    assert body["last_tick_age_sec"] < 60
    assert body["subscribed_symbols"] == 2


# ---------------------------------------------------------------------------
# Port open, no proxy instance → healthy (age unknown)
# ---------------------------------------------------------------------------


def test_healthy_no_proxy_instance(client, monkeypatch):
    _open_socket(monkeypatch)

    with patch("websocket_proxy.app_integration._websocket_proxy_instance", None):
        resp = client.get("/health/ws_proxy")

    body = resp.get_json()
    assert body["status"] == "healthy"
    assert body["last_tick_age_sec"] is None
    assert body["subscribed_symbols"] is None


# ---------------------------------------------------------------------------
# Port open, stale tick 60–179 s → degraded
# ---------------------------------------------------------------------------


def test_degraded_stale_tick(client, monkeypatch):
    _open_socket(monkeypatch)

    proxy = MagicMock()
    proxy.last_message_time = {("NIFTY", "NSE_INDEX", "live"): time.time() - 90}
    proxy.subscription_index = {"NIFTY": True}

    with patch("websocket_proxy.app_integration._websocket_proxy_instance", proxy):
        resp = client.get("/health/ws_proxy")

    body = resp.get_json()
    assert body["status"] == "degraded"
    assert 60 <= body["last_tick_age_sec"] < 180


# ---------------------------------------------------------------------------
# Port open, very stale tick ≥ 180 s → down
# ---------------------------------------------------------------------------


def test_down_very_stale_tick(client, monkeypatch):
    _open_socket(monkeypatch)

    proxy = MagicMock()
    proxy.last_message_time = {("NIFTY", "NSE_INDEX", "live"): time.time() - 200}
    proxy.subscription_index = {"NIFTY": True}

    with patch("websocket_proxy.app_integration._websocket_proxy_instance", proxy):
        resp = client.get("/health/ws_proxy")

    body = resp.get_json()
    assert body["status"] == "down"
    assert body["last_tick_age_sec"] >= 180


# ---------------------------------------------------------------------------
# Required keys always present
# ---------------------------------------------------------------------------


def test_required_keys_present(client, monkeypatch):
    _closed_socket(monkeypatch)
    body = client.get("/health/ws_proxy").get_json()
    for key in ("status", "last_tick_age_sec", "thread_count", "subscribed_symbols"):
        assert key in body


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
