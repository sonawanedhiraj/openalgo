"""Stage-1 LLM veto layer — service-side glue.

Builds the operator/market context, invokes ``claude -p`` **in-process** (via
``services.llm_review_client.invoke_claude_review`` — a blocking subprocess on a
dedicated real OS thread, eventlet-safe), parses the model's decision block,
persists the result as a ``signal_decision`` row, and returns the decision for
the engine to act on (or ignore, in shadow mode).

Phase 1 of #266 retired the Claude Bridge (``http://127.0.0.1:5001/review-signal``)
from this path: the bridge was never auto-started, so every real veto call failed
``ConnectError`` and fell safe to 'take' — the veto never actually fired. The
reasoning call is now made directly, so 'active' finally means active.

Hard rule: every error path returns ``decision='take'`` with
``reasoning='review_failed'``. The reviewer being unavailable must never block
the engine. Enforcement mode is a separate concern owned by the caller — this
service always records the row, the caller decides whether to enforce it.

Configuration (env, with defaults):

* ``VETO_LAYER_MODE`` — 'off' | 'shadow' | 'active'. Default 'shadow'. Read by
  the engine to decide whether to enforce; this service stamps it onto the
  audit row regardless.
* ``VETO_CACHE_TTL_SECONDS`` — same (symbol, source) reuses a prior decision
  for this many seconds. Default 300 (5 min).
* ``VETO_CLAUDE_TIMEOUT_SECONDS`` — wall-clock budget for the ``claude -p``
  subprocess. Default 25.
* ``CLAUDE_CMD`` — override the ``claude`` binary path (read by
  ``llm_review_client``). Default ``claude`` (resolved against PATH).
"""

import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Any

import pytz

from database import signal_decision_db
from services.llm_review_client import invoke_claude_review
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


def _resolve_llm_mode_from_db(strategy_name: str | None) -> str | None:
    """Read the strategy's persistent ``llm_mode`` (issue #266 Phase 2) and map
    it to the internal enforcement mode.

    Mapping (the UI-selectable axis → the engine's enforcement axis):

    * ``off``      → ``off``    (no reviewer runs)
    * ``veto``     → ``active`` (a ``skip`` verdict blocks the order)
    * ``delegate`` → ``active`` (stored, but the LLM-decides path isn't built
                     yet — treated as ``veto``/``active`` for now)

    Returns the mapped enforcement mode, or ``None`` when there is no DB row for
    the strategy (so the caller falls through to the env/default resolution).
    ``shadow`` is intentionally NOT reachable from the DB — it stays an
    env-only internal option.
    """
    if not strategy_name:
        return None
    try:
        from database.strategy_llm_config_db import get_llm_mode

        row = get_llm_mode(strategy_name)
    except Exception:
        logger.exception(
            "signal_review: strategy_llm_config lookup failed for %s — env fallback",
            strategy_name,
        )
        return None
    if not row:
        return None
    llm_mode = (row.get("llm_mode") or "").strip().lower()
    if llm_mode == "off":
        return "off"
    if llm_mode in ("veto", "delegate"):
        return "active"
    logger.warning(
        "signal_review: unknown llm_mode=%r for %s — env fallback", llm_mode, strategy_name
    )
    return None


def get_veto_layer_mode(effective_mode: str | None = None, strategy_name: str | None = None) -> str:
    """Resolve the veto-layer enforcement mode (``off`` / ``shadow`` / ``active``).

    Resolution order (issue #266 Phase 2):

    1. **Per-strategy DB row** — if ``strategy_name`` is given and the
       ``strategy_llm_config`` table has a row, that operator-set ``llm_mode``
       wins (``off``→off, ``veto``/``delegate``→active). This is the single UI
       control; it replaces the hidden env flag once the operator sets it.
    2. **``VETO_LAYER_MODE`` env** — the first-boot fallback / emergency
       override when no DB row exists. Set it to ``off`` for an emergency
       disable, or ``shadow`` for the env-only observe-only mode (not
       UI-selectable).
    3. **Mode-aware default** — env unset and no DB row: a strategy routing to
       ``sandbox`` defaults to ``active`` (the veto *enforces* on the virtual
       ₹1Cr book so the layer is exercised before it ever gates live money);
       any other mode defaults to ``shadow`` (observe-only). This preserves the
       pre-Phase-2 behavior until the operator sets a UI value, and — because
       the DB row is now the explicit source of truth for a configured strategy
       — resolves the #274 shadow-vs-active ambiguity in sandbox.
    """
    # 1. Per-strategy DB row is the authoritative operator control.
    db_mode = _resolve_llm_mode_from_db(strategy_name)
    if db_mode is not None:
        return db_mode

    # 2. Env override (first-boot fallback + the env-only 'shadow' option).
    raw = os.getenv("VETO_LAYER_MODE")
    if raw is not None and raw.strip() != "":
        val = raw.strip().lower()
        if val in VALID_VETO_MODES:
            return val
        logger.warning("signal_review: unknown VETO_LAYER_MODE=%r; falling back to default", raw)

    # 3. Mode-aware default.
    if (effective_mode or "").strip().lower() == "sandbox":
        return "active"
    return "shadow"


def _cache_ttl_seconds() -> float:
    return _env_float("VETO_CACHE_TTL_SECONDS", 300.0)


def _claude_timeout_seconds() -> float:
    return _env_float("VETO_CLAUDE_TIMEOUT_SECONDS", 25.0)


# ---------------------------------------------------------------------------
# In-process review cache
# ---------------------------------------------------------------------------

_cache_lock = threading.Lock()
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def _cache_key(symbol: str, source: str, direction: str | None = None) -> tuple[str, str, str]:
    # Direction is part of the key: the BUY and SELL legs share the same
    # ``source`` string (both chartink legs POST to one webhook), so without
    # it a long review could be reused for the opposite-side short candidate.
    return (symbol.upper(), source.lower(), (direction or "").upper())


def get_review_cache(
    symbol: str, source: str, direction: str | None = None
) -> dict[str, Any] | None:
    """Return a cached decision dict for (symbol, source, direction) if fresh, else None."""
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    key = _cache_key(symbol, source, direction)
    with _cache_lock:
        entry = _cache.get(key)
        if entry is None:
            return None
        stored_at, decision = entry
        if (time.time() - stored_at) >= ttl:
            _cache.pop(key, None)
            return None
        return dict(decision)


def _store_in_cache(
    symbol: str, source: str, decision: dict[str, Any], direction: str | None = None
) -> None:
    key = _cache_key(symbol, source, direction)
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
    success, response, _ = get_quotes(symbol="INDIAVIX", exchange="NSE_INDEX", api_key=api_key)
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
        "regime_snapshot": None,
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

    try:
        ctx["regime_snapshot"] = _fetch_regime_snapshot()
    except Exception as exc:
        logger.warning("signal_review: regime_snapshot fetch failed: %s", exc)
        ctx["regime_snapshot"] = None

    return ctx


def _fetch_regime_snapshot() -> dict[str, Any] | None:
    """Return a compact regime dict for the veto prompt.

    Uses the cached regime (refreshed at most every 5 min by
    ``market_regime_service.get_cached_regime``) so this call is cheap
    even on a tight signal flow. Returns ``None`` if the classifier
    failed (cache miss + recompute exception).

    Trims ``raw_metrics['sector_rotation']['sector_pct']`` to the top-5
    by absolute % change to keep the prompt size bounded — the LLM
    can still reason about leaders + laggards without seeing every
    tracked sector.
    """
    from services.market_regime_service import get_cached_regime

    regime = get_cached_regime(max_age_minutes=5)
    if regime is None:
        return None

    sector_raw = (regime.raw_metrics or {}).get("sector_rotation", {}) or {}
    sector_pct = sector_raw.get("sector_pct") or {}
    top_sectors: dict[str, float] = {}
    if isinstance(sector_pct, dict) and sector_pct:
        ranked = sorted(
            sector_pct.items(),
            key=lambda kv: abs(float(kv[1] or 0.0)),
            reverse=True,
        )
        top_sectors = {sym: float(pct) for sym, pct in ranked[:5]}

    return {
        "trend": regime.trend,
        "volatility": regime.volatility,
        "breadth": regime.breadth,
        "time_of_day": regime.time_of_day,
        "sector_leaders": list(regime.sector_leaders),
        "sector_leader_concentration": float(regime.sector_leader_concentration),
        "top_sector_pct": top_sectors,
    }


# ---------------------------------------------------------------------------
# Review prompt + decision-block parsing (ported from bridge/server.py)
# ---------------------------------------------------------------------------

REVIEW_PROMPT_TEMPLATE = """You are reviewing a candidate trading signal for an Indian F&O intraday strategy.

CANDIDATE:
- Symbol: {symbol}
- Source: {source}
- Direction: {direction}        # BUY = long entry | SELL = short entry
- Time: {candidate_at}

OPERATOR CONTEXT:
- Current positions: {positions_count} ({positions_summary})
- P&L today: ₹{pnl_today}
- Trades today: {trades_today}/{max_trades_today}

MARKET CONTEXT (today):
- NIFTY return: {nifty_pct}%
- India VIX: {india_vix}

REGIME SNAPSHOT:
{regime_block}

Decide whether the operator should take this signal. Use the stated Direction above — do NOT infer it from the Source string. Be conservative: skip when the broader market regime conflicts with the signal's stated Direction. For a BUY (long): skip on a -1% NIFTY day with elevated VIX, or when the stock's sector is bottom-3 today while leadership is concentrated elsewhere. For a SELL (short) the conflict is inverted: a strongly bullish regime / broad green breadth works against a short, while a weak/bearish regime is favourable for it. Also skip when the operator is already near their daily trade limit.

Respond with a short reasoning paragraph followed by a final JSON block. The JSON block must be the LAST thing in your response and must contain exactly these keys:

{{
  "decision": "take" | "skip",
  "reasoning": "1-2 sentence summary",
  "confidence": 0.0 to 1.0
}}
"""


def _format_regime_block(regime: dict | None) -> str:
    """Render the regime snapshot dict as a small bullet list.

    Returns ``"- unavailable"`` when the classifier didn't return data
    (cache miss or empty universe) so the LLM treats it as missing
    rather than zero. Only includes fields that are actually populated.
    """
    if not regime or not isinstance(regime, dict):
        return "- unavailable"
    lines: list[str] = []
    for key in ("trend", "volatility", "breadth", "time_of_day"):
        val = regime.get(key)
        if val:
            lines.append(f"- {key}: {val}")
    leaders = regime.get("sector_leaders") or []
    if leaders:
        lines.append(f"- sector_leaders (top 3): {', '.join(leaders)}")
    concentration = regime.get("sector_leader_concentration")
    if concentration is not None:
        lines.append(
            f"- sector_leader_concentration: {float(concentration):.2f} "
            "(>0.5 dominant, <0.2 broad rotation)"
        )
    top_sector_pct = regime.get("top_sector_pct") or {}
    if top_sector_pct:
        rendered = ", ".join(f"{sym} {float(pct):+.2f}%" for sym, pct in top_sector_pct.items())
        lines.append(f"- top sectors today: {rendered}")
    if not lines:
        return "- unavailable"
    return "\n".join(lines)


def _format_review_prompt(candidate: dict[str, Any], ctx: dict[str, Any]) -> str:
    """Interpolate the review prompt template.

    Operator-state fields (positions, trades) render missing values as
    ``unknown``. The three macro/portfolio numbers (nifty_pct, india_vix,
    pnl_today) instead render as ``unavailable`` — they're best-effort live
    fetches and the LLM should treat their absence as "data not retrievable",
    not "value is zero".
    """

    def _or_unknown(value: Any) -> str:
        if value is None:
            return "unknown"
        return str(value)

    def _or_unavailable(value: Any) -> str:
        if value is None:
            return "unavailable"
        return str(value)

    return REVIEW_PROMPT_TEMPLATE.format(
        symbol=candidate.get("symbol"),
        source=candidate.get("source"),
        direction=_or_unknown(candidate.get("direction")),
        candidate_at=candidate.get("candidate_at"),
        positions_count=_or_unknown(ctx.get("positions_count")),
        positions_summary=_or_unknown(ctx.get("positions_summary")),
        pnl_today=_or_unavailable(ctx.get("pnl_today")),
        trades_today=_or_unknown(ctx.get("trades_today")),
        max_trades_today=_or_unknown(ctx.get("max_trades_today")),
        nifty_pct=_or_unavailable(ctx.get("nifty_pct")),
        india_vix=_or_unavailable(ctx.get("india_vix")),
        regime_block=_format_regime_block(ctx.get("regime_snapshot")),
    )


def _extract_decision_block(text: str) -> dict | None:
    """Find the LAST balanced ``{...}`` block in ``text`` containing ``"decision"``.

    Walks every ``{`` position, follows brace depth with quote/escape awareness,
    and keeps the rightmost successfully-parsed block whose object has a
    ``decision`` key. Returns ``None`` if no such block is present.
    """
    if not text:
        return None

    candidates: list[dict] = []
    n = len(text)
    for start_match in re.finditer(r"\{", text):
        start = start_match.start()
        depth = 0
        in_string = False
        escape = False
        for i in range(start, n):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    block = text[start : i + 1]
                    try:
                        parsed = json.loads(block)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict) and "decision" in parsed:
                        candidates.append(parsed)
                    break
    return candidates[-1] if candidates else None


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
    direction: str | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ask the LLM whether the operator should take this signal.

    ``direction`` (``'BUY'``/``'SELL'``, i.e. ``EntrySignal.action``) is the
    actual side the engine armed. It is passed explicitly because the ``source``
    string is the same for both chartink legs ("...intraday_buy") — without an
    explicit direction the reviewer cannot tell a short candidate from a long
    one. It rides the request body, the cache key, and the audit row.

    Returns a dict with ``decision`` (``'take' | 'skip'``), ``reasoning``,
    ``confidence``, ``id`` (signal_decision row id), ``enforcement_mode``, and
    a few diagnostic fields. The row is always written, including for the
    fail-safe paths — that way the audit log can distinguish a real ``take``
    from a reviewer-failed fallback (the row's ``decision`` will be
    ``'review_failed'`` in that case, even though the returned ``decision``
    key is ``'take'`` for the engine's convenience).
    """
    enforcement_mode = get_veto_layer_mode()

    # Cache check — same (symbol, source, direction) within TTL reuses the prior
    # decision.
    cached = get_review_cache(symbol, source, direction)
    if cached is not None:
        # Still record an audit row so we can see cache hits in the table,
        # but tag the reasoning so it's distinguishable from a fresh review.
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            direction=direction,
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

    candidate = {
        "symbol": symbol,
        "source": source,
        "direction": direction,
        "candidate_at": _now_ist_iso(),
    }
    prompt = _format_review_prompt(candidate, snapshot)
    timeout = _claude_timeout_seconds()

    # In-process claude -p invocation (Phase 1 of #266). Any failure — timeout,
    # missing binary, non-zero exit, unparseable output — falls safe to 'take'
    # with a tagged reasoning, exactly like the retired bridge transport did.
    started = time.time()
    model_text: str | None = None
    session_id = ""
    failure_reason: str | None = None
    try:
        model_text, session_id = invoke_claude_review(prompt, timeout)
    except TimeoutError:
        failure_reason = "claude_timeout"
    except FileNotFoundError:
        failure_reason = "claude_cli_missing"
    except Exception as exc:
        logger.exception("signal_review: unexpected error invoking claude review")
        failure_reason = f"claude_error:{type(exc).__name__}"

    bridge_latency = int((time.time() - started) * 1000)

    if failure_reason is not None:
        logger.warning(
            "signal_review: claude review failed (%s); failing safe to take", failure_reason
        )
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            direction=direction,
            decision="review_failed",
            reasoning=failure_reason,
            confidence=0.0,
            enforcement_mode=enforcement_mode,
            context_snapshot=snapshot,
            bridge_latency_ms=bridge_latency,
            bridge_session_id=session_id or None,
            raw_bridge_output=model_text,
        )
        result = _failsafe_decision(failure_reason)
        result["id"] = decision_id
        result["enforcement_mode"] = enforcement_mode
        result["latency_ms"] = bridge_latency
        return result

    # Parse the decision block out of the model's prose. Missing/malformed →
    # treat as a review failure rather than trusting bogus output.
    raw_output = model_text or ""
    decision_block = _extract_decision_block(raw_output)
    if decision_block is None:
        logger.warning("signal_review: could not parse a decision block from claude output")
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            direction=direction,
            decision="review_failed",
            reasoning="parse_failed",
            confidence=0.0,
            enforcement_mode=enforcement_mode,
            context_snapshot=snapshot,
            bridge_latency_ms=bridge_latency,
            bridge_session_id=session_id or None,
            raw_bridge_output=raw_output,
        )
        result = _failsafe_decision("parse_failed")
        result["id"] = decision_id
        result["enforcement_mode"] = enforcement_mode
        result["latency_ms"] = bridge_latency
        return result

    decision = decision_block.get("decision")
    reasoning = decision_block.get("reasoning", "")
    confidence = decision_block.get("confidence", 0.0)
    if not isinstance(reasoning, str):
        reasoning = str(reasoning)
    try:
        confidence = float(confidence) if confidence is not None else 0.0
    except (TypeError, ValueError):
        confidence = None

    if decision not in ("take", "skip") or confidence is None or not (0.0 <= confidence <= 1.0):
        # Contract violation — decision not in {take, skip} or confidence out of
        # range — fail safe rather than trusting bogus output.
        logger.warning("signal_review: claude returned invalid decision=%r", decision)
        decision_id = _persist_decision(
            symbol=symbol,
            source=source,
            direction=direction,
            decision="review_failed",
            reasoning=f"bad_decision:{decision!r}",
            confidence=0.0,
            enforcement_mode=enforcement_mode,
            context_snapshot=snapshot,
            bridge_latency_ms=bridge_latency,
            bridge_session_id=session_id or None,
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
        direction=direction,
        decision=decision,
        reasoning=reasoning,
        confidence=float(confidence) if confidence is not None else None,
        enforcement_mode=enforcement_mode,
        context_snapshot=snapshot,
        bridge_latency_ms=bridge_latency,
        bridge_session_id=session_id,
        raw_bridge_output=raw_output,
    )

    # Fire a Telegram alert for fresh veto-skip decisions. This is the only
    # path that fires alerts — cache hits (line ~335) and review_failed /
    # bad_decision paths intentionally do not, to keep the operator's signal
    # tied to fresh LLM blocks. The helper itself no-ops for decision != 'skip',
    # so 'take' decisions are silently filtered there.
    try:
        from services.notification_service import publish_veto_decision_alert

        publish_veto_decision_alert(
            symbol=symbol,
            decision=decision,
            reasoning=reasoning,
            confidence=float(confidence) if confidence is not None else None,
            enforcement_mode=enforcement_mode,
            source=source,
        )
    except Exception:
        logger.exception("Failed to publish veto decision alert")

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
        direction,
    )

    return fresh_result


def _persist_decision(
    *,
    symbol: str,
    source: str,
    direction: str | None = None,
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
            direction=direction,
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
