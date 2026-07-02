"""Unit tests for the ``fno_intraday_sell_chartink`` 10-gate Chartink SELL rule.

Mirror of ``test_fno_intraday_buy_chartink.py``. Each of the 10 active gates is
exercised in isolation (one pass + one fail), plus insufficient-warm-up
rejections, a golden full-pass scenario, NaN guards, and live-bar (pre/post
15:31 IST) alignment.

The SELL leg is a gap-DOWN setup: synthetic frames build a flat history then a
gapped-down last bar, a downtrend 5m tape (Supertrend line above price) and a
falling 15m tape (RSI < 50). Time is frozen to 16:00 IST (post-settle, -1/-2
indexing) by default.
"""

from datetime import date as _date
from datetime import datetime as _RealDateTime
from datetime import timedelta as _timedelta

import numpy as np
import pandas as pd
import pytest

import services.scan_rules.fno_intraday_sell_chartink as rulemod
from services.scan_rules.fno_intraday_sell_chartink import rule


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
    the daily bar via legacy Path A) legitimately diverges from the 5m close —
    an artifact of the test setup, not a production condition (in production
    Path B, today_d.close IS the live 5m close). The dedicated divergence-block
    tests below opt the flag back on explicitly.
    """
    _freeze(monkeypatch, 16, 0)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "false")


# --------------------------------------------------------------------------- #
# Synthetic frame builders (gap-DOWN setup)
# --------------------------------------------------------------------------- #
def make_daily_bars(
    n=30,
    flat_close=2000.0,
    today_close=1900.0,  # < flat*0.97 (1940) → 3% gap down
    today_open=1910.0,  # < yest close (2000) and < pivot (2000)
    today_vol=2000.0,  # > prev_vol → volume gate passes
    prev_vol=1000.0,
    yest_high=None,
    yest_low=None,
):
    """Daily frame: flat history at ``flat_close`` then a gap-DOWN bar last."""
    idx = pd.date_range("2024-01-01", periods=n, freq="D")
    close = [flat_close] * (n - 1) + [today_close]
    open_ = [flat_close] * (n - 1) + [today_open]
    high = [flat_close] * (n - 1) + [today_open + 10]
    low = [flat_close] * (n - 1) + [today_close - 10]
    # Shape the second-last (yesterday) bar so gate-9 and gate-10 thresholds differ.
    high[-2] = yest_high if yest_high is not None else flat_close
    low[-2] = yest_low if yest_low is not None else flat_close
    vol = [prev_vol] * (n - 1) + [today_vol]
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


def make_5m_bars(n=20, start_close=1950.0, step=-2.0):
    """5m frame: falling closes (Supertrend downtrend, line above price).

    Default level (~1950 down to ~1912) keeps the Supertrend line between
    today's close (1900) and yesterday's close (2000) so gates 3 and 4 pass.
    """
    idx = pd.date_range("2026-06-04 09:15", periods=n, freq="5min")
    close = [start_close + step * i for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 5 for c in close],
            "low": [c - 5 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        },
        index=idx,
    )


def make_15m_bars(n=20, start_close=2000.0, step=5.0, rising=False):
    """15m frame: monotone closes → RSI≈0 (falling, default) or ≈100 (rising)."""
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
    """All-10-gates-pass indicators bundle."""
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
    ind["bars_daily"] = make_daily_bars(n=2)  # < 3 rows
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
    ["g6", "g12", "g1", "g9", "g10", "gV", "g7", "g5", "g3"],
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


def _fail_g1():  # close only -1% (needs < -3%)
    return make_indicators(
        make_daily_bars(today_close=1980.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g9():  # open >= yest close
    return make_indicators(
        make_daily_bars(today_open=2010.0), make_weekly_bars(), make_5m_bars(), make_15m_bars()
    )


def _fail_g10():  # open above pivot but below yest close
    # pivot = (2000 + 1700 + 2000)/3 = 1900; open 1950 < yest_close 2000 (g9 ok)
    # but 1950 >= 1900 → g10 fails.
    return make_indicators(
        make_daily_bars(today_open=1950.0, yest_high=2000.0, yest_low=1700.0),
        make_weekly_bars(),
        make_5m_bars(),
        make_15m_bars(),
    )


def _fail_gV():  # daily volume <= prev volume
    return make_indicators(
        make_daily_bars(today_vol=500.0, prev_vol=1000.0),
        make_weekly_bars(),
        make_5m_bars(),
        make_15m_bars(),
    )


def _fail_g7():  # weekly ATR <= 5% * close
    return make_indicators(
        make_daily_bars(), make_weekly_bars(rng=20.0), make_5m_bars(), make_15m_bars()
    )


def _fail_g5():  # 15m RSI >= 50 (rising tape)
    return make_indicators(
        make_daily_bars(), make_weekly_bars(), make_5m_bars(), make_15m_bars(rising=True)
    )


def _fail_g3():  # 5m Supertrend line <= today close (uptrend below today's close)
    return make_indicators(
        make_daily_bars(),
        make_weekly_bars(),
        make_5m_bars(start_close=1870.0, step=2.0),
        make_15m_bars(),
    )


@pytest.mark.parametrize(
    "builder",
    [
        _fail_g6,
        _fail_g12,
        _fail_g1,
        _fail_g9,
        _fail_g10,
        _fail_gV,
        _fail_g7,
        _fail_g5,
        _fail_g3,
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
def test_nan_daily_volume_rejects():
    daily = make_daily_bars()
    daily.iloc[-2, daily.columns.get_loc("volume")] = np.nan  # yest volume NaN
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
# test above continues to pass. The two scenarios below cover the new
# behavior that production relies on:
# --------------------------------------------------------------------------- #
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
    bars and still fire on a genuine gap-down setup.

    Setup: ``bars_daily`` ends with a flat history at 2000 dated *yesterday*
    (no today row). Today's 5m frame is a steeper falling tape (start_close=
    1980, step=-4: closes 1980 → 1904) so the derived today_d has open=1980,
    close=1904 (4.8% drop vs yest=2000 — gap-1 passes), cumulative
    volume=20000 (> prev_vol=1000 — gate V passes), and the steeper drop
    keeps the 5m Supertrend line above the running close so gate 3 fires.
    """
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    # Drop the synthetic "today" row from make_daily_bars so the frame
    # ends at the flat history (representing yesterday's settled bar).
    daily = make_daily_bars(n=30, flat_close=2000.0)
    daily = daily.iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)

    bars_5m = _attach_5m_timestamps(make_5m_bars(start_close=1980.0, step=-4.0), today_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    assert rule(None, ind) is True


def test_rejects_when_no_today_5m_and_bars_daily_ends_yesterday(monkeypatch):
    """If ``bars_daily`` ends yesterday AND 5m bars are all from prior days
    (no today data), the rule cannot derive today_d — must reject loudly.
    """
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    daily = make_daily_bars(n=30, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)
    # 5m bars dated YESTERDAY → no today data.
    bars_5m = _attach_5m_timestamps(make_5m_bars(), yest_date)
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    assert rule(None, ind) is False


# --------------------------------------------------------------------------- #
# Env-var threshold override (CHARTINK_RULE_SELL_GAP_PCT, read at call time)
# --------------------------------------------------------------------------- #
def test_env_override_lowers_sell_gap(monkeypatch):
    # ~2% gap-down (yest 1939 → today 1900): fails the default 3% gate-1, but
    # passes once the env lowers the threshold to 1.5%. The read is inside rule(),
    # so no restart is needed for the change to take effect.
    daily = make_daily_bars(flat_close=1939.0, today_close=1900.0, today_open=1910.0)
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    monkeypatch.setenv("CHARTINK_RULE_SELL_GAP_PCT", "3.0")  # pin default vs ambient .env
    assert rule(None, ind) is False
    monkeypatch.setenv("CHARTINK_RULE_SELL_GAP_PCT", "1.5")
    assert rule(None, ind) is True


# --------------------------------------------------------------------------- #
# D-bar-date verify (settled-bar staleness guard)
#
# Issue #197 reframed this guard: the rule no longer treats ``iloc[-1]`` as
# "today's settled bar" during the session, so the original AUROPHARMA bug
# class (firing on stale-as-today data) is structurally impossible. The
# guard now defends against a more extreme failure: the LATEST SETTLED bar
# being more than ~5 calendar days behind today, which means the backfill
# has been broken across multiple sessions and even the "yesterday"
# reference can't be trusted.
# --------------------------------------------------------------------------- #
import pytz as _pytz  # noqa: E402

_IST_TZ = _pytz.timezone("Asia/Kolkata")


def _with_timestamps(daily, last_date):
    """Attach a ``timestamp`` column (epoch seconds, IST 09:15) so the last row
    is dated ``last_date`` and prior rows walk back one calendar day each."""
    daily = daily.reset_index(drop=True).copy()
    n = len(daily)
    ts = []
    for i in range(n):
        d = last_date - _timedelta(days=(n - 1 - i))
        ts.append(int(_IST_TZ.localize(_RealDateTime(d.year, d.month, d.day, 9, 15)).timestamp()))
    daily["timestamp"] = ts
    return daily


def test_rule_aborts_when_settled_bar_is_very_stale(monkeypatch, caplog):
    """When the latest settled D-bar is more than 5 calendar days behind today
    (backfill broken across multiple sessions), the guard aborts loudly."""
    import logging

    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    # last_date 10 days before frozen today=06-04 → >5 day staleness threshold.
    daily = _with_timestamps(make_daily_bars(), last_date=_date(2026, 5, 25))
    # bars_5m carries today's date so Branch 2 is taken in the helper.
    bars_5m = _attach_5m_timestamps(make_5m_bars(), _date(2026, 6, 4))
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "AUROPHARMA"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        assert rule(None, ind) is False
    assert any("STALE" in r.message and "AUROPHARMA" in r.message for r in caplog.records)


def test_rule_proceeds_when_daily_bar_is_today(monkeypatch):
    """When ``iloc[-1]`` is dated TODAY (broker-refreshed or synthetic frame),
    the helper takes Branch 1 — use iloc[-1] as today's bar, iloc[-2] as
    yesterday's settled. The full-pass setup fires."""
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    daily = _with_timestamps(make_daily_bars(), last_date=_date(2026, 6, 4))  # frozen today
    ind = make_indicators(daily, make_weekly_bars(), make_5m_bars(), make_15m_bars())
    assert rule(None, ind) is True


def test_rule_dbar_verify_disabled_allows_very_stale(monkeypatch):
    """With the flag off, even a very-stale settled bar is NOT aborted.

    The rule will not fire on this scenario anyway (the synthetic gap-down
    bar is now at the settled-yest position and today's 5m-derived running
    close is HIGHER than that gap-down close, so gate 1 reverses); this
    test only verifies the *abort path* is gated by the env flag.
    """
    import logging

    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "false")
    daily = _with_timestamps(make_daily_bars(), last_date=_date(2026, 5, 25))  # 10 days stale
    bars_5m = _attach_5m_timestamps(make_5m_bars(), _date(2026, 6, 4))
    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "AUROPHARMA"
    with caplog_context(logging.WARNING) as caplog:
        rule(None, ind)
    # No STALE warning is logged because the guard is disabled.
    assert not any("STALE" in r.message for r in caplog.records)


def test_rule_dbar_verify_skipped_without_timestamp_column(monkeypatch):
    """Frames lacking a ``timestamp`` column (synthetic) are exempt — proves the
    existing tests are unaffected."""
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    assert rule(None, happy()) is True


from contextlib import contextmanager  # noqa: E402


@contextmanager
def caplog_context(level):
    """Adapter so the assertion above can use ``with`` syntax without the
    pytest ``caplog`` fixture — keeps the test self-contained."""
    import logging

    class _Capture:
        records = []  # noqa: RUF012

        def handle(self, record):
            self.records.append(record)

    cap = _Capture()

    class _H(logging.Handler):
        def emit(self, record):
            cap.handle(record)

    logger = logging.getLogger("services.scan_rules.fno_intraday_sell_chartink")
    h = _H(level=level)
    logger.addHandler(h)
    prev = logger.level
    logger.setLevel(level)
    try:
        yield cap
    finally:
        logger.removeHandler(h)
        logger.setLevel(prev)


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
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        assert rule(None, ind) is False
    assert any(
        "bars_daily is None" in r.message and "RELIANCE" in r.message for r in caplog.records
    )


def test_rule_short_history_is_debug_not_warning(caplog):
    """A short-but-present (warm-up) frame rejects without a WARNING — only the
    'no data' condition is loud."""
    import logging

    ind = happy()
    ind["bars_15m"] = make_15m_bars(n=10)  # present but < 15 rows
    ind["symbol"] = "RELIANCE"
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        assert rule(None, ind) is False
    assert not any("rejecting" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Live `ts`-column frame: morning crash but REBOUNDED by now → NO sell
#
# The core bug fix. Historify's daily bar is a frozen ~09:45 morning-crash
# snapshot; the live tick tape has since recovered. With Path B reading the
# live `ts`-column 5m aggregate, today_d.close is the rebounded price so the
# gap-down gate no longer fires. Previously (Path B dead) the frozen crash
# close fired a spurious SELL (KPITTECH/TATAELXSI 2026-06-29 misfire).
# --------------------------------------------------------------------------- #
def _attach_5m_ts_datetime(bars_5m, today_date):
    """Attach a ``ts`` column of NAIVE IST datetimes (the live aggregator shape,
    what ``ScannerService._append_bar`` produces), dated ``today_date``."""
    bars_5m = bars_5m.reset_index(drop=True).copy()
    start = _RealDateTime(today_date.year, today_date.month, today_date.day, 9, 15)
    bars_5m["ts"] = [start + _timedelta(minutes=5 * i) for i in range(len(bars_5m))]
    return bars_5m


def test_rebounded_stock_does_not_fire_sell(monkeypatch):
    """Morning crash frozen in historify, but the LIVE 5m tape has rebounded
    above the gap-down threshold → the SELL rule must NOT fire (today_d.close
    reflects the live rebound via the `ts`-column Path B, not the frozen bar)."""
    _freeze(monkeypatch, 11, 0)
    today_date = _date(2026, 6, 4)
    yest_date = today_date - _timedelta(days=1)

    # Historify daily ends YESTERDAY at a flat 2000 (frozen — no today bar yet).
    daily = make_daily_bars(n=30, flat_close=2000.0).iloc[:-1].reset_index(drop=True)
    daily = _attach_timestamps(daily, yest_date)

    # Live 5m tape: opened crashing (1900) but has REBOUNDED to ~2010 by now,
    # well above yest close 2000 → gap-down gate-1 (close < 2000*0.97) fails.
    rebounded = make_5m_bars(n=20, start_close=1900.0, step=6.0)  # 1900 → 2014
    bars_5m = _attach_5m_ts_datetime(rebounded, today_date)
    assert float(bars_5m["close"].iloc[-1]) > 2000.0  # sanity: rebounded

    ind = make_indicators(daily, make_weekly_bars(), bars_5m, make_15m_bars())
    ind["symbol"] = "KPITTECH"
    assert rule(None, ind) is False


# --------------------------------------------------------------------------- #
# Divergence BLOCK — reject when the flag is on, only WARN when off
# --------------------------------------------------------------------------- #
def _divergent_bare_fixture():
    """A synthetic (Path A) fixture whose today_d.close (daily iloc[-1]) diverges
    from the latest 5m close by > the default 0.5% threshold. Passes all other
    SELL gates so the ONLY reason it can be rejected is the divergence block."""
    ind = happy()
    b5 = ind["bars_5m"].copy()
    # today_d.close = daily iloc[-1] close = 1900; push the last 5m close DOWN
    # (keeps the SELL downtrend + supertrend-above-price gate intact) far enough
    # to trip the 0.5% divergence threshold: 1870 is ~1.6% below 1900.
    b5.loc[b5.index[-1], "close"] = 1870.0
    ind["bars_5m"] = b5
    ind["symbol"] = "INFY"
    return ind


def test_divergence_block_rejects_when_flag_on(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true")

    ind = _divergent_bare_fixture()
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        assert rule(None, ind) is False
    assert any("REJECTING on divergence" in r.message for r in caplog.records)


def test_divergence_only_warns_when_block_flag_off(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "false")

    ind = _divergent_bare_fixture()
    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        result = rule(None, ind)
    # The divergence WARNING still fires, but the symbol is NOT rejected on
    # divergence grounds (it passes all real SELL gates in this fixture).
    assert any("diverges" in r.message for r in caplog.records)
    assert not any("REJECTING on divergence" in r.message for r in caplog.records)
    assert result is True


# --------------------------------------------------------------------------- #
# Reference certificate gate (issue #305)
# --------------------------------------------------------------------------- #
def test_reference_uncertified_rejects_and_alerts(monkeypatch):
    """indicators['reference_certified'] is False → early rejection + the
    scanner_reference CRIT divergence alert (SELL mirror of the 2026-07-02
    DELHIVERY stale-reference class)."""
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


def test_reference_certified_true_passes():
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
    """The rejection WARNING fires once per (symbol, IST-day); the rejection
    itself always holds on every re-fire."""
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

    with caplog.at_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink"):
        for _ in range(5):
            assert rule(None, ind) is False
    warns = [
        r
        for r in caplog.records
        if "NOT certified against broker prev-close" in r.message and "DELHIVERY" in r.message
    ]
    assert len(warns) == 1, f"expected 1 dedup'd warning, got {len(warns)}"
