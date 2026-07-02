"""Golden incident replay — 2026-07-02 DELHIVERY false BUY (issues #299/#305).

Incident (see ``CLAUDE.md`` "Daily-D re-settle (issue #299, 2026-07-02)" and
issue #305's body): DELHIVERY fired BUY 42 times on 2026-07-02 while its
Chartink-mirror BUY rule's reference (``yest_d_close``) was a STALE,
never-re-settled provisional daily-D close. The historify daily bar for
2026-06-30 was written intraday as a running value and stored as **475.4**;
the broker-settled close was actually much higher. The rule read that stale
475.4 as "yesterday's close" and computed a spurious gap-up against a live
price that was, in truth, only modestly changed — a phantom BUY. The
production PASS-log snapshot for the incident (from ``get_last_eval_snapshot``
via the #205 gate-value stash) recorded:

    today_d_close=505.8  yest_d_close=475.4  today_d_open=507.7
    pivot=474.6  rsi_15m=82  (volume/ATR gates also cleared)

This test reconstructs exactly that snapshot using ONLY
``test/fixtures/frame_factory.py`` production-shaped frames (historify daily
with a real ``timestamp`` column, live 5m with a real ``ts`` column) — never
the bare no-timestamp synthetic frames the original gate-logic unit tests
use. It freezes the rule module's ``datetime`` the same way
``test/test_fno_intraday_buy_chartink.py`` does.

Two closely-related fixes bracket this incident and this test's status:

* **Issue #299 (MERGED, on dev)** — ``scanner_universe_backfill.
  resettle_recent_daily`` is a *data-pipeline* fix: it forces historify to
  re-fetch and overwrite the last few settled trading days' daily-D bars, so
  in a running system the 2026-06-30 bar would no longer be stuck at the
  stale 475.4 value. That fix operates on ``historify.duckdb`` — it does NOT
  touch ``fno_intraday_buy_chartink.py``'s gate logic at all.
* **Issue #305 (SHIPPED — this PR)** — the *rule-level* reference-data
  validation choke point: cross-check ``yest_d.close`` against an
  independently-captured broker prev-close
  (``services/scanner_reference_data.py``) and REJECT on divergence, so even
  if a stale value somehow reaches the rule (pipeline fix bypassed, race, or
  a future regression of #299), the rule itself refuses to fire on it. The
  scanner passes the verdict via the indicators dict; direct rule callers
  (this test, backtests) are covered by the rule-side fallback that consults
  the broker prev-close registry itself.

Because this test operates purely at the rule level — it hands the rule a
frame that already carries the stale reference value, exactly as the rule
saw it in production before #299 — it is unaffected by #299 (a pipeline fix,
not a rule fix) and pins #305's enforcement: with the broker prev-close
(~510, the real 07-01 settle) recorded the way the boot aggregator_seeder
records it, BUY must NOT fire on the stale 475.4 reference.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

import services.scan_rules.fno_intraday_buy_chartink as rulemod
import services.scanner_reference_data as refdata
from services.scan_rules.fno_intraday_buy_chartink import rule
from test.fixtures.frame_factory import (
    flat_closes,
    make_15m_frame,
    make_historify_daily_frame,
    make_live_5m_frame,
    make_weekly_frame,
    ramp_closes,
)

_TODAY = date(2026, 7, 2)
_STALE_YEST_SETTLE_DATE = date(2026, 6, 30)  # the never-re-settled provisional bar's date

# Incident snapshot values (from the production PASS-log gate stash, #205).
_TODAY_D_CLOSE = 505.8
_TODAY_D_OPEN = 507.7
_STALE_YEST_D_CLOSE = 475.4
# The REAL 2026-07-01 broker-settled close (what the aggregator_seeder's
# divergence log recorded as broker_last_close) — the value the stale 475.4
# reference should have been.
_REAL_BROKER_PREV_CLOSE = 510.0


@pytest.fixture(autouse=True)
def _reset_reference_registry():
    """Clean broker prev-close registry per test — the module-global registry
    must not leak the DELHIVERY recording into other test files."""
    refdata.reset_for_tests()
    yield
    refdata.reset_for_tests()


def _freeze(monkeypatch, hour, minute=0):
    """Pin ``rulemod.datetime.now(tz)`` to 2026-07-02 hour:minute IST — same
    pattern as ``test/test_fno_intraday_buy_chartink.py::_freeze``."""

    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            naive = datetime(2026, 7, 2, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(rulemod, "datetime", _FrozenDateTime)


def _delhivery_incident_frames():
    """Build the DELHIVERY 2026-07-02 incident frame bundle.

    - ``bars_daily``: 205 historify-shaped daily bars (SMA(200) volume-gate
      warm-up requirement), flat at 470 with the LAST bar dated
      2026-06-30 carrying the stale provisional close 475.4 — modelling the
      never-re-settled bar from issue #299's root-cause writeup. Daily
      volume held low (800k) so the live tape's volume clears both SMA(50)
      and SMA(200) — matching the incident where the volume gates DID clear.
    - ``bars_5m``: live-shaped intraday tape on 2026-07-02, opening 507.7 and
      settling at 505.8 (today_d_open / today_d_close from the incident
      snapshot). Per-bar volume is large enough that the running-day sum
      clears the daily volume SMAs.
    - ``bars_15m``: a rising tape so RSI(14) clears the >50 gate (incident
      recorded rsi_15m=82; a monotone ramp here saturates RSI near 100,
      which is >82 and exercises the same gate direction).
    - ``bars_weekly``: flat with enough range that ATR(21) clears the
      5%-of-close gate.
    """
    daily_closes = flat_closes(204, 470.0) + [_STALE_YEST_D_CLOSE]
    bars_daily = make_historify_daily_frame(
        daily_closes, end_date=_STALE_YEST_SETTLE_DATE, volumes=800_000.0
    )

    n5 = 20
    step = (_TODAY_D_CLOSE - _TODAY_D_OPEN) / (n5 - 1)
    closes5 = ramp_closes(n5, _TODAY_D_OPEN, step)
    closes5[-1] = _TODAY_D_CLOSE  # pin the exact incident close
    bars_5m = make_live_5m_frame(closes5, _TODAY, volumes=100_000.0)

    bars_15m = make_15m_frame(ramp_closes(20, 460.0, 3.0), _TODAY)
    bars_weekly = make_weekly_frame(flat_closes(25, 470.0), end_date=date(2026, 6, 26))

    return {
        "bars_5m": bars_5m,
        "bars_15m": bars_15m,
        "bars_daily": bars_daily,
        "bars_weekly": bars_weekly,
        "symbol": "DELHIVERY",
    }


def test_delhivery_2026_07_02_stale_reference_must_not_fire(monkeypatch):
    """BUY must NOT fire when yest_d_close is a stale, never-re-settled
    provisional daily bar — even though every other gate (gap-up math,
    volume, RSI, ATR, Supertrend) clears against that stale reference,
    exactly as it did in the live 2026-07-02 incident.
    """
    _freeze(monkeypatch, 9, 55)  # mid-morning, matches the incident's 09:30-09:55 firing window
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_BLOCK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_DBAR_DATE_VERIFY_ENABLED", "true")
    monkeypatch.setenv("SCANNER_REFERENCE_CHECK_ENABLED", "true")

    # #305: record the broker-known T-1 settled close the way the boot
    # aggregator_seeder records it in production. The rule's reference
    # cross-check (rule-side fallback — this test bypasses scanner_service)
    # compares yest_d.close (475.4) against this and rejects on the ~6.8%
    # divergence.
    refdata.record_broker_prev_close("DELHIVERY", _REAL_BROKER_PREV_CLOSE)

    ind = _delhivery_incident_frames()

    # Sanity: this fixture DOES reproduce the incident snapshot values via the
    # shared helper, so a failure here means the fixture itself drifted from
    # the incident, not that the assertion below is testing the wrong thing.
    from services.scan_rules._today_running import derive_today_and_yest

    today_d, yest_d, _yest_idx = derive_today_and_yest(
        ind["bars_daily"], ind["bars_5m"], rulemod.datetime.now(rulemod._IST)
    )
    assert today_d is not None and yest_d is not None
    assert today_d.close == pytest.approx(_TODAY_D_CLOSE)
    assert yest_d.close == pytest.approx(_STALE_YEST_D_CLOSE)

    # The existing divergence-block guard (#205/#279) does NOT catch this —
    # it only compares today_d.close against the live 5m close, and both are
    # in agreement here (the corruption is in the REFERENCE, not today's
    # value). This is precisely the gap issue #305 documents.
    assert rule(None, ind) is False, (
        "BUY fired on a stale yest_d_close reference — this is the exact "
        "2026-07-02 DELHIVERY false-BUY incident. Expected False once #305's "
        "reference-data validation ships."
    )
