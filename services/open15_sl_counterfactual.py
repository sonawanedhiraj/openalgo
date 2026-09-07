"""Stop-loss counterfactual for open15 real fills (issue #704).

The per-trade stop loss (#696) exits a row the moment its MTM reaches
``-stop_loss_inr`` — and from that second the contract vanished from every
measurement, so the one question the rule exists to answer ("would holding to
the scheduled exit have been better or worse?") had no data behind it. This
module keeps MARKING a stopped contract to the day's scheduled exit and stores
the result on the row as a **counterfactual**, never as money:

- ``derive_from_closes(row, closes, sched_exit_min)`` — pure: from 1m closes,
  the mark at the scheduled exit, the gross held-to-exit P&L on the same
  entry-fill basis and quantity the real row used, modelled charges on that
  counterfactual round trip, and the worst / best marks between the stop and
  the exit (how far the contract ran against and for us once we were out).
- ``backfill_missing(date=None)`` — prices every stop row whose counterfactual
  is still NULL from the broker 1m bars the intra-hold curve already fetches
  (``cf_source='bars'``). Called from the summary job and the next 09:10 arm,
  so a restart between the stop and the exit loses nothing, and usable as a
  CLI for historical rows (dry-run default).
- ``scorecard()`` — every stop since the rule went live, with the
  pre-registered decision rule applied, for the /logs card.

The LIVE capture (``cf_source='live'``) lives in the strategy service: the
risk monitor already polls ``live_pnl()`` every cycle, stopped rows ride the
same batched quote call as ``ghost`` marks, and ``flatten`` — which fires AT
the scheduled exit — stamps the last ghost mark. Bars back-fill whatever the
live path missed.

Rules, each load-bearing:

- **Counterfactual money never joins a P&L bucket** (#552/#548). The one
  derived figure is ``open15_breakout_db.stop_saved_of_row``; nothing here
  writes ``pnl`` or ``charges_inr``.
- **The window ends at the SCHEDULED exit** (operator decision): the
  comparison is against the strategy without the stop, not a research hold.
- **Priced at the 1m close, which slightly flatters "held"** — a real exit
  would cross the spread. The decision rule's threshold is set knowing that.
- **Read-only on the broker; never on the tick thread.** Every entry point is
  an APScheduler job, a request handler or the CLI.
- **Never raises to a caller** — a row that cannot be priced is reported as
  pending with a reason, and the next backstop retries it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

# Pre-registered decision rule. Fixed BEFORE the sample fills so the data can
# only be read one way: keep the per-trade stop only if, after MIN_EVENTS
# stops, the cumulative net "stop saved" is positive AND at least
# RIGHT_RATE_MIN of stops were right (held would have lost more). Code
# constants on purpose — a rule that can be re-tuned to fit the sample is not
# a rule (#651 shape: a correctness guard is not a preference).
DECISION_MIN_EVENTS = 20
DECISION_RIGHT_RATE_MIN = 0.5


def _min_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _hhmm_to_min(hhmm: str | None) -> int | None:
    try:
        h, m = str(hhmm).split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def scheduled_exit_min(trade_date: str) -> int:
    """The day's scheduled exit as minutes since midnight IST.

    Read from the persisted ``armed`` event (the arm records the effective
    ``exit_time``, #451/#645), falling back to the env/config default when the
    day has no log — a day that was never armed has no stop rows anyway.
    """
    try:
        from database.open15_breakout_db import get_day_log

        for ev in get_day_log(trade_date) or []:
            if isinstance(ev, dict) and ev.get("event") == "armed":
                m = _hhmm_to_min(ev.get("exit_time"))
                if m is not None:
                    return m
                break
    except Exception:
        logger.exception("open15 sl-cf: armed-event read failed for %s", trade_date)
    try:
        from services.open15_breakout_service import _exit_time_default

        m = _hhmm_to_min(_exit_time_default())
        if m is not None:
            return m
    except Exception:
        logger.exception("open15 sl-cf: exit-time default unavailable")
    return 9 * 60 + 30


def _stop_min(row) -> int | None:
    """Minute (IST) the stop exit was sent, from ``exit_ts``."""
    if not row.exit_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(row.exit_ts)
        if ts.tzinfo is None:
            ts = _IST.localize(ts)
        ts = ts.astimezone(_IST)
        return ts.hour * 60 + ts.minute
    except (ValueError, TypeError):
        return None


def counterfactual_charges(row, basis: float, cf_exit: float, qty: int) -> float | None:
    """Modelled round-trip charges had the row been held to ``cf_exit``.

    Mirrors ``_exit_open_row``: option rows are long premium (buy then sell on
    the FULL quantity); stock rows route the legs by side.
    """
    if (row.instrument or "stock") == "option":
        from services.open15_option_shadow import option_round_trip_charges

        return option_round_trip_charges(basis * qty, cf_exit * qty)
    from services.open15_breakout_service import mis_round_trip_charges

    buy_px = basis if row.side == "L" else cf_exit
    sell_px = cf_exit if row.side == "L" else basis
    return mis_round_trip_charges(buy_px * qty, sell_px * qty)


def derive_from_closes(row, closes: dict[str, float], sched_exit_min: int) -> dict | None:
    """Pure: the counterfactual fields for one stop row from 1m closes.

    Marks run from the stop minute's own bar (its close is AFTER the stop)
    through the last full bar before the scheduled exit — the same
    ``exit_min - 1`` convention the intra-hold curve uses, so "held to 09:30"
    is priced at the 09:29 close exactly as a real 09:30 flatten's curve ends.
    Returns None when the exit mark is missing (bars not yet available).
    """
    from services.open15_pnl_curve import _entry_basis, _row_sign

    basis, _src = _entry_basis(row)
    qty = int(row.entry_fill_qty or row.quantity or 0)
    stop_min = _stop_min(row)
    if basis is None or not qty or stop_min is None:
        return None
    last_mark = sched_exit_min - 1
    if stop_min > last_mark:
        # stopped at/after the scheduled exit — nothing to hold through
        return None
    sign = _row_sign(row)
    exit_close = closes.get(_min_to_hhmm(last_mark))
    if exit_close is None:
        return None
    mae = mfe = None
    mae_m = mfe_m = None
    for m in range(stop_min, last_mark + 1):
        hhmm = _min_to_hhmm(m)
        c = closes.get(hhmm)
        if c is None:
            continue
        mtm = round((c - basis) * qty * sign, 2)
        if mae is None or mtm < mae:
            mae, mae_m = mtm, hhmm
        if mfe is None or mtm > mfe:
            mfe, mfe_m = mtm, hhmm
    cf_pnl = round((exit_close - basis) * qty * sign, 2)
    return {
        "cf_exit_minute": _min_to_hhmm(sched_exit_min),
        "cf_exit_price": float(exit_close),
        "cf_pnl": cf_pnl,
        "cf_charges_inr": counterfactual_charges(row, basis, float(exit_close), qty),
        "cf_mae": mae,
        "cf_mae_minute": mae_m,
        "cf_mfe": mfe,
        "cf_mfe_minute": mfe_m,
    }


def derive_from_live(
    row, sched_exit_min: int, exit_ltp: float, path: list[tuple[str, float]]
) -> dict | None:
    """The counterfactual from the risk monitor's own ghost marks.

    ``exit_ltp`` is the last quote the monitor saw before the scheduled exit and
    ``path`` its per-poll ``(HH:MM:SS, mtm)`` ghost marks since the stop — the
    same basis and quantity as the bars variant, so ``live`` and ``bars`` rows
    are comparable. Quote-level MAE/MFE is finer than 1m closes; the source
    column says which one a row carries.
    """
    from services.open15_pnl_curve import _entry_basis, _row_sign

    basis, _src = _entry_basis(row)
    qty = int(row.entry_fill_qty or row.quantity or 0)
    if basis is None or not qty or exit_ltp is None:
        return None
    sign = _row_sign(row)
    cf_pnl = round((float(exit_ltp) - basis) * qty * sign, 2)
    mae = mfe = None
    mae_m = mfe_m = None
    for ts, mtm in path or []:
        if mtm is None:
            continue
        if mae is None or mtm < mae:
            mae, mae_m = mtm, ts[:5]
        if mfe is None or mtm > mfe:
            mfe, mfe_m = mtm, ts[:5]
    if mae is None:
        mae, mae_m = cf_pnl, _min_to_hhmm(sched_exit_min)
        mfe, mfe_m = cf_pnl, _min_to_hhmm(sched_exit_min)
    return {
        "cf_exit_minute": _min_to_hhmm(sched_exit_min),
        "cf_exit_price": float(exit_ltp),
        "cf_pnl": cf_pnl,
        "cf_charges_inr": counterfactual_charges(row, basis, float(exit_ltp), qty),
        "cf_mae": round(min(mae, cf_pnl), 2),
        "cf_mae_minute": mae_m,
        "cf_mfe": round(max(mfe, cf_pnl), 2),
        "cf_mfe_minute": mfe_m,
    }


def backfill_missing(trade_date: str | None = None, apply: bool = True) -> dict:
    """Price every unpriced stop row from broker 1m bars (``cf_source='bars'``).

    Idempotent (only NULL rows are touched) and per-row fail-graceful: a row
    whose bars are not yet available stays NULL for the next backstop. Returns
    ``{checked, priced, pending, rows}``; with ``apply=False`` nothing is
    written (the CLI's dry run) and ``rows`` carries what would be.
    """
    from database.open15_breakout_db import stop_loss_rows, update_trade
    from services.open15_pnl_curve import _closes_by_minute, _fetch_bars, _row_contract

    out: dict = {"checked": 0, "priced": 0, "pending": 0, "rows": []}
    rows = stop_loss_rows(trade_date=trade_date, unpriced_only=True)
    bars_cache: dict[tuple[str, str], dict[str, float] | None] = {}
    exit_cache: dict[str, int] = {}
    for row in rows:
        out["checked"] += 1
        detail = {"id": row.id, "date": row.trade_date, "symbol": row.symbol}
        try:
            sched = exit_cache.get(row.trade_date)
            if sched is None:
                sched = exit_cache[row.trade_date] = scheduled_exit_min(row.trade_date)
            contract, exchange = _row_contract(row)
            key = (contract, row.trade_date)
            if key not in bars_cache:
                bars = _fetch_bars(contract, row.trade_date, exchange=exchange)
                bars_cache[key] = _closes_by_minute(bars) if bars is not None else None
            closes = bars_cache[key]
            if not closes:
                detail["pending"] = "1m bars unavailable"
                out["pending"] += 1
                out["rows"].append(detail)
                continue
            cf = derive_from_closes(row, closes, sched)
            if cf is None:
                detail["pending"] = "exit mark not in bars yet"
                out["pending"] += 1
                out["rows"].append(detail)
                continue
            cf["cf_source"] = "bars"
            detail.update(cf)
            if apply:
                if update_trade(row.id, **cf):
                    out["priced"] += 1
                else:
                    detail["pending"] = "journal write failed"
                    out["pending"] += 1
            else:
                out["priced"] += 1
            out["rows"].append(detail)
        except Exception:
            logger.exception("open15 sl-cf: backfill failed for row %s", row.id)
            detail["pending"] = "error — see logs"
            out["pending"] += 1
            out["rows"].append(detail)
    return out


def _event_of(row) -> dict:
    from database.open15_breakout_db import cf_net_of_row, net_pnl_of_row, stop_saved_of_row
    from services.open15_pnl_curve import _row_contract

    contract, _ex = _row_contract(row)
    stop_at = None
    if row.exit_ts:
        try:
            ts = dt.datetime.fromisoformat(row.exit_ts)
            if ts.tzinfo is None:
                ts = _IST.localize(ts)
            stop_at = ts.astimezone(_IST).strftime("%H:%M:%S")
        except (ValueError, TypeError):
            stop_at = None
    saved = stop_saved_of_row(row)
    return {
        "id": row.id,
        "date": row.trade_date,
        "symbol": row.symbol,
        "side": row.side,
        "contract": contract,
        "qty": int(row.entry_fill_qty or row.quantity or 0),
        "mode": row.mode,
        "stop_at": stop_at,
        "stop_net": round(net_pnl_of_row(row), 2) if row.pnl is not None else None,
        "held_net": cf_net_of_row(row),
        "held_gross": row.cf_pnl,
        "cf_exit_minute": row.cf_exit_minute,
        "saved": saved,
        "verdict": None if saved is None else ("right" if saved > 0 else "wrong"),
        "mae": row.cf_mae,
        "mae_minute": row.cf_mae_minute,
        "mfe": row.cf_mfe,
        "mfe_minute": row.cf_mfe_minute,
        "source": row.cf_source,
        "pending": row.cf_pnl is None,
    }


def scorecard() -> dict:
    """Every stop since the rule went live + the decision rule's verdict.

    ``verdict`` is one of ``insufficient_sample`` / ``keep`` / ``disable``,
    derived ONLY from priced events; pending rows are counted and named so a
    thin scorecard is never mistaken for a quiet one.
    """
    from database.open15_breakout_db import stop_loss_rows

    events = [_event_of(r) for r in stop_loss_rows()]
    priced = [e for e in events if not e["pending"]]
    saved = [e["saved"] for e in priced]
    right = sum(1 for e in priced if e["verdict"] == "right")
    wrong = len(priced) - right
    net_saved = round(sum(saved), 2) if saved else 0.0
    right_rate = (right / len(priced)) if priced else None
    mfes = [e["mfe"] for e in priced if e["mfe"] is not None]
    if len(priced) < DECISION_MIN_EVENTS:
        verdict = "insufficient_sample"
    elif net_saved > 0 and right_rate is not None and right_rate >= DECISION_RIGHT_RATE_MIN:
        verdict = "keep"
    else:
        verdict = "disable"
    return {
        "status": "ok",
        "since": events[0]["date"] if events else None,
        "n_events": len(events),
        "n_priced": len(priced),
        "n_pending": len(events) - len(priced),
        "n_days": len({e["date"] for e in events}),
        "right": right,
        "wrong": wrong,
        "right_rate": round(right_rate, 3) if right_rate is not None else None,
        "net_saved": net_saved,
        "median_saved": round(statistics.median(saved), 2) if saved else None,
        "mean_saved": round(statistics.fmean(saved), 2) if saved else None,
        "worst_saved": round(min(saved), 2) if saved else None,
        "best_saved": round(max(saved), 2) if saved else None,
        "avg_mfe_after_stop": round(statistics.fmean(mfes), 2) if mfes else None,
        "rule": {
            "min_events": DECISION_MIN_EVENTS,
            "right_rate_min": DECISION_RIGHT_RATE_MIN,
            "text": (
                f"keep the per-trade stop only if, after {DECISION_MIN_EVENTS} stop events, "
                f"cumulative stop-saved is > 0 AND the stop-right rate is "
                f">= {int(DECISION_RIGHT_RATE_MIN * 100)}%"
            ),
        },
        "verdict": verdict,
        "events": events,
    }


def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="Price the stop-loss counterfactual for unpriced open15 stop rows "
        "from broker 1m bars (issue #704). Dry-run by default."
    )
    ap.add_argument("--date", help="YYYY-MM-DD; default = every unpriced stop row")
    ap.add_argument("--apply", action="store_true", help="write the journal (default: dry run)")
    ap.add_argument("--scorecard", action="store_true", help="print the scorecard and exit")
    args = ap.parse_args()
    # the cf_* columns are migrated in at app boot (`_ensure_columns`); a CLI
    # run against an install that has not restarted since #704 needs them too
    from database.open15_breakout_db import init_db

    init_db()
    if args.scorecard:
        print(json.dumps(scorecard(), indent=2))
        return 0
    res = backfill_missing(trade_date=args.date, apply=args.apply)
    res["mode"] = "apply" if args.apply else "dry_run"
    print(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
