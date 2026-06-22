# Runbook — `API_KEY_PEPPER` / `FERNET_SALT` rotation recovery

This runbook covers the case where `API_KEY_PEPPER` or `FERNET_SALT` in
`.env` has been changed on a running install, and password login (and/or the
broker session) no longer works.

> **Background:** see the "NEVER rotate `API_KEY_PEPPER` or `FERNET_SALT`
> on a running install" section in [`CLAUDE.md`](../../CLAUDE.md). This
> runbook is for when the rule has already been broken.

## How to recognise the failure

The symptom is **login fails with no informative error**.

`log/openalgo_YYYY-MM-DD.log` will show:

```
[LOGIN] POST from IP=…
[LOGIN] Session state: user=None, logged_in=None, broker=None
```

…and then nothing else for the same request. No "Password auth success",
no "Invalid credentials" exception, no stack trace. The route returned 401
and the user sees "Invalid credentials" in the UI.

Additional signals that point at a rotation specifically (rather than a
wrong password):

- `db/openalgo.db` has exactly one user row, untouched, whose
  `password_hash` starts with `$argon2id$…`.
- The most recent `.env` modification time is **after** the last successful
  `[LOGIN] Password auth success` line in any log.
- After restart, the boot log shows Fernet decrypt failures when the
  broker token is loaded — the Zerodha adapter reports the saved token as
  invalid even though no broker re-login happened.
- The user is sure the password is right (and can show that the saved value
  in their browser's password manager is unchanged).

## Recovery — preferred path: restore the original values from a backup

The original `API_KEY_PEPPER` / `FERNET_SALT` may still exist on disk:

1. **Sibling worktrees.** This project frequently uses `git worktree` for
   parallel work (e.g. `openalgo-A/`, `openalgo-preflight/`,
   `openalgo-tier1/`). Each worktree has its own `.env`. Check them:
   ```bash
   grep -E "^(API_KEY_PEPPER|FERNET_SALT)" \
     /c/workspace/ai-trade-agent/openalgo-*/.env
   ```
   If the values match across worktrees but differ from the live `.env`,
   the worktree value is almost certainly the original.

2. **`.env.bak*` files.** This repo's `.gitignore` ignores `.env.bak*`,
   and the recovery convention is to `cp .env .env.bak.<timestamp>`
   before any `.env` edit. Look in the project root.

3. **OneDrive / paired devices.** The user may have a checked-in `.env`
   on another machine; if so, use the `API_KEY_PEPPER` / `FERNET_SALT`
   from there.

Once you have the original values:

```bash
cp .env .env.bak.before-restore.$(date -u +%FT%H%M%SZ)
# Edit .env — replace API_KEY_PEPPER (and FERNET_SALT if rotated) with the
# original values from the backup.
```

Then restart OpenAlgo. Verify two things:

- **Login works** — `[LOGIN] Password auth success for: <username>` appears
  in the day's log after the next attempt.
- **The Zerodha broker token decrypts** — the boot log shows
  `✅ Zerodha adapter initialized for user <username>` rather than a
  Fernet decrypt error. (The daily Zerodha session may still need a
  re-login, but that's a separate condition; the *token* should decrypt.)

If both pass, the recovery is complete.

## Recovery — fallback path: no backup exists

If neither `API_KEY_PEPPER` nor `FERNET_SALT` can be recovered, the install
is in a destructive-reset state. Every encrypted column in `db/openalgo.db`
is unreadable; the only honest options are:

### Option A — Reset the user password against the new pepper

The user can log in again, but every encrypted secret is gone.

```bash
uv run python -c "
from database.user_db import db_session, User
u = db_session.query(User).first()
print('Resetting password for:', u.username)
u.set_password('CHOOSE-A-NEW-PASSWORD-AND-CHANGE-AFTER-LOGIN')
db_session.commit()
print('Done')
"
```

After restart, log in with the new password and immediately use the
in-app change-password flow to set the real password.

Then you MUST re-enter every previously stored secret because the
ciphertexts in `db/openalgo.db` cannot be decrypted:

- Re-link every broker (Zerodha, Dhan, Angel, …) via `/broker`.
- Re-issue every API key via `/apikey` (any client using the old key —
  TradingView, Amibroker, ChartInk, Python scripts — must be updated).
- Re-enroll TOTP if used.
- Re-enter the SMTP password.

### Option B — Full re-setup

Stop OpenAlgo, move `db/openalgo.db` aside, restart, and walk through the
first-run setup again. This is the cleanest state but loses every order
log row and configured strategy artefact stored in the main DB. Usually
Option A is preferable.

## Aftercare — log the incident

The whole point of the original rule is that this should not happen. If
it has:

- Add a one-line entry to [`docs/PARAMETER_LOG.md`](../PARAMETER_LOG.md)
  recording the date, what changed, why, and the recovery path used.
- If this happened during an agent-driven session, save a memory entry
  so future-you / future-Claude does not repeat the move.
