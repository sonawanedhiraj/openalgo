"""Pytest plugin registration for the ``test/integration/`` suite.

Loads the time-pinned mid-session fixtures from ``test/fixtures/mid_session.py``
as a plugin so any integration test can ask for ``at_14_30_restart``,
``at_15_10_stale_daily``, ``at_09_30_cold_start`` or ``at_10_00_post_relogin``
without importing the symbols (which would trip ruff F811 against the test
function parameter of the same name).
"""

from __future__ import annotations

pytest_plugins = ["test.fixtures.mid_session"]
