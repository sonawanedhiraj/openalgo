"""One-way operator notifications — Telegram first, extensible.

Stage 0/2 operational floor. Bridges the existing
:mod:`services.telegram_bot_service` (which exposes an async
``broadcast_message`` method running on a PTB-owned event loop in a separate
OS thread) to the *synchronous* publish call sites scattered through the
scan-cycle, preflight, engine entry/exit, EOD summary, and anomaly paths.

Design rules:

* **Fail-safe.** Every ``notify()`` wraps the underlying Telegram send in a
  try/except. A bot that isn't running, a missing event loop, a network
  hiccup, a malformed message — none of these may bubble up into trading or
  scan-cycle code. Audit loss is recoverable; a missed order or a stalled
  cycle isn't.
* **Operator opts in.** ``NOTIFY_TELEGRAM_ENABLED`` is the master switch and
  defaults to false. Each event type (cycle summary, preflight abort, trade
  opened/closed, EOD summary, anomaly) has its own per-event toggle so the
  operator can tune signal-to-noise.
* **No new credentials.** Bot token and recipient list come from OpenAlgo's
  existing Telegram config (``telegram_db`` / the ``/telegram`` UI). This
  module never reads a bot token from ``.env`` and never accepts credentials,
  passwords, or OTPs from inbound Telegram traffic — Stage 4 territory.
* **Two-way explicitly out of scope.** This module is publish-only; the
  operator never drives an action by replying to a notification in Stage 0/2.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Mapping

from utils.logging import get_logger

logger = get_logger(__name__)


_EVENT_TYPES = (
    "cycle_summary",
    "preflight_abort",
    "trade_opened",
    "trade_closed",
    "eod_summary",
    "anomaly_alert",
    "eod_watchdog",
    "veto_decision",
)

_SEVERITY_PREFIX = {
    "info": "ℹ️",
    "warning": "⚠️",
    "error": "🚨",
    "critical": "🚨",
}


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "on")


def _fmt_rupees(amount: float | None) -> str:
    """Format a P&L amount with explicit sign and a rupee symbol."""
    if amount is None:
        return "₹0.00"
    sign = "+" if amount > 0 else ("-" if amount < 0 else "")
    return f"{sign}₹{abs(float(amount)):,.2f}"


def _fmt_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"


class NotificationService:
    """One-way notifications to the operator.

    Construction reads all enable flags from the environment once. Per-event
    toggles are evaluated on each :meth:`notify` call against the snapshot
    taken at construction — restart the process to change them at runtime.

    The publish_* convenience methods structure typed events into a markdown
    body and delegate to :meth:`notify`. They never raise.
    """

    def __init__(self) -> None:
        self.enabled: bool = _env_bool("NOTIFY_TELEGRAM_ENABLED", default=False)
        self.per_event: dict[str, bool] = {
            "cycle_summary": _env_bool("NOTIFY_CYCLE_SUMMARY", default=True),
            "preflight_abort": _env_bool("NOTIFY_PREFLIGHT_ABORT", default=True),
            "trade_opened": _env_bool("NOTIFY_TRADE_OPENED", default=True),
            "trade_closed": _env_bool("NOTIFY_TRADE_CLOSED", default=True),
            "eod_summary": _env_bool("NOTIFY_EOD_SUMMARY", default=True),
            "anomaly_alert": _env_bool("NOTIFY_ANOMALY_ALERT", default=True),
            # EOD watchdog summaries / failures default ON — the watchdog is
            # a safety net and the operator wants to know whenever it ran or
            # couldn't. Set NOTIFY_EOD_WATCHDOG=false only when intentionally
            # going dark on watchdog telemetry (e.g. during scheduler dev).
            "eod_watchdog": _env_bool("NOTIFY_EOD_WATCHDOG", default=True),
            # LLM veto-layer skip alerts default ON — operator needs visibility
            # into what the veto layer blocks (shadow: would-block, active:
            # actual block) before flipping VETO_LAYER_MODE=active.
            "veto_decision": _env_bool("NOTIFY_VETO_ALERTS", default=True),
        }

    # ------------------------------------------------------------------
    # Core publish — fail-safe, no exceptions ever bubble out.
    # ------------------------------------------------------------------

    def notify(self, event_type: str, message: str, **metadata: Any) -> None:
        """Publish ``message`` for ``event_type`` to the Telegram channel.

        No-op when:

        * the master switch ``NOTIFY_TELEGRAM_ENABLED`` is false;
        * the per-event toggle for ``event_type`` is false;
        * the Telegram bot is not running / has no live event loop;
        * the underlying send raises.

        Any failure path is logged at WARNING and swallowed. Callers may
        assume this method *never* raises and *never* blocks for more than
        the time it takes to schedule a coroutine onto the bot's event loop.
        """
        if not self.enabled:
            return
        if event_type not in self.per_event:
            logger.warning(
                "notification_service.notify: unknown event_type=%r — dropping",
                event_type,
            )
            return
        if not self.per_event[event_type]:
            return

        try:
            from services.telegram_bot_service import telegram_bot_service

            loop = getattr(telegram_bot_service, "bot_loop", None)
            if loop is None or not getattr(telegram_bot_service, "is_running", False):
                logger.warning(
                    "notification_service.notify: telegram bot not running "
                    "(event=%s) — dropping notification",
                    event_type,
                )
                return

            coro = telegram_bot_service.broadcast_message(
                message,
                filters={"notifications_enabled": True},
            )
            asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception as e:  # noqa: BLE001 — fail-safe by design
            logger.warning(
                "notification_service.notify: send failed (event=%s): %s",
                event_type, e,
            )

    # ------------------------------------------------------------------
    # Convenience publishers — typed event_type + structured formatting.
    # Each is a thin shim around notify() and must remain fail-safe.
    # ------------------------------------------------------------------

    def publish_cycle_summary(
        self,
        cycle_kind: str,
        buy_count: int,
        sell_count: int,
        effective_mode: str,
        post_status: str,
    ) -> None:
        try:
            text = (
                "🔁 *Scan cycle*\n"
                f"├ Kind: `{cycle_kind}`\n"
                f"├ Mode: `{effective_mode}`\n"
                f"├ Status: `{post_status}`\n"
                f"├ Buy hits: {int(buy_count)}\n"
                f"└ Sell hits: {int(sell_count)}"
            )
            self.notify("cycle_summary", text,
                        cycle_kind=cycle_kind, post_status=post_status)
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_cycle_summary failed: %s", e)

    def publish_preflight_abort(self, reasons: list[str]) -> None:
        try:
            if not reasons:
                bullet_block = "_no reasons given_"
            else:
                bullet_block = "\n".join(f"• {r}" for r in reasons)
            text = "🛑 *Preflight aborted*\n" + bullet_block
            self.notify("preflight_abort", text, reason_count=len(reasons))
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_preflight_abort failed: %s", e)

    def publish_trade_opened(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        entry_price: float,
        strategy: str,
    ) -> None:
        try:
            arrow = "📈" if (direction or "").upper() == "LONG" else "📉"
            text = (
                f"{arrow} *Trade opened*\n"
                f"├ Symbol: `{symbol}`\n"
                f"├ Side: `{direction}`\n"
                f"├ Qty: {int(quantity)}\n"
                f"├ Entry: ₹{float(entry_price):,.2f}\n"
                f"└ Strategy: `{strategy}`"
            )
            self.notify("trade_opened", text, symbol=symbol)
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_trade_opened failed: %s", e)

    def publish_trade_closed(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        hold_duration_seconds: int,
    ) -> None:
        try:
            pnl_emoji = "🟢" if (pnl or 0) > 0 else ("🔴" if (pnl or 0) < 0 else "⚪")
            text = (
                f"{pnl_emoji} *Trade closed*\n"
                f"├ Symbol: `{symbol}`\n"
                f"├ Side: `{direction}`\n"
                f"├ Entry: ₹{float(entry_price):,.2f}\n"
                f"├ Exit: ₹{float(exit_price):,.2f}\n"
                f"├ P&L: {_fmt_rupees(pnl)}\n"
                f"├ Reason: `{exit_reason}`\n"
                f"└ Held: {_fmt_duration(hold_duration_seconds)}"
            )
            self.notify("trade_closed", text, symbol=symbol, pnl=pnl)
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_trade_closed failed: %s", e)

    def publish_eod_summary(
        self,
        trade_count: int,
        winners: int,
        losers: int,
        net_pnl: float,
        by_strategy: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        try:
            head = (
                "🏁 *End-of-day summary*\n"
                f"├ Trades: {int(trade_count)}\n"
                f"├ Winners: {int(winners)}\n"
                f"├ Losers: {int(losers)}\n"
                f"└ Net P&L: {_fmt_rupees(net_pnl)}"
            )
            if by_strategy:
                rows = []
                for strat, bucket in by_strategy.items():
                    if not isinstance(bucket, Mapping):
                        continue
                    count = bucket.get("count", 0)
                    pnl = bucket.get("pnl", 0.0)
                    rows.append(
                        f"  • `{strat}` — {int(count)} trades, {_fmt_rupees(float(pnl))}"
                    )
                if rows:
                    head += "\n\n*By strategy*\n" + "\n".join(rows)
            self.notify("eod_summary", head, trade_count=trade_count)
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_eod_summary failed: %s", e)

    def publish_eod_watchdog_summary(
        self, strategy_name: str, result: Mapping[str, Any]
    ) -> None:
        """One-line Telegram summary of a watchdog run.

        Shape of ``result`` matches :func:`flatten_strategy_positions` —
        keys: ``attempted``, ``succeeded``, ``failed`` (list), ``skipped``
        (list), ``reason``. Missing keys are tolerated.

        A "nothing to flatten" run (attempted=0) is still announced so the
        operator gets a daily heartbeat confirming the watchdog ran. That's
        the whole point — silence at 15:20 IST means *something is wrong*.
        """
        try:
            attempted = int(result.get("attempted", 0) or 0)
            succeeded = int(result.get("succeeded", 0) or 0)
            failed = result.get("failed") or []
            skipped = result.get("skipped") or []
            reason = str(result.get("reason") or "eod_watchdog")

            if attempted == 0 and not failed and not skipped:
                text = (
                    "🐕 *EOD watchdog*\n"
                    f"├ Strategy: `{strategy_name}`\n"
                    f"├ Reason: `{reason}`\n"
                    "└ No open positions — nothing to flatten."
                )
            else:
                lines = [
                    "🐕 *EOD watchdog*",
                    f"├ Strategy: `{strategy_name}`",
                    f"├ Reason: `{reason}`",
                    f"├ Attempted: {attempted}",
                    f"├ Succeeded: {succeeded}",
                    f"├ Failed: {len(failed)}",
                    f"└ Skipped: {len(skipped)}",
                ]
                if failed:
                    lines.append("\n*Failures*")
                    for f in failed[:5]:
                        sym = f.get("symbol") if isinstance(f, Mapping) else "?"
                        err = f.get("error") if isinstance(f, Mapping) else str(f)
                        lines.append(f"  • `{sym}` — {err}")
                    if len(failed) > 5:
                        lines.append(f"  • …and {len(failed) - 5} more")
                text = "\n".join(lines)

            self.notify(
                "eod_watchdog", text,
                strategy=strategy_name, attempted=attempted, failed=len(failed),
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_eod_watchdog_summary failed: %s", e)

    def publish_eod_watchdog_failure(
        self, strategy_name: str, error: str
    ) -> None:
        """Loud alert: the watchdog itself failed.

        Distinct from per-position flatten failures (which are surfaced via
        ``publish_eod_watchdog_summary``). This one means the safety net is
        not safe — usually a missing api_key, a hung scheduler, or an
        unexpected crash in the cron job body.
        """
        try:
            text = (
                "🚨 *EOD watchdog FAILED*\n"
                f"├ Strategy: `{strategy_name}`\n"
                f"├ Error: {error}\n"
                "└ *Manual intervention required.*"
            )
            self.notify(
                "eod_watchdog", text,
                strategy=strategy_name, severity="critical",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_eod_watchdog_failure failed: %s", e)

    def publish_anomaly(
        self,
        source: str,
        message: str,
        severity: str = "warning",
    ) -> None:
        try:
            sev_key = (severity or "warning").lower()
            prefix = _SEVERITY_PREFIX.get(sev_key, "⚠️")
            text = (
                f"{prefix} *Anomaly [{sev_key}]*\n"
                f"├ Source: `{source}`\n"
                f"└ {message}"
            )
            self.notify("anomaly_alert", text, source=source, severity=sev_key)
        except Exception as e:  # noqa: BLE001
            logger.warning("publish_anomaly failed: %s", e)


# ---------------------------------------------------------------------------
# Module-level singleton accessor.
# ---------------------------------------------------------------------------

_singleton: NotificationService | None = None


def get_notification_service() -> NotificationService:
    """Return the process-wide :class:`NotificationService` singleton."""
    global _singleton
    if _singleton is None:
        _singleton = NotificationService()
    return _singleton


def reset_notification_service_for_tests() -> None:
    """Clear the singleton so tests can re-read env vars on next access."""
    global _singleton
    _singleton = None


# ---------------------------------------------------------------------------
# LLM veto-layer skip alerts (module-level shim — called from signal_review).
# ---------------------------------------------------------------------------


def _format_confidence_pct(confidence: float | None) -> str:
    if confidence is None:
        return "—"
    try:
        return f"{int(round(float(confidence) * 100))}%"
    except (TypeError, ValueError):
        return "—"


def publish_veto_decision_alert(
    *,
    symbol: str,
    decision: str,
    reasoning: str,
    confidence: float | None,
    enforcement_mode: str,
    source: str | None = None,
) -> None:
    """Send a Telegram alert when the veto layer decides to skip a signal.

    Shadow mode → 🔬 [SHADOW] prefix (would-block, informational).
    Active mode → 🚫 prefix (actually blocked, action taken).

    No-op for ``decision != 'skip'``.
    No-op when ``NOTIFY_VETO_ALERTS`` env var is 'false'.
    No-op when ``NOTIFY_TELEGRAM_ENABLED`` master switch is false (enforced
    inside :meth:`NotificationService.notify`).

    Fail-safe: any exception is caught and logged — never raises.
    """
    try:
        if decision != "skip":
            return
        if not _env_bool("NOTIFY_VETO_ALERTS", default=True):
            return

        confidence_pct = _format_confidence_pct(confidence)
        source_str = source or "unknown"
        reasoning_str = reasoning or "(no reasoning provided)"
        mode = (enforcement_mode or "").lower()

        if mode == "shadow":
            text = (
                f"🔬 [SHADOW] {symbol} ({source_str}) — veto would block\n\n"
                f"Reason: {reasoning_str}\n"
                f"Confidence: {confidence_pct}\n"
                f"Mode: shadow (informational only)"
            )
        elif mode == "active":
            text = (
                f"🚫 {symbol} ({source_str}) — veto BLOCKED entry\n\n"
                f"Reason: {reasoning_str}\n"
                f"Confidence: {confidence_pct}\n"
                f"Mode: active (trade not placed)"
            )
        else:
            text = (
                f"🔬 {symbol} ({source_str}) — veto decision: skip\n\n"
                f"Reason: {reasoning_str}\n"
                f"Confidence: {confidence_pct}\n"
                f"Mode: {enforcement_mode}"
            )

        get_notification_service().notify(
            "veto_decision",
            text,
            symbol=symbol,
            decision=decision,
            enforcement_mode=enforcement_mode,
        )
    except Exception as e:  # noqa: BLE001 — fail-safe by design
        logger.warning("publish_veto_decision_alert failed: %s", e)
