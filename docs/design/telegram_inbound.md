# Telegram inbound intent bot (Phase 6)

Status: **shipped, feature-flagged OFF by default** (`TELEGRAM_INBOUND_ENABLED=false`).
Module: [`services/telegram_inbound_service.py`](../../services/telegram_inbound_service.py).
Tests: [`test/e2e/test_critical_flows.py`](../../test/e2e/test_critical_flows.py) (`TestTelegramInboundEndToEnd`, `TestChatAllowlist`).

## Purpose

Let the operator set the unified [`strategy_daily_intent`](strategy_daily_intent.md)
row for a strategy **from the phone** — pause / resume / halt a strategy, or cap
its daily capital — without laptop access. It is the *inbound* counterpart to the
existing *outbound* `telegram_bot_service` (alerts / EOD summaries). It writes the
same table the engines already gate on (`resolve_strategy_mode`), so a phone
command takes effect on the next scheduled job with zero engine changes.

## Hard safety boundaries

| Boundary | Rule |
| --- | --- |
| **Mode flips** | NOT exposed. `live` / `sandbox` / `skip` (HOW orders route) cannot be set from Telegram. A mode word replies `"Mode changes require laptop access for safety."` When an *intent* is set, the row's `mode` is **preserved** from the current effective decision — Telegram never silently changes routing. |
| **Authorization** | chat_id allowlist in `bot_config.telegram_chat_ids` (comma-separated). Unauthorized chat_ids are **silently ignored** — no reply, no log spam. |
| **Halt confirmation** | Any halt-triggering input (command, free-text, or button) arms a 30-second `"reply YES"` confirmation before the row is written. |
| **Feature flag** | `TELEGRAM_INBOUND_ENABLED` (default `false`) gates boot wiring. Deploying the module starts no poller. |
| **Single poller per token** | Telegram allows ONE `getUpdates` consumer per bot token. Do NOT enable this while the full interactive `telegram_bot_service` is also polling the same token (Telegram returns a `Conflict`). In this deployment the outbound path is send-only, so the inbound bot owns polling when enabled. |

## Command grammar

```
/start, /status                       show today's intent for all strategies
/intent <strategy> <run|pause|halt>   set intent (halt → YES confirm)
/intent <strategy> cap <amount>       set daily_capital_cap override
/intent <strategy> clear              delete today's row → fall-through resumes
/intent <strategy> <live|sandbox|skip> refused (laptop-only)
/pause <strategy>                     = /intent <strategy> pause
/resume <strategy>                    = /intent <strategy> run
/halt <strategy>                      = /intent <strategy> halt (YES confirm)
/morning                              send the inline-keyboard intent prompt
```

Free-text replies (e.g. replying to a kill-switch alert or EOD summary) are
parsed the same way: `pause sector_follow`, `resume simplified`, `halt <x>`.
Unparseable input → a usage hint. Strategy **aliases** are accepted:
`simplified` / `engine` → `simplified_engine`; `sector` / `sf` / `sector_follow`
→ `sector_follow_cap5_vol`.

## Morning auto-prompt

An APScheduler cron job at **08:45 IST** (`Asia/Kolkata`, mon–fri) sends
*"Good morning. Set today's intent:"* plus the current status and an inline
keyboard — one row per registry strategy (`simplified_engine`,
`sector_follow_cap5_vol`) with **Run / Pause / Halt** buttons. Button
`callback_data` is `intent:<strategy>:<word>`. A Run/Pause press applies
immediately; a Halt press arms the YES-confirmation flow. The job is a no-op when
the bot isn't running.

## Audit

Every intent change writes `updated_by = "telegram:<chat_id>:<message_id>"`, so
each DB row traces back to the exact Telegram message (or callback) that caused
it. All changes log at `INFO`.

## Threading / eventlet

`python-telegram-bot` (22.6) is asyncio-based and conflicts with eventlet's
monkey-patched loop. Like `telegram_bot_service`, the polling `Application` runs
on a **real OS thread** with its own event loop (`_run_bot_in_thread` resets the
event-loop policy under eventlet). The morning-prompt scheduler job (running on
the APScheduler thread) dispatches the async send onto that loop via
`asyncio.run_coroutine_threadsafe`.

## Testability

The command-parsing + DB-write logic lives in pure, dependency-injected methods
(`handle_text`, `handle_callback`, `status_text`, `morning_keyboard_spec`) that
return reply strings and take injected `set_intent` / `get_intent` /
`delete_intent` / `resolve_strategy_mode` / `authorized_chat_ids` / `now`. The
PTB async handlers are thin wrappers. E2E tests drive the pure methods against a
temp-SQLite intent DB and assert the row appears AND that the real
`SectorFollowService` gate then honors it — no network, no event loop.

## Operator activation

1. Add the chat_id to the allowlist:
   ```sql
   -- db/openalgo.db
   UPDATE bot_config SET telegram_chat_ids = '<your_chat_id>' WHERE id = 1;
   ```
   (or `database.telegram_db.add_authorized_chat_id(<chat_id>)`).
2. Set `TELEGRAM_INBOUND_ENABLED=true` in `.env`.
3. Restart OpenAlgo. Confirm the boot log line *"Telegram inbound bot polling for
   intent commands"*. Ensure the full interactive bot is not polling the same
   token.
