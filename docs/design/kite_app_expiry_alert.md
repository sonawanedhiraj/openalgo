# Kite Connect app-expiry countdown + alert — plan

**Status:** proposal (2026-09-18). Not yet an issue. Nothing shipped.

**Problem.** The Kite Connect app (`dhirubhai_trading`, "Kite connect + Historical
Chart data") is a paid monthly subscription. When it lapses the app goes inactive
and every login — manual, boot auto-login, watcher re-login — fails with
`InputException: Invalid api_key`, which reads exactly like a typo'd key. Today
the only place the expiry date exists is https://developers.kite.trade/apps
(today: 11 days, 29 Sep 2026). The operator should see the countdown inside
OpenAlgo and get a Telegram warning 3 days out without opening the portal.

## 1. What Zerodha exposes — verified 2026-09-18

| Source | Result |
| --- | --- |
| Kite Connect `GET /user/profile`, `POST /session/token` | Return user/session fields only (`user_id`, `exchanges`, `products`, `access_token`, `login_time`…). **No app name, no expiry, no subscription field.** |
| Forum: "Api to get kiteconnect app Expiry date" (Sujith, Zerodha, Mar 2021) | *"We don't have an API to check the app's expiry date. You may go to the billing section and add the Zerodha client id for a recurring subscription."* |
| Forum: "API Key Expiry Details – Kite API" (Salim_chisty, Zerodha, Apr 2025) | *"At present, we do not offer an API endpoint to retrieve app-specific expiry or renewal dates."* Directs users to the renewal emails. |
| `developers.kite.trade` | Separate **developer-account email + password** login (not Kite SSO, not the trading `user_id`/TOTP). The `/apps` page is server-rendered HTML behind that login; no public/JSON endpoint. |
| Expiry behaviour | App becomes inactive; login returns `Invalid api_key`. Renewal = buy credits → app page → Subscribe. Auto-renewal exists: Billing → link Zerodha client ID; renews at ~18:00 IST on expiry day if credits/funds exist. |

**Conclusion: there is no API.** The date has to come from (a) the operator
typing it once per renewal, or (b) a headless read of the developer portal.
Build (a) first as the source of truth; (b) is an optional refresher on top.

## 2. Phase 0 — operator action, no code (do this today)

Enable auto-renewal on https://developers.kite.trade (Billing → link Zerodha
client ID, keep credits). This removes the *outage* risk; the countdown below
still matters because auto-renew can fail (insufficient funds — Zerodha's
Dec-2024 incident) and the historical-data add-on has its own cycle.

## 3. Phase 1 — manual date, countdown + 3-day Telegram warning (ship first)

### Data
New table `broker_app_subscription` in `db/openalgo.db`
(`database/broker_app_subscription_db.py`, boot `db_init_functions` list):

```
scope         TEXT PK   -- 'primary' | 'acct:<id>'  (each child has its OWN Kite app + expiry)
broker        TEXT      -- 'zerodha'
app_name      TEXT      -- 'dhirubhai_trading'
api_key_hint  TEXT      -- last 4 chars of BROKER_API_KEY / child api_key, to prove the row matches the app
expires_on    DATE      -- the portal date (IST calendar day)
renewal_days  INTEGER   -- default 30, used only by the "I renewed" button
addons_json   TEXT      -- optional: {"historical": "YYYY-MM-DD"} if the add-on cycle differs
source        TEXT      -- 'manual' (Phase 1) | 'portal' (Phase 2)
updated_at, updated_by
```

### Pure logic (`services/broker_app_expiry_service.py`, no I/O)
- `days_remaining(expires_on, today_ist)` → int (negative = expired).
- `level(days)` → `ok` (> warn) / `warn` (≤ `BROKER_APP_EXPIRY_WARN_DAYS`, default 3)
  / `critical` (≤ 1) / `expired` (< 0). Expiry happens ~18:00 IST on the date,
  so `expires_on` itself counts as the last usable trading day.
- `summary()` → one dict per scope for the UI, `/status` and the alert text.

### Alerting
- Job `broker_app_expiry_check` on the shared scheduler, **08:30 IST daily**
  (before the 09:15 open; every day, not just trading days — the 18:00 expiry
  can land on a weekend). Add a `JobSpec` to `scheduler_registry.CATALOG`
  (`test_scheduler_registry.py` fails otherwise), tier `free`.
- `notify("broker_app_expiry", …)` — new event key in `notification_service`,
  toggle `NOTIFY_BROKER_APP_EXPIRY` (default true). One message per scope per
  day inside the window; text names the app, the date, days left and the exact
  renewal action. `expired` re-alerts every day until the date is bumped.
- Escalation: also alert on **boot** if any scope is `warn`+ (a restart is when
  the operator is looking), and enrich the existing auto-login watcher / manual
  login failure text: when the broker returns `Invalid api_key` and the recorded
  expiry is ≤ 0 days away, say *"Kite app subscription likely expired (recorded
  expiry YYYY-MM-DD) — renew at developers.kite.trade"* instead of the generic
  API error.
- Post-market: add `Expect(broker, app_subscription_runway, P1)` in
  `strategy_expectations.py` reading the digest (digest gains a
  `broker_app_subscription` section) so the 17:15 review lists it too.

### UI (walkthrough from the user's entry point — the issue's Validation section)
- **`/broker`** (`frontend/src/pages/BrokerSelect.tsx`, next to the existing
  `ZerodhaAutoLogin` card): card *"Kite Connect app · dhirubhai_trading ·
  expires 29 Sep 2026 · 11 days"*, colour by level, with a date input, an
  **"I renewed (+30 days)"** button and the `api_key_hint` shown so the operator
  can confirm it is the same app as the portal row.
- **Navbar badge** (the broker status chip): amber `App expires in 3d`, red
  `App expires today` / `App EXPIRED`. Hidden while `ok`.
- **`/accounts`**: one expiry cell per child (children each have their own app).
- **Telegram `/status`** (`telegram_inbound_service`): append one line per scope.
- API: `GET/PUT /api/broker-app-subscription` on `blueprints/broker_auto_login.py`
  (same `require_login` gate — NOT `check_session_validity`, per #462).

### Flags (→ `docs/PARAMETER_LOG.md`, direct to dev)
`BROKER_APP_EXPIRY_ALERT_ENABLED` (true), `BROKER_APP_EXPIRY_WARN_DAYS` (3),
`BROKER_APP_EXPIRY_ALERT_TIME` (08:30), `NOTIFY_BROKER_APP_EXPIRY` (true).

### Docs in the same commit
`docs/SYSTEM_MAP.md` (new table + job), `CLAUDE.md` Security section (one
paragraph next to the auto-login watcher), `PARAMETER_LOG.md`.

### Tests
`test/test_broker_app_expiry.py`: days/level boundaries (3, 1, 0, −1), one alert
per scope per day, no row → no alert but a once-per-week "not configured" nudge,
child scopes included, `Invalid api_key` enrichment only when expiry ≤ 0,
scheduler-registry + thread-registry catalog tests stay green.

## 4. Phase 2 (optional) — headless refresh from the developer portal

Only after Phase 1 is live. Reuses the Playwright pattern of
`services/zerodha_web_login.py` / `console_tradebook_fetch.py`:

- New encrypted credential row for the **developer-portal** email + password
  (`broker_login_credentials`, `broker='kite_developer'`). This is a different
  account from Kite trading — it does **not** hit Kite's one-web-session rule,
  so the fetch does not kill the API token. Still schedule it post-close
  (~16:00 IST) and re-probe the token afterwards as the console fetch does.
- Flow: login → `GET /apps` → parse each app row (name, api_key, "N days",
  "DD Mon YYYY") → upsert `broker_app_subscription` with `source='portal'` for
  the scope whose `api_key_hint` matches. Fail-open: any failure leaves the
  manual row untouched and logs a WARNING; the Phase-1 alert path never
  depends on it.
- Unknowns to confirm empirically once (not from memory): whether the portal
  login has 2FA/captcha, and the exact DOM of the apps table. If either blocks
  a headless run, Phase 2 is dropped and Phase 1 stands alone.
- CLI first (`uv run python -m services.kite_developer_portal_fetch --dry-run`),
  wire into the scheduler only after a week of clean dry runs.

## 5. Sequence

1. Phase 0 today (auto-renew) — and record `2026-09-29` in the table the moment
   Phase 1 lands so the first warning fires 2026-09-26 08:30 IST.
2. Open the issue (`type:enhancement`, `area:broker`, `P1`) with the §3
   walkthrough as its Validation section; branch `feat/<N>-kite-app-expiry`.
3. Ship Phase 1 (~1 day). Parameter-log + registry lines direct to dev.
4. Decide Phase 2 after the first live renewal cycle.

## Sources

- https://kite.trade/forum/discussion/9465/api-to-get-kiteconnect-app-expiry-date
- https://kite.trade/forum/discussion/16000/api-key-expiry-details-kite-api
- https://kite.trade/forum/discussion/14626/important-update-regarding-your-kite-connect-subscription
- https://kite.trade/forum/discussion/10474/how-to-renew-expired-subscription-of-kite-connect
- https://kite.trade/docs/connect/v3/user/
