"""
OpenAlgo ↔ Claude Code Bridge Server
=====================================
A FastAPI server that lets Cowork (Claude Desktop) invoke Claude Code CLI
for automated bug fixing, testing, and app management.

Start:  uv run python bridge/server.py
Runs:   http://127.0.0.1:5001

Cowork calls this via browser JS:
    fetch("http://127.0.0.1:5001/fix-bug", { method: "POST", ... })

Claude Code runs with --dangerously-skip-permissions so it can edit files
and run commands without prompting. Safe for single-user self-hosted setup.
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from datetime import time as dtime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

import pytz
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Project root (where CLAUDE.md lives)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OPENALGO_APP = PROJECT_ROOT / "app.py"
LOG_DIR = PROJECT_ROOT / "log"
ERRORS_LOG = LOG_DIR / "errors.jsonl"
# Per-request audit trail (2026-06-11 retrospective item #6). One JSON line per
# request: timestamp, endpoint, body summary, response status. The bridge can
# mutate code, run pytest, and restart OpenAlgo — every invocation must leave a
# trace so a surprise mid-market edit/restart can be traced to its trigger.
ACCESS_LOG = LOG_DIR / "bridge_access.jsonl"

# Market-hours guard (2026-06-11 retrospective item #6). /fix-bug and
# /restart-app are refused (409) during the NSE/BSE continuous session
# (09:15–15:30 IST, weekdays). Auto-fix on the live host during market hours is
# what triggered the 2026-06-11 pytest-pollution + high-risk mid-session restart.
IST = pytz.timezone("Asia/Kolkata")
MARKET_OPEN_IST = dtime(9, 15)
MARKET_CLOSE_IST = dtime(15, 30)

# Claude CLI command — adjust if claude is installed elsewhere
CLAUDE_CMD = "claude"

# Default tools Claude Code is allowed to use without prompting
ALLOWED_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Bash(uv run *)",
    "Bash(cd *)",
    "Bash(python *)",
    "Bash(pytest *)",
    "Bash(npm *)",
    "Bash(cat *)",
    "Bash(head *)",
    "Bash(tail *)",
    "Bash(ls *)",
    "Bash(git *)",
]

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bridge")

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------


class BridgeStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"


class BridgeState:
    def __init__(self):
        self.status: BridgeStatus = BridgeStatus.IDLE
        self.current_task: str | None = None
        self.started_at: float | None = None
        self.history: list[dict] = []  # last 20 results

    def start_task(self, task_name: str):
        self.status = BridgeStatus.BUSY
        self.current_task = task_name
        self.started_at = time.time()

    def finish_task(self, result: dict):
        self.status = BridgeStatus.IDLE
        elapsed = time.time() - (self.started_at or time.time())
        entry = {
            "task": self.current_task,
            "timestamp": datetime.now().isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "success": result.get("success", False),
            "summary": result.get("summary", ""),
        }
        self.history.append(entry)
        if len(self.history) > 20:
            self.history.pop(0)
        self.current_task = None
        self.started_at = None


state = BridgeState()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="OpenAlgo Claude Bridge",
    description="Bridge between Cowork and Claude Code CLI",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # localhost only — safe for self-hosted
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def access_log_middleware(request: Request, call_next):
    """Write one audit line per request to log/bridge_access.jsonl (item #6).

    Captures timestamp, endpoint, a short body summary, and the response status.
    Reading the body here caches it on the request, so downstream handlers still
    parse it normally. Audit failures never affect the request/response.
    """
    started = time.time()
    body_summary: Any = None
    try:
        raw = await request.body()
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    # Summarize keys + truncated values; bridge bodies are small
                    # localhost JSON, but keep it bounded regardless.
                    body_summary = {
                        k: (v if not isinstance(v, str) else v[:120])
                        for k, v in list(parsed.items())[:12]
                    }
                else:
                    body_summary = {"_type": type(parsed).__name__, "_len": len(raw)}
            except json.JSONDecodeError:
                body_summary = {"_bytes": len(raw)}
    except Exception:  # noqa: BLE001 — never block the request on summary failure
        body_summary = {"_error": "body-read-failed"}

    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        _write_access_log(
            {
                "ts": datetime.now(IST).isoformat(),
                "method": request.method,
                "endpoint": request.url.path,
                "query": str(request.url.query) or None,
                "client": request.client.host if request.client else None,
                "body_summary": body_summary,
                "status": status_code,
                "elapsed_ms": int((time.time() - started) * 1000),
            }
        )


# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class FixBugRequest(BaseModel):
    error_message: str
    log_lines: str | None = None
    file_path: str | None = None
    traceback: str | None = None
    additional_context: str | None = None


class RunTestsRequest(BaseModel):
    test_path: str | None = "test/"
    specific_test: str | None = None
    fix_failures: bool = False


class RunCommandRequest(BaseModel):
    prompt: str
    allowed_tools: list[str] | None = None


class RestartAppRequest(BaseModel):
    kill_existing: bool = True


# Stage 1: LLM veto layer — /review-signal request shape.
class ReviewCandidate(BaseModel):
    symbol: str
    source: str
    # Side the engine armed: 'BUY' (long) | 'SELL' (short). Optional for
    # backward compatibility with callers that predate the field — the prompt
    # renders 'unknown' when absent. The ``source`` string can't be trusted to
    # imply direction (both chartink legs carry "...intraday_buy").
    direction: str | None = None
    candidate_at: str


class ReviewContext(BaseModel):
    positions_count: int | None = None
    positions_summary: str | None = None
    pnl_today: float | None = None
    trades_today: int | None = None
    max_trades_today: int | None = None
    nifty_pct: float | None = None
    india_vix: float | None = None
    # Stage 1.7: full 5-dim regime snapshot. Free-form dict so the
    # bridge doesn't have to track every field the classifier might
    # add — keys today are trend / volatility / breadth / time_of_day
    # / sector_leaders / sector_leader_concentration / top_sector_pct.
    regime_snapshot: dict | None = None


class ReviewSignalRequest(BaseModel):
    candidate: ReviewCandidate
    context: ReviewContext = Field(default_factory=ReviewContext)


# ---------------------------------------------------------------------------
# Core: Run Claude Code CLI
# ---------------------------------------------------------------------------


def build_claude_command(prompt: str, extra_tools: list[str] | None = None) -> list[str]:
    """Build the claude CLI command with appropriate flags."""
    tools = ALLOWED_TOOLS + (extra_tools or [])
    cmd = [
        CLAUDE_CMD,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--allowedTools",
        ",".join(tools),
    ]
    return cmd


# Live openalgo.db gets cloned to this path before each subprocess spawn so
# the subprocess inherits the schema + reference data but writes nowhere
# near production. Pure :memory: doesn't work because some tests read from
# auth/settings tables that no test fixture creates.
_LIVE_DB_PATH = PROJECT_ROOT / "db" / "openalgo.db"
_BRIDGE_TEST_DB_PATH = PROJECT_ROOT / "db" / "openalgo_test_bridge.db"
_MEMORY_DB_URL = "sqlite:///:memory:"


def _refresh_bridge_test_db() -> None:
    """Clone the live openalgo.db into the bridge's throwaway test DB.

    Uses SQLite's online backup API so the copy is consistent even if the
    live DB is being written to. The destination is overwritten on every
    call, which guarantees each /fix-bug or /run-tests run starts from a
    clean state — no accumulated synthetic rows across runs.
    """
    if not _LIVE_DB_PATH.exists():
        # Fresh checkout / first boot before the live DB has been
        # initialised. Skip the copy; the subprocess will create the
        # destination on demand via SQLAlchemy.
        return
    _BRIDGE_TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if _BRIDGE_TEST_DB_PATH.exists():
        _BRIDGE_TEST_DB_PATH.unlink()
    src = sqlite3.connect(str(_LIVE_DB_PATH))
    dst = sqlite3.connect(str(_BRIDGE_TEST_DB_PATH))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def build_subprocess_env() -> dict[str, str]:
    """Build an env dict for the spawned `claude -p` subprocess.

    The bridge's /fix-bug + /run-tests prompts ask Claude to run
    `uv run pytest …`. Many tests (notably test_simplified_stock_engine_service)
    instantiate real services without monkeypatching the journal DB, so calls
    like trade_journal_service.record_entry land in the live db/openalgo.db
    via the module-level engine bound to DATABASE_URL at import time. On
    2026-06-01 a fix-bug run wrote 12 synthetic rows into trade_journal while
    real positions were open, poisoning the EOD watchdog + rehydrate logic.

    Override DATABASE_URL to a throwaway clone of the live DB (refreshed on
    every spawn) so the subprocess sees the live schema + reference data
    but every write lands in the clone. Sandbox / latency / health get
    :memory: — no test depends on their schema today, and it keeps the
    clone footprint to one file.
    """
    env = os.environ.copy()
    # Main openalgo.db — hosts trade_journal, signal_decision, daily_intent,
    # auth, settings, orders, etc. Cloned per-spawn (see _refresh_bridge_test_db).
    env["DATABASE_URL"] = f"sqlite:///{_BRIDGE_TEST_DB_PATH.as_posix()}"
    # Sandbox virtual capital DB — routed to :memory: so sandbox tests
    # don't leak virtual orders into db/sandbox.db.
    env["SANDBOX_DATABASE_URL"] = _MEMORY_DB_URL
    # Latency + health monitors fall back to db/latency.db and db/health.db;
    # isolate those too for symmetry (no tests rely on them today).
    env["LATENCY_DATABASE_URL"] = _MEMORY_DB_URL
    env["HEALTH_DATABASE_URL"] = _MEMORY_DB_URL
    return env


# ---------------------------------------------------------------------------
# Market-hours guard (item #6)
# ---------------------------------------------------------------------------


def _now_ist() -> datetime:
    """Current time in IST. Indirected so tests can monkeypatch the clock."""
    return datetime.now(IST)


def _is_market_hours(now: datetime | None = None) -> bool:
    """True iff ``now`` (default: live IST) is inside the cash session.

    09:15–15:30 IST inclusive, Monday–Friday. Holidays are NOT modelled — the
    guard errs on the side of refusing (a holiday weekday still blocks), which
    is the safe direction for a destructive auto-fix / restart.
    """
    now = now or _now_ist()
    if now.tzinfo is None:
        now = IST.localize(now)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    return MARKET_OPEN_IST <= now.timetz().replace(tzinfo=None) <= MARKET_CLOSE_IST


def _assert_not_market_hours(endpoint: str) -> None:
    """Raise HTTPException(409) if called during market hours."""
    if _is_market_hours():
        raise HTTPException(
            status_code=409,
            detail=(
                f"{endpoint} is refused during market hours "
                f"(09:15–15:30 IST, weekdays). Code edits, pytest, and app "
                f"restarts on the live trading host must wait until the close. "
                f"See the 2026-06-11 retrospective (plan item #6)."
            ),
        )


# ---------------------------------------------------------------------------
# Scoped pytest for /fix-bug (item #6)
# ---------------------------------------------------------------------------

_PY_PATH_RE = re.compile(r"[\w./\\-]+\.py")


def _relevant_test_files(request: "FixBugRequest") -> list[str]:
    """Best-effort map from the reported error to the test files that cover it.

    Scans ``file_path`` and any ``.py`` paths in the traceback. For a source
    file ``services/foo_service.py`` it looks for ``test/test_foo_service.py``
    (and ``test/test_foo_service*.py``); a referenced test file is included
    directly. Returns existing repo-relative paths only, deduped and capped.
    """
    raw_paths: list[str] = []
    if request.file_path:
        raw_paths.append(request.file_path)
    if request.traceback:
        raw_paths.extend(_PY_PATH_RE.findall(request.traceback))

    found: list[str] = []
    seen: set[str] = set()
    test_dir = PROJECT_ROOT / "test"
    for raw in raw_paths:
        stem = Path(raw.replace("\\", "/")).stem
        if not stem:
            continue
        if stem.startswith("test_"):
            # A test file was referenced directly — run it if it exists.
            matches = list(test_dir.rglob(f"{stem}.py"))
        else:
            matches = list(test_dir.rglob(f"test_{stem}.py"))
            matches += list(test_dir.rglob(f"test_{stem}_*.py"))
        for m in matches:
            rel = m.relative_to(PROJECT_ROOT).as_posix()
            if rel not in seen:
                seen.add(rel)
                found.append(rel)
    return found[:10]


def _scoped_pytest_command(request: "FixBugRequest") -> str:
    """The pytest invocation embedded in the /fix-bug verify step.

    Never the full ``pytest test/`` suite (2026-06-11 item #6: a full-suite run
    on the live host is what polluted trade_journal). Prefer the test files that
    cover the reported error; fall back to the fast deterministic unit marker.
    """
    targets = _relevant_test_files(request)
    if targets:
        return "uv run pytest " + " ".join(targets) + " -q --maxfail=5"
    return "uv run pytest -m unit -q --maxfail=5"


# ---------------------------------------------------------------------------
# Access-log audit trail (item #6)
# ---------------------------------------------------------------------------


def _write_access_log(entry: dict) -> None:
    """Append one JSON line to log/bridge_access.jsonl. Best-effort, never raises."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with ACCESS_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")
    except Exception:  # noqa: BLE001 — audit logging must never break a request
        logger.warning("bridge access-log write failed", exc_info=True)


async def run_claude(prompt: str, task_name: str, extra_tools: list[str] | None = None) -> dict:
    """
    Run Claude Code CLI and return structured result.

    Uses -p (print mode) for non-interactive execution.
    Uses --allowedTools to pre-approve file and shell operations.
    Uses --output-format json for structured output.
    """
    if state.status == BridgeStatus.BUSY:
        return {
            "success": False,
            "error": f"Bridge is busy with: {state.current_task}",
            "status": "busy",
        }

    state.start_task(task_name)
    cmd = build_claude_command(prompt, extra_tools)

    logger.info(f"Starting task: {task_name}")
    logger.info(f"Command: {' '.join(cmd[:4])}...")

    # Isolate the subprocess from live SQLite DBs — see build_subprocess_env()
    # docstring for the full rationale. Without this, a fix-bug or run-tests
    # invocation can write synthetic rows into db/openalgo.db's trade_journal
    # and poison the EOD watchdog + position rehydrate logic.
    _refresh_bridge_test_db()
    subprocess_env = build_subprocess_env()
    try:
        # Run claude -p as subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
            env=subprocess_env,
        )

        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(),
            timeout=300,  # 5 minute timeout
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        # Try to parse JSON output
        try:
            claude_result = json.loads(stdout)
        except json.JSONDecodeError:
            claude_result = {"raw_output": stdout}

        success = process.returncode == 0
        result = {
            "success": success,
            "return_code": process.returncode,
            "result": claude_result,
            "stderr": stderr if stderr else None,
            "task": task_name,
            "elapsed_seconds": round(time.time() - (state.started_at or time.time()), 1),
        }

        # Extract summary from Claude's response
        if isinstance(claude_result, dict) and "result" in claude_result:
            result["summary"] = claude_result["result"][:500]
        elif isinstance(claude_result, dict) and "raw_output" in claude_result:
            result["summary"] = claude_result["raw_output"][:500]

        logger.info(f"Task completed: {task_name} (success={success})")
        state.finish_task(result)
        return result

    except TimeoutError:
        result = {
            "success": False,
            "error": "Claude Code timed out after 5 minutes",
            "task": task_name,
        }
        state.finish_task(result)
        return result

    except FileNotFoundError:
        result = {
            "success": False,
            "error": (
                f"Claude CLI not found. Make sure '{CLAUDE_CMD}' is in your PATH. "
                "Install with: npm install -g @anthropic-ai/claude-code"
            ),
            "task": task_name,
        }
        state.finish_task(result)
        return result

    except Exception as e:
        result = {
            "success": False,
            "error": str(e),
            "task": task_name,
        }
        state.finish_task(result)
        return result


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/")
async def root():
    """Health check and status."""
    return {
        "service": "OpenAlgo Claude Bridge",
        "status": state.status.value,
        "current_task": state.current_task,
        "project_root": str(PROJECT_ROOT),
        "recent_tasks": len(state.history),
    }


@app.get("/status")
async def get_status():
    """Detailed status including task history."""
    return {
        "status": state.status.value,
        "current_task": state.current_task,
        "started_at": state.started_at,
        "elapsed": round(time.time() - state.started_at, 1) if state.started_at else None,
        "history": state.history[-5:],  # last 5 tasks
    }


@app.post("/fix-bug")
async def fix_bug(request: FixBugRequest):
    """
    Send a bug to Claude Code for fixing.

    Cowork calls this when it detects an error in OpenAlgo logs.
    Claude Code will read the relevant files, understand the error,
    edit the code to fix it, and optionally run tests.
    """
    # Item #6: never auto-fix the live host during the cash session.
    _assert_not_market_hours("/fix-bug")

    # Build a rich prompt with all available context
    prompt_parts = [
        f"Fix this bug in the OpenAlgo project at {PROJECT_ROOT}.",
        f"\nError: {request.error_message}",
    ]

    if request.file_path:
        prompt_parts.append(f"\nFile: {request.file_path}")

    if request.traceback:
        prompt_parts.append(f"\nFull traceback:\n```\n{request.traceback}\n```")

    if request.log_lines:
        prompt_parts.append(f"\nRelevant log lines:\n```\n{request.log_lines}\n```")

    if request.additional_context:
        prompt_parts.append(f"\nAdditional context: {request.additional_context}")

    # Item #6: scope the verify step to the test files that cover the reported
    # error (or the fast `-m unit` marker), never the full `pytest test/` suite
    # — a full-suite run on the live host is what polluted trade_journal.
    scoped_pytest = _scoped_pytest_command(request)
    prompt_parts.append(
        "\nInstructions:"
        "\n1. Read the CLAUDE.md for project context"
        "\n2. Read the failing file and understand the bug"
        "\n3. Fix the code"
        f"\n4. Run ONLY the relevant scoped tests to verify: {scoped_pytest}"
        "\n   Do NOT run the full `pytest test/` suite — it is slow and, on the"
        "\n   live host, risks writing to the live databases."
        "\n5. Summarize what you changed and why"
    )

    prompt = "\n".join(prompt_parts)
    return await run_claude(prompt, "fix-bug")


@app.post("/run-tests")
async def run_tests(request: RunTestsRequest):
    """
    Run tests via Claude Code.

    Claude will run the tests, analyze failures, and optionally fix them.
    """
    if request.specific_test:
        test_target = request.specific_test
    else:
        test_target = request.test_path

    if request.fix_failures:
        prompt = (
            f"Run the tests in {test_target} using 'uv run pytest {test_target} -v'. "
            f"If any tests fail, analyze the failures and fix the code. "
            f"Then re-run the tests to confirm they pass. "
            f"Project root: {PROJECT_ROOT}"
        )
    else:
        prompt = (
            f"Run the tests in {test_target} using 'uv run pytest {test_target} -v'. "
            f"Report the results — how many passed, failed, and any error details. "
            f"Do NOT fix anything, just report. "
            f"Project root: {PROJECT_ROOT}"
        )

    return await run_claude(prompt, "run-tests")


@app.post("/run")
async def run_custom(request: RunCommandRequest):
    """
    Run any custom prompt via Claude Code.

    This is the general-purpose endpoint for anything that doesn't fit
    the specific endpoints above.
    """
    return await run_claude(
        request.prompt,
        "custom-command",
        extra_tools=request.allowed_tools,
    )


@app.post("/restart-app")
async def restart_app(request: RestartAppRequest):
    """
    Restart the OpenAlgo application.

    Kills existing process (if requested) and starts fresh.
    Note: This runs uv run app.py in the background.
    """
    # Item #6: a restart wipes in-memory engine state (open positions, stops,
    # EOD timers). Refuse during the cash session — the 2026-06-11 mid-market
    # restart was high-risk and only tolerable because both strategies were
    # in sandbox that day.
    _assert_not_market_hours("/restart-app")

    if state.status == BridgeStatus.BUSY:
        raise HTTPException(status_code=409, detail="Bridge is busy")

    state.start_task("restart-app")

    try:
        if request.kill_existing:
            # Kill any existing OpenAlgo process on port 5000
            try:
                kill_result = await asyncio.create_subprocess_exec(
                    "powershell",
                    "-Command",
                    "Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | "
                    "ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await kill_result.communicate()
                logger.info("Killed existing process on port 5000")
                await asyncio.sleep(2)  # Wait for port release
            except Exception as e:
                logger.warning(f"Could not kill existing process: {e}")

        # Start OpenAlgo in background
        process = await asyncio.create_subprocess_exec(
            "uv",
            "run",
            "app.py",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(PROJECT_ROOT),
        )

        # Wait a bit and check if it started
        await asyncio.sleep(5)

        if process.returncode is None:
            # Process is still running = good
            result = {
                "success": True,
                "message": "OpenAlgo restarted successfully",
                "pid": process.pid,
            }
        else:
            result = {
                "success": False,
                "message": f"OpenAlgo exited with code {process.returncode}",
            }

        state.finish_task(result)
        return result

    except Exception as e:
        result = {"success": False, "error": str(e)}
        state.finish_task(result)
        return result


@app.get("/read-errors")
async def read_errors(last_n: int = 10):
    """
    Read the last N entries from errors.jsonl.

    Cowork can use this to check for new errors without reading
    the file directly.
    """
    if not ERRORS_LOG.exists():
        return {"errors": [], "count": 0}

    lines = ERRORS_LOG.read_text(encoding="utf-8").strip().split("\n")
    recent = lines[-last_n:] if len(lines) >= last_n else lines

    errors = []
    for line in recent:
        try:
            errors.append(json.loads(line))
        except json.JSONDecodeError:
            errors.append({"raw": line})

    return {"errors": errors, "count": len(errors), "total": len(lines)}


@app.get("/engine-status")
async def engine_status():
    """
    Proxy the simplified engine status endpoint.

    Saves Cowork from needing to call OpenAlgo directly.
    """
    import urllib.request

    try:
        req = urllib.request.Request("http://127.0.0.1:5000/chartink/simplified-engine/api/status")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            return {"success": True, "data": data}
    except Exception as e:
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# /review-signal — Stage 1 LLM veto layer
# ---------------------------------------------------------------------------

# Hard wall-clock budget for one veto decision. The OpenAlgo-side service uses
# a slightly larger timeout so the bridge wins the race on a hung Claude call.
REVIEW_CLAUDE_TIMEOUT_SECONDS = 25.0


REVIEW_PROMPT_TEMPLATE = """You are reviewing a candidate trading signal for an Indian F&O intraday strategy.

CANDIDATE:
- Symbol: {symbol}
- Source: {source}
- Direction: {direction}        # BUY = long entry | SELL = short entry
- Time: {candidate_at}

OPERATOR CONTEXT:
- Current positions: {positions_count} ({positions_summary})
- P&L today: ₹{pnl_today}
- Trades today: {trades_today}/{max_trades_today}

MARKET CONTEXT (today):
- NIFTY return: {nifty_pct}%
- India VIX: {india_vix}

REGIME SNAPSHOT:
{regime_block}

Decide whether the operator should take this signal. Use the stated Direction above — do NOT infer it from the Source string. Be conservative: skip when the broader market regime conflicts with the signal's stated Direction. For a BUY (long): skip on a -1% NIFTY day with elevated VIX, or when the stock's sector is bottom-3 today while leadership is concentrated elsewhere. For a SELL (short) the conflict is inverted: a strongly bullish regime / broad green breadth works against a short, while a weak/bearish regime is favourable for it. Also skip when the operator is already near their daily trade limit.

Respond with a short reasoning paragraph followed by a final JSON block. The JSON block must be the LAST thing in your response and must contain exactly these keys:

{{
  "decision": "take" | "skip",
  "reasoning": "1-2 sentence summary",
  "confidence": 0.0 to 1.0
}}
"""


def _format_regime_block(regime: dict | None) -> str:
    """Render the regime snapshot dict as a small bullet list.

    Returns ``"- unavailable"`` when the classifier didn't return data
    (cache miss or empty universe) so the LLM treats it as missing
    rather than zero. Only includes fields that are actually populated.
    """
    if not regime or not isinstance(regime, dict):
        return "- unavailable"
    lines: list[str] = []
    for key in ("trend", "volatility", "breadth", "time_of_day"):
        val = regime.get(key)
        if val:
            lines.append(f"- {key}: {val}")
    leaders = regime.get("sector_leaders") or []
    if leaders:
        lines.append(f"- sector_leaders (top 3): {', '.join(leaders)}")
    concentration = regime.get("sector_leader_concentration")
    if concentration is not None:
        lines.append(
            f"- sector_leader_concentration: {float(concentration):.2f} "
            "(>0.5 dominant, <0.2 broad rotation)"
        )
    top_sector_pct = regime.get("top_sector_pct") or {}
    if top_sector_pct:
        rendered = ", ".join(f"{sym} {float(pct):+.2f}%" for sym, pct in top_sector_pct.items())
        lines.append(f"- top sectors today: {rendered}")
    if not lines:
        return "- unavailable"
    return "\n".join(lines)


def _format_review_prompt(candidate: ReviewCandidate, ctx: ReviewContext) -> str:
    """Interpolate the prompt template.

    Operator-state fields (positions, trades) render missing values as
    ``unknown``. The three macro/portfolio numbers (nifty_pct, india_vix,
    pnl_today) instead render as ``unavailable`` — they're best-effort live
    fetches and the LLM should treat their absence as "data not retrievable",
    not "value is zero".
    """

    def _or_unknown(value: Any) -> str:
        if value is None:
            return "unknown"
        return str(value)

    def _or_unavailable(value: Any) -> str:
        if value is None:
            return "unavailable"
        return str(value)

    return REVIEW_PROMPT_TEMPLATE.format(
        symbol=candidate.symbol,
        source=candidate.source,
        direction=_or_unknown(candidate.direction),
        candidate_at=candidate.candidate_at,
        positions_count=_or_unknown(ctx.positions_count),
        positions_summary=_or_unknown(ctx.positions_summary),
        pnl_today=_or_unavailable(ctx.pnl_today),
        trades_today=_or_unknown(ctx.trades_today),
        max_trades_today=_or_unknown(ctx.max_trades_today),
        nifty_pct=_or_unavailable(ctx.nifty_pct),
        india_vix=_or_unavailable(ctx.india_vix),
        regime_block=_format_regime_block(ctx.regime_snapshot),
    )


def _extract_decision_block(text: str) -> dict | None:
    """Find the LAST balanced ``{...}`` block in ``text`` containing ``"decision"``.

    Walks every ``{`` position, follows brace depth with quote/escape awareness,
    and keeps the rightmost successfully-parsed block whose object has a
    ``decision`` key. Returns ``None`` if no such block is present.
    """
    if not text:
        return None

    candidates: list[dict] = []
    n = len(text)
    for start_match in re.finditer(r"\{", text):
        start = start_match.start()
        depth = 0
        in_string = False
        escape = False
        for i in range(start, n):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    block = text[start : i + 1]
                    try:
                        parsed = json.loads(block)
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict) and "decision" in parsed:
                        candidates.append(parsed)
                    break
    return candidates[-1] if candidates else None


def _failsafe_response(reasoning: str, started: float, session_id: str = "", raw: str = "") -> dict:
    """Build a ``decision='take'`` response used whenever review can't complete."""
    return {
        "decision": "take",
        "reasoning": reasoning,
        "confidence": 0.0,
        "latency_ms": int((time.time() - started) * 1000),
        "claude_session_id": session_id,
        "raw_output": raw,
    }


async def _invoke_claude_for_review(prompt: str) -> tuple[str, str]:
    """Run ``claude -p`` against the prompt and return ``(model_text, session_id)``.

    ``model_text`` is the prose Claude emitted (the ``result`` field of the JSON
    envelope, or raw stdout when the envelope can't be parsed). ``session_id``
    is the Claude Code session id when available, else the empty string.

    Raises ``asyncio.TimeoutError`` if the subprocess exceeds
    ``REVIEW_CLAUDE_TIMEOUT_SECONDS`` — caller is responsible for fail-safe.
    """
    cmd = [
        CLAUDE_CMD,
        "-p",
        prompt,
        "--output-format",
        "json",
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    try:
        stdout_bytes, _stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=REVIEW_CLAUDE_TIMEOUT_SECONDS
        )
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        raise

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    session_id = ""
    model_text = stdout
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, dict):
            if "result" in envelope and isinstance(envelope["result"], str):
                model_text = envelope["result"]
            session_id = str(envelope.get("session_id", "") or "")
    except json.JSONDecodeError:
        pass

    return model_text, session_id


@app.post("/review-signal")
async def review_signal(request: ReviewSignalRequest):
    """Run a single LLM veto check against a candidate trading signal.

    Fail-safe semantics: any error path (timeout, parse failure, validation
    error, subprocess crash) returns ``decision='take'`` with a short
    ``reasoning`` tag so the calling service can ship orders even when the
    reviewer is unavailable. The OpenAlgo-side service is responsible for
    enforcement (shadow vs active vs off).
    """
    started = time.time()
    prompt = _format_review_prompt(request.candidate, request.context)

    try:
        model_text, session_id = await _invoke_claude_for_review(prompt)
    except TimeoutError:
        return _failsafe_response("timeout", started)
    except FileNotFoundError:
        return _failsafe_response("claude_cli_missing", started)
    except Exception as exc:
        logger.exception("review-signal: subprocess failure")
        return _failsafe_response(f"subprocess_error:{type(exc).__name__}", started)

    decision_block = _extract_decision_block(model_text)
    if decision_block is None:
        return _failsafe_response("parse_failed", started, session_id, model_text)

    decision = decision_block.get("decision")
    reasoning = decision_block.get("reasoning", "")
    confidence_raw = decision_block.get("confidence", 0.0)

    if decision not in ("take", "skip"):
        return _failsafe_response("parse_failed", started, session_id, model_text)

    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        return _failsafe_response("parse_failed", started, session_id, model_text)

    if not (0.0 <= confidence <= 1.0):
        return _failsafe_response("parse_failed", started, session_id, model_text)

    if not isinstance(reasoning, str):
        reasoning = str(reasoning)

    return {
        "decision": decision,
        "reasoning": reasoning,
        "confidence": confidence,
        "latency_ms": int((time.time() - started) * 1000),
        "claude_session_id": session_id,
        "raw_output": model_text,
    }


# ---------------------------------------------------------------------------
# /reflect — Stage 2 part 2 journal reflection
# ---------------------------------------------------------------------------

# Wall-clock budget for a single reflection call. Reflection is heavier than
# /review-signal because it ships the day's full journal + screener + backtest
# data, so the LLM has more to read before it answers. The OpenAlgo-side
# service uses a slightly larger HTTP read timeout.
REFLECT_CLAUDE_TIMEOUT_SECONDS = 180.0


class ReflectRequest(BaseModel):
    prompt: str
    model: str | None = None


@app.post("/reflect")
async def reflect(request: ReflectRequest):
    """Relay a free-form reflection prompt to Claude Code and return the response.

    Unlike ``/review-signal`` this endpoint does no JSON-block extraction;
    the caller (services.journal_reflection_service) parses the reply itself.
    The response shape is intentionally minimal: ``response`` is the prose the
    model emitted, ``model`` is the model id when Claude reports one, and
    ``latency_ms`` measures the bridge-side wall clock.

    Errors surface as a non-200 status with a ``detail`` body so the caller
    fails loudly. We do NOT fail-safe here — reflection is forensic, not
    order-routing, so a silent fallback would just paper over a real outage.
    """
    started = time.time()
    if not request.prompt or not request.prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")

    cmd = [
        CLAUDE_CMD,
        "-p",
        request.prompt,
        "--output-format",
        "json",
    ]
    if request.model:
        cmd.extend(["--model", request.model])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=REFLECT_CLAUDE_TIMEOUT_SECONDS
            )
        except TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            raise HTTPException(status_code=504, detail="claude_timeout") from None
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="claude_cli_missing") from None
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("reflect: subprocess failure")
        raise HTTPException(
            status_code=500, detail=f"subprocess_error:{type(exc).__name__}"
        ) from exc

    if process.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=500,
            detail=f"claude_returncode_{process.returncode}: {stderr[:300]}",
        )

    stdout = stdout_bytes.decode("utf-8", errors="replace")
    model_text = stdout
    model_used = request.model or ""
    try:
        envelope = json.loads(stdout)
        if isinstance(envelope, dict):
            if "result" in envelope and isinstance(envelope["result"], str):
                model_text = envelope["result"]
            # Newer claude CLI reports model id under different keys; accept
            # any of them.
            for key in ("model", "model_id", "modelName"):
                if envelope.get(key):
                    model_used = str(envelope[key])
                    break
    except json.JSONDecodeError:
        pass

    return {
        "response": model_text,
        "model": model_used,
        "latency_ms": int((time.time() - started) * 1000),
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting OpenAlgo Claude Bridge on port 5001")
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"Claude CLI: {CLAUDE_CMD}")
    logger.info(f"Allowed tools: {len(ALLOWED_TOOLS)} pre-approved")
    logger.info("=" * 60)

    uvicorn.run(app, host="127.0.0.1", port=5001, log_level="info")
