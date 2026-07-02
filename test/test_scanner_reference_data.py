"""Tests for the scanner reference-data contract (issue #305).

Covers:
* the broker prev-close registry (record/get, day-scoping semantics,
  derivation from a broker 1m-bar series),
* ``compute_reference_certificate`` — the golden 2026-07-02 DELHIVERY
  incident numbers (settled 475.4 vs broker 510.0 → NOT certified,
  divergence ~6.78%), the fail-open paths (missing broker prev-close,
  disabled flag, index skip), and the threshold knob,
* the end-to-end golden regression: an uncertified reference makes the BUY
  rule return False and fires the source-divergence alert,
* the aggregator_seeder wiring: the broker-fallback fetch records the T-1
  settled close as a side effect.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import services.scanner_reference_data as refdata

_IST = timezone(timedelta(hours=5, minutes=30))

# The golden incident day: 2026-07-02, mid-morning.
TODAY = date(2026, 7, 2)
NOW = datetime(2026, 7, 2, 9, 30, tzinfo=_IST)
YESTERDAY_NOW = datetime(2026, 7, 1, 8, 40, tzinfo=_IST)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clean registry + warning dedup per test; pin the flags to defaults."""
    refdata.reset_for_tests()
    monkeypatch.setenv("SCANNER_REFERENCE_CHECK_ENABLED", "true")
    monkeypatch.setenv("SCANNER_REFERENCE_DIVERGENCE_MAX_PCT", "1.0")
    yield
    refdata.reset_for_tests()


def _daily(yest_close=475.4, today_close=470.0, n=5):
    """Synthetic daily frame WITHOUT a timestamp column → Path A in
    ``derive_today_and_yest``: iloc[-1] is today, iloc[-2] is yesterday
    (the settled reference the certificate validates)."""
    close = [yest_close] * (n - 1) + [today_close]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c + 1 for c in close],
            "low": [c - 1 for c in close],
            "close": close,
            "volume": [1000.0] * n,
        }
    )


# --------------------------------------------------------------------------- #
# Registry — record/get + day-scoping
# --------------------------------------------------------------------------- #


def test_record_and_get_same_day():
    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    got = refdata.get_broker_prev_close("DELHIVERY", today=TODAY)
    assert got is not None
    close, as_of = got
    assert close == 510.0
    assert as_of == NOW


def test_get_unknown_symbol_returns_none():
    assert refdata.get_broker_prev_close("UNKNOWN", today=TODAY) is None


def test_day_scoping_prior_day_recording_not_served():
    """A prev-close recorded YESTERDAY was yesterday's T-1 — it must never be
    served as today's T-1 (the core day-scoping semantics of issue #305)."""
    refdata.record_broker_prev_close("DELHIVERY", 475.4, as_of=YESTERDAY_NOW)
    assert refdata.get_broker_prev_close("DELHIVERY", today=TODAY) is None
    # ... but it IS served for the day it was recorded on.
    assert refdata.get_broker_prev_close("DELHIVERY", today=date(2026, 7, 1)) == (
        475.4,
        YESTERDAY_NOW,
    )


def test_same_day_recording_is_served_even_when_recorded_pre_open():
    """A value recorded at 08:40 today IS today's correct T-1."""
    pre_open = datetime(2026, 7, 2, 8, 40, tzinfo=_IST)
    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=pre_open)
    assert refdata.get_broker_prev_close("DELHIVERY", today=TODAY) == (510.0, pre_open)


def test_record_unparseable_close_is_ignored():
    refdata.record_broker_prev_close("X", "not-a-number", as_of=NOW)  # type: ignore[arg-type]
    assert refdata.get_broker_prev_close("X", today=TODAY) is None


# --------------------------------------------------------------------------- #
# record_prev_close_from_bars — derivation from the seeder's broker series
# --------------------------------------------------------------------------- #


def _bar(ts, close):
    return {"ts": ts, "open": close, "high": close, "low": close, "close": close, "volume": 100}


def test_prev_close_from_bars_picks_last_pre_today_bar():
    """Mid-session restart: the series spans yesterday + today; the T-1 close
    is the LAST bar dated strictly before today, never a today running bar."""
    bars = [
        _bar(datetime(2026, 7, 1, 15, 25), 509.0),
        _bar(datetime(2026, 7, 1, 15, 29), 510.0),  # ← the T-1 settled close
        _bar(datetime(2026, 7, 2, 9, 16), 470.0),  # today's running bar — skipped
    ]
    assert refdata.record_prev_close_from_bars("DELHIVERY", bars, today=TODAY) is True
    got = refdata.get_broker_prev_close("DELHIVERY", today=datetime.now(_IST).date())
    assert got is not None
    assert got[0] == 510.0


def test_prev_close_from_bars_all_today_records_nothing():
    bars = [_bar(datetime(2026, 7, 2, 9, 16), 470.0)]
    assert refdata.record_prev_close_from_bars("DELHIVERY", bars, today=TODAY) is False
    assert refdata.get_broker_prev_close("DELHIVERY", today=datetime.now(_IST).date()) is None


def test_prev_close_from_bars_empty_series_is_safe():
    assert refdata.record_prev_close_from_bars("DELHIVERY", [], today=TODAY) is False


# --------------------------------------------------------------------------- #
# compute_reference_certificate
# --------------------------------------------------------------------------- #


def test_golden_incident_divergence_not_certified():
    """The 2026-07-02 DELHIVERY numbers: settled reference 475.4 (stale
    historify-D) vs broker prev-close 510.0 → divergence ~6.78% > 1.0 →
    NOT certified."""
    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    assert cert["reference_certified"] is False
    assert cert["reference_settled_close"] == 475.4
    assert cert["reference_broker_prev_close"] == 510.0
    assert abs(cert["reference_divergence_pct"] - 6.7843) < 0.01


def test_small_divergence_is_certified():
    refdata.record_broker_prev_close("DELHIVERY", 476.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    assert cert["reference_certified"] is True
    assert cert["reference_divergence_pct"] is not None
    assert cert["reference_divergence_pct"] < 1.0


def test_threshold_env_override(monkeypatch):
    """SCANNER_REFERENCE_DIVERGENCE_MAX_PCT=10 makes the golden 6.78%
    divergence pass — the knob is live."""
    monkeypatch.setenv("SCANNER_REFERENCE_DIVERGENCE_MAX_PCT", "10.0")
    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    assert cert["reference_certified"] is True


def test_missing_broker_prev_close_fail_open_with_deduped_warning(caplog):
    """No broker prev-close recorded today → certified (fail-open) + a
    WARNING logged exactly once per (symbol, day)."""
    with caplog.at_level(logging.WARNING):
        cert1 = refdata.compute_reference_certificate(
            "DELHIVERY", None, _daily(), exchange="NSE", now_ist=NOW
        )
        cert2 = refdata.compute_reference_certificate(
            "DELHIVERY", None, _daily(), exchange="NSE", now_ist=NOW
        )
    assert cert1["reference_certified"] is True
    assert cert1["reference_divergence_pct"] is None
    assert cert2["reference_certified"] is True
    warns = [r for r in caplog.records if "no broker prev-close recorded today" in r.getMessage()]
    assert len(warns) == 1, "missing-cross-check WARNING must be deduped per (symbol, day)"


def test_stale_prior_day_recording_treated_as_missing(caplog):
    """A registry entry recorded yesterday is ignored (day-scoping) — the
    certificate falls back to the fail-open missing-cross-check path."""
    refdata.record_broker_prev_close("DELHIVERY", 475.4, as_of=YESTERDAY_NOW)
    with caplog.at_level(logging.WARNING):
        cert = refdata.compute_reference_certificate(
            "DELHIVERY", None, _daily(), exchange="NSE", now_ist=NOW
        )
    assert cert["reference_certified"] is True
    assert cert["reference_broker_prev_close"] is None
    assert any("no broker prev-close recorded today" in r.getMessage() for r in caplog.records)


def test_flag_off_skips_check_entirely(monkeypatch):
    """SCANNER_REFERENCE_CHECK_ENABLED=false → no keys at all (the rules'
    missing-key-is-certified backward compat covers the rest)."""
    monkeypatch.setenv("SCANNER_REFERENCE_CHECK_ENABLED", "false")
    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    assert cert == {}


def test_index_symbols_skipped_quietly(caplog):
    """NSE_INDEX symbols are never rule-evaluated — no cross-check, no
    missing-broker warning noise."""
    with caplog.at_level(logging.WARNING):
        cert = refdata.compute_reference_certificate(
            "NIFTY", None, _daily(), exchange="NSE_INDEX", now_ist=NOW
        )
    assert cert["reference_certified"] is True
    assert not any("no broker prev-close recorded today" in r.getMessage() for r in caplog.records)


def test_no_daily_frame_is_certified_without_warning(caplog):
    """bars_daily None → nothing to validate; the rules reject loudly on
    their own missing-daily path. No missing-cross-check warning."""
    with caplog.at_level(logging.WARNING):
        cert = refdata.compute_reference_certificate(
            "DELHIVERY", None, None, exchange="NSE", now_ist=NOW
        )
    assert cert["reference_certified"] is True
    assert not any("no broker prev-close recorded today" in r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------- #
# End-to-end golden regression — BUY rule + alert
# --------------------------------------------------------------------------- #


def test_golden_incident_buy_rule_rejects_and_alerts(monkeypatch):
    """Frames with settled reference 475.4 while the recorded broker
    prev-close is 510.0 → the BUY rule returns False and the divergence
    alert fired (issue #305 acceptance criterion)."""
    import services.scan_rules.fno_intraday_buy_chartink as buymod
    import services.source_divergence_alerts as sda

    calls: list[dict] = []
    monkeypatch.setattr(sda, "check_and_alert", lambda **kw: calls.append(kw) or True)
    buymod._uncertified_warned.clear()

    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    assert cert["reference_certified"] is False

    indicators = {"symbol": "DELHIVERY", "exchange": "NSE", **cert}
    assert buymod.rule(None, indicators) is False
    assert len(calls) == 1
    assert calls[0]["service"] == "scanner_reference"
    assert calls[0]["symbol"] == "DELHIVERY"
    assert calls[0]["source_a_value"] == 475.4
    assert calls[0]["source_b_value"] == 510.0


def test_golden_incident_sell_rule_rejects_and_alerts(monkeypatch):
    """SELL mirror of the golden regression — a stale reference manufactures
    phantom signals in both directions."""
    import services.scan_rules.fno_intraday_sell_chartink as sellmod
    import services.source_divergence_alerts as sda

    calls: list[dict] = []
    monkeypatch.setattr(sda, "check_and_alert", lambda **kw: calls.append(kw) or True)
    sellmod._uncertified_warned.clear()

    refdata.record_broker_prev_close("DELHIVERY", 510.0, as_of=NOW)
    cert = refdata.compute_reference_certificate(
        "DELHIVERY", None, _daily(yest_close=475.4), exchange="NSE", now_ist=NOW
    )
    indicators = {"symbol": "DELHIVERY", "exchange": "NSE", **cert}
    assert sellmod.rule(None, indicators) is False
    assert len(calls) == 1
    assert calls[0]["service"] == "scanner_reference"


# --------------------------------------------------------------------------- #
# aggregator_seeder wiring — broker fetch records the T-1 close
# --------------------------------------------------------------------------- #


def test_seeder_broker_fallback_records_prev_close(monkeypatch):
    """The broker-fallback arm of ``_read_1m_bars_for_symbol`` records the
    last pre-today broker close into the registry — reusing the existing
    fetch, no new API load."""
    import services.scanner_aggregator_seeder as seeder

    today = datetime.now(_IST).date()
    yest = datetime.combine(today - timedelta(days=1), datetime.min.time())
    broker_bars = [
        _bar(yest.replace(hour=15, minute=25), 509.0),
        _bar(yest.replace(hour=15, minute=29), 510.0),
        _bar(datetime.combine(today, datetime.min.time()).replace(hour=9, minute=16), 470.0),
    ]
    monkeypatch.setenv("SCANNER_AGGREGATOR_SEED_BROKER_FALLBACK_ENABLED", "true")
    monkeypatch.setattr(seeder, "_read_1m_bars_from_historify", lambda *a, **k: [])
    monkeypatch.setattr(seeder, "_read_1m_bars_from_broker", lambda *a, **k: list(broker_bars))

    out = seeder._read_1m_bars_for_symbol("DELHIVERY", "NSE", 500, api_key="test-key")
    assert out == broker_bars
    got = refdata.get_broker_prev_close("DELHIVERY", today=today)
    assert got is not None
    assert got[0] == 510.0


def test_seeder_no_broker_fetch_records_nothing(monkeypatch):
    """Historify-sufficient path never fetches the broker → registry stays
    empty (the certificate's fail-open missing-cross-check path)."""
    import services.scanner_aggregator_seeder as seeder

    base = datetime(2026, 7, 2, 9, 16)
    hist_bars = [_bar(base + timedelta(minutes=i), 470.0) for i in range(200)]
    monkeypatch.setattr(seeder, "_read_1m_bars_from_historify", lambda *a, **k: list(hist_bars))

    out = seeder._read_1m_bars_for_symbol("DELHIVERY", "NSE", 500, api_key="test-key")
    assert out == hist_bars
    assert refdata.get_broker_prev_close("DELHIVERY", today=datetime.now(_IST).date()) is None
