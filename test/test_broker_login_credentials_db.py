"""Tests for the encrypted broker login-credential store (issue #654).

Covers ``database/broker_login_credentials_db``: set→get roundtrip, encrypted at
rest (the plaintext password never appears in the stored ciphertext),
``has_credentials`` / ``get_user_id`` accessors, replace-on-reset, and delete.

Hermetic: the global ``test/conftest.py`` redirect points ``DATABASE_URL`` at a
throwaway temp DB before any ``database.*`` import binds its engine.
"""

from __future__ import annotations

import pytest

USER_ID = "AB1234"
PASSWORD = "sup3r-s3cret-pw"  # pragma: allowlist secret
BROKER = "zerodha"


@pytest.fixture
def creds_db():
    import database.broker_login_credentials_db as db

    db.init_db()
    yield db
    try:
        db.db_session.query(db.BrokerLoginCredential).delete()
        db.db_session.commit()
    finally:
        db.db_session.remove()


def test_set_get_roundtrip(creds_db):
    assert creds_db.has_credentials(BROKER) is False
    assert creds_db.set_credentials(BROKER, USER_ID, PASSWORD) is True

    assert creds_db.has_credentials(BROKER) is True
    assert creds_db.get_user_id(BROKER) == USER_ID
    assert creds_db.get_credentials(BROKER) == (USER_ID, PASSWORD)


def test_password_encrypted_at_rest(creds_db):
    creds_db.set_credentials(BROKER, USER_ID, PASSWORD)
    row = creds_db.db_session.query(creds_db.BrokerLoginCredential).filter_by(broker=BROKER).first()
    assert row is not None
    assert PASSWORD not in row.password_encrypted
    creds_db.db_session.remove()


def test_replace_on_reset(creds_db):
    creds_db.set_credentials(BROKER, USER_ID, PASSWORD)
    creds_db.set_credentials(BROKER, "CD5678", "new-pw")  # pragma: allowlist secret
    assert creds_db.get_credentials(BROKER) == ("CD5678", "new-pw")
    # Still exactly one row for the broker.
    rows = creds_db.db_session.query(creds_db.BrokerLoginCredential).filter_by(broker=BROKER).all()
    assert len(rows) == 1
    creds_db.db_session.remove()


def test_delete(creds_db):
    creds_db.set_credentials(BROKER, USER_ID, PASSWORD)
    assert creds_db.delete_credentials(BROKER) is True
    assert creds_db.has_credentials(BROKER) is False
    assert creds_db.get_credentials(BROKER) is None
    # Deleting again is a no-op (no row).
    assert creds_db.delete_credentials(BROKER) is False


def test_missing_broker_returns_none(creds_db):
    assert creds_db.get_credentials("dhan") is None
    assert creds_db.get_user_id("dhan") is None
    assert creds_db.has_credentials("dhan") is False
