"""Persistent audit trail for scanner-driven cycles.

Before this table existed, scan history was only logged through the Cowork
bridge — so the moment the bridge was down (or the dev machine was off) we
lost the record of what fired, when, and why. The webhook handler now writes
``scan_cycle`` and ``cycle_heartbeat`` rows directly in the OpenAlgo process
itself, independent of any external observer.

Two design rules:

* **Fail-safe writes.** ``start_cycle`` / ``heartbeat`` / ``complete_cycle``
  must never raise into the order path. The service layer wraps each call in
  a try/except and logs warnings on DB failure instead of bubbling. Audit
  loss is preferable to a missed order or a webhook 500.
* **Heartbeats are progress markers, not aggregate metrics.** One row per
  stage transition is plenty for replay and bridge-down forensics; we don't
  emit a heartbeat per tick.
"""

import os
from datetime import datetime

import pytz
from sqlalchemy import Column, Integer, String, Text, create_engine
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


class ScanCycle(Base):
    __tablename__ = "scan_cycle"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(String(32), nullable=False, index=True)
    completed_at = Column(String(32), nullable=True)
    # 'chartink' | 'inhouse' | 'manual' | 'test'
    cycle_kind = Column(String(16), nullable=False)
    # JSON-encoded list of symbols. Text columns hold serialised JSON because
    # SQLite's JSON1 extension support is uneven across distros and a stringy
    # column is good enough for an audit table.
    screener_buy = Column(Text, nullable=True)
    screener_sell = Column(Text, nullable=True)
    # 'pending' | 'ok' | 'error' | 'skipped'
    post_status = Column(String(16), nullable=True)
    engine_response = Column(Text, nullable=True)
    error_payload = Column(Text, nullable=True)
    operator_intent = Column(String(16), nullable=True)
    effective_mode = Column(String(16), nullable=True)
    bridge_logged = Column(Integer, nullable=False, default=0)


class CycleHeartbeat(Base):
    __tablename__ = "cycle_heartbeat"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # No FK constraint — keep the heartbeat write path as cheap as possible
    # and don't let a missing parent block the audit.
    cycle_id = Column(Integer, nullable=False, index=True)
    # 'preflight' | 'scan_buy' | 'scan_sell' | 'post' | 'status_check'
    # | 'complete' | 'error'
    stage = Column(String(32), nullable=False)
    ts = Column(String(32), nullable=False, index=True)
    # 'started' | 'ok' | 'error' | 'skipped'
    status = Column(String(16), nullable=False)
    detail = Column(Text, nullable=True)


def init_db():
    """Create scan_cycle + cycle_heartbeat tables if missing. Idempotent."""
    from database.db_init_helper import init_db_with_logging

    init_db_with_logging(Base, engine, "Scan Cycle DB", logger)


def _now_iso() -> str:
    return datetime.now(pytz.timezone("Asia/Kolkata")).isoformat()


def _cycle_to_dict(row: ScanCycle) -> dict:
    return {
        "id": row.id,
        "started_at": row.started_at,
        "completed_at": row.completed_at,
        "cycle_kind": row.cycle_kind,
        "screener_buy": row.screener_buy,
        "screener_sell": row.screener_sell,
        "post_status": row.post_status,
        "engine_response": row.engine_response,
        "error_payload": row.error_payload,
        "operator_intent": row.operator_intent,
        "effective_mode": row.effective_mode,
        "bridge_logged": bool(row.bridge_logged),
    }


def _heartbeat_to_dict(row: CycleHeartbeat) -> dict:
    return {
        "id": row.id,
        "cycle_id": row.cycle_id,
        "stage": row.stage,
        "ts": row.ts,
        "status": row.status,
        "detail": row.detail,
    }
