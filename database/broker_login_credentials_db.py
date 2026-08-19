"""Encrypted broker login-credential storage (``broker_login_credentials``).

Additive table in the main database (``db/openalgo.db``) holding the operator's
Kite **user-id + login password** per broker (today: Zerodha), used by the
headless auto-login flow (``services/zerodha_web_login.py``). The password is
Fernet-encrypted with the same ``API_KEY_PEPPER``-derived key used for auth
tokens (``database.auth_db.encrypt_token`` / ``decrypt_token``) — never stored
or logged in plaintext.

**Write-only from the UI.** The API layer exposes only a boolean "configured"
status and the ``user_id`` (not a secret); the password is returned exclusively
to the server-side login service via ``get_credentials`` and never echoed to any
HTTP response. This is the first unattended-usable broker credential in the repo
— see ``docs/design/multi_account_plan.md`` §5.4 for the operator sign-off and
the ToS/risk trade-off it records.
"""

import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from database.auth_db import decrypt_token, encrypt_token
from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///db/openalgo.db")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class BrokerLoginCredential(Base):
    """One row per broker holding the encrypted Kite login password + user-id."""

    __tablename__ = "broker_login_credentials"

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(32), nullable=False, unique=True, index=True)
    user_id = Column(String(64), nullable=False)
    password_encrypted = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the ``broker_login_credentials`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("broker_login_credentials table ready")
    except Exception as e:
        logger.exception(f"Failed to init broker_login_credentials table: {e}")


init_broker_login_credentials_db = init_db


def set_credentials(broker: str, user_id: str, password: str) -> bool:
    """Store (or replace) the encrypted login credentials for ``broker``."""
    try:
        encrypted = encrypt_token(password)
        row = BrokerLoginCredential.query.filter_by(broker=broker).first()
        if row:
            row.user_id = user_id
            row.password_encrypted = encrypted
            row.updated_at = datetime.utcnow()
        else:
            db_session.add(
                BrokerLoginCredential(broker=broker, user_id=user_id, password_encrypted=encrypted)
            )
        db_session.commit()
        logger.info(f"Login credentials stored for broker '{broker}'")
        return True
    except Exception as e:
        db_session.rollback()
        logger.exception(f"Failed to store login credentials for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()


def get_credentials(broker: str) -> tuple[str, str] | None:
    """Return ``(user_id, decrypted_password)`` for ``broker``, or None if unset.

    Server-side only — never route the returned password to an HTTP response.
    """
    try:
        row = BrokerLoginCredential.query.filter_by(broker=broker).first()
        if not row:
            return None
        password = decrypt_token(row.password_encrypted)
        if not password:
            return None
        return row.user_id, password
    except Exception as e:
        logger.exception(f"Failed to read login credentials for broker '{broker}': {e}")
        return None
    finally:
        db_session.remove()


def get_user_id(broker: str) -> str | None:
    """Return the stored Kite user-id for ``broker`` (not a secret), or None."""
    try:
        row = BrokerLoginCredential.query.filter_by(broker=broker).first()
        return row.user_id if row else None
    except Exception as e:
        logger.exception(f"Failed to read user-id for broker '{broker}': {e}")
        return None
    finally:
        db_session.remove()


def has_credentials(broker: str) -> bool:
    """True when a login-credential row exists for ``broker``."""
    try:
        return BrokerLoginCredential.query.filter_by(broker=broker).first() is not None
    except Exception as e:
        logger.exception(f"Failed to check login credentials for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()


def delete_credentials(broker: str) -> bool:
    """Remove the login-credential row for ``broker``. True if a row was deleted."""
    try:
        deleted = BrokerLoginCredential.query.filter_by(broker=broker).delete()
        db_session.commit()
        if deleted:
            logger.info(f"Login credentials deleted for broker '{broker}'")
        return bool(deleted)
    except Exception as e:
        db_session.rollback()
        logger.exception(f"Failed to delete login credentials for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()
