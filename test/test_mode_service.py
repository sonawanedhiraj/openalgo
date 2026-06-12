"""Tests for the Stage-0 mode resolver and the daily_intent table.

Every test rebinds ``database.daily_intent_db.engine`` and ``db_session`` to a
fresh in-memory SQLite database so the test never touches ``db/openalgo.db``.
We mock ``services.mode_service.get_analyze_mode`` rather than redirecting
settings_db too — the resolver only cares about its return value.
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


# ---------------------------------------------------------------------------
# resolve_effective_mode rules
# ---------------------------------------------------------------------------


def test_no_intent_resolves_to_sandbox(fresh_intent_db):
    """Mode-only: no config → SANDBOX default (was DISABLED). External callers
    are never refused for lack of setup — orders route to the virtual book."""
    from services.mode_service import EffectiveMode, resolve_effective_mode

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX


def test_skip_intent_now_resolves_sandbox(fresh_intent_db):
    """'skip' is retired in mode-only — a legacy skip row collapses to SANDBOX,
    never a refusal. An operator wanting to halt entries uses a runtime override."""
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX


def test_skip_resolves_sandbox_regardless_of_analyze(fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX


def test_sandbox_intent_returns_sandbox(fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("sandbox", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX


def test_live_with_analyze_on_returns_sandbox(fresh_intent_db):
    """The bug we're fixing: live intent + analyze on must conservative-down."""
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX


def test_live_with_analyze_off_returns_live(fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.LIVE


# ---------------------------------------------------------------------------
# set_daily_intent semantics
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
# IST default
# ---------------------------------------------------------------------------


def test_resolve_uses_ist_date_by_default(fresh_intent_db):
    """No date_str argument → today in Asia/Kolkata, not UTC, not local."""
    import pytz

    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    today_ist = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    set_daily_intent("sandbox", set_by="operator", date_str=today_ist)

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        # Calling without date_str must hit the same row we just wrote.
        assert resolve_effective_mode() == EffectiveMode.SANDBOX


# ---------------------------------------------------------------------------
# Mode-only canonical resolver (resolve_mode) + shims
# ---------------------------------------------------------------------------


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


def test_resolve_mode_strategy_row_primary(fresh_mode_db):
    from services.mode_service import resolve_mode

    fresh_mode_db.set_mode("simplified_engine", "live", updated_by="op")
    rm = resolve_mode("simplified_engine")
    assert rm.mode == "live"
    assert rm.source == "strategy_mode"


def test_resolve_mode_env_fallthrough(fresh_mode_db, monkeypatch):
    from services.mode_service import resolve_mode

    monkeypatch.setenv("SIMPLIFIED_ENGINE_MODE", "live")
    rm = resolve_mode("simplified_engine")  # no row → env
    assert rm.mode == "live"
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

    fresh_mode_db.set_mode("simplified_engine", "sandbox", updated_by="op")
    d = resolve_strategy_mode("simplified_engine")
    assert d.mode == "sandbox"
    assert d.intent == "run"
    assert d.daily_capital_cap is None
    assert d.source == "strategy_mode"


def test_global_strategy_mode_live_routes_live(fresh_mode_db, fresh_intent_db):
    """An explicit global strategy_mode row drives the external gate live
    (analyze off)."""
    from services.mode_service import GLOBAL_MODE_KEY, EffectiveMode, resolve_effective_mode

    fresh_mode_db.set_mode(GLOBAL_MODE_KEY, "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode() == EffectiveMode.LIVE


def test_global_strategy_mode_live_downgraded_by_analyze(fresh_mode_db, fresh_intent_db):
    from services.mode_service import GLOBAL_MODE_KEY, EffectiveMode, resolve_effective_mode

    fresh_mode_db.set_mode(GLOBAL_MODE_KEY, "live", updated_by="op")
    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_effective_mode() == EffectiveMode.SANDBOX


def test_global_strategy_mode_primary_over_legacy_daily_intent(fresh_mode_db, fresh_intent_db):
    """strategy_mode['__global__']=sandbox wins even if a legacy daily_intent
    says live — the persistent knob is primary."""
    from services.mode_service import (
        GLOBAL_MODE_KEY,
        EffectiveMode,
        resolve_effective_mode,
        set_daily_intent,
    )

    fresh_mode_db.set_mode(GLOBAL_MODE_KEY, "sandbox", updated_by="op")
    set_daily_intent("live", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SANDBOX
