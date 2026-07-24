"""Read-only ``GET /mode/status`` — the mode-resolution observability surface.

Exposes the inputs that drive live/sandbox order routing so an operator (or
Cowork) can see at a glance *why* the dispatch lands where it does. Setting a
mode is intentionally NOT exposed here — flips happen only through the
strategies-page toggle (``strategy_mode_service.flip_mode``, preflight-gated).
The point of this surface is observability, not control.

**UI-driven per-strategy dispatch (issue #440).** Routing is decided by
exactly two visible controls: the navbar Analyze/Live toggle
(``analyze_mode``) and the per-strategy ``strategy_mode`` rows written by the
strategies-page toggles. This endpoint reports, per registered strategy, the
persistent row AND the *effective routing* an order placed right now would
get (``resolve_order_mode``). The retired hidden ``__global__`` gate and
legacy ``daily_intent`` fall-through no longer participate; the legacy
``daily_intent`` row is still surfaced for observability only.

Back-compat: the historical keys ``today``, ``daily_intent``,
``analyze_mode``, ``effective_mode`` (scoped to ``simplified_engine``) and
``source`` are preserved; ``intent`` is always ``'run'`` /
``daily_capital_cap`` always ``None`` (those axes are retired).
"""

from flask import Blueprint, jsonify

from database.daily_intent_db import _today_ist_str
from database.settings_db import get_analyze_mode
from services.mode_service import (
    get_daily_intent,
    resolve_order_mode,
    resolve_strategy_mode,
)
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

mode_status_bp = Blueprint("mode_status_bp", __name__, url_prefix="/mode")

# The strategy the legacy scalar keys report on (historical /mode/status shape).
_STRATEGY = "simplified_engine"


@mode_status_bp.route("/status", methods=["GET"])
@check_session_validity
def mode_status():
    """Return the resolver inputs and per-strategy effective routing."""
    try:
        today = _today_ist_str()
        intent_row = get_daily_intent(today)
        analyze_mode = bool(get_analyze_mode())
        decision = resolve_strategy_mode(_STRATEGY, date=today)

        # Per-strategy effective routing: every persistent row, plus what an
        # order for that strategy would do RIGHT NOW (analyze overlay applied).
        strategies = []
        try:
            from database.strategy_mode_db import list_modes

            for row in list_modes():
                name = row["strategy_name"]
                strategies.append(
                    {
                        "strategy_name": name,
                        "mode": row["mode"],
                        "updated_by": row["updated_by"],
                        "updated_at": row["updated_at"],
                        "effective_routing": resolve_order_mode(name).value,
                    }
                )
        except Exception:
            logger.exception("mode_status: per-strategy listing failed")

        return jsonify(
            {
                "today": today,
                "strategy": _STRATEGY,
                "daily_intent": intent_row,
                "analyze_mode": analyze_mode,
                # Back-compat scalar keys (scoped to the simplified engine).
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
                # Issue #440: the actual dispatch truth, one entry per
                # strategy_mode row. 'live' here means an order placed now
                # WOULD reach the real broker. Unlisted strategies have no
                # row and always route sandbox (default deny).
                "strategies": strategies,
                "unregistered_strategies_route": "sandbox",
            }
        )
    except Exception as e:
        logger.exception("mode_status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
