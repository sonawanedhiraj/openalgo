"""R85 Phase 1 validation of the stock-option bhavcopy dataset (DuckDB aggregation; 77M rows do not fit pandas).
Calendar = NSE trading days from the NIFTY daily series (R82 bars_fwd), 2019-01-01 -> 2026-11-13.
  uv run python backtest/r85_options/validate.py"""

import json

import duckdb
import engine as E
import pandas as pd

ROOT = E.ROOT


def main():
    c = E.con()
    cal = (
        duckdb.connect(str(ROOT / "backtest/r82_sizing/data/bars_fwd.duckdb"), read_only=True)
        .execute(
            "select bar_date from d1 where symbol='IDX:NIFTY' and bar_date between '2019-01-01' and '2026-11-13'"
        )
        .df()
    )
    cal = set(pd.to_datetime(cal.bar_date))
    have = set(pd.to_datetime(c.execute("select distinct date from fo").df().date))
    missing = sorted(cal - have)
    extra = sorted(have - cal)
    yr = (
        c.execute("""
        select year(date) yr, count(distinct date) n_days, count(*) / count(distinct date) rows_per_day,
               sum(case when contracts > 0 then 1 else 0 end) / count(distinct date) traded_rows_per_day,
               100 - 100.0 * sum(case when contracts > 0 then 1 else 0 end) / count(*) zero_volume_pct,
               count(distinct symbol) symbols, string_agg(distinct fmt, ',') fmt
        from fo where kind='OPT' group by 1 order by 1""")
        .df()
        .set_index("yr")
    )
    yr["calendar_days"] = pd.Series([d.year for d in cal]).value_counts().sort_index()
    yr["missing_days"] = (
        pd.Series([d.year for d in missing], dtype=int)
        .value_counts()
        .reindex(yr.index)
        .fillna(0)
        .astype(int)
    )
    # lot-size recovery on UDiFF days where the true lot is known
    lot_chk = (
        c.execute("""
        with f as (select symbol, date, expiry, "close", contracts, value_lakh, lot, fmt,
                          row_number() over (partition by symbol, date order by expiry) rn from fo where kind='FUT')
        select avg(case when abs(round(value_lakh*1e5/(contracts*"close")/25)*25 / lot - 1) < 0.02 then 1.0 else 0.0 end) ok,
               count(*) n from f where rn=1 and fmt='udiff' and contracts > 0 and lot > 0""")
        .df()
        .iloc[0]
    )
    # liquidity per symbol-year: front expiry, strikes within +-15% of front-futures close
    liq = c.execute("""
        with fut as (select symbol, date, "close" s, row_number() over (partition by symbol, date order by expiry) rn
                     from fo where kind='FUT'),
             o as (select o.symbol, o.date, o.expiry, o.strike, o.contracts, o.oi, f.s,
                          min(o.expiry) over (partition by o.symbol, o.date) fe
                   from fo o join fut f on f.symbol=o.symbol and f.date=o.date and f.rn=1 where o.kind='OPT'),
             d as (select symbol, date, sum(contracts) contracts,
                          sum(case when contracts > 0 then 1 else 0 end) strikes_traded, count(*) strikes
                   from o where expiry = fe and abs(strike / s - 1) <= 0.15 group by 1, 2)
        select year(date) yr, symbol, median(contracts) med_contracts, median(strikes_traded) med_strikes_traded,
               avg(strikes_traded * 1.0 / strikes) share_strikes_traded
        from d group by 1, 2""").df()
    tiers = {"any_options": -1, ">=100 contracts/day": 100, ">=500": 500, ">=2000": 2000}
    usable = pd.DataFrame(
        {k: liq[liq.med_contracts > v].groupby("yr").symbol.nunique() for k, v in tiers.items()}
    )
    near = liq.groupby("yr").agg(median_share_strikes_traded=("share_strikes_traded", "median"))
    out = {
        "calendar_days": len(cal),
        "days_with_file": len(have),
        "missing_sessions": [str(d.date()) for d in missing],
        "non_calendar_days_in_files": [str(d.date()) for d in extra],
        "legacy_lot_formula_matches_udiff_lot_within_2pct": round(float(lot_chk.ok), 4),
        "lot_check_n": int(lot_chk.n),
        "opt_rows_total": int(c.execute("select count(*) from fo where kind='OPT'").fetchone()[0]),
    }
    yr.to_csv(E.D / "phase1_coverage_by_year.csv")
    usable.join(near).to_csv(E.D / "phase1_usable_names_by_year.csv")
    liq.to_csv(E.D / "phase1_symbol_liquidity.csv", index=False)
    json.dump(out, open(E.D / "phase1_summary.json", "w"), indent=1)
    print(yr.round(1).to_string())
    print(usable.join(near).round(3).to_string())
    print(
        json.dumps(
            {
                k: (v if not isinstance(v, list) or len(v) < 20 else f"{len(v)} items: {v[:5]}...")
                for k, v in out.items()
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
