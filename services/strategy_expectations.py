"""Deterministic expectation contracts over the post-market day digest.

Phase 2 of the post-market review (issue #532). Phase 1 (#511) collects the day
into a digest and reports it; this module is what turns that report into a
**verdict**.

The rule inherited from Phase 1 and enforced here: **Python detects, the LLM
triages.** A contract is a plain predicate that is true or false. Phase 3's model
gets the resulting violations to rank and explain — it never decides what counts
as broken. If it did, we would get invented findings on quiet days and silence on
real ones.

Three design rules, each load-bearing:

1. **Contracts read the digest only**, never the DB. That makes every contract
   replayable against any past day through the existing CLI, and testable
   without fixtures for ten tables.
2. **Missing input is ``unknown``, never ``fail``.** When a digest section
   degraded to ``None``, the strategy did not misbehave — our collection did.
   Reporting that as a violation would train the operator to ignore the report,
   which is the exact outcome this feature exists to prevent. Contracts declare
   their `requires` paths and are short-circuited to `unknown` automatically.
3. **Severity belongs to the contract**, fixed at definition time.

Fingerprints
------------

Every violation carries a stable ``fingerprint`` derived from
``strategy | contract_id | shape`` — deliberately *not* including the observed
numbers. The #497 outage produced the same violation on four consecutive days
with different lot counts; Phase 4 must recognise those as one recurring problem
and file one issue, not four.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# Tri-state outcomes. (bandit's B105 heuristic keys on the *name* containing
# "PASS" and flags the literal as a hardcoded password — it is an outcome label.)
PASS = "pass"  # nosec B105 — outcome label, not a credential
FAIL = "fail"
UNKNOWN = "unknown"

SEVERITIES = ("P0", "P1", "P2")


# --------------------------------------------------------------------------
# Digest access
# --------------------------------------------------------------------------


_MISSING = object()


def dig(digest: dict[str, Any], path: str, default: Any = None) -> Any:
    """Read a dotted ``path`` out of the digest, tolerating missing/None nodes.

    ``dig(d, "futures_carry.exits_today")`` returns ``None`` when the section
    failed, the key is absent, or any intermediate node is None — the three cases
    a contract must not distinguish between.
    """
    node: Any = digest
    for part in path.split("."):
        if not isinstance(node, dict):
            return default
        node = node.get(part, _MISSING)
        if node is _MISSING or node is None:
            return default
    return node


def _section_available(digest: dict[str, Any], path: str) -> bool:
    """True when ``path`` resolves to something other than a missing/None node."""
    return dig(digest, path, _MISSING) is not _MISSING


# --------------------------------------------------------------------------
# Contract definition
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Expect:
    """One declarative expectation.

    Args:
        contract_id: Stable identifier; part of the fingerprint, so renaming one
            re-files its issue in Phase 4. Treat as an API.
        strategy: Owning strategy, or ``"platform"`` for cross-cutting checks.
        description: Operator-facing statement of what must hold.
        predicate: ``(digest) -> bool | None``. ``None`` means "cannot tell".
        severity: ``P0`` | ``P1`` | ``P2``.
        requires: Digest paths that must be present; any missing path
            short-circuits the contract to ``unknown`` without running the
            predicate.
        shape: Coarse label for the *kind* of failure, used in the fingerprint.
            Defaults to ``contract_id``. Never include observed values.
        summary: ``(digest) -> str`` producing the violation's one-line summary.
        observe: Digest paths captured onto the violation as evidence.
    """

    contract_id: str
    strategy: str
    description: str
    predicate: Callable[[dict[str, Any]], bool | None]
    severity: str = "P2"
    requires: tuple[str, ...] = ()
    shape: str | None = None
    summary: Callable[[dict[str, Any]], str] | None = None
    observe: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        raw = f"{self.strategy}|{self.contract_id}|{self.shape or self.contract_id}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]  # noqa: S324  # nosec B324 — dedupe key, not a security primitive


@dataclass
class Violation:
    """A contract that evaluated to FAIL."""

    strategy: str
    contract_id: str
    severity: str
    description: str
    summary: str
    fingerprint: str
    observed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "contract_id": self.contract_id,
            "severity": self.severity,
            "description": self.description,
            "summary": self.summary,
            "fingerprint": self.fingerprint,
            "observed": self.observed,
        }


# --------------------------------------------------------------------------
# Predicate helpers
# --------------------------------------------------------------------------


def _audit_covers_date(digest: dict[str, Any]) -> bool:
    """Was the job-run audit actually live on the digest's date?

    Replaying a day from before the audit shipped must not look like "nothing
    fired" — that is history, not a fault.
    """
    date = digest.get("date")
    earliest = dig(digest, "jobs.audit_earliest_date")
    if not date or not earliest:
        return False
    return str(date) >= str(earliest)


def _job_ran(digest: dict[str, Any], job_id: str) -> bool | None:
    """Did ``job_id`` fire at all today? ``None`` when the audit has no data.

    An empty audit is reported as unknown rather than "nothing ran" — on the
    first day after the audit shipped, and after any restart-before-first-fire,
    zero rows is a collection gap, not a strategy fault.
    """
    if not _audit_covers_date(digest):
        return None
    jobs = dig(digest, "jobs.jobs")
    if not isinstance(jobs, dict) or not jobs:
        return None
    entry = jobs.get(job_id)
    return bool(entry and entry.get("ran"))


def _carry_without_exit(digest: dict[str, Any]) -> bool | None:
    """True when futures carry is HEALTHY (the predicate must pass on health).

    The strategy is T+1: a lot entered on day T is sold on T+1. So a position
    whose oldest open entry predates today, on a day with no exit at all, means
    the exit leg did not run — the #497 shape, which went unnoticed for four
    trading days while lots accumulated to 110% of book.

    Returns True (healthy) when there is no stale carry, or when an exit did
    happen today.
    """
    oldest = dig(digest, "futures_carry.oldest_open_entry_date")
    exits_today = dig(digest, "futures_carry.exits_today")
    date = digest.get("date")
    if exits_today is None or not date:
        return None
    if not oldest:
        return True  # nothing carried — nothing to exit
    if oldest >= date:
        return True  # entered today; its exit is due tomorrow
    return int(exits_today) > 0


def _no_stale_feed(digest: dict[str, Any]) -> bool | None:
    """True when every tracked universe reported a fresh feed."""
    health = dig(digest, "data_health")
    if not isinstance(health, dict) or not health:
        return None
    verdicts = [v for v in health.values() if isinstance(v, dict)]
    if not verdicts:
        return None
    return all(v.get("overall_ok") for v in verdicts)


def _sum_over_strategies(digest: dict[str, Any], key: str) -> int | None:
    by_strategy = dig(digest, "trade_journal.by_strategy")
    if not isinstance(by_strategy, dict):
        return None
    return sum(
        int(entry.get(key) or 0) for entry in by_strategy.values() if isinstance(entry, dict)
    )


# --------------------------------------------------------------------------
# The registry
# --------------------------------------------------------------------------


EXPECTATIONS: tuple[Expect, ...] = (
    # ---------------- platform / cross-cutting ----------------
    Expect(
        contract_id="scheduler_audit_recording",
        strategy="platform",
        description="The job-run audit recorded at least one scheduled fire today",
        predicate=lambda d: (None if not _audit_covers_date(d) else bool(dig(d, "jobs.recorded"))),
        severity="P1",
        requires=("jobs",),
        summary=lambda d: f"job_run recorded {dig(d, 'jobs.recorded', 0)} jobs on a trading day",
    ),
    Expect(
        contract_id="no_errored_jobs",
        strategy="platform",
        description="No scheduled job raised today",
        predicate=lambda d: not dig(d, "jobs.jobs_with_errors"),
        severity="P1",
        requires=("jobs",),
        summary=lambda d: ("jobs raised: " + ", ".join(dig(d, "jobs.jobs_with_errors", [])[:5])),
        observe=("jobs.jobs_with_errors",),
    ),
    Expect(
        contract_id="no_missed_jobs",
        strategy="platform",
        description="No scheduled job missed its fire window",
        predicate=lambda d: not dig(d, "jobs.jobs_missed"),
        severity="P2",
        requires=("jobs",),
        summary=lambda d: "jobs missed: " + ", ".join(dig(d, "jobs.jobs_missed", [])[:5]),
        observe=("jobs.jobs_missed",),
    ),
    Expect(
        contract_id="feed_fresh",
        strategy="platform",
        description="Every tracked data universe reported a fresh feed",
        predicate=_no_stale_feed,
        severity="P1",
        requires=("data_health",),
        summary=lambda d: "stale feed reported by data_health",
        observe=("data_health",),
    ),
    # ---------------- futures_follow_cap50 ----------------
    Expect(
        contract_id="t1_exit_for_carry",
        strategy="futures_follow_cap50",
        description="A NIFTY lot carried from a previous session is exited today (T+1)",
        predicate=_carry_without_exit,
        severity="P0",
        requires=("futures_carry",),
        shape="carry_without_exit",
        summary=lambda d: (
            f"{dig(d, 'futures_carry.open_lots_carried', '?')} lots open, oldest entry "
            f"{dig(d, 'futures_carry.oldest_open_entry_date', '?')} "
            f"({dig(d, 'futures_carry.carry_age_days', '?')}d), but 0 exits today"
        ),
        observe=(
            "futures_carry.open_lots_carried",
            "futures_carry.oldest_open_entry_date",
            "futures_carry.carry_age_days",
            "futures_carry.exits_today",
            "futures_carry.entries_today",
        ),
    ),
    Expect(
        contract_id="entry_job_fired",
        strategy="futures_follow_cap50",
        description="The entry job fired on a trading day",
        predicate=lambda d: _job_ran(d, "futures_follow_entry"),
        severity="P1",
        requires=("jobs",),
        summary=lambda d: "futures_follow_entry did not fire",
    ),
    Expect(
        contract_id="exit_job_fired",
        strategy="futures_follow_cap50",
        description="The exit job fired on a trading day",
        predicate=lambda d: _job_ran(d, "futures_follow_exit"),
        severity="P0",
        summary=lambda d: "futures_follow_exit did not fire",
        requires=("jobs",),
    ),
    # ---------------- sector_follow_cap5_vol ----------------
    Expect(
        contract_id="entry_job_fired",
        strategy="sector_follow_cap5_vol",
        description="The entry job fired on a trading day",
        predicate=lambda d: _job_ran(d, "sector_follow_entry"),
        severity="P1",
        requires=("jobs",),
        summary=lambda d: "sector_follow_entry did not fire",
    ),
    Expect(
        contract_id="exit_job_fired",
        strategy="sector_follow_cap5_vol",
        description="The exit job fired on a trading day",
        predicate=lambda d: _job_ran(d, "sector_follow_exit"),
        severity="P0",
        requires=("jobs",),
        summary=lambda d: "sector_follow_exit did not fire",
    ),
    # ---------------- simplified engine / journal-wide ----------------
    Expect(
        contract_id="no_unpriced_exits",
        strategy="simplified_engine",
        description="Every stamped exit carries a fill price",
        predicate=lambda d: (_sum_over_strategies(d, "unpriced_exits") or 0) == 0,
        severity="P2",
        requires=("trade_journal",),
        shape="unpriced_exit_rows",
        summary=lambda d: (
            f"{_sum_over_strategies(d, 'unpriced_exits')} exit rows have no price "
            "(dashboard P&L will understate)"
        ),
        observe=("trade_journal.by_strategy",),
    ),
    Expect(
        contract_id="flat_at_eod",
        strategy="simplified_engine",
        description="No intraday position is left open after the EOD flatten",
        predicate=lambda d: (_sum_over_strategies(d, "open_at_eod") or 0) == 0,
        severity="P1",
        requires=("trade_journal",),
        shape="open_position_at_eod",
        summary=lambda d: (
            f"{_sum_over_strategies(d, 'open_at_eod')} position(s) still open at EOD"
        ),
        observe=("trade_journal.by_strategy",),
    ),
)


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


def _enabled() -> bool:
    return os.environ.get("POSTMARKET_CONTRACTS_ENABLED", "true").strip().lower() == "true"


def _disabled_ids() -> set[str]:
    """Contracts silenced via env, as ``strategy:contract_id`` or bare id.

    Exists so a noisy contract can be turned off the same day it starts crying
    wolf, without a code change and without disabling the whole layer.
    """
    raw = os.environ.get("POSTMARKET_CONTRACTS_DISABLED", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_disabled(contract: Expect, disabled: set[str]) -> bool:
    return (
        contract.contract_id in disabled
        or f"{contract.strategy}:{contract.contract_id}" in disabled
    )


def evaluate_expectations(digest: dict[str, Any]) -> dict[str, Any]:
    """Run every contract against ``digest``.

    Returns:
        ``{"violations": [...], "counts": {pass, fail, unknown, skipped},
        "unknown_contracts": [...], "evaluated": bool}``. Never raises — a
        contract that blows up is recorded as ``unknown``, because a broken rule
        must not take down the report it is part of.
    """
    if not _enabled():
        return {
            "violations": [],
            "counts": {"pass": 0, "fail": 0, "unknown": 0, "skipped": len(EXPECTATIONS)},  # nosec B105 — outcome tally keys
            "unknown_contracts": [],
            "evaluated": False,
        }

    # A non-trading day has nothing to expect of anyone.
    if digest.get("is_trading_day") is False:
        return {
            "violations": [],
            "counts": {"pass": 0, "fail": 0, "unknown": 0, "skipped": len(EXPECTATIONS)},  # nosec B105 — outcome tally keys
            "unknown_contracts": [],
            "evaluated": False,
        }

    disabled = _disabled_ids()
    violations: list[Violation] = []
    unknown_contracts: list[str] = []
    counts = {"pass": 0, "fail": 0, "unknown": 0, "skipped": 0}  # nosec B105 — outcome tally keys

    for contract in EXPECTATIONS:
        if _is_disabled(contract, disabled):
            counts["skipped"] += 1
            continue

        label = f"{contract.strategy}:{contract.contract_id}"

        missing = [p for p in contract.requires if not _section_available(digest, p)]
        if missing:
            # Collection gap, not a strategy fault.
            counts["unknown"] += 1
            unknown_contracts.append(f"{label} (missing: {', '.join(missing)})")
            continue

        try:
            outcome = contract.predicate(digest)
        except Exception:
            logger.exception("strategy_expectations: contract %s raised", label)
            counts["unknown"] += 1
            unknown_contracts.append(f"{label} (predicate raised)")
            continue

        if outcome is None:
            counts["unknown"] += 1
            unknown_contracts.append(f"{label} (indeterminate)")
            continue
        if outcome:
            counts["pass"] += 1
            continue

        counts["fail"] += 1
        try:
            summary = contract.summary(digest) if contract.summary else contract.description
        except Exception:
            logger.exception("strategy_expectations: summary for %s raised", label)
            summary = contract.description
        violations.append(
            Violation(
                strategy=contract.strategy,
                contract_id=contract.contract_id,
                severity=contract.severity,
                description=contract.description,
                summary=summary,
                fingerprint=contract.fingerprint(),
                observed={path: dig(digest, path) for path in contract.observe},
            )
        )

    order = {sev: i for i, sev in enumerate(SEVERITIES)}
    violations.sort(key=lambda v: (order.get(v.severity, 99), v.strategy, v.contract_id))

    return {
        "violations": [v.to_dict() for v in violations],
        "counts": counts,
        "unknown_contracts": unknown_contracts,
        "evaluated": True,
    }
