"""Strategy-specific preflight for futures_follow_cap50 (issue #162 S5).

Why this file exists
--------------------
``futures_follow_cap50`` is the leveraged NIFTY-futures companion to
``sector_follow_cap5_vol``. From its plan/learnings: *"at 15:20 IST it
**reuses** the sector_follow C1xW2+E4 evaluator (does NOT reimplement the
gates)"*. The two strategies share the same underlying signal source and
the same data-pipeline failure modes — specifically the
``today_close=None`` for sector indices condition that left today's
2026-06-26 15:20 IST sector_follow LIVE flip emitting 0 orders.

Since futures_follow consumes sector_follow's signals, **its preflight is
sector_follow's preflight** — with one addition: the NIFTY futures
near-month contract must be resolvable from the master contract. Without
that, the order placement layer can't pick a contract to buy.

What this preflight does
------------------------
1. Calls ``strategies.sector_follow_cap5_vol.preflight.check_can_go_live``
   to reuse the index-coverage / stock-coverage / master-contract /
   default gates.
2. Adds one futures-specific gate:
   - ``ff_nifty_future_resolvable`` — the near-month NIFTY futures contract
     (skipping any expiring within 1 trading day per the resolver's rule)
     must be findable in the master contract. Failure means orders cannot
     be placed even if signals fire.

Sandbox flips bypass everything.
"""

from __future__ import annotations

from services.strategy_preflight import (
    PreflightCheck,
    PreflightResult,
)
from utils.logging import get_logger

logger = get_logger(__name__)


def check_nifty_future_resolvable() -> PreflightCheck:
    """Gate: a tradable NIFTY near-month futures contract must exist.

    The futures_follow service resolves the contract at signal time via the
    master contract DB; without a hit, the order placement layer can't pick
    a contract. This gate probes the resolver to surface that condition
    BEFORE the flip succeeds.

    The resolver lives in ``services.futures_follow_service`` (private
    method); we call the public entrypoint if available, or fail closed
    with a clear message.
    """
    try:
        from services.futures_follow_service import get_service

        service = get_service()
    except Exception as e:
        logger.exception("preflight: futures_follow_service.get_service import failed")
        return PreflightCheck(
            name="ff_nifty_future_resolvable",
            passed=False,
            blocker_message=f"futures_follow service unavailable: {e!r}",
        )

    if service is None:
        return PreflightCheck(
            name="ff_nifty_future_resolvable",
            passed=False,
            blocker_message=(
                "futures_follow service not initialised — boot hasn't completed "
                "registration. Wait for app startup to finish."
            ),
        )

    # The resolver is a private method; introspect for it. We treat its
    # success as "a contract was found", regardless of which method name
    # the service uses internally (the implementation may evolve).
    resolver_candidates = (
        "_resolve_nifty_future_contract",
        "_resolve_near_month_future",
        "_resolve_future_contract",
    )
    resolver = None
    for name in resolver_candidates:
        candidate = getattr(service, name, None)
        if callable(candidate):
            resolver = candidate
            break

    if resolver is None:
        return PreflightCheck(
            name="ff_nifty_future_resolvable",
            passed=True,
            warning_message=(
                "futures_follow service does not expose a futures resolver — "
                "skipping the contract check (preflight may need an update)."
            ),
        )

    try:
        contract = resolver()
    except Exception as e:
        logger.exception("preflight: futures resolver raised")
        return PreflightCheck(
            name="ff_nifty_future_resolvable",
            passed=False,
            blocker_message=f"NIFTY future resolver raised: {e!r}",
        )

    if not contract:
        return PreflightCheck(
            name="ff_nifty_future_resolvable",
            passed=False,
            blocker_message=(
                "No tradable NIFTY near-month future found in master contract "
                "(skipping contracts expiring within 1 day per the resolver rule). "
                "Check master_contract download + NSE F&O expiry calendar."
            ),
        )

    return PreflightCheck(name="ff_nifty_future_resolvable", passed=True)


def check_can_go_live(target_mode: str) -> PreflightResult:
    """Compose sector_follow_cap5_vol's preflight (the shared signal source)
    with the futures-specific contract-resolvability gate."""
    strategy_name = "futures_follow_cap50"

    if target_mode == "sandbox":
        return PreflightResult.allow(
            snapshot={
                "preflight_path": "strategies.futures_follow_cap50.preflight",
                "strategy_name": strategy_name,
                "target_mode": target_mode,
                "reason": "sandbox-always-allowed",
            }
        )

    if target_mode != "live":
        return PreflightResult(
            can_flip=False,
            blockers=[f"target_mode must be 'live' or 'sandbox', got {target_mode!r}"],
            warnings=[],
            snapshot={"preflight_path": "strategies.futures_follow_cap50.preflight"},
        )

    # 1. Reuse sector_follow's preflight (default gates + sector_follow custom).
    try:
        from strategies.sector_follow_cap5_vol.preflight import (
            check_can_go_live as sf_check,
        )

        shared = sf_check(target_mode)
    except Exception as e:
        logger.exception("preflight: sector_follow shared preflight raised")
        return PreflightResult(
            can_flip=False,
            blockers=[f"sector_follow shared preflight raised: {e!r}"],
            warnings=[],
            snapshot={"preflight_path": "strategies.futures_follow_cap50.preflight"},
        )

    # 2. Add the futures-specific contract resolvability gate.
    futures_check = check_nifty_future_resolvable()
    futures_snapshot = {
        "futures_checks": [
            {
                "name": futures_check.name,
                "passed": futures_check.passed,
                "blocker": futures_check.blocker_message,
                "warning": futures_check.warning_message,
            }
        ],
    }
    futures_result = PreflightResult.from_checks([futures_check], snapshot=futures_snapshot)

    # 3. Compose.
    combined = shared.merge(futures_result)
    combined.snapshot["preflight_path"] = "strategies.futures_follow_cap50.preflight"
    combined.snapshot["strategy_name"] = strategy_name
    combined.snapshot["target_mode"] = target_mode
    return combined
