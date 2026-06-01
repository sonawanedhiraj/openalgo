"""Persistence for sidecar market-intelligence snapshots.

Stage 1.7 introduces a minimal ``market_intel`` table to log periodic
classifier output (``kind='regime'``) for later reflection / journaling.
The schema is intentionally flat and free-form (``payload_json``) so we
can extend the sidecar to other intel types (e.g. options skew snapshots,
sector breadth detail, news flags) without further migrations.

The Stage 1.7 architecture spec (docs/architecture/AI_TRADING_BOT_DESIGN.md
§7.7.3) calls for a richer ``market_regime`` table with typed columns; we
defer that to the next iteration. Until then, regime snapshots live in
``market_intel`` with a ``kind`` discriminator — when the typed schema
lands, the writer flips over and old rows stay queryable as legacy intel.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import Column, DateTime, Integer, String, Text, create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import scoped_session, sessionmaker
from sqlalchemy.pool import NullPool

from utils.logging import get_logger

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL and "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, poolclass=NullPool, connect_args={"check_same_thread": False}
    )
else:
    engine = create_engine(DATABASE_URL, pool_size=50, max_overflow=100, pool_timeout=10)

db_session = scoped_session(sessionmaker(autocommit=False, autoflush=False, bind=engine))
Base = declarative_base()
Base.query = db_session.query_property()


class MarketIntel(Base):
    __tablename__ = "market_intel"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # IST timestamp at capture; stored as ISO 8601 string for portability
    # across the SQLite-only deployment target.
    captured_at = Column(String(32), nullable=False, index=True)
    # Discriminator for the row type: 'regime' for Stage 1.7. New intel
    # surfaces can land here without a migration.
    kind = Column(String(32), nullable=False, index=True)
    # Free-form JSON payload. Reader decodes based on ``kind``.
    payload_json = Column(Text, nullable=False)


def init_db():
    """Create the ``market_intel`` table if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Market Intel DB", logger)


def insert_intel(kind: str, payload_json: str, captured_at: str | None = None) -> int:
    """Append a row. Returns the new row id.

    ``captured_at`` defaults to "now in IST" as ISO 8601. Callers that
    need to backfill (e.g. a historical replay) can override it.
    """
    if captured_at is None:
        captured_at = datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()
    row = MarketIntel(captured_at=captured_at, kind=kind, payload_json=payload_json)
    db_session.add(row)
    db_session.commit()
    return int(row.id)


def latest_intel(kind: str) -> dict | None:
    """Return the most recent row of ``kind`` as a dict, or ``None``."""
    row = (
        db_session.query(MarketIntel)
        .filter(MarketIntel.kind == kind)
        .order_by(MarketIntel.id.desc())
        .first()
    )
    if row is None:
        return None
    return {
        "id": row.id,
        "captured_at": row.captured_at,
        "kind": row.kind,
        "payload_json": row.payload_json,
    }
