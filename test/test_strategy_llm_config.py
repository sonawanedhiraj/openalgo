"""Tests for the per-strategy LLM control (issue #266 Phase 2).

Covers:
  * ``strategy_llm_config_db`` upsert/read/delete (in-memory SQLite).
  * ``strategy_llm_config_service.flip_llm_mode`` writes + publishes the event.
  * ``signal_review_service.get_veto_layer_mode`` reads the DB llm_mode first
    (off→off, veto→active), env fallback when no row.
  * ``signal_decision_db`` list/count/summarize helpers + pagination.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_llm_config_db(monkeypatch):
    """Point strategy_llm_config_db at a fresh in-memory SQLite for one test."""
    from database import strategy_llm_config_db as db

    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
    monkeypatch.setattr(db, "engine", test_engine)
    monkeypatch.setattr(db, "db_session", test_session)
    db.Base.query = test_session.query_property()
    db.Base.metadata.create_all(test_engine)

    yield db

    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def fresh_signal_db(monkeypatch):
    from database import signal_decision_db as sdb

    test_engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
    monkeypatch.setattr(sdb, "engine", test_engine)
    monkeypatch.setattr(sdb, "db_session", test_session)
    # Force re-create on the new engine (the module caches by engine identity).
    monkeypatch.setattr(sdb, "_tables_ensured_for_engine", None)
    sdb.Base.metadata.create_all(test_engine)

    yield sdb

    test_session.remove()
    test_engine.dispose()


# --------------------------------------------------------------------------- #
# DB layer
# --------------------------------------------------------------------------- #


def test_get_llm_mode_none_when_no_row(fresh_llm_config_db):
    assert fresh_llm_config_db.get_llm_mode("simplified_engine") is None


def test_set_and_get_llm_mode(fresh_llm_config_db):
    fresh_llm_config_db._set_llm_mode_unchecked("simplified_engine", "veto", updated_by="test")
    row = fresh_llm_config_db.get_llm_mode("simplified_engine")
    assert row["llm_mode"] == "veto"
    assert row["updated_by"] == "test"


def test_set_llm_mode_upsert_overwrites(fresh_llm_config_db):
    fresh_llm_config_db._set_llm_mode_unchecked("s", "veto", updated_by="a")
    fresh_llm_config_db._set_llm_mode_unchecked("s", "off", updated_by="b")
    assert fresh_llm_config_db.get_llm_mode("s")["llm_mode"] == "off"
    assert len(fresh_llm_config_db.list_llm_modes()) == 1


def test_set_llm_mode_rejects_invalid(fresh_llm_config_db):
    with pytest.raises(ValueError):
        fresh_llm_config_db._set_llm_mode_unchecked("s", "shadow", updated_by="a")


def test_delete_llm_mode(fresh_llm_config_db):
    fresh_llm_config_db._set_llm_mode_unchecked("s", "veto", updated_by="a")
    assert fresh_llm_config_db.delete_llm_mode("s") is True
    assert fresh_llm_config_db.get_llm_mode("s") is None


# --------------------------------------------------------------------------- #
# Guarded writer service
# --------------------------------------------------------------------------- #


def test_flip_llm_mode_writes_and_audits(fresh_llm_config_db, monkeypatch):
    from services import strategy_llm_config_service as svc

    published = []
    monkeypatch.setattr(svc._default_bus, "publish", lambda ev: published.append(ev))
    # Silence Telegram.
    monkeypatch.setattr(svc, "_telegram_notify", lambda *a, **k: None)

    outcome = svc.flip_llm_mode("simplified_engine", "veto", flipped_by="ui:test")
    assert outcome.accepted is True
    assert outcome.new_llm_mode == "veto"
    assert outcome.previous_llm_mode is None
    # Row written.
    assert fresh_llm_config_db.get_llm_mode("simplified_engine")["llm_mode"] == "veto"
    # Event published.
    assert len(published) == 1
    assert published[0].new_llm_mode == "veto"
    assert published[0].strategy_name == "simplified_engine"


def test_flip_llm_mode_rejects_bad_mode(fresh_llm_config_db, monkeypatch):
    from services import strategy_llm_config_service as svc

    monkeypatch.setattr(svc, "_telegram_notify", lambda *a, **k: None)
    outcome = svc.flip_llm_mode("s", "shadow", flipped_by="ui:test")
    assert outcome.accepted is False
    assert "must be one of" in (outcome.error_message or "")


def test_flip_llm_mode_delegate_warns(fresh_llm_config_db, monkeypatch):
    from services import strategy_llm_config_service as svc

    monkeypatch.setattr(svc._default_bus, "publish", lambda ev: None)
    monkeypatch.setattr(svc, "_telegram_notify", lambda *a, **k: None)
    outcome = svc.flip_llm_mode("s", "delegate", flipped_by="ui:test")
    assert outcome.accepted is True
    assert any("delegate" in w for w in outcome.warnings)


def test_flip_llm_mode_same_mode_noop(fresh_llm_config_db, monkeypatch):
    from services import strategy_llm_config_service as svc

    published = []
    monkeypatch.setattr(svc._default_bus, "publish", lambda ev: published.append(ev))
    monkeypatch.setattr(svc, "_telegram_notify", lambda *a, **k: None)
    svc.flip_llm_mode("s", "veto", flipped_by="a")
    published.clear()
    outcome = svc.flip_llm_mode("s", "veto", flipped_by="b")
    assert outcome.accepted is True
    assert any("no-op" in w for w in outcome.warnings)
    # No event on a no-op.
    assert published == []


# --------------------------------------------------------------------------- #
# Resolver rewiring — get_veto_layer_mode reads the DB first
# --------------------------------------------------------------------------- #


def test_get_veto_layer_mode_db_off(fresh_llm_config_db, monkeypatch):
    from services import signal_review_service as srs

    fresh_llm_config_db._set_llm_mode_unchecked("simplified_engine", "off", updated_by="a")
    # Even with env=active and sandbox mode, the DB 'off' wins.
    monkeypatch.setenv("VETO_LAYER_MODE", "active")
    assert srs.get_veto_layer_mode("sandbox", strategy_name="simplified_engine") == "off"


def test_get_veto_layer_mode_db_veto_maps_active(fresh_llm_config_db, monkeypatch):
    from services import signal_review_service as srs

    fresh_llm_config_db._set_llm_mode_unchecked("simplified_engine", "veto", updated_by="a")
    # Env says shadow, but the DB 'veto' → active wins (fixes #274 ambiguity).
    monkeypatch.setenv("VETO_LAYER_MODE", "shadow")
    assert srs.get_veto_layer_mode("sandbox", strategy_name="simplified_engine") == "active"


def test_get_veto_layer_mode_db_delegate_maps_active(fresh_llm_config_db, monkeypatch):
    from services import signal_review_service as srs

    fresh_llm_config_db._set_llm_mode_unchecked("s", "delegate", updated_by="a")
    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    assert srs.get_veto_layer_mode("live", strategy_name="s") == "active"


def test_get_veto_layer_mode_env_fallback_when_no_row(fresh_llm_config_db, monkeypatch):
    from services import signal_review_service as srs

    # No DB row for this strategy → env override applies.
    monkeypatch.setenv("VETO_LAYER_MODE", "off")
    assert srs.get_veto_layer_mode("sandbox", strategy_name="unknown_strat") == "off"


def test_get_veto_layer_mode_mode_aware_default_when_no_row_no_env(
    fresh_llm_config_db, monkeypatch
):
    from services import signal_review_service as srs

    monkeypatch.delenv("VETO_LAYER_MODE", raising=False)
    # No row, no env → sandbox defaults active, live defaults shadow.
    assert srs.get_veto_layer_mode("sandbox", strategy_name="x") == "active"
    assert srs.get_veto_layer_mode("live", strategy_name="x") == "shadow"


# --------------------------------------------------------------------------- #
# signal_decision query helpers
# --------------------------------------------------------------------------- #


def _seed(sdb, n, source, decision):
    for i in range(n):
        sdb.insert_signal_decision(
            symbol=f"SYM{i}",
            source=source,
            decision=decision,
            reasoning="r",
            confidence=0.5,
            enforcement_mode="shadow",
            context_snapshot=None,
            bridge_latency_ms=10,
            bridge_session_id="sess",
            raw_bridge_output=None,
        )


def test_list_and_count_signal_decisions(fresh_signal_db):
    _seed(fresh_signal_db, 3, "chartink_FnO_intraday_buy", "take")
    _seed(fresh_signal_db, 2, "trend-up", "skip")
    assert fresh_signal_db.count_signal_decisions() == 5
    rows = fresh_signal_db.list_signal_decisions(limit=10)
    assert len(rows) == 5
    # Newest first (highest id first).
    assert rows[0]["id"] > rows[-1]["id"]


def test_list_signal_decisions_pagination(fresh_signal_db):
    _seed(fresh_signal_db, 5, "s", "take")
    page1 = fresh_signal_db.list_signal_decisions(limit=2, offset=0)
    page2 = fresh_signal_db.list_signal_decisions(limit=2, offset=2)
    assert len(page1) == 2 and len(page2) == 2
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_summarize_signal_decisions(fresh_signal_db):
    _seed(fresh_signal_db, 4, "s", "take")
    _seed(fresh_signal_db, 2, "s", "skip")
    _seed(fresh_signal_db, 1, "s", "review_failed")
    summary = fresh_signal_db.summarize_signal_decisions()
    assert summary["total"] == 7
    assert summary["take"] == 4
    assert summary["skip"] == 2
    assert summary["review_failed"] == 1
    assert summary["last_decision"] is not None
    # last seeded row is review_failed → recent_review_failed >= 1
    assert summary["recent_review_failed"] >= 1


def test_summarize_empty(fresh_signal_db):
    summary = fresh_signal_db.summarize_signal_decisions()
    assert summary["total"] == 0
    assert summary["last_decision"] is None
