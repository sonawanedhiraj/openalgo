"""Tests for the scan rule registry mechanics.

The ``@scan_rule`` decorator self-registers callables into
``services.scanner_service``'s process-global registry on import. We exercise:

* Registration / lookup via the decorator.
* The decorator rejects an invalid ``screener_type``.
* ``get_rule`` returns ``None`` for an unknown name.

Per-rule predicate behaviour lives with each rule's own test module
(e.g. ``test_fno_intraday_buy_chartink.py``), not here.
"""

from __future__ import annotations

import pytest

# Import the package once so the production rules self-register before any test
# touches the registry.
import services.scan_rules  # noqa: F401
from services import scanner_service

# ---------------------------------------------------------------------------
# registry mechanics
# ---------------------------------------------------------------------------


def test_decorator_registers_rule():
    """A rule decorated with @scan_rule is reachable via ``get_rule``."""

    @scanner_service.scan_rule("__test_register_rule", "buy", "fixture-only")
    def _rule(_bars, _indicators):
        return True

    try:
        assert scanner_service.get_rule("__test_register_rule") is _rule
        meta = scanner_service.all_rules()["__test_register_rule"]
        assert meta["screener_type"] == "buy"
        assert meta["description"] == "fixture-only"
        assert meta["fn"] is _rule
    finally:
        # Don't leak into other tests — the registry is process-global.
        scanner_service._rule_registry.pop("__test_register_rule", None)
        scanner_service._rule_metadata.pop("__test_register_rule", None)


def test_decorator_rejects_bad_screener_type():
    with pytest.raises(ValueError):

        @scanner_service.scan_rule("__test_bad_screener", "hold", "x")  # noqa: ARG001
        def _rule(_bars, _indicators):
            return True


def test_get_rule_missing_returns_none():
    assert scanner_service.get_rule("does_not_exist_anywhere") is None
