"""Tests for services/scheduler_registry.py.

The load-bearing test in this file is
:func:`test_every_add_job_id_in_the_source_is_catalogued`. The catalog is
hand-maintained, and the whole point of the page is to answer "what runs?" — a
catalog that quietly falls behind the code answers it wrongly, which is worse
than not answering at all. So the source is walked for ``add_job(..., id="X")``
literals and the test fails when one is not covered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from services import scheduler_registry as sr

REPO_ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("services", "blueprints", "sandbox")


class _FakeJob:
    def __init__(self, job_id, name=None, next_run_time=None, trigger="cron[...]"):
        self.id = job_id
        self.name = name
        self.next_run_time = next_run_time
        self.trigger = trigger


class _FakeScheduler:
    def __init__(self, jobs):
        self._jobs = jobs

    def get_jobs(self):
        return self._jobs


@pytest.fixture
def _no_live(monkeypatch):
    monkeypatch.setattr(sr, "_resolve_schedulers", lambda: [])
    monkeypatch.setattr(sr, "_last_run", lambda job_id: None)


# ---------------------------------------------------------------------------
# Merge behaviour
# ---------------------------------------------------------------------------


def test_catalogued_but_absent_job_is_not_registered(_no_live):
    """The row an operator most wants: scheduled in principle, absent in fact."""
    rows = {r["job_id"]: r for r in sr.snapshot()}
    assert rows["futures_follow_entry"]["state"] == sr.STATE_NOT_REGISTERED
    assert rows["futures_follow_entry"]["next_run_time"] is None


def test_live_catalogued_job_is_registered(monkeypatch):
    monkeypatch.setattr(sr, "_last_run", lambda job_id: None)
    monkeypatch.setattr(
        sr,
        "_resolve_schedulers",
        lambda: [(sr.SCHED_SHARED, _FakeScheduler([_FakeJob("futures_follow_entry")]))],
    )
    rows = {r["job_id"]: r for r in sr.snapshot()}
    assert rows["futures_follow_entry"]["state"] == sr.STATE_REGISTERED
    assert rows["futures_follow_entry"]["tier"] == sr.TIER_GUARDED
    # The T+1 exit is the one that must never become toggleable.
    assert rows["futures_follow_exit"]["tier"] == sr.TIER_PROTECTED


def test_live_unknown_job_is_unregistered_not_dropped(monkeypatch):
    """User-defined historify / python / flow / chartink schedules land here.

    They must appear on the page without needing a catalog entry each.
    """
    monkeypatch.setattr(sr, "_last_run", lambda job_id: None)
    monkeypatch.setattr(
        sr,
        "_resolve_schedulers",
        lambda: [(sr.SCHED_PYTHON, _FakeScheduler([_FakeJob("user_strategy_17")]))],
    )
    rows = {r["job_id"]: r for r in sr.snapshot()}
    assert rows["user_strategy_17"]["state"] == sr.STATE_UNREGISTERED
    assert rows["user_strategy_17"]["group"] == "user"


def test_dynamic_job_family_matches_by_prefix(monkeypatch):
    """eod_watchdog_<strategy> and squareoff_<config> are named at runtime."""
    monkeypatch.setattr(sr, "_last_run", lambda job_id: None)
    monkeypatch.setattr(
        sr,
        "_resolve_schedulers",
        lambda: [
            (
                sr.SCHED_EOD_WATCHDOG,
                _FakeScheduler([_FakeJob("eod_watchdog_trending_equity_intraday")]),
            )
        ],
    )
    rows = {r["job_id"]: r for r in sr.snapshot()}
    row = rows["eod_watchdog_trending_equity_intraday"]
    assert row["state"] == sr.STATE_REGISTERED
    assert row["tier"] == sr.TIER_PROTECTED
    assert row["safety_note"]


def test_a_broken_scheduler_does_not_kill_the_snapshot(monkeypatch):
    class _Exploding:
        def get_jobs(self):
            raise RuntimeError("jobstore gone")

    monkeypatch.setattr(sr, "_last_run", lambda job_id: None)
    monkeypatch.setattr(
        sr,
        "_resolve_schedulers",
        lambda: [
            (sr.SCHED_SANDBOX, _Exploding()),
            (sr.SCHED_SHARED, _FakeScheduler([_FakeJob("postmarket_review")])),
        ],
    )
    rows = {r["job_id"]: r for r in sr.snapshot()}
    assert rows["postmarket_review"]["state"] == sr.STATE_REGISTERED


def test_last_run_is_attached(monkeypatch):
    monkeypatch.setattr(sr, "_resolve_schedulers", lambda: [])
    monkeypatch.setattr(
        sr, "_last_run", lambda job_id: {"status": "error", "error": "bridge refused"}
    )
    rows = {r["job_id"]: r for r in sr.snapshot()}
    assert rows["nightly_reflection"]["last_run"]["status"] == "error"
    assert sr.summarize(list(rows.values()))["last_run_error"] > 0


def test_resolution_does_not_import_modules(monkeypatch):
    """Importing blueprints.python_strategy runs a strategy cleanup.

    An observability call must never trigger that, so resolution reads
    sys.modules instead of importing.
    """
    import sys

    for path in ("blueprints.python_strategy", "blueprints.chartink", "blueprints.strategy"):
        sys.modules.pop(path, None)

    sr._resolve_schedulers()

    still_absent = [
        p
        for p in ("blueprints.python_strategy", "blueprints.chartink", "blueprints.strategy")
        if p not in sys.modules
    ]
    assert len(still_absent) == 3


# ---------------------------------------------------------------------------
# Anti-rot: the catalog must keep up with the source
# ---------------------------------------------------------------------------


def _add_job_ids(path: Path) -> tuple[set[str], set[str]]:
    """``(exact_ids, prefixes)`` passed as ``id=`` to any ``add_job(...)`` call.

    An ``ast.Constant`` id is exact. An f-string id (``id=f"squareoff_{name}"``)
    yields only its literal prefix, because the rest is runtime state. A job
    registered with a fully computed id (``id=job_id``) cannot be checked
    statically and is intentionally skipped — those are the user-defined
    families that land in the ``unregistered`` bucket by design.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # pragma: no cover - defensive
        return set(), set()

    exact: set[str] = set()
    prefixes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        label = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        if label != "add_job":
            continue
        for kw in node.keywords:
            if kw.arg != "id":
                continue
            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                exact.add(kw.value.value)
            elif isinstance(kw.value, ast.JoinedStr):
                parts = kw.value.values
                if parts and isinstance(parts[0], ast.Constant):
                    prefix = parts[0].value
                    if isinstance(prefix, str) and prefix:
                        prefixes.add(prefix)
    return exact, prefixes


def _prefix_is_covered(prefix: str) -> bool:
    """A dynamic family counts as covered by a matching ``id_prefix`` spec, or
    by exact catalog ids that all begin with the literal prefix (the
    ``multi_account_login_reminder_{HHMM}`` shape)."""
    if sr._spec_for(prefix) is not None:
        return True
    return any(spec.job_id and spec.job_id.startswith(prefix) for spec in sr.CATALOG)


def _source_files() -> list[Path]:
    files: list[Path] = []
    for directory in SCANNED_DIRS:
        files.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    return files


def test_every_add_job_id_in_the_source_is_catalogued():
    """The catalog must cover every statically-declared job id.

    Add a job, add it here — otherwise the page under-reports what runs, which
    is the exact failure this feature exists to fix.
    """
    uncovered: list[str] = []
    for path in _source_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        exact, prefixes = _add_job_ids(path)
        for job_id in exact:
            if sr._spec_for(job_id) is None:
                uncovered.append(f"{job_id}  ({rel})")
        for prefix in prefixes:
            if not _prefix_is_covered(prefix):
                uncovered.append(f"{prefix}*  ({rel})")

    assert not uncovered, (
        "These scheduled jobs exist in the source but not in "
        "services/scheduler_registry.CATALOG:\n  " + "\n  ".join(sorted(uncovered))
    )


def test_catalog_entries_are_well_formed():
    for spec in sr.CATALOG:
        assert bool(spec.job_id) != bool(spec.id_prefix), (
            f"{spec.label}: set exactly one of job_id / id_prefix"
        )
        assert spec.tier in (sr.TIER_PROTECTED, sr.TIER_GUARDED, sr.TIER_FREE)
        if spec.tier == sr.TIER_PROTECTED:
            assert spec.safety_note, (
                f"{spec.label} is protected but gives no reason. The reason is "
                "what stops a future maintainer from downgrading it."
            )


def test_catalog_has_no_duplicate_ids():
    ids = [s.job_id for s in sr.CATALOG if s.job_id]
    assert len(ids) == len(set(ids))


def test_registry_cannot_mutate_a_scheduler():
    """Phase 1 is read-only. Controls are Phase 2, behind a durable table."""
    tree = ast.parse((REPO_ROOT / "services" / "scheduler_registry.py").read_text(encoding="utf-8"))
    forbidden = {
        "add_job",
        "remove_job",
        "pause_job",
        "resume_job",
        "modify_job",
        "reschedule_job",
        "shutdown",
        "start",
    }
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    offenders = sorted(forbidden & {c for c in called if c})
    assert not offenders, f"scheduler_registry must not call: {offenders}"
