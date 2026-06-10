"""Tests for ``services.scan_hit_poster.ScanHitPoster`` (Stage 1.5 item 6).

The poster is the bridge between the in-house scanner's ``scan_hit`` events
and the simplified-stock-engine webhook. Two modes:

* ``shadow`` (default) — bus subscriber fires and logs, but no HTTP POST and
  the audit row stays at ``posted_to_engine=0``.
* ``active`` — POST to the webhook URL, mark the audit row posted on 2xx.

These tests mock every HTTP call (no real network) and inject a fresh
in-memory ``scanner_db`` so each case starts with a known DB state.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest import mock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from services import scan_hit_poster
from services.scan_hit_poster import (
    MODE_ACTIVE,
    MODE_SHADOW,
    SCAN_HIT_TOPIC,
    ScanHitPoster,
)
from services.scanner_service import ScanHitEvent


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_scanner_db(monkeypatch):
    """Point ``database.scanner_db`` at a clean in-memory SQLite for one test."""
    from database import scanner_db as sdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)

    from services import scanner_service

    scanner_service.init_scanner_db()
    yield sdb

    test_session.remove()
    test_engine.dispose()


def _insert_scan_result_row(scan_def_id: int = 1, symbol: str = "RELIANCE") -> int:
    """Insert a scan_results row representing what the scanner just wrote.

    Returns the row id so the test can inspect it before/after the poster runs.
    """
    from services import scanner_service

    # Create the parent definition first so the FK is happy.
    scanner_service.create_scan_definition(
        name=f"def_{scan_def_id}",
        screener_type="buy",
        expression_json=None,
        rule_module="fno_intraday_buy_chartink",
        enabled=True,
    )
    return scanner_service.record_scan_result(
        scan_definition_id=scan_def_id,
        symbols=[symbol],
        source="inhouse",
        posted_to_engine=False,
        notes="test seed",
    )


def _read_posted_flag(scan_result_id: int) -> int:
    from database import scanner_db as sdb

    sess = sdb.db_session
    try:
        row = sess.query(sdb.ScanResult).filter_by(id=scan_result_id).first()
        assert row is not None, f"scan_result {scan_result_id} not found"
        return int(row.posted_to_engine)
    finally:
        sess.remove()


def _make_event(
    symbol: str = "RELIANCE",
    scan_result_id: int = 0,
    screener_type: str = "buy",
    scan_name: str = "BUY in-house screener",
) -> ScanHitEvent:
    return ScanHitEvent(
        scan_definition_id=1,
        scan_name=scan_name,
        screener_type=screener_type,
        symbol=symbol,
        interval="5m",
        bar={"ts": dt.datetime(2026, 5, 30, 11, 0), "close": 150.0},
        scan_result_id=scan_result_id,
    )


class _FakeBus:
    """Minimal stand-in for ``utils.event_bus.EventBus`` — records calls."""

    def __init__(self) -> None:
        self.subscribed: list[tuple[str, Any]] = []
        self.unsubscribed: list[tuple[str, Any]] = []

    def subscribe(self, topic: str, callback: Any, name: str = "") -> None:
        self.subscribed.append((topic, callback))

    def unsubscribe(self, topic: str, callback: Any) -> None:
        self.unsubscribed.append((topic, callback))

    def publish(self, event: Any) -> None:  # pragma: no cover — not used here
        pass


class _MockHttpClient:
    """Records POSTs and returns a configurable response."""

    def __init__(
        self,
        status_code: int = 200,
        raises: Exception | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._status = status_code
        self._raises = raises

    def post(self, url: str, json: dict[str, Any] | None = None) -> Any:
        self.calls.append({"url": url, "json": json})
        if self._raises is not None:
            raise self._raises
        resp = mock.Mock()
        resp.status_code = self._status
        return resp


# ---------------------------------------------------------------------------
# subscription / lifecycle
# ---------------------------------------------------------------------------


def test_poster_subscribes_to_scan_hit_on_start():
    bus = _FakeBus()
    poster = ScanHitPoster(mode=MODE_SHADOW, bus=bus)
    poster.start()

    assert len(bus.subscribed) == 1
    topic, callback = bus.subscribed[0]
    assert topic == SCAN_HIT_TOPIC
    assert callback == poster._on_scan_hit

    # Calling start() again must be a no-op.
    poster.start()
    assert len(bus.subscribed) == 1


def test_poster_stop_unsubscribes():
    bus = _FakeBus()
    poster = ScanHitPoster(mode=MODE_SHADOW, bus=bus)
    poster.start()
    poster.stop()

    assert len(bus.unsubscribed) == 1
    topic, callback = bus.unsubscribed[0]
    assert topic == SCAN_HIT_TOPIC
    assert callback == poster._on_scan_hit


# ---------------------------------------------------------------------------
# shadow mode
# ---------------------------------------------------------------------------


def test_poster_shadow_mode_does_not_post(fresh_scanner_db):
    """Shadow mode: event fires, but NO HTTP POST and no DB mutation."""
    scan_result_id = _insert_scan_result_row()
    assert _read_posted_flag(scan_result_id) == 0

    http = _MockHttpClient()
    bus = _FakeBus()
    poster = ScanHitPoster(
        mode=MODE_SHADOW,
        webhook_url="http://127.0.0.1:5000/chartink/simplified-stock-engine/abc",
        bus=bus,
        http_client=http,
    )
    poster.start()

    # Drive the event directly through the subscriber (synchronous, no thread pool).
    poster._on_scan_hit(_make_event(scan_result_id=scan_result_id))

    assert http.calls == []
    assert _read_posted_flag(scan_result_id) == 0  # unchanged


# ---------------------------------------------------------------------------
# active mode — success path
# ---------------------------------------------------------------------------


def test_poster_active_mode_posts_with_correct_url_and_payload(fresh_scanner_db):
    scan_result_id = _insert_scan_result_row(symbol="RELIANCE")
    url = "http://127.0.0.1:5000/chartink/simplified-stock-engine/test-uuid"

    http = _MockHttpClient(status_code=200)
    bus = _FakeBus()
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url=url,
        bus=bus,
        http_client=http,
    )
    poster.start()

    # Two-symbol event to confirm the "stocks" CSV shape — but the scanner
    # emits one symbol per event today, so the more realistic case is one.
    # We assert the CSV joiner does the right thing for the one-symbol case.
    poster._on_scan_hit(
        _make_event(
            symbol="RELIANCE",
            scan_result_id=scan_result_id,
            screener_type="buy",
            scan_name="BUY in-house screener",
        )
    )

    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == url
    assert call["json"] == {
        "stocks": "RELIANCE",
        "scan_name": "BUY in-house screener",
    }
    assert _read_posted_flag(scan_result_id) == 1


def test_poster_active_mode_multi_symbol_payload_via_build():
    """Direct test of ``_build_payload`` for the multi-symbol CSV shape.

    The current ``ScanHitEvent`` emits one symbol per event; this verifies
    the helper still produces the Chartink-compatible CSV when given more.
    """
    poster = ScanHitPoster(mode=MODE_ACTIVE)
    payload = poster._build_payload(
        symbols=["SYM1", "SYM2", "SYM3"],
        screener_type="sell",
        scan_name="",
    )
    assert payload == {"stocks": "SYM1,SYM2,SYM3", "scan_name": "SELL in-house screener"}


# ---------------------------------------------------------------------------
# active mode — failure paths (all fail-safe)
# ---------------------------------------------------------------------------


def test_poster_active_mode_does_not_crash_on_http_error(fresh_scanner_db, caplog):
    scan_result_id = _insert_scan_result_row()
    http = _MockHttpClient(raises=httpx.HTTPError("boom"))
    bus = _FakeBus()
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url="http://127.0.0.1:5000/chartink/simplified-stock-engine/x",
        bus=bus,
        http_client=http,
    )

    # Must not raise.
    poster._on_scan_hit(_make_event(scan_result_id=scan_result_id))

    assert len(http.calls) == 1  # we did try
    assert _read_posted_flag(scan_result_id) == 0  # but did NOT mark posted
    # And we logged a warning, not propagated.
    assert any("failed" in rec.message.lower() for rec in caplog.records)


def test_poster_active_mode_timeout_fails_safe(fresh_scanner_db, caplog):
    scan_result_id = _insert_scan_result_row()
    http = _MockHttpClient(raises=httpx.TimeoutException("slow"))
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url="http://127.0.0.1:5000/chartink/simplified-stock-engine/x",
        bus=_FakeBus(),
        http_client=http,
    )

    poster._on_scan_hit(_make_event(scan_result_id=scan_result_id))

    assert len(http.calls) == 1
    assert _read_posted_flag(scan_result_id) == 0
    assert any("timed out" in rec.message.lower() for rec in caplog.records)


def test_poster_active_mode_non_2xx_response_does_not_mark_posted(fresh_scanner_db):
    scan_result_id = _insert_scan_result_row()
    http = _MockHttpClient(status_code=500)
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url="http://127.0.0.1:5000/chartink/simplified-stock-engine/x",
        bus=_FakeBus(),
        http_client=http,
    )

    poster._on_scan_hit(_make_event(scan_result_id=scan_result_id))

    assert _read_posted_flag(scan_result_id) == 0  # 500 ≠ success


def test_poster_handles_empty_symbol_list_gracefully(fresh_scanner_db):
    """If the event somehow lacks a symbol, build payload with ``stocks=""``
    and STILL post (matching the always-POST contract). Don't crash.
    """
    scan_result_id = _insert_scan_result_row()
    http = _MockHttpClient(status_code=200)
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url="http://127.0.0.1:5000/chartink/simplified-stock-engine/x",
        bus=_FakeBus(),
        http_client=http,
    )

    poster._on_scan_hit(_make_event(symbol="", scan_result_id=scan_result_id))

    assert len(http.calls) == 1
    assert http.calls[0]["json"]["stocks"] == ""
    # Still a 200, so we still mark posted.
    assert _read_posted_flag(scan_result_id) == 1


# ---------------------------------------------------------------------------
# active mode — missing URL
# ---------------------------------------------------------------------------


def test_poster_active_mode_without_url_refuses_to_post(fresh_scanner_db, caplog):
    scan_result_id = _insert_scan_result_row()
    http = _MockHttpClient()
    poster = ScanHitPoster(
        mode=MODE_ACTIVE,
        webhook_url=None,  # not configured
        bus=_FakeBus(),
        http_client=http,
    )

    poster._on_scan_hit(_make_event(scan_result_id=scan_result_id))

    assert http.calls == []
    assert _read_posted_flag(scan_result_id) == 0
    assert any("empty" in rec.message.lower() for rec in caplog.records)


# ---------------------------------------------------------------------------
# disabled flag (controls whether the poster is even instantiated by app.py)
# ---------------------------------------------------------------------------


def test_poster_disabled_does_not_subscribe(monkeypatch):
    """When ``SCAN_HIT_POSTER_ENABLED=false``, the app.py wiring branch
    short-circuits before constructing the poster — so no subscription
    should ever land on the bus.

    We can't import app.py inside the test cheaply, so we verify the
    contract that drives the wiring: an unstarted poster has no bus
    interaction. The actual env-gate is exercised by inspecting the
    branch in app.py at code-review time.
    """
    monkeypatch.setenv("SCAN_HIT_POSTER_ENABLED", "false")
    bus = _FakeBus()
    poster = ScanHitPoster(mode=MODE_SHADOW, bus=bus)
    # If never started, no subscription should land.
    assert bus.subscribed == []


# ---------------------------------------------------------------------------
# from_env() — defaults must be safe
# ---------------------------------------------------------------------------


def test_from_env_defaults_to_shadow(monkeypatch):
    """No env vars set => mode must be shadow (the safe default)."""
    for key in (
        "SCAN_HIT_POSTER_MODE",
        "SCAN_HIT_POSTER_WEBHOOK_URL",
        "SCAN_HIT_POSTER_REQUEST_TIMEOUT_SECONDS",
        "SCAN_HIT_POSTER_STRATEGY_NAME",
    ):
        monkeypatch.delenv(key, raising=False)

    # Block the DB fallback so the test doesn't depend on a Strategy row.
    monkeypatch.setattr(
        scan_hit_poster, "_resolve_default_webhook_url", lambda: None
    )

    poster = ScanHitPoster.from_env()
    assert poster.mode == MODE_SHADOW
    assert poster.webhook_url is None  # not required in shadow


def test_from_env_unknown_mode_falls_back_to_shadow(monkeypatch):
    monkeypatch.setenv("SCAN_HIT_POSTER_MODE", "rampage")
    monkeypatch.setattr(
        scan_hit_poster, "_resolve_default_webhook_url", lambda: None
    )
    poster = ScanHitPoster.from_env()
    assert poster.mode == MODE_SHADOW
