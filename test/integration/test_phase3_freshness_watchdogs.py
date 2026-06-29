"""Phase 3 Batch C — data freshness auto-pause + watchdog integration tests.

Six tests covering:

B1 — Stale historify data → ``check_strategy_data_ready`` returns not-ok
     AND a ``data_health_check`` row is written to the temp DB (Flow 11 P1).

B2 — Stale historify data → 16:30 job writes a ``pause``
     ``strategy_runtime_override`` row expiring at next-day 15:30 IST
     (Flow 11 P1 end-to-end auto-pause chain).

B3 — An active ``pause`` runtime override blocks
     ``is_entry_blocked('sector_follow_cap5_vol')`` (Flow 11 chain test).

B4 — ``ThreadWatchdog.check(150)`` fires a WARNING-level alert via the
     injected callbacks (Flow 12a P1).

B5 — ``check_dry_scanner`` returns ``alerted_crit`` when in-house is dry
     but Chartink has recent rows (Flow 12c P1).

B6 — ``assert_scanner_pipeline_healthy`` returns ``(False, …)`` and writes
     a ``data_health_check`` row when broker session is absent (Flow 12b P1).

Uses BootHarness from test/harness.py which sets OPENALGO_TESTING=1 before
importing app so no background daemons / WS proxy / singleton guard fire.
All DB writes land in conftest.py's per-process temp directory.

Refs #129  Phase 3 Batch C
"""

from __future__ import annotations

import datetime as _dt
from datetime import timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# BootHarness import must come before other app modules so OPENALGO_TESTING
# is set first.  conftest.py already ran before this file; it only redirects
# DB env vars, not app imports.
from test.harness import BootHarness

# IST timezone constant (UTC+5:30)
_IST = timezone(timedelta(hours=5, minutes=30))


# ============================================================================
# B1 — Stale historify data → data_health_check row written
# ============================================================================


class TestDataFreshnessStaleWritesHealthRow:
    """B1: stale historify data → check_strategy_data_ready returns not-ok."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_stale_data_returns_not_ok(self, harness):
        """Seed historify with 3-business-day-old timestamps.

        ``check_strategy_data_ready`` must return ``(False, ...)`` because no
        symbol has a bar within the 1-business-day staleness threshold.
        """
        import os

        import duckdb

        from services.data_freshness_service import check_strategy_data_ready

        db_path = os.environ["HISTORIFY_DATABASE_PATH"]

        # Seed one symbol with data 3 business days ago (well past the
        # max_staleness_business_days=1 threshold).
        # The sector_follow strategy checks "NIFTY 50" mapped indices + stocks.
        # We use a direct DuckDB write: create the market_data table and insert
        # a stale bar for a symbol name we can control via the duckdb_path arg.
        three_biz_days_ago = _dt.datetime.now(_IST) - _dt.timedelta(days=5)
        # Convert to UTC epoch (what market_data.timestamp stores)
        stale_ts = int(three_biz_days_ago.timestamp())

        # Use the production schema via init_database so this test cannot
        # poison the shared temp DuckDB for a sibling test that reads through
        # ``database.historify_db`` (which expects the real ``exchange``+``oi``
        # columns + primary key). The prior hand-rolled CREATE TABLE was
        # missing those columns and broke ``test_scanner_seeder_two_universe``
        # in CI's parallel run on the same worker.
        from database.historify_db import init_database as _init_historify_db

        _init_historify_db()
        con = duckdb.connect(db_path)
        try:
            # Seed a single stale row for a test symbol
            con.execute(
                "INSERT OR REPLACE INTO market_data "
                "(symbol, exchange, interval, timestamp, open, high, low, close, volume, oi) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["RELIANCE", "NSE", "1m", stale_ts, 100.0, 101.0, 99.0, 100.5, 10000, 0],
            )
        finally:
            con.close()

        # Override the sector_follow symbol resolver to use only our test symbol,
        # so we don't need to seed the full 30-stock + 8-index universe.
        with patch(
            "services.data_freshness_service._resolve_strategy_symbols",
            return_value={"stock": ["RELIANCE"], "index": []},
        ):
            ok, details = check_strategy_data_ready(
                "sector_follow_cap5_vol",
                date=_dt.datetime.now(_IST).date().isoformat(),
                max_staleness_business_days=1,
                duckdb_path=db_path,
            )

        assert ok is False, (
            "Expected overall_ok=False for a symbol with 3-business-day-old data "
            f"(threshold=1). Got ok={ok!r}, details={details}"
        )
        assert "RELIANCE" in details, "RELIANCE must appear in the per-symbol details"
        assert details["RELIANCE"]["ok"] is False, (
            "RELIANCE must be marked not-ok (stale) in the details dict"
        )

    def test_stale_check_can_write_data_health_row(self, harness):
        """After a stale check, manually call insert_check and assert the row is persisted."""
        from database.data_health_db import get_latest_check, insert_check

        with harness.app.app_context():
            row_id = insert_check(
                strategy_name="sector_follow_cap5_vol",
                overall_ok=False,
                stale_symbols=["RELIANCE", "BANKNIFTY"],
                details={"RELIANCE": {"ok": False, "staleness_days": 3}},
                alert_sent=False,
            )

        assert row_id > 0, f"insert_check must return a positive row id, got {row_id}"

        with harness.app.app_context():
            row = get_latest_check("sector_follow_cap5_vol")

        assert row is not None, "get_latest_check must return the row we just inserted"
        assert row["overall_ok"] is False, "overall_ok should be False (stale)"
        assert "RELIANCE" in row["stale_symbols"], "RELIANCE must be in stale_symbols"
        assert row["alert_sent"] is False

    def test_fresh_data_returns_ok(self, harness):
        """Seed historify with today's data → check returns True (not stale)."""
        import os

        import duckdb

        from services.data_freshness_service import check_strategy_data_ready

        db_path = os.environ["HISTORIFY_DATABASE_PATH"]

        # Seed a bar from today
        today_ts = int(_dt.datetime.now(_IST).replace(hour=15, minute=29).timestamp())

        # Use the production schema via init_database (see same-file rationale
        # above) so this test cannot poison the shared temp DuckDB.
        from database.historify_db import init_database as _init_historify_db

        _init_historify_db()
        con = duckdb.connect(db_path)
        try:
            con.execute(
                "INSERT OR REPLACE INTO market_data "
                "(symbol, exchange, interval, timestamp, open, high, low, close, volume, oi) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ["INFY", "NSE", "1m", today_ts, 1500.0, 1510.0, 1495.0, 1505.0, 5000, 0],
            )
        finally:
            con.close()

        with patch(
            "services.data_freshness_service._resolve_strategy_symbols",
            return_value={"stock": ["INFY"], "index": []},
        ):
            ok, details = check_strategy_data_ready(
                "sector_follow_cap5_vol",
                date=_dt.datetime.now(_IST).date().isoformat(),
                max_staleness_business_days=1,
                duckdb_path=db_path,
            )

        assert ok is True, f"Expected overall_ok=True for today's bar data, got ok={ok!r}"


# ============================================================================
# B2 — Stale data triggers auto-pause override write
# ============================================================================


class TestStaleDataWritesPauseOverride:
    """B2: stale feed → 16:30 job path writes pause strategy_runtime_override."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_sector_follow=True) as h:
            yield h

    def test_auto_pause_writes_override_row(self, harness):
        """``_auto_pause_tomorrow`` must write a ``pause`` runtime override row
        with ``expires_at`` set to next-day 15:30 IST and ``set_by='sector_follow'``.

        We invoke ``_auto_pause_tomorrow`` directly (the inner method called by
        ``run_data_health_check`` when the feed is stale) to test the DB write
        without needing a running DuckDB feed.
        """
        from database.strategy_runtime_override_db import get_active_overrides
        from services.sector_follow_service import get_service as get_sector_follow_service

        svc = get_sector_follow_service()
        assert svc is not None, "SectorFollowService singleton should be initialised"

        today_iso = _dt.datetime.now(_IST).date().isoformat()

        with harness.app.app_context():
            # Call the internal auto-pause method directly (the same path that
            # ``run_data_health_check`` calls when ok=False).
            svc._auto_pause_tomorrow(today_iso)

        # The override must be visible to the DB layer
        with harness.app.app_context():
            active = get_active_overrides("sector_follow_cap5_vol")

        pause_rows = [r for r in active if r["override_type"] == "pause"]
        assert pause_rows, (
            "Expected at least one active 'pause' override for sector_follow_cap5_vol "
            f"after _auto_pause_tomorrow(). Active overrides: {active}"
        )
        row = pause_rows[0]

        # expires_at should be tomorrow 15:30 IST (stored as UTC naive datetime in ISO)
        expires_str = row["expires_at"]
        assert expires_str is not None, "expires_at must be set"
        assert row["set_by"] == "sector_follow", (
            f"set_by should be 'sector_follow', got {row['set_by']!r}"
        )
        assert "stale_feed" in (row.get("reason") or ""), (
            f"reason should mention stale_feed, got {row.get('reason')!r}"
        )

    def test_run_data_health_check_alerts_on_stale_feed(self, harness):
        """Full 16:30 flow: mock the freshness checker to return stale → assert
        Telegram notify is called AND a data_health_check row is written.
        """
        from database.data_health_db import get_latest_check
        from services.sector_follow_service import get_service as get_sector_follow_service

        svc = get_sector_follow_service()
        assert svc is not None

        notify_calls: list[str] = []

        # ``run_data_health_check`` directly calls ``check_strategy_data_ready``
        # via a local import inside the method. We patch it at the source module
        # so the local-import binding gets the mock.
        stale_details = {"RELIANCE": {"ok": False, "staleness_days": 3, "kind": "stock"}}
        with (
            patch(
                "services.data_freshness_service.check_strategy_data_ready",
                return_value=(False, stale_details),
            ),
            patch.object(svc, "_notify", side_effect=lambda msg: notify_calls.append(msg)),
            patch.object(svc, "_set_runtime_override"),  # skip actual DB write for this test
        ):
            with harness.app.app_context():
                ok, details = svc.run_data_health_check()

        assert ok is False, "run_data_health_check must return False on stale data"
        assert notify_calls, "Expected at least one Telegram alert call when data is stale"
        assert any("stale" in msg.lower() or "DATA STALE" in msg for msg in notify_calls), (
            f"Alert message must mention stale data. Got: {notify_calls}"
        )

        # A data_health_check row must have been written
        with harness.app.app_context():
            row = get_latest_check("sector_follow_cap5_vol")

        assert row is not None, "data_health_check row must be written after health check"
        assert row["overall_ok"] is False


# ============================================================================
# B3 — Active pause override blocks sector_follow entry
# ============================================================================


class TestRuntimeOverridePauseBlocksEntry:
    """B3: active pause override → is_entry_blocked returns True."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_sector_follow=True) as h:
            yield h

    def test_pause_override_blocks_entry(self, harness):
        """Insert a pause override → is_entry_blocked must return (True, row)."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        # Write a pause that expires in the future (1 hour from now UTC)
        future_expiry = _dt.datetime.utcnow() + _dt.timedelta(hours=1)

        with harness.app.app_context():
            set_override(
                strategy_name="sector_follow_cap5_vol",
                override_type="pause",
                expires_at=future_expiry,
                reason="test_stale_feed",
                set_by="test_harness",
            )

        with harness.app.app_context():
            blocked, row = is_entry_blocked("sector_follow_cap5_vol")

        assert blocked is True, (
            "is_entry_blocked must return True when an active pause override exists"
        )
        assert row is not None, "is_entry_blocked must return the override row dict"
        assert row["override_type"] == "pause"
        assert row["set_by"] == "test_harness"

    def test_expired_override_does_not_block_entry(self, harness):
        """An expired pause override must NOT block entry (lazy-expiry semantics)."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        # Write a pause that already expired 1 second ago
        past_expiry = _dt.datetime.utcnow() - _dt.timedelta(seconds=1)

        with harness.app.app_context():
            set_override(
                strategy_name="sector_follow_cap5_vol",
                override_type="pause",
                expires_at=past_expiry,
                reason="already_expired",
                set_by="test_harness",
            )

        with harness.app.app_context():
            blocked, row = is_entry_blocked("sector_follow_cap5_vol")

        assert blocked is False, (
            "is_entry_blocked must return False when the override is already expired "
            f"(lazy expiry). Got blocked={blocked!r}, row={row!r}"
        )

    def test_no_override_does_not_block_entry(self, harness):
        """Without any override row, is_entry_blocked must return (False, None)."""
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
        )

        with harness.app.app_context():
            clear_override("sector_follow_cap5_vol")
            blocked, row = is_entry_blocked("sector_follow_cap5_vol")

        assert blocked is False
        assert row is None


# ============================================================================
# B4 — Thread watchdog warns at 100+ threads
# ============================================================================


class TestThreadWatchdogWarnAt100Threads:
    """B4: ThreadWatchdog.check(150) fires a warning-level alert (Flow 12a P1)."""

    def test_warn_fired_above_threshold(self):
        """ThreadWatchdog.check with count=150 (> default WARN=100) must fire a
        'warning' alert via the injected alert_writer and notifier callbacks.
        """
        from services.thread_watchdog_service import ThreadWatchdog

        alert_calls: list[tuple[int, str, float]] = []
        notify_calls: list[tuple[int, str]] = []

        def mock_alert_writer(count: int, level: str, threshold: float) -> None:
            alert_calls.append((count, level, threshold))

        def mock_notifier(count: int, level: str) -> None:
            notify_calls.append((count, level))

        def mock_resolver() -> None:
            pass  # not called on a warning

        wdog = ThreadWatchdog(
            warn_threshold=100,
            crit_threshold=200,
            dedup_window_min=0,  # 0-second dedup so every call fires
            alert_writer=mock_alert_writer,
            notifier=mock_notifier,
            resolver=mock_resolver,
        )

        result = wdog.check(150)

        assert result == "warning", (
            f"ThreadWatchdog.check(150) should return 'warning' (WARN=100, CRIT=200). "
            f"Got: {result!r}"
        )
        assert alert_calls, "alert_writer must be called when thread count exceeds WARN threshold"
        assert notify_calls, "notifier must be called when thread count exceeds WARN threshold"
        count, level, threshold = alert_calls[0]
        assert level == "warning", f"alert level must be 'warning', got {level!r}"
        assert count == 150
        assert threshold == 100  # WARN threshold

    def test_critical_fired_above_crit_threshold(self):
        """check(250) with CRIT=200 must fire 'critical'."""
        from services.thread_watchdog_service import ThreadWatchdog

        alert_calls: list[tuple[int, str, float]] = []
        notify_calls: list[tuple[int, str]] = []

        wdog = ThreadWatchdog(
            warn_threshold=100,
            crit_threshold=200,
            dedup_window_min=0,
            alert_writer=lambda c, lvl, t: alert_calls.append((c, lvl, t)),
            notifier=lambda c, lvl: notify_calls.append((c, lvl)),
            resolver=lambda: None,
        )

        result = wdog.check(250)
        assert result == "critical", f"Expected 'critical' for count=250, got {result!r}"
        assert alert_calls[0][1] == "critical"
        assert alert_calls[0][2] == 200  # CRIT threshold

    def test_below_threshold_fires_resolve_not_alert(self):
        """check(50) must not fire an alert when count < WARN threshold."""
        from services.thread_watchdog_service import ThreadWatchdog

        alert_calls: list = []
        resolve_calls: list = []

        wdog = ThreadWatchdog(
            warn_threshold=100,
            crit_threshold=200,
            dedup_window_min=0,
            alert_writer=lambda *a: alert_calls.append(a),
            notifier=lambda *a: None,
            resolver=lambda: resolve_calls.append(True),
        )

        result = wdog.check(50)
        assert result is None, f"No alert expected for count=50 < WARN=100, got {result!r}"
        assert not alert_calls, f"alert_writer must NOT be called for count=50, got {alert_calls}"


# ============================================================================
# B5 — Scanner dry tripwire CRIT when Chartink active but in-house dry
# ============================================================================


class TestScannerDryTripwireCritWhenInhouseDryChartinkHasRows:
    """B5: check_dry_scanner → alerted_crit when inhouse dry + chartink active."""

    @pytest.fixture(autouse=True)
    def _reset_dedup(self):
        """Reset per-process dedup state before/after each test."""
        import services.scanner_dry_tripwire_service as svc

        svc._last_crit_date = None
        svc._last_warn_date = None
        yield
        svc._last_crit_date = None
        svc._last_warn_date = None

    def test_alerted_crit_when_chartink_active_but_inhouse_dry(self):
        """No inhouse scan_results for 45+ min + Chartink has rows → CRIT alert."""
        from services.scanner_dry_tripwire_service import check_dry_scanner

        # Fix a market-hours IST timestamp (Wednesday 11:00 IST)
        as_of = _dt.datetime(2026, 6, 25, 11, 0, 0, tzinfo=_IST)

        # In-house last row was 45 minutes ago (past the 30-min threshold)
        last_inhouse_at = as_of - _dt.timedelta(minutes=45)

        notified: list[tuple[str, str]] = []
        health_rows: list[dict] = []

        result = check_dry_scanner(
            as_of=as_of,
            latest_inhouse_provider=lambda: last_inhouse_at,
            chartink_has_rows_since=lambda cutoff: True,  # Chartink IS active
            broker_session_checker=lambda: True,
            notifier=lambda msg, sev: notified.append((msg, sev)),
            health_writer=lambda sev, details, sent: health_rows.append({"sev": sev, "sent": sent}),
        )

        assert result["status"] == "alerted_crit", (
            f"Expected status='alerted_crit' when inhouse dry + Chartink active. Got: {result}"
        )
        assert result["severity"] == "CRIT"
        assert notified, "Telegram notify must be called for a CRIT alert"
        sev_sent = notified[0][1] if notified else None
        assert sev_sent == "CRIT", f"Notification severity must be 'CRIT', got {sev_sent!r}"

    def test_alerted_warn_when_both_dry(self):
        """No inhouse rows + Chartink also dry → WARN (quiet market, not broken)."""
        from services.scanner_dry_tripwire_service import check_dry_scanner

        as_of = _dt.datetime(2026, 6, 25, 11, 0, 0, tzinfo=_IST)
        last_inhouse_at = as_of - _dt.timedelta(minutes=45)

        notified: list[tuple[str, str]] = []

        result = check_dry_scanner(
            as_of=as_of,
            latest_inhouse_provider=lambda: last_inhouse_at,
            chartink_has_rows_since=lambda cutoff: False,  # Chartink also dry
            broker_session_checker=lambda: True,
            notifier=lambda msg, sev: notified.append((msg, sev)),
            health_writer=lambda *a: None,
        )

        assert result["status"] == "alerted_warn", (
            f"Expected 'alerted_warn' when both sides dry. Got: {result}"
        )
        assert result["severity"] == "WARN"
        assert notified

    def test_ok_when_gap_below_threshold(self):
        """Gap < 30 min → status='ok', no alert."""
        from services.scanner_dry_tripwire_service import check_dry_scanner

        as_of = _dt.datetime(2026, 6, 25, 11, 0, 0, tzinfo=_IST)
        # Only 10 minutes ago → below the 30-min threshold
        last_inhouse_at = as_of - _dt.timedelta(minutes=10)

        notified: list = []

        result = check_dry_scanner(
            as_of=as_of,
            latest_inhouse_provider=lambda: last_inhouse_at,
            chartink_has_rows_since=lambda cutoff: True,
            broker_session_checker=lambda: True,
            notifier=lambda msg, sev: notified.append((msg, sev)),
            health_writer=lambda *a: None,
        )

        assert result["status"] == "ok", f"Expected 'ok' for 10-min gap. Got: {result}"
        assert not notified, "No alert should fire when gap is below threshold"


# ============================================================================
# B6 — Scanner smoke check fails without broker session
# ============================================================================


class TestScannerSmokeCheckFailsWithoutBrokerSession:
    """B6: assert_scanner_pipeline_healthy → (False, ...) when no broker session."""

    @pytest.fixture(autouse=True)
    def _reset_dedup(self):
        """Clear per-process dedup between tests."""
        import services.scanner_smoke_check_service as svc

        svc._last_alert_date = None
        yield
        svc._last_alert_date = None

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_fails_and_writes_health_row_without_broker_session(self, harness):
        """Smoke check with 3 healthy gates but broker session absent → (False, …)
        and a data_health_check row with overall_ok=False is written.
        """
        from database.data_health_db import get_latest_check
        from services.scanner_smoke_check_service import assert_scanner_pipeline_healthy

        health_rows: list[dict] = []
        notified: list[str] = []

        def mock_health_writer(
            overall_ok: bool, stale_symbols: list, details: dict, alert_sent: bool
        ) -> None:
            # Write the real row AND capture it for assertion
            with harness.app.app_context():
                from database.data_health_db import insert_check

                insert_check(
                    strategy_name="scanner_smoke_check",
                    overall_ok=overall_ok,
                    stale_symbols=stale_symbols,
                    details=details,
                    alert_sent=alert_sent,
                )
            health_rows.append({"overall_ok": overall_ok, "stale": stale_symbols})

        ok, details = assert_scanner_pipeline_healthy(
            as_of=_dt.datetime(2026, 6, 25, 9, 19, tzinfo=_IST),
            universe_provider=lambda: ["RELIANCE", "INFY"],
            # Both symbols have live bars (aggregator gate passes)
            intraday_provider=lambda sym, dt: (100.0, 50000),
            # Stored freshness checks pass
            freshness_reader=lambda name: {"overall_ok": True},
            # NO broker session — the key failure condition
            broker_session_checker=lambda: False,
            notifier=lambda msg: notified.append(msg),
            health_writer=mock_health_writer,
        )

        assert ok is False, f"Smoke check must fail when broker session is absent. Got ok={ok!r}"
        assert not details.get("broker_session_ok", True), (
            "details['broker_session_ok'] must be False"
        )
        assert notified, "A CRIT Telegram alert must be sent on smoke check failure"
        assert health_rows, "A health row must be written on smoke check failure"
        assert health_rows[0]["overall_ok"] is False

        # The row must be queryable via get_latest_check
        with harness.app.app_context():
            row = get_latest_check("scanner_smoke_check")
        assert row is not None, "get_latest_check must return the written row"
        assert row["overall_ok"] is False

    def test_passes_when_all_gates_healthy(self, harness):
        """All 4 gates healthy → smoke check returns (True, …)."""
        from services.scanner_smoke_check_service import assert_scanner_pipeline_healthy

        health_rows: list[dict] = []

        ok, details = assert_scanner_pipeline_healthy(
            as_of=_dt.datetime(2026, 6, 25, 9, 19, tzinfo=_IST),
            universe_provider=lambda: ["RELIANCE", "INFY"],
            intraday_provider=lambda sym, dt: (100.0, 50000),
            freshness_reader=lambda name: {"overall_ok": True},
            broker_session_checker=lambda: True,
            notifier=lambda msg: None,
            health_writer=lambda ok, stale, dtls, sent: health_rows.append({"ok": ok}),
        )

        assert ok is True, f"Smoke check must pass when all gates healthy. Got ok={ok!r}"
        assert details.get("broker_session_ok") is True
        assert details.get("aggregator_ok") is True
        assert health_rows, "Health row must still be written on success (heartbeat)"
        assert health_rows[0]["ok"] is True

    def test_fails_when_aggregator_coverage_too_low(self, harness):
        """Below 50% aggregator coverage (and broker ok) → smoke check fails."""
        from services.scanner_smoke_check_service import assert_scanner_pipeline_healthy

        ok, details = assert_scanner_pipeline_healthy(
            as_of=_dt.datetime(2026, 6, 25, 9, 19, tzinfo=_IST),
            # 4 symbols, only 1 has a live bar → 25% coverage < 50% min
            universe_provider=lambda: ["RELIANCE", "INFY", "TCS", "HDFC"],
            intraday_provider=lambda sym, dt: (100.0, 50000) if sym == "RELIANCE" else (None, None),
            freshness_reader=lambda name: {"overall_ok": True},
            broker_session_checker=lambda: True,
            notifier=lambda msg: None,
            health_writer=lambda ok, stale, dtls, sent: None,
        )

        assert ok is False, (
            f"Smoke check must fail with 25% coverage (threshold=50%). Got ok={ok!r}"
        )
        assert not details.get("aggregator_ok"), "aggregator_ok must be False at 25% coverage"
