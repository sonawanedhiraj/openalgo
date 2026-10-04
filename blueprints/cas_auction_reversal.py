"""Observability + operator-control endpoints for cas_auction_reversal (issue #752).

Additive blueprint over the ``CasAuctionReversalService`` singleton
(services/cas_auction_reversal_service.py). URL prefix: ``/cas_auction_reversal``.

Auth follows ``blueprints/cas_straddle.py``: mutating endpoints take an API key
(``X-API-KEY`` header / ``apikey`` in body or query); read-only endpoints ALSO
accept a valid logged-in browser session. ``@check_session_validity`` is
deliberately NOT used — its failure path revokes broker tokens.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.cas_auction_reversal_service import (
    BOUNDS,
    DEFAULTS,
    STRATEGY_NAME,
    config_sources,
    get_service,
    resolve_config,
    validate_config,
)
from utils.logging import get_logger

logger = get_logger(__name__)

cas_auction_reversal_bp = Blueprint(
    "cas_auction_reversal_bp", __name__, url_prefix=f"/{STRATEGY_NAME}"
)


def _extract_api_key() -> str | None:
    key = request.headers.get("X-API-KEY")
    if not key:
        key = (request.get_json(silent=True) or {}).get("apikey")
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


def _unauthorized():
    return jsonify({"status": "error", "message": "Invalid or missing API key"}), 401


def _service_or_503():
    svc = get_service()
    if svc is None:
        return None, (
            jsonify({"status": "error", "message": "cas_auction_reversal service not initialised"}),
            503,
        )
    return svc, None


@cas_auction_reversal_bp.route("/api/status", methods=["GET"])
def status():
    """Mode, config, today's arm/poll/decision state and entries."""
    if not (_authed() or _session_ok()):
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    return jsonify({"status": "success", "data": svc.get_status()})


APPLIES_AT = "next 15:14:30 IST arm"


def _config_payload(row: dict | None) -> dict:
    svc = get_service()
    return {
        "defaults": DEFAULTS,
        "bounds": {
            k: {"min": lo, "max": hi, "integer": typ is int} for k, (lo, hi, typ) in BOUNDS.items()
        },
        "override": row,
        "effective": resolve_config(row),
        "sources": config_sources(row),
        "applies_at": APPLIES_AT,
        # what TODAY's arm actually ran with (None before 15:14:30 / after a restart)
        "day_config": (svc.day.get("config") if svc is not None else None),
    }


@cas_auction_reversal_bp.route("/api/config", methods=["GET", "POST"])
def config():
    """UI-editable config (issue #755).

    GET  → ``{defaults, bounds, override, effective, sources, applies_at, day_config}``.
    POST → validate (out-of-range values are REFUSED, never clamped) + upsert the
    single ``cas_ar_config`` row; ``null`` clears a field back to env/default.
    Applies at the next 15:14:30 IST arm.
    """
    if not (_authed() or _session_ok()):
        return _unauthorized()
    from database.cas_auction_reversal_db import get_config, save_config

    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            values, errors = validate_config(body)
            if errors:
                return (
                    jsonify({"status": "error", "message": "; ".join(errors), "errors": errors}),
                    400,
                )
            if not values:
                return jsonify({"status": "error", "message": "no config fields in body"}), 400
            stored = save_config(values, updated_by="api" if _authed() else "ui")
            if stored is None:
                return jsonify({"status": "error", "message": "config save failed"}), 500
            logger.info("cas_ar config saved: %s (applies at the %s)", values, APPLIES_AT)
            return jsonify({"status": "success", "data": _config_payload(stored)})
        return jsonify({"status": "success", "data": _config_payload(get_config())})
    except Exception:
        logger.exception("cas_ar config endpoint failed")
        return jsonify({"status": "error", "message": "config read/save failed"}), 500


@cas_auction_reversal_bp.route("/api/candidates", methods=["GET"])
def candidates():
    """Every symbol that crossed the threshold on ``?date=YYYY-MM-DD`` (default
    today): reference print, indicative close at the decision, selected / skip
    reason, and (after 15:45) the final auction close."""
    if not (_authed() or _session_ok()):
        return _unauthorized()
    svc = get_service()
    day = request.args.get("date") or (svc.trade_date() if svc else None)
    if not day:
        return jsonify({"status": "error", "message": "date required"}), 400
    try:
        from database.cas_auction_reversal_db import (
            candidate_to_dict,
            candidates_for_date,
            poll_count,
        )

        rows = [candidate_to_dict(c) for c in candidates_for_date(day)]
        return jsonify(
            {
                "status": "success",
                "data": {"date": day, "polls": poll_count(day), "candidates": rows},
            }
        )
    except Exception:
        logger.exception("cas_ar candidates endpoint failed")
        return jsonify({"status": "error", "message": "candidates read failed"}), 500


@cas_auction_reversal_bp.route("/api/trades", methods=["GET"])
def trades():
    """Most recent trades (entries + T+1 exits), newest first."""
    if not (_authed() or _session_ok()):
        return _unauthorized()
    try:
        from database.cas_auction_reversal_db import recent_trades, trade_to_dict

        limit = max(1, min(int(request.args.get("limit", 50)), 200))
        return jsonify(
            {"status": "success", "data": [trade_to_dict(r) for r in recent_trades(limit)]}
        )
    except Exception:
        logger.exception("cas_ar trades endpoint failed")
        return jsonify({"status": "error", "message": "trades read failed"}), 500


@cas_auction_reversal_bp.route("/api/pause", methods=["POST"])
def pause():
    """Hold new entries (durable runtime override). T+1 exits still run."""
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    return jsonify(svc.pause())


@cas_auction_reversal_bp.route("/api/resume", methods=["POST"])
def resume():
    if not _authed():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    return jsonify(svc.resume())
