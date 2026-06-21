"""Read-only API endpoints for the in-house scanner browser (Tier 1).

Two endpoints consumed by the React /scanner page:

  GET /scanner/api/definitions
      All enabled scan_definitions with their latest 5 signals and today's
      hit count.  Returns the full definition list so the index page can
      render without extra round-trips.

  GET /scanner/api/definitions/<id>/signals
      Signal history for a single definition.  Accepts ``?since=<iso>``
      (default: now-24h) and ``?limit=<int>`` (default: 200, max: 500).

Authentication: Flask session (same as /chartink/api/scanner-comparison).
This is a React-facing endpoint served inside the authenticated app, so
session auth is the natural fit — no API key header required.

READ-ONLY.  These endpoints never write to any database.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytz
from flask import Blueprint, jsonify, request, session

from database.scanner_db import ScanDefinition, ScanResult, db_session
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

scanner_api_bp = Blueprint("scanner_api_bp", __name__, url_prefix="/scanner")

_SIGNAL_LIMIT_MAX = 500
_SIGNAL_LIMIT_DEFAULT = 200
_LATEST_SIGNAL_COUNT = 5


def _now_ist_iso() -> str:
    return datetime.now(_IST).isoformat()


def _iso_minus_hours(hours: int) -> str:
    """Return an ISO-8601 string for now-minus-hours in IST."""
    return (datetime.now(_IST) - timedelta(hours=hours)).isoformat()


def _today_ist() -> str:
    """Return today's date string in IST (YYYY-MM-DD)."""
    return datetime.now(_IST).strftime("%Y-%m-%d")


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except Exception:
        return []


@scanner_api_bp.route("/api/definitions", methods=["GET"])
@check_session_validity
def list_definitions():
    """List all enabled scan_definitions with latest signals and today hit count."""
    user_id = session.get("user")
    if not user_id:
        return jsonify({"status": "error", "message": "Session expired"}), 401

    try:
        sess = db_session()
        defs = (
            sess.query(ScanDefinition)
            .filter(ScanDefinition.enabled == 1)
            .order_by(ScanDefinition.name)
            .all()
        )

        today_str = _today_ist()
        result = []

        for d in defs:
            # latest N signals
            latest_rows = (
                sess.query(ScanResult)
                .filter(ScanResult.scan_definition_id == d.id)
                .order_by(ScanResult.run_at.desc())
                .limit(_LATEST_SIGNAL_COUNT)
                .all()
            )
            latest_signals = [
                {
                    "id": r.id,
                    "run_at": r.run_at,
                    "symbols": _parse_symbols(r.symbols),
                    "source": r.source,
                    "posted_to_engine": bool(r.posted_to_engine),
                }
                for r in latest_rows
            ]

            # today's hit count — rows where run_at starts with today's date
            today_count = (
                sess.query(ScanResult)
                .filter(
                    ScanResult.scan_definition_id == d.id,
                    ScanResult.run_at.like(f"{today_str}%"),
                )
                .count()
            )

            result.append(
                {
                    "id": d.id,
                    "name": d.name,
                    "screener_type": d.screener_type,
                    "rule_module": d.rule_module,
                    "enabled": bool(d.enabled),
                    "created_at": d.created_at,
                    "updated_at": d.updated_at,
                    "latest_signals": latest_signals,
                    "today_hit_count": today_count,
                }
            )

        return jsonify({"status": "success", "data": result})

    except Exception as e:
        logger.exception("scanner list_definitions failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@scanner_api_bp.route("/api/definitions/<int:definition_id>/signals", methods=["GET"])
@check_session_validity
def get_signals(definition_id: int):
    """Signal history for a single definition.

    Query params:
        since   ISO-8601 string (default: now - 24h)
        limit   int (default 200, max 500)
    """
    user_id = session.get("user")
    if not user_id:
        return jsonify({"status": "error", "message": "Session expired"}), 401

    try:
        since_raw = (request.args.get("since") or "").strip()
        since = since_raw if since_raw else _iso_minus_hours(24)

        try:
            limit = int(request.args.get("limit", _SIGNAL_LIMIT_DEFAULT))
        except ValueError:
            limit = _SIGNAL_LIMIT_DEFAULT
        limit = min(max(1, limit), _SIGNAL_LIMIT_MAX)

        sess = db_session()

        # Verify the definition exists
        defn = sess.query(ScanDefinition).filter(ScanDefinition.id == definition_id).first()
        if defn is None:
            return jsonify({"status": "error", "message": "Definition not found"}), 404

        rows = (
            sess.query(ScanResult)
            .filter(
                ScanResult.scan_definition_id == definition_id,
                ScanResult.run_at >= since,
            )
            .order_by(ScanResult.run_at.desc())
            .limit(limit)
            .all()
        )

        signals = [
            {
                "id": r.id,
                "run_at": r.run_at,
                "symbols": _parse_symbols(r.symbols),
                "source": r.source,
                "posted_to_engine": bool(r.posted_to_engine),
                "notes": r.notes,
            }
            for r in rows
        ]

        return jsonify(
            {
                "status": "success",
                "data": {
                    "definition": {
                        "id": defn.id,
                        "name": defn.name,
                        "screener_type": defn.screener_type,
                        "rule_module": defn.rule_module,
                        "enabled": bool(defn.enabled),
                        "created_at": defn.created_at,
                        "updated_at": defn.updated_at,
                    },
                    "signals": signals,
                    "since": since,
                    "limit": limit,
                    "count": len(signals),
                },
            }
        )

    except Exception as e:
        logger.exception("scanner get_signals %s failed: %s", definition_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500
