import os
from datetime import datetime, timedelta
from functools import wraps

import pytz
from flask import redirect, session, url_for

from utils.logging import get_logger

logger = get_logger(__name__)


def is_session_expiry_disabled():
    """Check if session expiry is disabled (e.g., for crypto brokers with 24/7 markets).

    Note: Each OpenAlgo instance serves a single broker, so this env var is
    instance-scoped — it only affects the broker configured for this instance,
    not all brokers globally.  The install script sets it automatically when
    a crypto broker (e.g. deltaexchange) is selected.
    """
    return os.getenv("DISABLE_SESSION_EXPIRY", "false").lower() == "true"


def get_session_expiry_time():
    """Get session expiry time set to 3 AM IST next day"""
    # Skip expiry for crypto brokers (24/7 markets)
    if is_session_expiry_disabled():
        logger.debug("Session expiry disabled (crypto broker / 24/7 market)")
        return timedelta(days=365)

    now_utc = datetime.now(pytz.timezone("UTC"))
    now_ist = now_utc.astimezone(pytz.timezone("Asia/Kolkata"))

    # Get configured expiry time or default to 3 AM
    expiry_time = os.getenv("SESSION_EXPIRY_TIME", "03:00")
    hour, minute = map(int, expiry_time.split(":"))

    target_time_ist = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If current time is past target time, set expiry to next day
    if now_ist > target_time_ist:
        target_time_ist += timedelta(days=1)

    remaining_time = target_time_ist - now_ist
    logger.debug(f"Session expiry time set to: {target_time_ist}")
    return remaining_time


def set_session_login_time():
    """Set the session login time in IST"""
    now_utc = datetime.now(pytz.timezone("UTC"))
    now_ist = now_utc.astimezone(pytz.timezone("Asia/Kolkata"))
    session["login_time"] = now_ist.isoformat()
    logger.info(f"Session login time set to: {now_ist}")


def is_session_valid():
    """Check if the current session is valid"""
    if not session.get("logged_in"):
        logger.debug("Session invalid: 'logged_in' flag not set")
        return False

    # If no login time is set, consider session invalid
    if "login_time" not in session:
        logger.debug("Session invalid: 'login_time' not in session")
        return False

    # Skip expiry check for crypto brokers (24/7 markets)
    if is_session_expiry_disabled():
        logger.debug("Session expiry disabled (crypto broker / 24/7 market)")
        return True

    now_utc = datetime.now(pytz.timezone("UTC"))
    now_ist = now_utc.astimezone(pytz.timezone("Asia/Kolkata"))

    # Parse login time
    login_time = datetime.fromisoformat(session["login_time"])

    # Get configured expiry time
    expiry_time = os.getenv("SESSION_EXPIRY_TIME", "03:00")
    hour, minute = map(int, expiry_time.split(":"))

    # Get today's expiry time
    daily_expiry = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)

    # If current time is past expiry time and login was before expiry time
    if now_ist > daily_expiry and login_time < daily_expiry:
        logger.info(f"Session expired at {daily_expiry} IST")
        return False

    logger.debug(
        f"Session valid. Current time: {now_ist}, Login time: {login_time}, Daily expiry: {daily_expiry}"
    )
    return True


def _broker_token_safe_after():
    """IST clock time after which a freshly minted broker token survives the day.

    Kite flushes every access token between ~06:45 and 07:30 IST (staff on the
    Kite Connect forum; the docs say "6 AM"), so a token written before this
    time on a given morning is expected to die. Shared with the auto-login
    watcher via ``AUTO_LOGIN_EARLIEST_TIME`` (default ``07:30``).
    """
    raw = os.getenv("AUTO_LOGIN_EARLIEST_TIME", "07:30")
    try:
        hour, minute = (int(x) for x in raw.split(":", 1))
        return hour, minute
    except (TypeError, ValueError):
        return 7, 30


def classify_stored_token(username, now_ist=None):
    """Say whether the stored broker token for ``username`` may outlive the cookie.

    The session-expiry path (issue #719) used to revoke the DB token whenever the
    browser cookie was older than ``SESSION_EXPIRY_TIME`` — which was only ever
    true while the operator's browser was the sole token writer. The headless
    auto-login writes tokens with no browser, so the cookie's age says nothing
    about the token's age. Verdicts, keyed on ``auth.token_updated_at``:

    - ``"stale"``   — no stamp (legacy row) or written before the expiry boundary
                     of the morning this cookie expired on → legacy revoke.
    - ``"trusted"`` — written at/after that morning's ``AUTO_LOGIN_EARLIEST_TIME``
                     (post-flush) → keep, no broker call needed.
    - ``"probe"``   — written after the boundary but inside the flush window
                     (an operator-forced early login) → only the broker knows.

    Pure given ``now_ist``; never raises (a read failure is ``"stale"``, i.e.
    the pre-#719 behaviour).
    """
    try:
        from database.auth_db import get_token_updated_at

        stamp = get_token_updated_at(username)
    except Exception:
        logger.exception(f"classify_stored_token: stamp read failed for {username}")
        return "stale"
    if stamp is None:
        return "stale"

    ist = pytz.timezone("Asia/Kolkata")
    if stamp.tzinfo is None:
        stamp = pytz.utc.localize(stamp)  # repo contract: naive timestamps are UTC
    stamp_ist = stamp.astimezone(ist)
    if now_ist is None:
        now_ist = datetime.now(pytz.utc).astimezone(ist)

    expiry_time = os.getenv("SESSION_EXPIRY_TIME", "03:00")
    hour, minute = map(int, expiry_time.split(":"))
    boundary = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_ist < boundary:
        boundary -= timedelta(days=1)
    if stamp_ist < boundary:
        return "stale"

    safe_hour, safe_minute = _broker_token_safe_after()
    safe_after = boundary.replace(hour=safe_hour, minute=safe_minute)
    if stamp_ist >= safe_after:
        return "trusted"
    return "probe"


def _stored_token_is_fresh(username):
    """True when the stored broker token must be PRESERVED on cookie expiry.

    ``trusted`` → True without a broker call. ``probe`` → ask the broker; a dead
    token is invalidated here (``invalidate_auth`` does the cache/ZMQ/pool
    cleanup) so background subsystems stop retrying it, and the auto-login
    watcher re-logs in. ``stale`` → False (legacy revoke). Never raises.
    """
    try:
        verdict = classify_stored_token(username)
        if verdict == "trusted":
            return True
        if verdict == "stale":
            return False
        from services.broker_session_health import is_live_session_for

        alive = is_live_session_for(username)
        if alive:
            logger.info(
                f"Session cookie expired for {username}; stored broker token was written "
                "inside the Kite flush window but the broker still accepts it — preserved"
            )
            return True
        logger.info(
            f"Session cookie expired for {username}; stored broker token was written inside "
            "the Kite flush window and the broker rejects it — invalidating"
        )
        from database.auth_db import invalidate_auth

        invalidate_auth(username)
        return False
    except Exception:
        logger.exception(f"_stored_token_is_fresh: verdict failed for {username}")
        return False


def revoke_user_tokens(revoke_db_tokens=True):
    """
    Revoke auth tokens for the current user when session expires.

    Also publishes cache invalidation events via ZeroMQ for multi-process deployments.
    This ensures WebSocket proxy and other processes clear their stale cached tokens.
    See GitHub issue #765 for details on the cross-process cache synchronization problem.

    A stored token that is NEWER than the expiry boundary (written by the
    headless auto-login after the operator's cookie went stale, issue #719) is
    preserved untouched — no DB revoke, no cache invalidation, no WS-proxy
    reconnect, no master-contract cache clear. Only the browser session is
    stale, and the caller clears that. Explicit ``/auth/logout`` does not come
    through here and stays destructive.

    Args:
        revoke_db_tokens (bool): If True, revokes the token in the database (Invalidates API Key).
                                 If False, only clears local caches (Preserves API Key).
    """
    if "user" in session:
        username = session.get("user")
        if _stored_token_is_fresh(username):
            logger.info(
                f"Session cookie expired for {username}; stored broker token is fresh "
                "(written after today's expiry boundary) — preserved, only the browser "
                "session is cleared (issue #719)"
            )
            return
        try:
            from database.auth_db import auth_cache, feed_token_cache, upsert_auth

            # Clear cache entries first to prevent stale data access
            cache_key_auth = f"auth-{username}"
            cache_key_feed = f"feed-{username}"
            if cache_key_auth in auth_cache:
                del auth_cache[cache_key_auth]
            if cache_key_feed in feed_token_cache:
                del feed_token_cache[cache_key_feed]

            # Publish cache invalidation event via ZeroMQ for other processes
            # This notifies WebSocket proxy and other processes to clear their stale caches
            try:
                from database.cache_invalidation import publish_all_cache_invalidation

                publish_all_cache_invalidation(username)
                logger.debug(f"Published cache invalidation for user: {username}")
            except Exception as invalidation_error:
                # Don't fail logout if cache invalidation fails
                logger.warning(
                    f"Failed to publish cache invalidation for user {username}: {invalidation_error}"
                )

            # Clear symbol cache on logout/session expiry
            try:
                from database.master_contract_cache_hook import clear_cache_on_logout

                clear_cache_on_logout()
            except Exception as cache_error:
                logger.exception(f"Error clearing symbol cache: {cache_error}")

            # Clear settings cache on logout/session expiry
            try:
                from database.settings_db import clear_settings_cache

                clear_settings_cache()
            except Exception as cache_error:
                logger.exception(f"Error clearing settings cache: {cache_error}")

            # Clear strategy cache on logout/session expiry
            try:
                from database.strategy_db import clear_strategy_cache

                clear_strategy_cache()
            except Exception as cache_error:
                logger.exception(f"Error clearing strategy cache: {cache_error}")

            # Clear telegram cache on logout/session expiry
            try:
                from database.telegram_db import clear_telegram_cache

                clear_telegram_cache()
            except Exception as cache_error:
                logger.exception(f"Error clearing telegram cache: {cache_error}")

            if revoke_db_tokens:
                # Revoke the auth token in database
                inserted_id = upsert_auth(username, "", "", revoke=True)
                if inserted_id is not None:
                    logger.info(f"Auto-expiry: Revoked auth tokens for user: {username}")
                else:
                    logger.error(f"Auto-expiry: Failed to revoke auth tokens for user: {username}")

                # Clear all active sessions for this user (tokens are invalid now)
                try:
                    from database.auth_db import clear_user_sessions

                    clear_user_sessions(username)
                    logger.info(f"Auto-expiry: Cleared active sessions for user: {username}")
                except Exception as session_error:
                    logger.warning(f"Error clearing active sessions: {session_error}")
            else:
                logger.info(
                    f"Auto-expiry: Skipped DB revocation for user: {username} (Preserving API access)"
                )

        except Exception as e:
            logger.exception(f"Error revoking tokens during auto-expiry for user {username}: {e}")


def check_session_validity(f):
    """Decorator to check session validity before executing route"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_session_valid():
            # Revoke tokens before clearing session
            revoke_user_tokens()
            session.clear()

            # Check if this is an AJAX/fetch request
            from flask import jsonify, request

            is_ajax = (
                request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or request.headers.get("Accept", "").startswith("application/json")
                or request.content_type == "application/json"
                or request.is_json
            )

            if is_ajax:
                # Return JSON response for AJAX requests instead of redirect
                # This prevents consuming rate limits on the login endpoint
                logger.info("Invalid session detected - returning 401 for AJAX request")
                return jsonify(
                    {
                        "status": "error",
                        "error": "session_expired",
                        "message": "Your session has expired. Please log in again.",
                    }
                ), 401

            logger.info("Invalid session detected - redirecting to login")
            return redirect(url_for("auth.login"))
        logger.debug("Session validated successfully")
        return f(*args, **kwargs)

    return decorated_function


def invalidate_session_if_invalid(f):
    """Decorator to invalidate session if invalid without redirecting"""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not is_session_valid():
            logger.info("Invalid session detected - clearing session")
            # Revoke tokens before clearing session
            revoke_user_tokens()
            session.clear()
        return f(*args, **kwargs)

    return decorated_function
