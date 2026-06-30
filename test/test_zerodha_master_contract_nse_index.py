"""Unit tests for Zerodha master-contract NSE_INDEX normalisation (issue #241).

The Zerodha ``instruments`` CSV ships NSE sectoral indices with the broker
tradingsymbol carrying spaces (e.g. ``NIFTY OIL AND GAS``,
``NIFTY CONSR DURBL``). The legacy static-allowlist normaliser dropped any
index not explicitly mapped, so ``get_token('NIFTYOILANDGAS', 'NSE_INDEX')``
returned ``None`` and scanner_presubscribe / ws_recovery failed closed.

These tests exercise :func:`process_zerodha_csv` against a minimal in-memory
CSV that covers:

* The two formerly-absent indices from issue #241 (NIFTY OIL AND GAS,
  NIFTY CONSR DURBL) — Pass-2 space-stripping must catch them.
* An index that IS in the explicit allowlist (NIFTY 50) — Pass-1 mapping
  still wins.
* An NSE equity with a space-y ``tradingsymbol`` shouldn't be affected
  (Pass-2 is scoped to ``exchange == NSE_INDEX``).

The fixture does NOT touch the live db/openalgo.db; the function under test
operates purely on the input DataFrame and returns a transformed DataFrame.
"""

from __future__ import annotations

import os
import tempfile

import pandas as pd
import pytest

from broker.zerodha.database.master_contract_db import process_zerodha_csv

# Each of the 11 issue-241 sectoral indices and the expected OpenAlgo
# symbol (i.e. all spaces stripped from the Zerodha tradingsymbol).
_ISSUE_241_INDICES = [
    ("NIFTY AUTO", "NIFTYAUTO"),
    ("NIFTY REALTY", "NIFTYREALTY"),
    ("NIFTY PVT BANK", "NIFTYPVTBANK"),
    ("NIFTY PSU BANK", "NIFTYPSUBANK"),
    ("NIFTY PHARMA", "NIFTYPHARMA"),
    ("NIFTY OIL AND GAS", "NIFTYOILANDGAS"),
    ("NIFTY METAL", "NIFTYMETAL"),
    ("NIFTY IT", "NIFTYIT"),
    ("NIFTY FMCG", "NIFTYFMCG"),
    ("NIFTY CONSUMPTION", "NIFTYCONSUMPTION"),
    ("NIFTY CONSR DURBL", "NIFTYCONSRDURBL"),
]


def _make_zerodha_csv(rows: list[dict]) -> str:
    """Write a minimal Zerodha-instruments CSV to a tempfile and return path.

    Includes every column ``process_zerodha_csv`` accesses; the caller's
    ``rows`` overlays the index/equity-specific values.
    """
    base = {
        "instrument_token": 0,
        "exchange_token": 0,
        "tradingsymbol": "",
        "name": "",
        "last_price": 0.0,
        "expiry": "",
        "strike": 0.0,
        "tick_size": 0.05,
        "lot_size": 1,
        "instrument_type": "EQ",
        "segment": "INDICES",
        "exchange": "NSE",
    }
    merged = []
    for i, r in enumerate(rows):
        m = dict(base)
        m.update(r)
        m["instrument_token"] = m["instrument_token"] or (1000 + i)
        m["exchange_token"] = m["exchange_token"] or (2000 + i)
        merged.append(m)
    df = pd.DataFrame(merged)
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    df.to_csv(path, index=False)
    return path


@pytest.mark.parametrize("broker_tradingsymbol,openalgo_symbol", _ISSUE_241_INDICES)
def test_each_issue_241_index_resolves_to_canonical_openalgo_symbol(
    broker_tradingsymbol, openalgo_symbol, tmp_path
):
    """Every issue-241 sectoral index, ingested under segment=INDICES /
    exchange=NSE, must produce a row with ``exchange='NSE_INDEX'`` AND
    ``symbol`` equal to the canonical OpenAlgo space-stripped form.
    """
    path = _make_zerodha_csv(
        [
            {
                "tradingsymbol": broker_tradingsymbol,
                "name": broker_tradingsymbol,
                "segment": "INDICES",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
    )
    try:
        out = process_zerodha_csv(path)
    finally:
        os.remove(path)

    assert len(out) == 1
    row = out.iloc[0]
    assert row["exchange"] == "NSE_INDEX"
    assert row["symbol"] == openalgo_symbol
    # brsymbol retains the original spaces for compatibility.
    assert row["brsymbol"] == broker_tradingsymbol


def test_explicit_allowlist_mapping_still_wins_for_known_indices():
    """Pass-1 must still rename ``NIFTY 50 → NIFTY`` and ``NIFTY BANK →
    BANKNIFTY`` (these are NOT just space-stripped forms — they're
    operator-canonical short names).
    """
    path = _make_zerodha_csv(
        [
            {
                "tradingsymbol": "NIFTY 50",
                "name": "NIFTY 50",
                "segment": "INDICES",
                "exchange": "NSE",
                "instrument_type": "EQ",
            },
            {
                "tradingsymbol": "NIFTY BANK",
                "name": "NIFTY BANK",
                "segment": "INDICES",
                "exchange": "NSE",
                "instrument_type": "EQ",
            },
            {
                "tradingsymbol": "NIFTY MID SELECT",
                "name": "NIFTY MID SELECT",
                "segment": "INDICES",
                "exchange": "NSE",
                "instrument_type": "EQ",
            },
        ]
    )
    try:
        out = process_zerodha_csv(path)
    finally:
        os.remove(path)

    out = out.sort_values("brsymbol").reset_index(drop=True)
    assert list(out["exchange"]) == ["NSE_INDEX"] * 3
    sym_by_br = dict(zip(out["brsymbol"], out["symbol"], strict=False))
    assert sym_by_br["NIFTY 50"] == "NIFTY"
    assert sym_by_br["NIFTY BANK"] == "BANKNIFTY"
    assert sym_by_br["NIFTY MID SELECT"] == "MIDCPNIFTY"


def test_pass2_space_strip_scoped_to_nse_index_only():
    """The Pass-2 space-stripping must NOT affect NSE equities, even if (in
    some hypothetical world) their tradingsymbol contained spaces. The mask
    is ``exchange == NSE_INDEX``, so an EQ row with spaces in the
    tradingsymbol passes through unchanged.
    """
    path = _make_zerodha_csv(
        [
            {
                "tradingsymbol": "FAKE EQUITY",
                "name": "FAKE EQUITY",
                "segment": "NSE",
                "exchange": "NSE",
                "instrument_type": "EQ",
            }
        ]
    )
    try:
        out = process_zerodha_csv(path)
    finally:
        os.remove(path)

    assert len(out) == 1
    row = out.iloc[0]
    # NSE equity → ``exchange='NSE'``, symbol retains its spaces (pass-2
    # is scoped to NSE_INDEX).
    assert row["exchange"] == "NSE"
    assert row["symbol"] == "FAKE EQUITY"
