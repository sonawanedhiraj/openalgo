"""Intra-hold P&L curve + live P&L for open15 real fills (issue #692).

Answers the operator question "did holding to the exit time help, or would an
earlier exit have done better?" with data instead of a guess:

- ``build_pnl_curve(date)`` — minute-by-minute mark-to-market of the day's
  REAL-fill trades between entry and exit, from the same broker 1m bars the
  option shadow already fetches (``open15_option_shadow.fetch_1m_bars``).
  Marks are 1m closes measured against the broker ``entry_fill_price``; the
  final point is anchored to the journal's own gross ``pnl`` (fill-derived
  since #555), so the curve's endpoint always reproduces the journal exactly.
- ``live_pnl()`` — the same mark for trades still OPEN, from ONE batched
  quote call (``get_multiquotes``), server-cached so N viewers never multiply
  broker calls. The poll interval is UI-configurable
  (``open15_config.live_poll_interval_s``, env seed ``OPEN15_LIVE_POLL_S``,
  clamped 3..60 s).

Rules carried from the journal's own conventions:

- **Real fills only** (#555): sim / paper / shadow / replay rows never enter
  this curve — their prices are conventions, not money.
- **Net derives only via ``net_pnl_of_row``** (#552) — this module never
  invents a second P&L convention; the curve itself is GROSS and says so.
- **Read-only.** Nothing here places, modifies or retries an order, and the
  live poll never runs on the tick thread (#626 rule). Entry points are Flask
  request handlers plus, since #696, the strategy's background risk-monitor
  thread — which is what keeps this cache fresh with no page open. Acting on
  the numbers (stops, trail) stays in ``open15_breakout_service``; this
  module only ever measures.
- **Degrades loudly, never raises to the page** (#645 shape): a trade whose
  bars cannot be fetched is returned as ``unavailable`` with a reason; the
  endpoint still answers 200 with everything it could compute.
"""

from __future__ import annotations

import datetime as dt
import os
import threading

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

_LIVE_POLL_MIN_S = 3
_LIVE_POLL_MAX_S = 60
_LIVE_POLL_FALLBACK_S = 5


def clamp_live_poll_interval(value) -> int:
    """Clamp a proposed live-poll interval (seconds) into 3..60. Bad input -> 5.

    Server-side clamping is deliberate (the same contract as
    ``clamp_rolling_cadence``): the UI number input is a hint, never a trust
    boundary — a hand-crafted POST must not be able to set a sub-second poll
    against the broker's quote rate limit.
    """
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return _LIVE_POLL_FALLBACK_S
    return max(_LIVE_POLL_MIN_S, min(v, _LIVE_POLL_MAX_S))


def _live_poll_default() -> int:
    return clamp_live_poll_interval(os.getenv("OPEN15_LIVE_POLL_S", "5"))


def resolve_live_poll_interval() -> int:
    """DB config row wins; env seed is first-boot only (#484 rule)."""
    try:
        from database.open15_breakout_db import get_config

        cfg = get_config() or {}
        v = cfg.get("live_poll_interval_s")
        if v is not None:
            return clamp_live_poll_interval(v)
    except Exception:
        logger.exception("open15 pnl-curve: config read failed — env default")
    return _live_poll_default()


def _now_ist() -> dt.datetime:
    return dt.datetime.now(_IST)


def _today_ist() -> str:
    return _now_ist().strftime("%Y-%m-%d")


def _hhmm_to_min(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)
    except (AttributeError, ValueError):
        return None


def _min_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fetch_bars(symbol: str, trade_date: str, exchange: str = "NFO"):
    """Seam for tests; delegates to the option shadow's shared fetcher."""
    from services.open15_option_shadow import fetch_1m_bars

    return fetch_1m_bars(symbol, trade_date, exchange=exchange)


def _closes_by_minute(bars: list[dict] | None) -> dict[str, float]:
    """{'HH:MM' (IST): close} from broker 1m bars; unparseable bars skipped."""
    out: dict[str, float] = {}
    for b in bars or []:
        try:
            ts = dt.datetime.fromtimestamp(int(b["timestamp"]), tz=pytz.UTC).astimezone(_IST)
            close = b.get("close")
            if close is None:
                continue
            out[ts.strftime("%H:%M")] = float(close)
        except (KeyError, ValueError, TypeError, OSError):
            continue
    return out


def _row_contract(row) -> tuple[str, str]:
    """(symbol, exchange) whose price the trade's P&L moves with."""
    if (row.instrument or "stock") == "option" and row.opt_symbol:
        return row.opt_symbol, "NFO"
    return row.symbol, "NSE"


def _row_sign(row) -> int:
    """Direction of the P&L per unit of price move.

    Option rows are always LONG premium (buy CE for L, buy PE for S) -> +1.
    Stock rows follow the side: a short stock position gains when price falls.
    """
    if (row.instrument or "stock") == "option":
        return 1
    return 1 if row.side == "L" else -1


def _entry_basis(row) -> tuple[float | None, str]:
    """(price the MTM is measured against, its provenance label)."""
    if row.entry_fill_price:
        return float(row.entry_fill_price), "fill"
    if (row.instrument or "stock") == "option":
        if row.opt_entry_premium:
            return float(row.opt_entry_premium), "quote"
    elif row.trigger_price:
        return float(row.trigger_price), "quote"
    return None, "none"


def _exit_minute_of(row) -> int | None:
    """The exit minute (minutes since midnight IST) from ``exit_ts``."""
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


def _real_rows(date: str):
    """The day's REAL trade rows (open or closed), oldest first.

    ``status='error'`` rows carry no fill class by design (#643 — they must
    never join a P&L bucket) and are excluded; so is anything without a
    positive quantity (nothing was ever held).
    """
    from database.open15_breakout_db import _REAL_FILL, Open15Trade, db_session

    return (
        db_session.query(Open15Trade)
        .filter(
            Open15Trade.trade_date == date,
            _REAL_FILL,
            Open15Trade.status.in_(("open", "closed")),
            Open15Trade.quantity.isnot(None),
            Open15Trade.quantity > 0,
        )
        .order_by(Open15Trade.id.asc())
        .all()
    )


# completed-day curves are immutable — cache per date, in-process. Incomplete
# days (today, mid-hold) get a short TTL instead: the /logs page refreshes
# every 5 s and each rebuild is up to ``max_trades`` broker history calls, but
# the broker's intraday 1m history lags 5-15 min anyway — refetching faster
# than once a minute buys nothing and spends real rate-limit budget.
_CURVE_CACHE: dict[str, dict] = {}
_CURVE_TTL_CACHE: dict[str, tuple[float, dict]] = {}
_CURVE_TTL_S = 60.0
_CURVE_LOCK = threading.Lock()


def clear_caches() -> None:
    """Test hook + operational escape hatch."""
    with _CURVE_LOCK:
        _CURVE_CACHE.clear()
        _CURVE_TTL_CACHE.clear()
    with _LIVE_LOCK:
        _LIVE_CACHE.clear()


def invalidate_live_cache() -> None:
    """Drop the live-P&L cache so the next poll re-fetches (issue #696).

    Called by the risk monitor right after it exits a position — the cached
    payload still shows that trade as open, and both the chart and the next
    risk evaluation must not act on it.
    """
    with _LIVE_LOCK:
        _LIVE_CACHE.clear()


def build_pnl_curve(date: str) -> dict:
    """The day's intra-hold P&L curve payload (see module docstring)."""
    now_mono = dt.datetime.now(dt.UTC).timestamp()
    with _CURVE_LOCK:
        cached = _CURVE_CACHE.get(date)
        ttl_hit = _CURVE_TTL_CACHE.get(date)
    if cached is not None:
        return cached
    if ttl_hit and (now_mono - ttl_hit[0]) < _CURVE_TTL_S:
        return ttl_hit[1]

    from database.open15_breakout_db import db_session

    try:
        rows = _real_rows(date)
        trades = []
        all_closed = True
        for row in rows:
            trades.append(_trade_curve(row, date))
            if row.status != "closed":
                all_closed = False
        payload = _assemble(date, trades)
    except Exception:
        logger.exception("open15 pnl-curve: build failed for %s", date)
        return {"status": "error", "date": date, "message": "curve build failed — see logs"}
    finally:
        db_session.remove()

    # cache only once the day can no longer change: a past date, or today with
    # every row closed (the reconcile passes rewrite pnl in place, so give the
    # exit-time reconcile a wide margin before freezing today's payload)
    try:
        settled = False
        if trades and all_closed and not any(t.get("unavailable") for t in trades):
            is_past = date < _today_ist()
            settled_today = date == _today_ist() and _now_ist().strftime("%H:%M") >= "15:35"
            settled = is_past or settled_today
        with _CURVE_LOCK:
            if settled:
                _CURVE_CACHE[date] = payload
                _CURVE_TTL_CACHE.pop(date, None)
            else:
                _CURVE_TTL_CACHE[date] = (now_mono, payload)
    except Exception:
        logger.exception("open15 pnl-curve: cache decision failed for %s", date)

    return payload


def _trade_curve(row, date: str) -> dict:
    from database.open15_breakout_db import net_pnl_of_row

    contract, exchange = _row_contract(row)
    sign = _row_sign(row)
    basis, basis_src = _entry_basis(row)
    qty = int(row.entry_fill_qty or row.quantity or 0)
    trig_min = _hhmm_to_min(row.trigger_minute or "")
    exit_min = _exit_minute_of(row)

    out = {
        "id": row.id,
        "symbol": row.symbol,
        "side": row.side,
        "instrument": row.instrument or "stock",
        "contract": contract,
        "qty": qty,
        "entry_time": (
            f"{row.trigger_minute}:{int(row.trigger_second or 0):02d}"
            if row.trigger_minute
            else None
        ),
        "entry_minute": row.trigger_minute,
        "entry_second": int(row.trigger_second or 0),
        "entry_basis": basis,
        "basis": basis_src,
        "exit_minute": _min_to_hhmm(exit_min) if exit_min is not None else None,
        "status": row.status,
        "gross_pnl": row.pnl,
        "charges_inr": row.charges_inr,
        "net_pnl": net_pnl_of_row(row) if row.pnl is not None else None,
        "series": [],
        "final": None,
        "unavailable": False,
        "reason": None,
    }

    if basis is None or not qty or trig_min is None:
        out["unavailable"] = True
        out["reason"] = "no entry fill/quantity recorded"
        return out

    bars = _fetch_bars(contract, date, exchange=exchange)
    if bars is None:
        out["unavailable"] = True
        out["reason"] = "1m bars unavailable (no broker session, or contract expired)"
        return out
    closes = _closes_by_minute(bars)
    if not closes:
        out["unavailable"] = True
        out["reason"] = "broker returned no 1m bars for the contract"
        return out

    # marks: full minutes strictly between entry and exit; the bar labelled m
    # covers m:00-m:59, so the last full bar before an exit at E is E-1
    last_mark = (exit_min - 1) if exit_min is not None else (trig_min + 60)
    for m in range(trig_min + 1, last_mark + 1):
        hhmm = _min_to_hhmm(m)
        close = closes.get(hhmm)
        if close is None:
            continue
        out["series"].append([hhmm, round((close - basis) * qty * sign, 2)])

    # final point: the journal's own (fill-derived) gross P&L, never re-derived
    if row.status == "closed" and exit_min is not None and row.pnl is not None:
        out["final"] = [_min_to_hhmm(exit_min), round(float(row.pnl), 2)]
    return out


def _assemble(date: str, trades: list[dict]) -> dict:
    usable = [t for t in trades if not t["unavailable"]]
    # portfolio = per-minute sum over trades holding a mark at that minute
    minutes: dict[str, float] = {}
    for t in usable:
        for hhmm, v in t["series"]:
            minutes[hhmm] = round(minutes.get(hhmm, 0.0) + v, 2)
    portfolio = [[m, minutes[m]] for m in sorted(minutes)]

    finals = [t["final"] for t in usable if t["final"]]
    portfolio_final = None
    if finals and all(t["status"] == "closed" for t in usable):
        exit_hhmm = max(f[0] for f in finals)
        portfolio_final = [exit_hhmm, round(sum(f[1] for f in finals), 2)]

    gross = [t["gross_pnl"] for t in trades if t["gross_pnl"] is not None]
    charges = [t["charges_inr"] for t in trades if t["charges_inr"] is not None]
    nets = [t["net_pnl"] for t in trades if t["net_pnl"] is not None]
    return {
        "status": "ok",
        "date": date,
        "trades": trades,
        "portfolio": portfolio,
        "portfolio_final": portfolio_final,
        "gross_total": round(sum(gross), 2) if gross else None,
        "charges_total": round(sum(charges), 2) if charges else None,
        "net_total": round(sum(nets), 2) if nets else None,
        "n_real": len(trades),
        "n_unavailable": sum(1 for t in trades if t["unavailable"]),
    }


# ---------------------------------------------------------------------------
# live P&L — trades still open, ONE batched quote call, short server cache
# ---------------------------------------------------------------------------

_LIVE_CACHE: dict[str, tuple[float, dict]] = {}
_LIVE_LOCK = threading.Lock()


def _batched_ltp(contracts: list[tuple[str, str]]) -> dict[str, float] | None:
    """{contract symbol: ltp} via one ``get_multiquotes`` call; None on failure."""
    try:
        from database.auth_db import get_first_available_api_key
        from services.quotes_service import get_multiquotes

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning("open15 live-pnl: no API key / broker session")
            return None
        payload = [{"symbol": s, "exchange": ex} for s, ex in contracts]
        ok, data, _status = get_multiquotes(payload, api_key=api_key)
        if not ok:
            logger.warning("open15 live-pnl: batch quote failed: %s", data)
            return None
        out: dict[str, float] = {}
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict):
                continue
            d = r.get("data") or {}
            ltp = d.get("ltp")
            if ltp:
                out[r.get("symbol")] = float(ltp)
        return out
    except Exception:
        logger.exception("open15 live-pnl: batch quote raised")
        return None


def live_pnl() -> dict:
    """Live MTM of today's open real trades; ``status`` drives the page.

    ``live``   -> trades are open; payload carries per-trade + portfolio MTM.
    ``closed`` -> real rows exist today but none are open (curve is the record).
    ``idle``   -> no real rows today at all.

    Cached in-process for ~80% of the configured poll interval, so any number
    of open browser tabs still produce at most one broker call per interval.
    """
    from database.open15_breakout_db import db_session

    today = _today_ist()
    interval = resolve_live_poll_interval()
    ttl = max(2.0, interval * 0.8)
    now_mono = dt.datetime.now(dt.UTC).timestamp()

    with _LIVE_LOCK:
        hit = _LIVE_CACHE.get(today)
    if hit and (now_mono - hit[0]) < ttl:
        return hit[1]

    try:
        rows = _real_rows(today)
        open_rows = [r for r in rows if r.status == "open"]
        if not open_rows:
            payload = {
                "status": "closed" if rows else "idle",
                "date": today,
                "poll_interval_s": interval,
            }
            # cache the terminal answer briefly too — the page may poll from
            # several tabs, and "no open trades" need not hit the DB each time
            with _LIVE_LOCK:
                _LIVE_CACHE[today] = (now_mono, payload)
            return payload

        marks = _batched_ltp([_row_contract(r) for r in open_rows])
        trades = []
        total = 0.0
        for r in open_rows:
            contract, _ex = _row_contract(r)
            basis, basis_src = _entry_basis(r)
            qty = int(r.entry_fill_qty or r.quantity or 0)
            ltp = (marks or {}).get(contract)
            mtm = None
            if ltp is not None and basis is not None and qty:
                mtm = round((ltp - basis) * qty * _row_sign(r), 2)
                total += mtm
            trades.append(
                {
                    "symbol": r.symbol,
                    "side": r.side,
                    "contract": contract,
                    "qty": qty,
                    "entry_basis": basis,
                    "basis": basis_src,
                    "ltp": ltp,
                    "mtm": mtm,
                }
            )
        payload = {
            "status": "live",
            "date": today,
            "asof": _now_ist().strftime("%H:%M:%S"),
            "poll_interval_s": interval,
            "trades": trades,
            "portfolio_mtm": round(total, 2),
            "quotes_ok": marks is not None,
        }
        with _LIVE_LOCK:
            _LIVE_CACHE[today] = (now_mono, payload)
        return payload
    except Exception:
        logger.exception("open15 live-pnl: failed")
        return {"status": "error", "date": today, "poll_interval_s": interval}
    finally:
        db_session.remove()
