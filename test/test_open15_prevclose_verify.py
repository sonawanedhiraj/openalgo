"""Issue #456 — open15 prev-close verification against the broker registry.

The 2026-07-23 incident: the 09:10 arm read historify-D prev-closes while the
daily-D resettle (#299) was still overwriting provisional values (09:08:37 ->
09:18:45), silently shifting the gap ranking. ``verify_prev_closes`` makes the
broker prev-close registry (#305) enforceable at arm time: on a confirmed
divergence the broker's settled value wins; a missing registry entry fails
open to the historify value.
"""

import datetime as dt

import pytest

from services import scanner_reference_data as ref
from services.open15_breakout_service import verify_prev_closes

TODAY = dt.date(2026, 7, 23)
NOW = dt.datetime(2026, 7, 23, 9, 10, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30)))


@pytest.fixture(autouse=True)
def _clean_registry():
    ref.reset_for_tests()
    yield
    ref.reset_for_tests()


def _record(symbol: str, close: float, when: dt.datetime = NOW) -> None:
    ref.record_broker_prev_close(symbol, close, as_of=when)


def test_stale_historify_close_overridden_by_registry():
    # The OFSS 07-23 shape: historify held a provisional close 0.69% below
    # the broker-settled value at arm time.
    _record("OFSS", 10851.0)
    out, prov = verify_prev_closes({"OFSS": 10776.6}, TODAY)
    assert out["OFSS"] == 10851.0
    assert prov["overridden"] == 1
    assert prov["checked"] == 1
    assert prov["overrides"][0]["symbol"] == "OFSS"
    assert prov["overrides"][0]["divergence_pct"] > 0.05


def test_matching_close_kept_verbatim():
    _record("ONGC", 251.90)
    out, prov = verify_prev_closes({"ONGC": 251.90}, TODAY)
    assert out["ONGC"] == 251.90
    assert prov["overridden"] == 0
    assert prov["checked"] == 1


def test_sub_threshold_rounding_difference_kept():
    # A few-bps difference is rounding, not staleness — historify value kept.
    _record("OIL", 451.15)
    out, prov = verify_prev_closes({"OIL": 451.20}, TODAY)  # 0.011% apart
    assert out["OIL"] == 451.20
    assert prov["overridden"] == 0


def test_missing_registry_entry_fails_open():
    out, prov = verify_prev_closes({"DRREDDY": 1179.9}, TODAY)
    assert out["DRREDDY"] == 1179.9
    assert prov["no_registry_entry"] == 1
    assert prov["checked"] == 0
    assert prov["overridden"] == 0


def test_yesterdays_registry_recording_is_not_served_today():
    # Day-scoping (#305): a value recorded yesterday was YESTERDAY's T-1 close.
    _record("SBIN", 800.0, when=NOW - dt.timedelta(days=1))
    out, prov = verify_prev_closes({"SBIN": 790.0}, TODAY)
    assert out["SBIN"] == 790.0
    assert prov["no_registry_entry"] == 1


def test_flag_disables_verification(monkeypatch):
    monkeypatch.setenv("OPEN15_PREVCLOSE_REGISTRY_CHECK_ENABLED", "false")
    _record("INFY", 1500.0)
    out, prov = verify_prev_closes({"INFY": 1400.0}, TODAY)
    assert out["INFY"] == 1400.0
    assert prov == {"enabled": False}


def test_threshold_knob_respected(monkeypatch):
    # With a huge threshold even the 07-23 divergence passes untouched.
    monkeypatch.setenv("OPEN15_PREVCLOSE_DIVERGENCE_MAX_PCT", "5.0")
    _record("OFSS", 10851.0)
    out, prov = verify_prev_closes({"OFSS": 10776.6}, TODAY)
    assert out["OFSS"] == 10776.6
    assert prov["overridden"] == 0


def test_mixed_universe_provenance_counts():
    _record("OFSS", 10851.0)  # will override
    _record("ONGC", 251.90)  # matches
    closes = {"OFSS": 10776.6, "ONGC": 251.90, "TITAN": 4404.0}  # TITAN unregistered
    out, prov = verify_prev_closes(closes, TODAY)
    assert out == {"OFSS": 10851.0, "ONGC": 251.90, "TITAN": 4404.0}
    assert (prov["checked"], prov["no_registry_entry"], prov["overridden"]) == (2, 1, 1)


def test_internal_error_fails_open(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(ref, "get_broker_prev_close", boom)
    closes = {"SBIN": 790.0}
    out, prov = verify_prev_closes(closes, TODAY)
    assert out == closes
    assert prov.get("error") is True
