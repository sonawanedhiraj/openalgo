"""Telegram INBOUND bot — set ``strategy_daily_intent`` from the phone.

This is the Phase 6 counterpart to the OUTBOUND ``telegram_bot_service`` (alerts
/ EOD summaries). It polls Telegram for operator commands and writes the unified
``strategy_daily_intent`` table (``database/strategy_daily_intent_db.py``) so the
operator can pause / resume / halt a strategy, or cap its daily capital, without
laptop access.

Design (full grammar, auth model, halt-confirm, audit): ``docs/design/telegram_inbound.md``.

Key safety properties
----------------------
* **Mode flips are NOT exposed.** ``run`` / ``pause`` / ``halt`` (the *intent*
  axis) can be set from Telegram; ``live`` / ``sandbox`` / ``skip`` (the *mode*
  axis — HOW orders route) cannot. A mode word replies with a laptop-only notice.
  When an intent is set, the row's ``mode`` is preserved from the current
  effective decision so routing is never silently changed.
* **chat_id allowlist.** Only chat_ids in ``bot_config.telegram_chat_ids`` are
  honored; everyone else is silently ignored (no reply, no log spam).
* **Halt requires confirmation.** Any halt-triggering input arms a 30-second
  "reply YES" confirmation before the row is written.
* **Feature-flagged off by default.** ``TELEGRAM_INBOUND_ENABLED`` (default
  ``false``) gates boot wiring, so deploying this module starts no poller.

Threading / eventlet
---------------------
``python-telegram-bot`` is asyncio-based and incompatible with eventlet's
monkey-patched loop, so — exactly like ``telegram_bot_service`` — the polling
Application runs on a *real* OS thread with its own event loop (see
``_run_bot_in_thread``). Only ONE poller may run per bot token; do not enable
this while the full interactive ``telegram_bot_service`` is also polling the same
token (Telegram returns a getUpdates Conflict).

The command-parsing + DB-write logic lives in pure, dependency-injected methods
(``handle_text`` / ``handle_callback`` / ``status_text``) so it is fully testable
without a network or event loop. The PTB handlers are thin async wrappers.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from typing import Optional

import pytz

from utils.logging import get_logger

# Real (unpatched) threading so the bot loop is independent of eventlet.
if "eventlet" in sys.modules:
    import eventlet

    original_threading = eventlet.patcher.original("threading")
else:
    import threading as original_threading

logger = get_logger(__name__)

_IST = pytz.timezone("Asia/Kolkata")

# Strategies the inbound bot can control. Order drives the status list + the
# morning-prompt keyboard. Keep in sync with the engines that read the unified
# intent table.
INTENT_STRATEGIES = ("simplified_engine", "sector_follow_cap5_vol")

# Operator-friendly aliases → canonical strategy name.
_STRATEGY_ALIASES = {
    "simplified": "simplified_engine",
    "simplified_engine": "simplified_engine",
    "engine": "simplified_engine",
    "simplified-engine": "simplified_engine",
    "sector": "sector_follow_cap5_vol",
    "sector_follow": "sector_follow_cap5_vol",
    "sector-follow": "sector_follow_cap5_vol",
    "sector_follow_cap5_vol": "sector_follow_cap5_vol",
    "sf": "sector_follow_cap5_vol",
}

_INTENT_WORDS = ("run", "pause", "halt")
_MODE_WORDS = ("live", "sandbox", "skip")

_HALT_CONFIRM_WINDOW_SEC = 30

_MODE_DENIED_MSG = "Mode changes require laptop access for safety."

# Mode-only architecture (2026-06-12): the per-day intent control (run/pause/halt
# + capital cap) is retired. Strategies run continuously in their configured
# persistent mode (strategy_mode). The only daily input from Telegram is gone;
# every intent-setting command now returns this deprecation notice. Emergency
# pause is the sector_follow /api/pause REST endpoint (reachable over WireGuard/SSH).
_DEPRECATED_MSG = (
    "⚠️ This control was retired (mode-only architecture).\n"
    "Strategies run continuously in their configured mode — there is no daily "
    "run/pause/halt to set.\n"
    "• Mode changes (live/sandbox) require laptop access for safety.\n"
    "• For an emergency pause use the /api/pause REST endpoint over WireGuard/SSH.\n"
    "Use /status to see each strategy's current mode."
)

_USAGE = (
    "Usage:\n"
    "/status — show each strategy's current mode\n"
    "(The /intent, /pause, /resume, /halt commands are retired — see /status.)"
)


def _canonical_strategy(name: str) -> str | None:
    if not name:
        return None
    return _STRATEGY_ALIASES.get(name.strip().lower())


def _short(strategy: str) -> str:
    return "simplified" if strategy == "simplified_engine" else "sector_follow"


class TelegramInboundService:
    """Poll Telegram, gate on the chat_id allowlist, write the intent table.

    All side-effecting collaborators are injected (with production defaults) so
    the parsing + DB logic can be unit/E2E-tested without a network or loop.
    """

    def __init__(
        self,
        *,
        set_intent: Callable | None = None,
        get_intent: Callable | None = None,
        delete_intent: Callable | None = None,
        resolve_strategy_mode: Callable | None = None,
        authorized_chat_ids: Callable[[], set] | None = None,
        now: Callable[[], object] | None = None,
        scheduler=None,
    ):
        # Lazy production wiring (kept importable without a DB during tests).
        if set_intent is None or get_intent is None or delete_intent is None:
            from database.strategy_daily_intent_db import (
                delete_intent as _del,
            )
            from database.strategy_daily_intent_db import (
                get_intent as _get,
            )
            from database.strategy_daily_intent_db import (
                set_intent as _set,
            )

            set_intent = set_intent or _set
            get_intent = get_intent or _get
            delete_intent = delete_intent or _del
        if resolve_strategy_mode is None:
            from services.mode_service import resolve_strategy_mode as _rsm

            resolve_strategy_mode = _rsm
        if authorized_chat_ids is None:
            from database.telegram_db import get_authorized_chat_ids as _acl

            authorized_chat_ids = _acl

        self._set_intent = set_intent
        self._get_intent = get_intent
        self._delete_intent = delete_intent
        self._resolve = resolve_strategy_mode
        self._authorized_chat_ids = authorized_chat_ids
        self._now = now or (lambda: __import__("datetime").datetime.now(_IST))

        self.scheduler = scheduler
        # Pending halt confirmations: chat_id -> (strategy, expiry_epoch_seconds).
        self._pending_halt: dict[int, tuple[str, float]] = {}

        # PTB runtime state (populated on start()).
        self.application = None
        self.bot_token: str | None = None
        self.bot_thread = None
        self.bot_loop = None
        self.is_running = False
        self._stop_event = original_threading.Event()

    # ------------------------------------------------------------------ #
    # Authorization
    # ------------------------------------------------------------------ #
    def is_authorized(self, chat_id) -> bool:
        try:
            return int(chat_id) in self._authorized_chat_ids()
        except Exception:
            logger.debug("authorization check failed", exc_info=True)
            return False

    # ------------------------------------------------------------------ #
    # Pure command logic (testable without a network)
    # ------------------------------------------------------------------ #
    def _audit(self, chat_id, message_id) -> str:
        return f"telegram:{chat_id}:{message_id}"

    def _resolved_mode(self, strategy: str) -> str:
        try:
            return self._resolve(strategy).mode
        except Exception:
            logger.debug("resolve_strategy_mode failed for %s", strategy, exc_info=True)
            return "sandbox"

    def _today_row(self, strategy: str):
        try:
            return self._get_intent(strategy)
        except Exception:
            logger.debug("get_intent failed for %s", strategy, exc_info=True)
            return None

    def _apply_intent(self, strategy: str, intent: str, chat_id, message_id) -> str:
        """Write {mode(preserved), intent} for today. Mode is taken from the
        existing row if present else the current effective decision — Telegram
        never changes the routing mode."""
        existing = self._today_row(strategy)
        mode = existing["mode"] if existing else self._resolved_mode(strategy)
        cap = existing["daily_capital_cap"] if existing else None
        self._set_intent(
            strategy,
            None,
            mode,
            intent,
            cap,
            updated_by=self._audit(chat_id, message_id),
            notes="set via telegram inbound bot",
        )
        logger.info(
            "telegram intent set: %s -> %s (mode=%s) by chat=%s", strategy, intent, mode, chat_id
        )
        return f"✅ {strategy}: intent={intent} (mode={mode}, unchanged) for today."

    def _apply_cap(self, strategy: str, amount: float, chat_id, message_id) -> str:
        existing = self._today_row(strategy)
        mode = existing["mode"] if existing else self._resolved_mode(strategy)
        intent = existing["intent"] if existing else "run"
        self._set_intent(
            strategy,
            None,
            mode,
            intent,
            float(amount),
            updated_by=self._audit(chat_id, message_id),
            notes="cap set via telegram inbound bot",
        )
        logger.info("telegram cap set: %s -> ₹%s by chat=%s", strategy, amount, chat_id)
        return f"✅ {strategy}: daily_capital_cap=₹{float(amount):,.0f} (mode={mode}, intent={intent})."

    def _apply_clear(self, strategy: str, chat_id, message_id) -> str:
        removed = self._delete_intent(strategy)
        logger.info(
            "telegram intent cleared: %s (removed=%s) by chat=%s", strategy, removed, chat_id
        )
        if removed:
            return f"✅ {strategy}: today's intent row cleared — reverting to fall-through (legacy/env default)."
        return f"ℹ️ {strategy}: no intent row for today (already on fall-through)."

    def _arm_halt_confirm(self, strategy: str, chat_id) -> str:
        expiry = self._epoch() + _HALT_CONFIRM_WINDOW_SEC
        self._pending_halt[int(chat_id)] = (strategy, expiry)
        return (
            f"⚠️ Confirm HALT for {strategy}? This skips entries AND exits today.\n"
            f"Reply YES within {_HALT_CONFIRM_WINDOW_SEC} seconds."
        )

    def _epoch(self) -> float:
        try:
            return self._now().timestamp()
        except Exception:
            return time.time()

    def _consume_pending_halt(self, chat_id) -> str | None:
        """If a non-expired halt confirmation is pending for chat_id, return its
        strategy and clear it; else None. Expired pendings are dropped."""
        cid = int(chat_id)
        pending = self._pending_halt.get(cid)
        if not pending:
            return None
        strategy, expiry = pending
        del self._pending_halt[cid]
        if self._epoch() > expiry:
            return None
        return strategy

    def status_text(self) -> str:
        """Mode-only: report each strategy's current persistent mode + an active
        runtime-override (pause/kill_switch) if one is holding entries."""
        lines = ["📋 Strategy modes (mode-only)"]
        for strat in INTENT_STRATEGIES:
            try:
                d = self._resolve(strat)
                note = ""
                try:
                    from database.strategy_runtime_override_db import is_entry_blocked

                    blocked, ov = is_entry_blocked(strat)
                    if blocked and ov:
                        note = f"  ⏸ entries held ({ov.get('override_type')}: {ov.get('reason')})"
                except Exception:
                    pass
                lines.append(f"• {strat}: {d.mode}  ({d.source}){note}")
            except Exception:
                lines.append(f"• {strat}: (unavailable)")
        lines.append("")
        lines.append("Mode flips are laptop-only. Emergency pause: /api/pause (REST).")
        return "\n".join(lines)

    def handle_text(self, chat_id, message_id, text: str) -> str | None:
        """Route one inbound text message. Returns the reply string, or None to
        send nothing (unauthorized chat, or pure silent ignore).

        ``/morning`` is intentionally NOT handled here — it needs to *send* an
        inline keyboard, which the PTB layer does. This method covers every
        command + free-text reply that resolves to a text answer."""
        if not self.is_authorized(chat_id):
            return None  # silent ignore

        raw = (text or "").strip()

        if not raw:
            return _USAGE

        tokens = raw.split()
        head = tokens[0].lower()

        # Live commands (mode-only): only status/help remain.
        if head in ("/start", "/status", "start", "status"):
            return self.status_text()
        if head in ("/help", "help"):
            return _USAGE

        # RETIRED intent-setting commands + their free-text forms → deprecation
        # notice. The run/pause/halt + capital-cap daily control is gone; mode is
        # the persistent operator knob (laptop-only), emergency pause is /api/pause.
        if head in (
            "/intent",
            "intent",
            "/pause",
            "pause",
            "/resume",
            "resume",
            "/halt",
            "halt",
            "/morning",
        ):
            return _DEPRECATED_MSG

        # Any other slash command or free-text → usage (points at /status).
        return _USAGE

    def _cmd_intent(self, args: list[str], chat_id, message_id) -> str:
        if len(args) < 2:
            return _USAGE
        strategy = _canonical_strategy(args[0])
        if strategy is None:
            return f"❓ Unknown strategy '{args[0]}'. Known: {', '.join(INTENT_STRATEGIES)}"
        sub = args[1].lower()
        if sub in _INTENT_WORDS:
            if sub == "halt":
                return self._arm_halt_confirm(strategy, chat_id)
            return self._apply_intent(strategy, sub, chat_id, message_id)
        if sub in _MODE_WORDS:
            return _MODE_DENIED_MSG
        if sub == "cap":
            if len(args) < 3:
                return "Usage: /intent <strategy> cap <amount>"
            try:
                amount = float(args[2].replace(",", "").replace("₹", ""))
            except ValueError:
                return f"❓ '{args[2]}' is not a valid amount."
            if amount <= 0:
                return "Amount must be positive."
            return self._apply_cap(strategy, amount, chat_id, message_id)
        if sub == "clear":
            return self._apply_clear(strategy, chat_id, message_id)
        return _USAGE

    def _cmd_simple(self, args: list[str], intent: str, chat_id, message_id) -> str:
        if not args:
            return f"Usage: /{'resume' if intent == 'run' else intent} <strategy>"
        strategy = _canonical_strategy(args[0])
        if strategy is None:
            return f"❓ Unknown strategy '{args[0]}'. Known: {', '.join(INTENT_STRATEGIES)}"
        return self._apply_intent(strategy, intent, chat_id, message_id)

    def _cmd_halt(self, args: list[str], chat_id, message_id) -> str:
        if not args:
            return "Usage: /halt <strategy>"
        strategy = _canonical_strategy(args[0])
        if strategy is None:
            return f"❓ Unknown strategy '{args[0]}'. Known: {', '.join(INTENT_STRATEGIES)}"
        return self._arm_halt_confirm(strategy, chat_id)

    def handle_callback(self, chat_id, message_id, data: str) -> str | None:
        """Inline-button presses are retired (mode-only). The morning intent
        keyboard no longer ships; any stale button press returns the deprecation
        notice."""
        if not self.is_authorized(chat_id):
            return None
        return _DEPRECATED_MSG

    # ------------------------------------------------------------------ #
    # Morning prompt (inline keyboard)
    # ------------------------------------------------------------------ #
    def morning_keyboard_spec(self) -> list[list[tuple[str, str]]]:
        """Keyboard as rows of (button_label, callback_data) — one row per
        strategy with Run / Pause / Halt. Pure; used to build the markup and to
        assert structure in tests."""
        rows = []
        for strat in INTENT_STRATEGIES:
            short = _short(strat)
            rows.append(
                [
                    (f"▶ {short} Run", f"intent:{strat}:run"),
                    (f"⏸ {short} Pause", f"intent:{strat}:pause"),
                    (f"⏹ {short} Halt", f"intent:{strat}:halt"),
                ]
            )
        return rows

    def _build_markup(self):
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        rows = [
            [InlineKeyboardButton(label, callback_data=cb) for label, cb in row]
            for row in self.morning_keyboard_spec()
        ]
        return InlineKeyboardMarkup(rows)

    def morning_text(self) -> str:
        return "🌅 Good morning. Set today's intent:\n" + self.status_text()

    # ------------------------------------------------------------------ #
    # PTB async handlers (thin wrappers over the pure logic)
    # ------------------------------------------------------------------ #
    async def _on_message(self, update, context) -> None:
        try:
            msg = update.effective_message
            chat_id = update.effective_chat.id
            text = (msg.text or "") if msg else ""
            if not self.is_authorized(chat_id):
                return  # silent ignore
            head = text.strip().split()[0].lower() if text.strip() else ""
            if head == "/morning":
                await self._send_morning(context.bot, chat_id)
                return
            reply = self.handle_text(chat_id, msg.message_id, text)
            if reply:
                await context.bot.send_message(chat_id=chat_id, text=reply)
        except Exception as e:
            logger.exception(f"inbound _on_message failed: {e}")

    async def _on_callback(self, update, context) -> None:
        try:
            query = update.callback_query
            chat_id = update.effective_chat.id
            await query.answer()
            if not self.is_authorized(chat_id):
                return
            msg_id = query.message.message_id if query.message else "cb"
            reply = self.handle_callback(chat_id, msg_id, query.data)
            if reply:
                await context.bot.send_message(chat_id=chat_id, text=reply)
        except Exception as e:
            logger.exception(f"inbound _on_callback failed: {e}")

    async def _send_morning(self, bot, chat_id) -> None:
        await bot.send_message(
            chat_id=chat_id, text=self.morning_text(), reply_markup=self._build_markup()
        )

    def send_morning_prompt_to_all(self) -> int:
        """Scheduler entry point: send the morning keyboard to every authorized
        chat_id, scheduling the async send on the live bot loop. Returns the
        number of chats targeted. No-op if the bot isn't running."""
        if not self.is_running or self.bot_loop is None or self.application is None:
            logger.info("morning prompt skipped — inbound bot not running")
            return 0
        import asyncio

        chats = self._authorized_chat_ids()
        sent = 0
        for cid in chats:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._send_morning(self.application.bot, cid), self.bot_loop
                )
                sent += 1
            except Exception as e:
                logger.exception(f"morning prompt to {cid} failed: {e}")
        logger.info("morning intent prompt sent to %d chat(s)", sent)
        return sent

    def send_message_to_all(self, text: str) -> int:
        """Send a plain-text message to every authorized chat_id.

        The outbound counterpart of :meth:`send_morning_prompt_to_all`, exposed
        so the one-way :mod:`services.notification_service` can reuse this live
        poller's bot loop when the legacy ``telegram_bot_service`` is inactive
        (Phase 6 freed the bot token to this poller). Returns the number of
        chats targeted; a no-op (returns 0) when the bot isn't running. Never
        raises — a send failure for one chat is logged and the rest proceed.
        """
        if not self.is_running or self.bot_loop is None or self.application is None:
            return 0
        import asyncio

        chats = self._authorized_chat_ids()
        sent = 0
        for cid in chats:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.application.bot.send_message(chat_id=cid, text=text),
                    self.bot_loop,
                )
                sent += 1
            except Exception as e:
                logger.exception(f"inbound send to {cid} failed: {e}")
        return sent

    # ------------------------------------------------------------------ #
    # Lifecycle (real-thread event loop, eventlet-safe)
    # ------------------------------------------------------------------ #
    def start(self) -> tuple[bool, str]:
        if self.is_running:
            return False, "Inbound bot already running"
        try:
            from database.telegram_db import get_bot_config

            cfg = get_bot_config()
            token = (cfg or {}).get("bot_token")
            if not token:
                return False, "Bot token not configured (set it on the Telegram page first)"
            self.bot_token = token
            self._stop_event.clear()
            self.bot_thread = original_threading.Thread(
                target=self._run_bot_in_thread, daemon=True, name="TelegramInboundThread"
            )
            self.bot_thread.start()
            for _ in range(20):  # up to 10s
                if self.is_running:
                    return True, "Inbound bot started"
                time.sleep(0.5)
            return False, "Inbound bot failed to start within timeout"
        except Exception as e:
            logger.exception(f"Failed to start inbound bot: {e}")
            return False, str(e)

    def stop(self) -> tuple[bool, str]:
        if not self.is_running:
            return False, "Inbound bot not running"
        self._stop_event.set()
        if self.bot_thread and self.bot_thread.is_alive():
            self.bot_thread.join(timeout=10.0)
        self.is_running = False
        self.application = None
        self.bot_loop = None
        self.bot_thread = None
        logger.info("Telegram inbound bot stopped")
        return True, "Inbound bot stopped"

    def _run_bot_in_thread(self) -> None:
        import asyncio

        if "eventlet" in sys.modules:
            try:
                from asyncio import DefaultEventLoopPolicy

                asyncio.set_event_loop_policy(DefaultEventLoopPolicy())
            except Exception as e:
                logger.warning(f"inbound: could not reset event loop policy: {e}")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.bot_loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            logger.exception(f"inbound bot thread error: {e}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self.bot_loop = None
            self.is_running = False

    async def _serve(self) -> None:
        import asyncio

        from telegram import Update
        from telegram.ext import (
            Application,
            CallbackQueryHandler,
            MessageHandler,
            filters,
        )

        self.application = Application.builder().token(self.bot_token).build()
        self.application.add_handler(
            MessageHandler(filters.TEXT | filters.COMMAND, self._on_message)
        )
        self.application.add_handler(CallbackQueryHandler(self._on_callback))

        await self.application.initialize()
        await self.application.start()
        await self.application.updater.start_polling(
            drop_pending_updates=True, allowed_updates=Update.ALL_TYPES
        )
        self.is_running = True
        logger.info("Telegram inbound bot polling for intent commands")

        while not self._stop_event.is_set():
            await asyncio.sleep(1)

        self.is_running = False
        try:
            if self.application.updater.running:
                await self.application.updater.stop()
            await self.application.stop()
            await self.application.shutdown()
        except Exception as e:
            logger.debug(f"inbound shutdown error: {e}")

    # ------------------------------------------------------------------ #
    # Scheduler registration
    # ------------------------------------------------------------------ #
    def register_jobs(self, scheduler=None) -> None:
        """Mode-only: the 08:45 IST morning intent-prompt job is RETIRED — there
        is no per-day intent to prompt for. This is now a no-op beyond pinning
        the singleton, kept so boot wiring (``init_telegram_inbound_service``)
        need not change. As a safety net it also removes any stale
        ``telegram_inbound_morning_prompt`` job left in a persistent scheduler."""
        global _SINGLETON
        _SINGLETON = self
        sched = scheduler or self.scheduler
        if sched is None:
            try:
                from services.historify_scheduler_service import get_historify_scheduler

                sched = get_historify_scheduler().scheduler
            except Exception:
                sched = None
        if sched is not None:
            try:
                sched.remove_job("telegram_inbound_morning_prompt")
                logger.info("removed stale telegram_inbound_morning_prompt job (retired)")
            except Exception:
                pass  # not registered — expected
        logger.info("telegram inbound: morning-prompt job retired (mode-only)")


# --------------------------------------------------------------------------- #
# Module-level singleton + serializable scheduler entry point
# --------------------------------------------------------------------------- #
_SINGLETON: TelegramInboundService | None = None


def get_service() -> TelegramInboundService | None:
    return _SINGLETON


def _morning_prompt_job() -> None:
    if _SINGLETON is not None:
        _SINGLETON.send_morning_prompt_to_all()


def _inbound_enabled() -> bool:
    raw = os.getenv("TELEGRAM_INBOUND_ENABLED", "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def init_telegram_inbound_service(app=None, scheduler=None) -> TelegramInboundService | None:
    """Build the singleton, register the morning-prompt job, and start polling —
    but ONLY when ``TELEGRAM_INBOUND_ENABLED`` is truthy. Returns the service (or
    None when the flag is off). Safe to call at boot: a missing token or a
    disabled flag is a no-op."""
    global _SINGLETON
    if not _inbound_enabled():
        logger.info("Telegram inbound bot disabled (TELEGRAM_INBOUND_ENABLED=false)")
        return None
    svc = TelegramInboundService(scheduler=scheduler)
    _SINGLETON = svc
    try:
        svc.register_jobs(scheduler)
    except Exception as e:
        logger.exception(f"Failed to register inbound morning-prompt job: {e}")
    ok, msg = svc.start()
    logger.info("Telegram inbound bot start: %s (%s)", ok, msg)
    if app is not None:
        app.telegram_inbound_service = svc
    return svc
