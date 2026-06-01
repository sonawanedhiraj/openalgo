"""Integration test for the scan_cycle audit wired into chartink webhook.

We exercise ``simplified_stock_engine_webhook`` through a Flask test request
context with the strategy lookup, engine service, and symbol parser stubbed
out. The point of the test is to confirm the audit rows land — we do NOT
assert on the engine call itself, because the engine is mocked.

Mocks used:
    * ``blueprints.chartink.get_strategy_by_webhook_id`` → returns a fake
      strategy with is_intraday=False (skips the time-window guards).
    * ``services.simplified_stock_engine_service.parse_chartink_symbols`` →
      returns a fixed list so the scan_buy heartbeat carries a real count.
    * ``services.simplified_stock_engine_service.get_simplified_stock_engine_service``
      → returns a stub whose process_chartink_webhook returns success.
    * ``services.mode_service.resolve_effective_mode`` is left untouched —
      it will throw because the daily_intent table isn't init'd here, and
      the webhook handler must catch that gracefully (effective_mode=None).
"""

import json
from types import SimpleNamespace

import pytest
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_cycle_db(monkeypatch):
    """Point scan_cycle_db at a fresh in-memory SQLite for one test."""
    from database import scan_cycle_db as scdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=test_engine)
    )

    monkeypatch.setattr(scdb, "engine", test_engine)
    monkeypatch.setattr(scdb, "db_session", test_session)
    scdb.Base.metadata.create_all(test_engine)

    yield scdb

    test_session.remove()
    test_engine.dispose()


@pytest.fixture
def app_with_chartink(fresh_cycle_db):
    """Bare Flask app with the chartink blueprint mounted, limiter bound."""
    from blueprints.chartink import chartink_bp
    from limiter import limiter

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    # Limiter has to be bound to the new app once so its decorators don't
    # blow up. Storage stays in-memory so per-test isolation is fine.
    limiter.init_app(app)
    app.register_blueprint(chartink_bp)
    return app


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _fake_strategy(name="chartink_FnO_intraday_buy"):
    return SimpleNamespace(
        id=42,
        user_id="test_user",
        name=name,
        is_active=True,
        is_intraday=False,  # bypass time-window guards
        start_time="09:15",
        end_time="15:00",
        squareoff_time="15:15",
    )


class _FakeEngineService:
    def process_chartink_webhook(self, user_id, strategy_name, payload):
        return {
            "status": "success",
            "armed": ["RELIANCE", "INFY"],
            "message": "test stub",
        }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_webhook_records_cycle_and_heartbeats(app_with_chartink, fresh_cycle_db, monkeypatch):
    """Happy-path: one scan_cycle row + at least 3 heartbeats land in the DB."""
    from blueprints import chartink as chartink_module

    monkeypatch.setattr(chartink_module, "get_strategy_by_webhook_id", lambda _id: _fake_strategy())
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.parse_chartink_symbols",
        lambda payload: ["RELIANCE", "INFY", "TCS"],
    )
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        lambda: _FakeEngineService(),
    )

    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/simplified-stock-engine/test-webhook-id",
        data=json.dumps({"stocks": "RELIANCE,INFY,TCS"}),
        content_type="application/json",
    )

    assert resp.status_code == 200, resp.data

    cycles = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle)
        .filter_by(cycle_kind="chartink")
        .all()
    )
    assert len(cycles) == 1
    cycle = cycles[0]
    assert cycle.post_status == "ok"
    assert cycle.completed_at is not None
    assert json.loads(cycle.screener_buy) == ["RELIANCE", "INFY", "TCS"]
    # mode_service.resolve_effective_mode is allowed to fail (no daily_intent
    # table in this test). The handler should record None and move on.
    # We don't assert the value — just that the cycle itself completed.

    heartbeats = (
        fresh_cycle_db.db_session.query(fresh_cycle_db.CycleHeartbeat)
        .filter_by(cycle_id=cycle.id)
        .order_by(fresh_cycle_db.CycleHeartbeat.id.asc())
        .all()
    )
    assert len(heartbeats) >= 3
    stages = [h.stage for h in heartbeats]
    # preflight ok, scan_buy started, scan_buy ok, post started, post ok
    assert "preflight" in stages
    assert "scan_buy" in stages
    assert "post" in stages


def test_webhook_unknown_id_still_audits(app_with_chartink, fresh_cycle_db, monkeypatch):
    """Even invalid-webhook 404 path leaves a cycle row marked 'error'."""
    from blueprints import chartink as chartink_module

    monkeypatch.setattr(chartink_module, "get_strategy_by_webhook_id", lambda _id: None)

    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/simplified-stock-engine/bogus-id",
        data=json.dumps({"stocks": "X"}),
        content_type="application/json",
    )

    assert resp.status_code == 404

    cycles = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).all()
    assert len(cycles) == 1
    assert cycles[0].post_status == "error"
    assert cycles[0].completed_at is not None


def test_webhook_engine_exception_records_error(app_with_chartink, fresh_cycle_db, monkeypatch):
    """If the engine raises, audit records the error_payload but webhook 500s."""
    from blueprints import chartink as chartink_module

    class _BoomService:
        def process_chartink_webhook(self, *a, **kw):
            raise RuntimeError("engine blew up")

    monkeypatch.setattr(chartink_module, "get_strategy_by_webhook_id", lambda _id: _fake_strategy())
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.parse_chartink_symbols",
        lambda payload: ["X"],
    )
    monkeypatch.setattr(
        "services.simplified_stock_engine_service.get_simplified_stock_engine_service",
        lambda: _BoomService(),
    )

    client = app_with_chartink.test_client()
    resp = client.post(
        "/chartink/simplified-stock-engine/test-id",
        data=json.dumps({"stocks": "X"}),
        content_type="application/json",
    )

    assert resp.status_code == 500

    cycle = fresh_cycle_db.db_session.query(fresh_cycle_db.ScanCycle).first()
    assert cycle is not None
    assert cycle.post_status == "error"
    error_payload = json.loads(cycle.error_payload)
    assert error_payload["type"] == "RuntimeError"
    assert "engine blew up" in error_payload["error"]
