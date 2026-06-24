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
    text,
)
from sqlalchemy.exc import IntegrityError, OperationalError
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
    # Tier-3: parameter overrides (nullable — NULL means use rule defaults).
    parameters_json = Column(Text, nullable=True)
    # Tier-3: FK reference to the source row this was cloned from (unenforced at
    # the SQLite layer); NULL = code-backed / built-in definition.
    parent_definition_id = Column(Integer, nullable=True)

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


def _migrate_scan_definitions() -> None:
    """Add Tier-3 columns to scan_definitions on existing databases (idempotent).

    ``Base.metadata.create_all`` only creates tables that don't exist yet; it
    never alters existing tables. This function runs ``ALTER TABLE … ADD COLUMN``
    for each new column, catching the ``OperationalError`` that SQLite raises
    when the column already exists so that the function is safe to call on both
    fresh and pre-existing databases.
    """
    new_columns = [
        ("parameters_json", "TEXT"),
        ("parent_definition_id", "INTEGER"),
    ]
    try:
        with engine.begin() as conn:
            for col_name, col_type in new_columns:
                try:
                    conn.execute(
                        text(f"ALTER TABLE scan_definitions ADD COLUMN {col_name} {col_type}")  # noqa: S608
                    )
                except OperationalError as exc:
                    if "duplicate column name" in str(exc).lower():
                        pass  # already present — idempotent
                    else:
                        raise
    except Exception:
        logger.debug("_migrate_scan_definitions: table may not exist yet (fresh DB)", exc_info=True)


def init_db():
    """Create scan_definitions + scan_results tables if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Scanner DB", logger)
    _migrate_scan_definitions()


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
        "parameters_json": row.parameters_json,
        "parent_definition_id": row.parent_definition_id,
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


def _encode_parameters_json(parameters_json: "str | dict | None") -> "str | None":
    """Encode parameters_json to a JSON string (or None)."""
    if parameters_json is None:
        return None
    if isinstance(parameters_json, dict):
        return json.dumps(parameters_json)
    return parameters_json  # already a str


def clone_definition(
    source_id: int,
    new_name: str,
    parameters_json: "str | dict | None" = None,
) -> int:
    """Clone a scan_definition row, returning the new definition's id.

    The clone inherits screener_type, rule_module, and expression_json from
    source_id; enabled defaults to True; parent_definition_id is set to source_id.
    Raises ValueError if source_id does not exist.
    Raises IntegrityError on duplicate new_name.
    """
    sess = db_session()
    try:
        source = sess.query(ScanDefinition).filter(ScanDefinition.id == source_id).first()
        if source is None:
            raise ValueError(f"source_id {source_id} does not exist")
        now = _now_iso()
        clone = ScanDefinition(
            name=new_name,
            screener_type=source.screener_type,
            expression_json=source.expression_json,
            rule_module=source.rule_module,
            enabled=1,
            created_at=now,
            updated_at=now,
            parameters_json=_encode_parameters_json(parameters_json),
            parent_definition_id=source_id,
        )
        sess.add(clone)
        sess.commit()
        return clone.id
    except Exception:
        sess.rollback()
        raise
    finally:
        db_session.remove()


def update_definition_params(
    definition_id: int,
    parameters_json: "str | dict | None",
) -> None:
    """Update parameters_json on a cloned (non-code-backed) definition.

    Raises ValueError if definition_id does not exist OR if the row has
    parent_definition_id IS NULL (code-backed rows are immutable by policy).
    """
    sess = db_session()
    try:
        row = sess.query(ScanDefinition).filter(ScanDefinition.id == definition_id).first()
        if row is None:
            raise ValueError(f"definition_id {definition_id} does not exist")
        if row.parent_definition_id is None:
            raise ValueError(
                f"definition_id {definition_id} is code-backed (parent_definition_id is NULL)"
                " and cannot be modified"
            )
        row.parameters_json = _encode_parameters_json(parameters_json)
        row.updated_at = _now_iso()
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        db_session.remove()


def delete_definition(definition_id: int) -> None:
    """Hard-delete a cloned definition.

    Raises ValueError if the row does not exist OR if parent_definition_id IS NULL
    (code-backed rows cannot be deleted via the UI).
    Raises ValueError with 'has children' message if other rows have
    parent_definition_id = definition_id.
    """
    sess = db_session()
    try:
        row = sess.query(ScanDefinition).filter(ScanDefinition.id == definition_id).first()
        if row is None:
            raise ValueError(f"definition_id {definition_id} does not exist")
        if row.parent_definition_id is None:
            raise ValueError(
                f"definition_id {definition_id} is code-backed (parent_definition_id is NULL)"
                " and cannot be deleted"
            )
        children_count = (
            sess.query(ScanDefinition)
            .filter(ScanDefinition.parent_definition_id == definition_id)
            .count()
        )
        if children_count > 0:
            raise ValueError(f"definition_id {definition_id} has children and cannot be deleted")
        sess.delete(row)
        sess.commit()
    except Exception:
        sess.rollback()
        raise
    finally:
        db_session.remove()
