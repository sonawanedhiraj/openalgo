"""Tests for ``services.notification_service``.

The notification layer must be fail-safe: it never raises into the order
path, it respects the master/per-event toggles, and it formats messages
predictably across the six supported event types.

Test strategy:
* Patch the ``telegram_bot_service`` singleton with a recording fake so we
  can observe the messages produced WITHOUT touching a real Telegram bot
  or its asyncio loop.
* Patch ``asyncio.run_coroutine_threadsafe`` to a sync no-op that closes
  the coroutine returned by ``broadcast_message`` — the fake's side-effect
  already captures the message during the call itself, so we don't need an
  event loop.
* For tests that exercise toggles, instantiate fresh ``NotificationService``
  objects directly so each test reads its own env snapshot rather than
  sharing the module-level singleton.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Recording fake for telegram_bot_service.
# ---------------------------------------------------------------------------


class _RecordingBot:
    """Stand-in for the global ``telegram_bot_service`` singleton.

    The side-effect (recording the message) happens during the
    ``broadcast_message`` call itself rather than inside the returned
    coroutine body, so tests can assert on ``sent`` without waiting on a
    real event loop.
    """

    def __init__(self, *, is_running: bool = True, raise_on_send: bool = False) -> None:
        self.is_running = is_running
        self.bot_loop = object() if is_running else None
        self.sent: list[tuple[str, dict | None]] = []
        self._raise = raise_on_send

    def broadcast_message(self, message: str, filters: dict | None = None):
        self.sent.append((message, filters))
        if self._raise:
            raise RuntimeError("simulated telegram failure")

        async def _noop():
            return None

        return _noop()


def _install_fake_bot(monkeypatch, bot: _RecordingBot) -> None:
    """Swap the recording fake into the telegram_bot_service module path."""
    import services.telegram_bot_service as tbs

    monkeypatch.setattr(tbs, "telegram_bot_service", bot, raising=False)

    # asyncio.run_coroutine_threadsafe needs a real running loop; replace it
    # with a no-op that just closes the coroutine so we don't leak warnings.
    import services.notification_service as ns

    def _fake_run_coro(coro, loop):  # noqa: ANN001
        try:
            coro.close()
        except Exception:
            pass
        return None

    monkeypatch.setattr(ns.asyncio, "run_coroutine_threadsafe", _fake_run_coro)


def _fresh_service(monkeypatch, **env: str):
    """Build a fresh NotificationService after setting env vars.

    Bypasses the module-level singleton so each test reads its own snapshot
    of the toggles.
    """
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    from services.notification_service import NotificationService

    return NotificationService()


# ---------------------------------------------------------------------------
# Core notify() behaviour
# ---------------------------------------------------------------------------


def test_notify_calls_telegram_send_when_enabled(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.notify("cycle_summary", "hello operator")

    assert len(bot.sent) == 1
    message, filters = bot.sent[0]
    assert "hello operator" in message
    assert filters == {"notifications_enabled": True}


def test_notify_no_op_when_master_disabled(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="false")

    svc.notify("cycle_summary", "should not appear")

    assert bot.sent == []


def test_notify_no_op_when_event_type_disabled(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(
        monkeypatch,
        NOTIFY_TELEGRAM_ENABLED="true",
        NOTIFY_CYCLE_SUMMARY="false",
        NOTIFY_TRADE_OPENED="true",
    )

    svc.notify("cycle_summary", "should be silent")
    svc.notify("trade_opened", "should fire")

    assert len(bot.sent) == 1
    assert "should fire" in bot.sent[0][0]


def test_notify_failsafe_on_telegram_error(monkeypatch, caplog):
    bot = _RecordingBot(is_running=True, raise_on_send=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.WARNING):
        # Must not raise even though broadcast_message raises.
        svc.notify("cycle_summary", "boom")

    # The fake records the message before raising; the exception is swallowed
    # by notify() and logged as a warning.
    assert any("send failed" in rec.message for rec in caplog.records)


def test_notify_no_op_when_bot_not_running(monkeypatch, caplog):
    bot = _RecordingBot(is_running=False)
    _install_fake_bot(monkeypatch, bot)
    # No inbound bot available either → final dropped-notification warning.
    _install_fake_inbound(monkeypatch, None)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.WARNING):
        svc.notify("cycle_summary", "drop me")

    assert bot.sent == []
    assert any("no live telegram bot" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Phase 6 inbound-bot fallback: when the legacy outbound bot is inactive
# (bot_config.is_active=0, e.g. token freed to the inbound poller), notify()
# routes through the inbound bot's live send path instead of dropping.
# ---------------------------------------------------------------------------


class _RecordingInbound:
    """Stand-in for the Phase 6 inbound poller singleton."""

    def __init__(self, *, is_running: bool = True, sent_count: int = 1) -> None:
        self.is_running = is_running
        self.sent: list[str] = []
        self._sent_count = sent_count

    def send_message_to_all(self, text: str) -> int:
        self.sent.append(text)
        return self._sent_count


def _install_fake_inbound(monkeypatch, inbound) -> None:
    """Patch services.telegram_inbound_service.get_service to return ``inbound``."""
    import services.telegram_inbound_service as tis

    monkeypatch.setattr(tis, "get_service", lambda: inbound, raising=False)


def test_notify_falls_through_to_inbound_when_legacy_inactive(monkeypatch):
    """Legacy bot down + inbound running + chats configured → inbound sends."""
    legacy = _RecordingBot(is_running=False)
    _install_fake_bot(monkeypatch, legacy)
    inbound = _RecordingInbound(is_running=True, sent_count=1)
    _install_fake_inbound(monkeypatch, inbound)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.notify("cycle_summary", "via inbound")

    assert legacy.sent == []  # legacy never used
    assert inbound.sent == ["via inbound"]  # inbound carried it


def test_notify_inbound_no_chats_returns_gracefully(monkeypatch, caplog):
    """Inbound running but 0 chats → no crash, logs the dropped warning."""
    legacy = _RecordingBot(is_running=False)
    _install_fake_bot(monkeypatch, legacy)
    inbound = _RecordingInbound(is_running=True, sent_count=0)
    _install_fake_inbound(monkeypatch, inbound)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.WARNING):
        svc.notify("cycle_summary", "no chats")

    assert inbound.sent == ["no chats"]  # attempted
    assert any("0 chats targeted" in rec.message for rec in caplog.records)
    assert any("no live telegram bot" in rec.message for rec in caplog.records)


def test_notify_legacy_primary_when_running(monkeypatch):
    """Regression: legacy bot running → inbound path is never touched."""
    legacy = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, legacy)
    inbound = _RecordingInbound(is_running=True, sent_count=1)
    _install_fake_inbound(monkeypatch, inbound)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.notify("cycle_summary", "legacy first")

    assert len(legacy.sent) == 1
    assert legacy.sent[0][0] == "legacy first"
    assert inbound.sent == []  # inbound untouched


def test_notify_unknown_event_type_logs_warning(monkeypatch, caplog):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    with caplog.at_level(logging.WARNING):
        svc.notify("not_a_real_event", "anything")

    assert bot.sent == []
    assert any("unknown event_type" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Convenience-publisher formatting
# ---------------------------------------------------------------------------


def test_publish_cycle_summary_formats_message(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_cycle_summary(
        cycle_kind="chartink",
        buy_count=3,
        sell_count=1,
        effective_mode="sandbox",
        post_status="ok",
    )

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "chartink" in body
    assert "sandbox" in body
    assert "ok" in body
    assert "3" in body and "1" in body
    assert body.startswith("🔁")


def test_publish_cycle_summary_icon_per_status(monkeypatch):
    expected = {
        "ok": "🔁",
        "empty": "📭",
        "aborted_preflight": "🛑",
        "error": "❌",
        "something_unexpected": "🔁",  # fallback
    }
    for status, icon in expected.items():
        bot = _RecordingBot(is_running=True)
        _install_fake_bot(monkeypatch, bot)
        svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")
        svc.publish_cycle_summary(
            cycle_kind="chartink",
            buy_count=0,
            sell_count=0,
            effective_mode="sandbox",
            post_status=status,
        )
        assert bot.sent[0][0].startswith(icon), status


def test_publish_trade_closed_formats_pnl_with_sign(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_trade_closed(
        symbol="RELIANCE",
        direction="LONG",
        entry_price=2500.0,
        exit_price=2550.0,
        pnl=500.0,
        exit_reason="target_hit",
        hold_duration_seconds=900,
    )
    svc.publish_trade_closed(
        symbol="INFY",
        direction="LONG",
        entry_price=1800.0,
        exit_price=1780.0,
        pnl=-200.0,
        exit_reason="stop_loss",
        hold_duration_seconds=300,
    )

    assert len(bot.sent) == 2
    positive_body = bot.sent[0][0]
    negative_body = bot.sent[1][0]
    assert "+₹500" in positive_body
    assert "-₹200" in negative_body
    assert "target_hit" in positive_body
    assert "stop_loss" in negative_body
    assert "15m00s" in positive_body or "15m" in positive_body


def test_publish_preflight_abort_lists_reasons(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_abort(["no daily_intent declared for today", "daily_intent is skip"])

    assert len(bot.sent) == 1
    body = bot.sent[0][0]
    assert "Preflight aborted" in body
    assert "no daily_intent declared for today" in body
    assert "daily_intent is skip" in body


# ---------------------------------------------------------------------------
# Preflight-abort alert de-duplication (2026-06-03 incident: a 14s DNS blip
# produced 14 identical "🛑 Preflight aborted" alerts).
# ---------------------------------------------------------------------------


def _clock(monkeypatch, start: float = 1000.0):
    """Install a controllable monotonic clock; returns an advance() callable."""
    import services.notification_service as ns

    state = {"t": start}
    monkeypatch.setattr(ns.time, "monotonic", lambda: state["t"])

    def advance(seconds: float) -> None:
        state["t"] += seconds

    return advance


def test_preflight_abort_burst_collapses_to_single_alert(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    # Five rapid aborts with the SAME reason set, no time passing.
    for _ in range(5):
        svc.publish_preflight_abort(["3 errors in last hour"])

    assert len(bot.sent) == 1  # only the first fires; the rest are suppressed
    assert "Preflight aborted" in bot.sent[0][0]


def test_preflight_abort_sliding_count_is_same_signature(monkeypatch):
    """The rolling-hour count drifts each slot — must NOT re-alert."""
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    advance = _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_abort(["14 errors in last hour"])
    advance(60)
    svc.publish_preflight_abort(["13 errors in last hour"])
    advance(60)
    svc.publish_preflight_abort(["12 errors in last hour"])

    assert len(bot.sent) == 1  # digit-normalized signature is identical


def test_preflight_abort_cooldown_expiry_re_alerts(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    advance = _clock(monkeypatch)
    svc = _fresh_service(
        monkeypatch,
        NOTIFY_TELEGRAM_ENABLED="true",
        NOTIFY_PREFLIGHT_ABORT_COOLDOWN_SEC="900",
    )

    svc.publish_preflight_abort(["3 errors in last hour"])
    advance(600)  # inside cooldown
    svc.publish_preflight_abort(["3 errors in last hour"])
    assert len(bot.sent) == 1

    advance(400)  # now 1000s total > 900s cooldown
    svc.publish_preflight_abort(["3 errors in last hour"])
    assert len(bot.sent) == 2  # cooldown reminder fires


def test_preflight_abort_new_signature_bypasses_cooldown(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    advance = _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_abort(["3 errors in last hour"])
    advance(5)  # well inside cooldown
    # A genuinely different failure joins — must alert immediately.
    svc.publish_preflight_abort(["3 errors in last hour", "no active broker session"])

    assert len(bot.sent) == 2
    assert "broker session" in bot.sent[1][0]


def test_preflight_clear_emits_once_after_abort(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    advance = _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_abort(["3 errors in last hour"])
    advance(300)  # 5 minutes of aborts
    svc.publish_preflight_clear()
    svc.publish_preflight_clear()  # second healthy slot — no duplicate clear

    assert len(bot.sent) == 2  # one abort + one clear
    clear_body = bot.sent[1][0]
    assert "Preflight cleared" in clear_body
    assert "5 min" in clear_body


def test_preflight_clear_without_prior_abort_is_noop(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_clear()

    assert bot.sent == []


def test_preflight_abort_re_alerts_after_recovery_cycle(monkeypatch):
    """abort → clear → abort: the post-recovery abort is a fresh episode."""
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    advance = _clock(monkeypatch)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_preflight_abort(["3 errors in last hour"])
    advance(60)
    svc.publish_preflight_clear()
    advance(60)
    svc.publish_preflight_abort(["3 errors in last hour"])  # same reason, new episode

    assert len(bot.sent) == 3  # abort, clear, abort — clear reset the signature


def test_preflight_abort_fresh_instance_resets_dedup(monkeypatch):
    """A process restart (fresh singleton) must alert again immediately."""
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    _clock(monkeypatch)

    svc1 = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")
    svc1.publish_preflight_abort(["3 errors in last hour"])

    svc2 = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")
    svc2.publish_preflight_abort(["3 errors in last hour"])

    assert len(bot.sent) == 2  # each fresh instance fires once


def test_publish_anomaly_includes_severity_prefix(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_anomaly(
        source="simplified_engine.entry_order",
        message="broker rejected order",
        severity="error",
    )
    svc.publish_anomaly(
        source="bridge",
        message="bridge unreachable",
        severity="warning",
    )

    bodies = [b for b, _ in bot.sent]
    assert any("🚨" in b for b in bodies)  # error severity prefix
    assert any("⚠️" in b for b in bodies)  # warning severity prefix
    assert any("simplified_engine.entry_order" in b for b in bodies)


def test_publish_trade_opened_includes_arrow_by_direction(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_trade_opened(
        symbol="TCS",
        direction="LONG",
        quantity=10,
        entry_price=3450.5,
        strategy="trending_equity_intraday",
    )
    svc.publish_trade_opened(
        symbol="HDFC",
        direction="SHORT",
        quantity=5,
        entry_price=1620.0,
        strategy="trending_equity_intraday",
    )

    long_body, short_body = bot.sent[0][0], bot.sent[1][0]
    assert "📈" in long_body and "TCS" in long_body
    assert "📉" in short_body and "HDFC" in short_body
    assert "trending_equity_intraday" in long_body


def test_publish_eod_summary_renders_by_strategy(monkeypatch):
    bot = _RecordingBot(is_running=True)
    _install_fake_bot(monkeypatch, bot)
    svc = _fresh_service(monkeypatch, NOTIFY_TELEGRAM_ENABLED="true")

    svc.publish_eod_summary(
        trade_count=4,
        winners=3,
        losers=1,
        net_pnl=1200.5,
        by_strategy={
            "trending_equity_intraday": {"count": 4, "pnl": 1200.5},
        },
    )

    body = bot.sent[0][0]
    assert "End-of-day summary" in body
    assert "+₹1,200.50" in body
    assert "trending_equity_intraday" in body
    # The P&L line must be self-describing: gross, closed-only, single-strategy
    # scope — and point the operator at /mypnl for the net account figure. This
    # is the A1 mismatch relabel (Telegram ≠ /mypnl by construction).
    assert "Realized (closed, gross, simplified-engine only)" in body
    assert "/mypnl" in body
    assert "Net P&L" not in body  # the old misleading label is gone


# ---------------------------------------------------------------------------
# Integration: publish points call into the notification service
# ---------------------------------------------------------------------------


class _RecordingNotifier:
    """Mimics NotificationService — records every publish_* call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def publish_cycle_summary(self, **kw):
        self.calls.append(("cycle_summary", kw))

    def publish_preflight_abort(self, reasons):
        self.calls.append(("preflight_abort", {"reasons": list(reasons)}))

    def publish_trade_opened(self, **kw):
        self.calls.append(("trade_opened", kw))

    def publish_trade_closed(self, **kw):
        self.calls.append(("trade_closed", kw))

    def publish_eod_summary(self, **kw):
        self.calls.append(("eod_summary", kw))

    def publish_anomaly(self, **kw):
        self.calls.append(("anomaly_alert", kw))


@pytest.fixture
def patched_notifier(monkeypatch):
    rec = _RecordingNotifier()
    import services.notification_service as ns

    monkeypatch.setattr(ns, "get_notification_service", lambda: rec)
    return rec


def test_scan_cycle_complete_publishes_summary_when_enabled(patched_notifier):
    """Cycle completion via the sentinel path still fires the notification."""
    from services import scan_cycle_service

    scan_cycle_service.complete_cycle(
        cycle_id=-1,
        post_status="ok",
        screener_buy=["RELIANCE", "SBIN"],
        screener_sell=["INFY"],
        effective_mode="sandbox",
        cycle_kind="chartink",
    )

    cycle_calls = [c for c in patched_notifier.calls if c[0] == "cycle_summary"]
    assert len(cycle_calls) == 1
    _, kw = cycle_calls[0]
    assert kw["buy_count"] == 2
    assert kw["sell_count"] == 1
    assert kw["effective_mode"] == "sandbox"
    assert kw["post_status"] == "ok"
    assert kw["cycle_kind"] == "chartink"


def test_engine_entry_publishes_trade_opened(patched_notifier):
    """The engine entry hook delegates to publish_trade_opened."""
    from services.simplified_stock_engine_service import (
        SimplifiedStockEngineService,
    )

    signal = SimpleNamespace(
        symbol="RELIANCE",
        action="BUY",
        quantity=10,
    )
    # Bind the helper to a bare object so we don't construct the full engine.
    SimplifiedStockEngineService._notify_trade_opened(
        SimpleNamespace(JOURNAL_STRATEGY_NAME="trending_equity_intraday"),
        signal,
        2500.5,
    )

    opened = [c for c in patched_notifier.calls if c[0] == "trade_opened"]
    assert len(opened) == 1
    _, kw = opened[0]
    assert kw["symbol"] == "RELIANCE"
    assert kw["direction"] == "LONG"
    assert kw["quantity"] == 10
    assert kw["entry_price"] == 2500.5
    assert kw["strategy"] == "trending_equity_intraday"


def test_engine_exit_publishes_trade_closed(patched_notifier, monkeypatch):
    """The engine exit hook delegates to publish_trade_closed."""
    from services import simplified_stock_engine_service as ses
    from services.simplified_stock_engine_service import (
        SimplifiedStockEngineService,
    )

    # The hook reads the freshly-finalised journal row to populate the
    # message; stub get_trades_for_symbol so we don't need a live DB.
    monkeypatch.setattr(
        "services.trade_journal_service.get_trades_for_symbol",
        lambda symbol, days=1: [
            {
                "direction": "LONG",
                "entry_price": 2500.0,
                "pnl": 500.0,
                "hold_duration_seconds": 600,
            }
        ],
    )

    signal = SimpleNamespace(
        symbol="RELIANCE",
        action="SELL",
        reason="target_hit",
    )
    SimplifiedStockEngineService._notify_trade_closed(
        SimpleNamespace(JOURNAL_STRATEGY_NAME="trending_equity_intraday"),
        signal,
        2550.0,
    )

    closed = [c for c in patched_notifier.calls if c[0] == "trade_closed"]
    assert len(closed) == 1
    _, kw = closed[0]
    assert kw["symbol"] == "RELIANCE"
    assert kw["direction"] == "LONG"
    assert kw["entry_price"] == 2500.0
    assert kw["exit_price"] == 2550.0
    assert kw["pnl"] == 500.0
    assert kw["exit_reason"] == "target_hit"
    assert kw["hold_duration_seconds"] == 600


def test_engine_entry_notification_is_failsafe(patched_notifier, monkeypatch, caplog):
    """A blow-up inside the publish helper must NOT propagate into entry path."""
    from services.simplified_stock_engine_service import (
        SimplifiedStockEngineService,
    )

    class _BoomNotifier:
        def publish_trade_opened(self, **kw):
            raise RuntimeError("downstream failure")

    monkeypatch.setattr(
        "services.notification_service.get_notification_service",
        lambda: _BoomNotifier(),
    )

    signal = SimpleNamespace(symbol="X", action="BUY", quantity=1)
    with caplog.at_level(logging.WARNING):
        # Must not raise.
        SimplifiedStockEngineService._notify_trade_opened(
            SimpleNamespace(JOURNAL_STRATEGY_NAME="trending_equity_intraday"),
            signal,
            100.0,
        )
    assert any("notification publish failed" in rec.message for rec in caplog.records)
