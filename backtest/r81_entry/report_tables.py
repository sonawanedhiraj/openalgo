"""Generate the R81 report tables from data/result_E*.json (no hand transcription). -> data/report_tables.md"""
import json
from pathlib import Path
D = Path(__file__).resolve().parent / "data"
NAMES = {"E2": "E2 sector-relative strength 09:45", "E5": "E5 opening-drive efficiency (1/day)", "E1": "E1 VWAP trend-day pullback",
         "E4": "E4 first-hour compression → expansion", "E3": "E3 large-gap fade"}
L = []
summ = ["| Candidate | Train net ₹ (t) | Validate net ₹ (t) | **Holdout** net ₹ (t, 95% CI) | Holdout gross bps vs 14 | Half-years + (of 10) | Plateau | Placebo timing / selection pctile (val; hold) | Verdict |",
        "|---|---|---|---|---|---|---|---|---|"]
for c in ("E2", "E5", "E1", "E4", "E3"):
    r = json.load(open(D / f"result_{c}.json")); P = r["periods"]; g = r["gates"]
    a = lambda per: P[per][c]["all"]
    pv, ph = P["validate"]["placebo"]["all"], P["holdout"]["placebo"]["all"]
    summ.append(f"| {NAMES[c]} | {a('train')['net_rs']} ({a('train')['t']}) | {a('validate')['net_rs']} ({a('validate')['t']}) | "
                f"**{a('holdout')['net_rs']}** ({a('holdout')['t']}, {a('holdout')['ci']}) | {a('holdout')['gross_bps']} → {a('holdout')['bar_14bps']} | "
                f"{g['half_years_positive']} | {'pass' if g['plateau_ok'] else 'fail'} | {pv['timing_pctile']}/{pv['selection_pctile']}; {ph['timing_pctile']}/{ph['selection_pctile']} | {g['verdict']} |")
    L += [f"### {NAMES[c]}", "",
          "| Period | Side | Trades | /day | Gross ₹ / bps | Net ₹ / bps | Net 0-slip ₹ | Win % | Payoff | t | 95% CI ₹ | Max DD ₹ | Loss > ₹500 % |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for per in ("train", "validate", "holdout"):
        for side in ("all", "long", "short"):
            s = P[per][c][side]
            L.append(f"| {per} | {side} | {s['n']} | {s['trades_per_day']} | {s['gross_rs']} / {s['gross_bps']} | {s['net_rs']} / {s['net_bps']} | "
                     f"{s['net0_rs']} | {s['win_pct']} | {s['payoff']} | {s['t']} | {s['ci']} | {s['maxdd']:.0f} | {s['breach500']} |")
    hy = {**P["train"][c]["half_years"], **P["validate"][c]["half_years"], **P["holdout"][c]["half_years"]}
    L += ["", "Half-years (trades : net ₹/trade): " + ", ".join(f"{k} {v}" for k, v in hy.items()), "",
          f"Neighbours, validate net ₹/trade: {g['plateau']}. Holdout neighbours: " +
          str({v: b['all']['net_rs'] for v, b in P['holdout'].items() if v not in (c, 'placebo')}), "",
          "Placebo percentiles (timing / selection), all · long · short: validate " +
          " · ".join(f"{P['validate']['placebo'][s].get('timing_pctile')}/{P['validate']['placebo'][s].get('selection_pctile')}" for s in ("all", "long", "short")) +
          "; holdout " + " · ".join(f"{P['holdout']['placebo'][s].get('timing_pctile')}/{P['holdout']['placebo'][s].get('selection_pctile')}" for s in ("all", "long", "short")),
          "", f"**Verdict:** {g['verdict']}", ""]
(D / "report_tables.md").write_text("\n".join(summ) + "\n\n" + "\n".join(L), encoding="utf-8")
print("\n".join(summ))
