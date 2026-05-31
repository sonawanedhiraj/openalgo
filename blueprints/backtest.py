"""``/backtest`` — MVP backtester trigger + inspection endpoints.

Routes:

* ``POST /backtest/run``           — kick off a synchronous backtest. Body is
  JSON; ``symbols``, ``from_date``, ``to_date`` are required. Returns the
  new ``run_id`` on success. Small backtests complete in seconds so a
  synchronous response is fine; long-horizon multi-symbol runs are out of
  scope for the MVP.
* ``GET  /backtest/<run_id>``      — returns the run row + summary metrics.
* ``GET  /backtest/<run_id>/trades`` — returns every trade recorded for a run.
* ``GET  /backtest/recent``        — recent runs, newest first.

Like ``/journal/*`` and ``/preflight``, these are intentionally
unauthenticated — they expose no secrets, never touch live trading state,
and are meant to be called by local automation.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from services import backtest_service
from utils.logging import get_logger

logger = get_logger(__name__)

backtest_bp = Blueprint("backtest_bp", __name__)


def _int_arg(name: str, default: int, *, minimum: int = 1) -> int:
    raw = request.args.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(value, minimum)


@backtest_bp.route("/backtest/run", methods=["POST"])
def trigger_run():
    """Launch a backtest synchronously and return the new ``run_id``.

    Required body fields: ``symbols`` (list[str]), ``from_date``, ``to_date``.
    Optional: ``strategy_name``, ``rule_names``, ``interval``, ``atr_period``,
    ``atr_sl_mult``, ``rr_target``, ``position_size``, ``eod_time_ist``,
    ``exchange``.
    """
    payload = request.get_json(silent=True) or {}

    symbols = payload.get("symbols")
    from_date = payload.get("from_date")
    to_date = payload.get("to_date")

    missing = [
        name
        for name, value in (
            ("symbols", symbols),
            ("from_date", from_date),
            ("to_date", to_date),
        )
        if not value
    ]
    if missing:
        return jsonify({
            "status": "error",
            "message": f"missing required field(s): {', '.join(missing)}",
        }), 400

    if not isinstance(symbols, list) or not all(isinstance(s, str) for s in symbols):
        return jsonify({
            "status": "error",
            "message": "symbols must be a list of strings",
        }), 400

    kwargs = {
        "symbols": symbols,
        "from_date": from_date,
        "to_date": to_date,
    }
    for opt in (
        "strategy_name",
        "rule_names",
        "interval",
        "atr_period",
        "atr_sl_mult",
        "rr_target",
        "position_size",
        "eod_time_ist",
        "exchange",
    ):
        if opt in payload and payload[opt] is not None:
            kwargs[opt] = payload[opt]

    try:
        run_id = backtest_service.run_backtest(**kwargs)
        return jsonify({"status": "success", "run_id": run_id})
    except Exception as e:
        logger.exception("backtest.trigger_run failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/backtest/<int:run_id>", methods=["GET"])
def get_run_details(run_id: int):
    """Return the run row + summary stats (404 when the id is unknown)."""
    try:
        row = backtest_service.get_run(run_id)
        if not row:
            return jsonify({"status": "error", "message": "run not found"}), 404
        return jsonify(row)
    except Exception as e:
        logger.exception("backtest.get_run_details(%s) failed: %s", run_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/backtest/<int:run_id>/trades", methods=["GET"])
def get_run_trades(run_id: int):
    """Return every trade recorded for a run."""
    try:
        trades = backtest_service.get_run_trades(run_id)
        return jsonify({"run_id": run_id, "trades": trades})
    except Exception as e:
        logger.exception("backtest.get_run_trades(%s) failed: %s", run_id, e)
        return jsonify({"status": "error", "message": str(e)}), 500


@backtest_bp.route("/backtest/recent", methods=["GET"])
def list_recent_runs():
    """Return recent runs ordered newest-first. ``?limit=N`` (default 10)."""
    limit = _int_arg("limit", default=10)
    try:
        runs = backtest_service.get_recent_runs(limit=limit)
        return jsonify({"limit": limit, "runs": runs})
    except Exception as e:
        logger.exception("backtest.list_recent_runs failed: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500
