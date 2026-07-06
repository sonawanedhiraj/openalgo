"""System-enforced pre-flight checks for strategy mode flips (issue #162).

Why this exists
---------------
On 2026-06-26 15:20 IST, sector_follow_cap5_vol was flipped from sandbox to
live via raw SQL UPDATE on the ``strategy_mode`` table. The strategy's own
smoke check passed (it verified 30/30 STOCK aggregator coverage) but **all 8
mapped sector indices had empty intraday aggregators**, so the strategy
emitted 0 orders in LIVE mode silently. The operator had no idea anything
was wrong until next-day forensics.

The root architectural fix is to **prevent the flip itself** when the system
isn't ready to honour it. The operator should click a toggle and either see
"✅ flipped" or "🚫 cannot enable LIVE: <reason>". Memory + checklists are
the wrong place for safety.

What this module provides
-------------------------
* :class:`PreflightResult` — the standard return shape from every preflight check.
* :func:`run_preflight` — locates the strategy-specific preflight module and
  runs it; falls back to :func:`default_preflight` when no custom module exists.
* :func:`default_preflight` — gates that apply to every strategy regardless
  of its own custom checks:
    - Broker session is live
    - No orphan trades in trade_journal for this strategy
    - DuckDB lock error count in the last 60 min is below threshold
* :class:`PreflightCheck` — a tiny named-tuple wrapper for individual gate
  results, used by both default and strategy-specific preflights to compose
  cleanly.

Per-strategy preflight modules live at
``strategies/<name>/preflight.py`` and expose a function
``check_can_go_live(target_mode: str) -> PreflightResult``. They MAY call
:func:`run_default_checks` first and append their own gates.

Sandbox flips are always allowed (no gates) — the gates exist specifically to
protect LIVE-mode mistakes. Sandbox is the safe default state.

Failure is the only thing that blocks the flip. Warnings are non-blocking but
surface in the audit row + Telegram notification so the operator can see them.
"""

from __future__ import annotations

import importlib
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))

# Default thresholds for the broad system-health gates. All are overridable
# via env so an operator can loosen them in a controlled way without code
# changes; defaults are conservative.
_DEFAULT_RECENT_ERROR_WINDOW_MIN = 60
_DEFAULT_RECENT_ERROR_THRESHOLD = 5
_DEFAULT_ORPHAN_AGE_HOURS = 24

# Regex for DuckDB lock errors that signal the system is currently unstable.
# Catches the in-process config mismatch (PR #142's class) and the retry
# exhaustion message from ``database.historify_db.get_connection``.
_DUCKDB_LOCK_ERROR_RE = re.compile(
    r"different configuration|Failed to connect to DuckDB after \d+ attempts|"
    r"being used by another process|Unique file handle conflict",
    re.IGNORECASE,
)


@dataclass
class PreflightCheck:
    """One gate's outcome — used to compose default + custom gates cleanly."""

    name: str
    passed: bool
    blocker_message: str | None = None
    warning_message: str | None = None


@dataclass
class PreflightResult:
    """The standard return shape from every preflight check.

    A flip is allowed iff ``can_flip`` is True. Warnings are non-blocking and
    surface in the audit row + Telegram notification.
    """

    can_flip: bool
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)

    def merge(self, other: PreflightResult) -> PreflightResult:
        """Combine two preflight results — used by strategy-specific preflights
        that call run_default_checks first and append their own gates."""
        return PreflightResult(
            can_flip=self.can_flip and other.can_flip,
            blockers=list(self.blockers) + list(other.blockers),
            warnings=list(self.warnings) + list(other.warnings),
            snapshot={**self.snapshot, **other.snapshot},
        )

    @classmethod
    def from_checks(
        cls, checks: list[PreflightCheck], snapshot: dict[str, Any] | None = None
    ) -> PreflightResult:
        """Build a result from a list of individual gate outcomes."""
        blockers = [c.blocker_message for c in checks if not c.passed and c.blocker_message]
        warnings = [c.warning_message for c in checks if c.warning_message]
        return cls(
            can_flip=all(c.passed for c in checks),
            blockers=blockers,
            warnings=warnings,
            snapshot=snapshot or {},
        )

    @classmethod
    def allow(cls, snapshot: dict[str, Any] | None = None) -> PreflightResult:
        """Unconditional pass — used for sandbox flips and the always-allowed case."""
        return cls(can_flip=True, blockers=[], warnings=[], snapshot=snapshot or {})


# --------------------------------------------------------------------------- #
# Individual default gates (each returns a PreflightCheck)
# --------------------------------------------------------------------------- #


def check_broker_session_live() -> PreflightCheck:
    """Gate: a broker session must be live before any strategy can go LIVE."""
    try:
        from services.broker_session_health import is_live_broker_session

        if is_live_broker_session():
            return PreflightCheck(name="broker_session_live", passed=True)
        return PreflightCheck(
            name="broker_session_live",
            passed=False,
            blocker_message=(
                "Broker session is not live — log in to Zerodha before enabling LIVE mode"
            ),
        )
    except Exception as e:
        logger.exception("preflight: broker_session probe raised — failing closed")
        return PreflightCheck(
            name="broker_session_live",
            passed=False,
            blocker_message=f"Broker session probe failed: {e}",
        )


def check_no_orphan_trades(strategy_name: str, max_age_hours: int | None = None) -> PreflightCheck:
    """Gate: trade_journal must not have orphan rows for this strategy.

    An orphan row is one where ``exit_reason`` is set but ``exit_price`` is
    NULL — the EOD watchdog or scheduled exit fired but the order never
    confirmed. The engine keeps re-attempting these on every restart (see
    issue #157), so blocking the LIVE flip until they're reconciled stops
    a bad-state strategy from going live.

    Args:
        strategy_name: The journal's strategy_name to filter by.
        max_age_hours: Orphan rows newer than this are tolerated as "still
            in flight" (default ``STRATEGY_PREFLIGHT_ORPHAN_AGE_HOURS`` env
            or 24). Older rows block the flip.
    """
    if max_age_hours is None:
        try:
            max_age_hours = int(
                os.environ.get(
                    "STRATEGY_PREFLIGHT_ORPHAN_AGE_HOURS",
                    str(_DEFAULT_ORPHAN_AGE_HOURS),
                )
            )
        except (TypeError, ValueError):
            max_age_hours = _DEFAULT_ORPHAN_AGE_HOURS

    try:
        from database.trade_journal_db import TradeJournal, db_session
    except Exception:
        logger.exception("preflight: trade_journal_db import failed — skipping orphan check")
        # Fail open: if the journal DB isn't reachable, don't block. The
        # broker session gate is the load-bearing one.
        return PreflightCheck(
            name="no_orphan_trades",
            passed=True,
            warning_message="orphan_trades probe skipped (journal DB unreachable)",
        )

    cutoff = datetime.utcnow() - timedelta(hours=max_age_hours)
    try:
        try:
            orphans = (
                db_session.query(TradeJournal.id, TradeJournal.symbol)
                .filter(TradeJournal.strategy_name == strategy_name)
                .filter(TradeJournal.exit_price.is_(None))
                .filter(TradeJournal.exit_reason.isnot(None))
                .filter(TradeJournal.placed_at < cutoff)
                .all()
            )
        finally:
            db_session.remove()
    except Exception as e:
        logger.exception("preflight: orphan trade query raised — failing open")
        return PreflightCheck(
            name="no_orphan_trades",
            passed=True,
            warning_message=f"orphan_trades probe failed ({e})",
        )

    if not orphans:
        return PreflightCheck(name="no_orphan_trades", passed=True)
    sample = ", ".join(f"{sym}#{rid}" for rid, sym in orphans[:5])
    return PreflightCheck(
        name="no_orphan_trades",
        passed=False,
        blocker_message=(
            f"{len(orphans)} orphan trade(s) in journal for {strategy_name} "
            f"(exit_reason set, exit_price NULL, >{max_age_hours}h old): {sample}"
            + (" ..." if len(orphans) > 5 else "")
            + " — reconcile via the orphan-cleanup job before enabling LIVE"
        ),
    )


def check_recent_duckdb_errors(
    window_min: int | None = None, threshold: int | None = None
) -> PreflightCheck:
    """Gate: count of DuckDB lock errors in the recent window must be low.

    High recent count means the system is currently unstable — flipping to LIVE
    now risks losing trades to the same failure mode. Reads ``log/errors.jsonl``
    (last N lines, time-filtered) since it's the canonical structured error
    sink (per CLAUDE.md "Logging Architecture").

    Args:
        window_min: Lookback window (default ``STRATEGY_PREFLIGHT_RECENT_ERROR_WINDOW_MIN``
            env or 60 min).
        threshold: Max tolerated count (default ``STRATEGY_PREFLIGHT_RECENT_ERROR_THRESHOLD``
            env or 5).
    """
    if window_min is None:
        try:
            window_min = int(
                os.environ.get(
                    "STRATEGY_PREFLIGHT_RECENT_ERROR_WINDOW_MIN",
                    str(_DEFAULT_RECENT_ERROR_WINDOW_MIN),
                )
            )
        except (TypeError, ValueError):
            window_min = _DEFAULT_RECENT_ERROR_WINDOW_MIN
    if threshold is None:
        try:
            threshold = int(
                os.environ.get(
                    "STRATEGY_PREFLIGHT_RECENT_ERROR_THRESHOLD",
                    str(_DEFAULT_RECENT_ERROR_THRESHOLD),
                )
            )
        except (TypeError, ValueError):
            threshold = _DEFAULT_RECENT_ERROR_THRESHOLD

    errors_path = Path("log/errors.jsonl")
    if not errors_path.exists():
        # First boot or test environment — nothing to read.
        return PreflightCheck(name="recent_duckdb_errors", passed=True)

    cutoff = datetime.now(_IST) - timedelta(minutes=window_min)
    count = 0
    try:
        # Read the last ~5000 lines — errors.jsonl is auto-truncated to 1000
        # at startup per CLAUDE.md, so 5000 is comfortable headroom and bounds
        # the worst-case scan.
        with errors_path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()[-5000:]
        for line in lines:
            if not _DUCKDB_LOCK_ERROR_RE.search(line):
                continue
            # Cheap timestamp prefix check: ts": "YYYY-MM-DD HH:MM:SS"
            try:
                ts_start = line.index('"ts": "') + len('"ts": "')
                ts_end = line.index('"', ts_start)
                ts_str = line[ts_start:ts_end]
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_IST)
                if ts >= cutoff:
                    count += 1
            except (ValueError, IndexError):
                # Malformed line — count it as recent (defensive).
                count += 1
    except Exception as e:
        logger.exception("preflight: errors.jsonl scan failed — failing open")
        return PreflightCheck(
            name="recent_duckdb_errors",
            passed=True,
            warning_message=f"recent_duckdb_errors probe failed ({e})",
        )

    if count <= threshold:
        return PreflightCheck(name="recent_duckdb_errors", passed=True)
    return PreflightCheck(
        name="recent_duckdb_errors",
        passed=False,
        blocker_message=(
            f"{count} DuckDB lock errors in the last {window_min} min "
            f"(threshold {threshold}) — system is currently unstable; "
            "wait for the boot/backfill burst to settle, or raise "
            "STRATEGY_PREFLIGHT_RECENT_ERROR_THRESHOLD if intentional"
        ),
    )


# --------------------------------------------------------------------------- #
# Default + dispatch entry points
# --------------------------------------------------------------------------- #


def run_default_checks(strategy_name: str) -> PreflightResult:
    """Run the system-wide default gates that apply to every LIVE flip.

    Strategy-specific preflights call this first then append their own gates.
    """
    checks = [
        check_broker_session_live(),
        check_no_orphan_trades(strategy_name),
        check_recent_duckdb_errors(),
    ]
    snapshot = {
        "default_checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "blocker": c.blocker_message,
                "warning": c.warning_message,
            }
            for c in checks
        ],
        "evaluated_at_ist": datetime.now(_IST).isoformat(),
    }
    return PreflightResult.from_checks(checks, snapshot=snapshot)


def default_preflight(strategy_name: str, target_mode: str) -> PreflightResult:
    """The fallback preflight for strategies without a custom module.

    Sandbox flips are always allowed; LIVE flips must pass the default gates.
    """
    if target_mode != "live":
        return PreflightResult.allow(
            snapshot={
                "preflight_path": "default",
                "target_mode": target_mode,
                "evaluated_at_ist": datetime.now(_IST).isoformat(),
            }
        )
    return run_default_checks(strategy_name)


def _load_strategy_preflight(strategy_name: str):
    """Locate ``strategies/<strategy_name>/preflight.py`` and return its
    ``check_can_go_live`` function, or None if no custom module exists.
    """
    try:
        module = importlib.import_module(f"strategies.{strategy_name}.preflight")
    except ModuleNotFoundError:
        return None
    except Exception:
        logger.exception(
            "preflight: failed to import strategies.%s.preflight — falling back to default",
            strategy_name,
        )
        return None
    func = getattr(module, "check_can_go_live", None)
    if not callable(func):
        logger.warning(
            "preflight: strategies.%s.preflight has no check_can_go_live() — "
            "falling back to default",
            strategy_name,
        )
        return None
    return func


def run_preflight(strategy_name: str, target_mode: str) -> PreflightResult:
    """Run the preflight check for a flip attempt — the single public entry.

    Strategy-specific preflight at ``strategies/<name>/preflight.py`` wins
    when present; otherwise the default applies. Sandbox flips are always
    allowed regardless.

    Never raises. A custom preflight that raises is logged and treated as
    "blocker: preflight raised" so the flip is refused safely rather than
    bypassed.
    """
    if target_mode not in ("live", "sandbox"):
        return PreflightResult(
            can_flip=False,
            blockers=[f"target_mode must be one of (live, sandbox), got {target_mode!r}"],
            warnings=[],
            snapshot={"preflight_path": "validation"},
        )

    # Sandbox is always allowed — the gates exist to protect LIVE.
    if target_mode == "sandbox":
        return PreflightResult.allow(
            snapshot={
                "preflight_path": "sandbox-always-allowed",
                "strategy_name": strategy_name,
                "target_mode": target_mode,
                "evaluated_at_ist": datetime.now(_IST).isoformat(),
            }
        )

    custom = _load_strategy_preflight(strategy_name)
    if custom is None:
        return default_preflight(strategy_name, target_mode)

    try:
        result = custom(target_mode)
    except Exception as e:
        logger.exception(
            "preflight: strategies.%s.preflight.check_can_go_live raised", strategy_name
        )
        return PreflightResult(
            can_flip=False,
            blockers=[
                f"Strategy preflight raised: {e!r} — refusing flip; check logs and fix the preflight module"
            ],
            warnings=[],
            snapshot={
                "preflight_path": f"strategies.{strategy_name}.preflight",
                "exception": str(e),
            },
        )

    if not isinstance(result, PreflightResult):
        logger.error(
            "preflight: strategies.%s.preflight returned %s, not PreflightResult",
            strategy_name,
            type(result).__name__,
        )
        return PreflightResult(
            can_flip=False,
            blockers=[
                f"Strategy preflight returned {type(result).__name__}, expected PreflightResult"
            ],
            warnings=[],
            snapshot={"preflight_path": f"strategies.{strategy_name}.preflight"},
        )

    # Annotate the snapshot so audit rows record the path that ran.
    result.snapshot.setdefault("preflight_path", f"strategies.{strategy_name}.preflight")
    result.snapshot.setdefault("strategy_name", strategy_name)
    result.snapshot.setdefault("target_mode", target_mode)
    result.snapshot.setdefault("evaluated_at_ist", datetime.now(_IST).isoformat())
    return result
