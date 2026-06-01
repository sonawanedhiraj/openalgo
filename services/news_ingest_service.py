"""Phase A news ingest sidecar.

Periodically fetches configured RSS feeds, dedupes by hash(source, title),
writes new items to the ``market_intel`` table with ``kind='news'``.

NO consumers in Phase A — just ingestion. The intel is available for
future veto/regime/reflection integration in Phase B.

Operator knobs (env vars):

* ``NEWS_INGEST_ENABLED`` — set to ``false``/``0``/``no`` to skip the
  scheduler at boot. Default: enabled.
* ``NEWS_FEEDS`` — comma-separated list of ``url|label`` pairs to
  override :data:`DEFAULT_FEEDS`. If a pair has no ``|`` the URL is
  reused as the label.

Cron: every 5 min, 08:00–15:30 IST, Mon–Fri. Misfire grace 120s.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import feedparser
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.logging import get_logger

logger = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Default feed list — operator can override via NEWS_FEEDS env.
DEFAULT_FEEDS: list[tuple[str, str]] = [
    ("https://www.moneycontrol.com/rss/marketreports.xml", "moneycontrol_markets"),
    (
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "et_markets",
    ),
    # Easy to add more later: livemint, bloombergquint, BSE/NSE corp announcements...
]

_scheduler: BackgroundScheduler | None = None


def _parse_feeds_env() -> list[tuple[str, str]]:
    raw = os.getenv("NEWS_FEEDS", "").strip()
    if not raw:
        return DEFAULT_FEEDS
    out: list[tuple[str, str]] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            url, label = entry.split("|", 1)
            out.append((url.strip(), label.strip()))
        else:
            out.append((entry, entry))
    return out or DEFAULT_FEEDS


def _dedup_hash(source: str, title: str) -> str:
    key = f"{source.strip().lower()}::{title.strip().lower()}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def fetch_feed_items(url: str, source_label: str) -> list[dict]:
    """Fetch one feed; return normalized items. Failures return ``[]``."""
    try:
        parsed = feedparser.parse(url)
    except Exception as e:
        logger.warning(f"Feed fetch failed [{source_label}]: {e}")
        return []
    entries = getattr(parsed, "entries", None) or []
    items: list[dict] = []
    for entry in entries[:50]:  # cap to recent 50 per feed
        title = (entry.get("title") or "").strip()
        if not title:
            continue
        link = entry.get("link") or ""
        published_raw = entry.get("published") or entry.get("updated") or ""
        summary = (entry.get("summary") or "").strip()
        items.append(
            {
                "source": source_label,
                "title": title,
                "link": link,
                "published_raw": published_raw,
                "summary": summary[:500],
                "dedup_hash": _dedup_hash(source_label, title),
            }
        )
    return items


def get_existing_hashes(since_minutes: int = 1440) -> set[str]:
    """Return dedup hashes from news rows captured in the last N minutes."""
    from database.market_intel_db import latest_intel_by_kind

    rows = latest_intel_by_kind("news", limit=2000, since_minutes=since_minutes)
    out: set[str] = set()
    for r in rows:
        payload = r.get("payload_json")
        if isinstance(payload, dict) and "dedup_hash" in payload:
            out.add(payload["dedup_hash"])
    return out


def run_ingest_cycle() -> dict:
    """One ingest cycle: fetch all feeds, dedup, write new rows.

    Returns a summary dict with ``duration_s``, ``feeds``,
    ``total_fetched``, ``total_new``, and ``per_feed`` breakdown.
    """
    from database.market_intel_db import insert_intel

    feeds = _parse_feeds_env()
    existing = get_existing_hashes()
    total_fetched = 0
    total_new = 0
    per_feed: dict[str, dict] = {}
    started = time.time()
    for url, label in feeds:
        items = fetch_feed_items(url, label)
        total_fetched += len(items)
        new_count = 0
        for item in items:
            if item["dedup_hash"] in existing:
                continue
            try:
                insert_intel(kind="news", payload_json=json.dumps(item))
                existing.add(item["dedup_hash"])
                new_count += 1
                total_new += 1
            except Exception as e:
                logger.warning(
                    f"insert_intel failed for {item['title'][:60]}: {e}"
                )
        per_feed[label] = {"fetched": len(items), "new": new_count}
    summary = {
        "duration_s": round(time.time() - started, 2),
        "feeds": len(feeds),
        "total_fetched": total_fetched,
        "total_new": total_new,
        "per_feed": per_feed,
    }
    logger.info(f"News ingest: {summary}")
    return summary


def start_news_ingest_scheduler() -> dict:
    """Start APScheduler with the news ingest cron job.

    Mirrors the EOD watchdog pattern: idempotent, honours
    ``NEWS_INGEST_ENABLED``, returns a small status dict.
    """
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return {"started": False, "reason": "already running"}
    if os.getenv("NEWS_INGEST_ENABLED", "true").strip().lower() in (
        "false",
        "0",
        "no",
    ):
        return {"started": False, "reason": "NEWS_INGEST_ENABLED=false"}

    _scheduler = BackgroundScheduler(timezone=IST)
    # Every 5 min from 08:00–15:30 IST mon-fri. Cron: minute=*/5, hour=8-15.
    _scheduler.add_job(
        run_ingest_cycle,
        CronTrigger(
            minute="*/5", hour="8-15", day_of_week="mon-fri", timezone=IST
        ),
        id="news_ingest_cycle",
        replace_existing=True,
        misfire_grace_time=120,
    )
    _scheduler.start()
    job = _scheduler.get_job("news_ingest_cycle")
    next_run = job.next_run_time if job else None
    logger.info(f"News ingest scheduler started, next_run={next_run}")
    return {"started": True, "next_run": str(next_run)}


def stop_news_ingest_scheduler() -> None:
    """Shut down the scheduler if running. No-op otherwise."""
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
