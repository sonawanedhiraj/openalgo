"""Persistence for Stage 1.5 scanner definitions and their run results.

Two tables, both intentionally minimal so we can iterate on the scanner
service (item 5) without paying schema migration cost up-front.

* ``scan_definitions`` — the catalogue of what we know how to scan for.
  Definitions can be declarative (``expression_json`` carries the rule
  body) or code-backed (``rule_module`` points at a Python module that
  exposes a ``match`` callable). Exactly one of the two should be set in
  practice; the schema allows either so we don't pre-commit to one
  encoding before the scanner service lands.

* ``scan_results`` — one row per scan invocation. ``source`` distinguishes
  Chartink-supplied symbol lists (the legacy path), in-house scans (item 5
  output), shadow comparisons (item 7), and operator-uploaded ad-hoc lists.

Lives in the main ``openalgo.db`` next to ``daily_intent`` and the scan
cycle audit tables — querying scanner output against today's intent and
cycle history is the bread-and-butter use case.
"""

import json
import os
from datetime import datetime, timedelta
from typing import Any

import pytz
from sqlalchemy import (
    Column,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.exc import IntegrityError
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


class ScanDefinition(Base):
    __tablename__ = "scan_definitions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False)
    screener_type = Column(String(8), nullable=False)  # 'buy' | 'sell'
    expression_json = Column(Text, nullable=False)
    rule_module = Column(String(256), nullable=True)
    enabled = Column(Integer, nullable=False, default=1)
    created_at = Column(String(40), nullable=False)
    updated_at = Column(String(40), nullable=False)

    __table_args__ = (
        UniqueConstraint("name", name="uq_scan_definitions_name"),
        Index("idx_scan_definitions_enabled", "enabled"),
    )


class ScanResult(Base):
    __tablename__ = "scan_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    scan_definition_id = Column(Integer, ForeignKey("scan_definitions.id"), nullable=False)
    run_at = Column(String(40), nullable=False)
    symbols = Column(Text, nullable=False)  # JSON array
    source = Column(String(16), nullable=False)  # 'chartink' | 'inhouse' | 'shadow' | 'manual'
    posted_to_engine = Column(Integer, nullable=False, default=0)
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_scan_results_run_at", "run_at"),
        Index("idx_scan_results_source", "source"),
        Index("idx_scan_results_definition", "scan_definition_id"),
    )


def init_db():
    """Create scan_definitions + scan_results tables if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Scanner DB", logger)


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _definition_to_dict(row: ScanDefinition) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "screener_type": row.screener_type,
        "expression_json": row.expression_json,
        "rule_module": row.rule_module,
        "enabled": bool(row.enabled),
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


def _result_to_dict(row: ScanResult) -> dict[str, Any]:
    return {
        "id": row.id,
        "scan_definition_id": row.scan_definition_id,
        "run_at": row.run_at,
        "symbols": json.loads(row.symbols) if row.symbols else [],
        "source": row.source,
        "posted_to_engine": bool(row.posted_to_engine),
        "notes": row.notes,
    }
