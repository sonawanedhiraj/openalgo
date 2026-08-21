"""ATM option shadow trade for open15_vol_breakout journal rows (issue #435).

Research-only measurement: for every CLOSED equity trade the strategy journals,
record what ONE LOT of the ATM option (CE for a long signal, PE for a short)
did over the same entry->exit window — real broker 1m premiums, never orders.
Together with the equity columns this builds the dataset for the R58 follow-up
question "does option buying beat stock on this signal?".

Conventions (locked with the operator, 2026-07-23):
  - Contract: strike nearest the equity trigger price, nearest NON-EXPIRED
    expiry, resolved from the master contract (``SymToken``).
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


def pick_contract(candidates: list[dict], spot: float, trade_date: str) -> dict | None:
    """Pure contract picker: nearest strike, then nearest non-expired expiry.

    ``candidates`` are dicts with ``symbol``/``strike``/``expiry`` (``DD-MMM-YY``)
    /``lotsize``. ``trade_date`` is ``YYYY-MM-DD``; a contract expiring ON the
    trade date is still alive intraday and allowed.
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
    nearest_expiry = min(exp for exp, _ in alive)
    same_expiry = [c for exp, c in alive if exp == nearest_expiry]
    return min(same_expiry, key=lambda c: abs(float(c["strike"]) - spot))


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


def _fetch_1m_bars(symbol: str, trade_date: str) -> list[dict] | None:
    """Fetch the contract's 1m bars for the trade date via the broker API."""
    try:
        from database.auth_db import get_first_available_api_key
        from services.history_service import get_history

        api_key = get_first_available_api_key()
        if not api_key:
            logger.warning("open15 opt-shadow: no API key / broker session — skipping")
            return None
        ok, data, _code = get_history(
            symbol=symbol,
            exchange="NFO",
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


def premiums_from_bars(
    bars: list[dict], trigger_minute: str, tz_name: str = "Asia/Kolkata"
) -> tuple[float | None, float | None]:
    """Extract (entry_premium, exit_premium) from 1m bars per the locked convention."""
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

    entry = None
    if nxt in by_minute:
        entry = by_minute[nxt].get("open")
    elif trig in by_minute:
        entry = by_minute[trig].get("close")

    exit_p = None
    if _EXIT_MINUTE in by_minute:
        exit_p = by_minute[_EXIT_MINUTE].get("open")
    else:
        before = [t for t in by_minute if t <= _EXIT_MINUTE]
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


if __name__ == "__main__":
    print(enrich_missing(max_rows=100))
