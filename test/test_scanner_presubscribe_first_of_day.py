"""Hermetic regression tests for issue #244 (scanner WS first-of-day starvation).

The pre-#244 boot-retry thread in ``app.py:_wire_pre_subscribe`` fetched the
api_key ONCE before its polling loop and exited early when it was None.
Operators whose normal flow is "boot OpenAlgo → login to Zerodha" lost the
race when OAuth completed milliseconds after thread start: the api_key landed
in ``auth_db`` after the thread had bailed, nothing else triggered the WS
client to connect, and the scanner aggregator stayed at 0/N coverage all day
until a manual restart.

The fix is in ``services.scanner_presubscribe.wire_pre_subscribe``:

* The api_key is re-fetched on EVERY boot-retry iteration so a post-boot OAuth
  is picked up.
* A new subscription to the ``broker_session_refreshed`` event bus topic fires
  ``_attempt`` the instant ``utils.auth_utils.notify_broker_session_refreshed``
  publishes — no 15s poll latency, and the trigger survives past the
  ``PRESUBSCRIBE_MAX_WAIT_SEC`` deadline.

These tests pin both pieces. Pure hermetic — no Flask, no proxy, no broker.
All dependencies are injected (api_key_provider, ws_connection_getter, bus,
sleep_fn, time_fn) so the boot loop runs synchronously and produces a
deterministic order of calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from services.scanner_presubscribe import (
    PreSubscriber,
    PreSubscribeWiring,
    wire_pre_subscribe,
)


class _FakeBus:
    """Stub for ``utils.event_bus.EventBus`` capable of synchronous publish.

    The production bus dispatches via a ThreadPoolExecutor; for tests we want
    deterministic, in-line callback execution so an ``assert`` immediately
    after ``publish`` sees the effects.
    """

    def __init__(self):
        self.subscriptions: dict[str, list] = {}

    def subscribe(self, topic, callback, name=""):  # pragma: no cover - trivial
        self.subscriptions.setdefault(topic, []).append(callback)

    def publish(self, event):
        for cb in self.subscriptions.get(event.topic, []):
            cb(event)


class _FakeEvent:
    """Minimal stand-in for ``BrokerSessionRefreshedEvent``."""

    def __init__(
        self, topic: str = "broker_session_refreshed", username: str = "u", broker: str = "zerodha"
    ):
        self.topic = topic
        self.username = username
        self.broker = broker


def _make_subscriber():
    """Build a PreSubscriber whose connection getter is a Mock so we can
    drive ``ensure()`` purely in memory."""
    pre = PreSubscriber("test", lambda _s: "NSE")
    pre._connection_getter = lambda _uid: (False, None, None)  # ensure() will treat WS as down
    return pre


def _wire(
    *,
    api_keys,
    ws_ups,
    register_callback=None,
    bus=None,
    retry_sec=1,
    max_wait_sec=60,
    time_seq=None,
):
    """Test helper. ``api_keys`` and ``ws_ups`` are lists used as queues:
    each call to the provider/getter consumes the next entry (or repeats the
    last one if the queue is empty).

    Returns ``(pre_subscriber, ensure_spy, wiring, sleep_calls)`` where
    ``wiring`` is the :class:`PreSubscribeWiring` instance.
    """
    pre = _make_subscriber()
    ensure_spy = MagicMock(return_value=1)
    pre.ensure = ensure_spy  # spy the idempotent subscription point

    api_q = list(api_keys)
    ws_q = list(ws_ups)
    sleep_calls = []

    def api_provider():
        return api_q.pop(0) if len(api_q) > 1 else (api_q[0] if api_q else None)

    def verifier(api_key):
        # API key "VALID*" → user id "u_<api_key>"; anything else → None.
        return f"u_{api_key}" if api_key and api_key.startswith("VALID") else None

    def broker_resolver(_api_key):
        return "zerodha"

    def ws_getter(_user_id):
        ok = ws_q.pop(0) if len(ws_q) > 1 else (ws_q[0] if ws_q else False)
        return (ok, None, None)

    def sleep_fn(sec):
        sleep_calls.append(sec)

    # Default monotonic clock — emits an increasing sequence so the boot
    # loop exits after enough iterations rather than spinning forever.
    if time_seq is None:
        ticks = iter(range(10_000))
    else:
        ticks = iter(time_seq)

    def time_fn():
        return next(ticks)

    wiring = wire_pre_subscribe(
        "test_pre_subscribe",
        pre,
        ["RELIANCE", "INFY"],
        thread_name="TestThread",
        api_key_provider=api_provider,
        user_id_verifier=verifier,
        broker_resolver=broker_resolver,
        ws_connection_getter=ws_getter,
        register_callback=register_callback or (lambda _n, _c: None),
        bus=bus if bus is not None else _FakeBus(),
        sleep_fn=sleep_fn,
        time_fn=time_fn,
        retry_sec=retry_sec,
        max_wait_sec=max_wait_sec,
        start_thread=False,
    )
    assert isinstance(wiring, PreSubscribeWiring)
    return pre, ensure_spy, wiring, sleep_calls


# ---------------------------------------------------------------------------
# Trigger 3 (boot-retry thread) — the #244 regression guard
# ---------------------------------------------------------------------------


def test_boot_retry_picks_up_post_boot_api_key():
    """The load-bearing #244 regression guard.

    Pre-fix: the daemon thread fetched the api_key ONCE before the loop and
    exited if it was None. An OAuth that completed milliseconds later was
    never picked up.

    Post-fix: the api_key is re-fetched on EVERY iteration, so the loop will
    catch up the moment the operator finishes Zerodha login.
    """
    # api_key is missing for the first two polls, then lands on the third.
    # ws is up by the time the api_key arrives (the same OAuth that wrote
    # the key also primes the broker session).
    pre, ensure_spy, wiring, sleep_calls = _wire(
        api_keys=[None, None, "VALID_KEY", "VALID_KEY"],
        ws_ups=[False, False, True, True],
    )

    wiring.establish()

    # Loop ran at least until the api_key + ws-up combination was true.
    assert ensure_spy.called, (
        "ensure() must be called once the post-boot api_key + ws-up combination is observed — "
        "this is the #244 fix (pre-fix the thread exited early on first None api_key)"
    )
    call_args = ensure_spy.call_args
    # Args: (user_id, broker, symbols). user_id comes from the test verifier.
    assert call_args.args[0] == "u_VALID_KEY"
    assert call_args.args[1] == "zerodha"
    assert list(call_args.args[2]) == ["RELIANCE", "INFY"]

    # Loop slept between iterations rather than busy-waiting.
    assert sleep_calls, "boot retry must sleep between iterations"


def test_boot_retry_exits_cleanly_after_deadline_with_no_api_key():
    """No api_key ever lands within max_wait_sec → the loop logs a warning
    and exits without raising. The other two triggers stay armed."""
    pre, ensure_spy, wiring, _ = _wire(
        api_keys=[None],
        ws_ups=[False],
        # time_seq ascends past max_wait_sec=2 quickly so the loop exits.
        time_seq=[0, 0, 1, 1, 3, 3, 3, 3],
        max_wait_sec=2,
        retry_sec=1,
    )
    # Must not raise.
    wiring.establish()
    # ensure() was never called (the api_key never appeared).
    ensure_spy.assert_not_called()


def test_boot_retry_first_iteration_succeeds_when_api_key_already_present():
    """At a fresh boot AFTER an earlier same-day login (e.g. the 15:21
    restart scenario), the api_key is already in auth_db. The boot loop
    should succeed on the FIRST iteration without sleeping."""
    pre, ensure_spy, wiring, sleep_calls = _wire(
        api_keys=["VALID_KEY"],
        ws_ups=[True],
    )

    wiring.establish()

    ensure_spy.assert_called_once()
    # First-iteration success means no sleep was needed.
    assert sleep_calls == [], "first-iteration success must not sleep"


# ---------------------------------------------------------------------------
# Trigger 2 (event-bus subscription) — covers post-deadline OAuth
# ---------------------------------------------------------------------------


def test_event_bus_subscription_triggers_ensure_on_session_refreshed():
    """Publishing ``BrokerSessionRefreshedEvent`` on the bus invokes the
    pre-subscribe ``ensure()`` immediately, without waiting on the boot
    retry's poll cadence (or even after its deadline)."""
    bus = _FakeBus()
    pre, ensure_spy, wiring, _ = _wire(
        api_keys=["VALID_KEY"],
        ws_ups=[True],
        bus=bus,
    )
    # The boot thread is not started (start_thread=False). Confirm the
    # event-bus path works in isolation.
    assert "broker_session_refreshed" in bus.subscriptions, (
        "wire_pre_subscribe must subscribe to broker_session_refreshed on the bus"
    )

    bus.publish(_FakeEvent())

    ensure_spy.assert_called_once()
    args = ensure_spy.call_args.args
    assert args[0] == "u_VALID_KEY"
    assert args[1] == "zerodha"


def test_event_bus_callback_handles_missing_api_key_silently():
    """If the event fires while the api_key is still missing (unlikely —
    notify_broker_session_refreshed is published AFTER upsert_auth — but
    defended for clock-skew / partial-failure paths), the bus callback
    must NOT raise back into the bus dispatcher."""
    bus = _FakeBus()
    pre, ensure_spy, wiring, _ = _wire(
        api_keys=[None],
        ws_ups=[False],
        bus=bus,
    )

    # Must not raise.
    bus.publish(_FakeEvent())

    ensure_spy.assert_not_called()


def test_event_bus_subscription_failure_does_not_block_other_triggers():
    """If the bus subscription itself raises (a corrupted bus, a transient
    issue), wire_pre_subscribe must still register the connect callback
    and start the boot retry — the other two triggers carry the load."""

    class FailingBus:
        def subscribe(self, *_args, **_kwargs):
            raise RuntimeError("bus is down")

    registered = []

    def register_callback(name, cb):
        registered.append((name, cb))

    # Must not raise despite the bus exception.
    pre, _ensure_spy, wiring, _ = _wire(
        api_keys=["VALID_KEY"],
        ws_ups=[True],
        register_callback=register_callback,
        bus=FailingBus(),
    )

    # Connect callback was still registered.
    assert len(registered) == 1
    assert registered[0][0] == "test_pre_subscribe"


# ---------------------------------------------------------------------------
# Trigger 1 (connect callback) — unchanged behaviour, regression-pin it
# ---------------------------------------------------------------------------


def test_connect_callback_registered_with_reset_true():
    """The persistent connect callback (fires on every WS connect+auth)
    must call ``pre_subscriber.ensure(..., reset=True)`` — broker-side
    subscriptions don't survive a reconnect."""
    registered = []

    def register_callback(name, cb):
        registered.append((name, cb))

    pre, ensure_spy, _wiring, _ = _wire(
        api_keys=["VALID_KEY"],
        ws_ups=[True],
        register_callback=register_callback,
    )

    assert len(registered) == 1
    name, cb = registered[0]
    assert name == "test_pre_subscribe"

    # Drive the callback as the WS client would: (user_id, broker).
    cb("u_VALID_KEY", "zerodha")

    ensure_spy.assert_called_once()
    kwargs = ensure_spy.call_args.kwargs
    assert kwargs.get("reset") is True, (
        "reset=True is load-bearing — a broker-side reconnect drops the "
        "subscriptions so they must be re-placed, not assumed live"
    )


# ---------------------------------------------------------------------------
# Cross-trigger: convergence on ensure() is idempotent
# ---------------------------------------------------------------------------


def test_all_three_triggers_can_fire_without_double_subscribing():
    """Convergence test: all three triggers can fire in any order; the same
    ``ensure()`` is reached every time and it is idempotent (the real
    PreSubscriber dedups by symbol). The spy here records every call, but
    the production ensure() would just no-op past the first."""
    registered = []
    bus = _FakeBus()

    def register_callback(name, cb):
        registered.append((name, cb))

    pre, ensure_spy, wiring, _ = _wire(
        api_keys=["VALID_KEY"],
        ws_ups=[True],
        register_callback=register_callback,
        bus=bus,
    )

    # Boot trigger
    wiring.establish()
    # Event-bus trigger
    bus.publish(_FakeEvent())
    # Connect-callback trigger
    registered[0][1]("u_VALID_KEY", "zerodha")

    assert ensure_spy.call_count == 3, (
        "every trigger must reach ensure() — the function de-dups, not the wiring"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
