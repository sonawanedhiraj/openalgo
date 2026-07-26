"""Make the time-pinned mid-session fixtures visible to integration tests.

Pytest 9.x rejects ``pytest_plugins = [...]`` in non-top-level conftests
("Defining 'pytest_plugins' in a non-top-level conftest is no longer
supported"). Instead, import the fixture functions directly here — a
``@pytest.fixture``-decorated function imported into a conftest is treated
the same as one defined there, so tests in ``test/integration/`` can request
them by name without needing to import them in the test module (which would
shadow the parameter names and trip ruff F811).

The ``# noqa: F401`` markers say "yes, the import is the point" — the
fixtures are deliberately re-exported, not unused.
"""

from __future__ import annotations

import pytest

from test.fixtures.mid_session import (  # noqa: F401
    at_09_30_cold_start,
    at_10_00_post_relogin,
    at_14_30_restart,
    at_15_10_stale_daily,
)


@pytest.fixture(autouse=True)
def _clear_strategy_runtime_overrides():
    """Wipe strategy_runtime_override rows this test left behind (issue #472).

    The phase3/phase4 integration tests write pause/kill_switch rows into the
    session-scoped shared temp DB and most never clean up. The rows then leak
    into every later-collected test that counts or reads overrides globally
    (test_kill_switch_db_first's ``assert len(rows) == 1`` saw 14). Clearing the
    table after each test keeps the shared DB at its empty baseline.
    """
    yield
    try:
        from database.strategy_runtime_override_db import (
            StrategyRuntimeOverride,
            db_session,
        )

        try:
            db_session.query(StrategyRuntimeOverride).delete()
            db_session.commit()
        except Exception:
            db_session.rollback()
        finally:
            db_session.remove()
    except Exception:
        # Best-effort: an import failure here must not mask the test's own result.
        pass
