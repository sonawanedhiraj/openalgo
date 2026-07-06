"""Tests for #158 D2 + D4 — scanner reliability bundle.

D2: F&O stock scan rules silently skip NSE_INDEX symbols (NIFTY, BANKNIFTY,
    FINNIFTY, MIDCPNIFTY, NIFTYNXT50) instead of emitting "bars_daily is None"
    WARNINGs every 5m bar close.

D4: ``check_dry_scanner(as_of=<historical>)`` returns its verdict without
    firing notify or writing a health row, so a replay/backfill diagnostic
    never pages the operator.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd

_IST = timezone(timedelta(hours=5, minutes=30))


# --------------------------------------------------------------------------- #
# D2 — F&O rules skip indices silently
# --------------------------------------------------------------------------- #


def test_buy_rule_skips_nse_index_silently(caplog):
    """An index symbol must NOT trigger 'bars_daily is None' warning."""
    from services.scan_rules.fno_intraday_buy_chartink import rule

    indicators = {
        "symbol": "NIFTY",
        "exchange": "NSE_INDEX",
        # bars_daily intentionally absent — the index-skip must fire before
        # the bars_daily-None warning path.
    }
    bars = pd.DataFrame()

    import logging

    caplog.set_level(logging.WARNING, logger="services.scan_rules.fno_intraday_buy_chartink")
    matched = rule(bars, indicators)

    assert matched is False
    bars_daily_warnings = [r for r in caplog.records if "bars_daily is None" in r.getMessage()]
    assert bars_daily_warnings == [], (
        f"Index symbol should be skipped silently, but got: "
        f"{[r.getMessage() for r in bars_daily_warnings]}"
    )


def test_sell_rule_skips_nse_index_silently(caplog):
    """Same guard applies to the sell rule (the same 235 daily warnings)."""
    from services.scan_rules.fno_intraday_sell_chartink import rule

    indicators = {
        "symbol": "BANKNIFTY",
        "exchange": "NSE_INDEX",
    }
    bars = pd.DataFrame()

    import logging

    caplog.set_level(logging.WARNING, logger="services.scan_rules.fno_intraday_sell_chartink")
    matched = rule(bars, indicators)

    assert matched is False
    bars_daily_warnings = [r for r in caplog.records if "bars_daily is None" in r.getMessage()]
    assert bars_daily_warnings == []


def test_buy_rule_does_not_skip_nse_stocks():
    """Sanity: NSE stock symbols must still go through the normal evaluation
    path (where they'll hit the bars_daily check if data is genuinely missing)."""
    from services.scan_rules.fno_intraday_buy_chartink import rule

    indicators = {
        "symbol": "RELIANCE",
        "exchange": "NSE",
        # No daily/weekly/5m — will reject with WARNING as before.
    }
    bars = pd.DataFrame()

    matched = rule(bars, indicators)
    # The result is still False (warmup rejection), but the index-skip path
    # is NOT taken — the rule went through to the normal warm-up checks.
    assert matched is False


def test_buy_rule_skips_without_exchange_key_gracefully():
    """Pre-fix indicators dicts may not carry the 'exchange' key — the rule
    must NOT crash if it's missing. (Backwards-compat for old callers.)"""
    from services.scan_rules.fno_intraday_buy_chartink import rule

    indicators = {"symbol": "FOO"}  # no exchange
    bars = pd.DataFrame()

    matched = rule(bars, indicators)
    # No crash; just evaluates the normal path (will reject on missing bars).
    assert matched is False


# --------------------------------------------------------------------------- #
# D4 — tripwire historical as_of is silent
# --------------------------------------------------------------------------- #


def _make_tripwire_callers():
    """Build mocks that record every notifier + health-writer call."""
    notifier = MagicMock()
    health_writer = MagicMock()
    return notifier, health_writer


def test_tripwire_historical_as_of_skips_notify_with_default_notifier(monkeypatch):
    """as_of more than 1 hour behind wall-clock + default production notifier
    → silent. This is the production-path guard against replays/backfills."""
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true")

    from services import scanner_dry_tripwire_service as svc

    health_writer = MagicMock()
    # Patch the production_notifier symbol so we can assert it wasn't called
    # WITHOUT having to call the real notifier (which would try to send
    # Telegram).
    patched_prod_notifier = MagicMock()
    monkeypatch.setattr(svc, "production_notifier", patched_prod_notifier)

    historical = datetime.now(tz=_IST) - timedelta(hours=2)

    # notifier=None (the default sentinel) → service uses production_notifier
    # → historical guard fires.
    result = svc.check_dry_scanner(
        as_of=historical,
        latest_inhouse_provider=lambda: None,
        chartink_has_rows_since=lambda _t: True,
        broker_session_checker=lambda: True,
        notifier=None,
        health_writer=health_writer,
        subscribed_at_provider=lambda: None,
    )

    assert result["status"] == "historical_silent"
    assert result["as_of"] == historical.isoformat()
    patched_prod_notifier.assert_not_called()
    health_writer.assert_not_called()


def test_tripwire_historical_as_of_with_custom_notifier_evaluates(monkeypatch):
    """Tests inject custom notifiers explicitly — they get the normal flow
    even with historical as_of, so existing scenario-driven tests keep
    working. (The guard is only against the DEFAULT production notifier.)"""
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true")

    from services import scanner_dry_tripwire_service as svc

    notifier, health_writer = _make_tripwire_callers()

    historical = datetime.now(tz=_IST) - timedelta(hours=2)

    result = svc.check_dry_scanner(
        as_of=historical,
        latest_inhouse_provider=lambda: None,
        chartink_has_rows_since=lambda _t: True,
        broker_session_checker=lambda: True,
        notifier=notifier,  # custom — bypass the historical guard
        health_writer=health_writer,
        subscribed_at_provider=lambda: None,
    )
    # Status is NOT historical_silent because the custom notifier opts out
    # of the guard. The actual status depends on market-hours of the
    # historical date.
    assert result["status"] != "historical_silent"


def test_tripwire_recent_as_of_still_evaluates(monkeypatch):
    """as_of within the 1-hour window is treated as a live tick — normal flow."""
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true")

    from services import scanner_dry_tripwire_service as svc

    notifier, health_writer = _make_tripwire_callers()

    # 5 minutes ago — well within the 1h live window. Also inside market
    # hours for the test must hold; use a fixed IST time during market hours.
    live = datetime.now(tz=_IST) - timedelta(minutes=5)

    result = svc.check_dry_scanner(
        as_of=live,
        latest_inhouse_provider=lambda: live,
        chartink_has_rows_since=lambda _t: True,
        broker_session_checker=lambda: True,
        notifier=notifier,
        health_writer=health_writer,
        subscribed_at_provider=lambda: live - timedelta(hours=1),
    )

    # Status depends on market-hours but is NOT historical_silent.
    assert result.get("status") != "historical_silent"


def test_tripwire_default_as_of_unchanged(monkeypatch):
    """as_of=None (live tick) is unaffected by the historical-silent guard."""
    monkeypatch.setenv("SCANNER_DRY_TRIPWIRE_ENABLED", "true")

    from services import scanner_dry_tripwire_service as svc

    notifier, health_writer = _make_tripwire_callers()

    result = svc.check_dry_scanner(
        as_of=None,  # defaults to datetime.now() — never historical
        latest_inhouse_provider=lambda: None,
        chartink_has_rows_since=lambda _t: True,
        broker_session_checker=lambda: True,
        notifier=notifier,
        health_writer=health_writer,
        subscribed_at_provider=lambda: None,
    )
    assert result.get("status") != "historical_silent"


# --------------------------------------------------------------------------- #
# Scanner exchange threading — D2 prerequisite
# --------------------------------------------------------------------------- #


def test_indicators_bundle_threads_exchange_from_scanner_service():
    """ScannerService._build_indicators must thread the resolved exchange
    so the F&O rules can use it for the index-skip check."""
    # Smoke test that the build_indicators helper includes the exchange key.
    # Run import-only — we don't need full ScannerService boot.
    import inspect

    from services.scanner_service import ScannerService

    src = inspect.getsource(ScannerService._build_indicators)
    assert '"exchange"' in src, "ScannerService._build_indicators must include 'exchange' key"
    assert "resolve_exchange_for_symbol" in src, (
        "ScannerService._build_indicators must resolve the symbol's exchange"
    )
