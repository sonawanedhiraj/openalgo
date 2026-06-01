"""Project-root pytest configuration.

Two responsibilities:

1. Pin the project-root ``sandbox`` package in ``sys.modules`` before pytest
   starts adding test directories to ``sys.path``. Once it lives at
   ``test/sandbox/`` as a regular package (it does — see ``test/sandbox/``),
   any later ``import sandbox`` inside ``services.sandbox_service`` would
   resolve to the test subpackage and crash with
   ``ModuleNotFoundError: No module named 'sandbox.fund_manager'``. The cheap
   eager import here pins the right module in ``sys.modules`` so subsequent
   imports see the real one.

2. Provide a lazy ``_restx_loaded`` fixture for tests that need the openalgo
   ``restx_api`` / ``services`` circular import resolved before they call
   ``unittest.mock.patch("services.<name>.<fn>")`` against a module that
   participates in the cycle.

History (why this file used to do ``import restx_api`` at module top):

    services.place_order_service
      -> restx_api.schemas
         -> restx_api/__init__.py (loads every namespace at module level)
            -> .options_multiorder
               -> services.options_multiorder_service
                  -> from services.place_order_service import place_order
                     (cycle: place_order_service is still being initialised)

The eager ``import restx_api`` worked but booted the entire Flask app graph
(every namespace, every service) before any test was collected. That (a) made
``pytest`` hang when the live dev server was running and contending for SQLite
file locks during module import side effects, and (b) made a targeted
``pytest test/test_mode_service.py`` pay the full graph cost even though
that test doesn't touch the cycle at all.

Current approach: tests that need the cycle pre-resolved either import the
relevant ``services.X`` submodule(s) directly at the top of the test file
(see ``test/test_simplified_stock_engine_service.py`` and
``test/test_engine_veto_shadow.py``, both of which preface those imports
with ``import restx_api``) or depend on the ``_restx_loaded`` fixture below.
The fixture is session-scoped, lazy, and not autouse, so collection-only
runs and unrelated tests never trigger the heavy import.
"""

# Pin the project-root sandbox package in sys.modules — see docstring (1).
# Must happen at conftest module load, before pytest adds test/ to sys.path.
import sandbox  # noqa: F401, E402

import pytest


@pytest.fixture(scope="session")
def _restx_loaded():
    """Force-resolve the restx_api / services circular import once per session.

    Opt in by adding ``_restx_loaded`` to a test's fixture list when the test
    uses ``mock.patch("services.options_multiorder_service.<fn>")`` or any
    other dotted path that walks attributes of a service that participates in
    the cycle. Eager top-level ``import services.X`` in the test module is
    usually simpler — use this fixture when you need the *entire* graph
    settled (e.g. a test that builds up many patches dynamically).
    """
    import restx_api  # noqa: F401

    return restx_api
