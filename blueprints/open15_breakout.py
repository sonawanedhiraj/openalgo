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

import datetime as dt
import threading

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

    # The option-liquidity defaults are read from the SERVICE, never re-derived
    # here — see the note in the env_defaults block below.
    from services.open15_breakout_service import (
        TRADE_SIDES,
        _coverage_target_default,
        _impact_gate_enabled_default,
        _impact_max_pct_default,
        _liq_backfill_rank_default,
        _liq_gate_enabled_default,
        _liq_max_staleness_default,
        _liq_min_days_default,
        _liq_min_pctile_default,
        _liq_reentry_days_default,
        _liq_reentry_pctile_default,
        _min_oi_lots_default,
        get_open15_service,
    )
    from services.open15_breakout_service import (
        clamp_min_oi_lots as _clamp_min_oi_lots,
    )
    from services.open15_breakout_service import (
        clamp_rolling_cadence as _clamp_rolling_cadence,
    )
    from services.open15_breakout_service import (
        clamp_rolling_top_n as _clamp_rolling_top_n,
    )
    from services.open15_breakout_service import (
        clamp_shadow_max_trades as _clamp_shadow_max_trades,
    )

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
        # rolling additive watch list (issue #529). Empty string / absent = NULL
        # = env default, matching every other field. The two numeric knobs are
        # CLAMPED server-side rather than rejected: the UI number inputs already
        # carry min/max, so an out-of-range value here means a hand-crafted POST
        # (or a stale page) and silently landing on the nearest legal value beats
        # both trusting it and failing the whole save.
        rolling_enabled = body.get("rolling_watchlist_enabled")
        if rolling_enabled is not None and rolling_enabled != "":
            if isinstance(rolling_enabled, str):
                rolling_enabled = rolling_enabled.strip().lower() in ("1", "true", "yes", "on")
            else:
                rolling_enabled = bool(rolling_enabled)
        else:
            rolling_enabled = None
        rolling_cadence_s = body.get("rolling_cadence_s")
        rolling_cadence_s = (
            None if rolling_cadence_s in (None, "") else _clamp_rolling_cadence(rolling_cadence_s)
        )
        rolling_top_n = body.get("rolling_top_n")
        rolling_top_n = None if rolling_top_n in (None, "") else _clamp_rolling_top_n(rolling_top_n)
        # shadow-log the excluded side (issue #581) — same NULL-is-env-default and
        # clamp-don't-reject treatment as the rolling knobs above
        shadow_excluded_side = body.get("shadow_excluded_side")
        if shadow_excluded_side is not None and shadow_excluded_side != "":
            if isinstance(shadow_excluded_side, str):
                shadow_excluded_side = shadow_excluded_side.strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                shadow_excluded_side = bool(shadow_excluded_side)
        else:
            shadow_excluded_side = None
        shadow_max_trades = body.get("shadow_max_trades")
        shadow_max_trades = (
            None if shadow_max_trades in (None, "") else _clamp_shadow_max_trades(shadow_max_trades)
        )
        # ---- option-liquidity gates (issue #583) ---------------------------
        # Every numeric is clamped SERVER-side: the UI number input is a hint, never
        # a trust boundary. Empty string means "clear the override", not zero.
        from services.open15_breakout_service import clamp_days as _clamp_days
        from services.open15_breakout_service import clamp_pctile as _clamp_pctile

        def _opt_bool(key):
            v = body.get(key)
            return None if v in (None, "") else bool(v)

        def _opt_pct(key, default):
            v = body.get(key)
            return None if v in (None, "") else _clamp_pctile(v, default)

        def _opt_days(key, default, lo, hi):
            v = body.get(key)
            return None if v in (None, "") else _clamp_days(v, default, lo, hi)

        liq_gate = _opt_bool("option_liquidity_gate_enabled")
        liq_min = _opt_pct("option_liquidity_min_pctile", 20.0)
        liq_reentry = _opt_pct("option_liquidity_reentry_pctile", 25.0)
        liq_reentry_days = _opt_days("option_liquidity_reentry_days", 3, 1, 30)
        liq_min_days = _opt_days("option_liquidity_min_days", 10, 1, 120)
        liq_stale = _opt_days("option_liquidity_max_staleness_days", 3, 0, 60)
        liq_backfill = _opt_bool("option_liquidity_backfill_rank")
        impact_gate = _opt_bool("option_impact_gate_enabled")
        impact_max = _opt_pct("option_impact_max_pct", 2.0)
        # broker OI floor (issue #595) — clamp-don't-reject; empty = env default
        min_oi_lots = body.get("option_min_oi_lots")
        min_oi_lots = None if min_oi_lots in (None, "") else _clamp_min_oi_lots(min_oi_lots)
        # ATM lot-cost coverage target (issue #591) — clamp-don't-reject, like
        # every other numeric knob on this form
        from services.open15_breakout_service import (
            clamp_coverage_target as _clamp_coverage_target,
        )

        coverage_target = body.get("coverage_target_pct")
        coverage_target = (
            None if coverage_target in (None, "") else _clamp_coverage_target(coverage_target)
        )
        if liq_min is not None and liq_reentry is not None and liq_reentry < liq_min:
            # an inverted band would readmit a name the same day it was excluded,
            # which is the flapping the hysteresis exists to prevent
            errors.append("re-entry percentile must be >= the exclusion percentile")
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
            rolling_watchlist_enabled=rolling_enabled,
            rolling_cadence_s=rolling_cadence_s,
            rolling_top_n=rolling_top_n,
            shadow_excluded_side=shadow_excluded_side,
            shadow_max_trades=shadow_max_trades,
            option_liquidity_gate_enabled=liq_gate,
            option_liquidity_min_pctile=liq_min,
            option_liquidity_reentry_pctile=liq_reentry,
            option_liquidity_reentry_days=liq_reentry_days,
            option_liquidity_min_days=liq_min_days,
            option_liquidity_max_staleness_days=liq_stale,
            option_liquidity_backfill_rank=liq_backfill,
            option_impact_gate_enabled=impact_gate,
            option_impact_max_pct=impact_max,
            option_min_oi_lots=min_oi_lots,
            coverage_target_pct=coverage_target,
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
                "rolling_watchlist_enabled": _os.getenv(
                    "OPEN15_ROLLING_WATCHLIST_ENABLED", "false"
                ).lower()
                == "true",
                "rolling_cadence_s": _clamp_rolling_cadence(
                    _os.getenv("OPEN15_ROLLING_CADENCE_S", "30")
                ),
                "rolling_top_n": _clamp_rolling_top_n(_os.getenv("OPEN15_ROLLING_TOP_N", "3")),
                "shadow_excluded_side": _os.getenv("OPEN15_SHADOW_EXCLUDED_SIDE", "false").lower()
                == "true",
                "shadow_max_trades": _clamp_shadow_max_trades(
                    _os.getenv("OPEN15_SHADOW_MAX_TRADES", "3")
                ),
                # Call the SERVICE getters rather than re-deriving from os.getenv.
                # Duplicating a default in two places is how the UI ends up showing
                # "on" while the engine resolves "off": this block said "true" while
                # _liq_gate_enabled_default() said false, and the page rendered a
                # ticked box for a gate that would never fire. One default, one place.
                "option_liquidity_gate_enabled": _liq_gate_enabled_default(),
                "option_liquidity_min_pctile": _liq_min_pctile_default(),
                "option_liquidity_reentry_pctile": _liq_reentry_pctile_default(),
                "option_liquidity_reentry_days": _liq_reentry_days_default(),
                "option_liquidity_min_days": _liq_min_days_default(),
                "option_liquidity_max_staleness_days": _liq_max_staleness_default(),
                "option_liquidity_backfill_rank": _liq_backfill_rank_default(),
                "option_impact_gate_enabled": _impact_gate_enabled_default(),
                "option_impact_max_pct": _impact_max_pct_default(),
                "option_min_oi_lots": _min_oi_lots_default(),
                "coverage_target_pct": _coverage_target_default(),
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
        return jsonify(
            {
                "date": today,
                "source": "live",
                "events": svc.day_log,
                # the journal rides along so the page never has to reconstruct
                # prices or P&L from the timeline (issue #557)
                "journal": journal_for_date(today),
            }
        )
    from database.open15_breakout_db import get_day_log

    day = date or today
    events = get_day_log(day)
    return jsonify(
        {
            "date": day,
            "source": "snapshot",
            "events": events or [],
            "journal": journal_for_date(day),
        }
    )


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
    from database.open15_breakout_db import (
        paper_pnl_by_date,
        real_fill_dates,
        replay_pnl_by_date,
        shadow_pnl_by_date,
        sim_pnl_by_date,
        trades_pnl_by_date,
    )
    from services.open15_log_view import summarize_day

    try:
        pnl_by_date = trades_pnl_by_date()
        paper_by_date = paper_pnl_by_date()
        sim_by_date = sim_pnl_by_date()
        shadow_by_date = shadow_pnl_by_date()
        replay_by_date = replay_pnl_by_date()
        # ONE query for the whole sidebar (#606). None = unknown -> every day
        # is treated as traded, so no replay is offered. Fail CLOSED.
        traded = real_fill_dates()
        from services.open15_replay import eligibility_or_reason

        out = []
        for date, events, source in _all_day_logs():
            digest = summarize_day(
                date,
                events,
                trades_pnl=pnl_by_date.get(date),
                paper_pnl=paper_by_date.get(date),
                sim_pnl=sim_by_date.get(date),
                shadow_pnl=shadow_by_date.get(date),
                replay_pnl=replay_by_date.get(date),
            )
            digest["source"] = source
            # Replay affordance, computed from data already in hand — the events
            # are right here and `traded` was one query. Costs no extra request
            # and no extra DB round trip per day (#606).
            elig = eligibility_or_reason(date, events, True if traded is None else (date in traded))
            digest["replay_eligible"] = elig["eligible"]
            digest["replay_reason"] = elig["reason"]
            digest["replay_detail"] = elig.get("detail") or ""
            digest["replay_warning"] = elig.get("warning")
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
            # journal per day (issue #557) — the export must carry the CORRECTED
            # prices and P&L, not whatever was believed when the log was sealed
            rows.extend(selection_outcomes(date, events, journal=journal_for_date(date)))
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
 .ev-watch_stats{color:#8aa0b4}.ev-watchlist_add{color:#f5c2e7}
 .ev-entry_rejected,.ev-rejection_unverified{color:#f38ba8;font-weight:bold}
 .ev-exit_paper{color:#f9e2af}.ev-exit_sim{color:#cba6f7}
 .ev-fill_reconcile,.ev-fill_reconcile_row{color:#94e2d5}
 .b-seed{background:#1b2b3a;color:#89b4fa}.b-roll{background:#3a2436;color:#f5c2e7}
 .ev-skipped_late_boot,.ev-skipped_no_prev_closes{color:#f38ba8;font-weight:bold}
 input{background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px 8px}
 .muted{color:#6b7886;font-size:12px}
 button{background:#1e2630;color:#d7dde4;border:1px solid #2a3138;padding:4px 10px;cursor:pointer}
 .layout{display:flex;gap:18px;align-items:flex-start;margin-top:12px}
 .side{width:232px;flex:none}
 .day{border:1px solid #2a3138;border-radius:6px;padding:6px 10px;margin-bottom:6px;cursor:pointer}
 .day:hover{background:#161d25}.day.sel{background:#1e2630;border-color:#7dc4e4}
 .day .d1{display:flex;justify-content:space-between;align-items:center;gap:4px}
 /* the date must never wrap, and the replay control must never shrink the
    badge+amount into a second line (issue #606) */
 .day .d1 > span:first-child{white-space:nowrap}
 .day .d1 .rbtn{flex:none;margin-left:auto}
 .pos{color:#a6e3a1}.neg{color:#f38ba8}
 .badge{font-size:10px;border-radius:8px;padding:1px 7px}
 .b-live{background:#12324a;color:#89b4fa}.b-skip{background:#463a20;color:#f9e2af}
 .b-paper{background:#463a20;color:#f9e2af}
 .b-sim{background:#2b2438;color:#cba6f7}.b-real{background:#16331f;color:#a6e3a1}
 /* issue #581 — its OWN colour: a shadow row is not a sim row, and the page
    must never let the two read as one bucket */
 .b-shadow{background:#153037;color:#94e2d5}
 .ev-entry_shadow,.ev-exit_shadow{color:#94e2d5}
 /* issue #583 — amber like the other "held, not traded" events, and distinct
    from the red rejection colours: an exclusion is a decision, not a failure */
 .ev-universe_excluded{color:#f9e2af}
 .ev-atm_lot_cost{color:#7dc4e4}
 .b-liq{background:#463a20;color:#f9e2af}
 .b-nocontract{background:#3a2028;color:#f38ba8}
 /* issue #600 — replay is a RECONSTRUCTION, not a trade. Its own colour,
    deliberately not green/amber/teal (real/paper/shadow), so a replayed
    number can never be mistaken for one of those buckets at a glance. */
 .b-replay{background:#2b2b3d;color:#b4b0e8}
 .ev-replay_meta,.ev-entry_replay,.ev-exit_replay{color:#b4b0e8}
 .rpbanner{background:#1c1a2b;border-left:3px solid #7f77dd;padding:9px 12px;margin-bottom:12px}
 .rpbanner .rt{color:#b4b0e8}.rpbanner .rm{color:#8aa0b4;margin-top:4px}
 .rpbanner .rn{color:#6b7886;margin-top:4px}
 .rbtn{border:1px solid #3a4650;color:#7dc4e4;border-radius:4px;padding:0 6px;cursor:pointer;
   font-size:12px;background:none;line-height:1.5}
 .rbtn:hover{background:#232c36}.rbtn[disabled]{color:#4a5560;border-color:#2a3138;cursor:default}
 /* replayable, but with a cost worth stating (market hours) — amber, still clickable */
 .rbtn.warn{color:#f9e2af;border-color:#463a20}
 .rprog{height:3px;background:#232c36;border-radius:2px;margin-top:5px}
 .rprog > div{height:3px;background:#7f77dd;border-radius:2px;width:0}
 .b-newlisting{background:#232c36;color:#8aa0b4}
 .b-ok{background:#16331f;color:#a6e3a1}.b-warn{background:#463a20;color:#f9e2af}
 .b-pend{background:#232c36;color:#8aa0b4}
 .leg{color:#8aa0b4;font-size:11px;display:block;margin-top:2px}
 .opt{color:#94e2d5}.slip{color:#f9e2af}
 /* merged symbol table (issue #559): one row per watched symbol, detail on click */
 tr.main{cursor:pointer}tr.main:hover{background:#161d25}tr.main.open{background:#161d25}
 tr.main:focus-visible{outline:1px solid #7dc4e4;outline-offset:-1px}
 .caret{color:#6b7886;-webkit-user-select:none;user-select:none}
 tr.detail>td{background:#12181f;border-bottom:2px solid #2a3138;padding:10px 14px}
 .dgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
 .dbox .h{color:#7dc4e4;font-size:10px;margin-bottom:5px;letter-spacing:.5px}
 .dbox table{margin:0;width:auto}
 .dbox td{border:0;padding:2px 0;font-size:11.5px}
 .dbox td:first-child{color:#6b7886;padding-right:14px;white-space:nowrap}
 .rejbanner{background:#2a1416;border-left:3px solid #f38ba8;padding:9px 12px;margin-bottom:12px}
 .rejbanner .rt{color:#f38ba8}.rejbanner .rm{color:#8aa0b4;margin-top:4px}
 .rejbanner .rn{color:#6b7886;margin-top:4px}
 .main{flex:1;min-width:0}
 .chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
 .chip{background:#161d25;border-radius:6px;padding:6px 12px}
 .chip .k{display:block;font-size:10px;color:#6b7886}.chip .v{font-size:14px}
 .chip .net{background:#232c36;border-radius:3px;padding:0 3px;color:#8aa0b4}
 .fbtn{font-size:11px;padding:2px 10px;border-radius:10px}.fbtn.on{background:#2a3138;color:#7dc4e4}
 .sec{color:#8aa0b4;font-size:12px;margin:14px 0 0}
 /* ATM lot-cost coverage ladder (issue #591) */
 .atmcard{border:1px solid #2a3138;border-radius:6px;padding:10px 14px;margin:4px 0 10px;background:#121820}
 .atmcard .atitle{color:#7dc4e4;font-size:12px;letter-spacing:.5px}
 .atmcard .asub{color:#6b7886;font-size:11px;margin-left:8px}
 .covtbl{width:auto}
 .covtbl td,.covtbl th{font-size:12px;padding:3px 12px;border-bottom:1px solid #1c232b}
 .covtbl tr.crow{cursor:pointer}
 .covtbl tr.crow:hover td{background:#161d25}
 .covtbl tr.cur td{background:#1b2b3a;color:#89b4fa}
 .covtbl tr.tgt td{background:#152030}
 .covtbl .hl{color:#89b4fa}
 .covtbl tr.rall .hl{color:#a6e3a1}
 .atmdet{margin-top:8px;background:#12181f;border-radius:6px;padding:8px 12px;font-size:12px}
 .atmdet .dh{color:#89b4fa;font-size:10px;letter-spacing:.5px;margin-bottom:5px}
 .atmfoot{margin-top:8px;color:#6b7886;font-size:11px}
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
 <div style="margin-top:8px;padding-top:8px;border-top:1px solid #2a3138">
  <span class="muted">shadow the excluded side (issue #581 — journal only, no orders, never compounds)</span>
  <label class="muted" style="margin-left:14px"><input id="c_shadow" type="checkbox"> log the excluded side's triggers</label>
  <label class="muted" style="margin-left:14px">shadow rows/day
   <input id="c_shadowmax" type="number" min="0" max="10" step="1" style="width:44px"></label>
  <span id="c_shadowhint" class="muted" style="margin-left:10px"></span>
 </div>
 <div style="margin-top:8px;padding-top:8px;border-top:1px solid #2a3138">
  <span class="muted">rolling watch-list (issue #529 — measurement, off by default)</span>
  <label class="muted" style="margin-left:14px"><input id="c_roll" type="checkbox"> enabled</label>
  <label class="muted" style="margin-left:14px">re-rank every
   <input id="c_rollcad" type="number" min="10" max="300" step="5" style="width:60px"> s</label>
  <label class="muted" style="margin-left:14px">top-N per side
   <input id="c_rolltn" type="number" min="1" max="10" step="1" style="width:44px"></label>
  <span class="muted" style="margin-left:10px">clamped 10&ndash;300 s / 1&ndash;10 server-side</span>
 </div>
 <div style="margin-top:6px">
  <label><input id="c_liqgate" type="checkbox"> exclude illiquid option books</label>
  <label class="muted" style="margin-left:14px">out below p
   <input id="c_liqmin" type="number" min="0" max="100" step="1" style="width:48px"></label>
  <label class="muted" style="margin-left:10px">back above p
   <input id="c_liqre" type="number" min="0" max="100" step="1" style="width:48px"></label>
  <label class="muted" style="margin-left:10px">for
   <input id="c_liqredays" type="number" min="1" max="30" step="1" style="width:40px"> sessions</label>
  <label class="muted" style="margin-left:14px"><input id="c_liqbackfill" type="checkbox"> backfill the freed slot</label>
  <span class="leg">percentile of ATM premium turnover within the day's universe, per SIDE,
   as a 20-day median. Below <code>min days</code> of history a symbol is NOT ranked and is
   watched anyway. Applies in option mode only.</span>
 </div>
 <div style="margin-top:6px">
  <label class="muted">min days
   <input id="c_liqmindays" type="number" min="1" max="120" step="1" style="width:44px"></label>
  <label class="muted" style="margin-left:10px">max staleness
   <input id="c_liqstale" type="number" min="0" max="60" step="1" style="width:44px"> sessions</label>
  <label style="margin-left:14px"><input id="c_impactgate" type="checkbox"> skip on impact cost</label>
  <label class="muted" style="margin-left:10px">above
   <input id="c_impactmax" type="number" min="0" max="100" step="0.5" style="width:52px"> %</label>
  <span class="leg">impact cost walks the 5 visible ask levels for the SIZED order,
   measured from the MID. A book that cannot fill the order at all is skipped whatever the
   percentage says. Stale scores FAIL OPEN &mdash; nothing is excluded.</span>
 </div>
 <div style="margin-top:6px">
  <label class="muted">broker OI floor
   <input id="c_minoi" type="number" min="0" max="5000" step="50" style="width:56px"> lots</label>
  <span class="leg">Zerodha blocks MIS orders on stock option contracts whose OI is under
   <b>500 lots</b> &mdash; a per-CONTRACT, absolute rule (2026-08-13: 4 of 5 entries rejected;
   KALYANKJIL at p96 was among them). Candidates whose live ATM-contract OI &divide; lot size is
   below this floor are skipped at seed/rolling selection and the slot promotes the next name.
   0 switches the check off; unknown OI FAILS OPEN. Applies in option mode only.</span>
 </div>
 <div style="margin-top:6px">
  <label class="muted">ATM lot-cost coverage target
   <input id="c_covtgt" type="number" min="50" max="100" step="1" style="width:48px"> %</label>
  <span class="leg">the &quot;cover MOST&quot; row of each day's coverage-ladder card &mdash; the minimum
   capital/slot at which this share of priced names is affordable (1 ATM lot, worst of CE/PE).
   Observational: nothing gates on it. Clamped 50&ndash;100 server-side.</span>
 </div>
 <button onclick="saveCfg()" style="margin-left:14px;background:#1e2630;color:#a6e3a1;border:1px solid #2a3138;padding:4px 12px;cursor:pointer">save</button>
 <span id="c_msg" class="muted" style="margin-left:10px"></span>
 <div id="c_rollsrc" class="muted" style="margin-top:6px"></div>
 <div id="c_eff" class="muted" style="margin-top:6px"></div>
</fieldset>
<div class="layout">
 <div class="side">
  <div class="muted" style="margin-bottom:6px">history</div>
  <div id="days"></div>
 </div>
 <div class="main">
  <div id="status" class="muted"></div>
  <div id="rejbox"></div>
  <div id="lostbox"></div>
<div id="rpbox"></div>
<div class="chips" id="chips"></div>
  <div id="atmcard" class="atmcard" style="display:none"></div>
  <div class="sec">selection outcomes
   <span class="muted">&mdash; click a row for fills, liquidity and the decision detail</span></div>
  <div id="rollCfg" class="muted"></div>
  <table id="sel"><thead><tr><th style="width:16px"></th><th>symbol</th><th>side</th>
   <th>source</th><th>gap %</th>
   <th id="selVolHdr">max vol&times;</th><th>entry</th><th>exit</th><th>qty</th>
   <th>net P&amp;L</th><th>outcome</th></tr></thead><tbody></tbody></table>
  <div id="liqNote" class="muted" style="margin-top:6px"></div>
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
// Shadowing only means something when a side is EXCLUDED, so with trade side
// `both` the controls are disabled and say why. The server derives the same
// answer independently (shadow_side_for) — this is the explanation, not the
// enforcement.
function syncShadowUi(){
  const both=document.getElementById('c_side').value==='both';
  const cb=document.getElementById('c_shadow'), mx=document.getElementById('c_shadowmax');
  cb.disabled=both; mx.disabled=both||!cb.checked;
  document.getElementById('c_shadowhint').textContent=both
    ?'trade side is “both” — no excluded side to shadow'
    :(cb.checked
      ?('watches the '+(document.getElementById('c_side').value==='long_only'?'SHORT':'LONG')+
        ' side and journals it — no orders, excluded from real P&L and from compound sizing')
      :'off — the excluded side is not watched at all');
}
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
  // rolling watch list (issue #529): `??` not `||` — a stored `false` / `0`
  // must beat the env default, and 0 is never a legal cadence anyway
  document.getElementById('c_roll').checked=
    !!(o.rolling_watchlist_enabled??d.rolling_watchlist_enabled);
  document.getElementById('c_rollcad').value=o.rolling_cadence_s??d.rolling_cadence_s??30;
  document.getElementById('c_rolltn').value=o.rolling_top_n??d.rolling_top_n??3;
  // shadow the excluded side (issue #581) — same `??` treatment: a stored
  // `false` must beat a `true` env default
  document.getElementById('c_shadow').checked=
    !!(o.shadow_excluded_side??d.shadow_excluded_side);
  document.getElementById('c_shadowmax').value=o.shadow_max_trades??d.shadow_max_trades??3;
  // option-liquidity gates (issue #583) — `??` again, so a stored false/0 wins
  document.getElementById('c_liqgate').checked=
    !!(o.option_liquidity_gate_enabled??d.option_liquidity_gate_enabled);
  document.getElementById('c_liqmin').value=
    o.option_liquidity_min_pctile??d.option_liquidity_min_pctile??20;
  document.getElementById('c_liqre').value=
    o.option_liquidity_reentry_pctile??d.option_liquidity_reentry_pctile??25;
  document.getElementById('c_liqredays').value=
    o.option_liquidity_reentry_days??d.option_liquidity_reentry_days??3;
  document.getElementById('c_liqmindays').value=
    o.option_liquidity_min_days??d.option_liquidity_min_days??10;
  document.getElementById('c_liqstale').value=
    o.option_liquidity_max_staleness_days??d.option_liquidity_max_staleness_days??3;
  document.getElementById('c_liqbackfill').checked=
    !!(o.option_liquidity_backfill_rank??d.option_liquidity_backfill_rank);
  document.getElementById('c_impactgate').checked=
    !!(o.option_impact_gate_enabled??d.option_impact_gate_enabled);
  document.getElementById('c_impactmax').value=
    o.option_impact_max_pct??d.option_impact_max_pct??2.0;
  document.getElementById('c_minoi').value=
    o.option_min_oi_lots??d.option_min_oi_lots??500;
  document.getElementById('c_covtgt').value=
    o.coverage_target_pct??d.coverage_target_pct??90;
  syncShadowUi();
  // which source each rolling field resolved from, so "saved" is visible
  const src=k=>(o[k]==null?'env default':'db');
  document.getElementById('c_rollsrc').textContent=
    'rolling config source: enabled='+src('rolling_watchlist_enabled')+
    ', cadence='+src('rolling_cadence_s')+', top-N='+src('rolling_top_n');
  const e=j.effective_today;
  if(e){
    const nea=e.no_entry_after||'09:29', ext=e.exit_time||'09:30';
    const sd=e.trade_side||'both';
    const sdLbl={both:'both',long_only:'longs only',short_only:'shorts only'}[sd]||sd;
    // the shadowed side is watched but NEVER traded — say so on the same line
    // that states what the day traded, so the two can't be read apart
    const shLbl=e.shadow_side
      ?(' + shadow '+(e.shadow_side==='S'?'shorts':'longs')+
        ' (max '+(e.shadow_max_trades??3)+', no orders)')
      :'';
    document.getElementById('c_eff').textContent=
      'effective today: '+(e.instrument||'stock')+' | trade side '+sdLbl+shLbl+
      (sd!=='both'?' ⚠ one-sided (parity targets measured both sides)':'')+
      ' | max trades '+(e.max_trades||3)+
      ' | '+e.sizing_mode+' | margin '+e.margin_effective+' (base '+e.margin_per_slot+
      (e.sizing_mode==='compound'?(' + cum P&L '+e.cum_realized_pnl):'')+
      ') x '+e.leverage+' = notional '+e.notional+' | vol_mult '+e.vol_mult+
      ' | entries 09:16–'+nea+' | exit '+ext+
      ((nea!=='09:29'||ext!=='09:30')?' ⚠ non-default window (R58 measured 09:29/09:30)':'')+
      ' | rolling watch-list '+(e.rolling_watchlist_enabled
        ?('every '+e.rolling_cadence_s+'s, top '+e.rolling_top_n+'/side'):'disabled');
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
    exit_time:document.getElementById('c_exit').value,
    rolling_watchlist_enabled:document.getElementById('c_roll').checked,
    rolling_cadence_s:+document.getElementById('c_rollcad').value,
    rolling_top_n:+document.getElementById('c_rolltn').value,
    shadow_excluded_side:document.getElementById('c_shadow').checked,
    shadow_max_trades:+document.getElementById('c_shadowmax').value,
    option_liquidity_gate_enabled:document.getElementById('c_liqgate').checked,
    option_liquidity_min_pctile:+document.getElementById('c_liqmin').value,
    option_liquidity_reentry_pctile:+document.getElementById('c_liqre').value,
    option_liquidity_reentry_days:+document.getElementById('c_liqredays').value,
    option_liquidity_min_days:+document.getElementById('c_liqmindays').value,
    option_liquidity_max_staleness_days:+document.getElementById('c_liqstale').value,
    option_liquidity_backfill_rank:document.getElementById('c_liqbackfill').checked,
    option_impact_gate_enabled:document.getElementById('c_impactgate').checked,
    option_impact_max_pct:+document.getElementById('c_impactmax').value,
    option_min_oi_lots:+document.getElementById('c_minoi').value,
    coverage_target_pct:+document.getElementById('c_covtgt').value};
  const msg=document.getElementById('c_msg');
  try{
    const tok=await csrfToken();
    const r=await fetch('/open15_vol_breakout/api/config',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':tok},body:JSON.stringify(body)});
    const j=await r.json();
    msg.textContent=(j.status==='success')?'saved ✓'
      :('error: '+((j.errors&&j.errors.length)?j.errors.join('; '):(j.message||j.error||('HTTP '+r.status))));
  }catch(e){msg.textContent='error: '+e;}
  loadCfg();
}
document.getElementById('c_side').addEventListener('change',syncShadowUi);
document.getElementById('c_shadow').addEventListener('change',syncShadowUi);
loadCfg();
</script>
<script>
let replayPolling={};
let curDate=null, curEvents=[], curJournal=[], curFilter='all', digests=[];
// symbols whose detail row is open, so the 5s live refresh does not collapse
// them under the operator (issue #559). Cleared when a different day is picked.
let expanded=new Set();
// live max-vol overlay for today (issue #524): the tick-by-tick running max is
// only published to the decision log at the exit job, so mid-window it comes
// from /api/status instead. Cleared whenever a past day is selected.
let liveWatch={}, liveNeeded=null;
// which coverage-ladder row's drill-down is open (issue #591) — kept across
// the 5s live refresh so it does not collapse under the operator, cleared
// when a different day is picked (same contract as `expanded` above)
let atmOpenPct=null;
const esc=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function kindOf(e){
  if(e.event==='entry'||e.event==='exit'||e.event==='entry_skipped'||
     e.event==='entry_rejected'||e.event==='exit_paper'||e.event==='exit_sim'||
     e.event==='entry_shadow'||e.event==='exit_shadow'||
     e.event==='fill_reconcile_row'||e.event==='liquidity_row'||
     e.event==='rejection_unverified')return 'trade';
  if(e.event==='no_entry')return 'no_entry';
  // issue #583 — an exclusion is a decision ABOUT a symbol, so it belongs with
  // the other "watched but not traded" rows rather than buried in sys noise
  if(e.event==='universe_excluded')return 'no_entry';
  return 'sys';
}
async function csrfToken(){
  // CSRFProtect is global (issue #446): ANY POST from this page is rejected 400
  // without this header. Factored into one helper because the replay POST
  // (#613) shipped without it and the button silently did nothing — a second
  // hand-rolled POST is exactly how that recurs.
  try{return (await (await fetch('/auth/csrf-token')).json()).csrf_token||'';}
  catch(e){return '';}
}
async function loadDays(){
  const r=await fetch('/open15_vol_breakout/api/decision_log/days'); const j=await r.json();
  digests=j.days||[];
  const box=document.getElementById('days'); box.innerHTML='';
  for(const d of digests){
    const el=document.createElement('div');
    el.className='day'+(d.date===curDate?' sel':'');
    const skip=d.status&&d.status.startsWith('skipped');
    const amt=(v)=>'<span class="'+(v>=0?'pos':'neg')+'">'+(v>=0?'+':'')+
      '&#8377;'+Math.round(v)+'</span>';
    // a paper day shows its simulated P&L ONLY behind the badge (issue #548) —
    // an unbadged number in this column reads as money that was actually made.
    // Keyed off paper_pnl too, not just the event count: a day repaired by the
    // one-off backfill has paper P&L in the DB, and showing that number bare
    // would be exactly the failure this badge exists to prevent.
    const isPaper=(d.paper>0)||(d.paper_pnl!=null);
    const isSim=(d.sim>0)||(d.sim_pnl!=null);
    const isShadow=(d.shadow>0)||(d.shadow_pnl!=null);
    const paperTag=isPaper?'<span class="badge b-paper">paper</span> ':'';
    const simTag=isSim?'<span class="badge b-sim">sim</span> ':'';
    const shadowTag=isShadow?'<span class="badge b-shadow">shadow</span> ':'';
    // real P&L wins the headline. A day with no real fills falls back to paper,
    // then to sim, then to shadow — each behind its own badge, because an
    // unbadged number in this column reads as money that was actually made.
    const isReplay=(d.replay>0)||(d.replay_pnl!=null);
    const replayTag='<span class="badge b-replay">replay</span> ';
    // replay OUTRANKS the skipped badge: a reconstructed day is no longer
    // just "skipped", and the badge is what stops its number reading as money.
    const right=d.source==='live'&&!isReplay?'<span class="badge b-live">live</span>'
      :isReplay?(replayTag+(d.replay_pnl!=null?amt(d.replay_pnl):''))
      :skip?'<span class="badge b-skip">skipped</span>'
      :(d.pnl!=null?amt(d.pnl)
        :(d.paper_pnl!=null?(paperTag+amt(d.paper_pnl))
          :(d.sim_pnl!=null?(simTag+amt(d.sim_pnl))
            :(d.shadow_pnl!=null?(shadowTag+amt(d.shadow_pnl))
              :(isPaper?paperTag:(isSim?simTag:(isShadow?shadowTag
                :'<span class="muted">&mdash;</span>')))))));
    el.innerHTML='<div class="d1"><span>'+esc(d.date)+'</span>'+right+'</div>'+
      '<div class="muted">'+(skip&&!d.replay?esc(d.status):(d.selected+' sel &middot; '+d.entered+
        ' filled'+(d.paper?(' &middot; '+d.paper+' paper'):'')+
        (d.sim?(' &middot; '+d.sim+' sim'):'')+
        (d.shadow?(' &middot; '+d.shadow+' shadow'):'')+
        (d.replay?(' &middot; '+d.replay+' replayed'):'')))+'</div>'+
      '<div class="rprog" id="rp-'+d.date+'" style="display:none"><div></div></div>'+
      '<div class="muted" id="rpmsg-'+d.date+'" style="display:none"></div>';
    el.onclick=()=>selectDay(d.date);
    // the replay affordance is added asynchronously: eligibility is a server
    // decision (traded day? market hours? already replayed?) and the button
    // must never appear on a day the server would refuse.
    maybeAddReplayBtn(el,d);
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
  if(date!==curDate){expanded.clear();atmOpenPct=null;} // a different day's rows are different rows
  curDate=date;
  document.getElementById('dayjson').href='/open15_vol_breakout/api/decision_log?date='+date;
  const r=await fetch('/open15_vol_breakout/api/decision_log?date='+date); const j=await r.json();
  curEvents=j.events||[];
  curJournal=j.journal||[];   // authoritative prices/P&L (issue #557)
  if(j.source==='live'){await loadLiveWatch();}else{liveWatch={}; liveNeeded=null;}
  document.getElementById('status').textContent=j.date+' ('+j.source+') — '+curEvents.length+' events';
  renderRejected(); renderLogLostBanner(); renderReplayBanner(); renderChips(); renderAtmLadder();
  renderRolling(); renderSel(); renderTimeline();
  document.querySelectorAll('#days .day').forEach(el=>
    el.classList.toggle('sel',el.querySelector('span').textContent===date));
}
function maybeAddReplayBtn(el,d){
  // SYNCHRONOUS on purpose (issue #606). This used to fetch eligibility per
  // card, and the sidebar re-renders every 5s — 140 requests in 18s, each one
  // a DB query plus a full day-log parse, hammering the live DB while the
  // strategy traded. Eligibility now rides the days digest the sidebar already
  // fetches, so the affordance costs zero extra requests.
  const head=el.querySelector('.d1');
  if(!head||d.replay_eligible===undefined)return;
  const running=(replayPolling[d.date]===true);
  const b=document.createElement('button');
  b.className='rbtn'; b.innerHTML='&#8635;';
  if(running){b.disabled=true; b.title='replay running…';}
  else if(!d.replay_eligible){
    // Disabled WITH the reason rather than hidden: "why is there no button?" is
    // the question an operator would otherwise have to read code to answer.
    // day_was_traded is the one that matters — clicking through THAT would
    // overwrite a real trading day, so it stays a hard block.
    b.disabled=true;
    b.title='cannot replay — '+(d.replay_reason||'ineligible')+
      (d.replay_detail?(': '+d.replay_detail):'');
  }else{
    b.title=(d.replay_reason==='re_replay')
      ? 'replay again from 1m bars' : 'reconstruct this session from 1m bars';
    // A cost worth stating is not a reason to take the choice away (operator
    // decision 2026-08-17): the button stays live and the tooltip carries the
    // warning. Only correctness blocks disable it.
    if(d.replay_warning){b.classList.add('warn'); b.title='⚠ '+d.replay_warning+' — '+b.title;}
    b.onclick=(ev)=>{ev.stopPropagation(); startReplay(d.date,b);};
  }
  head.appendChild(b);
}
async function startReplay(date,btn){
  btn.disabled=true; btn.title='starting…';
  let j={};
  try{
    const r=await fetch('/open15_vol_breakout/api/replay',{method:'POST',
      headers:{'Content-Type':'application/json','X-CSRFToken':await csrfToken()},
      body:JSON.stringify({date:date,force:true})});
    j=await r.json();
    if(!r.ok){replayMsg(date,'refused — '+(j.reason||j.message||r.status));return;}
  }catch(err){replayMsg(date,'request failed');return;}
  replayMsg(date,'starting…'); pollReplay(date);
}
function replayMsg(date,text){
  const m=document.getElementById('rpmsg-'+date);
  if(m){m.style.display=''; m.textContent=text;}
}
function pollReplay(date){
  if(replayPolling[date])return;   // one poller per date
  replayPolling[date]=true;
  const bar=document.getElementById('rp-'+date);
  if(bar)bar.style.display='';
  const tick=async()=>{
    let s={};
    try{
      const r=await fetch('/open15_vol_breakout/api/replay/status?date='+date);
      s=await r.json();
    }catch(err){setTimeout(tick,4000);return;}
    if(s.status==='running'){
      const pct=s.total?Math.round(100*(s.progress||0)/s.total):0;
      if(bar)bar.firstChild.style.width=pct+'%';
      replayMsg(date,'replaying '+(s.progress||0)+(s.total?('/'+s.total):'')+'…');
      setTimeout(tick,3000); return;
    }
    if(bar)bar.style.display='none';
    replayPolling[date]=false;
    if(s.status==='done'){
      replayMsg(date,'replayed — '+(s.rows_written||0)+' rows');
      await loadDays(); selectDay(date);
    }else if(s.status==='failed'){
      replayMsg(date,'failed — '+(s.error||'unknown'));
    }else{replayMsg(date,'');}
  };
  setTimeout(tick,1200);
}
function renderLogLostBanner(){
  // issue #612 — say why the timeline is missing, above the numbers, in the
  // same place the rejection banner says its rows are simulated. Without this
  // the page asserted "no selection this day" beside a real +Rs1438 and left
  // the reader to reconcile it.
  const box=document.getElementById('lostbox');
  if(!box)return;
  const lost=(curJournal||[]).length>0&&!curEvents.some(e=>e.event==='selection');
  if(!lost){box.innerHTML='';return;}
  const real=(curJournal||[]).filter(j=>!j.fill||j.fill==='real').length;
  box.innerHTML='<div class="rejbanner" style="border-left-color:#f9e2af;background:#241f14">'+
    '<div class="rt" style="color:#f9e2af">Decision timeline lost &mdash; the trades are intact'+
    '</div><div class="rm">A late-boot arm overwrote the log for this day with a single '+
    '<code>skipped_late_boot</code> event (the pre-#597 clobber; restarts now append '+
    '<code>late_boot_restart</code> instead). The '+(curJournal||[]).length+
    ' trade'+((curJournal||[]).length===1?'':'s')+' below '+
    ((curJournal||[]).length===1?'is':'are')+' read from the journal, which was never '+
    'touched'+(real?(' &mdash; '+real+' real fill'+(real===1?'':'s')):'')+'.</div>'+
    '<div class="rn">Gone for good: the gap ranking, per-symbol volume ratios, the rolling '+
    'additions and the universe exclusions. Not reconstructed &mdash; a partial rebuild would '+
    'look like a record.</div></div>';
}
function renderReplayBanner(){
  // A replayed day must SAY it is a reconstruction, above the numbers, in the
  // same place the broker-rejection banner says its rows are simulated.
  const meta=curEvents.find(e=>e.event==='replay_meta');
  const box=document.getElementById('rpbox');
  if(!box)return;
  if(!meta){box.innerHTML='';return;}
  const summ=curEvents.find(e=>e.event==='summary')||{};
  const degen=curEvents.filter(e=>e.event==='exit_replay'&&e.reason==='degenerate_hold').length;
  box.innerHTML='<div class="rpbanner"><div class="rt">Reconstruction &mdash; this session was '+
    'never traded</div>'+
    '<div class="rm">'+esc(meta.eligible_reason||'missed')+'; rebuilt from broker 1m candles at '+
    esc(meta.ran_at||'')+'. Selection, gates and OI verdicts are exact. Entry <b>price</b> is '+
    'not &mdash; the gate fires mid-minute and bars only close it, so P&amp;L is a band.</div>'+
    '<div class="rn">config from '+esc(meta.config_source||'?')+' &middot; '+
    esc(String(meta.symbols_fetched||'?'))+'/'+esc(String(meta.symbols_requested||'?'))+
    ' symbols &middot; '+esc(String(meta.contracts_resolved||0))+' contracts'+
    (degen?(' &middot; '+degen+' degenerate '+(degen===1?'hold':'holds')+
      ' (triggered in the last entry minute, ~0s hold)'):'')+
    ' &middot; not real money: excluded from realised P&amp;L and from compound sizing</div></div>';
  if(summ.net_close_entry!=null){
    const rupee=v=>(v>=0?'+':'')+'\\u20B9'+Math.round(v);
    const cls=v=>v>=0?'pos':'neg';
    box.insertAdjacentHTML('beforeend','<div class="chips">'+
      '<div class="chip"><span class="k">close-entry (conservative)</span>'+
      '<span class="v '+cls(summ.net_close_entry)+'">'+rupee(summ.net_close_entry)+'</span></div>'+
      (summ.net_early_entry!=null?('<div class="chip"><span class="k">early-entry (optimistic)'+
        '</span><span class="v '+cls(summ.net_early_entry)+'">'+rupee(summ.net_early_entry)+
        '</span></div>'):'')+
      '</div>');
  }
}
function renderRejected(){
  // broker-rejected entries (issue #548): no position was taken, and every
  // number on those rows is a sandbox-equivalent simulation. Say so once,
  // loudly, above the numbers — not in a tooltip.
  const rej=curEvents.filter(e=>e.event==='entry_rejected');
  const box=document.getElementById('rejbox');
  if(!rej.length){box.innerHTML='';return;}
  const msgs=[...new Set(rej.map(e=>e.error).filter(Boolean))];
  const capped=rej.filter(e=>e.paper_capped).length;
  box.innerHTML='<div class="rejbanner"><div class="rt">'+rej.length+
    ' '+(rej.length===1?'entry':'entries')+' rejected by broker — no live position was taken</div>'+
    msgs.map(m=>'<div class="rm">'+esc(m)+'</div>').join('')+
    '<div class="rn">Values below are simulated as if the day had run in sandbox. No money moved.'+
    (capped?(' '+capped+' beyond the paper cap '+(capped===1?'was':'were')+' left unpriced.'):'')+
    '</div></div>';
}
function renderChips(){
  const armed=curEvents.find(e=>e.event==='armed')||{};
  const summ=curEvents.find(e=>e.event==='summary')||{};
  const dig=digests.find(d=>d.date===curDate)||{};
  // When the log was destroyed but the journal survived (#612), the EVENT-derived
  // counts are false — 2026-08-13 read "0 filled" beside a real +Rs1438. Count
  // the journal instead, which is what the P&L chips already read.
  const jFills=(curJournal||[]).filter(j=>!j.fill||j.fill==='real').length;
  const logLost=(curJournal||[]).length>0&&!curEvents.some(e=>e.event==='selection');
  const filled=logLost?jFills:(dig.entered??summ.filled??0);
  const paper=dig.paper??summ.paper??0;
  const rupee=v=>(v>=0?'+':'')+'\\u20B9'+Math.round(v);
  // real and paper P&L are NEVER summed into one figure — paper money was
  // never made, and blending them is how a rejected day reads as a traded one.
  // Both are NET of modelled charges (issue #552) and say so: these chips used
  // to show gross while the rows below showed net, which on 2026-08-05 read
  // +2109 above rows totalling +1384, and on 2026-07-23 flipped the sign.
  // sim = triggers no order was ever sent for, priced at 1 lot (issue #555).
  // A THIRD bucket, never folded into paper: "the broker refused" and "we could
  // not afford it" are different facts, and one blended number states neither.
  // shadow = the side `trade_side` switched off, priced at full slot size
  // (issue #581). A FOURTH bucket for the same reason sim is a third one: "we
  // could not afford it" and "we do not trade that side" are different facts.
  const sim=dig.sim??summ.sim??0;
  const shadow=dig.shadow??summ.shadow??0;
  const chips=[['status',summ.day||dig.status||'—'],['mode',armed.mode||'—'],
    ['instrument',armed.instrument||(summ.instrument||'—')],
    ['universe',armed.universe??'—'],['vol&times;',armed.vol_mult??'—'],
    // a literal '\\u00B7', NOT '&middot;': chip VALUES go through esc() (keys are
    // inserted raw), so an HTML entity here renders as the text "&middot;"
    ['entries',filled+' filled'+(paper?(' \\u00B7 '+paper+' paper'):'')+
      (sim?(' \\u00B7 '+sim+' sim'):'')+
      (shadow?(' \\u00B7 '+shadow+' shadow'):'')+
      ' / '+(logLost?(curJournal||[]).length:(dig.selected??summ.selected??0))+' sel'],
    ['real P&amp;L <span class="net">net</span>',dig.pnl==null?'—':rupee(dig.pnl)]];
  if(paper||dig.paper_pnl!=null)
    chips.push(['paper P&amp;L <span class="net">net</span>',
      dig.paper_pnl==null?'—':rupee(dig.paper_pnl)]);
  if(sim||dig.sim_pnl!=null)
    chips.push(['sim P&amp;L <span class="net">net</span>',
      dig.sim_pnl==null?'—':rupee(dig.sim_pnl)]);
  if(shadow||dig.shadow_pnl!=null)
    chips.push(['shadow P&amp;L <span class="net">net</span>',
      dig.shadow_pnl==null?'—':rupee(dig.shadow_pnl)]);
  document.getElementById('chips').innerHTML=chips.map(([k,v])=>{
    const isPaper=k.startsWith('paper P&amp;L'), isReal=k.startsWith('real P&amp;L');
    const isSim=k.startsWith('sim P&amp;L'), isShadow=k.startsWith('shadow P&amp;L');
    const val=isPaper?dig.paper_pnl
      :(isSim?dig.sim_pnl:(isShadow?dig.shadow_pnl:(isReal?dig.pnl:null)));
    const border=isPaper?' style="border:1px solid #463a20"'
      :(isSim?' style="border:1px solid #2b2438"'
        :(isShadow?' style="border:1px solid #153037"':''));
    return '<div class="chip"'+border+
      '><span class="k">'+k+'</span><span class="v'+
      (val!=null?(val>=0?' pos':' neg'):'')+'">'+esc(v)+'</span></div>';
  }).join('');
}
function srcBadge(src){
  return '<span class="badge '+(src==='rolling'?'b-roll':'b-seed')+'">'+esc(src||'seed')+'</span>';
}
// ---- ATM lot-cost coverage ladder (issue #591) ------------------------------
// One `atm_lot_cost` event per day (09:10 arm), priced from the previous EOD
// option-liquidity sweep. Days without the event (all history before #591)
// render no card at all.
const rupeeIN=v=>'\\u20B9'+Math.round(v).toLocaleString('en-IN');
function atmWatchedSet(){
  const w=new Set();
  for(const e of curEvents){
    if(e.event==='selection')Object.keys(e.selected||{}).forEach(s=>w.add(s));
    if(e.event==='watchlist_add'&&e.symbol)w.add(e.symbol);
  }
  return w;
}
function renderAtmLadder(){
  const ev=curEvents.find(e=>e.event==='atm_lot_cost');
  const box=document.getElementById('atmcard');
  if(!ev||!(ev.ladder||[]).length){box.innerHTML='';box.style.display='none';return;}
  box.style.display='';
  let rows='';
  for(const r of ev.ladder){
    const cur=r.marker==='current_slot', tgt=r.marker==='target', all=r.pct===100&&!cur;
    const note=cur?('\\u25C0 your capital/slot')
      :tgt?'\\u25C0 coverage target (configurable above)'
      :(r.costliest?('costliest: '+esc(r.costliest)
        +(ev.drop_top&&ev.drop_top['5']?(' \\u00B7 drop top 5 \\u2192 '+rupeeIN(ev.drop_top['5'])):'')
        +(ev.drop_top&&ev.drop_top['10']?(' \\u00B7 top 10 \\u2192 '+rupeeIN(ev.drop_top['10'])):'')):'');
    rows+='<tr class="crow'+(cur?' cur':'')+(tgt?' tgt':'')+(all?' rall':'')
      +'" data-cap="'+r.capital+'" data-pct="'+r.pct+'">'
      +'<td class="'+((tgt||all)?'hl':'')+'">'+r.pct+'%</td>'
      +'<td>'+r.names+' / '+ev.priced+'</td>'
      +'<td class="'+((tgt||all)?'hl':'')+'">'+rupeeIN(r.capital)+'</td>'
      +'<td class="muted">'+note+'</td></tr>';
  }
  const u=ev.unresolved||{n:0};
  const parts=[];
  if((u.not_scored||[]).length)parts.push('no contract/score: '+u.not_scored.map(esc).join(', '));
  if((u.no_quote||[]).length)parts.push('no ATM quote: '+u.no_quote.map(esc).join(', '));
  const unres=u.n?('<span style="color:#f9e2af">'+u.n+' unresolved</span> ('
    +parts.join(' \\u00B7 ')+') \\u2014 excluded from all counts \\u00B7 '):'';
  box.innerHTML='<span class="atitle">ATM LOT COST \\u2014 COVERAGE LADDER</span>'
    +'<span class="asub">capital/slot \\u2192 names affordable (1 lot, worst of CE/PE)'
    +' \\u00B7 priced from the '+esc(ev.as_of||'?')+' option sweep'
    +' \\u00B7 front expiry '+esc(ev.expiry||'?')+(ev.dte!=null?(' ('+ev.dte+' DTE)'):'')
    +' \\u00B7 '+ev.priced+'/'+ev.universe_n+' priced</span>'
    +'<table class="covtbl"><thead><tr><th>coverage</th><th>names</th>'
    +'<th>capital/slot needed</th><th></th></tr></thead><tbody>'+rows+'</tbody></table>'
    +'<div id="atmdet"></div>'
    +'<div class="atmfoot">'+unres
    +'click a row for the names excluded at that capital'
    +' \\u00B7 lot costs are the sweep day\\u2019s close premiums \\u2014 overnight gaps move them'
    +' \\u00B7 costs fall through the expiry cycle and jump at rollover: size to a cycle-start day</div>';
  box.querySelectorAll('tr.crow').forEach(tr=>tr.onclick=()=>{
    const pct=+tr.dataset.pct;
    atmOpenPct=(atmOpenPct===pct)?null:pct;
    renderAtmDetail(ev,+tr.dataset.cap,pct);
  });
  if(atmOpenPct!=null){
    const tr=[...box.querySelectorAll('tr.crow')].find(t=>+t.dataset.pct===atmOpenPct);
    if(tr)renderAtmDetail(ev,+tr.dataset.cap,atmOpenPct);else atmOpenPct=null;
  }
}
function renderAtmDetail(ev,cap,pct){
  const det=document.getElementById('atmdet');
  if(!det)return;
  if(atmOpenPct==null||atmOpenPct!==pct){det.innerHTML='';return;}
  const watched=atmWatchedSet();
  // excluded = costlier than this row's capital, costliest first — the names
  // this capital level gives up, which is the actionable list
  const excl=(ev.costs||[]).filter(c=>c.w>cap).reverse();
  if(!excl.length){
    det.innerHTML='<div class="atmdet"><div class="dh">nothing excluded at '+rupeeIN(cap)
      +' \\u2014 every priced name is affordable</div></div>';
    return;
  }
  const MAX=40;
  let rows='';
  for(const c of excl.slice(0,MAX)){
    rows+='<tr><td'+(watched.has(c.s)?' style="color:#94e2d5"':'')+'>'+esc(c.s)+'</td>'
      +'<td>'+(c.k!=null?c.k:'\\u2014')+'</td><td>'+(c.lot!=null?c.lot:'\\u2014')+'</td>'
      +'<td>'+(c.ce!=null?rupeeIN(c.ce):'\\u2014')+'</td>'
      +'<td>'+(c.pe!=null?rupeeIN(c.pe):'\\u2014')+'</td>'
      +'<td>'+rupeeIN(c.w)+'</td></tr>';
  }
  det.innerHTML='<div class="atmdet"><div class="dh">EXCLUDED AT '+pct+'% / '+rupeeIN(cap)
    +' \\u2014 '+excl.length+' name'+(excl.length===1?'':'s')+', costliest first'
    +' <span style="color:#6b7886">(teal = on today\\u2019s watch list)</span></div>'
    +'<table class="covtbl"><thead><tr><th>symbol</th><th>ATM strike</th><th>lot</th>'
    +'<th>CE lot cost</th><th>PE lot cost</th><th>worst</th></tr></thead><tbody>'+rows
    +(excl.length>MAX?('<tr><td colspan="6" class="muted">\\u2026 +'+(excl.length-MAX)+' more</td></tr>'):'')
    +'</tbody></table></div>';
}
function renderRolling(){
  // issue #529 config summary. The per-add TABLE is gone (issue #559): every
  // add is a watched symbol and already has a row, so its unique fields
  // (time / rank / watch size) moved into that row's source cell and detail.
  // `% change at add` was never unique — it is the `gap %` column for a
  // rolling row.
  const armed=curEvents.find(e=>e.event==='armed')||{};
  const adds=curEvents.filter(e=>e.event==='watchlist_add');
  const cfg=document.getElementById('rollCfg');
  if(armed.rolling_watchlist_enabled===undefined&&!adds.length){
    // pre-#529 day: the armed event predates the feature entirely
    cfg.textContent='rolling watch-list — not recorded for this day';
  }else if(armed.rolling_watchlist_enabled){
    cfg.textContent='rolling watch-list — enabled: re-rank every '+armed.rolling_cadence_s+
      's, top '+armed.rolling_top_n+'/side · '+adds.length+' added';
  }else{
    cfg.textContent='rolling watch-list — disabled this day';
  }
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
      for(const[s,side]of Object.entries(e.selected||{})){
        // never downgrade a row a `watchlist_add` already proved rolling (#545)
        if(rows[s]&&rows[s].src==='rolling')continue;
        rows[s]={side,src:'seed',gap:(e.gaps_pct||{})[s],out:'no trigger'};
      }
    }else if(e.event==='watchlist_add'){
      // rolling adds are watched symbols too (issue #529) — they belong in the
      // outcome table, marked apart from the 09:16 seed picks.
      // A `watchlist_add` PROVES a rolling add and so OVERRIDES the `selection`
      // event (issue #545): maybe_rerank skips any symbol already selected, so
      // a seed pick can never emit one. Pre-#545 logs recorded the first
      // re-rank pass inside `selection` too — and that event is written LATER
      // in the same tick, so merely filling the gap here would lose the repair.
      if(!rows[e.symbol])rows[e.symbol]={out:'no trigger'};
      const wr=rows[e.symbol];
      wr.side=e.side; wr.src='rolling'; wr.gap=e.pct_change;
      // the add's own fields (issue #559) — these are what the separate rolling
      // table used to carry, and they belong on the row they describe
      wr.addAt=e.at||e.ts; wr.addRank=e.rank; wr.addSize=e.watch_size;
    }else if(e.event==='watch_stats'){
      // every selected symbol, entered ones included (issue #524)
      for(const[s,st]of Object.entries(e.stats||{})){
        if(!rows[s])continue;
        if(rows[s].vol==null){
          rows[s].vol=st.max_vol_ratio; rows[s].volBeyond=st.max_vol_ratio_beyond;
        }
        if(!rows[s].src&&st.watch_source)rows[s].src=st.watch_source;
        if(rows[s].levelBroken==null)rows[s].levelBroken=st.level_broken;
        if(rows[s].needed==null)rows[s].needed=e.needed;
      }
    }else if(e.event==='summary'){
      // the captured-drift measurement this deployment exists for (SPEC 4)
      for(const d of (e.captured_drift||[])){
        if(rows[d.symbol])Object.assign(rows[d.symbol],
          {drift:d.trigger_vs_level_pct,driftClose:d.minclose_vs_level_pct});
      }
    }else if(!rows[e.symbol]){continue;
    }else if(e.event==='entry'){
      const r=rows[e.symbol];
      r.fill='real'; r.qty=e.qty; r.stockEntry=e.trigger_price;
      r.level=e.level??r.level; r.at=e.at; r.volRatio=e.vol_ratio;
      r.orderId=e.order_id;
      // in option mode the money is on the premium while `trigger_price` is the
      // stock — record BOTH legs (issue #555), never one standing in for the other
      if(e.instrument==='option'){r.instr='option'; r.contract=e.contract; r.optEntry=e.premium;
        r.entryBid=e.bid; r.entryAsk=e.ask; r.tick=e.tick_size; r.lotSize=e.lot_size;
        r.entryVol=e.volume; r.entryOi=e.oi;}
      r.out='<span class="pos">entered '+esc(e.at||'')+
        (e.vol_ratio?(' &middot; vol '+e.vol_ratio+'&times;'):'')+'</span>';
    }else if(e.event==='entry_rejected'){
      // issue #548 — the order never reached the market; what follows is a
      // sandbox-equivalent simulation and is badged as such
      const r=rows[e.symbol];
      r.fill='paper'; r.qty=e.qty;
      if(e.instrument==='option'){r.instr='option'; r.contract=e.contract; r.optEntry=e.entry_price;
        r.entryBid=e.bid; r.entryAsk=e.ask; r.tick=e.tick_size;}
      else{r.stockEntry=e.entry_price;}
      r.out='<span class="badge b-paper">paper</span> '+
        '<span class="muted" title="'+esc(e.error||'')+'">rejected @ '+
        esc(e.entry_price)+'</span>';
    }else if(e.event==='entry_shadow'){
      // issue #581 — this side is switched off by trade_side. The trigger was
      // real and legal; no order was placed for it, so it is badged and its
      // P&L never joins the real bucket.
      const r=rows[e.symbol];
      r.fill='shadow'; r.qty=e.qty; r.level=e.level??r.level; r.at=e.at;
      r.volRatio=e.vol_ratio; r.skipReason=e.reason;
      // BOTH legs, always (issue #555): in option mode the P&L is on the
      // premium while the signal is on the stock, and a row showing only one
      // cannot be reconciled against its own P&L. Shadow rows are no exception.
      r.stockEntry=e.trigger_price;
      if(e.instrument==='option'){r.instr='option'; r.contract=e.contract; r.optEntry=e.entry_price;}
      r.out='<span class="badge b-shadow">shadow</span> '+
        '<span class="muted">would have entered '+esc(e.at||'')+
        ' &middot; no order placed ('+esc(e.reason||'side_excluded')+')</span>';
    }else if(e.event==='entry_skipped'){
      const r=rows[e.symbol];
      r.skipReason=e.reason;
      if(e.fill==='sim')r.fill='sim';
      if(e.opt_symbol){r.instr='option'; r.contract=e.opt_symbol; r.optEntry=e.opt_entry_premium;
        // `??=`, not `=`: a later event for the same symbol that omits a field
        // must not erase what an earlier one recorded. Assigning undefined here
        // would blank the book on any day whose log carries two skip events.
        r.entryBid??=e.opt_entry_bid; r.entryAsk??=e.opt_entry_ask;
        r.tick??=e.opt_tick_size; r.lotSize??=e.opt_lot_size;
        r.entryVol??=e.opt_entry_volume; r.entryOi??=e.opt_entry_oi;}
      if(e.sim_quantity)r.qty=e.sim_quantity;
      r.out='skipped: '+esc(e.reason||'')+
        (e.fill==='sim'?' <span class="badge b-sim">priced 1 lot</span>':'');
    }else if((e.event==='exit'||e.event==='exit_paper'||e.event==='exit_sim'||
              e.event==='exit_shadow')&&e.pnl!=null){
      const r=rows[e.symbol];
      r.gross=e.gross; r.charges=e.charges; r.net=e.pnl; r.qty=e.qty??r.qty;
      r.stockExit=e.stock_exit_price??(e.instrument==='option'?r.stockExit:e.exit_price);
      if(e.instrument==='option'){
        r.instr='option'; r.contract=e.contract??r.contract;
        r.optExit=e.exit_price; r.optEntry=r.optEntry??e.opt_entry_premium??e.entry_price;
        r.exitBid=e.bid; r.exitAsk=e.ask; r.exitVol=e.volume; r.exitOi=e.oi;
      }
      if(e.stock_entry_price!=null)r.stockEntry=r.stockEntry??e.stock_entry_price;
      if(e.event==='exit_paper')r.fill='paper';
      if(e.event==='exit_sim')r.fill='sim';
      if(e.event==='exit_shadow')r.fill='shadow';
      // The outcome text no longer repeats the P&L (issues #557, #559): the
      // `net P&L` column owns that number and reads it from the JOURNAL, which
      // the reconcile passes correct. Appending the EVENT's figure here printed
      // a stale "-> +Rs316" beside a correctly reconciled "-Rs288.15" in the
      // same row — the exact divergence #557 was opened for, surviving in the
      // one cell that was prose rather than data.
    }else if(e.event==='no_entry'){
      rows[e.symbol].vol=e.max_vol_ratio;
      rows[e.symbol].volBeyond=e.max_vol_ratio_while_beyond;
      rows[e.symbol].levelBroken=e.level_broken; rows[e.symbol].needed=e.needed;
      // the gate compares the ratio measured WHILE price is beyond the level
      // (on_tick: `beyond and cum_in_min >= vol_mult*baseline`), so the outcome
      // must quote max_vol_ratio_while_beyond — the `max vol×` column's
      // peak-anywhere number can be >= needed on a symbol that never entered.
      const vb=e.max_vol_ratio_while_beyond??e.max_vol_ratio;
      rows[e.symbol].out=e.level_broken?('level broken &middot; vol '+vb+
        '&times; &lt; '+e.needed+' while beyond'):'level never broken';
    }
  }
  // A day whose LOG was destroyed still has its JOURNAL (issue #612). On
  // 2026-08-13 a late-boot arm at 14:33 overwrote the whole log with one
  // skipped_late_boot event (the #597 class), yet the 8 trades survived — so
  // this table said "no selection this day" beside a real +Rs1438. Seed a row
  // for any journal symbol the events never mentioned. Mirrors the same block
  // in selection_outcomes(); the parity test compares the SYMBOL SETS, so both
  // sides must seed or they diverge the moment a journal is passed.
  // NARROW on purpose — only when the log has NO selection event, i.e. the
  // whole timeline is gone. A stray journal symbol beside an INTACT selection is
  // anomalous and must not be seeded (the Python twin's invariant test).
  const logLostRows=!curEvents.some(e=>e.event==='selection');
  for(const j of (logLostRows?(curJournal||[]):[])){
    if(!j.symbol||rows[j.symbol])continue;
    rows[j.symbol]={side:j.side,src:j.watch_source,fromJournal:true,
      out:'<span class="muted">decision detail lost — log overwritten by a '+
          'late-boot arm; the trade below is from the journal</span>'};
  }
  // the journal wins over anything the timeline said (issue #557) — applied
  // here, immediately after the event loop, so it is part of the row-building
  // logic the parser-parity test extracts and checks against Python
  applyJournal(rows);
  // mid-window the log has published nothing yet — overlay the running max
  const liveSyms=new Set();
  for(const[s,st]of Object.entries(liveWatch)){
    if(!rows[s])continue;
    if(!rows[s].src&&st.watch_source)rows[s].src=st.watch_source;
    if(rows[s].vol==null&&st.max_vol_ratio!=null){
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
    // one row per watched symbol, its detail directly beneath (issue #559).
    // The three symbol-keyed tables this replaces forced the reader to match
    // rows by eye across the page to connect a fill to its contract's spread.
    const tr=document.createElement('tr');
    tr.className='main'; tr.tabIndex=0;
    tr.innerHTML='<td class="caret">&#9656;</td><td>'+esc(s)+'</td><td>'+esc(r.side)+
      '</td><td>'+srcBadge(r.src)+
      (r.addAt?('<span class="leg">#'+esc(r.addRank)+' @ '+esc(r.addAt)+'</span>'):'')+
      '</td><td>'+(r.gap??'')+
      '</td><td>'+fmtVol(r.vol,r.volBeyond,needed,liveSyms.has(s))+
      '</td><td>'+legCell(r,'entry')+'</td><td>'+legCell(r,'exit')+
      '</td><td>'+qtyCell(r)+'</td><td>'+pnlCell(r)+'</td><td>'+r.out+'</td>';
    const det=document.createElement('tr');
    det.className='detail';
    det.innerHTML='<td></td><td colspan="10">'+detailFor(s,r,needed)+'</td>';
    // Expansion survives the 5s auto-refresh (issue #559). The live day
    // re-renders this whole table every tick, so without this an operator
    // reading a row's fills during the window has it snap shut under them.
    const open=expanded.has(s);
    det.style.display=open?'':'none';
    tr.classList.toggle('open',open);
    tr.firstChild.innerHTML=open?'&#9662;':'&#9656;';
    tr.dataset.sym=s;
    tr.onclick=()=>toggleDetail(tr);
    tr.onkeydown=ev=>{if(ev.key==='Enter'||ev.key===' '){ev.preventDefault();toggleDetail(tr);}};
    tb.appendChild(tr); tb.appendChild(det);
  }
  if(!Object.keys(rows).length)
    tb.innerHTML='<tr><td colspan="11" class="muted">no selection this day</td></tr>';
  renderLiqNote(rows);
}
function toggleDetail(tr){
  const d=tr.nextElementSibling, open=d.style.display!=='none';
  d.style.display=open?'none':'';
  tr.classList.toggle('open',!open);
  tr.querySelector('.caret').innerHTML=open?'&#9656;':'&#9662;';
  if(open)expanded.delete(tr.dataset.sym); else expanded.add(tr.dataset.sym);
}
function dbox(title,pairs){
  // a box with nothing in it is noise — drop it rather than render empty labels
  const body=pairs.filter(p=>p&&p[1]!=null&&p[1]!=='').map(
    p=>'<tr><td>'+p[0]+'</td><td>'+p[1]+'</td></tr>').join('');
  return body?('<div class="dbox"><div class="h">'+title+'</div><table>'+body+'</table></div>'):'';
}
function detailFor(sym,r,needed){
  // The detail ADAPTS to what the row is: a filled row needs fills and
  // slippage, a skipped row needs the arithmetic that skipped it, a
  // never-entered row needs the gate that held it. Rendering all three
  // shapes for every row is how a detail panel becomes wallpaper.
  const isOpt=r.instr==='option';
  const es=spreadOf(r.entryBid,r.entryAsk,r.tick), xs=spreadOf(r.exitBid,r.exitAsk,r.tick);
  const rt=(es.pct!=null&&xs.pct!=null)?(es.pct+xs.pct):null;
  const cost=(es.abs!=null&&xs.abs!=null&&r.qty)?((es.abs+xs.abs)/2*+r.qty):null;
  const slip=(q,f)=>(q&&f)?('<span class="'+(f>q?'neg':'pos')+'">'+
    ((f/q-1)*100>=0?'+':'')+((f/q-1)*100).toFixed(2)+'%</span>'):null;
  const boxes=[];

  if(r.net!=null||r.entryFill!=null){
    let chk=null;
    if(r.brokerPnl!=null&&r.gross!=null){
      const d=Math.abs(r.brokerPnl-r.gross);
      chk=d<=1?'<span class="badge b-ok">&#10003; matches book</span>'
        :'<span class="badge b-warn">&#9888; &#8377;'+(Math.round(d*100)/100)+' diff</span>';
    }else if(r.reconcile==='unavailable')chk='<span class="badge b-warn">unavailable</span>';
    else if(r.reconcile==='pending')chk='<span class="badge b-pend">pending</span>';
    else if(r.pnlSource!=='fill')chk='<span class="badge b-pend">not reconciled</span>';
    boxes.push(dbox('FILLS &amp; CHARGES',[
      ['quote in / out',(px(isOpt?r.optEntry:r.stockEntry)||'—')+' / '+
        (px(isOpt?r.optExit:r.stockExit)||'—')],
      ['fill in / out',(r.entryFill!=null||r.exitFill!=null)
        ?('<b>'+(px(r.entryFill)||'—')+' / '+(px(r.exitFill)||'—')+'</b>'):null],
      ['slippage in',slip(isOpt?r.optEntry:r.stockEntry,r.entryFill)],
      ['slippage out',slip(isOpt?r.optExit:r.stockExit,r.exitFill)],
      ['gross',r.gross!=null?rupee2(r.gross):null],
      ['charges <span class="muted">modelled</span>',r.charges!=null?rupee2(r.charges):null],
      ['net',r.net!=null?('<span class="'+(r.net>=0?'pos':'neg')+'">'+rupee2(r.net)+'</span>'):null],
      ['priced from',r.pnlSource==='fill'?'broker fills':'quotes (not reconciled)'],
      ['broker check',chk],
      ['entry order',r.orderId?('<span class="muted">'+esc(r.orderId)+'</span>'):null],
    ]));
  }

  if(isOpt){
    boxes.push(dbox('CONTRACT LIQUIDITY',[
      ['contract','<span class="opt">'+esc(r.contract||'')+'</span>'],
      ['lot / tick',(r.lotSize??'—')+' / '+(r.tick??'—')],
      ['entry bid/ask',r.entryBid?(px(r.entryBid)+' / '+px(r.entryAsk)):null],
      ['entry spread',es.pct!=null?fmtSpread(es):null],
      ['exit bid/ask',r.exitBid?(px(r.exitBid)+' / '+px(r.exitAsk)):null],
      ['exit spread',xs.pct!=null?fmtSpread(xs):null],
      ['round trip',rt!=null?('<b>'+rt.toFixed(2)+'%</b> <span class="muted">of premium</span>'):null],
      ['spread cost',cost!=null?('&#8377;'+Math.round(cost)+
        ' <span class="muted">not deducted</span>'):null],
      ['volume',r.entryVol!=null&&r.lotSize?(Math.round(r.entryVol/r.lotSize)+
        ' <span class="muted">lots</span>'):null],
      ['OI',r.entryOi!=null&&r.lotSize?(Math.round(r.entryOi/r.lotSize)+
        ' <span class="muted">lots</span>'):null],
      ['vol / OI',(r.entryVol!=null&&r.entryOi)?(r.entryVol/r.entryOi).toFixed(1):null],
      ['OI path',(r.oiPath&&r.oiPath.oi_change!=null)
        ?('<span class="'+(r.oiPath.oi_change>=0?'pos':'neg')+'">'+
          (r.oiPath.oi_change>=0?'+':'')+
          (r.oiPath.oi_change_lots!=null?(r.oiPath.oi_change_lots+' lots'):r.oiPath.oi_change)+
          '</span> <span class="muted">'+r.oiPath.minutes+' min, '+
          (r.oiPath.oi_change>=0?'building':'unwinding')+'</span>'):null],
    ]));
  }

  if(r.fill==='sim'||/unaffordable|max_trades_cap/.test(r.skipReason||'')){
    // the arithmetic that skipped it — currently only derivable by hand
    const armed=curEvents.find(e=>e.event==='armed')||{};
    const need=(r.lotSize&&r.optEntry)?(r.lotSize*r.optEntry):null;
    const slot=armed.margin_effective??armed.margin_per_slot;
    boxes.push(dbox('WHY IT WAS SKIPPED',[
      ['reason',esc(r.skipReason||'')],
      ['needed',need!=null?(r.lotSize+' &times; '+rupee2(r.optEntry)+' = <b>'+
        rupee2(need)+'</b>'):null],
      ['slot capital',slot!=null?rupee2(slot):null],
      ['short by',(need!=null&&slot!=null&&need>slot)
        ?('<span class="neg">'+rupee2(need-slot)+'</span>'):null],
      ['priced at',r.qty?(r.qty+' <span class="muted">(1 lot, measurement only)</span>'):null],
    ]));
  }else if(r.net==null&&r.vol!=null){
    boxes.push(dbox('WHY IT NEVER ENTERED',[
      ['level',r.level!=null?px(r.level):null],
      ['level broken',r.levelBroken==null?null
        :(r.levelBroken?'<span class="pos">yes</span>':'<span class="neg">never</span>')],
      ['peak vol anywhere',r.vol!=null?(r.vol+'&times;'):null],
      ['peak vol while beyond',r.volBeyond!=null?(r.volBeyond+'&times;')
        :'<span class="muted">n/a — never beyond</span>'],
      ['needed',(r.needed??needed)!=null?((r.needed??needed)+'&times; while beyond'):null],
    ]));
  }

  boxes.push(dbox('DECISION',[
    ['level',r.level!=null?px(r.level):null],
    ['trigger',r.at?(esc(r.at)+(r.stockEntry?(' @ '+px(r.stockEntry)):'')):null],
    ['vol at trigger',r.volRatio!=null?(r.volRatio+'&times;'):null],
    ['captured drift',r.drift!=null?(r.drift+'% <span class="muted">vs level</span>'):null],
    ['min-close drift',r.driftClose!=null?(r.driftClose+'%'):null],
    ['watch-list add',r.addAt?(esc(r.addAt)+', rank #'+esc(r.addRank)+
      ', size '+esc(r.addSize)):null],
    ['% change at add',(r.addAt&&r.gap!=null)?(r.gap+'%'):null],
  ]));

  const html=boxes.filter(Boolean).join('');
  return html?('<div class="dgrid">'+html+'</div>')
    :'<span class="muted">nothing further recorded for this symbol</span>';
}
function rupee2(v){
  return (v<0?'-':'')+'&#8377;'+Math.abs(Math.round(v*100)/100).toLocaleString('en-IN');
}
function renderLiqNote(rows){
  const anySpread=Object.values(rows).some(r=>r.entryBid||r.exitBid);
  const anyOpt=Object.values(rows).some(r=>r.instr==='option');
  const note=document.getElementById('liqNote');
  if(!anyOpt){note.textContent='';return;}
  note.innerHTML=anySpread
    ? 'Liquidity is reported in <b>LOTS</b> — raw contract counts are not comparable '+
      'across a universe whose lot sizes differ ~30&times;. <b>Spread cost is NOT deducted '+
      'from P&amp;L</b>: sim, paper and shadow rows price both legs at the quote LTP, so '+
      'their net is optimistic by roughly that amount; real rows use broker fills, where '+
      'the spread is already inside the fill price.'
    : 'Bid/ask not captured for this day &mdash; it starts with the next armed session.';
}
// --- liquidity derivations (issue #555) ------------------------------------
// Mirrors services/open15_liquidity.py. Two rules it must not break:
//  * spread % is of the MID, not the LTP — the LTP is whichever side last
//    traded, so quoting against it makes the same book look wider or narrower
//    depending on who traded last;
//  * a contract COUNT is not a quantity until divided by lot size. Lot sizes
//    here differ ~30x (HAL 150 vs SAIL 4700), which is why the raw #488
//    columns ranked contracts backwards.
function spreadOf(bid,ask,tick){
  const b=+bid, a=+ask;
  if(!bid||!ask||!(a>=b))return {abs:null,pct:null,ticks:null};
  const w=a-b, mid=(a+b)/2;
  return {abs:w, pct:mid?(w/mid*100):null, ticks:tick?(w/+tick):null};
}
function inLots(n,lot){return (n!=null&&lot)?(+n/+lot):null;}
function fmtSpread(s){
  if(s.pct==null)return dash;
  return s.abs.toFixed(2)+' <span class="muted">'+s.pct.toFixed(2)+'%'+
    (s.ticks!=null?(' &middot; '+s.ticks.toFixed(0)+'t'):'')+'</span>';
}
function applyJournal(rows){
  // issue #557: the JOURNAL is authoritative for everything it stores, because
  // the reconcile passes correct it IN PLACE. The decision log records what was
  // believed at the time. Reading P&L out of events left the rows stale next to
  // a chip that reads the journal — and the arm-time catch-up, whose whole job
  // is to run on a LATER day, can never append events to the day it corrects.
  // Mirrors `apply_journal` in services/open15_log_view.py.
  for(const j of (curJournal||[])){
    const r=rows[j.symbol];
    if(!r)continue;                       // never triggered: nothing to correct
    r.stockEntry=j.trigger_price??r.stockEntry;
    r.stockExit=j.exit_price??r.stockExit;
    // the option-mode `entry` event never carried `level` (only the stock path
    // logs it), so without this the breakout level is blank on exactly the rows
    // that traded. Python's `_JOURNAL_OWNED` already had it.
    r.level=j.level??r.level;
    r.skipReason=j.reason??r.skipReason;
    r.instr=j.instrument||r.instr;
    r.contract=j.opt_symbol??r.contract;
    r.optEntry=j.opt_entry_premium??r.optEntry;
    r.optExit=j.opt_exit_premium??r.optExit;
    r.entryFill=j.entry_fill_price??r.entryFill;
    r.exitFill=j.exit_fill_price??r.exitFill;
    r.pnlSource=j.pnl_source||r.pnlSource;
    r.reconcile=j.fill_reconcile_status??r.reconcile;
    r.brokerPnl=j.broker_pnl??r.brokerPnl;
    r.fill=j.fill||r.fill;
    // `quantity` is what was ORDERED; `sim_quantity` is what a non-traded row
    // is priced on. Exactly one is meaningful per row.
    r.qty=j.quantity||j.sim_quantity||r.qty;
    r.lotSize=j.opt_lot_size??r.lotSize;
    r.tick=j.opt_tick_size??r.tick;
    r.entryBid=j.opt_entry_bid??r.entryBid; r.entryAsk=j.opt_entry_ask??r.entryAsk;
    r.exitBid=j.opt_exit_bid??r.exitBid;    r.exitAsk=j.opt_exit_ask??r.exitAsk;
    r.entryVol=j.opt_entry_volume??r.entryVol; r.entryOi=j.opt_entry_oi??r.entryOi;
    r.exitVol=j.opt_exit_volume??r.exitVol;    r.exitOi=j.opt_exit_oi??r.exitOi;
    if(j.liquidity_path&&j.liquidity_path.minutes)r.oiPath=j.liquidity_path;
    if(j.pnl!=null){
      // the journal stores GROSS in `pnl` with charges separate; every number
      // this page shows is NET (issue #552). Copying `pnl` straight across
      // would reintroduce the gross/net split inside the fix for #557.
      r.gross=j.pnl; r.charges=j.charges_inr;
      r.net=Math.round((j.pnl-(j.charges_inr||0))*100)/100;
    }
  }
}
const dash='<span class="muted">&mdash;</span>';
function px(v){return v==null?null:(Math.round(v*100)/100).toFixed(2);}
function legCell(r,leg){
  // stock line on top, option leg beneath. In option mode the P&L is on the
  // premium while the signal is on the stock, so a cell showing only one of them
  // cannot be reconciled against its own P&L (issue #555).
  const stock=px(leg==='entry'?r.stockEntry:r.stockExit);
  const opt=px(leg==='entry'?r.optEntry:r.optExit);
  const fill=px(leg==='entry'?r.entryFill:r.exitFill);
  if(stock==null&&opt==null)return dash;
  let out=stock!=null?stock:'<span class="muted">n/a</span>';
  if(r.instr==='option'&&opt!=null){
    out+='<span class="leg"><span class="opt">'+esc(shortC(r.contract))+'</span> '+opt;
    // the broker's own number, next to the quote it is being compared against
    if(fill!=null){
      const slip=((fill/parseFloat(opt))-1)*100;
      out+=' &rarr; <b>'+fill+'</b> <span class="'+(Math.abs(slip)<0.01?'muted':'slip')+'">'+
        (slip>=0?'+':'')+slip.toFixed(2)+'%</span>';
    }
    out+='</span>';
  }else if(fill!=null&&stock!=null){
    const slip=((fill/parseFloat(stock))-1)*100;
    out+='<span class="leg">fill <b>'+fill+'</b> <span class="slip">'+
      (slip>=0?'+':'')+slip.toFixed(2)+'%</span></span>';
  }
  return out;
}
function shortC(c){
  // the contract's tail (strike + CE/PE) is what distinguishes it; the leading
  // underlying repeats the symbol column
  if(!c)return '';
  return c.length>16?('\\u2026'+c.slice(-14)):c;
}
function qtyCell(r){
  // `quantity` is what was ORDERED. For a bucket where nothing was ordered it
  // is 0, and the number below it is the PRICING size — never the other way
  // round, or a shadow row would read as a position that existed.
  if(r.fill==='sim'||r.fill==='shadow')
    return '<span class="muted">0</span><span class="leg">'+esc(r.fill)+' '+
      esc(r.qty??'')+'</span>';
  return r.qty!=null?esc(r.qty):dash;
}
function pnlCell(r){
  if(r.net==null)return dash;
  const cls=r.net>=0?'pos':'neg';
  const badge={real:'<span class="badge b-real">real</span>',
    paper:'<span class="badge b-paper">paper</span>',
    sim:'<span class="badge b-sim">sim</span>',
    shadow:'<span class="badge b-shadow">shadow</span>'}[r.fill]||'';
  const src=r.pnlSource==='fill'
    ?'<span class="badge b-ok" title="gross computed from broker fill prices">fill</span>'
    :'<span class="badge b-pend" title="gross computed from quote/tick prices — '+
     'not yet reconciled against the broker">quote</span>';
  return '<span class="'+cls+'">'+(r.net>=0?'+':'')+'&#8377;'+r.net+'</span>'+
    '<span class="leg">gross '+(r.gross??'?')+' &middot; charges '+(r.charges??'?')+
    ' '+badge+' '+src+'</span>';
}
function renderTimeline(){
  const tb=document.querySelector('#t tbody'); tb.innerHTML='';
  for(const e of curEvents){
    if(curFilter!=='all'&&kindOf(e)!==curFilter)continue;
    const{ts,event,...rest}=e;
    // the sorted per-name cost list is the card's data, not timeline prose —
    // rendered raw it is a ~15 KB cell (issue #591)
    if(event==='atm_lot_cost')rest.costs='['+(rest.costs||[]).length+' names — see ladder card]';
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


def serialize_trade(r) -> dict:
    """One journal row as JSON — the SINGLE serializer (issue #557).

    Shared by ``/api/trades`` and ``/api/decision_log`` so the two can never
    drift into describing the same row differently, which is the defect class
    #557 is about one level up.
    """
    from services import open15_liquidity as _liquidity

    return {
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
        # top-of-book at both decision moments (issue #555). The
        # strategy sends MARKET orders, so the spread is a real cost
        # and these are what make it measurable.
        "opt_entry_bid": r.opt_entry_bid,
        "opt_entry_ask": r.opt_entry_ask,
        "opt_exit_bid": r.opt_exit_bid,
        "opt_exit_ask": r.opt_exit_ask,
        "opt_tick_size": r.opt_tick_size,
        # derived, lot- and rupee-normalized — raw contract counts
        # are not comparable across a universe whose lot sizes differ
        # ~30x, which is what made the #488 columns unreadable
        "liquidity": _liquidity.derive(r),
        "spread_cost_inr": _liquidity.spread_cost_inr(r),
        "liquidity_path": _liquidity.summarize_path(r.opt_liquidity_path, r.opt_lot_size),
        # seed (09:16 gap ranking) vs rolling (intraday re-rank),
        # issue #529. Pre-#529 rows are NULL — they are all seeds.
        "watch_source": r.watch_source or "seed",
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
        "charges_inr": r.charges_inr,
        # what the BROKER actually filled, vs the quote/tick prices
        # above that the decision was made on (issue #555). The gap
        # between them is this strategy's slippage.
        "entry_fill_price": r.entry_fill_price,
        "exit_fill_price": r.exit_fill_price,
        "entry_fill_qty": r.entry_fill_qty,
        "exit_fill_qty": r.exit_fill_qty,
        "fill_reconcile_status": r.fill_reconcile_status,
        # `fill` once reconciled, else `quote`; NULL on pre-#555 rows
        # (all quote-derived)
        "pnl_source": r.pnl_source or "quote",
        "broker_pnl": r.broker_pnl,
        # pricing size for a row no order was placed for — never an
        # order quantity (`quantity` stays 0 on those)
        "sim_quantity": r.sim_quantity,
        "status": r.status,
        "reason": r.reason,
        # real fill vs broker-rejected paper simulation (issue #548).
        # NULL on pre-#548 rows — those are all real.
        "fill": r.fill or "real",
        "error_message": r.error_message,
    }


def journal_for_date(date: str) -> list[dict]:
    """Serialized journal rows for one trade date (issue #557).

    The journal is AUTHORITATIVE for everything it stores — prices, quantities,
    P&L, fills, liquidity — because it is corrected in place by the reconcile
    passes. The decision log is authoritative for the decision TIMELINE. Rows
    that read P&L out of the event log go stale the moment a reconcile lands,
    which is the whole of #557.
    """
    from database.open15_breakout_db import Open15Trade, db_session

    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(Open15Trade.trade_date == date)
            .order_by(Open15Trade.id.asc())
            .all()
        )
        return [serialize_trade(r) for r in rows]
    except Exception:
        logger.exception("open15: journal read failed for %s", date)
        return []
    finally:
        db_session.remove()


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
        return jsonify([serialize_trade(r) for r in rows])
    except Exception:
        logger.exception("open15: trades query failed")
        return jsonify([]), 500
    finally:
        db_session.remove()


# --------------------------------------------------------------------------- #
# Replay of a missed session (issue #604 — endpoints for the #600 engine)
# --------------------------------------------------------------------------- #
# One run at a time, process-wide. A replay is ~250 broker historical calls
# against the same 3 req/s budget the live strategy needs, so two concurrent
# runs (a double-click, two browser tabs) would be materially worse than slow —
# they would contend with the strategy itself. The lock is the enforcement; the
# UI's disabled button is only the hint.
_REPLAY_LOCK = threading.Lock()
_REPLAY_STATE: dict = {}  # date -> {status, progress, total, error, started_at}
_REPLAY_STATE_LOCK = threading.Lock()


def _replay_set(date: str, **fields) -> None:
    with _REPLAY_STATE_LOCK:
        _REPLAY_STATE.setdefault(date, {}).update(fields)


def _replay_get(date: str) -> dict:
    with _REPLAY_STATE_LOCK:
        return dict(_REPLAY_STATE.get(date) or {})


def _replay_worker(date: str, force: bool) -> None:
    """Run the replay on a real OS thread and record the outcome.

    A REAL thread, not an eventlet green thread: the engine makes blocking
    broker calls, and under gunicorn's eventlet worker a blocking call on the
    hub stalls every other request in the process (the same reason the Stage-1
    veto uses an unpatched thread).
    """
    from services.open15_replay import ReplayIneligible, replay_session

    try:
        out = replay_session(
            date,
            apply=True,
            force=force,
            progress=lambda n, total: _replay_set(date, progress=n, total=total),
        )
        summary = out["events"][-1]
        _replay_set(
            date,
            status="done",
            rows_written=out["persisted"]["rows_written"],
            net_close_entry=summary.get("net_close_entry"),
            net_early_entry=summary.get("net_early_entry"),
            error=None,
        )
        logger.info(
            "open15 replay %s: done — %d rows, net %s",
            date,
            out["persisted"]["rows_written"],
            summary.get("net_close_entry"),
        )
    except ReplayIneligible as e:
        # Not a crash: the day stopped qualifying between the click and the
        # write (most importantly ``day_was_traded``). Report the reason.
        _replay_set(date, status="failed", error=f"{e.reason}: {e.detail}".strip(": "))
        logger.warning("open15 replay %s: refused — %s", date, e)
    except Exception as e:
        _replay_set(date, status="failed", error=str(e) or e.__class__.__name__)
        logger.exception("open15 replay %s: failed", date)
    finally:
        _REPLAY_LOCK.release()


@open15_bp.route("/api/replay/eligibility", methods=["GET"])
@check_session_validity
def replay_eligibility():
    """Can this date be replayed? Drives the button and its tooltip."""
    date = (request.args.get("date") or "").strip()
    if not date:
        return jsonify({"status": "error", "message": "date is required"}), 400
    try:
        from services.open15_replay import check_eligibility

        out = check_eligibility(date, allow_rereplay=True)
        # An in-flight run is not an eligibility fact, but the button must not
        # offer a second one, so it rides the same response.
        out["running"] = _replay_get(date).get("status") == "running"
        out["busy"] = _REPLAY_LOCK.locked()
        return jsonify(out)
    except Exception:
        logger.exception("open15: replay eligibility failed for %s", date)
        return jsonify({"eligible": False, "reason": "check_failed"}), 500


@open15_bp.route("/api/replay", methods=["POST"])
@check_session_validity
def replay_start():
    """Start a replay. 403 if ineligible, 409 if one is already running."""
    body = request.get_json(silent=True) or {}
    date = str(body.get("date") or "").strip()
    force = bool(body.get("force"))
    if not date:
        return jsonify({"status": "error", "message": "date is required"}), 400

    from services.open15_replay import check_eligibility

    # Server-side re-check: the button is a hint, this is the gate. A crafted
    # POST must not be able to replay a traded day or run during market hours.
    elig = check_eligibility(date, allow_rereplay=force)
    if not elig.get("eligible"):
        return jsonify(
            {
                "status": "error",
                "reason": elig.get("reason"),
                "message": elig.get("detail") or elig.get("reason"),
            }
        ), 403

    if not _REPLAY_LOCK.acquire(blocking=False):
        return jsonify(
            {"status": "error", "reason": "busy", "message": "a replay is already running"}
        ), 409

    # released by the worker's finally, including on failure
    _replay_set(
        date,
        status="running",
        progress=0,
        total=None,
        error=None,
        started_at=dt.datetime.now().isoformat(timespec="seconds"),
    )
    threading.Thread(
        target=_replay_worker, args=(date, force), name=f"open15-replay-{date}", daemon=True
    ).start()
    return jsonify({"status": "success", "date": date}), 202


@open15_bp.route("/api/replay/status", methods=["GET"])
@check_session_validity
def replay_status():
    """Progress for a replay started in this process."""
    date = (request.args.get("date") or "").strip()
    if not date:
        return jsonify({"status": "error", "message": "date is required"}), 400
    state = _replay_get(date)
    if not state:
        return jsonify({"status": "idle", "date": date})
    return jsonify({"date": date, **state})
