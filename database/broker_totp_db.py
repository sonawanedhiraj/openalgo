"""Encrypted broker TOTP secret storage (``broker_totp_secrets``).

Additive table in the main database (``db/openalgo.db``) holding the operator's
external-2FA TOTP secret per broker (today: Zerodha). The secret is Fernet-
encrypted with the same ``API_KEY_PEPPER``-derived key used for auth tokens
(``database.auth_db.encrypt_token`` / ``decrypt_token``) — never stored or
logged in plaintext. Write-only from the UI: the API layer exposes only the
derived 6-digit code, never the secret itself.
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


class BrokerTotpSecret(Base):
    """One row per broker holding the encrypted external-TOTP secret."""

    __tablename__ = "broker_totp_secrets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(32), nullable=False, unique=True, index=True)
    totp_secret_encrypted = Column(Text, nullable=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


def init_db():
    """Create the ``broker_totp_secrets`` table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("broker_totp_secrets table ready")
    except Exception as e:
        logger.exception(f"Failed to init broker_totp_secrets table: {e}")


init_broker_totp_db = init_db


def set_secret(broker: str, secret: str) -> bool:
    """Store (or replace) the encrypted TOTP secret for ``broker``."""
    try:
        encrypted = encrypt_token(secret)
        row = BrokerTotpSecret.query.filter_by(broker=broker).first()
        if row:
            row.totp_secret_encrypted = encrypted
            row.updated_at = datetime.utcnow()
        else:
            db_session.add(BrokerTotpSecret(broker=broker, totp_secret_encrypted=encrypted))
        db_session.commit()
        logger.info(f"TOTP secret stored for broker '{broker}'")
        return True
    except Exception as e:
        db_session.rollback()
        logger.exception(f"Failed to store TOTP secret for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()


def get_secret(broker: str) -> str | None:
    """Return the decrypted TOTP secret for ``broker``, or None if unset."""
    try:
        row = BrokerTotpSecret.query.filter_by(broker=broker).first()
        if not row:
            return None
        secret = decrypt_token(row.totp_secret_encrypted)
        return secret or None
    except Exception as e:
        logger.exception(f"Failed to read TOTP secret for broker '{broker}': {e}")
        return None
    finally:
        db_session.remove()


def has_secret(broker: str) -> bool:
    """True when a TOTP secret row exists for ``broker``."""
    try:
        return BrokerTotpSecret.query.filter_by(broker=broker).first() is not None
    except Exception as e:
        logger.exception(f"Failed to check TOTP secret for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()


def delete_secret(broker: str) -> bool:
    """Remove the TOTP secret row for ``broker``. Returns True if a row was deleted."""
    try:
        deleted = BrokerTotpSecret.query.filter_by(broker=broker).delete()
        db_session.commit()
        if deleted:
            logger.info(f"TOTP secret deleted for broker '{broker}'")
        return bool(deleted)
    except Exception as e:
        db_session.rollback()
        logger.exception(f"Failed to delete TOTP secret for broker '{broker}': {e}")
        return False
    finally:
        db_session.remove()
