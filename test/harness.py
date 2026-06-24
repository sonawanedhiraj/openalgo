"""BootHarness — Spring-Boot-style integration test harness for OpenAlgo.

Sets OPENALGO_TESTING=1 before importing ``app`` so the module-level
singleton check, background daemons, and WebSocket proxy are all skipped.
The conftest.py DB redirect is the structural guard that keeps all DB writes
in a throwaway temp directory.

Usage::

    harness = BootHarness.create()
    harness.mock_zerodha_login()
    harness.client.get("/strategies/api/list")

Phase 2 of #129 — provides the test infrastructure that Phases 3+4 chain off.
"""

from __future__ import annotations

import os

# Must be set BEFORE importing app so the module-level guard skips daemons.
os.environ["OPENALGO_TESTING"] = "1"
# Prevent OpenBLAS OOM on the RAM-starved dev box (see memory/ram-starved-box).
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import importlib
from typing import Any
from unittest.mock import MagicMock, patch

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

from app import create_app  # noqa: E402 — must follow the env-var guard above


class BootHarness:
    """Minimal Flask test harness — no background daemons, real (temp) DBs."""

    def __init__(self, flask_app, scheduler: BackgroundScheduler):
        self._app = flask_app
        self._scheduler = scheduler
        self._client = flask_app.test_client()
        self._client.__enter__()

    # ---------------------------------------------------------------------- #
    # Factory
    # ---------------------------------------------------------------------- #

    @classmethod
    def create(
        cls,
        *,
        init_sector_follow: bool = False,
        init_futures_follow: bool = False,
    ) -> BootHarness:
        """Boot a minimal Flask app with all DBs in the conftest.py temp dir.

        ``init_sector_follow`` / ``init_futures_follow``: if True, register the
        real APScheduler jobs for those services (they're added to the internal
        scheduler but the scheduler is never *started*, so no cron fires).
        """
        flask_app = create_app(testing=True)

        # Initialise DB tables for the redirected temp DBs.
        # conftest.py's session fixture handles most tables; ensure the ones
        # integration tests need most are present.
        with flask_app.app_context():
            _init_integration_tables()

        # A real scheduler (not started) so get_jobs() / fire_job() work.
        scheduler = BackgroundScheduler()

        harness = cls(flask_app, scheduler)

        if init_sector_follow:
            harness._init_sector_follow(scheduler)
        if init_futures_follow:
            harness._init_futures_follow(scheduler)

        return harness

    # ---------------------------------------------------------------------- #
    # Test client
    # ---------------------------------------------------------------------- #

    @property
    def client(self):
        return self._client

    @property
    def app(self):
        return self._app

    @property
    def scheduler(self) -> BackgroundScheduler:
        return self._scheduler

    # ---------------------------------------------------------------------- #
    # Broker session mock
    # ---------------------------------------------------------------------- #

    def mock_zerodha_login(
        self,
        *,
        username: str = "admin",
        token: str = "fake_zerodha_token_abc123",
        user_id: str = "ZR0001",
    ) -> None:
        """Insert a fake Zerodha broker session into auth_db, bypassing OAuth.

        Patches ZMQ so ``upsert_auth`` does not try to publish on a real socket.
        ``publish_all_cache_invalidation`` is a local import inside upsert_auth,
        so we patch it at the source module, not the auth_db namespace.
        """
        from database.auth_db import upsert_auth

        with (
            self._app.app_context(),
            patch("database.cache_invalidation.publish_all_cache_invalidation"),
        ):
            upsert_auth(
                name=username,
                auth_token=token,
                broker="zerodha",
                user_id=user_id,
            )

    def set_auth_session(self, username: str = "admin") -> None:
        """Set a valid Flask session so session-gated API routes return 200.

        Uses the test client's session_transaction() to inject ``logged_in``
        and ``login_time`` (a recent IST timestamp) so ``is_session_valid()``
        passes without needing a real login flow.
        """
        import datetime as _dt

        import pytz

        now_ist = _dt.datetime.now(pytz.timezone("Asia/Kolkata"))
        with self._client.session_transaction() as sess:
            sess["logged_in"] = True
            sess["user"] = username
            sess["login_time"] = now_ist.isoformat()

    def get_auth_token(self, username: str = "admin") -> str | None:
        """Retrieve the stored auth token for ``username`` (decrypted)."""
        from database.auth_db import get_auth_token

        with self._app.app_context():
            return get_auth_token(username)

    # ---------------------------------------------------------------------- #
    # historify seeding
    # ---------------------------------------------------------------------- #

    def seed_historify(
        self,
        symbol: str,
        interval: str,
        bars: list[dict],
    ) -> None:
        """Write synthetic OHLCV rows into the temp historify.duckdb.

        Each bar dict: {timestamp, open, high, low, close, volume}.
        ``timestamp`` should be a ``datetime`` or ISO string.
        """
        import duckdb

        db_path = os.environ["HISTORIFY_DATABASE_PATH"]
        con = duckdb.connect(db_path)
        try:
            # Ensure table exists (mirrors historify_db schema)
            con.execute("""
                CREATE TABLE IF NOT EXISTS market_data (
                    symbol VARCHAR,
                    interval VARCHAR,
                    timestamp TIMESTAMP,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume BIGINT,
                    PRIMARY KEY (symbol, interval, timestamp)
                )
            """)
            for b in bars:
                con.execute(
                    """
                    INSERT OR REPLACE INTO market_data
                        (symbol, interval, timestamp, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        symbol,
                        interval,
                        b["timestamp"],
                        b["open"],
                        b["high"],
                        b["low"],
                        b["close"],
                        b.get("volume", 0),
                    ],
                )
        finally:
            con.close()

    # ---------------------------------------------------------------------- #
    # Tick injection (direct aggregator write — no ZMQ)
    # ---------------------------------------------------------------------- #

    def inject_tick(
        self,
        symbol: str,
        price: float,
        volume: int = 1000,
        timestamp=None,
    ) -> None:
        """Push a synthetic tick directly into the in-process bar aggregator.

        Does NOT go through ZMQ. Use ``ScannerService.get_today_ohlcv()`` or
        inspect aggregator state afterwards.
        """
        import datetime as _dt

        if timestamp is None:
            timestamp = _dt.datetime.now()

        try:
            from services.scanner_service import get_scanner_service

            svc = get_scanner_service()
            if svc is None:
                return
            if hasattr(svc, "_aggregator"):
                svc._aggregator.on_tick(symbol, price, volume, timestamp)
        except Exception:
            pass

    # ---------------------------------------------------------------------- #
    # Strategy mode helpers
    # ---------------------------------------------------------------------- #

    def set_strategy_mode(self, strategy_name: str, mode: str) -> None:
        """Set a strategy's persistent mode directly in the DB."""
        from database.strategy_mode_db import set_mode

        with self._app.app_context():
            set_mode(strategy_name, mode, updated_by="harness")

    def get_strategy_mode(self, strategy_name: str) -> str | None:
        """Return a strategy's current mode from the DB."""
        from database.strategy_mode_db import get_mode

        with self._app.app_context():
            result = get_mode(strategy_name)
            return result.get("mode") if result else None

    # ---------------------------------------------------------------------- #
    # APScheduler job firing
    # ---------------------------------------------------------------------- #

    def fire_scheduled_job(self, job_id: str, **kwargs: Any) -> None:
        """Trigger a named APScheduler job synchronously (no clock advance needed)."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            raise ValueError(f"No job with id={job_id!r} registered in harness scheduler")
        job.func(*job.args, **{**job.kwargs, **kwargs})

    def get_registered_job_ids(self) -> list[str]:
        """Return all job IDs registered with the harness scheduler."""
        return [j.id for j in self._scheduler.get_jobs()]

    # ---------------------------------------------------------------------- #
    # Private — service init helpers (testing-safe)
    # ---------------------------------------------------------------------- #

    def _init_sector_follow(self, scheduler: BackgroundScheduler) -> None:
        """Register sector_follow jobs on ``scheduler`` (no background threads)."""
        try:
            from services.sector_follow_service import init_sector_follow_service

            with self._app.app_context():
                with (
                    patch("services.sector_follow_service.production_order_placer"),
                    patch("services.sector_follow_service.production_data_health_checker"),
                    patch("services.sector_follow_service.production_intraday_provider"),
                ):
                    init_sector_follow_service(app=self._app, scheduler=scheduler)
        except Exception as exc:
            import warnings

            warnings.warn(f"BootHarness: sector_follow init failed: {exc}", stacklevel=2)

    def _init_futures_follow(self, scheduler: BackgroundScheduler) -> None:
        """Register futures_follow jobs on ``scheduler`` (no background threads)."""
        try:
            from services.futures_follow_service import init_futures_follow_service

            with self._app.app_context():
                init_futures_follow_service(app=self._app, scheduler=scheduler)
        except Exception as exc:
            import warnings

            warnings.warn(f"BootHarness: futures_follow init failed: {exc}", stacklevel=2)

    # ---------------------------------------------------------------------- #
    # Cleanup
    # ---------------------------------------------------------------------- #

    def ensure_scan_definition(
        self,
        name: str,
        screener_type: str = "buy",
        rule_module: str | None = None,
        enabled: bool = True,
    ) -> int:
        """Idempotent create-or-return a scan definition.

        Unlike ``create_scan_definition`` this never raises IntegrityError —
        if the name already exists (e.g. from a prior test in the same session)
        it returns the existing row's id. Useful for integration tests that need
        a definition but must survive a session-level shared DB.
        """
        from sqlalchemy.exc import IntegrityError as _SAIntegrityError

        from services.scanner_service import create_scan_definition, get_scan_definitions

        with self._app.app_context():
            try:
                return create_scan_definition(
                    name=name,
                    screener_type=screener_type,
                    rule_module=rule_module,
                    enabled=enabled,
                )
            except _SAIntegrityError:
                # Already exists — return the existing id.
                existing = [
                    d for d in get_scan_definitions(enabled_only=False) if d["name"] == name
                ]
                if existing:
                    return existing[0]["id"]
                raise

    def teardown(self) -> None:
        """Release resources. Call in test teardown (or use as context manager)."""
        try:
            self._client.__exit__(None, None, None)
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.teardown()


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _init_integration_tables() -> None:
    """Ensure the DB tables most used by integration tests exist in the temp DBs."""
    targets = [
        ("database.strategy_mode_db", "init_db"),
        ("database.strategy_runtime_override_db", "init_db"),
        ("database.auth_db", "init_db"),
        ("database.sector_follow_db", "init_db"),
        ("database.scanner_db", "init_db"),
        ("database.scan_cycle_db", "init_db"),
    ]
    for module_path, fn_name in targets:
        try:
            mod = importlib.import_module(module_path)
            getattr(mod, fn_name)()
        except Exception:
            pass
