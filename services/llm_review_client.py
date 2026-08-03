"""In-process ``claude -p`` invocation for the Stage-1 LLM veto.

This module replaces the Claude Bridge (``bridge/server.py`` ``/review-signal``)
as the transport for the veto's reasoning call. The veto call is a **bare**
``claude -p "<prompt>" --output-format json`` — no ``--allowedTools``, no DB
clone — so it is pure reasoning over the context handed in the prompt and does
not need the bridge's tool/DB machinery.

Why a real OS thread instead of asyncio: OpenAlgo runs under eventlet in
production (``--worker-class eventlet``), which monkey-patches the stdlib and is
incompatible with ``asyncio.run()`` / a live event loop. The reliable escape
hatch — the same one ``telegram_bot_service._render_plotly_png`` uses — is a
brand-new **unpatched** OS thread that runs a blocking ``subprocess.run``. The
caller blocks on ``t.join()`` for the duration of the call (bounded by
``timeout_s``).

Import-light: only stdlib. No repo/DB access.
"""

from __future__ import annotations

import json
import os
import queue as _queue
import subprocess  # noqa: S404  # nosec B404 — spawning the claude CLI is the whole point of this module
import sys
import time
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

# Import the original (unpatched) threading module so the worker runs on a real
# OS thread even under eventlet's monkey-patching — see the module docstring.
if "eventlet" in sys.modules:
    import eventlet

    original_threading = eventlet.patcher.original("threading")
else:
    import threading as original_threading


def _claude_cmd() -> str:
    """Resolve the ``claude`` binary.

    ``CLAUDE_CMD`` env override wins (e.g. an absolute path to a non-PATH
    install); otherwise defaults to the bare ``claude`` name, which
    ``subprocess.run`` resolves against ``PATH``.
    """
    raw = os.getenv("CLAUDE_CMD")
    if raw is not None and raw.strip() != "":
        return raw.strip()
    return "claude"


def _parse_envelope(stdout: str) -> tuple[str, str]:
    """Extract ``(model_text, session_id)`` from the ``--output-format json`` envelope.

    ``model_text`` is the prose Claude emitted (the ``result`` field), or the
    raw stdout when the envelope can't be parsed. ``session_id`` is the Claude
    Code session id when present, else the empty string.

    Raises:
        RuntimeError: when the envelope sets ``is_error``. **The CLI signals some
            failures with exit code 0 and ``is_error: true`` in the envelope** —
            a logged-out CLI returns exactly that, with ``result`` set to
            "Not logged in · Please run /login". Without this check that string
            is returned as if it were the model's answer: the veto would treat it
            as unparseable reasoning and the triage layer would file it as a day
            assessment. Raising here routes it through the caller's existing
            error handling, where ``_AUTH_MARKERS`` classifies it as
            ``not_logged_in``.
    """
    model_text = stdout
    session_id = ""
    try:
        envelope: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return model_text, session_id
    if isinstance(envelope, dict):
        result = envelope.get("result")
        if isinstance(result, str):
            model_text = result
        session_id = str(envelope.get("session_id", "") or "")
        if envelope.get("is_error"):
            raise RuntimeError(f"claude reported an error: {model_text[:300]}")
    return model_text, session_id


def invoke_claude_review(prompt: str, timeout_s: float) -> tuple[str, str]:
    """Run ``claude -p <prompt> --output-format json`` and return ``(model_text, session_id)``.

    Spawns a blocking ``subprocess.run`` on a dedicated real OS thread (eventlet
    monkey-patches ``threading``, so we use the original module — otherwise the
    subprocess call would run on a greenlet that shares the parent's context).

    Enforces ``timeout_s`` end-to-end: on expiry the subprocess is killed and
    ``TimeoutError`` is raised. A non-zero exit or an unspawnable binary raises
    (``RuntimeError`` / ``FileNotFoundError``). The caller is responsible for the
    fail-safe-to-'take' behaviour on any of these.

    Returns:
        A ``(model_text, session_id)`` tuple. ``model_text`` is the model's
        prose; ``session_id`` is the Claude Code session id, or ``""`` if the
        envelope didn't carry one.
    """
    cmd = [_claude_cmd(), "-p", prompt, "--output-format", "json"]

    result_q: _queue.Queue[tuple[str, object]] = _queue.Queue()

    def _worker() -> None:
        try:
            completed = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv (claude CLI), not shell; no untrusted input in argv
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
            result_q.put(("ok", completed))
        except BaseException as exc:  # noqa: BLE001 — propagate across the thread boundary
            result_q.put(("err", exc))

    t = original_threading.Thread(target=_worker, daemon=True, name="openalgo-claude-review")
    t.start()
    # Give join a little slack beyond the subprocess timeout so a clean
    # TimeoutExpired surfaces from the worker rather than us abandoning the
    # thread mid-kill. The subprocess.run(timeout=...) is the real budget.
    t.join(timeout=timeout_s + 5.0)

    if t.is_alive():
        # The worker never returned even past the subprocess timeout — treat as
        # a timeout. The daemon thread will be reaped on interpreter exit.
        logger.warning("llm_review_client: worker thread still alive past join budget")
        raise TimeoutError("claude review worker did not complete in time")

    status, payload = result_q.get_nowait()
    if status == "err":
        exc = payload
        # subprocess.run raises TimeoutExpired on timeout — normalise to the
        # stdlib TimeoutError the caller checks for.
        if isinstance(exc, subprocess.TimeoutExpired):
            raise TimeoutError("claude review timed out") from exc
        raise exc  # type: ignore[misc]

    completed = payload  # type: ignore[assignment]
    if completed.returncode != 0:
        # The CLI writes its diagnostic to STDOUT (inside the JSON envelope), not
        # stderr — a logged-out CLI exits 1 with stderr EMPTY and
        # `result: "Not logged in · Please run /login"` in stdout. Reporting only
        # stderr produced a useless "claude review exited 1: " and cost the
        # `_AUTH_MARKERS` classification its only evidence, so the operator was
        # told "error" instead of "run claude login".
        detail = (completed.stderr or "").strip()
        if not detail:
            detail = _envelope_error_text(completed.stdout or "")
        raise RuntimeError(f"claude review exited {completed.returncode}: {detail[:500]}")

    return _parse_envelope(completed.stdout or "")


def _envelope_error_text(stdout: str) -> str:
    """Best-effort human-readable reason out of a failed run's stdout envelope."""
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()[:500]
    if isinstance(envelope, dict):
        result = envelope.get("result")
        if isinstance(result, str) and result.strip():
            return result.strip()
    return stdout.strip()[:500]


def classify_invocation_error(exc: BaseException) -> str:
    """Map an :func:`invoke_claude_review` failure to a reason code.

    Lets a caller classify the **real** call's failure instead of paying for a
    separate pre-flight probe. That matters: a cold ``claude -p`` takes longer
    than the probe's default 12 s budget, so probing first reports ``timeout``
    on a perfectly healthy CLI and skips the work — a false negative that only
    shows up against a live model, never against a stub.

    Returns one of ``timeout`` | ``cli_missing`` | ``not_logged_in`` | ``error``.
    """
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "cli_missing"
    message = str(exc).lower()
    if any(marker in message for marker in _AUTH_MARKERS):
        return "not_logged_in"
    return "error"


# Substrings that mark a non-zero ``claude`` exit as an auth/login problem rather
# than a generic error — surfaced to the operator as "run claude login".
_AUTH_MARKERS = (
    "login",
    "logged in",
    "log in",
    "authenticat",
    "unauthor",
    "not authenticated",
    "credential",
    "api key",
    "oauth",
    "invalid token",
    "expired",
)


def probe_claude_health(timeout_s: float = 12.0) -> dict[str, Any]:
    """Run a lightweight ``claude -p`` liveness probe of the veto's LLM transport.

    This is the ground-truth reachability check for the Stage-1 LLM veto: it
    spawns the same ``claude`` CLI the veto uses (via ``invoke_claude_review``)
    with a trivial prompt, and classifies the outcome. Because it spawns a real
    subprocess (seconds, and it consumes tokens), callers should invoke it
    **on demand only** — never on a fast poll.

    Args:
        timeout_s: Wall-clock budget for the probe subprocess.

    Returns:
        A dict with:
          * ``reachable`` (bool) — True only on a clean, non-empty reply.
          * ``latency_ms`` (int) — round-trip time.
          * ``reason`` — one of ``ok`` | ``timeout`` | ``cli_missing`` |
            ``not_logged_in`` | ``error``.
          * ``detail`` — short human-readable context (truncated).
    """
    started = time.time()

    def _elapsed_ms() -> int:
        return int((time.time() - started) * 1000)

    try:
        model_text, _session_id = invoke_claude_review("Reply with only: OK", timeout_s)
    except TimeoutError:
        return {
            "reachable": False,
            "latency_ms": _elapsed_ms(),
            "reason": "timeout",
            "detail": f"claude did not respond within {timeout_s:.0f}s",
        }
    except FileNotFoundError:
        return {
            "reachable": False,
            "latency_ms": _elapsed_ms(),
            "reason": "cli_missing",
            "detail": "claude CLI not found on PATH (set CLAUDE_CMD to its full path)",
        }
    except RuntimeError as exc:
        msg = str(exc)
        reason = "not_logged_in" if any(m in msg.lower() for m in _AUTH_MARKERS) else "error"
        return {
            "reachable": False,
            "latency_ms": _elapsed_ms(),
            "reason": reason,
            "detail": msg[:300],
        }
    except Exception as exc:  # noqa: BLE001 — probe must never raise into the caller
        logger.exception("probe_claude_health: unexpected failure")
        return {
            "reachable": False,
            "latency_ms": _elapsed_ms(),
            "reason": "error",
            "detail": f"{type(exc).__name__}: {str(exc)[:280]}",
        }

    text = (model_text or "").strip()
    if not text:
        return {
            "reachable": False,
            "latency_ms": _elapsed_ms(),
            "reason": "error",
            "detail": "claude returned an empty response",
        }
    return {
        "reachable": True,
        "latency_ms": _elapsed_ms(),
        "reason": "ok",
        "detail": text[:120],
    }
