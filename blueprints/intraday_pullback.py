"""Control + observability API for the intraday_pullback_top2 strategy (issue #394).

Routes under ``/intraday_pullback_top2/api/*``. Mirrors ``blueprints/futures_follow.py``:
mutating routes require an API key; read-only routes accept an API key OR a logged-in session.
"""

from flask import Blueprint, jsonify, request

from database.auth_db import verify_api_key
from services.intraday_pullback_core import TRADE_SIDES
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


def _parse_hhmm(s):
    try:
        hh, mm = str(s).split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return h * 60 + m
    except Exception:  # noqa: BLE001
        pass
    return None


@intraday_pullback_bp.route("/api/settings", methods=["GET"])
def get_settings():
    """Current editable settings + computed deployable capital / realized P&L (for the UI form)."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        return jsonify({"status": "success", "data": svc.current_settings()})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback get_settings failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/settings", methods=["POST"])
def update_settings():
    """Validate + persist the editable settings, then apply to the running service.

    Accepts a logged-in web session OR an API key (the React settings page saves over the
    session cookie, same as the strategies-dashboard mutations)."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    body = request.get_json(silent=True) or {}
    errors = []
    fields = {}

    cap = body.get("base_capital")
    try:
        cap = float(cap)
        if not (10000 <= cap <= 10_000_000):
            errors.append("base_capital must be between 10,000 and 1,00,00,000")
        else:
            fields["base_capital"] = cap
    except (TypeError, ValueError):
        errors.append("base_capital must be a number")

    sm = str(body.get("sizing_mode", "")).lower()
    if sm in ("fixed", "compound", "capped"):
        fields["sizing_mode"] = sm
    else:
        errors.append("sizing_mode must be fixed | compound | capped")

    # Trade side (issue #509). Omitted -> keep whatever is stored (the settings
    # form always sends it, but an API-key caller updating only capital should
    # not silently reset the operator's side selection).
    if "trade_side" in body:
        ts = str(body.get("trade_side", "")).lower()
        if ts in TRADE_SIDES:
            fields["trade_side"] = ts
        else:
            errors.append(f"trade_side must be one of {' | '.join(TRADE_SIDES)}")

    nts, nte = _parse_hhmm(body.get("no_trade_start")), _parse_hhmm(body.get("no_trade_end"))
    afs, afe = _parse_hhmm(body.get("afternoon_start")), _parse_hhmm(body.get("afternoon_end"))
    OPEN, FLAT = 9 * 60 + 30, 15 * 60 + 10
    if None in (nts, nte, afs, afe):
        errors.append("windows must be valid HH:MM times")
    else:
        if not (OPEN < nts < nte):
            errors.append("no-trade start must be after 09:30 and before its end")
        if nte != afs:
            errors.append("no-trade end must equal afternoon start (contiguous windows)")
        if not (afs < afe <= FLAT):
            errors.append("afternoon window must end by 15:10 and after its start")
        if not errors:
            fields.update(
                no_trade_start=body["no_trade_start"],
                no_trade_end=body["no_trade_end"],
                afternoon_start=body["afternoon_start"],
                afternoon_end=body["afternoon_end"],
            )

    if errors:
        return jsonify({"status": "error", "message": "; ".join(errors)}), 400

    try:
        from database.intraday_pullback_config_db import set_config
        from services.intraday_pullback_service import STRATEGY_NAME

        set_config(STRATEGY_NAME, updated_by="ui", **fields)
        svc._apply_editable_config()  # reflect immediately (affects new entries only)
        return jsonify({"status": "success", "data": svc.current_settings()})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback update_settings failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/settings/reset", methods=["POST"])
def reset_settings():
    """Delete operator overrides -> revert to config_snapshot.json defaults."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        from database.intraday_pullback_config_db import delete_config
        from services.intraday_pullback_service import STRATEGY_NAME

        delete_config(STRATEGY_NAME)
        svc._apply_editable_config()
        return jsonify({"status": "success", "data": svc.current_settings()})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback reset_settings failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@intraday_pullback_bp.route("/api/entry_breakdown", methods=["GET"])
def entry_breakdown():
    """Per-pick evaluation for a day (why entries did/didn't fire). ?date=YYYY-MM-DD for history;
    defaults to today's live breakdown, falling back to the persisted EOD snapshot."""
    if not _authed_for_read():
        return _unauthorized()
    svc, err = _service_or_503()
    if err:
        return err
    try:
        date = request.args.get("date")
        if not date or date == svc.today_date:
            live = svc.entry_breakdown()
            if live.get("selected"):
                return jsonify({"status": "success", "data": live, "source": "live"})
            date = svc.today_date
        from database import intraday_pullback_eval_db
        from services.intraday_pullback_service import STRATEGY_NAME

        snap = intraday_pullback_eval_db.get_snapshot(STRATEGY_NAME, date)
        if snap is None:
            return jsonify(
                {
                    "status": "success",
                    "data": None,
                    "source": "none",
                    "message": f"no evaluation recorded for {date}",
                }
            )
        return jsonify({"status": "success", "data": snap, "source": "persisted"})
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback entry_breakdown failed: %s", e)
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
