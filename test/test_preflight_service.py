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

    # ``get_recent_cycles`` uses real wallclock for its 24h cutoff, so cycles
    # inserted at the fixture's fake_now get filtered out once the real date
    # drifts past it. Anchor the cutoff to fake_now so the recent_cycles gate
    # observes the rows the tests insert. Mirrors the prod read-path but
    # against the in-memory engine bound above.
    def _fake_get_recent_cycles(hours: int = 24):
        cutoff = (fake_now - dt.timedelta(hours=hours)).isoformat()
        sess = scdb.db_session
        try:
            rows = (
                sess.query(scdb.ScanCycle)
                .filter(scdb.ScanCycle.started_at >= cutoff)
                .order_by(scdb.ScanCycle.started_at.desc())
                .all()
            )
            return [scdb._cycle_to_dict(r) for r in rows]
        finally:
            sess.remove()

    monkeypatch.setattr(
        "services.scan_cycle_service.get_recent_cycles", _fake_get_recent_cycles
    )

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


def _insert_preflight_heartbeat(scdb, ts_iso: str, status: str = "ok") -> None:
    """Direct-insert a stage='preflight' heartbeat row at a given ts.

    Mirrors what ``_write_preflight_heartbeat`` does in production, but
    lets a test plant heartbeats at arbitrary past/future timestamps to
    exercise the heartbeat-fallback gate.
    """
    from services.preflight_service import PREFLIGHT_CYCLE_ID_SENTINEL

    row = scdb.CycleHeartbeat(
        cycle_id=PREFLIGHT_CYCLE_ID_SENTINEL,
        stage="preflight",
        ts=ts_iso,
        status=status,
        detail=None,
    )
    scdb.db_session.add(row)
    scdb.db_session.commit()
    scdb.db_session.remove()


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

    # Write 11 errors within the last 60 min (default threshold is 10).
    # Note: errors.jsonl uses naive timestamps in the format
    # "YYYY-MM-DD HH:MM:SS" (local clock = IST on this host).
    entries = []
    for minute in (0, 3, 6, 9, 12, 15, 18, 21, 24, 27, 29):
        ts_naive = dt.datetime(2026, 5, 28, 11, minute, 0)
        entries.append({"ts": ts_naive.strftime("%Y-%m-%d %H:%M:%S"), "level": "ERROR"})
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "abort"
    assert result["checks"]["recent_errors"]["ok"] is False
    assert result["checks"]["recent_errors"]["count_last_hour"] == 11
    assert "11 errors in last" in result["checks"]["recent_errors"]["reason"]


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
    import database.auth_db  # noqa: F401 — ensure attr is bound before stubbing
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
    # ``from database import auth_db`` resolves via the package attribute,
    # so swap both that and sys.modules to be safe. The eager
    # ``import database.auth_db`` above guarantees the attribute exists on
    # the ``database`` package object — needed since the project-root
    # conftest no longer pre-imports the entire restx_api graph (which used
    # to bind every services/database submodule as a side effect).
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


# ---------------------------------------------------------------------------
# 11. Test-source entry filtering — keep pytest noise out of the rate gate
# ---------------------------------------------------------------------------


def _ts(minute: int) -> str:
    """Naive ts string inside the last hour (fake_now is 11:30 IST)."""
    return dt.datetime(2026, 5, 28, 11, minute, 0).strftime("%Y-%m-%d %H:%M:%S")


def test_recent_errors_excludes_pytest_traceback_entries(preflight_env):
    """Tracebacks pointing at ``test/test_*.py`` are excluded from the count."""
    from services import preflight_service

    prod = [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "broker.zerodha",
            "message": "broker hiccup",
        }
        for m in (1, 7, 13)
    ]
    test_entries = [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "services.signal_review",
            "message": "AssertionError",
            "exception": [
                "Traceback (most recent call last):\n",
                f'  File "test/test_foo.py", line {m}, in test_thing\n',
                "AssertionError\n",
            ],
        }
        for m in (2, 3, 4, 5, 6, 8, 9, 10)
    ]
    _write_errors_jsonl(preflight_env.tmp_path, prod + test_entries)

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["ok"] is True
    assert result["count_last_hour"] == 3


def test_recent_errors_excludes_unittest_mock_entries(preflight_env):
    """Tracebacks routed through ``unittest/mock`` are excluded."""
    from services import preflight_service

    entries = [
        {
            "ts": _ts(1),
            "level": "ERROR",
            "logger": "services.engine",
            "message": "real prod blowup",
        }
    ] + [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "services.engine",
            "message": "mocked side effect",
            "exception": [
                "Traceback (most recent call last):\n",
                '  File "unittest/mock.py", line 1, in __call__\n',
                "RuntimeError: boom\n",
            ],
        }
        for m in (2, 3, 4, 5)
    ]
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["ok"] is True
    assert result["count_last_hour"] == 1


def test_recent_errors_excludes_logger_starting_with_test_(preflight_env):
    """Entries whose logger looks like a pytest module are excluded."""
    from services import preflight_service

    entries = [
        {"ts": _ts(1), "level": "ERROR", "logger": "broker.zerodha", "message": "x"},
        {"ts": _ts(2), "level": "ERROR", "logger": "services.flow", "message": "y"},
    ] + [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "test_signal_review",
            "message": "synthetic",
        }
        for m in (3, 4, 5)
    ]
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["ok"] is True
    assert result["count_last_hour"] == 2


def test_recent_errors_excludes_synthetic_test_markers_in_message(preflight_env):
    """Synthetic marker phrases in the message also flag an entry as test-source."""
    from services import preflight_service

    entries = [
        {
            "ts": _ts(1),
            "level": "ERROR",
            "logger": "services.engine",
            "message": "simulated downstream failure",
        }
    ]
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["ok"] is True
    assert result["count_last_hour"] == 0


def test_recent_errors_default_threshold_is_now_10(preflight_env):
    """Default threshold bumped 5 → 10."""
    from services import preflight_service

    # No env override — exercise the default path explicitly.
    preflight_env.monkeypatch.delenv("PREFLIGHT_MAX_ERRORS_LAST_HOUR", raising=False)

    assert preflight_service.PREFLIGHT_MAX_ERRORS_LAST_HOUR_DEFAULT == 10

    # 7 prod entries — would have aborted under old default of 5, must pass now.
    entries = [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "broker.zerodha",
            "message": "transient",
        }
        for m in (1, 5, 10, 15, 20, 25, 28)
    ]
    _write_errors_jsonl(preflight_env.tmp_path, entries)

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["threshold"] == 10
    assert result["count_last_hour"] == 7
    assert result["ok"] is True


def test_is_test_source_entry_helper_unit_cases():
    """Table-driven coverage of :func:`_is_test_source_entry`."""
    from services.preflight_service import _is_test_source_entry

    pytest_tb = {
        "logger": "root",
        "message": "AssertionError",
        "exception": [
            "Traceback (most recent call last):\n",
            '  File "test/test_preflight_service.py", line 1, in test_x\n',
        ],
    }
    unittest_tb = {
        "logger": "services.engine",
        "exception": [
            "Traceback (most recent call last):\n",
            '  File "unittest/mock.py", line 1, in __call__\n',
        ],
    }
    pytest_in_tb = {
        "logger": "root",
        "exception": ["pytest: collected 1 item\n", "RuntimeError\n"],
    }
    prod_db_err = {
        "logger": "database.user_db",
        "message": "no such column",
        "exception": [
            "Traceback (most recent call last):\n",
            '  File "database/user_db.py", line 224, in find_user_by_username\n',
            "sqlite3.OperationalError\n",
        ],
    }
    prod_broker_err = {
        "logger": "zerodha_websocket",
        "message": "WebSocket error: Handshake status 403 Forbidden",
    }
    test_logger_strict = {
        "logger": "test_signal_review",
        "message": "fixture failure",
    }
    test_logger_single_segment = {
        "logger": "test_service",
        "message": "this is a real production service called test_service",
    }
    synthetic_marker = {
        "logger": "services.engine",
        "message": "engine blew up while replaying bogus-id",
    }
    plain_error = {
        "logger": "services.flow",
        "message": "regular runtime error from a flow node",
    }
    not_a_dict = "not a dict"

    cases = [
        (pytest_tb, True, "pytest traceback path"),
        (unittest_tb, True, "unittest/mock traceback path"),
        (pytest_in_tb, True, "'pytest' marker in traceback"),
        (prod_db_err, False, "real DB error from prod"),
        (prod_broker_err, False, "real broker error from prod"),
        (test_logger_strict, True, "test_<word>_<word> logger"),
        (test_logger_single_segment, False, "test_service is a real service"),
        (synthetic_marker, True, "synthetic 'engine blew up' marker"),
        (plain_error, False, "ordinary production error"),
        (not_a_dict, False, "non-dict input is safe"),
    ]

    for entry, expected, label in cases:
        assert _is_test_source_entry(entry) is expected, label


def test_excludes_synthetic_order_id_marker():
    """``OID-\\d+`` in the message flags an entry as test-source.

    Real broker order IDs are bare 16-digit numbers (e.g. ``250528000123``)
    — the ``OID-`` prefix is only ever produced by test fixtures.
    """
    from services.preflight_service import _is_test_source_entry

    e1 = {"logger": "services.engine", "message": "synthetic order OID-1 placed"}
    e2 = {"logger": "services.engine", "message": "rejecting OID-42 in dry-run"}
    assert _is_test_source_entry(e1) is True
    assert _is_test_source_entry(e2) is True


def test_excludes_python_locals_qualified_name():
    """``<locals>.`` (with trailing dot) flags an entry as test-source.

    Qualified names like ``test_foo.<locals>.<lambda>`` appear when a
    function (often a lambda) is defined inside another function — the
    canonical shape of a test fixture.
    """
    from services.preflight_service import _is_test_source_entry

    in_message = {
        "logger": "services.signal_review",
        "message": "callback test_tradebook_falls_through.<locals>.<lambda> raised",
    }
    in_traceback = {
        "logger": "services.signal_review",
        "message": "unhandled exception",
        "exception": [
            "Traceback (most recent call last):\n",
            '  File "x.py", line 1, in test_foo.<locals>.inner\n',
            "RuntimeError\n",
        ],
    }
    assert _is_test_source_entry(in_message) is True
    assert _is_test_source_entry(in_traceback) is True


def test_excludes_lambda_marker():
    """``<lambda>`` in the message flags an entry as test-source.

    Lambda mocks are an overwhelmingly test-suite construct; prod code
    rarely surfaces ``<lambda>`` in error messages.
    """
    from services.preflight_service import _is_test_source_entry

    entry = {
        "logger": "services.engine",
        "message": "callback <lambda> raised RuntimeError",
    }
    assert _is_test_source_entry(entry) is True


def test_does_not_exclude_real_stock_symbols():
    """Regression guard — real stock symbols in messages must NOT be filtered.

    Symbol names like ``RELIANCE`` can appear in genuine prod errors;
    excluding them would silently mask real failures.
    """
    from services.preflight_service import _is_test_source_entry

    entry = {
        "logger": "services.order_router",
        "message": "RELIANCE order placement failed: insufficient funds",
    }
    assert _is_test_source_entry(entry) is False


def test_does_not_exclude_real_broker_order_id():
    """Regression guard — bare 16-digit broker order IDs must NOT be filtered.

    Real broker order IDs (e.g. ``250528000123``) have no ``OID-`` prefix
    and must remain visible to the preflight gate.
    """
    from services.preflight_service import _is_test_source_entry

    entry = {
        "logger": "broker.zerodha",
        "message": "order 250528000123 rejected by exchange",
    }
    assert _is_test_source_entry(entry) is False


def test_does_not_exclude_real_aggregator_failure():
    """Regression guard — aggregator failures must NOT be filtered.

    ``MultiIntervalAggregator on_bar_close raised for RELIANCE/5m`` could
    happen in prod if a real callback raises; the preflight gate must
    still surface it.
    """
    from services.preflight_service import _is_test_source_entry

    entry = {
        "logger": "services.aggregator",
        "message": "MultiIntervalAggregator on_bar_close raised for RELIANCE/5m",
    }
    assert _is_test_source_entry(entry) is False


# ---------------------------------------------------------------------------
# 12. Preflight-heartbeat fallback — scheduler-liveness evidence
# ---------------------------------------------------------------------------


def test_recent_cycles_passes_when_preflight_heartbeats_exist_even_without_recent_scan_cycle(
    preflight_env,
):
    """Empty-screener day: scheduler is firing every cycle but neither
    BUY nor SELL produces a symbol, so no webhook POST and no scan_cycle
    row gets written. The freshness gate must NOT abort — preflight
    heartbeats are direct evidence the scheduler is alive.
    """
    from services import preflight_service

    _set_intent("live")
    # Last scan_cycle 45 min ago — would normally trip the staleness gate.
    stale = IST.localize(dt.datetime(2026, 5, 28, 10, 45, 0))
    _insert_cycle(preflight_env.scdb, stale.isoformat())

    # 3 preflight heartbeats inside the 30-min window (11:20, 11:25, 11:28).
    for minute in (20, 25, 28):
        ts = IST.localize(dt.datetime(2026, 5, 28, 11, minute, 0)).isoformat()
        _insert_preflight_heartbeat(preflight_env.scdb, ts)

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["preflight_heartbeats_in_window"] == 3
    assert (
        "preflight heartbeats present"
        in result["checks"]["recent_cycles"]["reason"]
    )
    # The stale scan_cycle is still reported in the diagnostic fields.
    assert result["checks"]["recent_cycles"]["minutes_since"] == 45
    assert result["go_decision"] == "go"


def test_recent_cycles_aborts_when_no_scan_cycle_and_no_recent_preflight_heartbeats(
    preflight_env,
):
    """Genuine scheduler death: last scan_cycle 45 min ago AND no preflight
    heartbeats in the staleness window. With both liveness signals absent,
    the gate must still abort — the fallback only bypasses when heartbeats
    are actually present.
    """
    from services import preflight_service

    _set_intent("live")
    # Stale scan_cycle 45 min ago.
    stale = IST.localize(dt.datetime(2026, 5, 28, 10, 45, 0))
    _insert_cycle(preflight_env.scdb, stale.isoformat())

    # One preflight heartbeat 50 min ago — outside the 30-min window, so
    # the fallback must not credit it as liveness evidence.
    old_ts = IST.localize(dt.datetime(2026, 5, 28, 10, 40, 0)).isoformat()
    _insert_preflight_heartbeat(preflight_env.scdb, old_ts)

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is False
    assert result["checks"]["recent_cycles"]["minutes_since"] == 45
    assert "scheduler may be stalled" in result["checks"]["recent_cycles"]["reason"]
    assert "preflight_heartbeats_in_window" not in result["checks"]["recent_cycles"]
    assert result["go_decision"] == "abort"


def test_recent_cycles_existing_recent_scan_cycle_still_passes_normally(
    preflight_env,
):
    """Regression guard: a fresh scan_cycle 5 min ago must pass via the
    existing path (minutes_since < threshold), not via the heartbeat
    fallback. ``preflight_heartbeats_in_window`` should NOT appear in the
    response — the fallback only triggers on the stale branch.
    """
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))  # 5 min ago
    _insert_cycle(preflight_env.scdb, recent.isoformat())
    # No preflight heartbeats inserted — recent scan_cycle alone must suffice.

    result = preflight_service.run_preflight()

    assert result["checks"]["recent_cycles"]["ok"] is True
    assert result["checks"]["recent_cycles"]["minutes_since"] == 5
    assert "preflight_heartbeats_in_window" not in result["checks"]["recent_cycles"]
    assert result["checks"]["recent_cycles"]["reason"] is None
    assert result["go_decision"] == "go"


# ---------------------------------------------------------------------------
# Daily circuit breaker integration
# ---------------------------------------------------------------------------


def test_daily_circuit_breaker_trips_yields_abort(preflight_env):
    """When risk_service trips, preflight must abort with a daily-limits reason."""
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    preflight_env.monkeypatch.setattr(
        "services.risk_service.daily_circuit_breaker_tripped",
        lambda: (True, "daily limits: 3 losses today (max 3)"),
    )

    result = preflight_service.run_preflight()

    assert result["ok"] is False
    assert result["go_decision"] == "abort"
    assert result["checks"]["daily_circuit_breaker"]["ok"] is False
    assert result["checks"]["daily_circuit_breaker"]["tripped"] is True
    assert any("daily limits" in r for r in result["reasons"])


def test_daily_circuit_breaker_clear_does_not_block(preflight_env):
    """Happy-path fixture already has the breaker clear (no engine running).

    Regression guard: the breaker check defaults to ok=True when the engine
    is unavailable, so the preflight go-path must still resolve to go.
    """
    from services import preflight_service

    _set_intent("live")
    recent = IST.localize(dt.datetime(2026, 5, 28, 11, 25, 0))
    _insert_cycle(preflight_env.scdb, recent.isoformat())

    result = preflight_service.run_preflight()

    assert result["go_decision"] == "go"
    assert result["checks"]["daily_circuit_breaker"]["ok"] is True
    assert result["checks"]["daily_circuit_breaker"]["tripped"] is False


# ---------------------------------------------------------------------------
# 13. Pre-market error filtering (before 09:15 IST) + configurable toggle
# ---------------------------------------------------------------------------


def _premarket_storm() -> list[dict]:
    """50 ERROR entries at 08:xx IST — the morning WS-reconnect storm."""
    return [
        {
            "ts": dt.datetime(2026, 5, 28, 8, m, 0).strftime("%Y-%m-%d %H:%M:%S"),
            "level": "ERROR",
            "logger": "zerodha_websocket",
            "message": "WebSocket reconnect storm",
        }
        for m in range(50)
    ]


def _intraday_errors() -> list[dict]:
    """5 ERROR entries at 11:2x IST — genuine intraday noise within window."""
    return [
        {
            "ts": _ts(m),
            "level": "ERROR",
            "logger": "broker.zerodha",
            "message": "intraday hiccup",
        }
        for m in (25, 26, 27, 28, 29)
    ]


def test_premarket_errors_excluded_during_market_hours(preflight_env):
    """50 pre-09:15 errors + 5 intraday → only the 5 count → ok (go).

    A wide rolling window is set so the window alone wouldn't drop the 08:xx
    storm — this isolates the pre-market filter as the thing excluding them.
    """
    from services import preflight_service

    preflight_env.monkeypatch.setenv("PREFLIGHT_ERROR_WINDOW_MIN", "600")
    preflight_env.monkeypatch.setenv("PREFLIGHT_IGNORE_PREMARKET_ERRORS", "1")
    _write_errors_jsonl(preflight_env.tmp_path, _premarket_storm() + _intraday_errors())

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["count_last_hour"] == 5
    assert result["ok"] is True  # 5 <= threshold 10 → contributes "go"


def test_premarket_filter_disabled_via_env(preflight_env):
    """With the filter off, all 55 errors count → over threshold → abort."""
    from services import preflight_service

    preflight_env.monkeypatch.setenv("PREFLIGHT_ERROR_WINDOW_MIN", "600")
    preflight_env.monkeypatch.setenv("PREFLIGHT_IGNORE_PREMARKET_ERRORS", "0")
    _write_errors_jsonl(preflight_env.tmp_path, _premarket_storm() + _intraday_errors())

    result = preflight_service._check_recent_errors(preflight_env.fake_now)

    assert result["count_last_hour"] == 55
    assert result["ok"] is False  # 55 > threshold 10 → contributes "abort"
