"""Read-only ``GET /mode/status`` for the Stage-0 operational floor.

Exposes the four inputs that contribute to the effective trade mode so an
operator (or Cowork) can see at a glance *why* the resolver landed where it
did. Setting ``daily_intent`` is intentionally NOT exposed over HTTP in this
pass — flips happen via direct DB call from a REPL or a future authenticated
endpoint. The point of this surface is observability, not control.
"""

from flask import Blueprint, jsonify

from database.daily_intent_db import _today_ist_str
from database.settings_db import get_analyze_mode
from services.mode_service import get_daily_intent, resolve_effective_mode
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

mode_status_bp = Blueprint("mode_status_bp", __name__, url_prefix="/mode")


@mode_status_bp.route("/status", methods=["GET"])
@check_session_validity
def mode_status():
    """Return the resolver inputs and the effective mode for today (IST)."""
    try:
        today = _today_ist_str()
        intent_row = get_daily_intent(today)
        analyze_mode = bool(get_analyze_mode())
        effective = resolve_effective_mode(today)

        return jsonify(
            {
                "today": today,
                "daily_intent": intent_row,
                "analyze_mode": analyze_mode,
                "effective_mode": effective.value,
            }
        )
    except Exception as e:
        logger.exception("mode_status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
