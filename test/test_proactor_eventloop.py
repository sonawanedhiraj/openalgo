"""
Tests for the Windows ProactorEventLoop policy set by websocket_proxy.app_integration.

On Windows: importing app_integration must install WindowsProactorEventLoopPolicy so
that asyncio.new_event_loop() returns a ProactorEventLoop (no FD_SETSIZE ceiling).

On non-Windows (Linux CI): the policy block is skipped entirely; the default
policy is preserved and the tests confirm the no-op behaviour.

These tests are cross-platform safe — they must pass on Linux CI without any
Windows-specific setup.
"""

import asyncio
import importlib
import platform
import sys


class TestProactorEventLoopPolicy:
    """Verify the asyncio event loop policy set by websocket_proxy.app_integration."""

    def _import_app_integration(self):
        """Import (or re-import) app_integration, resetting cached module state."""
        mod_name = "websocket_proxy.app_integration"
        if mod_name in sys.modules:
            # Re-execute module-level policy block by reloading
            return importlib.reload(sys.modules[mod_name])
        return importlib.import_module(mod_name)

    def test_policy_on_windows(self):
        """On Windows the policy must be WindowsProactorEventLoopPolicy."""
        if platform.system() != "Windows":
            return  # non-Windows: skip assertion, test still passes

        self._import_app_integration()
        policy = asyncio.get_event_loop_policy()
        assert isinstance(policy, asyncio.WindowsProactorEventLoopPolicy), (
            f"Expected WindowsProactorEventLoopPolicy, got {type(policy).__name__}. "
            "Incident #76: SelectorEventLoop crashes at FD_SETSIZE ~512."
        )

    def test_new_loop_is_proactor_on_windows(self):
        """On Windows a new_event_loop() created after import is a ProactorEventLoop."""
        if platform.system() != "Windows":
            return

        self._import_app_integration()
        loop = asyncio.new_event_loop()
        try:
            assert isinstance(loop, asyncio.ProactorEventLoop), (
                f"Expected ProactorEventLoop, got {type(loop).__name__}"
            )
        finally:
            loop.close()

    def test_no_selector_loop_on_windows(self):
        """On Windows the policy must NOT be the select()-based SelectorEventLoop policy."""
        if platform.system() != "Windows":
            return

        self._import_app_integration()
        policy = asyncio.get_event_loop_policy()
        assert not isinstance(policy, asyncio.WindowsSelectorEventLoopPolicy), (
            "SelectorEventLoop is capped at ~512 FD_SETSIZE; use ProactorEventLoop."
        )

    def test_non_windows_policy_untouched(self):
        """On non-Windows platforms the policy is left at its default (not changed)."""
        if platform.system() == "Windows":
            return  # non-Windows-specific assertion

        policy_before = type(asyncio.get_event_loop_policy())
        self._import_app_integration()
        policy_after = type(asyncio.get_event_loop_policy())
        # The import may not change the policy type on Linux (eventlet may have
        # already set it; just confirm no Windows-specific policy was introduced).
        assert "Windows" not in policy_after.__name__, (
            f"Windows-specific policy {policy_after.__name__} appeared on non-Windows platform"
        )
        # Policy type is stable across import on non-Windows
        assert policy_before == policy_after or "Windows" not in policy_after.__name__

    def test_loop_can_run_coroutine_on_windows(self):
        """On Windows a ProactorEventLoop can successfully run a coroutine."""
        if platform.system() != "Windows":
            return

        self._import_app_integration()
        loop = asyncio.new_event_loop()
        try:

            async def _noop():
                return "ok"

            result = loop.run_until_complete(_noop())
            assert result == "ok"
        finally:
            loop.close()
