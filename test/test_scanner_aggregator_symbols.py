"""Tests for ``services.scanner_aggregator_symbols`` (issue #161).

The function that unions every symbol source the scanner aggregator must
track. Today's 2026-06-26 15:20 IST sector_follow LIVE-with-0-orders bug
was caused by sector_follow's mapped indices not being in the aggregator;
this function is the fix.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from services.scanner_aggregator_symbols import compute_aggregator_symbols

# --------------------------------------------------------------------------- #
# Union behaviour — the core of the fix
# --------------------------------------------------------------------------- #


def test_union_includes_sector_follow_indices_not_in_scanner_symbols(monkeypatch):
    """The today's-failure regression: sector_follow's 6 unique sector
    indices (NIFTYAUTO/FMCG/IT/METAL/PSUBANK/PVTBANK) must end up in the
    aggregator universe even when they're not in SCANNER_SYMBOLS."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN,NIFTY,BANKNIFTY")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[
            "NIFTY",
            "BANKNIFTY",
            "NIFTYAUTO",
            "NIFTYFMCG",
            "NIFTYIT",
            "NIFTYMETAL",
            "NIFTYPSUBANK",
            "NIFTYPVTBANK",
        ],
    ):
        result = compute_aggregator_symbols()

    assert set(result) == {
        "RELIANCE",
        "SBIN",
        "NIFTY",
        "BANKNIFTY",
        "NIFTYAUTO",
        "NIFTYFMCG",
        "NIFTYIT",
        "NIFTYMETAL",
        "NIFTYPSUBANK",
        "NIFTYPVTBANK",
    }


def test_union_includes_regime_sector_symbols(monkeypatch):
    """REGIME_SECTOR_SYMBOLS (e.g. NIFTYPHARMA) should contribute to the
    aggregator universe."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE")
    monkeypatch.setenv(
        "REGIME_SECTOR_SYMBOLS",
        "NIFTYAUTO,NIFTYPHARMA,NIFTYREALTY",
    )
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        result = compute_aggregator_symbols()

    assert "NIFTYPHARMA" in result
    assert "NIFTYREALTY" in result
    assert "NIFTYAUTO" in result


def test_deduplication_across_sources(monkeypatch):
    """The same symbol in multiple sources appears once."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "NIFTY,BANKNIFTY,RELIANCE")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "NIFTY,NIFTYAUTO")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=["NIFTY", "BANKNIFTY", "NIFTYAUTO"],
    ):
        result = compute_aggregator_symbols()

    assert result.count("NIFTY") == 1
    assert result.count("BANKNIFTY") == 1
    assert result.count("NIFTYAUTO") == 1


def test_output_is_sorted(monkeypatch):
    """Sorted output keeps boot log diffs readable across restarts."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN,ABB")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        result = compute_aggregator_symbols()
    assert result == sorted(result)


def test_uppercases_symbols(monkeypatch):
    """Symbols are normalised to uppercase so case-mismatched env values
    don't create duplicate aggregator builders."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "reliance,Sbin")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        result = compute_aggregator_symbols()
    assert "RELIANCE" in result
    assert "SBIN" in result
    assert "reliance" not in result


# --------------------------------------------------------------------------- #
# Edge cases / fail-safe
# --------------------------------------------------------------------------- #


def test_empty_inputs_returns_empty_list(monkeypatch):
    """All sources empty → empty list (caller logs the 'will idle' warning)."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        result = compute_aggregator_symbols()
    assert result == []


def test_sector_index_symbols_exception_contributes_empty_set(monkeypatch):
    """A broken sector_map.json must NOT break aggregator construction —
    that source contributes [] with an exception log, the others still
    flow through."""
    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "NIFTYAUTO")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        side_effect=RuntimeError("sector_map.json missing"),
    ):
        result = compute_aggregator_symbols()
    assert set(result) == {"RELIANCE", "SBIN", "NIFTYAUTO"}


def test_whitespace_in_env_is_stripped(monkeypatch):
    monkeypatch.setenv("SCANNER_SYMBOLS", "  RELIANCE , SBIN  ,  ,  TCS")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "")
    with patch(
        "services.sector_follow_index_backfill.sector_index_symbols",
        return_value=[],
    ):
        result = compute_aggregator_symbols()
    assert set(result) == {"RELIANCE", "SBIN", "TCS"}


def test_logs_per_source_count(monkeypatch, caplog):
    """The boot log lets the operator confirm at a glance which strategy's
    indices made it in — the diagnostic this would have surfaced today."""
    import logging

    monkeypatch.setenv("SCANNER_SYMBOLS", "RELIANCE,SBIN")
    monkeypatch.setenv("REGIME_SECTOR_SYMBOLS", "NIFTYAUTO,NIFTYFMCG")
    with (
        patch(
            "services.sector_follow_index_backfill.sector_index_symbols",
            return_value=["NIFTY", "NIFTYAUTO"],  # one overlap with REGIME
        ),
        caplog.at_level(logging.INFO, logger="services.scanner_aggregator_symbols"),
    ):
        compute_aggregator_symbols()

    log_text = " ".join(r.message for r in caplog.records)
    assert "SCANNER_SYMBOLS=2" in log_text
    assert "REGIME_SECTOR_SYMBOLS=2" in log_text
    assert "sector_follow=2" in log_text
    # 2+2+2 = 6 inputs, 5 unique (NIFTYAUTO overlaps REGIME + sector_follow) = 1 drop
    assert "dedup-drop=1" in log_text
