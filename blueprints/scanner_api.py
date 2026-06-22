"""API endpoints for the in-house scanner browser (Tier 1 + Tier 2).

Endpoints consumed by the React /scanner page:

  GET  /scanner/api/definitions
      All scan_definitions (enabled and disabled), enabled-first.  Each entry
      carries its latest 5 signals and today's hit count.

  GET  /scanner/api/definitions/<id>/signals
      Signal history for a single definition.  Accepts ``?since=<iso>``
      (default: now-24h), ``?until=<iso>`` (optional ceiling), and
      ``?limit=<int>`` (default: 200, max: 500).

  POST /scanner/api/definitions/<id>/toggle
      Flip a definition's enabled state (1→0 or 0→1).  Returns the new state.

  GET  /scanner/api/hits-by-symbol
      Aggregate hits by symbol for a given date (default: today IST).
      Returns [{symbol, hit_count, definitions, latest_hit}] sorted by
      hit_count descending.

Authentication: Flask session (same as /chartink/api/scanner-comparison).
This is a React-facing endpoint served inside the authenticated app, so
session auth is the natural fit — no API key header required.
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


def _unauthorized():
    return jsonify({"status": "error", "message": "Session expired"}), 401


@scanner_api_bp.route("/api/definitions", methods=["GET"])
@check_session_validity
def list_definitions():
    """List all scan_definitions (enabled and disabled) with latest signals and today hit count.

    Enabled definitions sort before disabled ones; within each group, alphabetical by name.
    """
    if not session.get("user"):
        return _unauthorized()

    try:
        sess = db_session()
        # Return ALL definitions — enabled-first, then alphabetical
        defs = (
            sess.query(ScanDefinition)
            .order_by(ScanDefinition.enabled.desc(), ScanDefinition.name)
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
        until   ISO-8601 string (optional ceiling; not applied when absent)
        limit   int (default 200, max 500)
    """
    if not session.get("user"):
        return _unauthorized()

    try:
        since_raw = (request.args.get("since") or "").strip()
        since = since_raw if since_raw else _iso_minus_hours(24)

        until_raw = (request.args.get("until") or "").strip()
        until: str | None = until_raw if until_raw else None

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

        q = sess.query(ScanResult).filter(
            ScanResult.scan_definition_id == definition_id,
            ScanResult.run_at >= since,
        )
        if until:
            q = q.filter(ScanResult.run_at <= until)

        rows = q.order_by(ScanResult.run_at.desc()).limit(limit).all()

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
                    "until": until,
                    "limit": limit,
                    "count": len(signals),
                },
            }
        )

    except Exception as e:
        logger.exception("scanner get_signals %s failed: %s", definition_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@scanner_api_bp.route("/api/definitions/<int:definition_id>/toggle", methods=["POST"])
@check_session_validity
def toggle_definition(definition_id: int):
    """Flip a definition's enabled state (1→0 or 0→1).

    Returns {"status": "success", "data": {"id": ..., "enabled": bool}}.
    """
    if not session.get("user"):
        return _unauthorized()

    try:
        sess = db_session()
        defn = sess.query(ScanDefinition).filter(ScanDefinition.id == definition_id).first()
        if defn is None:
            return jsonify({"status": "error", "message": "Definition not found"}), 404

        new_state = 0 if defn.enabled else 1
        defn.enabled = new_state
        defn.updated_at = _now_ist_iso()
        sess.commit()

        logger.info("scanner definition %s toggled → enabled=%s", definition_id, bool(new_state))
        return jsonify(
            {"status": "success", "data": {"id": definition_id, "enabled": bool(new_state)}}
        )

    except Exception as e:
        logger.exception("scanner toggle_definition %s failed: %s", definition_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@scanner_api_bp.route("/api/hits-by-symbol", methods=["GET"])
@check_session_validity
def hits_by_symbol():
    """Aggregate signal hits by symbol for a given date.

    Query params:
        date    YYYY-MM-DD (default: today IST)

    Returns:
        {date, symbols: [{symbol, hit_count, definitions, latest_hit}]}
        sorted by hit_count descending.
    """
    if not session.get("user"):
        return _unauthorized()

    try:
        date_str = (request.args.get("date") or "").strip() or _today_ist()

        sess = db_session()
        rows = (
            sess.query(ScanResult, ScanDefinition)
            .join(ScanDefinition, ScanResult.scan_definition_id == ScanDefinition.id)
            .filter(ScanResult.run_at.like(f"{date_str}%"))
            .order_by(ScanResult.run_at.desc())
            .all()
        )

        # Aggregate per symbol across all definitions
        symbol_map: dict[str, dict] = {}
        for result, defn in rows:
            symbols = _parse_symbols(result.symbols)
            for sym in symbols:
                if sym not in symbol_map:
                    symbol_map[sym] = {
                        "symbol": sym,
                        "hit_count": 0,
                        "definitions": set(),
                        "latest_hit": result.run_at,
                    }
                symbol_map[sym]["hit_count"] += 1
                symbol_map[sym]["definitions"].add(defn.name)
                if result.run_at > symbol_map[sym]["latest_hit"]:
                    symbol_map[sym]["latest_hit"] = result.run_at

        symbols_list = [
            {
                "symbol": v["symbol"],
                "hit_count": v["hit_count"],
                "definitions": sorted(v["definitions"]),
                "latest_hit": v["latest_hit"],
            }
            for v in symbol_map.values()
        ]
        symbols_list.sort(key=lambda x: x["hit_count"], reverse=True)

        return jsonify({"status": "success", "data": {"date": date_str, "symbols": symbols_list}})

    except Exception as e:
        logger.exception("scanner hits_by_symbol failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
