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
from services.open15_breakout_service import fetch_broker_prev_closes, verify_prev_closes

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


# --------------------------------------------------------------------------- #
# fetch_broker_prev_closes — the 09:10 batched quote snapshot (#456 commit 2)
# --------------------------------------------------------------------------- #


def _mock_quotes(monkeypatch, results, success=True, message="err"):
    import database.auth_db as auth_db
    import services.quotes_service as qs

    monkeypatch.setattr(auth_db, "get_first_available_api_key", lambda: "test-key")
    resp = {"results": results} if success else {"status": "error", "message": message}
    monkeypatch.setattr(qs, "get_multiquotes", lambda payload, api_key=None: (success, resp, 200))


def test_quote_snapshot_returns_and_records_prev_closes(monkeypatch):
    _mock_quotes(
        monkeypatch,
        [
            {"symbol": "OFSS", "data": {"prev_close": 10851.0, "ltp": 11240.0}},
            {"symbol": "ONGC", "data": {"prev_close": 251.90}},
        ],
    )
    out = fetch_broker_prev_closes({"OFSS", "ONGC"})
    assert out == {"OFSS": 10851.0, "ONGC": 251.90}
    # values recorded into the #305 registry (scanner benefits all day)
    assert ref.get_broker_prev_close("OFSS")[0] == 10851.0


def test_quote_snapshot_failure_returns_empty(monkeypatch):
    _mock_quotes(monkeypatch, [], success=False, message="broker down")
    assert fetch_broker_prev_closes({"OFSS"}) == {}


def test_quote_snapshot_no_api_key_returns_empty(monkeypatch):
    import database.auth_db as auth_db

    monkeypatch.setattr(auth_db, "get_first_available_api_key", lambda: None)
    assert fetch_broker_prev_closes({"OFSS"}) == {}


def test_quote_snapshot_skips_malformed_and_zero_rows(monkeypatch):
    _mock_quotes(
        monkeypatch,
        [
            {"symbol": "GOOD", "data": {"prev_close": 100.0}},
            {"symbol": "ZERO", "data": {"prev_close": 0}},
            {"symbol": "NONE", "data": {"prev_close": None}},
            {"symbol": "BAD", "data": {"prev_close": "n/a"}},
            {"symbol": None, "data": {"prev_close": 50.0}},
            {"symbol": "NODATA"},
        ],
    )
    assert fetch_broker_prev_closes({"GOOD", "ZERO", "NONE", "BAD", "NODATA"}) == {"GOOD": 100.0}


def test_quote_snapshot_flag_off_makes_no_call(monkeypatch):
    monkeypatch.setenv("OPEN15_PREVCLOSE_QUOTES_ENABLED", "false")

    import services.quotes_service as qs

    def boom(*a, **k):
        raise AssertionError("quote call must not happen when disabled")

    monkeypatch.setattr(qs, "get_multiquotes", boom)
    assert fetch_broker_prev_closes({"OFSS"}) == {}


def test_quote_snapshot_exception_fails_open(monkeypatch):
    import database.auth_db as auth_db
    import services.quotes_service as qs

    monkeypatch.setattr(auth_db, "get_first_available_api_key", lambda: "test-key")

    def boom(*a, **k):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(qs, "get_multiquotes", boom)
    assert fetch_broker_prev_closes({"OFSS"}) == {}


def test_quote_values_dominate_stale_historify_via_chain(monkeypatch):
    """End-to-end shape of the arm's chain: quote snapshot -> merge -> verify.

    Even a symbol the quote call MISSED is still protected: its quote-recorded
    registry sibling verifies it, and unrecorded symbols fail open.
    """
    _mock_quotes(monkeypatch, [{"symbol": "OFSS", "data": {"prev_close": 10851.0}}])
    historify = {"OFSS": 10776.6, "TITAN": 4404.0}  # OFSS provisional, TITAN fine
    quotes = fetch_broker_prev_closes({"OFSS", "TITAN"})
    merged = {**historify, **quotes}
    out, prov = verify_prev_closes(merged, dt.date.today())
    assert out["OFSS"] == 10851.0  # quote value won the merge
    assert out["TITAN"] == 4404.0  # missed by quotes -> historify, fail-open
    assert prov["overridden"] == 0  # nothing left diverging after the merge
