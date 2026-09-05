"""Headless fetch of a child's Zerodha Console tradebook (issue #702).

Kite Connect has no historical-trades endpoint; Console (console.zerodha.com)
is a separate web app behind the Kite web SSO, so the child's API token cannot
reach it. But every child already has an encrypted password + external-TOTP
secret stored for the headless auto-login (#654), and
``services/zerodha_web_login.py`` already drives Kite's real login pages with
Playwright. This module reuses that pattern to sign the CHILD into Kite web,
hop to Console, pull the tradebook rows for a date range from Console's own
report endpoint, and write them in the Console CSV shape that
``services/account_console_import.py`` already understands — so the fetched
rows go through the SAME importer and pairing as a hand-exported file.

Operator CLI (not scheduled)::

    uv run python -m services.console_tradebook_fetch --account <id> \
        --from 2026-08-26 --to 2026-09-04 [--segment FO] [--import [--apply]]

Load-bearing rules:

- **Each child logs in as itself** with its own stored credentials; the
  primary's credentials are never used.
- **Kite allows ONE web session per user** — a fresh web login can kill that
  child's live API token. The CLI therefore REFUSES inside market hours on a
  trading day unless ``--force``, and re-probes the child's API token
  afterwards so a killed session is reported, not discovered on Monday.
- **Fail loud, never partial.** A login/2FA failure, an unexpected Console
  response shape, or a page that cannot be read exits non-zero and writes NO
  file; a partial file would import as a partial day.
- Read-only on the broker: no orders, no settings.
- Playwright's sync API and eventlet's hub conflict, so the browser session
  runs on a real OS thread (same as ``zerodha_web_login``).
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

if "eventlet" in sys.modules:  # pragma: no cover - runtime shape
    import eventlet

    original_threading = eventlet.patcher.original("threading")
else:
    import threading as original_threading

KITE_WEB = "https://kite.zerodha.com/"
CONSOLE = "https://console.zerodha.com"
CONSOLE_TRADEBOOK_PAGE = f"{CONSOLE}/reports/tradebook"
CONSOLE_TRADEBOOK_API = f"{CONSOLE}/api/reports/tradebook"
_IST = timedelta(hours=5, minutes=30)
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# The Console CSV export header — the importer's contract (#700).
CSV_COLUMNS = (
    "symbol",
    "isin",
    "trade_date",
    "exchange",
    "segment",
    "series",
    "trade_type",
    "auction",
    "quantity",
    "price",
    "trade_id",
    "order_id",
    "order_execution_time",
)
_REQUIRED = ("order_id", "quantity", "price", "trade_type")
_MAX_PAGES = 200


def _headless() -> bool:
    return os.getenv("ZERODHA_LOGIN_HEADLESS", "true").lower() == "true"


def _timeout_ms() -> int:
    try:
        return max(15000, int(os.getenv("ZERODHA_LOGIN_TIMEOUT_MS", "45000")))
    except (TypeError, ValueError):
        return 45000


def in_market_hours_now() -> bool:
    """True inside 09:00–15:45 IST on a trading day (holiday-aware, fail-open
    to weekday)."""
    now = datetime.utcnow() + _IST
    try:
        from services.data_freshness_service import is_trading_day

        trading = is_trading_day(now.date())
    except Exception:
        trading = now.weekday() < 5
    if not trading:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 <= minutes <= 15 * 60 + 45


# --------------------------------------------------------------------------- #
# Row shaping (pure)
# --------------------------------------------------------------------------- #
def validate_rows(rows: list[dict]) -> str | None:
    """None when every row carries the importer's required fields, else the
    reason (with the keys actually seen, so a Console shape change is
    diagnosable from the error alone)."""
    for i, r in enumerate(rows):
        missing = [k for k in _REQUIRED if r.get(k) in (None, "")]
        if missing:
            return (
                f"row {i} lacks {missing}; keys seen: {sorted(r.keys())[:20]} — "
                "Console's tradebook shape may have changed"
            )
    return None


def to_csv_rows(rows: list[dict]) -> list[dict]:
    """Project Console rows onto the CSV export columns (extra keys dropped)."""
    out = []
    for r in rows:
        out.append({c: ("" if r.get(c) is None else r.get(c)) for c in CSV_COLUMNS})
    return out


def write_console_csv(rows: list[dict], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for r in to_csv_rows(rows):
            writer.writerow(r)
    return path


def default_output_path(display_name: str, segment: str, d_from: date, d_to: date) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in display_name)
    return Path("imports") / "console" / f"{safe}_tradebook_{segment}_{d_from}_{d_to}.csv"


# --------------------------------------------------------------------------- #
# Browser session (real OS thread)
# --------------------------------------------------------------------------- #
def fetch_console_tradebook(
    user_id: str,
    password: str,
    totp_secret: str,
    d_from: date,
    d_to: date,
    segment: str = "FO",
) -> tuple[list[dict] | None, str | None]:
    """``(rows, error)``. Never raises; blocks up to the login timeout ×3."""
    if not all([user_id, password, totp_secret]):
        return None, "Missing one of user_id / password / totp_secret for this account."
    result: dict[str, tuple[list[dict] | None, str | None]] = {}

    def _worker() -> None:
        try:
            result["value"] = _run_browser_fetch(
                user_id, password, totp_secret, d_from, d_to, segment
            )
        except Exception as e:
            logger.exception("Console tradebook fetch worker crashed")
            result["value"] = (None, f"Console fetch crashed: {e}")

    timeout_s = _timeout_ms() / 1000.0
    t = original_threading.Thread(target=_worker, daemon=True, name="openalgo-console-fetch")
    t.start()
    t.join(timeout=timeout_s * 3 + 10.0)
    if t.is_alive():
        return None, f"Console fetch timed out after {timeout_s * 3:.0f}s."
    return result.get("value", (None, "Console fetch produced no result."))


def _api_url(segment: str, d_from: date, d_to: date, page_no: int) -> str:
    q = urllib.parse.urlencode(
        {
            "segment": segment,
            "from_date": d_from.isoformat(),
            "to_date": d_to.isoformat(),
            "page": page_no,
            "sort_by": "order_execution_time",
            "sort_desc": "false",
        }
    )
    return f"{CONSOLE_TRADEBOOK_API}?{q}"


def _extract_result(payload) -> tuple[list[dict] | None, dict | None, str | None]:
    """Console's envelope: ``{"status": "success", "data": {"state": …,
    "result": [...], "pagination": {...}}}`` — tolerate a bare list too."""
    if isinstance(payload, list):
        return payload, None, None
    if not isinstance(payload, dict):
        return None, None, f"unexpected payload type {type(payload).__name__}"
    if payload.get("status") not in (None, "success"):
        return None, None, f"Console error: {payload.get('message') or payload.get('status')}"
    data = payload.get("data")
    if isinstance(data, list):
        return data, None, None
    if isinstance(data, dict):
        result = data.get("result")
        if isinstance(result, list):
            return (
                result,
                data.get("pagination") if isinstance(data.get("pagination"), dict) else None,
                None,
            )
        state = data.get("state")
        if state and str(state).lower() not in ("success", "done", "complete"):
            return None, None, f"Console report state {state!r} (not ready)"
    return None, None, f"no result list in payload; top-level keys {sorted(payload.keys())[:10]}"


def _run_browser_fetch(
    user_id: str, password: str, totp_secret: str, d_from: date, d_to: date, segment: str
) -> tuple[list[dict] | None, str | None]:
    try:
        import pyotp
        from playwright.sync_api import TimeoutError as PWTimeout
        from playwright.sync_api import sync_playwright
    except Exception as e:
        return None, f"Playwright unavailable ({e}). Run 'uv run playwright install chromium'."

    timeout = _timeout_ms()
    seen_console_urls: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=_headless())
            try:
                context = browser.new_context(user_agent=_USER_AGENT)
                page = context.new_page()
                page.set_default_timeout(timeout)
                page.on(
                    "response",
                    lambda resp: seen_console_urls.append(resp.url)
                    if "console.zerodha.com/api" in resp.url and len(seen_console_urls) < 50
                    else None,
                )

                # 1. Kite web login (same form as the connect flow, no api_key).
                page.goto(KITE_WEB, wait_until="domcontentloaded")
                page.fill("input#userid", user_id)
                page.fill("input#password", password)
                page.click("button[type=submit]")
                page.wait_for_selector("input#userid[type='number']", timeout=timeout)
                page.fill("input#userid[type='number']", pyotp.TOTP(totp_secret).now())
                try:
                    page.click("button[type=submit]")
                except Exception:
                    pass
                # Dashboard = logged in. Kite renders the 2FA error inline if not.
                try:
                    page.wait_for_url(
                        lambda u: "kite.zerodha.com" in u
                        and "/connect/login" not in u
                        and "/login" not in u,
                        timeout=timeout,
                    )
                except PWTimeout:
                    return None, _diagnose(page, "Kite web login did not reach the dashboard")

                # 2. Hop to Console — SSO via the live Kite session.
                page.goto(CONSOLE_TRADEBOOK_PAGE, wait_until="domcontentloaded")
                waited = 0
                while waited < timeout and "console.zerodha.com" not in page.url:
                    # Kite Connect authorize page for Zerodha's own app usually
                    # auto-continues; click a Continue/Authorize button if shown.
                    for sel in (
                        "button:has-text('Authorize')",
                        "button:has-text('Continue')",
                        "button[type=submit]",
                    ):
                        try:
                            if page.is_visible(sel):
                                page.click(sel)
                                break
                        except Exception:  # nosec B112 — best-effort; try the next selector
                            continue
                    page.wait_for_timeout(500)
                    waited += 500
                if "console.zerodha.com" not in page.url:
                    return None, _diagnose(page, "Console SSO did not complete")

                # 3. CSRF token = Console's public_token cookie.
                cookies = {c["name"]: c["value"] for c in context.cookies(CONSOLE)}
                csrf = cookies.get("public_token") or cookies.get("csrftoken") or ""
                headers = (
                    {"x-csrftoken": csrf, "accept": "application/json"}
                    if csrf
                    else {"accept": "application/json"}
                )

                rows: list[dict] = []
                empty_first_page_retries = 0
                page_no = 0
                while page_no < _MAX_PAGES:
                    page_no += 1
                    resp = page.request.get(
                        _api_url(segment, d_from, d_to, page_no), headers=headers
                    )
                    if resp.status != 200:
                        body = resp.text()[:200]
                        return None, (
                            f"Console tradebook API returned HTTP {resp.status} on page {page_no}: {body}"
                            + _seen(seen_console_urls)
                        )
                    try:
                        payload = resp.json()
                    except Exception:
                        return (
                            None,
                            f"Console tradebook API returned non-JSON on page {page_no}"
                            + _seen(seen_console_urls),
                        )
                    batch, pagination, err = _extract_result(payload)
                    if err:
                        return None, f"{err} (page {page_no})" + _seen(seen_console_urls)
                    if not batch and page_no == 1 and empty_first_page_retries < 8:
                        # Console builds the report asynchronously: the first
                        # answer can be an empty result while it is still
                        # generating (observed 2026-09-05 — 0 rows, then 33 on
                        # the next call). Give it a few seconds before
                        # believing "no trades".
                        empty_first_page_retries += 1
                        page_no = 0
                        page.wait_for_timeout(1500)
                        continue
                    rows.extend(batch)
                    if not batch:
                        break
                    if pagination:
                        total_pages = pagination.get("total_pages") or pagination.get("pages")
                        if total_pages is not None and page_no >= int(total_pages):
                            break
                    elif len(batch) < 20:
                        break
                bad = validate_rows(rows)
                if bad:
                    return None, bad
                if not rows:
                    # A genuinely quiet range is possible, but 0 rows is also
                    # exactly what a wrong endpoint/param name looks like — so
                    # say which Console endpoints the UI itself called.
                    logger.warning(
                        "Console tradebook returned 0 rows for %s..%s segment=%s%s",
                        d_from,
                        d_to,
                        segment,
                        _seen(seen_console_urls),
                    )
                return rows, None
            finally:
                browser.close()
    except PWTimeout as e:
        return None, f"Console fetch timed out: {e}"
    except Exception as e:
        return None, f"Console fetch failed: {e}"


def _seen(urls: list[str]) -> str:
    if not urls:
        return ""
    uniq = []
    for u in urls:
        base = u.split("?")[0]
        if base not in uniq:
            uniq.append(base)
    return " | Console API URLs observed: " + ", ".join(uniq[:8])


def _diagnose(page, prefix: str) -> str:
    try:
        for sel in (".notice.error", ".error-message", "p.message", ".login-error"):
            try:
                if page.is_visible(sel):
                    txt = (page.inner_text(sel) or "").strip()
                    if txt:
                        return f"{prefix}: {txt[:200]}"
            except Exception:  # nosec B112 — best-effort diagnostics
                continue
        return f"{prefix}. Current URL: {page.url[:120]}"
    except Exception:
        return prefix


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def fetch_for_account(
    account_id: int, d_from: date, d_to: date, segment: str = "FO", out: Path | None = None
) -> tuple[Path | None, str | None, dict]:
    """Fetch + write the CSV for one child. Returns ``(path, error, meta)``."""
    from database import broker_accounts_db

    account = broker_accounts_db.get_account(account_id)
    if not account:
        return None, f"no child account with id {account_id}", {}
    user_id = broker_accounts_db.get_user_id(account_id)
    password = broker_accounts_db.get_password(account_id)
    totp_secret = broker_accounts_db.get_totp_secret(account_id)
    if not (user_id and password and totp_secret):
        return (
            None,
            (
                f"{account['display_name']} has no stored Kite user-id / password / TOTP secret — "
                "add them on /accounts (the headless auto-login fields) first."
            ),
            {},
        )
    rows, err = fetch_console_tradebook(user_id, password, totp_secret, d_from, d_to, segment)
    if err or rows is None:
        return None, err or "no rows returned", {}
    path = out or default_output_path(account["display_name"], segment, d_from, d_to)
    write_console_csv(rows, path)
    logger.info(
        "Console tradebook fetched - %s: %s rows %s..%s -> %s",
        account["display_name"],
        len(rows),
        d_from,
        d_to,
        path,
    )
    return path, None, {"rows": len(rows), "account": account}


def probe_child_api_token(account_id: int) -> bool | None:
    """Did the child's API session survive the web login? ``None`` = no token."""
    try:
        from database import broker_accounts_db
        from database.auth_db import get_auth_token
        from services.broker_session_health import probe_token

        account = broker_accounts_db.get_account(account_id) or {}
        token = get_auth_token(broker_accounts_db.auth_name(account_id))
        if not token:
            return None
        return probe_token(account.get("broker") or "zerodha", token)
    except Exception:
        logger.exception("post-fetch token probe failed")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--account", type=int, required=True)
    parser.add_argument("--from", dest="d_from", required=True, help="YYYY-MM-DD")
    parser.add_argument("--to", dest="d_to", required=True, help="YYYY-MM-DD")
    parser.add_argument("--segment", default="FO", help="FO (default) | EQ")
    parser.add_argument("--out", default=None, help="CSV path (default imports/console/…)")
    parser.add_argument(
        "--import", dest="do_import", action="store_true", help="run the importer on the file"
    )
    parser.add_argument(
        "--apply", action="store_true", help="with --import: write (default dry-run)"
    )
    parser.add_argument("--force", action="store_true", help="allow inside market hours")
    args = parser.parse_args(argv)

    try:
        d_from, d_to = date.fromisoformat(args.d_from), date.fromisoformat(args.d_to)
    except ValueError:
        print("dates must be YYYY-MM-DD", file=sys.stderr)
        return 2
    if d_from > d_to:
        print("--from must be on or before --to", file=sys.stderr)
        return 2
    if in_market_hours_now() and not args.force:
        print(
            "Refusing inside market hours: a fresh Kite web login can kill this child's live API "
            "session (one session per user). Re-run after 15:45 IST or pass --force.",
            file=sys.stderr,
        )
        return 3

    path, err, meta = fetch_for_account(
        args.account, d_from, d_to, args.segment.upper(), Path(args.out) if args.out else None
    )
    if err:
        print(f"FETCH FAILED: {err}", file=sys.stderr)
        return 1
    print(f"fetched {meta['rows']} trade row(s) -> {path}")

    alive = probe_child_api_token(args.account)
    if alive is False:
        print(
            "WARNING: the child's API session no longer answers — the web login likely "
            "replaced it. Re-login on /accounts before the next trading day.",
            file=sys.stderr,
        )
    elif alive:
        print("child API session still alive after the web login.")

    if not args.do_import:
        return 0
    from services.account_console_import import main as import_main

    import_argv = ["--account", str(args.account), "--file", str(path)]
    if args.apply:
        import_argv.append("--apply")
    return import_main(import_argv)


if __name__ == "__main__":  # pragma: no cover - operator CLI
    sys.exit(main())
