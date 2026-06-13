"""Stage 2 part 2 — nightly journal reflection.

Once a day, after EOD, synthesise the trading day's signal/decision/outcome
chain into a forensic summary the operator can read in the morning. The
three substrates pulled in are:

* ``trade_journal`` — what the live engine actually traded.
* ``scan_results`` — what the screeners surfaced as candidates.
* ``backtest_trades`` — what the offline simulator says the strategy would
  have done historically.

The reflection is LLM-generated and persisted into ``journal_reflection``,
one row per ``reflection_date``. It is purely retrospective — no orders, no
config writes, no engine flags touched.

**Critical caveat baked into the prompt.** Today the backtest dataset is
generated with an "all symbols, every day, 6 months" methodology, while the
live strategy only takes positions on the screener's same-day picks. That
mismatch means backtest hit-rate and P&L numbers are over-broad and not
directly comparable to live outcomes. The LLM prompt names this explicitly
so it doesn't claim "we'd have made X%" from backtest data. A separate task
will re-run the backtest screener-filtered; once that's live the caveat in
:data:`BACKTEST_CAVEAT` should be revised.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from typing import Any

import httpx
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from utils.logging import get_logger

logger = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")

# Verbatim — must not be edited without updating
# tests/test_journal_reflection_service.py and the README's backtest section.
# When the screener-filtered re-run lands, swap this string for one that
# describes the new methodology and bump the test.
BACKTEST_CAVEAT = (
    "NOTE on backtest data: the backtest results below were generated with an "
    "ALL-SYMBOL methodology — every symbol in the F&O universe was tested "
    "every day for 6 months. The live strategy only takes positions on stocks "
    "that appeared on the screener that day. Backtest hit-rate and P&L numbers "
    "are therefore over-broad and not directly comparable to live outcomes. "
    'Use backtest patterns directionally only (e.g. "SHORTs perform worse on '
    'high-VIX days" is OK; "we\'d have made X% returns" is NOT). A screener-'
    "filtered re-run is on the roadmap."
)

# Verbatim — used when the backtest rows in the window were produced by
# the screener-filtered harness (services.backtest_screener_filtered_service).
# Both notes can appear in a single prompt if the window straddles old and
# new data; see render_reflection_prompt below.
SCREENER_FILTERED_BACKTEST_NOTE = (
    "NOTE on backtest data below: this is screener-filtered data — only "
    "stocks that the in-house scanner rule WOULD have picked on each "
    "historical day are included. Note that the scanner rule is admitted-"
    "placeholder and not yet tuned against live Chartink output, so "
    "quantitative conclusions about hit rate and P&L should still be "
    "treated as directional, but the methodology is now consistent with "
    "how the live engine takes positions (scanner-gated, not all-symbol)."
)

_DEFAULT_BRIDGE_URL = "http://127.0.0.1:5001/reflect"
_DEFAULT_REQUEST_TIMEOUT = 200.0  # bridge wall-clock is 180s — leave a buffer.

# Module-level singleton scheduler. Mirrors eod_watchdog_service's pattern;
# only one reflection cron per process.
_scheduler: BackgroundScheduler | None = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _bridge_url() -> str:
    return os.getenv("REFLECTION_BRIDGE_URL", _DEFAULT_BRIDGE_URL).strip() or _DEFAULT_BRIDGE_URL


def _request_timeout() -> float:
    raw = os.getenv("REFLECTION_REQUEST_TIMEOUT_SECONDS")
    if not raw:
        return _DEFAULT_REQUEST_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        logger.warning("reflection: invalid REFLECTION_REQUEST_TIMEOUT_SECONDS=%r", raw)
        return _DEFAULT_REQUEST_TIMEOUT


def _resolve_date(date: str | None) -> dt.date:
    """Coerce the optional ``date`` arg to a ``date``. Default = today IST."""
    if date is None:
        return dt.datetime.now(IST).date()
    return dt.date.fromisoformat(date)


# ---------------------------------------------------------------------------
# Input gathering
# ---------------------------------------------------------------------------


def gather_reflection_inputs(
    date: str | None = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """Return the joined ``trade_journal`` / ``scan_results`` / ``backtest_trades``
    payload over the last ``window_days`` ending on ``date`` (IST date string
    or ``None`` for today).

    Each sub-source is fetched defensively — a single source failure surfaces
    as an empty list in the returned dict rather than blowing up the whole
    reflection. The LLM is told via the prompt to interpret missing sources
    as "no data available" rather than "no activity."
    """
    target_date = _resolve_date(date)
    window_hours = max(1, window_days * 24)

    journal_trades = _safe_journal_trades(window_hours)
    screener_hits = _safe_screener_hits(window_hours)
    backtest_trades = _safe_backtest_trades(window_days)

    return {
        "reflection_date": target_date.isoformat(),
        "window_days": window_days,
        "journal_trades": journal_trades,
        "screener_hits": screener_hits,
        "backtest_trades": backtest_trades,
        "counts": {
            "n_journal_trades": len(journal_trades),
            "n_screener_hits": len(screener_hits),
            "n_backtest_trades": len(backtest_trades),
        },
    }


def _safe_journal_trades(window_hours: int) -> list[dict]:
    try:
        from services.trade_journal_service import get_recent_trades

        return get_recent_trades(hours=window_hours)
    except Exception:
        logger.exception("reflection: failed to fetch trade_journal rows")
        return []


def _safe_screener_hits(window_hours: int) -> list[dict]:
    try:
        from services.scanner_service import get_scan_results

        return get_scan_results(hours=window_hours)
    except Exception:
        logger.exception("reflection: failed to fetch scan_results rows")
        return []


def _safe_backtest_trades(window_days: int) -> list[dict]:
    """Pull recent backtest trades by walking the most recent runs.

    There's no first-class "trades by date" query today, so we walk the
    most-recent ``backtest_runs`` and pull each run's trades. We cap at 5
    runs so a long history of small runs doesn't bloat the prompt.
    """
    try:
        from services import backtest_service

        recent_runs = backtest_service.get_recent_runs(limit=5)
        trades: list[dict] = []
        cutoff = dt.datetime.now(IST) - dt.timedelta(days=window_days)
        for run in recent_runs:
            started = run.get("started_at")
            if started:
                try:
                    started_dt = dt.datetime.fromisoformat(started)
                    if started_dt.tzinfo is None:
                        started_dt = IST.localize(started_dt)
                    if started_dt < cutoff:
                        continue
                except ValueError:
                    pass
            trades.extend(backtest_service.get_run_trades(int(run["id"])))
        return trades
    except Exception:
        logger.exception("reflection: failed to fetch backtest_trades rows")
        return []


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def render_reflection_prompt(inputs: dict[str, Any]) -> str:
    """Build the single LLM prompt for ``run_reflection``.

    The returned string includes the backtest caveat verbatim — see the
    test in ``test/test_journal_reflection_service.py``.
    """
    counts = inputs.get("counts") or {}

    header = (
        "You are reviewing an algorithmic trader's full day-of-the-market "
        "audit trail and synthesising patterns for the human operator. "
        "Three data sources are included below. Use ALL of them; do not "
        "synthesise from one source in isolation."
    )

    structure_note = (
        "Respond with three sections in this exact order:\n"
        "1. A 3-5 sentence top-level summary of the day.\n"
        "2. A JSON array of structured patterns/observations under a heading "
        "`PATTERNS_JSON`. Each entry: "
        '`{"observation": str, "evidence": str, "confidence": "low|med|high"}`.\n'
        "3. A JSON array of open questions worth investigating under a heading "
        '`QUESTIONS_JSON`. Each entry: `{"question": str, "why": str}`.\n'
        "Both JSON arrays MUST be valid JSON in fenced code blocks tagged "
        "```json so the post-processor can extract them."
    )

    journal_block = _format_block(
        "TRADE_JOURNAL (live engine round-trips — the trades that actually fired)",
        inputs.get("journal_trades") or [],
    )
    screener_block = _format_block(
        "SCAN_RESULTS (screener candidates — what the screeners surfaced as possible trades)",
        inputs.get("screener_hits") or [],
    )
    backtest_block = _format_block(
        "BACKTEST_TRADES (offline simulator results)",
        inputs.get("backtest_trades") or [],
    )

    counts_line = (
        f"COUNTS: journal={counts.get('n_journal_trades', 0)} "
        f"screener={counts.get('n_screener_hits', 0)} "
        f"backtest={counts.get('n_backtest_trades', 0)} "
        f"window_days={inputs.get('window_days', 0)}"
    )

    backtest_rows = inputs.get("backtest_trades") or []
    methodologies = _detect_backtest_methodologies(backtest_rows)
    caveat_blocks = _select_backtest_caveats(methodologies)

    sections: list[str] = [
        header,
        "",
        structure_note,
        "",
        f"REFLECTION DATE: {inputs.get('reflection_date')}",
        counts_line,
        "",
    ]
    for block in caveat_blocks:
        sections.append(block)
        sections.append("")
    sections.extend(
        [
            journal_block,
            "",
            screener_block,
            "",
            backtest_block,
        ]
    )
    return "\n".join(sections)


def _detect_backtest_methodologies(rows: list[dict]) -> set[str]:
    """Return the set of distinct methodology tags found in backtest rows.

    Pre-migration rows have no methodology column and are bucketed as
    ``"all_symbol"`` — the original (and only) methodology that produced
    them. Anything else (today: ``"screener_filtered"``) is returned
    verbatim.
    """
    tags: set[str] = set()
    for row in rows or []:
        tag = (row or {}).get("methodology")
        if tag:
            tags.add(str(tag))
        else:
            tags.add("all_symbol")
    return tags


def _select_backtest_caveats(methodologies: set[str]) -> list[str]:
    """Pick the right caveat block(s) for the methodologies in the window.

    * Only ``screener_filtered`` rows → emit
      :data:`SCREENER_FILTERED_BACKTEST_NOTE`.
    * Only ``all_symbol`` rows (legacy) → emit :data:`BACKTEST_CAVEAT`.
    * Both present → emit both with a short framer so the LLM applies
      each note to the appropriate subset.
    * No backtest rows → emit the all-symbol caveat (it explains the
      historic dataset shape even when nothing was loaded this run).
    """
    if not methodologies:
        return [BACKTEST_CAVEAT]

    has_screener = "screener_filtered" in methodologies
    has_all_symbol = any(t != "screener_filtered" for t in methodologies)

    if has_screener and has_all_symbol:
        return [
            "BACKTEST METHODOLOGY NOTES (data below contains BOTH old all-symbol "
            "rows AND newer screener-filtered rows — apply each note to the "
            "rows it describes; the methodology field on each row marks which):",
            BACKTEST_CAVEAT,
            SCREENER_FILTERED_BACKTEST_NOTE,
        ]
    if has_screener:
        return [SCREENER_FILTERED_BACKTEST_NOTE]
    return [BACKTEST_CAVEAT]


def _format_block(heading: str, rows: list[dict]) -> str:
    """Format a source block as ``heading + JSON-encoded rows``.

    Rows are emitted as compact JSON one per line so a wedged LLM that bails
    early at least sees recent data, and the operator reading the prompt can
    spot-check what was shipped.
    """
    if not rows:
        return f"=== {heading} ===\n(no data)"
    body_lines = ["=== " + heading + " ==="]
    for row in rows:
        try:
            body_lines.append(json.dumps(row, default=str))
        except (TypeError, ValueError):
            body_lines.append(str(row))
    return "\n".join(body_lines)


# ---------------------------------------------------------------------------
# LLM invocation
# ---------------------------------------------------------------------------


def _call_bridge(prompt: str) -> dict[str, Any]:
    """POST the prompt to the bridge ``/reflect`` endpoint.

    Raises ``RuntimeError`` on any failure so :func:`run_reflection` surfaces
    the problem loudly. Unlike :mod:`signal_review_service`, reflection MUST
    NOT fail-safe to a silent fallback — the operator needs to know the loop
    didn't run.
    """
    url = _bridge_url()
    timeout = _request_timeout()
    try:
        response = httpx.post(url, json={"prompt": prompt}, timeout=timeout)
    except httpx.HTTPError as exc:
        raise RuntimeError(f"bridge unreachable at {url}: {exc}") from exc
    if response.status_code >= 300:
        raise RuntimeError(f"bridge returned HTTP {response.status_code}: {response.text[:300]}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"bridge returned non-JSON body: {response.text[:300]}") from exc
    if "response" not in payload:
        raise RuntimeError(f"bridge response missing 'response' key: {payload!r}")
    return payload


# ---------------------------------------------------------------------------
# Reply parsing
# ---------------------------------------------------------------------------

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _parse_reply(model_text: str) -> tuple[str, list[dict], list[dict]]:
    """Extract ``(summary, patterns, questions)`` from the LLM reply.

    The summary is everything before the first fenced JSON block (or the
    whole reply if no blocks are present). Patterns and questions are pulled
    from the first two ```json blocks respectively, in order. Anything that
    fails to parse as JSON becomes an empty list — the persisted row still
    records a row, the prose summary is still useful.
    """
    matches = _JSON_BLOCK_RE.findall(model_text or "")
    patterns: list[dict] = []
    questions: list[dict] = []
    if len(matches) >= 1:
        patterns = _safe_load_array(matches[0])
    if len(matches) >= 2:
        questions = _safe_load_array(matches[1])

    if _JSON_BLOCK_RE.search(model_text or ""):
        summary = _JSON_BLOCK_RE.split(model_text)[0].strip()
    else:
        summary = (model_text or "").strip()

    # Strip trailing headings (e.g. ``PATTERNS_JSON``) from the summary slice
    # since they're orphaned without the JSON block they introduce.
    for tag in ("PATTERNS_JSON", "QUESTIONS_JSON"):
        idx = summary.rfind(tag)
        if idx != -1:
            summary = summary[:idx].rstrip()

    return summary, patterns, questions


def _safe_load_array(raw: str) -> list[dict]:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("reflection: JSON block failed to parse")
        return []
    if isinstance(data, list):
        return [d for d in data if isinstance(d, dict)]
    if isinstance(data, dict):
        return [data]
    return []


# ---------------------------------------------------------------------------
# Persistence + public entry point
# ---------------------------------------------------------------------------


def _persist_reflection(
    *,
    reflection_date: dt.date,
    window_days: int,
    counts: dict,
    summary: str,
    patterns: list[dict],
    questions: list[dict],
    llm_model: str,
    llm_latency_ms: int,
) -> dict[str, Any]:
    """Write (or replace) the reflection row for ``reflection_date``.

    Idempotent — re-running for the same date overwrites the previous row's
    LLM-generated fields. The unique constraint on ``reflection_date``
    enforces one row per day at the DB level.
    """
    from database import journal_reflection_db as jrdb

    sess = jrdb.db_session
    try:
        existing = (
            sess.query(jrdb.JournalReflection)
            .filter_by(reflection_date=reflection_date)
            .one_or_none()
        )
        if existing is None:
            row = jrdb.JournalReflection(reflection_date=reflection_date)
            sess.add(row)
        else:
            row = existing

        row.created_at = jrdb._now_iso()
        row.data_window_days = int(window_days)
        row.n_journal_trades = int(counts.get("n_journal_trades", 0))
        row.n_screener_hits = int(counts.get("n_screener_hits", 0))
        row.n_backtest_trades = int(counts.get("n_backtest_trades", 0))
        row.backtest_caveat = BACKTEST_CAVEAT
        row.summary = summary or "(empty summary)"
        row.patterns_json = json.dumps(patterns or [])
        row.questions_json = json.dumps(questions or [])
        row.llm_model = llm_model or None
        row.llm_latency_ms = int(llm_latency_ms) if llm_latency_ms is not None else None
        sess.commit()
        sess.refresh(row)
        return jrdb._row_to_dict(row)
    except Exception:
        sess.rollback()
        raise
    finally:
        sess.remove()


def run_reflection(
    date: str | None = None,
    window_days: int = 7,
) -> dict[str, Any]:
    """End-to-end: gather inputs, render prompt, call bridge, persist row.

    Returns the persisted row as a dict. Raises ``RuntimeError`` on any
    bridge failure — the cron caller (:func:`schedule_nightly_reflection`)
    catches that and logs but the manual smoke test deliberately surfaces it.
    """
    target_date = _resolve_date(date)
    inputs = gather_reflection_inputs(date=target_date.isoformat(), window_days=window_days)
    prompt = render_reflection_prompt(inputs)

    started = time.time()
    bridge_response = _call_bridge(prompt)
    latency_ms = int((time.time() - started) * 1000)

    model_text = bridge_response.get("response") or ""
    model_id = bridge_response.get("model") or ""
    # The bridge reports its own latency too; prefer it when present since
    # it doesn't include the round-trip HTTP overhead.
    bridge_latency = bridge_response.get("latency_ms")
    if isinstance(bridge_latency, (int, float)) and bridge_latency > 0:
        latency_ms = int(bridge_latency)

    summary, patterns, questions = _parse_reply(model_text)

    return _persist_reflection(
        reflection_date=target_date,
        window_days=window_days,
        counts=inputs.get("counts") or {},
        summary=summary,
        patterns=patterns,
        questions=questions,
        llm_model=model_id,
        llm_latency_ms=latency_ms,
    )


def get_latest_reflection() -> dict[str, Any] | None:
    """Return the most recent reflection row as a dict, or ``None`` if empty.

    Used by status endpoints and the future EOD Telegram summary.
    """
    try:
        from database import journal_reflection_db as jrdb

        sess = jrdb.db_session
        try:
            row = (
                sess.query(jrdb.JournalReflection)
                .order_by(jrdb.JournalReflection.reflection_date.desc())
                .first()
            )
            return jrdb._row_to_dict(row) if row else None
        finally:
            sess.remove()
    except Exception:
        logger.exception("reflection: get_latest_reflection failed")
        return None


# ---------------------------------------------------------------------------
# Nightly cron
# ---------------------------------------------------------------------------


def schedule_nightly_reflection() -> dict[str, Any]:
    """Start (or resume) the APScheduler cron job that calls ``run_reflection``
    at 16:00 IST on weekdays.

    Idempotent — calling twice does not double-schedule. Mirrors the EOD
    watchdog's singleton pattern; the scheduler lives on the module so tests
    can introspect it.
    """
    global _scheduler

    if _scheduler is not None and _scheduler.running:
        logger.debug("reflection: scheduler already running")
        return {"started": False, "job_id": "nightly_reflection"}

    _scheduler = BackgroundScheduler(
        timezone=IST,
        executors={"default": {"type": "threadpool", "max_workers": 1}},
    )
    _scheduler.add_job(
        _cron_run_reflection,
        CronTrigger(hour=16, minute=0, day_of_week="mon-fri", timezone=IST),
        id="nightly_reflection",
        replace_existing=True,
        misfire_grace_time=900,
    )
    _scheduler.start()
    logger.info("reflection: nightly cron scheduled (16:00 IST mon-fri)")
    return {"started": True, "job_id": "nightly_reflection"}


def stop_nightly_reflection() -> None:
    """Shutdown hook for tests."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        if _scheduler.running:
            _scheduler.shutdown(wait=False)
    except Exception:
        logger.exception("reflection: scheduler shutdown raised — ignoring")
    finally:
        _scheduler = None


def _cron_run_reflection() -> None:
    """Cron-job body. Catches everything so APScheduler never sees an
    exception bubbling out of the reflection path."""
    try:
        result = run_reflection()
        logger.info(
            "reflection: nightly run completed id=%s n_journal=%s n_screener=%s n_backtest=%s",
            result.get("id"),
            result.get("n_journal_trades"),
            result.get("n_screener_hits"),
            result.get("n_backtest_trades"),
        )
    except Exception:
        logger.exception("reflection: nightly run crashed")
