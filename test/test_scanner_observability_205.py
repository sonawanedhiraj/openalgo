"""Unit tests for Issue #205 — divergence WARNING + PASS-log gate-value
expansion + ``get_last_eval_snapshot`` thread-local on both rule modules.

These tests are FAST and HERMETIC (synthetic frames, no broker). The
broader live-data contract test lives in
``test/test_scanner_rule_vs_broker_contract.py`` and is opt-in.
"""

from __future__ import annotations

import logging
from datetime import datetime as _RealDateTime
from types import SimpleNamespace

import pandas as pd
import pytest

import services.scan_rules.fno_intraday_buy_chartink as buymod
import services.scan_rules.fno_intraday_sell_chartink as sellmod
import services.scanner_service as scanner_service

# Re-use the existing synthetic frame builders from the BUY rule's test
# module so frame layouts stay in sync.
from test.test_fno_intraday_buy_chartink import happy as _happy_buy


def _freeze_buy(monkeypatch, hour=16, minute=0):
    class _Frozen:
        @classmethod
        def now(cls, tz=None):
            naive = _RealDateTime(2026, 6, 4, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(buymod, "datetime", _Frozen)


def _freeze_sell(monkeypatch, hour=16, minute=0):
    class _Frozen:
        @classmethod
        def now(cls, tz=None):
            naive = _RealDateTime(2026, 6, 4, hour, minute)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(sellmod, "datetime", _Frozen)


# --------------------------------------------------------------------------- #
# Divergence WARNING — BUY rule
# --------------------------------------------------------------------------- #
def _buy_divergent_fixture():
    """Happy BUY fixture but with today_d.close DELIBERATELY mismatched against
    the latest 5m close. ``derive_today_and_yest`` returns iloc[-1] for both
    when no timestamp column is present, so today_d.close = daily.iloc[-1].close
    and last_5m_close = bars_5m.iloc[-1].close — we set those independently.
    """
    ind = _happy_buy()
    daily = ind["bars_daily"].copy()
    daily.loc[daily.index[-1], "close"] = 2050.0  # today_d.close
    ind["bars_daily"] = daily
    b5 = ind["bars_5m"].copy()
    b5.loc[b5.index[-1], "close"] = 2103.0  # latest 5m close — > 0.5% apart
    ind["bars_5m"] = b5
    return ind


def test_buy_divergence_warning_fires_on_mismatch(monkeypatch, caplog):
    _freeze_buy(monkeypatch)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")

    ind = _buy_divergent_fixture()
    ind["symbol"] = "TCS"

    with caplog.at_level(logging.WARNING, logger=buymod.logger.name):
        buymod.rule(None, ind)  # may pass or fail — we only care about the WARNING

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "diverges" in r.getMessage()
    ]
    assert warnings, "divergence WARNING should fire when today_d.close ≠ last 5m close"
    msg = warnings[0].getMessage()
    assert "TCS" in msg
    assert "2050.00" in msg
    assert "2103.00" in msg


def test_buy_divergence_warning_silent_when_values_agree(monkeypatch, caplog):
    _freeze_buy(monkeypatch)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")

    ind = _happy_buy()
    # Set both to the same value (no divergence).
    daily = ind["bars_daily"].copy()
    daily.loc[daily.index[-1], "close"] = 2100.0
    ind["bars_daily"] = daily
    b5 = ind["bars_5m"].copy()
    b5.loc[b5.index[-1], "close"] = 2100.0
    ind["bars_5m"] = b5
    ind["symbol"] = "TCS"

    with caplog.at_level(logging.WARNING, logger=buymod.logger.name):
        buymod.rule(None, ind)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "diverges" in r.getMessage()
    ]
    assert not warnings, f"no WARNING expected when values agree, got: {warnings}"


def test_buy_divergence_warning_disabled_by_env_flag(monkeypatch, caplog):
    _freeze_buy(monkeypatch)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "false")

    ind = _buy_divergent_fixture()
    ind["symbol"] = "TCS"

    with caplog.at_level(logging.WARNING, logger=buymod.logger.name):
        buymod.rule(None, ind)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "diverges" in r.getMessage()
    ]
    assert not warnings, "WARNING should be suppressed when the env flag is off"


# --------------------------------------------------------------------------- #
# Divergence WARNING — SELL rule
# --------------------------------------------------------------------------- #
def _sell_divergent_fixture():
    """SELL has no SMA(volume, 200) gate so daily needs only ~3 settled rows.
    Build a minimal happy fixture inline — we only need ``derive_today_and_yest``
    to succeed for the divergence guard to fire.
    """
    n_daily = 10
    daily = pd.DataFrame(
        {
            "open": [2000.0] * (n_daily - 1) + [1950.0],
            "high": [2010.0] * (n_daily - 1) + [1960.0],
            "low": [1990.0] * (n_daily - 1) + [1940.0],
            # iloc[-1] (today_d) close DELIBERATELY mismatched against last 5m.
            "close": [2000.0] * (n_daily - 1) + [1950.0],
            "volume": [1000.0] * n_daily,
        }
    )
    weekly_n = 25
    weekly = pd.DataFrame(
        {
            "open": [2000.0] * weekly_n,
            "high": [2200.0] * weekly_n,
            "low": [1800.0] * weekly_n,
            "close": [2000.0] * weekly_n,
            "volume": [1000.0] * weekly_n,
        }
    )
    n_5m = 20
    b5 = pd.DataFrame(
        {
            "open": [1900.0] * n_5m,
            "high": [1910.0] * n_5m,
            "low": [1890.0] * n_5m,
            # last 5m close at 1900 vs today_d.close 1950 → +2.6% divergence.
            "close": [1900.0] * n_5m,
            "volume": [100.0] * n_5m,
        }
    )
    b15 = pd.DataFrame(
        {
            "open": [1900.0] * 20,
            "high": [1910.0] * 20,
            "low": [1890.0] * 20,
            "close": [1900.0] * 20,
            "volume": [100.0] * 20,
        }
    )
    return {
        "symbol": "INFY",
        "exchange": "NSE",
        "bars_5m": b5,
        "bars_15m": b15,
        "bars_daily": daily,
        "bars_weekly": weekly,
    }


def test_sell_divergence_warning_fires_on_mismatch(monkeypatch, caplog):
    _freeze_sell(monkeypatch)
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_ENABLED", "true")
    monkeypatch.setenv("SCANNER_RULE_DIVERGENCE_WARN_PCT", "0.5")

    ind = _sell_divergent_fixture()

    with caplog.at_level(logging.WARNING, logger=sellmod.logger.name):
        sellmod.rule(None, ind)

    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and "diverges" in r.getMessage()
    ]
    assert warnings, "SELL rule divergence WARNING should fire on mismatch"
    msg = warnings[0].getMessage()
    assert "INFY" in msg
    assert "1950.00" in msg
    assert "1900.00" in msg


# --------------------------------------------------------------------------- #
# Gate-snapshot stash → ``get_last_eval_snapshot()``
# --------------------------------------------------------------------------- #
def test_buy_get_last_eval_snapshot_after_pass(monkeypatch):
    """A successful BUY evaluation must stash all gate values on the thread-local."""
    _freeze_buy(monkeypatch)
    # Clear any prior thread-local snapshot from earlier tests.
    if hasattr(buymod._last_eval, "snapshot"):
        del buymod._last_eval.snapshot

    ind = _happy_buy()
    matched = buymod.rule(None, ind)
    assert matched is True, "happy fixture should pass all BUY gates"

    snap = buymod.get_last_eval_snapshot()
    assert isinstance(snap, dict)
    expected_keys = {
        "today_d_close",
        "yest_d_close",
        "today_d_open",
        "today_d_volume",
        "pivot",
        "rsi_15m",
        "st_now",
        "st_prev",
        "weekly_atr",
        "sma_vol_short",
        "sma_vol_long",
    }
    assert expected_keys.issubset(snap.keys()), f"missing keys: {expected_keys - snap.keys()}"
    # Spot-check sanity: today_d.close should equal the synthetic 2100.0 default.
    assert snap["today_d_close"] == pytest.approx(2100.0)


def test_sell_get_last_eval_snapshot_returns_dict():
    """``get_last_eval_snapshot`` should be exposed on the SELL module too."""
    assert callable(sellmod.get_last_eval_snapshot)


def test_get_last_eval_snapshot_is_none_initially():
    """Before any evaluation, the helper returns None (clean thread)."""
    import threading

    captured: dict = {}

    def _worker():
        # A brand-new OS thread sees no snapshot yet.
        captured["snap"] = buymod.get_last_eval_snapshot()

    t = threading.Thread(target=_worker)
    t.start()
    t.join()
    assert captured["snap"] is None


# --------------------------------------------------------------------------- #
# scanner_service._resolve_eval_snapshot helper
# --------------------------------------------------------------------------- #
def test_resolve_eval_snapshot_returns_buy_snapshot(monkeypatch):
    """The scanner_service helper must locate the rule's get_last_eval_snapshot
    via ``rule_fn.__module__`` and return whatever it produces."""
    _freeze_buy(monkeypatch)
    # Drive a successful evaluation so the thread-local is populated.
    buymod.rule(None, _happy_buy())
    snap = scanner_service._resolve_eval_snapshot(buymod.rule)
    assert isinstance(snap, dict)
    assert "today_d_close" in snap


def test_resolve_eval_snapshot_returns_none_for_uninstrumented_rule():
    """A rule whose module exposes no ``get_last_eval_snapshot`` resolves to None
    so the PASS-log site falls back to the prior ``close=...`` shape."""

    def _bare_rule(bars, indicators):
        return True

    # __module__ defaults to this test module; the test module has no helper.
    assert scanner_service._resolve_eval_snapshot(_bare_rule) is None


def test_resolve_eval_snapshot_swallows_helper_exceptions(monkeypatch):
    """If the rule's helper raises, the scanner must NOT crash the log site."""

    class _BoomModule:
        def get_last_eval_snapshot(self):
            raise RuntimeError("boom")

    monkeypatch.setitem(__import__("sys").modules, "test._fake_boom_205", _BoomModule())

    def _boom_rule(bars, indicators):
        return True

    _boom_rule.__module__ = "test._fake_boom_205"
    assert scanner_service._resolve_eval_snapshot(_boom_rule) is None


# --------------------------------------------------------------------------- #
# scanner_service PASS log line includes gate values
# --------------------------------------------------------------------------- #
def test_scanner_pass_log_includes_gate_values(monkeypatch, caplog):
    """A PASS through the scanner_service evaluation path must include the
    rule's snapshot keys in the log line."""
    _freeze_buy(monkeypatch)
    # Populate the thread-local by running the rule directly first.
    buymod.rule(None, _happy_buy())

    # Now simulate the scanner's PASS-log site by reading the snapshot back.
    snapshot = scanner_service._resolve_eval_snapshot(buymod.rule)
    assert snapshot is not None

    # Mirror the kv formatting the PASS log site uses.
    kv = " ".join(
        f"{k}={v:.4g}" if isinstance(v, float) else f"{k}={v}" for k, v in snapshot.items()
    )
    assert "today_d_close=" in kv
    assert "rsi_15m=" in kv
    assert "weekly_atr=" in kv
    assert "st_now=" in kv


def test_scanner_pass_log_uses_expanded_form_end_to_end(monkeypatch, caplog):
    """End-to-end: drive the PASS-log code path with a real registered rule and
    assert the emitted INFO line carries the snapshot keys.

    Uses a custom rule whose ``__module__`` is set to the BUY rule's module so
    ``_resolve_eval_snapshot`` finds the helper.
    """
    _freeze_buy(monkeypatch)
    # Disable the market-hours gate and the completeness metric so the test
    # doesn't depend on wall-clock time and doesn't try to send Telegram.
    monkeypatch.setenv("SCANNER_POSTCLOSE_GATE_ENABLED", "false")
    monkeypatch.setenv("SCANNER_COMPLETENESS_ENABLED", "false")

    rule_name = "_test_205_pass_log_rule"

    @scanner_service.scan_rule(rule_name, "buy", "Test rule for #205 PASS-log expansion.")
    def _test_rule(bars, indicators):
        return buymod.rule(bars, indicators)

    # The decorator preserves the original function's __module__ — that's
    # this test module's name, which has no get_last_eval_snapshot helper.
    # Re-point to the BUY rule module so the resolver finds the helper.
    _test_rule.__module__ = buymod.__name__

    # Stub the scan-definitions reader so it returns a single definition
    # pointing at our registered rule.
    monkeypatch.setattr(
        scanner_service,
        "get_scan_definitions",
        lambda enabled_only=True: [
            {
                "id": 999_205,
                "rule_module": rule_name,
                "name": rule_name,
                "screener_type": "buy",
                "parameters_json": None,
            }
        ],
    )
    # Stub the DB write so the test doesn't need a DB.
    monkeypatch.setattr(scanner_service, "record_scan_result", lambda **kw: 1)

    # Build a minimal ScannerService and call the evaluate path directly.
    svc = scanner_service.ScannerService(symbols=["TCS"], intervals=["5m"])
    indicators = _happy_buy()
    indicators["symbol"] = "TCS"
    indicators["exchange"] = "NSE"

    bar = {"close": 2100.0, "open": 2050.0}
    with caplog.at_level(logging.INFO, logger=scanner_service.logger.name):
        svc._evaluate_definitions(
            symbol="TCS",
            interval="5m",
            bars=indicators["bars_5m"],
            indicators_dict=indicators,
            bar=bar,
        )

    pass_lines = [
        r for r in caplog.records if "scanner PASS" in r.getMessage() and "TCS" in r.getMessage()
    ]
    assert pass_lines, "scanner PASS line should have been emitted"
    msg = pass_lines[0].getMessage()
    for key in ("today_d_close=", "rsi_15m=", "weekly_atr=", "st_now="):
        assert key in msg, f"PASS log should include '{key}', got: {msg}"


# --------------------------------------------------------------------------- #
# Suppress unused-import warnings
# --------------------------------------------------------------------------- #
_ = SimpleNamespace
