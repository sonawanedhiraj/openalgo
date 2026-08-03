"""Narrow ``gh`` CLI wrapper for automated issue work.

The only path from this codebase to GitHub. Deliberately small: an **allow-list of
verbs**, argv assembled in Python from fixed templates, and every text body
routed through :mod:`services.publish_guard` before it leaves the machine.

Why an allow-list rather than a general runner. The text this carries is authored
by an LLM that has read the repo and the logs. A general `gh` runner would let a
mistake — or text injected into a log line the agent was shown — reach `gh pr
merge`, `gh release create`, or `gh api` with a mutation. Enumerating the four
verbs that issue triage actually needs removes that entirely: **model output is
only ever a value inside a fixed template, never part of the command shape.**

Auth is the operator's ambient ``gh auth`` (keyring on this host) with
``GH_TOKEN`` honoured when present, so a future service/Docker deploy has a path.
No token is read or logged here.

Never available through this module: anything that touches code or CI —
``pr merge``, ``push``, ``workflow run``, ``release``, ``repo delete``.
"""

from __future__ import annotations

import json
import os
import subprocess  # noqa: S404  # nosec B404 — invoking the gh CLI is the purpose of this module
from typing import Any

from utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_TIMEOUT_SECONDS = 60.0

# The complete set of operations this module can perform.
ALLOWED_VERBS: frozenset[str] = frozenset(
    {"list", "view", "create", "comment", "edit", "reopen", "close"}
)


class GhError(RuntimeError):
    """A ``gh`` invocation failed."""


def _gh_cmd() -> str:
    raw = os.getenv("GH_CMD")
    return raw.strip() if raw and raw.strip() else "gh"


def _run(args: list[str], timeout_s: float = _DEFAULT_TIMEOUT_SECONDS) -> str:
    """Run ``gh <args>`` and return stdout. Raises :class:`GhError` on failure."""
    verb_position = 1  # args == ["issue", "<verb>", ...]
    if len(args) > verb_position and args[verb_position] not in ALLOWED_VERBS:
        raise GhError(f"verb {args[verb_position]!r} is not in the allow-list")

    cmd = [_gh_cmd(), *args]
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            cmd, capture_output=True, text=True, timeout=timeout_s, check=False
        )
    except FileNotFoundError as exc:
        raise GhError("gh CLI not found on PATH (set GH_CMD)") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"gh timed out after {timeout_s:.0f}s") from exc

    if completed.returncode != 0:
        raise GhError(
            f"gh {' '.join(args[:2])} exited {completed.returncode}: "
            f"{(completed.stderr or '').strip()[:400]}"
        )
    return completed.stdout or ""


def check_available() -> dict[str, Any]:
    """Report whether ``gh`` is usable, without mutating anything.

    Kept separate from the operations so the caller can degrade loudly at the top
    of a run rather than failing halfway through a batch.
    """
    try:
        _run(["issue", "list", "--limit", "1", "--json", "number"])
        return {"available": True, "detail": "ok"}
    except GhError as exc:
        return {"available": False, "detail": str(exc)[:300]}


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_issues(
    *,
    state: str = "open",
    limit: int = 30,
    search: str | None = None,
    labels: list[str] | None = None,
) -> list[dict[str, Any]]:
    """List issues with the fields the post-market pipeline needs."""
    args = [
        "issue",
        "list",
        "--state",
        state,
        "--limit",
        str(max(1, min(int(limit), 100))),
        "--json",
        "number,title,state,stateReason,labels,body,closedAt,updatedAt",
    ]
    if search:
        args.extend(["--search", search])
    for label in labels or []:
        args.extend(["--label", label])
    try:
        return json.loads(_run(args) or "[]")
    except (GhError, ValueError):
        logger.exception("github_cli_client: list_issues failed")
        return []


def view_issue(number: int) -> dict[str, Any] | None:
    """Full issue including comments, or None on failure."""
    args = [
        "issue",
        "view",
        str(int(number)),
        "--json",
        "number,title,state,stateReason,labels,body,comments,closedAt",
    ]
    try:
        return json.loads(_run(args) or "{}") or None
    except (GhError, ValueError):
        logger.exception("github_cli_client: view_issue(%s) failed", number)
        return None


# --------------------------------------------------------------------------
# Writes — every body passes the publish guard first
# --------------------------------------------------------------------------


def _guard(text: str, what: str) -> None:
    """Refuse to publish ``text`` if it looks like it carries a secret."""
    from services.publish_guard import scan_text

    verdict = scan_text(text)
    if not verdict["safe"]:
        logger.error("github_cli_client: BLOCKED publishing %s — %s", what, verdict["findings"])
        raise GhError(f"publish guard blocked {what}: {verdict['findings']}")


def create_issue(
    title: str,
    body: str,
    labels: list[str] | None = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Create an issue. ``dry_run=True`` (the default) reports without writing."""
    _guard(f"{title}\n{body}", "issue body")

    if dry_run:
        logger.info("github_cli_client: DRY RUN — would create issue %r", title[:80])
        return {"created": False, "dry_run": True, "title": title, "url": None}

    args = ["issue", "create", "--title", title, "--body", body]
    for label in labels or []:
        args.extend(["--label", label])
    try:
        out = _run(args).strip()
    except GhError:
        logger.exception("github_cli_client: create_issue failed")
        return {"created": False, "dry_run": False, "title": title, "url": None}
    return {"created": True, "dry_run": False, "title": title, "url": out.splitlines()[-1:]}


def comment_issue(number: int, body: str, *, dry_run: bool = True) -> bool:
    """Comment on an issue. Returns True when the comment was actually posted."""
    _guard(body, f"comment on #{number}")

    if dry_run:
        logger.info("github_cli_client: DRY RUN — would comment on #%s", number)
        return False
    try:
        _run(["issue", "comment", str(int(number)), "--body", body])
        return True
    except GhError:
        logger.exception("github_cli_client: comment_issue(%s) failed", number)
        return False


def reopen_issue(number: int, comment: str | None = None, *, dry_run: bool = True) -> bool:
    """Reopen an issue, optionally with a comment explaining why."""
    if comment:
        _guard(comment, f"reopen comment on #{number}")

    if dry_run:
        logger.info("github_cli_client: DRY RUN — would reopen #%s", number)
        return False
    args = ["issue", "reopen", str(int(number))]
    if comment:
        args.extend(["--comment", comment])
    try:
        _run(args)
        return True
    except GhError:
        logger.exception("github_cli_client: reopen_issue(%s) failed", number)
        return False


def add_labels(number: int, labels: list[str], *, dry_run: bool = True) -> bool:
    """Add labels to an issue."""
    if not labels:
        return False
    if dry_run:
        logger.info("github_cli_client: DRY RUN — would label #%s %s", number, labels)
        return False
    args = ["issue", "edit", str(int(number))]
    for label in labels:
        args.extend(["--add-label", label])
    try:
        _run(args)
        return True
    except GhError:
        logger.exception("github_cli_client: add_labels(%s) failed", number)
        return False


def update_body(number: int, body: str, *, dry_run: bool = True) -> bool:
    """Replace an issue body — used only to tick verified validation boxes."""
    _guard(body, f"body update on #{number}")

    if dry_run:
        logger.info("github_cli_client: DRY RUN — would update body of #%s", number)
        return False
    try:
        _run(["issue", "edit", str(int(number)), "--body", body])
        return True
    except GhError:
        logger.exception("github_cli_client: update_body(%s) failed", number)
        return False
