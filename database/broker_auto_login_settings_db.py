"""DB-backed master switch for headless auto-login (issue #654).

A single-row table holding whether automatic (boot + watcher) re-login is on.
This is the UI-toggleable source of truth — the ``BROKER_AUTO_LOGIN_ENABLED``
env var is only a first-boot **seed** (mirrors ``multi_account_settings``, issue
#484): the DB row, once written, always wins, and a UI flip applies immediately
with no restart. The manual "Auto login now" buttons work regardless of this
flag — it gates only the automatic behaviour.
"""

import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

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


class BrokerAutoLoginSettings(Base):
    """Single-row (id=1) master switch for automatic auto-login."""

    __tablename__ = "broker_auto_login_settings"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)


def init_db():
    """Create the table if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("broker_auto_login_settings table ready")
    except Exception as e:
        logger.exception(f"Failed to init broker_auto_login_settings table: {e}")


init_broker_auto_login_settings_db = init_db


def _env_seed() -> bool:
    return os.getenv("BROKER_AUTO_LOGIN_ENABLED", "false").lower() == "true"


def get_enabled() -> bool:
    """Master auto-login flag. DB row wins; falls back to the env seed if unset.

    Fail-open to the env seed on any read error so a transient DB issue can't
    silently disable a configured install.
    """
    try:
        row = BrokerAutoLoginSettings.query.filter_by(id=1).first()
        if row is None:
            return _env_seed()
        return bool(row.enabled)
    except Exception as e:
        logger.exception(f"broker_auto_login_settings read failed: {e}")
        return _env_seed()
    finally:
        db_session.remove()


def set_enabled(enabled: bool, updated_by: str | None = None) -> bool:
    """Persist the master flag. Returns True on success."""
    try:
        row = BrokerAutoLoginSettings.query.filter_by(id=1).first()
        if row is None:
            row = BrokerAutoLoginSettings(id=1, enabled=bool(enabled), updated_by=updated_by)
            db_session.add(row)
        else:
            row.enabled = bool(enabled)
            row.updated_at = datetime.utcnow()
            row.updated_by = updated_by
        db_session.commit()
        logger.info(f"broker auto-login master flag set to {bool(enabled)} by {updated_by}")
        return True
    except Exception as e:
        db_session.rollback()
        logger.exception(f"broker_auto_login_settings write failed: {e}")
        return False
    finally:
        db_session.remove()
