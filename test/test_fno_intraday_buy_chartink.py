"""Unit tests for the ``fno_intraday_buy_chartink`` 12-gate Chartink BUY rule.

Each of the 12 gates is exercised in isolation (one pass + one fail), plus
insufficient-warm-up rejections, a golden full-pass scenario, NaN guards, and
live-bar (pre/post 15:31 IST) alignment. Synthetic daily/weekly/5m/15m frame
builders at the top keep each test ~10 lines.

Time is frozen by patching the ``datetime`` symbol inside the rule module so
the rule's ``datetime.now(_IST)`` returns a fixed instant. Default is 16:00 IST
(post-settle) so the rule uses the ``-1 / -2`` daily indexing — the most recent
daily bar is "today".
"""

from datetime import datetime as _RealDateTime

import numpy as np
import pandas as pd
import pytest

import services.scan_rules.fno_intraday_buy_chartink as rulemod
from services.scan_rules.fno_intraday_buy_chartink import rule


# --------------------------------------------------------------------------- #
# Time freezing
# --------------------------------------------------------------------------- #
def _freeze(monkeypatch, hour, minute=0):
    """Pin ``rulemod.datetime.now(tz)`` to 2026-06-04 hour:minute (tz-aware)."""

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            naive = _RealDateTime(2026, 6, 4, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(rulemod, "datetime", _FrozenDateTime)


@pytest.fixture(autouse=True)
def _default_post_close(monkeypatch):
    """All tests default to 16:00 IST (post-settle → -1/-2 indexing).

    Also disable the divergence BLOCK flag by default: the synthetic gate-logic
    fixtures build the daily and 5m frames independently, so today_d.close (from
    the daily bar via legacy Path A) can legitimately diverge from the 5m close
    — an artifact of the test setup, not a production condition (in production
    Path B, today_d.close IS the live 5m close). The dedicated divergence-block
    tests below opt the flag back on explicitly.
    """
    _freeze(monkeypatch, 16, 0)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "false")


# --------------------------------------------------------------------------- #
# Synthetic frame builders
# --------------------------------------------------------------------------- #
def make_daily_bars(
    n=210,
    flat_close=2000.0,
    today_close=2100.0,
    today_open=2050.0,
    today_vol=5000.0,
    old_vol=1000.0,
    recent_vol=1000.0,
    yest_high=None,
    yest_low=None,
):
    """Daily frame: flat history at ``flat_close`` then a gap-up bar last.

    Volume layout (so SMA(50) and SMA(200) can be steered independently):
    first ``n-50`` bars ``old_vol``, next 49 ``recent_vol``, last ``today_vol``.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = [flat_close] * (n - 1) + [today_close]
    open_ = [flat_close] * (n - 1) + [today_open]
    high = [flat_close] * (n - 1) + [today_close + 10]
    low = [flat_close] * (n - 1) + [today_open - 10]
    # Shape the second-last (yesterday) bar so gate-9 and gate-10 thresholds differ.
    high[-2] = yest_high if yest_high is not None else flat_close
    low[-2] = yest_low if yest_low is not None else flat_close
    vol = [old_vol] * (n - 50) + [recent_vol] * 49 + [today_vol]
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


def make_weekly_bars(n=25, close=2000.0, rng=100.0):
    """Weekly frame with constant high-low range ``2*rng`` → ATR≈``2*rng``."""
    idx = pd.date_range("2024-01-07", periods=n, freq="W")
    return pd.DataFrame(
        {
            "open": [close] * n,
            "high": [close + rng] * n,
            "low": [close - rng] * n,
            "close": [close] * n,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def make_5m_bars(n=20, start_close=2060.0, step=2.0, last_vol=1000.0, base_vol=100.0):
    """5m frame: gently rising closes (Supertrend uptrend) + a volume spike last."""
    idx = pd.date_range("2026-06-04 09:15", periods=n, freq="5min")
    close = [start_close + step * i for i in range(n)]
    vol = [base_vol] * (n - 1) + [last_vol]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": vol,
        },
        index=idx,
    )


def make_15m_bars(n=20, start_close=2000.0, step=5.0, rising=True):
    """15m frame: monotone closes → RSI≈100 (rising) or ≈0 (falling)."""
    idx = pd.date_range("2026-06-04 09:15", periods=n, freq="15min")
    close = [start_close + step * i * (1 if rising else -1) for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 2 for c in close],
            "low": [c - 2 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def make_indicators(daily, weekly, b5m, b15m):
    return {
        "bars_5m": b5m,
        "bars_15m": b15m,
        "bars_daily": daily,
        "bars_weekly": weekly,
        "ema_20": None,  # backward-compat key the rule does not read
    }


def happy():
    """All-12-gates-pass indicators bundle."""
    return make_indicators(make_daily_bars(), make_weekly_bars(), make_5m_bars(), make_15m_bars())


# --------------------------------------------------------------------------- #
# Insufficient warm-up
# --------------------------------------------------------------------------- #
def test_warmup_daily_none():
    ind = happy()
    ind["bars_daily"] = None
    assert rule(None, ind) is False


def test_warmup_daily_short():
    ind = happy()
    ind["bars_daily"] = make_daily_bars(n=199)
    assert rule(None, ind) is False


def test_warmup_weekly_short():
    ind = happy()
    ind["bars_weekly"] = make_weekly_bars(n=21)
    assert rule(None, ind) is False


def test_warmup_15m_short():
    ind = happy()
    ind["bars_15m"] = make_15m_bars(n=14)
    assert rule(None, ind) is False


# --------------------------------------------------------------------------- #
# Per-gate pass cases (all gates satisfied → True)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "gate",
    ["g6", "g12", "g1", "g9", "g10", "g2", "g8", "g7", "g5", "g3", "g4"],
)
def test_gate_passes(gate):
    assert rule(None, happy()) is True


# --------------------------------------------------------------------------- #
# Per-gate fail cases (exactly one gate broken → False)
# --------------------------------------------------------------------------- #
def _fail_g6():  # daily close <= 100
    return make_indicators(
        make_daily_bars(today_close=50.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g12():  # daily close >= 5000
    return make_indicators(
        make_daily_bars(today_close=6000.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g1():  # close only +1% (needs > +3%)
    return make_indicators(
        make_daily_bars(today_close=2020.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g9():  # open <= yest close
    return make_indicators(
        make_daily_bars(today_open=1990.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g10():  # open below pivot but above yest close
    return make_indicators(
        make_daily_bars(today_open=2050.0, yest_high=2300.0, yest_low=2000.0),
        make_weekly_bars(),
        make_5m_bars(),
        make_15m_bars(),
    )


def _fail_g2():  # vol below SMA(50) but above SMA(200) (old_vol low)
    return make_indicators(
        make_daily_bars(today_vol=900.0, old_vol=500.0, recent_vol=1000.0),
        make_weekly_bars(),
        make_5m_bars(),
        make_15m_bars(),
    )


def _fail_g8():  # vol above SMA(50) but below SMA(200) (old_vol high)
    return make_indicators(
        make_daily_bars(today_vol=1500.0, old_vol=10000.0, recent_vol=1000.0),
        make_weekly_bars(),
        make_5m_bars(),
        make_15m_bars(),
    )


def _fail_g7():  # weekly ATR <= 5% * close
    return make_indicators(
        make_daily_bars(), make_weekly_bars(rng=20.0), make_5m_bars(), make_15m_bars()
    )


def _fail_g5():  # 15m RSI <= 50
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(), make_15m_bars(rising=False)
    )


def _fail_g3():  # 5m Supertrend line >= today close (prices shifted up)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(start_close=2200.0), make_15m_bars()
    )


def _fail_g4():  # 5m prior Supertrend line < yest close (prices shifted down)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(start_close=1900.0), make_15m_bars()
    )


@pytest.mark.parametrize(
    "builder",
    [
        _fail_g6,
        _fail_g12,
        _fail_g1,
        _fail_g9,
        _fail_g10,
        _fail_g2,
        _fail_g8,
        _fail_g7,
        _fail_g5,
        _fail_g3,
        _fail_g4,
    ],
)
def test_gate_fails(builder):
    assert rule(None, builder()) is False


# --------------------------------------------------------------------------- #
# Golden full-pass
# --------------------------------------------------------------------------- #
def test_golden_full_pass():
    assert rule(None, happy()) is True


# --------------------------------------------------------------------------- #
# NaN guards (NaN indicator must reject, never silently pass)
# --------------------------------------------------------------------------- #
def test_nan_daily_sma_rejects():
    daily = make_daily_bars()
    daily.iloc[-5, daily.columns.get_loc("volume")] = np.nan  # taints SMA(50)/(200)
    assert (
        rule(None, make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars()))
        is False
    )


def test_nan_15m_rsi_rejects():
    # Flat closes → zero gains / zero losses → RSI = 0/0 = NaN (not a silent pass).
    b15 = make_15m_bars(step=0.0)
    assert (
        rule(None, make_indicators(make_daily_bars(), make_weekly_bars(), make_5m_bars(), b15))
        is False
    )


# --------------------------------------------------------------------------- #
# Today-running-daily snapshot derivation (Issue #197)
#
# The rule no longer assumes ``bars_daily.iloc[-1]`` is today's forming bar;
# it resolves today/yesterday via the shared ``derive_today_and_yest`` helper.
# Synthetic test frames (no ``timestamp`` column) still get the legacy
# behavior — iloc[-1] is treated as today, iloc[-2] as yesterday — so every
# test above continues to pass.
# --------------------------------------------------------------------------- #
from datetime import date as _date  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402


def _attach_timestamps(daily, last_date):
    """Attach a ``timestamp`` column so the LAST bar is dated ``last_date``
    IST (09:15) and earlier rows step back one calendar day each."""
    import pytz as _pytz

    ist = _pytz.timezone("Asia/Kolkata")
    daily = daily.reset_index(drop=True).copy()
    n = len(daily)
    daily["timestamp"] = [
        int(
            ist.localize(
                _RealDateTime(
                    (last_date - _timedelta(days=(n - 1 - i))).year,
                    (last_date - _timedelta(days=(n - 1 - i))).month,
                    (last_date - _timedelta(days=(n - 1 - i))).day,
                    9,
                    15,
                )
            ).timestamp()
        )
        for i in range(n)
    ]
    return daily


def _attach_5m_timestamps(bars_5m, today_date):
    """Attach a ``timestamp`` column so every 5m bar is dated ``today_date``
    (sequential 5-min increments from 09:15 IST)."""
    import pytz as _pytz

    ist = _pytz.timezone("Asia/Kolkata")
    bars_5m = bars_5m.reset_index(drop=True).copy()
    start = ist.localize(_RealDateTime(today_date.year, today_date.month, today_date.day, 9, 15))
    bars_5m["timestamp"] = [
        int((start + _timedelta(minutes=5 * i)).timestamp()) for i in range(len(bars_5m))
    ]
    return bars_5m


def test_today_derived_from_5m_when_bars_daily_ends_yesterday(monkeypatch):
    """Production path: ``bars_daily`` is stale (latest bar is yesterday).
    The rule must derive today's running daily snapshot from today's 5m
    bars and still fire on a genuine gap-up setup.

    Setup: ``bars_daily`` ends with the flat history at 2000 dated yesterday.
    Today's 5m frame is a steeper rising tape (start_close=2020, step=4:
    closes 2020 → 2096) so the derived today_d has open=2020,
    close=2096 (4.8% gap up vs yest=2000), volume=20000 (> prev_vol's SMA),
    and the steeper rise keeps the 5m Supertrend below the running close so
    gate 3 fires.
    """
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    # Drop the synthetic "today" gap-up row so iloc[-1] is the flat history.
    # The BUY rule needs 200+ rows for the SMA(volume, 200) gate — use n=205.
    daily = make_daily_bars(n=205, flat_close=2000.0)
    daily = daily.iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)

    bars_5m = _attach_5m_timestamps(make_5m_bars(start_close=2020.0, step=4.0), today_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    assert rule(None, ind) is True


def test_rejects_when_no_today_5m_and_bars_daily_ends_yesterday(monkeypatch):
    """If ``bars_daily`` ends yesterday AND 5m bars are all from prior days
    (no today data), the rule cannot derive today_d — must reject loudly.
    """
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    daily = make_daily_bars(n=205, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)
    bars_5m = _attach_5m_timestamps(make_5m_bars(), yest_date)  # YESTERDAY
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    assert rule(None, ind) is False


# --------------------------------------------------------------------------- #
# Env-var threshold override (CHARTINK_RULE_BUY_GAP_PCT, read at call time)
# --------------------------------------------------------------------------- #
def test_env_override_lowers_buy_gap(monkeypatch):
    # ~2% gap-up (yest 2040 → today 2080): fails the default 3% gate-1, but passes
    # once the env lowers the threshold to 1.5%. The read is inside rule(), so no
    # restart is needed for the change to take effect.
    daily = make_daily_bars(flat_close=2040.0, today_close=2080.0, today_open=2060.0)
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    monkeypatch.setenv("CHARTINK_RULE_BUY_GAP_PCT", "3.0")  # pin default vs ambient .env
    assert rule(None, ind) is False
    monkeypatch.setenv("CHARTINK_RULE_BUY_GAP_PCT", "1.5")
    assert rule(None, ind) is True


# --------------------------------------------------------------------------- #
# D-bar-date verify — settled-bar staleness guard (Issue #197 reframing)
#
# The original AUROPHARMA bug class (firing on stale-as-today data) is
# structurally impossible in the new design — the rule no longer trusts
# ``iloc[-1]`` as today when its date doesn't match. The guard now defends
# against the LATEST SETTLED bar being more than ~5 calendar days behind
# today (backfill broken across multiple sessions).
# --------------------------------------------------------------------------- #
import pytz as _pytz  # noqa: E402

_IST_TZ = _pytz.timezone("Asia/Kolkata")


# Legacy alias kept for tests that still use `_with_timestamps`.
_with_timestamps = _attach_timestamps


def test_rule_aborts_when_settled_bar_is_very_stale(monkeypatch, caplog):
    """When the latest settled D-bar is more than 5 calendar days behind today,
    the guard aborts loudly."""
    import logging

    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    daily = _attach_timestamps(make_daily_bars(n=205), last_date=_date(2026, 5, 25))
    bars_5m = _attach_5m_timestamps(make_5m_bars(), _date(2026, 6, 4))
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "AUROPHARMA"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        assert rule(None, ind) is False
    assert any("STALE" in r.message and "AUROPHARMA" in r.message for r in caplog.records)


def test_rule_proceeds_when_daily_bar_is_today(monkeypatch):
    """When ``iloc[-1]`` is dated TODAY, the helper takes Branch 1 — use
    iloc[-1] as today's bar, iloc[-2] as yesterday's settled. The full-pass
    setup fires."""
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    daily = _attach_timestamps(make_daily_bars(), last_date=_date(2026, 6, 4))
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    assert rule(None, ind) is True


def test_rule_dbar_verify_disabled_allows_very_stale(monkeypatch, caplog):
    """With the flag off, even a very-stale settled bar is NOT aborted (no
    STALE warning). The rule may not fire on this scenario for other
    reasons; this test only verifies the *abort path* is gated by the flag."""
    import logging

    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "false")
    daily = _attach_timestamps(make_daily_bars(n=205), last_date=_date(2026, 5, 25))
    bars_5m = _attach_5m_timestamps(make_5m_bars(), _date(2026, 6, 4))
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "AUROPHARMA"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        rule(None, ind)
    assert not any("STALE" in r.message for r in caplog.records)


def test_rule_dbar_verify_skipped_without_timestamp_column(monkeypatch):
    """Frames lacking a ``timestamp`` column (synthetic) are exempt — the guard
    only fires where there's a real timestamp to check (proves existing tests
    are unaffected)."""
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    assert rule(None, happy()) is True


# --------------------------------------------------------------------------- #
# Tier-1 Fix #2 — loud missing-input logging
# --------------------------------------------------------------------------- #
def test_rule_logs_warning_when_input_is_none(caplog):
    """A ``None`` daily frame is rejected with a WARNING naming the symbol +
    reason, not the old silent ``return False``."""
    import logging

    ind = happy()
    ind["bars_daily"] = None
    ind["symbol"] = "RELIANCE"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        assert rule(None, ind) is False
    assert any(
        "bars_daily is None" in r.message and "RELIANCE" in r.message for r in caplog.records
    )


def test_rule_short_history_is_debug_not_warning(caplog):
    """A short-but-present (warm-up) frame rejects without a WARNING — only the
    'no data' condition is loud."""
    import logging

    ind = happy()
    ind["bars_daily"] = make_daily_bars(n=50)  # present but < 200 rows
    ind["symbol"] = "RELIANCE"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        assert rule(None, ind) is False
    assert not any("rejecting" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Live `ts`-column frame: full-day live volume clears the gates
#
# The core bug fix. Historify's daily bar is frozen at ~09:45 with PARTIAL
# volume that never clears SMA(50)/SMA(200) → 0 buy hits all day (the observed
# empty-BUY class). With Path B reading the live `ts`-column 5m aggregate, the
# FULL running-day volume + live gap-up close clears the gates and fires.
# --------------------------------------------------------------------------- #
def _attach_5m_ts_datetime(bars_5m, today_date):
    """Attach a ``ts`` column of NAIVE IST datetimes (the live aggregator shape,
    what ``ScannerService._append_bar`` produces), dated ``today_date``."""
    bars_5m = bars_5m.reset_index(drop=True).copy()
    start = _RealDateTime(today_date.year, today_date.month, today_date.day, 9, 15)
    bars_5m["ts"] = [start + _timedelta(minutes=5 * i) for i in range(len(bars_5m))]
    return bars_5m


def test_live_volume_clears_gates_via_ts_column(monkeypatch):
    """Historify daily ends YESTERDAY (frozen — no today bar). The live
    `ts`-column 5m tape gaps up AND its aggregated full-day volume exceeds the
    prior SMA(50)/SMA(200) reference → the BUY rule fires. Previously the dead
    Path B fell to a frozen historify bar and never cleared the volume gates."""
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    # Historify daily: flat 2000 history, ends yesterday. Prior volume SMA ~1000.
    daily = make_daily_bars(n=205, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)

    # Live 5m gap-up tape (2020 → 2096, +4.8% over yest 2000) with per-bar
    # volume 500 → 20 bars sum to 10,000 >> the ~1000 daily SMA reference.
    live = make_5m_bars(n=20, start_close=2020.0, step=4.0, last_vol=500.0, base_vol=500.0)
    bars_5m = _attach_5m_ts_datetime(live, today_date)

    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "GLENMARK"
    assert rule(None, ind) is True


# --------------------------------------------------------------------------- #
# Divergence BLOCK — reject when the flag is on, only WARN when off
# --------------------------------------------------------------------------- #
def _divergent_bare_fixture():
    """A synthetic (Path A) BUY fixture whose today_d.close (daily iloc[-1]
    close) diverges from the latest 5m close by > the default 0.5% threshold,
    while all other gates pass — so the ONLY rejection cause is the block."""
    ind = happy()
    b5 = ind["bars_5m"].copy()
    # today_d.close = daily iloc[-1] close = 2100; push the last 5m close far
    # enough to trip the 0.5% threshold WITHOUT breaking gate 3 (st < close).
    b5.loc[b5.index[-1], "close"] = 2085.0  # ~0.7% below 2100
    ind["bars_5m"] = b5
    ind["symbol"] = "INFY"
    return ind


def test_divergence_block_rejects_when_flag_on(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true")

    ind = _divergent_bare_fixture()
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        assert rule(None, ind) is False
    assert any("REJECTING on divergence" in r.message for r in caplog.records)


def test_divergence_only_warns_when_block_flag_off(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "false")

    ind = _divergent_bare_fixture()
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        result = rule(None, ind)
    assert any("diverges" in r.message for r in caplog.records)
    assert not any("REJECTING on divergence" in r.message for r in caplog.records)
    assert result is True


# --------------------------------------------------------------------------- #
# Issue #280 — SMA(200) daily-depth warm-up is OBSERVABLE, not silent, and the
# real live-aggregator frame shape (naive-datetime ``ts`` 5m column) is covered.
#
# #279 fixed derive_today_and_yest's Path B for the live ``ts`` column, but its
# tests used the historify epoch ``timestamp`` column on the 5m frame. The real
# in-process aggregator frame (ScannerService._append_bar) carries a NAIVE IST
# ``datetime`` ``ts`` column. And the actual 0-BUY-all-session cause was upstream
# of Path B entirely: the scanner's stored historify-D is short (<200 rows) for
# the F&O universe, so the BUY rule bailed at the SMA(vol_sma_l) daily warm-up —
# silently, at DEBUG — indistinguishable from a quiet market.
# --------------------------------------------------------------------------- #
def _attach_5m_ts_naive(bars_5m, today_date):
    """Attach a NAIVE-datetime ``ts`` column dated ``today_date`` — EXACTLY the
    shape ``ScannerService._append_bar`` produces from ``bar.get("ts")`` (a naive
    IST ``datetime`` bucket). Distinct from ``_attach_5m_timestamps`` which
    attaches the historify epoch ``timestamp`` column."""
    bars_5m = bars_5m.reset_index(drop=True).copy()
    start = _RealDateTime(today_date.year, today_date.month, today_date.day, 9, 15)
    bars_5m["ts"] = [start + _timedelta(minutes=5 * i) for i in range(len(bars_5m))]
    return bars_5m


def test_buy_short_daily_warns_and_rejects_with_live_ts_frame(monkeypatch, caplog):
    """Issue #280 core: a live aggregator 5m frame (naive ``ts``) present, but a
    SHORT daily (<200 rows) → the rule REJECTS and emits a dedup'd WARNING naming
    the SMA-depth gap. Previously this was a silent DEBUG (0 BUY, invisible)."""
    import logging

    _freeze(monkeypatch, 11, 0)
    monkeypatch.setattr(rulemod, "_shallow_daily_warned", set())  # reset dedup
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    # 150 daily rows (< SMA(200)); latest bar dated yesterday (production case).
    daily = make_daily_bars(n=150, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)
    bars_5m = _attach_5m_ts_naive(make_5m_bars(start_close=2020.0, step=4.0), today_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "ETERNAL"

    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        assert rule(None, ind) is False
    msgs = [r.message for r in caplog.records]
    assert any("ETERNAL" in m and "too short for SMA(200)" in m for m in msgs), msgs


def test_buy_short_daily_warning_is_deduped_per_symbol_day(monkeypatch, caplog):
    """The short-daily WARNING fires at most once per (symbol, day) — the rule
    re-fires every 5m bar close, so a bare WARNING would flood the log."""
    import logging

    _freeze(monkeypatch, 11, 0)
    monkeypatch.setattr(rulemod, "_shallow_daily_warned", set())
    today_date = _date(2026, 6, 4)
    daily = make_daily_bars(n=120).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, today_date - _timedelta(days=1))
    bars_5m = _attach_5m_ts_naive(make_5m_bars(), today_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "TCS"

    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        for _ in range(5):  # five successive scan cycles
            assert rule(None, ind) is False
    warns = [
        r for r in caplog.records if "too short for SMA(200)" in r.message and "TCS" in r.message
    ]
    assert len(warns) == 1, f"expected 1 dedup'd warning, got {len(warns)}"


def test_buy_full_daily_with_live_ts_frame_engages_path_b_and_fires(monkeypatch):
    """With a FULL daily (>=200 rows) AND a live naive-``ts`` 5m frame, Path B
    engages (today_d = live 5m aggregate) and a genuine gap-up setup fires — the
    exact production shape #279's fix targets, now on the real ``ts`` frame."""
    _freeze(monkeypatch, 11, 0)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true")  # block ON: must NOT reject
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    daily = make_daily_bars(n=205, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)
    # Rising tape: today_d.close is the last 5m close, so divergence == 0 → no block.
    bars_5m = _attach_5m_ts_naive(make_5m_bars(start_close=2020.0, step=4.0), today_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "ETERNAL"
    assert rule(None, ind) is True


# --------------------------------------------------------------------------- #
# Reference certificate gate (issue #305)
# --------------------------------------------------------------------------- #
def test_reference_uncertified_rejects_and_alerts(monkeypatch):
    """indicators['reference_certified'] is False → early rejection + the
    scanner_reference CRIT divergence alert (the 2026-07-02 DELHIVERY
    42x-false-BUY regression)."""
    import services.source_divergence_alerts as sda

    calls = []
    monkeypatch.setattr(sda, "check_and_alert", lambda **kw: calls.append(kw) or True)
    rulemod._uncertified_warned.clear()

    ind = happy()
    ind["symbol"] = "DELHIVERY"
    ind["reference_certified"] = False
    ind["reference_settled_close"] = 475.4
    ind["reference_broker_prev_close"] = 510.0
    ind["reference_divergence_pct"] = 6.78

    assert rule(None, ind) is False
    assert len(calls) == 1
    assert calls[0]["service"] == "scanner_reference"
    assert calls[0]["symbol"] == "DELHIVERY"
    assert calls[0]["source_a_value"] == 475.4
    assert calls[0]["source_b_value"] == 510.0


def test_reference_certified_true_passes(monkeypatch):
    """An explicitly-certified reference does not block a valid setup."""
    ind = happy()
    ind["reference_certified"] = True
    assert rule(None, ind) is True


def test_reference_missing_key_treated_as_certified():
    """Backward compat: an indicators dict WITHOUT the reference keys (unit
    tests / other callers) evaluates exactly as before."""
    ind = happy()
    assert "reference_certified" not in ind
    assert rule(None, ind) is True


def test_reference_uncertified_warning_deduped_per_symbol_day(monkeypatch, caplog):
    """The rejection WARNING fires once per (symbol, IST-day) even though the
    rule re-fires every 5m bar close; the rejection itself always holds."""
    import logging

    import services.source_divergence_alerts as sda

    monkeypatch.setattr(sda, "check_and_alert", lambda **kw: True)
    rulemod._uncertified_warned.clear()

    ind = happy()
    ind["symbol"] = "DELHIVERY"
    ind["reference_certified"] = False
    ind["reference_settled_close"] = 475.4
    ind["reference_broker_prev_close"] = 510.0
    ind["reference_divergence_pct"] = 6.78

    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink"):
        for _ in range(5):
            assert rule(None, ind) is False
    warns = [
        r
        for r in caplog.records
        if "NOT certified against broker prev-close" in r.message and "DELHIVERY" in r.message
    ]
    assert len(warns) == 1, f"expected 1 dedup'd warning, got {len(warns)}"


def test_reference_fallback_consults_registry_when_no_verdict(monkeypatch):
    """Option (a) of issue #305: with NO reference keys in the indicators dict
    (direct rule callers), the rule consults the broker prev-close registry
    itself and rejects on a confirmed divergence."""
    import services.scanner_reference_data as refdata
    import services.source_divergence_alerts as sda

    calls = []
    monkeypatch.setattr(sda, "check_and_alert", lambda **kw: calls.append(kw) or True)
    rulemod._uncertified_warned.clear()
    refdata.reset_for_tests()
    try:
        # happy() yest_d.close is 2000.0; a broker prev-close of 2100 → ~4.76%
        # divergence > 1.0% default → reject.
        refdata.record_broker_prev_close("DELHIVERY", 2100.0)
        ind = happy()
        ind["symbol"] = "DELHIVERY"
        assert "reference_certified" not in ind
        assert rule(None, ind) is False
        assert len(calls) == 1
        assert calls[0]["service"] == "scanner_reference"
        assert calls[0]["source_a_value"] == 2000.0
        assert calls[0]["source_b_value"] == 2100.0
    finally:
        refdata.reset_for_tests()


def test_reference_fallback_fail_open_when_registry_empty():
    """No broker prev-close recorded → the fallback is a no-op (fail-open on
    the missing cross-check) and a valid setup still fires."""
    import services.scanner_reference_data as refdata

    refdata.reset_for_tests()
    ind = happy()
    ind["symbol"] = "DELHIVERY"
    assert rule(None, ind) is True
