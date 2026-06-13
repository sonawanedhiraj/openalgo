"""
Running-app smoke. Requires OpenAlgo on 127.0.0.1:5000 and a live broker
session. Manual use after a restart — NOT for CI (CI has no running app).

Hits:
  GET  /preflight                                   → expect 200, ok=True
  GET  /chartink/simplified-engine/api/status       → expect 200 OR 302 (login)
  POST /api/v1/quotes for RELIANCE                   → expect 200 with ltp present
"""

import os
import sys

import requests

# Ensure the repo root is importable when run as `python scripts/...` so the
# `import app` / database helpers in the quote check resolve.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force UTF-8 stdout so the ✓/✗ markers render on Windows (cp1252) consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

BASE = "http://127.0.0.1:5000"
CHECKS = []


def check(name, fn):
    try:
        fn()
        CHECKS.append((True, name, ""))
        print(f"  ✓ {name}")
    except Exception as e:  # noqa: BLE001 — smoke harness reports every failure
        CHECKS.append((False, name, str(e)))
        print(f"  ✗ {name}: {e}")


def main():
    print("=== smoke_engine_live ===")
    print(f"BASE: {BASE}")

    def check_preflight():
        r = requests.get(f"{BASE}/preflight", timeout=10)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        body = r.json()
        if not body.get("ok"):
            failed = [k for k, v in (body.get("checks") or {}).items() if not (v or {}).get("ok")]
            raise RuntimeError(
                f"preflight not ok: failed={failed}, decision={body.get('go_decision')}"
            )

    check("preflight passes", check_preflight)

    def check_engine_status():
        r = requests.get(
            f"{BASE}/chartink/simplified-engine/api/status",
            timeout=10,
            allow_redirects=False,
        )
        if r.status_code not in (200, 302):
            raise RuntimeError(f"HTTP {r.status_code}")

    check("engine status endpoint reachable", check_engine_status)

    def check_quotes():
        # Resolve an API key from the DB so we don't hardcode it. This needs a
        # Flask app context because it uses the SQLAlchemy ORM.
        import app as app_module
        from database.auth_db import get_first_available_api_key

        with app_module.app.app_context():
            api_key = get_first_available_api_key()
        if not api_key:
            raise RuntimeError("no active api key found in DB")
        r = requests.post(
            f"{BASE}/api/v1/quotes",
            json={"apikey": api_key, "symbol": "RELIANCE", "exchange": "NSE"},
            timeout=10,
        )
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:100]}")
        body = r.json()
        data = body.get("data") or body
        ltp = data.get("ltp") or data.get("price") or data.get("last_price")
        if ltp is None:
            raise RuntimeError(f"no ltp in response: {str(body)[:100]}")

    check("RELIANCE quote returns 200 + ltp", check_quotes)

    passed = sum(1 for ok, _, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    print(f"\n=== {passed} passed, {failed} failed ===")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
