"""API endpoint tests for the intraday_pullback evaluation-history route (issue #422).

Builds a minimal Flask app with only the intraday_pullback blueprint registered, and
monkeypatches the auth + service lookups so no live broker/DB is touched. Mirrors
test_futures_follow_blueprint.
"""

import os

# blueprints.intraday_pullback imports database.auth_db, which requires a pepper at import time.
# Set a throwaway one before the blueprint is imported (the conftest tripwire already redirects
# DATABASE_URL to a temp dir).
os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

STRAT = "intraday_pullback_top2"
_HISTORY_URL = "/intraday_pullback_top2/api/entry_breakdown/history"


@pytest.fixture
def client(monkeypatch):
    import blueprints.intraday_pullback as bp

    # Auth: accept the key "GOOD" only.
    monkeypatch.setattr(bp, "verify_api_key", lambda k: k == "GOOD")

    app = Flask(__name__)
    app.register_blueprint(bp.intraday_pullback_bp)
    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def _eval_db(monkeypatch):
    """Rebind database.intraday_pullback_eval_db to a fresh in-memory DB per test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker

    from database import intraday_pullback_eval_db as eval_db

    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    sess = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=eng))
    monkeypatch.setattr(eval_db, "engine", eng)
    monkeypatch.setattr(eval_db, "db_session", sess)
    eval_db.Base.query = sess.query_property()
    eval_db.Base.metadata.create_all(eng)
    yield eval_db
    sess.remove()
    eng.dispose()


def _payload(n_trades=0, picks=("AAA", "BBB")):
    return {
        "date": "2026-07-15",
        "mode": "sandbox",
        "side_today": "L",
        "nifty_930_pct": 0.5,
        "selected": True,
        "picks": list(picks),
        "n_trades_today": n_trades,
        "evaluation": [
            {
                "symbol": s,
                "sector": "IDX1",
                "gain_930_pct": 1.5,
                "sector_930_pct": 0.4,
                "diag": {
                    "candles": 12,
                    "ref_formed": 1,
                    "breakouts": 2,
                    "gate_blocked": 1,
                    "no_slot": 0,
                    "entries": 0,
                    "exits": 0,
                },
                "reason": "gate blocked",
                "position": "none",
            }
            for s in picks
        ],
    }


def test_history_requires_auth(client, _eval_db):
    assert client.get(_HISTORY_URL).status_code == 401


def test_history_rejects_bad_key(client, _eval_db):
    assert client.get(_HISTORY_URL, headers={"X-API-KEY": "BAD"}).status_code == 401


def test_history_session_auth_allows_read(client, _eval_db, monkeypatch):
    """The React card authenticates with the session cookie, not an API key."""
    monkeypatch.setattr("utils.session.is_session_valid", lambda: True)
    assert client.get(_HISTORY_URL).status_code == 200


def test_history_empty_is_success_not_error(client, _eval_db):
    resp = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"})
    assert resp.status_code == 200
    data = resp.get_json()["data"]
    assert data["rows"] == []
    assert data["has_more"] is False


def test_history_returns_summaries_newest_first(client, _eval_db):
    for d in ("2026-07-14", "2026-07-15", "2026-07-16"):
        assert _eval_db.upsert_snapshot(STRAT, d, _payload())
    rows = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["rows"]
    assert [r["eval_date"] for r in rows] == ["2026-07-16", "2026-07-15", "2026-07-14"]
    # Summaries, not payloads: the per-pick `evaluation` list is not shipped to the table.
    assert "evaluation" not in rows[0]
    assert rows[0]["n_picks"] == 2
    assert rows[0]["diag"]["breakouts"] == 4  # 2 picks x 2 breakouts


def test_history_has_more_flag_and_limit(client, _eval_db):
    for d in ("2026-07-14", "2026-07-15", "2026-07-16"):
        assert _eval_db.upsert_snapshot(STRAT, d, _payload())
    data = client.get(f"{_HISTORY_URL}?limit=2", headers={"X-API-KEY": "GOOD"}).get_json()["data"]
    assert [r["eval_date"] for r in data["rows"]] == ["2026-07-16", "2026-07-15"]
    assert data["has_more"] is True

    data = client.get(f"{_HISTORY_URL}?limit=3", headers={"X-API-KEY": "GOOD"}).get_json()["data"]
    assert data["has_more"] is False


def test_history_before_pages_backwards(client, _eval_db):
    for d in ("2026-07-14", "2026-07-15", "2026-07-16"):
        assert _eval_db.upsert_snapshot(STRAT, d, _payload())
    data = client.get(
        f"{_HISTORY_URL}?before=2026-07-16&limit=2", headers={"X-API-KEY": "GOOD"}
    ).get_json()["data"]
    assert [r["eval_date"] for r in data["rows"]] == ["2026-07-15", "2026-07-14"]


@pytest.mark.parametrize("qs", ["limit=abc", "limit=0", "limit=-5", "before=not-a-date"])
def test_history_invalid_params_400(client, _eval_db, qs):
    assert client.get(f"{_HISTORY_URL}?{qs}", headers={"X-API-KEY": "GOOD"}).status_code == 400


def test_history_today_reports_pending_on_a_trading_day(client, _eval_db, monkeypatch):
    """No snapshot yet + a trading day -> the UI shows the pending row."""
    monkeypatch.setattr("services.data_freshness_service.is_trading_day", lambda d: True)
    today = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["today"]
    assert today["is_trading_day"] is True
    assert today["snapshot_exists"] is False


def test_history_today_no_pending_row_on_a_holiday(client, _eval_db, monkeypatch):
    """A weekend / NSE holiday must not promise an evaluation that never comes."""
    monkeypatch.setattr("services.data_freshness_service.is_trading_day", lambda d: False)
    today = client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["today"]
    assert today["is_trading_day"] is False


def test_history_today_snapshot_exists_survives_paging(client, _eval_db):
    """`snapshot_exists` is queried directly, never inferred from the page — a ?before= page
    never contains today's row."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    today_str = datetime.now(ist).date().isoformat()
    assert _eval_db.upsert_snapshot(STRAT, today_str, _payload())
    assert _eval_db.upsert_snapshot(STRAT, "2026-01-02", _payload())

    data = client.get(
        f"{_HISTORY_URL}?before={today_str}", headers={"X-API-KEY": "GOOD"}
    ).get_json()["data"]
    assert today_str not in [r["eval_date"] for r in data["rows"]]
    assert data["today"]["snapshot_exists"] is True


def test_history_ignores_other_strategies_rows(client, _eval_db):
    assert _eval_db.upsert_snapshot("some_other_strategy", "2026-07-16", _payload())
    assert client.get(_HISTORY_URL, headers={"X-API-KEY": "GOOD"}).get_json()["data"]["rows"] == []
