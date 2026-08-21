"""Test: orphaned API keys don't break background services.

Simulates the scenario where:
1. User 'admin' has an API key but their auth session is revoked (no broker)
2. User 'jagat' has an API key with an active auth session (broker=shoonya)
3. get_first_available_api_key() should return jagat's key, not admin's
4. get_auth_token_broker() should cache negative results for revoked users
   to prevent log spam from background polling (every 5s)

To run inside the OpenAlgo container:
    docker exec openalgo python test_orphaned_apikey.py
"""

import hashlib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

# NOTE: this module must NOT mutate os.environ["DATABASE_URL"] at import time.
# Under pytest, test/conftest.py already redirects every DB to a throwaway temp
# dir, and a module-level ``DATABASE_URL=sqlite:///:memory:`` leaks into the
# rest of the session: any database.* module first-imported AFTER this file
# binds its engine to an empty per-connection :memory: DB and every query fails
# with "no such table". Standalone execution sets it in ``__main__`` below.


@pytest.fixture(autouse=True)
def _restore_auth_db_binding():
    """Undo ``setup_test_db``'s rebinding of database.auth_db after each test.

    ``setup_test_db`` swaps ``auth_mod.db_session`` / ``Base.query`` to a
    StaticPool :memory: engine seeded with an ACTIVE user + API key and never
    restored it. Left in place, every later test in the pytest session sees
    ``get_first_available_api_key()`` return a real key — which let the scanner
    boot-convergence tests fire an UNMOCKED ``resettle_recent_daily`` broker
    download whose ``wait_for_jobs`` poll hung the full suite (2026-07-26).
    """
    import database.auth_db as auth_mod

    orig_session = auth_mod.db_session
    orig_engine = auth_mod.engine
    # Raw descriptor lookup — ``Base.query`` would INVOKE the query_property
    # descriptor on the unmapped Base class and raise ArgumentError.
    orig_query = auth_mod.Base.__dict__["query"]
    yield
    leaked = auth_mod.db_session
    if leaked is not orig_session:
        try:
            leaked.remove()
        except Exception:
            pass
    leaked_engine = auth_mod.engine
    if leaked_engine is not orig_engine:
        try:
            leaked_engine.dispose()
        except Exception:
            pass
    auth_mod.db_session = orig_session
    auth_mod.engine = orig_engine
    auth_mod.Base.query = orig_query
    # Remove the temp DB files this test created. Best-effort: a leftover temp
    # dir is harmless, a failed teardown that masks a real error is not.
    import shutil

    while _TEMP_DB_DIRS:
        shutil.rmtree(_TEMP_DB_DIRS.pop(), ignore_errors=True)
    # The tests plant entries keyed by their fake API keys; don't let them
    # shadow real lookups later in the session.
    auth_mod.auth_cache.clear()
    auth_mod.verified_api_key_cache.clear()
    auth_mod.invalid_api_key_cache.clear()


# Temp directories created by ``setup_test_db``, cleaned up after each test.
_TEMP_DB_DIRS: list[str] = []


def setup_test_db():
    """Create a fresh **file-backed** test database, seeded and rebound.

    History (three fixes, the third because the first two were not enough):

    1. ``sqlite:///:memory:`` + the default ``QueuePool`` gives every connection
       its OWN isolated in-memory database — ``create_all`` ran on connection A
       and the session later checked out connection B, which saw an empty DB.
       (#231)
    2. The prior ``scoped_session`` lingered in the thread-local registry, so
       rebinding ``auth_mod.db_session`` alone was not enough; it is explicitly
       ``.remove()``-d first. (#231)
    3. ``StaticPool`` fixed (1) only by making one connection *the* database —
       which means if that connection is ever recycled, invalidated, or opened
       from another thread than the one ``create_all`` ran on, the DB is silently
       EMPTY again and every query fails ``no such table: auth``. That is
       schedule-dependent, so it reappeared whenever a PR added tests and shifted
       the xdist grouping: #233 (13 contract tests), and again on `dev` itself at
       606aa324 and on #536.

       Fixed properly by using a **temp file** instead of ``:memory:``. Connection
       identity then stops mattering at all: every connection, on any thread, at
       any point in the pool's lifetime, opens the same file and sees the same
       tables. ``NullPool`` matches production (see CLAUDE.md, which bans
       ``StaticPool`` for exactly this class of bug).
    """
    import tempfile

    from sqlalchemy import create_engine
    from sqlalchemy.orm import scoped_session, sessionmaker
    from sqlalchemy.pool import NullPool

    import database.auth_db as auth_mod

    # Drop any prior thread-local session so the rebind below actually takes
    # effect for queries issued in this test.
    prior = getattr(auth_mod, "db_session", None)
    if prior is not None:
        try:
            prior.remove()
        except Exception:
            pass

    tmp_dir = tempfile.mkdtemp(prefix="orphaned_apikey_")
    _TEMP_DB_DIRS.append(tmp_dir)
    engine = create_engine(
        f"sqlite:///{tmp_dir}/auth_test.db",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    # Rebind the module's engine too — some auth_db helpers reach for it
    # directly rather than going through db_session.
    auth_mod.engine = engine
    auth_mod.db_session = scoped_session(
        sessionmaker(autocommit=False, autoflush=False, bind=engine)
    )
    auth_mod.Base.query = auth_mod.db_session.query_property()
    auth_mod.Base.metadata.create_all(engine)

    return auth_mod


def test_get_first_available_api_key_skips_revoked():
    """get_first_available_api_key() must skip users with revoked auth sessions."""
    auth_mod = setup_test_db()

    # Setup: admin with revoked session (no broker)
    admin_auth = auth_mod.Auth(name="admin", auth="", feed_token=None, broker="", is_revoked=1)
    auth_mod.db_session.add(admin_auth)
    auth_mod.db_session.commit()

    admin_api_key = "admin_key_abc123"
    auth_mod.upsert_api_key("admin", admin_api_key)

    # Setup: jagat with active session (broker=shoonya)
    jagat_auth = auth_mod.Auth(
        name="jagat_user",
        auth="encrypted_token",
        feed_token=None,
        broker="shoonya",
        is_revoked=0,
    )
    auth_mod.db_session.add(jagat_auth)
    auth_mod.db_session.commit()

    jagat_api_key = "jagat_key_xyz789"
    auth_mod.upsert_api_key("jagat_user", jagat_api_key)

    # Test: get_first_available_api_key should return jagat's key, not admin's
    result = auth_mod.get_first_available_api_key()

    assert result is not None, "Expected a valid API key, got None"
    assert result == jagat_api_key, (
        f"Expected jagat's key '{jagat_api_key}', got '{result}'. "
        "get_first_available_api_key() is returning an orphaned/revoked user's key."
    )

    print("PASS: get_first_available_api_key() correctly skips revoked users")

    # Test: with ALL sessions revoked, should return None
    jagat_auth.is_revoked = 1
    auth_mod.db_session.commit()

    result2 = auth_mod.get_first_available_api_key()
    assert result2 is None, f"Expected None when all sessions are revoked, got '{result2}'"

    print("PASS: get_first_available_api_key() returns None when all sessions revoked")


def test_get_first_available_api_key_skips_no_broker():
    """get_first_available_api_key() must skip users with empty broker field."""
    auth_mod = setup_test_db()

    # User with active session but no broker configured
    no_broker_auth = auth_mod.Auth(
        name="no_broker_user",
        auth="",
        feed_token=None,
        broker="",
        is_revoked=0,
    )
    auth_mod.db_session.add(no_broker_auth)
    auth_mod.db_session.commit()
    auth_mod.upsert_api_key("no_broker_user", "no_broker_key_111")

    # User with active session and broker
    active_auth = auth_mod.Auth(
        name="active_user",
        auth="token",
        feed_token=None,
        broker="shoonya",
        is_revoked=0,
    )
    auth_mod.db_session.add(active_auth)
    auth_mod.db_session.commit()
    auth_mod.upsert_api_key("active_user", "active_key_222")

    result = auth_mod.get_first_available_api_key()
    assert result == "active_key_222", (
        f"Expected active_user's key, got '{result}'. "
        "get_first_available_api_key() returned key for user with no broker."
    )

    print("PASS: get_first_available_api_key() skips users with empty broker")


def test_auth_token_broker_caches_negative_result():
    """get_auth_token_broker() should cache revoked results to prevent log spam."""
    auth_mod = setup_test_db()

    # Clear all caches
    auth_mod.auth_cache.clear()
    auth_mod.verified_api_key_cache.clear()
    auth_mod.invalid_api_key_cache.clear()

    # User with revoked session
    revoked_auth = auth_mod.Auth(
        name="revoked_user",
        auth="old_token",
        feed_token=None,
        broker="shoonya",
        is_revoked=1,
    )
    auth_mod.db_session.add(revoked_auth)
    auth_mod.db_session.commit()

    api_key = "revoked_user_key_333"
    auth_mod.upsert_api_key("revoked_user", api_key)

    # First call — should return None and cache the negative result
    result1 = auth_mod.get_auth_token_broker(api_key)
    assert result1 == (None, None), f"Expected (None, None) for revoked user, got {result1}"

    # Check that the negative result was cached in auth_cache
    cache_key = f"{hashlib.sha256(api_key.encode()).hexdigest()}_False"
    assert cache_key in auth_mod.auth_cache, (
        "Negative result was NOT cached in auth_cache. This causes log spam "
        "because every subsequent call hits the DB and logs a warning."
    )
    assert auth_mod.auth_cache[cache_key] == (None, None), (
        f"Expected cached (None, None), got {auth_mod.auth_cache[cache_key]}"
    )

    print("PASS: get_auth_token_broker() caches negative result for revoked users")


def test_only_admin_revoked_reproduces_original_bug():
    """Reproduce the exact production scenario from 2026-03-25.

    State:
    - api_keys row 1: user_id='admin' (created first, so .first() returns this)
    - api_keys row 2: user_id='jagat_4579e4'
    - auth row 1: name='admin', broker='', is_revoked=1
    - auth row 2: name='jagat_4579e4', broker='shoonya', is_revoked=0

    Before fix: get_first_available_api_key() returned admin's key → downstream
    calls failed with "No valid auth token or broker found for user_id 'admin'"
    every 5 seconds.

    After fix: get_first_available_api_key() returns jagat's key.
    """
    auth_mod = setup_test_db()

    # Exact production state
    admin_auth = auth_mod.Auth(name="admin", auth="", feed_token=None, broker="", is_revoked=1)
    jagat_auth = auth_mod.Auth(
        name="jagat_4579e4",
        auth="encrypted_shoonya_token",
        feed_token=None,
        broker="shoonya",
        is_revoked=0,
    )
    auth_mod.db_session.add(admin_auth)
    auth_mod.db_session.add(jagat_auth)
    auth_mod.db_session.commit()

    auth_mod.upsert_api_key("admin", "old_admin_key")
    auth_mod.upsert_api_key("jagat_4579e4", "a55ef11f02ae_actual_key")

    result = auth_mod.get_first_available_api_key()

    assert result == "a55ef11f02ae_actual_key", (
        f"REGRESSION: get_first_available_api_key() returned '{result}' instead of "
        "jagat's key. The orphaned admin key is being returned, which will cause "
        "'No valid auth token or broker' warning spam every 5 seconds."
    )

    print("PASS: Production scenario (admin revoked + jagat active) returns correct key")


if __name__ == "__main__":
    # Standalone (non-pytest) safety belt: make sure a direct `python` run can
    # never touch the live DB. Under pytest this is unnecessary (conftest
    # redirects) and harmful at import time (see module note above).
    os.environ["DATABASE_URL"] = "sqlite:///:memory:"

    print("=" * 60)
    print("Testing orphaned API key handling")
    print("=" * 60)
    print()

    test_get_first_available_api_key_skips_revoked()
    print()
    test_get_first_available_api_key_skips_no_broker()
    print()
    test_auth_token_broker_caches_negative_result()
    print()
    test_only_admin_revoked_reproduces_original_bug()

    print()
    print("=" * 60)
    print("All tests passed!")
    print("=" * 60)
