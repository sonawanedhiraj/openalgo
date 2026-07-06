"""Phase 4 Layer C — synthetic full trading-day smoke tests.

Three tests that orchestrate the complete seam chain the platform depends on
every trading day:

C1 — ``test_full_trading_day_smoke`` (P0)
     Happy-path orchestration: Boot → broker auth → strategy modes → tick
     injection → kill-switch check → journal entries → EOD reconciliation.
     Proves all major seams wire together end-to-end.

C2 — ``test_boot_with_stale_feed_smoke_check_aborts_entry`` (P1)
     Freshness → entry-blocked chain: writing a pause override for one strategy
     blocks its entries and does NOT affect another strategy (scope isolation).

C3 — ``test_three_strategy_mode_roundtrip`` (P0)
     All three active strategies can set and read back their persistent mode.
     Regression guard: mode persistence is a load-bearing seam — a broken
     strategy_mode table makes every engine entry go to the wrong venue.

Uses BootHarness from test/harness.py which sets OPENALGO_TESTING=1 before
importing app so no background daemons / WS proxy / singleton guard fire.
All DB writes land in conftest.py's per-process temp directory.

Refs #129  Phase 4 Layer C
"""

from __future__ import annotations

import datetime as dt
from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest

# BootHarness import must come first so OPENALGO_TESTING=1 is set before any
# other app module is imported. conftest.py's DB redirect already ran; this
# import order is load-bearing (see test/harness.py module docstring).
from test.harness import BootHarness

# ============================================================================
# C1 — Full trading-day happy-path smoke
# ============================================================================


class TestFullTradingDaySmoke:
    """C1: boot → auth → mode → ticks → kill-switch check → journal → reconcile.

    Tests that all major seams wire together for a complete synthetic day.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_sector_follow=True, init_futures_follow=True) as h:
            yield h

    def test_full_trading_day_smoke(self, harness):
        """Synthetic full-day smoke: all 6 major seams must chain correctly.

        Seam 1 — Boot: sector_follow + futures_follow APScheduler jobs registered.
        Seam 2 — Broker auth: mock_zerodha_login stores a decryptable token.
        Seam 3 — Strategy modes: all three active strategies set to sandbox.
        Seam 4 — Tick injection: 5 symbols injected (no error, fire-and-forget).
        Seam 5 — Kill-switch check: no override → is_entry_blocked returns False.
        Seam 6 — Journal entries: two rows written and visible as open.
        Seam 7 — EOD reconciliation: mocked sandbox fills → exits stamped,
                  entries_checked==2, exits_added==2, no open rows remain.
        """
        from database.strategy_runtime_override_db import is_entry_blocked
        from services import trade_journal_service
        from services.engine_eod_reconciliation_service import reconcile_engine_journal

        STRATEGY = "c1_smoke_day_engine"
        TODAY = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).date()
        TODAY_ISO = TODAY.isoformat()

        # ------------------------------------------------------------------ #
        # Seam 1 — Boot: verify both strategy job sets are registered
        # ------------------------------------------------------------------ #
        registered = set(harness.get_registered_job_ids())
        assert "sector_follow_entry" in registered, (
            "sector_follow_entry job must be registered after init_sector_follow=True"
        )
        assert "futures_follow_entry" in registered, (
            "futures_follow_entry job must be registered after init_futures_follow=True"
        )

        # ------------------------------------------------------------------ #
        # Seam 2 — Broker auth: fake Zerodha login → token decryptable
        # ------------------------------------------------------------------ #
        harness.mock_zerodha_login(token="c1_smoke_token_abc", user_id="ZR9999")
        with harness.app.app_context():
            token = harness.get_auth_token("admin")
        assert token == "c1_smoke_token_abc", (
            f"Auth token round-trip failed: expected 'c1_smoke_token_abc', got {token!r}"
        )

        # ------------------------------------------------------------------ #
        # Seam 3 — Strategy modes: set sandbox for all three strategies
        # ------------------------------------------------------------------ #
        for name in ("simplified_engine", "sector_follow", "futures_follow_cap50"):
            harness.set_strategy_mode(name, "sandbox")
        for name in ("simplified_engine", "sector_follow", "futures_follow_cap50"):
            mode = harness.get_strategy_mode(name)
            assert mode == "sandbox", (
                f"Mode for {name!r} should be 'sandbox' after set, got {mode!r}"
            )

        # ------------------------------------------------------------------ #
        # Seam 4 — Tick injection (5 symbols; fire-and-forget into aggregator)
        # ------------------------------------------------------------------ #
        five_symbols = [
            ("SBIN", 550.0),
            ("INFY", 1750.0),
            ("TATAMOTORS", 850.0),
            ("RELIANCE", 2900.0),
            ("HDFCBANK", 1700.0),
        ]
        # inject_tick is best-effort — scanner may not be initialised in test
        # mode, so we only verify it does NOT raise.
        for sym, price in five_symbols:
            harness.inject_tick(sym, price)

        # ------------------------------------------------------------------ #
        # Seam 5 — Kill-switch check: no override → entries NOT blocked
        # ------------------------------------------------------------------ #
        with harness.app.app_context():
            from database.strategy_runtime_override_db import clear_override

            clear_override(STRATEGY)
            blocked, override = is_entry_blocked(STRATEGY)
        assert not blocked, (
            f"Expected is_entry_blocked=False when no override exists, got blocked={blocked!r}"
        )
        assert override is None, f"Expected override=None when no override exists, got {override!r}"

        # ------------------------------------------------------------------ #
        # Seam 6 — Journal entries: write 2 open rows
        # ------------------------------------------------------------------ #
        with harness.app.app_context():
            jid1 = trade_journal_service.record_entry(
                symbol="SBIN",
                direction="LONG",
                quantity=10,
                strategy_name=STRATEGY,
                signal_source="smoke_test_c1",
                entry_price=548.5,
                entry_order_id="C1_ORD_001",
            )
            jid2 = trade_journal_service.record_entry(
                symbol="INFY",
                direction="LONG",
                quantity=5,
                strategy_name=STRATEGY,
                signal_source="smoke_test_c1",
                entry_price=1745.0,
                entry_order_id="C1_ORD_002",
            )
        assert jid1 > 0, f"record_entry for SBIN must return a positive id, got {jid1}"
        assert jid2 > 0, f"record_entry for INFY must return a positive id, got {jid2}"

        # Both must appear as open rows for today
        with harness.app.app_context():
            open_rows = trade_journal_service.get_open_trades_for_date(
                TODAY_ISO, strategy_name=STRATEGY
            )
        assert len(open_rows) == 2, (
            f"Expected 2 open journal rows after 2 record_entry calls, got {len(open_rows)}: "
            f"{open_rows}"
        )
        open_symbols = {r["symbol"] for r in open_rows}
        assert open_symbols == {"SBIN", "INFY"}, (
            f"Open rows must be for SBIN and INFY, got {open_symbols}"
        )

        # ------------------------------------------------------------------ #
        # Seam 7 — EOD reconciliation with mocked sandbox positions + fills
        # ------------------------------------------------------------------ #
        # Patch _sandbox_position_qty to return 0 (flat) and _closing_fills to
        # return a synthetic fill for each symbol so reconciliation closes both rows.
        _make_fill = _synthetic_fill_factory()

        sbin_fill = _make_fill("SBIN", price=552.0, qty=10, action="SELL")
        infy_fill = _make_fill("INFY", price=1760.0, qty=5, action="SELL")

        fill_map = {
            "SBIN": [sbin_fill],
            "INFY": [infy_fill],
        }

        with (
            patch(
                "services.engine_eod_reconciliation_service._sandbox_position_qty",
                side_effect=lambda sess, sandbox_db, symbol: 0,
            ),
            patch(
                "services.engine_eod_reconciliation_service._closing_fills",
                side_effect=lambda sess, sandbox_db, symbol, direction, date: fill_map.get(
                    symbol, []
                ),
            ),
            harness.app.app_context(),
        ):
            result = reconcile_engine_journal(
                date=TODAY_ISO,
                strategy_name=STRATEGY,
                dry_run=False,
            )

        assert result.entries_checked == 2, (
            f"Expected entries_checked=2, got {result.entries_checked}. Result: {result}"
        )
        assert result.exits_added == 2, (
            f"Expected exits_added=2 after mocked sandbox fills, got {result.exits_added}. "
            f"Skipped: {result.skipped}"
        )

        # After reconciliation, no open rows must remain
        with harness.app.app_context():
            remaining_open = trade_journal_service.get_open_trades_for_date(
                TODAY_ISO, strategy_name=STRATEGY
            )
        assert len(remaining_open) == 0, (
            f"Expected 0 open rows after reconciliation, got {len(remaining_open)}: "
            f"{remaining_open}"
        )

    def test_both_strategy_job_sets_present_simultaneously(self, harness):
        """When both init_sector_follow=True and init_futures_follow=True, all 11
        expected job IDs must be registered without cross-contamination.

        This is the 'both strategies boot simultaneously' coverage the Phase 3 B3/B4
        tests skipped (each was initialised in isolation).
        """
        registered = set(harness.get_registered_job_ids())

        sector_expected = {
            "sector_follow_entry",
            "sector_follow_exit",
            "sector_follow_daily_reset",
            "sector_follow_eod_summary",
            "sector_follow_data_health",
            "sector_follow_smoke_check",
        }
        futures_expected = {
            "futures_follow_daily_reset",
            "futures_follow_eod_watchdog",
            "futures_follow_entry",
            "futures_follow_exit",
            "futures_follow_eod_summary",
        }

        missing_sector = sector_expected - registered
        missing_futures = futures_expected - registered

        assert not missing_sector, (
            f"Missing sector_follow jobs when both strategies initialised: {missing_sector!r}. "
            f"All registered: {registered!r}"
        )
        assert not missing_futures, (
            f"Missing futures_follow jobs when both strategies initialised: {missing_futures!r}. "
            f"All registered: {registered!r}"
        )


# ============================================================================
# C2 — Freshness → entry-blocked chain + scope isolation
# ============================================================================


class TestBootWithStaleFeedSmokeCheckAbortsEntry:
    """C2: pause override on sector_follow blocks ITS entries and not simplified_engine.

    Tests the scope isolation of strategy_runtime_override: a pause on strategy A
    must not block strategy B.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create(init_sector_follow=True) as h:
            yield h

    def test_pause_override_blocks_targeted_strategy_only(self, harness):
        """Active pause on 'c2_sector_strategy' must block THAT strategy but not
        'c2_engine_strategy' (scope isolation — overrides are per strategy_name).
        """
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
            set_override,
        )

        SECTOR = "c2_sector_strategy"
        ENGINE = "c2_engine_strategy"

        future = dt.datetime.utcnow() + timedelta(hours=1)

        with harness.app.app_context():
            # Ensure clean slate for both strategies
            clear_override(SECTOR)
            clear_override(ENGINE)

            # Insert an active pause for the sector strategy only
            set_override(
                strategy_name=SECTOR,
                override_type="pause",
                expires_at=future,
                reason="c2_stale_feed_simulation",
                set_by="c2_test_harness",
            )

        # sector strategy must be blocked
        with harness.app.app_context():
            blocked_sector, override = is_entry_blocked(SECTOR)
        assert blocked_sector is True, (
            f"Expected sector strategy entry to be blocked after pause, got {blocked_sector!r}"
        )
        assert override is not None
        assert override["override_type"] == "pause"
        assert override["set_by"] == "c2_test_harness"

        # engine strategy must NOT be blocked (different strategy_name → different row)
        with harness.app.app_context():
            blocked_engine, _ = is_entry_blocked(ENGINE)
        assert blocked_engine is False, (
            f"Expected engine strategy NOT blocked (different scope), got {blocked_engine!r}"
        )

    def test_clear_override_unblocks_strategy(self, harness):
        """After clear_override, is_entry_blocked must return False (no longer blocked)."""
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
            set_override,
        )

        STRATEGY = "c2_clear_test_strategy"
        future = dt.datetime.utcnow() + timedelta(hours=2)

        with harness.app.app_context():
            set_override(
                strategy_name=STRATEGY,
                override_type="pause",
                expires_at=future,
                reason="c2_clear_test",
                set_by="c2_test",
            )
            blocked_before, _ = is_entry_blocked(STRATEGY)

        assert blocked_before is True, "Must be blocked before clear"

        with harness.app.app_context():
            cleared = clear_override(STRATEGY)
            blocked_after, override_after = is_entry_blocked(STRATEGY)

        assert cleared >= 1, f"clear_override must remove at least 1 row, got {cleared}"
        assert blocked_after is False, (
            f"After clear_override, is_entry_blocked must return False, got {blocked_after!r}"
        )
        assert override_after is None, (
            f"After clear_override, override dict must be None, got {override_after!r}"
        )

    def test_kill_switch_also_blocks_entry(self, harness):
        """A kill_switch override type must also block entries (same semantics as pause)."""
        from database.strategy_runtime_override_db import (
            clear_override,
            is_entry_blocked,
            set_override,
        )

        STRATEGY = "c2_kill_switch_strategy"
        future = dt.datetime.utcnow() + timedelta(hours=1)

        with harness.app.app_context():
            clear_override(STRATEGY)
            set_override(
                strategy_name=STRATEGY,
                override_type="kill_switch",
                expires_at=future,
                reason="daily_loss_exceeded",
                set_by="kill_switch_guard",
            )
            blocked, override = is_entry_blocked(STRATEGY)

        assert blocked is True, "kill_switch override must block entries"
        assert override["override_type"] == "kill_switch"

    def test_expired_override_does_not_block(self, harness):
        """An expired pause override must NOT block entries (lazy-expiry semantics)."""
        from database.strategy_runtime_override_db import is_entry_blocked, set_override

        STRATEGY = "c2_expired_test_strategy"
        past = dt.datetime.utcnow() - timedelta(seconds=1)

        with harness.app.app_context():
            set_override(
                strategy_name=STRATEGY,
                override_type="pause",
                expires_at=past,
                reason="already_expired",
                set_by="c2_test",
            )
            blocked, _ = is_entry_blocked(STRATEGY)

        assert blocked is False, (
            f"An expired override must NOT block entries (lazy expiry). Got blocked={blocked!r}"
        )


# ============================================================================
# C3 — Three-strategy mode round-trip regression guard
# ============================================================================


class TestThreeStrategyModeRoundtrip:
    """C3 (P0): all three active strategies can set and read back their mode.

    A broken strategy_mode table would silently route every engine entry to the
    wrong venue (live orders firing in sandbox or vice versa). This is the
    regression guard.
    """

    @pytest.fixture(autouse=True)
    def harness(self):
        with BootHarness.create() as h:
            yield h

    def test_all_three_strategies_sandbox_roundtrip(self, harness):
        """Set all three active strategies to 'sandbox'; every read-back must agree."""
        strategies = ["simplified_engine", "sector_follow", "futures_follow_cap50"]

        for name in strategies:
            harness.set_strategy_mode(name, "sandbox")

        for name in strategies:
            mode = harness.get_strategy_mode(name)
            assert mode == "sandbox", (
                f"Expected mode='sandbox' for {name!r} after set_strategy_mode, got {mode!r}"
            )

    def test_mode_transitions_sandbox_to_live_and_back(self, harness):
        """Each strategy can transition sandbox→live→sandbox without leaking state
        to the other strategies.
        """
        STRATS = ["simplified_engine", "sector_follow", "futures_follow_cap50"]

        # Start all in sandbox
        for name in STRATS:
            harness.set_strategy_mode(name, "sandbox")

        # Flip each one to live; verify the others stay sandbox.
        # force: this test checks per-strategy isolation of a seeded live row,
        # not the flip preflight gate.
        for target in STRATS:
            harness.set_strategy_mode(target, "live", force=True)
            for name in STRATS:
                expected = "live" if name == target else "sandbox"
                actual = harness.get_strategy_mode(name)
                assert actual == expected, (
                    f"After setting {target!r} to 'live', {name!r} should be {expected!r} "
                    f"but got {actual!r} (state leaked?)"
                )
            # Restore
            harness.set_strategy_mode(target, "sandbox")

    def test_unset_strategy_returns_none_or_default(self, harness):
        """A strategy with no DB row must return None (or possibly 'sandbox' from
        the resolver default — either is acceptable; the key is it doesn't raise).
        """
        mode = harness.get_strategy_mode("__c3_never_set_strategy__")
        # The mode layer may default to 'sandbox' or return None; both are valid.
        # What matters: it does not raise and it's not 'live'.
        assert mode != "live", f"An unset strategy must never default to 'live', got {mode!r}"

    def test_mode_write_is_visible_via_strategy_mode_db_directly(self, harness):
        """Verify that set_strategy_mode writes to strategy_mode_db in a way
        that get_mode reads back correctly (validates the DB seam directly).
        """
        from database.strategy_mode_db import _set_mode_unchecked, get_mode

        with harness.app.app_context():
            _set_mode_unchecked("c3_direct_test", "live", updated_by="c3_test")
            result = get_mode("c3_direct_test")

        assert result is not None, "get_mode must return a dict after _set_mode_unchecked"
        assert result.get("mode") == "live", (
            f"Expected mode='live' from direct DB write, got {result!r}"
        )


# ============================================================================
# Private helpers
# ============================================================================


def _synthetic_fill_factory():
    """Return a factory that builds mock SandboxTrades objects for reconciliation.

    The reconciliation service reads ``.quantity``, ``.price``, ``.orderid``,
    and ``.trade_timestamp`` from the fill rows returned by ``_closing_fills``.
    """

    def make_fill(symbol: str, price: float, qty: int, action: str):
        fill = MagicMock()
        fill.symbol = symbol
        fill.price = price
        fill.quantity = qty
        fill.action = action
        fill.orderid = f"MOCK_EOD_{symbol}"
        fill.trade_timestamp = dt.datetime.now()
        return fill

    return make_fill
