"""Observability + operator-control endpoints for sector_follow_cap5_vol.

Phase 2 Deliverable 2. Additive blueprint — it only reads/controls the
SectorFollowService singleton (services/sector_follow_service.py); it mutates no
existing blueprint or platform route. URL prefix: /sector_follow_cap5_vol.

All endpoints are API-key authenticated (X-API-KEY header or ``apikey`` in the
JSON body / query string), matching the pattern used by restx_api/telegram_bot.py
and the broader /api/v1 surface. The strategy ships ``scaffold`` by default, so in
that mode these endpoints report state and toggle in-memory flags without ever
touching a broker.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.sector_follow_service import get_service
from utils.logging import get_logger

logger = get_logger(__name__)

sector_follow_bp = Blueprint("sector_follow_bp", __name__, url_prefix="/sector_follow_cap5_vol")


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
            jsonify(
                {
                    "status": "error",
                    "message": "sector_follow service not initialised",
                }
            ),
            503,
        )
    return svc, None


@sector_follow_bp.route("/api/status", methods=["GET"])
def status():
    """Current strategy state (mode, kill switch, today's entries/exits, open book)."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.get_status()})
    except Exception as e:
        logger.exception("sector_follow status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@sector_follow_bp.route("/api/data_health", methods=["GET"])
def data_health():
    """Live market-data freshness for the strategy's index + stock feeds.

    Read-only on historify.duckdb — does not write the data_health_check row (that
    is the 16:30 IST job's job). Returns per-symbol last-timestamp + staleness."""
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
                "details": details,
            }
        )
    except Exception as e:
        logger.exception("sector_follow data_health failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@sector_follow_bp.route("/api/positions", methods=["GET"])
def positions():
    """Open positions + today's closed exits in full detail."""
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
                },
            }
        )
    except Exception as e:
        logger.exception("sector_follow positions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@sector_follow_bp.route("/api/pause", methods=["POST"])
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
        logger.exception("sector_follow pause failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@sector_follow_bp.route("/api/resume", methods=["POST"])
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
        logger.exception("sector_follow resume failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@sector_follow_bp.route("/api/close_all", methods=["POST"])
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
            jsonify(
                {
                    "status": "error",
                    "message": 'close_all requires body {"confirm": "yes"}',
                }
            ),
            400,
        )
    try:
        closed = svc.close_all_positions()
        return jsonify({"status": "success", "closed": closed, "count": len(closed)})
    except Exception as e:
        logger.exception("sector_follow close_all failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
