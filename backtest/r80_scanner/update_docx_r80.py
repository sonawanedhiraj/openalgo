"""Append section 18 (R80) to Dheeraj's 2026-11-15 handover. Sections 1-17 untouched; tables copy the doc's
look (TableGrid, navy 17365D header, F2F6FA zebra, Aptos). Usage: update_docx_r80.py SRC OUT"""
import json
import os
import sys

import docx
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

SRC, OUT = sys.argv[1], sys.argv[2]
D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
S = json.load(open(f"{D}/score.json"))
d = docx.Document(SRC)
TOTAL_W = 10512


def para(text, style="Normal", bold_prefix=None):
    p = d.add_paragraph(style=style)
    if bold_prefix:
        p.add_run(bold_prefix).bold = True
    p.add_run(text)
    return p


def shade(cell, fill):
    shd = OxmlElement("w:shd")
    shd.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shd)


def table(rows, widths, size=8):
    t = d.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = d.styles["Table Grid"]
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    scale = TOTAL_W / sum(widths)
    for i, row in enumerate(rows):
        for j, val in enumerate(row):
            cell = t.cell(i, j)
            cell.width = int(widths[j] * scale) * 635
            va = OxmlElement("w:vAlign")
            va.set(qn("w:val"), "center")
            cell._tc.get_or_add_tcPr().append(va)
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            run = p.add_run(str(val))
            run.font.name = "Aptos"
            if i == 0:
                run.bold = True
                run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                run.font.size = Pt(size + 0.5)
                shade(cell, "17365D")
            else:
                run.font.size = Pt(size)
                if i % 2 == 0:
                    shade(cell, "F2F6FA")
    d.add_paragraph(style="Normal")


def rs(x):
    s = f"₹{abs(x):,.0f}"
    return ("−" + s) if x < 0 else ("+" + s if x > 0 else s)


d.add_paragraph(style="Normal")
d.add_heading("18. Out-of-sample test of the improvement plan: 18 months of scanner signals "
              "(added 15 November 2026, research round R80)", level=1)
para("Everything above this section is unchanged. This section runs the plan in sections 16 and 17 on data "
     "that no earlier review has used: 1 January 2024 to 19 June 2025, 362 trading days. The thresholds were "
     "fixed and committed to the repository before any result existed. Every variant tried is in the ledger, "
     "including the failures.")

d.add_heading("18.1 Verdict", level=2)
para("No exit and no signal filter produces a positive net expectancy that holds in both periods. The entry "
     "earns about zero before costs (0.3 bps a trade), and costs are about ₹74 a trade. Under section 16.9's own "
     "rule, that means the entry logic needs to be redesigned rather than given more exit or filter settings.",
     bold_prefix="Reject. ")
para("The earlier test on the following year (R56: 20 June 2025 to 6 July 2026, 1,177 trades, net −₹2.52 lakh, "
     "no profitable month) found the same. These results do not depend on which year is examined.",
     bold_prefix="Consistent with the prior result. ")

d.add_heading("18.2 How the test was built", level=2)
for b, txt in [
    ("Signals. ", "ChartInk's archive is not available, so the in-house scanner stands in for it. The scanner "
     "runs the same rules as the two ChartInk screeners, replayed on historical 1-minute bars with no look-ahead. "
     "Caveat on every number: live, the scanner finds 86.8% of ChartInk's hits, but only 36.6% of its own hits are "
     "ChartInk's, and it fires about 12 minutes earlier. The backfill found 13.5 signals a day, which the engine "
     "turned into 4.6 trades a day, against 5.1 in the live record."),
    ("Universe. ", "The NSE F&O list of 1 March 2024 (182 stocks, 180 usable). It is the only dated list "
     "available, so membership is approximate: stocks added to F&O later in the period are left out."),
    ("Engine. ", "The production engine code, unchanged, with the running settings: ₹500 risk, stop at 1.5 × ATR, "
     "volume 2.5×, 70% elapsed, 20-minute signal expiry, six trades a day, exit at 15:20. Entries are taken on "
     "completed 5-minute candles. Stops and trails are checked minute by minute; when the stop and a target fall "
     "inside the same minute, the stop is assumed to come first. "
     "The spec in section 2 names a five-stock subscription limit, but the code has none. Applying one "
     "changes nothing material."),
    ("Charges. ", "Zerodha MIS on each order's real side, with the corrected exchange rate of 0.00307%. A "
     "slippage stress of 5 bps per round trip is reported for every result."),
    ("Split. ", "Train on 2024; validate on January to June 2025. July 2025 onward was already used by R56 and is "
     "not treated as clean."),
]:
    para(txt, style="List Bullet", bold_prefix=b)

d.add_heading("18.3 Exit rules on identical entries (net ₹ per trade)", level=2)
B = {}
import csv  # noqa: E402

for r in csv.DictReader(open(f"{D}/variant_ledger.csv", encoding="utf-8")):
    if r["variant"] == "BASELINE":
        B[(r["exit"], r["side"])] = r
rows = [["Exit", "Side", "Train trades", "Train net", "Validate trades", "Validate net (95% CI)", "With 5 bps slippage"]]
names = {"a": "Current trail (0.6R)", "b": "Stop, else hold to 15:20", "c": "Fixed 2R target",
         "h": "Hybrid: half at 1R, trail rest"}
for ex in ("a", "b", "c", "h"):
    for side in ("long", "short"):
        r = B[(ex, side)]
        ci = r["val_ci"].replace("np.float64(", "").replace(")", "").strip("()").split(",")
        rows.append([names[ex], side, r["train_n"], rs(float(r["train_exp_net"])), r["val_n"],
                     f"{rs(float(r['val_exp_net']))} ({rs(float(ci[0]))} to {rs(float(ci[1]))})",
                     rs(float(r["val_exp_stress"]))])
table(rows, [26, 9, 11, 11, 11, 24, 13])
para("The hybrid was specified before the test: half the position is sold at +1R; the rest has its stop moved to "
     "at least breakeven and trails 1R behind the best 1-minute close. It does no better than the current trail. "
     "The fixed 2R target loses least, but it is negative everywhere. To break even, 38% of trades "
     "would need to reach 2R before the stop; 15–25% do.")

d.add_heading("18.4 The 1:2 attainment ladder (section 17.2)", level=2)
lad = S["ladder"]
rows = [["Side, period", "Trades", "+0.5R", "+1R", "+1.5R", "+2R", "Stop first"]]
for side in ("long", "short"):
    for per, lab in (("train", "2024"), ("validate", "2025 H1")):
        x = lad[f"engine_entries|{side}|{per}"]
        cells = [f"{x[k]['reached_pct']}% · {x[k]['median_min']:.0f} min · {x[k]['reverse_to_stop_pct']}% rev"
                 for k in ("0.5R", "1.0R", "1.5R", "2.0R")]
        rows.append([f"{side}, {lab}", x["0.5R"]["n"], *cells, f"{x['stop_first_pct']}%"])
table(rows, [14, 8, 20, 20, 20, 20, 10], size=7.5)
para("Each cell shows three figures: the share of trades reaching that level before the −1R stop, the median minutes "
     "from entry, and the share of those that later fell back to the stop anyway. 2R is reachable within the "
     "session (median about one hour), but only one trade in five gets there before the stop. Only about 43% reach "
     "+1R first, which is below the 50% that random price movement would give.")
gb = S["giveback"]
para(f"Under the current trail, the median trade's best unrealised gain is {gb['all|train']['peak_R_median']}R "
     f"and it realises {gb['all|train']['realised_R_median']}R. The median amount handed back is "
     f"{gb['all|train']['giveback_R']['p50']}R (worst tenth: {gb['all|train']['giveback_R']['p90']}R). The giveback is "
     "real, but removing it does not turn the strategy positive.", bold_prefix="Giveback, measured: ")

d.add_heading("18.5 The four signal-quality criteria (thresholds fixed in advance)", level=2)
rows = [["Criterion", "Rule", "Result"],
        ["3. Target space (first priority)", "Enough room to reach 2R before the 5-day high (longs) or low (shorts), "
         "or price already beyond it", "Only 10% of trades are blocked; 70% are already past the level. Removing the "
         "blocked ones helps 2025 slightly and not 2024. Still negative. Fails."],
        ["1. Market structure", "Higher high and higher low (or the reverse for shorts) over the last 30 minutes",
         "Small gain in both periods under two exits; still negative, and indistinguishable from a random filter. "
         "Fails."],
        ["2. Index alignment, with trend", "NIFTY up more than 0.10% in the trade's direction at entry",
         "Longs: positive in 2025 H1 (+₹9 to +₹19 a trade) but negative in 2024, inside its error band, and negative "
         "with slippage. Shorts: positive in 2024, strongly negative in 2025. A market-regime effect, not a rule. "
         "Not usable."],
        ["2. Index alignment, counter trend", "NIFTY moving more than 0.10% against the trade",
         "Worse than taking every trade. An earlier review (R65) found counter-trend trades reached +1R more often "
         "(0.61 vs 0.47). On 1,655 new trades it is 0.41 vs 0.46, the other way round."],
        ["4. Entry not extended", "Move from the open to entry no more than 0.75 × the daily ATR",
         "No effect in either direction. Unlike R68's version it was not tuned on the sample, and there was "
         "nothing to lose out of sample. Fails."]]
table(rows, [22, 34, 44])
para("288 variants were evaluated, 72 of them primary. At a 5% significance level about 3–4 would pass "
     "by chance; three passed, and all three are the same with-trend long filter under different exits.")

d.add_heading("18.6 Should the long side be switched off? (section 17.5)", level=2)
ls = S["long_side"]
para(f"Tested as a hypothesis, not assumed. Short-only lost less per day than both sides in 2024 (by "
     f"{rs(ls['a|train']['diff_per_day'])} a day under the current trail), but in 2025 H1 the difference "
     f"({rs(ls['a|validate']['diff_per_day'])} a day) was within its error band under every exit. In 2025 H1, "
     "longs actually beat shorts per trade under every exit. Short-only is itself still loss-making. "
     "Conclusion: not confirmed. Switching off longs would reduce losses, not create profits, and which side does "
     "better changes with the market.")
para("Taking only the first trade of the day loses about ₹50 a trade in both periods. Caps of one, two or three "
     "trades a day leave no combination positive in both periods.")

d.add_heading("18.7 Section 17, point by point", level=2)
rows = [["Section", "Done?", "Note"],
        ["17.1 Define 1:2", "Yes", "Risk R and 2R measured gross; net reported separately. 27.6% of trades lose more "
         "than ₹500 after charges, because sizing ignores charges and gaps. A true all-in ₹500 limit needs about ₹75 "
         "held back for costs."],
        ["17.2 Attainment ladder", "Yes", "Table 18.4, on engine entries and on all 4,572 armed signals."],
        ["17.3 Identical entries", "Yes, except the time exit", "Same entries, stop, size and costs; a target is not "
         "credited when price gaps through it; hybrid specified in advance. A maximum-holding-time exit was not "
         "tested."],
        ["17.4 Net expectancy, not hit rate", "Yes", "Every ledger row has net and gross expectancy, profit factor, "
         "drawdown, charges by component, confidence interval and stress."],
        ["17.5 Selection without data-mining", "Yes", "First-trade-only and caps of 1/2/3 checked; long and short "
         "judged separately; position size never increased."],
        ["17.6 Experiment output", "Yes", "Ledger of 288 rows, failures included, with code hash and date split."],
        ["17.7 Acceptance rule", "Applied", "No variant qualifies; nothing is adopted."],
        ["17.8 Next actions", "Done", "All four items; the 511 live trades were path-reconstructed in research round R79 (13 November 2026)."]]
table(rows, [24, 14, 62])

d.add_heading("18.8 Recommendation", level=2)
para("Do not change exits or add filters to this entry. Eighteen months of new data show its gross edge is "
     "about zero, so no exit can make it pay its costs. If the strategy continues, the next piece of work is a "
     "different entry trigger, tested the same way. The with-trend long observation (18.5) may be recorded as "
     "a paper-only forward log over the next 40 or more trades. It should not be traded on.")
para("Prepared from the openalgo repository on 15 November 2026. Research record: "
     "docs/research/strategy/screener/2026-11-15_r80_steps2-4_scanner_backfill_exits_criteria.md (round R80). "
     "Every result is a simulation on historical data, not live trading. This is not investment advice.")
d.save(OUT)
print("saved", OUT)
