"""Compacts a day's logs into a small, structured digest.

Two log sources, both far too large to carry around verbatim:

* ``log/openalgo_<date>.log`` — the daily text log, **~5 MB on a normal trading
  day**. Streamed line-by-line; only counts and known structured markers are
  kept. Raw log text never enters the digest.
* ``log/errors.jsonl`` — ERROR+ records as JSON Lines. Hard-capped at **1000
  lines and truncated on every app startup** (``utils/logging.py``), so error
  history is silently lost whenever OpenAlgo restarts.

The truncation problem is why this module also writes a durable per-day snapshot
(``log/error_digest_<date>.json``) and **merges** into it on every run: a restart
mid-day can no longer erase the morning's errors from the record. Merging is by
error template, so re-running for the same date is idempotent in shape and
monotonic in counts.

Error grouping
--------------

Errors are bucketed by ``(logger, normalized_message)``. Normalization strips the
parts that vary per occurrence — numbers, quoted values, paths, hex ids — so that
1000 raw errors collapse to a couple of dozen templates, each with a count and a
single exemplar (including one traceback). On a sample day this took 1000 error
lines down to ~15 templates, the largest being ``database.auth_db`` at 397.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# Cap on bytes read from the text log per file. A pathological log (a tight
# exception loop) must not turn the digest job into a multi-minute file scan.
_MAX_TEXT_LOG_BYTES = 64 * 1024 * 1024

# How many error templates and marker kinds to retain.
_DEFAULT_TOP_N = 25

# Longest exemplar message / traceback we keep in the durable snapshot.
_MAX_EXEMPLAR_CHARS = 400
_MAX_TRACEBACK_CHARS = 1200

# Tighter budgets for the returned digest, which is carried in memory, persisted
# per day, and (from Phase 3) embedded in an LLM prompt. Only the busiest
# templates keep a traceback — see `_trim_for_output`.
_OUTPUT_EXEMPLAR_CHARS = 240
_OUTPUT_TRACEBACK_CHARS = 900
_OUTPUT_TRACEBACK_COUNT = 5

# `[2026-08-01 00:28:00,867] ERROR in health_db: <message>`
_APP_LOG_LINE_RE = re.compile(
    r"^\[(?P<ts>\d{4}-\d{2}-\d{2}) [\d:,]+\]\s+(?P<level>[A-Z]+)\s+in\s+(?P<module>[\w.]+):\s?(?P<message>.*)$"
)

# Structured markers worth counting by name. These are the load-bearing
# "something degraded but did not raise" signals this codebase emits; a raw
# WARNING count alone would not distinguish them.
_MARKERS: tuple[tuple[str, str], ...] = (
    ("scanner_fail", "scanner FAIL"),
    ("scanner_held", "scanner HELD"),
    ("scanner_pass", "scanner PASS"),
    ("aggregator_miss", "aggregator had no today bars"),
    ("historify_fallback", "falling back to historify"),
    ("mirror_skipped_no_quote", "skipped_no_quote"),
    ("mirror_skipped_no_capital", "skipped_no_capital"),
    ("mirror_skipped_zero_qty", "skipped_zero_qty"),
    ("mirror_skipped_no_position", "skipped_no_position"),
    ("veto_skip", "veto_skip"),
    ("kill_switch", "kill_switch"),
    ("smoke_check_failed", "smoke_check_failed"),
    ("reference_divergence", "reference_divergence"),
    ("duckdb_lock", "different configuration"),
    ("broker_session_missing", "no broker session"),
)


# --------------------------------------------------------------------------
# Message normalisation
# --------------------------------------------------------------------------

_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Windows and POSIX paths first — they contain digits and separators that
    # the later rules would otherwise shred into noise.
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),
    (re.compile(r"(?<![\w])/(?:[\w.\-]+/){2,}[\w.\-]*"), "<path>"),
    (re.compile(r"https?://\S+"), "<url>"),
    # Quoted values vary per occurrence (symbols, ids, filenames).
    (re.compile(r"'[^']*'"), "<v>"),
    (re.compile(r'"[^"]*"'), "<v>"),
    # UUIDs / long hex before generic numbers.
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b"), "<id>"),
    (re.compile(r"\b0x[0-9a-fA-F]+\b"), "<id>"),
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<id>"),
    # Timestamps, then any remaining number-ish run.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]?[\d:,.]*\b"), "<ts>"),
    # Numbers, including ones glued to a unit (`3.5s`, `250ms`). The trailing
    # alternation ends the match on a digit so a trailing separator (`boom 1.`)
    # is left alone; requiring a closing \b instead would stop at the `.` in
    # `3.5s` and leave a stray `5s` behind, splitting one template into many.
    (re.compile(r"(?<![\w.])\d[\d,._:+-]*\d|(?<![\w.])\d"), "<n>"),
)


def normalize_message(message: str, max_chars: int = 200) -> str:
    """Collapse an error message to a stable template.

    Strips the per-occurrence variance (numbers, quoted values, paths, hex ids)
    so repeated instances of the same failure group into one bucket.

    >>> normalize_message("Error placing order for 'SBIN': qty 25 rejected")
    'Error placing order for <v>: qty <n> rejected'
    """
    text = (message or "").strip()
    for pattern, replacement in _NORMALISERS:
        text = pattern.sub(replacement, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


# --------------------------------------------------------------------------
# errors.jsonl
# --------------------------------------------------------------------------


def _log_dir() -> Path:
    return Path(os.environ.get("LOG_DIR") or "log")


def _iter_error_records(path: Path) -> Any:
    """Yield parsed JSON objects from ``errors.jsonl``, skipping bad lines."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except FileNotFoundError:
        return
    except Exception:
        logger.exception("postmarket_log_digest: failed reading %s", path)
        return


def collect_error_templates(target_date: str, log_dir: Path | None = None) -> dict[str, Any]:
    """Bucket ``errors.jsonl`` entries for ``target_date`` into templates.

    ``ts`` in errors.jsonl is server-local time formatted ``%Y-%m-%d %H:%M:%S``
    (see ``utils/logging.py``), so the date filter is a prefix match.

    Returns:
        ``{"total": int, "templates": {key: {...}}, "by_logger": {...}}`` — the
        raw, unmerged view of what is currently in the file.
    """
    directory = log_dir or _log_dir()
    path = directory / "errors.jsonl"

    templates: dict[str, dict[str, Any]] = {}
    by_logger: dict[str, int] = {}
    total = 0

    for record in _iter_error_records(path):
        ts = str(record.get("ts") or "")
        if not ts.startswith(target_date):
            continue
        total += 1
        source = str(record.get("logger") or record.get("module") or "?")
        by_logger[source] = by_logger.get(source, 0) + 1

        template = normalize_message(str(record.get("message") or ""))
        key = f"{source}|{template}"
        entry = templates.get(key)
        if entry is None:
            exception = record.get("exception")
            if isinstance(exception, list):
                traceback_text = "".join(str(part) for part in exception)[:_MAX_TRACEBACK_CHARS]
            elif exception:
                traceback_text = str(exception)[:_MAX_TRACEBACK_CHARS]
            else:
                traceback_text = None
            templates[key] = {
                "logger": source,
                "template": template,
                "count": 1,
                "level": str(record.get("level") or "ERROR"),
                "file": str(record.get("file") or ""),
                "first_ts": ts,
                "last_ts": ts,
                "exemplar": str(record.get("message") or "")[:_MAX_EXEMPLAR_CHARS],
                "traceback": traceback_text,
            }
        else:
            entry["count"] += 1
            entry["last_ts"] = ts

    return {"total": total, "templates": templates, "by_logger": by_logger}


# --------------------------------------------------------------------------
# Durable snapshot (survives the 1000-line truncation on restart)
# --------------------------------------------------------------------------


def _snapshot_path(target_date: str, log_dir: Path | None = None) -> Path:
    return (log_dir or _log_dir()) / f"error_digest_{target_date}.json"


def _load_snapshot(target_date: str, log_dir: Path | None = None) -> dict[str, Any]:
    path = _snapshot_path(target_date, log_dir)
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.exception("postmarket_log_digest: failed reading snapshot %s", path)
        return {}


def _merge_templates(
    prior: dict[str, dict[str, Any]], fresh: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Merge two template maps, taking the max count per template.

    Max rather than sum: within one day, ``errors.jsonl`` is cumulative until a
    restart truncates it. Summing would double-count everything already captured
    by an earlier run; taking the max keeps the record monotonic and makes
    re-running the digest for the same date idempotent. The cost is that errors
    lost to a truncation *between* two runs are undercounted rather than
    invented — the honest direction to err.
    """
    merged: dict[str, dict[str, Any]] = {k: dict(v) for k, v in prior.items()}
    for key, entry in fresh.items():
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(entry)
            continue
        existing["count"] = max(int(existing.get("count") or 0), int(entry.get("count") or 0))
        existing["last_ts"] = max(
            str(existing.get("last_ts") or ""), str(entry.get("last_ts") or "")
        )
        existing["first_ts"] = min(
            str(existing.get("first_ts") or entry.get("first_ts") or ""),
            str(entry.get("first_ts") or existing.get("first_ts") or ""),
        )
        if not existing.get("traceback") and entry.get("traceback"):
            existing["traceback"] = entry["traceback"]
    return merged


def write_snapshot(target_date: str, payload: dict[str, Any], log_dir: Path | None = None) -> bool:
    """Persist the merged template map for ``target_date``. Never raises."""
    path = _snapshot_path(target_date, log_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash mid-write cannot leave a corrupt snapshot.
        tmp = path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, default=str)
        tmp.replace(path)
        return True
    except Exception:
        logger.exception("postmarket_log_digest: failed writing snapshot %s", path)
        return False


# --------------------------------------------------------------------------
# Daily text log
# --------------------------------------------------------------------------


def _candidate_text_logs(target_date: str, directory: Path) -> list[Path]:
    """Log files that may contain lines dated ``target_date``.

    The daily handler's rotation boundary and its file naming do not line up
    perfectly — a file named for one date can carry the first minutes of the
    next. Checking the neighbouring days costs one ``exists()`` each and removes
    a whole class of off-by-one gaps. Lines are date-filtered regardless, so a
    wrong candidate contributes nothing.
    """
    try:
        parsed = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError:
        return []
    names = [
        f"openalgo_{(parsed + timedelta(days=offset)).strftime('%Y-%m-%d')}.log"
        for offset in (-1, 0, 1)
    ]
    return [directory / name for name in names if (directory / name).exists()]


def scan_text_log(target_date: str, log_dir: Path | None = None) -> dict[str, Any]:
    """Stream the daily text log, returning counts only — never log text.

    Returns:
        ``{"lines_scanned", "by_level", "warning_by_module", "markers",
        "bytes_scanned", "truncated"}``.
    """
    directory = log_dir or _log_dir()
    by_level: dict[str, int] = {}
    warning_by_module: dict[str, int] = {}
    markers: dict[str, int] = {}
    lines_scanned = 0
    bytes_scanned = 0
    truncated = False

    for path in _candidate_text_logs(target_date, directory):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    bytes_scanned += len(line)
                    if bytes_scanned > _MAX_TEXT_LOG_BYTES:
                        truncated = True
                        logger.warning(
                            "postmarket_log_digest: text log scan hit the %d MB cap at %s",
                            _MAX_TEXT_LOG_BYTES // (1024 * 1024),
                            path,
                        )
                        break

                    match = _APP_LOG_LINE_RE.match(line)
                    if match is None:
                        # Continuation line (traceback body). Markers can appear
                        # there too, but attributing them to a date/level is not
                        # possible, so they are skipped rather than misfiled.
                        continue
                    if match.group("ts") != target_date:
                        continue

                    lines_scanned += 1
                    level = match.group("level")
                    by_level[level] = by_level.get(level, 0) + 1
                    if level in ("WARNING", "ERROR", "CRITICAL"):
                        module = match.group("module")
                        warning_by_module[module] = warning_by_module.get(module, 0) + 1

                    message = match.group("message")
                    for name, needle in _MARKERS:
                        if needle in message:
                            markers[name] = markers.get(name, 0) + 1
        except Exception:
            logger.exception("postmarket_log_digest: failed scanning %s", path)
            continue
        if truncated:
            break

    return {
        "lines_scanned": lines_scanned,
        "by_level": by_level,
        "warning_by_module": dict(
            sorted(warning_by_module.items(), key=lambda kv: kv[1], reverse=True)[:_DEFAULT_TOP_N]
        ),
        "markers": markers,
        "bytes_scanned": bytes_scanned,
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def _trim_for_output(templates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Shrink templates for the returned digest (the snapshot keeps everything).

    Tracebacks dominate the payload — 25 of them at full length is ~30 KB, most
    of it repeated framework frames nobody reads. Only the busiest few templates
    keep a traceback; the rest keep counts and a short exemplar, which is what
    triage actually needs. The on-disk snapshot is unaffected, so nothing is
    permanently lost.
    """
    trimmed: list[dict[str, Any]] = []
    for index, entry in enumerate(templates):
        item = dict(entry)
        item["exemplar"] = str(item.get("exemplar") or "")[:_OUTPUT_EXEMPLAR_CHARS]
        if index < _OUTPUT_TRACEBACK_COUNT and item.get("traceback"):
            item["traceback"] = str(item["traceback"])[:_OUTPUT_TRACEBACK_CHARS]
        else:
            item.pop("traceback", None)
        trimmed.append(item)
    return trimmed


def build_log_digest(
    target_date: str | date_cls,
    log_dir: Path | None = None,
    top_n: int = _DEFAULT_TOP_N,
    persist: bool = True,
) -> dict[str, Any]:
    """Build the compacted log view for ``target_date``.

    Reads ``errors.jsonl``, merges it into the durable per-day snapshot, scans
    the daily text log, and returns a bounded summary. Never raises — every
    section degrades to an empty/partial value and says so.

    Args:
        target_date: IST calendar date, ``YYYY-MM-DD`` or a ``date``.
        log_dir: Override the log directory (tests).
        top_n: How many error templates to return.
        persist: Write the merged snapshot back to disk.

    Returns:
        A dict with ``date``, ``errors`` (total, templates, by_logger),
        ``app_log`` (counts + markers) and ``snapshot_written``.
    """
    day = (
        target_date.strftime("%Y-%m-%d") if isinstance(target_date, date_cls) else str(target_date)
    )
    directory = log_dir or _log_dir()

    try:
        fresh = collect_error_templates(day, directory)
    except Exception:
        logger.exception("postmarket_log_digest: error collection failed for %s", day)
        fresh = {"total": 0, "templates": {}, "by_logger": {}}

    prior = _load_snapshot(day, directory)
    prior_templates = prior.get("templates") if isinstance(prior.get("templates"), dict) else {}
    merged = _merge_templates(prior_templates, fresh["templates"])

    prior_by_logger = prior.get("by_logger") if isinstance(prior.get("by_logger"), dict) else {}
    by_logger = dict(prior_by_logger)
    for source, count in fresh["by_logger"].items():
        by_logger[source] = max(int(by_logger.get(source, 0)), int(count))

    total = sum(int(entry.get("count") or 0) for entry in merged.values())

    snapshot_written = False
    if persist:
        snapshot_written = write_snapshot(
            day,
            {
                "date": day,
                "updated_at": datetime.utcnow().isoformat(),
                "templates": merged,
                "by_logger": by_logger,
            },
            directory,
        )

    try:
        app_log = scan_text_log(day, directory)
    except Exception:
        logger.exception("postmarket_log_digest: text log scan failed for %s", day)
        app_log = None

    top_templates = _trim_for_output(
        sorted(merged.values(), key=lambda entry: int(entry.get("count") or 0), reverse=True)[
            :top_n
        ]
    )

    return {
        "date": day,
        "errors": {
            "total": total,
            "distinct_templates": len(merged),
            "by_logger": dict(
                sorted(by_logger.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
            ),
            "top_templates": top_templates,
        },
        "app_log": app_log,
        "snapshot_written": snapshot_written,
    }
