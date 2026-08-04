"""Tests for services/thread_registry.py.

Two kinds of test here:

* behaviour — heartbeat states, staleness, the deliberately-narrow alert policy;
* **anti-rot** — the catalog is a hand-maintained list, so a test asserts every
  entry still matches a real ``threading.Thread(name=...)`` literal in its owner
  module. Without it the registry silently drifts the first time someone renames
  a thread, and a drifted registry reports a healthy thread as dead.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services import thread_registry as tr

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_registry():
    tr.reset_for_tests()
    yield
    tr.reset_for_tests()


# ---------------------------------------------------------------------------
# Heartbeat behaviour
# ---------------------------------------------------------------------------


def test_beat_records_and_counts():
    tr.beat("TickLivenessWatchdog")
    tr.beat("TickLivenessWatchdog")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["beat_count"] == 2


def test_beat_never_raises_on_unknown_name():
    """beat() is on the hot path of a 5-second loop; it must never throw."""
    tr.beat("NotInTheCatalogAtAll")


def test_alive_and_beating_is_running(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: {"TickLivenessWatchdog"})
    tr.beat("TickLivenessWatchdog")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["state"] == tr.STATE_RUNNING


def test_alive_but_silent_past_the_deadline_is_stale(monkeypatch):
    """A thread wedged on a socket keeps is_alive() True forever.

    That is the whole reason the heartbeat exists, so it is asserted directly:
    the thread is present in the live set the entire time.
    """
    monkeypatch.setattr(tr, "_live_thread_names", lambda: {"TickLivenessWatchdog"})

    clock = [1000.0]
    monkeypatch.setattr(tr.time, "monotonic", lambda: clock[0])
    tr.beat("TickLivenessWatchdog")

    # Cadence is 30s and the default multiplier is 3 -> stale past 90s.
    clock[0] += 60
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["state"] == tr.STATE_RUNNING

    clock[0] += 60
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["state"] == tr.STATE_STALE


def test_beat_then_vanished_is_dead(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("TickLivenessWatchdog")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["state"] == tr.STATE_DEAD


def test_never_beat_is_not_started_not_dead(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["TickLivenessWatchdog"]["state"] == tr.STATE_NOT_STARTED


def test_boot_oneshot_that_exited_is_completed_not_dead(monkeypatch):
    """A one-shot that finished is a success. Reporting it as dead would make
    every healthy boot look like a failure."""
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("ScannerAggregatorSeed")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["ScannerAggregatorSeed"]["state"] == tr.STATE_COMPLETED


def test_unregistered_live_thread_is_surfaced(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: {"SomeBrandNewThread"})
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["SomeBrandNewThread"]["group"] == "unregistered"


def test_runtime_threads_are_filtered_out_of_the_uncatalogued_bucket(monkeypatch):
    """The "not in the catalog" bucket is a to-do list, so it must not contain
    threads nobody would ever catalog.

    The live process contributes ~24 of these (ThreadPoolExecutor-*,
    Thread-9322 (process_request_thread), eventbus_*, MainThread, ...), which
    buried the one genuinely uncatalogued application thread.
    """
    noise = {
        "MainThread",
        "APScheduler",
        "Tornado selector",
        "ThreadPoolExecutor-8_1",
        "Thread-9322 (process_request_thread)",
        "asyncio_0",
        "eventbus_3",
        "openalgo-claude-review",
        "connect-cb-scanner",
    }
    monkeypatch.setattr(tr, "_live_thread_names", lambda: noise | {"RealAppThread"})

    rows = tr.snapshot()
    unregistered = [r["thread_name"] for r in rows if r["group"] == "unregistered"]
    assert unregistered == ["RealAppThread"]

    # Filtered, never silently dropped.
    assert tr.summarize(rows)["runtime_suppressed"] == len(noise)


# ---------------------------------------------------------------------------
# Alert policy
# ---------------------------------------------------------------------------


def test_never_started_threads_never_alert(monkeypatch):
    """The single most important alerting property.

    On a normal install most catalog threads are legitimately absent (no broker
    session, outside the window, flag off). If those alerted, the channel would
    be noise every day and the operator would learn to ignore it.
    """
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    assert tr.evaluate_alerts() == []


def test_dead_thread_alerts_once_then_dedups(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    published: list[str] = []

    class _FakeNotifier:
        def notify(self, event, message, **kw):
            published.append(message)

    monkeypatch.setattr(
        "services.notification_service.get_notification_service",
        lambda: _FakeNotifier(),
    )

    tr.beat("TickLivenessWatchdog")

    fired = tr.check_and_alert(now=0.0)
    assert len(fired) == 1
    assert len(published) == 1

    # Still degraded a minute later, but inside the dedup window.
    assert tr.check_and_alert(now=60.0) == []
    assert len(published) == 1

    # Past the 30-minute default window it reminds.
    again = tr.check_and_alert(now=60.0 + 31 * 60)
    assert len(again) == 1
    assert len(published) == 2


def test_check_and_alert_respects_the_master_flag(monkeypatch):
    monkeypatch.setenv("THREAD_REGISTRY_ENABLED", "false")
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("TickLivenessWatchdog")
    assert tr.check_and_alert() == []


def test_alert_failure_does_not_propagate(monkeypatch):
    """A broken notifier must not take down the watchdog loop hosting this."""
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    monkeypatch.setattr(
        "services.notification_service.get_notification_service",
        lambda: (_ for _ in ()).throw(RuntimeError("telegram down")),
    )
    tr.beat("TickLivenessWatchdog")
    assert len(tr.check_and_alert(now=0.0)) == 1


# ---------------------------------------------------------------------------
# Anti-rot: the catalog must match the source
# ---------------------------------------------------------------------------


def _thread_name_literals(path: Path) -> set[str]:
    """Thread names a module actually constructs.

    Collects ``name=`` string literals on ``Thread(...)`` calls, plus
    ``thread_name=`` literals anywhere (a few threads are built by a helper
    that receives the name from the boot site — ``scanner_presubscribe``).

    Deliberately does **not** fall back to a plain substring search of the
    file. That looseness let ``futures_follow_rehydrate`` — an event-bus
    subscription named with the same keyword, not a thread at all — sit in the
    catalog, where it could only ever render as a permanently "not started"
    thread that does not exist.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # pragma: no cover - defensive
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for kw in node.keywords:
            if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
                continue
            if kw.arg == "name" and label == "Thread":
                names.add(kw.value.value)
            elif kw.arg == "thread_name":
                names.add(kw.value.value)
    return names


def test_every_catalog_thread_name_exists_in_its_owner_module():
    """Guards against a rename silently orphaning a catalog entry.

    ``owner`` points at where the thread's *work* lives, which is the useful
    thing for an operator to read. For a few threads that is not where the name
    literal sits: ``scanner_presubscribe`` builds the thread but ``app.py``
    supplies the name via ``thread_name=``. Those are allowed to match in
    ``app.py`` instead, which still catches a rename.
    """
    boot_names = _thread_name_literals(REPO_ROOT / "app.py")
    missing: list[str] = []

    for spec in tr.CATALOG:
        owner = REPO_ROOT / spec.owner
        if not owner.exists():
            missing.append(f"{spec.thread_name}: owner {spec.owner} does not exist")
            continue
        if spec.thread_name in _thread_name_literals(owner) or spec.thread_name in boot_names:
            continue
        missing.append(f"{spec.thread_name}: no Thread(name=...) literal in {spec.owner} or app.py")

    assert not missing, "Catalog entries no longer match the source:\n" + "\n".join(missing)


def test_catalog_has_no_duplicate_names():
    names = [s.thread_name for s in tr.CATALOG]
    assert len(names) == len(set(names))


def test_every_loop_declares_a_cadence():
    """Staleness is meaningless without one, so a loop without a cadence would
    silently never be checked."""
    for spec in tr.CATALOG:
        if spec.group == tr.GROUP_LOOP:
            assert spec.cadence_sec, f"{spec.thread_name} is a loop with no cadence"


def test_registry_is_read_only():
    """Phase 1 observes. Nothing here may start, stop or join a thread.

    Parsed rather than grepped: the module's own docstrings mention
    ``threading.Thread(name=...)``, and a substring check would fire on the
    prose instead of on real calls.
    """
    tree = ast.parse((REPO_ROOT / "services" / "thread_registry.py").read_text(encoding="utf-8"))
    forbidden = {"start", "join", "Thread", "setDaemon", "_bootstrap"}
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    offenders = sorted(forbidden & {c for c in called if c})
    assert not offenders, f"thread_registry must not call: {offenders}"
