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


# ---------------------------------------------------------------------------
# Declared completion for window-scoped loops (issue #709)
# ---------------------------------------------------------------------------
#
# The open15 risk monitor is a `loop` thread whose lifetime is the armed day:
# `_risk_loop` returns by design once `day_status != "armed"`. Before #709 the
# registry only knew a normal exit for boot one-shots, so this thread read as
# DEAD from the 09:30 exit onwards and Telegrammed every 30 minutes.


def test_loop_that_declared_done_then_exited_is_completed_not_dead(monkeypatch):
    """armed -> beat -> normal exit with done() => completed, no alert."""
    alive = {"open15-risk-monitor"}
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set(alive))

    tr.beat("open15-risk-monitor")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_RUNNING

    tr.done("open15-risk-monitor")
    alive.clear()  # the loop returned; the thread is gone
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_COMPLETED
    assert rows["open15-risk-monitor"]["done_at"] is not None
    assert "open15-risk-monitor" not in {r["thread_name"] for r in tr.evaluate_alerts()}
    assert tr.summarize(list(rows.values()))["completed"] >= 1


def test_loop_that_vanished_without_done_still_alerts(monkeypatch):
    """armed -> beat -> silent vanish => still DEAD, still alerts.

    This is the #539 rule and it must survive #709 untouched: a loop that
    stops without declaring it is exactly the failure the registry exists
    to catch.
    """
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("open15-risk-monitor")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_DEAD
    assert "open15-risk-monitor" in {r["thread_name"] for r in tr.evaluate_alerts()}


def test_next_beat_clears_the_done_mark(monkeypatch):
    """Tomorrow's arm restarts the loop; yesterday's completion must not
    mask a silent death of the NEW run."""
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("open15-risk-monitor")
    tr.done("open15-risk-monitor")
    assert {r["thread_name"]: r for r in tr.snapshot()}["open15-risk-monitor"]["state"] == (
        tr.STATE_COMPLETED
    )

    tr.beat("open15-risk-monitor")  # new run
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_DEAD
    assert rows["open15-risk-monitor"]["done_at"] is None


def test_done_does_not_hide_a_thread_that_is_still_alive(monkeypatch):
    """A loop that says done and then hangs in its own teardown is judged on
    its heartbeat like any other alive thread."""
    monkeypatch.setattr(tr, "_live_thread_names", lambda: {"open15-risk-monitor"})
    clock = [1000.0]
    monkeypatch.setattr(tr.time, "monotonic", lambda: clock[0])
    tr.beat("open15-risk-monitor")
    tr.done("open15-risk-monitor")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_RUNNING

    clock[0] += 60 * 3 + 1  # past cadence 60 x multiplier 3
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["open15-risk-monitor"]["state"] == tr.STATE_STALE


def test_done_never_raises_on_unknown_name():
    tr.done("NotInTheCatalogAtAll")


def test_boot_oneshot_without_done_is_still_completed(monkeypatch):
    """Boot one-shots never needed done(); their contract is unchanged."""
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    tr.beat("ScannerAggregatorSeed")
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["ScannerAggregatorSeed"]["state"] == tr.STATE_COMPLETED


def test_open15_risk_loop_declares_done_on_its_return_path():
    """Anti-rot for the other half of #709.

    The registry side is only useful if `_risk_loop` actually calls
    `done("open15-risk-monitor")` when it returns — and NOT from a `finally`,
    which would disguise a crash exit as a completion.
    """
    src = (REPO_ROOT / "services" / "open15_breakout_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    loop_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_risk_loop"
        ),
        None,
    )
    assert loop_fn is not None, "_risk_loop no longer exists"

    def _is_done_call(node) -> bool:
        if not isinstance(node, ast.Call):
            return False
        label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        return (
            label == "done"
            and len(node.args) == 1
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "open15-risk-monitor"
        )

    assert any(_is_done_call(n) for n in ast.walk(loop_fn)), (
        "_risk_loop must call done('open15-risk-monitor') on its normal exit"
    )
    for node in ast.walk(loop_fn):
        if isinstance(node, ast.Try):
            for stmt in node.finalbody:
                assert not any(_is_done_call(n) for n in ast.walk(stmt)), (
                    "done() must not live in a finally: a crash exit must still read as dead"
                )


def test_unregistered_live_thread_is_surfaced(monkeypatch):
    monkeypatch.setattr(tr, "_live_thread_names", lambda: {"SomeBrandNewThread"})
    rows = {r["thread_name"]: r for r in tr.snapshot()}
    assert rows["SomeBrandNewThread"]["group"] == "unregistered"


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
    """A broken notifier must not take down the watchdog loop hosting this.

    Asserts membership rather than an exact count: ``_beats`` is process-global,
    so when a real registry-beating daemon (e.g. TickLivenessWatchdog) is alive
    elsewhere in the same test process — which happens under the parallelized CI
    run — it can add a second degraded row between the reset fixture and this
    call. The invariants that matter are that the broken notifier is swallowed
    (a list is returned, not an exception) and the thread we beat is reported.
    """
    monkeypatch.setattr(tr, "_live_thread_names", lambda: set())
    monkeypatch.setattr(
        "services.notification_service.get_notification_service",
        lambda: (_ for _ in ()).throw(RuntimeError("telegram down")),
    )
    tr.beat("TickLivenessWatchdog")
    fired = tr.check_and_alert(now=0.0)
    assert "TickLivenessWatchdog" in {row["thread_name"] for row in fired}


# ---------------------------------------------------------------------------
# Anti-rot: the catalog must match the source
# ---------------------------------------------------------------------------


def _thread_name_literals(path: Path) -> set[str]:
    """Every string literal passed as ``name=`` to a Thread(...) call."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # pragma: no cover - defensive
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        label = getattr(func, "attr", None) or getattr(func, "id", None)
        if label != "Thread":
            continue
        for kw in node.keywords:
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                if isinstance(kw.value.value, str):
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
    boot_source = (REPO_ROOT / "app.py").read_text(encoding="utf-8", errors="ignore")
    missing: list[str] = []

    for spec in tr.CATALOG:
        owner = REPO_ROOT / spec.owner
        if not owner.exists():
            missing.append(f"{spec.thread_name}: owner {spec.owner} does not exist")
            continue
        if spec.thread_name in _thread_name_literals(owner):
            continue
        owner_source = owner.read_text(encoding="utf-8", errors="ignore")
        if spec.thread_name in owner_source or spec.thread_name in boot_source:
            continue
        missing.append(f"{spec.thread_name}: not found in {spec.owner} or app.py")

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
