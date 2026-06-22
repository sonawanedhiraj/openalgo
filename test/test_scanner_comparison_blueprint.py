"""Blueprint test for ``GET /chartink/api/scanner-comparison/today``.

Exercises the read-only Flask route added for the Screener Comparison page —
the endpoint reuses ``services.scanner_comparison_eod_service`` (covered by
``test_scanner_comparison_eod_service``), so this test focuses on:

* the session gate (401 when ``logged_in`` is not in the session),
* the JSON envelope (``status='success'``, ``data.date``, ``data.summary.{BUY,SELL}``,
  ``data.timeline.{chartink,inhouse}``),
* the timeline structure — Chartink cycles can carry both sides at once and the
  per-event rows must carry the (ts, side, symbols, posted) tuple the UI needs,
* explicit ``?date=`` is respected,
* prior-day rows and non-chartink ``cycle_kind`` rows are excluded.

Hermetic. The global ``test/conftest.py`` already redirects DB env vars to a
temp dir; we additionally rebind the three modules the service reads to
``:memory:`` engines for full isolation of the seeded scenario.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

import pytest
import pytz
from flask import Flask
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

DATE = "2026-06-19"


def _mk(module):
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    module.Base.metadata.create_all(bind=eng)
    return eng, sess


@pytest.fixture
def wired_dbs(monkeypatch):
    """Rebind scan_cycle_db + scanner_db to fresh in-memory engines + seed events.

    Scenario seeded — close to the shape the live system produces:
      Chartink BUY  = {INFY, RELIANCE}    (two cycles, both posted)
      Chartink SELL = {TATAELXSI, ITC}    (one cycle, post_status='error')
      In-house BUY  = {INFY}              (one scan_result, posted_to_engine=1)
      In-house SELL = {ITC, ZEEL}         (one scan_result, posted_to_engine=0)
      → BUY intersection = {INFY}, SELL intersection = {ITC}.
    Plus noise that must be filtered out: a prior-day chartink cycle,
    a non-chartink ``cycle_kind`` row, and a ``source='chartink'`` scan_result.
    """
    from database import scan_cycle_db, scanner_db

    cyc_eng, cyc_sess = _mk(scan_cycle_db)
    scan_eng, scan_sess = _mk(scanner_db)

    monkeypatch.setattr(scan_cycle_db, "engine", cyc_eng)
    monkeypatch.setattr(scan_cycle_db, "db_session", cyc_sess)
    monkeypatch.setattr(scanner_db, "engine", scan_eng)
    monkeypatch.setattr(scanner_db, "db_session", scan_sess)

    cyc_sess.add_all(
        [
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T09:18:00+05:30",
                cycle_kind="chartink",
                screener_buy='["INFY"]',
                screener_sell=None,
                post_status="ok",
            ),
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T13:20:00+05:30",
                cycle_kind="chartink",
                screener_buy='["RELIANCE"]',
                screener_sell='["TATAELXSI", "ITC"]',
                post_status="error",
            ),
            # Noise: a manual cycle that must NOT appear.
            scan_cycle_db.ScanCycle(
                started_at=f"{DATE}T14:00:00+05:30",
                cycle_kind="manual",
                screener_buy='["NOISE"]',
                screener_sell='["NOISE"]',
                post_status="ok",
            ),
            # Noise: a prior-day chartink cycle that must NOT appear.
            scan_cycle_db.ScanCycle(
                started_at="2026-06-18T14:19:00+05:30",
                cycle_kind="chartink",
                screener_buy='["OLD"]',
                screener_sell='["OLD"]',
                post_status="ok",
            ),
        ]
    )
    cyc_sess.commit()

    buy_def = scanner_db.ScanDefinition(
        name="fno_intraday_buy_20",
        screener_type="buy",
        expression_json="{}",
        rule_module="fno_intraday_buy_chartink",
        enabled=1,
        created_at=DATE,
        updated_at=DATE,
    )
    sell_def = scanner_db.ScanDefinition(
        name="fno_intraday_sell_20",
        screener_type="sell",
        expression_json="{}",
        rule_module="fno_intraday_sell_chartink",
        enabled=1,
        created_at=DATE,
        updated_at=DATE,
    )
    scan_sess.add_all([buy_def, sell_def])
    scan_sess.commit()
    buy_id, sell_id = buy_def.id, sell_def.id

    scan_sess.add_all(
        [
            scanner_db.ScanResult(
                scan_definition_id=buy_id,
                run_at=f"{DATE}T09:30:00+05:30",
                symbols='["INFY"]',
                source="inhouse",
                posted_to_engine=1,
            ),
            scanner_db.ScanResult(
                scan_definition_id=sell_id,
                run_at=f"{DATE}T13:25:00+05:30",
                symbols='["ITC", "ZEEL"]',
                source="inhouse",
                posted_to_engine=0,
            ),
            # Noise: a chartink-sourced scan_result that must NOT appear.
            scanner_db.ScanResult(
                scan_definition_id=buy_id,
                run_at=f"{DATE}T13:30:00+05:30",
                symbols='["NOISE"]',
                source="chartink",
                posted_to_engine=0,
            ),
        ]
    )
    scan_sess.commit()

    yield

    cyc_sess.remove()
    scan_sess.remove()
    cyc_eng.dispose()
    scan_eng.dispose()


@pytest.fixture
def app_with_chartink(wired_dbs):
    """Bare Flask app with the chartink blueprint mounted + a secret key for session."""
    from blueprints.chartink import chartink_bp
    from limiter import limiter

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False
    app.config["SECRET_KEY"] = "test-secret-screener-comparison"  # pragma: allowlist secret
    limiter.init_app(app)
    app.register_blueprint(chartink_bp)
    return app


def _login(client) -> None:
    """Seed a valid session so ``check_session_validity`` lets us through."""
    now_ist = _dt.datetime.now(pytz.timezone("Asia/Kolkata"))
    with client.session_transaction() as s:
        s["logged_in"] = True
        s["user"] = "test_user"
        s["login_time"] = now_ist.isoformat()
        s["broker"] = "zerodha"


def _assert_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    assert payload["status"] == "success", payload
    data = payload["data"]
    assert "date" in data and "summary" in data and "timeline" in data
    assert {"BUY", "SELL"} == set(data["summary"].keys())
    assert {"chartink", "inhouse"} == set(data["timeline"].keys())
    return data


def test_session_gate_returns_401(app_with_chartink):
    """No session → 401 JSON, no DB read attempted.

    Mirrors how the React UI calls the endpoint — axios sends an
    ``Accept: application/json`` header, which is the same path
    ``check_session_validity`` keys off to return JSON instead of a redirect.
    """
    client = app_with_chartink.test_client()
    resp = client.get(
        "/chartink/api/scanner-comparison/today",
        headers={"Accept": "application/json"},
    )
    assert resp.status_code == 401
    body = resp.get_json()
    assert body is not None
    assert body.get("status") == "error"
    assert (body.get("error") == "session_expired") or "session" in (
        body.get("message", "") or ""
    ).lower()


def test_envelope_and_summary_for_explicit_date(app_with_chartink):
    """Explicit ``?date=`` is honored and the summary block carries the metrics."""
    client = app_with_chartink.test_client()
    _login(client)

    resp = client.get(f"/chartink/api/scanner-comparison/today?date={DATE}")
    assert resp.status_code == 200, resp.get_data(as_text=True)
    data = _assert_envelope(resp.get_json())
    assert data["date"] == DATE

    buy = data["summary"]["BUY"]
    sell = data["summary"]["SELL"]

    # BUY: chartink={INFY,RELIANCE} ∩ inhouse={INFY} → ∩=1, union=2
    assert buy["chartink_count"] == 2
    assert buy["inhouse_count"] == 1
    assert buy["intersection_count"] == 1
    assert buy["jaccard"] == pytest.approx(1 / 2)
    assert buy["ratio"] == pytest.approx(1 / 2)
    assert "INFY" in buy["intersection"]

    # SELL: chartink={TATAELXSI,ITC} ∩ inhouse={ITC,ZEEL} → ∩=1, union=3
    assert sell["chartink_count"] == 2
    assert sell["inhouse_count"] == 2
    assert sell["intersection_count"] == 1
    assert sell["jaccard"] == pytest.approx(1 / 3)
    assert sell["ratio"] == pytest.approx(1 / 2)
    assert "ITC" in sell["intersection"]
    # Top-diff names are populated so the UI can show them inline.
    assert "ZEEL" in sell["false_positives"]
    assert "TATAELXSI" in sell["false_negatives"]


def test_timeline_carries_per_event_rows(app_with_chartink):
    """Per-event timeline rows must carry ts/side/symbols/posted for the UI."""
    client = app_with_chartink.test_client()
    _login(client)

    resp = client.get(f"/chartink/api/scanner-comparison/today?date={DATE}")
    assert resp.status_code == 200
    data = _assert_envelope(resp.get_json())

    # --- Chartink side: noise (manual cycle + prior-day) must be excluded. ---
    chartink_events = data["timeline"]["chartink"]
    # The 2nd cycle carries both BUY and SELL, so it appears twice on the
    # timeline (once per side). The 1st cycle is BUY-only.
    assert len(chartink_events) == 3, chartink_events
    sides_per_ts: dict[str, set[str]] = {}
    posted_per_ts: dict[str, bool] = {}
    for ev in chartink_events:
        assert {"ts", "side", "symbols", "count", "posted", "post_status", "cycle_id"} <= set(
            ev.keys()
        )
        sides_per_ts.setdefault(ev["ts"], set()).add(ev["side"])
        posted_per_ts[ev["ts"]] = ev["posted"]
    assert sides_per_ts == {
        f"{DATE}T09:18:00+05:30": {"BUY"},
        f"{DATE}T13:20:00+05:30": {"BUY", "SELL"},
    }
    # post_status='ok' → posted=True; 'error' → posted=False.
    assert posted_per_ts[f"{DATE}T09:18:00+05:30"] is True
    assert posted_per_ts[f"{DATE}T13:20:00+05:30"] is False

    # No noise symbol slipped through.
    for ev in chartink_events:
        assert "NOISE" not in ev["symbols"]
        assert "OLD" not in ev["symbols"]

    # --- In-house side: source='chartink' noise row must be excluded. ---
    inhouse_events = data["timeline"]["inhouse"]
    assert len(inhouse_events) == 2, inhouse_events
    for ev in inhouse_events:
        assert {"ts", "side", "symbols", "count", "posted", "definition", "result_id"} <= set(
            ev.keys()
        )
        assert "NOISE" not in ev["symbols"]
    by_side = {ev["side"]: ev for ev in inhouse_events}
    assert by_side["BUY"]["symbols"] == ["INFY"]
    assert by_side["BUY"]["posted"] is True
    assert sorted(by_side["SELL"]["symbols"]) == ["ITC", "ZEEL"]
    assert by_side["SELL"]["posted"] is False


def test_quiet_day_returns_empty_timeline_and_zero_summary(app_with_chartink):
    """A date with no rows on either side returns a clean empty envelope."""
    client = app_with_chartink.test_client()
    _login(client)

    resp = client.get("/chartink/api/scanner-comparison/today?date=2026-06-17")
    assert resp.status_code == 200
    data = _assert_envelope(resp.get_json())
    assert data["date"] == "2026-06-17"
    assert data["timeline"]["chartink"] == []
    assert data["timeline"]["inhouse"] == []
    for side in ("BUY", "SELL"):
        s = data["summary"][side]
        assert s["chartink_count"] == 0 and s["inhouse_count"] == 0
        assert s["intersection_count"] == 0
        # Empty union → Jaccard undefined → None.
        assert s["jaccard"] is None


def test_internal_failure_returns_500_with_error_envelope(app_with_chartink, monkeypatch):
    """A service-layer exception is caught and returns a JSON error envelope.

    Mirrors the contract every other route on this blueprint uses (see
    ``simplified_engine_status``): the UI must never see a 500 HTML page.
    """
    client = app_with_chartink.test_client()
    _login(client)

    from services import scanner_comparison_eod_service as svc

    def _boom(date=None):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(svc, "compute_comparison", _boom)
    # Make sure the import-from inside the route resolves to our patched module.
    import sys

    sys.modules["services.scanner_comparison_eod_service"] = svc

    # Use a session() override so the route's own session.get('user') sees a user.
    with client.session_transaction() as s:
        s["user"] = "test_user"
        s["logged_in"] = True
        s["login_time"] = _dt.datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()

    resp = client.get("/chartink/api/scanner-comparison/today")
    assert resp.status_code == 500
    body = resp.get_json()
    assert body["status"] == "error"
    assert "synthetic" in body["message"]
