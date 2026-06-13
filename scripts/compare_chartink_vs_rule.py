"""Side-by-side comparison: today's Chartink scans vs our in-house rule.

Builds a per-symbol, per-gate breakdown so the operator can see *why* the
in-house ``fno_intraday_buy_chartink`` rule fired (Set C) and *why* it did not
fire on the Chartink NL-scan stocks (Set B). For every evaluable symbol we run
the 8 daily/weekly gates, record each gate's numerical value vs its threshold,
and name the single decisive "killer" gate (first failure in short-circuit
order). Gates 3/4/5/13 (intraday Supertrend/RSI/5m-volume) are not evaluated
here — we have no intraday tape for today.

Gate logic is duplicated from ``scripts/validate_chartink_rule.py`` verbatim;
the production rule is NOT modified.

Sets:
  A — Chartink ``fno-intraday-buy-20`` BUY hits (0 today; baseline absent).
  B — union across the 7 NL scans (43 stocks, mostly cash-segment).
  C — in-house daily-gates matches (CGPOWER, SAMMAANCAP).
  D — (B ∪ C) ∩ our F&O DuckDB universe — the only symbols we can evaluate.

Run: ``uv run python scripts/compare_chartink_vs_rule.py``
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
REPORT = OUTPUTS / f"chartink_vs_rule_comparison_{DATE}.md"
DUCKDB_PATH = ROOT / "db" / "historify.duckdb"
EXCHANGE = "NSE"

# Short-circuit order of the 8 daily/weekly gates (first failure = killer).
GATE_ORDER = ["gate6", "gate12", "gate1", "gate9", "gate10", "gate2", "gate8", "gate7"]
GATE_DESC = {
    "gate6": "close > 100",
    "gate12": "close < 5000",
    "gate1": "close > prevClose × 1.03 (3% gap)",
    "gate9": "open > prevClose",
    "gate10": "open > pivot",
    "gate2": "vol > SMA(vol,50)",
    "gate8": "vol > SMA(vol,200)",
    "gate7": "weekly ATR(21) > 5% × close",
}


def _any_nan(*values: float) -> bool:
    return any(pd.isna(v) for v in values)


def load_set_a() -> set[str]:
    if not BASELINE_JSON.exists():
        return set()
    data = json.loads(BASELINE_JSON.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for scan in data.get("scans", []):
        name = (scan.get("scan_name") or scan.get("name") or "").lower()
        if "fno-intraday-buy" in name or "fno_intraday_buy" in name:
            for s in scan.get("top_matching_stocks", scan.get("stocks", [])):
                sym = s.get("symbol") if isinstance(s, dict) else s
                if sym:
                    symbols.add(sym.upper())
    return symbols


def load_set_b() -> set[str]:
    data = json.loads(NL_JSON.read_text(encoding="utf-8"))
    symbols: set[str] = set()
    for scan in data.get("scans", []):
        for s in scan.get("top_matching_stocks", []):
            sym = s.get("symbol")
            if sym:
                symbols.add(sym.upper())
    return symbols


def get_universe() -> list[str]:
    con = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT symbol FROM market_data "
            "WHERE interval = 'D' AND exchange = ? ORDER BY symbol",
            [EXCHANGE],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def eval_gates(symbol: str) -> dict:
    """Compute all 8 gates (no short-circuit) with values, pass-flags, killer.

    Returns dict with keys: status ('ok'|'skipped'), reason, gates (list of
    dicts with name/desc/value/threshold/passed), killer, all_pass.
    """
    daily = get_ohlcv(symbol, EXCHANGE, "D")
    if daily is None or len(daily) < 200:
        n = 0 if daily is None else len(daily)
        return {"status": "skipped", "reason": f"insufficient daily bars ({n} < 200)"}
    weekly = get_ohlcv(symbol, EXCHANGE, "W")
    if weekly is None or len(weekly) < 22:
        n = 0 if weekly is None else len(weekly)
        return {"status": "skipped", "reason": f"insufficient weekly bars ({n} < 22)"}

    today = daily.iloc[-1]
    yest = daily.iloc[-2]
    if _any_nan(today.close, today.open, today.volume, yest.close, yest.high, yest.low):
        return {"status": "skipped", "reason": "NaN in required daily fields"}

    sma50 = sma(daily["volume"], 50).iloc[-1]
    sma200 = sma(daily["volume"], 200).iloc[-1]
    weekly_for_atr = weekly.iloc[:-1] if len(weekly) > 22 else weekly
    watr = atr(weekly_for_atr, period=21).iloc[-1]
    if _any_nan(sma50, sma200, watr):
        return {"status": "skipped", "reason": "NaN in volume SMA / weekly ATR"}

    pivot = (yest.high + yest.low + yest.close) / 3.0
    gap_thr = yest.close * 1.03
    atr_floor = today.close * 0.05

    gates = [
        ("gate6", round(today.close, 2), 100.0, today.close > 100),
        ("gate12", round(today.close, 2), 5000.0, today.close < 5000),
        ("gate1", round(today.close, 2), round(gap_thr, 2), today.close > gap_thr),
        ("gate9", round(today.open, 2), round(yest.close, 2), today.open > yest.close),
        ("gate10", round(today.open, 2), round(pivot, 2), today.open > pivot),
        ("gate2", int(today.volume), int(sma50), today.volume > sma50),
        ("gate8", int(today.volume), int(sma200), today.volume > sma200),
        ("gate7", round(watr, 2), round(atr_floor, 2), watr > atr_floor),
    ]
    glist = [
        {"name": n, "desc": GATE_DESC[n], "value": v, "threshold": t, "passed": bool(p)}
        for (n, v, t, p) in gates
    ]
    by_name = {g["name"]: g for g in glist}
    killer = next((n for n in GATE_ORDER if not by_name[n]["passed"]), None)
    return {
        "status": "ok",
        "gates": glist,
        "killer": killer,
        "all_pass": killer is None,
    }


def gate_table(res: dict) -> list[str]:
    """Render a per-gate markdown table for one evaluated symbol."""
    rows = ["| Gate | Check | Value | Threshold | Result |", "|---|---|---|---|---|"]
    for g in res["gates"]:
        mark = "✅" if g["passed"] else "❌"
        rows.append(f"| {g['name']} | {g['desc']} | {g['value']} | {g['threshold']} | {mark} |")
    return rows


def main() -> None:
    set_a = load_set_a()
    set_b = load_set_b()
    universe = set(get_universe())

    # Set C: full pass over universe (re-derive, don't trust cached file).
    set_c: set[str] = set()
    for sym in universe:
        try:
            res = eval_gates(sym)
        except Exception:  # noqa: BLE001
            continue
        if res.get("status") == "ok" and res.get("all_pass"):
            set_c.add(sym.upper())

    set_d = (set_b | set_c) & universe

    # Evaluate every Set D symbol once and cache results.
    results: dict[str, dict] = {}
    for sym in sorted(set_d):
        try:
            results[sym] = eval_gates(sym)
        except Exception as e:  # noqa: BLE001
            results[sym] = {"status": "skipped", "reason": f"error: {e}"}

    b_in_universe = sorted(set_b & universe)
    b_not_in_universe = sorted(set_b - universe)

    L: list[str] = []
    L.append(f"# Chartink scans vs in-house rule — side-by-side ({DATE})\n")
    L.append(
        "Per-symbol, per-gate breakdown of why our `fno_intraday_buy_chartink` "
        "rule fired (Set C) and why it skipped the Chartink NL-scan stocks "
        "(Set B). Only the **8 daily/weekly gates** are evaluable today — "
        "intraday gates 3/4/5/13 (5m Supertrend, 15m RSI, 5m vol surge) need a "
        "live tape we don't have. Production rule unchanged; gate logic "
        "duplicated from `scripts/validate_chartink_rule.py`.\n"
    )
    L.append("Short-circuit kill order: " + " → ".join(GATE_ORDER) + "\n")

    # 1. Summary table
    L.append("## 1. Summary\n")
    L.append("| Set | Definition | Count |")
    L.append("|---|---|---|")
    L.append(f"| A | Chartink `fno-intraday-buy-20` BUY | {len(set_a)} |")
    L.append(f"| B | Union of 7 NL scans | {len(set_b)} |")
    L.append(f"| C | In-house daily-gates matches | {len(set_c)} |")
    L.append(f"| D | (B ∪ C) ∩ F&O universe (evaluable) | {len(set_d)} |")
    L.append(f"| — | Our F&O DuckDB universe | {len(universe)} |")
    L.append("")
    L.append("**Intersections**")
    L.append(f"- A ∩ C = {len(set_a & set_c)}")
    L.append(f"- B ∩ C = {len(set_b & set_c)}")
    L.append(
        f"- B ∩ universe = {len(set_b & universe)} (rest are cash-segment, not in our F&O bars)"
    )
    L.append(f"- C: {', '.join(sorted(set_c)) or '(none)'}")
    L.append("")

    # 2. Set C — full breakdown
    L.append("## 2. Our rule's hits (Set C) — full gate breakdown\n")
    if not set_c:
        L.append("_No symbols passed all 8 daily gates._\n")
    for sym in sorted(set_c):
        res = results.get(sym) or eval_gates(sym)
        L.append(f"### {sym}\n")
        if res.get("status") != "ok":
            L.append(f"_skipped: {res.get('reason')}_\n")
            continue
        L.extend(gate_table(res))
        L.append("")
        L.append("**Verdict:** all 8 daily gates ✅ → armed for intraday triggers.\n")

    # 3. Set B in universe — kill reasons
    L.append("## 3. NL scan stocks (Set B) in our F&O universe\n")
    if not b_in_universe:
        L.append(
            "_None of the 43 NL stocks exist in our F&O DuckDB universe — they "
            "are cash-segment names (NL builder ignored the 'FnO' qualifier). "
            "Nothing to evaluate here; this is the expected result._\n"
        )
    else:
        kill_groups: dict[str, list[str]] = {}
        for sym in b_in_universe:
            res = results.get(sym, {})
            if res.get("status") != "ok":
                kill_groups.setdefault("skipped/data", []).append(sym)
                continue
            k = res.get("killer") or "ALL PASS"
            kill_groups.setdefault(k, []).append(sym)
        L.append("**Primary kill reason grouping**\n")
        L.append("| Killer gate | Check | # stocks | Symbols |")
        L.append("|---|---|---|---|")
        for k in GATE_ORDER + ["ALL PASS", "skipped/data"]:
            if k not in kill_groups:
                continue
            desc = GATE_DESC.get(k, k)
            syms = ", ".join(kill_groups[k])
            L.append(f"| {k} | {desc} | {len(kill_groups[k])} | {syms} |")
        L.append("")
        L.append("**Per-symbol detail (how far off the killer threshold)**\n")
        for sym in b_in_universe:
            res = results.get(sym, {})
            if res.get("status") != "ok":
                L.append(f"- **{sym}** — skipped: {res.get('reason')}")
                continue
            k = res.get("killer")
            if k is None:
                L.append(f"- **{sym}** — all 8 daily gates ✅")
                continue
            g = next(g for g in res["gates"] if g["name"] == k)
            L.append(
                f"- **{sym}** — killed by `{k}` ({g['desc']}): "
                f"value {g['value']} vs threshold {g['threshold']} ❌"
            )
        L.append("")

    # 4. Set A
    L.append("## 4. Chartink BUY (Set A)\n")
    if not set_a:
        L.append(
            f"Baseline `{BASELINE_JSON.name}` absent; operator-documented "
            "`fno-intraday-buy-20` BUY count today is **0**. Nothing to compare. "
            "Our rule also fired 0 on the BUY-scan intersection — consistent.\n"
        )
    else:
        L.append(f"Set A = {len(set_a)}: {', '.join(sorted(set_a))}\n")

    # 5. Key takeaways
    L.append("## 5. Key takeaways\n")
    # Tally killers across all evaluated Set D symbols (excludes Set C all-pass).
    tally: dict[str, int] = {}
    for sym, res in results.items():
        if res.get("status") != "ok":
            continue
        k = res.get("killer")
        if k:
            tally[k] = tally.get(k, 0) + 1
    if tally:
        L.append("Killer-gate frequency across all evaluated Set D symbols:\n")
        L.append("| Gate | Check | # killed |")
        L.append("|---|---|---|")
        for k in GATE_ORDER:
            if k in tally:
                L.append(f"| {k} | {GATE_DESC[k]} | {tally[k]} |")
        L.append("")
        top = sorted(tally.items(), key=lambda kv: -kv[1])
        heavy = ", ".join(f"`{k}` ({n})" for k, n in top[:3])
        L.append(f"**Heaviest filtering:** {heavy}.")
    else:
        L.append(
            "Every evaluable Set D symbol is a Set C pass — the NL stocks that "
            "made it into our F&O universe all cleared the daily gates. The real "
            f"filtering happens earlier: {len(set_b - universe)} of {len(set_b)} "
            "NL names never reach evaluation because they are cash-segment and "
            "absent from our F&O bars."
        )
    L.append("")
    L.append(
        "**One-liner:** the daily gates are tight — only "
        f"{len(set_c)}/{len(universe)} F&O names pass all 8 — and Set B barely "
        "overlaps our universe, so segment mismatch (cash vs F&O) is the biggest "
        "single reason our rule and Chartink's NL scans disagree.\n"
    )

    REPORT.write_text("\n".join(L) + "\n", encoding="utf-8")

    # ---- stdout summary ----
    print(f"Set A={len(set_a)}  Set B={len(set_b)}  Set C={len(set_c)}  Set D={len(set_d)}")
    print(f"Universe={len(universe)}  B&universe={len(set_b & universe)}")
    print(f"Set C: {', '.join(sorted(set_c)) or '(none)'}")
    print(f"B in universe: {b_in_universe or '(none)'}")
    print(f"B NOT in universe: {len(b_not_in_universe)} cash-segment names")
    if tally:
        print("Killer tally:", {k: tally[k] for k in GATE_ORDER if k in tally})
    print(f"Report: {REPORT}")


if __name__ == "__main__":
    main()
