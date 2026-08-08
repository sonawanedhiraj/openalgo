"""API endpoints for the Strategies Dashboard (Tier 2).

GET endpoints consumed by the React /strategies page:

  GET /strategies/api/list
      All known strategies with summary metrics (mode, deployable, today's P&L,
      open position count, last trade timestamp, health LED).

  GET /strategies/api/<name>
      Full detail for one strategy: config snapshot, version log entries,
      mode, active runtime overrides, and 3-column performance comparison
      (Sandbox | Live | Backtest from the config_snapshot parity_target).

  GET /strategies/api/<name>/pnl-curve?window=1d|1w|1m|all
      Daily net P&L time series for the P&L curve chart.

  GET /strategies/api/<name>/parameters/diff?vs=<version>
      Parameter diff between the current config_snapshot and a named version.

  GET /strategies/api/<name>/mode/audit?limit=N
      Recent strategy_mode_audit rows for this strategy (accepted + blocked).
      Used by the UI to surface what happened on past flip attempts.

POST endpoint (issue #162):

  POST /strategies/api/<name>/mode  {"mode": "live" | "sandbox", "notes": "..."}
      Flip the strategy's mode through services.strategy_mode_service.flip_mode().
      Runs the preflight; on block returns 409 with the blocker list; on accept
      writes the strategy_mode row + audit row + publishes the in-process event.
      This is the sanctioned path that replaces raw SQL UPDATE on strategy_mode
      (which produced today's silent 0-orders-in-LIVE incident).

Authentication: Flask session (same as /scanner/api/*, no API key required).
GETs are read-only. POST /mode mutates strategy_mode + strategy_mode_audit only
via the audited service path.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytz
from flask import Blueprint, jsonify, request
from sqlalchemy import text

from database.futures_follow_db import FuturesFollowTrade
from database.futures_follow_db import db_session as ff_session
from database.sector_follow_db import SectorFollowTrade
from database.sector_follow_db import db_session as sf_session
from database.strategy_mode_db import StrategyMode
from database.strategy_mode_db import db_session as mode_session
from database.strategy_runtime_override_db import db_session as override_session
from database.trade_journal_db import TradeJournal, mode_of_row
from database.trade_journal_db import db_session as tj_session
from services.strategy_performance_metrics import compute_realized_metrics
from utils.logging import get_logger
from utils.session import check_session_validity

logger = get_logger(__name__)

strategies_dashboard_bp = Blueprint("strategies_dashboard_bp", __name__, url_prefix="/strategies")

_IST = pytz.timezone("Asia/Kolkata")
_STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"

# Folder↔journal name bridge (issue #235).
# The simplified stock engine lives under the strategies/simplified_engine/ folder
# (config_snapshot + LEARNINGS docs) but journals every trade_journal row under the
# *registered* strategy identity "trending_equity_intraday" — see
# SimplifiedStockEngineService.JOURNAL_STRATEGY_NAME
# (services/simplified_stock_engine_service.py) and strategies/trending_equity_intraday/.
# That registered name is load-bearing across ~28 files (reconciliation, backtest,
# registry, tests), so we bridge the two names HERE at the dashboard query layer
# rather than renaming persisted data. Without this bridge the dashboard queried a
# "simplified_engine" strategy that has no journal rows → 0 positions / 0 P&L.
_SIMPLIFIED_ENGINE_FOLDER = "simplified_engine"
_SIMPLIFIED_ENGINE_JOURNAL_NAME = "trending_equity_intraday"

# trade_journal.direction is 'LONG' | 'SHORT' (database/trade_journal_db.py) —
# unlike open15, which stores 'L' | 'S'. Anything else buckets into neither side.
_SIMPLIFIED_ENGINE_SIDE_KEY = {"LONG": "long", "SHORT": "short"}

# Strategies to surface (read from filesystem, filtered below).
# trending_equity_intraday is the journal-name twin of the simplified_engine folder
# (see the bridge above) — it has no config_snapshot of its own and would otherwise
# show up as an empty duplicate row, so it is hidden; simplified_engine carries it.
_EXCLUDE_NAMES = {
    "examples",
    "scripts",
    "__pycache__",
    "STRATEGY_REGISTRY.md",
    "README.md",
    _SIMPLIFIED_ENGINE_JOURNAL_NAME,
}


# ---------------------------------------------------------------------------
# Helpers — filesystem reads
# ---------------------------------------------------------------------------


def _list_strategy_dirs() -> list[str]:
    """Return strategy names (directories) under strategies/, excluding noise."""
    if not _STRATEGIES_DIR.exists():
        return []
    return sorted(
        d.name
        for d in _STRATEGIES_DIR.iterdir()
        if d.is_dir() and d.name not in _EXCLUDE_NAMES and not d.name.startswith(".")
    )


def _load_config_snapshot(name: str) -> dict:
    """Load strategies/<name>/config_snapshot.json; return {} on missing."""
    p = _STRATEGIES_DIR / name / "config_snapshot.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Failed to read config_snapshot for %s", name)
        return {}


def _load_version_log(name: str) -> list[dict]:
    """Parse strategies/<name>/VERSION_LOG.md into a list of {version, date, body}."""
    p = _STRATEGIES_DIR / name / "VERSION_LOG.md"
    if not p.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
        # Each entry starts with "## vX.Y.Z — YYYY-MM-DD"
        entries = []
        pattern = re.compile(r"^## (v[\d.]+)\s*[—–-]\s*(\d{4}-\d{2}-\d{2})", re.MULTILINE)
        matches = list(pattern.finditer(text))
        for i, m in enumerate(matches):
            version = m.group(1)
            date_str = m.group(2)
            body_start = m.end()
            body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            body = text[body_start:body_end].strip()
            entries.append({"version": version, "date": date_str, "body": body})
        return entries
    except Exception:
        logger.exception("Failed to parse VERSION_LOG for %s", name)
        return []


def _list_backtest_refs(name: str) -> list[str]:
    """Return markdown filenames from docs/research/strategy/<name>/."""
    research_dir = Path(__file__).parent.parent / "docs" / "research" / "strategy" / name
    if not research_dir.exists():
        return []
    return sorted(p.name for p in research_dir.glob("*.md"))


# ---------------------------------------------------------------------------
# Helpers — database reads
# ---------------------------------------------------------------------------


def _ist_date_str(created_at: datetime | None) -> str | None:
    """IST calendar date (YYYY-MM-DD) for a trade row's ``created_at`` timestamp.

    ``created_at`` is stored as a naive UTC datetime (``datetime.utcnow`` default),
    so it must be localized to UTC and converted to IST before taking the date —
    this is the *execution* date of the order leg, used to bucket a T+1 exit under
    the day it actually filled (see ``_futures_follow_stats``). Returns None on a
    missing/unparseable value.
    """
    if created_at is None:
        return None
    try:
        return pytz.utc.localize(created_at).astimezone(_IST).strftime("%Y-%m-%d")
    except Exception:
        # Already tz-aware or otherwise odd — best-effort direct format.
        try:
            return created_at.astimezone(_IST).strftime("%Y-%m-%d")
        except Exception:
            return created_at.strftime("%Y-%m-%d")


def _normalize_last_trade_at(value: str | None) -> str | None:
    """Normalize a dashboard ``last_trade_at`` to a **naive-UTC** ISO string.

    Contract (issue #317): the strategy cards render this value as
    ``new Date(last_trade_at + 'Z')`` — i.e. they treat it as UTC. The
    futures/sector paths already emit ``created_at.isoformat()`` from
    ``datetime.utcnow()`` (naive UTC), but the simplified engine's
    ``trade_journal.placed_at`` is a **tz-aware IST** string (``…+05:30``);
    appending ``'Z'`` to that yields ``…+05:30Z`` → JS "Invalid Date".

    So: a tz-aware value is converted to naive UTC; a naive value is returned
    unchanged (already the assumed-UTC form). Unparseable input is passed through.
    """
    if not value:
        return value
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return value
    if dt.tzinfo is None:
        return value
    return dt.astimezone(UTC).replace(tzinfo=None).isoformat()


def _get_strategy_mode(name: str) -> str:
    """Return current mode from strategy_mode table, default 'sandbox'."""
    try:
        row = mode_session.query(StrategyMode).filter_by(strategy_name=name).first()
        if row:
            return row.mode
    except Exception:
        logger.exception("Failed to query strategy_mode for %s", name)
    return "sandbox"


# ---------------------------------------------------------------------------
# LLM control (issue #266 Phase 2)
# ---------------------------------------------------------------------------

# Which strategies actually run the Stage-1 LLM veto today. The simplified
# engine calls _run_pre_order_review; futures_follow_cap50 reviews each
# selected signal in run_entry (issue #318, strategy-aware prompt/context).
# sector_follow still has no veto call, so its decisions view is empty by
# construction. The UI notes this rather than faking rows.
_FUTURES_FOLLOW_FOLDER = "futures_follow_cap50"
_INTRADAY_PULLBACK_FOLDER = "intraday_pullback_top2"
_SECTOR_FOLLOW_FOLDER = "sector_follow_cap5_vol"
_VETO_ENABLED_STRATEGIES = {_SIMPLIFIED_ENGINE_FOLDER, _FUTURES_FOLLOW_FOLDER}

# Map a dashboard strategy name → the signal_decision source filters its veto
# rows need (kwargs for list/count/summarize_signal_decisions). The simplified
# engine reviews with source=<chartink strategy label> (e.g.
# "chartink_FnO_intraday_buy", "trend-up"), NOT its folder name — so a clean
# inclusion filter isn't available; its view is ALL rows EXCEPT the strategies
# that tag rows with their own name (R1, #318 — keeps futures_follow rows out).
# futures_follow_cap50 tags rows source='futures_follow_cap50', so its view is
# a clean inclusion filter.
_LLM_DECISION_SOURCES: dict[str, dict[str, list[str]]] = {
    _SIMPLIFIED_ENGINE_FOLDER: {"exclude_sources": [_FUTURES_FOLLOW_FOLDER]},
    _FUTURES_FOLLOW_FOLDER: {"sources": [_FUTURES_FOLLOW_FOLDER]},
}


def _llm_review_fields(d: dict) -> dict:
    """Project a signal_decision row to the compact shape the trades table embeds."""
    return {
        "decision_id": d.get("id"),
        "decision": d.get("decision"),
        "confidence": d.get("confidence"),
        "reasoning": d.get("reasoning"),
        "enforcement_mode": d.get("enforcement_mode"),
        "candidate_at": d.get("candidate_at"),
    }


# Journal direction (LONG/SHORT) → decision direction (BUY/SELL) for the fuzzy join.
_JOURNAL_DIRECTION_TO_DECISION = {"LONG": "BUY", "SHORT": "SELL"}


def _attach_llm_reviews(name: str, recent_trades: list[dict]) -> set[int]:
    """Embed the matched Stage-1 LLM veto decision on each trade row (issue #358).

    Sets ``row['llm']`` (or ``None``) on every row, entry legs only — exits are
    never reviewed. Exact join first (the ``decision_id`` /
    ``signal_decision_id`` FK the services stamp at placement time), then a
    fuzzy fallback for rows that predate the FK: same strategy source filter,
    same symbol (futures_follow journals the SOURCE STOCK in ``signal_id``; the
    decision row's ``symbol`` is that stock), same entry day, direction-compatible.
    Returns the set of decision ids consumed so the unmatched-skip pass can
    exclude them. Fail-graceful: any error leaves the rows untouched.
    """
    matched: set[int] = set()
    if name not in _VETO_ENABLED_STRATEGIES or not recent_trades:
        return matched
    try:
        from database.signal_decision_db import (
            get_signal_decisions_by_ids,
            list_signal_decisions,
        )

        fk_key = "signal_decision_id" if name == _SIMPLIFIED_ENGINE_FOLDER else "decision_id"
        by_id = get_signal_decisions_by_ids(
            [r.get(fk_key) for r in recent_trades if r.get(fk_key) is not None]
        )
        # Fuzzy pool for pre-FK rows: recent decisions under the strategy's
        # source filter, newest first (bounded — one query).
        filters = _LLM_DECISION_SOURCES.get(name) or {}
        fuzzy_pool = list_signal_decisions(limit=500, **filters)

        for row in recent_trades:
            row.setdefault("llm", None)
            if row.get("side") == "SELL":  # exit legs are never reviewed
                continue
            fk = row.get(fk_key)
            if fk is not None and fk in by_id:
                row["llm"] = _llm_review_fields(by_id[fk])
                matched.add(fk)
                continue
            # Fuzzy fallback: symbol + same day (+ direction when both known).
            if name == _FUTURES_FOLLOW_FOLDER:
                sym, day = row.get("signal_id"), row.get("entry_date")
                want_dir = "BUY"
            else:
                sym = row.get("symbol")
                day = (row.get("created_at") or "")[:10]
                want_dir = _JOURNAL_DIRECTION_TO_DECISION.get(row.get("side") or "")
            if not sym or not day:
                continue
            for d in fuzzy_pool:
                if d["id"] in matched:
                    continue
                if d.get("symbol") != sym or (d.get("candidate_at") or "")[:10] != day:
                    continue
                if want_dir and d.get("direction") and d["direction"] != want_dir:
                    continue
                row["llm"] = _llm_review_fields(d)
                matched.add(d["id"])
                break
    except Exception:
        logger.exception("Failed to attach LLM reviews to recent trades for %s", name)
    return matched


def _unmatched_skip_decisions(name: str, matched_ids: set[int], limit: int = 25) -> list[dict]:
    """Enforced LLM skips that produced NO journal row (issue #358).

    These render as pseudo-rows in the merged trades table — a vetoed entry the
    strategy never placed (the simplified engine does not journal vetoed
    entries; futures_follow journals ``veto_skip`` rows, so its skips normally
    arrive matched and are excluded here). Only enforced skips qualify: a
    shadow-mode 'skip' still placed, and its trade row already carries the
    decision.
    """
    if name not in _VETO_ENABLED_STRATEGIES:
        return []
    try:
        from database.signal_decision_db import list_signal_decisions

        filters = _LLM_DECISION_SOURCES.get(name) or {}
        rows = list_signal_decisions(limit=100, **filters)
        out = []
        for d in rows:
            if d["id"] in matched_ids or d.get("decision") != "skip":
                continue
            if d.get("enforcement_mode") != "active" and d.get("actually_taken") is not False:
                continue
            out.append(
                {
                    **_llm_review_fields(d),
                    "symbol": d.get("symbol"),
                    "direction": d.get("direction"),
                }
            )
            if len(out) >= limit:
                break
        return out
    except Exception:
        logger.exception("Failed to fetch unmatched skip decisions for %s", name)
        return []


def _get_llm_mode(name: str) -> str:
    """Return the persistent llm_mode for a strategy, default 'off'.

    Reads the strategy_llm_config table directly (read-only). 'off' is the
    default when no row exists — matches the DB default and the resolver's
    first-boot behavior.
    """
    try:
        from database.strategy_llm_config_db import get_llm_mode

        row = get_llm_mode(name)
        if row and row.get("llm_mode"):
            return row["llm_mode"]
    except Exception:
        logger.exception("Failed to query strategy_llm_config for %s", name)
    return "off"


def _get_active_overrides(name: str) -> list[dict]:
    """Return active (non-expired) runtime override rows for a strategy."""
    try:
        now = datetime.utcnow()
        rows = override_session.execute(
            text(
                "SELECT override_type, reason, expires_at, set_by "
                "FROM strategy_runtime_override "
                "WHERE strategy_name = :name "
                "  AND (expires_at IS NULL OR expires_at > :now)"
            ),
            {"name": name, "now": now},
        ).fetchall()
        return [
            {
                "type": r[0],
                "reason": r[1],
                "expires_at": r[2]
                if isinstance(r[2], str)
                else (r[2].isoformat() if r[2] else None),
                "set_by": r[3],
            }
            for r in rows
        ]
    except Exception:
        logger.exception("Failed to query overrides for %s", name)
        return []


def _sector_follow_stats(since: datetime | None = None) -> dict:
    """Aggregate P&L and position stats from sector_follow_trades.

    Open-position count (issue #353 same-class audit): computed **per-symbol**
    from signed quantity sums over ALL placed rows, not a same-day row-count
    pairing. sector_follow is also a T+1 strategy — a SELL row's
    ``entry_date`` carries the *original entry session* (see
    ``database/sector_follow_db.py`` and the mirrored bucketing note on
    ``_futures_follow_stats``), so the previous ``today_entries vs
    today_exits`` (both filtered on ``entry_date == today``) never actually
    matched a T+1 exit against the entry it closes — a same-day exit's
    ``entry_date`` is always a **prior** day, so ``today_exits`` was
    structurally empty and ``open_count`` only ever grew regardless of actual
    book state.

    Unlike futures_follow (single NIFTY-future instrument, sized in
    lots), sector_follow holds up to 5 *different* equity symbols at varying
    quantities (₹50k/position sizing means qty differs per stock) — there is
    no single "lot size" to divide by, and "open positions" means "distinct
    symbols with a net-long placed quantity". So this sums BUY − SELL
    quantity **within each symbol** and counts a symbol as open when its net
    is positive, rather than dividing a strategy-wide quantity total by a lot
    size (that conversion is futures_follow-specific, see
    ``_lot_size_from_rows``). This also happens to be immune to the same
    batched-exit-row class documented on ``_futures_follow_stats``, should
    sector_follow ever batch an exit the same way.
    """
    try:
        q = sf_session.query(SectorFollowTrade)
        if since:
            q = q.filter(SectorFollowTrade.created_at >= since)
        trades = q.all()
        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        today_entries = [t for t in trades if t.side == "BUY" and t.entry_date == today_str]
        today_exits = [
            t for t in trades if t.side == "SELL" and _ist_date_str(t.created_at) == today_str
        ]
        placed = [t for t in trades if (t.status or "") == "placed"]
        # Issue #562: net WITHIN each mode. Sandbox and live positions are not
        # the same kind of thing and must never be summed into one number — the
        # #552 convention. Before this, the card blended 12 sandbox positions
        # with 7 live ones into a single "Open 10", which is what made a live
        # book impossible to read off the dashboard.
        net_by_mode: dict[str, dict[str, int]] = {}
        for t in placed:
            sign = 1 if t.side == "BUY" else -1
            bucket = net_by_mode.setdefault((t.mode or "sandbox").lower(), {})
            bucket[t.symbol] = bucket.get(t.symbol, 0) + sign * int(t.quantity or 0)
        open_by_mode = {
            mode: sum(1 for qty in symbols.values() if qty > 0)
            for mode, symbols in net_by_mode.items()
        }
        # The headline number is the book the strategy would trade RIGHT NOW,
        # so it answers "what am I exposed to?" rather than "what has this
        # journal ever contained". The per-mode split rides alongside it.
        current_mode = _get_strategy_mode(_SECTOR_FOLLOW_FOLDER)
        open_count = open_by_mode.get(current_mode, 0)
        last = max((t.created_at for t in trades), default=None)
        return {
            "open_positions": open_count,
            "open_positions_by_mode": open_by_mode,
            "open_positions_mode": current_mode,
            "last_trade_at": last.isoformat() if last else None,
            "today_trade_count": len(today_entries) + len(today_exits),
        }
    except Exception:
        logger.exception("Failed to aggregate sector_follow_stats")
        return {
            "open_positions": 0,
            "open_positions_by_mode": {},
            "open_positions_mode": None,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


def _lot_size_from_rows(rows: list) -> int:
    """Derive a display lot size from a set of trade rows (issue #353).

    Uses the smallest *positive* BUY ``quantity`` seen across the rows as the
    per-lot unit, rather than a hardcoded/config lot size — the journal's
    historical rows may not agree with the strategy's current configured lot
    size (e.g. legacy 65-qty rows vs. the post-2024-11-20 75-unit NIFTY lot),
    and a single BUY row is always exactly 1 lot by construction
    (``place_entry`` journals one row per signal at ``lots * lot_size``). This
    is a *display* concern only — it never feeds back into order sizing.
    Falls back to 1 (treat quantity as lot count) when no positive BUY
    quantity exists.
    """
    buy_qtys = [int(t.quantity) for t in rows if t.side == "BUY" and (t.quantity or 0) > 0]
    return min(buy_qtys) if buy_qtys else 1


def _futures_follow_stats(since: datetime | None = None) -> dict:
    """Aggregate P&L and position stats from futures_follow_trades.

    Bucketing note (issue #301): the strategy holds T+1, so an **exit (SELL)** row
    carries the *original entry session* in ``entry_date`` (yesterday), NOT the day
    it filled. Today's realized P&L and trade count must therefore key an exit off
    its **execution timestamp** (``created_at``, IST date), while an **entry (BUY)**
    row is correctly keyed by ``entry_date`` (which IS its execution date). Keying
    exits off ``entry_date`` was the bug that showed ₹0 today despite a profitable
    T+1 sell.

    Open-position count (issue #353): computed from **signed quantity sums**,
    not row counts. A single SELL row can square off more than one BUY lot in
    one order (a batched T+1 exit, e.g. two 65-qty BUYs closed by one 130-qty
    SELL) — row-count parity (``len(BUYs) - len(SELLs)``) then overcounts the
    open book by exactly the number of lots the batched exit covered, showing
    a phantom open position on an otherwise flat book. Summing quantity is
    immune to how many order legs the exit was split across. The quantity
    total is converted to a lot/position count for display using
    ``_lot_size_from_rows`` (see there for why it isn't the hardcoded config
    lot size).
    """
    try:
        q = ff_session.query(FuturesFollowTrade)
        if since:
            q = q.filter(FuturesFollowTrade.created_at >= since)
        trades = q.all()
        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        entries_today = [t for t in trades if t.side == "BUY" and t.entry_date == today_str]
        exits_today = [
            t for t in trades if t.side == "SELL" and _ist_date_str(t.created_at) == today_str
        ]
        today_pnl = sum((t.net_pnl or 0.0) for t in exits_today if t.net_pnl is not None)
        # Open = net placed QUANTITY across ALL sessions (placed BUY qty − placed
        # SELL qty), so an overnight-held position stays counted until its T+1
        # exit fills. (Pairing today's exits against today's entries is wrong:
        # today's SELL closes YESTERDAY's entry, not one of today's.)
        placed = [t for t in trades if (t.status or "") == "placed"]
        open_qty = max(
            0,
            sum(int(t.quantity or 0) for t in placed if t.side == "BUY")
            - sum(int(t.quantity or 0) for t in placed if t.side == "SELL"),
        )
        lot_size = _lot_size_from_rows(placed)
        open_count = (open_qty + lot_size - 1) // lot_size if lot_size else 0
        last = max((t.created_at for t in trades), default=None)
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "last_trade_at": last.isoformat() if last else None,
            "today_trade_count": len(entries_today) + len(exits_today),
        }
    except Exception:
        logger.exception("Failed to aggregate futures_follow_stats")
        return {
            "open_positions": 0,
            "today_net_pnl": 0.0,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


def _simplified_engine_stats() -> dict:
    """Aggregate today's open positions, realized P&L, trade count, and last
    trade time from ``trade_journal`` rows tagged with the simplified engine's
    registered journal name (``trending_equity_intraday``).

    "Open" = ``exited_at IS NULL``; "today" = the IST calendar date matched
    against the ``placed_at`` prefix. ``today_net_pnl`` sums the ``pnl`` of rows
    closed today (``exited_at`` prefix == today). All read-only.
    """
    try:
        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        rows = (
            tj_session.query(TradeJournal)
            .filter(TradeJournal.strategy_name == _SIMPLIFIED_ENGINE_JOURNAL_NAME)
            .all()
        )
        open_count = 0
        today_trade_count = 0
        today_pnl = 0.0
        unpriced_exits = 0
        last_at: str | None = None
        for r in rows:
            placed = r.placed_at or ""
            if placed.startswith(today_str):
                today_trade_count += 1
                if r.exited_at is None:
                    open_count += 1
            if last_at is None or placed > last_at:
                last_at = placed
            exited = r.exited_at or ""
            if exited.startswith(today_str):
                if r.pnl is not None:
                    today_pnl += float(r.pnl)
                else:
                    # Closed today but never priced (e.g. a watchdog exit whose
                    # fill wasn't reconciled yet — issue #350). Surfaced so ₹X
                    # with N unpriced trades can't masquerade as a complete ₹X.
                    unpriced_exits += 1
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "today_unpriced_exits": unpriced_exits,
            # placed_at is a tz-aware IST string; the card appends 'Z', so emit
            # naive-UTC to match futures/sector and avoid "Invalid Date" (#317).
            "last_trade_at": _normalize_last_trade_at(last_at),
            "today_trade_count": today_trade_count,
        }
    except Exception:
        logger.exception("Failed to aggregate simplified_engine_stats")
        return {
            "open_positions": 0,
            "today_net_pnl": 0.0,
            "today_unpriced_exits": 0,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


def _lifetime_from_pnls(pnls: list[float]) -> dict:
    """Since-inception cumulative realized P&L + running win-rate over a list of
    closed-trade net P&L values.

    ``win_rate_pct`` counts a strictly-positive P&L as a win. Both ``cum_net_pnl``
    and ``win_rate_pct`` are ``None`` when there are no closed trades yet, so the
    UI renders '—' rather than a misleading ₹0 / 0%. ``closed_trades`` is the
    realized-trade denominator (pairs against the backtest's ``n_trades``).
    """
    n = len(pnls)
    if n == 0:
        return {"cum_net_pnl": None, "closed_trades": 0, "win_rate_pct": None}
    wins = sum(1 for p in pnls if p > 0)
    return {
        "cum_net_pnl": round(sum(pnls), 2),
        "closed_trades": n,
        "win_rate_pct": round(100.0 * wins / n, 1),
    }


def _futures_follow_lifetime() -> dict[str, dict]:
    """Per-mode (sandbox|live) since-inception realized P&L + win-rate from
    ``futures_follow_trades`` closed exits (SELL rows carrying ``net_pnl``).

    Splitting by the row's own ``mode`` keeps the Sandbox and Live dashboard
    columns honest once the strategy is flipped live — sandbox history never
    leaks into the live column and vice-versa.
    """
    out = {"sandbox": _lifetime_from_pnls([]), "live": _lifetime_from_pnls([])}
    try:
        rows = (
            ff_session.query(FuturesFollowTrade)
            .filter(
                FuturesFollowTrade.side == "SELL",
                FuturesFollowTrade.net_pnl.isnot(None),
            )
            .all()
        )
        buckets: dict[str, list[float]] = {"sandbox": [], "live": []}
        for r in rows:
            m = (r.mode or "").lower()
            if m in buckets:
                buckets[m].append(float(r.net_pnl or 0.0))
        out = {m: _lifetime_from_pnls(p) for m, p in buckets.items()}
    except Exception:
        logger.exception("Failed to aggregate futures_follow lifetime stats")
    return out


def _simplified_engine_lifetime() -> dict[str, dict]:
    """Per-mode (sandbox|live) since-inception realized stats for the simplified
    engine's closed ``trade_journal`` rows (``exited_at`` + ``pnl`` present).

    Splitting on the row's own ``mode`` column (issue #568) is what keeps the
    Live column honest. Previously ``trade_journal`` had no mode and the caller
    attributed the WHOLE journal to whatever mode the strategy sat in today — so
    flipping the engine live would have silently re-labelled 235 sandbox trades
    as live performance. Rows written before the column exists resolve to
    ``sandbox`` via ``mode_of_row``.

    Carries ``long``/``short`` sub-aggregates keyed off the journal's
    ``direction`` column (issue #494) — the two sides of this strategy diverge
    sharply, which the blended headline hides — plus realized CAGR / Sharpe /
    Max DD per mode (issue #568).
    """
    empty = {
        **_lifetime_from_pnls([]),
        **_side_split({}),
        **_simplified_engine_metrics([]),
    }
    out: dict[str, dict] = {"sandbox": dict(empty), "live": dict(empty)}
    try:
        rows = (
            tj_session.query(TradeJournal)
            .filter(
                TradeJournal.strategy_name == _SIMPLIFIED_ENGINE_JOURNAL_NAME,
                TradeJournal.exited_at.isnot(None),
                TradeJournal.pnl.isnot(None),
            )
            .all()
        )
        buckets: dict[str, list] = {"sandbox": [], "live": []}
        for r in rows:
            if _is_non_trade_row(r):
                continue
            bucket = buckets.get(mode_of_row(r))
            if bucket is not None:
                bucket.append(r)

        for mode_key, mode_rows in buckets.items():
            pnls = [float(r.pnl) for r in mode_rows]
            sides: dict[str, list[float]] = {"long": [], "short": []}
            for r in mode_rows:
                side_key = _SIMPLIFIED_ENGINE_SIDE_KEY.get((r.direction or "").upper())
                if side_key:
                    sides[side_key].append(float(r.pnl))
            out[mode_key] = {
                **_lifetime_from_pnls(pnls),
                **_side_split(sides),
                **_simplified_engine_metrics(mode_rows),
            }
        return out
    except Exception:
        logger.exception("Failed to aggregate simplified_engine lifetime stats")
        return out


#: Exit reasons marking rows that are NOT trades — data-repair tombstones left by
#: the 2026-06-11 pytest-pollution cleanup. They carry ``pnl=0.0``, so counting
#: them scores 28 phantom rows as losses and drags the reported win rate down
#: (issue #568). Matched by prefix so a future cleanup tag is excluded too.
_NON_TRADE_EXIT_PREFIXES = ("phantom_cleanup",)


def _is_non_trade_row(row) -> bool:
    return str(row.exit_reason or "").startswith(_NON_TRADE_EXIT_PREFIXES)


def _simplified_engine_metrics(rows: list) -> dict:
    """Realized CAGR / Sharpe / Max DD for a set of closed journal rows.

    The capital basis is read from the strategy's own ``config_snapshot.json``
    so it tracks the declared config instead of being duplicated here. It is
    flagged ``notional`` because R56 is explicit that the engine's ₹20,000 is a
    per-trade risk-sizing base rather than a compounding book — the maths is
    still run (operator's call), but the caveat rides along to the UI instead of
    being silently dropped.
    """
    daily: list[tuple[date, float]] = []
    for r in rows:
        day = _ist_date_of(r.exited_at or r.placed_at)
        if day is not None:
            daily.append((day, float(r.pnl or 0.0)))

    capital = None
    try:
        capital = float(
            (_load_config_snapshot(_SIMPLIFIED_ENGINE_FOLDER).get("config") or {}).get("capital")
            or 0
        )
    except Exception:
        logger.exception("simplified_engine: could not read capital basis from config_snapshot")

    return compute_realized_metrics(
        daily,
        capital_inr=capital,
        capital_is_notional=True,
    )


def _ist_date_of(ts: str | None) -> date | None:
    """Calendar (IST) date of an ISO journal timestamp, or None if unparseable.

    Journal timestamps are written with an explicit +05:30 offset, so the date
    component is already IST and is taken directly — no tz conversion that could
    shift a 15:2x exit onto the previous day.
    """
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts)).date()
    except Exception:
        return None


def _open15_net_pnl(row) -> float:
    """Net P&L for a closed ``open15_trades`` row.

    The journal ``pnl`` is gross (trigger price -> exit price); the modelled MIS
    round-trip charges live separately in ``charges_inr`` (issue #433). Deduct
    them when stamped — same convention as the Recent Trades table's ``net_pnl``.

    Delegates to the journal's own helper so this file cannot drift from it
    again (issue #552): a local copy here is exactly how the logs page ended up
    reporting gross while this dashboard reported net.
    """
    from database.open15_breakout_db import net_pnl_of_row

    return net_pnl_of_row(row)


def _open15_stats() -> dict:
    """Today's open positions, realized net P&L, trade count, and last trade
    time from ``open15_trades`` (issue #442).

    The strategy is intraday (entry and its 09:30 flatten share ``trade_date``),
    so "today" keys directly off ``trade_date``. ``observe``-mode rows are
    journal-only dry runs (no orders placed) and are excluded so they can't
    inflate the book. ``created_at`` is naive UTC (``datetime.utcnow`` default),
    which is exactly the card's ``last_trade_at`` contract (issue #317).
    """
    try:
        from database.open15_breakout_db import Open15Trade
        from database.open15_breakout_db import db_session as o15_session

        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        rows = (
            o15_session.query(Open15Trade).filter(Open15Trade.mode.in_(("sandbox", "live"))).all()
        )
        open_count = 0
        today_trade_count = 0
        today_pnl = 0.0
        today_unpriced_exits = 0
        last_at: datetime | None = None
        for r in rows:
            if (r.status or "") == "open":
                open_count += 1
            if r.created_at is not None and (last_at is None or r.created_at > last_at):
                last_at = r.created_at
            if r.trade_date != today_str:
                continue
            today_trade_count += 1
            if (r.status or "") == "closed":
                if r.pnl is not None:
                    today_pnl += _open15_net_pnl(r)
                else:
                    # Closed but never priced (e.g. exit tick unavailable at
                    # flatten) — surfaced so ₹X with N unpriced trades can't
                    # masquerade as a complete ₹X (same contract as #350).
                    today_unpriced_exits += 1
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "today_unpriced_exits": today_unpriced_exits,
            "last_trade_at": last_at.isoformat() if last_at else None,
            "today_trade_count": today_trade_count,
        }
    except Exception:
        logger.exception("Failed to aggregate open15 stats")
        return {
            "open_positions": 0,
            "today_net_pnl": 0.0,
            "today_unpriced_exits": 0,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


_OPEN15_SIDE_KEY = {"L": "long", "S": "short"}


def _side_split(pnls_by_side: dict[str, list[float]]) -> dict[str, dict]:
    """Long/short sub-aggregates for the performance table (issue #458).

    Uniform shape across all three columns: ``{n_trades, wins, win_rate_pct,
    net_pnl_inr}``. A side with no closed trades keeps ``n_trades=0`` and
    ``None`` stats so the UI renders '—' rather than a misleading 0%.
    """
    out: dict[str, dict] = {}
    for key in ("long", "short"):
        pnls = pnls_by_side.get(key) or []
        n = len(pnls)
        wins = sum(1 for p in pnls if p > 0)
        out[key] = {
            "n_trades": n,
            "wins": wins,
            "win_rate_pct": round(100.0 * wins / n, 1) if n else None,
            "net_pnl_inr": round(sum(pnls), 2) if n else None,
        }
    return out


def _open15_lifetime() -> dict[str, dict]:
    """Per-mode (sandbox|live) since-inception realized net P&L + win-rate from
    closed ``open15_trades`` rows (issue #442).

    Splitting by the row's own ``mode`` keeps the Sandbox and Live dashboard
    columns honest once the strategy is flipped live — sandbox history never
    leaks into the live column and vice-versa (futures_follow precedent).
    ``observe`` rows fall outside both buckets by construction. Each mode also
    carries ``long``/``short`` sub-aggregates keyed off the journal's ``side``
    column (issue #458).
    """
    out = {
        "sandbox": {**_lifetime_from_pnls([]), **_side_split({})},
        "live": {**_lifetime_from_pnls([]), **_side_split({})},
    }
    try:
        from database.open15_breakout_db import Open15Trade
        from database.open15_breakout_db import db_session as o15_session

        rows = (
            o15_session.query(Open15Trade)
            .filter(Open15Trade.status == "closed", Open15Trade.pnl.isnot(None))
            .all()
        )
        buckets: dict[str, list[float]] = {"sandbox": [], "live": []}
        sides: dict[str, dict[str, list[float]]] = {
            "sandbox": {"long": [], "short": []},
            "live": {"long": [], "short": []},
        }
        for r in rows:
            m = (r.mode or "").lower()
            if m not in buckets:
                continue
            pnl = _open15_net_pnl(r)
            buckets[m].append(pnl)
            side_key = _OPEN15_SIDE_KEY.get((r.side or "").upper())
            if side_key:
                sides[m][side_key].append(pnl)
        out = {m: {**_lifetime_from_pnls(p), **_side_split(sides[m])} for m, p in buckets.items()}
    except Exception:
        logger.exception("Failed to aggregate open15 lifetime stats")
    return out


def _open15_opt_shadow() -> dict[str, dict | None]:
    """Per-mode (sandbox|live) aggregate of the 1-lot ATM option shadow (#435).

    Closed stock-instrument rows carry ``opt_pnl`` (net of modelled option
    charges, 1 lot). Option-MODE rows are excluded — their real fills already
    ARE the mode's P&L, not a shadow. Returns ``None`` for a mode with no
    priced shadow rows so the UI renders '—' instead of a misleading 0.
    """
    out: dict[str, dict | None] = {"sandbox": None, "live": None}
    try:
        from database.open15_breakout_db import Open15Trade
        from database.open15_breakout_db import db_session as o15_session

        rows = (
            o15_session.query(Open15Trade)
            .filter(Open15Trade.status == "closed", Open15Trade.opt_pnl.isnot(None))
            .all()
        )
        buckets: dict[str, list[float]] = {"sandbox": [], "live": []}
        sides: dict[str, dict[str, list[float]]] = {
            "sandbox": {"long": [], "short": []},
            "live": {"long": [], "short": []},
        }
        for r in rows:
            if r.instrument not in (None, "stock"):
                continue
            m = (r.mode or "").lower()
            if m not in buckets:
                continue
            pnl = float(r.opt_pnl)
            buckets[m].append(pnl)
            side_key = _OPEN15_SIDE_KEY.get((r.side or "").upper())
            if side_key:
                sides[m][side_key].append(pnl)
        for m, pnls in buckets.items():
            if pnls:
                wins = sum(1 for p in pnls if p > 0)
                out[m] = {
                    "n_trades": len(pnls),
                    "win_rate_pct": round(100.0 * wins / len(pnls), 1),
                    "net_pnl_inr": round(sum(pnls), 2),
                    "basis": "1-lot ATM shadow",
                    **_side_split(sides[m]),
                }
    except Exception:
        logger.exception("Failed to aggregate open15 option shadow")
    return out


_INTRADAY_PULLBACK_SIDE_KEY = {"L": "long", "S": "short"}


def _intraday_pullback_net_pnl(row) -> float | None:
    """Net P&L for a closed ``intraday_pullback_trades`` row.

    Unlike ``open15_trades`` (gross ``pnl`` + separate ``charges_inr``), this
    journal stamps ``net_pnl`` directly. Fall back to ``gross_pnl - charges_inr``
    only when the net column was never stamped, and return ``None`` when neither
    is available so the caller can count it as an unpriced exit rather than
    silently booking a ₹0 trade (issue #350 contract).
    """
    if row.net_pnl is not None:
        return float(row.net_pnl)
    if row.gross_pnl is not None:
        return float(row.gross_pnl) - float(row.charges_inr or 0.0)
    return None


def _intraday_pullback_stats() -> dict:
    """Today's open positions, realized net P&L, trade count, and last trade time
    from ``intraday_pullback_trades`` (issue #508).

    The strategy is intraday (entry and its 15:15 flatten share ``trade_date``),
    so "today" keys directly off ``trade_date``. ``observe``-mode rows are
    journal-only dry runs (no orders placed) and are excluded so they can't
    inflate the book — same contract as open15. ``created_at`` is naive UTC
    (``datetime.utcnow`` default), which is exactly the card's ``last_trade_at``
    contract (issue #317).
    """
    try:
        from database.intraday_pullback_db import IntradayPullbackTrade
        from database.intraday_pullback_db import db_session as ip_session

        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        rows = (
            ip_session.query(IntradayPullbackTrade)
            .filter(IntradayPullbackTrade.mode.in_(("sandbox", "live")))
            .all()
        )
        open_count = 0
        today_trade_count = 0
        today_pnl = 0.0
        today_unpriced_exits = 0
        last_at: datetime | None = None
        for r in rows:
            if (r.status or "") == "open":
                open_count += 1
            if r.created_at is not None and (last_at is None or r.created_at > last_at):
                last_at = r.created_at
            if r.trade_date != today_str:
                continue
            today_trade_count += 1
            if (r.status or "") == "closed":
                pnl = _intraday_pullback_net_pnl(r)
                if pnl is not None:
                    today_pnl += pnl
                else:
                    today_unpriced_exits += 1
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "today_unpriced_exits": today_unpriced_exits,
            "last_trade_at": last_at.isoformat() if last_at else None,
            "today_trade_count": today_trade_count,
        }
    except Exception:
        logger.exception("Failed to aggregate intraday_pullback stats")
        return {
            "open_positions": 0,
            "today_net_pnl": 0.0,
            "today_unpriced_exits": 0,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


def _intraday_pullback_lifetime() -> dict[str, dict]:
    """Per-mode (sandbox|live) since-inception realized net P&L + win-rate from
    closed ``intraday_pullback_trades`` rows (issue #508).

    Splitting by the row's own ``mode`` keeps the Sandbox and Live dashboard
    columns honest once the strategy is flipped live — sandbox history never
    leaks into the live column and vice-versa (futures_follow / open15
    precedent). ``observe`` rows fall outside both buckets by construction.

    Each mode also carries ``long``/``short`` sub-aggregates keyed off the
    journal's ``side`` column (``L``/``S``). The split matters here more than
    elsewhere: the two books are mutually exclusive by day gate (NIFTY up →
    long, NIFTY down → short) and the deep-loser short is the unvalidated,
    slippage-fragile leg — a blended headline hides which book is working.
    """
    out = {
        "sandbox": {**_lifetime_from_pnls([]), **_side_split({})},
        "live": {**_lifetime_from_pnls([]), **_side_split({})},
    }
    try:
        from database.intraday_pullback_db import IntradayPullbackTrade
        from database.intraday_pullback_db import db_session as ip_session

        rows = (
            ip_session.query(IntradayPullbackTrade)
            .filter(IntradayPullbackTrade.status == "closed")
            .all()
        )
        buckets: dict[str, list[float]] = {"sandbox": [], "live": []}
        sides: dict[str, dict[str, list[float]]] = {
            "sandbox": {"long": [], "short": []},
            "live": {"long": [], "short": []},
        }
        for r in rows:
            m = (r.mode or "").lower()
            if m not in buckets:
                continue
            pnl = _intraday_pullback_net_pnl(r)
            if pnl is None:
                continue
            buckets[m].append(pnl)
            side_key = _INTRADAY_PULLBACK_SIDE_KEY.get((r.side or "").upper())
            if side_key:
                sides[m][side_key].append(pnl)
        out = {m: {**_lifetime_from_pnls(p), **_side_split(sides[m])} for m, p in buckets.items()}
    except Exception:
        logger.exception("Failed to aggregate intraday_pullback lifetime stats")
    return out


def _pnl_curve_simplified_engine(window_days: int | None) -> list[dict]:
    """Daily realized-P&L series from ``trade_journal`` rows for the simplified
    engine (closed rows carry ``pnl``; the date key is the ``exited_at`` IST
    calendar date). Read-only.
    """
    try:
        q = tj_session.query(TradeJournal).filter(
            TradeJournal.strategy_name == _SIMPLIFIED_ENGINE_JOURNAL_NAME,
            TradeJournal.exited_at.isnot(None),
            TradeJournal.pnl.isnot(None),
        )
        if window_days:
            cutoff = (datetime.now(_IST) - timedelta(days=window_days)).strftime("%Y-%m-%d")
            q = q.filter(TradeJournal.exited_at >= cutoff)
        rows = q.order_by(TradeJournal.exited_at).all()
        by_date: dict[str, float] = {}
        for r in rows:
            d = (r.exited_at or "")[:10]
            if not d:
                continue
            by_date[d] = by_date.get(d, 0.0) + float(r.pnl or 0.0)
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_date.items())]
    except Exception:
        logger.exception("Failed to build pnl_curve for simplified_engine")
        return []


def _pnl_curve_sector_follow(window_days: int | None) -> list[dict]:
    """Daily P&L series from sector_follow_trades (SELL rows carry realized P&L)."""
    try:
        q = sf_session.query(SectorFollowTrade).filter(SectorFollowTrade.side == "SELL")
        if window_days:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            q = q.filter(SectorFollowTrade.created_at >= cutoff)
        rows = q.order_by(SectorFollowTrade.created_at).all()
        by_date: dict[str, float] = {}
        for r in rows:
            d = r.entry_date
            by_date[d] = by_date.get(d, 0.0) + (r.price or 0.0)
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_date.items())]
    except Exception:
        logger.exception("Failed to build pnl_curve for sector_follow")
        return []


def _pnl_curve_futures_follow(window_days: int | None) -> list[dict]:
    """Daily realized-P&L series from futures_follow_trades (net_pnl on exit rows).

    Keyed by the exit's **execution date** (``created_at`` IST) — the day the P&L
    was realized — not ``entry_date`` (the original entry session for a T+1 exit),
    so the curve agrees with the ``today_net_pnl`` shown on the card (issue #301).
    """
    try:
        q = ff_session.query(FuturesFollowTrade).filter(
            FuturesFollowTrade.side == "SELL",
            FuturesFollowTrade.net_pnl.isnot(None),
        )
        if window_days:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            q = q.filter(FuturesFollowTrade.created_at >= cutoff)
        rows = q.order_by(FuturesFollowTrade.created_at).all()
        by_date: dict[str, float] = {}
        for r in rows:
            d = _ist_date_str(r.created_at) or r.entry_date
            by_date[d] = by_date.get(d, 0.0) + (r.net_pnl or 0.0)
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_date.items())]
    except Exception:
        logger.exception("Failed to build pnl_curve for futures_follow")
        return []


def _pnl_curve_intraday_pullback(window_days: int | None) -> list[dict]:
    """Daily realized-net-P&L series from ``intraday_pullback_trades`` (#508).

    The strategy is intraday — entry and its 15:15 flatten share ``trade_date``
    — so the realization date IS ``trade_date``, and no created_at/entry_date
    reconciliation (the futures_follow T+1 problem) applies. ``observe`` rows
    are excluded to match the card's stats.
    """
    try:
        from database.intraday_pullback_db import IntradayPullbackTrade
        from database.intraday_pullback_db import db_session as ip_session

        q = ip_session.query(IntradayPullbackTrade).filter(
            IntradayPullbackTrade.status == "closed",
            IntradayPullbackTrade.mode.in_(("sandbox", "live")),
        )
        if window_days:
            cutoff = datetime.utcnow() - timedelta(days=window_days)
            q = q.filter(IntradayPullbackTrade.created_at >= cutoff)
        rows = q.order_by(IntradayPullbackTrade.created_at).all()
        by_date: dict[str, float] = {}
        for r in rows:
            pnl = _intraday_pullback_net_pnl(r)
            if pnl is None:
                continue
            by_date[r.trade_date] = by_date.get(r.trade_date, 0.0) + pnl
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_date.items())]
    except Exception:
        logger.exception("Failed to build pnl_curve for intraday_pullback")
        return []


# ---------------------------------------------------------------------------
# Health LED
# ---------------------------------------------------------------------------


def _health_led(name: str, overrides: list[dict], config: dict, is_live: bool = False) -> str:
    """Return 'healthy' | 'paused' | 'scaffold' | 'unknown'.

    ``is_live`` is the resolved routing from the ``strategy_mode`` row. It wins
    over the static ``config_snapshot.json`` (issue #561): a strategy that is
    actually routing orders to the real broker is never a "scaffold", whatever
    a stale JSON file claims. Defaults False so existing callers are unchanged.
    """
    if any(o["type"] in ("pause", "kill_switch") for o in overrides):
        return "paused"
    if is_live:
        return "healthy"
    mode_val = config.get("mode", "")
    if "scaffold" in str(mode_val).lower():
        return "scaffold"
    deployable = config.get("deployable", False)
    if not deployable:
        return "scaffold"
    return "healthy"


# Dashboard folder name -> the strategy_name the data_health_check row is keyed
# under. futures_follow reuses the sector_follow feed (its evaluator is shared),
# so its freshness IS the sector_follow feed's freshness. The simplified engine
# is webhook-driven and runs no data-freshness check, so it has no row.
_DATA_HEALTH_FEED = {
    "sector_follow_cap5_vol": "sector_follow_cap5_vol",
    "futures_follow_cap50": "sector_follow_cap5_vol",
}


def _data_health_summary(name: str) -> dict:
    """Compact latest data-freshness state for the dashboard tile (issue #237).

    Surfaces the most recent ``data_health_check`` row for the strategy's feed:
    ``overall_ok`` + ``check_at`` + a stale-symbol count (+ a short sample). Never
    raises — returns ``{"available": False, ...}`` when the strategy has no feed
    check or the read fails, so the tile renders "no check" rather than erroring.
    """
    feed = _DATA_HEALTH_FEED.get(name)
    if feed is None:
        return {"available": False, "reason": "no_feed_check"}
    try:
        from database.data_health_db import get_latest_check

        row = get_latest_check(feed)
    except Exception:
        logger.exception("data_health tile: get_latest_check failed for %s", name)
        return {"available": False, "reason": "read_error"}
    if not row:
        return {"available": False, "reason": "no_check_yet", "feed": feed}
    stale = row.get("stale_symbols") or []
    return {
        "available": True,
        "feed": feed,
        "shared": feed != name,
        "overall_ok": bool(row.get("overall_ok")),
        "check_at": row.get("check_at"),
        "stale_count": len(stale),
        "stale_symbols": stale[:10],
    }


# ---------------------------------------------------------------------------
# Strategy summary builder
# ---------------------------------------------------------------------------


def _build_summary(name: str) -> dict:
    config = _load_config_snapshot(name)
    mode_val = _get_strategy_mode(name)
    overrides = _get_active_overrides(name)

    stats: dict = {}
    if name == "sector_follow_cap5_vol":
        stats = _sector_follow_stats()
    elif name == "futures_follow_cap50":
        stats = _futures_follow_stats()
    elif name == _SIMPLIFIED_ENGINE_FOLDER:
        stats = _simplified_engine_stats()
    elif name == "open15_vol_breakout":
        stats = _open15_stats()
    elif name == _INTRADAY_PULLBACK_FOLDER:
        stats = _intraday_pullback_stats()

    # Effective order routing RIGHT NOW (issue #440): the per-strategy
    # dispatch verdict an order would get — 'live' only when the navbar is on
    # Live AND this strategy's row says live. Lets the UI show routing truth
    # next to the toggle (e.g. "live" toggle but "sandbox (Analyze on)").
    try:
        from services.mode_service import resolve_order_mode

        effective_routing = resolve_order_mode(name).value
    except Exception:
        logger.exception("resolve_order_mode failed for %s", name)
        effective_routing = "sandbox"

    # Issue #561: `deployable` comes from the STATIC config_snapshot.json and is
    # advisory metadata only — it gates the sandbox→live direction, never the
    # live→sandbox one, and it must never decide what routing the card DISPLAYS.
    # sector_follow_cap5_vol shipped `deployable: false, mode: "scaffold-only"`
    # in June and kept it while the strategy routed live to Zerodha from
    # 2026-07-29; the card rendered "Scaffold" with no off-switch because the UI
    # trusted this file over the strategy_mode row that actually drives dispatch.
    # `config_conflict` names that disagreement so the UI can show it instead of
    # silently preferring the JSON.
    deployable = bool(config.get("deployable", False))
    config_declared_mode = config.get("mode")
    config_conflict = bool(
        (mode_val == "live" or effective_routing == "live")
        and (not deployable or "scaffold" in str(config_declared_mode or "").lower())
    )

    return {
        "name": name,
        "display_name": name.replace("_", " ").title(),
        "mode": mode_val,
        "effective_routing": effective_routing,
        "llm_mode": _get_llm_mode(name),
        "llm_veto_enabled": name in _VETO_ENABLED_STRATEGIES,
        "deployable": deployable,
        "config_declared_mode": config_declared_mode,
        "config_conflict": config_conflict,
        "version": config.get("version", "—"),
        "open_positions": stats.get("open_positions", 0),
        # Issue #562: sandbox and live position counts are reported separately —
        # never summed (the #552 convention). Absent for strategies that do not
        # yet split, so the UI falls back to the single headline number.
        "open_positions_by_mode": stats.get("open_positions_by_mode"),
        "open_positions_mode": stats.get("open_positions_mode"),
        "today_net_pnl": stats.get("today_net_pnl", None),
        "today_unpriced_exits": stats.get("today_unpriced_exits", 0),
        "today_trade_count": stats.get("today_trade_count", 0),
        "last_trade_at": stats.get("last_trade_at"),
        "active_overrides": overrides,
        "health": _health_led(name, overrides, config, is_live=(mode_val == "live")),
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@strategies_dashboard_bp.route("/api/list", methods=["GET"])
@check_session_validity
def list_strategies():
    """All known strategies with summary metrics."""
    names = _list_strategy_dirs()
    result = []
    for name in names:
        try:
            result.append(_build_summary(name))
        except Exception:
            logger.exception("Failed to build summary for strategy %s", name)
            result.append({"name": name, "display_name": name, "error": True})
    return jsonify({"status": "success", "data": result})


@strategies_dashboard_bp.route("/api/<name>", methods=["GET"])
@check_session_validity
def strategy_detail(name: str):
    """Full detail for one strategy."""
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    config = _load_config_snapshot(name)
    mode_val = _get_strategy_mode(name)
    overrides = _get_active_overrides(name)
    version_log = _load_version_log(name)
    backtest_refs = _list_backtest_refs(name)

    # 3-column performance data.
    # NB: `or {}` (not a .get default) — a config_snapshot with an explicit
    # `"parity_target": null` returns None from .get and crashed the endpoint
    # with AttributeError (open15_vol_breakout, 2026-07-20).
    parity = config.get("parity_target") or {}
    # Optional instrument-variant sub-object (issue #455): a backtest that also
    # priced the ATM-option leg records it under parity_target.options_variant;
    # normalized here so the UI's paired stock/options cells can render it.
    opt_variant = parity.get("options_variant") or {}
    bt_options = (
        {
            "n_trades": opt_variant.get("n_trades"),
            "win_rate_pct": opt_variant.get("win_rate_pct"),
            "net_pnl_inr": opt_variant.get("net_pnl_inr"),
            "max_dd_pct": opt_variant.get("max_dd_pct"),
            "basis": "fit-to-capital lots",
            # Optional long/short sub-aggregates (issue #458) — pass-through
            # from the config snapshot; absent for strategies without a split.
            "long": opt_variant.get("long"),
            "short": opt_variant.get("short"),
        }
        if opt_variant
        else None
    )
    performance = {
        "backtest": {
            # NB: `cagr_pct` must NOT fall back to `sharpe_daily` (issue #568).
            # It used to, which rendered sector_follow_cap5_vol's Sharpe of 2.19
            # as a CAGR of "2.19%" — a wrong number that looks real, which is
            # worse than the '—' an honestly-absent metric produces. `sharpe`
            # keeps its fallback: sharpe_daily IS a Sharpe, just a daily-series
            # one, so it belongs in that row.
            "cagr_pct": parity.get("cagr_pct"),
            "sharpe": parity.get("sharpe") or parity.get("sharpe_daily"),
            "max_dd_pct": parity.get("max_dd_pct"),
            "win_rate_pct": parity.get("win_rate_pct"),
            "n_trades": parity.get("n_trades_window") or parity.get("n_trades"),
            "net_pnl_inr": parity.get("net_pnl_inr"),
            "window": parity.get("window"),
            "options": bt_options,
            "long": parity.get("long"),
            "short": parity.get("short"),
        },
        "sandbox": None,
        "live": None,
    }

    # Sandbox / live stats. Today's open+P&L come from the per-day aggregators;
    # the since-inception cumulative P&L + running win-rate come from the lifetime
    # helpers (issue #323). The Live column is populated only when that mode has
    # realized history, so it stays '—' until the strategy is actually flipped live.
    if name == "futures_follow_cap50":
        stats = _futures_follow_stats()
        lifetime = _futures_follow_lifetime()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "last_trade_at": stats["last_trade_at"],
            **lifetime["sandbox"],
        }
        if lifetime["live"]["closed_trades"] > 0:
            performance["live"] = {**lifetime["live"]}
    elif name == "sector_follow_cap5_vol":
        # sector_follow journals no realized net P&L (its curve uses price as a
        # proxy), so cumulative P&L / win-rate aren't meaningful here — only
        # open positions are surfaced.
        stats = _sector_follow_stats()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "last_trade_at": stats["last_trade_at"],
        }
    elif name == _SIMPLIFIED_ENGINE_FOLDER:
        stats = _simplified_engine_stats()
        lifetime = _simplified_engine_lifetime()
        # Per-mode split off the journal's own `mode` column (issue #568) — the
        # Live column is populated only by genuinely-live rows, so flipping the
        # strategy live no longer re-labels its sandbox history as live P&L.
        # Today's open/P&L counters remain whole-journal and are attached to the
        # currently-routing mode, which is the only one that can still change.
        current = "live" if mode_val == "live" else "sandbox"
        performance["sandbox"] = {**lifetime["sandbox"]}
        if lifetime["live"]["closed_trades"] > 0:
            performance["live"] = {**lifetime["live"]}
        performance[current] = {
            **(performance[current] or lifetime[current]),
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "today_unpriced_exits": stats.get("today_unpriced_exits", 0),
            "last_trade_at": stats["last_trade_at"],
        }
    elif name == "open15_vol_breakout":
        stats = _open15_stats()
        lifetime = _open15_lifetime()
        opt_shadow = _open15_opt_shadow()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "today_unpriced_exits": stats["today_unpriced_exits"],
            "last_trade_at": stats["last_trade_at"],
            **lifetime["sandbox"],
            "options": opt_shadow["sandbox"],
        }
        if lifetime["live"]["closed_trades"] > 0:
            performance["live"] = {**lifetime["live"], "options": opt_shadow["live"]}
    elif name == _INTRADAY_PULLBACK_FOLDER:
        stats = _intraday_pullback_stats()
        lifetime = _intraday_pullback_lifetime()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "today_unpriced_exits": stats["today_unpriced_exits"],
            "last_trade_at": stats["last_trade_at"],
            **lifetime["sandbox"],
        }
        if lifetime["live"]["closed_trades"] > 0:
            performance["live"] = {**lifetime["live"]}

    # Recent trades (last 50)
    recent_trades: list[dict] = []
    if name == "sector_follow_cap5_vol":
        try:
            rows = (
                sf_session.query(SectorFollowTrade)
                .order_by(SectorFollowTrade.created_at.desc())
                .limit(50)
                .all()
            )
            recent_trades = [
                {
                    "id": r.id,
                    "side": r.side,
                    "symbol": r.symbol,
                    "quantity": r.quantity,
                    "price": r.price,
                    "mode": r.mode,
                    "status": r.status,
                    "entry_date": r.entry_date,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)
    elif name == "futures_follow_cap50":
        try:
            rows = (
                ff_session.query(FuturesFollowTrade)
                .order_by(FuturesFollowTrade.created_at.desc())
                .limit(50)
                .all()
            )
            recent_trades = [
                {
                    "id": r.id,
                    "side": r.side,
                    "symbol": r.nifty_symbol,
                    "quantity": r.quantity,
                    "lots": r.lots,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "gross_pnl": r.gross_pnl,
                    "charges_inr": r.charges_inr,
                    "net_pnl": r.net_pnl,
                    "margin_inr": r.margin_inr,
                    "mode": r.mode,
                    "status": r.status,
                    "entry_date": r.entry_date,
                    "signal_id": r.signal_id,
                    "decision_id": getattr(r, "decision_id", None),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)
    elif name == "open15_vol_breakout":
        try:
            from database.open15_breakout_db import Open15Trade
            from database.open15_breakout_db import db_session as o15_session

            rows = o15_session.query(Open15Trade).order_by(Open15Trade.id.desc()).limit(50).all()
            recent_trades = [
                {
                    "id": r.id,
                    "side": r.side,
                    "symbol": r.symbol,
                    "instrument": r.instrument or "stock",
                    "quantity": r.quantity,
                    "entry_price": r.trigger_price,
                    "exit_price": r.exit_price,
                    "charges_inr": r.charges_inr,
                    # journal pnl is gross (trigger -> exit); net deducts the
                    # modelled MIS round-trip charges when stamped (issue #433)
                    "net_pnl": round(r.pnl - r.charges_inr, 2)
                    if (r.pnl is not None and r.charges_inr is not None)
                    else r.pnl,
                    "mode": r.mode,
                    "status": r.status,
                    "entry_date": r.trade_date,
                    "trigger": f"{r.trigger_minute}:{r.trigger_second:02d}"
                    if r.trigger_minute
                    else None,
                    "exit_ts": r.exit_ts,
                    # ATM option shadow trade (issue #435) — research columns
                    "opt_symbol": r.opt_symbol,
                    "opt_lot_size": r.opt_lot_size,
                    "opt_entry_premium": r.opt_entry_premium,
                    "opt_exit_premium": r.opt_exit_premium,
                    "opt_charges_inr": r.opt_charges_inr,
                    "opt_pnl": r.opt_pnl,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)
    elif name == _INTRADAY_PULLBACK_FOLDER:
        try:
            from database.intraday_pullback_db import IntradayPullbackTrade
            from database.intraday_pullback_db import db_session as ip_session

            rows = (
                ip_session.query(IntradayPullbackTrade)
                .order_by(IntradayPullbackTrade.id.desc())
                .limit(50)
                .all()
            )
            recent_trades = [
                {
                    "id": r.id,
                    "side": r.side,
                    "symbol": r.symbol,
                    "sector": r.sector,
                    "session": r.session,
                    "quantity": r.quantity,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "stop_price": r.stop_price,
                    "exit_reason": r.exit_reason,
                    # `trigger` is the shared table's "Entry Time" cell (named
                    # for open15's mid-bar trigger); here it is the 5m breakout
                    # candle's entry timestamp.
                    "trigger": r.entry_time.strftime("%H:%M:%S") if r.entry_time else None,
                    # gross_pnl is deliberately NOT sent: it flips the shared
                    # table's `hasFinancials` group on, which is labelled for
                    # the NIFTY-futures sleeve ("Buy Price"/"Sell Price" of the
                    # future) and duplicates the Charges column. Gross is
                    # net + charges, both of which ARE shown.
                    "charges_inr": r.charges_inr,
                    # The journal stamps net_pnl directly (charges already
                    # deducted) — unlike open15, no derivation needed here.
                    "net_pnl": _intraday_pullback_net_pnl(r),
                    "mode": r.mode,
                    "status": r.status,
                    "entry_date": r.trade_date,
                    "exit_ts": r.exit_time.isoformat() if r.exit_time else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)
    elif name == _SIMPLIFIED_ENGINE_FOLDER:
        try:
            rows = (
                tj_session.query(TradeJournal)
                .filter(TradeJournal.strategy_name == _SIMPLIFIED_ENGINE_JOURNAL_NAME)
                .order_by(TradeJournal.placed_at.desc())
                .limit(50)
                .all()
            )
            recent_trades = [
                {
                    "id": r.id,
                    "side": r.direction,
                    "symbol": r.symbol,
                    "quantity": r.quantity,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "pnl": r.pnl,
                    "exit_reason": r.exit_reason,
                    "signal_source": r.signal_source,
                    "placed_at": r.placed_at,
                    "exited_at": r.exited_at,
                    "signal_decision_id": r.signal_decision_id,
                    # Normalized aliases so the shared RecentTrades UI renders
                    # P&L / Mode / Status / Time for journal rows too (#358).
                    "net_pnl": r.pnl,
                    "created_at": r.placed_at,
                    "mode": r.signal_source,
                    "status": "closed" if r.exited_at else "open",
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)

    # Issue #358: merged trades + LLM-decisions view. Embed the matched veto
    # decision on each entry row, then surface enforced skips that never
    # journaled as pseudo-rows.
    matched_decision_ids = _attach_llm_reviews(name, recent_trades)
    llm_unmatched_skips = _unmatched_skip_decisions(name, matched_decision_ids)

    # Effective order routing RIGHT NOW (issue #440) — see _build_summary.
    try:
        from services.mode_service import resolve_order_mode

        effective_routing = resolve_order_mode(name).value
    except Exception:
        logger.exception("resolve_order_mode failed for %s", name)
        effective_routing = "sandbox"

    return jsonify(
        {
            "status": "success",
            "data": {
                "name": name,
                "display_name": name.replace("_", " ").title(),
                "mode": mode_val,
                "effective_routing": effective_routing,
                "llm_mode": _get_llm_mode(name),
                "llm_veto_enabled": name in _VETO_ENABLED_STRATEGIES,
                "deployable": bool(config.get("deployable", False)),
                # Issue #561 — same routing-truth contract as /api/list.
                "config_declared_mode": config.get("mode"),
                "config_conflict": bool(
                    (mode_val == "live" or effective_routing == "live")
                    and (
                        not config.get("deployable", False)
                        or "scaffold" in str(config.get("mode") or "").lower()
                    )
                ),
                "version": config.get("version", "—"),
                "config_snapshot": config,
                "active_overrides": overrides,
                "health": _health_led(name, overrides, config, is_live=(mode_val == "live")),
                "data_health": _data_health_summary(name),
                "performance": performance,
                "recent_trades": recent_trades,
                "llm_unmatched_skips": llm_unmatched_skips,
                "version_log": version_log,
                "backtest_refs": backtest_refs,
                # optional per-strategy console page (decision log / settings),
                # declared in config_snapshot.json — rendered as a header button
                # on the React detail page (issue #430: requirements must be
                # REACHABLE from the strategies section, not URL-only).
                "console_url": config.get("console_url"),
            },
        }
    )


@strategies_dashboard_bp.route("/api/<name>/pnl-curve", methods=["GET"])
@check_session_validity
def pnl_curve(name: str):
    """Daily P&L time series. ?window=1d|1w|1m|all (default all)."""
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    window = request.args.get("window", "all")
    window_days: int | None = None
    if window == "1d":
        window_days = 1
    elif window == "1w":
        window_days = 7
    elif window == "1m":
        window_days = 30

    points: list[dict] = []
    if name == "sector_follow_cap5_vol":
        points = _pnl_curve_sector_follow(window_days)
    elif name == "futures_follow_cap50":
        points = _pnl_curve_futures_follow(window_days)
    elif name == _SIMPLIFIED_ENGINE_FOLDER:
        points = _pnl_curve_simplified_engine(window_days)
    elif name == _INTRADAY_PULLBACK_FOLDER:
        points = _pnl_curve_intraday_pullback(window_days)
    # Other strategies: empty series (no journal yet)

    return jsonify({"status": "success", "data": {"window": window, "points": points}})


@strategies_dashboard_bp.route("/api/<name>/parameters/diff", methods=["GET"])
@check_session_validity
def parameters_diff(name: str):
    """Parameter diff between current config_snapshot and a named version.

    ?vs=<version_tag>  e.g. ?vs=v0.1.0
    Returns current params, previous params (from VERSION_LOG body), and a list
    of changed keys. If the prior version can't be found the diff is empty.
    """
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    current = _load_config_snapshot(name)
    vs_version = request.args.get("vs", "")

    previous: dict = {}
    if vs_version:
        version_log = _load_version_log(name)
        for entry in version_log:
            if entry["version"] == vs_version:
                # Try to extract a JSON block from the body
                body = entry["body"]
                m = re.search(r"```json\s*([\s\S]+?)\s*```", body)
                if m:
                    try:
                        previous = json.loads(m.group(1))
                    except Exception:
                        pass
                break

    # Compute changed keys (flat comparison)
    changed: list[dict] = []
    all_keys = set(current.keys()) | set(previous.keys())
    for k in sorted(all_keys):
        cur_val = current.get(k)
        prev_val = previous.get(k)
        if cur_val != prev_val:
            changed.append({"key": k, "current": cur_val, "previous": prev_val})

    return jsonify(
        {
            "status": "success",
            "data": {
                "name": name,
                "current_version": current.get("version", "—"),
                "vs_version": vs_version or None,
                "current": current,
                "previous": previous,
                "changed_keys": changed,
            },
        }
    )


# --------------------------------------------------------------------------- #
# Mode flip endpoint (issue #162) — the single sanctioned mutation path
# --------------------------------------------------------------------------- #


def _flipped_by_label() -> str:
    """Identify the operator behind the flip for the audit row.

    Falls back to ``"ui:unknown"`` when no Flask session user is set —
    handles the dev-server "no auth required" case without crashing.
    """
    try:
        from flask import session

        user = session.get("user") or session.get("username") or "unknown"
        return f"ui:{user}"
    except Exception:
        return "ui:unknown"


@strategies_dashboard_bp.route("/api/<name>/mode", methods=["POST"])
@check_session_validity
def flip_strategy_mode(name: str):
    """Flip a strategy's mode (sandbox↔live) through the preflight gate.

    Body: ``{"mode": "live" | "sandbox", "notes": "optional"}``

    Returns:
        202 + accepted=True  → flip succeeded, mode mutated, event fired.
        409 + accepted=False → preflight refused; blockers list explains why.
                              mode is unchanged.
        400                  → bad input (missing/invalid mode in body).
    """
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    body = request.get_json(silent=True) or {}
    target_mode = (body.get("mode") or "").lower().strip()
    notes = body.get("notes") or None

    if target_mode not in ("live", "sandbox"):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Body must include {'mode': 'live' | 'sandbox'}",
                }
            ),
            400,
        )

    try:
        from services.strategy_mode_service import flip_mode
    except Exception:
        logger.exception("flip_strategy_mode: failed to import strategy_mode_service")
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Strategy mode service unavailable — see logs",
                }
            ),
            500,
        )

    outcome = flip_mode(
        strategy_name=name,
        target_mode=target_mode,
        flipped_by=_flipped_by_label(),
        notes=notes,
    )
    payload = outcome.to_dict()
    payload["status"] = "success" if outcome.accepted else "blocked"
    status_code = 202 if outcome.accepted else 409
    return jsonify(payload), status_code


@strategies_dashboard_bp.route("/api/<name>/mode/audit", methods=["GET"])
@check_session_validity
def strategy_mode_audit(name: str):
    """Return recent mode-flip attempts for this strategy.

    Used by the UI to show the operator: "what happened on the last 10
    flip attempts?" — accepted AND blocked attempts both surface.
    """
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    try:
        limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, 100))

    try:
        from database.strategy_mode_audit_db import list_attempts

        rows = list_attempts(strategy_name=name, limit=limit)
    except Exception:
        logger.exception("strategy_mode_audit: list_attempts failed for %s", name)
        rows = []

    return jsonify({"status": "success", "data": {"name": name, "rows": rows, "limit": limit}})


# --------------------------------------------------------------------------- #
# LLM mode flip + decisions history (issue #266 Phase 2)
# --------------------------------------------------------------------------- #


@strategies_dashboard_bp.route("/api/<name>/llm-mode", methods=["POST"])
@check_session_validity
def flip_strategy_llm_mode(name: str):
    """Set a strategy's LLM mode (off | veto) through the guarded writer.

    Body: ``{"llm_mode": "off" | "veto" | "delegate"}``

    ``delegate`` is accepted and stored, but the resolver treats it as ``veto``
    for now (the LLM-decides path isn't built) — the response ``warnings`` say
    so. The UI shows delegate as a disabled/"coming soon" option.

    Returns:
        202 + accepted=True  → llm_mode set, event fired.
        400                  → bad input (missing/invalid llm_mode).
    """
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    body = request.get_json(silent=True) or {}
    target = (body.get("llm_mode") or "").lower().strip()
    notes = body.get("notes") or None

    if target not in ("off", "veto", "delegate"):
        return (
            jsonify(
                {
                    "status": "error",
                    "message": "Body must include {'llm_mode': 'off' | 'veto' | 'delegate'}",
                }
            ),
            400,
        )

    try:
        from services.strategy_llm_config_service import flip_llm_mode
    except Exception:
        logger.exception("flip_strategy_llm_mode: failed to import strategy_llm_config_service")
        return (
            jsonify({"status": "error", "message": "LLM config service unavailable — see logs"}),
            500,
        )

    outcome = flip_llm_mode(
        strategy_name=name,
        target_llm_mode=target,
        flipped_by=_flipped_by_label(),
        notes=notes,
    )
    payload = outcome.to_dict()
    payload["status"] = "success" if outcome.accepted else "error"
    return jsonify(payload), (202 if outcome.accepted else 400)


@strategies_dashboard_bp.route("/api/<name>/llm-decisions", methods=["GET"])
@check_session_validity
def strategy_llm_decisions(name: str):
    """Paginated LLM-veto decision history for a strategy + a health summary.

    Query: ``?limit=&offset=`` (limit clamped 1..200, default 50).

    Per-strategy filtering (see _LLM_DECISION_SOURCES): the simplified engine's
    veto rows are tagged with chartink source labels, not the folder name, so
    its view is all rows EXCEPT strategies that tag rows with their own name
    (today: futures_follow_cap50). futures_follow_cap50 (#318) filters cleanly
    to source='futures_follow_cap50'. For strategies that don't run the veto,
    ``veto_enabled=false`` and rows are empty.
    """
    strategy_dir = _STRATEGIES_DIR / name
    if not strategy_dir.exists():
        return jsonify({"status": "error", "message": f"Strategy '{name}' not found"}), 404

    try:
        limit = int(request.args.get("limit", "50"))
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))
    try:
        offset = int(request.args.get("offset", "0"))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    veto_enabled = name in _VETO_ENABLED_STRATEGIES
    if not veto_enabled:
        # No veto call for this strategy — return an honest empty view.
        return jsonify(
            {
                "status": "success",
                "data": {
                    "name": name,
                    "veto_enabled": False,
                    "llm_mode": _get_llm_mode(name),
                    "rows": [],
                    "total": 0,
                    "limit": limit,
                    "offset": offset,
                    "summary": None,
                    "source_filtered": False,
                },
            }
        )

    # Per-strategy source filter kwargs; {} → all sources (no filter).
    filters = _LLM_DECISION_SOURCES.get(name) or {}
    try:
        from database.signal_decision_db import (
            count_signal_decisions,
            list_signal_decisions,
            summarize_signal_decisions,
        )

        rows = list_signal_decisions(limit=limit, offset=offset, **filters)
        total = count_signal_decisions(**filters)
        summary = summarize_signal_decisions(**filters)
    except Exception:
        logger.exception("strategy_llm_decisions: query failed for %s", name)
        rows, total, summary = [], 0, None

    return jsonify(
        {
            "status": "success",
            "data": {
                "name": name,
                "veto_enabled": True,
                "llm_mode": _get_llm_mode(name),
                "rows": rows,
                "total": total,
                "limit": limit,
                "offset": offset,
                "summary": summary,
                # True when the view is a clean per-strategy source filter
                # (futures_follow). False for the simplified engine, whose view
                # is all-rows-except-other-strategies (the UI notes this).
                "source_filtered": bool(filters.get("sources")),
            },
        }
    )


def _llm_health_probe_timeout() -> float:
    """Wall-clock budget for the on-demand LLM health probe (env-tunable)."""
    raw = os.getenv("LLM_HEALTH_PROBE_TIMEOUT_SECONDS", "12")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return 12.0
    # Clamp to a sane band: too short falsely reports timeouts; too long makes
    # the operator's manual click hang.
    return max(3.0, min(val, 60.0))


@strategies_dashboard_bp.route("/api/llm/health", methods=["GET"])
@check_session_validity
def strategy_llm_health():
    """On-demand liveness probe of the shared ``claude`` CLI used by the LLM veto.

    Reachability is install-global — every strategy's Stage-1 veto calls the same
    ``claude`` binary/login — so this is a single shared check, not per-strategy.

    This spawns a real ``claude -p`` subprocess (seconds, consumes tokens), so it
    is deliberately built for **manual** invocation: the Strategies-page chip
    probes only when the operator clicks its refresh icon. Do NOT auto-poll it.

    Returns ``{reachable, latency_ms, reason, detail, checked_at}`` where
    ``reason`` ∈ ``ok`` | ``timeout`` | ``cli_missing`` | ``not_logged_in`` |
    ``error``. Never 5xx's on an unreachable LLM — an unreachable model is a
    successful probe with ``reachable=false``.
    """
    checked_at = datetime.now(_IST).isoformat()
    try:
        from services.llm_review_client import probe_claude_health

        result = probe_claude_health(_llm_health_probe_timeout())
    except Exception as exc:
        logger.exception("strategy_llm_health: probe raised")
        result = {
            "reachable": False,
            "latency_ms": 0,
            "reason": "error",
            "detail": f"{type(exc).__name__}: {str(exc)[:280]}",
        }
    result["checked_at"] = checked_at
    return jsonify({"status": "success", "data": result})
