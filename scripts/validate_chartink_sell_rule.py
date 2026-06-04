"""Day-1 validation of the new ``fno_intraday_sell_chartink`` rule.

Companion to ``validate_chartink_rule.py`` (the BUY validator). Validates the
SELL rule's *daily-only* gates against today's (2026-06-04) cached data:

  Set A_sell — operator's Chartink SELL hits, from ``scan_cycle.screener_sell``
               (ground truth — what Chartink actually fired at us today).
  Set C_sell — in-house daily-gates-only SELL matches over the F&O daily universe.

Only the daily/weekly-evaluable gates are checked here: 1, 6, 7, 9, 10, 12 and
the simple volume gate (``daily volume > 1d-ago volume``). The intraday gates
3, 4, 5 (5m Supertrend, 15m RSI) need a live tape we don't have for today, so
they are intentionally skipped. The production rule
(``services/scan_rules/fno_intraday_sell_chartink.py``) is NOT modified — gate
logic is re-implemented inline against daily/weekly frames.

Unlike the BUY validator, the SELL daily gates need NO 200-day SMA warm-up
(the SELL leg dropped BUY's SMA(50)/SMA(200) volume gates), so the universe's
~121-day history is sufficient and Set C_sell is a real signal, not a
data-readiness artifact.

Run: ``uv run python scripts/validate_chartink_sell_rule.py``
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
from services.indicators import atr  # noqa: E402

OUTPUTS = ROOT / "outputs"
DATE = "2026-06-04"
REPORT = OUTPUTS / f"chartink_sell_rule_validation_{DATE}.md"
DUCKDB_PATH = ROOT / "db" / "historify.duckdb"
OPENALGO_DB = ROOT / "db" / "openalgo.db"
EXCHANGE = "NSE"


def get_chartink_sell_set_for_date(date_str: str) -> tuple[set[str], dict]:
    """Return (unique_symbols_set, meta_dict) for the chartink SELL scan on date_str.

    Ground truth = ``scan_cycle`` rows the chartink scheduled task writes on
    every POST. ``screener_sell`` JSON unioned across all rows, deduplicated.
    Filters to ``cycle_kind = 'chartink'`` (excludes pytest test rows).
    """
    con = sqlite3.connect(str(OPENALGO_DB))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT started_at, screener_sell FROM scan_cycle "
            "WHERE cycle_kind = 'chartink' "
            "AND substr(started_at, 1, 10) = ? "
            "ORDER BY started_at ASC",
            [date_str],
        ).fetchall()
    finally:
        con.close()

    symbols: set[str] = set()
    per_symbol_fire_count: dict[str, int] = {}
    firing_rows = 0
    first_fire_ts: str | None = None
    last_fire_ts: str | None = None

    for r in rows:
        raw = r["screener_sell"]
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

    meta = {
        "total_rows": len(rows),
        "firing_rows": firing_rows,
        "unique_symbols": len(symbols),
        "per_symbol_fire_count": per_symbol_fire_count,
        "first_fire_ts": first_fire_ts,
        "last_fire_ts": last_fire_ts,
    }
    return symbols, meta


def _any_nan(*values: float) -> bool:
    return any(pd.isna(v) for v in values)


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
    """Evaluate the 7 daily-only SELL gates. Returns (passed, reason).

    passed is None when warm-up/data is insufficient (excluded from Set C_sell).
    """
    daily = get_ohlcv(symbol, EXCHANGE, "D")
    if daily is None or len(daily) < 3:
        return None, f"insufficient daily bars ({0 if daily is None else len(daily)} < 3)"
    weekly = get_ohlcv(symbol, EXCHANGE, "W")
    if weekly is None or len(weekly) < 22:
        return None, f"insufficient weekly bars ({0 if weekly is None else len(weekly)} < 22)"

    # End-of-day validation on settled bars: today = -1, yesterday = -2.
    today = daily.iloc[-1]
    yest = daily.iloc[-2]
    if _any_nan(today.close, today.open, today.volume, yest.close, yest.high, yest.low, yest.volume):
        return None, "NaN in required daily fields"

    # Gate 6: close > 100
    if today.close <= 100:
        return False, "gate6 close<=100"
    # Gate 12: close < 5000
    if today.close >= 5000:
        return False, "gate12 close>=5000"
    # Gate 1: close < 1d-ago close * 0.97 (3% gap DOWN)
    if today.close >= yest.close * 0.97:
        return False, "gate1 no 3% gap-down on close"
    # Gate 9: open < 1d-ago close
    if today.open >= yest.close:
        return False, "gate9 open>=prev close"
    # Gate 10: open < pivot (H+L+C of prev)/3
    pivot = (yest.high + yest.low + yest.close) / 3.0
    if today.open >= pivot:
        return False, "gate10 open>=pivot"
    # Volume gate: today volume > prev volume
    if today.volume <= yest.volume:
        return False, "gateV vol<=prev vol"
    # Gate 7: weekly ATR(21) > 5% * daily close
    weekly_for_atr = weekly.iloc[:-1] if len(weekly) > 22 else weekly
    watr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(watr):
        return None, "NaN weekly ATR"
    if watr <= today.close * 0.05:
        return False, "gate7 weekly ATR<=5% close"

    return True, "all 7 daily SELL gates pass"


def fmt_list(symbols: set[str] | list[str], limit: int = 20) -> str:
    items = sorted(symbols)
    shown = items[:limit]
    s = ", ".join(shown) if shown else "(none)"
    if len(items) > limit:
        s += f", … (+{len(items) - limit} more)"
    return s


def main() -> None:
    set_a, meta_a = get_chartink_sell_set_for_date(DATE)
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
    a_not_c = set_a - set_c
    c_not_a = set_c - set_a
    evaluated = len(universe) - skipped

    lines: list[str] = []
    lines.append(f"# Chartink SELL rule — Day-1 validation ({DATE})\n")
    lines.append("## Methodology & limitations\n")
    lines.append(
        "Daily-gates-only validation of `fno_intraday_sell_chartink`. The "
        "intraday gates **3, 4, 5** (5m Supertrend, 15m RSI) are not evaluated "
        "(no live tape for today). The 7 daily/weekly gates (1, 6, 7, 9, 10, 12 "
        "and the simple volume gate) are evaluated against cached DuckDB bars. "
        "The production rule is unchanged; gate logic is re-implemented inline.\n"
    )
    lines.append(
        "Unlike the BUY validator, the SELL daily gates need **no 200-day SMA "
        "warm-up**, so the universe's ~121-day history is sufficient — Set "
        "C_sell is a real gate signal, not a data-readiness artifact.\n"
    )
    for n in notes_u:
        lines.append(f"- {n}")
    lines.append(f"- Universe evaluated: {evaluated}; skipped (insufficient/NaN): {skipped}")
    if skip_reasons:
        for k, v in sorted(skip_reasons.items(), key=lambda kv: -kv[1]):
            lines.append(f"  - skip `{k}`: {v}")

    lines.append("\n## Chartink SELL firing detail (scan_cycle ground truth)\n")
    lines.append(
        f"- `scan_cycle`: {meta_a['total_rows']} chartink rows today, "
        f"{meta_a['firing_rows']} with non-empty SELL payloads, "
        f"{meta_a['unique_symbols']} unique SELL symbols."
    )
    if meta_a["firing_rows"]:
        lines.append(
            f"- Firing window (IST): **{meta_a['first_fire_ts']} → {meta_a['last_fire_ts']}**."
        )
        pscf = meta_a["per_symbol_fire_count"]
        for sym in sorted(pscf, key=lambda s: (-pscf[s], s)):
            lines.append(f"  - **{sym}**: fired **{pscf[sym]}×**")
    else:
        lines.append(
            "- **No SELL payloads captured today** — every `screener_sell` row "
            "is NULL/empty. The operator's SELL screener was not POSTing SELL "
            "payloads into `scan_cycle`, so Set A_sell is empty and the live "
            "A∩C comparison cannot be made for the SELL leg today."
        )

    lines.append("\n## Set sizes\n")
    lines.append(f"- **Set A_sell** (Chartink SELL ground truth): **{len(set_a)}**")
    lines.append(f"- **Set C_sell** (in-house daily-gates-only): **{len(set_c)}**")

    lines.append("\n## Intersections\n")
    lines.append(f"- **A ∩ C** = {len(a_and_c)} → {fmt_list(a_and_c)}")
    lines.append(f"- **A − C** (Chartink fired, daily gates miss) = {len(a_not_c)} → {fmt_list(a_not_c)}")
    lines.append(f"- **C − A** (rule fires, no Chartink SELL) = {len(c_not_a)} → {fmt_list(c_not_a)}")

    lines.append("\n## Per-set symbol lists (first 20)\n")
    lines.append(f"- **Set A_sell**: {fmt_list(set_a)}")
    lines.append(f"- **Set C_sell**: {fmt_list(set_c)}")

    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- stdout summary ----
    print(f"Set A_sell (Chartink SELL): {len(set_a)}  -> {fmt_list(set_a, 10)}")
    print(f"Set C_sell (daily gates):   {len(set_c)}  -> {fmt_list(set_c, 10)}")
    print(f"A&C={len(a_and_c)}  A-C={len(a_not_c)}  C-A={len(c_not_a)}")
    print(f"Universe evaluated={evaluated} skipped={skipped}")
    if not meta_a["firing_rows"]:
        print("NOTE: scan_cycle has ZERO SELL payloads today (screener_sell all NULL).")
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
