"""Tests for the unified strategy_daily_intent table + resolve_strategy_mode.

Each test rebinds ``database.strategy_daily_intent_db`` (and, for the migration
test, ``database.daily_intent_db``) to a shared in-memory SQLite so nothing
touches ``db/openalgo.db``. Mirrors the rebinding pattern in test_mode_service.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_db(monkeypatch):
    """Point strategy_daily_intent_db, the legacy daily_intent table AND the
    mode-only ``strategy_mode_db`` at one shared in-memory SQLite engine for a
    single test.

    The mode-only architecture (issue #197 → ``mode_service.resolve_strategy_mode``)
    consults ``strategy_mode_db`` first; if that module's engine isn't
    monkeypatched too, the resolver hits the global temp DB and any
    ``strategy_mode`` row written by another test (via xdist parallel order
    shifting) pollutes this test — manifesting as ``source='strategy_mode'``
    when the test expected ``source='env'``. Pin all three engines to the
    same per-test in-memory DB so the resolver fall-through cascade runs
    against a known-empty starting state.
    """
    from database import daily_intent_db as dim
    from database import strategy_daily_intent_db as sdi
    from database import strategy_mode_db as smd

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))

    monkeypatch.setattr(sdi, "engine", eng)
    monkeypatch.setattr(sdi, "db_session", sess)
    sdi.Base.query = sess.query_property()
    # All three tables live on the same engine so cross-module reads
    # (mode_service.resolve_strategy_mode → smd.get_mode, then →
    # sdi.get_intent, then env) all see the same empty starting state.
    monkeypatch.setattr(smd, "engine", eng)
    monkeypatch.setattr(smd, "db_session", sess)
    smd.Base.query = sess.query_property()
    sdi.Base.metadata.create_all(eng)
    dim.Base.metadata.create_all(eng)
    smd.Base.metadata.create_all(eng)

    yield sdi, dim, sess

    sess.remove()
    eng.dispose()


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #
def test_init_is_idempotent(fresh_db):
    sdi, _, _ = fresh_db
    sdi.init_db()
    sdi.init_db()  # second call must not raise


def test_get_returns_none_when_no_row(fresh_db):
    sdi, _, _ = fresh_db
    assert sdi.get_intent("sector_follow_cap5_vol", "2026-06-10") is None


def test_set_get_roundtrip(fresh_db):
    sdi, _, _ = fresh_db
    sdi.set_intent(
        "sector_follow_cap5_vol",
        "2026-06-10",
        "sandbox",
        "pause",
        daily_capital_cap=100000.0,
        updated_by="cli",
        notes="hi",
    )
    row = sdi.get_intent("sector_follow_cap5_vol", "2026-06-10")
    assert row["mode"] == "sandbox"
    assert row["intent"] == "pause"
    assert row["daily_capital_cap"] == 100000.0
    assert row["updated_by"] == "cli"
    assert row["notes"] == "hi"
    assert row["source"] == "unified"


def test_set_updates_existing(fresh_db):
    sdi, _, _ = fresh_db
    sdi.set_intent("simplified_engine", "2026-06-10", "sandbox", "run", updated_by="a")
    sdi.set_intent("simplified_engine", "2026-06-10", "live", "halt", updated_by="b")
    row = sdi.get_intent("simplified_engine", "2026-06-10")
    assert row["mode"] == "live"
    assert row["intent"] == "halt"
    assert row["updated_by"] == "b"


def test_set_rejects_bad_mode(fresh_db):
    sdi, _, _ = fresh_db
    with pytest.raises(ValueError):
        sdi.set_intent("x", "2026-06-10", "yolo", "run", updated_by="cli")


def test_set_rejects_bad_intent(fresh_db):
    sdi, _, _ = fresh_db
    with pytest.raises(ValueError):
        sdi.set_intent("x", "2026-06-10", "sandbox", "nope", updated_by="cli")


def test_list_intents_for_date(fresh_db):
    sdi, _, _ = fresh_db
    sdi.set_intent("a", "2026-06-10", "sandbox", "run", updated_by="cli")
    sdi.set_intent("b", "2026-06-10", "live", "run", updated_by="cli")
    sdi.set_intent("c", "2026-06-11", "skip", "run", updated_by="cli")
    rows = sdi.list_intents("2026-06-10")
    assert {r["strategy_name"] for r in rows} == {"a", "b"}
    all_rows = sdi.list_intents("all")
    assert len(all_rows) == 3


def test_delete_intent(fresh_db):
    sdi, _, _ = fresh_db
    sdi.set_intent("a", "2026-06-10", "sandbox", "run", updated_by="cli")
    assert sdi.delete_intent("a", "2026-06-10") is True
    assert sdi.get_intent("a", "2026-06-10") is None
    assert sdi.delete_intent("a", "2026-06-10") is False  # already gone


# --------------------------------------------------------------------------- #
# Migration
# --------------------------------------------------------------------------- #
def test_migration_backfills_legacy_rows(fresh_db):
    sdi, dim, sess = fresh_db
    # Seed legacy rows directly on the shared engine via the patched session.
    sess.add(
        dim.DailyIntent(
            date="2026-06-08",
            intent="live",
            set_by="operator",
            set_at="2026-06-08T09:00:00",
            locked=0,
        )
    )
    sess.add(
        dim.DailyIntent(
            date="2026-06-09",
            intent="sandbox",
            set_by="agent",
            set_at="2026-06-09T09:00:00",
            locked=0,
        )
    )
    sess.commit()
    sess.remove()

    inserted = sdi.migrate_legacy_daily_intent()
    assert inserted == 2

    r1 = sdi.get_intent("simplified_engine", "2026-06-08")
    assert r1["mode"] == "live"
    assert r1["intent"] == "run"
    assert r1["updated_by"] == "migration"
    r2 = sdi.get_intent("simplified_engine", "2026-06-09")
    assert r2["mode"] == "sandbox"


def test_migration_is_idempotent(fresh_db):
    sdi, dim, sess = fresh_db
    sess.add(
        dim.DailyIntent(
            date="2026-06-08",
            intent="live",
            set_by="operator",
            set_at="2026-06-08T09:00:00",
            locked=0,
        )
    )
    sess.commit()
    sess.remove()

    assert sdi.migrate_legacy_daily_intent() == 1
    # Second run inserts nothing.
    assert sdi.migrate_legacy_daily_intent() == 0


def test_migration_does_not_clobber_operator_edits(fresh_db):
    sdi, dim, sess = fresh_db
    # Operator already set an explicit unified row for that date.
    sdi.set_intent("simplified_engine", "2026-06-08", "skip", "halt", updated_by="operator")
    sess.add(
        dim.DailyIntent(
            date="2026-06-08",
            intent="live",
            set_by="operator",
            set_at="2026-06-08T09:00:00",
            locked=0,
        )
    )
    sess.commit()
    sess.remove()

    assert sdi.migrate_legacy_daily_intent() == 0  # existing row untouched
    row = sdi.get_intent("simplified_engine", "2026-06-08")
    assert row["mode"] == "skip"
    assert row["intent"] == "halt"


# --------------------------------------------------------------------------- #
# resolve_strategy_mode fall-through
# --------------------------------------------------------------------------- #
def test_resolver_ignores_legacy_intent_table(fresh_db, monkeypatch):
    """Mode-only: resolve_strategy_mode reads the strategy_mode table, NOT the
    retired strategy_daily_intent table. A unified-intent row is ignored — the
    resolver falls through to env/default and always reports intent='run'."""
    sdi, _, _ = fresh_db
    monkeypatch.delenv("SECTOR_FOLLOW_CAP5_VOL_MODE", raising=False)
    today = sdi._today_ist_str()
    # This row used to drive the resolver; it must now be ignored.
    sdi.set_intent(
        "sector_follow_cap5_vol",
        today,
        "live",
        "pause",
        daily_capital_cap=75000.0,
        updated_by="cli",
    )

    from services.mode_service import resolve_strategy_mode

    d = resolve_strategy_mode("sector_follow_cap5_vol")
    assert d.source != "unified"  # the intent table is no longer a source
    assert d.intent == "run"  # intent axis retired
    assert d.daily_capital_cap is None


def test_resolver_env_fallthrough_when_no_row(fresh_db, monkeypatch):
    # fresh_db fixture ensures the (empty) table exists on the in-memory engine.
    monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "true")
    monkeypatch.setenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "sandbox")

    from services.mode_service import resolve_strategy_mode

    d = resolve_strategy_mode("sector_follow_cap5_vol")
    assert d.source == "env"
    assert d.mode == "sandbox"
    assert d.intent == "run"


def test_resolver_flag_off_skips_unified(fresh_db, monkeypatch):
    sdi, _, _ = fresh_db
    today = sdi._today_ist_str()
    sdi.set_intent("sector_follow_cap5_vol", today, "live", "halt", updated_by="cli")
    monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "false")
    monkeypatch.setenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "sandbox")

    from services.mode_service import resolve_strategy_mode

    d = resolve_strategy_mode("sector_follow_cap5_vol")
    # Flag off → the unified row is ignored, env wins.
    assert d.source == "env"
    assert d.mode == "sandbox"
    assert d.intent == "run"


def test_resolver_default_for_unknown_strategy(fresh_db, monkeypatch):
    monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "true")

    from services.mode_service import resolve_strategy_mode

    d = resolve_strategy_mode("some_unknown_strategy")
    assert d.source == "default"
    assert d.mode == "sandbox"
    assert d.intent == "run"
