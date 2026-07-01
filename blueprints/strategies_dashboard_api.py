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
import re
from datetime import date, datetime, timedelta
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
from database.trade_journal_db import TradeJournal
from database.trade_journal_db import db_session as tj_session
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

# Which strategies actually run the Stage-1 LLM veto today. Only the simplified
# engine calls _run_pre_order_review; sector_follow / futures_follow have no
# veto call, so their decisions view is empty by construction. The UI notes
# this rather than faking rows.
_VETO_ENABLED_STRATEGIES = {_SIMPLIFIED_ENGINE_FOLDER}

# Map a dashboard strategy name → the signal_decision.source labels its veto
# rows carry. The simplified engine reviews with source=<chartink strategy
# label> (e.g. "chartink_FnO_intraday_buy", "trend-up"), NOT its folder name —
# so a clean per-strategy source filter isn't available. We therefore return
# ALL signal_decision rows for the simplified engine (it is the only strategy
# running the veto today) and the UI notes that. Value None = "no source
# filter, return everything".
_LLM_DECISION_SOURCES: dict[str, list[str] | None] = {
    _SIMPLIFIED_ENGINE_FOLDER: None,
}


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
    """Aggregate P&L and position stats from sector_follow_trades."""
    try:
        q = sf_session.query(SectorFollowTrade)
        if since:
            q = q.filter(SectorFollowTrade.created_at >= since)
        trades = q.all()
        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        today_entries = [t for t in trades if t.side == "BUY" and t.entry_date == today_str]
        today_exits = [t for t in trades if t.side == "SELL" and t.entry_date == today_str]
        open_count = max(0, len(today_entries) - len(today_exits))
        # Best-effort net P&L: price diff on matched entry/exit pairs
        last = max((t.created_at for t in trades), default=None)
        return {
            "open_positions": open_count,
            "last_trade_at": last.isoformat() if last else None,
            "today_trade_count": len(today_entries) + len(today_exits),
        }
    except Exception:
        logger.exception("Failed to aggregate sector_follow_stats")
        return {"open_positions": 0, "last_trade_at": None, "today_trade_count": 0}


def _futures_follow_stats(since: datetime | None = None) -> dict:
    """Aggregate P&L and position stats from futures_follow_trades."""
    try:
        q = ff_session.query(FuturesFollowTrade)
        if since:
            q = q.filter(FuturesFollowTrade.created_at >= since)
        trades = q.all()
        today_str = datetime.now(_IST).strftime("%Y-%m-%d")
        entries = [t for t in trades if t.side == "BUY" and t.entry_date == today_str]
        exits = [t for t in trades if t.side == "SELL" and t.entry_date == today_str]
        open_count = max(0, len(entries) - len(exits))
        today_pnl = sum((t.net_pnl or 0.0) for t in exits if t.net_pnl is not None)
        last = max((t.created_at for t in trades), default=None)
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "last_trade_at": last.isoformat() if last else None,
            "today_trade_count": len(entries) + len(exits),
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
            if exited.startswith(today_str) and r.pnl is not None:
                today_pnl += float(r.pnl)
        return {
            "open_positions": open_count,
            "today_net_pnl": round(today_pnl, 2),
            "last_trade_at": last_at,
            "today_trade_count": today_trade_count,
        }
    except Exception:
        logger.exception("Failed to aggregate simplified_engine_stats")
        return {
            "open_positions": 0,
            "today_net_pnl": 0.0,
            "last_trade_at": None,
            "today_trade_count": 0,
        }


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
    """Daily P&L series from futures_follow_trades (net_pnl on exit rows)."""
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
            d = r.entry_date
            by_date[d] = by_date.get(d, 0.0) + (r.net_pnl or 0.0)
        return [{"date": d, "pnl": round(v, 2)} for d, v in sorted(by_date.items())]
    except Exception:
        logger.exception("Failed to build pnl_curve for futures_follow")
        return []


# ---------------------------------------------------------------------------
# Health LED
# ---------------------------------------------------------------------------


def _health_led(name: str, overrides: list[dict], config: dict) -> str:
    """Return 'healthy' | 'paused' | 'scaffold' | 'unknown'."""
    if any(o["type"] in ("pause", "kill_switch") for o in overrides):
        return "paused"
    mode_val = config.get("mode", "")
    if "scaffold" in str(mode_val).lower():
        return "scaffold"
    deployable = config.get("deployable", False)
    if not deployable:
        return "scaffold"
    return "healthy"


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

    return {
        "name": name,
        "display_name": name.replace("_", " ").title(),
        "mode": mode_val,
        "llm_mode": _get_llm_mode(name),
        "llm_veto_enabled": name in _VETO_ENABLED_STRATEGIES,
        "deployable": config.get("deployable", False),
        "version": config.get("version", "—"),
        "open_positions": stats.get("open_positions", 0),
        "today_net_pnl": stats.get("today_net_pnl", None),
        "today_trade_count": stats.get("today_trade_count", 0),
        "last_trade_at": stats.get("last_trade_at"),
        "active_overrides": overrides,
        "health": _health_led(name, overrides, config),
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

    # 3-column performance data
    parity = config.get("parity_target", {})
    performance = {
        "backtest": {
            "cagr_pct": parity.get("cagr_pct") or parity.get("sharpe_daily"),
            "sharpe": parity.get("sharpe") or parity.get("sharpe_daily"),
            "max_dd_pct": parity.get("max_dd_pct"),
            "win_rate_pct": parity.get("win_rate_pct"),
            "n_trades": parity.get("n_trades_window") or parity.get("n_trades"),
            "window": parity.get("window"),
        },
        "sandbox": None,
        "live": None,
    }

    # Sandbox live stats
    if name == "futures_follow_cap50":
        stats = _futures_follow_stats()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "last_trade_at": stats["last_trade_at"],
        }
    elif name == "sector_follow_cap5_vol":
        stats = _sector_follow_stats()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "last_trade_at": stats["last_trade_at"],
        }
    elif name == _SIMPLIFIED_ENGINE_FOLDER:
        stats = _simplified_engine_stats()
        performance["sandbox"] = {
            "open_positions": stats["open_positions"],
            "today_net_pnl": stats["today_net_pnl"],
            "last_trade_at": stats["last_trade_at"],
        }

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
                    "net_pnl": r.net_pnl,
                    "mode": r.mode,
                    "status": r.status,
                    "entry_date": r.entry_date,
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
                }
                for r in rows
            ]
        except Exception:
            logger.exception("Failed to fetch recent trades for %s", name)

    return jsonify(
        {
            "status": "success",
            "data": {
                "name": name,
                "display_name": name.replace("_", " ").title(),
                "mode": mode_val,
                "llm_mode": _get_llm_mode(name),
                "llm_veto_enabled": name in _VETO_ENABLED_STRATEGIES,
                "deployable": config.get("deployable", False),
                "version": config.get("version", "—"),
                "config_snapshot": config,
                "active_overrides": overrides,
                "health": _health_led(name, overrides, config),
                "performance": performance,
                "recent_trades": recent_trades,
                "version_log": version_log,
                "backtest_refs": backtest_refs,
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

    For the simplified engine (the only strategy running the veto today) this
    returns ALL signal_decision rows — its veto rows are tagged with the
    chartink source label, not the folder name, so a clean per-strategy source
    filter isn't available (see _LLM_DECISION_SOURCES). For strategies that
    don't run the veto, ``veto_enabled=false`` and rows are empty.
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

    sources = _LLM_DECISION_SOURCES.get(name)  # None → all sources
    try:
        from database.signal_decision_db import (
            count_signal_decisions,
            list_signal_decisions,
            summarize_signal_decisions,
        )

        rows = list_signal_decisions(sources=sources, limit=limit, offset=offset)
        total = count_signal_decisions(sources=sources)
        summary = summarize_signal_decisions(sources=sources)
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
                # True when we could NOT filter to a per-strategy source and so
                # returned all rows (the UI notes this).
                "source_filtered": sources is not None,
            },
        }
    )
