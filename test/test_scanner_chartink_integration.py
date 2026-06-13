"""End-to-end wiring test: ScannerHistoryProvider → ScannerService.

Validates that the dict produced by ``ScannerService._build_indicators``
matches exactly what ``fno_intraday_buy_chartink.rule`` reads. Task 6 covers
the rule gate-by-gate with hand-built dicts; this complements it by exercising
the real ``_build_indicators`` assembly path with a mocked history provider and
directly-populated per-symbol 15m buffers.

Approach is the pragmatic "pure-dict" path: construct a real ``ScannerService``,
swap in a mock ``_history_provider`` and stub the per-symbol 15m builders, then
call ``_build_indicators(symbol, bars_5m)`` and feed the result straight to the
rule. The full tick→aggregator path is exercised elsewhere
(``test_scanner_service.py``); here we isolate the indicator-bundle contract.
"""

from __future__ import annotations

# Reuse the happy/short synthetic frame builders from the Task 6 suite. The
# test dir has no __init__, so load the sibling module by path rather than
# relying on a particular pytest import mode.
import importlib.util as _ilu
import os as _os
from datetime import datetime as _RealDateTime
from unittest.mock import MagicMock, patch

import pytest

import services.scan_rules.fno_intraday_buy_chartink as rulemod
from services.scan_rules.fno_intraday_buy_chartink import rule
from services.scanner_service import ScannerService

_bld_path = _os.path.join(_os.path.dirname(__file__), "test_fno_intraday_buy_chartink.py")
_spec = _ilu.spec_from_file_location("_chartink_builders", _bld_path)
_bld = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_bld)
make_15m_bars = _bld.make_15m_bars
make_5m_bars = _bld.make_5m_bars
make_daily_bars = _bld.make_daily_bars
make_weekly_bars = _bld.make_weekly_bars


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(autouse=True)
def _freeze_post_close(monkeypatch):
    """Pin ``rulemod.datetime.now`` to 16:00 IST (post-15:31 → iloc[-1]/[-2])."""

    class _FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            naive = _RealDateTime(2026, 6, 4, 16, 0)
            return tz.localize(naive) if tz is not None else naive

    monkeypatch.setattr(rulemod, "datetime", _FrozenDateTime)


def _make_service(symbols, provider=None):
    """Construct a ScannerService without touching the real history provider.

    ``get_provider`` is patched during construction so __init__ never reaches
    the DB-backed singleton; the returned service then has its provider swapped
    for the supplied mock (or a default happy mock).
    """
    if provider is None:
        provider = MagicMock()
        provider.get_daily.return_value = make_daily_bars()
        provider.get_weekly.return_value = make_weekly_bars()
    with patch("services.scanner_history_provider.get_provider", return_value=provider):
        svc = ScannerService(symbols=symbols)
    svc._history_provider = provider
    return svc


def _stub_15m(svc, symbol, frame=None):
    """Replace the per-symbol 15m builder with a stub returning ``frame``."""
    builder = MagicMock()
    builder.get_recent_bars.return_value = frame if frame is not None else make_15m_bars()
    svc._bar_15m_history[symbol] = builder


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
def test_build_indicators_returns_4_new_keys():
    svc = _make_service(["RELIANCE"])
    _stub_15m(svc, "RELIANCE")
    ind = svc._build_indicators("RELIANCE", make_5m_bars())
    for key in ("bars_5m", "bars_15m", "bars_daily", "bars_weekly"):
        assert key in ind, f"missing key {key}"
    # Backward-compat 5m-derived keys still present (rule ignores them).
    for key in ("ema_20", "atr_14", "rsi_14", "volume_avg_20"):
        assert key in ind


def test_provider_daily_flows_through():
    daily = make_daily_bars()
    weekly = make_weekly_bars()
    provider = MagicMock()
    provider.get_daily.return_value = daily
    provider.get_weekly.return_value = weekly
    svc = _make_service(["RELIANCE"], provider=provider)
    _stub_15m(svc, "RELIANCE")

    ind = svc._build_indicators("RELIANCE", make_5m_bars())
    assert ind["bars_daily"] is daily
    assert ind["bars_weekly"] is weekly
    provider.get_daily.assert_called_once_with("RELIANCE")


def test_provider_none_makes_rule_reject():
    provider = MagicMock()
    provider.get_daily.return_value = None
    provider.get_weekly.return_value = make_weekly_bars()
    svc = _make_service(["RELIANCE"], provider=provider)
    _stub_15m(svc, "RELIANCE")

    bars_5m = make_5m_bars()
    ind = svc._build_indicators("RELIANCE", bars_5m)
    assert ind["bars_daily"] is None
    assert rule(bars_5m, ind) is False


def test_rule_passes_with_full_happy_path():
    svc = _make_service(["RELIANCE"])  # default happy daily/weekly
    _stub_15m(svc, "RELIANCE", make_15m_bars())  # happy rising 15m → RSI high

    bars_5m = make_5m_bars()  # rising closes + volume spike
    ind = svc._build_indicators("RELIANCE", bars_5m)
    assert rule(bars_5m, ind) is True


def test_rule_rejects_with_short_daily():
    provider = MagicMock()
    provider.get_daily.return_value = make_daily_bars(n=50)  # < 200 warm-up
    provider.get_weekly.return_value = make_weekly_bars()
    svc = _make_service(["RELIANCE"], provider=provider)
    _stub_15m(svc, "RELIANCE")

    bars_5m = make_5m_bars()
    ind = svc._build_indicators("RELIANCE", bars_5m)
    assert len(ind["bars_daily"]) == 50
    assert rule(bars_5m, ind) is False


def test_two_symbols_no_cross_contamination():
    rel_daily = make_daily_bars(today_close=2100.0)
    inf_daily = make_daily_bars(today_close=3300.0)
    provider = MagicMock()
    provider.get_daily.side_effect = lambda s: {
        "RELIANCE": rel_daily,
        "INFY": inf_daily,
    }[s]
    provider.get_weekly.side_effect = lambda s: make_weekly_bars()

    svc = _make_service(["RELIANCE", "INFY"], provider=provider)
    _stub_15m(svc, "RELIANCE")
    _stub_15m(svc, "INFY")

    ind_rel = svc._build_indicators("RELIANCE", make_5m_bars())
    ind_inf = svc._build_indicators("INFY", make_5m_bars())

    assert ind_rel["bars_daily"] is rel_daily
    assert ind_inf["bars_daily"] is inf_daily
    assert ind_rel["bars_daily"]["close"].iloc[-1] == 2100.0
    assert ind_inf["bars_daily"]["close"].iloc[-1] == 3300.0


def test_provider_exception_is_swallowed_keys_none():
    """A provider lookup blowing up must not break _build_indicators."""
    provider = MagicMock()
    provider.get_daily.side_effect = RuntimeError("boom")
    svc = _make_service(["RELIANCE"], provider=provider)
    _stub_15m(svc, "RELIANCE")

    ind = svc._build_indicators("RELIANCE", make_5m_bars())
    # Exception is caught in _build_indicators → both frames left as None.
    assert ind["bars_daily"] is None
    assert ind["bars_weekly"] is None
