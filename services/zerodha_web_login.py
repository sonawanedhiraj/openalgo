"""Headless Zerodha (Kite) web login → ``request_token`` via a real browser.

Given the operator's Kite ``user_id`` + ``password`` + external-TOTP secret and
the app's ``api_key``, this drives Kite's login pages with **Playwright**
(Chromium) and returns the short-lived ``request_token`` the standard
``/session/token`` checksum exchange then turns into an ``access_token`` (via the
existing ``broker.zerodha.api.auth_api.authenticate_broker`` for the primary, or
``broker_accounts_service._exchange_token`` for a child).

**Why a browser and not direct HTTP (issue #654).** The obvious approach — POST
``/api/login`` then ``/api/twofa`` — was implemented first and does NOT work
against current Kite: ``/api/login`` succeeds and returns a ``request_id``, but
``/api/twofa`` rejects a *provably correct* TOTP with
``TwoFAException: "login session has expired or doesn't exist"``. It fails
identically from ``httpx`` and ``requests``, with browser headers, and with the
connect-flow ``sess_id`` — the request reaches Kite's app server (clean JSON
error, not a Cloudflare block), so Kite itself requires browser-only context
(a JS/anti-bot challenge or a page token) at the 2FA step that a headless HTTP
client cannot reproduce. The same code works in a real browser. So we drive one.

Flow (one Chromium context):

    1. goto  https://kite.zerodha.com/connect/login?api_key=..&v=3
    2. fill  #userid (text)  + #password  -> click Login
    3. fill  #userid (type=number, "External TOTP") with the pyotp code -> Continue
    4. Kite 302s the browser to the app's registered redirect URL carrying
       ``?request_token=..`` — captured from that navigation REQUEST (the app
       bounces the tokenless browser on to /login, so the final URL loses it).

Eventlet-safety: Playwright's sync API drives a Node driver subprocess and uses
greenlets internally, which clashes with eventlet's monkey-patched hub. So the
whole browser session runs on a **real OS thread** (``eventlet.patcher.original``)
and the caller blocks on ``join`` — the same pattern as
``telegram_bot_service._render_plotly_png`` and ``llm_review_client``.

This module is Flask-context-free: it takes credentials in and returns
``(request_token, error)`` — it never touches the session, DB, or env. The
orchestration + storage live in ``services/broker_auto_login_service``.
"""

import os
import sys
import urllib.parse

from utils.logging import get_logger

logger = get_logger(__name__)

# Real OS thread even under eventlet — Playwright sync + eventlet's hub conflict.
if "eventlet" in sys.modules:
    import eventlet

    original_threading = eventlet.patcher.original("threading")
else:
    import threading as original_threading

KITE_CONNECT_LOGIN = "https://kite.zerodha.com/connect/login"
_DEFAULT_TIMEOUT_MS = 30000
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _headless() -> bool:
    return os.getenv("ZERODHA_LOGIN_HEADLESS", "true").lower() == "true"


def _timeout_ms() -> int:
    try:
        return max(10000, int(os.getenv("ZERODHA_LOGIN_TIMEOUT_MS", str(_DEFAULT_TIMEOUT_MS))))
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_MS


def _extract_request_token(url: str) -> str | None:
    """Return the ``request_token`` query param from a URL, or None. Pure."""
    if not url or "request_token=" not in url:
        return None
    try:
        tokens = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("request_token")
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
) -> tuple[str | None, str | None]:
    """Log in to Kite via a browser and return ``(request_token, error)``.

    Never raises: every failure path returns ``(None, "<reason>")``. Runs the
    browser session on a real OS thread and blocks up to the configured timeout.
    """
    if not all([user_id, password, totp_secret, api_key]):
        return None, "Missing one of user_id / password / totp_secret / api_key."

    result: dict[str, tuple[str | None, str | None]] = {}

    def _worker() -> None:
        # _run_browser_login shouldn't raise, but never let the worker thread die dirty.
        try:
            result["value"] = _run_browser_login(user_id, password, totp_secret, api_key)
        except Exception as e:
            logger.exception("Kite browser login worker crashed")
            result["value"] = (None, f"Kite browser login crashed: {e}")

    timeout_s = _timeout_ms() / 1000.0
    t = original_threading.Thread(target=_worker, daemon=True, name="openalgo-kite-login")
    t.start()
    t.join(timeout=timeout_s + 10.0)
    if t.is_alive():
        return None, f"Kite browser login timed out after {timeout_s:.0f}s."
    return result.get("value", (None, "Kite browser login produced no result."))


def _run_browser_login(
    user_id: str, password: str, totp_secret: str, api_key: str
) -> tuple[str | None, str | None]:
    """Drive Chromium through the Kite login. Runs on a real OS thread; never raises."""
    try:
        import pyotp
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except Exception as e:  # missing dependency / browser
        return None, f"Playwright unavailable ({e}). Run 'uv run playwright install chromium'."

    url = f"{KITE_CONNECT_LOGIN}?api_key={urllib.parse.quote(api_key)}&v=3"
    timeout = _timeout_ms()
    captured: dict[str, str] = {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=_headless())
            try:
                context = browser.new_context(user_agent=_USER_AGENT)
                page = context.new_page()
                page.set_default_timeout(timeout)

                # Capture request_token from the redirect navigation request — the
                # app bounces the tokenless browser to /login, losing it from the URL.
                def _on_request(req) -> None:
                    if "request_token=" in req.url and "token" not in captured:
                        tok = _extract_request_token(req.url)
                        if tok:
                            captured["token"] = tok

                page.on("request", _on_request)

                page.goto(url, wait_until="domcontentloaded")

                # Step 1: credentials.
                page.fill("input#userid", user_id)
                page.fill("input#password", password)
                page.click("button[type=submit]")

                # Step 2: the External-TOTP field is #userid re-rendered as type=number.
                page.wait_for_selector("input#userid[type='number']", timeout=timeout)
                code = pyotp.TOTP(totp_secret).now()
                page.fill("input#userid[type='number']", code)
                try:
                    page.click("button[type=submit]")
                except Exception:
                    pass  # some builds auto-submit on the 6th digit

                # Step 3: wait for the redirect request carrying the token.
                deadline = timeout
                waited = 0
                while waited < deadline and "token" not in captured:
                    page.wait_for_timeout(300)
                    waited += 300

                if captured.get("token"):
                    return captured["token"], None
                return None, _diagnose_failure(page)
            finally:
                browser.close()
    except PWTimeout as e:
        return None, f"Kite login timed out: {e}"
    except Exception as e:
        return None, f"Kite browser login failed: {e}"


def _diagnose_failure(page) -> str:
    """Best-effort human reason when no request_token was captured."""
    try:
        # Kite renders login/2FA errors in a small notification element.
        for sel in (".notice.error", ".error-message", "p.message", ".login-error"):
            try:
                if page.is_visible(sel):
                    txt = (page.inner_text(sel) or "").strip()
                    if txt:
                        return f"Kite login error: {txt[:200]}"
            except Exception:  # nosec B112 — best-effort diagnostics; try the next selector
                continue
        return (
            "No request_token captured (wrong password/TOTP, a changed Kite login "
            f"page, or anti-bot). Current URL: {page.url[:120]}"
        )
    except Exception:
        return "No request_token captured and the page could not be inspected."
