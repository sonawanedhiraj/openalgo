"""Observability + operator-control endpoints for futures_follow_cap50.

Additive blueprint — it only reads/controls the FuturesFollowService singleton
(services/futures_follow_service.py); it mutates no existing blueprint or platform
route. URL prefix: /futures_follow_cap50.

All endpoints are API-key authenticated (X-API-KEY header or ``apikey`` in the JSON
body / query string), matching blueprints/sector_follow.py. The strategy ships
``scaffold`` by default, so in that mode these endpoints report state and toggle
in-memory flags without ever touching a broker.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.futures_follow_service import get_service
from utils.logging import get_logger

logger = get_logger(__name__)

futures_follow_bp = Blueprint("futures_follow_bp", __name__, url_prefix="/futures_follow_cap50")


def _extract_api_key() -> str | None:
    """API key from header, then JSON body, then query string."""
    key = request.headers.get("X-API-KEY")
    if not key:
        body = request.get_json(silent=True) or {}
        key = body.get("apikey")
    if not key:
        key = request.args.get("apikey")
    return key


def _authed() -> bool:
    key = _extract_api_key()
    return bool(key and verify_api_key(key))


def _unauthorized():
    return jsonify({"status": "error", "message": "Invalid or missing API key"}), 401


def _service_or_503():
    svc = get_service()
    if svc is None:
        return None, (
            jsonify({"status": "error", "message": "futures_follow service not initialised"}),
            503,
        )
    return svc, None


@futures_follow_bp.route("/api/status", methods=["GET"])
def status():
    """Current strategy state (mode, kill switch, lots held, margin used, book)."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.get_status()})
    except Exception as e:
        logger.exception("futures_follow status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@futures_follow_bp.route("/api/data_health", methods=["GET"])
def data_health():
    """Live market-data freshness for the strategy's signal feed.

    The futures sleeve fires on the sector_follow_cap5_vol signal set, so its feed
    health IS the sector_follow feed health. Read-only on historify.duckdb — does
    not write the data_health_check row."""
    if not _authed():
        return _unauthorized()
    try:
        from datetime import datetime, timedelta, timezone

        from services.data_freshness_service import check_strategy_data_ready

        ist = timezone(timedelta(hours=5, minutes=30))
        checked_at = datetime.now(ist).isoformat()
        ok, details = check_strategy_data_ready("sector_follow_cap5_vol")
        return jsonify(
            {
                "overall_ok": ok,
                "checked_at": checked_at,
                "feed_source": "sector_follow_cap5_vol",
                "details": details,
            }
        )
    except Exception as e:
        logger.exception("futures_follow data_health failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@futures_follow_bp.route("/api/positions", methods=["GET"])
def positions():
    """Open positions + today's entries/exits in full detail."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify(
            {
                "status": "success",
                "data": {
                    "open_positions": svc.open_positions_view(),
                    "today_entries": list(svc.today_entries),
                    "today_exits": list(svc.today_exits),
                    "lots_held": svc.lots_held(),
                    "margin_used_inr": svc.margin_used(),
                },
            }
        )
    except Exception as e:
        logger.exception("futures_follow positions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@futures_follow_bp.route("/api/pause", methods=["POST"])
def pause():
    """Halt new entries. Existing positions hold to their scheduled T+1 exit."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.pause()})
    except Exception as e:
        logger.exception("futures_follow pause failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@futures_follow_bp.route("/api/resume", methods=["POST"])
def resume():
    """Clear manual pause AND the kill switch — re-enable new entries."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.resume()})
    except Exception as e:
        logger.exception("futures_follow resume failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@futures_follow_bp.route("/api/close_all", methods=["POST"])
def close_all():
    """Emergency square-off of all open positions. Requires body {"confirm":"yes"}."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    if str(body.get("confirm", "")).lower() != "yes":
        return (
            jsonify({"status": "error", "message": 'close_all requires body {"confirm": "yes"}'}),
            400,
        )
    try:
        closed = svc.close_all_positions()
        return jsonify({"status": "success", "closed": closed, "count": len(closed)})
    except Exception as e:
        logger.exception("futures_follow close_all failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
