# Multi-Account Trading — Design Plan

**Status (2026-07-27):** ALL FOUR PHASES IMPLEMENTED — Phase 1 accounts core
(#468), Phase 2 order fan-out (#474), Phase 3 observability (#476), Phase 4
hardening incl. the rejected-entry exit guard (#478). Mirroring stays dormant
until the operator sets `MULTI_ACCOUNT_ENABLED=true` AND takes a strategy live
with an enabled child. See the CLAUDE.md "Multi-account mirror trading"
section for the operational summary.
**Goal:** One OpenAlgo install drives multiple broker accounts. One **primary** account
keeps everything it does today (WebSocket feed, historical downloads, scanner,
dashboards). Additional **child** accounts exist for exactly one purpose:
**simultaneously placing the same trades**, each with its own capital and its own
choice of which strategies it mirrors.

---

## 1. Guiding principle — the primary account is untouched

The whole platform today is built around one broker session:

| Surface | Today | After this plan |
|---|---|---|
| WebSocket feed (`websocket_proxy/`) | one adapter keyed by username | **unchanged — primary only** |
| Historical data (`history_service`, `historify_service`) | `get_auth_token_broker(api_key)` → the one auth row | **unchanged — primary only** |
| Scanner, backfills, data-freshness, watchdogs | primary session | **unchanged** |
| Sandbox (`sandbox.db`) | one virtual ₹1Cr book | **unchanged — children never trade sandbox** |
| Strategy engines (signal generation, sizing, journaling) | compute qty from their own capital config | **unchanged** |
| Order placement (`place_order_service`) | one broker | **+ fan-out to enabled child accounts** |

Everything that reads market data, computes signals, or renders dashboards keeps
resolving the primary account exactly as it does now (`Auth.name = username`).
The *only* new behavior is a **mirror step** bolted onto the order-placement
path. This keeps the change small, keeps every existing test valid, and means a
child-account failure can never take down the feed or the primary book.

### Non-goals (deliberately out of scope)

- No per-child WebSocket feeds, no per-child historical data.
- No per-child sandbox books — children are live-only mirrors. When a strategy
  is in `sandbox` mode, children do nothing.
- No independent strategies per child — children mirror the primary's signals,
  never generate their own.
- No multi-user web login. Still one operator, one OpenAlgo login.
- No cancel/modify mirroring in v1 (engines use MARKET orders end-to-end; the
  one exception is noted in §6).

---

## 2. Real-world constraints (Zerodha, SEBI)

1. **Each child account needs its own Kite Connect app** (own `api_key` +
   `api_secret`) — but **all apps must live under ONE developer profile** (the
   operator's account at developers.kite.trade). This is forced by the IP rule
   below: Zerodha whitelists the static IP at the *developer-profile* level
   (it applies to every app under it), and each IP can be linked to only one
   developer profile. One dev profile → one app per child → any family client
   can log into its app. Check current Kite Connect pricing per app; children
   do **not** need the historical-data add-on (only the primary downloads
   history).
2. **SEBI static-IP mandate — sharing is family-only.** Within one broker, a
   static IP cannot be shared across *unrelated* clients. SEBI's family
   definition (spouse, dependent children, dependent parents) CAN share one
   IP, with a declaration + 2FA consent from each account holder. There is no
   published numeric cap — the limit is the relationship, not a count. All
   orders here originate from this one server IP, so **every child account
   must be a SEBI-family member of the IP's registrant**; a friend's account
   cannot legally ride this server.
3. **Daily login per account.** Zerodha tokens expire ~3 AM IST for every
   account independently. Each child needs its own morning login click (own
   request_token flow, own TOTP). The existing external-TOTP helper (#460) is
   extended per-account (§5).
4. **Kite redirect URL is fixed per app** — set each child app's redirect to
   `http://<host>:5000/zerodha/callback?account_id=<N>` so the callback knows
   which account it belongs to (the primary's app keeps the bare URL → zero
   change to the existing flow).

---

## 3. Data model (3 new tables, 1 changed, 0 migrations of existing data)

### 3.1 `broker_accounts` (new — `database/broker_accounts_db.py`)

| Column | Type | Notes |
|---|---|---|
| `id` | int PK | |
| `display_name` | str, unique | e.g. "Dad — Zerodha", shown everywhere |
| `broker` | str | `zerodha` initially |
| `broker_client_id` | str | e.g. `AB1234`, informational |
| `api_key_encrypted` | text | Fernet (existing pepper-derived key) |
| `api_secret_encrypted` | text | Fernet |
| `capital_inr` | float | the account's total book (drives sizing, §6) |
| `is_enabled` | bool, default **false** | master per-account switch (default deny) |
| `created_at`, `updated_at` | datetime | |

The **primary is NOT a row here**. Primary stays on `.env`
(`BROKER_API_KEY`/`SECRET`) + the existing `auth` row — untouched.

### 3.2 `account_strategies` (new)

| Column | Type | Notes |
|---|---|---|
| `account_id` | FK → broker_accounts | |
| `strategy_name` | str | canonical `mode_key` (`futures_follow_cap50`, …) |
| PK | (account_id, strategy_name) | row present = strategy mirrored |

Simple allow-list: a row means "this account mirrors this strategy". No row =
skip. (Optional later: a per-row `capital_override_inr`; not in v1.)

### 3.3 `account_orders` (new — the mirror journal)

One row per attempted child order: `id, account_id, strategy_name, symbol,
exchange, action, parent_qty, child_qty, status
(placed|rejected|skipped_zero_qty|skipped_no_session|error), broker_orderid,
error_text, created_at`. This is the audit trail and the source for the order
book's "Accounts" column and the EOD per-account summary. Child fills live in
the **broker's** books — OpenAlgo does not maintain child positions.

### 3.4 Changed: child auth tokens reuse the existing `auth` table

`Auth.name` is unique and is the session key everywhere. Children get rows with
`name = f"acct:{account_id}"` (can never collide with a username — usernames
can't contain `:`). Benefits: token encryption, revocation, caching, and the
daily-expiry semantics all come for free; `upsert_auth` / `get_auth_token`
work unchanged. The ZMQ `CACHE_INVALIDATE` that `upsert_auth` publishes is
harmless for child names (the WS proxy has no adapter under that key).

Also: `broker_totp_secrets.broker` unique constraint becomes
`(broker, account_id NULLABLE)` — `NULL` = primary (backward compatible).

---

## 4. New service layer

### 4.1 `services/broker_accounts_service.py`

CRUD + session state. Key functions:

- `list_accounts()` → rows + per-account session status (does `auth` row
  `acct:{id}` exist, non-revoked, today-fresh?)
- `add_account(...)` / `update_account(...)` / `delete_account(id)` (delete
  also revokes its auth row)
- `get_login_url(account_id)` → Kite login URL built from the account's own
  api_key
- `complete_login(account_id, request_token)` → exchanges token with the
  account's own api_secret → `upsert_auth(f"acct:{id}", f"{api_key}:{access_token}",
  "zerodha")` (same `key:token` format the Zerodha order API expects)

### 4.2 `services/account_fanout_service.py` — the core

One public function:

```
fan_out_order(order_data, mode_key, parent_result) -> list[ChildOrderResult]
```

Called from **one seam**: the tail of
`place_order_service.place_order_with_auth()` (line ~120), **only when**:

1. the parent order resolved `EffectiveMode.LIVE` (sandbox → no fan-out, so
   Analyze-mode ON silences children automatically — the existing kill switch
   covers everyone), **and**
2. `mode_key` is a known strategy name (manual UI/API orders don't fan out in
   v1 — mirrors are for strategies only), **and**
3. the parent order was **accepted** by the broker (a rejected primary entry
   must not create naked child positions).

For each `broker_accounts` row with `is_enabled` AND an `account_strategies`
row for this `mode_key`:

1. Resolve child token via `get_auth_token(f"acct:{id}")`. Missing/expired →
   journal `skipped_no_session` + Telegram WARNING, continue.
2. Scale quantity (§6). Zero after lot rounding → journal `skipped_zero_qty`.
3. Call the broker order API **directly** with the child token
   (`broker.zerodha.api.order_api.place_order_api(child_order, child_token)`)
   — bypassing `place_order` avoids recursion, sandbox dispatch, and semi-auto
   interception for mirrors.
4. Journal the result. Failures are **per-child, never raised** — one child's
   rejection cannot block the primary or the other children.

Because **exits go through the same `place_order` path** (engine exits, EOD
watchdog flattens, kill-switch closes are all `place_order` calls with the same
`mode_key`), fan-out covers entries AND exits with no extra wiring. This is the
single most important property of choosing this seam.

Runs on a small thread pool (children in parallel, ~1–2 s total), with a hard
per-child timeout so a hung broker call can't stall the parent's return.

---

## 5. Broker login flow (per account, daily)

### 5.1 Primary — completely unchanged

The existing flow stays byte-for-byte: `/zerodha/callback` → `request_token` →
exchange with the `.env` `BROKER_API_KEY`/`SECRET` → `handle_auth_success()` →
`upsert_auth(username, "<api_key>:<access_token>", "zerodha")` → master-contract
download decision → ZMQ `CACHE_INVALIDATE` → WS proxy reconnects the feed. The
primary's Kite app keeps its bare redirect URL, so the callback with **no**
`account_id` arg is, by definition, the primary.

### 5.2 Child — one-time setup (per account)

1. In the **operator's single developer console** (developers.kite.trade —
   same profile that hosts the primary's app, because the whitelisted IP is
   profile-level) → create one new Kite Connect app for this child.
2. File the family static-IP sharing declaration with Zerodha for this
   account (spouse / dependent child / dependent parent; 2FA consent from the
   account holder).
3. Set the app's **redirect URL** to
   `http://<host>:5000/zerodha/callback?account_id=<N>` — the Accounts page
   shows the exact URL to paste after the account row is saved (Kite has no
   OAuth `state` param; the fixed redirect URL carrying `account_id` is how the
   callback knows which account it belongs to).
4. Enter api_key + api_secret in the Add Account modal (stored Fernet-encrypted).
5. Optionally enroll the account's TOTP secret (per-account row in
   `broker_totp_secrets`) so the Accounts page can display the rolling 6-digit
   code — same pattern as the existing #460 helper.

### 5.3 Child — daily login (manual, ~20 seconds per account)

Zerodha tokens for EVERY account expire ~3:00 AM IST independently. The daily
sequence per child, driven from the Accounts page:

1. Row shows 🔴 **Login needed** → operator clicks **Connect**.
2. New tab opens `https://kite.zerodha.com/connect/login?api_key=<child_key>&v=3`.
3. Operator enters the child's client id + password, then the TOTP code read
   off the Accounts page row (if enrolled) or the account holder's phone.
4. Zerodha redirects to `/zerodha/callback?account_id=N&request_token=...`.
5. `brlogin.py` sees `account_id` → routes to
   `broker_accounts_service.complete_login(N, request_token)`: SHA-256 checksum
   exchange with the child's own api_secret →
   `upsert_auth("acct:N", "<child_api_key>:<access_token>", "zerodha")` → row
   flips to 🟢.

**What the child path deliberately does NOT do** (all primary-only side effects
of `handle_auth_success` are skipped): no Flask `session[...]` mutation (the
operator's web session stays bound to the primary), no master-contract download
(contracts are global, already loaded), no WS notify / `BrokerSessionRefreshedEvent`
(children have no feed). The ZMQ `CACHE_INVALIDATE` that `upsert_auth` always
publishes is harmless for `acct:N` names — the WS proxy holds no adapter under
that key. The callback route keeps `brlogin`'s existing gate (`"user" in
session`), NOT `check_session_validity` (destructive on failure — issue #462).

### 5.4 Headless auto-login — built under operator sign-off (issue #654, 2026-08-19)

> **This reverses the original "deliberately not built" decision.** The section
> below is kept for the record; the operator explicitly authorized headless
> auto-login on 2026-08-19 (both the primary and enabled children), accepting the
> risk trade-off it records.

**Original decision (superseded):** child passwords were never stored and the
daily login was manual by design — mirroring the #460 primary decision (external
TOTP helper yes, headless auto-login no), because storing broker passwords for
unattended login is a materially different risk class and Zerodha's ToS
discourage login automation (the exchange mandates a manual login at least once a
day; Kite deems automation "not recommended").

**What shipped (issue #654):** an **opt-in** (`BROKER_AUTO_LOGIN_ENABLED`, default
OFF) headless login for the primary and enabled children:

- The Kite login password is stored **Fernet-encrypted** (same `API_KEY_PEPPER`-
  derived key as auth tokens / the TOTP secret) — primary in
  `broker_login_credentials`, children in `broker_accounts.password_encrypted`.
  Write-only: no API ever returns it.
- Login is **browser-driven via Playwright/Chromium** (`services/zerodha_web_login`):
  it fills user-id/password then the External-TOTP field (pyotp) on Kite's own
  pages and captures `request_token` from the redirect, then the existing
  checksum exchange. **Direct HTTP was tried first and does NOT work** — Kite's
  `/api/twofa` rejects a provably-correct TOTP with `TwoFAException` from any HTTP
  client (httpx/requests, browser headers, connect `sess_id` all fail); the 2FA
  step requires real-browser context. Playwright runs on a real OS thread
  (eventlet-safe) and the browser binary must be installed on the host
  (`uv run playwright install chromium`).
- Triggered three ways, all reusing `services/broker_auto_login_service`: a manual
  button (`/api/broker-auto-login/login`, child `POST /<id>/auto_login`), a boot
  hook, and the **continuous watcher** (`services/broker_auto_login_watcher`) that
  re-logs-in on a confirmed dead session — covering both the ~06:30-07:30 IST
  daily-reset flush (which can land after a morning boot) and mid-session
  single-session invalidation.
- **Risk trade-off accepted:** the password is the first unattended-usable broker
  credential in the repo. Mitigations: opt-in default-OFF, encrypted at rest,
  write-only APIs, a daily attempt cap + backoff so a wrong password can't hammer
  Kite, loud failure that falls back to the always-available manual flow, and the
  standing rule to never rotate `API_KEY_PEPPER`/`FERNET_SALT`.

### 5.5 Reminders & stale-child safety

- **9:00 AM IST Telegram reminder** lists accounts still disconnected (reuses
  `notification_service.notify`, skipped on non-trading days via
  `data_freshness_service.is_trading_day`).
- A second nudge at **15:00 IST** if an *enabled* child with selected
  strategies is still 🔴 — the last practical window before the 15:20 entries.
- The 15:18 smoke checks stay primary-only. A child with no session gets
  `skipped_no_session` journal rows + a Telegram WARNING per fan-out batch —
  it never holds the primary's trading.
- Token expiry mid-day (rare, e.g. child logs into Kite web elsewhere and
  invalidates the API session): the broker rejects the mirror order, journaled
  as `error` + Telegram; next morning's login clears it.

---

## 6. Sizing — one multiplier per account

Keep it as simple as possible: each account has **one capital number**, and
every mirrored order scales by a single factor:

```
factor      = account.capital_inr / PRIMARY_BOOK_CAPITAL     (env, e.g. 1,000,000)
child_qty   = floor(parent_qty × factor)                      (equity)
child_lots  = floor(parent_lots × factor)                     (F&O — lot-size aware)
```

- `PRIMARY_BOOK_CAPITAL` is a new env var documented in `docs/PARAMETER_LOG.md`
  = the primary's total deployed capital across strategies. A ₹2.5L child on a
  ₹10L primary gets factor 0.25 — every strategy it enables mirrors at quarter
  size, so the child's book automatically has the same *proportions* as the
  primary's.
- Rounds **down**; a result of 0 shares / 0 lots is journaled `skipped_zero_qty`
  (important for `futures_follow_cap50`: one NIFTY lot needs ~₹2.8L margin — a
  ₹1L child correctly mirrors nothing, and the UI warns about this at
  strategy-selection time).
- **Exit asymmetry guard:** for SELL/exit orders the child qty is *not* scaled —
  it is read from the child's own open position for that symbol (one broker
  positions call, cached per fan-out batch). This prevents drift when an entry
  was partially rejected: exits always flatten what the child actually holds.
  This is the one place mirrors are smarter than a blind copy, and it's worth
  the ~20 lines.
- Per-strategy capital overrides: **not in v1** (noted as future work).

---

## 7. UI (React) — two touch points

### 7.1 New page: `/accounts`

`frontend/src/pages/accounts/AccountsPage.tsx` + route in `App.tsx` + route in
`blueprints/react_app.py` + new `blueprints/broker_accounts.py` API
(`/broker_accounts/api/*`, all `@check_session_validity` — **not** on any
broker-callback path, per the #462 rule). Sections:

1. **Primary card** (read-only): broker, session status, "manages feed +
   data + all strategy signals".
2. **Child accounts table**: name, client id, capital, factor, enabled toggle,
   session LED, Connect button, TOTP code, today's mirror stats (placed /
   skipped / errors from `account_orders`), edit/delete.
3. **Add account** modal: name, client id, api_key, api_secret, capital.
   Secrets are write-only (masked after save, like `/broker/credentials`).
4. Per-account **strategy checklist** (expand row): checkbox per known strategy
   with the computed per-strategy mirror size preview ("futures_follow_cap50 at
   factor 0.25 → 0 lots — will never place; increase capital or deselect").

### 7.2 Order book: "Account" awareness

- `account_orders` API + a small **Mirror Orders** card on `/orderbook` (or a
  tab): parent order → children status chips (AB1234 ✓, CD5678 skipped:no
  session). Simpler than merging child broker orderbooks into the main table.
- EOD Telegram summary (existing per-strategy summaries) gains one line per
  child account: `AB1234: 4 mirrored, 1 skipped (no session), P&L see broker`.

Sample screens: [`docs/design/multi_account_screens.html`](multi_account_screens.html).

---

## 8. Safety rails

1. **Default deny everywhere:** new account disabled; no strategies selected;
   fan-out only on LIVE + accepted parent + explicit strategy row.
2. **Analyze mode ON** → parent routes sandbox → no fan-out. The platform kill
   switch already covers children.
3. **Per-account pause** = the `is_enabled` toggle (instant, no restart —
   consulted per fan-out).
4. Child failures are journaled + Telegrammed, never raised, never block.
5. Child API secrets Fernet-encrypted with the existing key derivation (same
   `API_KEY_PEPPER` rules apply — the no-rotation warning in CLAUDE.md now
   covers child secrets too).
6. `strategy_mode` flips (`flip_mode` preflight) are unchanged — a strategy
   going live goes live for primary and mirrors alike; there is no separate
   per-child live flag beyond enable + strategy selection.
7. **Feature flag** `MULTI_ACCOUNT_ENABLED` (default `false`): the fan-out call
   is a no-op and the Accounts page shows a disabled banner until the operator
   opts in. One-line rollback.

---

## 9. Phased delivery (each phase = one issue + PR, per repo lifecycle)

| Phase | Scope | Files (new/changed) | Est. |
|---|---|---|---|
| **1. Accounts core** | `broker_accounts` + `account_strategies` tables, accounts service, per-account Zerodha login + callback routing, TOTP re-key, `/accounts` page (cards, add/edit, connect, strategy checklist) | new: `database/broker_accounts_db.py`, `services/broker_accounts_service.py`, `blueprints/broker_accounts.py`, `pages/accounts/`; changed: `brlogin.py`, `broker_totp_db.py`, `react_app.py`, `App.tsx` | ~2–3 days |
| **2. Fan-out** | `account_fanout_service`, seam in `place_order_with_auth`, sizing + exit-position lookup, `account_orders` journal, Telegram alerts, feature flag | new: `services/account_fanout_service.py`, `database/account_orders_db.py`; changed: `place_order_service.py` (~10 lines) | ~2 days |
| **3. Observability** | Mirror Orders card on `/orderbook`, mirror stats on `/accounts`, EOD summary lines, 9:00 login reminder | changed: orderbook page, `notification_service` callers | ~1 day |
| **4. Hardening** | E2E test with two mock broker sessions (extend the Tier-3 mock broker), partial-rejection drills, docs (`SYSTEM_MAP.md`, `CLAUDE.md`, `PARAMETER_LOG.md` — same-commit rule) | test + docs | ~1–2 days |

Validation (operator rule): Phase 1 walkthrough from navbar → /accounts → add
account → connect → green LED, screenshots on the issue. Phase 2 validated in a
controlled live session with a tiny-capital child (factor ≈ 0.05) mirroring one
strategy for one day before enabling real capital.

---

## 10. Decisions needed from the operator before Phase 1

1. **Broker scope:** Zerodha-only for v1? (Plan assumes yes; the design is
   broker-generic but only the Zerodha login path gets wired.)
2. **`PRIMARY_BOOK_CAPITAL` value** (suggest ₹10,00,000 to match the current
   consolidated book).
3. Should **manual orders** (placed from the UI / external API) ever fan out?
   (Plan says no — strategies only. A "mirror manual orders" per-account
   checkbox is easy to add later.)
4. Confirm each child account has (or will get) its own Kite Connect app and
   static-IP whitelisting.
