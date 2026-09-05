"""Per-account realized P&L for the strategy detail page (issue #700).

Answers ONE question for every account running a strategy: is it in profit or
loss, net of charges? One row per account (the primary plus each child), a
strategy total, and a verdict that is simply the sign of that total.

Sources, and the rule that binds them:

- **Primary** — the strategy's own journal, through the SAME row set the
  Performance Comparison table uses (``open15_breakout_db.real_closed_rows``
  + ``net_pnl_of_row``). One definition, so the two cards cannot disagree.
- **Children** — ``account_daily_pnl`` rows written by
  ``account_pnl_service`` from each child's own fills. A day with placed
  mirrors but no row is reported as MISSING and counted, never as ₹0.

Windows are IST calendar days: ``1d`` today, ``1w`` last 7, ``1m`` last 30,
``all`` since the first row (the default — the operator's decision).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from database import account_orders_db, broker_accounts_db
from utils.logging import get_logger

logger = get_logger(__name__)

_IST = timedelta(hours=5, minutes=30)
_STRATEGIES_DIR = Path(__file__).parent.parent / "strategies"
WINDOW_DAYS: dict[str, int | None] = {"1d": 1, "1w": 7, "1m": 30, "all": None}


def today_ist() -> date:
    return (datetime.utcnow() + _IST).date()


def window_since(window: str, today: date | None = None) -> date | None:
    """First IST day included by ``window`` (inclusive); ``None`` = no bound."""
    days = WINDOW_DAYS.get(window, None)
    if days is None:
        return None
    return (today or today_ist()) - timedelta(days=days - 1)


# --------------------------------------------------------------------------- #
# Primary (parent) daily series — per strategy
# --------------------------------------------------------------------------- #
def _open15_primary_daily() -> list[tuple[date, float]]:
    from database.open15_breakout_db import net_pnl_of_row, real_closed_rows

    by_day: dict[date, float] = {}
    for r in real_closed_rows(mode="live"):
        try:
            d = date.fromisoformat(str(r.trade_date)[:10])
        except (TypeError, ValueError):
            continue
        by_day[d] = by_day.get(d, 0.0) + net_pnl_of_row(r)
    return sorted(by_day.items())


_PRIMARY_DAILY = {"open15_vol_breakout": _open15_primary_daily}


def primary_daily_series(strategy_name: str) -> list[tuple[date, float]] | None:
    """Daily NET realized P&L of the primary account for ``strategy_name``, or
    ``None`` when the strategy has no journal adapter here (the card then
    shows children only)."""
    builder = _PRIMARY_DAILY.get(strategy_name)
    if builder is None:
        return None
    try:
        return builder()
    except Exception:
        logger.exception("primary daily series failed for %s", strategy_name)
        return None


def _primary_capital(strategy_name: str) -> float | None:
    """Declared slot budget from the config snapshot (same basis as the
    Performance table's notional capital)."""
    try:
        snap = json.loads(
            (_STRATEGIES_DIR / strategy_name / "config_snapshot.json").read_text(encoding="utf-8")
        )
        params = snap.get("params") or {}
        slot = float(params.get("margin_per_slot") or 0)
        slots = float(params.get("max_concurrent") or 0)
        if slot > 0 and slots > 0:
            return slot * slots
        cap = snap.get("capital_inr")
        return float(cap) if cap else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Window statistics (pure)
# --------------------------------------------------------------------------- #
def stats_from_daily(
    daily: list[tuple[date, float]], since: date | None, today: date | None = None
) -> dict:
    """Net, days traded, win-days %, max drawdown (on the window's cumulative
    curve), today's net and the daily series — all from one list of
    ``(day, net)`` pairs. ``None`` figures mean "no data", never zero."""
    today = today or today_ist()
    rows = sorted((d, v) for d, v in daily if since is None or d >= since)
    if not rows:
        return {
            "net_inr": None,
            "today_net_inr": None,
            "days_traded": 0,
            "win_days_pct": None,
            "max_dd_inr": None,
            "daily": [],
        }
    net = round(sum(v for _, v in rows), 2)
    wins = sum(1 for _, v in rows if v > 0)
    cum = peak = 0.0
    max_dd = 0.0
    series = []
    for d, v in rows:
        cum += v
        peak = max(peak, cum)
        max_dd = min(max_dd, cum - peak)
        series.append([d.isoformat(), round(cum, 2)])
    today_net = next((v for d, v in rows if d == today), None)
    return {
        "net_inr": net,
        "today_net_inr": round(today_net, 2) if today_net is not None else None,
        "days_traded": len(rows),
        "win_days_pct": round(100.0 * wins / len(rows), 1),
        "max_dd_inr": round(max_dd, 2),
        "daily": series,
    }


def verdict_of(total: float | None) -> str:
    """Sign of the total. No tolerance band (operator decision)."""
    if total is None or total == 0:
        return "flat"
    return "profit" if total > 0 else "loss"


def _child_capture(rows_today: list[dict], has_placed_today: bool) -> str:
    if rows_today:
        return "final" if any(r.get("finalized") for r in rows_today) else "provisional"
    return "missing" if has_placed_today else "idle"


def _combine_sources(sources: set[str]) -> str | None:
    sources = {s for s in sources if s}
    if not sources:
        return None
    return next(iter(sources)) if len(sources) == 1 else "mixed"


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #
def build_accounts_pnl(strategy_name: str, window: str = "all") -> dict:
    """The card payload. Never raises; a failed source degrades to an absent
    row and is named in ``sources_failed``."""
    window = window if window in WINDOW_DAYS else "all"
    today = today_ist()
    since = window_since(window, today)
    accounts: list[dict] = []
    sources_failed: list[str] = []
    first_day: date | None = None

    # ---- primary --------------------------------------------------------
    primary_daily = primary_daily_series(strategy_name)
    if primary_daily is not None:
        if primary_daily:
            first_day = primary_daily[0][0]
        st = stats_from_daily(primary_daily, since, today)
        cap = _primary_capital(strategy_name)
        accounts.append(
            {
                "account_id": None,
                "name": "Primary",
                "role": "primary",
                **st,
                "capital_basis_inr": cap,
                "return_pct": (
                    round(100.0 * st["net_inr"] / cap, 2)
                    if cap and st["net_inr"] is not None
                    else None
                ),
                "charges_source": "modelled",
                "capture": "journal",
                "days_missing": 0,
            }
        )

    # ---- children -------------------------------------------------------
    try:
        children = broker_accounts_db.list_accounts()
    except Exception:
        logger.exception("accounts P&L: could not list child accounts")
        children = []
        sources_failed.append("broker_accounts")

    for acct in children:
        try:
            aid = acct["id"]
            rows = account_orders_db.list_daily_pnl(strategy_name, account_id=aid)
            selected = strategy_name in broker_accounts_db.get_strategies(aid)
            if not rows and not selected:
                continue
            daily = [(date.fromisoformat(r["trade_date"]), float(r["realized_net"])) for r in rows]
            if daily and (first_day is None or daily[0][0] < first_day):
                first_day = daily[0][0]
            st = stats_from_daily(daily, since, today)
            captured_days = {d for d, _ in daily if since is None or d >= since}
            placed_days = {
                d
                for d in account_orders_db.placed_ist_days(aid, strategy_name, since)
                if since is None or d >= since
            }
            missing = sorted(placed_days - captured_days)
            rows_today = [r for r in rows if r["trade_date"] == today.isoformat()]
            in_window = [
                r for r in rows if since is None or date.fromisoformat(r["trade_date"]) >= since
            ]
            cap = float(acct.get("capital_inr") or 0) or None
            accounts.append(
                {
                    "account_id": aid,
                    "name": acct.get("display_name") or f"account {aid}",
                    "role": "child",
                    "connected": bool(acct.get("last_login_at")),
                    **st,
                    "capital_basis_inr": cap,
                    "return_pct": (
                        round(100.0 * st["net_inr"] / cap, 2)
                        if cap and st["net_inr"] is not None
                        else None
                    ),
                    "charges_source": _combine_sources(
                        {r.get("charges_source") for r in in_window}
                    ),
                    "capture": _child_capture(rows_today, today in placed_days),
                    "days_missing": len(missing),
                    "missing_days": [d.isoformat() for d in missing][-10:],
                    "capture_sources": sorted(
                        {r.get("capture_source") for r in in_window if r.get("capture_source")}
                    ),
                }
            )
        except Exception:
            logger.exception("accounts P&L: child %s failed", acct.get("id"))
            sources_failed.append(f"account:{acct.get('id')}")

    nets = [a["net_inr"] for a in accounts if a.get("net_inr") is not None]
    total_net = round(sum(nets), 2) if nets else None
    days_missing = sum(a.get("days_missing", 0) for a in accounts)
    all_days = {d for a in accounts for d, _ in ((x[0], x[1]) for x in a.get("daily", []))}
    return {
        "strategy": strategy_name,
        "window": window,
        "since": (since or first_day).isoformat() if (since or first_day) else None,
        "today": today.isoformat(),
        "verdict": verdict_of(total_net),
        "total": {
            "net_inr": total_net,
            "days_traded": len(all_days),
            "days_missing": days_missing,
            "n_accounts": len(accounts),
        },
        "accounts": accounts,
        "sources_failed": sources_failed,
    }
