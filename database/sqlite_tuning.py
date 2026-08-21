"""Process-wide SQLite pragmas for every SQLAlchemy engine (issue #633).

Why this is a global listener and not a per-module change
--------------------------------------------------------
There are ~80 ``create_engine`` call sites bound to these databases: 24 under
``database/`` plus one in every ``broker/*/database/master_contract_db.py``.
The broker ones matter most — the zerodha symtoken swap is the 114,268-row
write at the centre of the 2026-08-18 outage. A listener on the ``Engine``
class covers all of them, and covers engines added later without anyone having
to remember this file.

What went wrong on 2026-08-18
-----------------------------
``openalgo.db`` (100 MB) ran in **rollback-journal** mode. On the daily
master-contract boot the swap writes 114,268 rows and the cache hook then reads
all of them back in a single 38.88 s pass. In rollback-journal mode a writer
that needs EXCLUSIVE while a long read holds SHARED escalates to PENDING, and
PENDING blocks every NEW reader. Every read in the process then died on the
driver's 5 s timeout — the scanner backfill, the API-key lookups and every
``strategy_mode`` read, 50 minutes before market open.

**WAL removes the class**: readers never block writers and writers never block
readers, so no single long read can wall off the database regardless of which
component holds the write open. That last clause is the point — the specific
holder of the ~10-minute idle transaction was never identified (see #633), and
WAL does not require identifying it.

Deliberately NOT changed
------------------------
``synchronous`` stays at its default (FULL). WAL is commonly paired with
``synchronous=NORMAL`` for speed, but that trades durability on power loss, and
this database holds order and journal state. Concurrency is the problem being
solved here; durability is a separate decision for the operator to make
explicitly.

Rollback
--------
``SQLITE_JOURNAL_MODE=DELETE`` restores the previous behaviour. Note that
journal mode is **persisted in the database file**, so a rollback also needs
the app restarted for the pragma to be re-applied to each file.
"""

from __future__ import annotations

import os
import sqlite3

from sqlalchemy import event
from sqlalchemy.engine import Engine

from utils.logging import get_logger

logger = get_logger(__name__)

JOURNAL_MODE = os.getenv("SQLITE_JOURNAL_MODE", "WAL").strip().upper()
try:
    BUSY_TIMEOUT_MS = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "10000"))
except ValueError:
    BUSY_TIMEOUT_MS = 10000

_warned: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """One line per distinct problem — this runs on EVERY connect (NullPool)."""
    if key not in _warned:
        _warned.add(key)
        logger.warning(message)


@event.listens_for(Engine, "connect")
def _apply_sqlite_pragmas(dbapi_connection, connection_record) -> None:
    """Set busy_timeout then journal_mode on each new SQLite connection.

    Order is load-bearing: ``busy_timeout`` first, so that the ``journal_mode``
    switch can WAIT for a momentarily busy database instead of failing and
    leaving that connection in the old mode.

    Fail-open by design — a pragma that cannot be applied degrades this
    connection's concurrency, but raising here would break every database
    operation in the process.
    """
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return  # postgres / other dialects

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")

        if JOURNAL_MODE == "WAL":
            actual = str(cursor.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower()
            # ":memory:" databases report "memory" and cannot be WAL — expected,
            # not a failure. Anything else means the switch did not take.
            if actual not in ("wal", "memory"):
                _warn_once(
                    f"journal_mode:{actual}",
                    f"SQLite journal_mode is '{actual}', not WAL — concurrent "
                    "readers and writers will block each other (issue #633).",
                )
        elif JOURNAL_MODE:
            cursor.execute(f"PRAGMA journal_mode={JOURNAL_MODE}")
    except sqlite3.Error as exc:
        _warn_once(f"pragma_error:{exc}", f"Could not apply SQLite pragmas: {exc}")
    finally:
        cursor.close()
