"""Observability + operator-control endpoints for cas_320_expiry_straddle (issue #740).

Additive blueprint over the ``CasStraddleService`` singleton
(services/cas_straddle_service.py). URL prefix: ``/cas_320_expiry_straddle``.

Auth follows ``blueprints/futures_follow.py``: mutating endpoints take an API key
(``X-API-KEY`` header / ``apikey`` in body or query); the read-only endpoints
and the UI config endpoint ALSO accept a valid logged-in browser session so the
React strategy page can render and save from the Settings card. ``@check_session_validity``
is deliberately NOT used here — its failure path revokes broker tokens.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.cas_straddle_service import (
    DEFAULTS,
    STRATEGY_NAME,
    get_service,
    resolve_config,
    validate_config,
)
from utils.logging import get_logger

logger = get_logger(__name__)

cas_straddle_bp = Blueprint("cas_straddle_bp", __name__, url_prefix=f"/{STRATEGY_NAME}")


def _extract_api_key() -> str | None:
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


def _session_ok() -> bool:
    try:
        from utils.session import is_session_valid

        return bool(is_session_valid())
    except Exception:
        return False


def _authed_for_read() -> bool:
    return _authed() or _session_ok()


def _unauthorized():
    return jsonify({"status": "error", "message": "Invalid or missing API key"}), 401


def _service_or_503():
    svc = get_service()
    if svc is None:
        return None, (
            jsonify({"status": "error", "message": "cas_straddle service not initialised"}),
            503,
        )
    return svc, None


@cas_straddle_bp.route("/api/status", methods=["GET"])
def status():
    """Mode, config, today's per-underlying state (expiry day, ATM, legs, target eval)."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.get_status()})
    except Exception as e:
        logger.exception("cas_straddle status failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/config", methods=["GET", "POST"])
def config():
    """UI-editable strategy config.

    GET  → ``{defaults, override (DB row or null), effective, applies_at}``.
    POST → validate + upsert the single row (NULL clears an override). Applies
    at the next 15:12 IST arm (today's, if saved before 15:12).
    """
    if not _authed_for_read():
        return _unauthorized()
    from database.cas_straddle_db import get_config, save_config

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        values, errors = validate_config(body)
        if errors:
            return jsonify({"status": "error", "message": "; ".join(errors), "errors": errors}), 400
        if not values:
            return jsonify({"status": "error", "message": "no config fields in body"}), 400
        who = "api" if _authed() else "ui"
        stored = save_config(values, updated_by=who)
        if stored is None:
            return jsonify({"status": "error", "message": "config save failed"}), 500
        return jsonify(
            {
                "status": "success",
                "data": {
                    "defaults": DEFAULTS,
                    "override": stored,
                    "effective": resolve_config(stored),
                    "applies_at": "next 15:12 IST arm",
                },
            }
        )
    try:
        row = get_config()
        payload = {
            "defaults": DEFAULTS,
            "override": row,
            "effective": resolve_config(row),
            "applies_at": "next 15:12 IST arm",
        }
        svc = get_service()
        if svc is not None:
            payload["day_config"] = svc.day.get("config")
            payload["lotsizes"] = {
                u: (st.get("lotsize") if st else None) for u, st in (svc.day.get("u") or {}).items()
            }
        return jsonify({"status": "success", "data": payload})
    except Exception as e:
        logger.exception("cas_straddle config failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/positions", methods=["GET"])
def positions():
    """Today's journal rows (all legs, all fill classes) + in-memory legs."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        from database.cas_straddle_db import trade_to_dict, trades_for_date

        tds = request.args.get("date") or svc.trade_date()
        rows = [trade_to_dict(r) for r in trades_for_date(tds)]
        return jsonify({"status": "success", "data": {"trade_date": tds, "trades": rows}})
    except Exception as e:
        logger.exception("cas_straddle positions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/sessions", methods=["GET"])
def sessions():
    """Per-(date, underlying) session digests, newest first (``?limit=``)."""
    if not _authed_for_read():
        return _unauthorized()
    try:
        from database.cas_straddle_db import session_to_dict
        from database.cas_straddle_db import sessions as _sessions

        try:
            limit = max(1, min(int(request.args.get("limit", "60")), 500))
        except ValueError:
            return jsonify({"status": "error", "message": "invalid limit"}), 400
        return jsonify(
            {
                "status": "success",
                "data": {"sessions": [session_to_dict(s) for s in _sessions(limit)]},
            }
        )
    except Exception as e:
        logger.exception("cas_straddle sessions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/polls", methods=["GET"])
def polls():
    """Raw poll observations for one date (``?date=YYYY-MM-DD&underlying=&limit=``)."""
    if not _authed_for_read():
        return _unauthorized()
    try:
        from database.cas_straddle_db import poll_to_dict, polls_for

        svc = get_service()
        tds = request.args.get("date") or (svc.trade_date() if svc else None)
        if not tds:
            return jsonify({"status": "error", "message": "date required"}), 400
        underlying = request.args.get("underlying") or None
        try:
            limit = max(1, min(int(request.args.get("limit", "5000")), 50000))
        except ValueError:
            return jsonify({"status": "error", "message": "invalid limit"}), 400
        rows = [poll_to_dict(p) for p in polls_for(tds, underlying, limit)]
        return jsonify({"status": "success", "data": {"trade_date": tds, "polls": rows}})
    except Exception as e:
        logger.exception("cas_straddle polls failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/pause", methods=["POST"])
def pause():
    """Halt new entries (exits and the fallback flatten still run)."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.pause()})
    except Exception as e:
        logger.exception("cas_straddle pause failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/resume", methods=["POST"])
def resume():
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.resume()})
    except Exception as e:
        logger.exception("cas_straddle resume failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@cas_straddle_bp.route("/api/close_all", methods=["POST"])
def close_all():
    """Emergency square-off of every open leg. Requires body {"confirm":"yes"}."""
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
        logger.exception("cas_straddle close_all failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
