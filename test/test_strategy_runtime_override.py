"""Tests for the ephemeral, self-expiring strategy_runtime_override table.

Rebinds ``database.strategy_runtime_override_db`` to an in-memory SQLite per
test. Exercises upsert, lazy expiry, entry-block semantics, manual clear, and
housekeeping.
"""

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_sro(monkeypatch):
    from database import strategy_runtime_override_db as sro

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sro, "engine", eng)
    monkeypatch.setattr(sro, "db_session", sess)
    sro.Base.query = sess.query_property()
    sro.Base.metadata.create_all(eng)

    yield sro

    sess.remove()
    eng.dispose()


_NOW = dt.datetime(2026, 6, 12, 4, 0, 0)  # fixed "now" in UTC for determinism
_FUTURE = _NOW + dt.timedelta(hours=2)
_PAST = _NOW - dt.timedelta(hours=2)


def test_init_idempotent(fresh_sro):
    fresh_sro.init_db()
    fresh_sro.init_db()


def test_set_and_active_read(fresh_sro):
    fresh_sro.set_override(
        "sector_follow_cap5_vol", "pause", _FUTURE, reason="stale_feed:NIFTY", set_by="data_health"
    )
    active = fresh_sro.get_active_overrides("sector_follow_cap5_vol", now=_NOW)
    assert len(active) == 1
    assert active[0]["override_type"] == "pause"
    assert active[0]["reason"] == "stale_feed:NIFTY"


def test_entry_blocked_when_active(fresh_sro):
    fresh_sro.set_override("simplified_engine", "kill_switch", _FUTURE, set_by="risk")
    blocked, ov = fresh_sro.is_entry_blocked("simplified_engine", now=_NOW)
    assert blocked is True
    assert ov["override_type"] == "kill_switch"


def test_expired_override_is_inert(fresh_sro):
    """A row whose expires_at has passed never blocks (lazy expiry)."""
    fresh_sro.set_override("simplified_engine", "pause", _PAST, set_by="data_health")
    assert fresh_sro.get_active_overrides("simplified_engine", now=_NOW) == []
    blocked, ov = fresh_sro.is_entry_blocked("simplified_engine", now=_NOW)
    assert blocked is False
    assert ov is None


def test_upsert_replaces_same_type(fresh_sro):
    fresh_sro.set_override("s", "pause", _NOW + dt.timedelta(hours=1), reason="a", set_by="x")
    fresh_sro.set_override("s", "pause", _FUTURE, reason="b", set_by="y")
    active = fresh_sro.get_active_overrides("s", now=_NOW)
    assert len(active) == 1  # not duplicated
    assert active[0]["reason"] == "b"
    assert active[0]["set_by"] == "y"


def test_two_distinct_types_coexist(fresh_sro):
    fresh_sro.set_override("s", "pause", _FUTURE, set_by="x")
    fresh_sro.set_override("s", "kill_switch", _FUTURE, set_by="y")
    assert len(fresh_sro.get_active_overrides("s", now=_NOW)) == 2


def test_clear_specific_and_all(fresh_sro):
    fresh_sro.set_override("s", "pause", _FUTURE, set_by="x")
    fresh_sro.set_override("s", "kill_switch", _FUTURE, set_by="y")
    assert fresh_sro.clear_override("s", "pause") == 1
    assert {o["override_type"] for o in fresh_sro.get_active_overrides("s", now=_NOW)} == {
        "kill_switch"
    }
    assert fresh_sro.clear_override("s") == 1  # clears the rest
    assert fresh_sro.get_active_overrides("s", now=_NOW) == []


def test_clear_expired_housekeeping(fresh_sro):
    fresh_sro.set_override("s", "pause", _PAST, set_by="x")
    fresh_sro.set_override("s", "kill_switch", _FUTURE, set_by="y")
    removed = fresh_sro.clear_expired(now=_NOW)
    assert removed == 1
    assert len(fresh_sro.list_overrides(now=_NOW)) == 1


def test_set_rejects_bad_type(fresh_sro):
    with pytest.raises(ValueError):
        fresh_sro.set_override("s", "halt", _FUTURE, set_by="x")  # 'halt' not allowed


def test_set_rejects_non_datetime_expiry(fresh_sro):
    with pytest.raises(ValueError):
        fresh_sro.set_override("s", "pause", "2026-06-12", set_by="x")
