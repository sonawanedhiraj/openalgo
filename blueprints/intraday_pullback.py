"""Control + observability API for the intraday_pullback_top2 strategy (issue #394).

Routes under ``/intraday_pullback_top2/api/*``. Mirrors ``blueprints/futures_follow.py``:
mutating routes require an API key; read-only routes accept an API key OR a logged-in session.
"""

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.intraday_pullback_service import get_service
from utils.logging import get_logger

logger = get_logger(__name__)

intraday_pullback_bp = Blueprint(
    "intraday_pullback_bp", __name__, url_prefix="/intraday_pullback_top2"
)


def _extract_api_key():
    key = request.headers.get("X-API-KEY")
    if key:
        return key
    if request.is_json:
        body = request.get_json(silent=True) or {}
        if body.get("apikey"):
            return body["apikey"]
    return request.args.get("apikey")


def _authed() -> bool:
    key = _extract_api_key()
    return bool(key and verify_api_key(key))


def _authed_for_read() -> bool:
    if _authed():
        return True
    try:
        from utils.session import is_session_valid

        return bool(is_session_valid())
    except Exception:  # noqa: BLE001
        return False


def _unauthorized():
    return jsonify({"status": "error", "message": "Invalid or missing API key"}), 401


def _service_or_503():
    svc = get_service()
    if svc is None:
        return None, (jsonify({"status": "error", "message": "service not initialized"}), 503)
    return svc, None


@intraday_pullback_bp.route("/api/status", methods=["GET"])
def status():
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.get_status()})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/performance", methods=["GET"])
def performance():
    """Split long/short/combined win-rate, PF and net P&L. Optional ?date_from=&date_to=."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        from database import intraday_pullback_db as journal

        perf = journal.performance_by_side(
            svc.strategy_id,
            date_from=request.args.get("date_from"),
            date_to=request.args.get("date_to"),
            mode=svc.mode,
        )
        return jsonify({"status": "success", "data": perf})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback performance failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/positions", methods=["GET"])
def positions():
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        st = svc.get_status()
        return jsonify(
            {
                "status": "success",
                "data": {
                    "open_positions": st["open_positions"],
                    "open_count": st["open_count"],
                    "picks": st["picks"],
                    "side_today": st["side_today"],
                },
            }
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback positions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/pause", methods=["POST"])
def pause():
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    return jsonify({"status": "success", "data": svc.pause()})


@intraday_pullback_bp.route("/api/resume", methods=["POST"])
def resume():
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    return jsonify({"status": "success", "data": svc.resume()})


@intraday_pullback_bp.route("/api/close_all", methods=["POST"])
def close_all():
    if not _authed():
        return _unauthorized()
    body = request.get_json(silent=True) or {}
    if body.get("confirm") != "yes":
        return jsonify({"status": "error", "message": 'body must contain {"confirm": "yes"}'}), 400
    svc, err = _service_or_503()
    if err:
        return err
    try:
        closed = svc.close_all()
        return jsonify({"status": "success", "closed": closed, "count": len(closed)})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback close_all failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
