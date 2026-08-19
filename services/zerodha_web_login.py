"""Headless Zerodha (Kite) web login → ``request_token``.

Given the operator's Kite ``user_id`` + ``password`` + external-TOTP secret and
the app's ``api_key``, this drives Kite's **web** login endpoints directly (no
browser, no Selenium) and returns the short-lived ``request_token`` the standard
``/session/token`` checksum exchange then turns into an ``access_token`` (via the
existing ``broker.zerodha.api.auth_api.authenticate_broker`` for the primary, or
``broker_accounts_service._exchange_token`` for a child).

Flow (three calls on one cookie jar, then the caller exchanges the token):

    1. POST https://kite.zerodha.com/api/login   {user_id, password}       -> request_id
    2. POST https://kite.zerodha.com/api/twofa    {user_id, request_id,
                                                   twofa_value=TOTP, twofa_type=totp}
    3. GET  https://kite.zerodha.com/connect/login?api_key=..&v=3  (no auto-redirect)
       -> follow the 302 chain, capture ``request_token`` from a Location header

⚠ **Undocumented endpoints.** ``/api/login`` and ``/api/twofa`` are Kite's own
web-login endpoints, not part of the published Kite Connect REST API. They are
the community-standard autologin path but Kite may change them without notice,
and the exchange mandates a manual login at least once a day (Kite deems
automation "not recommended"). Every failure here must surface loudly to the
caller and fall back to the manual flow — never silently. See issue #654 and
``docs/design/multi_account_plan.md`` §5.4.

This module is **pure and Flask-context-free**: it takes credentials in and
returns ``(request_token, error)`` — it never touches the session, the DB, or
env. The orchestration + storage live in ``services/broker_auto_login_service``.
"""

import urllib.parse

import httpx
import pyotp

from utils.logging import get_logger

logger = get_logger(__name__)

KITE_WEB_BASE = "https://kite.zerodha.com"
LOGIN_URL = f"{KITE_WEB_BASE}/api/login"
TWOFA_URL = f"{KITE_WEB_BASE}/api/twofa"
CONNECT_LOGIN_URL = f"{KITE_WEB_BASE}/connect/login"

# Hops to follow when capturing request_token from the connect/login redirect
# chain (login -> optional finish/consent -> redirect_url). A small fixed cap
# stops a misbehaving redirect loop from spinning.
_MAX_REDIRECT_HOPS = 10
_TIMEOUT_S = 15.0


def _extract_request_token(location: str) -> str | None:
    """Return the ``request_token`` query param from a redirect Location, or None."""
    if not location or "request_token" not in location:
        return None
    try:
        parsed = urllib.parse.urlparse(location)
        tokens = urllib.parse.parse_qs(parsed.query).get("request_token")
        if tokens and tokens[0]:
            return tokens[0]
    except Exception:
        return None
    return None


def fetch_request_token(
    user_id: str,
    password: str,
    totp_secret: str,
    api_key: str,
    *,
    client: httpx.Client | None = None,
) -> tuple[str | None, str | None]:
    """Log in to Kite web and return ``(request_token, error)``.

    Never raises: every failure path returns ``(None, "<reason>")``. ``client`` is
    injectable for tests — when omitted a dedicated cookie-jar client is created
    and closed here (kept separate from the shared pooled API client so login
    cookies never leak into broker calls).

    Args:
        user_id: Kite user id (e.g. ``AB1234``).
        password: Kite login password.
        totp_secret: base32 external-2FA TOTP secret (a live 6-digit code is
            derived here with ``pyotp``).
        api_key: the Kite Connect app api_key whose registered redirect URL the
            connect flow will bounce the ``request_token`` back through.
        client: optional pre-built ``httpx.Client`` (tests inject a fake).
    """
    if not all([user_id, password, totp_secret, api_key]):
        return None, "Missing one of user_id / password / totp_secret / api_key."

    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            follow_redirects=False,
            timeout=_TIMEOUT_S,
            headers={"X-Kite-Version": "3"},
        )
    try:
        # 1. Password login -> request_id
        try:
            resp = client.post(LOGIN_URL, data={"user_id": user_id, "password": password})
            resp.raise_for_status()
            request_id = (resp.json().get("data") or {}).get("request_id")
        except Exception as e:
            return None, f"Kite /api/login failed: {_err(e)}"
        if not request_id:
            return None, "Kite /api/login returned no request_id (bad user_id/password?)."

        # 2. TOTP 2FA -> session cookie set on the client
        try:
            code = pyotp.TOTP(totp_secret).now()
        except Exception as e:
            return None, f"Could not derive TOTP code (bad secret?): {e}"
        try:
            resp = client.post(
                TWOFA_URL,
                data={
                    "user_id": user_id,
                    "request_id": request_id,
                    "twofa_value": code,
                    "twofa_type": "totp",
                },
            )
            resp.raise_for_status()
        except Exception as e:
            return None, f"Kite /api/twofa failed (wrong TOTP or expired request_id?): {_err(e)}"

        # 3. connect/login redirect chain -> request_token
        url = f"{CONNECT_LOGIN_URL}?api_key={urllib.parse.quote(api_key)}&v=3"
        for _ in range(_MAX_REDIRECT_HOPS):
            try:
                resp = client.get(url)
            except Exception as e:
                return None, f"Kite connect/login failed: {_err(e)}"

            location = resp.headers.get("location", "")
            token = _extract_request_token(location)
            if token:
                return token, None

            if resp.is_redirect and location:
                # Resolve relative Locations against the current URL and continue.
                url = urllib.parse.urljoin(url, location)
                continue

            # Non-redirect terminal response with no token — nothing more to follow.
            return None, (
                "connect/login did not yield a request_token "
                f"(HTTP {resp.status_code}); session may be unauthorized."
            )
        return None, "connect/login exceeded the redirect-hop limit without a request_token."
    finally:
        if owns_client:
            try:
                client.close()
            except Exception:
                pass


def _err(e: Exception) -> str:
    """Best-effort human-readable reason from an httpx error, preferring Kite's JSON message."""
    message = str(e)
    response = getattr(e, "response", None)
    if response is not None:
        try:
            body = response.json()
            message = body.get("message") or body.get("error_type") or message
        except Exception:
            pass
    return message
