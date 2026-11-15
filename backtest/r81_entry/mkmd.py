"""Render data/result_<cand>.json into a compact markdown checkpoint data/result_<cand>.md."""
import json
import sys
from pathlib import Path

D = Path(__file__).resolve().parent / "data"


def table(res, cand):
    L = ["| Period | Side | Trades | /day | Gross ₹ | Gross bps | Net ₹ | Net bps | Net (0-slip) ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | >₹500 loss % | vs 14 bps |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for per, blk in res["periods"].items():
        for side in ("all", "long", "short"):
            s = blk.get(cand, {}).get(side, {})
            if not s or not s.get("n"):
                continue
            L.append(f"| {per} | {side} | {s['n']} | {s['trades_per_day']} | {s['gross_rs']} | {s['gross_bps']} | {s['net_rs']} | "
                     f"{s['net_bps']} | {s['net0_rs']} | {s['win_pct']} | {s['payoff']} | {s['t']} | {s['ci']} | {s['maxdd']} | "
                     f"{s['breach500']} | {s['bar_14bps']} |")
    return "\n".join(L)


def main(cand):
    res = json.load(open(D / f"result_{cand}.json"))
    out = [f"# R81 {cand} — result (code {res['code_hash']})", "", table(res, cand), ""]
    for per, blk in res["periods"].items():
        hy = blk.get(cand, {}).get("half_years")
        if hy:
            out.append(f"- **Half-years ({per})**, trades:net ₹/trade: " + ", ".join(f"{k} {v}" for k, v in hy.items()))
        pl = blk.get("placebo")
        if pl:
            out.append(f"- **Placebo ({per})**, stop-or-15:20 lookup: " + "; ".join(
                f"{side}: n={p.get('n')}, actual ₹{p.get('actual_stop1520')}, timing p={p.get('timing_p')} "
                f"(pctile {p.get('timing_pctile')}), selection p={p.get('selection_p')} (pctile {p.get('selection_pctile')})"
                for side, p in pl.items()))
        others = {v: b["all"].get("net_rs") for v, b in blk.items() if v not in (cand, "placebo") and "all" in b}
        if others:
            out.append(f"- **Neighbours ({per})**, net ₹/trade: {others}")
    out.append(f"- **Gates:** {json.dumps(res['gates'], default=str)}")
    (D / f"result_{cand}.md").write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


if __name__ == "__main__":
    main(sys.argv[1])
