"""Operator-editable settings for the intraday_pullback_top2 strategy (issue #394).

One row per strategy_name holding the UI-editable overrides (base capital, sizing mode, the
no-trade and afternoon windows). The service layers these over the config_snapshot.json defaults
at construction and at the 09:00 daily reset, so an edit takes effect next session without a
restart. Non-editable strategy-logic params (bands, vol mult, stop, filters) stay in the JSON.

Mirrors the futures_follow_db boilerplate (NullPool sqlite engine on DATABASE_URL, scoped_session,
db_session.remove() in every finally).
"""

import os
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, Integer, String, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
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

TABLE_NAME = "intraday_pullback_config"
_EDITABLE = (
    "base_capital",
    "sizing_mode",
    "no_trade_start",
    "no_trade_end",
    "afternoon_start",
    "afternoon_end",
    "trade_side",
)


class IntradayPullbackConfig(Base):
    __tablename__ = TABLE_NAME

    id = Column(Integer, primary_key=True)
    strategy_name = Column(String(64), unique=True, nullable=False)
    base_capital = Column(Float, nullable=True)
    sizing_mode = Column(String(12), nullable=True)  # fixed | compound | capped
    no_trade_start = Column(String(5), nullable=True)  # HH:MM
    no_trade_end = Column(String(5), nullable=True)
    afternoon_start = Column(String(5), nullable=True)
    afternoon_end = Column(String(5), nullable=True)
    # both | long_only | short_only (issue #509). NULL = both (backtested default).
    trade_side = Column(String(12), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    updated_by = Column(String(64), nullable=True)


def _row_to_dict(row: IntradayPullbackConfig) -> dict:
    return {
        "strategy_name": row.strategy_name,
        "base_capital": row.base_capital,
        "sizing_mode": row.sizing_mode,
        "no_trade_start": row.no_trade_start,
        "no_trade_end": row.no_trade_end,
        "afternoon_start": row.afternoon_start,
        "afternoon_end": row.afternoon_end,
        "trade_side": row.trade_side,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "updated_by": row.updated_by,
    }


def _ensure_columns():
    """Idempotent additive migration (create_all never ALTERs an existing table).

    Mirrors ``intraday_pullback_db._ensure_columns``. Without this, a column
    added to the model after the table was first created is silently missing
    on a live install and every read of it raises OperationalError.
    """
    wanted = {
        "trade_side": "VARCHAR(12)",  # issue #509
    }
    try:
        with engine.connect() as conn:
            existing = {r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({TABLE_NAME})")}
            for col, ddl in wanted.items():
                if col not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {TABLE_NAME} ADD COLUMN {col} {ddl}")
                    logger.info("added column %s.%s", TABLE_NAME, col)
    except Exception as e:  # noqa: BLE001
        logger.exception("intraday_pullback_config _ensure_columns failed: %s", e)


def init_db():
    """Create the intraday_pullback_config table if it does not exist (idempotent)."""
    try:
        Base.metadata.create_all(bind=engine)
        _ensure_columns()
        logger.info("%s table ready", TABLE_NAME)
    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to init %s table: %s", TABLE_NAME, e)


init_intraday_pullback_config_db = init_db  # long-name alias for app.py init list


def get_config(strategy_name: str) -> dict | None:
    """Return the editable-settings row for the strategy, or None if unset."""
    try:
        row = IntradayPullbackConfig.query.filter_by(strategy_name=strategy_name).first()
        return _row_to_dict(row) if row else None
    except Exception as e:  # noqa: BLE001
        logger.exception("get_config failed: %s", e)
        return None
    finally:
        db_session.remove()


def delete_config(strategy_name: str) -> bool:
    """Delete the editable-settings row (revert to config_snapshot.json defaults)."""
    try:
        deleted = IntradayPullbackConfig.query.filter_by(strategy_name=strategy_name).delete()
        db_session.commit()
        return bool(deleted)
    except Exception as e:  # noqa: BLE001
        logger.exception("delete_config failed: %s", e)
        db_session.rollback()
        return False
    finally:
        db_session.remove()


def set_config(strategy_name: str, *, updated_by: str = "ui", **fields) -> dict | None:
    """Upsert the editable settings. Only whitelisted keys in ``fields`` are written.

    Returns the resulting row dict, or None on failure. Unknown keys are ignored.
    """
    try:
        row = IntradayPullbackConfig.query.filter_by(strategy_name=strategy_name).first()
        if row is None:
            row = IntradayPullbackConfig(strategy_name=strategy_name)
            db_session.add(row)
        for key in _EDITABLE:
            if key in fields and fields[key] is not None:
                setattr(row, key, fields[key])
        row.updated_by = updated_by
        db_session.commit()
        return _row_to_dict(row)
    except Exception as e:  # noqa: BLE001
        logger.exception("set_config failed: %s", e)
        db_session.rollback()
        return None
    finally:
        db_session.remove()
