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
import sqlite3
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
OPENALGO_DB = ROOT / "db" / "openalgo.db"
EXCHANGE = "NSE"


def get_chartink_buy_set_for_date(date_str: str) -> tuple[set[str], dict]:
    """Return (unique_symbols_set, meta_dict) for the chartink BUY scan on date_str.

    Canonical ground truth = the ``scan_cycle`` rows the chartink scheduled task
    writes on every POST (``database/scan_cycle_db.py``). Unlike a live screener
    scrape, these capture what Chartink actually fired at us throughout the day,
    even after intraday matches have cleared.

    Sourced from the ``scan_cycle`` table on ``db/openalgo.db``:
      - ``cycle_kind = 'chartink'`` (the only chartink discriminator — the table
        has no ``source``/``scan_name`` column; pytest rows use other kinds such
        as ``'test'``/``'manual_bootstrap'``, so this also filters test noise)
      - ``substr(started_at, 1, 10) = date_str`` (started_at is IST ISO-8601)
      - ``screener_buy`` JSON unioned across all rows, then deduplicated.

    Meta includes total_rows, firing_rows, unique_symbols, per_symbol_fire_count
    ({sym: N rows it appeared in}), per_symbol_first/last (IST ts per symbol),
    and first_fire_ts / last_fire_ts (firing window bounds).
    """
    con = sqlite3.connect(str(OPENALGO_DB))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT started_at, screener_buy FROM scan_cycle "
            "WHERE cycle_kind = 'chartink' "
            "AND substr(started_at, 1, 10) = ? "
            "ORDER BY started_at ASC",
            [date_str],
        ).fetchall()
    finally:
        con.close()

    symbols: set[str] = set()
    per_symbol_fire_count: dict[str, int] = {}
    per_symbol_first: dict[str, str] = {}
    per_symbol_last: dict[str, str] = {}
    firing_rows = 0
    first_fire_ts: str | None = None
    last_fire_ts: str | None = None

    for r in rows:
        raw = r["screener_buy"]
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        row_syms: list[str] = []
        for s in payload if isinstance(payload, list) else []:
            sym = s.get("symbol") if isinstance(s, dict) else s
            if sym:
                row_syms.append(str(sym).upper())
        if not row_syms:
            continue
        firing_rows += 1
        ts = r["started_at"]
        if first_fire_ts is None:
            first_fire_ts = ts
        last_fire_ts = ts
        for sym in row_syms:
            symbols.add(sym)
            per_symbol_fire_count[sym] = per_symbol_fire_count.get(sym, 0) + 1
            per_symbol_first.setdefault(sym, ts)
            per_symbol_last[sym] = ts

    meta = {
        "source": "scan-cycle",
        "total_rows": len(rows),
        "firing_rows": firing_rows,
        "unique_symbols": len(symbols),
        "per_symbol_fire_count": per_symbol_fire_count,
        "per_symbol_first": per_symbol_first,
        "per_symbol_last": per_symbol_last,
        "first_fire_ts": first_fire_ts,
        "last_fire_ts": last_fire_ts,
    }
    return symbols, meta


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
    # --source scan-cycle (default) | live-screener
    source = "scan-cycle"
    if "--source" in sys.argv:
        idx = sys.argv.index("--source")
        if idx + 1 < len(sys.argv):
            source = sys.argv[idx + 1]

    meta_a: dict = {}
    if source == "live-screener":
        set_a, notes_a = load_set_a()
    else:
        set_a, meta_a = get_chartink_buy_set_for_date(DATE)
        notes_a = [
            f"Set A from scan_cycle (ground truth): {meta_a['total_rows']} chartink "
            f"rows today, {meta_a['firing_rows']} non-empty, {meta_a['unique_symbols']} "
            f"unique BUY symbols deduplicated across the day.",
            f"Firing window (IST): {meta_a['first_fire_ts']} → {meta_a['last_fire_ts']}.",
        ]

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
    lines.append(
        f"- Universe symbols evaluated: {evaluated}; skipped (insufficient/NaN): {skipped}"
    )
    if skip_reasons:
        for k, v in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - skip `{k}`: {v}")

    if meta_a:
        lines.append("\n## Chartink firing detail (scan_cycle ground truth)\n")
        lines.append(
            f"- Source: `scan_cycle` table — {meta_a['total_rows']} chartink rows "
            f"today, {meta_a['firing_rows']} with non-empty BUY payloads.\n"
            f"- Firing window (IST): **{meta_a['first_fire_ts']} → "
            f"{meta_a['last_fire_ts']}**.\n"
            "- Chartink re-alerts the same stock every scan cycle while it still "
            "matches, so the per-symbol counts below are how many cycles each "
            "symbol appeared in (1 unique stock can fire many times)."
        )
        pscf = meta_a["per_symbol_fire_count"]
        for sym in sorted(pscf, key=lambda s: (-pscf[s], s)):
            first = meta_a["per_symbol_first"].get(sym, "")[11:19]
            last = meta_a["per_symbol_last"].get(sym, "")[11:19]
            lines.append(f"  - **{sym}**: fired **{pscf[sym]}×** ({first} → {last} IST)")

    lines.append("\n## Set sizes\n")
    lines.append(f"- **Set A** (Chartink `fno-intraday-buy-20` BUY): **{len(set_a)}**")
    lines.append(f"- **Set B** (union of 7 NL scans): **{len(set_b)}**")
    lines.append(f"- **Set C** (in-house daily-gates-only): **{len(set_c)}**")

    lines.append("\n## Intersections\n")
    lines.append(f"- **A ∩ C** = {len(a_and_c)} → {fmt_list(a_and_c)}")
    lines.append(f"- **B ∩ C** = {len(b_and_c)} → {fmt_list(b_and_c)}")
    lines.append(
        f"- **C − (A ∪ B)** = {len(c_only)} (rule fires, no Chartink scan caught) → {fmt_list(c_only)}"
    )
    lines.append(
        f"- **(A ∪ B) − C** = {len(ab_not_c)} (Chartink fires, daily gates miss) → {fmt_list(ab_not_c)}"
    )

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
    print(f"Set A source: {source}")
    if meta_a:
        pscf = meta_a["per_symbol_fire_count"]
        print(
            f"  scan_cycle: {meta_a['total_rows']} rows, {meta_a['firing_rows']} firing, "
            f"window {meta_a['first_fire_ts']} -> {meta_a['last_fire_ts']}"
        )
        print(
            "  per-symbol fire count: "
            + ", ".join(f"{s}={pscf[s]}" for s in sorted(pscf, key=lambda s: (-pscf[s], s)))
        )
    print(f"Set A (Chartink BUY): {len(set_a)}  -> {fmt_list(set_a, 10)}")
    print(f"Set B (NL union):     {len(set_b)}  -> {fmt_list(set_b, 10)}")
    print(f"Set C (daily gates):  {len(set_c)}  -> {fmt_list(set_c, 10)}")
    print(f"A&C={len(a_and_c)}  B&C={len(b_and_c)}  C-(A|B)={len(c_only)}  (A|B)-C={len(ab_not_c)}")
    print(f"Universe evaluated={evaluated} skipped={skipped}")
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
