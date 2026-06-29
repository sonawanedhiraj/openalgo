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

from test.fixtures.mid_session import (  # noqa: F401
    at_09_30_cold_start,
    at_10_00_post_relogin,
    at_14_30_restart,
    at_15_10_stale_daily,
)
