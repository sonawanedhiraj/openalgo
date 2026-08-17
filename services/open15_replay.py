"""Reconstruct a missed ``open15_vol_breakout`` session from 1-minute bars (#600).

WHAT THIS IS
------------
On a day the strategy did not run — the feed died (2026-08-12 delivered zero
ticks) or the app booted late (2026-08-17, ``skipped_late_boot``) — this rebuilds
the session from the broker's historical 1m candles and journals it as a FIFTH
P&L bucket, ``fill='replay'``.

WHAT IT IS NOT
--------------
It is not the live strategy, and it must never be mistaken for it. ``open15``
is tick-driven on purpose: the gate is ``cumvol_within_minute(t) >= vol_mult x
baseline`` evaluated at a TICK, and the deployment exists to measure what
fraction of the level->close burst a mid-minute entry captures (SPEC s2/s4).
Bars cannot resolve that. So:

* **Exact** — universe, prev closes, first candle, seed selection and gaps, the
  #595 OI verdicts, the volume gate, ``top_n``/``trade_side``/shadow/caps. This
  module drives the REAL :class:`Open15Core`; it reimplements no gate.
* **Approximate** — the rolling watch list (live re-ranks twice a minute on LTP;
  bars support once).
* **Not resolvable** — the entry PRICE. A one-tick-per-minute feed can only
  trigger at the minute's CLOSE, which is Round 58's honest convention and the
  pessimistic end of a band. ``opt_entry_premium_early`` carries the optimistic
  end (the trigger minute's option open); the live fill sits between them, and
  on both 2026-08 missed days that band spans zero.

PRODUCTION ISOLATION (issue #600, G1-G7)
----------------------------------------
This strategy trades real money. Nothing here may reach it:

* **it cannot place an order** — no order path is imported, anywhere in this
  module's graph. It reads bars and writes journal rows;
* **its P&L cannot compound** — ``'replay'`` is in ``NON_REAL_FILLS``, so
  ``total_realized_pnl()`` and ``trades_pnl_by_date()`` exclude it;
* **it does not run at boot** — nothing imports this module at import time, it
  registers no scheduler job and starts no thread;
* **it cannot overwrite a traded day** — :func:`check_eligibility` refuses one,
  and :func:`replay_session` re-checks immediately before writing;
* **it does not touch the live feed** — it reads the broker HISTORICAL API
  through ``history_service`` (already rate-limited) and never subscribes ZMQ
  or opens ``historify.duckdb`` read-write. It DOES share the broker's 3 req/s
  budget, so running one inside market hours is warned about, not blocked.

CLI: ``uv run python -m services.open15_replay --date YYYY-MM-DD [--apply]``
(dry-run by default).
"""

from __future__ import annotations

import datetime as dt
import os

from utils.logging import get_logger

logger = get_logger(__name__)

# 09:15 IST == 03:45 UTC; bars carry epoch timestamps.
_IST_OFFSET = dt.timedelta(hours=5, minutes=30)
_FIRST_MINUTE = "09:15"

# Broker current-day history lags 5-15 min behind the tape, so a same-day replay
# before this reads a truncated session and silently under-reports.
_SAME_DAY_READY_AFTER = dt.time(9, 45)
# Replay makes ~250 historical calls against the same 3 req/s budget the live
# strategy needs. Inside this window that is WARNED about, not blocked
# (operator decision 2026-08-17) — the cost is the operator's to accept.
_MARKET_OPEN, _MARKET_CLOSE = dt.time(9, 0), dt.time(15, 40)

_SKIP_EVENTS = ("skipped_late_boot", "skipped_no_prev_closes", "no_ticks_received")


class ReplayIneligible(Exception):
    """Raised when a date must not be replayed. ``.reason`` is machine-readable."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _now_ist() -> dt.datetime:
    import pytz

    return dt.datetime.now(pytz.timezone("Asia/Kolkata")).replace(tzinfo=None)


def bar_minute(ts: int) -> str:
    """``HH:MM`` IST for a bar's epoch timestamp."""
    return (dt.datetime.fromtimestamp(int(ts), dt.UTC).replace(tzinfo=None) + _IST_OFFSET).strftime(
        "%H:%M"
    )


# --------------------------------------------------------------------------- #
# Eligibility
# --------------------------------------------------------------------------- #
def check_eligibility(date: str, *, allow_rereplay: bool = False) -> dict:
    """Is ``date`` replayable? Returns a dict; raises nothing.

    ``{"eligible": bool, "reason": str, "detail": str, "warning": str|None}``.
    The reason is what the button's tooltip shows, so it names the condition,
    not a stack. ``warning`` is set on a day that IS replayable but carries a
    cost worth stating — currently only market hours.
    """
    try:
        return _check_eligibility(date, allow_rereplay=allow_rereplay)
    except ReplayIneligible as e:
        return {"eligible": False, "reason": e.reason, "detail": e.detail, "warning": None}
    except Exception:
        # an eligibility check that raises must not render the button clickable
        logger.exception("open15 replay: eligibility check failed for %s", date)
        return {"eligible": False, "reason": "check_failed", "detail": "", "warning": None}


def eligibility_from(
    date: str, events: list, real_fill: bool, now: dt.datetime | None = None
) -> dict:
    """PURE eligibility — no DB, no clock unless ``now`` is omitted.

    Split out so the sidebar can judge ~20 days from data it ALREADY has
    (issue #606). The per-card endpoint used to be called once per day per 5 s
    refresh — 140 requests in 18 s, each running a ``has_real_fill`` query and a
    full day-log JSON parse against the live DB while the strategy was trading.
    """
    from services.data_freshness_service import is_trading_day

    try:
        day = dt.date.fromisoformat(date)
    except ValueError as e:
        raise ReplayIneligible("bad_date", str(e)) from e

    if not is_trading_day(day):
        raise ReplayIneligible("not_a_trading_day", date)

    now = now or _now_ist()
    # Market hours is a COST concern, not a correctness one — a replay is ~250
    # historical calls against the same 3 req/s budget the live strategy needs.
    # Operator decision (2026-08-17): warn, do not block. It rides the result as
    # a `warning` so the UI can say so without taking the choice away.
    warning = None
    if _MARKET_OPEN <= now.time() <= _MARKET_CLOSE and is_trading_day(now.date()):
        warning = "market hours — ~250 broker calls will compete with the live strategy's quota"
    # These two stay HARD blocks: they are about the answer being WRONG, not
    # expensive. A same-day replay before the broker's history has caught up
    # silently reconstructs a truncated session.
    if day == now.date() and now.time() < _SAME_DAY_READY_AFTER:
        raise ReplayIneligible("too_early", "broker current-day history lags 5-15 min")
    if day > now.date():
        raise ReplayIneligible("future_date", date)

    # THE guard: a day that traded is never rewritten (G4).
    if real_fill:
        raise ReplayIneligible("day_was_traded", "journal holds a real fill for this date")

    kinds = {e.get("event") for e in (events or []) if isinstance(e, dict)}
    ok = {"eligible": True, "detail": "", "warning": warning}
    if not events:
        return {**ok, "reason": "no_day_log", "detail": "app was down"}
    hit = kinds & set(_SKIP_EVENTS)
    if hit:
        return {**ok, "reason": sorted(hit)[0]}
    if "replay_meta" in kinds:
        return {**ok, "reason": "re_replay"}
    raise ReplayIneligible("day_ran_normally", "the session armed and ran; nothing to reconstruct")


def eligibility_or_reason(
    date: str, events: list, real_fill: bool, now: dt.datetime | None = None
) -> dict:
    """:func:`eligibility_from` with the exception folded into the result."""
    try:
        return eligibility_from(date, events, real_fill, now)
    except ReplayIneligible as e:
        return {"eligible": False, "reason": e.reason, "detail": e.detail, "warning": None}
    except Exception:
        logger.exception("open15 replay: eligibility check failed for %s", date)
        return {"eligible": False, "reason": "check_failed", "detail": "", "warning": None}


def _check_eligibility(date: str, *, allow_rereplay: bool = False) -> dict:
    """Single-date eligibility, reading what it needs from the DB.

    The authoritative path for the POST re-check. The sidebar does NOT use this —
    see :func:`eligibility_from`.
    """
    from database.open15_breakout_db import get_day_log, has_real_fill

    events = get_day_log(date) or []
    kinds = {e.get("event") for e in events if isinstance(e, dict)}
    if "replay_meta" in kinds and not allow_rereplay:
        # checked here rather than in the pure helper: "already replayed" is a
        # UI-affordance rule, not a safety one, and force=true lifts it
        raise ReplayIneligible("already_replayed", "pass force=true to re-run")
    return eligibility_from(date, events, has_real_fill(date))


# --------------------------------------------------------------------------- #
# Day config
# --------------------------------------------------------------------------- #
def resolve_replay_config(date: str) -> dict:
    """The config the session WOULD have armed with, and where it came from.

    Preference order: the date's own persisted ``armed`` event -> the current
    ``open15_config`` row -> env defaults. A config that drifted since the missed
    day silently changes the answer, so the source rides the result and is
    recorded on ``replay_meta``.
    """
    from database.open15_breakout_db import get_config, get_day_log
    from services.open15_breakout_service import resolve_day_config

    for ev in get_day_log(date) or []:
        if isinstance(ev, dict) and ev.get("event") == "armed":
            cfg = {
                "vol_mult": ev.get("vol_mult"),
                "top_n": ev.get("top_n"),
                "trade_side": ev.get("trade_side", "both"),
                "shadow_side": ev.get("shadow_side"),
                "shadow_max_trades": ev.get("shadow_max_trades", 3),
                "max_trades": ev.get("max_trades", 3),
                "margin_effective": ev.get("margin_effective") or ev.get("margin_per_slot"),
                "instrument": ev.get("instrument", "stock"),
                "rolling_enabled": bool(ev.get("rolling_watchlist_enabled")),
                "rolling_cadence_s": ev.get("rolling_cadence_s", 30),
                "rolling_top_n": ev.get("rolling_top_n", 3),
                "no_entry_after": ev.get("no_entry_after", "09:29"),
                "exit_time": ev.get("exit_time", "09:30"),
                "option_min_oi_lots": ev.get("option_min_oi_lots") or 0,
                # Gate-1 stage-1 is only reproducible from the day's own record:
                # it scores a trailing option-liquidity window we cannot rebuild.
                "excluded": list(ev.get("option_liquidity_excluded") or [])
                if ev.get("option_liquidity_gate_enabled")
                else [],
                "config_source": "armed_event",
            }
            return cfg

    live = resolve_day_config(get_config(), 0.0)
    return {
        "vol_mult": live["vol_mult"],
        "top_n": live["top_n"],
        "trade_side": live["trade_side"],
        "shadow_side": live.get("shadow_side"),
        "shadow_max_trades": live.get("shadow_max_trades", 3),
        "max_trades": live.get("max_trades", 3),
        "margin_effective": live.get("margin_effective") or live.get("margin_per_slot"),
        "instrument": live.get("instrument", "stock"),
        "rolling_enabled": bool(live.get("rolling_watchlist_enabled")),
        "rolling_cadence_s": live.get("rolling_cadence_s", 30),
        "rolling_top_n": live.get("rolling_top_n", 3),
        "no_entry_after": live.get("no_entry_after", "09:29"),
        "exit_time": live.get("exit_time", "09:30"),
        "option_min_oi_lots": live.get("option_min_oi_lots") or 0,
        "excluded": [],
        "config_source": "open15_config_row",
    }


def universe_symbols() -> list[str]:
    """The scanner universe, as the live arm reads it."""
    raw = os.getenv("SCANNER_SYMBOLS", "")
    return sorted({s.strip() for s in raw.split(",") if s.strip()})


# --------------------------------------------------------------------------- #
# Stage 1 — equity bars
# --------------------------------------------------------------------------- #
def fetch_session_bars(date: str, symbols: list[str], progress=None) -> tuple[dict, dict]:
    """``({symbol: {HH:MM: bar}}, {symbol: prev_close})`` for the 09:15-09:31 window.

    Prev closes come from the SETTLED daily bar of the previous trading day, which
    post-close is the same value the live arm's batched quote reported. Per-symbol
    failures are logged and skipped — one dead symbol must not lose the session.
    """
    from database.auth_db import get_first_available_api_key
    from services.history_service import get_history

    api_key = get_first_available_api_key()
    if not api_key:
        raise ReplayIneligible("no_broker_session", "re-login to Zerodha and retry")

    day = dt.date.fromisoformat(date)
    d_from = (day - dt.timedelta(days=12)).isoformat()
    bars: dict[str, dict[str, dict]] = {}
    prev: dict[str, float] = {}
    failed: list[str] = []

    for i, sym in enumerate(symbols, 1):
        try:
            ok, payload, _ = get_history(
                symbol=sym,
                exchange="NSE",
                interval="1m",
                start_date=date,
                end_date=date,
                api_key=api_key,
            )
            for b in (payload or {}).get("data") or [] if ok else []:
                m = bar_minute(b["timestamp"])
                if _FIRST_MINUTE <= m <= "09:31":
                    bars.setdefault(sym, {})[m] = b

            ok_d, payload_d, _ = get_history(
                symbol=sym,
                exchange="NSE",
                interval="D",
                start_date=d_from,
                end_date=date,
                api_key=api_key,
            )
            closes = sorted(
                (
                    (
                        dt.datetime.fromtimestamp(int(b["timestamp"]), dt.UTC).date().isoformat(),
                        b["close"],
                    )
                    for b in (payload_d or {}).get("data") or []
                )
                if ok_d
                else []
            )
            before = [c for d_, c in closes if d_ < date]
            if before:
                prev[sym] = before[-1]
        except Exception:
            logger.exception("open15 replay: bar fetch failed for %s", sym)
            failed.append(sym)
        if progress:
            progress(i, len(symbols))

    if failed:
        logger.warning(
            "open15 replay %s: %d symbols failed to fetch: %s", date, len(failed), failed
        )
    return bars, prev


# --------------------------------------------------------------------------- #
# Stage 2 — ATM contracts + the OI verdicts the #595 filter needs
# --------------------------------------------------------------------------- #
def resolve_contracts_and_oi(date: str, candidates: list[tuple[str, str]], bars: dict) -> dict:
    """``{"SYM|SIDE": {contract fields, oi_lots, bars}}`` for the OI filter and pricing.

    **OI is read off the 09:15 bar, NOT 09:16.** A bar's ``oi`` is the value at
    the END of its minute, so the 09:15 bar is stamped ~09:16:00 — the instant
    the live filter's batched quote is taken. Verified against 2026-08-14's own
    decision log, where the live verdicts were MFSL 418 / JUBLFOOD 346 /
    CHOLAFIN 482 lots: the 09:15 bar reproduces all three EXACTLY. The 09:16 bar
    reads MFSL at 791 (its OI was ramping hard that morning) and on 2026-08-17
    it let NMDC clear the 500-lot floor, producing a phantom +Rs17,924 on a
    Rs1.67 put. Do not "simplify" this to the trigger minute.
    """
    from services.open15_option_shadow import _fetch_1m_bars, resolve_atm_option

    out: dict[str, dict] = {}
    series: dict[str, dict] = {}
    for sym, side in candidates:
        first = bars.get(sym, {}).get(_FIRST_MINUTE)
        if not first:
            continue
        contract = resolve_atm_option(sym, side, first["close"], date)
        if not contract:
            logger.info("open15 replay: no ATM contract for %s/%s", sym, side)
            continue
        osym = contract["symbol"]
        if osym not in series:
            series[osym] = {
                bar_minute(b["timestamp"]): b for b in (_fetch_1m_bars(osym, date) or [])
            }
        ob = series[osym]
        anchor = ob.get(_FIRST_MINUTE) or ob.get("09:16")
        oi = (anchor or {}).get("oi")
        lot = int(contract.get("lotsize") or 0)
        out[f"{sym}|{side}"] = {
            **contract,
            # 0 / None means "not available" and must fail OPEN (#555, #390) —
            # treating unknown OI as thin would silently narrow the universe.
            "oi_lots": round(oi / lot, 1) if oi and lot else None,
            "bars": ob,
        }
    return out


def make_oi_filter(contracts: dict, min_lots: int):
    """Replay of the broker-OI watch-list filter (#595). ``None`` when disabled."""
    if min_lots <= 0:
        return None

    def _fn(candidates):
        verdicts = {}
        for c in candidates:
            v = contracts.get(f"{c['symbol']}|{c['side']}")
            if not v or v.get("oi_lots") is None:
                continue  # fail open, exactly as the live filter does
            verdicts[(c["symbol"], c["side"])] = {
                "blocked": v["oi_lots"] < min_lots,
                "oi_lots": v["oi_lots"],
                "opt_symbol": v["symbol"],
                "min_lots": min_lots,
            }
        return verdicts

    return _fn


# --------------------------------------------------------------------------- #
# Stage 3 — drive the REAL core
# --------------------------------------------------------------------------- #
def _hhmm_to_min(v: str) -> int:
    h, m = v.split(":")
    return int(h) * 60 + int(m)


def _feed_minute(core, present: list, cum: dict, ts) -> list[dict]:
    """Offer one minute's synthetic tick per symbol; return any entry actions.

    Idempotent for a given ``(present, cum, ts)``: ``on_tick`` returns early once
    a symbol is in ``core.entered``, and the cumulative volume passed is
    absolute, so calling this twice inside a minute cannot double-count.
    """
    out = []
    for sym, row in present:
        action = core.on_tick(sym, row["close"], cum[sym], ts)
        if action:
            action["entry_minute_close"] = row["close"]
            out.append(action)
    return out


def run_core(date: str, cfg: dict, bars: dict, prev: dict, contracts: dict) -> dict:
    """Replay the session through :class:`Open15Core`. Reimplements no gate."""
    from services.open15_breakout_service import Open15Core

    excluded = set(cfg["excluded"])
    universe = sorted(s for s in bars if s in prev and s not in excluded)
    day = dt.date.fromisoformat(date)

    core = Open15Core(
        prev_closes={s: prev[s] for s in universe},
        vol_mult=cfg["vol_mult"],
        top_n=cfg["top_n"],
        entry_to_min=_hhmm_to_min(cfg["no_entry_after"]),
        track_to_min=_hhmm_to_min(cfg["exit_time"]),
        baseline_includes_first_minute=False,
        await_snapshot=True,
        trade_side=cfg["trade_side"],
        rolling_enabled=cfg["rolling_enabled"],
        rolling_cadence_s=cfg["rolling_cadence_s"],
        rolling_top_n=cfg["rolling_top_n"],
        shadow_side=cfg.get("shadow_side"),
        oi_filter_fn=make_oi_filter(contracts, int(cfg.get("option_min_oi_lots") or 0)),
    )

    # The live 09:16 quote snapshot's open/high/low ARE the 09:15 candle's
    # extremes (SPEC s3) — which is exactly what the 09:15 1m bar carries.
    core.apply_first_candles(
        {
            s: {"open": b["open"], "high": b["high"], "low": b["low"]}
            for s in universe
            if (b := bars[s].get(_FIRST_MINUTE))
        }
    )

    cum = dict.fromkeys(universe, 0.0)
    actions: list[dict] = []
    for minute in range(_hhmm_to_min(_FIRST_MINUTE), _hhmm_to_min(cfg["exit_time"]) + 1):
        hh, mm = divmod(minute, 60)
        key = f"{hh:02d}:{mm:02d}"
        ts = dt.datetime.combine(day, dt.time(hh, mm, 59))
        present = [(s, bars[s][key]) for s in universe if key in bars.get(s, {})]
        for sym, row in present:
            cum[sym] += row["volume"]
        # Pass 1 feeds every symbol so the re-rank below ranks a CONSISTENT
        # price set; pass 2 re-offers the same tick so a symbol the re-rank just
        # added can still trigger inside its own minute, as it could live.
        actions += _feed_minute(core, present, cum, ts)
        if core.rolling_enabled:
            core.maybe_rerank(ts)
            actions += _feed_minute(core, present, cum, ts)

    # max_trades / shadow_max_trades are enforced by the SERVICE, not the core.
    # Arrival order inside a shared minute is unknowable from bars, so ties
    # resolve alphabetically and ``cap_bound`` reports when that mattered.
    actions.sort(key=lambda a: (a["trigger_min_idx"], a["symbol"]))
    n_real = n_shadow = 0
    for a in actions:
        if a["shadow"]:
            a["slot"] = "shadow" if n_shadow < cfg["shadow_max_trades"] else "shadow_cap"
            n_shadow += a["slot"] == "shadow"
        else:
            a["slot"] = "real" if n_real < cfg["max_trades"] else "max_trades_cap"
            n_real += a["slot"] == "real"

    return {
        "universe_n": len(universe),
        "selected": dict(core.selected),
        "gaps": {s: round(core.gaps.get(s, 0.0) * 100, 3) for s in core.selected},
        "watch_source": dict(core.watch_source),
        "rolling_adds": list(core.rolling_adds),
        "watch_stats": core.watch_snapshot(),
        "oi_exclusions": [e for e in core.liquidity_exclusions if e.get("stage") == 3],
        "actions": actions,
        "exits": {
            a["symbol"]: (bars.get(a["symbol"], {}).get(cfg["exit_time"]) or {}).get("open")
            for a in actions
        },
    }


# --------------------------------------------------------------------------- #
# Stage 4 — price the option legs
# --------------------------------------------------------------------------- #
def _next_minute(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    return f"{h + (m + 1) // 60:02d}:{(m + 1) % 60:02d}"


def price_legs(cfg: dict, run: dict, contracts: dict) -> list[dict]:
    """One priced row per action, on the live option-shadow conventions.

    Entry = OPEN of the minute AFTER the trigger (a market order at the minute's
    close fills on the next prints); exit = the exit minute's bar OPEN. Charges
    are the same modelled Zerodha option round-trip the service books.
    """
    from services.open15_option_shadow import option_round_trip_charges

    slot = float(cfg.get("margin_effective") or 30_000)
    exit_min = cfg["exit_time"]
    rows = []
    for a in run["actions"]:
        sym, side = a["symbol"], a["side"]
        row = {
            "symbol": sym,
            "side": side,
            "slot": a["slot"],
            "watch_source": a.get("watch_source", "seed"),
            "shadow": a["shadow"],
            "trigger_minute": a["trigger_minute"],
            "trigger_second": a["trigger_second"],
            "trigger_price": a["price"],
            "level": a["level"],
            "gap_pct": a["gap_pct"],
            "baseline_vol": a["baseline_vol"],
            "cum_vol_at_trigger": a["cum_vol_at_trigger"],
            "entry_minute_close": a["entry_minute_close"],
        }
        c = contracts.get(f"{sym}|{side}")
        if not c:
            rows.append({**row, "status": "skipped", "reason": "no_option_contract"})
            continue
        ob, lot = c["bars"], int(c.get("lotsize") or 0)
        nxt, trig = ob.get(_next_minute(a["trigger_minute"])), ob.get(a["trigger_minute"])
        entry = (nxt or {}).get("open") or (trig or {}).get("close")
        exit_prem = (ob.get(exit_min) or {}).get("open")
        row.update(
            opt_symbol=c["symbol"],
            opt_lot_size=lot,
            opt_entry_premium=entry,
            opt_exit_premium=exit_prem,
            opt_entry_premium_early=(trig or {}).get("open"),
        )
        if not entry or not exit_prem or not lot:
            rows.append({**row, "status": "skipped", "reason": "no_option_quote"})
            continue

        lots = int(slot // (entry * lot))
        unaffordable = lots < 1
        qty = max(lots, 1) * lot  # the #555 sim convention: price it at 1 lot and say so
        gross = round((exit_prem - entry) * qty, 2)
        charges = option_round_trip_charges(entry * qty, exit_prem * qty) or 0.0
        # A trigger in the LAST entry minute fills at the exit bar's open, which
        # is also the exit price — a ~0s hold that reduces to charges. Live it
        # would have held 30-50s. Flagged so the UI can grey it rather than
        # letting it read as a real observation.
        degenerate = a["trigger_minute"] == cfg["no_entry_after"]
        rows.append(
            {
                **row,
                "status": "closed",
                "reason": "unaffordable"
                if unaffordable
                else ("degenerate_hold" if degenerate else None),
                "quantity": qty,
                "pnl": gross,
                "charges_inr": round(charges, 2),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Events + persistence
# --------------------------------------------------------------------------- #
def _early_net(row: dict) -> float | None:
    from database.open15_breakout_db import early_entry_net_pnl

    return early_entry_net_pnl(row)


def build_events(date: str, cfg: dict, run: dict, rows: list[dict], meta: dict) -> list[dict]:
    """The day's decision log, in the LIVE event vocabulary.

    The logs page renders a day by walking these events, so emitting the same
    names is what makes a replayed day render like a real one with no per-event
    UI change. Every event carries ``replay: True``. Only two names are new:
    ``replay_meta`` (provenance, drives the banner) and ``exit_replay`` — the
    latter deliberately NOT called ``exit``, because the digest sums events by
    name and folding a reconstruction into the traded bucket would destroy the
    comparison that bucket exists for (the #581 lesson).
    """
    exit_t = cfg["exit_time"]
    ev: list[dict] = [{"ts": "09:10:00.000", "event": "replay_meta", "replay": True, **meta}]
    ev.append(
        {
            "ts": "09:10:00.001",
            "event": "armed",
            "replay": True,
            "universe": run["universe_n"],
            "vol_mult": cfg["vol_mult"],
            "top_n": cfg["top_n"],
            "mode": "replay",
            "no_entry_after": cfg["no_entry_after"],
            "exit_time": exit_t,
            "trade_side": cfg["trade_side"],
            "shadow_side": cfg.get("shadow_side"),
            "instrument": cfg.get("instrument"),
            "margin_effective": cfg.get("margin_effective"),
            "rolling_watchlist_enabled": cfg["rolling_enabled"],
            "rolling_cadence_s": cfg["rolling_cadence_s"],
            "rolling_top_n": cfg["rolling_top_n"],
            "option_min_oi_lots": cfg.get("option_min_oi_lots"),
            "config_source": cfg.get("config_source"),
        }
    )
    seed = {
        s: d for s, d in run["selected"].items() if run["watch_source"].get(s, "seed") == "seed"
    }
    ev.append(
        {
            "ts": "09:16:00.000",
            "event": "selection",
            "replay": True,
            "selected": seed,
            "gaps_pct": {s: run["gaps"].get(s) for s in seed},
            "candidates": run["universe_n"],
        }
    )
    for x in run["oi_exclusions"]:
        ev.append({"ts": "09:16:00.100", "event": "universe_excluded", "replay": True, **x})
    for add in run["rolling_adds"]:
        ev.append({"ts": add["at"] + ".000", "event": "watchlist_add", "replay": True, **add})

    for r in rows:
        ev.append(
            {
                "ts": f"{r['trigger_minute']}:{r.get('trigger_second') or 0:02d}.000",
                # a DISTINCT name, like entry_shadow (#581): the digest counts
                # `entry` only when order_status=='success', so a replay entry
                # would silently vanish from every bucket — and if that guard
                # ever loosened it would be counted as a real fill instead.
                "event": "entry_replay",
                "replay": True,
                "fill": "replay",
                "quantity": r.get("quantity"),
                **{
                    k: r.get(k)
                    for k in (
                        "symbol",
                        "side",
                        "watch_source",
                        "slot",
                        "level",
                        "trigger_minute",
                        "trigger_price",
                        "opt_symbol",
                        "opt_lot_size",
                        "opt_entry_premium",
                    )
                },
            }
        )
    for r in rows:
        if r["status"] != "closed":
            continue
        ev.append(
            {
                "ts": exit_t + ":00.000",
                "event": "exit_replay",
                "replay": True,
                "symbol": r["symbol"],
                "fill": "replay",
                "reason": r.get("reason"),
                "opt_symbol": r.get("opt_symbol"),
                "opt_exit_premium": r.get("opt_exit_premium"),
                # the identical gross/charges/net triple every exit event emits (#552)
                "gross": r["pnl"],
                "charges": r["charges_inr"],
                "pnl": round((r["pnl"] or 0.0) - (r["charges_inr"] or 0.0), 2),
                "net_early": _early_net(r),
            }
        )
    ev.append(
        {
            "ts": exit_t + ":05.000",
            "event": "watch_stats",
            "replay": True,
            "stats": run["watch_stats"],
            "needed": cfg["vol_mult"],
        }
    )
    closed = [r for r in rows if r["status"] == "closed"]
    earlies = [e for r in closed if (e := _early_net(r)) is not None]
    ev.append(
        {
            "ts": exit_t + ":10.000",
            "event": "summary",
            "replay": True,
            "selected": len(run["selected"]),
            "entered": len(run["actions"]),
            "replayed": len(closed),
            "rolling_added": len(run["rolling_adds"]),
            "day": "replayed",
            "net_close_entry": round(
                sum((r["pnl"] or 0.0) - (r["charges_inr"] or 0.0) for r in closed), 2
            ),
            "net_early_entry": round(sum(earlies), 2) if earlies else None,
        }
    )
    return ev


_JOURNAL_FIELDS = (
    "symbol",
    "side",
    "gap_pct",
    "level",
    "baseline_vol",
    "cum_vol_at_trigger",
    "trigger_minute",
    "trigger_second",
    "trigger_price",
    "entry_minute_close",
    "quantity",
    "opt_symbol",
    "opt_lot_size",
    "opt_entry_premium",
    "opt_exit_premium",
    "opt_entry_premium_early",
    "pnl",
    "charges_inr",
    "watch_source",
    "reason",
)


def persist(date: str, cfg: dict, rows: list[dict], events: list[dict]) -> dict:
    """Write the journal rows and the day log. Re-checks the traded-day guard.

    The re-check is not belt-and-braces: eligibility runs at button-render time
    and the fetch that follows takes minutes, so the only check that can be
    trusted is the one taken immediately before the write (the #597 class).
    """
    from database.open15_breakout_db import (
        delete_replay_rows,
        has_real_fill,
        insert_trade,
        save_day_log,
    )

    if has_real_fill(date):
        raise ReplayIneligible("day_was_traded", "refusing to overwrite a traded session")

    delete_replay_rows(date)  # idempotent re-run: replace, never duplicate
    written = 0
    for r in rows:
        if r["status"] != "closed":
            continue
        payload = {k: r.get(k) for k in _JOURNAL_FIELDS if r.get(k) is not None}
        if insert_trade(
            trade_date=date,
            mode="replay",
            fill="replay",
            status="closed",
            instrument="option" if cfg.get("instrument") == "atm_option" else "stock",
            **payload,
        ):
            written += 1
    save_day_log(date, events)
    return {"rows_written": written}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def replay_session(date: str, *, apply: bool = False, force: bool = False, progress=None) -> dict:
    """Full replay for ``date``. Dry-run unless ``apply``. Raises ReplayIneligible."""
    elig = _check_eligibility(date, allow_rereplay=force)
    cfg = resolve_replay_config(date)
    symbols = universe_symbols()
    started = _now_ist()

    bars, prev = fetch_session_bars(date, symbols, progress=progress)
    if not bars:
        raise ReplayIneligible("no_bars", "broker returned no 1m data for this session")

    # Candidate pool for the OI filter: the deep gap ranking on both sides. The
    # live filter judges top_n+5 per side plus rolling additions; going deeper
    # here costs nothing (verdicts are day-cached) and covers backfill promotions.
    gaps = {
        s: b["open"] / prev[s] - 1.0
        for s, rows_ in bars.items()
        if s in prev and prev[s] and (b := rows_.get(_FIRST_MINUTE))
    }
    pos = sorted((s for s in gaps if gaps[s] > 0), key=lambda s: -gaps[s])[:20]
    neg = sorted((s for s in gaps if gaps[s] < 0), key=lambda s: gaps[s])[:20]
    contracts = resolve_contracts_and_oi(
        date, [(s, "L") for s in pos] + [(s, "S") for s in neg], bars
    )

    run = run_core(date, cfg, bars, prev, contracts)
    # A rolling addition can rank deeper than the pool above; resolve whatever
    # the run actually selected but the pool missed, then price.
    missing = [(s, d) for s, d in run["selected"].items() if f"{s}|{d}" not in contracts]
    if missing:
        contracts.update(resolve_contracts_and_oi(date, missing, bars))
    rows = price_legs(cfg, run, contracts)

    meta = {
        "ran_at": started.strftime("%Y-%m-%d %H:%M:%S"),
        "eligible_reason": elig.get("reason"),
        "config_source": cfg.get("config_source"),
        "symbols_fetched": len(bars),
        "symbols_requested": len(symbols),
        "contracts_resolved": len(contracts),
        "engine": "1m-bar close-entry (R58 honest convention)",
        "caveat": "entry price is not resolvable from bars; band = close-entry .. early-entry",
    }
    events = build_events(date, cfg, run, rows, meta)
    out = {"date": date, "config": cfg, "meta": meta, "run": run, "rows": rows, "events": events}
    out["persisted"] = persist(date, cfg, rows, events) if apply else {"rows_written": 0}
    return out


def main() -> None:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Replay a missed open15 session from 1m bars.")
    ap.add_argument("--date", required=True)
    ap.add_argument("--apply", action="store_true", help="write; omit for a dry run")
    ap.add_argument("--force", action="store_true", help="re-run an already-replayed day")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    try:
        out = replay_session(args.date, apply=args.apply, force=args.force)
    except ReplayIneligible as e:
        raise SystemExit("ineligible — " + str(e)) from e

    if args.json:
        print(json.dumps(out, indent=1, default=str))
        return

    meta, cfg = out["meta"], out["config"]
    print(f"=== {args.date} replay ({'APPLIED' if args.apply else 'dry run'}) ===")
    print(
        f"config from {cfg['config_source']} · universe {out['run']['universe_n']} · "
        f"{meta['symbols_fetched']}/{meta['symbols_requested']} symbols fetched"
    )
    for r in out["rows"]:
        if r["status"] != "closed":
            print(f"  {'':5} {r['symbol']:12} {r['side']}  SKIP {r['reason']}")
            continue
        net = round((r["pnl"] or 0.0) - (r["charges_inr"] or 0.0), 2)
        early = _early_net(r)
        early_txt = "" if early is None else f"  early {early:>10,.2f}"
        flag = f"  [{r['reason']}]" if r.get("reason") else ""
        print(
            f"  {r['trigger_minute']} {r['symbol']:12} {r['side']} {r['slot']:14} "
            f"{r.get('opt_symbol') or '':26} net {net:>10,.2f}{early_txt}{flag}"
        )
    s = out["events"][-1]
    print(f"\nclose-entry Rs{s['net_close_entry']} .. early-entry Rs{s['net_early_entry']}")
    print(f"rows written: {out['persisted']['rows_written']}")


if __name__ == "__main__":
    main()
