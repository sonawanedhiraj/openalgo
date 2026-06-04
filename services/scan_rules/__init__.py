"""Code-backed scan rules for the in-house scanner (Stage 1.5 item 5).

Each module in this package defines one or more ``@scan_rule``-decorated
callables. Importing the package triggers each module's import side
effect — the decorator inserts the rule into ``services.scanner_service``'s
registry.

Add new rules by:

1. Creating ``services/scan_rules/<name>.py``.
2. Decorating the predicate with ``@scan_rule("<name>", "buy"|"sell", "<desc>")``.
3. Importing it here so ``import services.scan_rules`` registers it on
   first use.

The scanner service evaluates rules whose name matches the
``scan_definitions.rule_module`` column for each enabled definition.
"""

from services.scan_rules import (  # noqa: F401 — imports trigger registration
    fno_intraday_buy_20,
    fno_intraday_buy_chartink,
    fno_intraday_sell_20,
    fno_intraday_sell_chartink,
)
