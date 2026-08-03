"""Secret scan on model-authored text before it is published.

The far end of the exfiltration defence described in
:mod:`services.llm_agent_client`. That module stops the agent *reading* ``.env``
and ``db/``; this one stops anything credential-shaped from leaving in an issue
body or comment, whatever route it arrived by.

Why both ends. The agent has read access to the repo and its output is published
to GitHub. A deny-list is a policy, and policies have gaps — a secret pasted into
a log line, a credential committed somewhere unexpected, a deny pattern that
doesn't match what someone actually named the file. This check does not care how
the string got there. It refuses to publish it.

Two layers:

1. **``detect-secrets``**, the same scanner the pre-commit hook runs, invoked over
   a temp file holding the candidate text. Catches high-entropy strings, private
   keys, and the provider-specific patterns its plugin set knows.
2. **A small explicit pattern set** for the values that matter most on *this*
   install and would be catastrophic to publish — ``API_KEY_PEPPER``,
   ``FERNET_SALT``, ``APP_KEY``, broker tokens. Layer 1 would probably catch a
   raw 64-hex value, but "probably" is not the standard for a credential that
   seals every stored secret in the database.

**Fail closed.** If the scan cannot run, ``is_safe_to_publish`` returns False. A
scanner that silently degrades to "looks fine" is worse than no scanner, because
it is trusted.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: S404  # nosec B404 — invoking the detect-secrets CLI is the purpose here
import tempfile
from pathlib import Path

from utils.logging import get_logger

logger = get_logger(__name__)

_SCAN_TIMEOUT_SECONDS = 60.0

# Names whose *values* must never appear in published text. Matched as
# `NAME=<value>` / `NAME: <value>` / `"NAME": "<value>"` shapes.
_SENSITIVE_NAMES: tuple[str, ...] = (
    "API_KEY_PEPPER",
    "FERNET_SALT",
    "APP_KEY",
    "BROKER_API_SECRET",
    "BROKER_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "SMTP_PASSWORD",
    "totp_secret",
    "api_key_encrypted",
    "api_secret_encrypted",
)

_ASSIGNMENT_RE = re.compile(
    r"(?P<name>" + "|".join(re.escape(n) for n in _SENSITIVE_NAMES) + r")"
    r"\s*[:=]\s*[\"']?(?P<value>[^\s\"',}]{8,})",
    re.IGNORECASE,
)

# Bare high-value shapes worth refusing on sight.
_RAW_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("zerodha_access_token", re.compile(r"\baccess_token\s*[:=]\s*[\"']?[A-Za-z0-9]{20,}")),
    # 64 hex chars is exactly what `secrets.token_hex(32)` produces — the format
    # of APP_KEY and API_KEY_PEPPER.
    ("token_hex_32", re.compile(r"\b[0-9a-fA-F]{64}\b")),
)


def _uv_path() -> str:
    """Absolute path to ``uv``, falling back to the bare name.

    Resolved rather than passed as ``"uv"`` so the executable is unambiguous —
    a partial path is decided by whatever PATH happens to be when the scheduler
    fires, which for a security check is the wrong kind of "depends".
    """
    import shutil

    return shutil.which("uv") or "uv"


def _explicit_findings(text: str) -> list[str]:
    findings: list[str] = []
    for match in _ASSIGNMENT_RE.finditer(text or ""):
        findings.append(f"sensitive assignment: {match.group('name')}")
    for label, pattern in _RAW_PATTERNS:
        if pattern.search(text or ""):
            findings.append(label)
    return sorted(set(findings))


def _detect_secrets_findings(text: str) -> tuple[list[str], bool]:
    """Run ``detect-secrets`` over ``text``.

    Returns ``(findings, scan_ran)``. ``scan_ran=False`` means the scanner could
    not be executed at all — the caller must treat that as unsafe, not clean.
    """
    tmp_path: Path | None = None
    try:
        # `.md` so the scanner treats it as prose rather than trying to parse it.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".md", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(text or "")
            tmp_path = Path(handle.name)

        completed = subprocess.run(  # noqa: S603  # nosec B603 — fixed argv, no shell
            [_uv_path(), "run", "--group", "dev", "detect-secrets", "scan", str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_SECONDS,
            check=False,
            cwd=os.getcwd(),
        )
        if completed.returncode != 0:
            logger.warning(
                "publish_guard: detect-secrets exited %s: %s",
                completed.returncode,
                (completed.stderr or "")[:300],
            )
            return [], False

        payload = json.loads(completed.stdout or "{}")
        results = payload.get("results") or {}
        findings = [
            f"detect-secrets: {item.get('type')} (line {item.get('line_number')})"
            for items in results.values()
            for item in items
        ]
        return findings, True
    except FileNotFoundError:
        logger.warning("publish_guard: uv/detect-secrets not available")
        return [], False
    except subprocess.TimeoutExpired:
        logger.warning("publish_guard: detect-secrets timed out")
        return [], False
    except Exception:
        logger.exception("publish_guard: detect-secrets scan failed")
        return [], False
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                logger.exception("publish_guard: could not remove temp scan file")


def scan_text(text: str, *, require_scanner: bool = True) -> dict:
    """Assess ``text`` for anything that must not be published.

    Args:
        text: Candidate issue body / comment.
        require_scanner: When True (the default), an unrunnable ``detect-secrets``
            makes the result unsafe. Set False only where the explicit pattern set
            is knowingly accepted as the sole check.

    Returns:
        ``{"safe": bool, "findings": [...], "scanner_ran": bool}``.
    """
    findings = _explicit_findings(text)
    scanner_findings, scanner_ran = _detect_secrets_findings(text)
    findings.extend(scanner_findings)

    safe = not findings
    if require_scanner and not scanner_ran:
        safe = False
        findings.append("detect-secrets could not run — failing closed")

    return {"safe": safe, "findings": sorted(set(findings)), "scanner_ran": scanner_ran}


def is_safe_to_publish(text: str, *, require_scanner: bool = True) -> bool:
    """Convenience predicate. Fails closed on any doubt."""
    return bool(scan_text(text, require_scanner=require_scanner)["safe"])
