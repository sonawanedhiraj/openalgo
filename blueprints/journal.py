"""``GET /journal/*`` — Stage 2 trade journal inspection endpoints.

These return read-only views over the ``trade_journal`` table for the
nightly reflection loop, the Cowork dashboard, and the operator's manual
EOD review. Like the preflight gate, they are intentionally unauthenticated
and meant to be called by local automation on the same machine — they
expose no secrets and trigger no broker actions.

Routes:

* ``GET /journal/today`` — aggregated counters for trades closed today:
  total count, total P&L, winners / losers, by_strategy, by_exit_reason.
* ``GET /journal/recent?hours=N`` — recent rows (default 24h).
* ``GET /journal/symbol/<symbol>?days=N`` — per-symbol history (default 7d).
"""

from flask import Blueprint, jsonify, request

from services import trade_journal_service
from utils.logging import get_logger

logger = get_logger(__name__)

journal_bp = Blueprint("journal_bp", __name__)


def _int_arg(name: str, default: int, *, minimum: int = 1) -> int:
    """Parse an optional integer query arg; clamp to ``minimum``."""
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


@journal_bp.route("/journal/today", methods=["GET"])
def journal_today():
    """Aggregated summary of trades closed today (IST calendar day)."""
    try:
        return jsonify(trade_journal_service.get_today_summary())
    except Exception as e:
        logger.exception("journal_today failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@journal_bp.route("/journal/recent", methods=["GET"])
def journal_recent():
    """Recent journal rows. ``?hours=N`` controls the window (default 24)."""
    hours = _int_arg("hours", default=24)
    try:
        return jsonify(
            {"hours": hours, "trades": trade_journal_service.get_recent_trades(hours=hours)}
        )
    except Exception as e:
        logger.exception("journal_recent failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@journal_bp.route("/journal/symbol/<symbol>", methods=["GET"])
def journal_symbol(symbol: str):
    """Per-symbol history. ``?days=N`` controls the window (default 7)."""
    days = _int_arg("days", default=7)
    try:
        rows = trade_journal_service.get_trades_for_symbol(symbol.upper(), days=days)
        return jsonify({"symbol": symbol.upper(), "days": days, "trades": rows})
    except Exception as e:
        logger.exception("journal_symbol failed (%s): %s", symbol, e)
        return jsonify({"status": "error", "message": str(e)}), 500
