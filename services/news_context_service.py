"""Read-only market-news context for operator alerts (informational only).

When futures_follow_cap50 takes a big overnight loss (or its kill switch fires),
the operator gets a Telegram alert. This helper attaches the most recent
market-news headlines — already ingested by ``news_ingest_service`` into
``market_intel(kind='news')`` — so the operator can SEE the likely macro reason
(war / geopolitics / policy / a broad sell-off) without switching apps, then
decide what to do.

Design constraints (load-bearing — do not relax):

* **Strictly informational + human-in-the-loop.** The headlines are DATA. They
  are ONLY ever concatenated into the operator alert text. Nothing here parses
  them for trading actions, and no caller acts on their content. The backtests
  (R43 news-momentum, R54 stops, R55 put hedge) all showed that *reacting* to a
  down-move — by stop, hedge, or news-triggered sell — is net-negative on this
  leveraged-beta sleeve; this feature exists to EXPLAIN a loss, never to trade it.
* **No outbound network call.** The ingest sidecar already fetched the feeds on
  its own 5-min cron; this is a pure DB read. So it adds zero latency / external
  dependency to the alert path.
* **Fail-open.** Any error returns an empty (or "unavailable") string — an alert
  must never be blocked or delayed by the news lookup.
* **Untrusted content.** Titles are stripped of newlines and truncated so a
  crafted headline cannot forge extra alert lines or overflow the message.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from utils.logging import get_logger

logger = get_logger(__name__)

# High-signal terms that get a ⚠️ marker in the alert — a hint to the operator,
# NEVER a trading trigger. Operator-overridable via NEWS_CONTEXT_HIGHLIGHT_TERMS.
_DEFAULT_HIGHLIGHT_TERMS = (
    "war",
    "attack",
    "missile",
    "strike",
    "invasion",
    "ceasefire",
    "airstrike",
    "conflict",
    "escalat",
    "sanction",
    "tariff",
    "crash",
    "plunge",
    "slump",
    "selloff",
    "sell-off",
    "tumble",
    "rbi",
    "fed",
    "rate cut",
    "rate hike",
    "inflation",
    "crude",
    "oil price",
    "geopolit",
    "border",
)
_TITLE_MAX = 160


def _enabled() -> bool:
    return os.getenv("NEWS_CONTEXT_ON_ALERTS_ENABLED", "true").strip().lower() == "true"


def _lookback_min() -> int:
    try:
        return int(os.getenv("NEWS_CONTEXT_LOOKBACK_MIN", "720"))
    except ValueError:
        return 720


def _max_items() -> int:
    try:
        return int(os.getenv("NEWS_CONTEXT_MAX_ITEMS", "6"))
    except ValueError:
        return 6


def _highlight_terms() -> tuple[str, ...]:
    raw = os.getenv("NEWS_CONTEXT_HIGHLIGHT_TERMS", "").strip()
    if not raw:
        return _DEFAULT_HIGHLIGHT_TERMS
    return tuple(t.strip().lower() for t in raw.split(",") if t.strip())


def _clean_title(title: str) -> str:
    """Strip newlines/control chars and truncate — headlines are untrusted data."""
    t = " ".join(str(title or "").split())
    if len(t) > _TITLE_MAX:
        t = t[: _TITLE_MAX - 1].rstrip() + "…"
    return t


def _short_time(captured_at: str) -> str:
    """HH:MM from an ISO 'captured_at', best-effort; '' on failure."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(captured_at)).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def get_recent_news_context(
    *,
    max_items: int | None = None,
    since_minutes: int | None = None,
    reader: Callable[..., list[dict]] | None = None,
) -> str:
    """Return a short Telegram-ready block of recent market headlines, or "".

    Pure DB read of ``market_intel(kind='news')`` via ``reader`` (defaults to
    ``latest_intel_by_kind``). Fail-open: returns "" on any error, and a short
    "no headlines" note when the flag is on but nothing was ingested (so the
    operator knows the check ran and the feed may be down). ``reader`` is
    injectable for hermetic tests.
    """
    if not _enabled():
        return ""
    n = max_items if max_items is not None else _max_items()
    window = since_minutes if since_minutes is not None else _lookback_min()
    if reader is None:
        try:
            from database.market_intel_db import latest_intel_by_kind as reader  # type: ignore
        except Exception:
            logger.debug("news context: market_intel reader unavailable", exc_info=True)
            return ""
    try:
        rows = reader("news", limit=max(n * 3, 30), since_minutes=window)
    except Exception:
        logger.debug("news context: reader raised", exc_info=True)
        return ""

    if not rows:
        hrs = round(window / 60, 1)
        return f"📰 No market headlines ingested in the last {hrs}h (news feed may be down)."

    terms = _highlight_terms()
    lines: list[str] = []
    for row in rows[:n]:
        payload = row.get("payload_json") if isinstance(row, dict) else None
        if not isinstance(payload, dict):
            continue
        title = _clean_title(payload.get("title", ""))
        if not title:
            continue
        source = str(payload.get("source", "")).strip()[:24]
        when = _short_time(row.get("captured_at", ""))
        flagged = any(term in title.lower() for term in terms)
        prefix = "⚠️" if flagged else "•"
        meta = f" [{source}]" if source else ""
        meta += f" ({when})" if when else ""
        lines.append(f"{prefix}{meta} {title}")

    if not lines:
        return ""
    hrs = round(window / 60, 1)
    header = f"📰 Recent market headlines (last {hrs}h — context only, not a signal):"
    return header + "\n" + "\n".join(lines)
