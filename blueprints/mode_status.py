"""Read-only ``GET /mode/status`` for the Stage-0 operational floor.

Exposes the inputs that contribute to the effective trade mode so an operator
(or Cowork) can see at a glance *why* the resolver landed where it did. Setting
the intent is intentionally NOT exposed over HTTP in this pass — flips happen
via the unified ``strategy_daily_intent`` table (direct DB call / Telegram bot).
The point of this surface is observability, not control.

**Mode resolution source (mode-only architecture — B2, 2026-06-12).** This
endpoint reads the effective mode via
:func:`services.mode_service.resolve_strategy_mode` (scoped to
``simplified_engine``) — a back-compat shim over the mode-only
:func:`resolve_mode`. Resolution is ``strategy_mode row → env mode flag →
sandbox default``; the retired ``strategy_daily_intent``/legacy ``daily_intent``
tables and the run/pause/halt ``intent`` axis no longer drive it. The legacy
``daily_intent`` row is still surfaced under ``daily_intent`` for
observability/back-compat, but no longer affects ``effective_mode``.

Response shape is backward compatible: the historical keys ``today``,
``daily_intent``, ``analyze_mode`` and ``effective_mode`` (a mode string) are all
preserved; ``source`` reflects the mode-only resolution and ``intent`` is always
``'run'`` / ``daily_capital_cap`` always ``None`` (those axes are retired).
"""

from flask import Blueprint, jsonify

from database.daily_intent_db import _today_ist_str
from database.settings_db import get_analyze_mode
from services.mode_service import get_daily_intent, resolve_strategy_mode
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

mode_status_bp = Blueprint("mode_status_bp", __name__, url_prefix="/mode")

# The strategy this observability surface reports on. The simplified engine is
# the one whose mode the legacy /mode/status historically described.
_STRATEGY = "simplified_engine"


@mode_status_bp.route("/status", methods=["GET"])
@check_session_validity
def mode_status():
    """Return the resolver inputs and the effective mode for today (IST)."""
    try:
        today = _today_ist_str()
        intent_row = get_daily_intent(today)
        analyze_mode = bool(get_analyze_mode())
        decision = resolve_strategy_mode(_STRATEGY, date=today)

        return jsonify(
            {
                "today": today,
                "strategy": _STRATEGY,
                "daily_intent": intent_row,
                "analyze_mode": analyze_mode,
                # Backward-compatible string key — now sourced from the unified
                # resolver (was resolve_effective_mode().value).
                "effective_mode": decision.mode,
                "intent": decision.intent,
                "daily_capital_cap": decision.daily_capital_cap,
                "source": decision.source,
                "effective": {
                    "mode": decision.mode,
                    "intent": decision.intent,
                    "daily_capital_cap": decision.daily_capital_cap,
                    "source": decision.source,
                },
            }
        )
    except Exception as e:
        logger.exception("mode_status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
