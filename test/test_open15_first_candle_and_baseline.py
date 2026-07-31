"""Regression tests for issue #502 — the three tick-sourcing bugs.

Each test is built to FAIL on the pre-fix tree, using the real numbers from the
2026-07-31 audit rather than synthetic shapes, so a future regression reproduces
the actual production failure and not merely an abstract one.

  Bug 1  the 09:15 "open" was the first tick RECEIVED, not the day's open.
         MPHASIS 2026-07-31: real open 2324.60 (gap -0.94%, rank #11); the feed's
         first tick landed at 09:15:08 already down at 2249.20 -> gap -4.15% and
         MPHASIS was watched as the #1 short, displacing TECHM (-2.94%).
  Bug 2  the tick-built first candle is NARROWER than the real one (high
         understated 24/24, low overstated 24/24), so the breakout level is
         easier to break than the real candle's extreme.
  Bug 3  the 09:15 minute (the day's busiest, plus pre-open auction volume
         carried in the tick cumulative) sat in the running volume baseline,
         inflating it 1.06x-1.67x and turning the configured 1.5x gate into
         ~2.5x. HYUNDAI 2026-07-31 09:17 is the worked example.
"""

import datetime as dt
import inspect

from services.open15_breakout_service import Open15Core


def t(h, m, s=0):
    return dt.datetime(2026, 7, 31, h, m, s)


def make_core(prev_closes, **kw):
    """Build a core, dropping kwargs the tree does not have yet.

    Deliberate: run against the PRE-FIX service and these tests must fail on the
    wrong selection / suppressed entry — the actual defect — not on a
    ``TypeError`` about a keyword that does not exist yet. A missing-symbol
    failure proves nothing about behaviour (repo convention, commit 611462055).
    """
    supported = inspect.signature(Open15Core.__init__).parameters
    return Open15Core(prev_closes, **{k: v for k, v in kw.items() if k in supported})


def apply_candles(core, candles):
    """Install broker first candles where supported; a no-op pre-fix."""
    fn = getattr(core, "apply_first_candles", None)
    if fn is not None:
        fn(candles)


def level_of(core, sym, side):
    """The breakout level the core would use, pre- or post-fix."""
    fn = getattr(core, "first_candle", None)
    fc = fn(sym) if fn is not None else core.sym[sym]["fc"]
    return fc["high"] if side == "L" else fc["low"]


# --------------------------------------------------------------------------- #
# Bug 1 — selection must use the broker's day open, not the first tick seen
# --------------------------------------------------------------------------- #
def test_late_first_tick_no_longer_manufactures_a_phantom_gap():
    """The MPHASIS 2026-07-31 incident, with the real numbers.

    MPHASIS's feed was 8 s late and its first tick was 3.24% below the real
    open. TECHM opened genuinely lower. Pre-fix the core ranks MPHASIS as the
    biggest loser; with the broker candle applied, TECHM wins the slot.
    """
    prev = {"MPHASIS": 2346.70, "TECHM": 1669.00, "HCLTECH": 1352.70, "INFY": 1155.10}
    core = make_core(prev, top_n=2, await_snapshot=True)

    # what the tick feed actually delivered (MPHASIS's first print is late/low)
    core.on_tick("MPHASIS", 2249.20, 14584, t(9, 15, 8))
    core.on_tick("TECHM", 1622.00, 50000, t(9, 15, 1))
    core.on_tick("HCLTECH", 1307.20, 22302, t(9, 15, 2))
    core.on_tick("INFY", 1118.00, 64151, t(9, 15, 1))

    # the broker's settled 09:15 candle
    apply_candles(
        core,
        {
            "MPHASIS": {"open": 2324.60, "high": 2324.60, "low": 2222.30},
            "TECHM": {"open": 1620.00, "high": 1626.00, "low": 1610.50},
            "HCLTECH": {"open": 1317.70, "high": 1317.70, "low": 1294.30},
            "INFY": {"open": 1118.90, "high": 1119.00, "low": 1109.00},
        },
    )
    core.on_tick("INFY", 1115.0, 300000, t(9, 16, 1))  # first 09:16 tick finalizes

    assert core.finalized
    # the defect assertion comes FIRST so a pre-fix run fails on the wrong
    # SELECTION, which is the bug, not on a provenance field
    shorts = [s for s, side in core.selected.items() if side == "S"]
    # pre-fix this is ["MPHASIS", "INFY"] — the phantom -4.15% wins a slot
    assert set(shorts) == {"INFY", "TECHM"}
    assert "MPHASIS" not in core.selected
    assert round(core.gaps["MPHASIS"] * 100, 2) == -0.94
    assert round(core.gaps["TECHM"] * 100, 2) == -2.94
    assert getattr(core, "first_candle_source", "ticks") == "quotes"


def test_snapshot_is_fail_open_per_symbol():
    """A symbol the snapshot missed keeps its tick-built candle rather than
    dropping out of the ranking entirely."""
    core = make_core({"AAA": 100.0, "BBB": 100.0}, top_n=1, await_snapshot=True)
    core.on_tick("AAA", 103.0, 1000, t(9, 15, 1))
    core.on_tick("BBB", 104.0, 1000, t(9, 15, 1))
    apply_candles(core, {"AAA": {"open": 101.0, "high": 101.5, "low": 100.5}})
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 1))
    # BBB keeps its tick open (104 -> +4%) and beats the corrected AAA (+1%)
    assert core.selected == {"BBB": "L"}
    assert level_of(core, "BBB", "L") == 104.0  # untouched tick-built candle


def test_selection_is_deferred_until_the_snapshot_lands():
    """Finalizing on the first 09:16 tick — before the broker candle arrives —
    is precisely bug 1, so the core must wait."""
    core = make_core({"AAA": 100.0}, top_n=1, await_snapshot=True)
    core.on_tick("AAA", 103.0, 1000, t(9, 15, 1))
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 1))
    assert not core.finalized  # deferred
    apply_candles(core, {"AAA": {"open": 101.0, "high": 101.5, "low": 100.5}})
    core.on_tick("AAA", 103.0, 1200, t(9, 16, 30))
    assert core.finalized
    assert round(core.gaps["AAA"] * 100, 2) == 1.0


def test_deferral_fails_open_past_the_deadline():
    """A snapshot that never arrives must not cost the day — past 09:16 the
    core finalizes on the tick-built candle."""
    core = make_core({"AAA": 100.0}, top_n=1, await_snapshot=True)
    core.on_tick("AAA", 103.0, 1000, t(9, 15, 1))
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 30))
    assert not core.finalized
    core.on_tick("AAA", 103.0, 1200, t(9, 17, 1))
    assert core.finalized
    assert getattr(core, "first_candle_source", "ticks") == "ticks"
    assert round(core.gaps["AAA"] * 100, 2) == 3.0


# --------------------------------------------------------------------------- #
# Bug 2 — the breakout level must be the real candle's extreme
# --------------------------------------------------------------------------- #
def test_level_comes_from_the_broker_candle_not_the_sampled_ticks():
    """HYUNDAI 2026-07-31: tick-built H1 2120.10 vs real H1 2123.00. A price of
    2121.00 is a breakout under the sampled candle and NOT under the real one."""
    core = make_core({"HYUNDAI": 2018.20}, vol_mult=1.5, top_n=1, await_snapshot=True)
    core.on_tick("HYUNDAI", 2113.00, 24290, t(9, 15, 2))
    core.on_tick("HYUNDAI", 2120.10, 156109, t(9, 15, 59))  # sampled high
    apply_candles(core, {"HYUNDAI": {"open": 2099.00, "high": 2123.00, "low": 2090.00}})
    core.on_tick("HYUNDAI", 2115.0, 240839, t(9, 16, 59))

    assert level_of(core, "HYUNDAI", "L") == 2123.00
    # 2121.00 clears the sampled 2120.10 but not the real 2123.00, and the
    # volume is a genuine surge — pre-fix this is an entry, post-fix it is not
    assert core.on_tick("HYUNDAI", 2121.00, 240839 + 200000, t(9, 17, 30)) is None
    assert core.watch_stats["HYUNDAI"]["level_broken"] is False
    # clearing the REAL high does fire
    action = core.on_tick("HYUNDAI", 2123.50, 240839 + 260000, t(9, 17, 40))
    assert action is not None and action["level"] == 2123.00


# --------------------------------------------------------------------------- #
# Bug 3 — the 09:15 minute must not inflate the volume baseline
# --------------------------------------------------------------------------- #
def test_baseline_excludes_the_first_minute_hyundai_20260731():
    """The worked example from the audit.

    HYUNDAI minute volumes: 09:15 = 156,109 (incl. 24,290 pre-open auction),
    09:16 = 84,730, 09:17 = 147,209.
      baseline WITH 09:15    = 120,420 -> needs 180,629, got 147,209 = 1.22x (no trade)
      baseline WITHOUT 09:15 =  84,730 -> needs 127,095            = 1.74x (fires)
    """
    prev = {"HYUNDAI": 2018.20}
    fc = {"HYUNDAI": {"open": 2099.00, "high": 2123.00, "low": 2090.00}}

    def run(include_first_minute):
        core = make_core(
            prev,
            vol_mult=1.5,
            top_n=1,
            await_snapshot=True,
            baseline_includes_first_minute=include_first_minute,
        )
        core.on_tick("HYUNDAI", 2113.00, 156109, t(9, 15, 2))
        apply_candles(core, fc)
        core.on_tick("HYUNDAI", 2115.00, 156109 + 84730, t(9, 16, 59))
        return core, core.on_tick("HYUNDAI", 2151.80, 156109 + 84730 + 147209, t(9, 17, 59))

    core_legacy, legacy = run(True)
    assert legacy is None  # the bug: a real surge suppressed
    assert round(core_legacy.watch_stats["HYUNDAI"]["max_vol_ratio"], 2) == 1.22

    core_fixed, fixed = run(False)
    assert fixed is not None
    assert fixed["baseline_vol"] == 84730
    assert round(fixed["cum_vol_at_trigger"] / fixed["baseline_vol"], 2) == 1.74


def test_auction_volume_leaves_the_baseline_with_the_first_minute():
    """Dropping 09:15 also drops the pre-open auction, because every later
    minute is a cumulative DIFFERENCE — so the baseline is auction-free."""
    core = make_core({"AAA": 100.0}, vol_mult=1.5, top_n=1, await_snapshot=True)
    # first tick already carries 500,000 of auction volume
    core.on_tick("AAA", 103.0, 500_000, t(9, 15, 1))
    core.on_tick("AAA", 103.0, 600_000, t(9, 15, 59))
    apply_candles(core, {"AAA": {"open": 103.0, "high": 103.5, "low": 102.5}})
    core.on_tick("AAA", 103.0, 610_000, t(9, 16, 59))  # 09:16 traded 10,000
    core.on_tick("AAA", 103.0, 615_000, t(9, 17, 59))  # 09:17 traded  5,000
    core.on_tick("AAA", 103.0, 615_001, t(9, 18, 1))
    assert core.sym["AAA"]["minute_vols"] == [10_000, 5_000]  # no 600,000 entry
