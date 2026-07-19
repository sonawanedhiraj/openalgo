"""Control/observability API for the open15_vol_breakout strategy (issue #425).

GET /open15_vol_breakout/api/status
    Live state: mode, day status (armed / skipped_late_boot / done), today's
    selection with gaps, entries, and positions. Session auth (same as the
    other strategy blueprints); read-only.

GET /open15_vol_breakout/api/trades?limit=N&date=YYYY-MM-DD
    Journal rows (research fields included: level / trigger second / trigger
    price / entry-minute close) — the captured-drift measurement data.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

open15_bp = Blueprint("open15_bp", __name__, url_prefix="/open15_vol_breakout")


@open15_bp.route("/api/status", methods=["GET"])
@check_session_validity
def status():
    from services.open15_breakout_service import get_open15_service

    svc = get_open15_service()
    if svc is None:
        return jsonify(
            {
                "strategy": "open15_vol_breakout",
                "enabled": False,
                "message": "service not initialized (OPEN15_ENABLED=false?)",
            }
        )
    return jsonify(svc.get_status())


@open15_bp.route("/api/decision_log", methods=["GET"])
@check_session_validity
def decision_log():
    """Decision timeline for the 15-min window. Today = live from the service;
    past dates = persisted snapshot (written at 09:30 flatten / 09:35 summary)."""
    import datetime as dt

    import pytz

    from services.open15_breakout_service import get_open15_service

    date = request.args.get("date")
    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    svc = get_open15_service()
    if (not date or date == today) and svc is not None and svc.day_log:
        return jsonify({"date": today, "source": "live", "events": svc.day_log})
    from database.open15_breakout_db import get_day_log

    events = get_day_log(date or today)
    return jsonify({"date": date or today, "source": "snapshot", "events": events or []})


_LOGS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>open15_vol_breakout — decision log</title>
<style>
 body{font-family:ui-monospace,Consolas,monospace;background:#0f1419;color:#d7dde4;margin:24px}
 h2{color:#7dc4e4} table{border-collapse:collapse;width:100%;margin-top:8px}
 td,th{border-bottom:1px solid #2a3138;padding:4px 10px;text-align:left;font-size:13px;vertical-align:top}
 th{color:#8aa0b4} .ev-entry{color:#a6e3a1}.ev-exit{color:#f9e2af}.ev-no_entry{color:#f38ba8}
 .ev-selection{color:#89b4fa}.ev-armed{color:#94e2d5}.ev-summary{color:#cba6f7}
 .ev-skipped_late_boot,.ev-skipped_no_prev_closes{color:#f38ba8;font-weight:bold}
 input{background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px 8px}
 .muted{color:#6b7886;font-size:12px}
</style></head><body>
<h2>open15_vol_breakout — decision log</h2>
<div class="muted">Live during 09:15–09:30 IST (auto-refresh 5s), snapshots after.
 <input id="d" type="date"> <button onclick="load()">load</button>
 <a href="/open15_vol_breakout/api/trades" style="color:#7dc4e4">trades json</a></div>
<div id="status" class="muted"></div>
<table id="t"><thead><tr><th>time</th><th>event</th><th>detail</th></tr></thead><tbody></tbody></table>
<script>
async function load(){
  const d=document.getElementById('d').value;
  const r=await fetch('/open15_vol_breakout/api/decision_log'+(d?('?date='+d):''));
  const j=await r.json();
  document.getElementById('status').textContent=j.date+' ('+j.source+') — '+(j.events||[]).length+' events';
  const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  for(const e of (j.events||[])){
    const {ts,event,...rest}=e;
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+ts+'</td><td class="ev-'+event+'">'+event+'</td><td>'+
      JSON.stringify(rest).slice(1,-1).replaceAll('"','')+'</td>';
    tb.appendChild(tr);
  }
}
load(); setInterval(()=>{if(!document.getElementById('d').value) load();},5000);
</script></body></html>"""


@open15_bp.route("/logs", methods=["GET"])
@check_session_validity
def logs_page():
    """Self-contained decision-log viewer (no frontend build required)."""
    return _LOGS_PAGE


@open15_bp.route("/api/trades", methods=["GET"])
@check_session_validity
def trades():
    from database.open15_breakout_db import Open15Trade, db_session

    limit = min(int(request.args.get("limit", 100)), 500)
    date = request.args.get("date")
    try:
        q = db_session.query(Open15Trade)
        if date:
            q = q.filter(Open15Trade.trade_date == date)
        rows = q.order_by(Open15Trade.id.desc()).limit(limit).all()
        return jsonify(
            [
                {
                    "id": r.id,
                    "trade_date": r.trade_date,
                    "symbol": r.symbol,
                    "side": r.side,
                    "mode": r.mode,
                    "gap_pct": r.gap_pct,
                    "level": r.level,
                    "baseline_vol": r.baseline_vol,
                    "cum_vol_at_trigger": r.cum_vol_at_trigger,
                    "trigger_minute": r.trigger_minute,
                    "trigger_second": r.trigger_second,
                    "trigger_price": r.trigger_price,
                    "entry_minute_close": r.entry_minute_close,
                    "quantity": r.quantity,
                    "entry_status": r.entry_status,
                    "exit_ts": r.exit_ts,
                    "exit_price": r.exit_price,
                    "exit_status": r.exit_status,
                    "pnl": r.pnl,
                    "status": r.status,
                    "reason": r.reason,
                }
                for r in rows
            ]
        )
    except Exception:
        logger.exception("open15: trades query failed")
        return jsonify([]), 500
    finally:
        db_session.remove()
