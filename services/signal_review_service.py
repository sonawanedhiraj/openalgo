"""Stage-1 LLM veto layer — service-side glue.

Sits between the simplified engine and the Claude Bridge's ``/review-signal``
endpoint. Builds the operator/market context, calls the bridge with a short
timeout, persists the result as a ``signal_decision`` row, and returns the
decision for the engine to act on (or ignore, in shadow mode).

Hard rule: every error path returns ``decision='take'`` with
``reasoning='review_failed'``. The reviewer being unavailable must never block
the engine. Enforcement mode is a separate concern owned by the caller — this
service always records the row, the caller decides whether to enforce it.

Configuration (env, with defaults):

* ``VETO_LAYER_MODE`` — 'off' | 'shadow' | 'active'. Default 'shadow'. Read by
  the engine to decide whether to enforce; this service stamps it onto the
  audit row regardless.
* ``VETO_BRIDGE_URL`` — bridge endpoint. Default
  ``http://127.0.0.1:5001/review-signal``.
* ``VETO_CACHE_TTL_SECONDS`` — same (symbol, source) reuses a prior decision
  for this many seconds. Default 300 (5 min).
* ``VETO_REQUEST_TIMEOUT_SECONDS`` — HTTP read timeout. Slightly longer than
  the bridge's own 25s wall-clock so the bridge wins the race. Default 30.
"""

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

import httpx
import pytz

from database import signal_decision_db
from utils.logging import get_logger

logger = get_logger(__name__)


VALID_VETO_MODES = ("off", "shadow", "active")


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("signal_review: invalid %s=%r; using default %s", name, raw, default)
        return default


def get_veto_layer_mode() -> str:
    raw = _env("VETO_LAYER_MODE", "shadow").lower()
    if raw not in VALID_VETO_MODES:
        logger.warning(
            "signal_review: unknown VETO_LAYER_MODE=%r; falling back to 'shadow'", raw
        )
        return "shadow"
    return raw


def _bridge_url() -> str:
    return _env("VETO_BRIDGE_URL", "http://127.0.0.1:5001/review-signal")


def _cache_ttl_seconds() -> float:
    return _env_float("VETO_CACHE_TTL_SECONDS", 300.0)


def _request_timeout_seconds() -> float:
    return _env_float("VETO_REQUEST_TIMEOUT_SECONDS", 30.0)


# ---------------------------------------------------------------------------
# In-process review cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _cache_key(symbol: str, source: str) -> tuple[str, str]:
    return (symbol.upper(), source.lower())


def get_review_cache(symbol: str, source: str) -> dict[str, Any] | None:
    """Return a cached decision dict for (symbol, source) if still fresh, else None."""
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    key = _cache_key(symbol, source)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, decision = entry
        if (time.time() - stored_at) >= ttl:
            _cache.pop(key, None)
            return None
        return dict(decision)


def _store_in_cache(symbol: str, source: str, decision: dict[str, Any]) -> None:
    key = _cache_key(symbol, source)
    with _cache_lock:
        _cache[key] = (time.time(), dict(decision))


def clear_review_cache() -> None:
    """Test/operator hook — drop every entry."""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
# Context builder
# ---------------------------------------------------------------------------


def _safe_call(label: str, fn, *args, **kwargs):
    """Run ``fn(*args, **kwargs)`` and swallow exceptions with a warning.

    Context-building must not fail the review. Any sub-fetch that blows up
    just contributes ``None`` to the context dict and we let the reviewer
    handle the missing field.
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        logger.warning("signal_review: context fetch %s failed; continuing", label, exc_info=True)
        return None


def _fetch_nifty_pct() -> float:
    """Return today's % change in NIFTY 50, computed from ltp and prev_close.

    Raises on any failure path so the caller's try/except records the gap.
    """
    from database.auth_db import get_first_available_api_key
    from services.quotes_service import get_quotes

    api_key = get_first_available_api_key()
    if not api_key:
        raise RuntimeError("no api key available")
    success, response, _ = get_quotes(symbol="NIFTY", exchange="NSE_INDEX", api_key=api_key)
    if not success:
        raise RuntimeError(f"quote fetch failed: {response.get('message', 'unknown')}")
    data = response.get("data") or {}
    ltp = float(data.get("ltp") or 0.0)
    prev_close = float(data.get("prev_close") or 0.0)
    if prev_close == 0.0:
        raise RuntimeError("prev_close is zero")
    return (ltp - prev_close) / prev_close * 100.0


def _fetch_india_vix() -> float:
    """Return the current India VIX level (the LTP itself, not a % change)."""
    from database.auth_db import get_first_available_api_key
    from services.quotes_service import get_quotes

    api_key = get_first_available_api_key()
    if not api_key:
        raise RuntimeError("no api key available")
    success, response, _ = get_quotes(
        symbol="INDIAVIX", exchange="NSE_INDEX", api_key=api_key
    )
    if not success:
        raise RuntimeError(f"quote fetch failed: {response.get('message', 'unknown')}")
    data = response.get("data") or {}
    ltp = data.get("ltp")
    if ltp is None:
        raise RuntimeError("no ltp in vix quote")
    return float(ltp)


def _fetch_pnl_today() -> float:
    """Return today's running P&L summed across all open positions.

    Sources via ``positionbook_service.get_positionbook`` which is mode-routed
    (sandbox vs live) by ``resolve_effective_mode()``. Sums the ``pnl`` field
    on each position — that field is the broker-supplied MTM for today.
    """
    from database.auth_db import get_first_available_api_key
    from services.positionbook_service import get_positionbook

    api_key = get_first_available_api_key()
    if not api_key:
        raise RuntimeError("no api key available")
    success, response, _ = get_positionbook(api_key=api_key)
    if not success:
        raise RuntimeError(f"positionbook fetch failed: {response.get('message', 'unknown')}")
    data = response.get("data") or []
    if not isinstance(data, list):
        raise RuntimeError("positionbook payload is not a list")
    total = 0.0
    for pos in data:
        value = pos.get("pnl")
        if value is None:
            continue
        total += float(value)
    return total


def _build_context(override: dict[str, Any] | None) -> dict[str, Any]:
    """Assemble the operator + market context dict shipped to the bridge.

    Pulls live data lazily so unit tests can opt out by supplying an override.
    Every field is best-effort: missing data lands as ``None`` and the LLM is
    instructed to tolerate that.
    """
    if override is not None:
        return dict(override)

    # All imports here are intentionally lazy — pulling these at module import
    # time would create cycles (engine ↔ services ↔ this module) and would
    # also force tests to mock paths inside heavy modules.
    ctx: dict[str, Any] = {
        "positions_count": None,
        "positions_summary": None,
        "pnl_today": None,
        "trades_today": None,
        "max_trades_today": None,
        "nifty_pct": None,
        "india_vix": None,
    }

    # Engine state: trade counts come from the live simplified engine instance.
    def _engine_stats() -> tuple[int | None, int | None, int | None, str | None]:
        from services.simplified_stock_engine_service import (
            get_simplified_stock_engine_service,
        )

        svc = get_simplified_stock_engine_service()
        engine = svc.engine
        positions = engine.positions
        position_count = len(positions)
        # Compact summary like "1 SHORT CONCOR @ 124.5". Cap at 3 positions
        # so we don't ship megabytes of state to the LLM.
        parts: list[str] = []
        for symbol, pos in list(positions.items())[:3]:
            side = "LONG" if pos.qty > 0 else "SHORT"
            parts.append(f"{abs(pos.qty)} {side} {symbol} @ {pos.entry_price:.2f}")
        summary = "; ".join(parts) if parts else "flat"
        return (
            position_count,
            len(engine.completed_trades),
            svc.config.max_trades_per_day,
            summary,
        )

    stats = _safe_call("engine_stats", _engine_stats)
    if stats is not None:
        positions_count, trades_today, max_trades_today, positions_summary = stats
        ctx["positions_count"] = positions_count
        ctx["trades_today"] = trades_today
        ctx["max_trades_today"] = max_trades_today
        ctx["positions_summary"] = positions_summary

    # Macro slots — each is wrapped in its own try/except so one failure
    # doesn't blank the others.
    try:
        ctx["nifty_pct"] = _fetch_nifty_pct()
    except Exception as exc:
        logger.warning("signal_review: nifty_pct fetch failed: %s", exc)
        ctx["nifty_pct"] = None

    try:
        ctx["india_vix"] = _fetch_india_vix()
    except Exception as exc:
        logger.warning("signal_review: india_vix fetch failed: %s", exc)
        ctx["india_vix"] = None

    try:
        ctx["pnl_today"] = _fetch_pnl_today()
    except Exception as exc:
        logger.warning("signal_review: pnl_today fetch failed: %s", exc)
        ctx["pnl_today"] = None

    return ctx


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _now_ist_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _failsafe_decision(reason: str) -> dict[str, Any]:
    return {
        "decision": "take",
        "reasoning": reason,
        "confidence": 0.0,
        "latency_ms": 0,
        "claude_session_id": "",
        "raw_output": "",
    }


def review_signal(
    symbol: str,
    source: str,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the LLM whether the operator should take this signal.

    Returns a dict with ``decision`` (``'take' | 'skip'``), ``reasoning``,
    ``confidence``, ``id`` (signal_decision row id), ``enforcement_mode``, and
    a few diagnostic fields. The row is always written, including for the
    fail-safe paths — that way the audit log can distinguish a real ``take``
    from a reviewer-failed fallback (the row's ``decision`` will be
    ``'review_failed'`` in that case, even though the returned ``decision``
    key is ``'take'`` for the engine's convenience).
    """
    enforcement_mode = get_veto_layer_mode()

    # Cache check — same (symbol, source) within TTL reuses the prior decision.
    cached = get_review_cache(symbol, source)
    if cached is not None:
        # Still record an audit row so we can see cache hits in the table,
        # but tag the reasoning so it's distinguishable from a fresh review.
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            decision=cached["decision"],
            reasoning=f"cache_hit: {cached.get('reasoning', '')}",
            confidence=cached.get("confidence", 0.0),
            enforcement_mode=enforcement_mode,
            context_snapshot=context or _build_context(None),
            bridge_latency_ms=0,
            bridge_session_id=cached.get("claude_session_id", ""),
            raw_bridge_output="(cache hit)",
        )
        result = dict(cached)
        result["id"] = decision_id
        result["enforcement_mode"] = enforcement_mode
        result["cache_hit"] = True
        return result

    snapshot = context if context is not None else _build_context(None)

    request_body = {
        "candidate": {
            "symbol": symbol,
            "source": source,
            "candidate_at": _now_ist_iso(),
        },
        "context": {
            "positions_count": snapshot.get("positions_count"),
            "positions_summary": snapshot.get("positions_summary"),
            "pnl_today": snapshot.get("pnl_today"),
            "trades_today": snapshot.get("trades_today"),
            "max_trades_today": snapshot.get("max_trades_today"),
            "nifty_pct": snapshot.get("nifty_pct"),
            "india_vix": snapshot.get("india_vix"),
        },
    }

    bridge_url = _bridge_url()
    timeout = _request_timeout_seconds()

    started = time.time()
    response_payload: dict[str, Any] | None = None
    failure_reason: str | None = None
    try:
        response = httpx.post(bridge_url, json=request_body, timeout=timeout)
        if 200 <= response.status_code < 300:
            response_payload = response.json()
        else:
            failure_reason = f"bridge_http_{response.status_code}"
    except httpx.TimeoutException:
        failure_reason = "bridge_timeout"
    except httpx.HTTPError as exc:
        failure_reason = f"bridge_error:{type(exc).__name__}"
    except Exception as exc:
        logger.exception("signal_review: unexpected error calling bridge")
        failure_reason = f"unexpected:{type(exc).__name__}"

    latency_ms = int((time.time() - started) * 1000)

    if response_payload is None:
        logger.warning(
            "signal_review: bridge call failed (%s); failing safe to take", failure_reason
        )
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            decision="review_failed",
            reasoning=failure_reason or "review_failed",
            confidence=0.0,
            enforcement_mode=enforcement_mode,
            context_snapshot=snapshot,
            bridge_latency_ms=latency_ms,
            bridge_session_id=None,
            raw_bridge_output=None,
        )
        result = _failsafe_decision(failure_reason or "review_failed")
        result["id"] = decision_id
        result["enforcement_mode"] = enforcement_mode
        result["latency_ms"] = latency_ms
        return result

    # Normal path — surface whatever the bridge gave us. The bridge already
    # fail-safes to 'take' on its own errors, so this branch is only "we got
    # a structured answer back, good or bad."
    decision = response_payload.get("decision")
    reasoning = response_payload.get("reasoning", "")
    confidence = response_payload.get("confidence", 0.0)
    bridge_latency = response_payload.get("latency_ms", latency_ms)
    session_id = response_payload.get("claude_session_id", "")
    raw_output = response_payload.get("raw_output", "")

    if decision not in ("take", "skip"):
        # Defensive — bridge contract says it can't happen, but if it does we
        # treat it as a review failure rather than trusting bogus output.
        logger.warning("signal_review: bridge returned invalid decision=%r", decision)
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            decision="review_failed",
            reasoning=f"bad_decision:{decision!r}",
            confidence=0.0,
            enforcement_mode=enforcement_mode,
            context_snapshot=snapshot,
            bridge_latency_ms=bridge_latency,
            bridge_session_id=session_id,
            raw_bridge_output=raw_output,
        )
        result = _failsafe_decision(f"bad_decision:{decision!r}")
        result["id"] = decision_id
        result["enforcement_mode"] = enforcement_mode
        result["latency_ms"] = bridge_latency
        return result

    decision_id = _persist_decision(
        symbol=symbol,
        source=source,
        decision=decision,
        reasoning=reasoning,
        confidence=float(confidence) if confidence is not None else None,
        enforcement_mode=enforcement_mode,
        context_snapshot=snapshot,
        bridge_latency_ms=bridge_latency,
        bridge_session_id=session_id,
        raw_bridge_output=raw_output,
    )

    fresh_result = {
        "id": decision_id,
        "decision": decision,
        "reasoning": reasoning,
        "confidence": float(confidence) if confidence is not None else 0.0,
        "latency_ms": bridge_latency,
        "claude_session_id": session_id,
        "raw_output": raw_output,
        "enforcement_mode": enforcement_mode,
        "cache_hit": False,
    }

    # Cache the fresh decision (sans audit-only fields). We intentionally do
    # NOT cache review_failed results — we want the next signal to retry the
    # bridge rather than reuse a failure for 5 minutes.
    _store_in_cache(
        symbol,
        source,
        {
            "decision": decision,
            "reasoning": reasoning,
            "confidence": fresh_result["confidence"],
            "claude_session_id": session_id,
        },
    )

    return fresh_result


def _persist_decision(
    *,
    symbol: str,
    source: str,
    decision: str,
    reasoning: str | None,
    confidence: float | None,
    enforcement_mode: str,
    context_snapshot: dict[str, Any] | None,
    bridge_latency_ms: int | None,
    bridge_session_id: str | None,
    raw_bridge_output: str | None,
) -> int | None:
    """Write the audit row. Returns the new id, or None if the write failed.

    Persistence is best-effort — if the DB blows up we still want the engine
    to receive a decision, so we log the exception and move on.
    """
    try:
        return signal_decision_db.insert_signal_decision(
            symbol=symbol,
            source=source,
            decision=decision,
            reasoning=reasoning,
            confidence=confidence,
            enforcement_mode=enforcement_mode,
            context_snapshot=context_snapshot,
            bridge_latency_ms=bridge_latency_ms,
            bridge_session_id=bridge_session_id,
            raw_bridge_output=raw_bridge_output,
        )
    except Exception:
        logger.exception("signal_review: failed to persist signal_decision row")
        return None


def mark_actually_taken(decision_id: int | None, taken: bool) -> None:
    """Caller hook — record whether the engine actually placed the order.

    Accepts None so the engine can pass through a failed-persist case without
    branching.
    """
    if decision_id is None:
        return
    signal_decision_db.mark_actually_taken(decision_id, taken)
