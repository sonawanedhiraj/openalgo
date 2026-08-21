"""Tests for the mode resolvers (UI-driven per-strategy dispatch, issue #440).

Every test rebinds ``database.daily_intent_db`` / ``database.strategy_mode_db``
to a fresh in-memory SQLite database so the test never touches
``db/openalgo.db``. We mock ``services.mode_service.get_analyze_mode`` rather
than redirecting settings_db too — the resolvers only care about its value.

Contract under test:

* ``resolve_order_mode(mode_key)`` — the ONLY gate for exposure-creating
  orders. LIVE requires Analyze OFF **and** a ``strategy_mode`` row = live for
  the key. No row / no key / env-only → SANDBOX (default deny). The retired
  hidden ``__global__`` row and legacy ``daily_intent`` have NO effect.
* ``resolve_effective_mode()`` — the platform analyze overlay for read paths
  and order management: SANDBOX iff Analyze is ON, else LIVE.
* ``resolve_mode`` — env values can never escalate to live (capped).
"""

import datetime as dt
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_intent_db(monkeypatch):
    """Point daily_intent_db at a fresh in-memory SQLite for one test."""
    from database import daily_intent_db as dim

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))

    monkeypatch.setattr(dim, "engine", test_engine)
    monkeypatch.setattr(dim, "db_session", test_session)
    dim.Base.metadata.create_all(test_engine)

    yield dim

    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def fresh_mode_db(monkeypatch):
    """Point strategy_mode_db at a fresh in-memory SQLite for one test."""
    from database import strategy_mode_db as sm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sm, "engine", eng)
    monkeypatch.setattr(sm, "db_session", sess)
    sm.Base.query = sess.query_property()
    sm.Base.metadata.create_all(eng)
    yield sm
    sess.remove()
    eng.dispose()


# ---------------------------------------------------------------------------
# resolve_order_mode — the per-strategy dispatch gate (issue #440)
# ---------------------------------------------------------------------------


def test_order_mode_live_row_and_analyze_off_routes_live(fresh_mode_db):
    from services.mode_service import EffectiveMode, resolve_order_mode

    fresh_mode_db._set_mode_unchecked("open15_vol_breakout", "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("open15_vol_breakout") == EffectiveMode.LIVE


def test_order_mode_analyze_on_overrides_live_row(fresh_mode_db):
    """Analyze mode is the platform kill switch — a live row cannot beat it."""
    from services.mode_service import EffectiveMode, resolve_order_mode

    fresh_mode_db._set_mode_unchecked("open15_vol_breakout", "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_order_mode("open15_vol_breakout") == EffectiveMode.SANDBOX


def test_order_mode_sandbox_row_protects_strategy(fresh_mode_db):
    """A sandbox-flagged strategy stays sandbox even with Analyze off and other
    strategies live — the futures_follow protection at the heart of #440."""
    from services.mode_service import EffectiveMode, resolve_order_mode

    fresh_mode_db._set_mode_unchecked("open15_vol_breakout", "live", updated_by="op")
    fresh_mode_db._set_mode_unchecked("futures_follow_cap50", "sandbox", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("futures_follow_cap50") == EffectiveMode.SANDBOX
        assert resolve_order_mode("open15_vol_breakout") == EffectiveMode.LIVE


def test_order_mode_no_row_default_denies(fresh_mode_db):
    """Unregistered strategy label → sandbox, even with Analyze off."""
    from services.mode_service import EffectiveMode, resolve_order_mode

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("some_external_tv_strategy") == EffectiveMode.SANDBOX


def test_order_mode_missing_key_default_denies(fresh_mode_db):
    from services.mode_service import EffectiveMode, resolve_order_mode

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode(None) == EffectiveMode.SANDBOX
        assert resolve_order_mode("") == EffectiveMode.SANDBOX


def test_order_mode_env_live_cannot_escalate(fresh_mode_db, monkeypatch):
    """An env mode flag is a hidden non-UI control — 'live' in env is capped."""
    from services.mode_service import EffectiveMode, resolve_order_mode

    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "live")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("simplified_engine") == EffectiveMode.SANDBOX


def test_order_mode_retired_global_row_has_no_effect(fresh_mode_db):
    """A manually re-created __global__ row must not open any gate (#440)."""
    from services.mode_service import EffectiveMode, resolve_order_mode

    fresh_mode_db._set_mode_unchecked("__global__", "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("some_external_tv_strategy") == EffectiveMode.SANDBOX
        assert resolve_order_mode(None) == EffectiveMode.SANDBOX


def test_order_mode_legacy_daily_intent_has_no_effect(fresh_mode_db, fresh_intent_db):
    """The legacy daily_intent table no longer participates in dispatch."""
    from services.mode_service import EffectiveMode, resolve_order_mode, set_daily_intent

    set_daily_intent("live", set_by="operator")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_order_mode("anything") == EffectiveMode.SANDBOX


def test_order_mode_unreadable_analyze_flag_fails_safe(fresh_mode_db):
    from services.mode_service import EffectiveMode, resolve_order_mode

    fresh_mode_db._set_mode_unchecked("open15_vol_breakout", "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", side_effect=RuntimeError("db down")):
        assert resolve_order_mode("open15_vol_breakout") == EffectiveMode.SANDBOX


# ---------------------------------------------------------------------------
# resolve_effective_mode — analyze overlay only (reads + order management)
# ---------------------------------------------------------------------------


def test_effective_mode_analyze_on_is_sandbox(fresh_mode_db, fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode

    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_effective_mode() == EffectiveMode.SANDBOX


def test_effective_mode_analyze_off_is_live(fresh_mode_db, fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode() == EffectiveMode.LIVE


def test_effective_mode_ignores_legacy_daily_intent(fresh_mode_db, fresh_intent_db):
    """A legacy daily_intent row (any value) no longer drives the overlay."""
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.LIVE


def test_effective_mode_unreadable_analyze_flag_fails_safe(fresh_mode_db, fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode

    with patch("services.mode_service.get_analyze_mode", side_effect=RuntimeError("db down")):
        assert resolve_effective_mode() == EffectiveMode.SANDBOX


# ---------------------------------------------------------------------------
# set_daily_intent semantics (legacy table retained for observability only)
# ---------------------------------------------------------------------------


def test_set_daily_intent_creates_row(fresh_intent_db):
    from services.mode_service import get_daily_intent, set_daily_intent

    result = set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28", notes="boot")

    assert result["status"] == "ok"
    assert result["row"]["intent"] == "sandbox"
    assert result["row"]["set_by"] == "operator"
    assert result["row"]["locked"] is False

    fetched = get_daily_intent("2026-05-28")
    assert fetched is not None
    assert fetched["intent"] == "sandbox"
    assert fetched["notes"] == "boot"


def test_set_daily_intent_updates_existing(fresh_intent_db):
    """Idempotent on same date: second call overwrites the first."""
    from services.mode_service import get_daily_intent, set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    set_daily_intent("live", set_by="agent", date_str="2026-05-28", notes="flipped")

    row = get_daily_intent("2026-05-28")
    assert row["intent"] == "live"
    assert row["set_by"] == "agent"
    assert row["notes"] == "flipped"


def test_set_daily_intent_respects_lock(fresh_intent_db):
    """Once locked, further writes return status='locked' without changing the row."""
    from services.mode_service import get_daily_intent, set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28", locked=True)
    result = set_daily_intent("skip", set_by="agent", date_str="2026-05-28")

    assert result["status"] == "locked"
    row = get_daily_intent("2026-05-28")
    assert row["intent"] == "live"
    assert row["locked"] is True


def test_set_daily_intent_rejects_bad_value(fresh_intent_db):
    from services.mode_service import set_daily_intent

    with pytest.raises(ValueError):
        set_daily_intent("yolo", set_by="operator", date_str="2026-05-28")


# ---------------------------------------------------------------------------
# Mode-only canonical resolver (resolve_mode) + shims
# ---------------------------------------------------------------------------


def test_resolve_mode_strategy_row_primary(fresh_mode_db):
    from services.mode_service import resolve_mode

    fresh_mode_db._set_mode_unchecked("simplified_engine", "live", updated_by="op")
    rm = resolve_mode("simplified_engine")
    assert rm.mode == "live"
    assert rm.source == "strategy_mode"


def test_resolve_mode_env_live_capped_to_sandbox(fresh_mode_db, monkeypatch):
    """Issue #440: env can never escalate to live — only the UI-written row."""
    from services.mode_service import resolve_mode

    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "live")
    rm = resolve_mode("simplified_engine")  # no row → env, capped
    assert rm.mode == "sandbox"
    assert rm.source == "env"


def test_resolve_mode_scaffold_env_collapses_to_sandbox(fresh_mode_db, monkeypatch):
    from services.mode_service import resolve_mode

    monkeypatch.setenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "scaffold")
    rm = resolve_mode("sector_follow_cap5_vol")
    assert rm.mode == "sandbox"  # 'scaffold' (no-orders) maps to the safe sandbox
    assert rm.source == "env"


def test_resolve_mode_default_sandbox(fresh_mode_db, monkeypatch):
    from services.mode_service import resolve_mode

    monkeypatch.delenv("SIMPLIFIED_ENGINE_MODE", raising=False)
    rm = resolve_mode("some_unknown_strategy")
    assert rm.mode == "sandbox"
    assert rm.source == "default"


def test_resolve_strategy_mode_shim_intent_always_run(fresh_mode_db):
    """The deprecated shim always reports intent='run' — the intent axis is gone."""
    from services.mode_service import resolve_strategy_mode

    fresh_mode_db._set_mode_unchecked("simplified_engine", "sandbox", updated_by="op")
    d = resolve_strategy_mode("simplified_engine")
    assert d.mode == "sandbox"
    assert d.intent == "run"
    assert d.daily_capital_cap is None
    assert d.source == "strategy_mode"


# ---------------------------------------------------------------------------
# Boot-time purge of the retired __global__ row
# ---------------------------------------------------------------------------


def test_purge_retired_global_row(fresh_mode_db):
    from database.strategy_mode_db import purge_retired_global_row

    fresh_mode_db._set_mode_unchecked("__global__", "live", updated_by="op")
    fresh_mode_db._set_mode_unchecked("open15_vol_breakout", "live", updated_by="op")

    purge_retired_global_row()

    names = {r["strategy_name"] for r in fresh_mode_db.list_modes()}
    assert "__global__" not in names
    assert "open15_vol_breakout" in names  # untouched


def test_purge_retired_global_row_noop_when_absent(fresh_mode_db):
    from database.strategy_mode_db import purge_retired_global_row

    purge_retired_global_row()  # must not raise on an empty table
    assert fresh_mode_db.list_modes() == []
