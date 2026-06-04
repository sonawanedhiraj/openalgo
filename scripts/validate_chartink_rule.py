"""Day-1 validation of the new ``fno_intraday_buy_chartink`` rule.

Skips the scheduler-driven 10-day shadow validation. Instead validates the
rule's *daily-only* gates against today's (2026-06-04) cached data:

  Set A  — operator's Chartink ``fno-intraday-buy-20`` hits (0 today; the
           baseline JSON was not captured, documented value is 0).
  Set B  — union of symbols across the 7 NL-generated FnO scans.
  Set C  — in-house daily-gates-only matches over the F&O daily universe.

Only gates 1, 2, 6, 7, 8, 9, 10, 12 are evaluable from daily/weekly bars.
Gates 3, 4, 5, 13 (Supertrend, RSI, 5m vol) need intraday tape we don't have
for today, so they are intentionally skipped here. Production rule is NOT
modified — gate logic is re-implemented inline against daily/weekly frames.

Run: ``uv run python scripts/validate_chartink_rule.py``
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from database.historify_db import get_ohlcv  # noqa: E402
from services.indicators import atr, sma  # noqa: E402

OUTPUTS = ROOT / "outputs"
DATE = "2026-06-04"
BASELINE_JSON = OUTPUTS / f"chartink_fno_baseline_{DATE}.json"
NL_JSON = OUTPUTS / f"chartink_nl_scans_{DATE}.json"
REPORT = OUTPUTS / f"chartink_rule_validation_{DATE}.md"
DUCKDB_PATH = ROOT / "db" / "historify.duckdb"
EXCHANGE = "NSE"


def _any_nan(*values: float) -> bool:
    return any(pd.isna(v) for v in values)


def load_set_a() -> tuple[set[str], list[str]]:
    """Operator's fno-intraday-buy-20 BUY hits. Baseline JSON optional."""
    notes: list[str] = []
    if not BASELINE_JSON.exists():
        notes.append(
            f"Baseline file {BASELINE_JSON.name} NOT present; documented "
            "fno-intraday-buy-20 count today is 0 (BUY side). Set A = empty."
        )
        return set(), notes
    data = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for scan in data.get("scans", []):
        name = (scan.get("scan_name") or scan.get("name") or "").lower()
        if "fno-intraday-buy" in name or "fno_intraday_buy" in name:
            for s in scan.get("top_matching_stocks", scan.get("stocks", [])):
                sym = s.get("symbol") if isinstance(s, dict) else s
                if sym:
                    symbols.add(sym.upper())
    notes.append(f"Loaded {len(symbols)} operator BUY symbols from baseline.")
    return symbols, notes


def load_set_b() -> tuple[set[str], list[str]]:
    """Union of all symbols across the 7 NL-generated scans."""
    notes: list[str] = []
    data = json.loads(NL_JSON.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    n_scans = 0
    for scan in data.get("scans", []):
        n_scans += 1
        for s in scan.get("top_matching_stocks", []):
            sym = s.get("symbol")
            if sym:
                symbols.add(sym.upper())
    notes.append(
        f"Union of {len(symbols)} symbols across {n_scans} NL scans. "
        "NOTE: NL builder ignored the 'FnO' qualifier — these are cash-segment "
        "scans, and row display was truncated to ~10-13 per scan by the capture."
    )
    return symbols, notes


def get_universe() -> tuple[list[str], list[str]]:
    """Distinct symbols with daily bars in DuckDB market_data."""
    notes: list[str] = []
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM market_data "
            "WHERE interval = 'D' AND exchange = ? ORDER BY symbol",
            [EXCHANGE],
        ).fetchall()
    finally:
        con.close()
    syms = [r[0] for r in rows]
    notes.append(f"Universe: {len(syms)} distinct symbols with daily bars (exchange={EXCHANGE}).")
    return syms, notes


def eval_daily_gates(symbol: str) -> tuple[bool | None, str]:
    """Evaluate the 8 daily-only gates. Returns (passed, reason).

    passed is None when warm-up/data is insufficient (excluded from Set C).
    """
    daily = get_ohlcv(symbol, EXCHANGE, "D")
    if daily is None or len(daily) < 200:
        return None, f"insufficient daily bars ({0 if daily is None else len(daily)} < 200)"
    weekly = get_ohlcv(symbol, EXCHANGE, "W")
    if weekly is None or len(weekly) < 22:
        return None, f"insufficient weekly bars ({0 if weekly is None else len(weekly)} < 22)"

    # End-of-day validation on settled bars: today = -1, yesterday = -2.
    today = daily.iloc[-1]
    yest = daily.iloc[-2]
    if _any_nan(today.close, today.open, today.volume, yest.close, yest.high, yest.low):
        return None, "NaN in required daily fields"

    # Gate 6: close > 100
    if today.close <= 100:
        return False, "gate6 close<=100"
    # Gate 12: close < 5000
    if today.close >= 5000:
        return False, "gate12 close>=5000"
    # Gate 1: close > 1d-ago close * 1.03
    if today.close <= yest.close * 1.03:
        return False, "gate1 no 3% gap on close"
    # Gate 9: open > 1d-ago close
    if today.open <= yest.close:
        return False, "gate9 open<=prev close"
    # Gate 10: open > pivot (H+L+C of prev)/3
    pivot = (yest.high + yest.low + yest.close) / 3.0
    if today.open <= pivot:
        return False, "gate10 open<=pivot"
    # Gates 2 + 8: volume vs SMA(50) and SMA(200)
    sma50 = sma(daily["volume"], 50).iloc[-1]
    sma200 = sma(daily["volume"], 200).iloc[-1]
    if _any_nan(sma50, sma200):
        return None, "NaN volume SMA"
    if today.volume <= sma50:
        return False, "gate2 vol<=SMA50"
    if today.volume <= sma200:
        return False, "gate8 vol<=SMA200"
    # Gate 7: weekly ATR(21) > 5% * daily close
    weekly_for_atr = weekly.iloc[:-1] if len(weekly) > 22 else weekly
    watr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(watr):
        return None, "NaN weekly ATR"
    if watr <= today.close * 0.05:
        return False, "gate7 weekly ATR<=5% close"

    return True, "all 8 daily gates pass"


def fmt_list(symbols: set[str] | list[str], limit: int = 20) -> str:
    items = sorted(symbols)
    shown = items[:limit]
    s = ", ".join(shown) if shown else "(none)"
    if len(items) > limit:
        s += f", … (+{len(items) - limit} more)"
    return s


def main() -> None:
    set_a, notes_a = load_set_a()
    set_b, notes_b = load_set_b()
    universe, notes_u = get_universe()

    set_c: set[str] = set()
    skipped = 0
    skip_reasons: dict[str, int] = {}
    for sym in universe:
        try:
            passed, reason = eval_daily_gates(sym)
        except Exception as e:  # noqa: BLE001 - validation must not crash on one symbol
            passed, reason = None, f"error: {e}"
        if passed is None:
            skipped += 1
            key = reason.split("(")[0].strip()
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
        elif passed:
            set_c.add(sym.upper())

    a_and_c = set_a & set_c
    b_and_c = set_b & set_c
    c_only = set_c - (set_a | set_b)
    ab_not_c = (set_a | set_b) - set_c
    evaluated = len(universe) - skipped

    lines: list[str] = []
    lines.append(f"# Chartink BUY rule — Day-1 validation ({DATE})\n")
    lines.append("## Methodology & limitations\n")
    lines.append(
        "This is a **daily-gates-only** validation. We do not have today's "
        "intraday tape (scanner WS issues + the rule wasn't deployed in the "
        "running OpenAlgo), so gates **3, 4, 5, 13** (5m Supertrend, 15m RSI, "
        "5m volume surge) are **not evaluated**. The 8 daily/weekly gates "
        "(1, 2, 6, 7, 8, 9, 10, 12) are evaluated against cached DuckDB bars. "
        "The production rule (`services/scan_rules/fno_intraday_buy_chartink.py`) "
        "is unchanged; gate logic is re-implemented inline here.\n"
    )
    lines.append(
        "End-of-day settled bars are used (today = iloc[-1], yesterday = "
        "iloc[-2]) — no live-forming-bar offset.\n"
    )
    lines.append("\n### Data notes\n")
    for n in notes_a + notes_b + notes_u:
        lines.append(f"- {n}")
    lines.append(f"- Universe symbols evaluated: {evaluated}; skipped (insufficient/NaN): {skipped}")
    if skip_reasons:
        for k, v in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - skip `{k}`: {v}")

    lines.append("\n## Set sizes\n")
    lines.append(f"- **Set A** (Chartink `fno-intraday-buy-20` BUY): **{len(set_a)}**")
    lines.append(f"- **Set B** (union of 7 NL scans): **{len(set_b)}**")
    lines.append(f"- **Set C** (in-house daily-gates-only): **{len(set_c)}**")

    lines.append("\n## Intersections\n")
    lines.append(f"- **A ∩ C** = {len(a_and_c)} → {fmt_list(a_and_c)}")
    lines.append(f"- **B ∩ C** = {len(b_and_c)} → {fmt_list(b_and_c)}")
    lines.append(f"- **C − (A ∪ B)** = {len(c_only)} (rule fires, no Chartink scan caught) → {fmt_list(c_only)}")
    lines.append(f"- **(A ∪ B) − C** = {len(ab_not_c)} (Chartink fires, daily gates miss) → {fmt_list(ab_not_c)}")

    lines.append("\n## Per-set symbol lists (first 20)\n")
    lines.append(f"- **Set A**: {fmt_list(set_a)}")
    lines.append(f"- **Set B**: {fmt_list(set_b)}")
    lines.append(f"- **Set C**: {fmt_list(set_c)}")

    lines.append("\n## Interpretation\n")
    b_overlap_pct = (100.0 * len(b_and_c) / len(set_b)) if set_b else 0.0
    if evaluated == 0:
        verdict = (
            "> **DATA-READINESS BLOCKER — Set C is NOT a rule signal.**\n\n"
            f"All {skipped} universe symbols were skipped: every symbol has only "
            "~121 daily bars in DuckDB, below the **200** the rule requires for "
            "the SMA(volume, 200) warm-up (gate 8). **Zero** symbols were "
            "actually evaluated, so Set C = 0 reflects missing history, not gate "
            "strictness. The production rule's own warm-up guard "
            "(`len(bars_daily) < 200`) would likewise reject all 211 symbols "
            "today — it would fire 0 (coincidentally matching Chartink's 0), but "
            "via warm-up rejection, not gate logic. **No conclusion can be drawn "
            "about whether the daily gates are loose or tight until the historify "
            "backfill extends to >=200 trading days (currently ~121, ~6 months).** "
            "The script is correct and re-runnable once that backfill lands."
        )
    elif len(set_c) == 0:
        verdict = (
            "The daily gates fire on **0** symbols today — on the strict side. "
            "With Set A also 0, the rule agrees with the operator's empty BUY "
            "list. Likely-binding exclusions are the 3% close-gap (gate 1) and "
            "the weekly-ATR>5% volatility floor (gate 7)."
        )
    elif len(set_c) <= len(set_b):
        verdict = (
            f"The daily gates fire on **{len(set_c)}** symbols — comparable to or "
            f"tighter than the {len(set_b)}-symbol NL union, suggesting the daily "
            f"gates are about right to slightly strict. B∩C overlap is "
            f"{b_overlap_pct:.0f}% of Set B."
        )
    else:
        verdict = (
            f"The daily gates fire on **{len(set_c)}** symbols — broader than the "
            f"NL union ({len(set_b)}). On daily evidence alone the gates look "
            f"loose; the missing intraday gates (3/4/5/13) are what would prune "
            f"this down to a tradeable shortlist."
        )
    lines.append(verdict + "\n")
    lines.append(
        "**Note on the 4 skipped gates.** Gates 3 & 4 (5m Supertrend vs daily "
        "close) demand price be riding above a freshly-flipped 5-min trend line; "
        "gate 5 (15m RSI>50) demands intraday momentum; gate 13 (5m vol > 2× "
        "SMA10) demands a live volume burst on the firing bar. Together they turn "
        "a daily 'eligible' list into a moment-of-entry trigger. Set C is "
        "therefore an **upper bound** on what the full rule would fire — every "
        "intraday gate can only shrink it. Validate these once we have an "
        "intraday tape (5m + 15m bars) for a session.\n"
    )

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- stdout summary ----
    print(f"Set A (Chartink BUY): {len(set_a)}  -> {fmt_list(set_a, 10)}")
    print(f"Set B (NL union):     {len(set_b)}  -> {fmt_list(set_b, 10)}")
    print(f"Set C (daily gates):  {len(set_c)}  -> {fmt_list(set_c, 10)}")
    print(
        f"A&C={len(a_and_c)}  B&C={len(b_and_c)}  "
        f"C-(A|B)={len(c_only)}  (A|B)-C={len(ab_not_c)}"
    )
    print(f"Universe evaluated={evaluated} skipped={skipped}")
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
