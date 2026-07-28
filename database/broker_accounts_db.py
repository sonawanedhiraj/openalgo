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


class MultiAccountSettings(Base):
    """Single-row UI-configurable multi-account settings (issue #484).

    DB row wins; the env vars ``MULTI_ACCOUNT_ENABLED`` / ``PRIMARY_BOOK_CAPITAL``
    are only the FIRST-READ seed (and fallback while no row exists) so a fresh
    install still defaults off and an env-armed install carries over exactly.
    Consulted at fire time — UI changes apply immediately, no restart.
    """

    __tablename__ = "multi_account_settings"

    id = Column(Integer, primary_key=True, default=1)
    enabled = Column(Boolean, nullable=False, default=False)
    primary_book_capital = Column(Float, nullable=False, default=1_000_000.0)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)


def _env_enabled_default() -> bool:
    return os.getenv("MULTI_ACCOUNT_ENABLED", "false").strip().lower() in ("true", "1", "yes")


def _env_capital_default() -> float:
    try:
        value = float(os.getenv("PRIMARY_BOOK_CAPITAL", "1000000"))
        return value if value > 0 else 1_000_000.0
    except (TypeError, ValueError):
        return 1_000_000.0


def get_multi_account_settings() -> dict:
    """The settings row, seeded from env on first read. Fail-safe: on any
    error returns the env-derived values (never blocks a page or a fan-out
    consult)."""
    try:
        row = db_session.query(MultiAccountSettings).filter_by(id=1).first()
        if row is None:
            row = MultiAccountSettings(
                id=1,
                enabled=_env_enabled_default(),
                primary_book_capital=_env_capital_default(),
                updated_by="env-seed",
            )
            db_session.add(row)
            db_session.commit()
            logger.info(
                f"multi_account_settings seeded from env (enabled={row.enabled}, "
                f"primary_book_capital={row.primary_book_capital})"
            )
        return {
            "enabled": bool(row.enabled),
            "primary_book_capital": float(row.primary_book_capital),
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "updated_by": row.updated_by,
        }
    except Exception:
        db_session.rollback()
        logger.exception("multi_account_settings read failed — env fallback")
        return {
            "enabled": _env_enabled_default(),
            "primary_book_capital": _env_capital_default(),
            "updated_at": None,
            "updated_by": None,
        }
    finally:
        db_session.remove()


def set_multi_account_settings(
    enabled: bool | None = None,
    primary_book_capital: float | None = None,
    updated_by: str = "ui",
) -> dict:
    """Update the settings row (partial). Raises ValueError on bad capital."""
    if primary_book_capital is not None and float(primary_book_capital) <= 0:
        raise ValueError("primary_book_capital must be positive")
    get_multi_account_settings()  # ensure the row exists (env seed)
    try:
        row = db_session.query(MultiAccountSettings).filter_by(id=1).first()
        if enabled is not None:
            row.enabled = bool(enabled)
        if primary_book_capital is not None:
            row.primary_book_capital = float(primary_book_capital)
        row.updated_at = datetime.utcnow()
        row.updated_by = updated_by
        db_session.commit()
        logger.info(
            f"multi_account_settings updated by {updated_by}: enabled={row.enabled}, "
            f"primary_book_capital={row.primary_book_capital}"
        )
        return {
            "enabled": bool(row.enabled),
            "primary_book_capital": float(row.primary_book_capital),
            "updated_at": row.updated_at.isoformat(),
            "updated_by": row.updated_by,
        }
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


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
    """Allow-list row: this account mirrors this strategy.

    ``capital_override_inr`` (issue #486): optional per-strategy sizing capital
    for THIS account — NULL means "use the account's base ``capital_inr``".
    Living on the selection row means the override exists only while the
    strategy is selected and disappears with a deselect.
    """

    __tablename__ = "account_strategies"

    account_id = Column(Integer, primary_key=True, nullable=False)
    strategy_name = Column(String(64), primary_key=True, nullable=False)
    capital_override_inr = Column(Float, nullable=True)
    # issue #490: derivative mirrors that scale to 0 lots round UP to 1 lot
    # instead of skipping (child mirrors 1-lot parent trades at 1 lot).
    min_one_lot = Column(Boolean, nullable=False, default=False)


def _migrate_add_capital_override() -> None:
    """Idempotent boot-time ALTER for installs created before issue #486
    (same pattern as trade_journal.ltp_at_signal)."""
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            cols = [
                row[1]
                for row in conn.execute(text("PRAGMA table_info(account_strategies)")).fetchall()
            ]
            if cols and "capital_override_inr" not in cols:
                conn.execute(
                    text("ALTER TABLE account_strategies ADD COLUMN capital_override_inr FLOAT")
                )
                conn.commit()
                logger.info("account_strategies.capital_override_inr column added (issue #486)")
            if cols and "min_one_lot" not in cols:
                conn.execute(
                    text(
                        "ALTER TABLE account_strategies "
                        "ADD COLUMN min_one_lot BOOLEAN NOT NULL DEFAULT 0"
                    )
                )
                conn.commit()
                logger.info("account_strategies.min_one_lot column added (issue #490)")
    except Exception:
        logger.exception("capital_override_inr migration failed")


def init_db():
    """Create the broker_accounts/account_strategies tables if missing. Idempotent."""
    try:
        Base.metadata.create_all(bind=engine)
        _migrate_add_capital_override()
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


def get_strategy_settings(account_id: int) -> list[dict]:
    """Selected strategies with their per-strategy capital overrides, sorted.

    ``capital_override_inr`` is None when the account's base capital applies.
    """
    try:
        rows = db_session.query(AccountStrategy).filter_by(account_id=account_id).all()
        return sorted(
            (
                {
                    "strategy_name": r.strategy_name,
                    "capital_override_inr": (
                        float(r.capital_override_inr)
                        if r.capital_override_inr is not None
                        else None
                    ),
                    "min_one_lot": bool(r.min_one_lot),
                }
                for r in rows
            ),
            key=lambda x: x["strategy_name"],
        )
    finally:
        db_session.remove()


def set_strategies(
    account_id: int,
    strategy_names: list[str],
    capital_overrides: dict[str, float] | None = None,
    min_one_lot: dict[str, bool] | None = None,
) -> list[str]:
    """Replace the account's strategy allow-list with ``strategy_names``.

    ``capital_overrides`` maps strategy_name → per-strategy capital (₹) for
    THIS account; a strategy absent from the map (or mapped to None) uses the
    account's base capital. Overrides are only stored for SELECTED strategies —
    deselecting deletes the row and its override with it (issue #486).
    Raises ValueError on a non-positive override.
    """
    cleaned = sorted({str(n).strip() for n in strategy_names if str(n).strip()})
    overrides = capital_overrides or {}
    lot_flags = min_one_lot or {}
    for name, value in overrides.items():
        if value is not None and float(value) <= 0:
            raise ValueError(f"capital override for {name} must be positive")
    try:
        db_session.query(AccountStrategy).filter_by(account_id=account_id).delete()
        for name in cleaned:
            override = overrides.get(name)
            db_session.add(
                AccountStrategy(
                    account_id=account_id,
                    strategy_name=name,
                    capital_override_inr=float(override) if override is not None else None,
                    min_one_lot=bool(lot_flags.get(name, False)),
                )
            )
        db_session.commit()
        return cleaned
    except Exception:
        db_session.rollback()
        raise
    finally:
        db_session.remove()


def accounts_for_strategy(strategy_name: str) -> list[dict]:
    """Enabled accounts that mirror ``strategy_name`` — the Phase-2 fan-out read.

    Each dict carries ``capital_override_inr`` (this strategy's override for
    that account, or None) so the fan-out can size per (account, strategy).
    """
    try:
        rows = (
            db_session.query(
                BrokerAccount, AccountStrategy.capital_override_inr, AccountStrategy.min_one_lot
            )
            .join(AccountStrategy, AccountStrategy.account_id == BrokerAccount.id)
            .filter(
                AccountStrategy.strategy_name == strategy_name,
                BrokerAccount.is_enabled.is_(True),
            )
            .order_by(BrokerAccount.id)
            .all()
        )
        result = []
        for account_row, override, lot_flag in rows:
            d = _row_to_dict(account_row)
            d["capital_override_inr"] = float(override) if override is not None else None
            d["min_one_lot"] = bool(lot_flag)
            result.append(d)
        return result
    finally:
        db_session.remove()
