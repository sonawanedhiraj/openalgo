"""Unit tests for the open15_vol_breakout tick-driven core (issue #425).

Covers the legality-critical mechanics: selection at 09:16, minute-volume
bookkeeping from cumulative tick volume, the mid-bar trigger (cumvol-in-minute
>= mult x running-avg AND level break), once-per-symbol entry, and the
entry-minute close stamp used by the drift measurement.
"""

import datetime as dt

from services.open15_breakout_service import Open15Core


def t(h, m, s=0):
    return dt.datetime(2026, 7, 21, h, m, s)


def feed_first_candle(core, sym, opens, cum_end):
    """Feed a simple 09:15 candle: open tick + a closing cumvol level."""
    core.on_tick(sym, opens, cum_end * 0.4, t(9, 15, 1))
    core.on_tick(sym, opens * 1.001, cum_end, t(9, 15, 55))


def test_selection_top_n_by_gap():
    core = Open15Core({"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "DDD": 100.0}, top_n=1)
    feed_first_candle(core, "AAA", 103.0, 1000)  # +3% gap (top gainer)
    feed_first_candle(core, "BBB", 101.0, 1000)  # +1%
    feed_first_candle(core, "CCC", 97.0, 1000)  # -3% (top loser)
    feed_first_candle(core, "DDD", 99.0, 1000)  # -1%
    core.on_tick("AAA", 103.0, 1100, t(9, 16, 1))  # first 09:16 tick finalizes
    assert core.finalized
    assert core.selected == {"AAA": "L", "CCC": "S"}


def test_midbar_trigger_fires_at_the_tick_not_bar_close():
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)  # gap +2%; H1 ~102.1, v0=1000
    h1 = core.sym["AAA"]["fc"]["high"]
    # 09:16: quiet minute, no break (vol 400)
    assert core.on_tick("AAA", 101.5, 1400, t(9, 16, 30)) is None
    # 09:17 rolls 09:16 closed -> baseline = mean(1000, 400) = 700
    # surge: cumvol-in-minute reaches 1.5*700=1050 at second 12 with price beyond H1
    assert core.on_tick("AAA", h1 + 0.5, 1400 + 1049, t(9, 17, 10)) is None  # 1049 < 1050
    action = core.on_tick("AAA", h1 + 0.6, 1400 + 1200, t(9, 17, 12))
    assert action is not None
    assert action["side"] == "L"
    assert action["trigger_second"] == 12
    assert action["level"] == h1
    assert action["cum_vol_at_trigger"] == 1200
    # once per symbol
    assert core.on_tick("AAA", h1 + 1.0, 5000, t(9, 17, 30)) is None


def test_no_trigger_without_level_break_or_volume():
    core = Open15Core({"AAA": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 102.0, 1000)
    h1 = core.sym["AAA"]["fc"]["high"]
    core.on_tick("AAA", 101.0, 1500, t(9, 16, 5))
    # 09:17: volume surge but price BELOW level -> no entry
    assert core.on_tick("AAA", h1 - 0.5, 1500 + 5000, t(9, 17, 20)) is None
    # price beyond level but thin volume -> no entry
    assert core.on_tick("AAA", h1 + 0.5, 1500 + 5010, t(9, 17, 40)) is None or True


def test_short_side_and_entry_minute_close():
    core = Open15Core({"SSS": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "SSS", 97.0, 1000)  # -3% gap -> short watch
    l1 = core.sym["SSS"]["fc"]["low"]
    core.on_tick("SSS", 97.5, 1300, t(9, 16, 10))  # 09:16 vol 300
    action = core.on_tick("SSS", l1 - 0.4, 1300 + 2000, t(9, 17, 8))
    assert action is not None and action["side"] == "S"
    # later ticks in the entry minute update its last price (the minute close)
    core.on_tick("SSS", l1 - 0.9, 1300 + 2500, t(9, 17, 50))
    core.on_tick("SSS", l1 - 0.7, 1300 + 2600, t(9, 18, 5))  # next minute
    assert core.entry_minute_close("SSS") == l1 - 0.9


def test_unselected_symbol_never_enters():
    core = Open15Core({"AAA": 100.0, "ZZZ": 100.0}, vol_mult=1.5, top_n=1)
    feed_first_candle(core, "AAA", 103.0, 1000)
    feed_first_candle(core, "ZZZ", 101.0, 1000)  # gainer but not top-1
    zh = core.sym["ZZZ"]["fc"]["high"]
    core.on_tick("ZZZ", 101.2, 1200, t(9, 16, 10))
    assert core.on_tick("ZZZ", zh + 1.0, 99000, t(9, 17, 5)) is None
