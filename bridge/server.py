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
import subprocess
import sys
import time
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
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

# Claude CLI command — adjust if claude is installed elsewhere
CLAUDE_CMD = "claude"

# Default tools Claude Code is allowed to use without prompting
ALLOWED_TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(uv run *)", "Bash(cd *)", "Bash(python *)",
    "Bash(pytest *)", "Bash(npm *)", "Bash(cat *)",
    "Bash(head *)", "Bash(tail *)", "Bash(ls *)",
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
        self.current_task: Optional[str] = None
        self.started_at: Optional[float] = None
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

# ---------------------------------------------------------------------------
# Request/Response models
# ---------------------------------------------------------------------------


class FixBugRequest(BaseModel):
    error_message: str
    log_lines: Optional[str] = None
    file_path: Optional[str] = None
    traceback: Optional[str] = None
    additional_context: Optional[str] = None


class RunTestsRequest(BaseModel):
    test_path: Optional[str] = "test/"
    specific_test: Optional[str] = None
    fix_failures: bool = False


class RunCommandRequest(BaseModel):
    prompt: str
    allowed_tools: Optional[list[str]] = None


class RestartAppRequest(BaseModel):
    kill_existing: bool = True


# Stage 1: LLM veto layer — /review-signal request shape.
class ReviewCandidate(BaseModel):
    symbol: str
    source: str
    candidate_at: str


class ReviewContext(BaseModel):
    positions_count: Optional[int] = None
    positions_summary: Optional[str] = None
    pnl_today: Optional[float] = None
    trades_today: Optional[int] = None
    max_trades_today: Optional[int] = None
    nifty_pct: Optional[float] = None
    india_vix: Optional[float] = None


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
        "-p", prompt,
        "--output-format", "json",
        "--allowedTools", ",".join(tools),
    ]
    return cmd


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

    try:
        # Run claude -p as subprocess
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
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

    except asyncio.TimeoutError:
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

    prompt_parts.append(
        "\nInstructions:"
        "\n1. Read the CLAUDE.md for project context"
        "\n2. Read the failing file and understand the bug"
        "\n3. Fix the code"
        "\n4. Run relevant tests to verify: uv run pytest test/ -v"
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
    if state.status == BridgeStatus.BUSY:
        raise HTTPException(status_code=409, detail="Bridge is busy")

    state.start_task("restart-app")

    try:
        if request.kill_existing:
            # Kill any existing OpenAlgo process on port 5000
            try:
                kill_result = await asyncio.create_subprocess_exec(
                    "powershell", "-Command",
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
            "uv", "run", "app.py",
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
- Time: {candidate_at}

OPERATOR CONTEXT:
- Current positions: {positions_count} ({positions_summary})
- P&L today: ₹{pnl_today}
- Trades today: {trades_today}/{max_trades_today}

MARKET CONTEXT (today):
- NIFTY return: {nifty_pct}%
- India VIX: {india_vix}

Decide whether the operator should take this signal. Be conservative: skip when the broader market regime conflicts with the signal direction (e.g., BUY signal on a -1% NIFTY day with elevated VIX), or when the operator is already near their daily trade limit.

Respond with a short reasoning paragraph followed by a final JSON block. The JSON block must be the LAST thing in your response and must contain exactly these keys:

{{
  "decision": "take" | "skip",
  "reasoning": "1-2 sentence summary",
  "confidence": 0.0 to 1.0
}}
"""


def _format_review_prompt(candidate: ReviewCandidate, ctx: ReviewContext) -> str:
    """Interpolate the prompt template. Missing context fields become 'unknown'."""

    def _or_unknown(value: Any) -> str:
        if value is None:
            return "unknown"
        return str(value)

    return REVIEW_PROMPT_TEMPLATE.format(
        symbol=candidate.symbol,
        source=candidate.source,
        candidate_at=candidate.candidate_at,
        positions_count=_or_unknown(ctx.positions_count),
        positions_summary=_or_unknown(ctx.positions_summary),
        pnl_today=_or_unknown(ctx.pnl_today),
        trades_today=_or_unknown(ctx.trades_today),
        max_trades_today=_or_unknown(ctx.max_trades_today),
        nifty_pct=_or_unknown(ctx.nifty_pct),
        india_vix=_or_unknown(ctx.india_vix),
    )


def _extract_decision_block(text: str) -> Optional[dict]:
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


def _failsafe_response(
    reasoning: str, started: float, session_id: str = "", raw: str = ""
) -> dict:
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
    except asyncio.TimeoutError:
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
    except asyncio.TimeoutError:
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
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logger.info(f"Starting OpenAlgo Claude Bridge on port 5001")
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info(f"Claude CLI: {CLAUDE_CMD}")
    logger.info(f"Allowed tools: {len(ALLOWED_TOOLS)} pre-approved")
    logger.info("=" * 60)

    uvicorn.run(app, host="127.0.0.1", port=5001, log_level="info")
