"""ATM option shadow trade for open15_vol_breakout journal rows (issue #435).

Research-only measurement: for every CLOSED equity trade the strategy journals,
record what ONE LOT of the ATM option (CE for a long signal, PE for a short)
did over the same entry->exit window — real broker 1m premiums, never orders.
Together with the equity columns this builds the dataset for the R58 follow-up
question "does option buying beat stock on this signal?".

Conventions (locked with the operator, 2026-07-23):
  - Contract: strike nearest the equity trigger price, nearest NON-EXPIRED
    expiry, resolved from the master contract (``SymToken``). Since issue #669
    the pick also skips expiries inside Zerodha's physical-delivery block
    window (expiry day + the trading day before — fresh stock-option buys are
    broker-rejected there), rolling to the next month on those two days.
  - Entry premium = OPEN of the minute AFTER the trigger minute (a market
    order at second :59 fills on the next prints); fallback trigger-minute
    close. Exit premium = 09:30 bar OPEN (the flatten fires at 09:30:0x);
    fallback last bar at/before 09:30.
  - ``opt_pnl`` is NET of modelled option round-trip charges, for 1 lot.

Timing: enrichment runs at the 09:35 summary, but Zerodha's current-day 1m
history lags ~5-15 min, so rows that can't be priced yet are retried by the
next 09:10 arm (which calls :func:`enrich_missing`) — that same catch-up path
is the operator-approved backfill for pre-#435 rows whose contracts are alive.
Everything is fail-graceful: a row that cannot be priced is skipped with a log,
never raised into the scheduler.
"""

from __future__ import annotations

import datetime as dt
import os

from utils.logging import get_logger

logger = get_logger(__name__)

# broker current-day history lag tail — bars for 09:30 are reliably present a
# few minutes later; anything fetched EARLIER same-day may be legitimately empty
_EXIT_MINUTE = dt.time(9, 30)


def option_round_trip_charges(buy_premium_value: float, sell_premium_value: float) -> float | None:
    """Modelled Zerodha option round-trip charges in Rs for one lot.

    Brokerage flat Rs20 per executed leg, NSE txn 0.3503% of premium turnover,
    STT 0.0625% of the sell-side premium, SEBI Rs10/crore, stamp 0.003% of the
    buy leg, 18% GST on brokerage + exchange txn + SEBI.
    """
    if not buy_premium_value or not sell_premium_value:
        return None
    turnover = buy_premium_value + sell_premium_value
    brokerage = 40.0
    exch_txn = 0.003503 * turnover
    stt = 0.000625 * sell_premium_value
    sebi = 0.000001 * turnover
    stamp = 0.00003 * buy_premium_value
    gst = 0.18 * (brokerage + exch_txn + sebi)
    return round(brokerage + exch_txn + stt + sebi + stamp + gst, 2)


def _prev_trading_day(day: dt.date) -> dt.date:
    """The trading day immediately before ``day`` (holiday-aware, fail-open).

    Delegates to ``data_freshness_service.is_trading_day`` (weekday AND not an
    NSE holiday, itself fail-open to weekday-only per issue #253). If even that
    import raises, degrades to plain weekday-walking — this helper feeds a
    broker-mirroring guard and must never raise into a resolution path.
    """
    try:
        from services.data_freshness_service import is_trading_day
    except Exception:  # pragma: no cover - import failure is environmental

        def is_trading_day(d: dt.date) -> bool:
            return d.weekday() < 5

    d = day - dt.timedelta(days=1)
    for _ in range(14):
        try:
            if is_trading_day(d):
                return d
        except Exception:
            if d.weekday() < 5:
                return d
        d -= dt.timedelta(days=1)
    return day - dt.timedelta(days=1)


def next_trading_day(day: dt.date) -> dt.date:
    """The trading day immediately after ``day`` — same fail-open discipline
    as :func:`_prev_trading_day`. Used by the EOD option-liquidity sweep to ask
    the block-window question for the day its scores get CONSUMED (issue #669):
    the ~15:40 sweep on day D feeds day D+1's 09:10 arm and coverage ladder.
    """
    try:
        from services.data_freshness_service import is_trading_day
    except Exception:  # pragma: no cover - import failure is environmental

        def is_trading_day(d: dt.date) -> bool:
            return d.weekday() < 5

    d = day + dt.timedelta(days=1)
    for _ in range(14):
        try:
            if is_trading_day(d):
                return d
        except Exception:
            if d.weekday() < 5:
                return d
        d += dt.timedelta(days=1)
    return day + dt.timedelta(days=1)


def is_expiry_blocked(expiry: dt.date, trade_date: dt.date) -> bool:
    """Is a fresh long position in a STOCK option of this expiry broker-blocked
    on ``trade_date``?

    Zerodha blocks fresh buy orders in current-month stock options on the
    expiry day and the trading day before it ("Monday and Tuesday" of expiry
    week — expiry is Tuesday) because of compulsory physical delivery. Verified
    2026-08-24 against their physical-settlement policy page after all 4 live
    entries were rejected with exactly this reason (issue #669). Stock options
    only — index options are cash-settled and never blocked; open15's universe
    is all stocks, so every caller here is in scope.

    An already-expired contract is NOT this check's job (returns False) —
    aliveness is filtered separately, and conflating the two would let a stale
    candidate list read as "blocked" instead of "expired".
    """
    if trade_date > expiry:
        return False
    return trade_date == expiry or trade_date == _prev_trading_day(expiry)


def pick_contract(candidates: list[dict], spot: float, trade_date: str) -> dict | None:
    """Pure contract picker: nearest strike, then nearest non-expired AND
    non-broker-blocked expiry (issue #669).

    ``candidates`` are dicts with ``symbol``/``strike``/``expiry`` (``DD-MMM-YY``)
    /``lotsize``. ``trade_date`` is ``YYYY-MM-DD``; a contract expiring ON the
    trade date is still alive intraday and allowed — but Zerodha refuses fresh
    stock-option buys on the expiry day and the trading day before it
    (:func:`is_expiry_blocked`), so on those two days the pick rolls to the next
    expiry. The roll is stamped on the returned dict (``expiry_rolled`` /
    ``rolled_from``) so the entry event can say the contract is next-month.

    Fails OPEN: if every alive expiry is blocked (master contract carries no
    later month), the nearest alive one is returned un-rolled with a warning —
    a rejected attempt lands in the #548 paper path, which beats resolving
    nothing at all.
    """
    today = dt.date.fromisoformat(trade_date)
    alive = []
    for c in candidates:
        try:
            exp = dt.datetime.strptime(str(c["expiry"]).title(), "%d-%b-%y").date()
        except (ValueError, KeyError):
            continue
        if exp >= today:
            alive.append((exp, c))
    if not alive:
        return None
    expiries = sorted({exp for exp, _ in alive})
    chosen = next((exp for exp in expiries if not is_expiry_blocked(exp, today)), None)
    rolled_from: dt.date | None = None
    if chosen is None:
        chosen = expiries[0]
        logger.warning(
            "open15 pick_contract: every alive expiry (%s) is in the broker's "
            "physical-delivery block window on %s — failing open to %s",
            ", ".join(str(e) for e in expiries),
            trade_date,
            chosen,
        )
    elif chosen != expiries[0]:
        rolled_from = expiries[0]
    same_expiry = [c for exp, c in alive if exp == chosen]
    best = min(same_expiry, key=lambda c: abs(float(c["strike"]) - spot))
    if rolled_from is not None:
        return {**best, "expiry_rolled": True, "rolled_from": rolled_from.isoformat()}
    return best


def resolve_atm_option(underlying: str, side: str, spot: float, trade_date: str) -> dict | None:
    """Resolve the ATM CE (side L) / PE (side S) row from the master contract."""
    try:
        from database.symbol import SymToken, db_session

        opt_type = "CE" if side == "L" else "PE"
        rows = (
            db_session.query(SymToken)
            .filter(
                SymToken.exchange == "NFO",
                SymToken.name == underlying,
                SymToken.instrumenttype == opt_type,
            )
            .all()
        )
        candidates = [
            {
                "symbol": r.symbol,
                "strike": r.strike,
                "expiry": r.expiry,
                "lotsize": r.lotsize,
                # per-contract and NOT constant (0.05 and 0.01 both observed on
                # 2026-08-06) — a spread is only comparable across contracts once
                # expressed in ticks, so it has to travel with the contract
                "ticksize": r.tick_size,
            }
            for r in rows
            if r.strike
        ]
        return pick_contract(candidates, spot, trade_date)
    except Exception:
        logger.exception("open15 opt-shadow: master-contract lookup failed for %s", underlying)
        return None
    finally:
        try:
            from database.symbol import db_session

            db_session.remove()
        except Exception:
            pass


def _fetch_1m_bars(symbol: str, trade_date: str, exchange: str = "NFO") -> list[dict] | None:
    """Fetch the contract's 1m bars for the trade date via the broker API.

    Public alias ``fetch_1m_bars`` (issue #692): the P&L-curve service reads
    the exact same bars for the same contracts — one fetch path, not two.
    ``exchange`` defaults to NFO (every option consumer); stock-instrument
    curve rows pass ``NSE``.
    """
    try:
        from database.auth_db import get_first_available_api_key
        from services.history_service import get_history

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning("open15 opt-shadow: no API key / broker session — skipping")
            return None
        ok, data, _code = get_history(
            symbol=symbol,
            exchange=exchange,
            interval="1m",
            start_date=trade_date,
            end_date=trade_date,
            api_key=api_key,
        )
        if not ok:
            logger.warning("open15 opt-shadow: history fetch failed for %s: %s", symbol, data)
            return None
        return data.get("data") or []
    except Exception:
        logger.exception("open15 opt-shadow: history fetch raised for %s", symbol)
        return None


# public name for cross-module consumers (issue #692)
fetch_1m_bars = _fetch_1m_bars


def premiums_from_bars(
    bars: list[dict],
    trigger_minute: str,
    tz_name: str = "Asia/Kolkata",
    exit_minute: str | None = None,
) -> tuple[float | None, float | None]:
    """Extract (entry_premium, exit_premium) from 1m bars per the locked convention.

    ``exit_minute`` (``HH:MM``) is the day's configured exit time (issue #728);
    ``None`` keeps the historical 09:30 default.
    """
    import pytz

    tz = pytz.timezone(tz_name)
    by_minute: dict[dt.time, dict] = {}
    for b in bars:
        try:
            ts = dt.datetime.fromtimestamp(int(b["timestamp"]), tz=pytz.UTC).astimezone(tz)
        except (KeyError, ValueError, TypeError, OSError):
            continue
        by_minute[ts.time().replace(second=0, microsecond=0)] = b

    try:
        h, m = (int(x) for x in trigger_minute.split(":"))
    except (ValueError, AttributeError):
        return None, None
    trig = dt.time(h, m)
    nxt = (dt.datetime.combine(dt.date.today(), trig) + dt.timedelta(minutes=1)).time()
    exit_t = _EXIT_MINUTE
    if exit_minute:
        try:
            eh, em = (int(x) for x in exit_minute.split(":"))
            exit_t = dt.time(eh, em)
        except (ValueError, AttributeError):
            exit_t = _EXIT_MINUTE

    entry = None
    if nxt in by_minute:
        entry = by_minute[nxt].get("open")
    elif trig in by_minute:
        entry = by_minute[trig].get("close")

    exit_p = None
    if exit_t in by_minute:
        exit_p = by_minute[exit_t].get("open")
    else:
        before = [t for t in by_minute if t <= exit_t]
        if before:
            exit_p = by_minute[max(before)].get("close")
    return (
        float(entry) if entry is not None else None,
        float(exit_p) if exit_p is not None else None,
    )


def liquidity_path_from_bars(
    bars: list[dict], from_minute: str, to_minute: str = "09:30", tz_name: str = "Asia/Kolkata"
) -> list[dict]:
    """``[{"m": "09:21", "v": 3300, "oi": 72150}, ...]`` over the hold (issue #555).

    Built from bars this module ALREADY fetches — ``volume`` and ``oi`` are on
    every 1m bar the broker returns (the historical endpoint is called with
    ``oi=1``) and were simply being discarded here.

    ``v`` is each bar's OWN traded quantity, not the quote's cumulative day
    figure — the two must never be mixed, so the summariser sums this and would
    be wrong to sum that. The point of the series is direction: whether open
    interest was BUILDING or UNWINDING while the position was held, which two
    endpoint snapshots structurally cannot show.
    """
    import pytz

    tz = pytz.timezone(tz_name)
    try:
        fh, fm = (int(x) for x in from_minute.split(":"))
        th, tm = (int(x) for x in to_minute.split(":"))
    except (ValueError, AttributeError):
        return []
    lo, hi = dt.time(fh, fm), dt.time(th, tm)

    out: list[dict] = []
    for b in bars:
        try:
            ts = dt.datetime.fromtimestamp(int(b["timestamp"]), tz=pytz.UTC).astimezone(tz)
        except (KeyError, ValueError, TypeError, OSError):
            continue
        t = ts.time().replace(second=0, microsecond=0)
        if not (lo <= t <= hi):
            continue
        entry = {"m": t.strftime("%H:%M")}
        if b.get("volume") is not None:
            entry["v"] = int(b["volume"])
        if b.get("oi") is not None:
            entry["oi"] = int(b["oi"])
        out.append(entry)
    return out


def enrich_liquidity_paths(max_rows: int = 20) -> dict:
    """Fill ``opt_liquidity_path`` for rows that traded (or simulated) an option.

    Runs alongside :func:`enrich_missing` and for the same reason — the broker's
    current-day 1m history lags 5-15 min, so a row unpriced at 09:35 is retried
    by the next 09:10 arm. Unlike ``enrich_missing`` this covers option-MODE
    rows too (they carry real fills, not a shadow, but their contract's OI path
    is just as measurable). Idempotent and fail-graceful.
    """
    import json

    if os.getenv("OPEN15_LIQUIDITY_PATH_ENABLED", "true").lower() != "true":
        return {"status": "disabled", "priced": 0, "skipped": 0}

    from database.open15_breakout_db import Open15Trade, db_session, update_trade

    done = skipped = 0
    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(
                Open15Trade.opt_symbol.isnot(None),
                Open15Trade.opt_liquidity_path.is_(None),
                Open15Trade.trigger_minute.isnot(None),
                # only rows whose window has closed — a path fetched mid-hold
                # would be truncated and then never retried, because the column
                # would no longer be NULL
                Open15Trade.exit_ts.isnot(None),
            )
            .order_by(Open15Trade.id.desc())
            .limit(max_rows)
            .all()
        )
        work = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "opt_symbol": r.opt_symbol,
                "opt_lot_size": r.opt_lot_size,
                "trade_date": r.trade_date,
                "trigger_minute": r.trigger_minute,
                "exit_ts": r.exit_ts,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("open15 liquidity: journal scan failed")
        return {"status": "error", "priced": 0, "skipped": 0}
    finally:
        db_session.remove()

    details: list[dict] = []
    for w in work:
        bars = _fetch_1m_bars(w["opt_symbol"], w["trade_date"])
        if not bars:
            skipped += 1
            continue
        exit_min = "09:30"
        try:
            exit_min = dt.datetime.fromisoformat(w["exit_ts"]).strftime("%H:%M")
        except (ValueError, TypeError):
            pass
        path = liquidity_path_from_bars(bars, w["trigger_minute"], exit_min)
        if not path:
            skipped += 1
            continue
        if update_trade(w["id"], opt_liquidity_path=json.dumps(path)):
            done += 1
            from services.open15_liquidity import summarize_path

            details.append({"symbol": w["symbol"], **summarize_path(path, w["opt_lot_size"])})
        else:
            skipped += 1
    return {"status": "ok", "priced": done, "skipped": skipped, "rows": details}


def enrich_missing(max_rows: int = 20) -> dict:
    """Price the ATM option shadow for closed journal rows that lack it.

    Idempotent + fail-graceful; returns a small status dict. Called from the
    09:35 summary (today's rows), the 09:10 arm (catch-up for rows the broker
    lag left unpriced), and manually for the one-off backfill.
    """
    from sqlalchemy import or_

    from database.open15_breakout_db import Open15Trade, db_session, update_trade

    done, skipped = 0, 0
    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(
                Open15Trade.status == "closed",
                Open15Trade.opt_pnl.is_(None),
                Open15Trade.trigger_price.isnot(None),
                Open15Trade.trigger_minute.isnot(None),
                # option-MODE rows carry real fills, not a shadow (issue #437)
                or_(Open15Trade.instrument.is_(None), Open15Trade.instrument == "stock"),
            )
            .order_by(Open15Trade.id.desc())
            .limit(max_rows)
            .all()
        )
        work = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "side": r.side,
                "trigger_price": r.trigger_price,
                "trigger_minute": r.trigger_minute,
                "trade_date": r.trade_date,
            }
            for r in rows
        ]
    except Exception:
        logger.exception("open15 opt-shadow: journal scan failed")
        return {"status": "error", "priced": 0, "skipped": 0}
    finally:
        db_session.remove()

    for w in work:
        contract = resolve_atm_option(w["symbol"], w["side"], w["trigger_price"], w["trade_date"])
        if not contract:
            skipped += 1
            logger.info(
                "open15 opt-shadow: no alive contract for %s %s (expired?) — skipping",
                w["symbol"],
                w["trade_date"],
            )
            continue
        bars = _fetch_1m_bars(contract["symbol"], w["trade_date"])
        if not bars:
            skipped += 1
            continue
        entry, exit_p = premiums_from_bars(bars, w["trigger_minute"])
        if entry is None or exit_p is None:
            skipped += 1
            logger.info(
                "open15 opt-shadow: premiums not yet available for %s (%s) — will retry",
                contract["symbol"],
                w["trade_date"],
            )
            continue
        lot = int(contract["lotsize"])
        charges = option_round_trip_charges(entry * lot, exit_p * lot)
        gross = (exit_p - entry) * lot
        net = round(gross - (charges or 0.0), 2)
        ok = update_trade(
            w["id"],
            opt_symbol=contract["symbol"],
            opt_lot_size=lot,
            opt_entry_premium=entry,
            opt_exit_premium=exit_p,
            opt_charges_inr=charges,
            opt_pnl=net,
        )
        if ok:
            done += 1
            logger.info(
                "open15 opt-shadow: %s -> %s entry %.2f exit %.2f lot %d net %.2f",
                w["symbol"],
                contract["symbol"],
                entry,
                exit_p,
                lot,
                net,
            )
        else:
            skipped += 1
    return {"status": "ok", "priced": done, "skipped": skipped}


# --------------------------------------------------------------------------- #
# Watched-break counterfactual (issue #728)
# --------------------------------------------------------------------------- #
# A watch-list name that ended the entry window WITHOUT a trigger but DID break
# the 09:15 candle high (long) / low (short) is priced as if 1 lot of the ATM
# option had been bought at the first break — no volume gate — and sold at the
# scheduled exit. Numbers only: the row is ``fill='watched'``, ``pnl`` stays
# NULL, and the value lives in ``opt_pnl`` (the #435 shadow column, 1 lot, net).
#
# Conventions (locked, code constants): entry = OPEN of the minute AFTER the
# break minute (fallback break-minute close); exit = OPEN of the ``exit_time``
# bar (fallback last bar at/before it); MAE/MFE from bar lows/highs over
# [entry minute, exit minute); ``cf_source='bars'``. Bars carry no bid/ask, so
# every number is optimistic by ~the round-trip spread and is labelled so.

WATCHED_SOURCE = "bars"


def _minute_of(hhmmss: str | None) -> str | None:
    """``'09:19:42'`` -> ``'09:19'``; ``None``/garbage -> ``None``."""
    if not hhmmss or not isinstance(hhmmss, str):
        return None
    parts = hhmmss.split(":")
    if len(parts) < 2:
        return None
    try:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    except ValueError:
        return None


def marks_from_bars(
    bars: list[dict],
    entry_minute: str,
    exit_minute: str,
    entry_price: float,
    qty: int,
    sign: int = 1,
    tz_name: str = "Asia/Kolkata",
) -> dict:
    """Worst / best mark of a position over ``[entry_minute, exit_minute)``.

    ``sign`` +1 for a long (every option row), -1 for a short stock. Returns
    ``{"mae", "mae_minute", "mfe", "mfe_minute"}`` (rupees for ``qty``), or an
    empty dict when no bar falls in the window.
    """
    import pytz

    tz = pytz.timezone(tz_name)
    try:
        eh, em = (int(x) for x in entry_minute.split(":"))
        xh, xm = (int(x) for x in exit_minute.split(":"))
    except (ValueError, AttributeError):
        return {}
    lo, hi = dt.time(eh, em), dt.time(xh, xm)
    worst = best = None
    worst_m = best_m = None
    for b in bars:
        try:
            ts = dt.datetime.fromtimestamp(int(b["timestamp"]), tz=pytz.UTC).astimezone(tz)
            h_px, l_px = float(b["high"]), float(b["low"])
        except (KeyError, ValueError, TypeError, OSError):
            continue
        t = ts.time().replace(second=0, microsecond=0)
        if t < lo or t >= hi:
            continue
        # for a long the low is the worst mark; for a short the high is
        adverse = (l_px if sign > 0 else h_px) - entry_price
        favour = (h_px if sign > 0 else l_px) - entry_price
        mae_v = sign * adverse * qty
        mfe_v = sign * favour * qty
        if worst is None or mae_v < worst:
            worst, worst_m = mae_v, t.strftime("%H:%M")
        if best is None or mfe_v > best:
            best, best_m = mfe_v, t.strftime("%H:%M")
    if worst is None:
        return {}
    return {
        "mae": round(worst, 2),
        "mae_minute": worst_m,
        "mfe": round(best, 2),
        "mfe_minute": best_m,
    }


def _armed_of(events: list[dict]) -> dict:
    for ev in events or []:
        if ev.get("event") == "armed":
            return ev
    return {}


def watched_candidates(events: list[dict], breaks: dict | None = None) -> list[dict]:
    """Every ``no_entry`` symbol of a day, with its break (if any).

    ``breaks`` (``{symbol: {"break_at": "HH:MM:SS", "break_price": float}}``)
    overrides / supplies the break for days whose ``no_entry`` events predate
    the tick-thread capture (the backfill CLI rebuilds it from the tick logs).
    Returns dicts with ``symbol, side, watch_source, level_broken, break_at,
    break_price, gap_pct``.
    """
    gaps: dict[str, float] = {}
    out: list[dict] = []
    for ev in events or []:
        kind = ev.get("event")
        if kind == "selection":
            gaps.update(ev.get("gaps_pct") or {})
        elif kind == "watchlist_add" and ev.get("symbol"):
            gaps[ev["symbol"]] = ev.get("pct_change")
        elif kind == "no_entry" and ev.get("symbol"):
            sym = ev["symbol"]
            b = (breaks or {}).get(sym) or {}
            out.append(
                {
                    "symbol": sym,
                    "side": ev.get("side"),
                    "watch_source": ev.get("watch_source") or "seed",
                    "level_broken": bool(ev.get("level_broken")) or bool(b.get("break_at")),
                    "break_at": b.get("break_at") or ev.get("first_break_at"),
                    "break_price": b.get("break_price") or ev.get("first_break_price"),
                    "gap_pct": gaps.get(sym),
                }
            )
    return out


def insert_watched_row(
    trade_date: str,
    cand: dict,
    contract: dict | None,
    exit_minute: str,
    mode: str | None,
) -> int | None:
    """Journal ONE ``fill='watched'`` row (issues #728/#730) — the single
    writer for both the live quote-poll path and the bars pass, so the two
    cannot drift. ``cf_source`` is left NULL here: whichever pricer stamps
    the row sets it (``live`` / ``bars``). ``contract`` None journals a
    ``no_contract`` row so the day says so.
    """
    from database.open15_breakout_db import WATCHED_FILL, WATCHED_REASON, insert_trade

    kw = {
        "trade_date": trade_date,
        "symbol": cand["symbol"],
        "side": cand["side"],
        "mode": mode,
        "instrument": "option",
        "gap_pct": cand.get("gap_pct"),
        "break_at": cand["break_at"],
        "break_price": float(cand["break_price"]),
        "quantity": 0,
        "watch_source": cand.get("watch_source") or "seed",
        "status": "skipped",
        "reason": WATCHED_REASON if contract else "no_contract",
        "fill": WATCHED_FILL,
        "cf_exit_minute": exit_minute,
    }
    if contract:
        kw.update(
            opt_symbol=contract["symbol"],
            opt_lot_size=int(contract["lotsize"]),
            opt_tick_size=contract.get("ticksize"),
            sim_quantity=int(contract["lotsize"]),
        )
    return insert_trade(**kw)


def live_watched_fields(
    entry_premium: float,
    exit_premium: float,
    lot: int,
    path: list[tuple[str, float]],
) -> dict:
    """Journal fields for a watched row priced from the LIVE quote poll (#730).

    ``path`` is ``[(HH:MM:SS, ltp), ...]`` — every poll from the entry mark
    to the exit; MAE/MFE are the worst / best of those LTP marks for 1 lot,
    the same LTP convention the ghosts use (#704). Pure; no I/O.
    """
    gross = round((exit_premium - entry_premium) * lot, 2)
    charges = option_round_trip_charges(entry_premium * lot, exit_premium * lot)
    net = round(gross - (charges or 0.0), 2)
    worst = best = None
    worst_at = best_at = None
    for at, ltp in path or []:
        v = round((float(ltp) - entry_premium) * lot, 2)
        if worst is None or v < worst:
            worst, worst_at = v, at
        if best is None or v > best:
            best, best_at = v, at
    return {
        "opt_exit_premium": exit_premium,
        "opt_charges_inr": charges,
        "opt_pnl": net,
        "cf_mae": worst,
        "cf_mae_minute": _minute_of(worst_at) if worst_at else None,
        "cf_mfe": best,
        "cf_mfe_minute": _minute_of(best_at) if best_at else None,
        "cf_source": "live",
        "gross": gross,
        "charges": charges,
    }


def _price_watched(
    opt_symbol: str,
    lot: int,
    break_at: str,
    exit_minute: str,
    trade_date: str,
) -> dict | None:
    """Price ONE watched row from bars. ``None`` = bars not available yet."""
    bars = _fetch_1m_bars(opt_symbol, trade_date)
    if not bars:
        return None
    break_min = _minute_of(break_at)
    if not break_min:
        return None
    entry, exit_p = premiums_from_bars(bars, break_min, exit_minute=exit_minute)
    if entry is None or exit_p is None:
        return None
    charges = option_round_trip_charges(entry * lot, exit_p * lot)
    gross = round((exit_p - entry) * lot, 2)
    net = round(gross - (charges or 0.0), 2)
    # the entry is the open of the minute AFTER the break minute
    try:
        bh, bm = (int(x) for x in break_min.split(":"))
        entry_minute = (
            dt.datetime.combine(dt.date.today(), dt.time(bh, bm)) + dt.timedelta(minutes=1)
        ).strftime("%H:%M")
    except ValueError:
        entry_minute = break_min
    marks = marks_from_bars(bars, entry_minute, exit_minute, entry, lot, sign=1)
    return {
        "entry_premium": entry,
        "exit_premium": exit_p,
        "gross": gross,
        "charges": charges,
        "pnl": net,
        "mae": marks.get("mae"),
        "mae_minute": marks.get("mae_minute"),
        "mfe": marks.get("mfe"),
        "mfe_minute": marks.get("mfe_minute"),
        "entry_minute": entry_minute,
    }


def enrich_watched(
    trade_date: str,
    events: list[dict],
    breaks: dict | None = None,
    max_rows: int = 40,
) -> dict:
    """Journal + price the watched-break counterfactual for one day (issue #728).

    Since issue #730 this is the FALLBACK: the risk monitor prices most rows
    live at the exit (``cf_source='live'``); this pass inserts + prices only
    what the poll left unpriced (restart mid-window, no quote for the
    contract). A row with a live entry mark but no exit is re-priced here
    on bars for BOTH legs — one convention per row.

    Idempotent and fail-graceful. For every ``no_entry`` symbol that broke its
    level and has a break time: insert the ``fill='watched'`` journal row once
    (contract resolved at the break price), then price it from bars. A row the
    broker cannot serve bars for yet stays unpriced and is retried by
    :func:`enrich_watched_pending` (the next 09:10 arm's catch-up).

    Returns ``{"status", "watched", "no_break", "no_break_time", "no_contract",
    "priced", "pending", "already", "unsupported", "rows": [...]}`` where
    ``rows`` are the details of rows priced IN THIS CALL — what the service
    emits as ``watched_counterfactual`` events.
    """
    from database.open15_breakout_db import (
        WATCHED_FILL,
        WATCHED_REASON,
        Open15Trade,
        db_session,
        update_trade,
    )

    armed = _armed_of(events)
    exit_minute = armed.get("exit_time") or "09:30"
    instrument = armed.get("instrument") or "stock"
    mode = armed.get("mode")
    res = {
        "status": "ok",
        "watched": 0,
        "no_break": 0,
        "no_break_time": 0,
        "no_contract": 0,
        "priced": 0,
        "pending": 0,
        "already": 0,
        "unsupported": 0,
        "rows": [],
    }
    cands = watched_candidates(events, breaks)
    if not cands:
        return res
    if instrument != "atm_option":
        # the counterfactual is the strategy AS CONFIGURED minus one gate; a
        # stock-mode day has no option leg to price and is reported, not guessed
        res["unsupported"] = len(cands)
        return res

    # existing watched rows for the day — keyed by symbol
    try:
        existing = {
            r.symbol: {
                "id": r.id,
                "opt_symbol": r.opt_symbol,
                "opt_lot_size": r.opt_lot_size,
                "opt_pnl": r.opt_pnl,
                "break_at": r.break_at,
                "cf_exit_minute": r.cf_exit_minute,
                "reason": r.reason,
            }
            for r in db_session.query(Open15Trade)
            .filter(Open15Trade.trade_date == trade_date, Open15Trade.fill == WATCHED_FILL)
            .all()
        }
    except Exception:
        logger.exception("open15 watched: journal scan failed for %s", trade_date)
        return {**res, "status": "error"}
    finally:
        db_session.remove()

    work = 0
    for c in cands:
        if not c["level_broken"]:
            res["no_break"] += 1
            continue
        if not c.get("break_at") or not c.get("break_price"):
            res["no_break_time"] += 1
            continue
        res["watched"] += 1
        row = existing.get(c["symbol"])
        if row is None:
            contract = resolve_atm_option(
                c["symbol"], c["side"], float(c["break_price"]), trade_date
            )
            rid = insert_watched_row(trade_date, c, contract, exit_minute, mode)
            if not contract:
                res["no_contract"] += 1
                logger.info(
                    "open15 watched: no alive contract for %s %s — journaled unpriced",
                    c["symbol"],
                    trade_date,
                )
                continue
            row = {
                "id": rid,
                "opt_symbol": contract["symbol"],
                "opt_lot_size": int(contract["lotsize"]),
                "opt_pnl": None,
                "break_at": c["break_at"],
                "cf_exit_minute": exit_minute,
                "reason": WATCHED_REASON,
            }
        if row.get("opt_pnl") is not None:
            res["already"] += 1
            continue
        if not row.get("opt_symbol"):
            res["no_contract"] += 1
            continue
        if work >= max_rows:
            res["pending"] += 1
            continue
        work += 1
        priced = _price_watched(
            row["opt_symbol"],
            int(row["opt_lot_size"] or 1),
            row.get("break_at") or c["break_at"],
            row.get("cf_exit_minute") or exit_minute,
            trade_date,
        )
        if priced is None:
            res["pending"] += 1
            logger.info(
                "open15 watched: bars not available yet for %s (%s) — will retry",
                row["opt_symbol"],
                trade_date,
            )
            continue
        ok = update_trade(
            row["id"],
            opt_entry_premium=priced["entry_premium"],
            opt_exit_premium=priced["exit_premium"],
            opt_charges_inr=priced["charges"],
            opt_pnl=priced["pnl"],
            cf_mae=priced["mae"],
            cf_mae_minute=priced["mae_minute"],
            cf_mfe=priced["mfe"],
            cf_mfe_minute=priced["mfe_minute"],
            cf_source=WATCHED_SOURCE,
            cf_exit_minute=row.get("cf_exit_minute") or exit_minute,
        )
        if not ok:
            res["pending"] += 1
            continue
        res["priced"] += 1
        res["rows"].append(
            {
                "symbol": c["symbol"],
                "side": c["side"],
                "watch_source": c["watch_source"],
                "break_at": row.get("break_at") or c["break_at"],
                "break_price": float(c["break_price"]),
                "contract": row["opt_symbol"],
                "lot_size": int(row["opt_lot_size"] or 1),
                "entry_minute": priced["entry_minute"],
                "entry_premium": priced["entry_premium"],
                "exit_minute": row.get("cf_exit_minute") or exit_minute,
                "exit_premium": priced["exit_premium"],
                "gross": priced["gross"],
                "charges": priced["charges"],
                "pnl": priced["pnl"],
                "mae": priced["mae"],
                "mfe": priced["mfe"],
                "source": WATCHED_SOURCE,
                "status": "priced",
                "note": "never triggered — priced as if 1 lot bought at the 09:15 break, "
                "no volume gate; bars, not money",
            }
        )
    return res


def enrich_watched_pending(max_rows: int = 40) -> dict:
    """Catch-up: price any ``fill='watched'`` row still lacking ``opt_pnl``.

    Called from the next 09:10 arm (the broker's same-day 1m lag) and the
    backfill CLI. Reads everything it needs from the row itself, so it needs
    no day log. Idempotent.
    """
    from database.open15_breakout_db import WATCHED_FILL, Open15Trade, db_session, update_trade

    try:
        rows = (
            db_session.query(Open15Trade)
            .filter(
                Open15Trade.fill == WATCHED_FILL,
                Open15Trade.opt_pnl.is_(None),
                Open15Trade.opt_symbol.isnot(None),
                Open15Trade.break_at.isnot(None),
            )
            .order_by(Open15Trade.id.desc())
            .limit(max_rows)
            .all()
        )
        work = [
            {
                "id": r.id,
                "symbol": r.symbol,
                "trade_date": r.trade_date,
                "opt_symbol": r.opt_symbol,
                "lot": int(r.opt_lot_size or 1),
                "break_at": r.break_at,
                "exit_minute": r.cf_exit_minute or "09:30",
            }
            for r in rows
        ]
    except Exception:
        logger.exception("open15 watched: pending scan failed")
        return {"status": "error", "priced": 0, "pending": 0}
    finally:
        db_session.remove()

    priced = pending = 0
    for w in work:
        p = _price_watched(
            w["opt_symbol"], w["lot"], w["break_at"], w["exit_minute"], w["trade_date"]
        )
        if p is None:
            pending += 1
            continue
        ok = update_trade(
            w["id"],
            opt_entry_premium=p["entry_premium"],
            opt_exit_premium=p["exit_premium"],
            opt_charges_inr=p["charges"],
            opt_pnl=p["pnl"],
            cf_mae=p["mae"],
            cf_mae_minute=p["mae_minute"],
            cf_mfe=p["mfe"],
            cf_mfe_minute=p["mfe_minute"],
            cf_source=WATCHED_SOURCE,
        )
        if ok:
            priced += 1
            logger.info(
                "open15 watched catch-up: %s %s -> %s net %.2f",
                w["trade_date"],
                w["symbol"],
                w["opt_symbol"],
                p["pnl"],
            )
        else:
            pending += 1
    return {"status": "ok", "priced": priced, "pending": pending}


if __name__ == "__main__":
    print(enrich_missing(max_rows=100))
