"""Unit tests for the Stage-0 preflight orchestrator.

Each test rebinds ``database.daily_intent_db`` and ``database.scan_cycle_db``
to in-memory SQLite databases so the test never touches ``db/openalgo.db``.
``_now_ist`` is monkeypatched to make recent-cycle and error-window logic
deterministic.

The broker-session check is patched out by default in the ``preflight_env``
fixture — individual tests can override it to exercise the real fallback
path. ``errors.jsonl`` lives under a per-test ``tmp_path``.
"""

import datetime as dt
import json
from types import SimpleNamespace

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# Shared fixture: in-memory DBs, mocked clock, mocked broker check, isolated
# errors.jsonl. Defaults put us inside market hours on a weekday so the
# happy-path tests don't need to override anything.
# ---------------------------------------------------------------------------


@pytest.fixture
def preflight_env(monkeypatch, tmp_path):
    from database import daily_intent_db as dim
    from database import scan_cycle_db as scdb

    # Daily intent in-memory DB
    di_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    di_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=di_engine)
    )
    monkeypatch.setattr(dim, "engine", di_engine)
    monkeypatch.setattr(dim, "db_session", di_session)
    dim.Base.metadata.create_all(di_engine)

    # Scan cycle in-memory DB
    sc_engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    sc_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=sc_engine)
    )
    monkeypatch.setattr(scdb, "engine", sc_engine)
    monkeypatch.setattr(scdb, "db_session", sc_session)
    scdb.Base.metadata.create_all(sc_engine)

    # analyze_mode = False by default — happy path resolves to LIVE.
    monkeypatch.setattr("services.mode_service.get_analyze_mode", lambda: False)

    # Default "now" = Thursday 2026-05-28 11:30 IST (weekday, in market).
    fake_now = IST.localize(dt.datetime(2026, 5, 28, 11, 30, 0))
    monkeypatch.setattr("services.preflight_service._now_ist", lambda: fake_now)

    # Errors directory empty.
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    # Broker session check is happy by default — individual tests override.
    monkeypatch.setattr(
        "services.preflight_service._check_broker_session",
        lambda: {
            "ok": True,
            "broker": "zerodha",
            "user": "VU3790",
            "reason": None,
        },
    )

    yield SimpleNamespace(
        dim=dim,
        scdb=scdb,
        fake_now=fake_now,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )

    di_session.remove()
    sc_session.remove()
    di_engine.dispose()
    sc_engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _insert_cycle(scdb, started_at_iso: str, kind: str = "chartink") -> int:
    row = scdb.ScanCycle(
        started_at=started_at_iso,
        cycle_kind=kind,
        post_status="ok",
    )
    scdb.db_session.add(row)
    scdb.db_session.commit()
    cid = row.id
    scdb.db_session.remove()
    return cid


def _set_intent(intent: str, date_str: str = "2026-05-28", set_by: str = "operator"):
    from services.mode_service import set_daily_intent_safe

    return set_daily_intent_safe(intent, set_by=set_by, date_str=date_str)


def _write_errors_jsonl(tmp_path, entries: list[dict]) -> None:
    path = tmp_path / "errors.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_all_checks_pass_yields_go(preflight_env):
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))  # 5 min ago
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["ok"] is True
    assert result["go_decision"] == "go"
    assert result["reasons"] == []
    assert result["checks"]["intent"]["ok"] is True
    assert result["checks"]["intent"]["value"] == "live"
    assert result["checks"]["intent"]["set_by"] == "operator"
    assert result["checks"]["effective_mode"]["ok"] is True
    assert result["checks"]["effective_mode"]["value"] == "live"
    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["minutes_since"] == 5
    assert result["checks"]["broker_session"]["ok"] is True
    assert result["checks"]["recent_errors"]["ok"] is True


# ---------------------------------------------------------------------------
# 2. No intent → abort
# ---------------------------------------------------------------------------


def test_no_intent_yields_abort_with_reason(preflight_env):
    from services import preflight_service

    # No _set_intent() call — daily_intent table is empty.
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["ok"] is False
    assert result["go_decision"] == "abort"
    assert result["checks"]["intent"]["ok"] is False
    assert result["checks"]["intent"]["value"] is None
    assert "no daily_intent" in result["checks"]["intent"]["reason"]
    assert any("no daily_intent" in r for r in result["reasons"])


# ---------------------------------------------------------------------------
# 3. Skip intent → abort
# ---------------------------------------------------------------------------


def test_skip_intent_yields_abort(preflight_env):
    from services import preflight_service

    _set_intent("skip")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "abort"
    # intent itself is on record — that check passes.
    assert result["checks"]["intent"]["ok"] is True
    assert result["checks"]["intent"]["value"] == "skip"
    # effective_mode is the one that fails.
    assert result["checks"]["effective_mode"]["ok"] is False
    assert result["checks"]["effective_mode"]["value"] == "skip"
    assert result["checks"]["effective_mode"]["reason"] == "daily_intent is skip"


# ---------------------------------------------------------------------------
# 4. Live + analyze_mode=True → effective=sandbox, still passes
# ---------------------------------------------------------------------------


def test_live_with_analyze_on_still_passes_effective_check(preflight_env):
    from services import preflight_service

    preflight_env.monkeypatch.setattr(
        "services.mode_service.get_analyze_mode", lambda: True
    )
    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "go"
    assert result["checks"]["effective_mode"]["ok"] is True
    assert result["checks"]["effective_mode"]["value"] == "sandbox"


# ---------------------------------------------------------------------------
# 5. Stale cycle during market hours → abort
# ---------------------------------------------------------------------------


def test_stale_cycle_during_market_hours_yields_abort(preflight_env):
    from services import preflight_service

    _set_intent("live")
    # Last cycle was 45 minutes ago — past the 30 min threshold.
    stale = IST.localize(dt.datetime(2026, 5, 28, 10, 45, 0))
    _insert_cycle(preflight_env.scdb, stale.isoformat())

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "abort"
    assert result["checks"]["recent_cycles"]["ok"] is False
    assert result["checks"]["recent_cycles"]["minutes_since"] == 45
    assert "scheduler may be stalled" in result["checks"]["recent_cycles"]["reason"]


# ---------------------------------------------------------------------------
# 6. First-cycle grace period
# ---------------------------------------------------------------------------


def test_first_cycle_grace_period(preflight_env):
    from services import preflight_service

    # Move clock to 09:20 IST — inside market, inside grace window.
    grace_now = IST.localize(dt.datetime(2026, 5, 28, 9, 20, 0))
    preflight_env.monkeypatch.setattr(
        "services.preflight_service._now_ist", lambda: grace_now
    )
    _set_intent("live")
    # No scan_cycle rows.

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["last_cycle_at"] is None
    assert result["go_decision"] == "go"


# ---------------------------------------------------------------------------
# 6b. Fresh-start day: zero cycles today, after grace → PASS
# ---------------------------------------------------------------------------


def test_recent_cycles_passes_when_zero_cycles_today_and_after_grace_window(
    preflight_env,
):
    """Overnight-restart morning: scheduler was off, zero scan_cycle rows
    exist for today. The first preflight call after the 09:30 grace window
    must not deadlock the skill — it should PASS so the first scan cycle
    can fire and populate the audit table.
    """
    from services import preflight_service

    # 10:30 IST — past the 09:30 grace cutoff, still well inside market.
    fresh_morning = IST.localize(dt.datetime(2026, 5, 28, 10, 30, 0))
    preflight_env.monkeypatch.setattr(
        "services.preflight_service._now_ist", lambda: fresh_morning
    )
    _set_intent("live")
    # No scan_cycle rows for today (or any day) — this is the bug scenario.

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["last_cycle_at"] is None
    assert "no cycles today" in result["checks"]["recent_cycles"]["reason"]
    assert result["go_decision"] == "go"


# ---------------------------------------------------------------------------
# 6c. Stale day: cycles fired earlier today but last one is > threshold → ABORT
# ---------------------------------------------------------------------------


def test_recent_cycles_aborts_when_cycles_existed_but_stale(preflight_env):
    """Genuine scheduler stall: cycles fired earlier today, but the most
    recent one is past the staleness threshold. This is the case the gate
    is meant to catch, and must keep aborting after the fresh-start fix.
    """
    from services import preflight_service

    # 11:30 IST.
    now = IST.localize(dt.datetime(2026, 5, 28, 11, 30, 0))
    preflight_env.monkeypatch.setattr(
        "services.preflight_service._now_ist", lambda: now
    )
    _set_intent("live")
    # One cycle at 10:30 today — 60 min before fake_now 11:30, well past
    # the 30 min threshold. (10:30 is also after the real-wall-clock 24h
    # cutoff used by get_recent_cycles, so the row survives the read.)
    earlier = IST.localize(dt.datetime(2026, 5, 28, 10, 30, 0))
    _insert_cycle(preflight_env.scdb, earlier.isoformat())

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is False
    assert result["checks"]["recent_cycles"]["minutes_since"] == 60
    assert "scheduler may be stalled" in result["checks"]["recent_cycles"]["reason"]
    assert result["go_decision"] == "abort"


# ---------------------------------------------------------------------------
# 6d. Recent cycle path — explicit focused assertion on recent_cycles only
# ---------------------------------------------------------------------------


def test_recent_cycles_passes_when_recent_cycle(preflight_env):
    """One cycle 5 minutes ago — the normal mid-session state. Already
    covered indirectly by ``test_all_checks_pass_yields_go``; this focuses
    the assertion on the ``recent_cycles`` sub-check so a regression there
    fails this test first."""
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))  # 5 min ago
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["minutes_since"] == 5
    assert result["checks"]["recent_cycles"]["last_cycle_at"] == recent.isoformat()


# ---------------------------------------------------------------------------
# 7. Outside market hours → recent_cycles always OK
# ---------------------------------------------------------------------------


def test_outside_market_hours_does_not_require_cycles(preflight_env):
    from services import preflight_service

    # 16:00 IST — past 15:30 close.
    after_hours = IST.localize(dt.datetime(2026, 5, 28, 16, 0, 0))
    preflight_env.monkeypatch.setattr(
        "services.preflight_service._now_ist", lambda: after_hours
    )
    _set_intent("live")

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["go_decision"] == "go"


# ---------------------------------------------------------------------------
# 8. Recent errors threshold breach
# ---------------------------------------------------------------------------


def test_recent_errors_threshold_breach(preflight_env):
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    # Write 6 errors within the last 60 min (threshold is 5).
    # Note: errors.jsonl uses naive timestamps in the format
    # "YYYY-MM-DD HH:MM:SS" (local clock = IST on this host).
    entries = []
    for minute in (0, 5, 10, 15, 20, 25):
        ts_naive = dt.datetime(2026, 5, 28, 11, minute, 0)
        entries.append({"ts": ts_naive.strftime("%Y-%m-%d %H:%M:%S"), "level": "ERROR"})
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "abort"
    assert result["checks"]["recent_errors"]["ok"] is False
    assert result["checks"]["recent_errors"]["count_last_hour"] == 6
    assert "6 errors in last hour" in result["checks"]["recent_errors"]["reason"]


# ---------------------------------------------------------------------------
# 9. Broker session skip path
# ---------------------------------------------------------------------------


def test_broker_session_skip_when_no_primitive_available(monkeypatch):
    """When the broker primitive can't run, ``_check_broker_session()`` must
    return ok=True with a documented skip reason — preflight never aborts
    because we lack visibility into broker state.

    We test the helper directly. ``database.auth_db`` is swapped in
    ``sys.modules`` for a stub whose attribute access raises, simulating
    the primitive being unreachable (PEPPER missing at import time, table
    absent, etc).
    """
    import sys

    import database
    from services import preflight_service

    class _RaisingAuthModule:
        """Stub that raises on the attributes ``_check_broker_session``
        actually touches (Auth, db_session) but stays quiet for Python's
        import-system dunders."""

        __spec__ = None
        __name__ = "database.auth_db"

        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            raise RuntimeError(f"simulated: '{name}' unavailable")

    stub = _RaisingAuthModule()
    # ``from database import auth_db`` resolves via the package attribute
    # first (set by the conftest's eager import graph), so swap both that
    # and sys.modules to be safe.
    monkeypatch.setattr(database, "auth_db", stub)
    monkeypatch.setitem(sys.modules, "database.auth_db", stub)

    result = preflight_service._check_broker_session()

    assert result["ok"] is True
    assert result["broker"] is None
    assert result["user"] is None
    assert "check skipped" in result["reason"]


# ---------------------------------------------------------------------------
# 10. Heartbeat side-effect
# ---------------------------------------------------------------------------


def test_preflight_writes_heartbeat(preflight_env):
    """Every preflight call must leave a cycle_heartbeat row with stage='preflight'.

    Uses cycle_id=0 as a sentinel because ScanCycle ids autoincrement from 1
    and the cycle_heartbeat.cycle_id column is NOT NULL with no FK.
    """
    from services import preflight_service
    from services.preflight_service import PREFLIGHT_CYCLE_ID_SENTINEL

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    preflight_service.run_preflight()

    rows = (
        preflight_env.scdb.db_session.query(preflight_env.scdb.CycleHeartbeat)
        .filter_by(stage="preflight", cycle_id=PREFLIGHT_CYCLE_ID_SENTINEL)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "ok"
    # Detail is a JSON-encoded summary.
    assert rows[0].detail is not None
    parsed = json.loads(rows[0].detail)
    assert parsed["go_decision"] == "go"
    assert parsed["reasons"] == []


def test_preflight_writes_error_heartbeat_on_abort(preflight_env):
    """A failed preflight should still write a heartbeat — status='error'."""
    from services import preflight_service
    from services.preflight_service import PREFLIGHT_CYCLE_ID_SENTINEL

    # No intent → abort.

    preflight_service.run_preflight()

    rows = (
        preflight_env.scdb.db_session.query(preflight_env.scdb.CycleHeartbeat)
        .filter_by(stage="preflight", cycle_id=PREFLIGHT_CYCLE_ID_SENTINEL)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "error"
    parsed = json.loads(rows[0].detail)
    assert parsed["go_decision"] == "abort"
    assert any("no daily_intent" in r for r in parsed["reasons"])
