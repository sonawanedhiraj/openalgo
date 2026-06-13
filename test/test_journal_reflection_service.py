"""Tests for the Stage 2 part 2 journal reflection service.

Covers the parts that don't need a live LLM:

* :func:`gather_reflection_inputs` returns the expected counts given seeded
  data on all three sub-sources.
* :func:`render_reflection_prompt` includes the verbatim backtest caveat.
* :func:`run_reflection` persists a row with the right fields when the bridge
  call is monkeypatched.

The bridge HTTP call is mocked — we do NOT touch the real bridge from tests.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker


@pytest.fixture
def fresh_reflection_db(monkeypatch):
    """Point journal_reflection_db at a fresh in-memory SQLite for one test."""
    from database import journal_reflection_db as jrdb

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
    )
    test_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=test_engine))
    monkeypatch.setattr(jrdb, "engine", test_engine)
    monkeypatch.setattr(jrdb, "db_session", test_session)
    jrdb.Base.metadata.create_all(test_engine)

    yield jrdb

    test_session.remove()
    test_engine.dispose()


# ---------------------------------------------------------------------------
# gather_reflection_inputs
# ---------------------------------------------------------------------------


def test_gather_reflection_inputs_returns_expected_counts(monkeypatch):
    """With each sub-source patched to return a known list, the counts
    field must reflect the lengths exactly."""
    from services import journal_reflection_service as jrs

    fake_journal = [
        {"id": 1, "symbol": "INFY", "direction": "LONG"},
        {"id": 2, "symbol": "TCS", "direction": "SHORT"},
    ]
    fake_screener = [
        {"id": 10, "symbols": ["INFY"], "source": "chartink"},
        {"id": 11, "symbols": ["TCS"], "source": "chartink"},
        {"id": 12, "symbols": ["WIPRO"], "source": "inhouse"},
    ]
    fake_backtest = [
        {"id": 100, "symbol": "INFY", "direction": "LONG"},
    ]

    monkeypatch.setattr(jrs, "_safe_journal_trades", lambda window_hours: fake_journal)
    monkeypatch.setattr(jrs, "_safe_screener_hits", lambda window_hours: fake_screener)
    monkeypatch.setattr(jrs, "_safe_backtest_trades", lambda window_days: fake_backtest)

    inputs = jrs.gather_reflection_inputs(date="2026-06-01", window_days=7)

    assert inputs["counts"]["n_journal_trades"] == 2
    assert inputs["counts"]["n_screener_hits"] == 3
    assert inputs["counts"]["n_backtest_trades"] == 1
    assert inputs["reflection_date"] == "2026-06-01"
    assert inputs["window_days"] == 7
    assert inputs["journal_trades"] == fake_journal
    assert inputs["screener_hits"] == fake_screener
    assert inputs["backtest_trades"] == fake_backtest


# ---------------------------------------------------------------------------
# render_reflection_prompt
# ---------------------------------------------------------------------------


def test_render_reflection_prompt_includes_backtest_caveat_verbatim():
    """The all-symbol methodology caveat must appear verbatim in the rendered
    prompt — the LLM relies on this to avoid quoting backtest P&L as live
    expectations."""
    from services import journal_reflection_service as jrs

    inputs = {
        "reflection_date": "2026-06-01",
        "window_days": 7,
        "journal_trades": [],
        "screener_hits": [],
        "backtest_trades": [],
        "counts": {"n_journal_trades": 0, "n_screener_hits": 0, "n_backtest_trades": 0},
    }
    prompt = jrs.render_reflection_prompt(inputs)

    assert jrs.BACKTEST_CAVEAT in prompt
    # Spot-check the exact key phrases the operator relied on in the task
    # description so an accidental rewording doesn't slip through.
    assert "ALL-SYMBOL methodology" in prompt
    assert "screener-filtered" in prompt
    assert "directionally only" in prompt
    # And that the section markers are present.
    assert "TRADE_JOURNAL" in prompt
    assert "SCAN_RESULTS" in prompt
    assert "BACKTEST_TRADES" in prompt


def test_render_reflection_prompt_swaps_caveat_for_screener_filtered():
    """When every backtest row carries ``methodology='screener_filtered'``,
    the prompt must drop the all-symbol caveat and emit the new
    SCREENER_FILTERED_BACKTEST_NOTE verbatim instead."""
    from services import journal_reflection_service as jrs

    inputs = {
        "reflection_date": "2026-06-01",
        "window_days": 7,
        "journal_trades": [],
        "screener_hits": [],
        "backtest_trades": [
            {"symbol": "INFY", "pnl": 1.0, "methodology": "screener_filtered"},
            {"symbol": "TCS", "pnl": -1.0, "methodology": "screener_filtered"},
        ],
        "counts": {"n_journal_trades": 0, "n_screener_hits": 0, "n_backtest_trades": 2},
    }
    prompt = jrs.render_reflection_prompt(inputs)

    # New note is present verbatim.
    assert jrs.SCREENER_FILTERED_BACKTEST_NOTE in prompt
    # Old all-symbol caveat is gone.
    assert jrs.BACKTEST_CAVEAT not in prompt
    # Key phrases from the task spec.
    assert "screener-filtered data" in prompt
    assert "admitted-placeholder" in prompt
    assert "scanner-gated, not all-symbol" in prompt


def test_render_reflection_prompt_includes_both_caveats_when_mixed():
    """If the window contains both legacy and screener-filtered rows,
    both caveats must appear so the LLM applies each to its subset."""
    from services import journal_reflection_service as jrs

    inputs = {
        "reflection_date": "2026-06-01",
        "window_days": 7,
        "journal_trades": [],
        "screener_hits": [],
        "backtest_trades": [
            {"symbol": "INFY", "pnl": 1.0, "methodology": "screener_filtered"},
            {"symbol": "TCS", "pnl": -1.0},  # legacy: no methodology
        ],
        "counts": {"n_journal_trades": 0, "n_screener_hits": 0, "n_backtest_trades": 2},
    }
    prompt = jrs.render_reflection_prompt(inputs)

    assert jrs.SCREENER_FILTERED_BACKTEST_NOTE in prompt
    assert jrs.BACKTEST_CAVEAT in prompt


def test_render_reflection_prompt_includes_row_data():
    """Sample rows from each source must appear in the prompt body."""
    from services import journal_reflection_service as jrs

    inputs = {
        "reflection_date": "2026-06-01",
        "window_days": 7,
        "journal_trades": [{"symbol": "INFY", "direction": "LONG", "pnl": 42.5}],
        "screener_hits": [{"id": 1, "symbols": ["INFY"], "source": "chartink"}],
        "backtest_trades": [{"symbol": "INFY", "pnl": 12.0}],
        "counts": {"n_journal_trades": 1, "n_screener_hits": 1, "n_backtest_trades": 1},
    }
    prompt = jrs.render_reflection_prompt(inputs)

    assert "INFY" in prompt
    assert "chartink" in prompt


# ---------------------------------------------------------------------------
# run_reflection — persistence
# ---------------------------------------------------------------------------


FAKE_BRIDGE_REPLY = """The day saw two trades, both LONG. Win-rate looks reasonable but sample size is tiny.

```json
[
  {"observation": "All trades were LONG today", "evidence": "2/2 journal rows", "confidence": "high"},
  {"observation": "Screener fired more often than engine acted", "evidence": "3 screener hits vs 2 journal rows", "confidence": "med"}
]
```

```json
[
  {"question": "Why was the third screener candidate skipped?", "why": "Investigate veto/skip path"}
]
```
"""


def test_run_reflection_persists_row(fresh_reflection_db, monkeypatch):
    """End-to-end: gather → render → bridge (mocked) → persist."""
    from services import journal_reflection_service as jrs

    # Stub out the three data sources so this test doesn't depend on any
    # of the live DB schemas.
    monkeypatch.setattr(
        jrs,
        "_safe_journal_trades",
        lambda window_hours: [{"id": 1, "symbol": "INFY"}, {"id": 2, "symbol": "TCS"}],
    )
    monkeypatch.setattr(
        jrs, "_safe_screener_hits", lambda window_hours: [{"id": 1}, {"id": 2}, {"id": 3}]
    )
    monkeypatch.setattr(
        jrs, "_safe_backtest_trades", lambda window_days: [{"id": 1, "symbol": "INFY"}]
    )

    # Mock the bridge HTTP call. Returning a real httpx.Response wrapper is
    # overkill — we just need _call_bridge to return the parsed payload.
    fake_payload = {
        "response": FAKE_BRIDGE_REPLY,
        "model": "claude-opus-4-7",
        "latency_ms": 1234,
    }
    monkeypatch.setattr(jrs, "_call_bridge", lambda prompt: fake_payload)

    result = jrs.run_reflection(date="2026-06-01", window_days=7)

    assert result["id"] is not None
    assert result["reflection_date"] == "2026-06-01"
    assert result["data_window_days"] == 7
    assert result["n_journal_trades"] == 2
    assert result["n_screener_hits"] == 3
    assert result["n_backtest_trades"] == 1
    assert result["backtest_caveat"] == jrs.BACKTEST_CAVEAT
    assert "two trades" in result["summary"].lower() or len(result["summary"]) > 0
    assert result["llm_model"] == "claude-opus-4-7"
    assert result["llm_latency_ms"] == 1234

    patterns = json.loads(result["patterns_json"])
    questions = json.loads(result["questions_json"])
    assert len(patterns) == 2
    assert patterns[0]["confidence"] == "high"
    assert len(questions) == 1
    assert "screener" in questions[0]["question"].lower()


def test_run_reflection_is_idempotent_per_date(fresh_reflection_db, monkeypatch):
    """A second run for the same date must update, not insert, so the unique
    constraint on reflection_date never trips."""
    from services import journal_reflection_service as jrs

    monkeypatch.setattr(jrs, "_safe_journal_trades", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_screener_hits", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_backtest_trades", lambda window_days: [])

    first_reply = {"response": "First reply.", "model": "m1", "latency_ms": 100}
    second_reply = {"response": "Second reply.", "model": "m2", "latency_ms": 200}

    monkeypatch.setattr(jrs, "_call_bridge", lambda prompt: first_reply)
    first = jrs.run_reflection(date="2026-06-01")

    monkeypatch.setattr(jrs, "_call_bridge", lambda prompt: second_reply)
    second = jrs.run_reflection(date="2026-06-01")

    assert first["id"] == second["id"]
    assert second["summary"] == "Second reply."
    assert second["llm_model"] == "m2"

    sess = fresh_reflection_db.db_session
    count = sess.query(fresh_reflection_db.JournalReflection).count()
    assert count == 1


def test_run_reflection_raises_on_bridge_failure(fresh_reflection_db, monkeypatch):
    """Bridge errors must surface — reflection is forensic and a silent
    failure would defeat the point of the loop."""
    from services import journal_reflection_service as jrs

    monkeypatch.setattr(jrs, "_safe_journal_trades", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_screener_hits", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_backtest_trades", lambda window_days: [])

    def _explode(prompt):
        raise RuntimeError("bridge unreachable at http://localhost:5001/reflect")

    monkeypatch.setattr(jrs, "_call_bridge", _explode)

    with pytest.raises(RuntimeError, match="bridge unreachable"):
        jrs.run_reflection(date="2026-06-01")


def test_get_latest_reflection_returns_most_recent(fresh_reflection_db, monkeypatch):
    from services import journal_reflection_service as jrs

    monkeypatch.setattr(jrs, "_safe_journal_trades", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_screener_hits", lambda window_hours: [])
    monkeypatch.setattr(jrs, "_safe_backtest_trades", lambda window_days: [])
    monkeypatch.setattr(
        jrs,
        "_call_bridge",
        lambda prompt: {"response": "ok.", "model": "m", "latency_ms": 1},
    )

    jrs.run_reflection(date="2026-05-30")
    jrs.run_reflection(date="2026-06-01")
    jrs.run_reflection(date="2026-05-31")

    latest = jrs.get_latest_reflection()
    assert latest is not None
    assert latest["reflection_date"] == "2026-06-01"
