import os
import sys

# === Live-DB isolation: must be set BEFORE any database.* import.
# database.telegram_db resolves DATABASE_URL at import time. This file calls
# update_bot_config({"broadcast_enabled": False}) at MODULE IMPORT time, so
# merely *collecting* the test would otherwise flip the operator's live
# broadcast setting off in db/openalgo.db. See fix/test-live-db-isolation.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

import database.telegram_db as _tdb

# The module engine uses NullPool, so a bare sqlite ":memory:" loses its
# tables between operations (each NullPool connection is a fresh empty DB).
# Rebind to a private default-pool in-memory engine whose connection persists,
# mirroring test/test_market_intel_db.py and test/test_orphaned_apikey.py.
_engine = create_engine("sqlite:///:memory:")
_tdb.db_session = scoped_session(
    sessionmaker(autocommit=False, autoflush=False, bind=_engine)
)
_tdb.Base.query = _tdb.db_session.query_property()
_tdb.Base.metadata.create_all(_engine)

from database.telegram_db import get_bot_config, update_bot_config

# Test saving with broadcast disabled
print("Testing broadcast_enabled field:")
print("1. Setting broadcast_enabled to False")
update_bot_config({"broadcast_enabled": False})

config = get_bot_config()
print(f"2. After save - broadcast_enabled: {config.get('broadcast_enabled')}")
print(f"   Type: {type(config.get('broadcast_enabled'))}")

# Try again with True
print("\n3. Setting broadcast_enabled to True")
update_bot_config({"broadcast_enabled": True})

config = get_bot_config()
print(f"4. After save - broadcast_enabled: {config.get('broadcast_enabled')}")
print(f"   Type: {type(config.get('broadcast_enabled'))}")
