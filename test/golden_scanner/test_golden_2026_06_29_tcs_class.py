"""Golden incident replay — 2026-06-29 TCS-class frozen-boot-LTP false SELL
(issues #203/#278/#279, fixed by #204/#279 — this is a REGRESSION LOCK, not an
open bug).

Incident (see ``services/scan_rules/_today_running.py`` module docstring and
``services/scan_rules/fno_intraday_sell_chartink.py``'s divergence-guard
comments): on 2026-06-29 the in-house SELL screener fired 41 SELL hits, only
7 confirmed real. TCS was one of the false ones — it fired SELL while the
live LTP was actually **+0.41% UP** for the day. The root cause:
``ScannerHistoryProvider``'s ``bars_daily`` cache is refreshed once at boot
and then FROZEN for the rest of the session. TCS's daily bar happened to be
written from an early-morning (~09:45) snapshot that looked like a small
drop; the SELL rule trusted that frozen bar as "today's close" and fired a
SELL the moment every other gate lined up, even though TCS had since
recovered and gone positive on the live tape.

Two fixes bracket this and both are ALREADY ON DEV (this test is a
regression lock, not a golden-xfail):

* **#279** — ``derive_today_and_yest`` Path B: when the 5m frame carries a
  timestamp column (live ``ts`` OR historify ``timestamp``), TODAY's running
  daily snapshot (open/high/low/close/volume) is derived by AGGREGATING
  today's 5m bars rather than trusting a same-day-dated ``bars_daily`` row
  at face value. So a frozen ~09:45 boot snapshot is superseded by the live
  5m tape's actual last close the instant Path B engages.
* **#204/#278** — the SELL rule's divergence-block guard
  (``SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED``, default true) is defense-in-
  depth: even if Path B somehow failed to engage, a ``today_d.close`` that
  diverges >0.5% from the live 5m close is rejected outright.

This test reconstructs the TCS scenario using ONLY
``test/fixtures/frame_factory.py`` production-shaped frames — a historify
daily frame with a ``timestamp`` column whose LAST bar is dated TODAY and
carries a frozen, lower snapshot close, alongside a live 5m frame (``ts``
column) whose actual last close is higher (the +0.4% recovery). It asserts
the DESIRED (and, per #279/#204, ALREADY SHIPPED) behavior: SELL must NOT
fire. This should PASS on current dev — if it doesn't, that is a live
regression of #279/#204 and must be reported, not silently patched around.
"""

from __future__ import annotations

from datetime import date, datetime

import services.scan_rules.fno_intraday_sell_chartink as rulemod
from services.scan_rules.fno_intraday_sell_chartink import rule
from test.fixtures.frame_factory import (
    flat_closes,
    make_15m_frame,
    make_historify_daily_frame,
    make_live_5m_frame,
    make_weekly_frame,
    ramp_closes,
)

_TODAY = date(2026, 6, 29)
_YEST_CLOSE = 3500.0
_FROZEN_MORNING_CLOSE = 3450.0  # looks like a ~1.4% drop if trusted at face value
_LIVE_RECOVERED_CLOSE = _YEST_CLOSE * 1.004  # actual: +0.4% UP for the day


def _freeze(monkeypatch, hour, minute=0):
    """Pin ``rulemod.datetime.now(tz)`` to 2026-06-29 hour:minute IST — same
    pattern as ``test/test_fno_intraday_sell_chartink.py::_freeze``."""

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime(2026, 6, 29, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(rulemod, "datetime", _FrozenDateTime)


def _tcs_frozen_boot_ltp_frames():
    """Build the TCS 2026-06-29 frame bundle.

    - ``bars_daily``: 30 historify-shaped daily bars, flat at 3500, with the
      LAST bar dated TODAY (2026-06-29) carrying a frozen morning snapshot
      close of 3450 — modelling ``ScannerHistoryProvider``'s once-at-boot
      cache holding a stale intraday capture.
    - ``bars_5m``: live-shaped intraday tape on 2026-06-29 whose actual last
      close is 3514 (+0.4% vs yesterday's 3500) — the live recovery the
      frozen daily bar never saw.
    - ``bars_15m`` / ``bars_weekly``: filled in only so the frame clears the
      warm-up length checks; this incident is decided entirely by the
      today/yesterday-close comparison (Gate 1), which is directional and
      fails immediately once Path B supplies the live close, so the other
      gates' pass/fail values are not the point of this test.
    """
    daily_closes = flat_closes(29, _YEST_CLOSE) + [_FROZEN_MORNING_CLOSE]
    bars_daily = make_historify_daily_frame(daily_closes, end_date=_TODAY, volumes=2_000_000.0)

    n5 = 20
    closes5 = ramp_closes(n5, _YEST_CLOSE + 5.0, 0.5)
    closes5[-1] = _LIVE_RECOVERED_CLOSE  # pin the exact +0.4% incident value
    bars_5m = make_live_5m_frame(closes5, _TODAY, volumes=50_000.0)

    bars_15m = make_15m_frame(ramp_closes(20, 3480.0, -2.0), _TODAY)
    bars_weekly = make_weekly_frame(flat_closes(25, _YEST_CLOSE), end_date=date(2026, 6, 25))

    return {
        "bars_5m": bars_5m,
        "bars_15m": bars_15m,
        "bars_daily": bars_daily,
        "bars_weekly": bars_weekly,
        "symbol": "TCS",
    }


def test_tcs_2026_06_29_frozen_boot_ltp_must_not_fire_sell(monkeypatch):
    """Regression lock: a frozen same-day daily bar that LOOKS like a gap-down
    must not fire SELL once the live 5m tape shows the stock actually UP.
    Encodes the shipped #279 (Path B live-derivation) + #204 (divergence
    block) fix for the 2026-06-29 41-SELL false-positive incident.
    """
    _freeze(monkeypatch, 15, 10)  # matches the incident's ~15:10 IST firing window
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")

    ind = _tcs_frozen_boot_ltp_frames()

    # Sanity: Path B must actually engage and prefer the LIVE close over the
    # frozen daily snapshot — if this assertion fails, the fixture itself
    # drifted from the incident shape, independent of the rule's final call.
    from services.scan_rules._today_running import derive_today_and_yest

    today_d, yest_d, _yest_idx = derive_today_and_yest(
        ind["bars_daily"], ind["bars_5m"], rulemod.datetime.now(rulemod._IST)
    )
    assert today_d is not None and yest_d is not None
    assert today_d.close > yest_d.close  # live: UP for the day
    assert today_d.close != _FROZEN_MORNING_CLOSE  # NOT the frozen boot snapshot

    assert rule(None, ind) is False, (
        "SELL fired on a frozen same-day daily bar while the live tape showed "
        "the stock UP — this is the 2026-06-29 TCS-class false SELL incident "
        "(issues #203/#278/#279/#204). If this fails, #279/#204's fix has "
        "regressed on dev — do NOT silently re-mark this xfail; investigate "
        "services/scan_rules/_today_running.py Path B and the divergence-block "
        "guard in fno_intraday_sell_chartink.py."
    )
