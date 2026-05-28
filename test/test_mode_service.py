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
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(dim, "engine", test_engine)
    monkeypatch.setattr(dim, "db_session", test_session)
    dim.Base.metadata.create_all(test_engine)

    yield dim

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# resolve_effective_mode rules
# ---------------------------------------------------------------------------


def test_no_intent_resolves_to_disabled(fresh_intent_db):
    """No daily_intent row → resolver refuses to trade."""
    from services.mode_service import EffectiveMode, resolve_effective_mode

    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.DISABLED


def test_skip_intent_returns_skip(fresh_intent_db):
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=False):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SKIP


def test_skip_overrides_analyze(fresh_intent_db):
    """skip is even safer than sandbox — analyze_mode=True must not flip it."""
    from services.mode_service import EffectiveMode, resolve_effective_mode, set_daily_intent

    set_daily_intent("skip", set_by="operator", date_str="2026-05-28")
    with patch("services.mode_service.get_analyze_mode", return_value=True):
        assert resolve_effective_mode("2026-05-28") == EffectiveMode.SKIP


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
