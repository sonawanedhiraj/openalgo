"""Project-root pytest configuration.

Pre-resolve openalgo's restx_api / services circular import so individual
tests can use unittest.mock.patch("services.<name>.<fn>") without hitting
"AttributeError: module 'services' has no attribute '<name>'" during
attribute-walk on the dotted path.

The cycle:
    services.place_order_service
      -> restx_api.schemas
         -> restx_api/__init__.py (loads every namespace at module level)
            -> .options_multiorder
               -> services.options_multiorder_service
                  -> from services.place_order_service import place_order
                     (cycle: place_order_service is still being initialised)

Normal openalgo execution doesn't trip this because all services.* imports
happen lazily inside Flask request handlers, by which time the cycle has
been broken by app boot. Tests that mock services.place_order_service must
force the full import graph to settle before any mock.patch() call.

Importing restx_api here once, at conftest load time (before any test
module is imported), walks the cycle in an order that lets every submodule
finish initialising. Subsequent `import services.X` statements in test
files are then cheap no-ops that succeed.
"""

# Importing restx_api triggers initialisation of every namespace in
# restx_api/__init__.py. This is heavier than strictly necessary, but it is
# the same import graph that runs during `uv run app.py`, so we know it works
# in this project and we don't have to enumerate every transitive dependency.
import restx_api  # noqa: F401
