"""Pure, broker-agnostic evaluator for the intraday_pullback_top2 combined strategy (issue #394).

This is the *validated backtest logic* ported to a live, incremental form: a per-stock state
machine fed one closed 5-minute candle at a time. The offline research harness
(`backtest/simplified_engine/userstrat_20mo_6535.py` and this session's `r53_combo.py`) evaluated
the whole day's bars in one pass; here the identical rules run candle-by-candle so the live service
can act on each new bar. Feeding the full day's bars sequentially reproduces the backtest exactly —
which is what the unit tests assert.

No I/O, no broker calls, no globals. Percent units throughout (band [1.0, 2.5], gate 0.30 = 0.30%),
matching the config and the harness. The live service converts feed fractions to percent before
calling in.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

# Valid operator trade-side selections (issue #509). Shared by the service's
# day-config merge and the blueprint's request validation so the two can never
# disagree about what is accepted.
TRADE_SIDES = ("both", "long_only", "short_only")

# Which book each selection permits, keyed by the core's side codes.
_TRADE_SIDE_ALLOWS = {
    "both": ("L", "S"),
    "long_only": ("L",),
    "short_only": ("S",),
}


def trade_side_allows(trade_side: str | None, side: str) -> bool:
    """Is ``side`` ('L'|'S') enabled under this operator ``trade_side``?

    Unknown/None falls back to ``both`` — an unrecognised stored value must
    never silently dark a book (fail-open to the backtested default).
    """
    allowed = _TRADE_SIDE_ALLOWS.get(str(trade_side or "both").lower())
    if allowed is None:
        return True
    return side in allowed


# 5-minute candle tuple: (ts: datetime, open, high, low, close, volume)
Candle = tuple


@dataclass(frozen=True)
class PullbackConfig:
    """All tunables. Values are percent where a % is implied (band, gates, stop floor)."""

    # windows (IST times)
    morning: tuple = (dt.time(9, 30), dt.time(11, 0))
    afternoon: tuple = (dt.time(13, 0), dt.time(15, 0))
    eod_flatten: dt.time = dt.time(15, 15)

    # long book
    long_band: tuple = (1.0, 2.5)  # [lo, hi) percent 09:30 gain
    long_nf_mom: bool = True
    long_noreentry_sl: bool = True

    # short book (deep losers)
    short_band: tuple = (-5.0, -3.0)  # (lo, hi] percent 09:30 loss
    short_nf_mom: bool = False
    short_noreentry_sl: bool = False

    # shared entry mechanics
    vol_multiplier: float = 2.5
    vol_avg_window: int = 6  # candles (~30 min)
    market_gate_pct: float = 0.30  # NIFTY fresh-gate at entry
    stop_floor_pct: float = 0.3
    max_attempts: int = 2
    candle_minutes: int = 5

    # Operator trade-side gate (issue #509): 'both' | 'long_only' | 'short_only'.
    # NOTE the day-gate interaction: the two books are mutually exclusive by
    # NIFTY direction at 09:30 (up -> long, down -> short), so this does NOT
    # rebalance a mixed book — excluding a side means the strategy simply does
    # not trade on the days that side would have run. 'long_only' gives up
    # every NIFTY-down day. Default 'both' = the backtested behaviour.
    trade_side: str = "both"


@dataclass
class GateContext:
    """Live index/market readings supplied by the service at the moment a candle closes.

    All returns are in PERCENT vs previous close.
    """

    nifty_ret_now: float | None  # NIFTY intraday return at this candle
    sector_ret_now: float | None  # the stock's sector index intraday return
    nifty_ret_930: float | None  # NIFTY's 09:30 return (fixed at selection)
    slot_available: bool  # is a margin slot free right now


# ---- action records returned to the service --------------------------------------------------


@dataclass
class EntryAction:
    ts: dt.datetime
    price: float
    stop: float
    side: str  # 'L' | 'S'


@dataclass
class ExitAction:
    ts: dt.datetime
    price: float
    reason: str  # 'SL' | 'EOD'


def _in_window(t: dt.time, cfg: PullbackConfig) -> bool:
    return (cfg.morning[0] <= t < cfg.morning[1]) or (cfg.afternoon[0] <= t < cfg.afternoon[1])


class StockState:
    """Per-stock intraday state machine for ONE side (long or short).

    Call ``process_candle`` for each closed 5m candle in chronological order. It returns a list of
    zero or more actions (an entry, and/or an exit) that the service should mirror into real orders.
    The state machine itself tracks the open position and stop, so exits (SL/EOD) are emitted here.
    """

    def __init__(self, side: str, cfg: PullbackConfig):
        if side not in ("L", "S"):
            raise ValueError(f"side must be 'L' or 'S', got {side!r}")
        self.side = side
        self.cfg = cfg
        self.attempts = 0
        self.ref: tuple | None = None  # (open, high, low, vol) of the reference candle
        self.pos: tuple | None = None  # (entry_ts, entry_price, stop_price)
        self.prior_vols: list[float] = []  # volumes of already-processed candles
        self.done = False  # set by noreentry-after-SL
        # per-day diagnostics — why an entry did/didn't fire (observability, not logic)
        self.diag = {
            "candles": 0,  # candles evaluated
            "ref_formed": 0,  # low-volume reference (no-supply) candles seen
            "breakouts": 0,  # candles meeting the 2.5x-vol + close-vs-ref trigger
            "gate_blocked": 0,  # a breakout that the NIFTY/sector fresh gate rejected
            "no_slot": 0,  # a breakout that couldn't enter (both slots busy)
            "entries": 0,
            "exits": 0,
        }

    @property
    def _nf_mom(self) -> bool:
        return self.cfg.long_nf_mom if self.side == "L" else self.cfg.short_nf_mom

    @property
    def _noreentry(self) -> bool:
        return self.cfg.long_noreentry_sl if self.side == "L" else self.cfg.short_noreentry_sl

    def _gate_ok(self, ctx: GateContext) -> bool:
        nf, sc = ctx.nifty_ret_now, ctx.sector_ret_now
        if self.side == "L":
            if not (
                nf is not None and nf >= self.cfg.market_gate_pct and sc is not None and sc > 0
            ):
                return False
            if self._nf_mom and ctx.nifty_ret_930 is not None and nf < ctx.nifty_ret_930:
                return False
        else:
            if not (
                nf is not None and nf <= -self.cfg.market_gate_pct and sc is not None and sc < 0
            ):
                return False
            if self._nf_mom and ctx.nifty_ret_930 is not None and nf > ctx.nifty_ret_930:
                return False
        return True

    def has_open_position(self) -> bool:
        return self.pos is not None

    def process_candle(self, candle: Candle, ctx: GateContext) -> list:
        ts, o, h, lo, c, v = candle
        actions: list = []
        self.diag["candles"] += 1

        # 1) manage an open position first (stop / EOD) — frees the slot
        if self.pos is not None:
            ets, entry, stop = self.pos
            breached = (lo <= stop) if self.side == "L" else (h >= stop)
            if breached:
                actions.append(ExitAction(ts=ts, price=stop, reason="SL"))
                self.pos = None
                self.diag["exits"] += 1
                if self._noreentry:
                    self.done = True
            elif ts.time() >= self.cfg.eod_flatten:
                actions.append(ExitAction(ts=ts, price=c, reason="EOD"))
                self.pos = None
                self.diag["exits"] += 1
            self.prior_vols.append(v)
            return actions

        # no position: maybe enter
        if self.done or self.attempts >= self.cfg.max_attempts:
            self.prior_vols.append(v)
            return actions

        entered = False
        if self.ref is not None and _in_window(ts.time(), self.cfg):
            ro, rh, rl, rv = self.ref
            rec = self.prior_vols[-self.cfg.vol_avg_window :]
            avg = (sum(rec) / len(rec)) if rec else v
            vol_ok = v >= self.cfg.vol_multiplier * avg
            close_ok = (c > ro) if self.side == "L" else (c < ro)
            if vol_ok and close_ok:  # a breakout candle (2.5x vol + close vs ref-open)
                self.diag["breakouts"] += 1
                if not self._gate_ok(ctx):
                    self.diag["gate_blocked"] += 1
                elif not ctx.slot_available:
                    self.diag["no_slot"] += 1
                else:
                    floor = self.cfg.stop_floor_pct / 100.0 * c
                    if self.side == "L":
                        dd = max(c - rl, floor)
                        stop = c - dd
                    else:
                        dd = max(rh - c, floor)
                        stop = c + dd
                    self.pos = (ts, c, stop)
                    self.attempts += 1
                    self.ref = None
                    actions.append(EntryAction(ts=ts, price=c, stop=stop, side=self.side))
                    entered = True
                    self.diag["entries"] += 1
            # a breakout that was gate-blocked or slot-blocked retains its ref (a breakout candle
            # can never satisfy the reference condition below), so it retries on the next breakout.

        # 2) update the reference candle (skipped only when we just entered)
        if not entered:
            prev2 = self.prior_vols[-2:]
            low_vol = (not prev2) or (v <= min(prev2))
            is_ref = (c < o) if self.side == "L" else (c > o)
            if is_ref and low_vol:
                self.ref = (o, h, lo, v)
                self.diag["ref_formed"] += 1

        self.prior_vols.append(v)
        return actions

    def reason(self) -> str:
        """One-line explanation of why this pick did / didn't produce an entry today."""
        d = self.diag
        if d["entries"] > 0:
            return "entered"
        if d["no_slot"] > 0:
            return "breakout formed but no free slot (both positions held)"
        if d["gate_blocked"] > 0:
            return "breakout formed but the live NIFTY/sector gate blocked it"
        if d["ref_formed"] == 0:
            return "no low-volume reference (no-supply pullback) candle formed"
        if d["breakouts"] == 0:
            return "reference formed but no >=2.5x-volume breakout candle followed"
        return "no valid entry setup"

    def force_eod(self, ts: dt.datetime, price: float) -> ExitAction | None:
        """Emit a flatten for any still-open position (watchdog backstop)."""
        if self.pos is None:
            return None
        self.pos = None
        return ExitAction(ts=ts, price=price, reason="EOD")


# ---- selection (pure) ------------------------------------------------------------------------


def select_top2(
    side: str,
    stock_returns: dict[str, float],
    sector_of: dict[str, str],
    sector_returns: dict[str, float | None],
    cfg: PullbackConfig,
) -> list[str]:
    """Rank the day's top-2 for a side. All returns in PERCENT.

    side 'L': stock ret in [lo, hi), sector green (>0), rank by ret desc.
    side 'S': stock ret in (lo, hi] (deep-loser band), sector red (<0), rank by ret asc.
    """
    lo, hi = cfg.long_band if side == "L" else cfg.short_band
    cand = []
    for sym, ret in stock_returns.items():
        if ret is None:
            continue
        sec = sector_of.get(sym)
        sret = sector_returns.get(sec) if sec else None
        if sret is None:
            continue
        if side == "L":
            if sret > 0 and lo <= ret < hi:
                cand.append(sym)
        else:
            if sret < 0 and lo < ret <= hi:
                cand.append(sym)
    reverse = side == "L"
    cand.sort(key=lambda s: stock_returns[s], reverse=reverse)
    return cand[:2]
