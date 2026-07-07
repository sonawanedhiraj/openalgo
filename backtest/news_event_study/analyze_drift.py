"""R45 PEAD-style multi-horizon post-event drift study.

R43 (intraday chase) and R44 (delayed reversal/continuation patterns) both
killed same-day/T+1 entries on news events -- see
strategies/STRATEGY_REGISTRY.md and docs/research/journal. The literature
suggests any real edge in news-event trading is multi-day post-event drift
(PEAD) conditioned on the day-0 tape reaction, not a same-day chase. This
script tests that hypothesis directly: does the day-0 reaction (return,
volume confirmation) predict abnormal, market-adjusted drift over the
following T+1..T+20 trading days?

Inputs
------
1. ``outputs/news_event_study/announcements.duckdb`` table ``events_filtered``
   -- seq_id, symbol, category, polarity, announced_at, summary_text,
   eff_date, entry_mode.
2. ``outputs/news_event_study/prices.duckdb`` table ``prices_daily_fwd`` --
   daily bars (symbol, bar_date, open, high, low, close, volume) for every
   event symbol AND the index proxy symbol 'NIFTY', spanning
   2025-06-01..2026-07-07. READ-ONLY here -- a separate process may be
   writing this file; ``connect_prices_readonly`` (imported from
   ``simulate.py``) already handles a locked/being-written file via a
   retry + temp-copy fallback.
3. ``db/openalgo.db`` (SQLite) table ``symtoken`` -- F&O underlyings are
   ``exchange='NFO' AND instrumenttype='FUT'``, underlying name in the
   ``name`` column (verified against the live DB: e.g. brsymbol
   'NIFTY26JULFUT' / symbol 'NIFTY28JUL26FUT' both carry name='NIFTY').
   Opened via a SQLite read-only URI connection (``mode=ro``) so this never
   competes for the app's write lock.

Output
------
``outputs/news_event_study/results_r45.duckdb`` table ``event_drift`` (one
row per distinct (symbol, eff_date, polarity, category) event group) plus
the six printed summary tables behind ``--summary``, plus the three
pre-declared strategy sims behind ``--summary`` (or ``--sims``).

IMPORTANT schema note -- SAST category is not in events_filtered
------------------------------------------------------------------
``events_filtered`` does NOT contain any row with
category='Disclosure under SEBI Takeover Regulations' (verified against the
real announcements.duckdb: 0 of 16,151 filtered rows; the raw
``announcements`` table has 4,711 such rows, so events_filtered's own
polarity-classification pass evidently drops this category since it has no
positive/negative/results/tape_decide polarity assigned). Since the R45 spec
requires a SAST promoter-direction module, this script pulls those rows
directly from the raw ``announcements`` table as a supplement (see
``load_sast_supplement``), deriving eff_date/entry_mode with the same
cutoff rule events_filtered itself uses (empirically: announced_at time
< 15:25:00 -> same-day 'intraday' eff_date, >= 15:25:00 -> next-calendar-day
'at_open' eff_date -- confirmed by inspecting the transition band in
events_filtered). These rows get polarity='sast' (a new value, not one of
the four declared polarities) so they flow through the same per-event drift
pipeline; they are excluded from the polarity-based summary tables (which
only cover positive/negative/results/tape_decide) but included in fno/
price-bucket/reaction-bucket enrichment and the dedicated SAST summary + H3
sim.

IMPORTANT caveat -- SAST classifier boilerplate collision
-----------------------------------------------------------
The classifier is implemented literally per the pre-declared spec (keyword
buckets: buy=acquisition/acquire/purchase/bought/creeping,
sell=disposal/dispose/sale/sold, pledge=pledge/invocation/invoke, else
unclear), checked in that order. But the SAST regulation's own full name is
"Securities and Exchange Board of India (Substantial Acquisition of Shares
and Takeovers) Regulations, 2011" -- the word "Acquisition" appears in the
boilerplate regulatory citation of nearly EVERY SAST filing regardless of
whether the disclosed event is actually an acquisition, a disposal, or a
pledge/release. Since 'buy' is checked first, this is expected to produce a
heavily 'buy'-skewed sast_direction distribution once run on real data. This
is flagged here rather than silently patched by adding boilerplate-stripping
logic that isn't in the pre-declared spec; the parent session should review
the printed SAST summary table's buy/sell/pledge/unclear split before
trusting H3's n.

Distinct-event-unit grouping
-----------------------------
The spec's unit is "distinct (symbol, eff_date, polarity, category)". Some
symbol-days have multiple raw announcement rows sharing this key (e.g.
several board-outcome sub-filings same day). Since day0/prev_close/AR are
identical for all of them, this script groups the raw event rows on
(symbol, eff_date, polarity, category) BEFORE the price computation --
one output row per group, with seq_id kept as the group's first seq_id,
n_announcements as the group size, and summary_text concatenated (joined by
' || ') so SAST classification sees the union of all filings in the group.

Usage
-----
    uv run python backtest/news_event_study/analyze_drift.py --summary
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import date as date_cls
from datetime import timedelta
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

# Make the sibling simulate.py importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from simulate import (  # noqa: E402
    ADVERSE_SLIPPAGE,
    cnc_charges,
    connect_announcements_readonly,
    connect_prices_readonly,
)

# --------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------

DEFAULT_ANNOUNCEMENTS = "outputs/news_event_study/announcements.duckdb"
DEFAULT_PRICES = "outputs/news_event_study/prices.duckdb"
DEFAULT_OPENALGO_DB = "db/openalgo.db"
DEFAULT_OUT = "outputs/news_event_study/results_r45.duckdb"

HORIZONS = [1, 2, 3, 5, 10, 15, 20]

INDEX_SYMBOL = "NIFTY"

SAST_CATEGORY = "Disclosure under SEBI Takeover Regulations"
SAST_POLARITY = "sast"
# Empirically-derived cutoff: events_filtered flips from same-day 'intraday'
# to next-day 'at_open' at announced_at time >= 15:25:00.
SAST_EFF_DATE_CUTOFF_TIME = "15:25:00"

DAY0_ROLL_FORWARD_DAYS = 3  # try eff_date, +1, +2, +3 calendar days
MIN_PRIOR_BARS_FOR_AVG20 = 20
PENNY_FLOOR = 50.0
VOL_CONFIRM_MULT = 2.0
HALF_SPLIT_DATE = date_cls(2026, 1, 1)

POSITION_SIZE = 50000.0

PRICE_BUCKET_EDGES = [
    (50.0, 150.0, "50-150"),
    (150.0, 500.0, "150-500"),
    (500.0, float("inf"), ">500"),
]

# --------------------------------------------------------------------------
# SAST promoter-direction classifier (pre-declared keyword buckets)
# --------------------------------------------------------------------------

_BUY_WORDS = [
    "acquisition",
    "acquire",
    "acquired",
    "acquiring",
    "purchase",
    "purchased",
    "purchasing",
    "bought",
    "creeping",
]
_SELL_WORDS = ["disposal", "dispose", "disposed", "disposing", "sale", "sold"]
_PLEDGE_WORDS = ["pledge", "pledged", "pledging", "invocation", "invoke", "invoked", "invoking"]

_BUY_RE = re.compile(r"\b(" + "|".join(_BUY_WORDS) + r")\b", re.IGNORECASE)
_SELL_RE = re.compile(r"\b(" + "|".join(_SELL_WORDS) + r")\b", re.IGNORECASE)
_PLEDGE_RE = re.compile(r"\b(" + "|".join(_PLEDGE_WORDS) + r")\b", re.IGNORECASE)


def classify_sast_direction(summary_text: str | None) -> str:
    """Classify a SAST filing's summary_text into buy/sell/pledge/unclear.

    Checked in the pre-declared order: buy, then sell, then pledge, else
    unclear. See module docstring for the boilerplate-collision caveat.
    """
    text = (summary_text or "").lower()
    if _BUY_RE.search(text):
        return "buy"
    if _SELL_RE.search(text):
        return "sell"
    if _PLEDGE_RE.search(text):
        return "pledge"
    return "unclear"


# --------------------------------------------------------------------------
# Event loading
# --------------------------------------------------------------------------


def load_events_filtered(ann_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = ann_con.execute(
        """
        SELECT
            seq_id,
            symbol,
            category,
            polarity,
            CAST(eff_date AS DATE) AS eff_date,
            announced_at,
            summary_text,
            entry_mode
        FROM events_filtered
        """
    ).df()
    return df


def _derive_sast_eff_date_and_mode(announced_at: pd.Timestamp) -> tuple[date_cls, str]:
    cutoff = pd.Timestamp(SAST_EFF_DATE_CUTOFF_TIME).time()
    if announced_at.time() < cutoff:
        return announced_at.date(), "intraday"
    return announced_at.date() + timedelta(days=1), "at_open"


def load_sast_supplement(
    ann_con: duckdb.DuckDBPyConnection, existing_seq_ids: set[str]
) -> pd.DataFrame:
    """Pull SAST ('Disclosure under SEBI Takeover Regulations') rows directly
    from the raw ``announcements`` table -- not present in events_filtered.
    See module docstring for why. Anti-joined against seq_ids already present
    in events_filtered (defensive; currently none overlap)."""
    raw = ann_con.execute(
        f"""
        SELECT seq_id, symbol, category, summary_text, announced_at
        FROM announcements
        WHERE category = '{SAST_CATEGORY}'
        """
    ).df()
    if raw.empty:
        return pd.DataFrame(
            columns=[
                "seq_id",
                "symbol",
                "category",
                "polarity",
                "eff_date",
                "announced_at",
                "summary_text",
                "entry_mode",
            ]
        )
    raw = raw[~raw["seq_id"].isin(existing_seq_ids)].copy()
    if raw.empty:
        return pd.DataFrame(
            columns=[
                "seq_id",
                "symbol",
                "category",
                "polarity",
                "eff_date",
                "announced_at",
                "summary_text",
                "entry_mode",
            ]
        )
    raw["announced_at"] = pd.to_datetime(raw["announced_at"])
    derived = raw["announced_at"].apply(_derive_sast_eff_date_and_mode)
    raw["eff_date"] = derived.apply(lambda t: t[0])
    raw["entry_mode"] = derived.apply(lambda t: t[1])
    raw["polarity"] = SAST_POLARITY
    return raw[
        [
            "seq_id",
            "symbol",
            "category",
            "polarity",
            "eff_date",
            "announced_at",
            "summary_text",
            "entry_mode",
        ]
    ]


def load_all_events(ann_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    ef = load_events_filtered(ann_con)
    sast = load_sast_supplement(ann_con, existing_seq_ids=set(ef["seq_id"]))
    combined = pd.concat([ef, sast], ignore_index=True) if not sast.empty else ef
    # NOTE: pd.Timestamp subclasses datetime.date, so `not isinstance(d, date_cls)`
    # would never fire for Timestamps (the simulate.py type-drift bug class).
    # Plain dates have no .date() method, so hasattr alone is the right guard.
    combined["eff_date"] = combined["eff_date"].apply(
        lambda d: d.date() if hasattr(d, "date") else d
    )
    return combined


def group_events(events: pd.DataFrame) -> pd.DataFrame:
    """Collapse to one row per distinct (symbol, eff_date, polarity, category)
    group -- see module docstring "Distinct-event-unit grouping"."""
    if events.empty:
        return events.assign(n_announcements=[])

    def _join_text(s: pd.Series) -> str:
        parts = [str(x) for x in s if x is not None and str(x).strip() != ""]
        return " || ".join(parts)

    grouped = events.groupby(["symbol", "eff_date", "polarity", "category"], as_index=False).agg(
        seq_id=("seq_id", "first"),
        n_announcements=("seq_id", "count"),
        summary_text=("summary_text", _join_text),
        announced_at=("announced_at", "min"),
        entry_mode=("entry_mode", "first"),
    )
    return grouped


# --------------------------------------------------------------------------
# F&O underlyings
# --------------------------------------------------------------------------


def load_fno_underlyings(openalgo_db_path: str) -> set[str]:
    """Read-only via SQLite URI (mode=ro) -- never competes for the app's
    write lock, and NullPool concerns in CLAUDE.md are about the live app's
    SQLAlchemy engine, not a one-off raw sqlite3 read here."""
    db_path = Path(openalgo_db_path).resolve()
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT name FROM symtoken WHERE exchange='NFO' AND instrumenttype='FUT'"
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows if r[0]}


# --------------------------------------------------------------------------
# Price-series helpers (pure -- operate on in-memory DataFrames so they are
# unit-testable against synthetic fixtures without ever opening prices.duckdb)
# --------------------------------------------------------------------------


def prep_symbol_series(df: pd.DataFrame) -> pd.DataFrame:
    """Sort + dedup a symbol's daily bars by bar_date ascending."""
    out = df.drop_duplicates(subset=["bar_date"]).sort_values("bar_date").reset_index(drop=True)
    return out


def nifty_close_asof(
    nifty_dates: np.ndarray, nifty_closes: np.ndarray, target: date_cls
) -> float | None:
    """Last NIFTY close on or before `target`. nifty_dates must be sorted
    ascending (as date objects or np.datetime64), nifty_closes aligned."""
    idx = np.searchsorted(nifty_dates, np.datetime64(target), side="right") - 1
    if idx < 0:
        return None
    return float(nifty_closes[idx])


def find_day0_position(symbol_series: pd.DataFrame, eff_date: date_cls) -> int | None:
    """Return the row position of day0 in symbol_series (bar on eff_date, or
    the first trading day found rolling forward up to
    DAY0_ROLL_FORWARD_DAYS calendar days), else None."""
    bar_dates = symbol_series["bar_date"].values
    for offset in range(0, DAY0_ROLL_FORWARD_DAYS + 1):
        cand = np.datetime64(eff_date + timedelta(days=offset))
        matches = np.where(bar_dates == cand)[0]
        if len(matches) > 0:
            return int(matches[0])
    return None


# --------------------------------------------------------------------------
# Per-event metric computation
# --------------------------------------------------------------------------


def price_bucket_for(prev_close: float) -> str | None:
    for lo, hi, label in PRICE_BUCKET_EDGES:
        if lo <= prev_close < hi:
            return label
    return None


def reaction_bucket_for(r0: float) -> str:
    """5 buckets covering the whole real line, boundaries assigned to the
    lower bucket except the leftmost (<=-5% is closed on the right at -5%)."""
    if r0 <= -0.05:
        return "<=-5%"
    if r0 < -0.02:
        return "-5..-2%"
    if r0 < 0.02:
        return "-2..+2%"
    if r0 < 0.05:
        return "+2..+5%"
    return ">+5%"


def compute_event_metrics(
    symbol_series: pd.DataFrame,
    nifty_dates: np.ndarray,
    nifty_closes: np.ndarray,
    eff_date: date_cls,
    fno_set: set[str],
    symbol: str,
) -> dict:
    """Core pure computation for one event group. symbol_series must already
    be prep_symbol_series()'d (sorted, deduped, columns bar_date/open/high/
    low/close/volume). Returns a dict of computed fields including
    skip_reason (None if the event was fully computed)."""

    result: dict = {
        "day0_date": None,
        "prev_close": None,
        "avg20_vol": None,
        "r0": None,
        "gap0": None,
        "vol_confirm": None,
        "fno": symbol in fno_set,
        "price_bucket": None,
        "reaction_bucket": None,
        "skip_reason": None,
    }
    for h in HORIZONS:
        result[f"ar_{h}"] = None
        result[f"ar_open_{h}"] = None

    day0_pos = find_day0_position(symbol_series, eff_date)
    if day0_pos is None:
        result["skip_reason"] = "no_day0"
        return result

    day0_row = symbol_series.iloc[day0_pos]
    day0_date = pd.Timestamp(day0_row["bar_date"]).date()
    result["day0_date"] = day0_date

    n_prior = day0_pos  # number of bars strictly before day0
    if n_prior < MIN_PRIOR_BARS_FOR_AVG20:
        result["skip_reason"] = "insufficient_history"
        return result

    prior = symbol_series.iloc[:day0_pos]
    prev_close = float(prior["close"].iloc[-1])
    avg20_vol = float(prior["volume"].iloc[-MIN_PRIOR_BARS_FOR_AVG20:].mean())
    result["prev_close"] = prev_close
    result["avg20_vol"] = avg20_vol

    if prev_close < PENNY_FLOOR:
        result["skip_reason"] = "penny"
        return result

    day0_close = float(day0_row["close"])
    day0_open = float(day0_row["open"])
    day0_volume = float(day0_row["volume"])

    r0 = (day0_close / prev_close) - 1.0
    gap0 = (day0_open / prev_close) - 1.0
    vol_confirm = day0_volume >= VOL_CONFIRM_MULT * avg20_vol

    result["r0"] = r0
    result["gap0"] = gap0
    result["vol_confirm"] = bool(vol_confirm)
    result["price_bucket"] = price_bucket_for(prev_close)
    result["reaction_bucket"] = reaction_bucket_for(r0)

    nifty_close_day0 = nifty_close_asof(nifty_dates, nifty_closes, day0_date)

    n = len(symbol_series)
    # T+1 open, for the ar_open_h variant (implementable entry)
    t1_pos = day0_pos + 1
    t1_open = float(symbol_series.iloc[t1_pos]["open"]) if t1_pos < n else None

    for h in HORIZONS:
        target_pos = day0_pos + h
        if target_pos >= n:
            continue  # stays None: truncated (beyond available data)
        target_close = float(symbol_series.iloc[target_pos]["close"])
        target_date = pd.Timestamp(symbol_series.iloc[target_pos]["bar_date"]).date()

        nifty_close_h = nifty_close_asof(nifty_dates, nifty_closes, target_date)

        if nifty_close_day0 is None or nifty_close_h is None:
            continue  # can't market-adjust; leave both AR variants None

        mkt_adj = (nifty_close_h / nifty_close_day0) - 1.0

        ar_h = (target_close / day0_close - 1.0) - mkt_adj
        result[f"ar_{h}"] = ar_h

        if t1_open is not None and t1_open > 0:
            # documented simplification: NIFTY adjustment term is the SAME
            # close[day0]->close[day0+h] window for both variants (avoids
            # relying on NIFTY's own T+1 open, which is less reliable).
            ar_open_h = (target_close / t1_open - 1.0) - mkt_adj
            result[f"ar_open_{h}"] = ar_open_h

    return result


# --------------------------------------------------------------------------
# DB-backed loaders (real run only -- not exercised by synthetic validation)
# --------------------------------------------------------------------------


def load_prices_by_symbol(
    prices_con: duckdb.DuckDBPyConnection, symbols: list[str]
) -> dict[str, pd.DataFrame]:
    if not symbols:
        return {}
    prices_con.register("symlist_r45", pd.DataFrame({"symbol": symbols}))
    df = prices_con.execute(
        """
        SELECT p.symbol, p.bar_date, p.open, p.high, p.low, p.close, p.volume
        FROM prices_daily_fwd p
        JOIN symlist_r45 s ON p.symbol = s.symbol
        ORDER BY p.symbol, p.bar_date
        """
    ).df()
    prices_con.unregister("symlist_r45")
    if df.empty:
        return {}
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    out = {}
    for sym, g in df.groupby("symbol"):
        out[sym] = prep_symbol_series(g)
    return out


def load_nifty_series(prices_con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    df = prices_con.execute(
        f"""
        SELECT bar_date, close
        FROM prices_daily_fwd
        WHERE symbol = '{INDEX_SYMBOL}'
        ORDER BY bar_date
        """
    ).df()
    df["bar_date"] = pd.to_datetime(df["bar_date"]).dt.date
    return df.drop_duplicates(subset=["bar_date"]).sort_values("bar_date").reset_index(drop=True)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_event_drift(
    events_grouped: pd.DataFrame,
    prices_by_symbol: dict[str, pd.DataFrame],
    nifty_series: pd.DataFrame,
    fno_set: set[str],
) -> pd.DataFrame:
    nifty_dates = np.array([np.datetime64(d) for d in nifty_series["bar_date"]])
    nifty_closes = nifty_series["close"].values.astype(float)

    rows = []
    for ev in events_grouped.itertuples(index=False):
        symbol = ev.symbol
        eff_date = ev.eff_date
        symbol_series = prices_by_symbol.get(symbol)

        base = {
            "seq_id": ev.seq_id,
            "n_announcements": ev.n_announcements,
            "symbol": symbol,
            "category": ev.category,
            "polarity": ev.polarity,
            "eff_date": eff_date,
            "announced_at": ev.announced_at,
        }
        base["sast_direction"] = (
            classify_sast_direction(ev.summary_text) if ev.category == SAST_CATEGORY else None
        )
        base["half"] = "A" if eff_date < HALF_SPLIT_DATE else "B"

        if symbol_series is None or symbol_series.empty:
            base["skip_reason"] = "no_day0"
            for h in HORIZONS:
                base[f"ar_{h}"] = None
                base[f"ar_open_{h}"] = None
            base.update(
                {
                    "day0_date": None,
                    "prev_close": None,
                    "avg20_vol": None,
                    "r0": None,
                    "gap0": None,
                    "vol_confirm": None,
                    "fno": symbol in fno_set,
                    "price_bucket": None,
                    "reaction_bucket": None,
                }
            )
            rows.append(base)
            continue

        metrics = compute_event_metrics(
            symbol_series=symbol_series,
            nifty_dates=nifty_dates,
            nifty_closes=nifty_closes,
            eff_date=eff_date,
            fno_set=fno_set,
            symbol=symbol,
        )
        base.update(metrics)
        rows.append(base)

    return pd.DataFrame(rows)


def run(
    announcements_path: str,
    prices_path: str,
    openalgo_db_path: str,
    out_path: str,
    print_summary: bool,
) -> dict:
    ann_con = connect_announcements_readonly(announcements_path)
    prices_con = connect_prices_readonly(prices_path)

    print("[run] loading events (events_filtered + SAST supplement)...")
    events = load_all_events(ann_con)
    print(f"[run] {len(events)} raw event rows loaded")

    events_grouped = group_events(events)
    print(f"[run] {len(events_grouped)} distinct (symbol, eff_date, polarity, category) groups")

    print("[run] loading F&O underlyings from db/openalgo.db symtoken...")
    fno_set = load_fno_underlyings(openalgo_db_path)
    print(f"[run] {len(fno_set)} distinct F&O underlyings")

    symbols_needed = sorted(events_grouped["symbol"].unique().tolist())
    print(f"[run] loading prices_daily_fwd for {len(symbols_needed)} symbols + NIFTY...")
    prices_by_symbol = load_prices_by_symbol(prices_con, symbols_needed)
    nifty_series = load_nifty_series(prices_con)
    print(f"[run] NIFTY has {len(nifty_series)} daily bars")

    event_drift = build_event_drift(events_grouped, prices_by_symbol, nifty_series, fno_set)

    print(f"[run] writing event_drift ({len(event_drift)} rows) to {out_path}...")
    out_con = duckdb.connect(out_path)
    out_con.execute("DROP TABLE IF EXISTS event_drift")
    out_con.register("event_drift_df", event_drift)
    out_con.execute("CREATE TABLE event_drift AS SELECT * FROM event_drift_df")
    out_con.close()

    ann_con.close()
    prices_con.close()

    raw_trade_frame = None
    if print_summary:
        print_all_summaries(event_drift)
        raw_trade_frame = build_raw_trade_frame(events_grouped, prices_by_symbol, event_drift)
        run_sims(raw_trade_frame)

    return {
        "n_events": len(event_drift),
        "event_drift": event_drift,
        "raw_trade_frame": raw_trade_frame,
    }


# --------------------------------------------------------------------------
# Summary tables (1-6)
# --------------------------------------------------------------------------


def _tstat(s: pd.Series) -> float | None:
    s = s.dropna()
    n = len(s)
    if n < 2:
        return None
    std = s.std(ddof=1)
    if std == 0 or pd.isna(std):
        return None
    return float(s.mean() / (std / np.sqrt(n)))


def _ar_long(df: pd.DataFrame, group_cols: list[str]) -> pd.DataFrame:
    """Melt ar_{h} columns to long form (horizon, ar) and aggregate by
    group_cols + horizon -> n, mean, tstat."""
    ar_cols = [f"ar_{h}" for h in HORIZONS]
    keep = group_cols + ar_cols
    long = df[keep].melt(
        id_vars=group_cols, value_vars=ar_cols, var_name="horizon", value_name="ar"
    )
    long["horizon"] = long["horizon"].str.replace("ar_", "", regex=False).astype(int)
    long = long.dropna(subset=["ar"])
    agg = (
        long.groupby(group_cols + ["horizon"])
        .agg(n=("ar", "count"), mean_ar=("ar", "mean"), tstat=("ar", _tstat))
        .reset_index()
    )
    return agg


def summary_reaction_bucket_by_horizon(df: pd.DataFrame) -> None:
    print("\n=== Summary 1: AR by reaction_bucket x horizon (pooled) ===")
    pooled = _ar_long(df.dropna(subset=["reaction_bucket"]), ["reaction_bucket"])
    print(pooled.sort_values(["reaction_bucket", "horizon"]).to_string(index=False))

    print("\n=== Summary 1b: AR by reaction_bucket x horizon, half A vs half B ===")
    for half in ["A", "B"]:
        sub = df[(df["half"] == half) & df["reaction_bucket"].notna()]
        agg = _ar_long(sub, ["reaction_bucket"])
        agg["half"] = half
        print(f"--- half {half} ---")
        print(agg.sort_values(["reaction_bucket", "horizon"]).to_string(index=False))


def summary_polarity_reaction_match(df: pd.DataFrame) -> None:
    print("\n=== Summary 2: AR by polarity x reaction-direction-match ===")
    sub = df[df["polarity"].isin(["positive", "negative", "results", "tape_decide"])].copy()
    sub = sub.dropna(subset=["r0"])

    def _match_label(row) -> str:
        if row["polarity"] == "positive" and row["r0"] > 0.02:
            return "positive & r0>+2%"
        if row["polarity"] == "negative" and row["r0"] < -0.02:
            return "negative & r0<-2%"
        return "other"

    sub["match_label"] = sub.apply(_match_label, axis=1)
    sub = sub[sub["match_label"] != "other"]
    agg = _ar_long(sub, ["polarity", "match_label"])
    print(agg.sort_values(["polarity", "match_label", "horizon"]).to_string(index=False))


def summary_fno_vs_nonfno(df: pd.DataFrame) -> None:
    print("\n=== Summary 3: fno vs non-fno x reaction_bucket @ h=5,10,20 ===")
    sub = df.dropna(subset=["reaction_bucket"])
    agg = _ar_long(sub, ["fno", "reaction_bucket"])
    agg = agg[agg["horizon"].isin([5, 10, 20])]
    print(agg.sort_values(["reaction_bucket", "fno", "horizon"]).to_string(index=False))


def summary_top_categories(df: pd.DataFrame) -> None:
    print("\n=== Summary 4: Top-10 categories by |ar_10|, n>=100, halves side by side ===")
    sub = df.dropna(subset=["ar_10"])
    cat_stats = (
        sub.groupby("category")
        .agg(n=("ar_10", "count"), mean_ar_10=("ar_10", "mean"))
        .reset_index()
    )
    cat_stats = cat_stats[cat_stats["n"] >= 100]
    cat_stats["abs_mean_ar_10"] = cat_stats["mean_ar_10"].abs()
    top = cat_stats.sort_values("abs_mean_ar_10", ascending=False).head(10)
    rows = []
    for cat in top["category"]:
        for half in ["A", "B"]:
            half_sub = sub[(sub["category"] == cat) & (sub["half"] == half)]
            rows.append(
                {
                    "category": cat,
                    "half": half,
                    "n": len(half_sub),
                    "mean_ar_10": half_sub["ar_10"].mean() if len(half_sub) else None,
                }
            )
    print(pd.DataFrame(rows).to_string(index=False))


def summary_sast(df: pd.DataFrame) -> None:
    print("\n=== Summary 5: SAST sast_direction x horizon AR ===")
    sub = df[df["polarity"] == SAST_POLARITY]
    print(f"(n SAST events total: {len(sub)}; sast_direction value_counts:)")
    print(sub["sast_direction"].value_counts(dropna=False).to_string())
    agg = _ar_long(sub, ["sast_direction"])
    print(agg.sort_values(["sast_direction", "horizon"]).to_string(index=False))


def summary_vol_confirm_interaction(df: pd.DataFrame) -> None:
    print("\n=== Summary 6: vol_confirm interaction on +2..+5% and >+5% buckets ===")
    sub = df[df["reaction_bucket"].isin(["+2..+5%", ">+5%"])].dropna(subset=["vol_confirm"])
    agg = _ar_long(sub, ["reaction_bucket", "vol_confirm"])
    print(agg.sort_values(["reaction_bucket", "vol_confirm", "horizon"]).to_string(index=False))


def print_all_summaries(df: pd.DataFrame) -> None:
    pd.set_option("display.max_rows", 300)
    pd.set_option("display.width", 200)
    summary_reaction_bucket_by_horizon(df)
    summary_polarity_reaction_match(df)
    summary_fno_vs_nonfno(df)
    summary_top_categories(df)
    summary_sast(df)
    summary_vol_confirm_interaction(df)


# --------------------------------------------------------------------------
# Pre-declared strategy sims (H1, H2, H3) -- no mining, exactly as specified
# --------------------------------------------------------------------------


def _simulate_trade(entry_open: float, exit_close: float) -> dict | None:
    """CNC long, T+1 open entry, horizon close exit. 0.25%/leg slippage,
    CNC delivery charges via simulate.cnc_charges (imported, not
    re-derived -- keeps R43/R44/R45 pricing the same trade the same way)."""
    if entry_open is None or exit_close is None or pd.isna(entry_open) or pd.isna(exit_close):
        return None
    if entry_open <= 0:
        return None
    entry_px = entry_open * (1 + ADVERSE_SLIPPAGE)
    exit_px = exit_close * (1 - ADVERSE_SLIPPAGE)
    qty = int(POSITION_SIZE // entry_px)
    if qty <= 0:
        return None
    buy_value = entry_px * qty
    sell_value = exit_px * qty
    gross_pnl = (exit_px - entry_px) * qty
    charges = cnc_charges(buy_value=buy_value, sell_value=sell_value)
    net_pnl = gross_pnl - charges["total"]
    net_ret_pct = (net_pnl / buy_value) * 100.0 if buy_value > 0 else None
    return {"net_pnl": net_pnl, "net_ret_pct": net_ret_pct}


def run_sims(raw_trade_frame: pd.DataFrame | None) -> None:
    """Print the 3 pre-declared strategy sims. `raw_trade_frame`, if given,
    must have columns [symbol, eff_date, polarity, category, half,
    entry_open_t1, exit_close_h10, exit_close_h20, sast_direction, r0,
    vol_confirm] -- i.e. the raw (non-market-adjusted) prices needed to cost
    an actual CNC trade. When None (e.g. in the synthetic-only validation
    harness where such a frame is built directly), sims are skipped with a
    note -- event_drift alone (AR ratios) is sufficient for Summaries 1-6 but
    not for cash P&L, which needs raw entry/exit prices."""
    print("\n=== Pre-declared strategy sims (H1/H2/H3) ===")
    if raw_trade_frame is None or raw_trade_frame.empty:
        print(
            "[run_sims] no raw_trade_frame supplied (event_drift only stores "
            "market-adjusted AR ratios, not raw prices) -- skipping cash P&L "
            "sims. The real run wires this from prices_by_symbol via "
            "build_raw_trade_frame()."
        )
        return

    sims = [
        (
            "H1_results_r0ge3pct_volconfirm_h10",
            (raw_trade_frame["polarity"] == "results")
            & (raw_trade_frame["r0"] >= 0.03)
            & (raw_trade_frame["vol_confirm"])
            & raw_trade_frame["exit_close_h10"].notna(),
            "exit_close_h10",
        ),
        (
            "H2_bagging_orders_r0ge2pct_h10",
            (raw_trade_frame["category"] == "Bagging/Receiving of orders/contracts")
            & (raw_trade_frame["r0"] >= 0.02)
            & raw_trade_frame["exit_close_h10"].notna(),
            "exit_close_h10",
        ),
        (
            "H3_sast_buy_h20",
            (raw_trade_frame["sast_direction"] == "buy")
            & raw_trade_frame["exit_close_h20"].notna(),
            "exit_close_h20",
        ),
    ]

    for name, mask, exit_col in sims:
        sub = raw_trade_frame[mask]
        trades = []
        for row in sub.itertuples(index=False):
            entry_open = row.entry_open_t1
            exit_close = getattr(row, exit_col)
            t = _simulate_trade(entry_open, exit_close)
            if t is not None:
                t["half"] = row.half
                trades.append(t)
        tdf = pd.DataFrame(trades)
        print(f"\n--- {name} ---")
        if tdf.empty:
            print("n=0 (no computable trades)")
            continue
        print(
            f"n={len(tdf)} hit_rate={100 * (tdf['net_pnl'] > 0).mean():.1f}% "
            f"avg_net_ret_pct={tdf['net_ret_pct'].mean():.3f} "
            f"median_net_ret_pct={tdf['net_ret_pct'].median():.3f} "
            f"total_net_pnl={tdf['net_pnl'].sum():.2f}"
        )
        for half in ["A", "B"]:
            hsub = tdf[tdf["half"] == half]
            if hsub.empty:
                print(f"  half {half}: n=0")
                continue
            print(
                f"  half {half}: n={len(hsub)} "
                f"hit_rate={100 * (hsub['net_pnl'] > 0).mean():.1f}% "
                f"avg_net_ret_pct={hsub['net_ret_pct'].mean():.3f} "
                f"median_net_ret_pct={hsub['net_ret_pct'].median():.3f} "
                f"total_net_pnl={hsub['net_pnl'].sum():.2f}"
            )


def build_raw_trade_frame(
    events_grouped: pd.DataFrame,
    prices_by_symbol: dict[str, pd.DataFrame],
    event_drift: pd.DataFrame,
) -> pd.DataFrame:
    """Second pass over the same events, this time keeping RAW (non-market-
    adjusted) T+1-open and horizon-close prices so the 3 sims can cost an
    actual CNC trade. Joins onto event_drift on (symbol, eff_date, polarity,
    category) to reuse its already-computed r0/vol_confirm/sast_direction/
    half/skip_reason."""
    rows = []
    for ev in events_grouped.itertuples(index=False):
        symbol = ev.symbol
        eff_date = ev.eff_date
        symbol_series = prices_by_symbol.get(symbol)
        if symbol_series is None or symbol_series.empty:
            continue
        day0_pos = find_day0_position(symbol_series, eff_date)
        if day0_pos is None:
            continue
        n = len(symbol_series)
        t1_pos = day0_pos + 1
        entry_open_t1 = float(symbol_series.iloc[t1_pos]["open"]) if t1_pos < n else None
        pos_h10 = day0_pos + 10
        pos_h20 = day0_pos + 20
        exit_close_h10 = float(symbol_series.iloc[pos_h10]["close"]) if pos_h10 < n else None
        exit_close_h20 = float(symbol_series.iloc[pos_h20]["close"]) if pos_h20 < n else None
        rows.append(
            {
                "symbol": symbol,
                "eff_date": eff_date,
                "polarity": ev.polarity,
                "category": ev.category,
                "entry_open_t1": entry_open_t1,
                "exit_close_h10": exit_close_h10,
                "exit_close_h20": exit_close_h20,
            }
        )
    raw = pd.DataFrame(rows)
    if raw.empty:
        return raw
    merged = raw.merge(
        event_drift[
            [
                "symbol",
                "eff_date",
                "polarity",
                "category",
                "r0",
                "vol_confirm",
                "sast_direction",
                "half",
            ]
        ],
        on=["symbol", "eff_date", "polarity", "category"],
        how="left",
    )
    return merged


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="R45 news-event multi-horizon drift study")
    parser.add_argument("--announcements", default=DEFAULT_ANNOUNCEMENTS)
    parser.add_argument("--prices", default=DEFAULT_PRICES)
    parser.add_argument("--openalgo-db", default=DEFAULT_OPENALGO_DB)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--summary", action="store_true", default=False)
    args = parser.parse_args()

    run(
        announcements_path=args.announcements,
        prices_path=args.prices,
        openalgo_db_path=args.openalgo_db,
        out_path=args.out,
        print_summary=args.summary,
    )


if __name__ == "__main__":
    main()
