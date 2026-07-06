"""Boot-time guard against a foreign process holding ``db/historify.duckdb``.

Why this exists
---------------
When OpenAlgo restarts while a prior Python process is still alive on the same
file (NSSM watchdog race, two ``uv run app.py`` shells, a stuck backtester),
every boot backfill job piles 30s-retry cycles into ``errors.jsonl`` against a
file they can never write to. The operator sees a 200-line lock-error flood with
no clear "another process holds this" signal.

This guard probes ``historify.duckdb`` once at boot, parses the holding PID
from DuckDB's IOException, alerts, and refuses to start the backfill schedulers.

It is intentionally a hard fail (``SystemExit(1)``) — limping along with
contested storage corrupts the day's first scanner cycle and produces silent
data gaps that are far harder to diagnose than a clean refusal at boot.

Gated by ``OPENALGO_BOOT_DB_PROBE_ENABLED`` (default ``true``); set to
``false`` to restore the legacy behaviour (spam + continue) for diagnostics.
"""

from __future__ import annotations

import os
import re
import sys

from utils.logging import get_logger

logger = get_logger(__name__)

_PID_RE = re.compile(r"\(PID\s+(\d+)\)", re.IGNORECASE)


def _parse_holding_pid(message: str) -> int | None:
    m = _PID_RE.search(message)
    return int(m.group(1)) if m else None


def assert_historify_unlocked(db_path: str | None = None) -> None:
    """Probe ``historify.duckdb`` and abort boot if a foreign process owns it.

    Args:
        db_path: Path to the DuckDB file. Defaults to ``database.historify_db.get_db_path()``.

    Behaviour:
        - Flag off (``OPENALGO_BOOT_DB_PROBE_ENABLED=false``) → no-op.
        - File missing → no-op (first boot creates it).
        - Probe succeeds → no-op.
        - Probe fails with a transient-lock error AND holding PID != this process
          → log CRITICAL, Telegram-alert (best-effort), ``sys.exit(1)``.
        - Probe fails with a transient-lock error but PID isn't extractable, OR
          the message doesn't match the lock patterns → log WARNING and return
          (don't false-positive on unrelated DuckDB issues).
    """
    if os.environ.get("OPENALGO_BOOT_DB_PROBE_ENABLED", "true").lower() != "true":
        logger.info("boot_db_probe: disabled via env flag — skipping")
        return

    if db_path is None:
        from database.historify_db import get_db_path

        db_path = get_db_path()

    if not os.path.exists(db_path):
        logger.info("boot_db_probe: %s missing — first boot, nothing to probe", db_path)
        return

    import duckdb

    from services.data_freshness_service import is_transient_lock_error

    try:
        conn = duckdb.connect(db_path, read_only=True)
        conn.close()
        logger.info("boot_db_probe: historify.duckdb is free")
        return
    except (duckdb.IOException, duckdb.ConnectionException, duckdb.BinderException) as e:
        if not is_transient_lock_error(e):
            logger.warning(
                "boot_db_probe: probe raised non-transient DuckDB error — not aborting boot: %s",
                e,
            )
            return

        msg = str(e)
        holder_pid = _parse_holding_pid(msg)
        self_pid = os.getpid()

        if holder_pid is None:
            logger.warning(
                "boot_db_probe: historify.duckdb shows transient lock but no holder PID "
                "in error — not aborting boot. error=%s",
                msg,
            )
            return

        if holder_pid == self_pid:
            logger.info(
                "boot_db_probe: held by THIS process (pid=%s) — expected during in-process "
                "read_only fallback, not aborting",
                self_pid,
            )
            return

        logger.critical(
            "boot_db_probe: ABORTING BOOT — historify.duckdb is held by another process "
            "(holder_pid=%s, self_pid=%s, path=%s). Kill the orphan and restart. "
            "Disable with OPENALGO_BOOT_DB_PROBE_ENABLED=false.",
            holder_pid,
            self_pid,
            db_path,
        )

        try:
            from services.notification_service import get_notification_service

            get_notification_service().notify(
                "boot_db_probe",
                (
                    f"🚨 OpenAlgo boot aborted — historify.duckdb held by orphan "
                    f"python.exe (PID {holder_pid}). Kill it and restart. "
                    f"Path: {db_path}"
                ),
                severity="critical",
                holder_pid=holder_pid,
                self_pid=self_pid,
                path=db_path,
            )
        except Exception:
            logger.exception("boot_db_probe: Telegram alert failed (continuing to exit)")

        sys.exit(1)
