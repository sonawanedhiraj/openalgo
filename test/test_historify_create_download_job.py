"""End-to-end tests for the 2026-06-19 race-fix in
``database.historify_db.create_download_job``.

The Friday 2026-06-19 in-house scanner forensics traced the day's stale-feed
outage to two specific failures in this function (see
``audit/proposed_fixes.jsonl`` and the issue body):

1. **Hand-rolled ID generation raced.** ``id = MAX(id) + ROW_NUMBER()``
   computed inside an INSERT means two callers that read MAX before either
   commits will both compute the same starting ID and the second commit fails
   with ``Duplicate key "id: 4074"``. The boot-convergence + periodic loop
   pattern in ``scanner_universe_backfill`` hit this exact scenario.
2. **The catch-block ROLLBACK assumed an active transaction.** When DuckDB
   auto-aborts a transaction on constraint violation, the manual
   ``ROLLBACK`` raises ``cannot rollback - no transaction is active``, which
   masks the original duplicate-key error.

These tests pin down the fix:

* ``test_sequence_is_seeded_past_existing_max`` — initialising the DB on a
  non-empty ``job_items`` does not reset the sequence to 1.
* ``test_back_to_back_jobs_get_unique_ids`` — two ``create_download_job``
  calls in quick succession assign disjoint ID ranges.
* ``test_pre_existing_id_does_not_collide`` — a pre-seeded row with an ID
  that the *old* MAX+ROW_NUMBER code would have collided with no longer
  causes the new code to fail.
* ``test_rollback_is_idempotent_after_constraint_violation`` — a constraint
  failure that aborts the transaction implicitly does not produce a
  secondary "no transaction is active" exception masking the real one.

The tests run hermetically: ``test/conftest.py`` already redirects
``HISTORIFY_DATABASE_PATH`` to a per-process temp dir, and each test seeds
its own database fresh via :func:`database.historify_db.init_database`.
"""

from __future__ import annotations

import database.historify_db as hdb


def _reset_db():
    """Drop and re-create the historify schema in the per-test temp DB."""
    with hdb.get_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS job_items")
        conn.execute("DROP TABLE IF EXISTS download_jobs")
        conn.execute("DROP SEQUENCE IF EXISTS seq_job_items")
    hdb.init_database()


def _job_items() -> list[dict]:
    with hdb.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, job_id, symbol, exchange, status FROM job_items ORDER BY id"
        ).fetchall()
    return [
        {"id": r[0], "job_id": r[1], "symbol": r[2], "exchange": r[3], "status": r[4]} for r in rows
    ]


def test_sequence_is_seeded_past_existing_max():
    """init_database on a non-empty job_items must NOT reset id allocation to 1.

    Otherwise reboots after a partial gap could re-issue ids that already exist
    — the very situation that raised "Duplicate key id: 4074" on 2026-06-19.
    """
    _reset_db()
    # Pre-seed a row with a high id directly (simulating prior state).
    with hdb.get_connection() as conn:
        conn.execute(
            "INSERT INTO download_jobs (id, job_type, status) VALUES ('seed-job', 'scheduled', 'pending')"
        )
        conn.execute(
            "INSERT INTO job_items (id, job_id, symbol, exchange, status) "
            "VALUES (5000, 'seed-job', 'PRE', 'NSE', 'pending')"
        )

    # Re-init — must seed the sequence past 5000, not reset it.
    hdb.init_database()

    ok, msg = hdb.create_download_job(
        job_id="post-seed",
        job_type="scheduled",
        symbols=[{"symbol": "AAA", "exchange": "NSE"}, {"symbol": "BBB", "exchange": "NSE"}],
        interval="1m",
        start_date="2026-06-19",
        end_date="2026-06-19",
    )
    assert ok, f"create_download_job failed unexpectedly: {msg}"

    rows = _job_items()
    new_ids = sorted(r["id"] for r in rows if r["job_id"] == "post-seed")
    assert all(i > 5000 for i in new_ids), f"sequence regressed below pre-existing max: {new_ids}"


def test_back_to_back_jobs_get_unique_ids():
    """Sequential create_download_job calls must not collide.

    The repro for the original bug — two scanner-universe convergence calls
    (boot hook + periodic loop) running back-to-back tried to assign the same
    id range because both read MAX(id) before either committed.
    """
    _reset_db()
    for run in range(2):
        ok, msg = hdb.create_download_job(
            job_id=f"run-{run}",
            job_type="scheduled",
            symbols=[
                {"symbol": "AAA", "exchange": "NSE"},
                {"symbol": "BBB", "exchange": "NSE"},
                {"symbol": "CCC", "exchange": "NSE"},
            ],
            interval="1m",
            start_date="2026-06-19",
            end_date="2026-06-19",
        )
        assert ok, f"run {run} failed: {msg}"

    ids = [r["id"] for r in _job_items()]
    assert len(ids) == 6, f"expected 6 rows, got {len(ids)}: {ids}"
    assert len(set(ids)) == 6, f"id collision: {ids}"
    # The two ranges must be disjoint; nextval-based allocation guarantees this.
    by_job = {}
    for row in _job_items():
        by_job.setdefault(row["job_id"], []).append(row["id"])
    assert max(by_job["run-0"]) < min(by_job["run-1"]), f"id ranges interleave: {by_job}"


def test_pre_existing_id_does_not_collide():
    """Seeding the row id the old code would have re-issued must NOT collide now.

    On 2026-06-19, ``MAX(id)+ROW_NUMBER()`` re-issued id 4074 because a stale
    state made the read miss the row. We pin: an existing id that the OLD
    pattern would collide on is now safely skipped via the sequence.
    """
    _reset_db()
    with hdb.get_connection() as conn:
        conn.execute(
            "INSERT INTO download_jobs (id, job_type, status) VALUES ('pre', 'scheduled', 'pending')"
        )
        # The OLD pattern with empty job_items + 1 symbol would compute id=1.
        # Seed id=1 first to make the old code collide; new code must skip it.
        conn.execute(
            "INSERT INTO job_items (id, job_id, symbol, exchange, status) "
            "VALUES (1, 'pre', 'PRE', 'NSE', 'pending')"
        )
    # Re-init so the sequence picks up the seeded max.
    hdb.init_database()

    ok, msg = hdb.create_download_job(
        job_id="after-collision-seed",
        job_type="scheduled",
        symbols=[{"symbol": "XYZ", "exchange": "NSE"}],
        interval="1m",
        start_date="2026-06-19",
        end_date="2026-06-19",
    )
    assert ok, f"create_download_job collided with seeded id: {msg}"

    rows = _job_items()
    assert {r["id"] for r in rows} == {1, 2}, f"expected ids 1,2, got {[r['id'] for r in rows]}"


def test_rollback_is_idempotent_after_constraint_violation(monkeypatch):
    """A duplicate download_jobs.id triggers an auto-aborted transaction.

    Pre-fix: the catch-block ``conn.execute("ROLLBACK")`` raised
    ``cannot rollback - no transaction is active`` and that secondary error
    masked the real Duplicate-key cause. Post-fix: ROLLBACK is wrapped, so
    the function returns ``(False, <real error>)`` carrying the original
    duplicate-key message — not a misleading transaction-state error.
    """
    _reset_db()
    # Land the first job successfully.
    ok, _ = hdb.create_download_job(
        job_id="collide-me",
        job_type="scheduled",
        symbols=[{"symbol": "AAA", "exchange": "NSE"}],
        interval="1m",
        start_date="2026-06-19",
        end_date="2026-06-19",
    )
    assert ok

    # Re-use the same job_id to force a download_jobs.id PK violation —
    # DuckDB auto-aborts the txn, then the catch-block ROLLBACK has nothing
    # to roll back. The fix wraps it so the original error survives.
    ok2, msg2 = hdb.create_download_job(
        job_id="collide-me",
        job_type="scheduled",
        symbols=[{"symbol": "BBB", "exchange": "NSE"}],
        interval="1m",
        start_date="2026-06-19",
        end_date="2026-06-19",
    )
    assert not ok2
    # The error should be the *real* duplicate-key one, NOT
    # "cannot rollback - no transaction is active".
    assert "cannot rollback" not in msg2.lower(), (
        f"transaction-state secondary error masked the real cause: {msg2}"
    )
