"""Control/observability API for the open15_vol_breakout strategy (issue #425).

GET /open15_vol_breakout/api/status
    Live state: mode, day status (armed / skipped_late_boot / done), today's
    selection with gaps, entries, and positions. Session auth (same as the
    other strategy blueprints); read-only.

GET /open15_vol_breakout/api/trades?limit=N&date=YYYY-MM-DD
    Journal rows (research fields included: level / trigger second / trigger
    price / entry-minute close) — the captured-drift measurement data.

GET /open15_vol_breakout/api/decision_log?date=YYYY-MM-DD
    One day's decision timeline (today live, past days from open15_day_logs).

GET /open15_vol_breakout/api/decision_log/days
    Digest of every stored day (status / selected / entered / P&L) for the
    history sidebar on /logs (issue #444).

GET /open15_vol_breakout/api/decision_log/export.csv
    All stored days flattened to one row per selected symbol — the
    backtest-facing export (issue #444).
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


@open15_bp.route("/api/config", methods=["GET", "POST"])
@check_session_validity
def config():
    """UI-editable strategy config (capital, sizing mode, volume filter).

    GET  -> {env_defaults, override (DB row or null), effective_today}.
    POST -> validate + upsert the single row. Applies at the NEXT 09:10 arm
    (or today's arm if saved before 09:10 IST).
    """
    import os as _os

    from database.open15_breakout_db import get_config, save_config
    from services.open15_breakout_service import TRADE_SIDES, get_open15_service

    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        errors = []
        margin = body.get("margin_per_slot")
        sizing = body.get("sizing_mode")
        vol = body.get("vol_mult")
        if margin is not None:
            try:
                margin = float(margin)
                if not (5_000 <= margin <= 500_000):
                    errors.append("margin_per_slot must be between 5000 and 500000")
            except (TypeError, ValueError):
                errors.append("margin_per_slot must be a number")
        if sizing is not None and sizing not in ("fixed", "compound"):
            errors.append("sizing_mode must be 'fixed' or 'compound'")
        if vol is not None:
            try:
                vol = float(vol)
                if not (1.0 <= vol <= 5.0):
                    errors.append("vol_mult must be between 1.0 and 5.0")
            except (TypeError, ValueError):
                errors.append("vol_mult must be a number")
        instrument = body.get("instrument")
        if instrument is not None and instrument not in ("stock", "atm_option"):
            errors.append("instrument must be 'stock' or 'atm_option'")
        trade_side = body.get("trade_side")
        if trade_side is not None and trade_side not in TRADE_SIDES:
            errors.append("trade_side must be 'both', 'long_only' or 'short_only'")
        max_trades = body.get("max_trades")
        if max_trades is not None:
            try:
                max_trades = int(max_trades)
                if not (1 <= max_trades <= 6):
                    errors.append("max_trades must be between 1 and 6")
            except (TypeError, ValueError):
                errors.append("max_trades must be an integer")
        no_entry_after = body.get("no_entry_after") or None  # empty input = env default
        exit_time = body.get("exit_time") or None
        if no_entry_after is not None or exit_time is not None:
            from services.open15_breakout_service import (
                _exit_time_default,
                _no_entry_after_default,
                validate_window,
            )

            # cross-field check against the effective value when only one is sent
            row = get_config() or {}
            errors.extend(
                validate_window(
                    no_entry_after or row.get("no_entry_after") or _no_entry_after_default(),
                    exit_time or row.get("exit_time") or _exit_time_default(),
                )
            )
        if errors:
            return jsonify({"status": "error", "errors": errors}), 400
        ok = save_config(
            margin,
            sizing,
            vol,
            updated_by="ui",
            instrument=instrument,
            max_trades=max_trades,
            no_entry_after=no_entry_after,
            exit_time=exit_time,
            trade_side=trade_side,
        )
        if not ok:
            return jsonify({"status": "error", "errors": ["save failed"]}), 500
        return jsonify({"status": "success", "saved": get_config()})

    svc = get_open15_service()
    return jsonify(
        {
            "env_defaults": {
                "margin_per_slot": float(_os.getenv("OPEN15_MARGIN_PER_SLOT", "30000")),
                "sizing_mode": _os.getenv("OPEN15_SIZING_MODE", "fixed"),
                "vol_mult": float(_os.getenv("OPEN15_VOL_MULT", "1.5")),
                "leverage": float(_os.getenv("OPEN15_LEVERAGE", "5")),
                "instrument": _os.getenv("OPEN15_INSTRUMENT", "stock"),
                "max_trades": int(_os.getenv("OPEN15_MAX_TRADES", "3")),
                "no_entry_after": _os.getenv("OPEN15_NO_ENTRY_AFTER", "09:29"),
                "exit_time": _os.getenv("OPEN15_EXIT_TIME", "09:30"),
                "trade_side": _os.getenv("OPEN15_TRADE_SIDE", "both"),
            },
            "override": get_config(),
            "effective_today": (svc.day_config if svc else None),
            "note": "changes apply at the next 09:10 IST arm",
        }
    )


@open15_bp.route("/api/decision_log", methods=["GET"])
@check_session_validity
def decision_log():
    """Decision timeline for the 15-min window. Today = live from the service;
    past dates = persisted snapshot (upserted on every logged event)."""
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


def _all_day_logs() -> list[tuple[str, list, str]]:
    """Persisted day logs newest-first, with today's live log overlaid.

    Returns ``[(date, events, source)]`` where source is ``live`` for the
    in-memory copy of today (fresher than — or identical to — the per-event
    snapshot) and ``snapshot`` for DB rows.
    """
    import datetime as dt

    import pytz

    from database.open15_breakout_db import list_day_logs
    from services.open15_breakout_service import get_open15_service

    today = dt.datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d")
    days = {d: (events, "snapshot") for d, events in list_day_logs()}
    svc = get_open15_service()
    if svc is not None and svc.day_log and getattr(svc, "_log_date", None) == today:
        days[today] = (svc.day_log, "live")
    return [(d, ev, src) for d, (ev, src) in sorted(days.items(), reverse=True)]


@open15_bp.route("/api/decision_log/days", methods=["GET"])
@check_session_validity
def decision_log_days():
    """Digest of every stored day for the history sidebar (issue #444)."""
    from database.open15_breakout_db import trades_pnl_by_date
    from services.open15_log_view import summarize_day

    try:
        pnl_by_date = trades_pnl_by_date()
        out = []
        for date, events, source in _all_day_logs():
            digest = summarize_day(date, events, trades_pnl=pnl_by_date.get(date))
            digest["source"] = source
            out.append(digest)
        return jsonify({"days": out})
    except Exception:
        logger.exception("open15: decision-log days digest failed")
        return jsonify({"days": []}), 500


@open15_bp.route("/api/decision_log/export.csv", methods=["GET"])
@check_session_validity
def decision_log_export_csv():
    """All stored days flattened to one CSV row per selected symbol (issue #444)."""
    from flask import Response

    from services.open15_log_view import render_csv, selection_outcomes

    try:
        rows = []
        for date, events, _source in reversed(_all_day_logs()):
            rows.extend(selection_outcomes(date, events))
        return Response(
            render_csv(rows),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=open15_decision_log.csv"},
        )
    except Exception:
        logger.exception("open15: decision-log CSV export failed")
        return jsonify({"status": "error", "message": "export failed"}), 500


_LOGS_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>open15_vol_breakout — decision log</title>
<style>
 body{font-family:ui-monospace,Consolas,monospace;background:#0f1419;color:#d7dde4;margin:24px}
 h2{color:#7dc4e4;margin-bottom:4px} table{border-collapse:collapse;width:100%;margin-top:8px}
 td,th{border-bottom:1px solid #2a3138;padding:4px 10px;text-align:left;font-size:13px;vertical-align:top}
 th{color:#8aa0b4} .ev-entry{color:#a6e3a1}.ev-exit{color:#f9e2af}.ev-no_entry{color:#f38ba8}
 .ev-selection{color:#89b4fa}.ev-armed{color:#94e2d5}.ev-summary{color:#cba6f7}
 .ev-watch_stats{color:#8aa0b4}
 .ev-skipped_late_boot,.ev-skipped_no_prev_closes{color:#f38ba8;font-weight:bold}
 input{background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px 8px}
 .muted{color:#6b7886;font-size:12px}
 button{background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px 10px;cursor:pointer}
 .layout{display:flex;gap:18px;align-items:flex-start;margin-top:12px}
 .side{width:200px;flex:none}
 .day{border:1px solid #2a3138;border-radius:6px;padding:6px 10px;margin-bottom:6px;cursor:pointer}
 .day:hover{background:#161d25}.day.sel{background:#1e2630;border-color:#7dc4e4}
 .day .d1{display:flex;justify-content:space-between}
 .pos{color:#a6e3a1}.neg{color:#f38ba8}
 .badge{font-size:10px;border-radius:8px;padding:1px 7px}
 .b-live{background:#12324a;color:#89b4fa}.b-skip{background:#463a20;color:#f9e2af}
 .main{flex:1;min-width:0}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
 .chip{background:#161d25;border-radius:6px;padding:6px 12px}
 .chip .k{display:block;font-size:10px;color:#6b7886}.chip .v{font-size:14px}
 .fbtn{font-size:11px;padding:2px 10px;border-radius:10px}.fbtn.on{background:#2a3138;color:#7dc4e4}
 .sec{color:#8aa0b4;font-size:12px;margin:14px 0 0}
</style></head><body>
<h2>open15_vol_breakout — decision log</h2>
<div class="muted">Every day is persisted — pick one from the history list. Today auto-refreshes 5s during the window.
 <a href="/open15_vol_breakout/api/trades" style="color:#7dc4e4">trades json</a>
 · <a href="/open15_vol_breakout/api/decision_log/export.csv" style="color:#7dc4e4">all days CSV</a>
 · <a id="dayjson" href="/open15_vol_breakout/api/decision_log" style="color:#7dc4e4">day JSON</a></div>
<fieldset style="border:1px solid #2a3138;border-radius:6px;margin:14px 0;padding:10px 14px">
 <legend style="color:#8aa0b4;font-size:13px;padding:0 6px">strategy config (applies at next 09:10 arm)</legend>
 <label class="muted">capital/slot (Rs) <input id="c_margin" type="number" min="5000" max="500000" step="1000" style="width:90px"></label>
 <label class="muted" style="margin-left:14px">sizing
  <select id="c_sizing" style="background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px">
   <option value="fixed">fixed</option><option value="compound">compounding</option></select></label>
 <label class="muted" style="margin-left:14px">volume filter (x avg) <input id="c_vol" type="number" min="1.0" max="5.0" step="0.1" style="width:60px"></label>
 <label class="muted" style="margin-left:14px">instrument
  <select id="c_instr" style="background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px">
   <option value="stock">stock</option><option value="atm_option">ATM option</option></select></label>
 <label class="muted" style="margin-left:14px">trade side
  <select id="c_side" style="background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px">
   <option value="both">both</option><option value="long_only">longs only</option><option value="short_only">shorts only</option></select></label>
 <label class="muted" style="margin-left:14px">max trades/day <input id="c_maxt" type="number" min="1" max="6" step="1" style="width:44px"></label>
 <label class="muted" style="margin-left:14px">no entry after <input id="c_nea" type="time" min="09:16" max="15:09" style="width:92px"></label>
 <label class="muted" style="margin-left:14px">exit time <input id="c_exit" type="time" min="09:17" max="15:10" style="width:92px"></label>
 <button onclick="saveCfg()" style="margin-left:14px;background:#1e2630;color:#a6e3a1;border:1px solid #2a3138;padding:4px 12px;cursor:pointer">save</button>
 <span id="c_msg" class="muted" style="margin-left:10px"></span>
 <div id="c_eff" class="muted" style="margin-top:6px"></div>
</fieldset>
<div class="layout">
 <div class="side">
  <div class="muted" style="margin-bottom:6px">history</div>
  <div id="days"></div>
 </div>
 <div class="main">
  <div id="status" class="muted"></div>
  <div class="chips" id="chips"></div>
  <div class="sec">selection outcomes</div>
  <table id="sel"><thead><tr><th>symbol</th><th>side</th><th>gap %</th><th id="selVolHdr">max vol&times;</th><th>outcome</th></tr></thead><tbody></tbody></table>
  <div class="sec" style="display:flex;align-items:center;gap:8px">event timeline
   <span style="flex:1"></span>
   <button class="fbtn on" data-f="all">all</button>
   <button class="fbtn" data-f="trade">entries</button>
   <button class="fbtn" data-f="no_entry">no-entry</button>
   <button class="fbtn" data-f="sys">system</button>
  </div>
  <table id="t"><thead><tr><th style="width:90px">time</th><th style="width:120px">event</th><th>detail</th></tr></thead><tbody></tbody></table>
 </div>
</div>
<script>
async function loadCfg(){
  const r=await fetch('/open15_vol_breakout/api/config'); const j=await r.json();
  const o=j.override||{}, d=j.env_defaults||{};
  document.getElementById('c_margin').value=o.margin_per_slot||d.margin_per_slot;
  document.getElementById('c_sizing').value=o.sizing_mode||d.sizing_mode||'fixed';
  document.getElementById('c_vol').value=o.vol_mult||d.vol_mult;
  document.getElementById('c_instr').value=o.instrument||d.instrument||'stock';
  document.getElementById('c_side').value=o.trade_side||d.trade_side||'both';
  document.getElementById('c_maxt').value=o.max_trades||d.max_trades||3;
  document.getElementById('c_nea').value=o.no_entry_after||d.no_entry_after||'09:29';
  document.getElementById('c_exit').value=o.exit_time||d.exit_time||'09:30';
  const e=j.effective_today;
  if(e){
    const nea=e.no_entry_after||'09:29', ext=e.exit_time||'09:30';
    const sd=e.trade_side||'both';
    const sdLbl={both:'both',long_only:'longs only',short_only:'shorts only'}[sd]||sd;
    document.getElementById('c_eff').textContent=
      'effective today: '+(e.instrument||'stock')+' | trade side '+sdLbl+
      (sd!=='both'?' ⚠ one-sided (parity targets measured both sides)':'')+
      ' | max trades '+(e.max_trades||3)+
      ' | '+e.sizing_mode+' | margin '+e.margin_effective+' (base '+e.margin_per_slot+
      (e.sizing_mode==='compound'?(' + cum P&L '+e.cum_realized_pnl):'')+
      ') x '+e.leverage+' = notional '+e.notional+' | vol_mult '+e.vol_mult+
      ' | entries 09:16–'+nea+' | exit '+ext+
      ((nea!=='09:29'||ext!=='09:30')?' ⚠ non-default window (R58 measured 09:29/09:30)':'');
  }
}
async function saveCfg(){
  const body={margin_per_slot:+document.getElementById('c_margin').value,
    sizing_mode:document.getElementById('c_sizing').value,
    vol_mult:+document.getElementById('c_vol').value,
    instrument:document.getElementById('c_instr').value,
    trade_side:document.getElementById('c_side').value,
    max_trades:+document.getElementById('c_maxt').value,
    no_entry_after:document.getElementById('c_nea').value,
    exit_time:document.getElementById('c_exit').value};
  const msg=document.getElementById('c_msg');
  try{
    // CSRFProtect is global (issue #446): the POST is rejected 400 without this token
    const tok=(await (await fetch('/auth/csrf-token')).json()).csrf_token||'';
    const r=await fetch('/open15_vol_breakout/api/config',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':tok},body:JSON.stringify(body)});
    const j=await r.json();
    msg.textContent=(j.status==='success')?'saved ✓'
      :('error: '+((j.errors&&j.errors.length)?j.errors.join('; '):(j.message||j.error||('HTTP '+r.status))));
  }catch(e){msg.textContent='error: '+e;}
  loadCfg();
}
loadCfg();
</script>
<script>
let curDate=null, curEvents=[], curFilter='all', digests=[];
// live max-vol overlay for today (issue #524): the tick-by-tick running max is
// only published to the decision log at the exit job, so mid-window it comes
// from /api/status instead. Cleared whenever a past day is selected.
let liveWatch={}, liveNeeded=null;
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function kindOf(e){
  if(e.event==='entry'||e.event==='exit'||e.event==='entry_skipped')return 'trade';
  if(e.event==='no_entry')return 'no_entry';
  return 'sys';
}
async function loadDays(){
  const r=await fetch('/open15_vol_breakout/api/decision_log/days'); const j=await r.json();
  digests=j.days||[];
  const box=document.getElementById('days'); box.innerHTML='';
  for(const d of digests){
    const el=document.createElement('div');
    el.className='day'+(d.date===curDate?' sel':'');
    const skip=d.status&&d.status.startsWith('skipped');
    const right=d.source==='live'?'<span class="badge b-live">live</span>'
      :skip?'<span class="badge b-skip">skipped</span>'
      :(d.pnl==null?'<span class="muted">—</span>'
        :'<span class="'+(d.pnl>=0?'pos':'neg')+'">'+(d.pnl>=0?'+':'')+'&#8377;'+Math.round(d.pnl)+'</span>');
    el.innerHTML='<div class="d1"><span>'+esc(d.date)+'</span>'+right+'</div>'+
      '<div class="muted">'+(skip?esc(d.status):(d.selected+' sel &middot; '+d.entered+' entered'))+'</div>';
    el.onclick=()=>selectDay(d.date);
    box.appendChild(el);
  }
  if(!curDate&&digests.length)selectDay(digests[0].date);
}
async function loadLiveWatch(){
  // running max vol ratio for today's still-open window (issue #524)
  try{
    const r=await fetch('/open15_vol_breakout/api/status'); const s=await r.json();
    liveWatch=s.watch_stats||{}; liveNeeded=s.vol_needed??null;
  }catch(e){liveWatch={}; liveNeeded=null;}
}
async function selectDay(date){
  curDate=date;
  document.getElementById('dayjson').href='/open15_vol_breakout/api/decision_log?date='+date;
  const r=await fetch('/open15_vol_breakout/api/decision_log?date='+date); const j=await r.json();
  curEvents=j.events||[];
  if(j.source==='live'){await loadLiveWatch();}else{liveWatch={}; liveNeeded=null;}
  document.getElementById('status').textContent=j.date+' ('+j.source+') — '+curEvents.length+' events';
  renderChips(); renderSel(); renderTimeline();
  document.querySelectorAll('#days .day').forEach(el=>
    el.classList.toggle('sel',el.querySelector('span').textContent===date));
}
function renderChips(){
  const armed=curEvents.find(e=>e.event==='armed')||{};
  const summ=curEvents.find(e=>e.event==='summary')||{};
  const dig=digests.find(d=>d.date===curDate)||{};
  const chips=[['status',summ.day||dig.status||'—'],['mode',armed.mode||'—'],
    ['universe',armed.universe??'—'],['vol&times;',armed.vol_mult??'—'],
    ['entries',(dig.entered??summ.entered??0)+' / '+(dig.selected??summ.selected??0)],
    ['day P&amp;L',dig.pnl==null?'—':((dig.pnl>=0?'+':'')+'\\u20B9'+Math.round(dig.pnl))]];
  document.getElementById('chips').innerHTML=chips.map(([k,v])=>
    '<div class="chip"><span class="k">'+k+'</span><span class="v'+
    (k==='day P&amp;L'&&dig.pnl!=null?(dig.pnl>=0?' pos':' neg'):'')+'">'+esc(v)+'</span></div>').join('');
}
function volNeeded(){
  const w=curEvents.find(e=>e.event==='watch_stats');
  if(w&&w.needed!=null)return w.needed;
  const a=curEvents.find(e=>e.event==='armed');
  if(a&&a.vol_mult!=null)return a.vol_mult;
  return liveNeeded;
}
function fmtVol(v,beyond,needed,live){
  if(v==null)return '<span class="muted">&mdash;</span>';
  // The displayed number is the peak ANYWHERE in the minute, but the gate is
  // `beyond and cum_in_min >= vol_mult*baseline` (issue #525) — so colour by
  // the while-beyond peak. Colouring the peak-anywhere number would paint
  // INDIGO green at 1.95x on a day it correctly never entered (1.27x beyond).
  const hit=(needed!=null&&beyond!=null&&beyond>=needed);
  const tip=beyond==null?'peak anywhere in the minute'
    :('peak anywhere '+v+'x; '+beyond+'x while beyond the level (what the gate compares)');
  return '<span class="'+(hit?'pos':'muted')+'" title="'+tip+'">'+v+'&times;</span>'+
    (live?' <span class="muted" style="font-size:10px">live</span>':'');
}
function renderSel(){
  const rows={};
  for(const e of curEvents){
    if(e.event==='selection'){
      for(const[s,side]of Object.entries(e.selected||{}))
        rows[s]={side,gap:(e.gaps_pct||{})[s],out:'no trigger'};
    }else if(e.event==='watch_stats'){
      // every selected symbol, entered ones included (issue #524)
      for(const[s,st]of Object.entries(e.stats||{}))
        if(rows[s]&&rows[s].vol==null){
          rows[s].vol=st.max_vol_ratio; rows[s].volBeyond=st.max_vol_ratio_beyond;
        }
    }else if(!rows[e.symbol]){continue;
    }else if(e.event==='entry'){
      rows[e.symbol].out='<span class="pos">entered @ '+e.trigger_price+
        (e.vol_ratio?(' &middot; vol '+e.vol_ratio+'&times;'):'')+'</span>';
    }else if(e.event==='exit'&&e.pnl!=null){
      rows[e.symbol].out+=' <span class="'+(e.pnl>=0?'pos':'neg')+'">&rarr; '+
        (e.pnl>=0?'+':'')+'&#8377;'+e.pnl+'</span>';
    }else if(e.event==='no_entry'){
      rows[e.symbol].vol=e.max_vol_ratio;
      rows[e.symbol].volBeyond=e.max_vol_ratio_while_beyond;
      // the gate compares the ratio measured WHILE price is beyond the level
      // (on_tick: `beyond and cum_in_min >= vol_mult*baseline`), so the outcome
      // must quote max_vol_ratio_while_beyond — the `max vol×` column's
      // peak-anywhere number can be >= needed on a symbol that never entered.
      const vb=e.max_vol_ratio_while_beyond??e.max_vol_ratio;
      rows[e.symbol].out=e.level_broken?('level broken &middot; vol '+vb+
        '&times; &lt; '+e.needed+' while beyond'):'level never broken';
    }else if(e.event==='entry_skipped'){rows[e.symbol].out='skipped: '+esc(e.reason||'');}
  }
  // mid-window the log has published nothing yet — overlay the running max
  const liveSyms=new Set();
  for(const[s,st]of Object.entries(liveWatch)){
    if(rows[s]&&rows[s].vol==null&&st.max_vol_ratio!=null){
      rows[s].vol=st.max_vol_ratio; rows[s].volBeyond=st.max_vol_ratio_beyond;
      liveSyms.add(s);
    }
  }
  const needed=volNeeded();
  // "beyond" is load-bearing in the label (issue #525): the number shown is the
  // peak anywhere, while green means the gate's while-beyond peak cleared it
  document.getElementById('selVolHdr').innerHTML='max vol&times;'+
    (needed!=null?' <span class="muted">(need '+needed+'&times; beyond)</span>':'');
  const tb=document.querySelector('#sel tbody'); tb.innerHTML='';
  for(const[s,r]of Object.entries(rows)){
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+esc(s)+'</td><td>'+esc(r.side)+'</td><td>'+(r.gap??'')+
      '</td><td>'+fmtVol(r.vol,r.volBeyond,needed,liveSyms.has(s))+'</td><td>'+r.out+'</td>';
    tb.appendChild(tr);
  }
  if(!Object.keys(rows).length)
    tb.innerHTML='<tr><td colspan="5" class="muted">no selection this day</td></tr>';
}
function renderTimeline(){
  const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  for(const e of curEvents){
    if(curFilter!=='all'&&kindOf(e)!==curFilter)continue;
    const{ts,event,...rest}=e;
    const tr=document.createElement('tr');
    tr.innerHTML='<td>'+esc(ts)+'</td><td class="ev-'+esc(event)+'">'+esc(event)+'</td><td>'+
      esc(JSON.stringify(rest).slice(1,-1).replaceAll('"',''))+'</td>';
    tb.appendChild(tr);
  }
}
document.querySelectorAll('.fbtn').forEach(b=>{b.onclick=()=>{
  curFilter=b.dataset.f;
  document.querySelectorAll('.fbtn').forEach(x=>x.classList.toggle('on',x===b));
  renderTimeline();};});
loadDays();
setInterval(()=>{
  const today=digests.length&&digests[0].source==='live'?digests[0].date:null;
  if(curDate&&curDate===today){loadDays();selectDay(curDate);}
},5000);
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
                    "instrument": r.instrument or "stock",
                    "opt_symbol": r.opt_symbol,
                    "opt_lot_size": r.opt_lot_size,
                    "opt_entry_premium": r.opt_entry_premium,
                    "opt_exit_premium": r.opt_exit_premium,
                    "opt_pnl": r.opt_pnl,
                    # contract liquidity at each decision moment (issue #488) —
                    # research fields; NULL means "not captured", not "zero"
                    "opt_entry_volume": r.opt_entry_volume,
                    "opt_entry_oi": r.opt_entry_oi,
                    "opt_exit_volume": r.opt_exit_volume,
                    "opt_exit_oi": r.opt_exit_oi,
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
