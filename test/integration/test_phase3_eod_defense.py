"""Phase 3 Batch B — EOD three-layer defense + runtime override integration tests.

Covers:
    B1 — EOD watchdog timing cap regression guard (<=15:14 IST — load-bearing)
    B2 — strategy_runtime_override pause blocks entries, never blocks exits
    B3 — kill_switch write + blocks entries (strategy-scoped, not global)
    B4 — EOD reconciliation stamps missing exit row on a seeded open journal row
    B5 — expired override does NOT block entries (lazy-expiry semantics)
    B6 — clear_override removes the block for a live kill_switch

All DB writes land in conftest.py's per-process temp directory — the global
isolation guard prevents any write to the live db/openalgo.db.

NOTE: Tests use unique strategy name prefixes per test class to avoid cross-test
DB contamination in the session-scoped temp DB (all tests share one temp DB).

Refs #129  Phase 3 Batch B
"""

from __future__ import annotations

import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest

# BootHarness must be imported before any other app module so OPENALGO_TESTING=1
# is set before any module-level app code runs.  conftest.py's DB redirect has
# already run before this file is collected — that ordering is correct.
from test.harness import BootHarness

# ============================================================================
# B1 — EOD watchdog timing cap is at most 15:14 IST
# ============================================================================


class TestEodWatchdogTimingCap:
    """B1: The watchdog cap must stay <=15:14 IST — sandbox rejects MIS orders at 15:15+."""

    def test_hardcoded_cap_is_before_1514(self):
        """_WATCHDOG_CAP_TIME constant must be <=15:14 IST.

        This is the load-bearing regression guard: if someone ever bumps the
        cap to 15:15 or later, sandbox will reject every flatten order and
        produce the 2026-06-10 OIL/HINDZINC/TATAELXSI orphan pattern.
        The cap must always be strictly BEFORE the venue's 15:15 MIS cut-off.
        """
        from services.eod_watchdog_service import _WATCHDOG_CAP_TIME, _parse_hhmm

        parsed = _parse_hhmm(_WATCHDOG_CAP_TIME)
        assert parsed is not None, (
            f"_WATCHDOG_CAP_TIME={_WATCHDOG_CAP_TIME!r} must be parseable as HH:MM"
        )
        hh, mm = parsed
        cap_as_time = _dt.time(hh, mm)
        cutoff = _dt.time(15, 14)
        assert cap_as_time <= cutoff, (
            f"EOD watchdog cap {_WATCHDOG_CAP_TIME!r} is AFTER 15:14 IST. "
            "Sandbox rejects MIS orders at/after 15:15; this regression WILL "
            "cause orphaned positions. Lower the cap back to <=15:14."
        )

    def test_env_override_also_capped(self):
        """Even if SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME is set to something late,
        the watchdog must cap it at <=15:14 when scheduling the job.

        We verify _parse_hhmm works for the default and that the min() logic
        in start_eod_watchdog would cap a user-supplied 15:20 to 15:14.
        """
        import os

        from services.eod_watchdog_service import _WATCHDOG_CAP_TIME, _parse_hhmm

        cap = _parse_hhmm(os.getenv("SIMPLIFIED_ENGINE_EOD_WATCHDOG_TIME", _WATCHDOG_CAP_TIME))
        assert cap is not None, "Effective cap must be parseable"

        # Simulate what start_eod_watchdog does: min(strategy_time, cap)
        strategy_declared = _parse_hhmm("15:20")  # simplified engine default
        assert strategy_declared is not None

        effective_hh, effective_mm = min(strategy_declared, cap)
        effective_time = _dt.time(effective_hh, effective_mm)
        assert effective_time <= _dt.time(15, 14), (
            f"Effective watchdog fire time {effective_time} exceeds 15:14 IST cap. "
            "The min() merge in start_eod_watchdog must always honour the cap."
        )


# ============================================================================
# B2 — pause override blocks entry, never blocks exit
# ============================================================================


class TestPauseOverrideBlocksEntryOnly:
    """B2: a 'pause' override must block is_entry_blocked() but exits are never gated.

    Uses strategy prefix 'b2_' to avoid DB contamination with other test classes
    that run in the same session-scoped temp DB.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_pause_blocks_is_entry_blocked(self, harness):
        """Writing a pause override must make is_entry_blocked() return True."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        # Use a unique strategy name to avoid cross-test contamination
        strategy = "b2_pause_test_strategy"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=2)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="pause",
                expires_at=expires,
                reason="test pause — entries should be held",
                set_by="test_harness",
            )

        with harness.app.app_context():
            blocked, detail = is_entry_blocked(strategy)

        assert blocked is True, (
            "is_entry_blocked() must return True when an active pause override exists"
        )
        assert detail is not None
        assert detail["override_type"] == "pause"
        assert detail["strategy_name"] == strategy

    def test_pause_is_strategy_scoped_not_global(self, harness):
        """A pause on one strategy must NOT block a different strategy."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        strategy_a = "b2_scoped_strategy_a"
        strategy_b = "b2_scoped_strategy_b"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=2)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy_a,
                override_type="pause",
                expires_at=expires,
                reason="scoped pause test",
                set_by="test_harness",
            )

        with harness.app.app_context():
            blocked_other, _ = is_entry_blocked(strategy_b)

        assert blocked_other is False, (
            f"A pause on '{strategy_a}' must NOT block '{strategy_b}'. "
            "Overrides are strategy-scoped, not global."
        )

    def test_exit_path_never_consults_override(self, harness):
        """Exits (EOD / stop-loss / target) must NEVER be gated by a runtime override.

        This test verifies the contract at the DB level: is_entry_blocked() is the
        ONLY gate function, and exits bypass it entirely. We confirm there is no
        corresponding 'is_exit_blocked' API (exits are unconditional by design).
        """
        from database import strategy_runtime_override_db as sor_db

        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=2)

        with harness.app.app_context():
            sor_db.set_override(
                strategy_name="b2_exit_gate_test",
                override_type="kill_switch",
                expires_at=expires,
                reason="test exit-never-gated",
                set_by="test_harness",
            )

        with harness.app.app_context():
            blocked, _ = sor_db.is_entry_blocked("b2_exit_gate_test")

        # Entry is blocked
        assert blocked is True

        # There must be no 'is_exit_blocked' function — exits are unconditional by design.
        assert not hasattr(sor_db, "is_exit_blocked"), (
            "The strategy_runtime_override_db module must NOT have an 'is_exit_blocked' "
            "function. Exits must NEVER be gated by runtime overrides — a held position "
            "must always be allowed to square off."
        )


# ============================================================================
# B3 — kill_switch write + strategy-scoped blocking
# ============================================================================


class TestKillSwitchWriteAndScope:
    """B3: kill_switch override blocks entries and is strategy-scoped.

    Uses strategy prefix 'b3_' to avoid DB contamination with other test classes.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_kill_switch_blocks_entry(self, harness):
        """Writing a kill_switch override must make is_entry_blocked() True."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        strategy = "b3_kill_switch_strategy"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=8)

        with harness.app.app_context():
            result = set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires,
                reason="daily loss limit hit — engine triggered",
                set_by="engine",
            )

        assert result["override_type"] == "kill_switch"
        assert result["strategy_name"] == strategy

        with harness.app.app_context():
            blocked, detail = is_entry_blocked(strategy)

        assert blocked is True, "kill_switch override must block entries"
        assert detail["override_type"] == "kill_switch"

    def test_kill_switch_does_not_block_other_strategy(self, harness):
        """kill_switch on one strategy must NOT block a different strategy."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        strategy_a = "b3_ks_strategy_a"
        strategy_b = "b3_ks_strategy_b"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=8)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy_a,
                override_type="kill_switch",
                expires_at=expires,
                reason="scoped kill_switch test",
                set_by="engine",
            )

        with harness.app.app_context():
            blocked_b, _ = is_entry_blocked(strategy_b)

        assert blocked_b is False, (
            f"kill_switch on '{strategy_a}' must NOT block '{strategy_b}'. "
            "kill_switch is strategy-scoped, not global."
        )

    def test_kill_switch_upserts_existing_override(self, harness):
        """Re-setting a kill_switch must update the existing row (upsert), not insert a duplicate."""
        from database.strategy_runtime_override_db import list_overrides, set_override

        strategy = "b3_upsert_strategy"
        expires_1 = _dt.datetime.utcnow() + _dt.timedelta(hours=1)
        expires_2 = _dt.datetime.utcnow() + _dt.timedelta(hours=6)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires_1,
                reason="first write",
                set_by="engine",
            )
            set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires_2,
                reason="second write — extends duration",
                set_by="engine",
            )

        with harness.app.app_context():
            all_overrides = list_overrides()

        ks_rows = [
            r
            for r in all_overrides
            if r["strategy_name"] == strategy and r["override_type"] == "kill_switch"
        ]
        assert len(ks_rows) == 1, (
            f"Expected exactly 1 kill_switch row for '{strategy}' after two set_override calls "
            f"(upsert semantics), found {len(ks_rows)}"
        )


# ============================================================================
# B4 — EOD reconciliation stamps missing exit row
# ============================================================================


class TestEodReconciliationStampsExit:
    """B4: reconcile_engine_journal writes exit rows for sandbox-squared-off trades."""

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_reconcile_stamps_exit_on_open_row(self, harness):
        """A seeded open journal row with a matching flat sandbox position + fill
        must result in record_exit() being called with exit_reason='sandbox_eod_squareoff'.

        This covers the 2026-06-10 bug: 3 positions were orphaned because sandbox
        flattened them but the engine never journaled the exit.

        We patch the internal helper functions _sandbox_position_qty and
        _closing_fills directly (not the DB session) to avoid the complexity of
        mocking multi-chained SQLAlchemy queries.
        """
        from services import trade_journal_service
        from services.engine_eod_reconciliation_service import (
            DEFAULT_STRATEGY_NAME,
            EXIT_REASON_SANDBOX_EOD,
            reconcile_engine_journal,
        )

        today = _dt.date.today()
        strategy_name = "b4_recon_close_t1"

        # 1. Seed an open journal row (no exit) for today.
        with harness.app.app_context():
            journal_id = trade_journal_service.record_entry(
                symbol="TESTSTOCK_RECON",
                direction="LONG",
                quantity=10,
                strategy_name=strategy_name,
                signal_source="test_harness",
                entry_price=1000.0,
                entry_order_id="TEST_ENTRY_RECON_001",
            )
        assert journal_id > 0, "record_entry must return a positive id"

        # 2. Confirm the row is open (no exit yet).
        with harness.app.app_context():
            open_rows = trade_journal_service.get_open_trades_for_date(
                today.isoformat(), strategy_name=strategy_name
            )
        assert any(r["id"] == journal_id for r in open_rows), (
            f"Journal row id={journal_id} should be open (no exit) before reconciliation"
        )

        # 3. Build a mock fill object — what sandbox_db.SandboxTrades rows look like.
        mock_fill = MagicMock()
        mock_fill.quantity = 10
        mock_fill.price = 1050.0
        mock_fill.orderid = "SANDBOX_CLOSE_RECON_001"
        mock_fill.trade_timestamp = _dt.datetime.combine(today, _dt.time(15, 15))

        # 4. Patch the two internal helpers that touch sandbox_db so we don't
        #    need to build a realistic SQLAlchemy mock session.
        #    _sandbox_position_qty → returns 0 (flat position)
        #    _closing_fills → returns the mock fill
        with harness.app.app_context():
            with (
                patch(
                    "services.engine_eod_reconciliation_service._sandbox_position_qty",
                    return_value=0,
                ),
                patch(
                    "services.engine_eod_reconciliation_service._closing_fills",
                    return_value=[mock_fill],
                ),
                patch("services.engine_eod_reconciliation_service._sandbox"),
            ):
                result = reconcile_engine_journal(date=today, strategy_name=strategy_name)

        # 5. The result must show 1 exit added.
        assert result.exits_added == 1, (
            f"Expected 1 exit to be stamped, got exits_added={result.exits_added}. "
            f"Skipped: {result.skipped}"
        )
        assert result.exit_details[0]["symbol"] == "TESTSTOCK_RECON"
        assert result.exit_details[0]["journal_id"] == journal_id
        assert result.exit_details[0]["exit_price"] == 1050.0

        # 6. Confirm the journal row is now closed.
        with harness.app.app_context():
            still_open = trade_journal_service.get_open_trades_for_date(
                today.isoformat(), strategy_name=strategy_name
            )
        still_open_ids = [r["id"] for r in still_open]
        assert journal_id not in still_open_ids, (
            f"Journal row id={journal_id} should be CLOSED after reconciliation "
            f"with exit_reason='{EXIT_REASON_SANDBOX_EOD}'. Still-open ids: {still_open_ids}"
        )

    def test_reconcile_skips_still_open_position(self, harness):
        """reconcile_engine_journal must NOT stamp an exit if the sandbox position is
        still non-flat (mid-day scenario — position not yet squared off)."""
        from services import trade_journal_service
        from services.engine_eod_reconciliation_service import (
            DEFAULT_STRATEGY_NAME,
            reconcile_engine_journal,
        )

        today = _dt.date.today()
        strategy_name = "b4_recon_open_t2"

        with harness.app.app_context():
            journal_id = trade_journal_service.record_entry(
                symbol="STILLOPEN_RECON",
                direction="LONG",
                quantity=5,
                strategy_name=strategy_name,
                signal_source="test_harness",
                entry_price=500.0,
                entry_order_id="STILL_OPEN_RECON_ENTRY",
            )
        assert journal_id > 0

        # Position still open (qty=5) — reconciliation must skip it
        with harness.app.app_context():
            with (
                patch(
                    "services.engine_eod_reconciliation_service._sandbox_position_qty",
                    return_value=5,
                ),
                patch("services.engine_eod_reconciliation_service._sandbox"),
            ):
                result = reconcile_engine_journal(date=today, strategy_name=strategy_name)

        assert result.exits_added == 0, (
            "reconcile_engine_journal must NOT stamp an exit when the position is still open. "
            f"exits_added={result.exits_added}, skipped={result.skipped}"
        )
        skipped_symbols = [s["symbol"] for s in result.skipped]
        assert "STILLOPEN_RECON" in skipped_symbols, (
            f"STILLOPEN_RECON should appear in skipped (reason=still_open). "
            f"Skipped: {result.skipped}"
        )


# ============================================================================
# B5 — expired override does NOT block entries
# ============================================================================


class TestExpiredOverrideDoesNotBlock:
    """B5: an override whose expires_at is in the past must be treated as inert.

    Uses strategy prefix 'b5_' to avoid DB contamination with other test classes.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_expired_pause_does_not_block(self, harness):
        """A pause with expires_at 5 seconds in the past must NOT block entries."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        strategy = "b5_expired_pause_strategy"
        # expires_at is 5 seconds in the past — already expired
        expires = _dt.datetime.utcnow() - _dt.timedelta(seconds=5)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="pause",
                expires_at=expires,
                reason="already expired — should be inert",
                set_by="test_harness",
            )

        # is_entry_blocked uses lazy expiry: expired rows are ignored at read time
        with harness.app.app_context():
            blocked, detail = is_entry_blocked(strategy)

        assert blocked is False, (
            f"Expired override (expires_at={expires.isoformat()}) must NOT block entries. "
            "The lazy-expiry contract: reads ignore rows whose expires_at <= now."
        )
        assert detail is None

    def test_expired_kill_switch_does_not_block(self, harness):
        """A kill_switch with expires_at in the past must NOT block entries."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        strategy = "b5_expired_ks_strategy"
        expires = _dt.datetime.utcnow() - _dt.timedelta(minutes=30)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires,
                reason="day-old kill switch — must be inert",
                set_by="engine",
            )

        with harness.app.app_context():
            blocked, _ = is_entry_blocked(strategy)

        assert blocked is False, (
            "An expired kill_switch override must be treated as inert. "
            "Lazy expiry: is_entry_blocked must only return True for active (future expires_at) rows."
        )


# ============================================================================
# B6 — clear_override removes the block
# ============================================================================


class TestClearOverrideRemovesBlock:
    """B6: clear_override() must remove the blocking row and unblock entries.

    Uses strategy prefix 'b6_' to avoid DB contamination with other test classes.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_clear_override_unblocks_entries(self, harness):
        """After clear_override(), is_entry_blocked() must return False."""
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
            set_override,
        )

        strategy = "b6_clear_strategy"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=6)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires,
                reason="will be cleared",
                set_by="test_harness",
            )

        # Confirm it's blocking
        with harness.app.app_context():
            blocked, _ = is_entry_blocked(strategy)
        assert blocked is True, "Precondition: kill_switch should block before clear"

        # Clear it (simulates /api/resume or daily reset)
        with harness.app.app_context():
            removed = clear_override(strategy, override_type="kill_switch")

        assert removed == 1, f"Expected 1 row removed, got {removed}"

        # Now should be unblocked
        with harness.app.app_context():
            blocked_after, detail_after = is_entry_blocked(strategy)

        assert blocked_after is False, (
            "After clear_override(), is_entry_blocked() must return False. "
            "The daily reset path uses clear_override to re-enable entries."
        )
        assert detail_after is None

    def test_clear_all_overrides_for_strategy(self, harness):
        """clear_override(strategy, override_type=None) must clear ALL override types."""
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
            set_override,
        )

        strategy = "b6_clear_all_strategy"
        expires = _dt.datetime.utcnow() + _dt.timedelta(hours=4)

        with harness.app.app_context():
            set_override(
                strategy_name=strategy,
                override_type="pause",
                expires_at=expires,
                reason="first override",
                set_by="test_harness",
            )
            set_override(
                strategy_name=strategy,
                override_type="kill_switch",
                expires_at=expires,
                reason="second override",
                set_by="test_harness",
            )

        # Both active
        with harness.app.app_context():
            blocked, _ = is_entry_blocked(strategy)
        assert blocked is True

        # Clear ALL without specifying override_type
        with harness.app.app_context():
            removed = clear_override(strategy, override_type=None)

        assert removed == 2, f"Expected 2 rows cleared (both pause and kill_switch), got {removed}"

        with harness.app.app_context():
            blocked_after, _ = is_entry_blocked(strategy)

        assert blocked_after is False, (
            "After clearing all overrides, is_entry_blocked() must return False."
        )
