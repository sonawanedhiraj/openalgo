"""cas_320_expiry_straddle — expiry-day ATM straddle across the closing auction (issue #740).

On weekly index-option expiry days (NIFTY on NFO, SENSEX on BFO — the day comes
from the master contract, never from ``weekday()``) the strategy:

* 15:12 IST ``arm``  — checks trading day + expiry day per underlying, snapshots
  the UI config, resolves the ATM ±2 strike ladder from the spot, starts the
  monitor thread, schedules the hard exit at the configured time.
* 15:14–15:41 monitor thread ``cas-straddle-monitor`` — ONE batched quote call
  per poll for spot + ladder (both underlyings, whether or not they trade),
  journals every poll, fixes the ATM from the 15:15:00 spot print (the last
  continuous print), tracks the indicative-index path, and evaluates the
  target rule on the same batch.
* 15:20:00 ``run_entry`` — BUY the ATM CE and PE (``NRML``, ``lots × lotsize``)
  for every underlying whose UI toggle is on, refusing (never trimming) when
  the premium exceeds ``max_premium_inr``. An ACK is not a fill: each leg is
  verified through the order book; a rejected leg becomes ``fill='paper'``.
* target — combined BID value of the open legs, net of modelled exit charges,
  ≥ ``target_mult`` × entry cost for ``CONFIRM_POLLS`` consecutive polls → SELL.
* ``hard_exit_time`` (default 15:28:00) — SELL any leg with a bid; a leg with
  no bid is left to cash-settle at zero (``expired_worthless``).
* 15:38 ``fallback_flatten`` (PROTECTED) — SELL anything the position book
  still affirmatively holds; an unreadable book still sends.
* 15:45 ``eod_summary`` — session digest per underlying + settlement
  counterfactual per leg + Telegram.

Mode is exactly like open15: ``sandbox`` or ``live``, decided per order by
``resolve_order_mode(STRATEGY_NAME)`` (the /strategies toggle). There is no env
flag, no observe state, no code switch — the only controls are that toggle, the
UI config row (``cas_straddle_config``) and pause/resume.

Every external effect is injected with a production default so the decision
logic is unit-testable with no broker, no DB and no clock.
"""

from __future__ import annotations

import math
import threading
import time as _time
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}  # fmt: skip

STRATEGY_NAME = "cas_320_expiry_straddle"
_STRATEGY_DIR = Path(__file__).resolve().parents[1] / "strategies" / STRATEGY_NAME

UNDERLYINGS: dict[str, dict[str, str]] = {
    "NIFTY": {"spot_exchange": "NSE_INDEX", "option_exchange": "NFO"},
    "SENSEX": {"spot_exchange": "BSE_INDEX", "option_exchange": "BFO"},
}

# Code defaults for the UI config row (NULL field → these). No env seeds.
DEFAULTS: dict[str, Any] = {
    "trade_nifty": True,
    "trade_sensex": True,
    "lots_nifty": 1,
    "lots_sensex": 1,
    "max_premium_inr": 15_000.0,
    "target_mult": 2.0,
    "hard_exit_time": "15:28:00",
    "poll_interval_s": 2,
}
LOTS_MIN, LOTS_MAX = 1, 10
TARGET_MIN, TARGET_MAX = 1.1, 5.0
POLL_MIN_S, POLL_MAX_S = 2, 60
PREMIUM_MIN_INR, PREMIUM_MAX_INR = 1_000.0, 1_000_000.0
# The hard exit must sit after the entry and before the 15:38 fallback flatten.
HARD_EXIT_MIN = time(15, 20, 30)
HARD_EXIT_MAX = time(15, 37, 0)

# Fixed schedule (code constants, not knobs). Derivatives trade to 15:40.
RESET_TIME = time(9, 0)
ARM_TIME = time(15, 12)
WINDOW_START = time(15, 14)
CONTINUOUS_CLOSE = time(15, 15)  # cash segment continuous trading ends
ENTRY_TIME = time(15, 20, 0)
FALLBACK_TIME = time(15, 38)
WINDOW_END = time(15, 41)
SUMMARY_TIME = time(15, 45)
LADDER_STEPS = 2  # ATM ±2 strikes, both sides, are polled
CONFIRM_POLLS = 2  # #716 lesson: never act on one print
LIMIT_BUFFER_TICKS = 5  # live marketable LIMIT: ask + n ticks / bid − n ticks
FILL_VERIFY_ATTEMPTS = 3
FILL_VERIFY_DELAY_S = 1.5
NOTIFY_EVENT = "cas_straddle"

_TERMINAL_UNFILLED = {"rejected", "cancelled", "canceled", "expired"}
_FILLED = {"complete", "completed", "filled", "executed", "traded"}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def parse_hms(value: str | None) -> time | None:
    """``"HH:MM"`` / ``"HH:MM:SS"`` → ``time``; None on bad input."""
    if not value:
        return None
    parts = str(value).strip().split(":")
    try:
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        s = int(parts[2]) if len(parts) > 2 else 0
        return time(h, m, s)
    except (ValueError, IndexError):
        return None


def _hms(t: time) -> str:
    return t.strftime("%H:%M:%S")


def clamp_lots(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULTS["lots_nifty"]
    return max(LOTS_MIN, min(n, LOTS_MAX))


def clamp_target_mult(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULTS["target_mult"]
    if math.isnan(f):
        return DEFAULTS["target_mult"]
    return max(TARGET_MIN, min(f, TARGET_MAX))


def clamp_poll_interval(v) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return DEFAULTS["poll_interval_s"]
    return max(POLL_MIN_S, min(n, POLL_MAX_S))


def clamp_premium(v) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return DEFAULTS["max_premium_inr"]
    if math.isnan(f):
        return DEFAULTS["max_premium_inr"]
    return max(PREMIUM_MIN_INR, min(f, PREMIUM_MAX_INR))


def clamp_hard_exit_time(v) -> str:
    t = parse_hms(v)
    if t is None:
        return DEFAULTS["hard_exit_time"]
    if t < HARD_EXIT_MIN:
        t = HARD_EXIT_MIN
    if t > HARD_EXIT_MAX:
        t = HARD_EXIT_MAX
    return _hms(t)


def resolve_config(row: dict | None) -> dict:
    """Effective config: DB row (NULL → code default), every field clamped."""
    row = row or {}

    def pick(key):
        v = row.get(key)
        return DEFAULTS[key] if v is None else v

    return {
        "trade_nifty": bool(pick("trade_nifty")),
        "trade_sensex": bool(pick("trade_sensex")),
        "lots_nifty": clamp_lots(pick("lots_nifty")),
        "lots_sensex": clamp_lots(pick("lots_sensex")),
        "max_premium_inr": clamp_premium(pick("max_premium_inr")),
        "target_mult": clamp_target_mult(pick("target_mult")),
        "hard_exit_time": clamp_hard_exit_time(pick("hard_exit_time")),
        "poll_interval_s": clamp_poll_interval(pick("poll_interval_s")),
    }


def validate_config(body: dict) -> tuple[dict, list[str]]:
    """Validate a POSTed config body. Returns ``(values_to_store, errors)``.

    Only keys present in ``body`` are considered; an explicit ``null`` clears
    the override (falls back to the code default). Numbers are bounds-checked
    rather than silently clamped so the UI can say what was refused.
    """
    values: dict = {}
    errors: list[str] = []
    for key in ("trade_nifty", "trade_sensex"):
        if key in body:
            v = body[key]
            if v is None:
                values[key] = None
            elif isinstance(v, bool):
                values[key] = v
            elif isinstance(v, (int, float)) and v in (0, 1):
                values[key] = bool(v)
            elif isinstance(v, str) and v.lower() in ("true", "false", "0", "1"):
                values[key] = v.lower() in ("true", "1")
            else:
                errors.append(f"{key} must be true or false")
    for key in ("lots_nifty", "lots_sensex"):
        if key in body:
            v = body[key]
            if v is None:
                values[key] = None
            else:
                try:
                    n = int(float(v))
                    if not (LOTS_MIN <= n <= LOTS_MAX):
                        errors.append(f"{key} must be between {LOTS_MIN} and {LOTS_MAX}")
                    else:
                        values[key] = n
                except (TypeError, ValueError):
                    errors.append(f"{key} must be a whole number")
    if "max_premium_inr" in body:
        v = body["max_premium_inr"]
        if v is None:
            values["max_premium_inr"] = None
        else:
            try:
                f = float(v)
                if not (PREMIUM_MIN_INR <= f <= PREMIUM_MAX_INR):
                    errors.append(
                        f"max_premium_inr must be between {PREMIUM_MIN_INR:.0f} "
                        f"and {PREMIUM_MAX_INR:.0f}"
                    )
                else:
                    values["max_premium_inr"] = f
            except (TypeError, ValueError):
                errors.append("max_premium_inr must be a number")
    if "target_mult" in body:
        v = body["target_mult"]
        if v is None:
            values["target_mult"] = None
        else:
            try:
                f = float(v)
                if not (TARGET_MIN <= f <= TARGET_MAX):
                    errors.append(f"target_mult must be between {TARGET_MIN} and {TARGET_MAX}")
                else:
                    values["target_mult"] = f
            except (TypeError, ValueError):
                errors.append("target_mult must be a number")
    if "hard_exit_time" in body:
        v = body["hard_exit_time"]
        if v is None:
            values["hard_exit_time"] = None
        else:
            t = parse_hms(v)
            if t is None:
                errors.append("hard_exit_time must be HH:MM or HH:MM:SS")
            elif not (HARD_EXIT_MIN <= t <= HARD_EXIT_MAX):
                errors.append(
                    f"hard_exit_time must be between {_hms(HARD_EXIT_MIN)} and {_hms(HARD_EXIT_MAX)}"
                )
            else:
                values["hard_exit_time"] = _hms(t)
    if "poll_interval_s" in body:
        v = body["poll_interval_s"]
        if v is None:
            values["poll_interval_s"] = None
        else:
            try:
                n = int(float(v))
                if not (POLL_MIN_S <= n <= POLL_MAX_S):
                    errors.append(f"poll_interval_s must be between {POLL_MIN_S} and {POLL_MAX_S}")
                else:
                    values["poll_interval_s"] = n
            except (TypeError, ValueError):
                errors.append("poll_interval_s must be a whole number")
    return values, errors


def parse_expiry(expiry: str | None) -> date | None:
    """``DD-MMM-YY`` / ``DD-MMM-YYYY`` / ``DDMMMYY`` → date; None when unparseable."""
    if not expiry:
        return None
    s = str(expiry).strip().upper().replace("/", "-")
    if "-" in s:
        parts = s.split("-")
        if len(parts) != 3:
            return None
        d, mon, y = parts
    else:
        if len(s) < 7:
            return None
        d, mon, y = s[:2], s[2:5], s[5:]
    try:
        m = _MONTHS.get(mon[:3])
        if m is None:
            return None
        yr = int(y)
        if yr < 100:
            yr += 2000
        return date(yr, m, int(d))
    except ValueError:
        return None


def expiry_to_ddmmmyy(expiry: str) -> str:
    """``28-OCT-25`` → ``28OCT25`` (what the strike/symbol helpers expect)."""
    d = parse_expiry(expiry)
    if d is None:
        return str(expiry).replace("-", "").upper()
    return f"{d.day:02d}{d.strftime('%b').upper()}{d.year % 100:02d}"


def expiry_to_dashed(expiry: str) -> str:
    d = parse_expiry(expiry)
    if d is None:
        return str(expiry).upper()
    return f"{d.day:02d}-{d.strftime('%b').upper()}-{d.year % 100:02d}"


def nearest_expiry(expiries: list[str], today: date) -> str | None:
    """Earliest listed expiry on/after ``today`` (``DD-MMM-YY`` strings in)."""
    dated = [(parse_expiry(e), e) for e in expiries or []]
    dated = [(d, e) for d, e in dated if d is not None and d >= today]
    if not dated:
        return None
    dated.sort(key=lambda t: t[0])
    return dated[0][1]


def is_expiry_today(expiries: list[str], today: date) -> tuple[bool, str | None]:
    """``(expiry_is_today, nearest_expiry)`` from the master-contract expiry list."""
    nxt = nearest_expiry(expiries, today)
    if nxt is None:
        return False, None
    return parse_expiry(nxt) == today, nxt


def atm_from_strikes(spot: float, strikes: list[float]) -> float | None:
    if not strikes or spot is None:
        return None
    return float(min(strikes, key=lambda k: abs(float(k) - float(spot))))


def ladder_strikes(atm: float, strikes: list[float], steps: int = LADDER_STEPS) -> list[float]:
    """The ``steps`` strikes on either side of ``atm`` that actually exist."""
    if atm is None or not strikes:
        return []
    ss = sorted(float(k) for k in strikes)
    if atm not in ss:
        return [atm]
    i = ss.index(atm)
    lo = max(0, i - steps)
    hi = min(len(ss), i + steps + 1)
    return ss[lo:hi]


def round_to_tick(px: float, tick: float | None) -> float:
    if not tick or tick <= 0:
        return round(px, 2)
    return round(round(px / tick) * tick, 2)


def option_leg_charges(
    buy_value: float, sell_value: float, exchange: str = "NFO", orders: int = 2
) -> float:
    """Modelled charges for one long option leg, Zerodha schedule as published
    2026-09-19 (zerodha.com/charges): ₹20/order, STT 0.15% of sell-side
    premium, exchange txn NSE 0.03553% / BSE 0.0325% of premium both sides,
    SEBI ₹10/crore, stamp 0.003% buy side, GST 18% on brokerage + txn + SEBI.

    ``orders`` is 1 for a leg left to expire worthless (only the BUY was sent).
    """
    buy_value = float(buy_value or 0.0)
    sell_value = float(sell_value or 0.0)
    brokerage = 20.0 * max(1, int(orders))
    txn_rate = 0.000325 if str(exchange).upper() == "BFO" else 0.0003553
    exch_txn = txn_rate * (buy_value + sell_value)
    stt = 0.0015 * sell_value
    sebi = 0.000001 * (buy_value + sell_value)
    stamp = 0.00003 * buy_value
    gst = 0.18 * (brokerage + exch_txn + sebi)
    return round(brokerage + exch_txn + stt + sebi + stamp + gst, 2)


def exercise_stt(intrinsic_value: float) -> float:
    """STT on a long option exercised at expiry: 0.15% of the intrinsic value."""
    return round(0.0015 * max(0.0, float(intrinsic_value or 0.0)), 2)


def combined_target_reached(legs: list[dict], target_mult: float) -> tuple[bool, dict]:
    """The exit rule on one poll.

    ``legs`` carry ``entry_price``, ``quantity``, ``bid`` (None when missing),
    ``exchange``. Returns ``(reached, detail)`` where ``detail`` has the cost,
    the combined bid value, the modelled charges and the net proceeds. A leg
    without a bid contributes 0 (it cannot be sold at this poll), which only
    makes the rule harder to satisfy — fail closed.
    """
    cost = 0.0
    combined = 0.0
    charges = 0.0
    for leg in legs:
        qty = int(leg.get("quantity") or 0)
        entry = float(leg.get("entry_price") or 0.0)
        bid = leg.get("bid")
        cost += entry * qty
        if bid:
            combined += float(bid) * qty
            charges += option_leg_charges(entry * qty, float(bid) * qty, leg.get("exchange", "NFO"))
    net = combined - charges
    reached = cost > 0 and net >= float(target_mult) * cost
    return reached, {
        "cost": round(cost, 2),
        "combined_bid": round(combined, 2),
        "charges_est": round(charges, 2),
        "net_value": round(net, 2),
        "target_value": round(float(target_mult) * cost, 2),
    }


def intrinsic_at(settle: float | None, strike: float, side: str) -> float | None:
    if settle is None:
        return None
    if side == "CE":
        return max(0.0, float(settle) - float(strike))
    return max(0.0, float(strike) - float(settle))


def session_digest(samples: list[dict], atm: float | None, target_mult: float) -> dict:
    """Pure digest of one underlying's poll samples for the session row.

    Each sample: ``{t: time(IST), spot, ce_bid, ce_ask, pe_bid, pe_ask}`` with
    the CE/PE fields for the ATM contracts (None before the ATM is fixed).
    """
    out: dict = {"n_polls": len(samples)}
    if not samples:
        return out
    spots = [s for s in samples if s.get("spot")]
    pre = [s for s in spots if s["t"] < CONTINUOUS_CLOSE]
    if pre:
        out["close_1515"] = pre[-1]["spot"]
    else:
        at_or_after = [s for s in spots if s["t"] >= CONTINUOUS_CLOSE]
        out["close_1515"] = at_or_after[0]["spot"] if at_or_after else None
    post = [s for s in spots if s["t"] >= ENTRY_TIME]
    if post:
        out["first_print"] = post[0]["spot"]
        out["first_print_at"] = _hms(post[0]["t"])
        lo = min(post, key=lambda s: s["spot"])
        hi = max(post, key=lambda s: s["spot"])
        out["iiv_low"], out["iiv_low_at"] = lo["spot"], _hms(lo["t"])
        out["iiv_high"], out["iiv_high_at"] = hi["spot"], _hms(hi["t"])
        out["iiv_close"] = post[-1]["spot"]
    out["settle"] = spots[-1]["spot"]
    if atm is not None and out.get("settle") is not None:
        out["settle_intrinsic"] = abs(float(out["settle"]) - float(atm))

    def _combined(s, key):
        a, b = s.get(f"ce_{key}"), s.get(f"pe_{key}")
        if a is None or b is None:
            return None
        return float(a) + float(b)

    entry_polls = [s for s in samples if s["t"] >= ENTRY_TIME and _combined(s, "ask") is not None]
    if entry_polls:
        e = entry_polls[0]
        ask0, bid0 = _combined(e, "ask"), _combined(e, "bid")
        out["straddle_ask_1520"] = ask0
        out["straddle_bid_1520"] = bid0
        if ask0 and bid0:
            mid = (ask0 + bid0) / 2.0
            out["spread_pct_1520"] = round(100.0 * (ask0 - bid0) / mid, 3) if mid else None
        bids = [(s["t"], _combined(s, "bid")) for s in entry_polls if _combined(s, "bid")]
        if bids:
            t, v = max(bids, key=lambda x: x[1])
            out["max_combined_bid"], out["max_combined_bid_at"] = v, _hms(t)
            if ask0:
                hit = [t for t, v in bids if v >= float(target_mult) * ask0]
                out["t_first_target"] = _hms(hit[0]) if hit else None
        for label, cutoff in (
            ("combined_bid_1525", time(15, 25)),
            ("combined_bid_1528", time(15, 28)),
            ("combined_bid_1530", time(15, 30)),
            ("combined_bid_1535", time(15, 35)),
        ):
            upto = [v for t, v in bids if t <= cutoff]
            out[label] = upto[-1] if upto else None
    return out


# --------------------------------------------------------------------------- #
# Production providers (injected defaults; lazily imported)
# --------------------------------------------------------------------------- #
def _api_key() -> str | None:
    try:
        from database.auth_db import get_first_available_api_key

        return get_first_available_api_key()
    except Exception:
        logger.debug("cas_straddle: api key lookup failed", exc_info=True)
        return None


def production_expiry_lister(underlying: str, option_exchange: str) -> list[str]:
    """Non-expired option expiries (``DD-MMM-YY``, sorted) from the master contract."""
    from services.expiry_service import get_expiry_dates

    ok, resp, _ = get_expiry_dates(underlying, option_exchange, "options")
    if not ok:
        logger.warning("cas_straddle: expiry list failed for %s: %s", underlying, resp)
        return []
    return list((resp or {}).get("data") or [])


def production_strike_lister(
    underlying: str, expiry: str, side: str, option_exchange: str
) -> list[float]:
    from services.option_symbol_service import get_available_strikes

    return [
        float(k)
        for k in get_available_strikes(underlying, expiry_to_ddmmmyy(expiry), side, option_exchange)
    ]


def production_contract_finder(
    underlying: str, expiry: str, strike: float, side: str, option_exchange: str
) -> dict | None:
    """``{symbol, exchange, strike, expiry, lotsize, tick_size}`` or None."""
    from services.option_symbol_service import construct_option_symbol, find_option_in_database

    sym = construct_option_symbol(underlying, expiry_to_ddmmmyy(expiry), strike, side)
    row = find_option_in_database(sym, option_exchange)
    if not row:
        return None
    return {
        "symbol": row["symbol"],
        "exchange": row["exchange"],
        "strike": float(row.get("strike") or strike),
        "expiry": expiry_to_dashed(str(row.get("expiry") or expiry)),
        "lotsize": int(row.get("lotsize") or 0),
        "tick_size": float(row.get("tick_size") or 0.05),
        "side": side,
    }


def production_quote_batch(instruments: list[tuple[str, str]]) -> dict[str, dict] | None:
    """``{symbol: {ltp, bid, ask, volume, oi}}`` via ONE ``get_multiquotes`` call."""
    try:
        from services.quotes_service import get_multiquotes

        api_key = _api_key()
        if not api_key:
            logger.warning("cas_straddle: no API key / broker session for quotes")
            return None
        payload = [{"symbol": s, "exchange": ex} for s, ex in instruments]
        ok, data, _ = get_multiquotes(payload, api_key=api_key)
        if not ok:
            logger.warning("cas_straddle: batch quote failed: %s", data)
            return None

        def _px(v):
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        def _n(v):
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return None

        out: dict[str, dict] = {}
        for r in (data or {}).get("results") or []:
            if not isinstance(r, dict) or r.get("error"):
                continue
            d = r.get("data") or {}
            out[r.get("symbol")] = {
                "ltp": _px(d.get("ltp")),
                "bid": _px(d.get("bid")),
                "ask": _px(d.get("ask")),
                "volume": _n(d.get("volume")),
                "oi": _n(d.get("oi")),
            }
        return out
    except Exception:
        logger.exception("cas_straddle: batch quote raised")
        return None


def production_order_placer(order: dict) -> dict:
    """Route one leg through ``place_order`` with the strategy's own mode key."""
    from services.place_order_service import place_order

    api_key = _api_key()
    if not api_key:
        return {"status": "error", "message": "no api key available"}
    payload = {
        "apikey": api_key,
        "strategy": STRATEGY_NAME,
        "symbol": order["symbol"],
        "exchange": order["exchange"],
        "action": order["action"],
        "product": order.get("product", "NRML"),
        "pricetype": order.get("pricetype", "MARKET"),
        "quantity": str(order["quantity"]),
    }
    if payload["pricetype"] == "LIMIT":
        payload["price"] = str(order.get("price"))
    success, response, _ = place_order(payload, api_key=api_key, mode_key=STRATEGY_NAME)
    response = dict(response or {})
    response.setdefault("status", "success" if success else "error")
    return response


def production_fill_reader(order_id: str) -> dict | None:
    """``{status: filled|rejected|pending, price, qty, message}`` for one order.

    ``average_price`` is the only field that means "what was transacted" and
    ``filled_quantity`` is read by presence (#626). Routing is automatic —
    ``get_order_status`` answers a sandbox id from ``sandbox.db``.
    """
    try:
        from services.orderstatus_service import get_order_status

        api_key = _api_key()
        if not api_key:
            return None
        ok, resp, _ = get_order_status(
            {"strategy": STRATEGY_NAME, "orderid": str(order_id)}, api_key=api_key
        )
        if not ok:
            return None
        data = (resp or {}).get("data") or {}
        status = str(data.get("order_status") or data.get("status") or "").lower()
        avg = data.get("average_price")
        try:
            avg = float(avg) if avg is not None else None
        except (TypeError, ValueError):
            avg = None
        fq = data.get("filled_quantity")
        if fq is None:
            fq = data.get("quantity")
        try:
            fq = int(float(fq)) if fq is not None else None
        except (TypeError, ValueError):
            fq = None
        msg = str(data.get("status_message") or data.get("message") or "").strip() or None
        if status in _TERMINAL_UNFILLED:
            return {"status": "rejected", "price": None, "qty": 0, "message": msg}
        if (status in _FILLED or (avg and avg > 0)) and avg and avg > 0:
            return {"status": "filled", "price": avg, "qty": fq, "message": msg}
        return {"status": "pending", "price": None, "qty": None, "message": msg}
    except Exception:
        logger.exception("cas_straddle: order status raised for %s", order_id)
        return None


def production_book_reader() -> list[dict] | None:
    """Open positions from the book the strategy's OWN mode resolves to
    (``mode_key=STRATEGY_NAME``, the #497 rule); None when unreadable."""
    try:
        from services.positionbook_service import get_positionbook

        api_key = _api_key()
        if not api_key:
            return None
        ok, resp, _ = get_positionbook(api_key=api_key, mode_key=STRATEGY_NAME)
        if not ok:
            return None
        rows = (resp or {}).get("data") or []
        out = []
        for p in rows:
            try:
                out.append(
                    {
                        "symbol": p.get("symbol"),
                        "exchange": p.get("exchange"),
                        "product": p.get("product"),
                        "quantity": int(float(p.get("quantity") or 0)),
                    }
                )
            except (TypeError, ValueError):
                continue
        return out
    except Exception:
        logger.exception("cas_straddle: position book raised")
        return None


def production_mode_resolver() -> str:
    from services.mode_service import resolve_order_mode

    return resolve_order_mode(STRATEGY_NAME).value


def production_broker_session_checker() -> bool:
    return bool(_api_key())


def production_notifier(message: str) -> None:
    try:
        from services.notification_service import get_notification_service

        get_notification_service().notify(NOTIFY_EVENT, message)
    except Exception:
        logger.debug("cas_straddle: notify failed", exc_info=True)


def production_config_reader() -> dict | None:
    from database.cas_straddle_db import get_config

    return get_config()


def production_trading_day_checker(d: date) -> bool:
    from services.data_freshness_service import is_trading_day

    return is_trading_day(d)


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #
class CasStraddleService:
    """Expiry-day ATM straddle evaluator + scheduler glue (see module docstring)."""

    def __init__(
        self,
        app=None,
        scheduler=None,
        *,
        expiry_lister: Callable[[str, str], list[str]] | None = None,
        strike_lister: Callable[[str, str, str, str], list[float]] | None = None,
        contract_finder: Callable[..., dict | None] | None = None,
        quote_batch: Callable[[list[tuple[str, str]]], dict | None] | None = None,
        order_placer: Callable[[dict], dict] | None = None,
        fill_reader: Callable[[str], dict | None] | None = None,
        book_reader: Callable[[], list[dict] | None] | None = None,
        mode_resolver: Callable[[], str] | None = None,
        broker_session_checker: Callable[[], bool] | None = None,
        notifier: Callable[[str], None] | None = None,
        config_reader: Callable[[], dict | None] | None = None,
        trading_day_checker: Callable[[date], bool] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        journal: bool = True,
    ):
        self.app = app
        self.scheduler = scheduler
        self._expiry_lister = expiry_lister or production_expiry_lister
        self._strike_lister = strike_lister or production_strike_lister
        self._contract_finder = contract_finder or production_contract_finder
        self._quote_batch = quote_batch or production_quote_batch
        self._order_placer = order_placer or production_order_placer
        self._fill_reader = fill_reader or production_fill_reader
        self._book_reader = book_reader or production_book_reader
        self._mode_resolver = mode_resolver or production_mode_resolver
        self._broker_session_checker = broker_session_checker or production_broker_session_checker
        self._notify = notifier or production_notifier
        self._config_reader = config_reader or production_config_reader
        self._trading_day = trading_day_checker or production_trading_day_checker
        self._now = now or (lambda: datetime.now(_IST))
        self._sleep = sleep or _time.sleep
        self._journal_enabled = journal

        self._lock = threading.RLock()
        self._monitor_thread: threading.Thread | None = None
        self.manual_pause = False
        self.day: dict[str, Any] = self._empty_day()

    # ----- day state ------------------------------------------------------ #
    def _empty_day(self) -> dict:
        return {"trade_date": None, "armed": False, "config": None, "u": {}, "armed_at": None}

    def _empty_underlying(self, u: str) -> dict:
        meta = UNDERLYINGS[u]
        return {
            "underlying": u,
            "spot_exchange": meta["spot_exchange"],
            "option_exchange": meta["option_exchange"],
            "trading_day": None,
            "expiry_day": False,
            "expiry": None,
            "nearest_expiry": None,
            "trade": False,
            "lots": 0,
            "lotsize": None,
            "tick_size": None,
            "strikes": [],
            "atm": None,
            "atm_fixed": False,  # set from the 15:15:00 print
            "close_1515": None,
            "ladder": {},  # symbol -> contract dict
            "legs": {},  # side -> leg dict
            "entered": False,
            "entry_skipped": None,
            "exit_done": False,
            "target_polls": 0,
            "last_eval": None,
            "samples": [],
            "digest": None,
            "errors": [],
        }

    def trade_date(self) -> str:
        return self._now().date().isoformat()

    def effective_config(self) -> dict:
        try:
            return resolve_config(self._config_reader())
        except Exception:
            logger.exception("cas_straddle: config read failed — code defaults")
            return resolve_config(None)

    def current_mode(self) -> str:
        try:
            return self._mode_resolver()
        except Exception:
            logger.exception("cas_straddle: mode resolve failed — sandbox")
            return "sandbox"

    # ----- runtime override (pause / resume) ------------------------------ #
    def _entry_held_by_override(self) -> bool:
        try:
            from database.strategy_runtime_override_db import is_entry_blocked

            blocked, ov = is_entry_blocked(STRATEGY_NAME)
            if blocked and ov:
                logger.info(
                    "cas_straddle entry held by %s (reason=%s, expires=%s)",
                    ov.get("override_type"),
                    ov.get("reason"),
                    ov.get("expires_at"),
                )
            return blocked
        except Exception:
            logger.debug(
                "cas_straddle runtime-override resolve failed; not blocking", exc_info=True
            )
            return False

    def _utc_naive(self, dt_ist: datetime) -> datetime:
        return dt_ist.astimezone(UTC).replace(tzinfo=None)

    def pause(self) -> dict:
        self.manual_pause = True
        try:
            from database.strategy_runtime_override_db import set_override

            expires = self._now().replace(hour=23, minute=59, second=0, microsecond=0)
            set_override(
                STRATEGY_NAME,
                "pause",
                self._utc_naive(expires),
                reason="operator manual pause",
                set_by="cas_straddle",
            )
        except Exception:
            logger.exception("cas_straddle: failed to write pause override")
        logger.warning("cas_straddle MANUALLY PAUSED — new entries halted (exits still run)")
        return {"status": "success", "manual_pause": True}

    def resume(self) -> dict:
        self.manual_pause = False
        try:
            from database.strategy_runtime_override_db import clear_override

            clear_override(STRATEGY_NAME)
        except Exception:
            logger.exception("cas_straddle: failed to clear overrides")
        logger.info("cas_straddle RESUMED")
        return {"status": "success", "manual_pause": False}

    # ----- 09:00 reset ---------------------------------------------------- #
    def run_daily_reset(self) -> None:
        with self._lock:
            self.day = self._empty_day()
        logger.info("cas_straddle daily reset")

    # ----- 15:12 arm ------------------------------------------------------ #
    def arm(self) -> dict:
        """Prepare the day: expiry check, config snapshot, ladder, monitor."""
        now = self._now()
        today = now.date()
        tds = today.isoformat()
        cfg = self.effective_config()
        with self._lock:
            if self.day.get("trade_date") != tds:
                self.day = self._empty_day()
            self.day["trade_date"] = tds
            self.day["config"] = cfg
            self.day["armed_at"] = now.isoformat()
        try:
            trading = bool(self._trading_day(today))
        except Exception:
            logger.exception("cas_straddle: trading-day check failed — assuming trading day")
            trading = today.weekday() < 5
        session_ok = True
        try:
            session_ok = bool(self._broker_session_checker())
        except Exception:
            session_ok = False
        summary: dict[str, dict] = {}
        any_expiry = False
        for u in UNDERLYINGS:
            st = self._empty_underlying(u)
            st["trading_day"] = trading
            st["trade"] = bool(cfg["trade_nifty" if u == "NIFTY" else "trade_sensex"])
            st["lots"] = int(cfg["lots_nifty" if u == "NIFTY" else "lots_sensex"])
            if trading:
                try:
                    expiries = self._expiry_lister(u, st["option_exchange"])
                except Exception:
                    logger.exception("cas_straddle: expiry list raised for %s", u)
                    expiries = []
                is_today, nxt = is_expiry_today(expiries, today)
                st["expiry_day"], st["nearest_expiry"] = is_today, nxt
                st["expiry"] = nxt if is_today else None
            with self._lock:
                self.day["u"][u] = st
            if not (trading and st["expiry_day"]):
                summary[u] = {"expiry_day": False, "nearest_expiry": st["nearest_expiry"]}
                continue
            any_expiry = True
            if not session_ok:
                st["errors"].append("no broker session at arm")
                summary[u] = {"expiry_day": True, "error": "no broker session"}
                continue
            self._build_ladder(u)
            summary[u] = {
                "expiry_day": True,
                "expiry": st["expiry"],
                "atm": st["atm"],
                "ladder": sorted(st["ladder"].keys()),
                "trade": st["trade"],
                "lots": st["lots"],
                "lotsize": st["lotsize"],
            }
            self._upsert_session(
                u,
                exchange=st["option_exchange"],
                expiry=st["expiry"],
                atm_strike=st["atm"],
                traded=0,
                config=cfg,
            )
        with self._lock:
            self.day["armed"] = any_expiry and session_ok
        if any_expiry and session_ok:
            self._schedule_hard_exit(cfg["hard_exit_time"])
            self._ensure_monitor_thread()
            self._notify(
                f"🎯 {STRATEGY_NAME} armed {tds} (mode={self.current_mode()}): "
                + "; ".join(
                    f"{u} exp {v.get('expiry')} ATM {v.get('atm')} "
                    f"{'TRADE ' + str(v.get('lots')) + ' lot(s)' if v.get('trade') else 'record only'}"
                    for u, v in summary.items()
                    if v.get("expiry_day")
                )
                + f" · target {cfg['target_mult']}x · hard exit {cfg['hard_exit_time']}"
            )
        elif any_expiry and not session_ok:
            self._notify(
                f"⚠️ {STRATEGY_NAME}: expiry day {tds} but NO broker session — nothing armed"
            )
        else:
            logger.info("cas_straddle: %s is not an expiry day for any underlying — idle", tds)
        return {"trade_date": tds, "armed": self.day["armed"], "underlyings": summary}

    def _build_ladder(self, u: str, spot: float | None = None) -> None:
        st = self.day["u"][u]
        if spot is None:
            q = self._quote_batch([(u, st["spot_exchange"])]) or {}
            spot = (q.get(u) or {}).get("ltp")
        if not spot:
            st["errors"].append("no spot quote")
            logger.error("cas_straddle: no spot quote for %s at arm", u)
            return
        try:
            strikes = self._strike_lister(u, st["expiry"], "CE", st["option_exchange"])
        except Exception:
            logger.exception("cas_straddle: strike list raised for %s", u)
            strikes = []
        if not strikes:
            st["errors"].append("no strikes for expiry")
            logger.error("cas_straddle: no strikes for %s %s", u, st["expiry"])
            return
        st["strikes"] = sorted(float(k) for k in strikes)
        atm = atm_from_strikes(float(spot), st["strikes"])
        st["atm"] = atm
        self._extend_ladder(u, atm)

    def _extend_ladder(self, u: str, atm: float) -> None:
        st = self.day["u"][u]
        for k in ladder_strikes(atm, st["strikes"]):
            for side in ("CE", "PE"):
                key = (k, side)
                if any((c["strike"], c["side"]) == key for c in st["ladder"].values()):
                    continue
                try:
                    c = self._contract_finder(u, st["expiry"], k, side, st["option_exchange"])
                except Exception:
                    logger.exception("cas_straddle: contract lookup raised %s %s %s", u, k, side)
                    c = None
                if not c:
                    continue
                st["ladder"][c["symbol"]] = c
                if st["lotsize"] is None and c.get("lotsize"):
                    st["lotsize"] = int(c["lotsize"])
                    st["tick_size"] = float(c.get("tick_size") or 0.05)

    def _atm_contracts(self, u: str) -> dict[str, dict]:
        st = self.day["u"][u]
        out = {}
        for c in st["ladder"].values():
            if st["atm"] is not None and float(c["strike"]) == float(st["atm"]):
                out[c["side"]] = c
        return out

    def _schedule_hard_exit(self, hms: str) -> None:
        sched = self.scheduler
        if sched is None:
            return
        t = parse_hms(hms) or parse_hms(DEFAULTS["hard_exit_time"])
        try:
            from apscheduler.triggers.cron import CronTrigger

            sched.add_job(
                _hard_exit_job,
                trigger=CronTrigger(
                    day_of_week="mon-fri",
                    hour=t.hour,
                    minute=t.minute,
                    second=t.second,
                    timezone="Asia/Kolkata",
                ),
                id="cas_straddle_hard_exit",
                replace_existing=True,
                name=f"CAS straddle hard exit ({hms} IST)",
            )
        except Exception:
            logger.exception("cas_straddle: failed to (re)schedule the hard exit at %s", hms)

    # ----- monitor thread ------------------------------------------------- #
    def _ensure_monitor_thread(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name="cas-straddle-monitor", daemon=True
        )
        self._monitor_thread.start()

    def _window_open(self, now: datetime | None = None) -> bool:
        now = now or self._now()
        return self.day.get("armed") and now.time() < WINDOW_END

    def _monitor_loop(self) -> None:
        from services.thread_registry import beat, done

        while True:
            beat("cas-straddle-monitor")
            now = self._now()
            if not self._window_open(now):
                try:
                    self._finalize_sessions()
                except Exception:
                    logger.exception("cas_straddle: session finalize failed")
                done("cas-straddle-monitor")
                return
            interval = self.effective_config()["poll_interval_s"]
            if now.time() >= WINDOW_START:
                try:
                    self.poll_once(now)
                except Exception:
                    logger.exception("cas_straddle: monitor tick failed")
            self._sleep(max(1, interval))

    def _watched(self) -> list[tuple[str, str]]:
        seen: list[tuple[str, str]] = []
        for u, st in self.day["u"].items():
            if not st["expiry_day"]:
                continue
            seen.append((u, st["spot_exchange"]))
            for c in st["ladder"].values():
                seen.append((c["symbol"], c["exchange"]))
        return seen

    def poll_once(self, now: datetime | None = None) -> dict | None:
        """One monitor cycle: quotes → polls journal → state → target rule."""
        now = now or self._now()
        watched = self._watched()
        if not watched:
            return None
        quotes = self._quote_batch(watched)
        if not quotes:
            logger.warning("cas_straddle: poll %s got no quotes", now.strftime("%H:%M:%S"))
            return None
        ts_utc = self._utc_naive(now)
        tds = self.day["trade_date"]
        poll_rows: list[dict] = []
        with self._lock:
            for u, st in self.day["u"].items():
                if not st["expiry_day"]:
                    continue
                spot_q = quotes.get(u) or {}
                spot = spot_q.get("ltp")
                # fix the ATM from the last continuous print (15:15:00)
                if spot and now.time() < CONTINUOUS_CLOSE:
                    st["close_1515"] = spot
                elif spot and not st["atm_fixed"]:
                    if st["close_1515"] is None:
                        st["close_1515"] = spot
                    atm = atm_from_strikes(float(st["close_1515"]), st["strikes"])
                    if atm is not None and atm != st["atm"]:
                        st["atm"] = atm
                        self._extend_ladder(u, atm)
                    st["atm_fixed"] = True
                    self._upsert_session(u, atm_strike=st["atm"], close_1515=st["close_1515"])
                poll_rows.append(
                    {
                        "ts": ts_utc,
                        "trade_date": tds,
                        "underlying": u,
                        "kind": "spot",
                        "symbol": u,
                        "exchange": st["spot_exchange"],
                        "strike": None,
                        "side": None,
                        "ltp": spot,
                        "bid": None,
                        "ask": None,
                        "volume": None,
                        "oi": None,
                    }
                )
                for c in st["ladder"].values():
                    q = quotes.get(c["symbol"]) or {}
                    poll_rows.append(
                        {
                            "ts": ts_utc,
                            "trade_date": tds,
                            "underlying": u,
                            "kind": "option",
                            "symbol": c["symbol"],
                            "exchange": c["exchange"],
                            "strike": c["strike"],
                            "side": c["side"],
                            "ltp": q.get("ltp"),
                            "bid": q.get("bid"),
                            "ask": q.get("ask"),
                            "volume": q.get("volume"),
                            "oi": q.get("oi"),
                        }
                    )
                atm_c = self._atm_contracts(u)
                ce = quotes.get(atm_c["CE"]["symbol"]) if "CE" in atm_c else None
                pe = quotes.get(atm_c["PE"]["symbol"]) if "PE" in atm_c else None
                st["samples"].append(
                    {
                        "t": now.time().replace(microsecond=0),
                        "spot": spot,
                        "ce_bid": (ce or {}).get("bid"),
                        "ce_ask": (ce or {}).get("ask"),
                        "pe_bid": (pe or {}).get("bid"),
                        "pe_ask": (pe or {}).get("ask"),
                    }
                )
                # pending fills: keep asking until answered
                if st["entered"] and any(leg["status"] == "placed" for leg in st["legs"].values()):
                    self._verify_legs(u, attempts=1)
                if st["entered"] and not st["exit_done"]:
                    self._evaluate_target(u, quotes, now)
        if self._journal_enabled and poll_rows:
            try:
                from database.cas_straddle_db import record_polls

                record_polls(poll_rows)
            except Exception:
                logger.exception("cas_straddle: poll journal write failed")
        return quotes

    # ----- 15:20 entry ---------------------------------------------------- #
    def run_entry(self) -> dict:
        """BUY the ATM CE + PE for every trading underlying (idempotent per day)."""
        now = self._now()
        tds = now.date().isoformat()
        results: dict[str, dict] = {}
        if self.day.get("trade_date") != tds or not self.day.get("armed"):
            logger.info("cas_straddle entry: day not armed (%s) — no orders", tds)
            return {"trade_date": tds, "armed": False, "results": results}
        if self.manual_pause or self._entry_held_by_override():
            for u, st in self.day["u"].items():
                if st["expiry_day"]:
                    st["entry_skipped"] = "paused"
                    results[u] = {"skipped": "paused"}
            logger.warning("cas_straddle entry held (pause/override) — no orders")
            return {"trade_date": tds, "armed": True, "results": results}
        cfg = self.day["config"] or self.effective_config()
        mode = self.current_mode()
        for u, st in self.day["u"].items():
            if not st["expiry_day"]:
                continue
            if st["entered"]:
                results[u] = {"skipped": "already_entered"}
                continue
            if not st["trade"] or st["lots"] < 1:
                st["entry_skipped"] = "trade_off"
                results[u] = {"skipped": "trade_off"}
                continue
            atm_c = self._atm_contracts(u)
            if "CE" not in atm_c or "PE" not in atm_c:
                st["entry_skipped"] = "no_atm_contracts"
                st["errors"].append("ATM contracts unresolved at entry")
                results[u] = {"skipped": "no_atm_contracts"}
                self._notify(f"⚠️ {STRATEGY_NAME} {u}: ATM contracts unresolved at 15:20 — no entry")
                continue
            instruments = [(atm_c[s]["symbol"], atm_c[s]["exchange"]) for s in ("CE", "PE")]
            quotes = self._quote_batch(instruments) or {}
            asks = {s: (quotes.get(atm_c[s]["symbol"]) or {}).get("ask") for s in ("CE", "PE")}
            bids = {s: (quotes.get(atm_c[s]["symbol"]) or {}).get("bid") for s in ("CE", "PE")}
            if not asks["CE"] or not asks["PE"]:
                st["entry_skipped"] = "no_ask"
                results[u] = {"skipped": "no_ask", "asks": asks}
                self._notify(f"⚠️ {STRATEGY_NAME} {u}: no ask on one leg at 15:20 — no entry")
                continue
            lotsize = int(st["lotsize"] or atm_c["CE"].get("lotsize") or 0)
            qty = st["lots"] * lotsize
            if qty <= 0:
                st["entry_skipped"] = "no_lotsize"
                results[u] = {"skipped": "no_lotsize"}
                continue
            cost = (float(asks["CE"]) + float(asks["PE"])) * qty
            if cost > float(cfg["max_premium_inr"]):
                st["entry_skipped"] = "premium_cap"
                results[u] = {"skipped": "premium_cap", "cost": round(cost, 2)}
                self._notify(
                    f"⛔ {STRATEGY_NAME} {u}: straddle cost ₹{cost:,.0f} for {st['lots']} lot(s) "
                    f"exceeds max_premium_inr ₹{cfg['max_premium_inr']:,.0f} — REFUSED, not trimmed"
                )
                continue
            st["entered"] = True
            placed = {}
            for side in ("CE", "PE"):
                c = atm_c[side]
                ask = float(asks[side])
                order = {
                    "symbol": c["symbol"],
                    "exchange": c["exchange"],
                    "action": "BUY",
                    "product": "NRML",
                    "quantity": qty,
                    "pricetype": "MARKET" if mode == "sandbox" else "LIMIT",
                }
                if order["pricetype"] == "LIMIT":
                    order["price"] = round_to_tick(
                        ask + LIMIT_BUFFER_TICKS * float(st["tick_size"] or 0.05),
                        st["tick_size"],
                    )
                leg = {
                    "side": side,
                    "symbol": c["symbol"],
                    "exchange": c["exchange"],
                    "strike": float(c["strike"]),
                    "expiry": c["expiry"],
                    "lots": st["lots"],
                    "quantity": qty,
                    "entry_ref_price": ask,
                    "entry_ref_bid": bids[side],
                    "entry_price": None,
                    "entry_qty": None,
                    "entry_order_id": None,
                    "status": "placed",
                    "fill": "real",
                    "row_id": None,
                    "mode": mode,
                    "error_message": None,
                    "exit_price": None,
                    "exit_reason": None,
                    "verify_attempts": 0,
                }
                try:
                    resp = self._order_placer(order)
                except Exception as e:
                    logger.exception("cas_straddle: order placer raised for %s", c["symbol"])
                    resp = {"status": "error", "message": f"exception: {e}"}
                if str(resp.get("status")).lower() == "success" and resp.get("orderid"):
                    leg["entry_order_id"] = str(resp["orderid"])
                else:
                    # placement rejection → paper leg (#548): nothing was ever held
                    leg["status"] = "rejected"
                    leg["fill"] = "paper"
                    leg["error_message"] = str(resp.get("message") or "order not accepted")[:255]
                    leg["entry_price"] = ask  # paper basis = the ask we would have paid
                    leg["entry_qty"] = qty
                leg["row_id"] = self._journal_leg(u, leg, tds, now)
                st["legs"][side] = leg
                placed[side] = {
                    "symbol": c["symbol"],
                    "order_id": leg["entry_order_id"],
                    "status": leg["status"],
                    "ask": ask,
                }
            self._upsert_session(
                u, traded=1, straddle_ask_1520=float(asks["CE"]) + float(asks["PE"])
            )
            # the entry poll is a sample too — the session digest's 15:20 cost
            # must be the ask we actually paid against, not the next monitor poll
            st["samples"].append(
                {
                    "t": now.time().replace(microsecond=0),
                    "spot": None,
                    "ce_bid": bids["CE"],
                    "ce_ask": float(asks["CE"]),
                    "pe_bid": bids["PE"],
                    "pe_ask": float(asks["PE"]),
                }
            )
            self._verify_legs(u, attempts=FILL_VERIFY_ATTEMPTS)
            results[u] = {"placed": placed, "cost": round(cost, 2), "mode": mode}
            legs_txt = ", ".join(
                f"{s} {p['symbol']} @ask {p['ask']} [{st['legs'][s]['status']}]"
                for s, p in placed.items()
            )
            self._notify(
                f"🟢 {STRATEGY_NAME} {u} entry ({mode}): {st['lots']} lot(s) × {lotsize} = {qty}; "
                f"cost ₹{cost:,.0f}; {legs_txt}"
            )
        return {"trade_date": tds, "armed": True, "results": results, "mode": mode}

    def _journal_leg(self, u: str, leg: dict, tds: str, now: datetime) -> int | None:
        if not self._journal_enabled:
            return None
        try:
            from database.cas_straddle_db import record_trade

            st = self.day["u"][u]
            return record_trade(
                mode=leg["mode"],
                trade_date=tds,
                underlying=u,
                exchange=leg["exchange"],
                symbol=leg["symbol"],
                strike=leg["strike"],
                expiry=leg["expiry"],
                side=leg["side"],
                product="NRML",
                lots=leg["lots"],
                quantity=leg["quantity"],
                entry_ref_price=leg["entry_ref_price"],
                entry_ref_bid=leg["entry_ref_bid"],
                entry_order_id=leg["entry_order_id"],
                entry_price=leg["entry_price"],
                entry_qty=leg["entry_qty"],
                entry_at=self._utc_naive(now),
                status=leg["status"],
                fill=leg["fill"],
                error_message=leg["error_message"],
                note=f"atm={st['atm']} close_1515={st['close_1515']}",
            )
        except Exception:
            logger.exception("cas_straddle: journal write failed for %s", leg.get("symbol"))
            return None

    def _update_leg_row(self, leg: dict, **fields) -> None:
        if not self._journal_enabled or not leg.get("row_id"):
            return
        try:
            from database.cas_straddle_db import update_trade

            update_trade(leg["row_id"], **fields)
        except Exception:
            logger.exception("cas_straddle: journal update failed for %s", leg.get("symbol"))

    def _verify_legs(self, u: str, attempts: int = 1) -> None:
        """Ask the order book what really happened to each ``placed`` leg."""
        st = self.day["u"][u]
        for _ in range(max(1, attempts)):
            pending = [leg for leg in st["legs"].values() if leg["status"] == "placed"]
            if not pending:
                return
            for leg in pending:
                leg["verify_attempts"] += 1
                try:
                    fill = self._fill_reader(leg["entry_order_id"])
                except Exception:
                    logger.exception("cas_straddle: fill reader raised for %s", leg["symbol"])
                    fill = None
                if not fill:
                    continue
                if fill["status"] == "filled":
                    leg["status"] = "open"
                    leg["entry_price"] = float(fill["price"])
                    leg["entry_qty"] = (
                        int(fill["qty"]) if fill.get("qty") is not None else leg["quantity"]
                    )
                    if leg["entry_qty"] and leg["entry_qty"] != leg["quantity"]:
                        logger.warning(
                            "cas_straddle: %s filled %s of %s",
                            leg["symbol"],
                            leg["entry_qty"],
                            leg["quantity"],
                        )
                    self._update_leg_row(
                        leg,
                        status="open",
                        entry_price=leg["entry_price"],
                        entry_qty=leg["entry_qty"],
                    )
                elif fill["status"] == "rejected":
                    # post-ACK rejection (#626): nothing held → paper leg
                    leg["status"] = "rejected"
                    leg["fill"] = "paper"
                    leg["error_message"] = (fill.get("message") or "rejected after ACK")[:255]
                    leg["entry_price"] = leg["entry_ref_price"]
                    leg["entry_qty"] = leg["quantity"]
                    self._update_leg_row(
                        leg,
                        status="rejected",
                        fill="paper",
                        error_message=leg["error_message"],
                        entry_price=leg["entry_price"],
                        entry_qty=leg["entry_qty"],
                    )
                    self._notify(
                        f"⚠️ {STRATEGY_NAME} {u} {leg['symbol']}: broker REJECTED the entry after ACK "
                        f"({leg['error_message']}) — leg is paper, never priced as real"
                    )
            if any(leg["status"] == "placed" for leg in st["legs"].values()) and attempts > 1:
                self._sleep(FILL_VERIFY_DELAY_S)

    # ----- target rule ---------------------------------------------------- #
    def _real_open_legs(self, st: dict) -> list[dict]:
        return [
            leg
            for leg in st["legs"].values()
            if leg["fill"] == "real" and leg["status"] in ("open", "placed")
        ]

    def _evaluate_target(self, u: str, quotes: dict, now: datetime) -> None:
        st = self.day["u"][u]
        cfg = self.day["config"] or self.effective_config()
        legs = self._real_open_legs(st)
        if not legs:
            return
        rows = []
        for leg in legs:
            q = quotes.get(leg["symbol"]) or {}
            rows.append(
                {
                    "entry_price": leg["entry_price"] or leg["entry_ref_price"],
                    "quantity": leg["entry_qty"] or leg["quantity"],
                    "bid": q.get("bid"),
                    "exchange": leg["exchange"],
                }
            )
        reached, detail = combined_target_reached(rows, cfg["target_mult"])
        detail["at"] = now.strftime("%H:%M:%S")
        st["last_eval"] = detail
        if reached:
            st["target_polls"] += 1
        else:
            st["target_polls"] = 0
        if st["target_polls"] >= CONFIRM_POLLS:
            logger.info("cas_straddle %s target reached (%s) — exiting", u, detail)
            self._exit_legs(u, "target", quotes=quotes)

    # ----- exits ---------------------------------------------------------- #
    def _exit_legs(
        self,
        u: str,
        reason: str,
        quotes: dict | None = None,
        legs_subset: list[dict] | None = None,
    ) -> list[dict]:
        """SELL every real open leg of ``u``; price paper legs at the bid.

        A leg with no bid is left to cash-settle (``expired_worthless``) — a
        SELL at 0.05 would cost more in brokerage than it returns.
        """
        st = self.day["u"][u]
        now = self._now()
        mode = self.current_mode()
        legs = [
            leg
            for leg in (legs_subset if legs_subset is not None else st["legs"].values())
            if leg["status"] in ("open", "placed", "rejected") and leg.get("exit_reason") is None
        ]
        if not legs:
            return []
        if quotes is None:
            quotes = self._quote_batch([(leg["symbol"], leg["exchange"]) for leg in legs]) or {}
        out = []
        for leg in legs:
            q = quotes.get(leg["symbol"]) or {}
            bid = q.get("bid")
            qty = leg["entry_qty"] or leg["quantity"]
            entry = leg["entry_price"] or leg["entry_ref_price"]
            if leg["fill"] == "paper":
                # measurement only — never an order
                px = float(bid) if bid else 0.0
                charges = option_leg_charges(
                    entry * qty, px * qty, leg["exchange"], orders=2 if px else 1
                )
                self._close_leg(leg, px, reason, charges, now, order_id=None, ref_bid=bid)
                out.append({"symbol": leg["symbol"], "paper": True, "exit_price": px})
                continue
            if not bid:
                charges = option_leg_charges(entry * qty, 0.0, leg["exchange"], orders=1)
                self._close_leg(
                    leg, 0.0, "expired_worthless", charges, now, order_id=None, ref_bid=None
                )
                out.append({"symbol": leg["symbol"], "exit_reason": "expired_worthless"})
                continue
            order = {
                "symbol": leg["symbol"],
                "exchange": leg["exchange"],
                "action": "SELL",
                "product": "NRML",
                "quantity": qty,
                "pricetype": "MARKET" if mode == "sandbox" else "LIMIT",
            }
            if order["pricetype"] == "LIMIT":
                order["price"] = max(
                    float(st["tick_size"] or 0.05),
                    round_to_tick(
                        float(bid) - LIMIT_BUFFER_TICKS * float(st["tick_size"] or 0.05),
                        st["tick_size"],
                    ),
                )
            try:
                resp = self._order_placer(order)
            except Exception as e:
                logger.exception("cas_straddle: exit placer raised for %s", leg["symbol"])
                resp = {"status": "error", "message": f"exception: {e}"}
            if str(resp.get("status")).lower() == "success" and resp.get("orderid"):
                leg["exit_order_id"] = str(resp["orderid"])
                fill = None
                for _ in range(FILL_VERIFY_ATTEMPTS):
                    try:
                        fill = self._fill_reader(leg["exit_order_id"])
                    except Exception:
                        logger.exception("cas_straddle: exit fill reader raised")
                        fill = None
                    if fill and fill["status"] in ("filled", "rejected"):
                        break
                    self._sleep(FILL_VERIFY_DELAY_S)
                if fill and fill["status"] == "filled":
                    px = float(fill["price"])
                    charges = option_leg_charges(entry * qty, px * qty, leg["exchange"])
                    self._close_leg(
                        leg, px, reason, charges, now, order_id=leg["exit_order_id"], ref_bid=bid
                    )
                    out.append(
                        {
                            "symbol": leg["symbol"],
                            "exit_price": px,
                            "order_id": leg["exit_order_id"],
                        }
                    )
                elif fill and fill["status"] == "rejected":
                    # a rejected EXIT on a filled entry means a REAL position may
                    # still be open (#626 asymmetry) — never papered
                    leg["status"] = "error"
                    leg["error_message"] = (fill.get("message") or "exit rejected")[:255]
                    self._update_leg_row(
                        leg,
                        status="error",
                        exit_order_id=leg["exit_order_id"],
                        exit_ref_price=bid,
                        error_message=leg["error_message"],
                    )
                    self._notify(
                        f"🚨 {STRATEGY_NAME} {u} {leg['symbol']}: EXIT REJECTED ({leg['error_message']}) "
                        f"— position may still be open; 15:38 fallback will retry"
                    )
                    out.append({"symbol": leg["symbol"], "error": leg["error_message"]})
                else:
                    # ACK but no fill answer yet: record the exit at the bid we
                    # asked against, flagged for the fallback to re-verify
                    px = float(bid)
                    charges = option_leg_charges(entry * qty, px * qty, leg["exchange"])
                    self._close_leg(
                        leg,
                        px,
                        reason,
                        charges,
                        now,
                        order_id=leg["exit_order_id"],
                        ref_bid=bid,
                        note="exit fill unverified — priced at the bid",
                    )
                    out.append({"symbol": leg["symbol"], "exit_price": px, "unverified": True})
            else:
                leg["status"] = "error"
                leg["error_message"] = str(resp.get("message") or "exit not accepted")[:255]
                self._update_leg_row(
                    leg, status="error", exit_ref_price=bid, error_message=leg["error_message"]
                )
                self._notify(
                    f"🚨 {STRATEGY_NAME} {u} {leg['symbol']}: exit order NOT accepted "
                    f"({leg['error_message']}) — 15:38 fallback will retry"
                )
                out.append({"symbol": leg["symbol"], "error": leg["error_message"]})
        if all(
            leg.get("exit_reason") is not None or leg["status"] == "error"
            for leg in st["legs"].values()
        ):
            st["exit_done"] = all(leg.get("exit_reason") is not None for leg in st["legs"].values())
        self._notify_exit(u, reason, out)
        return out

    def _close_leg(
        self,
        leg: dict,
        px: float,
        reason: str,
        charges: float,
        now: datetime,
        *,
        order_id: str | None,
        ref_bid: float | None,
        note: str | None = None,
    ) -> None:
        qty = leg["entry_qty"] or leg["quantity"]
        entry = leg["entry_price"] or leg["entry_ref_price"] or 0.0
        leg["status"] = "closed"
        leg["exit_price"] = px
        leg["exit_reason"] = reason
        leg["exit_order_id"] = order_id
        leg["charges_inr"] = charges
        leg["gross_pnl"] = round((px - entry) * qty, 2)
        leg["net_pnl"] = round(leg["gross_pnl"] - charges, 2)
        fields = {
            "status": "closed",
            "exit_price": px,
            "exit_reason": reason,
            "exit_order_id": order_id,
            "exit_ref_price": ref_bid,
            "exit_at": self._utc_naive(now),
            "charges_inr": charges,
        }
        if note:
            fields["note"] = note
        self._update_leg_row(leg, **fields)

    def _notify_exit(self, u: str, reason: str, out: list[dict]) -> None:
        st = self.day["u"][u]
        real = [
            leg
            for leg in st["legs"].values()
            if leg["fill"] == "real" and leg["status"] == "closed"
        ]
        net = sum(float(leg.get("net_pnl") or 0.0) for leg in real)
        parts = []
        for leg in st["legs"].values():
            tag = "paper" if leg["fill"] == "paper" else leg["status"]
            parts.append(
                f"{leg['side']} {leg.get('exit_reason') or '-'} @ {leg.get('exit_price')} "
                f"net ₹{(leg.get('net_pnl') or 0):,.0f} [{tag}]"
            )
        self._notify(
            f"🔻 {STRATEGY_NAME} {u} exit ({reason}): "
            + "; ".join(parts)
            + (f" · real net ₹{net:,.0f}" if real else "")
        )

    def run_hard_exit(self) -> dict:
        """The configured hard time exit for every entered underlying."""
        tds = self.trade_date()
        out = {}
        if self.day.get("trade_date") != tds or not self.day.get("armed"):
            return {"trade_date": tds, "armed": False}
        with self._lock:
            for u, st in self.day["u"].items():
                if st["expiry_day"] and st["entered"] and not st["exit_done"]:
                    out[u] = self._exit_legs(u, "hard_exit")
        return {"trade_date": tds, "armed": True, "results": out}

    def run_fallback_flatten(self) -> dict:
        """15:38 PROTECTED backstop: SELL whatever the position book still holds.

        Only an AFFIRMATIVE non-zero book quantity justifies a square-off of a
        leg we did not confirm filled; a leg we DID confirm filled is squared
        off even on an unreadable book (the believed-filled asymmetry, #626).
        """
        tds = self.trade_date()
        if self.day.get("trade_date") != tds:
            return {"trade_date": tds, "armed": False}
        out: dict[str, list] = {}
        try:
            book = self._book_reader()
        except Exception:
            logger.exception("cas_straddle: book read raised at fallback")
            book = None
        held = {}
        if book is not None:
            for p in book:
                held[p.get("symbol")] = int(p.get("quantity") or 0)
        with self._lock:
            for u, st in self.day["u"].items():
                if not st["expiry_day"]:
                    continue
                todo = []
                for leg in st["legs"].values():
                    if leg["fill"] != "real" or leg["status"] not in ("open", "placed", "error"):
                        continue
                    q_book = held.get(leg["symbol"]) if book is not None else None
                    if book is None:
                        if leg["status"] == "placed":
                            # never confirmed filled AND unreadable book: do not
                            # open a naked short on a guess
                            logger.warning(
                                "cas_straddle fallback: %s unverified + unreadable book — leaving",
                                leg["symbol"],
                            )
                            continue
                        todo.append(leg)  # believed filled → still send
                    elif q_book and q_book > 0:
                        leg["quantity"] = min(int(leg["entry_qty"] or leg["quantity"]), q_book)
                        leg["entry_qty"] = leg["quantity"]
                        todo.append(leg)
                    else:
                        # affirmatively flat
                        if leg["status"] == "placed":
                            leg["status"] = "rejected"
                            leg["fill"] = "paper"
                            leg["error_message"] = "never filled (book flat at fallback)"
                            leg["entry_price"] = leg["entry_ref_price"]
                            leg["entry_qty"] = leg["quantity"]
                            self._update_leg_row(
                                leg,
                                status="rejected",
                                fill="paper",
                                error_message=leg["error_message"],
                                entry_price=leg["entry_price"],
                                entry_qty=leg["entry_qty"],
                            )
                        else:
                            leg["status"] = "closed"
                            leg["exit_reason"] = "fallback"
                            self._update_leg_row(
                                leg,
                                status="closed",
                                exit_reason="fallback",
                                note="book flat at fallback; exit fill unknown",
                            )
                            self._notify(
                                f"⚠️ {STRATEGY_NAME} {u} {leg['symbol']}: book flat at 15:38 but no exit "
                                f"fill recorded — closed unpriced, check the order book"
                            )
                if todo:
                    for leg in todo:
                        if leg["status"] == "error":
                            leg["status"] = "open"  # retry the exit
                    out[u] = self._exit_legs(u, "fallback", legs_subset=todo)
        return {"trade_date": tds, "armed": bool(self.day.get("armed")), "results": out}

    def close_all_positions(self) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for u, st in self.day["u"].items():
                if st["expiry_day"] and st["entered"]:
                    out.extend(self._exit_legs(u, "manual"))
        return out

    # ----- sessions + summary --------------------------------------------- #
    def _upsert_session(self, u: str, **fields) -> None:
        if not self._journal_enabled:
            return
        try:
            from database.cas_straddle_db import upsert_session

            upsert_session(self.day["trade_date"], u, **fields)
        except Exception:
            logger.exception("cas_straddle: session upsert failed for %s", u)

    def _finalize_sessions(self) -> dict[str, dict]:
        cfg = self.day.get("config") or self.effective_config()
        out = {}
        with self._lock:
            for u, st in self.day["u"].items():
                if not st["expiry_day"]:
                    continue
                d = session_digest(st["samples"], st["atm"], cfg["target_mult"])
                st["digest"] = d
                out[u] = d
                self._upsert_session(u, atm_strike=st["atm"], **d)
                settle = d.get("settle")
                for leg in st["legs"].values():
                    if settle is None or leg.get("settlement_cf_pnl") is not None:
                        continue
                    intrinsic = intrinsic_at(settle, leg["strike"], leg["side"])
                    qty = leg["entry_qty"] or leg["quantity"]
                    entry = leg["entry_price"] or leg["entry_ref_price"] or 0.0
                    if intrinsic is None:
                        continue
                    cf = (
                        (intrinsic - entry) * qty
                        - exercise_stt(intrinsic * qty)
                        - option_leg_charges(entry * qty, 0.0, leg["exchange"], orders=1)
                    )
                    leg["settlement_cf_pnl"] = round(cf, 2)
                    self._update_leg_row(leg, settlement_cf_pnl=leg["settlement_cf_pnl"])
        return out

    def run_eod_summary(self) -> dict:
        tds = self.trade_date()
        if self.day.get("trade_date") != tds or not any(
            st["expiry_day"] for st in self.day["u"].values()
        ):
            return {"trade_date": tds, "expiry_day": False}
        digests = self._finalize_sessions()
        lines = [f"📊 {STRATEGY_NAME} EOD {tds} (mode={self.current_mode()})"]
        for u, st in self.day["u"].items():
            if not st["expiry_day"]:
                continue
            d = digests.get(u) or {}
            real = [leg for leg in st["legs"].values() if leg["fill"] == "real"]
            net = sum(float(leg.get("net_pnl") or 0.0) for leg in real if leg["status"] == "closed")
            cf = sum(float(leg.get("settlement_cf_pnl") or 0.0) for leg in st["legs"].values())
            lines.append(
                f"{u} exp {st['expiry']} ATM {st['atm']} · close15:15 {d.get('close_1515')} → "
                f"first {d.get('first_print')} lo {d.get('iiv_low')} hi {d.get('iiv_high')} "
                f"settle {d.get('settle')} · straddle ask@15:20 {d.get('straddle_ask_1520')} "
                f"peak bid {d.get('max_combined_bid')} @ {d.get('max_combined_bid_at')} · "
                f"{cfg_t(cfg=self.day.get('config'))} hit {'at ' + d['t_first_target'] if d.get('t_first_target') else 'no'} · "
                + (
                    f"TRADED {st['lots']} lot(s): real net ₹{net:,.0f}, hold-to-settle cf ₹{cf:,.0f}"
                    if st["entered"]
                    else f"not traded ({st['entry_skipped'] or 'record only'})"
                )
            )
        msg = "\n".join(lines)
        self._notify(msg)
        return {"trade_date": tds, "expiry_day": True, "digests": digests, "message": msg}

    # ----- observability -------------------------------------------------- #
    def get_status(self) -> dict:
        cfg = self.effective_config()
        today = self._now().date()
        u_out = {}
        for u, meta in UNDERLYINGS.items():
            st = self.day["u"].get(u)
            if st is None:
                nxt = None
                try:
                    _, nxt = is_expiry_today(self._expiry_lister(u, meta["option_exchange"]), today)
                except Exception:
                    logger.debug("cas_straddle status: expiry list failed for %s", u, exc_info=True)
                u_out[u] = {
                    "expiry_day": parse_expiry(nxt) == today if nxt else False,
                    "nearest_expiry": nxt,
                    "trade": bool(cfg["trade_nifty" if u == "NIFTY" else "trade_sensex"]),
                    "lots": int(cfg["lots_nifty" if u == "NIFTY" else "lots_sensex"]),
                    "armed": False,
                }
                continue
            atm_c = self._atm_contracts(u)
            u_out[u] = {
                "expiry_day": st["expiry_day"],
                "expiry": st["expiry"],
                "nearest_expiry": st["nearest_expiry"],
                "trade": st["trade"],
                "lots": st["lots"],
                "lotsize": st["lotsize"],
                "quantity": (st["lots"] * st["lotsize"]) if st["lotsize"] else None,
                "atm": st["atm"],
                "atm_fixed": st["atm_fixed"],
                "close_1515": st["close_1515"],
                "contracts": {s: c["symbol"] for s, c in atm_c.items()},
                "ladder": sorted(st["ladder"].keys()),
                "entered": st["entered"],
                "entry_skipped": st["entry_skipped"],
                "exit_done": st["exit_done"],
                "target_polls": st["target_polls"],
                "last_eval": st["last_eval"],
                "n_polls": len(st["samples"]),
                "legs": [
                    {k: v for k, v in leg.items() if k != "verify_attempts"}
                    for leg in st["legs"].values()
                ],
                "errors": list(st["errors"]),
                "digest": st.get("digest"),
                "armed": bool(self.day.get("armed")),
            }
        return {
            "strategy": STRATEGY_NAME,
            "mode": self.current_mode(),
            "manual_pause": self.manual_pause,
            "trade_date": self.day.get("trade_date"),
            "armed": bool(self.day.get("armed")),
            "armed_at": self.day.get("armed_at"),
            "monitor_alive": bool(self._monitor_thread and self._monitor_thread.is_alive()),
            "config": cfg,
            "day_config": self.day.get("config"),
            "schedule": {
                "reset": _hms(RESET_TIME),
                "arm": _hms(ARM_TIME),
                "window": f"{_hms(WINDOW_START)}–{_hms(WINDOW_END)}",
                "entry": _hms(ENTRY_TIME),
                "hard_exit": cfg["hard_exit_time"],
                "fallback_flatten": _hms(FALLBACK_TIME),
                "eod_summary": _hms(SUMMARY_TIME),
            },
            "underlyings": u_out,
        }

    # ----- scheduler ------------------------------------------------------ #
    def register_jobs(self, scheduler=None) -> None:
        sched = scheduler or self.scheduler
        if sched is None:
            from services.historify_scheduler_service import get_historify_scheduler

            sched = get_historify_scheduler().scheduler
        self.scheduler = sched
        from apscheduler.triggers.cron import CronTrigger

        global _SINGLETON
        _SINGLETON = self
        if self._journal_enabled:
            try:
                from database.cas_straddle_db import init_db

                init_db()
            except Exception:
                logger.exception("cas_straddle: journal init failed")

        def _cron(t: time):
            return CronTrigger(
                day_of_week="mon-fri",
                hour=t.hour,
                minute=t.minute,
                second=t.second,
                timezone="Asia/Kolkata",
            )

        sched.add_job(
            _daily_reset_job,
            trigger=_cron(RESET_TIME),
            id="cas_straddle_daily_reset",
            replace_existing=True,
            name="CAS straddle daily reset (09:00 IST)",
        )
        sched.add_job(
            _arm_job,
            trigger=_cron(ARM_TIME),
            id="cas_straddle_arm",
            replace_existing=True,
            name="CAS straddle arm (15:12 IST)",
        )
        sched.add_job(
            _entry_job,
            trigger=_cron(ENTRY_TIME),
            id="cas_straddle_entry",
            replace_existing=True,
            name="CAS straddle entry (15:20:00 IST)",
        )
        self._schedule_hard_exit(self.effective_config()["hard_exit_time"])
        sched.add_job(
            _fallback_flatten_job,
            trigger=_cron(FALLBACK_TIME),
            id="cas_straddle_fallback_flatten",
            replace_existing=True,
            name="CAS straddle fallback flatten (15:38 IST)",
        )
        sched.add_job(
            _eod_summary_job,
            trigger=_cron(SUMMARY_TIME),
            id="cas_straddle_eod_summary",
            replace_existing=True,
            name="CAS straddle EOD summary (15:45 IST)",
        )
        logger.info("cas_straddle jobs registered (mode=%s)", self.current_mode())


def cfg_t(cfg: dict | None) -> str:
    return f"{(cfg or DEFAULTS)['target_mult']}x"


# --------------------------------------------------------------------------- #
# Module-level scheduler entry points + singleton
# --------------------------------------------------------------------------- #
_SINGLETON: CasStraddleService | None = None


def get_service() -> CasStraddleService | None:
    return _SINGLETON


def _daily_reset_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.run_daily_reset()


def _arm_job() -> None:
    if _SINGLETON is not None:
        try:
            _SINGLETON.arm()
        except Exception:
            logger.exception("cas_straddle arm job raised")


def _entry_job() -> None:
    if _SINGLETON is not None:
        try:
            _SINGLETON.run_entry()
        except Exception:
            logger.exception("cas_straddle entry job raised")


def _hard_exit_job() -> None:
    if _SINGLETON is not None:
        try:
            _SINGLETON.run_hard_exit()
        except Exception:
            logger.exception("cas_straddle hard-exit job raised")


def _fallback_flatten_job() -> None:
    if _SINGLETON is not None:
        try:
            _SINGLETON.run_fallback_flatten()
        except Exception:
            logger.exception("cas_straddle fallback job raised")


def _eod_summary_job() -> None:
    if _SINGLETON is not None:
        try:
            _SINGLETON.run_eod_summary()
        except Exception:
            logger.exception("cas_straddle EOD summary job raised")


def init_cas_straddle_service(app=None, scheduler=None) -> CasStraddleService:
    """Build the singleton and register its jobs. Unconditional at boot — the
    strategy trades the sandbox book from the first expiry day; the operator's
    /strategies toggle is what takes it live."""
    svc = CasStraddleService(app=app, scheduler=scheduler)
    svc.register_jobs(scheduler)
    return svc


__all__ = [
    "CasStraddleService",
    "DEFAULTS",
    "STRATEGY_NAME",
    "UNDERLYINGS",
    "combined_target_reached",
    "get_service",
    "init_cas_straddle_service",
    "is_expiry_today",
    "option_leg_charges",
    "resolve_config",
    "session_digest",
    "validate_config",
]
