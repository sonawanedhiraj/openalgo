"""Scanner rule cross-source contract — historify D vs live 5m must NEVER
silently disagree.

What this test exercises that existing tests do NOT
---------------------------------------------------
``test/test_today_running.py`` and the per-rule unit tests in
``test/test_fno_intraday_{buy,sell}_chartink.py`` already drive
``derive_today_and_yest`` against many synthetic frames. Every one of
those tests builds a SINGLE, internally-consistent world: bars_daily and
bars_5m describe the same price path. None of them asks:

  "What if bars_daily.iloc[-1] is a STALE snapshot from 14:28 IST
   showing a -2% drop, while bars_5m is the live aggregator updated
   by every tick and shows a +0.5% recovery?"

That divergence is exactly the 2026-06-29 15:10 IST production bug
(commit ``ed7bcc0f0``, PR #204): the ``ScannerHistoryProvider`` cached
``bars_daily`` at boot. TCS was up +0.41% intraday but the frozen daily
showed -2% from morning, so the SELL rule's ``today_d.close`` was the
frozen 14:28 snapshot. 41 false SELL fires that afternoon; only 7
confirmed real on broker re-check.

PR #204 introduced ``derive_today_and_yest`` precisely to defend against
this — it prefers the live 5m close over a historify-cached today bar.
These tests pin that contract: when the two sources DISAGREE, the live
5m source must win.

The synthetic-test-frame fall-through (Path A — daily has no
``timestamp`` column) is NOT what we test here. That branch is kept for
existing unit-test ergonomics, and it deliberately trusts ``iloc[-1]``
without 5m cross-check. The contract test seeds REAL ``timestamp``
columns on both frames so the production code path (Path B / Path C) is
exercised end-to-end.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from services.scan_rules._today_running import derive_today_and_yest

_IST = timezone(timedelta(hours=5, minutes=30))


def _ist_dt(y: int, m: int, d: int, hh: int = 9, mm: int = 15) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_IST)


def _epoch(dt_: datetime) -> int:
    return int(dt_.timestamp())


def _daily_frame(rows: list[tuple[datetime, float, float, float, float, int]]) -> pd.DataFrame:
    """Build a daily-bar DataFrame with the REAL ``timestamp`` column the
    production code branches on.

    Each tuple is ``(ist_dt, open, high, low, close, volume)``.
    """
    return pd.DataFrame(
        [
            {
                "timestamp": _epoch(ts),
                "open": o,
                "high": h,
                "low": lo,
                "close": c,
                "volume": v,
            }
            for ts, o, h, lo, c, v in rows
        ]
    )


def _5m_frame(rows: list[tuple[datetime, float, float, float, float, int]]) -> pd.DataFrame:
    """Build a 5m-bar DataFrame with the REAL ``timestamp`` column."""
    return _daily_frame(rows)


# ----------------------------------------------------------------------- #
# Contract A — stale historify, fresh 5m: 5m wins.
# ----------------------------------------------------------------------- #
def test_stale_historify_today_disagrees_with_live_5m_5m_wins():
    """The frozen-historify TCS bug. ``bars_daily.iloc[-1]`` carries today's
    date but its close is the stale 14:28 snapshot (3501.0). The live 5m
    aggregator has been updated by ticks since then and the latest 5m
    close is 3525.0 (+0.41% from prior close). ``derive_today_and_yest``
    MUST resolve ``today_d.close`` to 3525.0 (live), not 3501.0 (frozen).
    """
    now = _ist_dt(2026, 6, 29, 15, 10)
    today = now.date()

    # bars_daily — settled history ending YESTERDAY, plus today's STALE
    # frozen-historify snapshot. The latest row IS dated today (its
    # timestamp resolves to today_date in IST), but its close is what
    # historify cached at boot — 3501.0.
    bars_daily = _daily_frame(
        [
            (_ist_dt(2026, 6, 26, 15, 29), 3520.0, 3540.0, 3510.0, 3530.0, 1_000_000),
            (_ist_dt(2026, 6, 27, 15, 29), 3530.0, 3545.0, 3500.0, 3510.0, 1_100_000),
            (
                _ist_dt(today.year, today.month, today.day, 14, 28),
                3510.0,
                3520.0,
                3495.0,
                3501.0,
                850_000,
            ),
        ]
    )

    # bars_5m — live aggregator series for today, last close +0.41% from
    # yest_d.close (3510). The DISAGREEMENT: 5m says 3525.0; daily says 3501.0.
    bars_5m = _5m_frame(
        [
            (
                _ist_dt(today.year, today.month, today.day, 9, 15),
                3510.0,
                3515.0,
                3508.0,
                3512.0,
                50_000,
            ),
            (
                _ist_dt(today.year, today.month, today.day, 15, 5),
                3518.0,
                3527.0,
                3517.0,
                3525.0,
                65_000,
            ),
        ]
    )

    today_d, yest_d, yest_idx = derive_today_and_yest(bars_daily, bars_5m, now)

    assert today_d is not None
    assert yest_d is not None
    # The contract: today's close MUST come from the live 5m (last tick),
    # never from the frozen historify snapshot. 3525.0 == live; 3501.0 == bug.
    assert today_d.close == pytest.approx(3525.0), (
        f"derive_today_and_yest returned today_d.close={today_d.close!r} — "
        f"expected 3525.0 (live 5m last close); 3501.0 would mean the frozen "
        f"historify snapshot won and the 2026-06-29 TCS bug is back"
    )
    # yest_d is the latest SETTLED bar — historify's iloc[-2] when iloc[-1]
    # is today-dated (whether stale or not, by date alone).
    assert yest_d.close == pytest.approx(3510.0)
    assert yest_idx == -2


# ----------------------------------------------------------------------- #
# Contract B — symmetric: stale historify in the SELL direction.
# ----------------------------------------------------------------------- #
def test_stale_historify_disagrees_in_sell_direction_5m_wins():
    """Symmetric case. Frozen historify shows today's close UP (stale fresh-open
    bar). Live 5m shows it dropping. 5m must still win — the stale-source
    bug is direction-agnostic.
    """
    now = _ist_dt(2026, 6, 29, 15, 10)
    today = now.date()

    bars_daily = _daily_frame(
        [
            (_ist_dt(2026, 6, 26, 15, 29), 100.0, 102.0, 99.0, 101.0, 500_000),
            (_ist_dt(2026, 6, 27, 15, 29), 101.0, 103.0, 100.0, 102.5, 600_000),
            # Stale snapshot from 09:18 IST — close is the fresh-open print.
            (
                _ist_dt(today.year, today.month, today.day, 9, 18),
                103.0,
                103.5,
                102.8,
                103.2,
                50_000,
            ),
        ]
    )

    # Live 5m: the print has FADED to 99.8 by 15:05 (drop from 102.5).
    bars_5m = _5m_frame(
        [
            (
                _ist_dt(today.year, today.month, today.day, 9, 15),
                103.0,
                103.5,
                102.0,
                102.8,
                20_000,
            ),
            (_ist_dt(today.year, today.month, today.day, 15, 5), 100.5, 100.8, 99.5, 99.8, 30_000),
        ]
    )

    today_d, yest_d, _ = derive_today_and_yest(bars_daily, bars_5m, now)

    assert today_d is not None
    assert yest_d is not None
    assert today_d.close == pytest.approx(99.8), (
        f"today_d.close={today_d.close!r} — expected 99.8 (live 5m); "
        f"103.2 would mean the stale 09:18 historify snapshot won"
    )


# ----------------------------------------------------------------------- #
# Contract C — no live 5m today (overnight / pre-open): historify falls
# through cleanly. NOT silently treated as today.
# ----------------------------------------------------------------------- #
def test_no_today_5m_returns_none_when_historify_lacks_today_bar():
    """Pre-open / overnight: bars_5m has no today-dated bars AND historify
    ends at yesterday. The function must return (None, None, None) so the
    caller (rule) can reject the symbol loudly. The silent-stale-source
    bug class would be returning a tuple where today_d.close == yest_d.close.
    """
    now = _ist_dt(2026, 6, 29, 9, 0)  # Pre-open IST.
    bars_daily = _daily_frame(
        [
            (_ist_dt(2026, 6, 26, 15, 29), 100.0, 101.0, 99.0, 100.5, 500_000),
            (_ist_dt(2026, 6, 27, 15, 29), 100.5, 101.5, 99.5, 101.0, 600_000),
        ]
    )
    # bars_5m is FROM yesterday (pre-open today). The selector's today-subset
    # will be empty.
    bars_5m = _5m_frame(
        [
            (_ist_dt(2026, 6, 27, 14, 0), 100.5, 101.0, 100.0, 100.8, 10_000),
            (_ist_dt(2026, 6, 27, 14, 5), 100.8, 101.2, 100.6, 101.0, 12_000),
        ]
    )

    today_d, yest_d, yest_idx = derive_today_and_yest(bars_daily, bars_5m, now)

    # The contract: don't fabricate today from yesterday. Return None loud.
    assert (today_d, yest_d, yest_idx) == (None, None, None), (
        "derive_today_and_yest produced a non-None today snapshot from "
        "yesterday's data — silent-stale-source bug class"
    )


# ----------------------------------------------------------------------- #
# Contract D — agreement: when both sources align, the function still
# resolves cleanly. (Sanity guard — the divergence-handling path must not
# break the happy path.)
# ----------------------------------------------------------------------- #
def test_agreement_between_historify_and_5m_resolves_cleanly():
    """Happy path: historify has a today-dated bar AND live 5m agrees on the
    close. The function should resolve to that close — same number on both
    sides — and yest_d should be the prior settled bar.
    """
    now = _ist_dt(2026, 6, 29, 11, 0)
    today = now.date()

    bars_daily = _daily_frame(
        [
            (_ist_dt(2026, 6, 26, 15, 29), 100.0, 101.0, 99.0, 100.5, 500_000),
            (_ist_dt(2026, 6, 27, 15, 29), 100.5, 101.5, 99.5, 101.0, 600_000),
            (
                _ist_dt(today.year, today.month, today.day, 10, 55),
                101.0,
                102.5,
                100.9,
                102.0,
                700_000,
            ),
        ]
    )
    bars_5m = _5m_frame(
        [
            (
                _ist_dt(today.year, today.month, today.day, 9, 15),
                101.0,
                102.0,
                100.9,
                101.5,
                30_000,
            ),
            (
                _ist_dt(today.year, today.month, today.day, 10, 55),
                101.5,
                102.5,
                101.5,
                102.0,
                40_000,
            ),
        ]
    )

    today_d, yest_d, _ = derive_today_and_yest(bars_daily, bars_5m, now)

    assert today_d is not None and yest_d is not None
    # Both sources say 102.0; the resolved close is unambiguous.
    assert today_d.close == pytest.approx(102.0)
    # And yest_d is the prior settled bar (101.0), NOT a duplicate of today_d.
    assert yest_d.close == pytest.approx(101.0)
    assert today_d.close != yest_d.close, "yest_d collapsed onto today_d — duplicate-bar bug"
