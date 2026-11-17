"""Materialise the bhavcopy parquet into data/fo.duckdb sorted by (kind, symbol, expiry, date) for fast chain lookups."""

import duckdb
import engine as E

if __name__ == "__main__":
    c = duckdb.connect(str(E.D / "fo.duckdb"))
    src = E.GLOB
    c.execute(
        "create or replace table fo as select date, kind, symbol, expiry, cast(strike as double) strike, opt, "
        'cast("close" as double) "close", cast(settle as double) settle, cast(contracts as double) contracts, '
        "cast(value_lakh as double) value_lakh, cast(oi as double) oi, cast(lot as double) lot, fmt "
        f"from read_parquet('{src}', union_by_name=true) order by kind, symbol, expiry, date"
    )
    print(c.execute("select count(*) from fo").fetchone())
