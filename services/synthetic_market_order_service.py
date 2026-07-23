"""Synthetic-market LIMIT conversion for RMS-blocked option MARKET orders.

Zerodha (and other Indian brokers) reject MARKET and SL-M orders on
individual-stock options (OPTSTK) and commodity options with
``RMS:Blocked for OPTSTK MKT`` — the books are too thin for an unpriced
sweep. Their documented workaround is a LIMIT order priced aggressively
past the LTP (BUY above / SELL below): it fills immediately at the best
available prices like a market order, with the limit as a worst-case cap.
Reference: https://support.zerodha.com/category/trading-and-markets/margins/
margin-leverage-and-product-and-order-types/articles/
what-does-rms-blocked-for-optstk-mkt-mean

``ensure_live_safe_pricetype`` is called on the LIVE dispatch path only
(place / basket / split order services) — sandbox fills MARKET fine and is
untouched. Index options (OPTIDX), futures, and equity pass through
unchanged. The conversion never raises; on an unpriceable order it returns
an error message so the caller rejects loudly instead of collecting a
guaranteed RMS rejection.

Flags (consult-time, so a runtime flip needs no restart):
- ``OPTSTK_SYNTHETIC_MARKET_ENABLED`` (default ``true``) — master switch.
- ``OPTSTK_SYNTHETIC_MARKET_BUFFER_PCT`` (default ``5.0``) — how far past
  LTP (or trigger, for SL-M) the protective limit is placed.
"""

import math
import os
from typing import Any

from database.token_db_enhanced import extract_underlying_from_symbol, get_symbol_info
from utils.logging import get_logger

logger = get_logger(__name__)

# Index underlyings whose options allow MARKET orders (OPTIDX is not blocked).
INDEX_UNDERLYINGS = {
    "NIFTY",
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "INDIAVIX",
    "SENSEX",
    "BANKEX",
    "SENSEX50",
}

# Exchanges that carry RMS-blockable option contracts. MCX options
# (commodity) are blocked wholesale; NFO/BFO only for non-index underlyings.
OPTION_EXCHANGES = {"NFO", "BFO", "MCX"}

DEFAULT_BUFFER_PCT = 5.0
DEFAULT_TICK_SIZE = 0.05


def _enabled() -> bool:
    return os.getenv("OPTSTK_SYNTHETIC_MARKET_ENABLED", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _buffer_pct() -> float:
    raw = os.getenv("OPTSTK_SYNTHETIC_MARKET_BUFFER_PCT", str(DEFAULT_BUFFER_PCT))
    try:
        value = float(raw)
        return value if value > 0 else DEFAULT_BUFFER_PCT
    except (TypeError, ValueError):
        logger.warning(
            "OPTSTK_SYNTHETIC_MARKET_BUFFER_PCT=%r is not a number — using default %.1f",
            raw,
            DEFAULT_BUFFER_PCT,
        )
        return DEFAULT_BUFFER_PCT


def is_market_blocked_option(symbol: str, exchange: str) -> bool:
    """True when the broker RMS rejects MARKET/SL-M orders on this contract.

    Stock options (CE/PE on NFO/BFO with a non-index underlying) and
    commodity options (CE/PE on MCX). An unclassifiable option defaults to
    True — a converted near-market LIMIT is safe even where MARKET would
    have been accepted, while the reverse is a guaranteed RMS rejection.
    """
    exchange = (exchange or "").upper()
    if exchange not in OPTION_EXCHANGES:
        return False

    sym = (symbol or "").upper()
    info = get_symbol_info(symbol, exchange)
    inst = (info.instrumenttype or "").upper() if info else ""
    if inst:
        if inst not in ("CE", "PE"):
            return False
    elif not (sym.endswith("CE") or sym.endswith("PE")):
        return False

    if exchange == "MCX":
        return True

    underlying = (info.underlying if info else None) or extract_underlying_from_symbol(
        sym, exchange
    )
    if not underlying:
        return True
    return underlying.upper() not in INDEX_UNDERLYINGS


def _tick_size(symbol: str, exchange: str) -> float:
    info = get_symbol_info(symbol, exchange)
    tick = getattr(info, "tick_size", None) if info else None
    return float(tick) if tick and tick > 0 else DEFAULT_TICK_SIZE


def _round_to_tick(price: float, tick: float, action: str) -> float:
    """BUY rounds up, SELL rounds down — never inside the intended buffer."""
    ticks = price / tick
    n = math.ceil(ticks - 1e-9) if action == "BUY" else math.floor(ticks + 1e-9)
    return round(max(n, 1) * tick, 2)


def _fetch_ltp(symbol: str, exchange: str, auth_token: str, broker: str) -> float | None:
    """LTP via the shared quotes path. Module-level so tests can monkeypatch."""
    try:
        from services.quotes_service import get_quotes

        ok, data, _code = get_quotes(symbol, exchange, auth_token=auth_token, broker=broker)
        if not ok:
            logger.warning("synthetic-market: quote failed for %s:%s — %s", exchange, symbol, data)
            return None
        ltp = (data.get("data") or {}).get("ltp")
        return float(ltp) if ltp else None
    except Exception:
        logger.exception("synthetic-market: quote raised for %s:%s", exchange, symbol)
        return None


def ensure_live_safe_pricetype(
    order_data: dict[str, Any], auth_token: str, broker: str
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Convert a live MARKET/SL-M stock-option order to its protective form.

    MARKET → LIMIT at ``LTP × (1 ± buffer)``; SL-M → SL at
    ``trigger × (1 ± buffer)`` (BUY above, SELL below), tick-rounded.
    Non-blocked instruments and non-market price types pass through
    untouched. Never raises.

    Returns:
        ``(order_data, conversion_info | None, error_message | None)`` —
        on error the order must NOT be sent to the broker (it would be
        RMS-rejected anyway); reject it with the message instead.
    """
    try:
        if not _enabled():
            return order_data, None, None

        pricetype = str(order_data.get("pricetype", "")).upper()
        if pricetype not in ("MARKET", "SL-M"):
            return order_data, None, None

        symbol = order_data.get("symbol", "")
        exchange = str(order_data.get("exchange", "")).upper()
        if not is_market_blocked_option(symbol, exchange):
            return order_data, None, None

        action = str(order_data.get("action", "")).upper()
        if action not in ("BUY", "SELL"):
            return order_data, None, None

        buffer_frac = _buffer_pct() / 100.0
        tick = _tick_size(symbol, exchange)

        if pricetype == "MARKET":
            reference = _fetch_ltp(symbol, exchange, auth_token, broker)
            if reference is None or reference <= 0:
                return (
                    order_data,
                    None,
                    (
                        f"Cannot place MARKET order on {exchange}:{symbol}: broker RMS "
                        f"blocks market orders on stock/commodity options and the LTP "
                        f"needed for the protective LIMIT conversion is unavailable. "
                        f"Order not placed — retry, or place a LIMIT order manually."
                    ),
                )
            target_pricetype = "LIMIT"
        else:  # SL-M
            try:
                reference = float(order_data.get("trigger_price") or 0)
            except (TypeError, ValueError):
                reference = 0.0
            if reference <= 0:
                return (
                    order_data,
                    None,
                    (
                        f"Cannot place SL-M order on {exchange}:{symbol}: broker RMS "
                        f"blocks SL-M on stock/commodity options and no trigger_price "
                        f"is set for the protective SL conversion. Order not placed."
                    ),
                )
            target_pricetype = "SL"

        raw = reference * (1 + buffer_frac) if action == "BUY" else reference * (1 - buffer_frac)
        limit_price = _round_to_tick(raw, tick, action)

        converted = dict(order_data)
        converted["pricetype"] = target_pricetype
        converted["price"] = limit_price

        conversion = {
            "original_pricetype": pricetype,
            "converted_pricetype": target_pricetype,
            "reference_price": reference,
            "limit_price": limit_price,
            "buffer_pct": _buffer_pct(),
            "tick_size": tick,
        }
        logger.info(
            "synthetic-market: %s %s:%s %s → %s @ %.2f (ref=%.2f buffer=%.1f%% tick=%.2f) — "
            "broker RMS blocks %s on stock/commodity options",
            action,
            exchange,
            symbol,
            pricetype,
            target_pricetype,
            limit_price,
            reference,
            _buffer_pct(),
            tick,
            pricetype,
        )
        return converted, conversion, None
    except Exception:
        # Fail open on unexpected internal errors: pass the order through
        # unchanged (worst case the broker RMS-rejects it — same as pre-fix)
        # rather than blocking live order flow on a conversion bug.
        logger.exception("synthetic-market: conversion failed — passing order through unchanged")
        return order_data, None, None
