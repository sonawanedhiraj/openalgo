"""Tests for the post-master-contract-sync NSE_INDEX verification hook
(issue #241).

After ``load_symbols_to_cache`` repopulates the in-memory symbol cache, the
hook calls ``_verify_canonical_nse_index_symbols`` and logs a single
WARNING listing any canonical NSE_INDEX symbol that did not resolve to a
token. The verification is observational (read-only) — it does not gate
downstream services, but it makes the issue #241 class of regression
visible at the next OpenAlgo restart instead of biting only at 15:20 IST
when sector_follow's smoke check fails closed.
"""

from __future__ import annotations

import logging
from unittest.mock import patch

from database import master_contract_cache_hook as hook


def _patched_get_token(missing_set):
    """Return a fake ``get_token`` that returns ``None`` for any symbol in
    ``missing_set`` and a deterministic non-empty token for everything else.
    """

    def _impl(symbol: str, exchange: str) -> str | None:
        if exchange != "NSE_INDEX":
            return None
        if symbol in missing_set:
            return None
        return f"tok:{symbol}"

    return _impl


def test_verify_canonical_returns_empty_when_all_present():
    """Happy path: every canonical NSE_INDEX symbol resolves → empty list."""
    with patch.object(hook, "_REQUIRED_NSE_INDEX_SYMBOLS", ("NIFTY", "NIFTYAUTO")):
        with patch(
            "database.token_db_enhanced.get_token",
            side_effect=_patched_get_token(missing_set=set()),
        ):
            missing = hook._verify_canonical_nse_index_symbols(broker="zerodha")
    assert missing == []


def test_verify_canonical_lists_unresolved_symbols():
    """Failure path: a missing symbol is returned in the list, in input order."""
    required = ("NIFTY", "NIFTYAUTO", "NIFTYOILANDGAS", "NIFTYCONSRDURBL")
    with patch.object(hook, "_REQUIRED_NSE_INDEX_SYMBOLS", required):
        with patch(
            "database.token_db_enhanced.get_token",
            side_effect=_patched_get_token(missing_set={"NIFTYOILANDGAS", "NIFTYCONSRDURBL"}),
        ):
            missing = hook._verify_canonical_nse_index_symbols(broker="zerodha")
    assert missing == ["NIFTYOILANDGAS", "NIFTYCONSRDURBL"]


def test_verify_canonical_treats_raising_lookup_as_a_miss():
    """A token lookup that raises is treated the same as one that returns None
    (the downstream consumer would treat both as a miss). The verification
    must not propagate the exception.
    """

    def _raising_get_token(symbol: str, exchange: str) -> str | None:
        if symbol == "NIFTYAUTO":
            raise RuntimeError("synthetic")
        return f"tok:{symbol}"

    with patch.object(hook, "_REQUIRED_NSE_INDEX_SYMBOLS", ("NIFTY", "NIFTYAUTO")):
        with patch("database.token_db_enhanced.get_token", side_effect=_raising_get_token):
            missing = hook._verify_canonical_nse_index_symbols(broker="zerodha")
    assert missing == ["NIFTYAUTO"]


def test_verify_canonical_set_covers_issue_241_eleven():
    """The canonical required set must include all 11 issue #241 symbols (so
    a regression on any one of them surfaces a post-sync WARNING).
    """
    expected = {
        "NIFTYAUTO",
        "NIFTYREALTY",
        "NIFTYPVTBANK",
        "NIFTYPSUBANK",
        "NIFTYPHARMA",
        "NIFTYOILANDGAS",
        "NIFTYMETAL",
        "NIFTYIT",
        "NIFTYFMCG",
        "NIFTYCONSUMPTION",
        "NIFTYCONSRDURBL",
    }
    assert expected.issubset(set(hook._REQUIRED_NSE_INDEX_SYMBOLS)), (
        "issue #241 sectoral indices missing from canonical NSE_INDEX set: "
        f"{expected - set(hook._REQUIRED_NSE_INDEX_SYMBOLS)}"
    )


def test_load_symbols_logs_warning_when_canonical_missing(caplog):
    """End-to-end (with cache + socketio mocked): when verification finds
    missing symbols, ``load_symbols_to_cache`` emits a single WARNING listing
    them so the operator sees it on the next restart.
    """
    with patch.object(hook, "_REQUIRED_NSE_INDEX_SYMBOLS", ("NIFTY", "NIFTYAUTO")):
        with (
            patch("database.token_db_enhanced.load_cache_for_broker", return_value=True),
            patch(
                "database.token_db_enhanced.get_cache_stats",
                return_value={
                    "total_symbols": 12345,
                    "stats": {"memory_usage_mb": 1.0},
                },
            ),
            patch(
                "database.token_db_enhanced.get_token",
                side_effect=_patched_get_token(missing_set={"NIFTYAUTO"}),
            ),
            patch.object(hook.socketio, "emit"),
        ):
            caplog.set_level(logging.WARNING, logger=hook.logger.name)
            ok = hook.load_symbols_to_cache("zerodha")
    assert ok is True
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warning_records, "expected at least one WARNING when canonicals are missing"
    msg = warning_records[0].getMessage()
    assert "NIFTYAUTO" in msg
    assert "zerodha" in msg


def test_load_symbols_no_warning_when_all_canonical_present(caplog):
    """End-to-end: a fully-good sync logs no canonical-missing WARNING."""
    with patch.object(hook, "_REQUIRED_NSE_INDEX_SYMBOLS", ("NIFTY", "NIFTYAUTO")):
        with (
            patch("database.token_db_enhanced.load_cache_for_broker", return_value=True),
            patch(
                "database.token_db_enhanced.get_cache_stats",
                return_value={
                    "total_symbols": 12345,
                    "stats": {"memory_usage_mb": 1.0},
                },
            ),
            patch(
                "database.token_db_enhanced.get_token",
                side_effect=_patched_get_token(missing_set=set()),
            ),
            patch.object(hook.socketio, "emit"),
        ):
            caplog.set_level(logging.WARNING, logger=hook.logger.name)
            hook.load_symbols_to_cache("zerodha")
    canonical_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "canonical NSE_INDEX" in r.getMessage()
    ]
    assert canonical_warnings == [], (
        "no canonical-missing WARNING expected when every symbol resolves"
    )
