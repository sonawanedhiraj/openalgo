"""Tests for the Stage-1 ``signal_review_service`` + ``signal_decision`` table.

Every test rebinds ``database.signal_decision_db.engine`` and ``db_session`` to
a fresh in-memory SQLite so we never touch ``db/openalgo.db``. The in-process
``invoke_claude_review`` call is mocked so no subprocess / claude CLI ever runs.
"""

import json

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
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

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


def _decision_block(decision: str, reasoning: str = "r", confidence: float = 0.5) -> str:
    """Render a claude-style prose reply ending in a JSON decision block."""
    block = json.dumps({"decision": decision, "reasoning": reasoning, "confidence": confidence})
    return f"Some reasoning prose here.\n\n{block}"


def _mock_claude_review(monkeypatch, model_text: str, session_id: str = "sess-1"):
    """Patch invoke_claude_review to return (model_text, session_id) — no subprocess."""
    import services.signal_review_service as srs

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001 — signature stub
        return model_text, session_id

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


def test_review_signal_returns_take_in_happy_path(fresh_signal_db, shadow_mode, monkeypatch):
    from services.signal_review_service import review_signal

    _mock_claude_review(
        monkeypatch,
        _decision_block("take", "regime aligned", 0.82),
        session_id="sess-1",
    )

    result = review_signal("RELIANCE", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "regime aligned"
    assert result["confidence"] == 0.82
    assert result["enforcement_mode"] == "shadow"
    assert result["id"] is not None
    assert result["cache_hit"] is False


def test_review_signal_writes_signal_decision_row(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_claude_review(
        monkeypatch,
        _decision_block("skip", "vix elevated, breadth negative", 0.74),
        session_id="sess-2",
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
    # bridge_session_id column carries the claude session id.
    assert row["bridge_session_id"] == "sess-2"
    # context_snapshot is JSON-serialised in the row
    snapshot = json.loads(row["context_snapshot"])
    assert snapshot["positions_count"] == 1
    assert snapshot["nifty_pct"] == -0.3


def test_review_signal_includes_direction_in_prompt(fresh_signal_db, shadow_mode, monkeypatch):
    """TATAELXSI fix: the explicit ``direction`` rides the claude prompt AND
    lands on the audit row, so a SELL candidate is never framed as a BUY."""
    import services.signal_review_service as srs
    from database.signal_decision_db import get_signal_decision

    captured: dict = {}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        captured["prompt"] = prompt
        return _decision_block("skip", "bullish regime conflicts with the short", 0.7), "sess-dir"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    result = srs.review_signal(
        "TATAELXSI",
        "chartink_FnO_intraday_buy",
        direction="SELL",
        context=_ctx_override(),
    )

    # 1. Direction is in the prompt sent to claude.
    assert "Direction: SELL" in captured["prompt"]
    assert "chartink_FnO_intraday_buy" in captured["prompt"]
    # 2. Direction is persisted on the audit row.
    row = get_signal_decision(result["id"])
    assert row["direction"] == "SELL"


def test_review_signal_direction_in_cache_key(fresh_signal_db, shadow_mode, monkeypatch):
    """A BUY and a SELL on the same (symbol, source) must NOT collide in the
    cache — direction is part of the key, so each side gets its own review."""
    import services.signal_review_service as srs

    calls: list[str | None] = []

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        direction = "BUY" if "Direction: BUY" in prompt else "SELL"
        calls.append(direction)
        # BUY → take, SELL → skip, so a collision would surface as the wrong one.
        return _decision_block("take" if direction == "BUY" else "skip"), "s"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    buy = srs.review_signal(
        "SBIN", "chartink_FnO_intraday_buy", direction="BUY", context=_ctx_override()
    )
    sell = srs.review_signal(
        "SBIN", "chartink_FnO_intraday_buy", direction="SELL", context=_ctx_override()
    )

    assert buy["decision"] == "take"
    assert sell["decision"] == "skip"
    assert calls == ["BUY", "SELL"]  # both invoked claude; no cross-direction reuse


# ---------------------------------------------------------------------------
# Cache behaviour
# ---------------------------------------------------------------------------


def test_review_signal_cache_hit_skips_invocation(fresh_signal_db, shadow_mode, monkeypatch):
    """Second call within TTL must NOT invoke claude again."""
    from services.signal_review_service import review_signal

    call_count = {"n": 0}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        call_count["n"] += 1
        return _decision_block("take", "fresh", 0.6), "sid"

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    first = review_signal("TCS", "chartink_buy", context=_ctx_override())
    second = review_signal("TCS", "chartink_buy", context=_ctx_override())

    assert call_count["n"] == 1
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert second["decision"] == "take"


def test_review_signal_cache_ttl_zero_disables_caching(fresh_signal_db, shadow_mode, monkeypatch):
    """VETO_CACHE_TTL_SECONDS=0 should mean every call invokes claude."""
    from services.signal_review_service import review_signal

    monkeypatch.setenv("VETO_CACHE_TTL_SECONDS", "0")

    call_count = {"n": 0}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        call_count["n"] += 1
        return _decision_block("take", "fresh", 0.5), "sid"

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    review_signal("HDFC", "chartink_buy", context=_ctx_override())
    review_signal("HDFC", "chartink_buy", context=_ctx_override())

    assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# Fail-safe paths
# ---------------------------------------------------------------------------


def test_review_signal_invoke_error_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    """A raised error from the claude invoker must fail-safe to take."""
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        raise RuntimeError("claude review exited 1: boom")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    result = review_signal("WIPRO", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "claude_error:RuntimeError"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_cli_missing_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        raise FileNotFoundError("claude not on PATH")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    result = review_signal("SBIN", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "claude_cli_missing"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_timeout_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        raise TimeoutError("claude review timed out")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    result = review_signal("AXIS", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "claude_timeout"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_unparseable_output_returns_take(fresh_signal_db, shadow_mode, monkeypatch):
    """No JSON decision block in the model output → fail-safe (parse_failed)."""
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_claude_review(monkeypatch, "I could not reach a conclusion, sorry.")

    result = review_signal("HCLTECH", "chartink_buy", context=_ctx_override())

    assert result["decision"] == "take"
    assert result["reasoning"] == "parse_failed"
    row = get_signal_decision(result["id"])
    assert row["decision"] == "review_failed"


def test_review_signal_returns_garbage_decision(fresh_signal_db, shadow_mode, monkeypatch):
    """Contract violation — decision not in {take, skip} — must fail-safe."""
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import review_signal

    _mock_claude_review(monkeypatch, _decision_block("MAYBE", "x", 0.5))

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


# Mode-aware default (mode-only architecture, 2026-06-12) ---------------------
def test_get_veto_layer_mode_sandbox_defaults_to_active(monkeypatch):
    """No env override + sandbox routing → veto ENFORCES by default."""
    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode("sandbox") == "active"


def test_get_veto_layer_mode_live_defaults_to_shadow(monkeypatch):
    """No env override + live routing → veto observes only (live unchanged)."""
    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode("live") == "shadow"


def test_get_veto_layer_mode_env_overrides_mode_aware_default(monkeypatch):
    """An explicit VETO_LAYER_MODE wins even in sandbox (emergency disable)."""
    monkeypatch.setenv("VETO_LAYER_MODE", "off")
    from services.signal_review_service import get_veto_layer_mode

    assert get_veto_layer_mode("sandbox") == "off"


# ---------------------------------------------------------------------------
# mark_actually_taken
# ---------------------------------------------------------------------------


def test_mark_actually_taken_updates_row(fresh_signal_db, shadow_mode, monkeypatch):
    from database.signal_decision_db import get_signal_decision
    from services.signal_review_service import mark_actually_taken, review_signal

    _mock_claude_review(monkeypatch, _decision_block("take", "ok", 0.7), session_id="sid")

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


def test_fresh_skip_decision_fires_telegram_alert(fresh_signal_db, shadow_mode, monkeypatch):
    """A fresh skip verdict must invoke publish_veto_decision_alert."""
    from services.signal_review_service import review_signal

    _mock_claude_review(
        monkeypatch,
        _decision_block("skip", "vix elevated, breadth negative", 0.74),
        session_id="sess-x",
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _spy)

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

    _mock_claude_review(
        monkeypatch, _decision_block("take", "regime aligned", 0.82), session_id="sess-y"
    )

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _spy)

    review_signal("RELIANCE", "chartink_buy", context=_ctx_override())

    assert len(captured) == 1
    assert captured[0]["decision"] == "take"


def test_cache_hit_does_not_fire_telegram_alert(fresh_signal_db, shadow_mode, monkeypatch):
    """Cache replays must NOT spam the operator with old decisions."""
    from services.signal_review_service import review_signal

    _mock_claude_review(monkeypatch, _decision_block("skip", "fresh skip", 0.6), session_id="sid")

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _spy)

    # First call — fresh, should fire.
    review_signal("TCS", "chartink_buy", context=_ctx_override())
    # Second call — cache hit, MUST NOT fire.
    review_signal("TCS", "chartink_buy", context=_ctx_override())

    assert len(captured) == 1
    assert captured[0]["symbol"] == "TCS"


def test_review_failure_does_not_fire_telegram_alert(fresh_signal_db, shadow_mode, monkeypatch):
    """review_failed rows must not produce a Telegram alert."""
    from services.signal_review_service import review_signal

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        raise RuntimeError("claude exited nonzero")

    import services.signal_review_service as srs

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _spy)

    review_signal("WIPRO", "chartink_buy", context=_ctx_override())

    assert captured == []


def test_bad_decision_does_not_fire_telegram_alert(fresh_signal_db, shadow_mode, monkeypatch):
    """Contract-violation responses must not produce a Telegram alert."""
    from services.signal_review_service import review_signal

    _mock_claude_review(monkeypatch, _decision_block("MAYBE", "x", 0.5))

    captured: list[dict] = []

    def _spy(**kw):
        captured.append(kw)

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _spy)

    review_signal("ITC", "chartink_buy", context=_ctx_override())

    assert captured == []


def test_alert_failure_does_not_break_review(fresh_signal_db, shadow_mode, monkeypatch):
    """A blow-up inside the alert helper must NOT propagate into review_signal."""
    from services.signal_review_service import review_signal

    _mock_claude_review(monkeypatch, _decision_block("skip", "trigger boom", 0.7), session_id="sid")

    def _boom(**kw):
        raise RuntimeError("downstream notification failure")

    monkeypatch.setattr("services.notification_service.publish_veto_decision_alert", _boom)

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
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
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


# ---------------------------------------------------------------------------
# Strategy-aware review (issue #318) — futures_follow_cap50 profile
# ---------------------------------------------------------------------------


FUTURES = "futures_follow_cap50"


def _futures_ctx() -> dict:
    """A complete futures_follow context (operator + signal + market)."""
    return {
        "vol_ratio": 2.1,
        "stock_ret": 0.012,  # fractions, as the sector_follow evaluator emits
        "sector_ret": 0.015,
        "contract_symbol": "NIFTY28JUL26FUT",
        "lots_held": 1,
        "margin_used_inr": 250000.0,
        "margin_cap_inr": 500000.0,
        "pnl_today": 1500.0,
        "kill_switch_active": False,
        "kill_switch_reason": None,
        "nifty_pct": 0.4,
        "india_vix": 13.2,
        "regime_snapshot": None,
    }


def test_futures_follow_prompt_contains_strategy_context(fresh_signal_db, shadow_mode, monkeypatch):
    """The futures prompt carries the strategy thesis + gates + leveraged-beta
    caveat AND both halves of the combined review (stock signal + NIFTY future
    context) — and NONE of the simplified engine's stock-breakout wording."""
    import services.signal_review_service as srs

    captured: dict = {}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        captured["prompt"] = prompt
        return _decision_block("take", "regime fine", 0.8), "sess-fut"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    result = srs.review_signal(
        "RELIANCE", FUTURES, direction="BUY", context=_futures_ctx(), strategy_name=FUTURES
    )

    prompt = captured["prompt"]
    # Strategy-context block: thesis + gates + honest caveat.
    assert "LEVERAGED BROAD-MARKET-BETA" in prompt
    assert "sector index up >1% intraday" in prompt
    assert "hit-rate 53.4" in prompt
    assert "OVERNIGHT-REGIME FIT" in prompt
    # Combined review (locked operator decision): source stock signal…
    assert "Signal stock: RELIANCE" in prompt
    assert "+1.20%" in prompt  # stock_ret 0.012 rendered as percent
    assert "+1.50%" in prompt  # sector_ret
    assert "2.1" in prompt  # vol_ratio
    # …AND the resolved NIFTY-future/book context.
    assert "NIFTY28JUL26FUT" in prompt
    assert "250000.0" in prompt and "500000.0" in prompt  # margin used vs cap
    assert "Kill switch: inactive" in prompt
    # NOT the simplified-engine stock-breakout wording.
    assert "bottom-3 today" not in prompt
    # Guardrail (#318 review): book-state lines are informational only — the LLM
    # must not invent an unbacktested "portfolio prudence" veto on utilization.
    assert "enforced by code before this review" in prompt
    assert "Do NOT skip on capital-utilization" in prompt
    assert "near their daily trade limit" not in prompt
    # Audit row tagged with the strategy's own source.
    from database.signal_decision_db import get_signal_decision

    row = get_signal_decision(result["id"])
    assert row["source"] == FUTURES
    assert row["direction"] == "BUY"


def test_simplified_prompt_unchanged_when_strategy_name_absent(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """Regression: strategy_name=None keeps the simplified-engine template —
    stock-breakout wording present, no futures strategy-context block."""
    import services.signal_review_service as srs

    captured: dict = {}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        captured["prompt"] = prompt
        return _decision_block("take", "ok", 0.7), "sess-se"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    srs.review_signal("RELIANCE", "chartink_buy", direction="BUY", context=_ctx_override())

    prompt = captured["prompt"]
    assert "bottom-3 today" in prompt
    assert "near their daily trade limit" in prompt
    assert "OVERNIGHT-REGIME FIT" not in prompt
    assert "STRATEGY CONTEXT" not in prompt


def test_unregistered_strategy_name_falls_back_to_default_prompt(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """An unknown strategy_name uses the simplified-engine defaults (fail-safe)."""
    import services.signal_review_service as srs

    captured: dict = {}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        captured["prompt"] = prompt
        return _decision_block("take", "ok", 0.7), "s"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

    srs.review_signal(
        "SBIN",
        "some_source",
        direction="BUY",
        context=_ctx_override(),
        strategy_name="not_a_registered_strategy",
    )

    assert "bottom-3 today" in captured["prompt"]
    assert "OVERNIGHT-REGIME FIT" not in captured["prompt"]


def test_build_context_futures_uses_futures_status_not_simplified_engine(monkeypatch):
    """Operator-context dispatch: the futures profile pulls from the
    futures_follow singleton's get_status(), never the simplified engine."""
    from services import signal_review_service as srs

    engine_calls = {"n": 0}

    def _engine_spy():
        engine_calls["n"] += 1
        raise AssertionError("simplified engine must not be consulted for futures_follow")

    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        _engine_spy,
    )

    class FakeFuturesSvc:
        @staticmethod
        def get_status():
            return {
                "lots_held": 2,
                "margin_used_inr": 500000.0,
                "margin_cap_inr": 500000.0,
                "today_pnl_net": -1200.0,
                "kill_switch_active": False,
                "kill_switch_reason": None,
                "mode": "sandbox",
            }

    monkeypatch.setattr("services.futures_follow_service.get_service", lambda: FakeFuturesSvc())
    # Stub the macro fetches so no live quotes are attempted.
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.3)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 12.5)
    monkeypatch.setattr(srs, "_fetch_regime_snapshot", lambda: None)

    ctx = srs._build_context(None, FUTURES)

    assert ctx["lots_held"] == 2
    assert ctx["margin_used_inr"] == 500000.0
    assert ctx["pnl_today"] == -1200.0
    assert ctx["nifty_pct"] == 0.3
    assert ctx["india_vix"] == 12.5
    assert engine_calls["n"] == 0


def test_build_context_futures_degrades_when_service_unavailable(monkeypatch):
    """No futures singleton → operator slots stay None, macros still filled."""
    from services import signal_review_service as srs

    monkeypatch.setattr("services.futures_follow_service.get_service", lambda: None)
    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: -0.9)
    monkeypatch.setattr(srs, "_fetch_india_vix", lambda: 17.8)
    monkeypatch.setattr(srs, "_fetch_regime_snapshot", lambda: None)

    ctx = srs._build_context(None, FUTURES)

    assert ctx["lots_held"] is None
    assert ctx["margin_used_inr"] is None
    assert ctx["nifty_pct"] == -0.9
    assert ctx["india_vix"] == 17.8


def test_build_market_context_helper(monkeypatch):
    """Public helper returns the three macro slots best-effort."""
    from services import signal_review_service as srs

    monkeypatch.setattr(srs, "_fetch_nifty_pct", lambda: 0.7)

    def _boom():
        raise RuntimeError("vix down")

    monkeypatch.setattr(srs, "_fetch_india_vix", _boom)
    monkeypatch.setattr(srs, "_fetch_regime_snapshot", lambda: {"trend": "bullish"})

    ctx = srs.build_market_context()
    assert ctx["nifty_pct"] == 0.7
    assert ctx["india_vix"] is None  # one failure doesn't blank the rest
    assert ctx["regime_snapshot"] == {"trend": "bullish"}


def test_llm_context_loader_falls_back_when_snapshot_missing(monkeypatch, tmp_path):
    from services import signal_review_service as srs

    monkeypatch.setattr(srs, "_FUTURES_FOLLOW_SNAPSHOT_PATH", tmp_path / "missing.json")
    assert srs._load_futures_follow_llm_context() == srs.FUTURES_FOLLOW_LLM_CONTEXT_FALLBACK


def test_llm_context_loader_prefers_snapshot_key(monkeypatch, tmp_path):
    """config_snapshot.json's llm_context key is the single source of truth."""
    from services import signal_review_service as srs

    snap = tmp_path / "config_snapshot.json"
    snap.write_text(json.dumps({"llm_context": "CUSTOM STRATEGY TEXT FROM SNAPSHOT"}))
    monkeypatch.setattr(srs, "_FUTURES_FOLLOW_SNAPSHOT_PATH", snap)
    assert srs._load_futures_follow_llm_context() == "CUSTOM STRATEGY TEXT FROM SNAPSHOT"
    # And it lands in the rendered prompt.
    prompt = srs._format_futures_follow_prompt(
        {"symbol": "X", "source": FUTURES, "direction": "BUY", "candidate_at": "t"},
        _futures_ctx(),
    )
    assert "CUSTOM STRATEGY TEXT FROM SNAPSHOT" in prompt


def test_shipped_snapshot_llm_context_in_sync_with_fallback():
    """The tracked config_snapshot.json llm_context must stay in sync with the
    code fallback (both carry the load-bearing caveat + the veto's job)."""
    from services import signal_review_service as srs

    snapshot_text = srs._load_futures_follow_llm_context()
    for marker in ("LEVERAGED BROAD-MARKET-BETA", "hit-rate 53.4", "OVERNIGHT-REGIME FIT"):
        assert marker in snapshot_text
        assert marker in srs.FUTURES_FOLLOW_LLM_CONTEXT_FALLBACK


# ---------------------------------------------------------------------------
# R1 (#318): exclude_sources filtering in signal_decision_db
# ---------------------------------------------------------------------------


def _seed_row(sdb, symbol, source):
    return sdb.insert_signal_decision(
        symbol=symbol,
        source=source,
        decision="take",
        reasoning="r",
        confidence=0.5,
        enforcement_mode="shadow",
        context_snapshot=None,
        bridge_latency_ms=1,
        bridge_session_id="s",
        raw_bridge_output=None,
    )


def test_exclude_sources_filters_rows_out(fresh_signal_db):
    sdb = fresh_signal_db
    _seed_row(sdb, "ASTRAL", "chartink_FnO_intraday_buy")
    _seed_row(sdb, "FORTIS", "trend-up")
    _seed_row(sdb, "RELIANCE", FUTURES)

    rows = sdb.list_signal_decisions(exclude_sources=[FUTURES])
    assert len(rows) == 2
    assert all(r["source"] != FUTURES for r in rows)
    assert sdb.count_signal_decisions(exclude_sources=[FUTURES]) == 2
    summary = sdb.summarize_signal_decisions(exclude_sources=[FUTURES])
    assert summary["total"] == 2
    assert summary["last_decision"]["source"] != FUTURES

    # Inclusion filter still works and is exact.
    only = sdb.list_signal_decisions(sources=[FUTURES])
    assert len(only) == 1 and only[0]["symbol"] == "RELIANCE"
    assert sdb.count_signal_decisions(sources=[FUTURES]) == 1


def test_exclude_sources_none_returns_everything(fresh_signal_db):
    sdb = fresh_signal_db
    _seed_row(sdb, "A", "trend-up")
    _seed_row(sdb, "B", FUTURES)
    assert sdb.count_signal_decisions() == 2
    assert len(sdb.list_signal_decisions()) == 2


def test_review_signal_forwards_regime_snapshot_into_prompt(
    fresh_signal_db, shadow_mode, monkeypatch
):
    """The prompt sent to claude must render the regime snapshot."""
    import services.signal_review_service as srs
    from services.signal_review_service import review_signal

    captured: dict = {}

    def fake_invoke(prompt, timeout_s):  # noqa: ARG001
        captured["prompt"] = prompt
        return _decision_block("take", "sector aligned", 0.7), "sid"

    monkeypatch.setattr(srs, "invoke_claude_review", fake_invoke)

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

    prompt = captured["prompt"]
    assert "trend: bullish" in prompt
    assert "NIFTYIT" in prompt
