"""Global pytest database isolation — the single load-bearing guard that makes
local/CI pytest *structurally incapable* of writing to the live databases.

Why this file exists
--------------------
Every ``database/*.py`` module binds its SQLAlchemy ``engine`` to
``os.getenv("DATABASE_URL")`` (and a handful of siblings — ``LOGS_``,
``LATENCY_``, ``HEALTH_``, ``SANDBOX_`` and ``HISTORIFY_DATABASE_PATH``) **at
import time**. ``.env`` points ``DATABASE_URL`` at the live ``db/openalgo.db``.
Before this file existed, DB isolation was *per-file opt-in*: a test rebound
``trade_journal_db`` to a temp SQLite only if its author remembered to copy the
``_rebind`` fixture. The moment a new engine-path test shipped without it
(``test/e2e/test_fno_flows.py``, 2026-06-11) it wrote real ``trade_journal``
rows to the live DB — the second occurrence of that exact pollution class
(phantom RELIANCE rows). See the retrospective at
``outputs/2026-06-11_retrospective_and_plan.md`` (Section 4).

This conftest kills the root cause: it redirects every DB env var to a throwaway
per-process temp directory *before any* ``database.*`` module is imported, so the
import-time bind lands on the temp DB no matter what. No future test can touch
the live DB, whether or not its author opts in.

The clobber trap (why we import ``utils.config`` first)
------------------------------------------------------
``utils/config.py`` runs ``load_dotenv(override=True)`` at module import. If that
ran *after* our redirect, it would re-read ``.env`` and stomp ``DATABASE_URL``
back to the live path — re-poisoning every module imported afterwards. We force
that one-time load to happen *now*, before we set our temp paths; the module is
then cached in ``sys.modules`` so a later ``import utils.config`` is a no-op and
our temp values survive. (Importing it here also loads ``API_KEY_PEPPER`` etc.
from ``.env``, which ``auth_db`` / ``user_db`` require at import.)

Three layers of protection
--------------------------
1. Unconditional env redirect (this module's top level) — the structural fix.
2. ``init_db`` on the temp DBs (the ``_isolate_databases`` session fixture) —
   creates the tables so the redirected DBs are usable. This is the specific
   ``settings_db`` breakage that made the earlier surgical fix stay per-file;
   fixing it here removes the reason isolation was ever kept opt-in.
3. A ``pytest_configure`` tripwire — fails collection loudly if ``DATABASE_URL``
   ever resolves to the live ``db/openalgo.db``, so even a future regression in
   layer 1 cannot start a run against the live DB.

Subdir conftests (e.g. ``test/e2e/conftest.py``) may still ``monkeypatch``-rebind
individual modules to their own per-test temp DBs — that layers cleanly on top of
this and is fully reverted after each test.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile

import pytest

# --------------------------------------------------------------------------- #
# Layer 1 — redirect every DB env var to a throwaway temp dir, BEFORE any
# ``database.*`` import binds its engine. Order is load-bearing (see docstring).
# --------------------------------------------------------------------------- #

# Capture the caller's DATABASE_URL FIRST — before dotenv runs — so it reflects
# only the real shell environment. The tripwire inspects this to catch a
# deliberate ``DATABASE_URL=sqlite:///db/openalgo.db pytest`` invocation. (After
# ``load_dotenv`` below, every run would show the live value from ``.env``, which
# is not a misconfiguration — so it must be read before that point.)
_INCOMING_DATABASE_URL = os.environ.get("DATABASE_URL", "")

# Force ``utils.config``'s one-time ``load_dotenv(override=True)`` to run NOW, so
# it cannot clobber our redirect later. The module is cached afterwards, so a
# later ``import utils.config`` is a no-op and our temp values survive. Done via
# ``import_module`` (a call, not an ``import`` statement) so the load-bearing
# ordering can't be "fixed" by an import sorter.
importlib.import_module("utils.config")

# Resolve the live DB path absolutely so the tripwire can compare against it
# without false-positiving on the temp DBs (which also end in ``openalgo.db``).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LIVE_DB_ABS = os.path.normcase(os.path.abspath(os.path.join(_REPO_ROOT, "db", "openalgo.db")))

# A single per-process temp dir so tables created in one test persist for the
# whole run (most fixtures expect the schema to stick around).
_TMP_DB_DIR = tempfile.mkdtemp(prefix="openalgo_pytest_")

_DB_ENV = {
    "DATABASE_URL": f"sqlite:///{_TMP_DB_DIR}/openalgo.db",
    "LOGS_DATABASE_URL": f"sqlite:///{_TMP_DB_DIR}/logs.db",
    "LATENCY_DATABASE_URL": f"sqlite:///{_TMP_DB_DIR}/latency.db",
    "HEALTH_DATABASE_URL": f"sqlite:///{_TMP_DB_DIR}/health.db",
    "SANDBOX_DATABASE_URL": f"sqlite:///{_TMP_DB_DIR}/sandbox.db",
    # historify is DuckDB and reads a bare filesystem path, not a sqlite:/// URL.
    "HISTORIFY_DATABASE_PATH": f"{_TMP_DB_DIR}/historify.duckdb",
}

for _k, _v in _DB_ENV.items():
    os.environ[_k] = _v


def _is_live_db(url: str) -> bool:
    """True iff ``url`` resolves to the live ``<repo>/db/openalgo.db`` file."""
    if not url:
        return False
    path = url
    for _prefix in ("sqlite:///", "sqlite://"):
        if path.startswith(_prefix):
            path = path[len(_prefix) :]
            break
    try:
        return os.path.normcase(os.path.abspath(path)) == _LIVE_DB_ABS
    except Exception:
        # Abspath can choke on exotic URLs; fall back to a conservative match.
        norm = url.replace("\\", "/").lower()
        return norm.endswith("/db/openalgo.db") or norm.endswith("///db/openalgo.db")


# --------------------------------------------------------------------------- #
# Layer 3 — tripwire. Runs after this module loads; a run can no longer *start*
# against the live DB even if layer 1 is ever broken or bypassed.
# --------------------------------------------------------------------------- #
def pytest_configure(config):  # noqa: ARG001
    resolved = os.environ.get("DATABASE_URL", "")
    if _is_live_db(resolved):
        pytest.exit(
            "FATAL: pytest DATABASE_URL resolves to the LIVE database "
            f"({resolved!r} -> {_LIVE_DB_ABS}). The test/conftest.py redirect did "
            "not take effect. Refusing to run — tests must never write to "
            "db/openalgo.db. Aborting collection.",
            returncode=2,
        )
    if _is_live_db(_INCOMING_DATABASE_URL):
        pytest.exit(
            "FATAL: DATABASE_URL was explicitly pointed at the LIVE database "
            f"({_INCOMING_DATABASE_URL!r}) before pytest started. Refusing to run "
            "against db/openalgo.db. Unset DATABASE_URL and let test/conftest.py "
            "redirect it to a temp DB. Aborting collection.",
            returncode=2,
        )


# --------------------------------------------------------------------------- #
# Layer 2 — create the schema in the redirected temp DBs. Best-effort per module
# so one import/init failure surfaces as a warning instead of bricking the suite;
# the redirect + tripwire above are the hard guarantees, this just makes the temp
# DBs usable (fixing the ``settings_db`` "tables don't exist" regression that
# kept isolation per-file last time).
# --------------------------------------------------------------------------- #
_INIT_TARGETS = [
    ("database.settings_db", "init_db"),  # the prior regression — keep first
    ("database.trade_journal_db", "init_db"),  # today's polluter
    ("database.sandbox_db", "init_db"),
    ("database.strategy_daily_intent_db", "init_db"),
    ("database.strategy_mode_db", "init_db"),
    ("database.strategy_mode_audit_db", "init_db"),  # issue #162 — flip audit trail
    ("database.strategy_runtime_override_db", "init_db"),
    ("database.daily_intent_db", "init_db"),
    ("database.data_health_db", "init_db"),
    ("database.option_liquidity_db", "init_db"),  # issue #583 — option-liquidity score
    ("database.market_calendar_db", "init_db"),  # issue #253 — holiday calendar
    ("database.signal_decision_db", "init_db"),
    ("database.scan_cycle_db", "init_db"),
    ("database.scanner_db", "init_db"),
    ("database.chartink_db", "init_db"),
    ("database.strategy_db", "init_db"),
    ("database.action_center_db", "init_db"),
    ("database.analyzer_db", "init_db"),
    ("database.apilog_db", "init_db"),
    ("database.backtest_db", "init_db"),
    ("database.journal_reflection_db", "init_db"),
    ("database.flow_db", "init_db"),
    ("database.leverage_db", "init_db"),
    ("database.telegram_db", "init_db"),
    ("database.auth_db", "init_db"),
    ("database.user_db", "init_db"),
    ("database.symbol", "init_db"),
    ("database.latency_db", "init_latency_db"),
    ("database.health_db", "init_health_db"),
    ("database.traffic_db", "init_logs_db"),
]


@pytest.fixture(scope="session", autouse=True)
def _isolate_databases():
    """Create every DB's schema in the redirected temp DBs once per session."""
    import importlib

    for module_path, init_name in _INIT_TARGETS:
        try:
            module = importlib.import_module(module_path)
            getattr(module, init_name)()
        except Exception as exc:  # pragma: no cover - defensive, per-module
            import warnings

            warnings.warn(
                f"test/conftest.py: could not init temp DB via "
                f"{module_path}.{init_name}(): {exc!r}",
                stacklevel=1,
            )
    yield


# --------------------------------------------------------------------------- #
# Layer 4 — no test may spawn the real ``claude`` CLI (issue #632).
#
# ``services/llm_review_client.py`` and ``services/llm_agent_client.py`` run
# ``claude -p`` through ``subprocess.run`` on an unpatched OS thread and block on
# ``thread.join(timeout_s + 5)``. The budgets are minutes (240s bare review, 600s
# tool-enabled agent), so a single unstubbed call does not fail a test — it
# *hangs the run* until ``pytest-timeout`` kills it, taking every remaining test
# with it. That is exactly how ``test_postmarket_review.py`` bricked the suite:
# ``run_review_for_date`` reaches the investigating agent only when the day's
# digest happens to carry violations, so the exposure is load-dependent and the
# file passes in isolation.
#
# The guard sits at the process boundary rather than on the ``invoke_*``
# functions themselves, for two reasons: it survives the function-local import in
# ``postmarket_investigation.investigate()`` (which rebinds the name on every
# call, so patching the *consumer* module is a no-op), and it leaves
# ``test_llm_review_client.py`` free to exercise the real ``invoke_claude_review``
# against its own ``subprocess.run`` fake.
#
# Every other subprocess (git, uv, node) passes straight through.
# --------------------------------------------------------------------------- #

_CLAUDE_CLI_TRIPWIRE = (
    "test/conftest.py: a test tried to spawn the real `claude` CLI ({argv0!r}).\n"
    "This blocks for minutes and hangs the whole pytest run rather than failing "
    "one test.\n"
    "Stub the seam instead — patch `services.llm_agent_client.invoke_claude_agent` "
    "and/or `services.llm_review_client.invoke_claude_review` (patch the *client* "
    "module, not the consumer: `postmarket_investigation.investigate()` imports the "
    "name inside the function body).\n"
    "To drive the client itself, patch `subprocess.run` as "
    "`test/test_llm_review_client.py` does."
)


def _looks_like_claude_cli(args) -> bool:
    """True when this argv would launch the Claude Code CLI."""
    if isinstance(args, (str, bytes, os.PathLike)):
        program = args
    else:
        try:
            program = next(iter(args))
        except (TypeError, StopIteration):
            return False
    if isinstance(program, bytes):
        program = program.decode("utf-8", "replace")
    elif isinstance(program, os.PathLike):
        program = os.fspath(program)
    if not isinstance(program, str):
        return False
    # Matches `claude`, `claude.cmd`, `claude.exe` and any absolute path to them,
    # which also covers a `CLAUDE_CMD` override pointing at a non-PATH install.
    return os.path.basename(program).lower().split(".")[0] == "claude"


@pytest.fixture(autouse=True)
def _block_real_claude_cli(monkeypatch):
    """Turn a real ``claude`` spawn into a loud, immediate failure."""
    import subprocess

    real_run = subprocess.run

    def guarded_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        if _looks_like_claude_cli(argv):
            argv0 = argv if isinstance(argv, str) else next(iter(argv), argv)
            message = _CLAUDE_CLI_TRIPWIRE.format(argv0=argv0)
            # Printed as well as raised: the callers wrap the spawn in a worker
            # thread behind a broad ``except Exception`` that degrades to an
            # "LLM unavailable" status, so the raise alone can be swallowed into
            # a green test. The stderr line names the offending test either way.
            current = os.environ.get("PYTEST_CURRENT_TEST", "?")
            print(f"[claude-cli-tripwire] {current}", file=sys.stderr)
            print(message, file=sys.stderr)
            raise AssertionError(message)
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    yield
