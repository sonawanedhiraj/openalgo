"""Tests for the mode-only strategy_mode table + the daily_intent→mode migration.

Each test rebinds ``database.strategy_mode_db`` (and, for the migration test,
``database.strategy_daily_intent_db``) to a shared in-memory SQLite so nothing
touches ``db/openalgo.db``. Mirrors the rebinding pattern in
test_strategy_daily_intent / test_mode_service.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_sm(monkeypatch):
    """Point strategy_mode_db at one in-memory SQLite engine for a single test."""
    from database import strategy_mode_db as sm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(sm, "engine", eng)
    monkeypatch.setattr(sm, "db_session", sess)
    sm.Base.query = sess.query_property()
    sm.Base.metadata.create_all(eng)

    yield sm, sess

    sess.remove()
    eng.dispose()


@pytest.fixture
def fresh_both(monkeypatch):
    """Both strategy_mode and strategy_daily_intent on ONE shared engine, so the
    migration (reads sdi, writes sm) sees both tables."""
    from database import strategy_daily_intent_db as sdi
    from database import strategy_mode_db as sm

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))

    for mod in (sm, sdi):
        monkeypatch.setattr(mod, "engine", eng)
        monkeypatch.setattr(mod, "db_session", sess)
        mod.Base.query = sess.query_property()
        mod.Base.metadata.create_all(eng)

    yield sm, sdi, sess

    sess.remove()
    eng.dispose()


# --------------------------------------------------------------------------- #
# Table round-trip + validation
# --------------------------------------------------------------------------- #
def test_init_is_idempotent(fresh_sm):
    sm, _ = fresh_sm
    sm.init_db()
    sm.init_db()


def test_get_returns_none_when_no_row(fresh_sm):
    sm, _ = fresh_sm
    assert sm.get_mode("simplified_engine") is None


def test_set_get_roundtrip(fresh_sm):
    sm, _ = fresh_sm
    sm._set_mode_unchecked("simplified_engine", "live", updated_by="cli", notes="armed")
    row = sm.get_mode("simplified_engine")
    assert row["mode"] == "live"
    assert row["updated_by"] == "cli"
    assert row["notes"] == "armed"


def test_set_updates_existing(fresh_sm):
    sm, _ = fresh_sm
    sm._set_mode_unchecked("simplified_engine", "live", updated_by="a")
    sm._set_mode_unchecked("simplified_engine", "sandbox", updated_by="b")
    row = sm.get_mode("simplified_engine")
    assert row["mode"] == "sandbox"
    assert row["updated_by"] == "b"


def test_set_rejects_invalid_mode(fresh_sm):
    sm, _ = fresh_sm
    with pytest.raises(ValueError):
        sm._set_mode_unchecked("x", "skip", updated_by="cli")  # 'skip' is not a valid mode here
    with pytest.raises(ValueError):
        sm._set_mode_unchecked("x", "yolo", updated_by="cli")


def test_set_requires_updated_by(fresh_sm):
    sm, _ = fresh_sm
    with pytest.raises(ValueError):
        sm._set_mode_unchecked("x", "sandbox", updated_by="")


def test_list_and_delete(fresh_sm):
    sm, _ = fresh_sm
    sm._set_mode_unchecked("a", "sandbox", updated_by="cli")
    sm._set_mode_unchecked("b", "live", updated_by="cli")
    names = {r["strategy_name"] for r in sm.list_modes()}
    assert names == {"a", "b"}
    assert sm.delete_mode("a") is True
    assert sm.get_mode("a") is None
    assert sm.delete_mode("a") is False  # already gone


# --------------------------------------------------------------------------- #
# Migration: strategy_daily_intent → strategy_mode
# --------------------------------------------------------------------------- #
def test_migration_takes_latest_per_strategy_and_drops_intent(fresh_both):
    sm, sdi, _ = fresh_both
    # Two days for the same strategy — the LATER date's mode wins.
    sdi.set_intent("simplified_engine", "2026-06-10", "sandbox", "run", updated_by="op")
    sdi.set_intent("simplified_engine", "2026-06-11", "live", "halt", updated_by="op")
    # A second strategy with a single row.
    sdi.set_intent("sector_follow_cap5_vol", "2026-06-11", "sandbox", "pause", updated_by="op")

    from scripts.migrate_strategy_daily_intent_to_strategy_mode import migrate

    result = migrate()
    assert result["inserted"] == 2

    se = sm.get_mode("simplified_engine")
    assert se["mode"] == "live"  # latest date (06-11) wins; intent='halt' dropped
    assert se["updated_by"] == "migration"
    sf = sm.get_mode("sector_follow_cap5_vol")
    assert sf["mode"] == "sandbox"  # intent='pause' dropped


def test_migration_maps_skip_to_sandbox(fresh_both):
    sm, sdi, _ = fresh_both
    sdi.set_intent("simplified_engine", "2026-06-11", "skip", "run", updated_by="op")

    from scripts.migrate_strategy_daily_intent_to_strategy_mode import migrate

    migrate()
    # 'skip' is not a valid strategy_mode value → collapses to the safe sandbox.
    assert sm.get_mode("simplified_engine")["mode"] == "sandbox"


def test_migration_is_idempotent_and_preserves_operator_edits(fresh_both):
    sm, sdi, _ = fresh_both
    sdi.set_intent("simplified_engine", "2026-06-11", "live", "run", updated_by="op")

    from scripts.migrate_strategy_daily_intent_to_strategy_mode import migrate

    assert migrate()["inserted"] == 1
    # Operator overrides the migrated row to sandbox.
    sm._set_mode_unchecked("simplified_engine", "sandbox", updated_by="operator")
    # Re-running the migration must NOT clobber the operator edit.
    second = migrate()
    assert second["inserted"] == 0
    assert second["skipped_existing"] == 1
    assert sm.get_mode("simplified_engine")["mode"] == "sandbox"


def test_migration_no_rows_is_a_noop(fresh_both):
    sm, _sdi, _ = fresh_both
    from scripts.migrate_strategy_daily_intent_to_strategy_mode import migrate

    result = migrate()
    assert result == {"inserted": 0, "skipped_existing": 0, "details": []}
    assert sm.list_modes() == []
