"""Tests for the Stage-1 ``signal_review_service`` + ``signal_decision`` table.

Every test rebinds ``database.signal_decision_db.engine`` and ``db_session`` to
a fresh in-memory SQLite so we never touch ``db/openalgo.db``. The ``httpx``
client is mocked so no network call ever fires.
"""

import json
from unittest.mock import patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_signal_db(monkeypatch):
    """Point signal_decision_db at a fresh in-memory SQLite for one test."""
    from database import signal_decision_db as sdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)
    sdb.Base.metadata.create_all(test_engine)

    yield sdb

    test_session.remove()
    test_engine.dispose()


@pytest.fixture(autouse=True)
def reset_cache():
    """Drop the in-process review cache before AND after every test."""
    from services import signal_review_service as srs

    srs.clear_review_cache()
    yield
    srs.clear_review_cache()


@pytest.fixture
def shadow_mode(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")


def _ctx_override() -> dict:
    """A complete context so _build_context never has to look at the engine."""
    return {
        "positions_count": 1,
        "positions_summary": "1 SHORT CONCOR @ 124.50",
        "pnl_today": 2300.0,
        "trades_today": 2,
        "max_trades_today": 4,
        "nifty_pct": -0.3,
        "india_vix": 14.2,
    }


def _mock_bridge_response(monkeypatch, payload: dict, status_code: int = 200):
    """Patch httpx.post to return a stub response object with the given payload."""
    import services.signal_review_service as srs

    class _StubResponse:
        def __init__(self, payload, status_code):
            self._payload = payload
            self.status_code = status_code

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None):  # noqa: ARG001 — signature stub
        return _StubResponse(payload, status_code)

    monkeypatch.setattr(srs.httpx, "post", fake_post)


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_review_signal_returns_take_in_happy_path(
    fresh_signal_db, shadow_mode, monkeypatch
):
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "take",
            "reasoning": "regime aligned",
            "confidence": 0.82,
            "latency_ms": 1500,
            "claude_session_id": "sess-1",
            "raw_output": "...prose...",
        },
    )

    result = review_signal("RELIANCE", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "regime aligned"
    assert result["confidence"] == 0.82
    assert result["enforcement_mode"] == "shadow"
    assert result["id"] is not None
    assert result["cache_hit"] is False


def test_review_signal_writes_signal_decision_row(
    fresh_signal_db, shadow_mode, monkeypatch
):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "skip",
            "reasoning": "vix elevated, breadth negative",
            "confidence": 0.74,
            "latency_ms": 9100,
            "claude_session_id": "sess-2",
            "raw_output": "prose...{...}",
        },
    )

    result = review_signal("INFY", "chartink_buy", context=_ctx_override())

    row = get_signal_decision(result["id"])
    assert row is not None
    assert row["symbol"] == "INFY"
    assert row["source"] == "chartink_buy"
    assert row["decision"] == "skip"
    assert row["reasoning"] == "vix elevated, breadth negative"
    assert row["confidence"] == 0.74
    assert row["enforcement_mode"] == "shadow"
    assert row["actually_taken"] is None
    assert row["bridge_session_id"] == "sess-2"
    # context_snapshot is JSON-serialised in the row
    snapshot = json.loads(row["context_snapshot"])
    assert snapshot["positions_count"] == 1
    assert snapshot["nifty_pct"] == -0.3


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_review_signal_cache_hit_skips_bridge(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """Second call within TTL must NOT hit the bridge."""
    from services.signal_review_service import review_signal

    call_count = {"n": 0}

    class _StubResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "decision": "take",
                "reasoning": "fresh",
                "confidence": 0.6,
                "latency_ms": 100,
                "claude_session_id": "sid",
                "raw_output": "",
            }

    def fake_post(url, json=None, timeout=None):  # noqa: ARG001
        call_count["n"] += 1
        return _StubResponse()

    import services.signal_review_service as srs

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    first = review_signal("TCS", "chartink_buy", context=_ctx_override())
    second = review_signal("TCS", "chartink_buy", context=_ctx_override())

    assert call_count["n"] == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["decision"] == "take"


def test_review_signal_cache_ttl_zero_disables_caching(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """VETO_CACHE_TTL_SECONDS=0 should mean every call hits the bridge."""
    from services.signal_review_service import review_signal

    monkeypatch.setenv("VETO_CACHE_TTL_SECONDS", "0")

    call_count = {"n": 0}

    class _StubResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "decision": "take",
                "reasoning": "fresh",
                "confidence": 0.5,
                "latency_ms": 10,
                "claude_session_id": "sid",
                "raw_output": "",
            }

    def fake_post(url, json=None, timeout=None):  # noqa: ARG001
        call_count["n"] += 1
        return _StubResponse()

    import services.signal_review_service as srs

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    review_signal("HDFC", "chartink_buy", context=_ctx_override())
    review_signal("HDFC", "chartink_buy", context=_ctx_override())

    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Fail-safe paths
# ---------------------------------------------------------------------------


def test_review_signal_bridge_unreachable_returns_take(
    fresh_signal_db, shadow_mode, monkeypatch
):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    def fake_post(*args, **kwargs):  # noqa: ARG001
        raise httpx.ConnectError("Connection refused")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    result = review_signal("WIPRO", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert "bridge_error" in result["reasoning"]
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_bridge_5xx_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_bridge_response(monkeypatch, {"detail": "boom"}, status_code=503)

    result = review_signal("SBIN", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "bridge_http_503"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_timeout_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    def fake_post(*args, **kwargs):  # noqa: ARG001
        raise httpx.TimeoutException("read timed out")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    result = review_signal("AXIS", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "bridge_timeout"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_bridge_returns_garbage_decision(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """Bridge contract violation — decision not in {take, skip} — must fail-safe."""
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "MAYBE",
            "reasoning": "x",
            "confidence": 0.5,
            "latency_ms": 100,
            "claude_session_id": "sid",
            "raw_output": "",
        },
    )

    result = review_signal("ITC", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert "bad_decision" in result["reasoning"]
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def test_context_builder_handles_engine_failure(fresh_signal_db, shadow_mode, monkeypatch):
    """If _build_context can't reach the engine, it returns a partial dict, not raise."""
    from services import signal_review_service as srs

    # Force the lazy engine import to blow up.
    def broken_import(*args, **kwargs):  # noqa: ARG001
        raise RuntimeError("engine module unreachable")

    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        broken_import,
    )
    # Also force the macro fetches to fail — this test asserts the engine-stat
    # branch fails gracefully and isn't supposed to depend on live broker state.
    def _boom():
        raise RuntimeError("macro fetch unavailable in test")

    monkeypatch.setattr(srs, "_fetch_nifty_pct", _boom)
    monkeypatch.setattr(srs, "_fetch_india_vix", _boom)
    monkeypatch.setattr(srs, "_fetch_pnl_today", _boom)

    ctx = srs._build_context(None)
    # All fields present, all None — reviewer is told nothing rather than blown up.
    assert ctx["positions_count"] is None
    assert ctx["positions_summary"] is None
    assert ctx["nifty_pct"] is None


def test_context_override_is_used_verbatim(fresh_signal_db, shadow_mode, monkeypatch):
    """When the caller passes a context, _build_context returns it unchanged.

    Also confirms the macro fetches are NOT called — operator override is
    authoritative and must short-circuit before any live data fetch.
    """
    from services import signal_review_service as srs

    called: dict[str, int] = {"nifty": 0, "vix": 0, "pnl": 0}

    def _spy_nifty():
        called["nifty"] += 1
        return -0.5

    def _spy_vix():
        called["vix"] += 1
        return 18.0

    def _spy_pnl():
        called["pnl"] += 1
        return 100.0

    monkeypatch.setattr(srs, "_fetch_nifty_pct", _spy_nifty)
    monkeypatch.setattr(srs, "_fetch_india_vix", _spy_vix)
    monkeypatch.setattr(srs, "_fetch_pnl_today", _spy_pnl)

    override = {"positions_count": 99, "pnl_today": -1234.0, "extra_field": "preserved"}
    out = srs._build_context(override)
    assert out == override
    assert called == {"nifty": 0, "vix": 0, "pnl": 0}


# ---------------------------------------------------------------------------
# Macro context fetches (NIFTY %, India VIX, P&L today)
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_engine_stats(monkeypatch):
    """Short-circuit the engine_stats path so macro tests don't need the engine."""
    from services import signal_review_service as srs

    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        lambda: (_ for _ in ()).throw(RuntimeError("engine unavailable for test")),
    )
    return srs


def test_build_context_includes_nifty_pct_when_available(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: -0.3)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 14.2)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 2300.0)

    ctx = srs._build_context(None)
    assert ctx["nifty_pct"] == -0.3


def test_build_context_includes_india_vix_when_available(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.5)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 14.2)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 0.0)

    ctx = srs._build_context(None)
    assert ctx["india_vix"] == 14.2


def test_build_context_includes_pnl_today_when_available(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.1)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 13.0)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 2300.0)

    ctx = srs._build_context(None)
    assert ctx["pnl_today"] == 2300.0


def test_build_context_handles_nifty_fetch_failure(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats

    def _boom():
        raise RuntimeError("quote service down")

    monkeypatch.setattr(srs, "_fetch_nifty_pct", _boom)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 14.2)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 100.0)

    ctx = srs._build_context(None)
    assert ctx["nifty_pct"] is None
    # Other slots stay populated — one failure mustn't blank the rest.
    assert ctx["india_vix"] == 14.2
    assert ctx["pnl_today"] == 100.0


def test_build_context_handles_vix_fetch_failure(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats

    def _boom():
        raise RuntimeError("INDIAVIX symbol not in master contract")

    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: -0.4)
    monkeypatch.setattr(srs, "_fetch_india_vix", _boom)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: -50.0)

    ctx = srs._build_context(None)
    assert ctx["india_vix"] is None
    assert ctx["nifty_pct"] == -0.4
    assert ctx["pnl_today"] == -50.0


def test_build_context_handles_pnl_fetch_failure(
    fresh_signal_db, shadow_mode, stub_engine_stats, monkeypatch
):
    srs = stub_engine_stats

    def _boom():
        raise RuntimeError("positionbook fetch failed")

    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.8)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 12.5)
    monkeypatch.setattr(srs, "_fetch_pnl_today", _boom)

    ctx = srs._build_context(None)
    assert ctx["pnl_today"] is None
    assert ctx["nifty_pct"] == 0.8
    assert ctx["india_vix"] == 12.5


# ---------------------------------------------------------------------------
# Enforcement-mode resolution
# ---------------------------------------------------------------------------


def test_get_veto_layer_mode_defaults_to_shadow(monkeypatch):
    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode() == "shadow"


def test_get_veto_layer_mode_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "panic")
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode() == "shadow"


def test_get_veto_layer_mode_accepts_active(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "ACTIVE")
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode() == "active"


def test_get_veto_layer_mode_accepts_off(monkeypatch):
    monkeypatch.setenv("VETO_LAYER_MODE", "off")
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode() == "off"


# ---------------------------------------------------------------------------
# mark_actually_taken
# ---------------------------------------------------------------------------


def test_mark_actually_taken_updates_row(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import mark_actually_taken, review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "take",
            "reasoning": "ok",
            "confidence": 0.7,
            "latency_ms": 200,
            "claude_session_id": "sid",
            "raw_output": "",
        },
    )

    result = review_signal("HDFCBANK", "chartink_buy", context=_ctx_override())
    assert get_signal_decision(result["id"])["actually_taken"] is None

    mark_actually_taken(result["id"], taken=True)
    assert get_signal_decision(result["id"])["actually_taken"] is True

    mark_actually_taken(result["id"], taken=False)
    assert get_signal_decision(result["id"])["actually_taken"] is False


def test_mark_actually_taken_handles_none_id(fresh_signal_db, shadow_mode):
    from services.signal_review_service import mark_actually_taken

    # Must not raise — the engine passes None when persistence failed.
    mark_actually_taken(None, taken=True)


# ---------------------------------------------------------------------------
# Lazy table self-init (regression: signal_decision write before init_db ran)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Veto-decision Telegram alert wiring
# ---------------------------------------------------------------------------


def test_fresh_skip_decision_fires_telegram_alert(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """A fresh bridge call returning skip must invoke publish_veto_decision_alert."""
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "skip",
            "reasoning": "vix elevated, breadth negative",
            "confidence": 0.74,
            "latency_ms": 9100,
            "claude_session_id": "sess-x",
            "raw_output": "prose...",
        },
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _spy
    )

    review_signal("INFY", "chartink_buy", context=_ctx_override())

    assert len(captured) == 1
    kw = captured[0]
    assert kw["symbol"] == "INFY"
    assert kw["decision"] == "skip"
    assert kw["reasoning"] == "vix elevated, breadth negative"
    assert kw["confidence"] == 0.74
    assert kw["enforcement_mode"] == "shadow"
    assert kw["source"] == "chartink_buy"


def test_fresh_take_decision_calls_helper_but_helper_no_ops(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """A fresh take decision still invokes the helper — which itself no-ops.

    The gate (decision != 'skip' → no broadcast) lives inside
    publish_veto_decision_alert. We assert the wiring is consistent: helper is
    called regardless of the decision, and the helper's own logic decides.
    """
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "take",
            "reasoning": "regime aligned",
            "confidence": 0.82,
            "latency_ms": 1500,
            "claude_session_id": "sess-y",
            "raw_output": "",
        },
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _spy
    )

    review_signal("RELIANCE", "chartink_buy", context=_ctx_override())

    assert len(captured) == 1
    assert captured[0]["decision"] == "take"


def test_cache_hit_does_not_fire_telegram_alert(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """Cache replays must NOT spam the operator with old decisions."""
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "skip",
            "reasoning": "fresh skip",
            "confidence": 0.6,
            "latency_ms": 100,
            "claude_session_id": "sid",
            "raw_output": "",
        },
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _spy
    )

    # First call — fresh, should fire.
    review_signal("TCS", "chartink_buy", context=_ctx_override())
    # Second call — cache hit, MUST NOT fire.
    review_signal("TCS", "chartink_buy", context=_ctx_override())

    assert len(captured) == 1
    assert captured[0]["symbol"] == "TCS"


def test_bridge_failure_does_not_fire_telegram_alert(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """review_failed rows must not produce a Telegram alert."""
    from services.signal_review_service import review_signal

    def fake_post(*args, **kwargs):  # noqa: ARG001
        raise httpx.ConnectError("Connection refused")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _spy
    )

    review_signal("WIPRO", "chartink_buy", context=_ctx_override())

    assert captured == []


def test_bad_decision_does_not_fire_telegram_alert(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """Contract-violation responses must not produce a Telegram alert."""
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "MAYBE",
            "reasoning": "x",
            "confidence": 0.5,
            "latency_ms": 100,
            "claude_session_id": "sid",
            "raw_output": "",
        },
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _spy
    )

    review_signal("ITC", "chartink_buy", context=_ctx_override())

    assert captured == []


def test_alert_failure_does_not_break_review(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """A blow-up inside the alert helper must NOT propagate into review_signal."""
    from services.signal_review_service import review_signal

    _mock_bridge_response(
        monkeypatch,
        {
            "decision": "skip",
            "reasoning": "trigger boom",
            "confidence": 0.7,
            "latency_ms": 100,
            "claude_session_id": "sid",
            "raw_output": "",
        },
    )

    def _boom(**kw):
        raise RuntimeError("downstream notification failure")

    monkeypatch.setattr(
        "services.notification_service.publish_veto_decision_alert", _boom
    )

    # Must NOT raise — review_signal wraps the call in try/except.
    result = review_signal("BOOM", "chartink_buy", context=_ctx_override())
    assert result["decision"] == "skip"


def test_insert_self_inits_table_when_init_db_was_skipped(monkeypatch):
    """If init_db() never ran (background-init race on a fresh process), the
    first insert must still succeed by lazily creating the table.
    """
    from database import signal_decision_db as sdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )
    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)
    # Force the lazy-ensure flag to point at "no engine seen yet" so the test
    # exercises the create_all path even if a prior test bound it to the real
    # engine.
    monkeypatch.setattr(sdb, "_tables_ensured_for_engine", None)
    # Deliberately do NOT call init_db() and do NOT pre-create the table.

    try:
        new_id = sdb.insert_signal_decision(
            symbol="TCS",
            source="trend-up",
            decision="take",
            reasoning="lazy-init regression",
            confidence=0.9,
            enforcement_mode="shadow",
            context_snapshot=None,
            bridge_latency_ms=10,
            bridge_session_id="sid",
            raw_bridge_output="",
        )
        row = sdb.get_signal_decision(new_id)
        assert row is not None
        assert row["symbol"] == "TCS"
        assert row["decision"] == "take"
    finally:
        test_session.remove()
        test_engine.dispose()


# ---------------------------------------------------------------------------
# Stage 1.7 — regime_snapshot in veto context
# ---------------------------------------------------------------------------


def _make_regime_for_context(**overrides):
    """Build a MarketRegime suitable for _fetch_regime_snapshot."""
    from datetime import datetime

    import pytz

    from services.market_regime_service import MarketRegime

    ist = pytz.timezone("Asia/Kolkata")
    base = {
        "timestamp": ist.localize(datetime(2026, 6, 1, 10, 30)),
        "trend": "bullish",
        "volatility": "medium",
        "breadth": "wide",
        "sector_leaders": ["NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA"],
        "sector_leader_concentration": 0.45,
        "time_of_day": "mid_morning",
        "raw_metrics": {
            "sector_rotation": {
                "sector_pct": {
                    "NIFTYIT": 2.5,
                    "NIFTYAUTO": 1.8,
                    "NIFTYPHARMA": 1.2,
                    "BANKNIFTY": 0.1,
                    "FINNIFTY": -0.2,
                    "NIFTYFMCG": -0.4,
                    "NIFTYMETAL": -1.0,
                }
            }
        },
    }
    base.update(overrides)
    return MarketRegime(**base)


def test_fetch_regime_snapshot_returns_compact_dict(monkeypatch):
    from services import signal_review_service as srs

    monkeypatch.setattr(
        "services.market_regime_service.get_cached_regime",
        lambda max_age_minutes=5: _make_regime_for_context(),
    )

    snap = srs._fetch_regime_snapshot()
    assert snap is not None
    assert snap["trend"] == "bullish"
    assert snap["volatility"] == "medium"
    assert snap["breadth"] == "wide"
    assert snap["time_of_day"] == "mid_morning"
    assert snap["sector_leaders"] == ["NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA"]
    assert snap["sector_leader_concentration"] == 0.45
    # top_sector_pct should be trimmed to 5 entries ranked by abs(pct).
    assert len(snap["top_sector_pct"]) == 5
    # NIFTYIT (+2.5) and NIFTYMETAL (-1.0) both make the cut by absolute value.
    assert "NIFTYIT" in snap["top_sector_pct"]
    assert "NIFTYMETAL" in snap["top_sector_pct"]


def test_fetch_regime_snapshot_returns_none_on_classifier_miss(monkeypatch):
    from services import signal_review_service as srs

    monkeypatch.setattr(
        "services.market_regime_service.get_cached_regime",
        lambda max_age_minutes=5: None,
    )
    assert srs._fetch_regime_snapshot() is None


def test_fetch_regime_snapshot_handles_empty_sector_data(monkeypatch):
    """When sector classifier returned [] + 0.0, snapshot still works."""
    from services import signal_review_service as srs

    empty_regime = _make_regime_for_context(
        sector_leaders=[],
        sector_leader_concentration=0.0,
        raw_metrics={"sector_rotation": {}},
    )
    monkeypatch.setattr(
        "services.market_regime_service.get_cached_regime",
        lambda max_age_minutes=5: empty_regime,
    )
    snap = srs._fetch_regime_snapshot()
    assert snap is not None
    assert snap["sector_leaders"] == []
    assert snap["sector_leader_concentration"] == 0.0
    assert snap["top_sector_pct"] == {}


def test_build_context_includes_regime_snapshot(monkeypatch):
    """_build_context (override=None path) must populate regime_snapshot."""
    from services import signal_review_service as srs

    # Stub every other fetch to keep this test focused on regime.
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.5)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 14.0)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 0.0)
    monkeypatch.setattr(srs, "_safe_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "services.market_regime_service.get_cached_regime",
        lambda max_age_minutes=5: _make_regime_for_context(),
    )

    ctx = srs._build_context(None)
    assert "regime_snapshot" in ctx
    assert ctx["regime_snapshot"] is not None
    assert ctx["regime_snapshot"]["trend"] == "bullish"
    assert ctx["regime_snapshot"]["sector_leaders"][0] == "NIFTYIT"


def test_build_context_regime_failure_yields_none(monkeypatch):
    """A failed regime fetch should leave regime_snapshot=None (no crash)."""
    from services import signal_review_service as srs

    def boom():
        raise RuntimeError("regime down")

    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.5)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 14.0)
    monkeypatch.setattr(srs, "_fetch_pnl_today", lambda: 0.0)
    monkeypatch.setattr(srs, "_safe_call", lambda *args, **kwargs: None)
    monkeypatch.setattr(srs, "_fetch_regime_snapshot", boom)

    ctx = srs._build_context(None)
    assert ctx["regime_snapshot"] is None


def test_review_signal_forwards_regime_snapshot_to_bridge(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """The HTTP request body sent to the bridge must include regime_snapshot."""
    import services.signal_review_service as srs
    from services.signal_review_service import review_signal

    captured: dict = {}

    class _StubResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "decision": "take",
                "reasoning": "sector aligned",
                "confidence": 0.7,
                "latency_ms": 200,
                "claude_session_id": "sid",
                "raw_output": "",
            }

    def fake_post(url, json=None, timeout=None):  # noqa: ARG001
        captured["body"] = json
        return _StubResponse()

    monkeypatch.setattr(srs.httpx, "post", fake_post)

    ctx = _ctx_override()
    ctx["regime_snapshot"] = {
        "trend": "bullish",
        "volatility": "medium",
        "breadth": "wide",
        "time_of_day": "mid_morning",
        "sector_leaders": ["NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA"],
        "sector_leader_concentration": 0.45,
        "top_sector_pct": {"NIFTYIT": 2.5, "NIFTYAUTO": 1.8},
    }

    review_signal("RELIANCE", "chartink_buy", context=ctx)

    assert "regime_snapshot" in captured["body"]["context"]
    assert (
        captured["body"]["context"]["regime_snapshot"]["sector_leaders"][0]
        == "NIFTYIT"
    )
