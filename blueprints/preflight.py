"""``GET /preflight`` — Stage-0 go / no-go gate for the scan-cycle skill.

This is an informational endpoint: it returns 200 with a structured response
whether the decision is ``go`` or ``abort``. Non-200 is reserved for actual
route errors (orchestrator blew up) — the caller must inspect
``go_decision`` rather than the HTTP status to decide whether to proceed.

The route is intentionally unauthenticated: it exposes no secrets, executes
no trades, and is meant to be called by an automated scheduled skill running
on the same local machine. The existing chartink webhook follows the same
public-but-by-design model.
"""

from flask import Blueprint, jsonify

from services.preflight_service import run_preflight
from utils.logging import get_logger

logger = get_logger(__name__)

preflight_bp = Blueprint("preflight_bp", __name__)


@preflight_bp.route("/preflight", methods=["GET"])
def preflight():
    """Return the go / no-go decision plus per-check detail."""
    try:
        return jsonify(run_preflight())
    except Exception as e:
        logger.exception("preflight: orchestrator failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
