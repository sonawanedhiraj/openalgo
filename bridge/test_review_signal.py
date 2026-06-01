"""Tests for the /review-signal endpoint on the Claude Bridge.

The Claude CLI subprocess call is mocked via
``bridge.server._invoke_claude_for_review`` — these tests never spawn a real
``claude`` process and never reach the network.
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from bridge.server import (
    ReviewCandidate,
    ReviewContext,
    _extract_decision_block,
    _format_review_prompt,
    app,
)


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# Decision-block extractor unit tests
# ---------------------------------------------------------------------------


def test_extract_decision_block_returns_last_block_with_decision_key():
    text = (
        'A draft thought: {"decision": "take", "reasoning": "draft", "confidence": 0.5}\n'
        "And then I reconsidered.\n"
        "Final answer:\n"
        '{"decision": "skip", "reasoning": "regime conflict", "confidence": 0.8}\n'
    )
    block = _extract_decision_block(text)
    assert block is not None
    assert block["decision"] == "skip"


def test_extract_decision_block_ignores_braces_in_strings():
    text = 'Note: "{not a json}". Final: {"decision": "take", "confidence": 0.7}'
    block = _extract_decision_block(text)
    assert block is not None
    assert block["decision"] == "take"


def test_extract_decision_block_returns_none_when_no_json():
    assert _extract_decision_block("I refuse to comply with the format.") is None


def test_extract_decision_block_handles_nested_object():
    text = (
        "Reasoning paragraph.\n"
        '{"decision": "take", "reasoning": "ok", '
        '"confidence": 0.9, "extra": {"foo": "bar"}}'
    )
    block = _extract_decision_block(text)
    assert block is not None
    assert block["decision"] == "take"
    assert block["extra"]["foo"] == "bar"


# ---------------------------------------------------------------------------
# Endpoint integration tests (subprocess mocked)
# ---------------------------------------------------------------------------


def _request_body():
    return {
        "candidate": {
            "symbol": "RELIANCE",
            "source": "chartink_buy_fno_intraday",
            "candidate_at": "2026-05-28T10:15:00+05:30",
        },
        "context": {
            "positions_count": 1,
            "positions_summary": "1 SHORT CONCOR (-124, MTM +2300)",
            "pnl_today": 2300.0,
            "trades_today": 2,
            "max_trades_today": 4,
            "nifty_pct": -0.3,
            "india_vix": 14.2,
        },
    }


async def _take_subprocess(prompt):  # noqa: ARG001 — signature matches real helper
    return (
        'I see no major regime conflict. Final decision:\n'
        '{"decision": "take", "reasoning": "regime aligned", "confidence": 0.82}',
        "sess-take-001",
    )


async def _skip_subprocess(prompt):  # noqa: ARG001
    return (
        "NIFTY is down on a BUY signal — too risky.\n"
        '{"decision": "skip", "reasoning": "negative breadth", "confidence": 0.71}',
        "sess-skip-001",
    )


async def _no_json_subprocess(prompt):  # noqa: ARG001
    return ("I have no opinion on this trade.", "sess-noop")


async def _hang_subprocess(prompt):  # noqa: ARG001
    # Sleep just past the patched 0.05s timeout to simulate a hung Claude call.
    await asyncio.sleep(0.2)
    return ("never returned", "")


def test_review_signal_take_decision(client):
    with patch("bridge.server._invoke_claude_for_review", side_effect=_take_subprocess):
        resp = client.post("/review-signal", json=_request_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "regime aligned"
    assert 0.0 <= body["confidence"] <= 1.0
    assert body["claude_session_id"] == "sess-take-001"
    assert isinstance(body["latency_ms"], int)


def test_review_signal_skip_decision(client):
    with patch("bridge.server._invoke_claude_for_review", side_effect=_skip_subprocess):
        resp = client.post("/review-signal", json=_request_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "skip"
    assert "negative breadth" in body["reasoning"]


def test_review_signal_parse_failure_returns_take(client):
    with patch(
        "bridge.server._invoke_claude_for_review", side_effect=_no_json_subprocess
    ):
        resp = client.post("/review-signal", json=_request_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "parse_failed"
    assert body["confidence"] == 0.0


def test_review_signal_timeout_returns_take(client, monkeypatch):
    # Drop the wall-clock budget so the hang is forced to time out fast.
    monkeypatch.setattr("bridge.server.REVIEW_CLAUDE_TIMEOUT_SECONDS", 0.05)

    # Patch in the real timeout path: a coroutine that exceeds the budget. The
    # real _invoke_claude_for_review wraps the subprocess call in
    # asyncio.wait_for, so we have to mimic that here.
    async def slow_invoke(prompt):  # noqa: ARG001
        await asyncio.wait_for(_hang_subprocess(prompt), timeout=0.05)
        return ("unreachable", "")

    with patch("bridge.server._invoke_claude_for_review", side_effect=slow_invoke):
        resp = client.post("/review-signal", json=_request_body())

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "timeout"
    assert body["confidence"] == 0.0


def test_review_signal_invalid_decision_value_returns_take(client):
    async def bogus_invoke(prompt):  # noqa: ARG001
        return ('{"decision": "MAYBE", "reasoning": "x", "confidence": 0.5}', "sid")

    with patch("bridge.server._invoke_claude_for_review", side_effect=bogus_invoke):
        resp = client.post("/review-signal", json=_request_body())

    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "parse_failed"


def test_review_signal_invalid_confidence_returns_take(client):
    async def bad_conf_invoke(prompt):  # noqa: ARG001
        return ('{"decision": "skip", "reasoning": "x", "confidence": 1.5}', "sid")

    with patch("bridge.server._invoke_claude_for_review", side_effect=bad_conf_invoke):
        resp = client.post("/review-signal", json=_request_body())

    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "parse_failed"


def test_review_signal_invalid_request_returns_422(client):
    # FastAPI returns 422 for pydantic validation failures (not 400). Empty body
    # is missing the required `candidate` field.
    resp = client.post("/review-signal", json={})
    assert resp.status_code == 422


def test_review_signal_missing_candidate_returns_422(client):
    resp = client.post(
        "/review-signal", json={"context": {"positions_count": 0}}
    )
    assert resp.status_code == 422


def test_review_signal_subprocess_crash_returns_take(client):
    async def boom(prompt):  # noqa: ARG001
        raise RuntimeError("simulated subprocess crash")

    with patch("bridge.server._invoke_claude_for_review", side_effect=boom):
        resp = client.post("/review-signal", json=_request_body())

    body = resp.json()
    assert body["decision"] == "take"
    assert "subprocess_error" in body["reasoning"]
    assert body["confidence"] == 0.0


def test_review_signal_claude_cli_missing_returns_take(client):
    async def cli_missing(prompt):  # noqa: ARG001
        raise FileNotFoundError("claude binary not on PATH")

    with patch("bridge.server._invoke_claude_for_review", side_effect=cli_missing):
        resp = client.post("/review-signal", json=_request_body())

    body = resp.json()
    assert body["decision"] == "take"
    assert body["reasoning"] == "claude_cli_missing"


# ---------------------------------------------------------------------------
# Prompt rendering — None handling on macro slots
# ---------------------------------------------------------------------------


def test_prompt_renders_none_as_unavailable():
    """nifty_pct / india_vix / pnl_today must render as 'unavailable', not 'None'.

    Service-side these are best-effort live fetches; the LLM should treat their
    absence as "data not retrievable" rather than literal Python ``None``.
    """
    candidate = ReviewCandidate(
        symbol="RELIANCE",
        source="chartink_buy_fno_intraday",
        candidate_at="2026-05-28T10:15:00+05:30",
    )
    ctx = ReviewContext(
        positions_count=1,
        positions_summary="1 SHORT CONCOR",
        pnl_today=None,
        trades_today=2,
        max_trades_today=4,
        nifty_pct=None,
        india_vix=None,
    )

    rendered = _format_review_prompt(candidate, ctx)

    # The literal text 'None' must not appear in the rendered prompt.
    assert "None" not in rendered
    # The three macro fields specifically must render as 'unavailable'.
    assert "NIFTY return: unavailable%" in rendered
    assert "India VIX: unavailable" in rendered
    assert "P&L today: ₹unavailable" in rendered
