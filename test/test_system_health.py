"""Tests for machine-wide system health (issue #744).

Covers the pure logic in ``utils/system_health.py`` (free-up-memory advice,
sustain window + alert state machine, unclean-exit detection, the Windows
crash-log parser), the ``health_metrics`` migration and run-tail reader, and
the non-destructive ``/health/api/system`` gate.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta

os.environ.setdefault("API_KEY_PEPPER", "0" * 64)
os.environ.setdefault("APP_KEY", "0" * 64)

import pytest  # noqa: E402
from flask import Flask  # noqa: E402

from utils import system_health as sh  # noqa: E402

# 2026-09-24 03:00 UTC = 08:30 IST
T0 = datetime(2026, 9, 24, 3, 0, tzinfo=UTC).timestamp()


def _cfg(**kw):
    base = {"alerts_enabled": True, "sustain_samples": 3}
    base.update(kw)
    return sh.SystemHealthConfig(**base)


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def __call__(self, event_type, message):
        assert event_type == "system_health"
        self.sent.append(message)


def _monitor(**kw):
    n = _Notifier()
    m = sh.SystemHealthMonitor(config=_cfg(**kw), notifier=n, db_alerts=False)
    return m, n


def _reading(avail=2000.0, nonpaged=500.0, tcp=100):
    return {"available_mb": avail, "nonpaged_mb": nonpaged, "tcp_system": tcp}


# ---------------------------------------------------------------------------
# classify_apps — recommend only, never list OpenAlgo or Windows itself
# ---------------------------------------------------------------------------

PROCS = [
    {"pid": 1, "name": "chrome.exe", "rss_mb": 400},
    {"pid": 2, "name": "chrome.exe", "rss_mb": 332},
    {"pid": 3, "name": "msedge.exe", "rss_mb": 155},
    {"pid": 4, "name": "WhatsApp.Root.exe", "rss_mb": 67},
    {"pid": 5, "name": "claude.exe", "rss_mb": 836},
    {"pid": 6, "name": "vmmem", "rss_mb": 142},
    {"pid": 7, "name": "svchost.exe", "rss_mb": 900},
    {"pid": 8, "name": "MsMpEng.exe", "rss_mb": 240},
    {"pid": 9, "name": "python.exe", "rss_mb": 1300},  # OpenAlgo itself
    {"pid": 10, "name": "powershell.exe", "rss_mb": 90},  # OpenAlgo's parent shell
    {"pid": 11, "name": "mystery.exe", "rss_mb": 450},
    {"pid": 12, "name": "tiny.exe", "rss_mb": 40},
]


def test_classify_never_lists_openalgo_its_ancestors_or_windows():
    out = sh.classify_apps(PROCS, own_pids={9, 10}, available_mb=1000, target_mb=1500)
    names = {g["app"] for k in ("safe", "careful", "unknown") for g in out[k]}
    assert "Other Python processes" not in names  # the only python is OpenAlgo
    assert "Terminal windows" not in names  # the only shell runs OpenAlgo
    assert not {"svchost", "msmpeng"} & {n.lower() for n in names}


def test_classify_groups_and_ranks_by_working_set():
    out = sh.classify_apps(PROCS, own_pids={9, 10}, available_mb=1000, target_mb=1500)
    safe = out["safe"]
    assert [g["app"] for g in safe] == ["Chrome", "Microsoft Edge", "WhatsApp"]
    assert safe[0]["rss_mb"] == 732 and safe[0]["processes"] == 2
    careful = [g["app"] for g in out["careful"]]
    assert careful == ["Claude app", "Claude Cowork VM (Hyper-V)"]
    assert [g["app"] for g in out["unknown"]] == ["mystery"]  # tiny.exe < 300 MB


def test_classify_suggests_safe_apps_until_target_reached():
    out = sh.classify_apps(PROCS, own_pids={9, 10}, available_mb=1000, target_mb=1500)
    assert out["need_mb"] == 500
    chrome, edge, _ = out["safe"]
    assert chrome["suggested"] and chrome["reaches_target"]
    assert not edge["suggested"]


def test_classify_nothing_suggested_when_ram_is_fine():
    out = sh.classify_apps(PROCS, own_pids=set(), available_mb=3000, target_mb=1500)
    assert out["need_mb"] == 0
    assert not any(g["suggested"] for g in out["safe"])


def test_classify_unknown_available_does_not_crash():
    out = sh.classify_apps(PROCS, own_pids=set(), available_mb=None, target_mb=1500)
    assert out["need_mb"] is None
    assert not any(g["suggested"] for g in out["safe"])


# ---------------------------------------------------------------------------
# Sustain window + alert state machine
# ---------------------------------------------------------------------------


def test_warn_needs_sustained_samples():
    m, n = _monitor()
    for i in range(2):
        m.evaluate(_reading(avail=350), now=T0 + i * 10)
    assert m.latest["status"] == "pass" and n.sent == []
    m.evaluate(_reading(avail=350), now=T0 + 20)
    assert m.latest["status"] == "warn"
    assert len(n.sent) == 1 and n.sent[0].startswith("WARN system_health")


def test_blip_does_not_alert():
    m, n = _monitor()
    for i, avail in enumerate([350, 350, 900, 350, 350, 900]):
        m.evaluate(_reading(avail=avail), now=T0 + i * 10)
    assert n.sent == [] and m.latest["status"] == "pass"


def test_escalation_then_recovery_sends_crit_and_recovered_once():
    m, n = _monitor()
    t = T0
    for avail in [350] * 3 + [200] * 3 + [350] * 3 + [900] * 3:
        m.evaluate(_reading(avail=avail), now=t)
        t += 10
    kinds = [msg.split(" ", 1)[0] for msg in n.sent]
    # WARN, CRIT, (fail->warn is not news), RECOVERED
    assert kinds == ["WARN", "CRIT", "RECOVERED"]
    assert "libzmq 10055" in n.sent[1]


def test_same_level_alerts_once_per_ist_day():
    m, n = _monitor()
    t = T0
    for avail in [350] * 3 + [900] * 3 + [350] * 3:
        m.evaluate(_reading(avail=avail), now=t)
        t += 10
    assert [x.split(" ", 1)[0] for x in n.sent] == ["WARN", "RECOVERED"]
    # next IST day: the same WARN goes out again
    t = T0 + 86400
    for avail in [900] * 3 + [350] * 3:
        m.evaluate(_reading(avail=avail), now=t)
        t += 10
    assert n.sent[-1].startswith("WARN")


def test_alerts_disabled_still_tracks_status_but_sends_nothing():
    m, n = _monitor(alerts_enabled=False)
    for i in range(3):
        m.evaluate(_reading(avail=200), now=T0 + i * 10)
    assert m.latest["status"] == "fail"
    assert n.sent == []


def test_nonpaged_size_and_tcp_rules():
    m, _ = _monitor()
    for i in range(3):
        m.evaluate(_reading(nonpaged=1600, tcp=2500), now=T0 + i * 10)
    rules = m.latest["rules"]
    assert rules["nonpaged"]["level"] == "fail"
    assert rules["tcp"]["level"] == "warn"
    assert m.latest["status"] == "fail"


def test_nonpaged_growth_warns_only_with_an_hour_of_history():
    m, _ = _monitor()
    # +150 MB over an hour (below the 1 GB size threshold)
    for i in range(0, 361):
        m.evaluate(_reading(nonpaged=600 + i * 150 / 360), now=T0 + i * 10)
    assert m.latest["readings"]["nonpaged_growth_mb_per_h"] == pytest.approx(150, rel=0.05)
    assert m.latest["rules"]["nonpaged"]["level"] == "warn"

    m2, _ = _monitor()
    for i in range(0, 60):  # only 10 minutes of history: no trend yet
        m2.evaluate(_reading(nonpaged=600 + i * 5), now=T0 + i * 10)
    assert m2.latest["readings"]["nonpaged_growth_mb_per_h"] is None
    assert m2.latest["rules"]["nonpaged"]["level"] == "pass"


def test_alert_names_the_app_that_reaches_target():
    m, n = _monitor()
    apps = sh.classify_apps(PROCS, own_pids={9, 10}, available_mb=200, target_mb=1500)
    for i in range(3):
        m.evaluate(_reading(avail=200), now=T0 + i * 10, apps=apps if i == 0 else None)
    assert "Free now: Chrome" in n.sent[0]


def test_missing_readings_are_pass_not_alarm():
    m, n = _monitor()
    for i in range(3):
        m.evaluate({"available_mb": None, "nonpaged_mb": None, "tcp_system": None}, now=T0 + i)
    assert m.latest["status"] == "pass" and n.sent == []


# ---------------------------------------------------------------------------
# Unclean-exit detection
# ---------------------------------------------------------------------------


def test_first_boot_has_no_report(tmp_path):
    tracker = sh.RunStateTracker(str(tmp_path))
    assert tracker.begin() is None
    state = json.loads((tmp_path / "health_run_state.json").read_text())
    assert state["status"] == "running" and state["pid"] == os.getpid()


def test_clean_shutdown_is_not_reported(tmp_path):
    first = sh.RunStateTracker(str(tmp_path))
    first.begin()
    first.mark_clean()
    # simulate the next process
    state = json.loads((tmp_path / "health_run_state.json").read_text())
    state["pid"] = 999999
    (tmp_path / "health_run_state.json").write_text(json.dumps(state))
    assert sh.RunStateTracker(str(tmp_path)).begin() is None


def _write_dead_run(tmp_path, pid=0x69C8):
    started = datetime(2026, 9, 24, 3, 1, tzinfo=UTC).isoformat()
    (tmp_path / "health_run_state.json").write_text(
        json.dumps({"status": "running", "pid": pid, "started_at": started})
    )


def test_unclean_exit_report_banner_and_alert(tmp_path, monkeypatch):
    _write_dead_run(tmp_path)

    def tail(started):
        assert started == datetime(2026, 9, 24, 3, 1, tzinfo=UTC)
        return {
            "last_sample": {"at": "2026-09-24T04:11:07+00:00", "available_mb": 620.0},
            "min_available_mb": 236.8,
            "min_available_at": "2026-09-24T04:10:01+00:00",
            "top_apps": [{"app": "Claude app", "rss_mb": 1600.0, "class": "careful"}],
        }

    tracker = sh.RunStateTracker(str(tmp_path))
    report = tracker.begin(sample_reader=tail)
    assert report["previous_pid"] == 0x69C8 and report["crash"] is None
    assert tracker.current_report()["min_available_mb"] == 236.8

    # the alert waits for uptime so the Telegram bot is up
    assert tracker.take_pending_alert(min_uptime_s=3600) is None

    def finder(pid, since):
        assert pid == 0x69C8
        return {
            "application": "python.exe",
            "module": "KERNELBASE.dll",
            "exception_code": "0x40000015",
        }

    alert = tracker.take_pending_alert(min_uptime_s=0, crash_finder=finder)
    assert alert["crash"]["meaning"].startswith("abort()")
    assert tracker.take_pending_alert(min_uptime_s=0) is None  # once only
    # the banner report now carries the crash record too
    assert tracker.current_report()["crash"]["exception_code"] == "0x40000015"

    text = sh.RunStateTracker.format_alert(alert)
    assert "unclean exit" in text
    assert "0x40000015" in text and "KERNELBASE.dll" in text
    assert "09:41:07" in text  # last sample, shown in IST
    assert "non-paged pool n/a" in text and "n/a MB" not in text
    assert "237 MB at 09:40:01" in text
    assert "Claude app 1.6 GB" in text


def test_killed_process_says_no_crash_record(tmp_path):
    _write_dead_run(tmp_path)
    tracker = sh.RunStateTracker(str(tmp_path))
    tracker.begin()
    alert = tracker.take_pending_alert(min_uptime_s=0, crash_finder=lambda pid, since: None)
    assert "No Windows crash record" in sh.RunStateTracker.format_alert(alert)


def test_dismiss_hides_the_banner(tmp_path):
    _write_dead_run(tmp_path)
    tracker = sh.RunStateTracker(str(tmp_path))
    tracker.begin()
    assert tracker.current_report() is not None
    assert tracker.dismiss() is True
    assert tracker.current_report() is None


def test_old_report_expires(tmp_path):
    _write_dead_run(tmp_path)
    tracker = sh.RunStateTracker(str(tmp_path))
    tracker.begin()
    report = json.loads((tmp_path / "health_unclean_exit.json").read_text())
    report["detected_at"] = (datetime.now(UTC) - timedelta(hours=30)).isoformat()
    (tmp_path / "health_unclean_exit.json").write_text(json.dumps(report))
    assert tracker.current_report() is None


# ---------------------------------------------------------------------------
# Windows crash-log parser (real wevtutil /f:text shape, 2026-09-24)
# ---------------------------------------------------------------------------

WEVTUTIL_TEXT = """Event[0]
  Log Name: Application
  Source: SomeOtherService
  Date: 2026-09-24T12:28:07.1810000Z
  Event ID: 1000
  Description:
N/A

Event[1]
  Log Name: Application
  Source: Application Error
  Date: 2026-09-24T09:41:18.8760000Z
  Event ID: 1000
  Description:
Faulting application name: python.exe, version: 0.0.0.0, time stamp: 0x69de4fec
Faulting module name: KERNELBASE.dll, version: 10.0.26100.9444, time stamp: 0x28606b21
Exception code: 0x40000015
Fault offset: 0x00000000000c41ca
Faulting process id: 0x69C8
"""


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout


def test_crash_parser_matches_pid(monkeypatch):
    monkeypatch.setattr(sh, "IS_WINDOWS", True)
    calls = []

    def runner(argv, **kw):
        calls.append(argv)
        return _Completed(WEVTUTIL_TEXT)

    since = datetime(2026, 9, 24, 3, 1, tzinfo=UTC)
    crash = sh.find_windows_crash(0x69C8, since, runner=runner)
    assert crash == {
        "time_local": "2026-09-24T09:41:18.8760000",
        "application": "python.exe",
        "module": "KERNELBASE.dll",
        "exception_code": "0x40000015",
    }
    assert "TimeCreated[@SystemTime>='2026-09-24T03:01:00.000Z']" in calls[0][3]
    assert sh.find_windows_crash(1234, since, runner=runner) is None


def test_crash_parser_fails_soft(monkeypatch):
    monkeypatch.setattr(sh, "IS_WINDOWS", True)

    def boom(*a, **kw):
        raise OSError("wevtutil missing")

    assert sh.find_windows_crash(1, datetime.now(UTC), runner=boom) is None


# ---------------------------------------------------------------------------
# health_metrics migration + run-tail reader
# ---------------------------------------------------------------------------


def test_migration_adds_missing_columns_and_is_idempotent(tmp_path):
    from sqlalchemy import create_engine, inspect, text

    from database.health_db import ensure_health_metric_columns

    eng = create_engine(f"sqlite:///{tmp_path / 'old_health.db'}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE health_metrics (id INTEGER PRIMARY KEY, fd_count INTEGER)"))
    added = ensure_health_metric_columns(eng)
    assert {"sys_nonpaged_mb", "sys_top_apps", "sys_status", "memory_available_mb"} <= set(added)
    cols = {c["name"] for c in inspect(eng).get_columns("health_metrics")}
    assert "sys_nonpaged_mb" in cols
    assert ensure_health_metric_columns(eng) == []
    eng.dispose()


def test_read_run_tail_summarises_previous_run():
    from database.health_db import HealthMetric, health_session, read_run_tail

    base = datetime(2026, 9, 24, 4, 0)  # naive UTC, as SQLite stores it
    rows = [
        (base, 533.0, None),
        (base + timedelta(minutes=8), 334.0, None),
        (base + timedelta(minutes=10, seconds=1), 236.8, [{"app": "Chrome", "rss_mb": 700.0}]),
        (base + timedelta(minutes=11, seconds=7), 620.0, None),
    ]
    try:
        health_session.query(HealthMetric).delete()
        for ts, avail, apps in rows:
            health_session.add(
                HealthMetric(
                    timestamp=ts,
                    memory_available_mb=avail,
                    sys_nonpaged_mb=800.0,
                    sys_top_apps=apps,
                )
            )
        health_session.commit()
        tail = read_run_tail(datetime(2026, 9, 24, 3, 1, tzinfo=UTC))
        assert tail["last_sample"]["available_mb"] == 620.0
        assert tail["last_sample"]["at"].startswith("2026-09-24T04:11:07")
        assert tail["min_available_mb"] == 236.8  # within the final 5 minutes
        assert tail["top_apps"] == [{"app": "Chrome", "rss_mb": 700.0}]
    finally:
        health_session.query(HealthMetric).delete()
        health_session.commit()
        health_session.remove()


# ---------------------------------------------------------------------------
# /health/api/system — polled from the navbar, so its gate must be harmless
# ---------------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch):
    import blueprints.health as bp

    monkeypatch.setattr(
        bp, "get_system_snapshot", lambda: {"enabled": True, "status": "warn", "unclean_exit": None}
    )
    monkeypatch.setattr(bp, "dismiss_unclean_exit", lambda: True)
    app = Flask(__name__)
    app.secret_key = "test"  # pragma: allowlist secret
    app.config["TESTING"] = True
    app.register_blueprint(bp.health_bp)
    return app.test_client()


def test_system_endpoint_rejects_without_touching_the_session(client):
    with client.session_transaction() as s:
        s["some_state"] = "keep-me"
    r = client.get("/health/api/system")
    assert r.status_code == 401
    with client.session_transaction() as s:
        assert s.get("some_state") == "keep-me"  # nothing cleared, nothing revoked


def test_system_endpoint_works_before_broker_login(client):
    # password-logged-in only: "user" set, "logged_in" absent (the /broker page stage)
    with client.session_transaction() as s:
        s["user"] = "admin"
    r = client.get("/health/api/system")
    assert r.status_code == 200 and r.get_json()["status"] == "warn"


def test_dismiss_endpoint(client):
    assert client.post("/health/api/system/unclean_exit/dismiss").status_code == 401
    with client.session_transaction() as s:
        s["user"] = "admin"
    assert client.post("/health/api/system/unclean_exit/dismiss").get_json() == {"dismissed": True}
