"""Multi-account child broker accounts (``broker_accounts`` + ``account_strategies``).

Phase 1 of the multi-account plan (issue #468, docs/design/multi_account_plan.md).
Child accounts exist for exactly one purpose: mirroring the primary's strategy
orders (Phase 2). This module owns only the two additive tables in the main
database (``db/openalgo.db``); it never touches the primary's auth row or any
order path.

- ``broker_accounts`` — one row per child account: Kite Connect app credentials
  (Fernet-encrypted with the same ``API_KEY_PEPPER``-derived key as auth tokens),
  capital, optional per-account TOTP secret, enabled flag (default OFF).
- ``account_strategies`` — allow-list rows (account_id, strategy_name); a row
  present means "this account mirrors this strategy".

The child's daily access token is NOT stored here — it lives in the existing
``auth`` table under ``name = f"acct:{id}"`` (see
``services.broker_accounts_service``). Secrets are write-only: list/get dicts
never include decrypted credentials; ``get_credentials`` exists for the login
exchange only. NullPool per the project's SQLite connection-pooling rule.
"""

import os
from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
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

AUTH_NAME_PREFIX = "acct:"


class BrokerAccount(Base):
    """One row per child broker account (the primary is NOT a row here)."""

    __tablename__ = "broker_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    display_name = Column(String(64), nullable=False, unique=True)
    broker = Column(String(32), nullable=False, default="zerodha")
    broker_client_id = Column(String(32), nullable=True)
    api_key_encrypted = Column(Text, nullable=False)
    api_secret_encrypted = Column(Text, nullable=False)
    totp_secret_encrypted = Column(Text, nullable=True)
    capital_inr = Column(Float, nullable=False, default=0.0)
    is_enabled = Column(Boolean, nullable=False, default=False)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountStrategy(Base):
    """Allow-list row: this account mirrors this strategy."""

    __tablename__ = "account_strategies"

    account_id = Column(Integer, primary_key=True, nullable=False)
    strategy_name = Column(String(64), primary_key=True, nullable=False)


def init_db():
    """Create the broker_accounts/account_strategies tables if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("broker_accounts tables ready")
    except Exception as e:
        logger.exception(f"Failed to init broker_accounts tables: {e}")


ensure_broker_accounts_tables_exists = init_db


def auth_name(account_id: int) -> str:
    """The ``auth`` table key for a child account's daily token."""
    return f"{AUTH_NAME_PREFIX}{int(account_id)}"


def _row_to_dict(row: BrokerAccount) -> dict:
    """Public dict — NEVER includes decrypted credentials or the TOTP secret."""
    return {
        "id": row.id,
        "display_name": row.display_name,
        "broker": row.broker,
        "broker_client_id": row.broker_client_id,
        "capital_inr": row.capital_inr,
        "is_enabled": bool(row.is_enabled),
        "has_totp_secret": row.totp_secret_encrypted is not None,
        "last_login_at": row.last_login_at.isoformat() if row.last_login_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_accounts() -> list[dict]:
    """All child accounts ordered by id."""
    try:
        return [_row_to_dict(r) for r in db_session.query(BrokerAccount).order_by(BrokerAccount.id)]
    finally:
        db_session.remove()


def get_account(account_id: int) -> dict | None:
    try:
        row = db_session.query(BrokerAccount).filter_by(id=account_id).first()
        return _row_to_dict(row) if row else None
    finally:
        db_session.remove()


def add_account(
    display_name: str,
    api_key: str,
    api_secret: str,
    capital_inr: float,
    broker: str = "zerodha",
    broker_client_id: str | None = None,
) -> dict:
    """Create a child account row (disabled by default). Raises ValueError on bad input."""
    if not display_name or not display_name.strip():
        raise ValueError("display_name is required")
    if not api_key or not api_secret:
        raise ValueError("api_key and api_secret are required")
    if capital_inr is None or float(capital_inr) <= 0:
        raise ValueError("capital_inr must be positive")
    try:
        row = BrokerAccount(
            display_name=display_name.strip(),
            broker=(broker or "zerodha").strip().lower(),
            broker_client_id=(broker_client_id or "").strip() or None,
            api_key_encrypted=encrypt_token(api_key.strip()),
            api_secret_encrypted=encrypt_token(api_secret.strip()),
            capital_inr=float(capital_inr),
            is_enabled=False,
        )
        db_session.add(row)
        db_session.commit()
        logger.info(f"Child broker account added: '{row.display_name}' (id={row.id}, disabled)")
        return _row_to_dict(row)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def update_account(account_id: int, **fields) -> dict | None:
    """Update editable fields. Credentials are write-only replacements.

    Accepted fields: display_name, broker_client_id, capital_inr, is_enabled,
    api_key, api_secret (re-encrypted), totp_secret (re-encrypted; empty string
    deletes), last_login_at.
    """
    try:
        row = db_session.query(BrokerAccount).filter_by(id=account_id).first()
        if not row:
            return None
        if "display_name" in fields and fields["display_name"]:
            row.display_name = str(fields["display_name"]).strip()
        if "broker_client_id" in fields:
            row.broker_client_id = (str(fields["broker_client_id"] or "")).strip() or None
        if "capital_inr" in fields and fields["capital_inr"] is not None:
            cap = float(fields["capital_inr"])
            if cap <= 0:
                raise ValueError("capital_inr must be positive")
            row.capital_inr = cap
        if "is_enabled" in fields and fields["is_enabled"] is not None:
            row.is_enabled = bool(fields["is_enabled"])
        if fields.get("api_key"):
            row.api_key_encrypted = encrypt_token(str(fields["api_key"]).strip())
        if fields.get("api_secret"):
            row.api_secret_encrypted = encrypt_token(str(fields["api_secret"]).strip())
        if "totp_secret" in fields:
            secret = str(fields["totp_secret"] or "").strip()
            row.totp_secret_encrypted = encrypt_token(secret) if secret else None
        if "last_login_at" in fields:
            row.last_login_at = fields["last_login_at"]
        row.updated_at = datetime.utcnow()
        db_session.commit()
        return _row_to_dict(row)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def delete_account(account_id: int) -> bool:
    """Remove the account row + its strategy rows. Caller revokes the auth row."""
    try:
        db_session.query(AccountStrategy).filter_by(account_id=account_id).delete()
        deleted = db_session.query(BrokerAccount).filter_by(id=account_id).delete()
        db_session.commit()
        if deleted:
            logger.info(f"Child broker account deleted (id={account_id})")
        return bool(deleted)
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def get_credentials(account_id: int) -> tuple[str, str, str] | None:
    """(api_key, api_secret, broker) decrypted — login-exchange use ONLY."""
    try:
        row = db_session.query(BrokerAccount).filter_by(id=account_id).first()
        if not row:
            return None
        return (
            decrypt_token(row.api_key_encrypted),
            decrypt_token(row.api_secret_encrypted),
            row.broker,
        )
    finally:
        db_session.remove()


def get_totp_secret(account_id: int) -> str | None:
    """Decrypted TOTP secret for code derivation, or None. Never expose via API."""
    try:
        row = db_session.query(BrokerAccount).filter_by(id=account_id).first()
        if not row or not row.totp_secret_encrypted:
            return None
        return decrypt_token(row.totp_secret_encrypted) or None
    finally:
        db_session.remove()


def get_strategies(account_id: int) -> list[str]:
    """Strategy names this account mirrors, sorted."""
    try:
        rows = db_session.query(AccountStrategy).filter_by(account_id=account_id).all()
        return sorted(r.strategy_name for r in rows)
    finally:
        db_session.remove()


def set_strategies(account_id: int, strategy_names: list[str]) -> list[str]:
    """Replace the account's strategy allow-list with ``strategy_names``."""
    cleaned = sorted({str(n).strip() for n in strategy_names if str(n).strip()})
    try:
        db_session.query(AccountStrategy).filter_by(account_id=account_id).delete()
        for name in cleaned:
            db_session.add(AccountStrategy(account_id=account_id, strategy_name=name))
        db_session.commit()
        return cleaned
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def accounts_for_strategy(strategy_name: str) -> list[dict]:
    """Enabled accounts that mirror ``strategy_name`` — the Phase-2 fan-out read."""
    try:
        rows = (
            db_session.query(BrokerAccount)
            .join(AccountStrategy, AccountStrategy.account_id == BrokerAccount.id)
            .filter(
                AccountStrategy.strategy_name == strategy_name,
                BrokerAccount.is_enabled.is_(True),
            )
            .order_by(BrokerAccount.id)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    finally:
        db_session.remove()
