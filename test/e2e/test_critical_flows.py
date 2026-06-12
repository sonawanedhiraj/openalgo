"""End-to-end critical-flow tests.

Covers the seams most likely to silently regress, exercised through REAL
components wherever deterministic:

* mode resolution fall-through (unified → legacy → env → default)
* the unified intent gate as the engines actually read it (pause / halt / cap /
  clear), via ``resolve_strategy_mode`` against a real (temp) intent DB
* the sector_follow entry→exit cycle + kill switch + EOD report file sink
* the Phase-6 Telegram inbound bot end-to-end: a parsed command writes the intent
  table, and the SAME engine gate then honors it; unauthorized chats are ignored;
  inline buttons + halt-confirm work

Broker and Telegram network boundaries are mocked; the DB layer is real but bound
to a temp SQLite (see conftest). Runs with ``uv run pytest test/e2e/ -v``.

Order routing is exercised here at the strategy seam — ``SectorFollowService``
routes mode-aware orders (scaffold→no-order, sandbox/live→placer) through the
injected placer, asserting the resolved ``mode`` propagates. The HTTP
``/api/v1/placeorder`` sandbox path and the chartink webhook → simplified engine →
5m-breakout → sandbox-order path require a full eventlet app boot + live quote
feed; those are intentionally NOT re-implemented as full-boot e2e tests here
(flaky, slow, and already covered by ``test/sandbox/`` and the 68
``test/test_simplified_stock_engine_*`` tests). Keeping this suite hermetic and
deterministic is the deliberate trade-off.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytz

from services.mode_service import EffectiveDecision, resolve_strategy_mode
from services.sector_follow_service import SectorFollowConfig, SectorFollowService
from services.telegram_inbound_service import TelegramInboundService

# RETIRED by the mode-only architecture (B2–B6, 2026-06-12). This e2e suite is
# built on the removed {mode, intent, daily_capital_cap} model: it asserts the
# unified strategy_daily_intent table as the resolver source, run/pause/halt
# engine gating, and Telegram commands writing intent rows. Mode-only is now
# covered at the UNIT level by test_strategy_mode / test_mode_service /
# test_strategy_runtime_override / test_simplified_stock_engine_service /
# test_sector_follow_service / test_preflight_service, plus
# test_telegram_inbound_deprecation. A mode-only cross-component e2e rewrite is a
# tracked follow-up; skipped (not silently passing) until then.
pytestmark = pytest.mark.skip(
    reason="retired by mode-only architecture (2026-06-12); e2e rewrite tracked — "
    "see module docstring / unit coverage"
)

_IST = timezone(timedelta(hours=5, minutes=30))
_ISTPZ = pytz.timezone("Asia/Kolkata")
SF = "sector_follow_cap5_vol"


# The engines resolve intent against the REAL current IST date (the production
# resolver / set_intent default to today), so the cross-component tests below
# anchor to today rather than a hard-coded date — this keeps the DB write, the
# resolver read, and the engine gate all on the same calendar day.
def _today() -> str:
    return datetime.now(_ISTPZ).strftime("%Y-%m-%d")


def _now_at(hour: int, minute: int = 20, day_offset: int = 0):
    base = datetime.now(_ISTPZ) + timedelta(days=day_offset)
    return base.replace(hour=hour, minute=minute, second=0, microsecond=0)


# --------------------------------------------------------------------------- #
# Builders
# --------------------------------------------------------------------------- #
def _config(**ov) -> SectorFollowConfig:
    base = {
        "capital_inr": 250000.0,
        "max_position_inr": 50000.0,
        "max_concurrent_positions": 5,
        "gate_sector_pct": 1.0,
        "gate_stock_pct": 0.5,
        "gate_vol_mult": 1.0,
        "daily_loss_kill_pct": 3.0,
        "cost_pct_round_trip": 0.0857,
        "vol_avg_lookback_days": 20,
        "broker": "zerodha",
        "exchange": "NSE",
        "product": "CNC",
        "universe": ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG"],
        "strategy_id": 99,
    }
    base.update(ov)
    return SectorFollowConfig(**base)


def _hit(sector=0.02, stock=0.01, vol=1.5, price=100.0):
    return {"sector_ret": sector, "stock_ret": stock, "vol_ratio": vol, "current_price": price}


def _make_sector_service(metrics, *, now=None, use_production_resolver=True, **ov):
    """SectorFollowService with mocked broker/journal but the REAL production
    intent resolver (reads the temp intent DB through resolve_strategy_mode)."""
    placed, journal = [], []

    def fake_placer(mode, order):
        placed.append((mode, order))
        return {"status": "success", "orderid": f"OID-{order['symbol']}"}

    def fake_metrics(as_of, universe, sector_map, config):
        return {
            s: metrics.get(s)
            or {"sector_ret": None, "stock_ret": None, "vol_ratio": None, "current_price": None}
            for s in universe
        }

    mode = ov.pop("mode", "scaffold")
    price_fetcher = ov.pop("price_fetcher", lambda s, e: None)
    intent_resolver = None if use_production_resolver else ov.pop("intent_resolver")
    cfg = _config(**ov)
    svc = SectorFollowService(
        config=cfg,
        sector_map=dict.fromkeys(cfg.universe, "NIFTY"),
        mode=mode,
        metrics_provider=fake_metrics,
        order_placer=fake_placer,
        price_fetcher=price_fetcher,
        notifier=lambda m: None,
        trade_recorder=lambda **k: journal.append(k) or len(journal),
        now=now or (lambda: _now_at(15, 20)),
        intent_resolver=intent_resolver,
    )
    svc._test_placed = placed
    svc._test_journal = journal
    return svc


# --------------------------------------------------------------------------- #
# 5. Mode resolution fall-through
# --------------------------------------------------------------------------- #
class TestModeResolutionFallThrough:
    def test_unified_row_wins(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "pause", None, updated_by="op")
        d = resolve_strategy_mode(SF, _today())
        assert (d.mode, d.intent, d.source) == ("sandbox", "pause", "unified")

    def test_legacy_layer_for_simplified(self, intent_db, clean_env, monkeypatch):
        # No unified row → simplified falls to legacy daily_intent.
        import services.mode_service as ms

        monkeypatch.setattr(ms, "get_daily_intent", lambda date: {"intent": "live"})
        d = resolve_strategy_mode("simplified_engine", "2026-06-11")
        assert (d.mode, d.intent, d.source) == ("live", "run", "legacy")

    def test_env_layer(self, intent_db, clean_env, monkeypatch):
        monkeypatch.setenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "sandbox")
        d = resolve_strategy_mode(SF, "2026-06-11")
        assert (d.mode, d.intent, d.source) == ("sandbox", "run", "env")

    def test_default_layer_for_unknown_strategy(self, intent_db, clean_env):
        d = resolve_strategy_mode("some_unknown_strategy", "2026-06-11")
        assert (d.mode, d.intent, d.source) == ("sandbox", "run", "default")

    def test_flag_off_skips_unified(self, intent_db, monkeypatch):
        monkeypatch.setenv("STRATEGY_DAILY_INTENT_ENABLED", "false")
        monkeypatch.setenv("SECTOR_FOLLOW_CAP5_VOL_MODE", "live")
        intent_db.set_intent(SF, _today(), "sandbox", "halt", None, updated_by="op")
        d = resolve_strategy_mode(SF, "2026-06-11")
        # Unified row ignored; env wins.
        assert (d.mode, d.source) == ("live", "env")


# --------------------------------------------------------------------------- #
# 4. Intent gates end-to-end (through the real resolver + temp DB)
# --------------------------------------------------------------------------- #
class TestIntentGatesEndToEnd:
    def test_pause_blocks_entries(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "pause", None, updated_by="op")
        svc = _make_sector_service({s: _hit() for s in ("AAA", "BBB", "CCC")})
        placed = svc.run_entry()
        assert placed == [] and svc._test_placed == []

    def test_halt_blocks_exits_too(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "halt", None, updated_by="op")
        svc = _make_sector_service({}, mode="sandbox")
        # Seed a prior-day open position so an un-halted exit WOULD square off.
        from services.sector_follow_service import PaperPosition

        svc.paper_book["AAA"] = PaperPosition("AAA", 10, 100.0, "2020-01-01", 1.5)
        exited = svc.run_exit()
        assert exited == [] and "AAA" in svc.paper_book  # halt skipped the exit

    def test_pause_still_allows_exits(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "pause", None, updated_by="op")
        svc = _make_sector_service({}, mode="sandbox")
        from services.sector_follow_service import PaperPosition

        svc.paper_book["AAA"] = PaperPosition("AAA", 10, 100.0, "2020-01-01", 1.5)
        exited = svc.run_exit()
        assert len(exited) == 1 and "AAA" not in svc.paper_book

    def test_capital_cap_reduces_concurrency(self, intent_db, clean_env):
        # cap 100k / 50k per slot => 2 concurrent max, even with 5 candidates.
        intent_db.set_intent(SF, _today(), "sandbox", "run", 100000.0, updated_by="op")
        svc = _make_sector_service(
            {s: _hit(vol=1.0 + i * 0.1) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
        )
        placed = svc.run_entry()
        assert len(placed) == 2

    def test_clear_reverts_to_fall_through(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "halt", None, updated_by="op")
        assert resolve_strategy_mode(SF, _today()).source == "unified"
        intent_db.delete_intent(SF, _today())
        d = resolve_strategy_mode(SF, _today())
        assert d.source == "env" and d.intent == "run"  # back-compat fall-through


# --------------------------------------------------------------------------- #
# 3. Sector-follow full entry → T+1 exit cycle (sandbox orders via fake broker)
# --------------------------------------------------------------------------- #
class TestSectorFollowFullCycle:
    def test_entry_then_next_day_exit(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "run", None, updated_by="op")
        svc = _make_sector_service(
            {s: _hit(vol=1.1 + i * 0.1) for i, s in enumerate(["AAA", "BBB", "CCC", "DDD", "EEE"])}
        )
        placed = svc.run_entry()
        assert len(placed) == 5  # capped at max_concurrent
        assert svc.mode == "sandbox"  # unified row mapped scaffold→sandbox
        assert all(m == "sandbox" for m, _ in svc._test_placed)
        # T+1: advance the service clock to the next IST day; the resolver still
        # reads today's run row, so the exit job squares off the prior-day book.
        svc._now = lambda: _now_at(15, 25, day_offset=1)
        exited = svc.run_exit()
        assert len(exited) == 5 and svc.paper_book == {}


# --------------------------------------------------------------------------- #
# 7. Kill switch
# --------------------------------------------------------------------------- #
class TestKillSwitch:
    def test_kill_switch_blocks_new_entries(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "run", None, updated_by="op")
        svc = _make_sector_service({s: _hit() for s in ("AAA", "BBB", "CCC")})
        # -3.1% of 250k = -7750 < -7500 threshold → trips.
        assert svc.update_daily_pnl(realized_today=-7750.0, open_mtm=0.0) is True
        assert svc.kill_switch_active
        placed = svc.run_entry()
        assert placed == []  # entries blocked while kill switch active

    def test_kill_switch_does_not_block_exits(self, intent_db, clean_env):
        svc = _make_sector_service({}, mode="sandbox")
        from services.sector_follow_service import PaperPosition

        svc.paper_book["AAA"] = PaperPosition("AAA", 10, 100.0, "2020-01-01", 1.5)
        svc.update_daily_pnl(realized_today=-9000.0, open_mtm=0.0)
        assert svc.kill_switch_active
        intent_db.set_intent(SF, _today(), "sandbox", "run", None, updated_by="op")
        exited = svc.run_exit()
        assert len(exited) == 1  # open positions still square off


# --------------------------------------------------------------------------- #
# 8. EOD report generation
# --------------------------------------------------------------------------- #
class TestEODReport:
    def test_report_written_with_sections(self, intent_db, clean_env, tmp_db_dir):
        svc = _make_sector_service({s: _hit() for s in ("AAA", "BBB")}, mode="sandbox")
        intent_db.set_intent(SF, _today(), "sandbox", "run", None, updated_by="op")
        svc.run_entry()
        svc.eod_reports_dir = tmp_db_dir
        msg = svc.run_eod_summary()
        import os

        path = os.path.join(tmp_db_dir, f"{_now_at(15, 30).strftime('%Y-%m-%d')}.md")
        assert os.path.exists(path)
        body = open(path, encoding="utf-8").read()
        for section in (
            "# sector_follow_cap5_vol — EOD Report",
            "## Summary",
            "## Sector breakdown",
            "## Positions",
            "## Kill switch",
        ):
            assert section in body
        assert "EOD" in msg

    def test_telegram_sink_failure_does_not_block_file(self, intent_db, clean_env, tmp_db_dir):
        svc = _make_sector_service({}, mode="sandbox")

        def boom(_m):
            raise RuntimeError("telegram down")

        svc._notify = boom
        svc.eod_reports_dir = tmp_db_dir
        # Must not raise even though the Telegram sink throws.
        svc.run_eod_summary()
        import os

        assert os.path.exists(
            os.path.join(tmp_db_dir, f"{_now_at(15, 30).strftime('%Y-%m-%d')}.md")
        )


# --------------------------------------------------------------------------- #
# 6. Telegram inbound bot end-to-end (parsed command → intent DB → engine gate)
# --------------------------------------------------------------------------- #
AUTH = 4242
UNAUTH = 9999


def _inbound(intent_db, authorized=(AUTH,), now=None):
    return TelegramInboundService(
        set_intent=intent_db.set_intent,
        get_intent=intent_db.get_intent,
        delete_intent=intent_db.delete_intent,
        authorized_chat_ids=lambda: set(authorized),
        now=now or (lambda: _now_at(8, 45)),
    )


class TestTelegramInboundEndToEnd:
    def test_pause_command_writes_row_and_engine_skips(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        reply = bot.handle_text(AUTH, 101, "/intent sector_follow_cap5_vol pause")
        assert "pause" in reply
        row = intent_db.get_intent(SF, _today())
        assert row and row["intent"] == "pause"
        assert row["updated_by"] == "telegram:4242:101"
        # SAME gate the 15:20 job uses now refuses entries.
        svc = _make_sector_service({s: _hit() for s in ("AAA", "BBB")})
        assert svc.run_entry() == []

    def test_unauthorized_is_silently_ignored(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        assert bot.handle_text(UNAUTH, 1, "/intent sector_follow_cap5_vol pause") is None
        assert intent_db.get_intent(SF, _today()) is None  # no DB write

    def test_mode_flip_refused(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        reply = bot.handle_text(AUTH, 1, "/intent sector_follow_cap5_vol live")
        assert "laptop access" in reply
        assert intent_db.get_intent(SF, _today()) is None

    def test_inline_button_callback_writes_row(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        reply = bot.handle_callback(AUTH, 55, f"intent:{SF}:pause")
        assert "pause" in reply
        assert intent_db.get_intent(SF, _today())["intent"] == "pause"

    def test_halt_confirmation_yes_within_window(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        prompt = bot.handle_text(AUTH, 1, "/halt sector_follow_cap5_vol")
        assert "YES" in prompt
        assert intent_db.get_intent(SF, _today()) is None  # not yet applied
        applied = bot.handle_text(AUTH, 2, "YES")
        assert "halt" in applied
        assert intent_db.get_intent(SF, _today())["intent"] == "halt"

    def test_halt_confirmation_expires(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        bot.handle_text(AUTH, 1, "/halt sector_follow_cap5_vol")
        # Advance the clock past the 30s confirm window.
        bot._now = lambda: _now_at(8, 46)
        reply = bot.handle_text(AUTH, 2, "YES")
        assert "expired" in reply.lower()
        assert intent_db.get_intent(SF, _today()) is None

    def test_mode_preserved_on_intent_change(self, intent_db, clean_env):
        # Pre-existing live/run row; a Telegram pause must keep mode=live.
        intent_db.set_intent(SF, _today(), "live", "run", None, updated_by="op")
        bot = _inbound(intent_db)
        bot.handle_text(AUTH, 7, "/pause sector_follow_cap5_vol")
        row = intent_db.get_intent(SF, _today())
        assert row["mode"] == "live" and row["intent"] == "pause"

    def test_cap_command(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        bot.handle_text(AUTH, 9, "/intent sector cap 100000")
        row = intent_db.get_intent(SF, _today())
        assert row["daily_capital_cap"] == 100000.0

    def test_clear_command(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "halt", None, updated_by="op")
        bot = _inbound(intent_db)
        bot.handle_text(AUTH, 11, "/intent sector clear")
        assert intent_db.get_intent(SF, _today()) is None

    def test_status_lists_strategies(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        txt = bot.handle_text(AUTH, 1, "/status")
        assert "simplified_engine" in txt and SF in txt

    def test_free_text_resume(self, intent_db, clean_env):
        intent_db.set_intent(SF, _today(), "sandbox", "pause", None, updated_by="op")
        bot = _inbound(intent_db)
        bot.handle_text(AUTH, 3, "resume sector_follow")
        assert intent_db.get_intent(SF, _today())["intent"] == "run"

    def test_morning_keyboard_structure(self, intent_db, clean_env):
        bot = _inbound(intent_db)
        spec = bot.morning_keyboard_spec()
        assert len(spec) == 2  # one row per registry strategy
        cbs = [cb for row in spec for _, cb in row]
        assert f"intent:{SF}:halt" in cbs


# --------------------------------------------------------------------------- #
# Telegram chat_id allowlist (new telegram_db code)
# --------------------------------------------------------------------------- #
class TestChatAllowlist:
    def test_add_and_read_authorized_chat_ids(self, telegram_db_temp):
        assert telegram_db_temp.get_authorized_chat_ids() == set()
        assert telegram_db_temp.add_authorized_chat_id(4242)
        assert telegram_db_temp.add_authorized_chat_id(4242)  # idempotent
        assert telegram_db_temp.add_authorized_chat_id(777)
        assert telegram_db_temp.get_authorized_chat_ids() == {4242, 777}

    def test_malformed_entries_skipped(self, telegram_db_temp):
        telegram_db_temp.update_bot_config({"telegram_chat_ids": "111, oops, 222"})
        assert telegram_db_temp.get_authorized_chat_ids() == {111, 222}

    def test_bot_config_exposes_chat_ids(self, telegram_db_temp):
        telegram_db_temp.add_authorized_chat_id(555)
        assert "555" in (telegram_db_temp.get_bot_config().get("telegram_chat_ids") or "")


# --------------------------------------------------------------------------- #
# Data-freshness validation: stale feed → health row + alert + tomorrow's pause
# --------------------------------------------------------------------------- #
class TestDataFreshnessValidation:
    def test_stale_feed_writes_health_row_and_auto_pauses_tomorrow(
        self, intent_db, data_health_db, clean_env, monkeypatch
    ):
        import services.data_freshness_service as dfs

        # Force the 16:30 check to see a stale feed (index 12 days behind).
        stale_details = {
            "NIFTY": {"ok": False, "last_date": "2026-05-29", "staleness_days": 9, "kind": "index"},
            "AAA": {"ok": True, "last_date": "2026-06-09", "staleness_days": 1, "kind": "stock"},
        }
        monkeypatch.setattr(
            dfs,
            "check_strategy_data_ready",
            lambda name, date=None, **kw: (False, stale_details),
        )

        alerts: list[str] = []
        svc = _make_sector_service({}, now=lambda: _now_at(16, 30))
        svc._notify = lambda m: alerts.append(m)

        ok, _ = svc.run_data_health_check()

        # 1. Verdict is "not ready".
        assert ok is False
        # 2. Operator was alerted.
        assert any("STALE" in a for a in alerts)
        # 3. A data_health_check row was persisted (not ok, alert sent, NIFTY listed).
        latest = data_health_db.get_latest_check(SF)
        assert latest is not None
        assert latest["overall_ok"] is False
        assert latest["alert_sent"] is True
        assert "NIFTY" in latest["stale_symbols"]
        # 4. Tomorrow's intent was auto-paused (operator can override).
        tomorrow = (datetime.now(_ISTPZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        row = intent_db.get_intent(SF, tomorrow)
        assert row is not None
        assert row["intent"] == "pause"
        assert row["updated_by"] == "data_health:auto-pause"

    def test_fresh_feed_no_alert_no_pause(self, intent_db, data_health_db, clean_env, monkeypatch):
        import services.data_freshness_service as dfs

        monkeypatch.setattr(
            dfs,
            "check_strategy_data_ready",
            lambda name, date=None, **kw: (
                True,
                {
                    "NIFTY": {
                        "ok": True,
                        "last_date": "2026-06-10",
                        "staleness_days": 0,
                        "kind": "index",
                    }
                },
            ),
        )
        alerts: list[str] = []
        svc = _make_sector_service({}, now=lambda: _now_at(16, 30))
        svc._notify = lambda m: alerts.append(m)

        ok, _ = svc.run_data_health_check()
        assert ok is True
        assert alerts == []
        latest = data_health_db.get_latest_check(SF)
        assert latest["overall_ok"] is True and latest["alert_sent"] is False
        tomorrow = (datetime.now(_ISTPZ) + timedelta(days=1)).strftime("%Y-%m-%d")
        assert intent_db.get_intent(SF, tomorrow) is None  # no auto-pause
