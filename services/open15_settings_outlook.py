"""Settings Outlook for open15_vol_breakout (issue #711).

Answers, from the journal alone, the question the /logs page never answered:
*with these settings and the win rate we are seeing, is the strategy making
money, and what should change?*

Everything here is READ-ONLY and derived from data the page already has:

- rows come from ``real_closed_rows(mode='live')`` and are priced with
  ``net_pnl_of_row`` - the ONE P&L convention (#552), the same row set the
  /strategies Live column shows (#458 ``_side_split``);
- intra-hold minute marks come from ``open15_pnl_curve.build_pnl_curve`` -
  the series the intra-hold chart already draws. Nothing new is persisted.
  Rows whose option contract has expired have no marks and fall back to a
  labelled closed-form (stop truncates the final loss; no path effects).

Three deliberate conventions, each load-bearing:

- **Real fills only by default.** ``scope='real_sim'`` adds the LIVE-DECIDED
  simulated triggers (``fill='sim'`` where the trigger fired in real time but no
  order was sent: cap reached, profit lock, unaffordable) to the win-rate
  statistics - never broker-rejected paper, never ``replay_missed_day`` rows,
  whose entry timing is reconstructed. Sim rows are 1 lot at the quote LTP, so
  the mixed scope works in **% of premium paid** and rupee P&L stays real-only.
- **The stop is judged on the side you trade.** On the first 41 live fills the
  same Rs2,500 stop LOWERED long P&L (it killed 7 winners before they turned)
  and RAISED short P&L (short losers run). A recommendation computed on both
  sides would be wrong for a longs-only deployment.
- **Rules, not prose.** Every recommendation is a named rule with fixed
  constants; the verdict is three boolean checks. There is no LLM here.
"""

from __future__ import annotations

import datetime as dt
import math
import random
import statistics
import threading
import time

from utils.logging import get_logger

logger = get_logger(__name__)

# ---- pre-registered constants (code, not tunables - #651 rule) -------------
RECENT_N = 20  # "recent run" window, in fills
CHECKPOINT_FILLS = 80  # re-decide at this many real fills
BOOTSTRAP_MONTHS = 2000
MONTH_DAYS = 20
BOOTSTRAP_SEED = 20260908
WR_GRID = [round(0.30 + i * 0.01, 2) for i in range(36)]  # 30%..65%
STOP_GRID = [0, 2000, 2500, 3000, 3500, 4000, 5000]
TARGET_GRID = [0, 5000, 6000, 8000, 10000, 12000, 15000]
TARGET_MIN_ENGAGE_FRAC = 0.30  # a lock that never engages is not a rule
STOP_NEAR_BEST_FRAC = 0.10  # "within 10% of the best net"
TRAIL_FLOOR_INR = 2500.0
TRAIL_ROUND_INR = 500.0
SIM_EXCLUDED_REASONS = ("replay_missed_day",)
_PATH_TTL_S = 300.0  # days with an unavailable row are re-tried after this

_IST_OFFSET = dt.timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------
def _hhmm_to_min(hhmm: str | None) -> int | None:
    if not hhmm or len(hhmm) < 5:
        return None
    try:
        return int(hhmm[:2]) * 60 + int(hhmm[3:5])
    except ValueError:
        return None


def _exit_min(exit_ts: str | None) -> int | None:
    if not exit_ts:
        return None
    try:
        ts = dt.datetime.fromisoformat(exit_ts)
        if ts.tzinfo is not None:
            ts = ts.astimezone(dt.timezone(_IST_OFFSET))
        return ts.hour * 60 + ts.minute
    except (ValueError, TypeError):
        return None


def row_dict(r, fill_class: str) -> dict:
    """One journal row reduced to what the engine needs (pure)."""
    from database.open15_breakout_db import net_pnl_of_row

    qty = (
        int(r.entry_fill_qty or r.quantity or 0)
        if fill_class == "real"
        else int(r.sim_quantity or 0)
    )
    prem = float(r.opt_entry_premium) if r.opt_entry_premium else None
    premium_paid = prem * qty if (prem and qty) else None
    net = net_pnl_of_row(r)
    return {
        "id": r.id,
        "date": r.trade_date,
        "symbol": r.symbol,
        "side": "long" if r.side == "L" else ("short" if r.side == "S" else None),
        "fill": fill_class,
        "net": round(net, 2),
        "gross": float(r.pnl or 0.0),
        "charges": float(r.charges_inr or 0.0),
        "qty": qty,
        "tick": float(r.opt_tick_size) if r.opt_tick_size else None,
        "premium_paid": premium_paid,
        "ret_pct": (net / premium_paid * 100.0) if premium_paid else None,
        "entry_min": _hhmm_to_min(r.trigger_minute),
        "exit_min": _exit_min(r.exit_ts),
        "reason": r.reason,
    }


def load_rows(scope: str = "real") -> list[dict]:
    """Closed, priced live rows. ``scope`` is ``real`` or ``real_sim``.

    Fail-open to ``[]`` - the card renders "no data", never a 500.
    """
    from database.open15_breakout_db import Open15Trade, db_session, real_closed_rows

    out = [row_dict(r, "real") for r in real_closed_rows(mode="live")]
    if scope == "real_sim":
        try:
            sims = (
                db_session.query(Open15Trade)
                .filter(
                    Open15Trade.mode == "live",
                    Open15Trade.fill == "sim",
                    Open15Trade.pnl.isnot(None),
                    Open15Trade.reason.notin_(SIM_EXCLUDED_REASONS),
                )
                .all()
            )
            out.extend(row_dict(r, "sim") for r in sims)
        except Exception:
            logger.exception("open15 outlook: sim rows read failed - real only")
        finally:
            db_session.remove()
    out.sort(key=lambda x: (x["date"], x["id"]))
    return out


# ---------------------------------------------------------------------------
# minute paths (from the intra-hold curve the page already serves)
# ---------------------------------------------------------------------------
_PATH_CACHE: dict[str, tuple[float | None, dict]] = {}
_PATH_LOCK = threading.Lock()


def clear_caches() -> None:
    with _PATH_LOCK:
        _PATH_CACHE.clear()


def _today_ist() -> str:
    return (dt.datetime.now(dt.UTC) + _IST_OFFSET).strftime("%Y-%m-%d")


def load_paths(dates: list[str]) -> dict[str, dict[str, dict]]:
    """{date: {symbol: {'series': {minute: mtm}, 'available': bool}}}.

    A settled past day with every row available is cached for the process;
    anything else is re-tried after ``_PATH_TTL_S`` (no broker session in the
    evening, or a row whose contract expired) so the page never hammers the
    broker on every keystroke.
    """
    from services.open15_pnl_curve import build_pnl_curve

    out: dict[str, dict[str, dict]] = {}
    today = _today_ist()
    for date in dates:
        with _PATH_LOCK:
            hit = _PATH_CACHE.get(date)
        if hit is not None and (hit[0] is None or (time.monotonic() - hit[0]) < _PATH_TTL_S):
            out[date] = hit[1]
            continue
        day: dict[str, dict] = {}
        complete = True
        try:
            payload = build_pnl_curve(date)
            for t in payload.get("trades") or []:
                avail = not t.get("unavailable") and bool(t.get("series"))
                complete = complete and avail
                day[t["symbol"]] = {
                    "available": avail,
                    "series": {_hhmm_to_min(h): v for h, v in (t.get("series") or [])},
                }
        except Exception:
            logger.exception("open15 outlook: path load failed for %s", date)
            complete = False
        stamp = None if (complete and day and date < today) else time.monotonic()
        with _PATH_LOCK:
            _PATH_CACHE[date] = (stamp, day)
        out[date] = day
    return out


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def wilson(wins: int, n: int, z: float = 1.96) -> tuple[float | None, float | None]:
    if n <= 0:
        return None, None
    p = wins / n
    den = 1 + z * z / n
    ctr = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return round(max(0.0, ctr - half), 4), round(min(1.0, ctr + half), 4)


def breakeven_wr(avg_win: float | None, avg_loss_abs: float | None) -> float | None:
    """The win rate at which wins exactly pay for losses."""
    if not avg_win or avg_loss_abs is None:
        return None
    if avg_win + avg_loss_abs <= 0:
        return None
    return round(avg_loss_abs / (avg_win + avg_loss_abs), 4)


def _max_drawdown(values: list[float]) -> float:
    cum = peak = dd = 0.0
    for v in values:
        cum += v
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return round(dd, 2)


def describe(rows: list[dict], unit: str = "inr", recent_n: int = RECENT_N) -> dict:
    """Descriptive statistics of a row set in one unit.

    ``unit='inr'`` reads ``net`` (rupees; real rows only make sense here);
    ``unit='pct'`` reads ``ret_pct`` (% of premium paid - size-free, the only
    unit in which 1-lot sim rows and 2-lot real rows compare).
    """
    key = "net" if unit == "inr" else "ret_pct"
    vals = [(r, r[key]) for r in rows if r.get(key) is not None]
    n = len(vals)
    if n == 0:
        return {"n": 0, "unit": unit}
    xs = [v for _, v in vals]
    wins = [v for v in xs if v > 0]
    losses = [v for v in xs if v <= 0]
    avg_win = statistics.fmean(wins) if wins else None
    avg_loss_abs = -statistics.fmean(losses) if losses else 0.0
    best_i = max(range(n), key=lambda i: xs[i])
    xs_ex = [v for i, v in enumerate(xs) if i != best_i]
    wins_ex = [v for v in xs_ex if v > 0]
    avg_win_ex = statistics.fmean(wins_ex) if wins_ex else None
    lo, hi = wilson(len(wins), n)
    recent = xs[-recent_n:]
    days = sorted({r["date"] for r, _ in vals})
    out = {
        "unit": unit,
        "n": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n, 4),
        "wilson_lo": lo,
        "wilson_hi": hi,
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(-avg_loss_abs, 2),
        "median_win": round(statistics.median(wins), 2) if wins else None,
        "median_loss": round(statistics.median(losses), 2) if losses else None,
        "breakeven_wr": breakeven_wr(avg_win, avg_loss_abs),
        "breakeven_wr_ex_best": breakeven_wr(avg_win_ex, avg_loss_abs),
        "avg_win_ex_best": round(avg_win_ex, 2) if avg_win_ex is not None else None,
        "expectancy": round(statistics.fmean(xs), 2),
        "expectancy_ex_best": round(statistics.fmean(xs_ex), 2) if xs_ex else None,
        "total": round(sum(xs), 2),
        "total_ex_best": round(sum(xs_ex), 2),
        "best": round(max(xs), 2),
        "best_symbol": vals[best_i][0]["symbol"],
        "best_date": vals[best_i][0]["date"],
        "worst": round(min(xs), 2),
        "max_drawdown": _max_drawdown(xs),
        "days": len(days),
        "first_date": days[0],
        "last_date": days[-1],
        "per_day": round(sum(xs) / len(days), 2),
        "recent": {
            "n": len(recent),
            "wins": sum(1 for v in recent if v > 0),
            "win_rate": round(sum(1 for v in recent if v > 0) / len(recent), 4),
            "total": round(sum(recent), 2),
        },
        "cum": [round(v, 2) for v in _cumsum(xs)],
    }
    if unit == "inr":
        gross = sum(r["gross"] for r, _ in vals)
        charges = sum(r["charges"] for r, _ in vals)
        out["gross"] = round(gross, 2)
        out["charges"] = round(charges, 2)
        out["charges_pct_of_gross"] = round(charges / gross * 100.0, 1) if gross > 0 else None
    return out


def _cumsum(xs: list[float]) -> list[float]:
    c = 0.0
    out = []
    for v in xs:
        c += v
        out.append(c)
    return out


def side_split(rows: list[dict], unit: str = "inr") -> dict[str, dict]:
    return {
        "long": describe([r for r in rows if r["side"] == "long"], unit),
        "short": describe([r for r in rows if r["side"] == "short"], unit),
    }


# ---------------------------------------------------------------------------
# replay: every past day through stop / lock / trail / slot cap
# ---------------------------------------------------------------------------
def replay_day(rows: list[dict], paths: dict[str, dict], settings: dict) -> dict:
    """Walk one day minute by minute through the risk rules (pure).

    ``rows`` are that day's rows in entry order; ``paths[symbol]['series']`` is
    ``{minute: mtm}`` (gross, before charges) when available. Rules mirror the
    live monitor (#696): a stop fires at the first mark at or below ``-stop``;
    the day locks when day P&L (realized + open MTM) reaches ``target`` and
    later entries are skipped; once locked, a retrace of ``trail`` from the
    peak flattens every open row at its mark. ``max_trades`` caps admitted
    entries. A row without marks contributes no MTM until its exit and is
    closed-form: its final loss is truncated at ``-(stop + charges)``.
    """
    stop = float(settings.get("stop") or 0.0) if settings.get("stop_on", True) else 0.0
    target = float(settings.get("target") or 0.0) if settings.get("lock_on", True) else 0.0
    trail = float(settings.get("trail") or 0.0)
    cap = int(settings.get("max_trades") or 99)

    rows = sorted(rows, key=lambda r: (r["entry_min"] or 0, r["id"]))
    entry_mins = [r["entry_min"] for r in rows if r["entry_min"] is not None]
    exit_mins = [r["exit_min"] for r in rows if r["exit_min"] is not None]
    if not rows or not entry_mins:
        return {"net": 0.0, "trades": [], "stops": 0, "locked": False, "trail": False}
    first = min(entry_mins)
    last = max(exit_mins) if exit_mins else first + 15

    state: dict[int, dict] = {}
    admitted = 0
    realized = 0.0
    locked = False
    peak = 0.0
    trail_fired = False
    outcomes: list[dict] = []

    def _mark(r: dict, m: int) -> float | None:
        p = paths.get(r["symbol"])
        if not p or not p.get("available"):
            return None
        s = p["series"]
        if m in s:
            return s[m]
        prev = [k for k in s if k <= m]
        return s[max(prev)] if prev else 0.0

    def _close(r: dict, net: float, reason: str) -> None:
        nonlocal realized
        realized += net
        state[r["id"]]["closed"] = True
        outcomes.append(
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "side": r["side"],
                "net": round(net, 2),
                "as_traded": r["net"],
                "reason": reason,
                "closed_form": not (paths.get(r["symbol"]) or {}).get("available"),
            }
        )

    for m in range(first, last + 1):
        # admissions at this minute
        for r in rows:
            if r["entry_min"] != m or r["id"] in state:
                continue
            if locked:
                outcomes.append(
                    {"id": r["id"], "symbol": r["symbol"], "side": r["side"], "skipped": "lock"}
                )
                state[r["id"]] = {"closed": True, "skipped": True}
                continue
            if admitted >= cap:
                outcomes.append(
                    {"id": r["id"], "symbol": r["symbol"], "side": r["side"], "skipped": "cap"}
                )
                state[r["id"]] = {"closed": True, "skipped": True}
                continue
            admitted += 1
            state[r["id"]] = {"closed": False, "skipped": False}
        open_rows = [r for r in rows if r["id"] in state and not state[r["id"]]["closed"]]
        # stops first (the monitor's ordering)
        if stop > 0:
            for r in open_rows:
                mk = _mark(r, m)
                if mk is not None and mk <= -stop and m < (r["exit_min"] or 10**6):
                    _close(r, mk - r["charges"], "stop_loss")
        open_rows = [r for r in rows if r["id"] in state and not state[r["id"]]["closed"]]
        # scheduled exits (and closed-form rows) at their journal exit minute
        for r in open_rows:
            if r["exit_min"] is not None and m >= r["exit_min"]:
                net = r["net"]
                if stop > 0 and _mark(r, m) is None and net < -(stop + r["charges"]):
                    _close(r, -(stop + r["charges"]), "stop_loss_cf")
                else:
                    _close(r, net, r["reason"] or "exit")
        open_rows = [r for r in rows if r["id"] in state and not state[r["id"]]["closed"]]
        # day rule
        mtm = 0.0
        for r in open_rows:
            mk = _mark(r, m)
            if mk is not None:
                mtm += mk - r["charges"]
        day_pnl = realized + mtm
        if target > 0 and not locked and day_pnl >= target:
            locked = True
            peak = day_pnl
        if locked:
            peak = max(peak, day_pnl)
            if trail > 0 and open_rows and (peak - day_pnl) >= trail:
                for r in open_rows:
                    mk = _mark(r, m)
                    _close(r, (mk - r["charges"]) if mk is not None else r["net"], "profit_trail")
                trail_fired = True
    for r in rows:  # anything still open (no exit minute recorded)
        if r["id"] in state and not state[r["id"]]["closed"]:
            _close(r, r["net"], r["reason"] or "exit")
    return {
        "net": round(realized, 2),
        "trades": outcomes,
        "stops": sum(1 for o in outcomes if str(o.get("reason", "")).startswith("stop_loss")),
        "locked": locked,
        "trail": trail_fired,
    }


def replay(rows: list[dict], paths_by_day: dict[str, dict], settings: dict) -> dict:
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["date"], []).append(r)
    day_nets: dict[str, float] = {}
    taken: list[dict] = []
    stops = locks = trails = skipped = cf = 0
    for date in sorted(by_day):
        res = replay_day(by_day[date], paths_by_day.get(date, {}), settings)
        day_nets[date] = res["net"]
        stops += res["stops"]
        locks += 1 if res["locked"] else 0
        trails += 1 if res["trail"] else 0
        for o in res["trades"]:
            if o.get("skipped"):
                skipped += 1
            else:
                taken.append(o)
                cf += 1 if o.get("closed_form") else 0
    nets = [o["net"] for o in taken]
    wins = sum(1 for v in nets if v > 0)
    return {
        "net": round(sum(nets), 2),
        "n": len(nets),
        "wins": wins,
        "win_rate": round(wins / len(nets), 4) if nets else None,
        "stops": stops,
        "lock_days": locks,
        "trail_days": trails,
        "skipped": skipped,
        "closed_form_rows": cf,
        "days": len(day_nets),
        "day_nets": day_nets,
        "wrong_stops": sum(
            1
            for o in taken
            if str(o.get("reason", "")).startswith("stop_loss") and o["as_traded"] > 0
        ),
    }


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------
def bootstrap(day_nets: list[float], months: int = BOOTSTRAP_MONTHS) -> dict | None:
    if len(day_nets) < 3:
        return None
    rng = random.Random(BOOTSTRAP_SEED)  # nosec B311 - simulation, not security
    res = sorted(sum(rng.choice(day_nets) for _ in range(MONTH_DAYS)) for _ in range(months))
    q = lambda f: res[min(len(res) - 1, int(f * len(res)))]  # noqa: E731
    return {
        "p10": round(q(0.10)),
        "p50": round(q(0.50)),
        "p90": round(q(0.90)),
        "p_positive": round(sum(1 for x in res if x > 0) / len(res), 3),
        "days": MONTH_DAYS,
        "months": months,
    }


def sensitivity(st: dict, trades_per_day: float) -> dict | None:
    if not st.get("n") or st.get("avg_win") is None:
        return None
    loss = -st["avg_loss"]
    win_all, win_ex = st["avg_win"], st.get("avg_win_ex_best") or st["avg_win"]
    scale = trades_per_day * MONTH_DAYS

    def line(win):
        return [[wr, round((wr * win - (1 - wr) * loss) * scale)] for wr in WR_GRID]

    return {
        "wr_grid": WR_GRID,
        "line_ex_best": line(win_ex),
        "line_all": line(win_all),
        "breakeven": st.get("breakeven_wr_ex_best"),
        "breakeven_all": st.get("breakeven_wr"),
        "trades_per_day": round(trades_per_day, 2),
    }


# ---------------------------------------------------------------------------
# verdict + recommendations (rules)
# ---------------------------------------------------------------------------
def verdict(st: dict) -> dict:
    be = st.get("breakeven_wr_ex_best") or st.get("breakeven_wr")
    if not st.get("n") or be is None:
        return {"status": "no_data", "checks": [], "label": "NO DATA"}
    c1 = st["win_rate"] > be
    c2 = (st.get("wilson_lo") or 0) > be
    rec = st["recent"]
    c3 = rec["win_rate"] >= be
    checks = [
        {"id": "above_breakeven", "ok": c1, "wr": st["win_rate"], "breakeven": be},
        {
            "id": "ci_above_breakeven",
            "ok": c2,
            "lo": st.get("wilson_lo"),
            "hi": st.get("wilson_hi"),
        },
        {
            "id": "recent_holding",
            "ok": c3,
            "n": rec["n"],
            "wr": rec["win_rate"],
            "net": rec["total"],
        },
    ]
    if c1 and c2 and c3:
        status, label = "edge_confirmed", "EDGE CONFIRMED"
    elif not c1:
        status, label = "losing", "BELOW BREAKEVEN"
    else:
        status, label = "not_proven", "NOT PROVEN YET"
    return {
        "status": status,
        "label": label,
        "green": sum(1 for c in checks if c["ok"]),
        "checks": checks,
        "checkpoint": {"fills": st["n"], "target": CHECKPOINT_FILLS},
    }


def _round_to(v: float, step: float) -> float:
    return math.ceil(v / step) * step


def recommendations(
    rows: list[dict], paths: dict, saved: dict, st_all: dict, sides: dict
) -> list[dict]:
    """The five rules. Each returns a row with its evidence, or is omitted when
    it cannot compute. Statuses: ``recommend`` / ``in_effect`` / ``hold``."""
    out: list[dict] = []
    be = st_all.get("breakeven_wr_ex_best") or st_all.get("breakeven_wr")
    traded_side = saved.get("trade_side") or "both"

    # R1 side: below breakeven AND flat without its best trade -> exclude + shadow
    if be is not None:
        for side in ("long", "short"):
            s = sides.get(side) or {}
            if s.get("n", 0) >= 5 and s["win_rate"] < be and (s.get("total_ex_best") or 0) <= 0:
                other = "long_only" if side == "short" else "short_only"
                out.append(
                    {
                        "rule": "side",
                        "field": "trade_side",
                        "from": traded_side,
                        "to": other,
                        "status": "in_effect" if traded_side == other else "recommend",
                        "evidence": (
                            f"{side}s win {s['win_rate'] * 100:.1f}% ({s['wins']}/{s['n']}), below the "
                            f"{be * 100:.1f}% breakeven, and net {s['total_ex_best']:+,.0f} once their best "
                            f"trade ({s['best_symbol']} {s['best_date']} {s['best']:+,.0f}) is removed. "
                            "Rule: a side below breakeven whose ex-best net <= 0 is excluded and shadowed."
                        ),
                        "effect": f"removes {s['expectancy']:+,.0f}/trade x {s['n']} trades of drag in-sample",
                    }
                )

    # R2 stop: grid replay on the side you trade; cheapest stop near the best net
    side_rows = rows
    if traded_side == "long_only":
        side_rows = [r for r in rows if r["side"] == "long"]
    elif traded_side == "short_only":
        side_rows = [r for r in rows if r["side"] == "short"]
    grid = []
    for sl in STOP_GRID:
        res = replay(side_rows, paths, {"stop": sl, "target": 0, "trail": 0, "max_trades": 99})
        grid.append(
            {"stop": sl, "net": res["net"], "stops": res["stops"], "wrong": res["wrong_stops"]}
        )
    if grid:
        best = max(g["net"] for g in grid)
        near = [g for g in grid if g["net"] >= best - abs(best) * STOP_NEAR_BEST_FRAC]
        pick = min(near, key=lambda g: (g["wrong"], -g["net"]))
        cur = float(saved.get("stop_loss_inr") or 0) if saved.get("stop_loss_enabled") else 0.0
        cur_g = next((g for g in grid if g["stop"] == cur), None)
        as_traded = next(g for g in grid if g["stop"] == 0)
        out.append(
            {
                "rule": "stop",
                "field": "stop_loss_inr",
                "from": cur,
                "to": pick["stop"],
                "status": "in_effect" if pick["stop"] == cur else "recommend",
                "evidence": (
                    f"replayed on {len(side_rows)} {traded_side.replace('_', ' ')} fills: no stop "
                    f"{as_traded['net']:+,.0f}; "
                    + (
                        f"current {cur:,.0f} -> {cur_g['net']:+,.0f} ({cur_g['stops']} stops, "
                        f"{cur_g['wrong']} were winners); "
                        if cur_g
                        else ""
                    )
                    + f"pick {pick['stop']:,.0f} -> {pick['net']:+,.0f} ({pick['stops']} stops, "
                    f"{pick['wrong']} were winners). Rule: within 10% of the best net, fewest wrong "
                    "stops wins; 0 = no stop."
                ),
                "effect": f"{pick['net'] - (cur_g['net'] if cur_g else as_traded['net']):+,.0f} vs current, in-sample",
                "grid": grid,
            }
        )

    # R3 trail: at least one tick of the largest recent lot
    recent = [r for r in rows if r["fill"] == "real"][-RECENT_N:]
    ticks = [(r["tick"] * r["qty"], r["symbol"]) for r in recent if r.get("tick") and r.get("qty")]
    if ticks:
        tick_inr, sym = max(ticks)
        want = _round_to(max(TRAIL_FLOOR_INR, tick_inr), TRAIL_ROUND_INR)
        cur = float(saved.get("trail_giveback_inr") or 0)
        out.append(
            {
                "rule": "trail",
                "field": "trail_giveback_inr",
                "from": cur,
                "to": want,
                "status": "in_effect" if cur >= want else "recommend",
                "evidence": (
                    f"one tick on the largest lot in the last {len(recent)} fills is Rs{tick_inr:,.0f} "
                    f"({sym}). Rule: trail = max(Rs{TRAIL_FLOOR_INR:,.0f}, one tick of the largest recent lot) "
                    f"rounded up to Rs{TRAIL_ROUND_INR:,.0f}. A trail under one tick can end the day on noise."
                ),
                "effect": "prevents a false flatten; not a return estimate",
            }
        )

    # R4 target: grid with the saved stop; best net that still engages on >=30% of days
    sl_now = float(saved.get("stop_loss_inr") or 0) if saved.get("stop_loss_enabled") else 0.0
    tg = []
    for t in TARGET_GRID:
        res = replay(
            side_rows,
            paths,
            {
                "stop": sl_now,
                "target": t,
                "trail": float(saved.get("trail_giveback_inr") or 0),
                "max_trades": 99,
            },
        )
        tg.append(
            {
                "target": t,
                "net": res["net"],
                "lock_days": res["lock_days"],
                "days": res["days"],
                "skipped": res["skipped"],
            }
        )
    if tg:
        eligible = [
            g
            for g in tg
            if g["target"] == 0
            or (g["days"] and g["lock_days"] / g["days"] >= TARGET_MIN_ENGAGE_FRAC)
        ]
        pick = max(eligible or tg, key=lambda g: g["net"])
        cur = (
            float(saved.get("profit_target_inr") or 0) if saved.get("profit_lock_enabled") else 0.0
        )
        cur_g = next((g for g in tg if g["target"] == cur), None)
        out.append(
            {
                "rule": "target",
                "field": "profit_target_inr",
                "from": cur,
                "to": pick["target"],
                "status": "in_effect" if pick["target"] == cur else "recommend",
                "evidence": (
                    f"replayed with the saved stop: no lock {tg[0]['net']:+,.0f}; "
                    + (
                        f"current {cur:,.0f} -> {cur_g['net']:+,.0f} on {cur_g['lock_days']}/{cur_g['days']} lock days; "
                        if cur_g
                        else ""
                    )
                    + f"pick {pick['target']:,.0f} -> {pick['net']:+,.0f} ({pick['lock_days']}/{pick['days']} days lock, "
                    f"{pick['skipped']} entries skipped). Rule: best net among targets that engage on "
                    f">= {int(TARGET_MIN_ENGAGE_FRAC * 100)}% of days."
                ),
                "effect": f"{pick['net'] - tg[0]['net']:+,.0f} vs no lock, in-sample",
                "grid": tg,
            }
        )

    # R5 size: never while the CI straddles breakeven
    if be is not None and st_all.get("n"):
        straddles = (st_all.get("wilson_lo") or 0) <= be <= (st_all.get("wilson_hi") or 1)
        out.append(
            {
                "rule": "size",
                "field": "margin_per_slot",
                "from": saved.get("margin_per_slot"),
                "to": saved.get("margin_per_slot"),
                "status": "hold" if straddles else "in_effect",
                "evidence": (
                    "size scales wins and losses alike and cannot move the win rate. Rule: never "
                    "recommend a size change while the win-rate 95% interval "
                    f"({(st_all.get('wilson_lo') or 0) * 100:.0f}-{(st_all.get('wilson_hi') or 0) * 100:.0f}%) "
                    f"straddles breakeven ({be * 100:.1f}%)."
                ),
                "effect": "-",
            }
        )
    return out


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------
def _settings_from_config(cfg: dict) -> dict:
    return {
        "max_trades": int(cfg.get("max_trades") or 3),
        "margin_per_slot": float(cfg.get("margin_per_slot") or 0),
        "stop": float(cfg.get("stop_loss_inr") or 0),
        "stop_on": bool(cfg.get("stop_loss_enabled")),
        "target": float(cfg.get("profit_target_inr") or 0),
        "lock_on": bool(cfg.get("profit_lock_enabled")),
        "trail": float(cfg.get("trail_giveback_inr") or 0),
        "trade_side": cfg.get("trade_side") or "both",
    }


def saved_settings() -> dict:
    """The saved config row merged over the service's env defaults."""
    from database.open15_breakout_db import get_config

    cfg = dict(get_config() or {})
    try:
        from services.open15_breakout_service import (
            _profit_lock_enabled_default,
            _profit_target_default,
            _stop_loss_enabled_default,
            _stop_loss_inr_default,
            _trail_giveback_default,
        )

        cfg.setdefault("profit_lock_enabled", _profit_lock_enabled_default())
        cfg.setdefault("profit_target_inr", _profit_target_default())
        cfg.setdefault("trail_giveback_inr", _trail_giveback_default())
        cfg.setdefault("stop_loss_enabled", _stop_loss_enabled_default())
        cfg.setdefault("stop_loss_inr", _stop_loss_inr_default())
    except Exception:
        logger.exception("open15 outlook: env defaults unavailable - saved row only")
    for k, v in list(cfg.items()):
        if v is None:
            cfg.pop(k)
    return cfg


def _apply_sample(rows: list[dict], sample: str, st_hint: dict | None = None) -> list[dict]:
    if sample == "ex_best_day" and rows:
        by_day: dict[str, float] = {}
        for r in rows:
            by_day[r["date"]] = by_day.get(r["date"], 0.0) + r["net"]
        best_day = max(by_day, key=by_day.get)
        return [r for r in rows if r["date"] != best_day]
    if sample == "last_20":
        return rows[-RECENT_N:]
    if sample == "longs":
        return [r for r in rows if r["side"] == "long"]
    if sample == "shorts":
        return [r for r in rows if r["side"] == "short"]
    if sample == "sim_only":
        return [r for r in rows if r["fill"] == "sim"]
    return rows


def compute_outlook(draft: dict | None = None, scope: str = "real", sample: str = "all") -> dict:
    """The full card payload. Always returns a dict with ``status``; on any
    failure ``status='error'`` with a message - the card renders text, never
    an empty box (#615/#622)."""
    try:
        return _compute(draft or {}, scope, sample)
    except Exception:
        logger.exception("open15 outlook: compute failed")
        return {"status": "error", "message": "outlook failed - see logs"}


def _compute(draft: dict, scope: str, sample: str) -> dict:
    rows_all = load_rows(scope)
    real = [r for r in rows_all if r["fill"] == "real"]
    if not real:
        return {"status": "no_data", "message": "no real closed live fills yet"}
    saved_cfg = saved_settings()
    saved = _settings_from_config(saved_cfg)
    draft_s = _settings_from_config(
        {**saved_cfg, **{k: v for k, v in draft.items() if v is not None}}
    )

    unit = "pct" if scope == "real_sim" else "inr"
    sample_rows = _apply_sample(rows_all, sample)
    st = describe(sample_rows, unit)
    st_inr = describe([r for r in sample_rows if r["fill"] == "real"], "inr")
    sides = side_split(sample_rows, unit)
    sides_inr = side_split([r for r in sample_rows if r["fill"] == "real"], "inr")

    dates = sorted({r["date"] for r in real})
    paths = load_paths(dates)
    n_paths = sum(
        1 for r in real if (paths.get(r["date"], {}).get(r["symbol"]) or {}).get("available")
    )

    def view(rows_, settings_):
        res = replay(rows_, paths, settings_)
        res["bootstrap"] = bootstrap(list(res["day_nets"].values()))
        return res

    as_traded = {"stop": 0, "target": 0, "trail": 0, "max_trades": 99}
    pnl = {}
    for key, rows_ in (
        ("all", real),
        ("long", [r for r in real if r["side"] == "long"]),
        ("short", [r for r in real if r["side"] == "short"]),
    ):
        pnl[key] = {
            "as_traded": describe(rows_, "inr"),
            "saved": view(rows_, saved),
            "draft": view(rows_, draft_s),
            "stop_only": view(rows_, {**saved, "target": 0, "trail": 0, "lock_on": False}),
        }

    trades_per_day = len(real) / max(1, len(dates))
    presets = [
        {"key": "as_traded", "label": "as traded (no rules)", "settings": as_traded},
        {"key": "saved", "label": "saved settings", "settings": saved},
        {"key": "draft", "label": "draft (config form)", "settings": draft_s},
        {"key": "cap2", "label": "saved, 2 slots", "settings": {**saved, "max_trades": 2}},
    ]
    for p in presets:
        res = view(real, p["settings"])
        p.update(
            {
                k: res[k]
                for k in (
                    "net",
                    "n",
                    "win_rate",
                    "stops",
                    "lock_days",
                    "trail_days",
                    "skipped",
                    "closed_form_rows",
                )
            }
        )
        p["bootstrap"] = res["bootstrap"]
        p["settings"] = {
            k: v
            for k, v in p["settings"].items()
            if k in ("max_trades", "stop", "target", "trail", "stop_on", "lock_on")
        }

    return {
        "status": "ok",
        "asof": _today_ist(),
        "scope": scope,
        "sample": sample,
        "unit": unit,
        "counts": {
            "real": len(real),
            "sim_live_decided": sum(1 for r in rows_all if r["fill"] == "sim"),
            "days": len(dates),
            "paths_available": n_paths,
            "paths_missing": len(real) - n_paths,
        },
        "saved": saved,
        "draft": draft_s,
        "draft_differs": {
            k: (saved.get(k), draft_s.get(k)) for k in draft_s if saved.get(k) != draft_s.get(k)
        },
        "stats": st,
        "stats_inr": st_inr,
        "sides": sides,
        "sides_inr": sides_inr,
        "verdict": verdict(st),
        "pnl": pnl,
        "sensitivity": sensitivity(st_inr if unit == "inr" else st, trades_per_day),
        "presets": presets,
        "recommendations": recommendations(real, paths, saved_cfg, st_inr, sides_inr),
        "constants": {
            "recent_n": RECENT_N,
            "checkpoint_fills": CHECKPOINT_FILLS,
            "month_days": MONTH_DAYS,
            "bootstrap_months": BOOTSTRAP_MONTHS,
        },
    }
