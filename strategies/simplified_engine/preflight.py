"""Strategy-specific preflight for simplified_engine (issue #162 S5).

Why this file exists
--------------------
The simplified stock engine is webhook-driven (Chartink → POST →
``blueprints/chartink.py:simplified_stock_engine_webhook``). Unlike
sector_follow which evaluates a fixed universe at a fixed time, this engine
processes signals as they arrive. Its preflight differs accordingly:

* The "is data fresh enough" question is moot — the engine doesn't poll a
  universe, it reacts to webhooks.
* The "can we trade" question reduces to: orphan-state-free + broker-live +
  the webhook endpoint is configured.

What this preflight does
------------------------
1. Default gates (broker session, no orphan trades, low recent DuckDB errors).
2. ``se_webhook_strategy_configured`` — the strategy row for the simplified
   engine webhook must exist + be active. Without it, incoming POSTs are
   logged and dropped; flipping to LIVE produces no trades.

Sandbox flips bypass.
"""

from __future__ import annotations

from services.strategy_preflight import (
    PreflightCheck,
    PreflightResult,
    run_default_checks,
)
from utils.logging import get_logger

logger = get_logger(__name__)

# The strategy_name used in trade_journal for the simplified engine.
_JOURNAL_STRATEGY_NAME = "trending_equity_intraday"


def check_webhook_strategy_configured() -> PreflightCheck:
    """Gate: at least one active strategy row exists for the simplified engine
    webhook handler.

    The simplified_stock_engine webhook (POST /chartink/simplified-stock-engine/<id>)
    requires a strategy row to route incoming signals. If no row exists or
    every matching row is inactive, the engine silently drops every webhook —
    operator sees 0 trades regardless of Chartink activity.
    """
    try:
        from database.strategy_db import db_session
        from database.strategy_db import strategy_model as strategy_mod

        try:
            # Probe for any active strategy. The simplified engine's exact
            # routing keys are operator-configured (per the webhook URL the
            # operator sets in Chartink), so this is a coarse but reliable
            # presence check.
            n_active = (
                db_session.query(strategy_mod.Strategy)
                .filter_by(is_active=True)
                .count()
            )
        finally:
            db_session.remove()
    except Exception:
        logger.exception("preflight: simplified_engine strategy probe failed — failing closed")
        return PreflightCheck(
            name="se_webhook_strategy_configured",
            passed=False,
            blocker_message=(
                "Could not probe strategy_db for active simplified-engine webhook rows "
                "— refusing LIVE flip until the probe path is healthy."
            ),
        )

    if n_active == 0:
        return PreflightCheck(
            name="se_webhook_strategy_configured",
            passed=False,
            blocker_message=(
                "No active strategy rows configured for the simplified engine. "
                "Create + activate the chartink_FnO_intraday_buy strategy (or "
                "equivalent webhook target) before enabling LIVE — without it "
                "the engine drops every incoming webhook silently."
            ),
        )

    return PreflightCheck(name="se_webhook_strategy_configured", passed=True)


def check_can_go_live(target_mode: str) -> PreflightResult:
    """Compose default gates with the simplified-engine-specific webhook check."""
    strategy_name = "simplified_engine"

    if target_mode == "sandbox":
        return PreflightResult.allow(
            snapshot={
                "preflight_path": "strategies.simplified_engine.preflight",
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
            snapshot={"preflight_path": "strategies.simplified_engine.preflight"},
        )

    # Default gates run against the journal's strategy_name (trade_journal
    # rows for the simplified engine are stored as 'trending_equity_intraday')
    # so the orphan check looks at the right rows.
    default_result = run_default_checks(_JOURNAL_STRATEGY_NAME)

    custom_check = check_webhook_strategy_configured()
    custom_snapshot = {
        "custom_checks": [
            {
                "name": custom_check.name,
                "passed": custom_check.passed,
                "blocker": custom_check.blocker_message,
                "warning": custom_check.warning_message,
            }
        ],
        "journal_strategy_name": _JOURNAL_STRATEGY_NAME,
    }
    custom_result = PreflightResult.from_checks([custom_check], snapshot=custom_snapshot)

    combined = default_result.merge(custom_result)
    combined.snapshot["preflight_path"] = "strategies.simplified_engine.preflight"
    combined.snapshot["strategy_name"] = strategy_name
    combined.snapshot["target_mode"] = target_mode
    return combined
