"""Tests for the ``market_intel`` table helpers — focused on the new
``latest_intel_by_kind`` reader used by the Phase A news ingest sidecar.

Uses a private in-memory SQLite engine so the tests never touch the
real ``db/openalgo.db`` file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import pytest
import pytz
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import database.market_intel_db as mi_mod

IST = pytz.timezone("Asia/Kolkata")


@pytest.fixture(autouse=True)
def _isolated_engine(monkeypatch):
    """Swap the module-level engine + session to a fresh in-memory DB per test."""
    engine = create_engine("sqlite:///:memory:")
    session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
    monkeypatch.setattr(mi_mod, "engine", engine)
    monkeypatch.setattr(mi_mod, "db_session", session)
    mi_mod.Base.query = session.query_property()
    mi_mod.Base.metadata.create_all(engine)
    yield
    session.remove()
    engine.dispose()


def _ist_iso(minutes_ago: int = 0) -> str:
    return (datetime.now(IST) - timedelta(minutes=minutes_ago)).isoformat()


def test_latest_intel_by_kind_returns_newest_first():
    mi_mod.insert_intel(kind="news", payload_json=json.dumps({"title": "first"}))
    mi_mod.insert_intel(kind="news", payload_json=json.dumps({"title": "second"}))
    mi_mod.insert_intel(kind="news", payload_json=json.dumps({"title": "third"}))

    rows = mi_mod.latest_intel_by_kind("news", limit=10)
    assert [r["payload_json"]["title"] for r in rows] == ["third", "second", "first"]


def test_latest_intel_by_kind_decodes_json_payload():
    payload = {"title": "RBI pauses", "dedup_hash": "abc", "summary": "..."}
    mi_mod.insert_intel(kind="news", payload_json=json.dumps(payload))

    rows = mi_mod.latest_intel_by_kind("news")
    assert len(rows) == 1
    assert rows[0]["payload_json"] == payload


def test_latest_intel_by_kind_filters_by_kind():
    mi_mod.insert_intel(kind="regime", payload_json=json.dumps({"trend": "bullish"}))
    mi_mod.insert_intel(kind="news", payload_json=json.dumps({"title": "x"}))

    rows = mi_mod.latest_intel_by_kind("news")
    assert len(rows) == 1
    assert rows[0]["payload_json"] == {"title": "x"}


def test_latest_intel_by_kind_respects_limit():
    for i in range(5):
        mi_mod.insert_intel(kind="news", payload_json=json.dumps({"i": i}))

    rows = mi_mod.latest_intel_by_kind("news", limit=2)
    assert len(rows) == 2


def test_latest_intel_by_kind_since_minutes_excludes_old_rows():
    # Old row (90 min ago) inserted with explicit captured_at.
    mi_mod.insert_intel(
        kind="news",
        payload_json=json.dumps({"title": "old"}),
        captured_at=_ist_iso(minutes_ago=90),
    )
    # Fresh row (default captured_at = now).
    mi_mod.insert_intel(kind="news", payload_json=json.dumps({"title": "new"}))

    recent = mi_mod.latest_intel_by_kind("news", since_minutes=30)
    assert [r["payload_json"]["title"] for r in recent] == ["new"]

    everything = mi_mod.latest_intel_by_kind("news")
    assert len(everything) == 2


def test_latest_intel_by_kind_handles_raw_string_payload():
    # Should not crash when payload_json isn't valid JSON — fall back to raw text.
    mi_mod.insert_intel(kind="news", payload_json="not-json-at-all")

    rows = mi_mod.latest_intel_by_kind("news")
    assert rows[0]["payload_json"] == "not-json-at-all"


def test_latest_intel_by_kind_empty_table():
    assert mi_mod.latest_intel_by_kind("news") == []
