"""Tests for ``services.llm_review_client.invoke_claude_review``.

The ``subprocess.run`` call is mocked in every test — no real ``claude`` binary
is ever spawned and no network request fires. We assert the JSON-envelope
parse, the timeout→kill→raise path, and the non-zero-exit / garbage paths.
"""

import json
import subprocess

import pytest


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _envelope(result_text: str, session_id: str = "sess-abc") -> str:
    return json.dumps({"result": result_text, "session_id": session_id})


def test_invoke_parses_result_and_session_id(monkeypatch):
    from services import llm_review_client as lrc

    prose = 'reasoning here\n{"decision": "skip", "reasoning": "r", "confidence": 0.6}'

    def fake_run(cmd, **kwargs):
        # Sanity: it's the claude CLI, not a shell string.
        assert cmd[0] == "claude"
        assert "-p" in cmd and "--output-format" in cmd
        return _FakeCompleted(returncode=0, stdout=_envelope(prose, "sess-abc"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    model_text, session_id = lrc.invoke_claude_review("PROMPT", timeout_s=5.0)

    assert model_text == prose
    assert session_id == "sess-abc"


def test_invoke_falls_back_to_raw_stdout_when_not_envelope(monkeypatch):
    """If stdout isn't a JSON envelope, model_text is the raw stdout, session empty."""
    from services import llm_review_client as lrc

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=0, stdout="plain prose, no json envelope")

    monkeypatch.setattr(subprocess, "run", fake_run)

    model_text, session_id = lrc.invoke_claude_review("PROMPT", timeout_s=5.0)

    assert model_text == "plain prose, no json envelope"
    assert session_id == ""


def test_invoke_timeout_raises_timeouterror_and_kills_subprocess(monkeypatch):
    """subprocess.run(timeout=...) raising TimeoutExpired → TimeoutError.

    subprocess.run itself sends SIGKILL to the child on timeout (documented),
    so asserting the normalised TimeoutError is the observable contract.
    """
    from services import llm_review_client as lrc

    def fake_run(cmd, **kwargs):
        # subprocess.run raises TimeoutExpired (and kills the child) when the
        # child overruns; emulate that here.
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(TimeoutError):
        lrc.invoke_claude_review("PROMPT", timeout_s=0.5)


def test_invoke_nonzero_exit_raises(monkeypatch):
    from services import llm_review_client as lrc

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=2, stdout="", stderr="auth error")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as excinfo:
        lrc.invoke_claude_review("PROMPT", timeout_s=5.0)
    assert "exited 2" in str(excinfo.value)


def test_invoke_missing_binary_propagates_filenotfound(monkeypatch):
    from services import llm_review_client as lrc

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("claude not found on PATH")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FileNotFoundError):
        lrc.invoke_claude_review("PROMPT", timeout_s=5.0)


def test_invoke_garbage_envelope_returns_raw_text(monkeypatch):
    """A zero-exit run with non-JSON stdout returns the raw text (caller parses)."""
    from services import llm_review_client as lrc

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=0, stdout="{not valid json at all")

    monkeypatch.setattr(subprocess, "run", fake_run)

    model_text, session_id = lrc.invoke_claude_review("PROMPT", timeout_s=5.0)
    assert model_text == "{not valid json at all"
    assert session_id == ""


def test_claude_cmd_env_override(monkeypatch):
    from services import llm_review_client as lrc

    monkeypatch.setenv("CLAUDE_CMD", "/opt/bin/claude")
    assert lrc._claude_cmd() == "/opt/bin/claude"

    monkeypatch.delenv("CLAUDE_CMD", raising=False)
    assert lrc._claude_cmd() == "claude"


def test_invoke_uses_claude_cmd_override_in_argv(monkeypatch):
    from services import llm_review_client as lrc

    monkeypatch.setenv("CLAUDE_CMD", "/custom/claude")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted(returncode=0, stdout=_envelope("ok"))

    monkeypatch.setattr(subprocess, "run", fake_run)

    lrc.invoke_claude_review("PROMPT", timeout_s=5.0)
    assert seen["cmd"][0] == "/custom/claude"


# ---------------------------------------------------------------------------
# probe_claude_health classification (issue #297)
# ---------------------------------------------------------------------------


def test_probe_health_ok(monkeypatch):
    from services import llm_review_client as lrc

    monkeypatch.setattr(lrc, "invoke_claude_review", lambda prompt, t: ("OK", "sess"))
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is True
    assert out["reason"] == "ok"
    assert out["latency_ms"] >= 0


def test_probe_health_empty_reply_is_error(monkeypatch):
    from services import llm_review_client as lrc

    monkeypatch.setattr(lrc, "invoke_claude_review", lambda prompt, t: ("   ", ""))
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is False
    assert out["reason"] == "error"


def test_probe_health_timeout(monkeypatch):
    from services import llm_review_client as lrc

    def boom(prompt, t):
        raise TimeoutError("timed out")

    monkeypatch.setattr(lrc, "invoke_claude_review", boom)
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is False
    assert out["reason"] == "timeout"


def test_probe_health_cli_missing(monkeypatch):
    from services import llm_review_client as lrc

    def boom(prompt, t):
        raise FileNotFoundError("claude")

    monkeypatch.setattr(lrc, "invoke_claude_review", boom)
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is False
    assert out["reason"] == "cli_missing"


def test_probe_health_not_logged_in(monkeypatch):
    from services import llm_review_client as lrc

    def boom(prompt, t):
        raise RuntimeError("claude review exited 1: Please run 'claude login' to authenticate")

    monkeypatch.setattr(lrc, "invoke_claude_review", boom)
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is False
    assert out["reason"] == "not_logged_in"


def test_probe_health_generic_runtime_error_is_error(monkeypatch):
    from services import llm_review_client as lrc

    def boom(prompt, t):
        raise RuntimeError("claude review exited 2: segfault in model runtime")

    monkeypatch.setattr(lrc, "invoke_claude_review", boom)
    out = lrc.probe_claude_health(timeout_s=5.0)
    assert out["reachable"] is False
    assert out["reason"] == "error"
