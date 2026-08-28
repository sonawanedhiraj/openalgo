"""Daily per-underlying, per-side option-liquidity score for ``open15_vol_breakout``.

``open15_vol_breakout`` buys the ATM CE/PE with **MARKET** orders and, until this
service, no liquidity input reached any decision it makes. The F&O universe is very
uneven in *option* liquidity — measured 2026-08-07, ATM call-side premium turnover ran
from Rs 0.39 Cr/day (BAJAJHLDNG, with 6 of its 12 nearest strikes trading nothing at
all) to Rs 679 Cr (SBIN). NSE/FAOP/40075 makes that concrete: an order that would
trade outside the execution range is **cancelled by the exchange**, so a thin book is
an execution risk, not a theoretical one.

**Phase 1 — this service only MEASURES.** It writes ``option_liquidity_daily`` and
nothing reads it. The gates that consume it ship separately, because the score needs
~``min_days`` sessions of history before it can be trusted (see the median note
below).

Why the broker API rather than the NSE bhavcopy
-----------------------------------------------
``get_multiquotes`` batches 500 instruments per call, so the whole ~2,500-contract
band sweep is ~5 calls / ~6 seconds, and it is already the house pattern (open15
itself calls it twice). Decisively, it returns **bid/ask**, which the UDiFF bhavcopy
does not carry at all — and since this strategy crosses the spread twice with MARKET
orders, the spread is the cost. The bhavcopy remains useful for *history* (it is
already loaded in ``historify.duckdb`` as ``fo_bhavcopy_eod``) and that is where the
backtest reads it; nothing here depends on NSE HTTP.

Four measurement decisions, each settled by data and each regression-tested
--------------------------------------------------------------------------
1. **Per SIDE, never blended.** On 20-day medians, blending CE and PE misclassifies
   **17 of 208** names — 8 thin only on calls, 9 only on puts. UNOMINDA is the clean
   case (CE p28 / PE p10): tradeable long, never short. A consumer reads the side it
   intends to trade.
2. **Premium turnover, not trade count.** An earlier draft ranked on a
   ``min(trades_pctile, turnover_pctile)`` composite and put MANAPPURAM in the bottom
   quintile — it sits mid-pack on turnover with a ~Rs 44,000 average ticket. Few
   large tickets is block flow, not illiquidity. Trade count is a diagnostic only,
   and the broker feed does not even expose it (``atm_trades`` is NULL on this path;
   it is populated only by a bhavcopy-sourced replay).
3. **6 strikes per side.** The picked strike is within 0.81% of the trigger price in
   every live journaled row, but it is ATM to the *post-gap* trigger, so a 1-strike
   band is unstable (20 of 42 names differ). The ranking is on a plateau from ~3/side
   outward; 6/side sits safely inside it.
4. **A 20-day median, not today's number.** Single-day scoring churns ~30 names a day
   (consecutive-day Jaccard 0.48 replayed over 2026-02..05); the median cuts that to
   3.4 (Jaccard 0.91). The median is load-bearing, not smoothing.

Units — the trap this codebase has been bitten by before (#555)
---------------------------------------------------------------
The broker quote's ``volume`` is in **UNITS** and is **cumulative for the day** (never
sum it across snapshots). The NSE bhavcopy's ``TtlTradgVol`` is in **LOTS**. Verified
against 12 journaled contracts: broker-units / lot came to 3.8%-58.9% of the NSE
full-day lot count in every case — always a fraction, never exceeding. So premium
turnover on this path is ``volume x ltp`` with **no lot multiply**, while every
*count* reported goes through ``open15_liquidity.lots()``.

CLI
---
``uv run python -m services.option_liquidity_service --dry-run``
``uv run python -m services.option_liquidity_service --reconcile-universe``
"""

from __future__ import annotations

import datetime as dt
import os
import statistics
import threading as _threading
from typing import Any

import pytz

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

_DEFAULT_TIME = "15:45"

#: Strikes per side around the money. See decision 3 in the module docstring.
DEFAULT_BAND_PER_SIDE = 6

#: Rolling window for the median, and the minimum days before a score is trusted.
DEFAULT_MEDIAN_DAYS = 20
DEFAULT_MIN_DAYS = 10

#: A band this dead is disqualifying regardless of turnover: if at least half the
#: strikes we might pick traded nothing all day, the book is not there.
_ZERO_VOL_FLOOR_FRAC = 0.5

#: If MORE than this fraction of the universe scores zero turnover, the sweep is
#: broken, not the market. See ``sweep_is_credible``.
DEFAULT_MAX_DEAD_FRAC = 0.5

#: Zerodha's own per-request instrument cap is 500 and the mapper batches internally;
#: this only bounds how much we hand it at once.
_SWEEP_CHUNK = 500


def _enabled() -> bool:
    return os.getenv("OPTION_LIQUIDITY_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _band_per_side() -> int:
    try:
        n = int(os.getenv("OPTION_LIQUIDITY_BAND_PER_SIDE", str(DEFAULT_BAND_PER_SIDE)))
    except ValueError:
        return DEFAULT_BAND_PER_SIDE
    return max(1, min(20, n))


def _median_days() -> int:
    try:
        n = int(os.getenv("OPTION_LIQUIDITY_MEDIAN_DAYS", str(DEFAULT_MEDIAN_DAYS)))
    except ValueError:
        return DEFAULT_MEDIAN_DAYS
    return max(1, min(120, n))


def _min_days() -> int:
    try:
        n = int(os.getenv("OPTION_LIQUIDITY_MIN_DAYS", str(DEFAULT_MIN_DAYS)))
    except ValueError:
        return DEFAULT_MIN_DAYS
    return max(1, min(120, n))


def _max_dead_frac() -> float:
    try:
        v = float(os.getenv("OPTION_LIQUIDITY_MAX_DEAD_FRAC", str(DEFAULT_MAX_DEAD_FRAC)))
    except ValueError:
        return DEFAULT_MAX_DEAD_FRAC
    return min(1.0, max(0.0, v))


def sweep_is_credible(rows: list[dict], max_dead_frac: float | None = None) -> tuple[bool, dict]:
    """Is this sweep a market reading, or a broken feed? ``(ok, stats)``.

    **The failure this exists to prevent.** Run against a closed market the broker
    returns LTP and OHLC quite happily but ``volume``, ``oi``, ``bid`` and ``ask`` all
    come back **0** (verified on a Sunday: every one of 416 rows scored zero turnover
    with 6/6 dead strikes). Scored naively that reads as *"the entire F&O universe is
    illiquid"* — and a consumer acting on it would exclude everything.

    The calendar gate in ``run_for_date`` catches the weekend case, but a calendar is
    not the general defence: a mid-session feed outage, an expired token accepted by a
    stale cache, or a broker-side incident all produce the same all-zero shape on a
    genuine trading day. So credibility is judged from the DATA, not from the date.

    A real market never has a majority of underlyings at zero ATM turnover — measured
    2026-08-07, zero of 208 did.
    """
    total = len(rows)
    if not total:
        return False, {"total": 0, "dead": 0, "dead_frac": 1.0}
    dead = sum(1 for r in rows if not r.get("atm_premium_turnover"))
    frac = dead / total
    limit = _max_dead_frac() if max_dead_frac is None else max_dead_frac
    return frac <= limit, {
        "total": total,
        "dead": dead,
        "dead_frac": round(frac, 3),
        "limit": limit,
    }


def _parse_hh_mm(raw: str, default: str = _DEFAULT_TIME) -> tuple[int, int]:
    try:
        hh, mm = raw.strip().split(":")
        return int(hh), int(mm)
    except Exception:
        dh, dm = default.split(":")
        return int(dh), int(dm)


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------


def load_equity_universe() -> set[str]:
    """The F&O stock names we care about — the same derivation open15 itself uses.

    ``SCANNER_SYMBOLS`` minus anything that resolves to an index exchange, mirroring
    ``Open15BreakoutService._load_universe``. Deliberately the same source: a score
    computed over a different universe would rank against a different denominator,
    and the percentile is a rank *within the universe*.
    """
    raw = os.getenv("SCANNER_SYMBOLS", "")
    syms = {s.strip() for s in raw.split(",") if s.strip()}
    if not syms:
        return set()
    try:
        from services.scanner_presubscribe import resolve_exchange_for_symbol

        return {s for s in syms if resolve_exchange_for_symbol(s) == "NSE"}
    except Exception:
        logger.exception("option_liquidity: exchange resolution failed — using raw SCANNER_SYMBOLS")
        return syms


def option_underlyings() -> set[str]:
    """Underlyings that actually have NFO option contracts, from the master contract.

    Refreshed daily from the broker instruments dump, so this tracks NSE's own
    additions and removals with no list to maintain.
    """
    try:
        from database.symbol import SymToken, db_session

        rows = (
            db_session.query(SymToken.name)
            .filter(SymToken.exchange == "NFO", SymToken.instrumenttype.in_(("CE", "PE")))
            .distinct()
            .all()
        )
        return {r[0] for r in rows if r[0]}
    except Exception:
        logger.exception("option_liquidity: master-contract underlying lookup failed")
        return set()
    finally:
        try:
            from database.symbol import db_session

            db_session.remove()
        except Exception:
            pass


def reconcile_universe() -> dict:
    """Diff ``SCANNER_SYMBOLS`` against the master contract's option underlyings.

    Reports both directions. **Never edits ``SCANNER_SYMBOLS``** — the scanner,
    sector_follow and the aggregator all read that variable, so rewriting it from here
    would silently change the universe of every one of them.

    ``missing_contracts`` is the urgent side: those names can still win a top-N slot
    and only fail after a trigger has fired. As of 2026-08-07 it held EXIDEIND,
    NUVAMA and SAMMAANCAP.
    """
    watched = load_equity_universe()
    have_options = option_underlyings()
    return {
        "watched": sorted(watched),
        "n_watched": len(watched),
        "n_option_underlyings": len(have_options),
        "missing_contracts": sorted(watched - have_options),
        "unwatched_with_options": sorted(have_options - watched),
    }


# ---------------------------------------------------------------------------
# band resolution
# ---------------------------------------------------------------------------


def _parse_expiry(raw: Any) -> dt.date | None:
    try:
        return dt.datetime.strptime(str(raw).title(), "%d-%b-%y").date()
    except (ValueError, TypeError):
        return None


def resolve_band(
    underlying: str, spot: float, trade_date: dt.date, per_side: int | None = None
) -> dict[str, list[dict]]:
    """``{"CE": [...], "PE": [...]}`` — the tradeable-month strikes nearest the money.

    Front month is ``min(expiry) >= trade_date``, matching
    ``open15_option_shadow.pick_contract`` so the score measures the book the strategy
    would actually hit — including that picker's expiry-week roll (issue #669):
    the block-window question is asked for the **next trading day after
    ``trade_date``**, because this EOD sweep's scores are consumed by the NEXT
    morning's arm and coverage ladder. Friday's sweep before a Tuesday expiry
    therefore prices the next month (Monday is broker-blocked), keeping the
    #591 ladder's lot costs on the contracts the strategy will actually buy.
    Fails OPEN to the plain front month when every alive expiry is blocked.
    Each side is resolved independently: the two legs can have
    different strike coverage, and forcing them onto a shared strike list would
    quietly change what is measured on the thinner side.
    """
    n = per_side or _band_per_side()
    out: dict[str, list[dict]] = {"CE": [], "PE": []}
    if not spot or spot <= 0:
        return out
    try:
        from database.symbol import SymToken, db_session

        rows = (
            db_session.query(SymToken)
            .filter(
                SymToken.exchange == "NFO",
                SymToken.name == underlying,
                SymToken.instrumenttype.in_(("CE", "PE")),
            )
            .all()
        )
        alive: list[tuple[dt.date, Any]] = []
        for r in rows:
            if not r.strike:
                continue
            exp = _parse_expiry(r.expiry)
            if exp and exp >= trade_date:
                alive.append((exp, r))
        if not alive:
            return out
        from services.open15_option_shadow import is_expiry_blocked, next_trading_day

        consumed_on = next_trading_day(trade_date)
        expiries = sorted({exp for exp, _ in alive})
        # skip expiries that are dead OR broker-blocked on the consumption day —
        # an expiry-day sweep must not hand tomorrow's ladder a dead contract
        front = next(
            (e for e in expiries if e >= consumed_on and not is_expiry_blocked(e, consumed_on)),
            None,
        )
        if front is None:
            front = expiries[0]
        for side in ("CE", "PE"):
            leg = [r for exp, r in alive if exp == front and r.instrumenttype == side]
            leg.sort(key=lambda r: abs(float(r.strike) - spot))
            out[side] = [
                {
                    "symbol": r.symbol,
                    "strike": float(r.strike),
                    "expiry": front,
                    "lotsize": r.lotsize,
                    "ticksize": r.tick_size,
                }
                for r in leg[:n]
            ]
        return out
    except Exception:
        logger.exception("option_liquidity: band resolution failed for %s", underlying)
        return out
    finally:
        try:
            from database.symbol import db_session

            db_session.remove()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# broker sweep
# ---------------------------------------------------------------------------


def sweep_quotes(payload: list[dict], api_key: str) -> dict[tuple[str, str], dict]:
    """Batched ``get_multiquotes`` -> ``{(symbol, exchange): data}``.

    Never raises. A failed batch logs a WARNING and contributes nothing, so a partial
    sweep degrades to fewer scored symbols rather than to wrong ones — and the
    percentile is recomputed over whatever universe genuinely came back.
    """
    from services.quotes_service import get_multiquotes

    out: dict[tuple[str, str], dict] = {}
    for i in range(0, len(payload), _SWEEP_CHUNK):
        chunk = payload[i : i + _SWEEP_CHUNK]
        try:
            success, resp, _status = get_multiquotes(chunk, api_key=api_key)
        except Exception:
            logger.exception(
                "option_liquidity: get_multiquotes raised on batch %d", i // _SWEEP_CHUNK
            )
            continue
        if not success:
            logger.warning(
                "option_liquidity: get_multiquotes failed on batch %d: %s",
                i // _SWEEP_CHUNK,
                (resp or {}).get("message", "unknown error"),
            )
            continue
        for item in (resp or {}).get("results") or []:
            sym, exch, data = item.get("symbol"), item.get("exchange"), item.get("data")
            if sym and exch and isinstance(data, dict):
                out[(sym, exch)] = data
    return out


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


def score_band(contracts: list[dict], quotes: dict[tuple[str, str], dict]) -> dict:
    """Metrics for ONE side's band. Pure over the quote payload.

    ``atm_premium_turnover`` is ``sum(volume x ltp)`` with **no lot multiply** — the
    broker's ``volume`` is already in units. Every *count* is reported in lots.
    """
    from services.open15_liquidity import lots, spread

    turnover = 0.0
    vol_units = 0.0
    oi_units = 0.0
    zero_vol = 0
    spreads: list[float] = []
    lot_size = None
    measured = 0

    for c in contracts:
        q = quotes.get((c["symbol"], "NFO"))
        if q is None:
            continue
        measured += 1
        lot_size = lot_size or c.get("lotsize")
        try:
            ltp = float(q.get("ltp") or 0)
            vol = float(q.get("volume") or 0)
            oi = float(q.get("oi") or 0)
        except (TypeError, ValueError):
            continue
        if vol <= 0:
            zero_vol += 1
        turnover += vol * ltp
        vol_units += vol
        oi_units += oi
        sp = spread(q.get("bid"), q.get("ask"), c.get("ticksize"))
        if sp["pct"] is not None:
            spreads.append(sp["pct"])

    # The ATM contract itself (issue #591): ``contracts`` arrive sorted nearest-
    # strike-first from resolve_band, so [0] is the money. Its LTP x lot size is
    # the capital one lot costs — the open15 coverage ladder's input. Recorded
    # only when the ATM contract's own quote came back with a positive LTP;
    # a zero/absent quote stays None rather than pretending the lot is free.
    atm_strike = atm_ltp = atm_lot_cost = None
    atm_lot_size = None
    if contracts:
        atm_q = quotes.get((contracts[0]["symbol"], "NFO")) or {}
        try:
            ltp0 = float(atm_q.get("ltp") or 0)
        except (TypeError, ValueError):
            ltp0 = 0.0
        lot0 = contracts[0].get("lotsize")
        if ltp0 > 0 and lot0:
            atm_strike = float(contracts[0]["strike"])
            atm_ltp = round(ltp0, 2)
            atm_lot_size = int(lot0)
            atm_lot_cost = round(ltp0 * int(lot0), 2)

    return {
        "atm_premium_turnover": round(turnover, 2) if measured else None,
        "atm_zero_vol_strikes": zero_vol if measured else None,
        "band_strikes": measured,
        "atm_strike": atm_strike,
        "atm_ltp": atm_ltp,
        "atm_lot_size": atm_lot_size,
        "atm_lot_cost_inr": atm_lot_cost,
        "atm_spread_pct": round(statistics.median(spreads), 4) if spreads else None,
        "atm_volume_lots": lots(vol_units, lot_size) if measured else None,
        "atm_oi_lots": lots(oi_units, lot_size) if measured else None,
        # the broker quote carries no trade count; only a bhavcopy-sourced replay
        # can populate these, so on this path they are honestly NULL rather than 0
        "atm_trades": None,
        "avg_ticket_inr": None,
        "expiry_used": contracts[0]["expiry"] if contracts else None,
    }


def assign_percentiles(scored: dict[tuple[str, str], dict]) -> None:
    """Stamp ``daily_pctile`` in place: rank within SIDE, within this day's universe.

    A band with at least ``_ZERO_VOL_FLOOR_FRAC`` of its strikes dead is forced to 0
    regardless of turnover — that is the hard tell, and it is what catches a name
    whose turnover looks acceptable only because one strike carried the whole day.

    A symbol with no measurable turnover is left at ``None``, not 0: "we could not
    measure it" and "it is the worst book in the universe" are different facts.

    Ranks use the **mid-rank** form ``100 x (i + 0.5) / n``, which is bounded to
    ``(0, 100)`` exclusive. That is deliberate: it reserves an exact **0.0 for the
    dead-band floor alone**, so a consumer (and the operator reading the UI) can tell
    "disqualified because half its strikes traded nothing" from "merely ranked last".
    A plain ``i / n`` gives the bottom name exactly 0.0 and the two become
    indistinguishable.
    """
    for side in ("CE", "PE"):
        keys = [
            k
            for k, v in scored.items()
            if k[1] == side and v.get("atm_premium_turnover") is not None
        ]
        if not keys:
            continue
        keys.sort(key=lambda k: scored[k]["atm_premium_turnover"])
        n = len(keys)
        for i, k in enumerate(keys):
            v = scored[k]
            pct = 100.0 * (i + 0.5) / n
            band = v.get("band_strikes") or 0
            dead = v.get("atm_zero_vol_strikes") or 0
            if band and dead >= max(1, round(band * _ZERO_VOL_FLOOR_FRAC)):
                pct = 0.0
            v["daily_pctile"] = round(pct, 2)


def apply_median(
    scored: dict[tuple[str, str], dict],
    history: dict[tuple[str, str], list[float]],
    min_days: int | None = None,
    median_days: int | None = None,
) -> None:
    """Stamp ``option_liquidity_pctile`` (the 20-day median) and ``n_days_in_median``.

    Below ``min_days`` the score is **NULL, not low**. A newly listed F&O stock
    genuinely has a thin book on day one, but its percentile is also unstable, and
    scoring it low would be indistinguishable from scoring it on no evidence. NSE
    itself does this — a newly listed security gets a provisional category and is only
    properly computed at the next monthly review.
    """
    md = median_days or _median_days()
    mn = min_days or _min_days()
    for k, v in scored.items():
        today = v.get("daily_pctile")
        series = list(history.get(k, []))[: md - 1]
        if today is not None:
            series = [today, *series]
        v["n_days_in_median"] = len(series)
        v["option_liquidity_pctile"] = (
            round(statistics.median(series), 2) if len(series) >= mn else None
        )


def compute_scores(trade_date: dt.date, api_key: str, per_side: int | None = None) -> list[dict]:
    """Full sweep -> per-(symbol, side) rows, ready to persist. Never raises.

    Two broker passes: the equity spots first (needed to locate the money), then the
    option band. ~1 + 5 batched calls for the whole universe.
    """
    universe = load_equity_universe()
    if not universe:
        logger.warning("option_liquidity: SCANNER_SYMBOLS is empty — nothing to score")
        return []

    spot_quotes = sweep_quotes(
        [{"symbol": s, "exchange": "NSE"} for s in sorted(universe)], api_key
    )
    spots: dict[str, float] = {}
    for s in universe:
        q = spot_quotes.get((s, "NSE")) or {}
        try:
            ltp = float(q.get("ltp") or 0)
        except (TypeError, ValueError):
            ltp = 0.0
        if ltp > 0:
            spots[s] = ltp
    logger.info("option_liquidity: %d/%d spots resolved", len(spots), len(universe))

    bands: dict[str, dict[str, list[dict]]] = {}
    payload: list[dict] = []
    for s, spot in spots.items():
        band = resolve_band(s, spot, trade_date, per_side)
        if not band["CE"] and not band["PE"]:
            continue
        bands[s] = band
        for side in ("CE", "PE"):
            payload.extend({"symbol": c["symbol"], "exchange": "NFO"} for c in band[side])
    logger.info(
        "option_liquidity: %d underlyings, %d option contracts to sweep", len(bands), len(payload)
    )
    if not payload:
        return []

    opt_quotes = sweep_quotes(payload, api_key)
    logger.info("option_liquidity: %d/%d option quotes returned", len(opt_quotes), len(payload))

    scored: dict[tuple[str, str], dict] = {}
    for s, band in bands.items():
        for side in ("CE", "PE"):
            if not band[side]:
                continue
            row = score_band(band[side], opt_quotes)
            row["symbol"] = s
            row["side"] = side
            scored[(s, side)] = row

    assign_percentiles(scored)
    try:
        from database.option_liquidity_db import get_daily_pctile_history

        history = get_daily_pctile_history(_median_days() - 1, trade_date)
    except Exception:
        logger.exception("option_liquidity: history read failed — today-only scores")
        history = {}
    apply_median(scored, history)
    return list(scored.values())


# ---------------------------------------------------------------------------
# job
# ---------------------------------------------------------------------------


def run_for_date(trade_date: dt.date | None = None, dry_run: bool = False) -> dict:
    """Sweep, score and persist one day. Returns a summary dict; never raises."""
    from services.data_freshness_service import is_trading_day

    try:
        from database.option_liquidity_db import init_db

        init_db()  # idempotent; the CLI may run before the app has ever booted
    except Exception:
        logger.exception("option_liquidity: table init failed")

    today = trade_date or dt.datetime.now().date()
    if not is_trading_day(today):
        logger.info("option_liquidity: %s is not a trading day — skipping", today)
        return {"status": "skipped_non_trading_day", "date": str(today), "rows": 0}

    try:
        from database.auth_db import get_first_available_api_key

        api_key = get_first_available_api_key()
    except Exception:
        logger.exception("option_liquidity: API key lookup failed")
        api_key = None
    if not api_key:
        # No session means no sweep, and that is fine: the score is a 20-day median,
        # so one missing day barely moves it. Writing a partial or stale row would be
        # worse than writing nothing.
        logger.warning("option_liquidity: no API key (broker session down?) — no write")
        return {"status": "skipped_no_session", "date": str(today), "rows": 0}

    rows = compute_scores(today, api_key)
    recon = reconcile_universe()
    if recon["missing_contracts"]:
        logger.warning(
            "option_liquidity: %d SCANNER_SYMBOLS names have NO NFO option contracts: %s",
            len(recon["missing_contracts"]),
            ", ".join(recon["missing_contracts"]),
        )
    if recon["unwatched_with_options"]:
        logger.info(
            "option_liquidity: %d option underlyings are not in SCANNER_SYMBOLS: %s",
            len(recon["unwatched_with_options"]),
            ", ".join(recon["unwatched_with_options"][:20]),
        )

    credible, stats = sweep_is_credible(rows)
    if not credible:
        # Writing this would record "the whole universe is illiquid", which is never
        # a market fact. Refuse the whole day rather than persist a plausible-looking
        # lie — a bad row would then sit in the 20-day median for four weeks.
        logger.error(
            "option_liquidity: sweep NOT credible (%d/%d underlying-sides with zero "
            "turnover, %.0f%% > %.0f%% limit) — writing NOTHING. Feed outage, dead "
            "token, or a closed market?",
            stats["dead"],
            stats["total"],
            stats["dead_frac"] * 100,
            stats["limit"] * 100,
        )
        try:
            from services.notification_service import get_notification_service

            get_notification_service().notify(
                "option_liquidity",
                f"option_liquidity sweep on {today} discarded: {stats['dead']}/"
                f"{stats['total']} underlying-sides had zero turnover. No score written.",
            )
        except Exception:
            logger.exception("option_liquidity: alert dispatch failed")
        return {
            "status": "discarded_not_credible",
            "date": str(today),
            "rows": len(rows),
            "written": 0,
            **stats,
        }

    written = 0
    if rows and not dry_run:
        from database.option_liquidity_db import upsert_scores

        written = upsert_scores(today, rows)

    ranked = [r for r in rows if r.get("option_liquidity_pctile") is not None]
    summary = {
        "status": "ok",
        "dead_frac": stats["dead_frac"],
        "date": str(today),
        "rows": len(rows),
        "written": written,
        "dry_run": dry_run,
        "n_ranked": len(ranked),
        "n_insufficient_history": len(rows) - len(ranked),
        "missing_contracts": recon["missing_contracts"],
        "unwatched_with_options": recon["unwatched_with_options"],
    }
    logger.info("option_liquidity: %s", summary)
    return summary


def _eod_job() -> None:
    """APScheduler entry point. Per-fire flag check, so a flip needs only a restart."""
    if not _enabled():
        logger.info("option_liquidity: OPTION_LIQUIDITY_ENABLED is false — skipping this fire")
        return
    try:
        run_for_date()
    except Exception:
        logger.exception("option_liquidity: EOD job failed")


# ---------------------------------------------------------------------------
# Missed-sweep convergence (issue #589)
# ---------------------------------------------------------------------------
#
# The 15:45 cron is the only writer, so an app that is down at 15:45 leaves a
# permanent hole in the score history — the gate then goes stale and fails
# open. The quote sweep stays valid ANY time after close the same day (the
# broker quote's ``volume`` is the day's cumulative until the next session
# opens), so a boot or periodic tick later the same evening can still recover
# the day. Mirrors the sector_follow / scanner backfill convergence pattern.
#
# The tick can NOT recover a fully-missed day the next morning: the quote
# volume has reset and ``sweep_is_credible`` would (correctly) refuse the
# all-zero sweep. That gap is what the trading-day-aware staleness in
# ``get_latest_scores`` absorbs.

_CONV_INTERVAL_SEC = 20 * 60
#: Let the 15:45 cron fire first — the convergence is a backstop, not a race.
_CONV_GRACE_MIN = 10

_conv_stop = _threading.Event()
_conv_thread: _threading.Thread | None = None


def _convergence_enabled() -> bool:
    return os.getenv("OPTION_LIQUIDITY_CONVERGENCE_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _convergence_tick(now: dt.datetime | None = None) -> bool:
    """Run the sweep iff today is a scoreable, unscored trading day past the
    sweep time. Returns True when a sweep was attempted. Never raises."""
    try:
        if not _enabled() or not _convergence_enabled():
            return False
        now = now or dt.datetime.now(_IST)
        today = now.date()
        from services.data_freshness_service import is_trading_day

        if not is_trading_day(today):
            return False
        hour, minute = _parse_hh_mm(os.environ.get("OPTION_LIQUIDITY_EOD_TIME", _DEFAULT_TIME))
        total = min(hour * 60 + minute + _CONV_GRACE_MIN, 23 * 60 + 59)
        earliest = dt.time(total // 60, total % 60)
        if now.time() < earliest:
            return False
        from database.option_liquidity_db import has_scores_for

        if has_scores_for(today):
            return False
        logger.warning(
            "option_liquidity: no scores for %s after the %02d:%02d sweep time — "
            "running catch-up sweep (issue #589)",
            today,
            hour,
            minute,
        )
        run_for_date(today)
        return True
    except Exception:
        logger.exception("option_liquidity: convergence tick failed")
        return False


def _convergence_loop() -> None:
    from services.thread_registry import beat as _beat

    logger.info("option_liquidity convergence loop started (every %ds)", _CONV_INTERVAL_SEC)
    while not _conv_stop.is_set():
        _beat("OptionLiquidityConvergence")
        _convergence_tick()
        _conv_stop.wait(_CONV_INTERVAL_SEC)
    logger.info("option_liquidity convergence loop stopped")


def start_convergence_loop() -> bool:
    """Start the catch-up daemon (idempotent). Returns True when started."""
    global _conv_thread
    if not _convergence_enabled():
        logger.info(
            "option_liquidity convergence disabled (OPTION_LIQUIDITY_CONVERGENCE_ENABLED!=true)"
        )
        return False
    if _conv_thread is not None and _conv_thread.is_alive():
        return False
    _conv_stop.clear()
    _conv_thread = _threading.Thread(
        target=_convergence_loop, daemon=True, name="OptionLiquidityConvergence"
    )
    _conv_thread.start()
    return True


def stop_convergence_loop() -> None:
    """Signal the loop to exit (tests / shutdown)."""
    _conv_stop.set()


def register_jobs(scheduler=None) -> None:
    """Register the daily sweep on the shared Historify scheduler.

    **15:45 IST is chosen so the broker session is still alive** — the sweep needs a
    live token, and Zerodha's expires overnight. Registration always happens; the
    per-fire ``OPTION_LIQUIDITY_ENABLED`` gate is what turns the work on and off.
    """
    sched = scheduler
    if sched is None:
        from services.historify_scheduler_service import get_historify_scheduler

        sched = get_historify_scheduler().scheduler

    from apscheduler.triggers.cron import CronTrigger

    hour, minute = _parse_hh_mm(os.environ.get("OPTION_LIQUIDITY_EOD_TIME", _DEFAULT_TIME))
    sched.add_job(
        _eod_job,
        trigger=CronTrigger(
            day_of_week="mon-fri", hour=hour, minute=minute, timezone="Asia/Kolkata"
        ),
        id="option_liquidity_eod",
        replace_existing=True,
        name=f"open15 option-liquidity sweep ({hour:02d}:{minute:02d} IST)",
    )
    logger.info("option_liquidity EOD job registered (%02d:%02d IST mon-fri)", hour, minute)


def init_option_liquidity_service(scheduler=None) -> None:
    """Boot entry point: ensure the table exists and register the job. No-op-safe."""
    try:
        from database.option_liquidity_db import init_db

        init_db()
    except Exception:
        logger.exception("option_liquidity: table init failed")
    register_jobs(scheduler)
    # Missed-sweep catch-up (issue #589): an evening boot after a 15:45 outage
    # recovers today's score instead of leaving a permanent hole.
    start_convergence_loop()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="open15 option-liquidity sweep")
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--dry-run", action="store_true", help="compute and print, do not write")
    ap.add_argument(
        "--reconcile-universe",
        action="store_true",
        help="diff SCANNER_SYMBOLS against the master contract and exit",
    )
    ap.add_argument("--top", type=int, default=15, help="rows to print per end of the ranking")
    args = ap.parse_args()

    if args.reconcile_universe:
        r = reconcile_universe()
        print(f"SCANNER_SYMBOLS (NSE-resolved): {r['n_watched']}")
        print(f"option underlyings in master contract: {r['n_option_underlyings']}")
        print(f"\nin SCANNER_SYMBOLS but NO option contracts ({len(r['missing_contracts'])}):")
        for s in r["missing_contracts"]:
            print(f"  {s}")
        print(f"\nhas options but NOT watched ({len(r['unwatched_with_options'])}):")
        for s in r["unwatched_with_options"]:
            print(f"  {s}")
        return

    day = dt.date.fromisoformat(args.date) if args.date else None
    if args.dry_run:
        # print the ranking itself, not just a count — the acceptance checks are
        # named symbols (BAJAJHLDNG at the bottom, MANAPPURAM mid-pack, FORTIS's
        # two sides far apart), and a summary line cannot show any of them
        from database.auth_db import get_first_available_api_key
        from database.option_liquidity_db import init_db
        from services.data_freshness_service import is_trading_day

        init_db()
        api_key = get_first_available_api_key()
        if not api_key:
            print("no API key — broker session down; cannot sweep")
            return
        when = day or dt.datetime.now().date()
        if not is_trading_day(when):
            # A live session is not the same as a live market. With the market shut
            # the broker still returns LTP and OHLC but zeroes volume/OI/bid/ask, so
            # every symbol scores dead and the table below is meaningless.
            print(
                f"\n*** {when} ({when:%A}) is NOT a trading day. The broker will "
                "return LTP but ZERO volume/OI/bid/ask, so every symbol will score "
                "dead. These numbers mean nothing — re-run on a trading day. ***"
            )
        rows = compute_scores(when, api_key)
        credible, stats = sweep_is_credible(rows)
        if not credible:
            print(
                f"\n*** SWEEP NOT CREDIBLE: {stats['dead']}/{stats['total']} "
                f"underlying-sides scored zero turnover ({stats['dead_frac']:.0%} > "
                f"{stats['limit']:.0%}). A real run would write NOTHING. ***"
            )
        ce = sorted(
            (r for r in rows if r["side"] == "CE" and r.get("daily_pctile") is not None),
            key=lambda r: r["daily_pctile"],
        )
        hdr = f"{'SYMBOL':<14}{'side':>5}{'pctile':>8}{'premium Rs':>13}{'dead':>6}{'spread%':>9}"
        print(f"\n{len(rows)} rows ({len(ce)} ranked CE)\n\n--- thinnest CE ---\n{hdr}")
        for r in ce[: args.top]:
            print(
                f"{r['symbol']:<14}{r['side']:>5}{r['daily_pctile']:>8.0f}"
                f"{(r['atm_premium_turnover'] or 0):>13,.0f}"
                f"{(r['atm_zero_vol_strikes'] or 0):>6}"
                f"{(r['atm_spread_pct'] or 0):>9.2f}"
            )
        print("\n--- deepest CE ---")
        for r in ce[-args.top :]:
            print(
                f"{r['symbol']:<14}{r['side']:>5}{r['daily_pctile']:>8.0f}"
                f"{(r['atm_premium_turnover'] or 0):>13,.0f}"
                f"{(r['atm_zero_vol_strikes'] or 0):>6}"
                f"{(r['atm_spread_pct'] or 0):>9.2f}"
            )
        by_key = {(r["symbol"], r["side"]): r for r in rows}
        print("\n--- acceptance checks ---")
        for sym in ("BAJAJHLDNG", "MANAPPURAM", "FORTIS", "MPHASIS", "SBIN"):
            c, p = by_key.get((sym, "CE")), by_key.get((sym, "PE"))
            if not c and not p:
                continue
            cp = c.get("daily_pctile") if c else None
            pp = p.get("daily_pctile") if p else None
            ct = (c or {}).get("atm_premium_turnover") or 0
            pt = (p or {}).get("atm_premium_turnover") or 0
            ratio = f"{ct / pt:.2f}x" if pt else "n/a"
            print(
                f"  {sym:<12} CE p{cp if cp is not None else '--':>4}  PE p"
                f"{pp if pp is not None else '--':>4}   CE/PE turnover {ratio}"
            )
        return
    summary = run_for_date(day, dry_run=False)
    print(summary)


if __name__ == "__main__":
    _main()
