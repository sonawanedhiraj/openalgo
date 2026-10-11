"""Issue #766 — single-flight master contract job + side-effect-free session resume.

On 2026-10-11 the boot auto-login started a master contract download at 08:51:14
and the browser login resumed the same broker session at 08:51:52. The resume
ran the full ``handle_auth_success``: it re-wrote the stored token (CACHE_INVALIDATE
→ WS pool torn down and rebuilt), fired ``broker_session_refreshed`` a second time,
and, because ``should_download_master_contract`` only sees COMPLETED downloads,
started a second 110k-row download beside the first. That overlap was part of the
~1.5 GB boot RSS peak behind the libzmq 10055 abort (#744).

These tests pin:
* one master contract job per broker at a time, for every caller;
* a skipped caller does not reset the status row under the running job;
* a resume never calls ``upsert_auth`` / CACHE_INVALIDATE / the session-refresh
  notify, and loads the master contract only when this process has not.
"""

import threading
from types import SimpleNamespace

import pytest
from flask import Flask, session

import blueprints.auth as auth_bp_module
import database.auth_db as auth_db
import services.broker_auto_login_service as auto_login_svc
import utils.auth_utils as auth_utils

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_in_flight():
    auth_utils._master_contract_jobs_in_flight.clear()
    yield
    auth_utils._master_contract_jobs_in_flight.clear()


@pytest.fixture()
def blocking_download(monkeypatch):
    """Fake download that blocks until released; records every call."""
    started = threading.Event()
    release = threading.Event()
    calls = []

    def _download(broker):
        calls.append(broker)
        started.set()
        release.wait(5)

    monkeypatch.setattr(auth_utils, "async_master_contract_download", _download)
    monkeypatch.setattr(
        auth_utils, "load_existing_master_contract", lambda b: pytest.fail("unexpected load")
    )
    monkeypatch.setattr(auth_utils, "should_download_master_contract", lambda b: (True, "test"))
    inits = []
    monkeypatch.setattr(auth_utils, "init_broker_status", lambda b: inits.append(b))
    return SimpleNamespace(started=started, release=release, calls=calls, inits=inits)


def _wait_released(broker, timeout=5.0):
    deadline = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if not auth_utils.is_master_contract_job_running(broker):
            return True
        deadline.wait(0.01)
    return False


def test_second_job_is_skipped_while_first_in_flight(blocking_download):
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert blocking_download.started.wait(5)

    assert auth_utils.start_master_contract_job("zerodha") is None
    assert auth_utils.start_master_contract_job("zerodha", force_download=True) is None

    blocking_download.release.set()
    assert _wait_released("zerodha")
    assert blocking_download.calls == ["zerodha"]
    # The skipped callers must not reset the status row under the running job.
    assert blocking_download.inits == ["zerodha"]


def test_slot_frees_after_completion(blocking_download):
    blocking_download.release.set()
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert _wait_released("zerodha")
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert _wait_released("zerodha")
    assert blocking_download.calls == ["zerodha", "zerodha"]


def test_raising_job_releases_slot(monkeypatch):
    def _boom(broker):
        raise RuntimeError("broker down")

    monkeypatch.setattr(auth_utils, "async_master_contract_download", _boom)
    monkeypatch.setattr(auth_utils, "should_download_master_contract", lambda b: (True, "t"))
    monkeypatch.setattr(auth_utils, "init_broker_status", lambda b: None)
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert _wait_released("zerodha")


def test_failure_before_thread_start_releases_slot(monkeypatch):
    def _boom(broker):
        raise RuntimeError("status db locked")

    monkeypatch.setattr(auth_utils, "init_broker_status", _boom)
    with pytest.raises(RuntimeError):
        auth_utils.start_master_contract_job("zerodha")
    assert not auth_utils.is_master_contract_job_running("zerodha")


def test_brokers_are_independent(blocking_download):
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert auth_utils.start_master_contract_job("dhan") == "download"
    blocking_download.release.set()
    assert _wait_released("zerodha") and _wait_released("dhan")
    assert sorted(blocking_download.calls) == ["dhan", "zerodha"]


def test_cached_path_uses_load_not_download(monkeypatch):
    loads = []
    monkeypatch.setattr(auth_utils, "should_download_master_contract", lambda b: (False, "t"))
    monkeypatch.setattr(auth_utils, "init_broker_status", lambda b: None)
    monkeypatch.setattr(
        auth_utils, "async_master_contract_download", lambda b: pytest.fail("downloaded")
    )
    monkeypatch.setattr(auth_utils, "load_existing_master_contract", lambda b: loads.append(b))
    assert auth_utils.start_master_contract_job("zerodha") == "load"
    assert _wait_released("zerodha")
    assert loads == ["zerodha"]


# --------------------------------------------------------------------------- #
# Callers
# --------------------------------------------------------------------------- #
@pytest.fixture()
def request_ctx():
    app = Flask(__name__)
    app.secret_key = "test-secret"  # pragma: allowlist secret
    with app.test_request_context(
        "/auth/login", method="POST", headers={"Accept": "application/json"}
    ):
        yield app


def test_login_during_in_flight_download_starts_no_second_one(
    monkeypatch, request_ctx, blocking_download
):
    """The 2026-10-11 shape: auto-login downloading, then a real OAuth login."""
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert blocking_download.started.wait(5)

    monkeypatch.setattr(auth_utils, "upsert_auth", lambda *a, **k: 1)
    monkeypatch.setattr(auth_utils, "notify_broker_session_refreshed", lambda u, b: None)
    monkeypatch.setattr(auth_db, "register_session", lambda **k: None)
    monkeypatch.setattr(auth_db, "get_active_sessions", lambda u: [])
    monkeypatch.setattr(auth_db, "log_login_attempt", lambda **k: None)
    import extensions

    monkeypatch.setattr(extensions, "socketio", SimpleNamespace(emit=lambda *a, **k: None))

    auth_utils.handle_auth_success("tok", "admin", "zerodha")

    blocking_download.release.set()
    assert _wait_released("zerodha")
    assert blocking_download.calls == ["zerodha"]


def test_auto_login_routes_through_single_flight(monkeypatch, blocking_download):
    assert auth_utils.start_master_contract_job("zerodha") == "download"
    assert blocking_download.started.wait(5)

    import database.user_db as user_db

    monkeypatch.setattr(auth_db, "upsert_auth", lambda *a, **k: 1)
    monkeypatch.setattr(user_db, "find_user_by_username", lambda: SimpleNamespace(username="admin"))
    notified = []
    monkeypatch.setattr(
        auth_utils, "notify_broker_session_refreshed", lambda u, b: notified.append((u, b))
    )

    auto_login_svc._apply_primary_session("key", "access")

    blocking_download.release.set()
    assert _wait_released("zerodha")
    assert blocking_download.calls == ["zerodha"]
    assert notified == [("admin", "zerodha")]  # a NEW token still notifies


def _auth_record():
    return SimpleNamespace(
        auth="encrypted-token",
        feed_token=None,
        broker="zerodha",
        user_id="AB1234",
        is_revoked=False,
    )


def test_resume_has_no_token_side_effects(monkeypatch, request_ctx):
    """A resume re-uses the stored token: no upsert, no invalidate, no notify."""
    import importlib

    import database.cache_invalidation as cache_inv

    session["user"] = "admin"
    fake_funds = SimpleNamespace(
        test_auth_token=lambda token: (True, None),
        get_margin_data=lambda token: {"availablecash": "100.00"},
    )
    real_import = importlib.import_module

    def _import(path, *a, **k):
        if path == "broker.zerodha.api.funds":
            return fake_funds
        return real_import(path, *a, **k)

    def _forbidden(name):
        def _f(*a, **k):
            raise AssertionError(f"{name} must not run on a session resume")

        return _f

    monkeypatch.setattr(auth_db, "get_auth_token_dbquery", lambda u: _auth_record())
    monkeypatch.setattr(auth_db, "decrypt_token", lambda t: "plain-token")
    monkeypatch.setattr(importlib, "import_module", _import)
    monkeypatch.setattr(auth_db, "upsert_auth", _forbidden("upsert_auth"))
    monkeypatch.setattr(auth_utils, "upsert_auth", _forbidden("upsert_auth"))
    monkeypatch.setattr(cache_inv, "publish_all_cache_invalidation", _forbidden("invalidate"))
    monkeypatch.setattr(
        auth_utils, "notify_broker_session_refreshed", _forbidden("broker_session_refreshed")
    )
    monkeypatch.setattr(auth_db, "register_session", lambda **k: None)
    ensured = []
    monkeypatch.setattr(auth_utils, "ensure_master_contract_loaded", lambda b: ensured.append(b))

    response, status = auth_bp_module._try_resume_broker_session("admin")

    assert status == 200
    assert response.get_json()["message"] == "Broker session resumed"
    assert session["logged_in"] is True
    assert session["broker"] == "zerodha"
    assert session["USER_ID"] == "AB1234"
    assert ensured == ["zerodha"]


def test_ensure_skips_when_cache_already_loaded(monkeypatch):
    import database.token_db_enhanced as tdb

    monkeypatch.setattr(
        tdb, "get_cache", lambda: SimpleNamespace(cache_loaded=True, is_cache_valid=lambda: True)
    )
    monkeypatch.setattr(
        auth_utils, "start_master_contract_job", lambda b: pytest.fail("job started")
    )
    assert auth_utils.ensure_master_contract_loaded("zerodha") == "loaded"


def test_ensure_loads_when_process_has_no_cache(monkeypatch):
    """A restart that found the token live at boot: the resume must load it."""
    import database.token_db_enhanced as tdb

    monkeypatch.setattr(
        tdb, "get_cache", lambda: SimpleNamespace(cache_loaded=False, is_cache_valid=lambda: False)
    )
    started = []
    monkeypatch.setattr(
        auth_utils, "start_master_contract_job", lambda b: started.append(b) or "load"
    )
    assert auth_utils.ensure_master_contract_loaded("zerodha") == "load"
    assert started == ["zerodha"]


def test_ensure_never_raises(monkeypatch):
    import database.token_db_enhanced as tdb

    def _boom():
        raise RuntimeError("cache broken")

    monkeypatch.setattr(tdb, "get_cache", _boom)

    def _boom_job(b):
        raise RuntimeError("status db locked")

    monkeypatch.setattr(auth_utils, "start_master_contract_job", _boom_job)
    assert auth_utils.ensure_master_contract_loaded("zerodha") is None
