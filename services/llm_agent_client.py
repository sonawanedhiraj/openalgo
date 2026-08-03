"""Tool-enabled ``claude -p`` invocation — an agent that can read this repo.

The sibling of :mod:`services.llm_review_client`. That module runs a **bare**
call: pure reasoning over whatever the prompt carries, no tools. This one hands
the model **read-only access to the source tree and the logs**, so it can verify
a finding against the actual code instead of inferring a cause from symptoms.

Same subprocess discipline as the bare client, for the same reason: OpenAlgo runs
under eventlet in production, which monkey-patches the stdlib, so the blocking
``subprocess.run`` goes on a brand-new **unpatched** OS thread and the caller
blocks on ``join``.

What the agent may and may not do
---------------------------------

* **Allowed:** ``Read``, ``Grep``, ``Glob`` — inspect files, search the tree.
* **Denied:** ``Bash``, ``Write``, ``Edit``, ``NotebookEdit``, ``WebFetch``,
  ``WebSearch``, ``Task``. No shell, no mutation, no network. The agent is
  strictly an observer, matching the read-only-on-code carve-out the Cowork
  scheduled tasks already run under.
* **Path deny-list:** ``.env*``, ``db/``, ``.git/``, key/cert files. This one is
  load-bearing rather than tidy — ``.env`` holds ``API_KEY_PEPPER`` and
  ``FERNET_SALT``, every encrypted secret in ``openalgo.db`` is sealed against
  them, and the whole point of the pipeline downstream is to **publish** what the
  agent writes to GitHub. Read access plus a publish path is an exfiltration
  channel; the deny-list closes the near end and the caller's secret scan closes
  the far end.

Defence in depth, not a single wall: the deny-list here, a `detect-secrets` gate
on anything about to be published, and evidence requirements on findings. Prompt
wording is never treated as a security boundary.
"""

from __future__ import annotations

import json
import os
import queue as _queue
import subprocess  # noqa: S404  # nosec B404 — spawning the claude CLI is the whole point of this module
import sys
from collections.abc import Sequence
from typing import Any

from services.llm_review_client import _parse_envelope
from utils.logging import get_logger

logger = get_logger(__name__)

# Import the original (unpatched) threading module so the worker runs on a real
# OS thread even under eventlet's monkey-patching — see the module docstring.
if "eventlet" in sys.modules:
    import eventlet

    original_threading = eventlet.patcher.original("threading")
else:
    import threading as original_threading


DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = ("Read", "Grep", "Glob")

# Everything that could mutate state, run a command, or reach the network.
DEFAULT_DISALLOWED_TOOLS: tuple[str, ...] = (
    "Bash",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)

# Deny rules passed to the CLI's own permission system. `.env` is the one that
# matters most: API_KEY_PEPPER and FERNET_SALT live there, and rotating them is a
# destructive reset of every stored credential (see CLAUDE.md).
DEFAULT_DENY_PATHS: tuple[str, ...] = (
    "./.env",
    "./.env.*",
    "./.env*",
    "./db/**",
    "./.git/**",
    "./**/*.key",
    "./**/*.pem",
    "./.secrets.baseline",
)


def _claude_cmd() -> str:
    raw = os.getenv("CLAUDE_CMD")
    if raw is not None and raw.strip() != "":
        return raw.strip()
    return "claude"


def build_settings(deny_paths: Sequence[str]) -> str:
    """Inline ``--settings`` JSON carrying the path deny rules.

    Passed as a single argv element (never through a shell), so no quoting
    concerns on Windows.
    """
    deny_rules = [f"Read({path})" for path in deny_paths]
    return json.dumps({"permissions": {"deny": deny_rules}})


def build_command(
    prompt: str,
    *,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
    deny_paths: Sequence[str] = DEFAULT_DENY_PATHS,
    add_dirs: Sequence[str] = (),
) -> list[str]:
    """Assemble the argv. Split out from the runner so tests can assert on it."""
    cmd = [_claude_cmd(), "-p", prompt, "--output-format", "json"]
    if allowed_tools:
        cmd.append("--allowedTools")
        cmd.extend(allowed_tools)
    if disallowed_tools:
        cmd.append("--disallowedTools")
        cmd.extend(disallowed_tools)
    if deny_paths:
        cmd.extend(["--settings", build_settings(deny_paths)])
    for directory in add_dirs:
        cmd.extend(["--add-dir", directory])
    return cmd


def invoke_claude_agent(
    prompt: str,
    timeout_s: float,
    *,
    cwd: str | None = None,
    allowed_tools: Sequence[str] = DEFAULT_ALLOWED_TOOLS,
    disallowed_tools: Sequence[str] = DEFAULT_DISALLOWED_TOOLS,
    deny_paths: Sequence[str] = DEFAULT_DENY_PATHS,
    add_dirs: Sequence[str] = (),
) -> tuple[str, str]:
    """Run a tool-enabled ``claude -p`` and return ``(model_text, session_id)``.

    Args:
        prompt: The instruction. Untrusted content inside it must be delimited by
            the caller — this function does not sanitise.
        timeout_s: Hard wall-clock budget; the subprocess is killed on expiry.
        cwd: Working directory the agent's tools are rooted at (the repo).
        allowed_tools / disallowed_tools / deny_paths: see module docstring.
        add_dirs: Extra directories to expose (e.g. a log dir outside the repo).

    Raises:
        TimeoutError: budget exceeded.
        FileNotFoundError: ``claude`` not on PATH.
        RuntimeError: non-zero exit, or an ``is_error`` envelope (which is how a
            logged-out CLI reports itself — see ``llm_review_client``).
    """
    cmd = build_command(
        prompt,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        deny_paths=deny_paths,
        add_dirs=add_dirs,
    )

    result_q: _queue.Queue[tuple[str, object]] = _queue.Queue()

    def _worker() -> None:
        try:
            completed = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv (claude CLI), not shell
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
                cwd=cwd,
            )
            result_q.put(("ok", completed))
        except BaseException as exc:  # noqa: BLE001 — propagate across the thread boundary
            result_q.put(("err", exc))

    thread = original_threading.Thread(target=_worker, daemon=True, name="openalgo-claude-agent")
    thread.start()
    thread.join(timeout=timeout_s + 5.0)

    if thread.is_alive():
        logger.warning("llm_agent_client: worker thread still alive past join budget")
        raise TimeoutError("claude agent worker did not complete in time")

    status, payload = result_q.get_nowait()
    if status == "err":
        exc = payload
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError("claude agent timed out") from exc
        raise exc  # type: ignore[misc]

    completed = payload  # type: ignore[assignment]
    if completed.returncode != 0:
        # The CLI puts its diagnostic in STDOUT's envelope, not stderr — see the
        # same handling in llm_review_client.
        detail = (completed.stderr or "").strip()
        if not detail:
            detail = _envelope_error_text(completed.stdout or "")
        raise RuntimeError(f"claude agent exited {completed.returncode}: {detail[:500]}")

    # Reuses the bare client's envelope parser, which raises on `is_error` —
    # duplicating that logic here would be a bug farm.
    return _parse_envelope(completed.stdout or "")


def _envelope_error_text(stdout: str) -> str:
    try:
        envelope: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()[:500]
    if isinstance(envelope, dict):
        result = envelope.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    return stdout.strip()[:500]
