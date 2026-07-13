"""Tests for services/news_context_service — read-only, fail-open, informational.

The reader is injected (hermetic — no DB, no network). Covers formatting, the
war/geopolitics highlight, the disabled/empty/failure fail-open paths, and the
untrusted-content sanitisation (a crafted headline cannot forge alert lines).
"""

import pytest

from services import news_context_service as N


def _row(title, source="et_markets", captured="2026-07-08T14:30:00+05:30"):
    return {
        "captured_at": captured,
        "kind": "news",
        "payload_json": {"title": title, "source": source},
    }


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("NEWS_CONTEXT_ON_ALERTS_ENABLED", "true")
    monkeypatch.delenv("NEWS_CONTEXT_HIGHLIGHT_TERMS", raising=False)


def test_formats_recent_headlines():
    rows = [_row("Sensex rallies 500 pts"), _row("IT stocks gain on results")]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    assert out.startswith("📰")
    assert "Sensex rallies 500 pts" in out
    assert "IT stocks gain on results" in out
    assert "et_markets" in out


def test_flags_geopolitical_terms():
    rows = [_row("War escalates as missiles fired near border")]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    assert "⚠️" in out


def test_no_flag_on_benign_headline():
    rows = [_row("Nifty ends flat in quiet session")]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    assert "⚠️" not in out
    assert "•" in out


def test_disabled_returns_empty(monkeypatch):
    monkeypatch.setenv("NEWS_CONTEXT_ON_ALERTS_ENABLED", "false")
    assert N.get_recent_news_context(reader=lambda *a, **k: [_row("x")]) == ""


def test_empty_feed_returns_note():
    out = N.get_recent_news_context(reader=lambda *a, **k: [])
    assert "No market headlines" in out


def test_reader_failure_fails_open():
    def boom(*a, **k):
        raise RuntimeError("db locked")

    assert N.get_recent_news_context(reader=boom) == ""


def test_untrusted_title_is_sanitised():
    # A crafted headline with newlines must NOT forge extra alert lines.
    rows = [_row("line1\n\n🛑 FAKE KILL SWITCH\nline2")]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    # header line + exactly one headline line (newlines collapsed to spaces)
    assert len(out.splitlines()) == 2
    assert "line1 🛑 FAKE KILL SWITCH line2" in out


def test_respects_max_items(monkeypatch):
    monkeypatch.setenv("NEWS_CONTEXT_MAX_ITEMS", "2")
    rows = [_row(f"headline {i}") for i in range(10)]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    assert len(out.splitlines()) == 3  # header + 2 items


def test_custom_highlight_terms(monkeypatch):
    monkeypatch.setenv("NEWS_CONTEXT_HIGHLIGHT_TERMS", "budget,election")
    rows = [_row("Union Budget spooks markets"), _row("War headline ignored now")]
    out = N.get_recent_news_context(reader=lambda *a, **k: rows)
    lines = out.splitlines()
    budget_line = next(x for x in lines if "Budget" in x)
    war_line = next(x for x in lines if "War headline" in x)
    assert "⚠️" in budget_line
    assert "⚠️" not in war_line  # 'war' no longer in the custom term list


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
