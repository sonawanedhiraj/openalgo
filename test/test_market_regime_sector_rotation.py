"""Tests for Stage 1.7 sector-rotation classifier.

Pin down the ``_classify_sector_rotation`` ranking + fallback + degrade
behaviour. All data sources are injected — no broker, duckdb, or
network calls.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pandas as pd
import pytest
import pytz

from services import market_regime_service as mrs

IST = pytz.timezone("Asia/Kolkata")


# ---------------------------------------------------------------------------
# _classify_sector_rotation — ranking + concentration
# ---------------------------------------------------------------------------


_TEN_SECTORS = [
    "NIFTYAUTO",
    "BANKNIFTY",
    "FINNIFTY",
    "NIFTYFMCG",
    "NIFTYIT",
    "NIFTYMETAL",
    "NIFTYPHARMA",
    "NIFTYREALTY",
    "NIFTYPSUBANK",
    "NIFTYCONSUMPTION",
]


def test_sector_rotation_ranks_by_pct_change():
    """Top-3 leaders are the 3 sectors with the highest % change."""
    quotes = {
        "NIFTYIT": 3.2,
        "NIFTYAUTO": 2.1,
        "NIFTYPHARMA": 1.5,
        "NIFTYFMCG": 0.8,
        "BANKNIFTY": 0.2,
        "FINNIFTY": -0.1,
        "NIFTYMETAL": -0.6,
        "NIFTYREALTY": -1.0,
        "NIFTYPSUBANK": -1.4,
        "NIFTYCONSUMPTION": -2.0,
    }
    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: _TEN_SECTORS,
        live_quote_fn=lambda sym: quotes.get(sym),
        historify_fn=lambda sym: None,
    )
    assert leaders == ["NIFTYIT", "NIFTYAUTO", "NIFTYPHARMA"]
    # concentration = (3.2 - median) / (3.2 + 0.01)
    # median index = 10 // 2 = 5 → 6th by descending rank → FINNIFTY = -0.1
    expected = max(0.0, 3.2 - (-0.1)) / (3.2 + 0.01)
    assert concentration == pytest.approx(expected, rel=1e-6)
    assert raw["live_count"] == 10
    assert raw["historify_count"] == 0
    assert raw["missing_count"] == 0
    assert raw["sector_pct"]["NIFTYIT"] == 3.2


def test_sector_rotation_falls_back_to_historify_when_live_fails():
    """When live_quote_fn returns None, historify_fn should be tried."""

    def live(sym):
        # Only IT has a live quote; the rest fall through.
        return 1.5 if sym == "NIFTYIT" else None

    def hist(sym):
        # Historify supplies everything else with a small negative drift.
        return -0.3

    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: _TEN_SECTORS,
        live_quote_fn=live,
        historify_fn=hist,
    )
    # IT (live, +1.5) wins; the rest tie at -0.3, top-3 includes any 2 of them.
    assert leaders[0] == "NIFTYIT"
    assert len(leaders) == 3
    assert raw["live_count"] == 1
    assert raw["historify_count"] == 9
    assert raw["source_per_symbol"]["NIFTYIT"] == "live"
    assert raw["source_per_symbol"]["BANKNIFTY"] == "historify"


def test_sector_rotation_returns_empty_when_no_data_source_works():
    """Both loaders return None for every symbol → empty leaders + 0.0."""
    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: _TEN_SECTORS,
        live_quote_fn=lambda sym: None,
        historify_fn=lambda sym: None,
    )
    assert leaders == []
    assert concentration == 0.0
    assert raw["missing_count"] == 10
    assert raw.get("reason") == "no_data"


def test_sector_rotation_empty_universe_degrades_gracefully():
    """REGIME_SECTOR_SYMBOLS unset (empty list) is not an error."""
    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: [],
        live_quote_fn=lambda sym: 1.0,
        historify_fn=lambda sym: None,
    )
    assert leaders == []
    assert concentration == 0.0
    assert raw["reason"] == "empty_universe"


def test_sector_rotation_concentration_dominant_leader():
    """A clear top-of-the-pack leader produces concentration > 0.5."""

    # IT +5%, others clustered near 0
    def live(sym):
        return 5.0 if sym == "NIFTYIT" else 0.1

    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: _TEN_SECTORS,
        live_quote_fn=live,
        historify_fn=lambda sym: None,
    )
    assert leaders[0] == "NIFTYIT"
    assert concentration > 0.5


def test_sector_rotation_concentration_broad_rotation():
    """When everyone moves together, concentration approaches 0."""

    def live(sym):
        return 0.9  # every sector up 0.9% — pure broad rotation

    leaders, concentration, raw = mrs._classify_sector_rotation(
        symbols_loader=lambda: _TEN_SECTORS,
        live_quote_fn=live,
        historify_fn=lambda sym: None,
    )
    # top == median ⇒ numerator clamped to 0
    assert concentration == 0.0


def test_default_sector_symbols_reads_env(monkeypatch):
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", " niftyit , niftyauto ,banknifty ")
    assert mrs._default_sector_symbols() == ["NIFTYIT", "NIFTYAUTO", "BANKNIFTY"]

    monkeypatch.delenv("REGIME_SECTOR_SYMBOLS", raising=False)
    assert mrs._default_sector_symbols() == []


# ---------------------------------------------------------------------------
# compute_current_regime — end-to-end with sector data
# ---------------------------------------------------------------------------


def _fake_nifty_ohlcv(n: int = 80) -> pd.DataFrame:
    closes = [20000 + i * 50 for i in range(n)]
    return pd.DataFrame({"close": closes})


def test_compute_current_regime_populates_sector_fields(monkeypatch):
    """End-to-end: configure sectors via env, mock live quotes, verify
    the MarketRegime is populated with real leaders + concentration."""
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", ",".join(_TEN_SECTORS))
    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda *a, **kw: _fake_nifty_ohlcv(),
    )

    quotes = {
        "NIFTYIT": 2.5,
        "NIFTYAUTO": 1.8,
        "NIFTYPHARMA": 1.2,
    }

    def fake_live(sym):
        return quotes.get(sym)  # None for the rest → fall through

    monkeypatch.setattr(mrs, "_live_sector_quote_pct", fake_live)
    monkeypatch.setattr(mrs, "_historify_sector_pct", lambda sym: -0.5)

    regime = mrs.compute_current_regime(
        now=IST.localize(datetime(2026, 6, 1, 10, 30)),
    )

    assert regime.sector_leaders[0] == "NIFTYIT"
    assert "NIFTYAUTO" in regime.sector_leaders
    assert "NIFTYPHARMA" in regime.sector_leaders
    assert regime.sector_leader_concentration > 0.0
    sector_raw = regime.raw_metrics["sector_rotation"]
    assert sector_raw["live_count"] == 3
    assert sector_raw["historify_count"] == 7
    assert sector_raw["sector_pct"]["NIFTYIT"] == 2.5


# ---------------------------------------------------------------------------
# Live + historify fetch helpers — verify graceful failure
# ---------------------------------------------------------------------------


def test_live_sector_quote_returns_none_when_no_api_key(monkeypatch):
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: None)
    assert mrs._live_sector_quote_pct("NIFTYIT") is None


def test_live_sector_quote_returns_none_when_prev_close_zero(monkeypatch):
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: "key")
    monkeypatch.setattr(
        "services.quotes_service.get_quotes",
        lambda **kwargs: (True, {"data": {"ltp": 100.0, "prev_close": 0.0}}, 200),
    )
    assert mrs._live_sector_quote_pct("NIFTYIT") is None


def test_live_sector_quote_returns_pct_on_success(monkeypatch):
    monkeypatch.setattr("database.auth_db.get_first_available_api_key", lambda: "key")
    monkeypatch.setattr(
        "services.quotes_service.get_quotes",
        lambda **kwargs: (
            True,
            {"data": {"ltp": 102.0, "prev_close": 100.0}},
            200,
        ),
    )
    assert mrs._live_sector_quote_pct("NIFTYIT") == pytest.approx(2.0)


def test_historify_sector_pct_needs_two_bars(monkeypatch):
    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda *a, **kw: pd.DataFrame({"close": [100.0]}),
    )
    assert mrs._historify_sector_pct("NIFTYIT") is None


def test_historify_sector_pct_computes_close_to_close(monkeypatch):
    monkeypatch.setattr(
        "database.historify_db.get_ohlcv",
        lambda *a, **kw: pd.DataFrame({"close": [100.0, 101.0]}),
    )
    assert mrs._historify_sector_pct("NIFTYIT") == pytest.approx(1.0)
