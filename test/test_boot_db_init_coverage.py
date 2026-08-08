"""Issue #575 — every table a module declares must be created by APP BOOT.

`database/strategy_mode_audit_db.py` shipped with an `init_strategy_mode_audit_db`
alias documented as "explicit alias for boot wiring callers" — and no caller. The
table was therefore absent from the live DB while `test/conftest.py` created it in
the temp DB, so `test_strategy_mode_audit_db.py` passed against a table production
never had. Every `flip_mode` attempt was dropped, including the live->sandbox flip
that remediated #561.

These tests are deliberately driven from **`app.py`'s own wiring**, never from
conftest's `_INIT_TARGETS`. Asserting against conftest would re-create the exact
blind spot: conftest is the thing that was masking the gap.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
APP_PY = REPO / "app.py"
DB_DIR = REPO / "database"

# Modules whose tables do NOT live in db/openalgo.db, or that app.py initialises
# conditionally by design. Each entry needs a reason — an unexplained exemption
# is how the next table goes missing.
_EXEMPT = {
    "health_db": "separate db/health.db",
    "latency_db": "separate db/latency.db",
    "traffic_db": "separate db/logs.db",
    "sandbox_db": "separate db/sandbox.db",
    "oauth_db": "initialised conditionally when the MCP OAuth blueprint loads",
}


def _app_source() -> str:
    return APP_PY.read_text(encoding="utf-8", errors="replace")


def _production_files() -> list[Path]:
    """app.py plus every service/blueprint.

    Not every table is created from app.py — `sector_follow_db`,
    `market_intel_db` and `master_contract_status_db` are legitimately
    initialised by the service or blueprint that owns them. The invariant is
    that SOMETHING in the production tree creates each table, not that app.py
    does it personally.
    """
    files = [APP_PY]
    for sub in ("services", "blueprints"):
        files.extend(sorted((REPO / sub).glob("*.py")))
    return files


def _table_creating_imports() -> set[str]:
    """{module_stem} for every database module a production file imports a
    table-creating symbol from.

    AST rather than regex: app.py's imports are multi-line parenthesised
    (``from database.x import (\\n    init_db as _y,\\n)``), which a line-oriented
    pattern silently misses — and a silent miss here would make this very test
    vacuous.

    A read-only import does NOT count: importing ``get_status`` creates no
    tables. Only ``init*`` / ``ensure_*`` symbols do.
    """
    wired: set[str] = set()
    for path in _production_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if not node.module.startswith("database."):
                continue
            stem = node.module.split(".", 1)[1]
            if any(a.name.startswith("init") or a.name.startswith("ensure_") for a in node.names):
                wired.add(stem)
    return wired


def _self_initialising_modules() -> set[str]:
    """{module_stem} for database modules that call ``init_db()`` at import time.

    A third legitimate mechanism alongside app.py wiring and service-owned
    init: `telegram_db` and `whatsapp_db` create their tables on module load
    (app.py even imports `get_bot_config` purely to trigger it). Detecting this
    keeps the coverage rule honest instead of papering over it with exemptions.
    """
    out: set[str] = set()
    for path in sorted(DB_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:  # pragma: no cover - defensive
            continue
        for node in tree.body:  # module level only, not inside a function
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id.startswith("init")
            ):
                out.add(path.stem)
    return out


def _modules_declaring_tables() -> dict[str, list[str]]:
    """{module_stem: [tablenames]} for every database/*.py declaring a table."""
    out: dict[str, list[str]] = {}
    for path in sorted(DB_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        tables = re.findall(r'__tablename__\s*=\s*["\'](\w+)["\']', text)
        if tables:
            out[path.stem] = tables
    return out


def test_strategy_mode_audit_is_initialised_at_boot():
    """The specific regression: app.py must wire the flip-audit table.

    Fails on the pre-fix tree — `strategy_mode_audit` appears nowhere in app.py.
    """
    assert "strategy_mode_audit" in _app_source(), (
        "app.py does not initialise strategy_mode_audit. The table is the ONLY "
        "history of who flipped a strategy and when (strategy_mode overwrites its "
        "row in place), so without this every flip is silently dropped — see #575."
    )


def test_every_declared_table_is_wired_into_app_boot():
    """No database module may declare a table app boot never creates.

    Drift here is invisible in production until something writes to the missing
    table and the failure is swallowed by a fail-open except block — exactly the
    #575 shape.
    """
    wired = _table_creating_imports() | _self_initialising_modules()
    missing = [
        f"{mod} (tables: {', '.join(tables)})"
        for mod, tables in _modules_declaring_tables().items()
        if mod not in _EXEMPT and mod not in wired
    ]
    assert not missing, (
        "These database modules declare tables that NO production code initialises:\n  "
        + "\n  ".join(missing)
        + "\nWire the init into app.py (or the owning service), or add an _EXEMPT "
        "entry stating WHY the table does not belong to boot."
    )


def test_exemptions_still_refer_to_real_modules():
    """A stale exemption silently re-opens the hole it was carved for."""
    known = set(_modules_declaring_tables())
    stale = sorted(set(_EXEMPT) - known)
    assert not stale, f"_EXEMPT lists modules that no longer declare tables: {stale}"


def test_boot_init_block_parses_and_calls_the_audit_init():
    """Guard the call, not just the mention.

    A bare import of the module would satisfy a substring check while creating
    nothing, so assert an actual call expression exists in app.py's AST.
    """
    tree = ast.parse(_app_source())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    audit_calls = {n for n in called if "strategy_mode_audit" in n}
    assert audit_calls, (
        "app.py mentions strategy_mode_audit but never CALLS its init — importing "
        "the module does not create the table."
    )


@pytest.mark.parametrize("table", ["strategy_mode", "strategy_mode_audit"])
def test_mode_tables_created_by_the_real_init(tmp_path, monkeypatch, table):
    """Run the modules' own init against a throwaway DB and assert the table lands.

    This exercises the production `init_db()` (not conftest's copy), so a broken
    `create_all` is caught even when app.py's wiring is correct.
    """
    import importlib

    from sqlalchemy import create_engine, inspect
    from sqlalchemy.orm import scoped_session, sessionmaker

    mod_name = (
        "database.strategy_mode_db"
        if table == "strategy_mode"
        else ("database.strategy_mode_audit_db")
    )
    module = importlib.import_module(mod_name)

    db_path = tmp_path / f"{table}.db"
    engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(module, "engine", engine)
    monkeypatch.setattr(module, "db_session", scoped_session(sessionmaker(bind=engine)))

    module.init_db()

    assert table in inspect(engine).get_table_names(), (
        f"{mod_name}.init_db() did not create {table!r}"
    )
