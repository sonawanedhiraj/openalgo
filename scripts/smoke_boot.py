"""
Import-only smoke check. Runs in a few seconds with no running app.

Verifies the Flask app can be constructed, all major blueprints import
cleanly, key services and DB helpers load, the engine webhook route is
registered, the version helper resolves, and the strategy registry is
populated.

Exits non-zero on any failure with a clear one-line reason. Suitable for
the CI pre-merge gate — no HTTP, no DB writes, no broker session needed.
"""

import gc
import os
import sys
import traceback

# Ensure the repo root is importable when run as `python scripts/smoke_boot.py`
# (Python puts scripts/ on sys.path[0], not the repo root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Force UTF-8 stdout so the ✓/✗ markers render on Windows (cp1252) consoles too.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

CHECKS_PASSED = 0
CHECKS_FAILED = 0
FAILURES = []


def check(name, fn):
    global CHECKS_PASSED, CHECKS_FAILED
    try:
        fn()
        CHECKS_PASSED += 1
        print(f"  ✓ {name}")
    except Exception as e:  # noqa: BLE001 — smoke harness reports every failure
        CHECKS_FAILED += 1
        FAILURES.append((name, e, traceback.format_exc()))
        print(f"  ✗ {name}: {type(e).__name__}: {e}")


def main():
    print("=== smoke_boot ===")

    # 1. App module imports cleanly (this also constructs the Flask app via
    #    the module-level `app = create_app()` call).
    check("app module imports", lambda: __import__("app"))

    # 2. Major blueprints import.
    for blueprint_name in [
        "blueprints.chartink",
        "blueprints.preflight",
        "blueprints.journal",
        "blueprints.backtest",
        "blueprints.mode_status",
        "blueprints.telegram",
        "blueprints.auth",
    ]:
        check(
            f"blueprint imports: {blueprint_name}",
            lambda b=blueprint_name: __import__(b, fromlist=["*"]),
        )

    # 3. Key services import.
    for svc in [
        "services.simplified_stock_engine_service",
        "services.scanner_service",
        "services.signal_review_service",
        "services.notification_service",
        "services.eod_watchdog_service",
        "services.journal_reflection_service",
        "services.market_regime_service",
        "services.strategy_activator_service",
        "services.news_ingest_service",
        "services.trade_journal_service",
        "services.scan_cycle_service",
        "services.mode_service",
    ]:
        check(f"service imports: {svc}", lambda s=svc: __import__(s, fromlist=["*"]))

    # 4. Key DB modules import.
    for db_mod in [
        "database.daily_intent_db",
        "database.scan_cycle_db",
        "database.scanner_db",
        "database.signal_decision_db",
        "database.trade_journal_db",
        "database.backtest_db",
        "database.market_intel_db",
        "database.journal_reflection_db",
        "database.telegram_db",
        "database.auth_db",
    ]:
        check(f"db module imports: {db_mod}", lambda m=db_mod: __import__(m, fromlist=["*"]))

    # 5. Engine webhook route is registered. This is the important check — it
    #    catches an accidentally-broken blueprint registration.
    def check_webhook_registered():
        import app as app_module

        flask_app = getattr(app_module, "app", None) or getattr(app_module, "application", None)
        if flask_app is None:
            raise RuntimeError("could not obtain Flask app object")
        urls = [str(r) for r in flask_app.url_map.iter_rules()]
        expected = "/chartink/simplified-stock-engine"
        if not any(expected in u for u in urls):
            raise RuntimeError(f"webhook route {expected!r} not found in url_map")

    check("engine webhook route registered", check_webhook_registered)

    # 6. Version helper resolves.
    def check_version():
        from utils.version import get_version

        v = get_version()
        if not v:
            raise RuntimeError("get_version() returned empty")

    check("version helper", check_version)

    # 7. Strategy registry has at least one strategy.
    def check_strategies():
        from strategies import list_intraday_strategies

        items = list_intraday_strategies()
        if not items:
            raise RuntimeError("no intraday strategies registered")

    check("strategy registry populated", check_strategies)

    print()
    print(f"=== {CHECKS_PASSED} passed, {CHECKS_FAILED} failed ===")

    if CHECKS_FAILED > 0:
        print()
        print("=== FAILURE DETAILS ===")
        for name, _err, tb in FAILURES:
            print(f"\n--- {name} ---")
            print(tb)


def shutdown_background_schedulers():
    # Importing app.py eagerly starts ~10 BackgroundScheduler instances across
    # blueprints and services (python_strategy, chartink, strategy, eod_watchdog,
    # news_ingest, journal_reflection, sector_follow, futures_follow, ...). None
    # registers an atexit shutdown, so a clean return runs straight into the
    # interpreter teardown closing the ThreadPoolExecutor backing each scheduler
    # while the scheduler's daemon thread is still alive — the next interval
    # tick raises RuntimeError("cannot schedule new futures after interpreter
    # shutdown") and the dying scheduler threads trigger Fatal Python error:
    # PyEval_SaveThread on a finalized interpreter. On CI that held the gate job
    # open until the 30-minute timeout (#217). gc.get_objects() finds every live
    # instance without us tracking them by hand. Smoke-harness only — production
    # code paths unchanged.
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        return
    for obj in gc.get_objects():
        if isinstance(obj, BackgroundScheduler):
            try:
                if obj.running:
                    obj.shutdown(wait=False)
            except Exception:  # noqa: BLE001 — best-effort shutdown
                pass


if __name__ == "__main__":
    try:
        main()
    finally:
        shutdown_background_schedulers()
        # Bypass interpreter teardown. The WebSocket proxy starts a non-daemon
        # thread (websocket_proxy/app_integration.py:310, daemon=False) running
        # an infinite asyncio loop that no atexit hook can stop, so a normal
        # return blocks forever on threading._shutdown waiting for it. The
        # project's own signal_handler uses the same os._exit pattern for the
        # same reason. The smoke check has no DB writes / broker session, so
        # skipping atexit is safe here.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1 if CHECKS_FAILED > 0 else 0)
