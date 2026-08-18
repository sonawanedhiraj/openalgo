"""Database package.

Importing this package registers the process-wide SQLite pragma listener in
``sqlite_tuning`` (issue #633). It lives here so that ANY ``database.*`` import
arms it — including the ~30 ``broker/*/database/master_contract_db.py`` engines,
which import from this package and perform the largest writes in the app.
"""

from database import sqlite_tuning as _sqlite_tuning  # noqa: F401  (import for side effect)
