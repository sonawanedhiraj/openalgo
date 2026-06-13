"""Tests for the Phase A news ingest sidecar.

All RSS fetches and DB writes are mocked — these tests never hit the
network or touch ``market_intel.db``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from services import news_ingest_service as nis

# ---------------------------------------------------------------------------
# _dedup_hash
# ---------------------------------------------------------------------------


def test_dedup_hash_is_deterministic():
    a = nis._dedup_hash("source_x", "RBI hikes repo rate by 25bps")
    b = nis._dedup_hash("source_x", "RBI hikes repo rate by 25bps")
    assert a == b
    assert len(a) == 24


def test_dedup_hash_is_case_insensitive_and_trims():
    a = nis._dedup_hash("Moneycontrol", "RBI Hikes Repo Rate")
    b = nis._dedup_hash("moneycontrol", "  rbi hikes repo rate  ")
    assert a == b


def test_dedup_hash_distinguishes_titles():
    a = nis._dedup_hash("src", "title one")
    b = nis._dedup_hash("src", "title two")
    assert a != b


# ---------------------------------------------------------------------------
# _parse_feeds_env
# ---------------------------------------------------------------------------


def test_parse_feeds_env_empty_returns_defaults(monkeypatch):
    monkeypatch.delenv("NEWS_FEEDS", raising=False)
    assert nis._parse_feeds_env() == nis.DEFAULT_FEEDS


def test_parse_feeds_env_parses_url_pipe_label(monkeypatch):
    monkeypatch.setenv(
        "NEWS_FEEDS",
        "https://a.example/rss|alpha , https://b.example/rss|beta",
    )
    feeds = nis._parse_feeds_env()
    assert feeds == [
        ("https://a.example/rss", "alpha"),
        ("https://b.example/rss", "beta"),
    ]


def test_parse_feeds_env_url_without_label_reuses_url(monkeypatch):
    monkeypatch.setenv("NEWS_FEEDS", "https://only.example/rss")
    feeds = nis._parse_feeds_env()
    assert feeds == [("https://only.example/rss", "https://only.example/rss")]


# ---------------------------------------------------------------------------
# fetch_feed_items
# ---------------------------------------------------------------------------


def _make_parsed(entries):
    return SimpleNamespace(entries=entries)


def test_fetch_feed_items_normalizes_entries():
    entries = [
        {
            "title": "RBI holds repo rate steady",
            "link": "https://x.example/news/1",
            "published": "Mon, 01 Jun 2026 10:00:00 +0530",
            "summary": "Central bank pauses cycle after strong inflation print.",
        },
        {
            "title": "Sensex hits record on tech rally",
            "link": "https://x.example/news/2",
            "updated": "Mon, 01 Jun 2026 11:00:00 +0530",
            "summary": "IT stocks lead the charge.",
        },
    ]
    with patch.object(nis.feedparser, "parse", return_value=_make_parsed(entries)):
        items = nis.fetch_feed_items("https://stub/rss", "stub_source")
    assert len(items) == 2
    assert items[0]["source"] == "stub_source"
    assert items[0]["title"] == "RBI holds repo rate steady"
    assert items[0]["link"] == "https://x.example/news/1"
    assert items[0]["published_raw"].startswith("Mon, 01 Jun")
    assert "Central bank" in items[0]["summary"]
    assert items[0]["dedup_hash"] == nis._dedup_hash("stub_source", "RBI holds repo rate steady")
    # Second entry falls back to `updated`.
    assert items[1]["published_raw"].startswith("Mon, 01 Jun")


def test_fetch_feed_items_skips_blank_titles():
    entries = [{"title": "", "link": "x"}, {"title": "valid", "link": "y"}]
    with patch.object(nis.feedparser, "parse", return_value=_make_parsed(entries)):
        items = nis.fetch_feed_items("u", "lbl")
    assert len(items) == 1
    assert items[0]["title"] == "valid"


def test_fetch_feed_items_returns_empty_on_exception():
    with patch.object(nis.feedparser, "parse", side_effect=RuntimeError("boom")):
        assert nis.fetch_feed_items("u", "lbl") == []


def test_fetch_feed_items_caps_at_50():
    entries = [{"title": f"title {i}", "link": f"l{i}"} for i in range(80)]
    with patch.object(nis.feedparser, "parse", return_value=_make_parsed(entries)):
        items = nis.fetch_feed_items("u", "lbl")
    assert len(items) == 50


def test_fetch_feed_items_truncates_long_summary():
    long_summary = "x" * 1000
    entries = [{"title": "t", "link": "l", "summary": long_summary}]
    with patch.object(nis.feedparser, "parse", return_value=_make_parsed(entries)):
        items = nis.fetch_feed_items("u", "lbl")
    assert len(items[0]["summary"]) == 500


# ---------------------------------------------------------------------------
# get_existing_hashes
# ---------------------------------------------------------------------------


def test_get_existing_hashes_extracts_set_from_payloads():
    fake_rows = [
        {"payload_json": {"dedup_hash": "h1", "title": "a"}},
        {"payload_json": {"dedup_hash": "h2", "title": "b"}},
        {"payload_json": {"title": "no hash"}},  # ignored
        {"payload_json": "raw string"},  # ignored
    ]
    with patch("database.market_intel_db.latest_intel_by_kind", return_value=fake_rows):
        out = nis.get_existing_hashes(since_minutes=60)
    assert out == {"h1", "h2"}


# ---------------------------------------------------------------------------
# run_ingest_cycle
# ---------------------------------------------------------------------------


def test_run_ingest_cycle_writes_new_skips_dupes(monkeypatch):
    monkeypatch.delenv("NEWS_FEEDS", raising=False)
    # Pretend we have two feeds returning 2 + 1 items, one of which is a dupe.
    h_dupe = nis._dedup_hash("livemint_markets", "Sensex closes flat")

    def fake_fetch(url, label):
        if label == "livemint_markets":
            return [
                {"title": "Sensex closes flat", "dedup_hash": h_dupe, "source": label},
                {
                    "title": "Nifty banks rally",
                    "dedup_hash": nis._dedup_hash(label, "Nifty banks rally"),
                    "source": label,
                },
            ]
        return [
            {
                "title": "Crude crashes",
                "dedup_hash": nis._dedup_hash(label, "Crude crashes"),
                "source": label,
            }
        ]

    inserted: list[dict] = []

    def fake_insert(kind, payload_json):
        assert kind == "news"
        inserted.append(json.loads(payload_json))
        return len(inserted)

    with (
        patch.object(nis, "fetch_feed_items", side_effect=fake_fetch),
        patch("database.market_intel_db.insert_intel", side_effect=fake_insert),
        patch.object(nis, "get_existing_hashes", return_value={h_dupe}),
    ):
        summary = nis.run_ingest_cycle()

    assert summary["feeds"] == 2
    assert summary["total_fetched"] == 3
    assert summary["total_new"] == 2
    assert summary["per_feed"]["livemint_markets"] == {"fetched": 2, "new": 1}
    assert summary["per_feed"]["et_markets"] == {"fetched": 1, "new": 1}
    assert {row["title"] for row in inserted} == {
        "Nifty banks rally",
        "Crude crashes",
    }


def test_run_ingest_cycle_handles_insert_failure(monkeypatch):
    monkeypatch.delenv("NEWS_FEEDS", raising=False)

    def fake_fetch(url, label):
        return [
            {
                "title": "X",
                "dedup_hash": nis._dedup_hash(label, "X"),
                "source": label,
            }
        ]

    with (
        patch.object(nis, "fetch_feed_items", side_effect=fake_fetch),
        patch("database.market_intel_db.insert_intel", side_effect=RuntimeError("db down")),
        patch.object(nis, "get_existing_hashes", return_value=set()),
    ):
        summary = nis.run_ingest_cycle()

    # Both feeds tried, no rows committed, no crash.
    assert summary["total_new"] == 0
    assert summary["total_fetched"] == 2


# ---------------------------------------------------------------------------
# Scheduler start/stop
# ---------------------------------------------------------------------------


def test_start_news_ingest_scheduler_creates_cron_job(monkeypatch):
    monkeypatch.delenv("NEWS_INGEST_ENABLED", raising=False)
    nis.stop_news_ingest_scheduler()
    try:
        result = nis.start_news_ingest_scheduler()
        assert result["started"] is True
        assert result["next_run"]  # non-empty string
        job = nis._scheduler.get_job("news_ingest_cycle")
        assert job is not None
        # Cron fields should reflect every-5-min within market hours.
        trig = job.trigger
        fields = {f.name: str(f) for f in trig.fields}
        assert fields["minute"] == "*/5"
        assert fields["hour"] == "8-15"
        assert "mon" in fields["day_of_week"].lower()
    finally:
        nis.stop_news_ingest_scheduler()


def test_start_news_ingest_scheduler_respects_disabled_env(monkeypatch):
    monkeypatch.setenv("NEWS_INGEST_ENABLED", "false")
    nis.stop_news_ingest_scheduler()
    result = nis.start_news_ingest_scheduler()
    assert result["started"] is False
    assert "NEWS_INGEST_ENABLED" in result["reason"]


def test_start_news_ingest_scheduler_is_idempotent(monkeypatch):
    monkeypatch.delenv("NEWS_INGEST_ENABLED", raising=False)
    nis.stop_news_ingest_scheduler()
    try:
        first = nis.start_news_ingest_scheduler()
        second = nis.start_news_ingest_scheduler()
        assert first["started"] is True
        assert second["started"] is False
        assert second["reason"] == "already running"
    finally:
        nis.stop_news_ingest_scheduler()
